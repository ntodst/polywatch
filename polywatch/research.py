"""Durable research evidence and a disclosed, conservative trader screen.

API backfill and locally observed account PnL deliberately live in separate
series. Cash balances and deposits are never used as returns.
"""
import hashlib
import json
import math
import time
from collections import defaultdict

from .models import ACCOUNT_SOURCE_TTL, account_status

DAY = 86400
SCREEN = {
    "observation_days": 90, "daily_samples": 60, "active_days": 30, "positive_days": .60,
    "closed_markets": 30, "paper_closes": 20, "paper_days": 30,
    "profile_max_age": 3600,
}
ARCHIVES = {
    "trades": ("trades", "timestamp", "wallet"),
    "alerts": ("alerts", "timestamp", "wallet"),
    "changes": ("position_events", "timestamp", "wallet"),
    "paper": ("paper_signals", "detected_at", "wallet"),
    "accounts": ("account_observations", "observed_at", "wallet"),
    "history": ("pnl_history", "observed_at", "wallet"),
    "journal": ("research_journal", "recorded_at", "wallet"),
}


def encoded(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False)


def initialize(store):
    store.db.executescript("""
        CREATE TABLE IF NOT EXISTS account_observations (
          wallet TEXT, observed_at REAL, recorded_at REAL, pnl REAL,
          realized REAL, unrealized REAL, raw TEXT,
          PRIMARY KEY(wallet, observed_at));
        CREATE INDEX IF NOT EXISTS account_observed ON account_observations(observed_at);
        CREATE TABLE IF NOT EXISTS pnl_history (
          wallet TEXT, observed_at REAL, recorded_at REAL, pnl REAL, raw TEXT,
          PRIMARY KEY(wallet, observed_at));
        CREATE TABLE IF NOT EXISTS research_journal (
          id INTEGER PRIMARY KEY, kind TEXT, wallet TEXT, entity TEXT,
          recorded_at REAL, raw TEXT);
        CREATE INDEX IF NOT EXISTS journal_wallet ON research_journal(wallet, recorded_at);
        CREATE INDEX IF NOT EXISTS journal_kind ON research_journal(kind, recorded_at);
        CREATE TABLE IF NOT EXISTS research_heads (
          kind TEXT, wallet TEXT, entity TEXT, digest TEXT, day INTEGER,
          PRIMARY KEY(kind, wallet, entity));
        CREATE INDEX IF NOT EXISTS alerts_wallet_time ON alerts(wallet, timestamp);
        CREATE INDEX IF NOT EXISTS events_wallet_time ON position_events(wallet, timestamp);
        CREATE INDEX IF NOT EXISTS paper_wallet_time ON paper_signals(wallet, detected_at);
    """)
    if store.state("research_started_at") is None:
        store.save_state("research_started_at", time.time())
    store.db.commit()


