"""Durable evidence and conservative ranking; no live API access required."""
import json
import time

from polywatch.engine import Engine
from polywatch.research import DAY, series_stats
from polywatch.store import Store

WALLET = "0x" + "a" * 40


def profile(now=None, **updates):
    now = time.time() if now is None else now
    return {"address": WALLET, "cohorts": ["profit"], "name": "Researcher",
            "account_pnl": 1000, "account_realized_pnl": 1100, "account_unrealized_pnl": -100,
            "pnl_checked_at": now, "pnl_observed_at": now, "updated": now,
            "all_pnl": 1100, "month_pnl": 300, "consistent": True,
            "closed_complete": True, "closed_markets": 40, **updates}


def test_retained_evidence_and_losing_sources_survive_maintenance_and_restart(tmp_path):
    path = tmp_path / "research.sqlite3"
    store = Store(path)
    old = time.time() - 200 * DAY
    raw = {"proxy_wallet": WALLET, "token_id": "123", "condition_id": "condition",
           "timestamp": int(old), "size": 40000, "price": .5,
           "transaction_hash": "retained", "side": "BUY"}
    store.ingest([raw], {WALLET}, 10000)
    store.ingest([{**raw, "transaction_hash": "buffer", "size": 1}], set(), 10000)
    store.position_event("EXITED", {"proxy_wallet": WALLET, "token_id": "123"}, 200, old)
    store.save_paper({"id": "loss", "wallet": WALLET, "token": "123", "condition": "condition",
                      "detected_at": old, "closed_at": old + DAY, "status": "closed", "pnl": -200})
    store.wallet(WALLET, profile(disqualified=True, cohorts=[], account_pnl=-50,
                                 account_realized_pnl=50, account_unrealized_pnl=-100))
    store.prune()
    store.close()
    store = Store(path)
    engine = Engine(None, store)
    assert engine.excluded(WALLET)
    assert not engine.recent()
    assert store.db.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert store.recent(alerts=True)
    assert store.position_events()
    assert engine.research.paper_summary()[WALLET]["pnl"] == -200
    assert engine.research.archive("trades")["total"] == 1
    store.close()


def test_repeated_source_observations_are_deduplicated_and_imports_do_not_mature_local_evidence():
    store = Store(":memory:")
    engine = Engine(None, store)
    p = profile()
    engine.wallets[WALLET] = p
    store.wallet(WALLET, p)
    store.wallet(WALLET, {**p, "pnl_checked_at": p["pnl_checked_at"] + 1})
    assert store.db.execute("SELECT COUNT(*) FROM account_observations").fetchone()[0] == 1
    points = [{"timestamp": time.time() - i * DAY, "position_pnl": 1000 - i} for i in range(180)]
    engine.research.save_history(WALLET, points)
    row = engine.research.traders()[0]
    assert row["stage"] == "building"
    assert row["local"]["observation_days"] == 0
    assert row["local"]["daily_samples"] == 0
    assert len(engine.research.detail(WALLET)["backfill"]) == 180
    store.close()


def test_missing_days_do_not_create_zero_returns_or_covered_samples():
    points = [{"at": 10 * DAY, "pnl": 100}, {"at": 11 * DAY, "pnl": 150},
              {"at": 14 * DAY, "pnl": 50}, {"at": 15 * DAY, "pnl": 75}]
    stats = series_stats(points)
    assert stats["daily_samples"] == 2
    assert stats["positive_days"] == 1
    assert stats["net_pnl"] == -25
    assert stats["drawdown"] == 100


def test_long_term_screen_requires_prospective_profitable_evidence_and_fresh_eligibility():
    store = Store(":memory:")
    engine = Engine(None, store)
    now = time.time()
    p = profile(now)
    engine.wallets[WALLET] = p
    for i in range(93):
        observed = now - (92 - i) * DAY
        store.db.execute("INSERT INTO account_observations VALUES(?,?,?,?,?,?,?)",
                         (WALLET, observed, observed, i * 10, i * 10, 0, json.dumps({"account_pnl": i * 10})))
    assert engine.research.traders()[0]["stage"] == "building"
    for i in range(20):
        at = now - (45 - 2 * i) * DAY
        store.save_paper({"id": str(i), "wallet": WALLET, "token": str(i), "condition": str(i),
                          "detected_at": at, "closed_at": at + DAY, "status": "closed", "pnl": 10})
    assert engine.research.traders()[0]["stage"] == "long_term"
    # Unknown PnL cannot silently disappear from an otherwise profitable sample.
    r = store.papers(limit=1)[0]
    store.save_paper({**r, "pnl": None})
    assert engine.research.traders()[0]["stage"] == "building"
    store.save_paper(r)
    engine.wallets[WALLET]["pnl_checked_at"] = now - 600
    assert engine.research.traders()[0]["stage"] == "pending"
    engine.wallets[WALLET]["disqualified"] = True
    assert engine.research.traders()[0]["stage"] == "excluded"
    store.close()


def test_journal_preserves_state_changes_without_verification_clock_noise(tmp_path):
    store = Store(tmp_path / "journal.sqlite3")
    row = {"proxy_wallet": WALLET, "token_id": "123", "current_size": 100,
           "current_value": 50, "current_price": .5, "verified_at": time.time()}
    store.save_position(WALLET, "123", row)
    store.save_position(WALLET, "123", {**row, "verified_at": row["verified_at"] + 10})
    store.save_position(WALLET, "123", {**row, "current_price": .4, "current_value": 40})
    assert store.db.execute("SELECT COUNT(*) FROM research_journal WHERE kind='position'").fetchone()[0] == 2
    assert store.position(WALLET, "123")["current_value"] == 40
    store.close()
