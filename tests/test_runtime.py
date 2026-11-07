import subprocess
import sys

import pytest

from polywatch.engine import Engine
from polywatch.runtime import InstanceLock, preferences
from polywatch.store import Store


def test_single_instance_lock_blocks_second_process_before_database_or_network(tmp_path):
    database = tmp_path / "watch.db"
    with InstanceLock(database):
        result = subprocess.run([sys.executable, "watch.py", "--once", "--json", "--db", str(database)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 1
        assert "already running" in result.stderr
        assert not database.exists()
    with InstanceLock(database):
        pass  # stale PID text is not a stale lock


def test_preferences_are_validated_and_saved_without_weakening_account_filter():
    store = Store(":memory:")
    store.save_state("ui_preferences", {"size": float("nan"), "min_value": -1, "pins": ["123", "bad"], "query": "hello", "sort": "holders"})
    prefs = preferences(store)
    assert prefs["size"] == 1000 and prefs["min_value"] == 10000
    assert prefs["pins"] == ["123"] and prefs["query"] == "hello"
    assert prefs["sort"] == "holders"
    store.close()


def test_startup_does_not_decode_entire_persisted_position_catalog():
    store = Store(":memory:")
    for i in range(1000):
        store.save_position("0xa", str(i), {"token_id": str(i), "proxy_wallet": "0xa", "current_value": 1})
    store.db.commit()
    engine = Engine(None, store)
    assert engine.positions.rows == {}
    assert store.db.execute("SELECT COUNT(*) FROM position_snapshots").fetchone()[0] == 1000
    store.close()
