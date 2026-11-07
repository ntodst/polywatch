import asyncio
import time

import httpx
import pytest

from polywatch.api import API
from polywatch.engine import Engine
from polywatch.execution import estimate
from polywatch.models import Trade
from polywatch.store import Store


def book(now=None, **extra):
    return {"asset_id": "123", "token_id": "123", "checked_at": time.time() if now is None else now,
            "asks": [(0.5, 10000)], "bids": [(0.48, 10000)], "fee": {"rate": .05, "exponent": 1},
            "min_size": 5, "accepting": True, **extra}


def test_fee_inclusive_buy_and_executable_sell():
    entry = estimate(book(), budget=1025)
    assert entry["shares"] == pytest.approx(2000)
    assert entry["gross"] == pytest.approx(1000)
    assert entry["fees"] == pytest.approx(25)
    assert entry["cash"] == pytest.approx(1025)
    exit_fill = estimate(book(), shares=entry["shares"])
    assert exit_fill["cash"] == pytest.approx(935.04)
    assert exit_fill["fees"] == pytest.approx(24.96)


def test_walks_multiple_levels_and_reports_partial_and_unknown_fees():
    fill = estimate(book(asks=[(.5, 100), (.6, 100)]), budget=200)
    assert not fill["complete"] and fill["status"] == "partial"
    assert fill["shares"] == 200
    assert fill["average"] == pytest.approx(.55)
    assert fill["slippage"] == pytest.approx(.05)
    assert estimate(book(fee=None), budget=100)["status"] == "fees_unknown"
    assert estimate(book(asks=[]), budget=100)["status"] == "no_liquidity"
    assert estimate(book(min_size=1000), budget=10)["status"] == "below_minimum"


def test_execution_uses_sorted_depth_canonical_asset_and_fee_curve():
    async def run():
        requests = []
        def handler(request):
            requests.append(request)
            if "/clob-markets/" in request.url.path:
                return httpx.Response(200, json={"fd": {"r": .05, "e": 1}, "ao": True,
                                               "t": [{"t": "123"}, {"t": "456"}]})
            return httpx.Response(200, json={"asset_id": request.url.params["token_id"], "timestamp": "1000",
                "asks": [{"price": ".6", "size": "100"}, {"price": ".5", "size": "100"}],
                "bids": [{"price": ".4", "size": "100"}, {"price": ".48", "size": "100"}]})
        api, store = API(10000, transport=httpx.MockTransport(handler)), Store(":memory:")
        engine = Engine(api, store)
        result = await engine.execution.fetch("old-id", {"condition_id": "condition", "outcome_index": 1})
        assert result["asset_id"] == "456"
        assert result["ask"] == .5 and result["bid"] == .48
        assert result["fee"] == {"rate": .05, "exponent": 1}
        assert all("_" not in r.url.params for r in requests)
        await api.close()
        store.close()
    asyncio.run(run())


def test_nonzero_base_fee_is_not_misinterpreted_as_a_fee_curve():
    async def run():
        def handler(request):
            if "clob-markets" in request.url.path:
                return httpx.Response(200, json={"ao": True})
            if "fee-rate" in request.url.path:
                return httpx.Response(200, json={"base_fee": 1000})
            return httpx.Response(200, json={"asset_id": "123", "asks": [{"price": ".5", "size": "1000"}], "bids": []})
        api, store = API(10000, transport=httpx.MockTransport(handler)), Store(":memory:")
        engine = Engine(api, store)
        result = await engine.execution.fetch("123", {"condition_id": "condition"})
        assert result["fee"] is None
        assert not estimate(result, budget=100)["complete"]
        await api.close()
        store.close()
    asyncio.run(run())


@pytest.fixture
def engine():
    store = Store(":memory:")
    engine = Engine(None, store)
    now = time.time()
    engine.wallets["0xa"] = {"address": "0xa", "cohorts": ["profit"], "account_pnl": 1000,
                             "pnl_checked_at": now, "pnl_observed_at": now}
    engine.preferences["size"] = 1025
    yield engine
    store.close()


def signal(engine, tx="a", token="123"):
    trade = Trade.parse({"proxy_wallet": "0xa", "token_id": token, "condition_id": "0x" + "a" * 64,
                         "outcome_index": 0, "timestamp": int(time.time()), "size": 40000, "price": .5,
                         "transaction_hash": tx, "side": "BUY"})
    engine.paper.capture("BIG BET", trade)
    return next(r for r in engine.paper.records.values() if r["token"] == token)


def test_paper_waits_for_post_detection_quote_and_preserves_qualification(engine):
    r = signal(engine)
    detected = r["detected_at"]
    engine.execution.books["123"] = book(detected - 1)
    engine.paper.step(detected + 1)
    assert r["status"] == "queued"
    engine.execution.books["123"] = book(detected + 2)
    engine.paper.step(detected + 3)
    assert r["status"] == "open"
    assert r["entry"]["shares"] == pytest.approx(2000)
    assert r["entry_at"] == detected + 2
    assert r["qualification"]["account_pnl"] == 1000
    # Horizon mark uses a quote observed AFTER the horizon, not an earlier book.
    engine.execution.books["123"] = book(r["entry_at"] + 299)
    engine.paper.step(r["entry_at"] + 302)
    assert "300" not in r["marks"]
    engine.execution.books["123"] = book(r["entry_at"] + 303)
    engine.paper.step(r["entry_at"] + 304)
    assert r["marks"]["300"]["pnl"] == pytest.approx(-89.96)


def test_thin_exit_book_never_marks_unfilled_shares_as_liquidated(engine):
    r = signal(engine)
    t = r["detected_at"]
    engine.execution.books["123"] = book(t + 1)
    engine.paper.step(t + 2)
    engine.execution.books["123"] = book(t + 3, bids=[(.48, 10)])
    engine.paper.step(t + 4)
    assert r["status"] == "open" and r["pnl"] is None
    assert "unpriced" in r["mark_warning"]


def test_skipped_quotes_are_recorded_and_cannot_backfill_entries(engine):
    r = signal(engine)
    engine.paper.step(r["detected_at"] + 46)
    assert not engine.paper.records
    assert engine.store.papers()[0]["status"] == "skipped"
    engine.execution.books["123"] = book()
    engine.paper.step()
    assert engine.store.papers()[0]["status"] == "skipped"


def test_disqualified_source_closes_using_a_new_quote_without_new_wallet_calls(engine):
    r = signal(engine)
    t = r["detected_at"]
    engine.execution.books["123"] = book(t + 1)
    engine.paper.step(t + 2)
    engine.wallets["0xa"]["disqualified"] = True
    engine.paper.step(t + 3)
    assert r["status"] == "open"  # pre-disqualification quote cannot close it
    engine.execution.books["123"] = book(t + 4)
    engine.paper.step(t + 5)
    assert not engine.paper.records
    closed = engine.store.papers()[0]
    assert closed["status"] == "closed" and closed["pnl"] == pytest.approx(-89.96)


def test_pending_entries_cannot_overrun_open_position_cap(engine):
    for i in range(30):
        r = signal(engine, tx=str(i), token=str(i))
        engine.execution.books[str(i)] = book(r["detected_at"] + .1, asset_id=str(i))
    engine.paper.step(time.time() + 1)
    assert sum(r["status"] == "open" for r in engine.paper.records.values()) == 25
    assert sum(r["status"] == "skipped" for r in engine.store.papers()) == 5
