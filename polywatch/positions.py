"""Position verification is independent of historical wallet scoring.

Only explicit balances change recorded quantities. Missing/partial responses
never synthesize a zero balance. Market and position freshness are independent.
"""
import asyncio
import re
import time
from datetime import datetime, timezone

from .models import number, compact_position, compact_market

POSITION_TTL = 120
MARKET_TTL = 120
DUST = 0.000001


def fresh(row, now=None):
    now = time.time() if now is None else now
    return bool(row.get("verified_at") and now - row["verified_at"] <= POSITION_TTL
                and row.get("balance_confirmed") is not False
                and not row.get("verification_pending") and not row.get("missing"))


def tradable(row, market, now=None):
    now = time.time() if now is None else now
    if market and market.get("endDate"):
        try:
            end = datetime.fromisoformat(market["endDate"].replace("Z", "+00:00"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            # This is a relevance filter for the main screen, not a claim
            # that a market automatically resolves at its scheduled end.
            if end.timestamp() <= now:
                return False
        except (ValueError, TypeError):
            return False
    return bool(fresh(row, now) and (number(row.get("current_size")) or 0) > DUST
                and row.get("status") == "OPEN" and row.get("redeemable") is False
                and market and now - market.get("checked_at", 0) <= MARKET_TTL
                and market.get("active") is True and market.get("closed") is False
                and market.get("archived") is not True and market.get("acceptingOrders") is True
                and market.get("enableOrderBook") is True
            and market.get("umaResolutionStatus") != "resolved")


def exposure(row, rows, now=None):
    """Offset complementary *shares*, not differently priced dollar values."""
    size = max(0, number(row.get("current_size")) or 0)
    opposite = rows.get((row["proxy_wallet"], row.get("opposite_token_id")))
    opposite_size = max(0, number(opposite.get("current_size")) or 0) if opposite and fresh(opposite, now) else 0
    paired = min(size, opposite_size)
    net = max(0, size - paired)
    fraction = net / size if size else 0
    value = number(row.get("current_value"))
    cost = number(row.get("entry_cost_usdc"))
    return {"paired_shares": paired, "net_shares": net,
            "net_value": value * fraction if value is not None else None,
            "net_cost": cost * fraction if cost is not None else None,
            "hedge_unknown": bool((opposite and (number(opposite.get("current_size")) or 0) > DUST and not fresh(opposite, now))
                                  or (row.get("hedge_complete") is False and not (opposite and fresh(opposite, now))))}


class Positions:
    def __init__(self, engine):
        self.engine, self.api, self.store = engine, engine.api, engine.store
        # Historic books stay on disk; only meaningful/recent working rows live
        # in memory. Startup no longer decodes every wallet's entire history.
        self.rows = {}
        self.wallet_keys = {}
        self.loaded = set()
        self.revision = 0
        self.checks = self.store.load_checks("position_checks")
        self.markets = self.store.load_checks("market_checks")
        self.pending = {}
        self.inflight = set()
        self.last_ok = 0
        self.error = ""
        self.market_error = ""
        self.target_turns = 0
        # A restart always revalidates old books, including pending snapshots.
        self.session_checks = {}
        self.retry_after = {}
        self.pending_dropped = 0

    def restore(self, wallet):
        if wallet in self.loaded:
            return
        self.rows.update(self.store.load_positions(wallet, self.engine.threshold / 10, limit=300))
        for (addr, _), row in list(self.rows.items()):
            opposite = row.get("opposite_token_id")
            if addr == wallet and opposite and (wallet, opposite) not in self.rows:
                other = self.store.position(wallet, opposite)
                if other:
                    self.rows[(wallet, opposite)] = other
        self.loaded.add(wallet)
        self.reindex()

    def reindex(self):
        self.wallet_keys = {}
        for wallet, token in self.rows:
            self.wallet_keys.setdefault(wallet, set()).add(token)

    def trim(self):
        now = time.time()
        keep = set()
        requested_tokens = {(k[0], t) for k, job in self.pending.items() for t in job.get("tokens", {})}
        for key, row in self.rows.items():
            wallet, token = key
            material = (number(row.get("current_value")) or 0) >= self.engine.threshold
            requested = key in requested_tokens
            recent_change = now - row.get("change_at", 0) < 300 and row.get("last_change") not in (None, "BASELINE", "FIRST SEEN")
            selected = wallet == self.engine.selected and len(self.wallet_keys.get(wallet, ())) <= 200
            if material or requested or recent_change or selected:
                keep.add(key)
                keep.add((wallet, row.get("opposite_token_id")))
        self.rows = {k: v for k, v in self.rows.items() if k in keep and not self.engine.excluded(k[0])}
        # Evict whole wallet books together so a complementary leg cannot be
        # silently dropped while claiming the retained leg is unhedged.
        while len(self.rows) > 6000:
            ages = {}
            for (wallet, _), row in self.rows.items():
                if wallet != self.engine.selected:
                    ages[wallet] = min(ages.get(wallet, now), row.get("verified_at", 0))
            if not ages:
                groups = {}
                for key, row in self.rows.items():
                    groups.setdefault((key[0], row["condition_id"]), []).append((key, row))
                kept = {}
                for group in sorted(groups.values(), key=lambda group: max(r.get("current_value") or 0 for _, r in group), reverse=True):
                    if len(kept) + len(group) <= 6000:
                        kept.update(group)
                self.rows = kept
                self.loaded.discard(self.engine.selected)
                break
            victim = min(ages, key=ages.get)
            self.rows = {k: v for k, v in self.rows.items() if k[0] != victim}
            self.loaded.discard(victim)
        self.reindex()
        self.revision += 1

    def request(self, wallet, condition=None, trade=None):
        now = time.time()
        wallet = wallet.lower()
        if not self.engine.allowed(wallet):
            return
        # The global feed also contains combo fills with structural 0x03 IDs.
        # Those are not valid selectors for the standard positions endpoint.
        if condition is not None and not re.fullmatch(r"0x[0-9a-fA-F]{64}", condition):
            return
        if trade:
            timestamp = int(trade.get("timestamp", 0))
            balance = self.rows.get((wallet, str(trade.get("token_id"))))
            if balance and fresh(balance, now) and int(balance.get("last_event_at") or 0) > timestamp:
                return
            if now - timestamp > POSITION_TTL:
                baseline = self.checks.get(wallet, {})
                if baseline.get("complete") and now - baseline.get("checked_at", 0) <= POSITION_TTL and baseline["checked_at"] > timestamp:
                    return
                # Downtime catch-up should coalesce old fills into a present-day
                # wallet check instead of flooding the live queue with old markets.
                condition, trade = None, None
        key = (wallet, condition)
        if key not in self.pending and len(self.pending) >= 256:
            self.pending_dropped += 1
            # The periodic whole-wallet rotation remains responsible for the
            # overflow; never advance a balance or assert an exit here.
            self.session_checks.pop(wallet, None)
            return
        old = self.pending.get(key, {})
        if not trade and old and not old.get("tokens"):
            old["due"] = min(old["due"], now + .5)
            return
        seq = old.get("seq", 0) + 1
        tokens = dict(old.get("tokens", {}))
        if trade and trade.get("token_id"):
            tokens[str(trade["token_id"])] = max(tokens.get(str(trade["token_id"]), 0), int(trade.get("timestamp", 0)))
        self.pending[key] = {"due": min(old.get("due", now + .5), now + .5),
                             "timestamp": max(old.get("timestamp", 0), int((trade or {}).get("timestamp", 0))),
                             "since": old.get("since", now), "seq": seq,
                             "attempts": old.get("attempts", 0), "tokens": tokens}
        for token in self.wallet_keys.get(wallet, ()):
            row = self.rows[(wallet, token)]
            if (condition is None or row["condition_id"] == condition) and (not tokens or token in tokens) and (number(row.get("current_size")) or 0) > DUST and not row.get("verification_pending"):
                row["verification_pending"] = True
                self.store.save_position(wallet, token, row)
        self.revision += 1

    def apply(self, wallet, rows, complete, *, conditions=None, now=None):
        """Apply a response; absent rows stay unverified until explicitly closed."""
        now = time.time() if now is None else now
        wallet = wallet.lower()
        if self.engine.excluded(wallet):
            return
        scope = set(conditions) if conditions is not None else None
        observed = set()
        supplied_tokens = {str(r["token_id"]) for r in rows}
        for raw in rows:
            if raw.get("proxy_wallet", "").lower() != wallet:
                raise ValueError("Position API returned a different wallet")
            if scope is not None and raw["condition_id"] not in scope:
                raise ValueError("Position API returned a different condition")
            token = str(raw["token_id"])
            observed.add(token)
            key = (wallet, token)
            old = self.rows.get(key) or self.store.position(wallet, token)
            size = number(raw.get("current_size"))
            if size is None or size < 0:
                raise ValueError("Position balance unavailable")
            request = self.pending.get((wallet, raw["condition_id"])) or self.pending.get((wallet, None))
            trade_clock = (request or {}).get("tokens", {}).get(token, 0)
            if trade_clock and int(raw.get("last_event_at") or 0) < trade_clock:
                # The holdings ledger is behind the observed fill. Preserve the
                # last confirmed balance and never emit a premature EXITED event.
                row = dict(old) if old else {**raw, "proxy_wallet": wallet, "balance_confirmed": False,
                                             "verified_at": 0, "last_change": "PENDING", "change_at": now}
                row["verification_pending"] = True
                self.rows[key] = row
                self.store.save_position(wallet, token, row)
                continue
            row = {**compact_position(raw), "proxy_wallet": wallet, "verified_at": now,
                   "verification_pending": False, "missing": False, "balance_confirmed": True,
                   "hedge_complete": bool(complete or raw.get("opposite_token_id") in supplied_tokens),
                   "last_change": (old or {}).get("last_change", "BASELINE"),
                   "change_at": (old or {}).get("change_at", now)}
            previous = number(old.get("current_size")) if old and old.get("balance_confirmed") is not False else None
            kind = None
            if previous is not None and abs(size - previous) > DUST:
                kind = "EXITED" if size <= DUST else "OPENED" if previous <= DUST else "ADDED" if size > previous else "REDUCED"
            elif previous is None and size > DUST:
                baseline = self.checks.get(wallet, {})
                acquired = int(raw.get("first_entry_at") or 0)
                kind = "OPENED" if baseline.get("complete") and acquired > baseline.get("checked_at", float("inf")) else "FIRST SEEN"
                if kind == "OPENED":
                    previous = 0
            if kind:
                row["last_change"], row["change_at"] = kind, now
                material = max(number(raw.get("current_value")) or 0, number(raw.get("entry_cost_usdc")) or 0,
                               number((old or {}).get("current_value")) or 0, number((old or {}).get("entry_cost_usdc")) or 0)
                if material >= self.engine.threshold:
                    self.store.position_event(kind, row, previous, now)
                    if kind == "EXITED" and hasattr(self.engine, "paper"):
                        self.engine.paper.close_source(wallet, token)
            self.rows[key] = row
            self.store.save_position(wallet, token, row)
            if not row["hedge_complete"] and (number(row.get("current_value")) or 0) >= self.engine.threshold:
                self.request(wallet, row["condition_id"])
        if complete:
            for (addr, token), old in self.rows.items():
                if addr == wallet and token not in observed and (scope is None or old["condition_id"] in scope) and (number(old.get("current_size")) or 0) > DUST:
                    old["missing"], old["verification_pending"] = True, True
                    self.store.save_position(addr, token, old)
                    if scope is None:
                        self.request(wallet, old["condition_id"])
        if scope is None:
            values = [number(r.get("unrealized_pnl")) for r in rows if (number(r.get("current_size")) or 0) > DUST]
            self.checks[wallet] = {"checked_at": now, "complete": complete,
                                   "count": len(observed), "unrealized_pnl": sum(values) if complete and all(v is not None for v in values) else None}
            self.session_checks[wallet] = now
            self.store.save_check("position_checks", wallet, self.checks[wallet])
        elif wallet in self.checks:
            self.checks[wallet]["unrealized_pnl"] = None
        self.last_ok = now
        self.reindex()
        self.trim()
        self.store.db.commit()

    async def refresh(self, wallet, conditions=None):
        if not self.engine.allowed(wallet):
            return [], False
        self.restore(wallet)
        params = {"user": wallet, "status": "OPEN", "include_archived": True,
                  "limit": 1000, "max_pages": 3}
        if conditions:
            params["condition"] = ",".join(conditions)
        rows, complete = await self.api.pages("/v2/positions", **params)
        if not self.engine.allowed(wallet):
            return [], False
        present = {str(r["token_id"]) for r in rows}
        scope = set(conditions) if conditions else None
        # Query CLOSED for missing previously held tokens and for targeted
        # requests. OPEN's absence alone cannot establish an exit.
        missing = {r["condition_id"] for (addr, token), r in self.rows.items()
                   if addr == wallet and token not in present and (number(r.get("current_size")) or 0) > DUST
                   and (scope is None or r["condition_id"] in scope)} if complete else set()
        closed_conditions = scope if scope is not None else missing
        closed_rows = []
        if closed_conditions:
            condition_list = sorted(closed_conditions)
            for start in range(0, len(condition_list), 20):
                if not self.engine.allowed(wallet):
                    return [], False
                closed, _ = await self.api.pages("/v2/positions", user=wallet, status="CLOSED",
                                                condition=",".join(condition_list[start:start + 20]),
                                                limit=1000, max_pages=3)
                # Never let an overlapping or inconsistent CLOSED row override
                # an explicit OPEN balance returned in this sweep.
                closed_rows.extend(r for r in closed if str(r["token_id"]) not in present
                                   and (number(r.get("current_size")) or 0) <= DUST)
        self.apply(wallet, rows + closed_rows, complete, conditions=conditions)
        return rows, complete

    def _settle_requests(self, wallet, jobs):
        now = time.time()
        for key, job in jobs.items():
            current = self.pending.get(key)
            if current is None or current["seq"] != job["seq"]:
                continue
            if key[1] is None and self.checks.get(wallet, {}).get("checked_at", 0) >= job["since"] and self.checks[wallet].get("complete"):
                del self.pending[key]
                continue
            related = [r for (addr, _), r in self.rows.items()
                       if addr == wallet and (key[1] is None or r["condition_id"] == key[1])
                       and (not job.get("tokens") or str(r["token_id"]) in job["tokens"])]
            if related and all(fresh(r, now) for r in related) and (not job.get("tokens") or set(job["tokens"]).issubset({str(r["token_id"]) for r in related})):
                del self.pending[key]
            elif now - job["since"] > 120:
                self.engine.note(f"Holdings {wallet[:10]}: balance still unverified; background scans will retry")
                del self.pending[key]
            else:
                current["attempts"] += 1
                current["due"] = now + min(30, 2 ** min(current["attempts"], 5))

    async def run(self):
        while True:
            now = time.time()
            ready = [(k, v) for k, v in self.pending.items() if v["due"] <= now and self.engine.allowed(k[0])]
            candidates = [a for a, p in self.engine.wallets.items() if p.get("cohorts") and self.engine.allowed(a)]
            due = [a for a in candidates if now - self.session_checks.get(a, 0) >= 30 and now >= self.retry_after.get(a, 0)]
            wallet = None
            jobs = {}
            if ready and (self.target_turns < 3 or not due):
                ready.sort(key=lambda kv: (kv[0][0] != self.engine.selected, kv[1]["due"]))
                wallet = ready[0][0][0]
                jobs = {k: dict(v) for k, v in ready if k[0] == wallet}
                jobs = dict(list(jobs.items())[:20])
                conditions = None if (wallet, None) in jobs else [k[1] for k in jobs]
                self.target_turns += 1
            elif due:
                wallet = min(due, key=lambda a: self.session_checks.get(a, 0))
                conditions = None
                self.target_turns = 0
            if wallet is None:
                await asyncio.sleep(.2)
                continue
            try:
                self.inflight.add(wallet)
                await self.refresh(wallet, conditions)
                self._settle_requests(wallet, jobs)
                self.retry_after.pop(wallet, None)
                self.error = ""
            except Exception as exc:
                self.error = f"Holdings {wallet[:10]}: {exc}"
                self.engine.note(self.error)
                self.retry_after[wallet] = time.time() + 10
                for key in jobs:
                    if key in self.pending:
                        if time.time() - self.pending[key]["since"] > 120:
                            del self.pending[key]
                        else:
                            self.pending[key]["due"] = time.time() + 5
                await asyncio.sleep(1)
            finally:
                self.inflight.discard(wallet)
            await asyncio.sleep(.05)

    async def market_loop(self):
        while True:
            now = time.time()
            candidates = {}
            for row in self.rows.values():
                if not self.engine.allowed(row["proxy_wallet"]):
                    continue
                if (number(row.get("current_size")) or 0) > DUST and row.get("status") == "OPEN" and not row.get("redeemable"):
                    value = number(row.get("current_value")) or 0
                    if value >= self.engine.threshold:
                        condition = row["condition_id"]
                        if now - self.markets.get(condition, {}).get("checked_at", 0) >= 60:
                            candidates[condition] = max(candidates.get(condition, 0), value)
            batch = sorted(candidates, key=lambda c: (self.markets.get(c, {}).get("checked_at", 0), -candidates[c]))[:50]
            if not batch:
                await asyncio.sleep(.5)
                continue
            try:
                rows = await self.api.get("https://gamma-api.polymarket.com/markets", envelope=False,
                                          condition_ids=batch, limit=100)
                if not isinstance(rows, list):
                    raise ValueError("Unexpected market metadata response")
                found = {r["conditionId"]: r for r in rows if r.get("conditionId") in batch}
                for condition in batch:
                    # A missing market is unknown, never implicitly tradable.
                    self.markets[condition] = compact_market({**found.get(condition, {}), "checked_at": time.time()})
                    self.store.save_check("market_checks", condition, self.markets[condition])
                self.store.db.commit()
                self.revision += 1
                self.market_error = ""
            except Exception as exc:
                self.market_error = "Market verification: " + str(exc)
                self.engine.note(self.market_error)
                await asyncio.sleep(5)
            await asyncio.sleep(.2)

    def active(self, now=None):
        now = time.time() if now is None else now
        active = []
        for row in self.rows.values():
            if not tradable(row, self.markets.get(row["condition_id"]), now):
                continue
            risk = exposure(row, self.rows, now)
            if risk["hedge_unknown"] or risk["net_value"] is None or risk["net_value"] < self.engine.threshold:
                continue
            p = self.engine.wallets.get(row["proxy_wallet"], {})
            if not p.get("cohorts") or not self.engine.allowed(row["proxy_wallet"]):
                continue
            active.append({**row, **risk})
        # Agreement uses balances that are still verified, not historical buys.
        holders = {}
        for row in active:
            p = self.engine.wallets.get(row["proxy_wallet"], {})
            if (p.get("account_pnl") or 0) > 0:
                holders.setdefault(row["token_id"], set()).add(row["proxy_wallet"])
        for row in active:
            row["profitable_holders"] = len(holders.get(row["token_id"], set()))
        return sorted(active, key=lambda r: (r["net_value"],
                                             self.engine.wallets.get(r["proxy_wallet"], {}).get("all_pnl") or 0), reverse=True)

    def wallet_rows(self, wallet):
        return sorted([r for (addr, _), r in self.rows.items() if addr == wallet and (number(r.get("current_size")) or 0) > DUST],
                      key=lambda r: r.get("current_value") or 0, reverse=True)

    def summary(self, wallet):
        rows = self.wallet_rows(wallet)
        check = self.checks.get(wallet, {})
        covered = check.get("complete") and time.time() - check.get("checked_at", 0) <= POSITION_TTL
        covered = covered and all(fresh(r) for r in rows)
        return {"unrealized_pnl": check.get("unrealized_pnl") if covered else None,
                "positions_updated": check.get("checked_at"), "open_complete": check.get("complete", False)}
