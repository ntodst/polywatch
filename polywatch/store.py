import json
import sqlite3
import time
from pathlib import Path

from .models import Trade, compact_position, compact_market


class Store:
    def __init__(self, path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA busy_timeout=5000;
            PRAGMA cache_size=-8192;
            CREATE TABLE IF NOT EXISTS trades (
              id TEXT PRIMARY KEY, wallet TEXT, timestamp INTEGER, amount REAL,
              token TEXT, side TEXT, retained INTEGER, raw TEXT);
            CREATE INDEX IF NOT EXISTS trade_time ON trades(timestamp);
            CREATE INDEX IF NOT EXISTS trade_wallet ON trades(wallet, token, side, timestamp);
            CREATE INDEX IF NOT EXISTS trade_token ON trades(token, side, timestamp);
            CREATE TABLE IF NOT EXISTS alerts (
              id TEXT PRIMARY KEY, kind TEXT, timestamp INTEGER, wallet TEXT, raw TEXT);
            CREATE TABLE IF NOT EXISTS wallets (address TEXT PRIMARY KEY, raw TEXT);
            CREATE TABLE IF NOT EXISTS state (name TEXT PRIMARY KEY, raw TEXT);
            CREATE TABLE IF NOT EXISTS position_snapshots (
              wallet TEXT, token TEXT, raw TEXT, PRIMARY KEY (wallet, token));
            CREATE TABLE IF NOT EXISTS position_checks (wallet TEXT PRIMARY KEY, raw TEXT);
            CREATE TABLE IF NOT EXISTS market_checks (condition TEXT PRIMARY KEY, raw TEXT);
            CREATE TABLE IF NOT EXISTS position_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp REAL, wallet TEXT,
              token TEXT, kind TEXT, raw TEXT);
            CREATE INDEX IF NOT EXISTS events_time ON position_events(timestamp);
            CREATE INDEX IF NOT EXISTS snapshot_value ON position_snapshots(wallet, CAST(json_extract(raw,'$.current_value') AS REAL) DESC);
            CREATE TABLE IF NOT EXISTS paper_signals (
              id TEXT PRIMARY KEY, wallet TEXT, token TEXT, condition TEXT,
              detected_at REAL, status TEXT, raw TEXT);
            CREATE INDEX IF NOT EXISTS paper_status ON paper_signals(status, detected_at);
        """)

    def close(self):
        self.db.commit()
        self.db.close()

    def state(self, name, default=None):
        row = self.db.execute("SELECT raw FROM state WHERE name=?", (name,)).fetchone()
        return json.loads(row[0]) if row else default

    def save_state(self, name, value):
        self.db.execute("INSERT OR REPLACE INTO state VALUES (?, ?)", (name, json.dumps(value)))

    def wallets(self):
        return {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT * FROM wallets")}

    def wallet(self, address, profile):
        self.db.execute("INSERT OR REPLACE INTO wallets VALUES (?, ?)", (address, json.dumps(profile)))
        self.db.commit()

    def alert(self, key, kind, timestamp, wallet, raw):
        return self.db.execute("INSERT OR IGNORE INTO alerts VALUES (?, ?, ?, ?, ?)",
                               (key, kind, timestamp, wallet, json.dumps(raw))).rowcount == 1

    def ingest(self, rows, tracked, threshold, profitable=(), excluded=()):
        inserted, new_alerts = 0, []
        self.last_inserted = []
        for raw in sorted(rows, key=lambda r: r.get("timestamp", 0)):
            if str(raw.get("proxy_wallet", "")).lower() in excluded:
                continue
            trade = Trade.parse(raw)
            retained = trade.amount >= threshold or trade.wallet in tracked
            cur = self.db.execute("INSERT OR IGNORE INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                  (trade.key, trade.wallet, trade.timestamp, trade.amount, raw["token_id"],
                                   raw["side"], int(retained), json.dumps(raw)))
            if not cur.rowcount:
                continue
            inserted += 1
            self.last_inserted.append(trade)
            if trade.amount >= threshold:
                if self.alert("big:" + trade.key, "BIG BET", trade.timestamp, trade.wallet, raw):
                    new_alerts.append(("BIG BET", trade))
            if raw["side"] == "BUY":
                # Evaluate at the newest event time for this wallet/outcome,
                # even when a delayed page arrives out of order.
                newest = self.db.execute("SELECT MAX(timestamp) FROM trades WHERE wallet=? AND token=? AND side='BUY'",
                                         (trade.wallet, raw["token_id"])).fetchone()[0]
                total, count = self.db.execute("SELECT SUM(amount), COUNT(*) FROM trades WHERE wallet=? AND token=? AND side='BUY' AND timestamp BETWEEN ? AND ?",
                                               (trade.wallet, raw["token_id"], newest - 300, newest)).fetchone()
                last = self.state("bundle:" + trade.wallet + ":" + raw["token_id"], 0)
                if count >= 2 and total >= threshold and newest - last >= 300:
                    details = {**raw, "bundle_amount": total, "bundle_count": count}
                    key = f"bundle:{trade.wallet}:{raw['token_id']}:{newest}"
                    if self.alert(key, "ACCUMULATION", newest, trade.wallet, details):
                        new_alerts.append(("ACCUMULATION", trade))
                        self.save_state("bundle:" + trade.wallet + ":" + raw["token_id"], newest)
                if trade.wallet in profitable and len(profitable) >= 2:
                    placeholders = ",".join("?" for _ in profitable)
                    buyers = self.db.execute(
                        f"SELECT wallet, SUM(amount) AS cash FROM trades WHERE token=? AND side='BUY' AND timestamp BETWEEN ? AND ? AND wallet IN ({placeholders}) GROUP BY wallet HAVING SUM(amount)>=?",
                        (raw["token_id"], newest - 300, newest, *profitable, threshold)).fetchall()
                    last_consensus = self.state("consensus:" + raw["token_id"], 0)
                    if len(buyers) >= 2 and newest - last_consensus >= 300:
                        details = {**raw, "bundle_amount": sum(r["cash"] for r in buyers),
                                   "consensus_wallets": [r["wallet"] for r in buyers]}
                        if self.alert(f"consensus:{raw['token_id']}:{newest}", "CO-BUY", newest, trade.wallet, details):
                            new_alerts.append(("CO-BUY", trade))
                            self.save_state("consensus:" + raw["token_id"], newest)
        self.db.commit()
        return inserted, new_alerts

    def recent(self, limit=50, alerts=False, wallets=None):
        clause, params = "", []
        if wallets is not None:
            if not wallets:
                return []
            params = sorted(wallets)
            clause = " AND wallet IN (" + ",".join("?" for _ in params) + ")"
        if alerts:
            rows = self.db.execute("SELECT kind, timestamp, wallet, raw FROM alerts WHERE 1=1" + clause + " ORDER BY timestamp DESC LIMIT ?", (*params, limit))
            return [{**json.loads(r["raw"]), "alert_kind": r["kind"], "timestamp": r["timestamp"]} for r in rows]
        rows = self.db.execute("SELECT raw FROM trades WHERE retained=1" + clause + " ORDER BY timestamp DESC LIMIT ?", (*params, limit))
        return [json.loads(r[0]) for r in rows]

    def prune(self, retention_days=7):
        now = time.time()
        self.db.execute("DELETE FROM trades WHERE (retained=0 AND timestamp < ?) OR timestamp < ?", (now - 900, now - retention_days * 86400))
        self.db.execute("DELETE FROM alerts WHERE timestamp < ?", (now - retention_days * 86400,))
        self.db.execute("DELETE FROM position_events WHERE timestamp < ?", (now - retention_days * 86400,))
        self.db.execute("DELETE FROM paper_signals WHERE status NOT IN ('open','queued') AND detected_at < ?", (now - 30 * 86400,))
        self.db.commit()

    def load_positions(self, wallet=None, min_value=None, limit=500):
        sql, params = "SELECT * FROM position_snapshots", []
        if wallet is not None:
            sql += " WHERE wallet=?"
            params.append(wallet)
            if min_value is not None:
                sql += " AND CAST(json_extract(raw,'$.current_value') AS REAL)>=?"
                params.append(min_value)
            sql += " ORDER BY CAST(json_extract(raw,'$.current_value') AS REAL) DESC LIMIT ?"
            params.append(limit)
        return {(r[0], r[1]): compact_position(json.loads(r[2])) for r in self.db.execute(sql, params)}

    def position(self, wallet, token):
        row = self.db.execute("SELECT raw FROM position_snapshots WHERE wallet=? AND token=?", (wallet, token)).fetchone()
        return compact_position(json.loads(row[0])) if row else None

    def save_position(self, wallet, token, row):
        self.db.execute("INSERT OR REPLACE INTO position_snapshots VALUES (?, ?, ?)", (wallet, token, json.dumps(compact_position(row), separators=(",", ":"))))

    def load_checks(self, table):
        if table not in ("position_checks", "market_checks"):
            raise ValueError("Unknown check table")
        return {r[0]: compact_market(json.loads(r[1])) if table == "market_checks" else json.loads(r[1]) for r in self.db.execute(f"SELECT * FROM {table}")}

    def save_check(self, table, key, value):
        if table not in ("position_checks", "market_checks"):
            raise ValueError("Unknown check table")
        self.db.execute(f"INSERT OR REPLACE INTO {table} VALUES (?, ?)", (key, json.dumps(value)))

    def position_event(self, kind, row, previous_size, now):
        event = {**row, "kind": kind, "previous_size": previous_size, "detected_at": now}
        self.db.execute("INSERT INTO position_events(timestamp,wallet,token,kind,raw) VALUES(?,?,?,?,?)",
                        (now, row["proxy_wallet"], row["token_id"], kind, json.dumps(event)))

    def position_events(self, limit=50, wallets=None):
        clause, params = "", []
        if wallets is not None:
            if not wallets:
                return []
            params = sorted(wallets)
            clause = " WHERE wallet IN (" + ",".join("?" for _ in params) + ")"
        return [json.loads(r[0]) for r in self.db.execute("SELECT raw FROM position_events" + clause + " ORDER BY id DESC LIMIT ?", (*params, limit))]

    def save_paper(self, record, commit=True):
        self.db.execute("INSERT OR REPLACE INTO paper_signals VALUES(?,?,?,?,?,?,?)",
                        (record["id"], record["wallet"], record["token"], record["condition"], record["detected_at"], record["status"], json.dumps(record)))
        if commit:
            self.db.commit()

    def papers(self, statuses=None, limit=200):
        clause, params = "", []
        if statuses:
            clause = " WHERE status IN (" + ",".join("?" for _ in statuses) + ")"
            params.extend(statuses)
        return [json.loads(r[0]) for r in self.db.execute("SELECT raw FROM paper_signals" + clause + " ORDER BY detected_at DESC LIMIT ?", (*params, limit))]
