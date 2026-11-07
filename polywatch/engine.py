import asyncio
import time
from dataclasses import dataclass, asdict

from .models import closed_summary, consistency, number, account_status, ACCOUNT_SOURCE_TTL
from .positions import Positions
from .runtime import preferences
from .execution import Execution
from .paper import Paper


@dataclass
class Feed:
    name: str
    last_ok: float = 0
    latest: int = 0
    pages: int = 0
    seen: int = 0
    backlog: bool = False
    error: str = ""


class Engine:
    def __init__(self, api, store, threshold=10000, interval=1, wallet_count=50):
        self.api, self.store = api, store
        self.threshold, self.interval, self.wallet_count = threshold, interval, wallet_count
        self.wallets = store.wallets()
        self.feeds = {name: Feed(name) for name in ("all trades", "big trades")}
        self.notices = []
        self.status = None
        self.status_time = 0
        self.board_time = 0
        self.queue = asyncio.Queue(maxsize=200)
        self.queued = set()
        self.selected = None
        self.started = time.time()
        self.positions = Positions(self)
        self.account_locks = {}
        self.preferences = preferences(store)
        if store.state("ui_preferences") is None:
            self.preferences["min_value"] = threshold
        self.execution = Execution(self)
        self.paper = Paper(self)

    def allowed(self, wallet):
        return account_status(self.wallets.get(wallet.lower(), {})) == "eligible"

    def excluded(self, wallet):
        return account_status(self.wallets.get(wallet.lower(), {})) == "excluded"

    def eligible_wallets(self):
        return {a for a in self.wallets if self.allowed(a)}

    def recent(self, limit=50, alerts=False):
        return self.store.recent(limit, alerts, wallets=self.eligible_wallets())

    def position_changes(self, limit=50):
        return self.store.position_events(limit, wallets=self.eligible_wallets())

    async def check_account(self, wallet, force=False):
        wallet = wallet.lower()
        lock = self.account_locks.setdefault(wallet, asyncio.Lock())
        async with lock:
            p = self.wallets.setdefault(wallet, {"address": wallet, "cohorts": []})
            if self.excluded(wallet):
                return False
            now = time.time()
            if not force and now < p.get("pnl_next_check", 0):
                return self.allowed(wallet)
            try:
                # This endpoint rejects unknown query parameters, including '_'.
                response = await self.api.get("/v2/user-stats", user=wallet, cache_bust=False)
                p = self.wallets.get(wallet, p)
                point = (response["data"] or {}).get("all_time_pnl") or {}
                realized, unrealized = number(point.get("realized_pnl")), number(point.get("unrealized_pnl"))
                observed = int(point.get("timestamp") or 0)
                overall = number(point.get("position_pnl"))
                if realized is None or unrealized is None or overall is None or not observed:
                    p.update(account_pnl=None, pnl_reason="Overall PnL unavailable; awaiting complete account data")
                elif abs(overall - (realized + unrealized)) > .0001:
                    p.update(account_pnl=None, pnl_reason="Inconsistent account PnL; awaiting verification")
                elif now - observed > ACCOUNT_SOURCE_TTL or observed > now + 60:
                    p.update(account_pnl=None, pnl_reason="Account PnL observation is stale or has an invalid timestamp")
                else:
                    p.update(account_pnl=overall, account_realized_pnl=realized,
                             account_unrealized_pnl=unrealized, pnl_observed_at=observed,
                             pnl_reason="Overall position PnL = realized + unrealized")
                    if overall < 0:
                        p.update(disqualified=True, disqualified_at=now,
                                 pnl_reason="Excluded: negative overall account PnL",
                                 disqualified_cohorts=list(p.get("cohorts", [])), cohorts=[])
                        for key in list(self.positions.pending):
                            if key[0] == wallet:
                                del self.positions.pending[key]
                        self.queued.discard(wallet)
                        if self.selected == wallet:
                            self.selected = None
                p.update(pnl_checked_at=time.time(), pnl_next_check=time.time() + 60)
            except Exception as exc:
                p = self.wallets.get(wallet, p)
                p["pnl_next_check"] = time.time() + 60
                p["pnl_reason"] = "PnL check failed: " + str(exc)
                self.note(f"Account PnL {wallet[:10]}: {exc}")
            self.store.wallet(wallet, p)
            return self.allowed(wallet)

    async def accounts(self):
        while True:
            now = time.time()
            candidates = [a for a, p in self.wallets.items() if p.get("cohorts") and not self.excluded(a)
                          and now >= p.get("pnl_next_check", 0)]
            if candidates:
                candidates.sort(key=lambda a: (self.allowed(a), self.wallets[a].get("pnl_checked_at", 0)))
                await self.check_account(candidates[0])
            else:
                await asyncio.sleep(.2)

    def note(self, message):
        self.notices = (self.notices + [message])[-5:]

    def enqueue(self, wallet):
        if self.excluded(wallet):
            return
        if wallet not in self.queued and not self.queue.full():
            self.queued.add(wallet)
            self.queue.put_nowait(wallet)

    def ingest(self, rows):
        tracked = {a for a, p in self.wallets.items() if p.get("cohorts") and self.allowed(a)}
        excluded = {a for a in self.wallets if self.excluded(a)}
        inserted, alerts = self.store.ingest(rows, tracked, self.threshold, excluded=excluded)
        for kind, trade in alerts:
            p = self.wallets.setdefault(trade.wallet, {"address": trade.wallet,
                    "name": trade.raw.get("name") or trade.raw.get("pseudonym"), "cohorts": []})
            if "observed whale" not in p["cohorts"]:
                p["cohorts"].append("observed whale")
                self.enqueue(trade.wallet)
                self.store.wallet(trade.wallet, p)
            self.paper.capture(kind, trade)
        impacted = {}
        for trade in self.store.last_inserted:
            if self.wallets.get(trade.wallet, {}).get("cohorts") and self.allowed(trade.wallet):
                key = (trade.wallet, trade.raw["condition_id"], trade.raw["token_id"])
                if key not in impacted or impacted[key]["timestamp"] < trade.timestamp:
                    impacted[key] = trade.raw
        for (wallet, condition, _), trade in impacted.items():
            self.positions.request(wallet, condition, trade)
        return inserted

    async def poll(self, name, big=False):
        feed = self.feeds[name]
        # Reopen the old state after a restart; do not silently discard downtime.
        state_key = "feed:" + name + (f":{self.threshold}" if big else "")
        state = self.store.state(state_key, {"watermark": int(time.time()) - (300 if big else 30)})
        while True:
            started = time.monotonic()
            try:
                if not state.get("cursor"):
                    state["sweep_top"] = state["watermark"]
                # Bound work per turn while retaining the deep-page cursor.
                # The independent big-trade poller continues while this catches up.
                for _ in range(8):
                    params = {"limit": 1000, "taker_only": False,
                              "filter_type": "CASH", "filter_amount": self.threshold if big else .000001,
                              "cursor": state.get("cursor")}
                    page = await self.api.get("/v2/trades", **params)
                    rows = page["data"]
                    if not isinstance(rows, list):
                        raise ValueError("Unexpected trades response")
                    eligible = [r for r in rows if r["timestamp"] >= state["watermark"] - 30]
                    feed.seen += self.ingest(eligible)
                    feed.pages += 1
                    if rows:
                        newest = max(r["timestamp"] for r in rows)
                        feed.latest = max(feed.latest, newest)
                        state["sweep_top"] = max(state["sweep_top"], newest)
                    cursor = page.get("pagination", {}).get("next_cursor")
                    # Re-read 30 seconds to cover equal timestamps and late indexing.
                    reached = not rows or min(r["timestamp"] for r in rows) < state["watermark"] - 30
                    if reached or not cursor:
                        state = {"watermark": state["sweep_top"]}
                        feed.backlog = False
                    else:
                        state["cursor"] = cursor
                        feed.backlog = True
                    self.store.save_state(state_key, state)
                    self.store.db.commit()
                    feed.last_ok, feed.error = time.time(), ""
                    if not feed.backlog:
                        break
                await asyncio.sleep(max(0.05, self.interval - (time.monotonic() - started)))
            except Exception as exc:
                feed.error = str(exc)
                # Expired or incompatible signed cursors require restarting from
                # the same committed watermark, never advancing it on failure.
                if "HTTP 400" in feed.error:
                    state.pop("cursor", None)
                await asyncio.sleep(min(30, max(2, self.interval * 2)))

    async def seed(self):
        while True:
            try:
                n = self.wallet_count // 2
                boards = await asyncio.gather(
                    self.api.get("/v2/leaderboard", time_period="all", sort_by="PNL", limit=n),
                    self.api.get("/v2/leaderboard", time_period="month", sort_by="PNL", limit=n),
                    self.api.get("/v2/leaderboard", time_period="month", sort_by="VOLUME", limit=n),
                )
                # Alternate all-time and monthly winners; preserve equal slots.
                profit = []
                seen = set()
                for pair in zip(boards[0]["data"], boards[1]["data"]):
                    for row in pair:
                        if row["user_id"] not in seen and len(profit) < n:
                            seen.add(row["user_id"])
                            profit.append(row)
                # zip may end early on small boards.
                for row in boards[0]["data"] + boards[1]["data"]:
                    if row["user_id"] not in seen and len(profit) < n:
                        seen.add(row["user_id"])
                        profit.append(row)
                for p in self.wallets.values():
                    p["cohorts"] = [c for c in p.get("cohorts", []) if c == "observed whale"]
                # Show available board economics immediately while deeper
                # position analysis loads for accounts with thousands of rows.
                for field, board in (("all_pnl", boards[0]), ("month_pnl", boards[1])):
                    for row in board["data"]:
                        addr = row["user_id"].lower()
                        if self.excluded(addr):
                            continue
                        p = self.wallets.setdefault(addr, {"address": addr, "cohorts": []})
                        p[field] = number(row.get("pnl"))
                        p["name"] = row.get("user_name") or addr[:12]
                for cohort, rows in (("profit", profit), ("whale", boards[2]["data"])):
                    for row in rows:
                        addr = row["user_id"].lower()
                        if self.excluded(addr):
                            continue
                        p = self.wallets.setdefault(addr, {"address": addr})
                        p["name"] = row["user_name"] or addr[:12]
                        p["cohorts"] = list(set(p.get("cohorts", []) + [cohort]))
                        if cohort == "whale":
                            p["month_volume_shares"] = number(row.get("volume"))
                            p["volume_rank"] = row["rank"]
                        self.enqueue(addr)
                for addr, p in self.wallets.items():
                    self.store.wallet(addr, p)
                self.board_time = time.time()
            except Exception as exc:
                self.note("Wallet discovery: " + str(exc))
            await asyncio.sleep(300)

    async def profile(self, wallet):
        if not await self.check_account(wallet):
            return self.wallets[wallet]
        p = dict(self.wallets.get(wallet, {"address": wallet, "cohorts": []}))
        # Standings identify the different PnL semantics explicitly.
        results = await asyncio.gather(
            self.api.get("/v2/leaderboard", user=wallet, time_period="all"),
            self.api.get("/v2/leaderboard", user=wallet, time_period="month"),
            self.api.get("/v2/value", user=wallet),
            self.api.get("/v2/user-pnl", user=wallet, interval="1m", fidelity="1d"),
            self.api.pages("/v2/positions", user=wallet, status="CLOSED", limit=1000, max_pages=10),
        )
        all_board, month_board, value, pnl, closed = results
        for field, board in (("all_pnl", all_board), ("month_pnl", month_board)):
            row = board["data"]
            p[field] = number(row.get("pnl")) if row else None
            if row:
                p["name"] = row.get("user_name") or p.get("name") or wallet[:12]
        p["portfolio"] = number((value["data"] or {}).get("value"))
        points = (pnl["data"] or {}).get("points", [])
        # Only consecutive, covered daily samples; cashflow-net position PnL
        # avoids deposits being counted as profitable trading days.
        changes = [b["position_pnl"] - a["position_pnl"] for a, b in zip(points, points[1:])
                   if a.get("position_pnl") is not None and b.get("position_pnl") is not None
                   and 20 * 3600 <= b["timestamp"] - a["timestamp"] <= 28 * 3600]
        active = [d for d in changes if abs(d) > .01]
        p["active_days"] = len(active)
        p["positive_days"] = sum(d > 0 for d in active) / len(active) if active else None
        p.update(closed_summary(*closed))
        p.update(self.positions.summary(wallet))
        p.pop("positions", None)
        p["consistent"] = consistency(p)
        p["updated"] = time.time()
        # Discovery can update cohorts during the awaits above.
        latest = self.wallets.get(wallet, p)
        p["cohorts"] = latest.get("cohorts", [])
        # An account check can disqualify the wallet while history is loading.
        for key in ("account_pnl", "account_realized_pnl", "account_unrealized_pnl", "pnl_observed_at",
                    "pnl_checked_at", "pnl_next_check", "pnl_reason", "disqualified", "disqualified_at", "disqualified_cohorts"):
            if key in latest:
                p[key] = latest[key]
        self.wallets[wallet] = p
        self.store.wallet(wallet, p)
        return p

    async def enrich(self):
        while True:
            wallet = await self.queue.get()
            try:
                if not self.excluded(wallet) and time.time() - self.wallets.get(wallet, {}).get("updated", 0) > 1800:
                    await self.profile(wallet)
            except Exception as exc:
                self.note(f"Profile {wallet[:10]}: {exc}")
            finally:
                self.queued.discard(wallet)
                self.queue.task_done()
            await asyncio.sleep(.1)

    async def health(self):
        while True:
            try:
                self.status = (await self.api.get("/v2/status"))["data"]
                self.status_time = time.time()
                self.store.prune()
                self.positions.trim()
                self.store.db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                self.store.save_state("last_health", {"time": self.status_time, "status": self.status})
                self.store.db.commit()
            except Exception as exc:
                self.note("API health: " + str(exc))
            await asyncio.sleep(15)

    def tasks(self):
        return [asyncio.create_task(coro) for coro in (
            self.poll("all trades"), self.poll("big trades", True),
            self.seed(), self.enrich(), self.health(), self.accounts(), self.positions.run(), self.positions.market_loop(),
            self.execution.run(), self.paper.run())]

    def snapshot(self):
        return {"threshold": self.threshold, "interval": self.interval,
                "feeds": {k: asdict(v) for k, v in self.feeds.items()},
                "wallets": [p for a, p in self.wallets.items() if not self.excluded(a)], "alerts": self.recent(alerts=True),
                "trades": self.recent(), "health": self.status, "notices": self.notices,
                "account_filter": {state: sum(account_status(p) == state for p in self.wallets.values()) for state in ("eligible", "pending", "excluded")},
                "paper": self.store.papers(limit=100),
                "active_positions": self.positions.active(), "position_changes": self.position_changes(),
                "position_verification": {"last_ok": self.positions.last_ok,
                    "pending": len(self.positions.pending), "wallet_checks": {a: p for a, p in self.positions.checks.items() if self.allowed(a)},
                    "error": self.positions.error, "market_error": self.positions.market_error}}