def journal(store, kind, wallet, entity, raw, daily_baseline=True):
    """Keep changed states and a daily baseline; skip identical polling noise."""
    now = time.time()
    ignored = {"verified_at", "checked_at", "opposite_verified_at"}
    content = {k: v for k, v in raw.items() if k not in ignored}
    digest = hashlib.sha256(encoded(content).encode()).hexdigest()
    day = int(now // DAY)
    head = store.db.execute(
        "SELECT digest,day FROM research_heads WHERE kind=? AND wallet=? AND entity=?",
        (kind, wallet, entity)).fetchone()
    if head and head[0] == digest and (not daily_baseline or head[1] == day):
        return
    store.db.execute("INSERT INTO research_journal(kind,wallet,entity,recorded_at,raw) VALUES(?,?,?,?,?)",
                     (kind, wallet, entity, now, encoded(raw)))
    store.db.execute("INSERT OR REPLACE INTO research_heads VALUES(?,?,?,?,?)",
                     (kind, wallet, entity, digest, day))


def observe_profile(store, address, profile):
    now = time.time()
    pnl, observed = profile.get("account_pnl"), profile.get("pnl_observed_at")
    realized, unrealized = profile.get("account_realized_pnl"), profile.get("account_unrealized_pnl")
    values = (pnl, observed, realized, unrealized)
    if all(isinstance(v, (float, int)) and math.isfinite(v) for v in values):
        if 0 <= now - observed <= ACCOUNT_SOURCE_TTL and abs(pnl - realized - unrealized) <= .0001:
            raw = {k: profile.get(k) for k in ("account_pnl", "account_realized_pnl", "account_unrealized_pnl",
                    "pnl_observed_at", "pnl_checked_at", "disqualified", "pnl_reason")}
            store.db.execute("""INSERT INTO account_observations VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(wallet,observed_at) DO UPDATE SET
                pnl=excluded.pnl,realized=excluded.realized,unrealized=excluded.unrealized,raw=excluded.raw""",
                (address, observed, now, pnl, realized, unrealized, encoded(raw)))
    # Preserve economics/qualification changes, without logging every check clock.
    keys = ("address", "name", "cohorts", "all_pnl", "month_pnl", "portfolio", "closed_markets",
            "closed_complete", "wins", "losses", "win_rate", "active_days", "positive_days",
            "unrealized_pnl", "consistent", "disqualified", "disqualified_at", "account_pnl",
            "account_realized_pnl", "account_unrealized_pnl", "pnl_observed_at", "pnl_reason")
    journal(store, "profile", address, address, {k: profile[k] for k in keys if k in profile})


def series_stats(points):
    changes = [b["pnl"] - a["pnl"] for a, b in zip(points, points[1:])
               if 20 * 3600 <= b["at"] - a["at"] <= 28 * 3600]
    active = [d for d in changes if abs(d) > .01]
    peak, drawdown = None, 0.0
    for p in points:
        peak = p["pnl"] if peak is None else max(peak, p["pnl"])
        drawdown = max(drawdown, peak - p["pnl"])
    return {
        "observation_days": (points[-1]["at"] - points[0]["at"]) / DAY if len(points) > 1 else 0,
        "daily_samples": len(changes), "active_days": len(active),
        "positive_days": sum(d > 0 for d in active) / len(active) if active else None,
        "net_pnl": points[-1]["pnl"] - points[0]["pnl"] if len(points) > 1 else None,
        "drawdown": drawdown if len(points) > 1 else None,
        "first_at": points[0]["at"] if points else None,
        "last_at": points[-1]["at"] if points else None,
    }


class Research:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def notice(self, message):
        self.store.db.execute("INSERT INTO research_journal(kind,wallet,entity,recorded_at,raw) VALUES(?,?,?,?,?)",
                              ("diagnostic", "", "scanner", time.time(), encoded({"message": message})))
        self.store.db.commit()

    def save_history(self, wallet, points):
        now = time.time()
        for point in points:
            at, pnl = point.get("timestamp"), point.get("position_pnl")
            if all(isinstance(v, (int, float)) and math.isfinite(v) for v in (at, pnl)) and 0 < at <= now + 60:
                self.store.db.execute("INSERT OR REPLACE INTO pnl_history VALUES(?,?,?,?,?)",
                                      (wallet, at, now, pnl, encoded(point)))
        self.store.db.commit()

    def save_closed(self, wallet, rows):
        for row in rows:
            token = str(row.get("token_id") or row.get("condition_id", ""))
            # Closed history is immutable in the normal case. Retain changes,
            # without duplicating thousands of old outcomes every day.
            journal(self.store, "closed_position", wallet, token, row, daily_baseline=False)
        self.store.db.commit()

    def daily(self, wallet=None):
        clause, args = (" WHERE wallet=?", (wallet,)) if wallet else ("", ())
        rows = self.store.db.execute("""SELECT wallet,observed_at,pnl FROM (
            SELECT wallet,observed_at,pnl,ROW_NUMBER() OVER (
                PARTITION BY wallet,CAST(observed_at/86400 AS INTEGER) ORDER BY observed_at DESC) AS n
            FROM account_observations""" + clause + ") WHERE n=1 ORDER BY wallet,observed_at", args)
        result = defaultdict(list)
        for row in rows:
            result[row["wallet"]].append({"at": row["observed_at"], "pnl": row["pnl"]})
        return result

    def paper_summary(self, wallet=None):
        clause, args = (" WHERE wallet=?", (wallet,)) if wallet else ("", ())
        rows = self.store.db.execute("""SELECT wallet,COUNT(*) AS signals,
            SUM(status='closed') AS closed,SUM(status='open') AS open,
            SUM(status='queued') AS queued,SUM(status='skipped') AS skipped,
            SUM(CASE WHEN status='closed' AND json_type(raw,'$.pnl') IN ('real','integer') THEN 1 ELSE 0 END) AS priced,
            SUM(CASE WHEN status='closed' THEN json_extract(raw,'$.pnl') END) AS pnl,
            SUM(CASE WHEN status='closed' AND json_extract(raw,'$.pnl')>0 THEN 1 ELSE 0 END) AS wins,
            MIN(CASE WHEN status='closed' THEN detected_at END) AS first_at,
            MAX(CASE WHEN status='closed' THEN json_extract(raw,'$.closed_at') END) AS last_at
            FROM paper_signals""" + clause + " GROUP BY wallet", args)
        return {r["wallet"]: {**dict(r), "days": max(0, ((r["last_at"] or 0) - (r["first_at"] or 0)) / DAY)} for r in rows}

    def traders(self):
        daily, paper = self.daily(), self.paper_summary()
        shortlist = set(self.store.state("web_shortlist", []))
        rows = []
        now = time.time()
        for address, profile in self.engine.wallets.items():
            local = series_stats(daily.get(address, []))
            simulated = paper.get(address, {"signals": 0, "closed": 0, "priced": 0, "open": 0,
                                            "queued": 0, "skipped": 0, "pnl": None, "wins": 0, "days": 0})
            eligible = account_status(profile, now) == "eligible"
            checks = [
                {"label": "Fresh, nonnegative overall account PnL", "pass": eligible},
                {"label": "Historical analysis refreshed within 1 hour", "pass": now - profile.get("updated", 0) <= SCREEN["profile_max_age"]},
                {"label": "90 days of locally observed account PnL", "pass": local["observation_days"] >= SCREEN["observation_days"]},
                {"label": "60 covered daily comparisons", "pass": local["daily_samples"] >= SCREEN["daily_samples"]},
                {"label": "Positive observed PnL; 60% positive across at least 30 active days", "pass": (local["net_pnl"] or 0) > 0 and local["active_days"] >= SCREEN["active_days"] and (local["positive_days"] or 0) >= SCREEN["positive_days"]},
                {"label": "Complete closed history, 30 markets, positive all-time and monthly PnL", "pass": bool(profile.get("consistent"))},
                {"label": "20 priced simulated closes across 30 days, positive net result", "pass": simulated["priced"] >= SCREEN["paper_closes"] and simulated["priced"] == simulated["closed"] and simulated["days"] >= SCREEN["paper_days"] and (simulated["pnl"] or 0) > 0},
            ]
            passed = sum(c["pass"] for c in checks)
            stage = "long_term" if passed == len(checks) else "building"
            if not eligible:
                stage = "excluded" if profile.get("disqualified") else "pending"
            rows.append({
                "address": address, "name": profile.get("name") or address[:10],
                "account_pnl": profile.get("account_pnl"), "all_pnl": profile.get("all_pnl"),
                "month_pnl": profile.get("month_pnl"), "win_rate": profile.get("win_rate"),
                "closed_markets": profile.get("closed_markets"), "closed_complete": profile.get("closed_complete"),
                "consistent": bool(profile.get("consistent")), "updated": profile.get("updated"),
                "pnl_observed_at": profile.get("pnl_observed_at"), "pnl_reason": profile.get("pnl_reason"),
                "stage": stage, "checks": checks, "passed": passed, "local": local, "paper": simulated,
                "shortlisted": address in shortlist,
            })
        rows.sort(key=lambda r: (r["stage"] == "long_term", r["stage"] == "building", r["passed"],
                                  r["local"]["daily_samples"], r["paper"]["pnl"] or 0, r["account_pnl"] or 0), reverse=True)
        return rows

    def detail(self, wallet):
        points = self.daily(wallet).get(wallet, [])
        imported = [dict(r) for r in self.store.db.execute(
            "SELECT observed_at AS at,pnl FROM pnl_history WHERE wallet=? ORDER BY observed_at DESC LIMIT 3660", (wallet,))]
        papers = [json.loads(r[0]) for r in self.store.db.execute(
            "SELECT raw FROM paper_signals WHERE wallet=? ORDER BY detected_at DESC LIMIT 100", (wallet,))]
        return {"series": points, "backfill": list(reversed(imported)), "paper_records": papers,
                "positions": self.engine.positions.wallet_rows(wallet) if self.engine.allowed(wallet) else [],
                "audit_only": self.engine.excluded(wallet)}

    def archive(self, kind, wallet="", offset=0, limit=50):
        table, clock, owner = ARCHIVES[kind]
        clause, args = (f" WHERE {owner}=?", [wallet]) if wallet else ("", [])
        # Untracked global rows are the short-lived aggregation buffer.
        if kind == "trades":
            clause += (" AND " if clause else " WHERE ") + "retained=1"
        total = self.store.db.execute(f"SELECT COUNT(*) FROM {table}" + clause, args).fetchone()[0]
        rows = self.store.db.execute(f"SELECT * FROM {table}" + clause + f" ORDER BY {clock} DESC LIMIT ? OFFSET ?",
                                    (*args, limit, offset))
        records = []
        for r in rows:
            meta = dict(r)
            raw = json.loads(meta.pop("raw"))
            records.append({"meta": meta, "data": raw})
        return {"records": records, "total": total, "offset": offset, "limit": limit}

    def storage(self):
        counts = {key: self.store.db.execute(f"SELECT COUNT(*) FROM {table}" + (" WHERE retained=1" if key == "trades" else "")).fetchone()[0]
                  for key, (table, _, _) in ARCHIVES.items()}
        page_count = self.store.db.execute("PRAGMA page_count").fetchone()[0]
        page_size = self.store.db.execute("PRAGMA page_size").fetchone()[0]
        return {"counts": counts, "database_bytes": page_count * page_size,
                "started_at": self.store.state("research_started_at"), "retention": "Permanent research history"}
