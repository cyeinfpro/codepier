"""Persistent Hub data compatibility declaration and launch preflight."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

CONTRACT_KEY = "room_host_connections_contract"
HOST_CONNECTIONS_MIN_VERSION = "1.33.9"
CONTRACT = {
    "version": 1,
    "min_reader_version": HOST_CONNECTIONS_MIN_VERSION,
    "min_writer_version": HOST_CONNECTIONS_MIN_VERSION,
}


def version_tuple(version):
    if not isinstance(version, str) or not re.fullmatch(
        r"[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}", version
    ):
        raise RuntimeError("Unknown Hub release version for this data store")
    return tuple(map(int, version.split(".")))


def initialize_fresh_contract(connection, version):
    if version_tuple(version) >= version_tuple(HOST_CONNECTIONS_MIN_VERSION):
        connection.execute(
            "INSERT INTO meta(key,value) VALUES (?,?)",
            (CONTRACT_KEY, json.dumps(CONTRACT, sort_keys=True)),
        )


def verify_compatibility(contract, version):
    if (
        not isinstance(contract, dict)
        or type(contract.get("version")) is not int
        or contract["version"] != 1
    ):
        raise RuntimeError("Unknown Hub data contract version")
    candidate = version_tuple(version)
    for key in ("min_reader_version", "min_writer_version"):
        if candidate < max(version_tuple(CONTRACT[key]), version_tuple(contract.get(key))):
            raise RuntimeError(
                f"Hub {version} cannot open this state; requires {key} >= {contract.get(key)}"
            )


def check_database(connection, version, *, required=False):
    has_meta = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
    ).fetchone()
    row = (
        connection.execute("SELECT value FROM meta WHERE key=?", (CONTRACT_KEY,)).fetchone()
        if has_meta
        else None
    )
    if row is None:
        if required:
            raise RuntimeError(
                "Independent host connections require a compatibility activation receipt"
            )
        return False
    try:
        contract = json.loads(row[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Invalid Hub data contract") from exc
    verify_compatibility(contract, version)
    return True


def check_path(directory, version, *, required=False):
    database = Path(directory).expanduser().resolve() / "hub.sqlite3"
    if database.is_symlink():
        raise RuntimeError("Hub database cannot be a symbolic link")
    if not database.exists() and not required:
        return False
    with closing(
        sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=5)
    ) as connection:
        connection.execute("PRAGMA query_only=ON")
        return check_database(connection, version, required=required)


def main():
    from shared.util import VERSION

    arguments = sys.argv[1:]
    state = (
        os.getenv("HUB_STATE_DIR")
        or os.getenv("HUB_DATA_DIR")
        or str(Path(__file__).resolve().parent.parent / "data")
    )
    if len(arguments) == 2 and arguments[0] == "--state-dir":
        state = arguments[1]
    elif arguments and (arguments[0] != "--exec" or len(arguments) == 1):
        raise SystemExit(
            "Usage: python -m shared.hub_data_contract [--state-dir DIR | --exec COMMAND ...]"
        )
    try:
        check_path(state, VERSION)
    except (RuntimeError, OSError, sqlite3.Error) as exc:
        raise SystemExit(str(exc)) from exc
    if arguments and arguments[0] == "--exec":
        os.execvp(arguments[1], arguments[1:])


if __name__ == "__main__":
    main()
