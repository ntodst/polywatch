import asyncio
import time

import httpx
import pytest

from polywatch.api import API
from polywatch.engine import Engine
from polywatch.models import account_status
from polywatch.store import Store
from polywatch.ui import ranked_wallets


def stats(realized=100, unrealized=-20, timestamp=None):
    return {"data": {"all_time_pnl": {"realized_pnl": realized, "unrealized_pnl": unrealized,
              "position_pnl": realized + unrealized if realized is not None and unrealized is not None else None,
              "timestamp": int(time.time()) if timestamp is None else timestamp}}}


def make_trade(wallet="0xa", token="yes", timestamp=None):
    return {"proxy_wallet": wallet, "side": "BUY", "price": .5, "size": 40000,
            "transaction_hash": wallet + token, "token_id": token,
            "condition_id": "0x" + "a" * 64, "timestamp": int(time.time()) if timestamp is None else timestamp}


def test_realized_winner_with_bigger_open_losses_is_excluded_before_expensive_calls(tmp_path):
    async def run():
        paths = []
        def handler(request):
            paths.append(request.url.path)
            assert "_" not in request.url.params
            return httpx.Response(200, json=stats(100000, -150000))
        api = API(10000, transport=httpx.MockTransport(handler))
        path = tmp_path / "watch.db"
        store = Store(path)
        engine = Engine(api, store)
        engine.wallets["0xa"] = {"address": "0xa", "cohorts": ["profit"]}
        await engine.profile("0xa")
        assert paths == ["/v2/user-stats"]
        assert engine.excluded("0xa")
        assert engine.wallets["0xa"]["account_pnl"] == -50000
        assert engine.wallets["0xa"]["cohorts"] == []
        assert not engine.snapshot()["wallets"]
        assert not ranked_wallets(engine, active_only=False)
        await engine.positions.refresh("0xa")
        assert len(paths) == 1
        engine.ingest([make_trade()])
        assert not store.last_inserted
        assert not store.recent(alerts=True)
        assert not engine.positions.pending
        engine.enqueue("0xa")
        assert engine.queue.empty()
        store.close()
        store = Store(path)
        restarted = Engine(api, store)
        assert restarted.excluded("0xa")
        assert not await restarted.check_account("0xa")
        assert len(paths) == 1  # no background checks for disqualified accounts
        store.close()
        await api.close()
    asyncio.run(run())


@pytest.mark.parametrize("realized,unrealized,expected", [(100, -20, True), (-100, 200, True), (100, -100, True), (-10, 0, False)])
def test_overall_pnl_not_just_realized_or_unrealized(realized, unrealized, expected):
    async def run():
        api = API(10000, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=stats(realized, unrealized))))
        store = Store(":memory:")
        engine = Engine(api, store)
        assert await engine.check_account("0xa") is expected
        assert engine.wallets["0xa"]["account_pnl"] == realized + unrealized
        await api.close()
        store.close()
    asyncio.run(run())


@pytest.mark.parametrize("payload", [
    {"data": None}, {"data": {"all_time_pnl": None}}, stats(100, None),
    stats(100, -20, timestamp=int(time.time()) - 7200),
    {"data": {"all_time_pnl": {"realized_pnl": 100, "unrealized_pnl": -200, "position_pnl": 100, "timestamp": int(time.time())}}},
])
def test_unknown_stale_or_inconsistent_pnl_is_pending_not_admitted(payload):
    async def run():
        api = API(10000, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
        store = Store(":memory:")
        engine = Engine(api, store)
        assert not await engine.check_account("0xa")
        assert account_status(engine.wallets["0xa"]) == "pending"
        engine.positions.request("0xa")
        assert not engine.positions.pending
        await api.close()
        store.close()
    asyncio.run(run())


def test_preexisting_alerts_trades_and_changes_are_hidden_after_rejection():
    async def run():
        api = API(10000, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=stats(100, -200))))
        store = Store(":memory:")
        engine = Engine(api, store)
        now = time.time()
        engine.wallets["0xa"] = {"address": "0xa", "cohorts": ["whale"], "account_pnl": 100,
                                 "pnl_checked_at": now, "pnl_observed_at": now}
        engine.ingest([make_trade()])
        assert engine.recent(alerts=True)
        row = {"proxy_wallet": "0xa", "token_id": "yes", "current_size": 40000}
        store.position_event("OPENED", row, 0, now)
        store.db.commit()
        assert engine.position_changes()
        assert engine.positions.pending
        await engine.check_account("0xa", force=True)
        assert not engine.recent(alerts=True)
        assert not engine.recent()
        assert not engine.position_changes()
        assert not engine.positions.pending
        # Historical database records survive for audit; they are not followed.
        assert store.recent(alerts=True)
        await api.close()
        store.close()
    asyncio.run(run())


def test_concurrent_checks_do_not_duplicate_api_work():
    async def run():
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json=stats())
        api = API(10000, transport=httpx.MockTransport(handler))
        store = Store(":memory:")
        engine = Engine(api, store)
        assert all(await asyncio.gather(*(engine.check_account("0xa") for _ in range(4))))
        assert len(calls) == 1
        await api.close()
        store.close()
    asyncio.run(run())


def test_eligibility_expires_and_unknown_alerts_stay_hidden_until_checked():
    async def run():
        api = API(10000, transport=httpx.MockTransport(lambda _: httpx.Response(200, json=stats())))
        store = Store(":memory:")
        engine = Engine(api, store)
        engine.ingest([make_trade()])
        assert not engine.recent(alerts=True)
        assert not engine.positions.pending
        await engine.check_account("0xa")
        assert engine.recent(alerts=True)
        p = engine.wallets["0xa"]
        assert account_status(p, now=p["pnl_checked_at"] + 301) == "pending"
        await api.close()
        store.close()
    asyncio.run(run())


def test_discovery_cannot_reintroduce_disqualified_wallet():
    async def run():
        def handler(request):
            return httpx.Response(200, json={"data": [{"user_id": "0xa", "user_name": "A", "pnl": 100000,
                                                      "volume": 1000000, "rank": 1}]})
        api = API(10000, transport=httpx.MockTransport(handler))
        store = Store(":memory:")
        engine = Engine(api, store)
        engine.wallets["0xa"] = {"address": "0xa", "cohorts": [], "disqualified": True, "account_pnl": -100}
        task = asyncio.create_task(engine.seed())
        for _ in range(100):
            if engine.board_time:
                break
            await asyncio.sleep(.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert engine.wallets["0xa"]["cohorts"] == []
        assert engine.queue.empty()
        assert engine.excluded("0xa")
        await api.close()
        store.close()
    asyncio.run(run())


def test_account_rejection_survives_a_concurrent_profile_replacement():
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            started.set()
            await release.wait()
            return httpx.Response(200, json=stats(100, -200))
        api = API(10000, transport=httpx.MockTransport(handler))
        store = Store(":memory:")
        engine = Engine(api, store)
        engine.wallets["0xa"] = {"address": "0xa", "cohorts": ["profit"]}
        check = asyncio.create_task(engine.check_account("0xa"))
        await started.wait()
        # Historical analysis can replace the cached profile during an API wait.
        engine.wallets["0xa"] = {**engine.wallets["0xa"], "name": "Updated profile"}
        release.set()
        assert not await check
        assert engine.excluded("0xa")
        assert engine.wallets["0xa"]["name"] == "Updated profile"
        assert store.wallets()["0xa"]["disqualified"]
        await api.close()
        store.close()
    asyncio.run(run())
