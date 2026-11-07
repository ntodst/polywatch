import asyncio
import json
import time

import httpx
import pytest

from polywatch.api import API, ApiError, retry_seconds
from polywatch.engine import Engine
from polywatch.models import Trade, closed_summary, consistency
from polywatch.store import Store


def trade(amount=6000, timestamp=1000, tx="a", wallet="0xabc", side="BUY", **kwargs):
    return {"proxy_wallet": wallet, "size": amount * 2, "price": .5, "timestamp": timestamp,
            "transaction_hash": tx, "token_id": "123", "condition_id": "condition",
            "side": side, "outcome": "Yes", "name": "test", "title": "Test market", **kwargs}


def test_cash_not_share_threshold_and_duplicate_restart(tmp_path):
    path = tmp_path / "watch.db"
    store = Store(path)
    # 12,000 shares is only $6,000; it must not fire a big-trade alert.
    store.ingest([trade()], set(), 10000)
    assert not store.recent(alerts=True)
    store.close()
    store = Store(path)
    inserted, _ = store.ingest([trade(), trade(5000, 1010, "b")], set(), 10000)
    assert inserted == 1
    alerts = store.recent(alerts=True)
    assert len(alerts) == 1 and alerts[0]["alert_kind"] == "ACCUMULATION"
    assert alerts[0]["bundle_amount"] == 11000
    store.ingest([trade(10000, 1020, "c")], set(), 10000)
    assert [a["alert_kind"] for a in store.recent(alerts=True)].count("BIG BET") == 1
    store.close()


def test_identity_keeps_both_wallets_sides_and_outcomes():
    rows = [trade(), trade(wallet="0xdef"), trade(side="SELL"), trade(token_id="456")]
    assert len({Trade.parse(t).key for t in rows}) == 4
    a = trade()
    b = {**a, "size": str(a["size"]), "price": "0.5000"}
    assert Trade.parse(a).key == Trade.parse(b).key


def test_time_window_and_out_of_order():
    store = Store(":memory:")
    store.ingest([trade(timestamp=1000), trade(timestamp=1301, tx="b")], set(), 10000)
    assert not store.recent(alerts=True)
    store.ingest([trade(5000, timestamp=1200, tx="late")], set(), 10000)
    assert store.recent(alerts=True)[0]["bundle_amount"] == 11000
    store.close()


def test_sell_not_buy_accumulation():
    store = Store(":memory:")
    store.ingest([trade(), trade(side="SELL", tx="sell")], set(), 10000)
    assert not store.recent(alerts=True)
    store.close()


def test_consensus_requires_two_profitable_wallets_above_threshold_each():
    store = Store(":memory:")
    wallets = {"0xabc", "0xdef"}
    store.ingest([trade(10000), trade(5000, 1010, "b", wallet="0xdef")], wallets, 10000, wallets)
    assert not any(r["alert_kind"] == "CO-BUY" for r in store.recent(alerts=True))
    store.ingest([trade(5000, 1020, "c", wallet="0xdef")], wallets, 10000, wallets)
    alert = next(r for r in store.recent(alerts=True) if r["alert_kind"] == "CO-BUY")
    assert alert["bundle_amount"] == 20000
    assert set(alert["consensus_wallets"]) == wallets
    store.close()


def test_closed_markets_group_hedges_and_unknown_is_not_zero():
    rows = [{"condition_id": "a", "realized_pnl": 100}, {"condition_id": "a", "realized_pnl": -110},
            {"condition_id": "b", "realized_pnl": 20}]
    result = closed_summary(rows, True)
    assert result["closed_markets"] == 2
    assert result["win_rate"] == .5
    assert closed_summary(rows + [{"condition_id": "c", "realized_pnl": None}], True)["closed_complete"] is False


def test_consistency_cannot_hide_open_losses_or_incomplete_history():
    p = {"closed_complete": True, "closed_markets": 40, "all_pnl": 100, "month_pnl": 10,
         "active_days": 12, "positive_days": .7, "unrealized_pnl": -50}
    assert consistency(p)
    assert not consistency({**p, "unrealized_pnl": -110})
    assert not consistency({**p, "closed_complete": False})
    assert not consistency({**p, "unrealized_pnl": None})


