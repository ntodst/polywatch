"""Read-only order-book estimates. No order submission or credentials."""
import asyncio
import json
import math
import time
from collections import OrderedDict

CLOB = "https://clob.polymarket.com"


def finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Non-finite order-book value")
    return value


def fee_per_share(price, fee):
    if fee is None:
        return None
    return fee["rate"] * (price * (1 - price)) ** fee["exponent"]


def estimate(book, *, budget=None, shares=None):
    """Walk asks for an inclusive cash budget, or bids to liquidate shares."""
    buy = budget is not None
    levels = book.get("asks" if buy else "bids", [])
    fee = book.get("fee")
    if fee is None:
        return {"status": "fees_unknown", "complete": False}
    remaining = finite(budget if buy else shares)
    if remaining <= 0:
        raise ValueError("Size must be positive")
    quantity = gross = fees = 0.0
    for price, available in levels:
        per_fee = fee_per_share(price, fee)
        take = min(available, remaining / (price + per_fee) if buy else remaining)
        quantity += take
        gross += take * price
        fees += take * per_fee
        remaining -= take * (price + per_fee) if buy else take
        if remaining <= 1e-7:
            break
    complete = remaining <= 1e-6
    status = "complete" if complete else "partial" if quantity else "no_liquidity"
    if buy and quantity and quantity + 1e-7 < book.get("min_size", 0):
        complete, status = False, "below_minimum"
    average = gross / quantity if quantity else None
    best = levels[0][0] if levels else None
    return {"status": status, "complete": complete, "shares": quantity,
            "gross": gross, "fees": fees, "cash": gross + fees if buy else gross - fees,
            "average": average, "slippage": abs(average - best) if average is not None else None,
            "unfilled": max(0, remaining)}


class Execution:
    def __init__(self, engine):
        self.engine, self.api = engine, engine.api
        self.targets = OrderedDict()
        self.books = OrderedDict()
        self.infos = OrderedDict()
        self.selected = None
        self.error = ""

    def watch(self, token, context=None, urgent=False):
        token = str(token)
        old = self.targets.get(token, {})
        self.targets[token] = {**old, **(context or {}), "token_id": token,
                               "due": 0 if urgent else old.get("due", 0), "touched": time.time()}
        self.targets.move_to_end(token)
        while len(self.targets) > 64:
            victim = next((k for k in self.targets if k != self.selected), None)
            if victim is None:
                break
            del self.targets[victim]

    def select(self, token, context=None):
        changed = self.selected != str(token)
        self.selected = str(token)
        self.watch(token, context, urgent=changed)

    async def fetch(self, token, context=None):
        context = context or self.targets.get(str(token), {})
        condition = context.get("condition_id")
        info, fee = {}, None
        cached = self.infos.get(condition)
        if condition:
            try:
                if cached and time.time() - cached[0] < 300:
                    info = cached[1]
                else:
                    raw = await self.api.get(CLOB + "/clob-markets/" + condition, envelope=False, cache_bust=False)
                    info = {k: raw[k] for k in ("fd", "t", "v", "ao", "mos") if k in raw}
                    self.infos[condition] = (time.time(), info)
                    self.infos.move_to_end(condition)
                    while len(self.infos) > 64:
                        self.infos.popitem(last=False)
            except Exception:
                info = {}
        schedule = info.get("fd")
        if schedule is not None:
            rate, exponent = finite(schedule["r"]), finite(schedule["e"])
            if not 0 <= rate <= 1 or not 0 <= exponent <= 4:
                raise ValueError("Unsupported fee curve")
            fee = {"rate": rate, "exponent": exponent}
        else:
            market = self.engine.positions.markets.get(condition, {})
            schedule = market.get("feeSchedule")
            if schedule and "rate" in schedule and "exponent" in schedule:
                rate, exponent = finite(schedule["rate"]), finite(schedule["exponent"])
                if 0 <= rate <= 1 and 0 <= exponent <= 4:
                    fee = {"rate": rate, "exponent": exponent}
            elif market.get("feesEnabled") is False:
                fee = {"rate": 0.0, "exponent": 1.0}
        asset = str(token)
        tokens = info.get("t", [])
        ids = [str(t.get("t")) for t in tokens]
        index = context.get("outcome_index")
        # A migrated market may expose a different active trading identifier.
        # Use the canonical CLOB outcome slot, never infer it from price.
        if asset not in ids and isinstance(index, int) and 0 <= index < len(ids):
            asset = ids[index]
        book = await self.api.get(CLOB + "/book", token_id=asset, envelope=False, cache_bust=False)
        if str(book.get("asset_id")) != asset:
            raise ValueError("Order book belongs to another outcome")
        if fee is None:
            try:
                base = await self.api.get(CLOB + "/fee-rate", token_id=asset, envelope=False, cache_bust=False)
                # A nonzero base_fee does not uniquely describe the fee curve.
                if base.get("base_fee") == 0:
                    fee = {"rate": 0.0, "exponent": 1.0}
            except Exception:
                pass
        def levels(name, reverse=False):
            data = []
            for level in book.get(name, []):
                price, size = finite(level["price"]), finite(level["size"])
                if not 0 < price < 1 or size < 0:
                    raise ValueError("Invalid order-book level")
                if size:
                    data.append((price, size))
            return sorted(data, reverse=reverse)
        result = {"token_id": str(token), "asset_id": asset, "condition_id": condition,
                  "checked_at": time.time(), "book_timestamp": book.get("timestamp"),
                  "bids": levels("bids", True), "asks": levels("asks"), "fee": fee,
                  "min_size": finite(book.get("min_order_size") or info.get("mos") or 0),
                  "accepting": (info.get("ao") is True or self.engine.positions.markets.get(condition, {}).get("acceptingOrders") is True)
                      and self.engine.positions.markets.get(condition, {}).get("closed") is not True}
        result["bid"] = result["bids"][0][0] if result["bids"] else None
        result["ask"] = result["asks"][0][0] if result["asks"] else None
        result["spread"] = result["ask"] - result["bid"] if result["ask"] is not None and result["bid"] is not None else None
        self.books[str(token)] = result
        self.books.move_to_end(str(token))
        while len(self.books) > 64:
            self.books.popitem(last=False)
        return result

    async def run(self):
        while True:
            now = time.time()
            for token in list(self.targets):
                if token != self.selected and now - self.targets[token]["touched"] > 120:
                    del self.targets[token]
            ready = [k for k, t in self.targets.items() if t["due"] <= now]
            if not ready:
                await asyncio.sleep(.2)
                continue
            ready.sort(key=lambda k: (k != self.selected, self.targets[k]["due"]))
            token = ready[0]
            target = self.targets[token]
            try:
                await self.fetch(token, target)
                self.error = ""
            except Exception as exc:
                self.books.pop(token, None)
                self.error = str(exc)
            if token in self.targets:
                self.targets[token]["due"] = time.time() + (3 if token == self.selected else 15)
            await asyncio.sleep(.1)
