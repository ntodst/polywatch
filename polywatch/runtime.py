"""Local lifecycle and bounded, validated user preferences."""
import fcntl
import json
import os
from pathlib import Path


class InstanceLock:
    def __init__(self, database):
        self.path = Path(str(Path(database).resolve()) + ".lock")
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+")
        try:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file.close()
            self.file = None
            raise RuntimeError("A watcher is already running for this database. Use its dashboard or stop it first.") from None
        self.file.seek(0)
        self.file.truncate()
        self.file.write(str(os.getpid()))
        self.file.flush()
        return self

    def __exit__(self, *_):
        if self.file:
            fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close()
            self.file = None


DEFAULTS = {"query": "", "category": "All", "min_value": 10000.0, "min_holders": 1,
            "recent_minutes": 0, "sort": "exposure", "pins": [], "size": 1000.0,
            "pinned_only": False, "tab": "markets", "pin_contexts": {}}


def preferences(store):
    saved = store.state("ui_preferences", {})
    result = {**DEFAULTS, "pins": [], "pin_contexts": {}}
    if not isinstance(saved, dict):
        return result
    for key in ("query", "category"):
        if isinstance(saved.get(key), str):
            result[key] = saved[key][:200]
    for key, low, high in (("min_value", 0, 1e10), ("min_holders", 1, 10000),
                           ("size", 1, 1e6), ("recent_minutes", 0, 10080)):
        try:
            value = float(saved[key])
            if low <= value <= high:
                result[key] = int(value) if key in ("min_holders", "recent_minutes") else value
        except (KeyError, ValueError, TypeError):
            pass
    if saved.get("sort") in ("exposure", "holders", "change", "profit"):
        result["sort"] = saved["sort"]
    if saved.get("tab") in ("markets", "wallets", "alerts"):
        result["tab"] = saved["tab"]
    result["pins"] = [p for p in saved.get("pins", []) if isinstance(p, str) and p.isdecimal()][:50] if isinstance(saved.get("pins"), list) else []
    result["pinned_only"] = saved.get("pinned_only") is True
    contexts = saved.get("pin_contexts", {})
    if isinstance(contexts, dict):
        for token in result["pins"]:
            row = contexts.get(token)
            if isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("condition_id"), str):
                result["pin_contexts"][token] = {k: row[k] for k in ("id", "title", "outcome", "condition_id", "token_id", "outcome_index", "category") if k in row}
    return result
