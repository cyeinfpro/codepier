"""Cross-request secret filtering, independent shutdown and bounded maintenance."""
import copy
import json
import sqlite3

import pytest
from starlette.testclient import TestClient

from hub.app import create_app
from hub.operation_history import PREFIX, unseal
from hub.store import Store
from scripts.maintain_history import run
from shared.instance_lock import InstanceLock
from shared.util import safe_summary
from tests.test_history_retention_execution import insert


@pytest.mark.parametrize("shape", [
    {"upload_id": "u1", "data": "binary-private"},
    {"action": "fill", "value": "browser-private"},
    {"action": "navigate", "url": "https://private.invalid/"},
    {"action": {"type": "type_text", "text": "native-private", "token": "nested-token"}},
    {"env": {"SAMPLE": "environment-private"}},
])
def test_sensitive_siblings_never_bypass_shape_filters(shape):
    raw = {**shape, "password": "password-private", "content": "content-private",
           "token": "token-private", "nested": [{"authorization": "authorization-private"}]}
    before = copy.deepcopy(raw)
    summary = safe_summary(raw)
    output = json.dumps(summary)
    for secret in ("password-private", "content-private", "token-private",
                   "authorization-private", "binary-private", "browser-private",
                   "native-private", "nested-token", "environment-private"):
        assert secret not in output
    assert raw == before


def test_rollout_shutdown_failure_still_closes_oidc_and_database(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "hub"))
    closed = []
    original_oidc_stop = app.state.oidc.stop
    original_rollout_stop = app.state.runtime.panel_agent_rollouts.stop

    async def fail_rollout():
        await original_rollout_stop()
        raise RuntimeError("injected rollout shutdown")

    async def oidc_stop():
        await original_oidc_stop()
        closed.append("oidc")

    monkeypatch.setattr(app.state.runtime.panel_agent_rollouts, "stop", fail_rollout)
    monkeypatch.setattr(app.state.oidc, "stop", oidc_stop)
    with pytest.raises(RuntimeError, match="injected rollout shutdown"):
        with TestClient(app):
            pass
    assert closed == ["oidc"]
    with pytest.raises(sqlite3.ProgrammingError):
        app.state.store.one("SELECT 1")
    lock = InstanceLock(tmp_path / "hub" / ".hub.lock")
    lock.close()


def test_legacy_encryption_cli_preview_apply_and_resume_preserve_receipts(tmp_path):
    directory = tmp_path / "hub"
    store = Store(directory)
    insert(store, "one")
    insert(store, "two", "running")
    original = store.all("SELECT * FROM operations ORDER BY id")
    store.close()
    report = run(directory, encrypt_legacy=True, batch=1)
    assert report["dry_run"] and report["fields"]["operations.result"] == 1
    readonly = sqlite3.connect(directory / "hub.sqlite3")
    assert readonly.execute("SELECT result FROM operations ORDER BY id").fetchall() == [
        (row["result"],) for row in original
    ]
    readonly.close()
    run(directory, apply=True, encrypt_legacy=True, batch=1)
    run(directory, apply=True, encrypt_legacy=True, batch=1)
    assert not any(run(directory, encrypt_legacy=True)["fields"].values())
    store = Store(directory)
    try:
        for before, after in zip(original, store.all("SELECT * FROM operations ORDER BY id"), strict=True):
            assert after["result"].startswith(PREFIX)
            assert unseal(store, after["result"]) == before["result"]
            assert unseal(store, after["output"]) == before["output"]
            assert (after["id"], after["fingerprint"], after["updated"], after["state"]) == (
                before["id"], before["fingerprint"], before["updated"], before["state"])
    finally:
        store.close()
