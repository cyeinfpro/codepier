"""Late results acknowledge the original file without republishing under another mapping."""

import json
import time
from types import SimpleNamespace

import pytest

from shared.util import DevError
from tests.collaboration_support import collab  # noqa: F401


@pytest.mark.parametrize("legacy", [False, True], ids=["retained-binding", "missing-binding"])
def test_late_artifact_result_survives_review_and_project_remap(collab, legacy):
    service, _, principal, *_ = collab
    store, runtime = service.store, service.runtime
    now = time.time()
    identifier = "f" * 32

    def operation():
        return store.one("SELECT * FROM operations WHERE id=?", (identifier,))

    payload = {
        "tool": "artifacts_register",
        "project": {"root": "/original"},
        "args": {"source_operation_id": "original-source", "path": "private-file.txt"},
        "principal_context": {"should_not_persist": True},
    }
    store.execute(
        """INSERT INTO operations
        (id,device_id,project_id,actor,grant_id,tool,args_summary,fingerprint,state,
        payload,created,updated,space_id,owner_user_id,accepted_at,journal_id)
        VALUES (?,'dev','proj',?,'worker','artifacts_register','{}','fp','running',
                ?,?,?,'legacy','owner',?,'journal')""",
        (identifier, principal.actor, store.encrypt(json.dumps(payload)), now, now, now),
    )
    conn = SimpleNamespace(journal_id="journal")
    runtime.connections["dev"] = conn

    def frame(**data):
        return runtime._receive_operation_event(
            "dev", conn, {"id": identifier, **data}, authenticated=True
        )

    frame(type="status", status="missing")
    pending = operation()
    assert pending["state"] == "needs_review"
    assert runtime._prepare_delivery(identifier) is None
    assert json.loads(store.decrypt(pending["payload"])) == {
        "project": {"root": "/original"},
        "args": {"source_operation_id": "original-source"},
    }
    if legacy:
        store.execute("UPDATE operations SET payload=NULL WHERE id=?", (identifier,))
    store.execute("UPDATE projects SET root='/other' WHERE id='proj'")
    receipt = {
        "artifact_id": identifier,
        "name": "release.tgz",
        "sha256": "a" * 64,
        "bytes": 3,
        "created": now - 30,
        "expires": now + 60,
    }
    sent = {"ok": True, "data": {**receipt, "download_path": "/untrusted"}}
    assert frame(type="result", result=sent) == {"type": "ack", "id": identifier}
    op = operation()
    assert op["payload"] is None
    artifact = store.one("SELECT * FROM artifacts WHERE id=?", (identifier,))
    if legacy:
        assert op["state"] == "needs_review" and artifact is None
        saved = runtime.operation_result(op)
        assert saved["error"]["code"] == "ARTIFACT_BINDING_UNAVAILABLE"
        assert saved["error"]["artifact_receipt"] == receipt
    else:
        assert op["state"] == "succeeded"
        assert (
            artifact["root"] == "/original" and artifact["source_operation_id"] == "original-source"
        )
        assert artifact["expires"] == receipt["expires"]
        with pytest.raises(DevError) as exc:
            runtime.artifacts.row(identifier, principal)
        assert exc.value.code == "ARTIFACT_MAPPING_CHANGED"
    assert frame(type="result", result=sent) == {"type": "ack", "id": identifier}
    assert operation()["updated"] == op["updated"]
