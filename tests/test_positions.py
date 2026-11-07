import asyncio
import time

import httpx
import pytest
from rich.console import Console

from polywatch.api import API
from polywatch.engine import Engine
from polywatch.positions import exposure, fresh, tradable
from polywatch.store import Store
from polywatch.ui import render, ranked_wallets

CONDITION = "0x" + "a" * 64


def position(wallet="0xa", token="yes", size=40000, mark=.5, **extra):
    return {"proxy_wallet": wallet, "token_id": token, "condition_id": CONDITION,
            "opposite_token_id": "no" if token == "yes" else "yes", "current_size": size,
            "current_value": size * mark, "entry_cost_usdc": size * .4,
            "avg_price": .4, "current_price": mark, "unrealized_pnl": size * (mark - .4),
            "status": "OPEN", "redeemable": False, "last_event_at": int(time.time()),
            "title": "Example", "outcome": token, "event_slug": "example", **extra}


def market(now=None, **extra):
    return {"conditionId": CONDITION, "active": True, "closed": False, "archived": False,
            "acceptingOrders": True, "enableOrderBook": True,
            "checked_at": time.time() if now is None else now, **extra}


@pytest.fixture
def engine():
    store = Store(":memory:")
    engine = Engine(None, store)
    engine.wallets = {"0xa": {"address": "0xa", "name": "A", "cohorts": ["whale"], "all_pnl": 1000},
                      "0xb": {"address": "0xb", "name": "B", "cohorts": ["profit"], "all_pnl": 2000}}
    for p in engine.wallets.values():
        p.update(account_pnl=1000, pnl_checked_at=time.time(), pnl_observed_at=time.time())
    engine.positions.markets[CONDITION] = market()
    yield engine
    store.close()


def test_only_verified_tradable_meaningful_positions_are_active(engine):
    manager = engine.positions
    manager.apply("0xa", [position()], True)
    assert len(manager.active()) == 1
    assert manager.active()[0]["last_change"] == "FIRST SEEN"
    assert len(ranked_wallets(engine)) == 1
    manager.apply("0xa", [position(size=100)], True)
    assert not manager.active()
    assert not ranked_wallets(engine)
    # History keeps inactive discovered accounts accessible.
    assert len(ranked_wallets(engine, active_only=False)) == 2


def test_large_original_bet_with_small_remaining_mark_is_not_active(engine):
    engine.positions.apply("0xa", [position(size=100000, mark=.001)], True)
    assert not engine.positions.active()


def test_old_catchup_trades_coalesce_and_already_verified_trades_are_skipped(engine):
    manager = engine.positions
    now = int(time.time())
    manager.request("0xa", "0x" + "b" * 64, {"token_id": "yes", "timestamp": now - 1000})
    original_seq = manager.pending[("0xa", None)]["seq"]
    manager.request("0xa", "0x" + "c" * 64, {"token_id": "no", "timestamp": now - 1000})
    assert set(manager.pending) == {("0xa", None)}
    assert manager.pending[("0xa", None)]["seq"] == original_seq
    manager.pending.clear()
    manager.apply("0xa", [position(last_event_at=now)], True)
    manager.request("0xa", CONDITION, {"token_id": "yes", "timestamp": now - 10})
    assert not manager.pending


def test_combo_structural_ids_do_not_break_standard_position_batches(engine):
    manager = engine.positions
    manager.request("0xa", "0x03" + "0" * 60, {"token_id": "combo", "timestamp": int(time.time())})
    assert not manager.pending
    manager.request("0xa", CONDITION, {"token_id": "yes", "timestamp": int(time.time())})
    assert ("0xa", CONDITION) in manager.pending


@pytest.mark.parametrize("changes", [{"closed": True}, {"active": False}, {"acceptingOrders": False},
                                    {"archived": True}, {"enableOrderBook": False},
                                    {"umaResolutionStatus": "resolved"}, {"acceptingOrders": None}])
def test_market_availability_must_be_explicit(engine, changes):
    engine.positions.apply("0xa", [position()], True)
    engine.positions.markets[CONDITION] = market(**changes)
    assert not engine.positions.active()


def test_settled_unredeemed_winners_and_losers_are_excluded(engine):
    for label, mark in (("win", 1), ("lose", 0)):
        engine.positions.apply("0xa", [position(token=label, mark=mark, status="REDEEMABLE", redeemable=True)], False)
    assert not engine.positions.active()


def test_past_scheduled_end_is_excluded_even_if_orders_are_still_accepted(engine):
    manager = engine.positions
    manager.apply("0xa", [position()], True)
    manager.markets[CONDITION] = market(endDate="2020-01-01T12:00:00Z")
    assert not manager.active()
    manager.markets[CONDITION] = market(endDate="2099-01-01T12:00:00Z")
    assert manager.active()


