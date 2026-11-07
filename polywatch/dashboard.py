"""One reusable dashboard snapshot per refresh, with stable logical row keys."""
import time


def aggregate(engine, positions):
    groups = {}
    for row in positions:
        wallet = row["proxy_wallet"]
        if not engine.allowed(wallet):
            continue
        slot = row.get("outcome_index")
        slot = str(slot) if slot in (0, 1) else row.get("outcome", row["token_id"])
        key = row["condition_id"] + ":" + slot
        group = groups.setdefault(key, {"id": key, "title": row.get("title") or row["condition_id"],
            "outcome": row.get("outcome") or "?", "condition_id": row["condition_id"],
            "token_id": row["token_id"], "outcome_index": row.get("outcome_index"),
            "held": 0.0, "positions": [], "wallets": set(), "tokens": set(), "flow": 0.0,
            "change_at": 0, "verified_at": row["verified_at"]})
        group["held"] += row["net_value"]
        group["positions"].append(row)
        group["wallets"].add(wallet)
        group["tokens"].add(row["token_id"])
        group["change_at"] = max(group["change_at"], row.get("change_at", 0) if row.get("last_change") not in ("FIRST SEEN", "BASELINE") else row.get("last_event_at", 0))
        group["verified_at"] = min(group["verified_at"], row["verified_at"])
    for group in groups.values():
        group["holders"] = len(group["wallets"])
        group["profitable"] = sum((engine.wallets[w].get("account_pnl") or 0) > 0 for w in group["wallets"])
        group["profit"] = sum(engine.wallets[w].get("account_pnl") or 0 for w in group["wallets"])
        group["category"] = engine.positions.markets.get(group["condition_id"], {}).get("category") or "Other"
    return list(groups.values())


class Dashboard:
    def __init__(self, engine):
        self.engine = engine
        self.frozen = None
        self.flow_at = 0
        self.flows = {}

    def freeze(self):
        self.frozen = None if self.frozen is not None else [dict(p) for p in self.engine.positions.active()]

    def markets(self):
        engine, prefs = self.engine, self.engine.preferences
        now = time.time()
        positions = self.frozen if self.frozen is not None else engine.positions.active()
        rows = aggregate(engine, positions)
        if now - self.flow_at >= 10:
            self.flows = {}
            allowed = engine.eligible_wallets()
            if allowed:
                params = sorted(allowed)
                sql = "SELECT token,SUM((json_extract(raw,'$.current_size')-json_extract(raw,'$.previous_size'))*json_extract(raw,'$.current_price')) FROM position_events WHERE timestamp>=? AND kind IN ('OPENED','ADDED','REDUCED','EXITED') AND wallet IN (" + ",".join("?" for _ in params) + ") GROUP BY token"
                self.flows = dict(engine.store.db.execute(sql, (now - 3600, *params)).fetchall())
            self.flow_at = now
        query = prefs["query"].casefold()
        pins = set(prefs["pins"])
        pinned_ids = {c["id"] for t, c in prefs.get("pin_contexts", {}).items() if t in pins}
        ids = {r["id"] for r in rows}
        for token, context in prefs.get("pin_contexts", {}).items():
            if token in pins and context["id"] not in ids:
                rows.append({**context, "held": 0, "wallets": set(), "tokens": {token}, "positions": [],
                             "holders": 0, "profitable": 0, "profit": 0, "change_at": 0, "verified_at": 0, "watch_only": True})
                ids.add(context["id"])
        result = []
        for row in rows:
            row["pinned"] = bool(row["tokens"] & pins) or row["id"] in pinned_ids
            row["flow"] = sum(self.flows.get(t) or 0 for t in row["tokens"])
            if not row.get("watch_only") and (row["held"] < prefs["min_value"] or row["holders"] < prefs["min_holders"]):
                continue
            if prefs["pinned_only"] and not row["pinned"]:
                continue
            if prefs["category"] != "All" and row["category"].casefold() != prefs["category"].casefold():
                continue
            if not row.get("watch_only") and prefs["recent_minutes"] and now - row["change_at"] > prefs["recent_minutes"] * 60:
                continue
            searchable = " ".join([row["title"], row["outcome"], row["category"], *[engine.wallets[w].get("name") or w for w in row["wallets"]]]).casefold()
            if query and query not in searchable:
                continue
            result.append(row)
        field = {"exposure": "held", "holders": "holders", "change": "change_at", "profit": "profit"}[prefs["sort"]]
        return sorted(result, key=lambda r: (r["pinned"], r[field]), reverse=True)

    def wallets(self, markets=None):
        # Reuse the single active-book snapshot instead of traversing it per row.
        values = {}
        for group in self.markets() if markets is None else markets:
            for row in group["positions"]:
                wallet = row["proxy_wallet"]
                values[wallet] = values.get(wallet, 0) + row["net_value"]
        rows = [{**self.engine.wallets[w], "active_value": value, "id": w} for w, value in values.items()]
        return sorted(rows, key=lambda r: r.get("account_pnl") or 0, reverse=True)

    def alerts(self):
        rows = []
        for r in self.engine.position_changes(100):
            if r["kind"] == "FIRST SEEN":
                continue
            row = {**r, "id": f"change:{r['proxy_wallet']}:{r['token_id']}:{r['detected_at']}", "at": r["detected_at"]}
            rows.append(row)
        for r in self.engine.recent(alerts=True, limit=100):
            rows.append({**r, "id": f"trade:{r['proxy_wallet']}:{r['transaction_hash']}:{r.get('alert_kind')}",
                         "at": r["timestamp"], "kind": r.get("alert_kind", "TRADE")})
        query = self.engine.preferences["query"].casefold()
        return sorted([r for r in rows if not query or query in (r.get("title", "") + " " + r.get("outcome", "")).casefold()], key=lambda r: r["at"], reverse=True)