def test_api_cursor_cache_busting_and_retry():
    async def run():
        calls = []
        async def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(503, headers={"Retry-After": "0"})
            if request.url.params.get("cursor"):
                return httpx.Response(200, json={"data": [2], "pagination": {"next_cursor": None}})
            return httpx.Response(200, json={"data": [1], "pagination": {"next_cursor": "opaque"}})
        api = API(10000, transport=httpx.MockTransport(handler))
        rows, complete = await api.pages("/v2/trades", taker_only=False, limit=1)
        assert rows == [1, 2] and complete
        assert api.retries == 1
        assert all(c.url.params["taker_only"] == "false" for c in calls)
        assert len({c.url.params["_"] for c in calls}) == 3
        await api.close()
    asyncio.run(run())


def test_rate_pacer_caps_concurrent_calls():
    async def run():
        times = []
        def handler(request):
            times.append(time.monotonic())
            return httpx.Response(200, json={"data": []})
        api = API(20, transport=httpx.MockTransport(handler))
        await asyncio.gather(*(api.get("/test") for _ in range(4)))
        assert times[-1] - times[0] >= .14
        await api.close()
    asyncio.run(run())


def test_feed_catches_up_multiple_pages_and_persists_watermark():
    async def run():
        now = int(time.time())
        calls = []
        rows = [trade(10000, now, "new"), trade(10000, now - 100, "old")]
        async def handler(request):
            calls.append(request)
            if request.url.params.get("cursor"):
                return httpx.Response(200, json={"data": [rows[1]], "pagination": {"next_cursor": "older"}})
            return httpx.Response(200, json={"data": [rows[0]], "pagination": {"next_cursor": "next"}})
        store = Store(":memory:")
        api = API(10000, transport=httpx.MockTransport(handler))
        engine = Engine(api, store)
        task = asyncio.create_task(engine.poll("all trades"))
        for _ in range(100):
            if engine.feeds["all trades"].pages >= 2:
                break
            await asyncio.sleep(.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert len(calls) == 2
        assert store.state("feed:all trades") == {"watermark": now}
        assert not engine.feeds["all trades"].backlog
        assert len(store.recent(alerts=True)) == 1
        await api.close()
        store.close()
    asyncio.run(run())


def test_bad_payload_and_finite_values():
    with pytest.raises(ValueError):
        Trade.parse(trade(price="NaN"))
    assert retry_seconds("invalid") == 1
    async def run():
        api = API(1000, transport=httpx.MockTransport(lambda _: httpx.Response(403)))
        with pytest.raises(ApiError, match="403"):
            await api.get("/v2/trades")
        await api.close()
    asyncio.run(run())


def test_equal_discovery_slots_and_immediate_board_profit():
    async def run():
        def handler(request):
            start = 5 if request.url.params.get("sort_by") == "VOLUME" else 1 if request.url.params.get("time_period") == "all" else 3
            rows = [{"user_id": f"0x{i}", "user_name": f"wallet{i}", "pnl": i * 100,
                     "volume": i * 1000, "rank": j + 1} for j, i in enumerate(range(start, start + 2))]
            return httpx.Response(200, json={"data": rows})
        api = API(10000, transport=httpx.MockTransport(handler))
        store = Store(":memory:")
        engine = Engine(api, store, wallet_count=4)
        task = asyncio.create_task(engine.seed())
        for _ in range(100):
            if engine.board_time:
                break
            await asyncio.sleep(.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert sum("profit" in p["cohorts"] for p in engine.wallets.values()) == 2
        assert sum("whale" in p["cohorts"] for p in engine.wallets.values()) == 2
        assert engine.wallets["0x1"]["all_pnl"] == 100
        assert engine.wallets["0x3"]["month_pnl"] == 300
        await api.close()
        store.close()
    asyncio.run(run())