def test_position_and_market_expire_independently(engine):
    now = time.time()
    engine.positions.apply("0xa", [position()], True, now=now)
    assert engine.positions.active(now=now + 119)
    assert not engine.positions.active(now=now + 121)
    engine.positions.apply("0xa", [position()], True, now=now + 130)
    assert not engine.positions.active(now=now + 130)  # fresh balance, expired market
    engine.positions.markets[CONDITION] = market(now + 130)
    assert engine.positions.active(now=now + 130)


def test_confirmed_add_reduce_exit_and_reopen_are_persisted(engine):
    manager = engine.positions
    for size in (40000, 50000, 25000, 0, 40000):
        manager.apply("0xa", [position(size=size, status="CLOSED" if size == 0 else "OPEN")], True)
    assert [r["kind"] for r in engine.store.position_events()] == ["OPENED", "EXITED", "REDUCED", "ADDED", "FIRST SEEN"]
    assert manager.rows[("0xa", "yes")]["last_change"] == "OPENED"
    assert engine.store.load_positions()[("0xa", "yes")]["current_size"] == 40000


def test_new_acquisition_after_complete_baseline_is_opened_not_historical_first_seen(engine):
    now = time.time()
    manager = engine.positions
    manager.apply("0xa", [], True, now=now - 10)
    manager.apply("0xa", [position(first_entry_at=int(now) - 5)], True, now=now)
    assert engine.store.position_events()[0]["kind"] == "OPENED"
    assert engine.store.position_events()[0]["previous_size"] == 0


def test_historical_buys_do_not_create_live_agreement_alerts(engine):
    rows = [{"proxy_wallet": addr, "side": "BUY", "price": .5, "size": 20000,
             "transaction_hash": addr, "token_id": "yes", "condition_id": CONDITION, "timestamp": int(time.time())}
            for addr in ("0xa", "0xb")]
    engine.ingest(rows)
    assert not any(r["alert_kind"] == "CO-BUY" for r in engine.store.recent(alerts=True))
    assert not engine.positions.active()


def test_partial_and_empty_responses_never_synthesize_an_exit(engine):
    manager = engine.positions
    manager.apply("0xa", [position()], True)
    manager.apply("0xa", [], False)
    assert manager.rows[("0xa", "yes")]["current_size"] == 40000
    assert not manager.rows[("0xa", "yes")].get("missing")
    manager.apply("0xa", [], True)
    assert manager.rows[("0xa", "yes")]["current_size"] == 40000
    assert manager.rows[("0xa", "yes")]["missing"]
    assert not manager.active()
    assert not any(r["kind"] == "EXITED" for r in engine.store.position_events())


def test_lagging_closed_record_cannot_generate_premature_exit(engine):
    now = int(time.time())
    manager = engine.positions
    manager.apply("0xa", [position(last_event_at=now - 10)], True)
    manager.request("0xa", CONDITION, {"token_id": "yes", "timestamp": now})
    manager.apply("0xa", [position(size=0, status="CLOSED", last_event_at=now - 5)], True, conditions=[CONDITION])
    assert manager.rows[("0xa", "yes")]["current_size"] == 40000
    assert manager.rows[("0xa", "yes")]["verification_pending"]
    assert not any(r["kind"] == "EXITED" for r in engine.store.position_events())
    manager.apply("0xa", [position(size=0, status="CLOSED", last_event_at=now)], True, conditions=[CONDITION])
    assert engine.store.position_events()[0]["kind"] == "EXITED"
    assert fresh(manager.rows[("0xa", "yes")])


def test_trade_verification_only_waits_for_the_traded_token(engine):
    now = int(time.time())
    manager = engine.positions
    manager.apply("0xa", [position(last_event_at=now - 10), position(token="no", size=100, last_event_at=now - 50)], True)
    manager.request("0xa", CONDITION, {"token_id": "yes", "timestamp": now})
    jobs = {k: dict(v) for k, v in manager.pending.items()}
    manager.apply("0xa", [position(size=45000, last_event_at=now), position(token="no", size=100, last_event_at=now - 50)], True, conditions=[CONDITION])
    manager._settle_requests("0xa", jobs)
    assert not manager.pending
    assert manager.active()


def test_equal_complementary_shares_are_fully_hedged(engine):
    manager = engine.positions
    manager.apply("0xa", [position(size=40000, mark=.7), position(token="no", size=40000, mark=.3)], True)
    assert not manager.active()
    assert exposure(manager.rows[("0xa", "yes")], manager.rows)["net_shares"] == 0
    manager.apply("0xa", [position(size=80000, mark=.7), position(token="no", size=40000, mark=.3)], True)
    active = manager.active()
    assert len(active) == 1
    assert active[0]["net_shares"] == 40000
    assert active[0]["net_value"] == 28000


