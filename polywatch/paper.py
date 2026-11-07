"""Prospective simulations, entered only using post-detection executable depth."""
import asyncio
import time

from .execution import estimate


class Paper:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.records = {r["id"]: r for r in self.store.papers(("queued", "open"), limit=125)}
        self.started = time.time()
        self.enabled = True

    def capture(self, kind, trade):
        now = time.time()
        if not self.enabled or not self.engine.allowed(trade.wallet) or trade.raw["side"] != "BUY" or now - trade.timestamp > 120:
            return
        key = "paper:" + trade.key
        if key in self.records or self.store.db.execute("SELECT 1 FROM paper_signals WHERE id=?", (key,)).fetchone():
            return
        profile = self.engine.wallets[trade.wallet]
        record = {"id": key, "wallet": trade.wallet, "token": trade.raw["token_id"],
                  "condition": trade.raw["condition_id"], "outcome_index": trade.raw.get("outcome_index"),
                  "title": trade.raw.get("title"), "outcome": trade.raw.get("outcome"), "signal": kind,
                  "detected_at": now, "trade_timestamp": trade.timestamp, "source_price": trade.raw["price"],
                  "qualification": {k: profile.get(k) for k in ("account_pnl", "pnl_observed_at", "pnl_checked_at")},
                  "budget": self.engine.preferences.get("size", 1000), "status": "queued", "marks": {}}
        if any(r["wallet"] == trade.wallet and r["token"] == record["token"] and r["status"] in ("open", "queued") for r in self.records.values()):
            record.update(status="skipped", reason="Existing paper position in this wallet/outcome")
        elif sum(r["status"] == "open" for r in self.records.values()) >= 25 or len(self.records) >= 125:
            record.update(status="skipped", reason="Paper capacity reached")
        self.store.save_paper(record)
        if record["status"] == "queued":
            self.records[key] = record
            self.engine.execution.watch(record["token"], {"condition_id": record["condition"], "outcome_index": record["outcome_index"]}, urgent=True)

    def close_source(self, wallet, token):
        for record in self.records.values():
            if record["wallet"] == wallet and record["token"] == token and record["status"] == "queued":
                record.update(status="skipped", reason="Source exited before a prospective entry was available")
                self.store.save_paper(record)
            elif record["wallet"] == wallet and record["token"] == token and record["status"] == "open":
                record["close_requested"] = "Confirmed source exit"
                record["close_requested_at"] = time.time()
                self.engine.execution.watch(token, {"condition_id": record["condition"], "outcome_index": record.get("outcome_index")}, urgent=True)
                self.store.save_paper(record)

    def step(self, now=None):
        now = time.time() if now is None else now
        for key, r in list(self.records.items()):
            before = (r["status"], r.get("entry_at"), r.get("mark_at"), r.get("close_requested"),
                      r.get("close_requested_at"), r.get("mark_warning"), r.get("pnl"), len(r["marks"]))
            book = self.engine.execution.books.get(r["token"])
            fresh = book and 0 <= now - book["checked_at"] <= 10
            if r["status"] == "queued":
                if not self.engine.allowed(r["wallet"]):
                    r.update(status="skipped", reason="Source no longer admitted")
                elif now - r["detected_at"] > 45:
                    r.update(status="skipped", reason="Fresh executable quote/fees unavailable within 45 seconds")
                elif fresh and book.get("accepting") is False:
                    r.update(status="skipped", reason="Market is not accepting orders")
                elif sum(other["status"] == "open" for other in self.records.values()) >= 25:
                    r.update(status="skipped", reason="Paper open-position capacity reached")
                elif fresh and book["checked_at"] >= r["detected_at"] and book.get("accepting"):
                    fill = estimate(book, budget=r["budget"])
                    if fill["complete"]:
                        r.update(status="open", entry_at=book["checked_at"], entry=fill,
                                 entry_asset=book["asset_id"], latency=book["checked_at"] - r["detected_at"],
                                 worst_pnl=0.0, best_pnl=0.0)
                    elif fill["status"] != "fees_unknown":
                        r.update(status="skipped", reason="Entry " + fill["status"], entry_estimate=fill)
            elif r["status"] == "open":
                if self.engine.excluded(r["wallet"]):
                    if not r.get("close_requested"):
                        r["close_requested"] = "Source disqualified; no further wallet tracking"
                        r["close_requested_at"] = now
                        self.engine.execution.watch(r["token"], {"condition_id": r["condition"], "outcome_index": r.get("outcome_index")}, urgent=True)
                if now - r["entry_at"] >= 86400:
                    if not r.get("close_requested"):
                        r["close_requested"] = "24-hour horizon"
                        r["close_requested_at"] = r["entry_at"] + 86400
                if fresh and book["checked_at"] > r.get("mark_at", 0):
                    exit_fill = estimate(book, shares=r["entry"]["shares"])
                    if exit_fill["complete"]:
                        r.pop("mark_warning", None)
                        pnl = exit_fill["cash"] - r["entry"]["cash"]
                        r.update(mark_at=book["checked_at"], mark=exit_fill, pnl=pnl,
                                 worst_pnl=min(r["worst_pnl"], pnl), best_pnl=max(r["best_pnl"], pnl))
                        for horizon in (300, 3600, 21600, 86400):
                            if book["checked_at"] - r["entry_at"] >= horizon and str(horizon) not in r["marks"]:
                                r["marks"][str(horizon)] = {"observed_at": book["checked_at"], "pnl": pnl}
                        if r.get("close_requested") and book["checked_at"] >= r.get("close_requested_at", now):
                            r.update(status="closed", closed_at=book["checked_at"], reason=r["close_requested"])
                    else:
                        r["pnl"] = None
                        r["mark_warning"] = "Full exit currently unpriced: " + exit_fill["status"]
            after = (r["status"], r.get("entry_at"), r.get("mark_at"), r.get("close_requested"),
                     r.get("close_requested_at"), r.get("mark_warning"), r.get("pnl"), len(r["marks"]))
            if before != after:
                self.store.save_paper(r, commit=False)
            if r["status"] not in ("open", "queued"):
                del self.records[key]
        self.store.db.commit()

    async def settlement(self, r):
        response = await self.engine.api.get("/v2/tokens", token_id=r["entry_asset"], cache_bust=False)
        token = next((t for t in response["data"] if str(t.get("token_id")) == r["entry_asset"]), None)
        if token and token.get("resolved") is True and token.get("final_price") is not None:
            price = float(token["final_price"])
            if 0 <= price <= 1:
                r.update(status="closed", closed_at=time.time(), reason="Resolved payout (simulated redemption)",
                         pnl=r["entry"]["shares"] * price - r["entry"]["cash"], settlement_price=price)
                self.store.save_paper(r)
                return True
        return False

    async def run(self):
        while True:
            for r in list(self.records.values()):
                self.engine.execution.watch(r["token"], {"condition_id": r["condition"], "outcome_index": r.get("outcome_index")})
                if r["status"] == "open" and r["token"] not in self.engine.execution.books and time.time() >= r.get("settlement_check", 0):
                    r["settlement_check"] = time.time() + 60
                    try:
                        if await self.settlement(r):
                            self.records.pop(r["id"], None)
                    except Exception:
                        pass
            self.step()
            await asyncio.sleep(2)
