"""Explicit opt-in separation of versioned state from stable user files."""
from __future__ import annotations
import json
import os
from pathlib import Path

STATE_LAYOUT = "split-state-v1"
STATE_MARKER = ".codepier-state.json"
STATE_SNAPSHOT_REQUIRED = frozenset({"hub.sqlite3", "master.key", STATE_MARKER})
STATE_SNAPSHOT_FILES = STATE_SNAPSHOT_REQUIRED | {"master.keys.json", "native-cli/native.sqlite3"}

def state_directory(data_directory: str | Path) -> Path:
    data = Path(data_directory).expanduser().resolve()
    configured = os.getenv("HUB_STATE_DIR", "")
    if not configured:
        return data
    raw = Path(configured).expanduser()
    if not raw.is_absolute() or raw.is_symlink():
        raise RuntimeError("HUB_STATE_DIR must be an absolute non-symlink initialized directory")
    state = raw.resolve()
    if state == data or data in state.parents or state in data.parents:
        raise RuntimeError("State and asset roots must be separate")
    marker = state / STATE_MARKER
    if marker.is_symlink() or not marker.is_file() or marker.stat().st_size > 4096:
        raise RuntimeError("Split state is not initialized; refusing an empty database")
    value = json.loads(marker.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or value.get("layout") != STATE_LAYOUT:
        raise RuntimeError("Unknown split-state initialization receipt")
    for name in ("hub.sqlite3", "master.key"):
        path = state / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError("Split state identity is incomplete; restore matching database and keys")
    return state