def test_agreement_drops_after_sell_and_excludes_pending_stale_holders(engine):
    manager = engine.positions
    manager.apply("0xa", [position()], True)
    manager.apply("0xb", [position(wallet="0xb")], True)
    assert all(r["profitable_holders"] == 2 for r in manager.active())
    manager.apply("0xb", [position(wallet="0xb", size=0, status="CLOSED")], True)
    assert manager.active()[0]["profitable_holders"] == 1
    manager.apply("0xb", [position(wallet="0xb")], True)
    manager.request("0xb", CONDITION, {"token_id": "yes", "timestamp": int(time.time()) + 5})
    assert manager.active()[0]["profitable_holders"] == 1


def test_overall_negative_wallet_cannot_count_even_with_positive_lifetime_realized(engine):
    manager = engine.positions
    manager.apply("0xa", [position()], True)
    manager.apply("0xb", [position(wallet="0xb")], True)
    engine.wallets["0xb"]["account_pnl"] = -100
    assert len(manager.active()) == 1
    assert manager.active()[0]["profitable_holders"] == 1


def test_overall_positive_wallet_can_count_despite_negative_realized_result(engine):
    engine.positions.apply("0xa", [position()], True)
    engine.wallets["0xa"]["all_pnl"] = -100
    assert engine.positions.active()[0]["profitable_holders"] == 1


def test_missing_market_metadata_is_unknown(engine):
    engine.positions.apply("0xa", [position()], True)
    engine.positions.markets.clear()
    assert not engine.positions.active()


def test_partial_book_cannot_claim_unhedged_exposure_without_opposite_coverage(engine):
    manager = engine.positions
    manager.apply("0xa", [position()], False)
    assert not manager.active()
    assert ("0xa", CONDITION) in manager.pending
    manager.apply("0xa", [position(), position(token="no", size=0, status="CLOSED")], True, conditions=[CONDITION])
    assert manager.active()


def test_old_zero_opposite_does_not_hide_new_complete_open_book(engine):
    now = time.time()
    manager = engine.positions
    manager.apply("0xa", [position(token="no", size=0, status="CLOSED")], True, now=now - 200)
    manager.apply("0xa", [position()], True, now=now)
    assert manager.active()


def test_small_fills_from_tracked_wallet_trigger_priority_verification(engine):
    row = {"proxy_wallet": "0xa", "side": "SELL", "price": .5, "size": 10,
           "transaction_hash": "tx", "token_id": "yes", "condition_id": CONDITION, "timestamp": int(time.time())}
    engine.positions.apply("0xa", [position()], True)
    engine.ingest([row])
    assert ("0xa", CONDITION) in engine.positions.pending
    assert engine.positions.rows[("0xa", "yes")]["verification_pending"]
    seq = engine.positions.pending[("0xa", CONDITION)]["seq"]
    engine.ingest([row])
    assert engine.positions.pending[("0xa", CONDITION)]["seq"] == seq


def test_refresh_requires_explicit_closed_balance_for_exit(engine):
    async def run():
        manager = engine.positions
        manager.apply("0xa", [position()], True)
        requests = []
        def handler(request):
            requests.append(request)
            closed = request.url.params.get("status") == "CLOSED"
            return httpx.Response(200, json={"data": [position(size=0, status="CLOSED")] if closed else [],
                                            "pagination": {"next_cursor": None}})
        api = API(10000, transport=httpx.MockTransport(handler))
        manager.api = api
        await manager.refresh("0xa")
        assert len(requests) == 2
        assert requests[1].url.params["condition"] == CONDITION
        assert manager.rows[("0xa", "yes")]["current_size"] == 0
        assert engine.store.position_events()[0]["kind"] == "EXITED"
        await api.close()
    asyncio.run(run())


def test_market_batch_arrays_and_shared_api_limiter(engine):
    async def run():
        engine.positions.apply("0xa", [position()], True)
        engine.positions.markets.clear()
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=[market()])
        api = API(10000, transport=httpx.MockTransport(handler))
        engine.positions.api = api
        task = asyncio.create_task(engine.positions.market_loop())
        for _ in range(100):
            if engine.positions.markets:
                break
            await asyncio.sleep(.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert requests[0].url.host == "gamma-api.polymarket.com"
        assert requests[0].url.params.get_list("condition_ids") == [CONDITION]
        assert engine.positions.active()
        await api.close()
    asyncio.run(run())


def test_all_terminal_views_render_pending_and_verified_states(engine):
    class Counters:
        requests = retries = 0
        cache_age = 0
    engine.api = Counters()
    engine.positions.apply("0xa", [position()], True)
    engine.selected = "0xa"
    for width in (80, 140):
        for view in ("live", "wallets", "history", "detail", "alerts", "changes", "trades"):
            console = Console(width=width)
            with console.capture() as capture:
                console.print(render(engine, view, height=40))
            output = capture.get()
            assert output and "Traceback" not in output
            if view == "live" and width == 140:
                assert "Net shares" in output
                assert "FIRST SEEN" in output
