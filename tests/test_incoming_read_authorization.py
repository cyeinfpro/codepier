"""Query-only receipt reads preserve live authority and exact completion proof."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import hub.incoming_files as incoming
from shared.util import DevError
from tests.test_incoming_files import ingress as ingress, lost_finish_receipt
from tests.test_iam_integration import team as team


@pytest.mark.parametrize("denial", ["revoked", "remapped", "pending", "expired", "wrong_workspace"])
def test_read_pool_finish_authorization_fails_closed(ingress, monkeypatch, denial):
    app, _, _, _, _, _ = ingress
    identifier, principal, row, completed = lost_finish_receipt(ingress)
    store = app.state.store
    if denial == "revoked":
        store.execute("DELETE FROM sessions")
    elif denial == "remapped":
        store.execute("UPDATE projects SET root=root||'/changed' WHERE id='project-team'")
    elif denial == "pending":
        store.execute("UPDATE operations SET state='queued' WHERE id=?", (identifier,))
    elif denial == "expired":
        store.execute("UPDATE operations SET result=? WHERE id=?",
                      (json.dumps({"ok": True, "data": {**completed, "expires": row["expires"]}}), identifier))
    else:
        operation = store.one("SELECT args_summary FROM operations WHERE id=?", (identifier,))
        summary = {**json.loads(operation["args_summary"]), "workspace_id": "a" * 32}
        store.execute("UPDATE operations SET args_summary=? WHERE id=?", (json.dumps(summary), identifier))
    # Revocation and remapping must reject even before the transfer lease ends.
    # Pending/wrong-workspace are invalid *completion proofs*: reads are valid
    # while the original lease lives, but cannot extend that lease afterwards.
    before_expiry = row["expires"] - 1
    if denial in {"pending", "wrong_workspace"}:
        monkeypatch.setattr(incoming, "time", SimpleNamespace(time=lambda: before_expiry))
        result = asyncio.run(store.run_read(app.state.runtime.operation, identifier, principal))
        assert (result.get("id") or result.get("operation_id")) == identifier
    now = before_expiry if denial in {"revoked", "remapped"} else row["expires"] + 1
    monkeypatch.setattr(incoming, "time", SimpleNamespace(time=lambda: now))
    with pytest.raises(DevError) as failure:
        asyncio.run(store.run_read(app.state.runtime.operation, identifier, principal))
    if denial == "remapped":
        assert failure.value.code == "FILE_IMPORT_MAPPING_CHANGED"
    elif denial == "revoked":
        assert failure.value.code != "FILE_IMPORT_EXPIRED"
    else:
        assert failure.value.code == "FILE_IMPORT_EXPIRED"
    saved = store.one("SELECT * FROM incoming_file_imports WHERE request_key=?", (row["request_key"],))
    assert saved == row


def test_read_pool_exact_completion_extends_lease_without_mutating_manifest(ingress, monkeypatch):
    app, _, _, _, _, _ = ingress
    identifier, principal, row, completed = lost_finish_receipt(ingress)
    monkeypatch.setattr(incoming, "time", SimpleNamespace(time=lambda: row["expires"] + 1))
    result = asyncio.run(app.state.store.run_read(app.state.runtime.operation, identifier, principal))
    assert result["state"] == "succeeded"
    assert result["result"]["data"]["expires"] == completed["expires"]
    assert result["result"]["data"]["ready"] is True
    assert app.state.store.one("SELECT * FROM incoming_file_imports WHERE request_key=?", (row["request_key"],)) == row
