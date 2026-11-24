"""Hub vps_ssh_v2 compatibility, migration, capability, and recovery tests.

Covers T04, T05, T06, T10, T11, T12, T17, T24, T28, T30, T31.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from types import SimpleNamespace

import pytest

from hub import iam
from hub.collaboration.delegation import effective_target_projection
from hub.collaboration.room_input_authority import vps_target_update_trigger_sql
from hub.runtime import Principal, Runtime
from hub.store import Store
from hub.vps import VPSAssignments, VPSInput
from hub.vps_connection import (
    compute_vps_capabilities,
    hub_native_readiness_capability,
    resolve_vps_connection_plan,
)
from hub.vps_readiness import HOST_VERIFICATION_REQUIRED, HOST_VERIFICATION_SKIP
from hub.vps_readiness_native import NativeSSHTarget, NativeVPSReadiness
from hub.vps_readiness_wiring import VPSReadinessWiring
from shared.util import DevError
from shared.vps_transport import (
    VPS_HOST_IDENTITY_VERIFICATION,
    VPS_SSH_PROTOCOL,
    agent_vps_ssh_capability,
)


PASSWORD = "fixture-vps-password-2026"


def _make_runtime(tmp_path):
    store = Store(tmp_path / "hub-data")
    from tests.legacy_iam_fixture import seed_owner

    seed_owner(store, "owner", "owner")
    device_id = uuid.uuid4().hex
    store.execute(
        "INSERT INTO devices(id,name,secret,created) VALUES (?,?,?,?)",
        (device_id, "Fixture Agent", store.encrypt("dev-secret"), time.time()),
    )
    project_id = uuid.uuid4().hex
    store.execute(
        "INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            project_id,
            "Alpha",
            "alpha",
            device_id,
            str(tmp_path / "alpha"),
            "",
            "write",
            1,
            time.time(),
        ),
    )
    runtime = Runtime(store)
    owner = Principal("panel:owner", "owner", {"read", "write", "execute"}, [], admin=True)
    return store, runtime, owner, device_id, project_id


def _fake_agent(
    *,
    exec_ok=True,
    terminal_ok=True,
    probe_ok=True,
    legacy=False,
    journal_id="journal-v2",
):
    packets = []

    class FakeCon:
        def __init__(self):
            self.journal_id = journal_id
            self.unusable = False
            self.closed = False
            self.last_seen = time.time()
            self.tool_protocol = None
            self.resource_semantics_protocol = 2
            self.file_options_protocol = 1
            self.tree_options_protocol = 1
            self.bounded_execution_protocol = 1
            self.cancel_pending_protocol = 1
            self.agent_vps_ssh = (
                None
                if legacy
                else agent_vps_ssh_capability(
                    available=True,
                    exec_supported=exec_ok,
                    terminal_supported=terminal_ok,
                    probe_supported=probe_ok,
                )
            )

        async def send(self, packet, *, before_send=None):
            if before_send is not None and not await before_send():
                return False
            packets.append(packet)
            return True

    return FakeCon(), packets


def test_t04_t05_t06_legacy_policies_malformed_pins_and_metadata_edits(tmp_path):
    """T04/T05/T06: Legacy strict/accept-new/confirm-change & invalid pins never block or bump revision."""
    store, runtime, owner, _device_id, project_id = _make_runtime(tmp_path)
    try:
        for idx, (policy, pin) in enumerate(
            [
                ("strict", ""),
                ("accept-new", "not-a-valid-sha256-pin!!!"),
                ("confirm-change", ""),
                ("confirm-change", "MALFORMED_PIN_12345"),
            ],
            start=1,
        ):
            saved = runtime.vps.save(
                VPSInput(
                    name=f"Legacy-{idx}",
                    host=f"legacy-{idx}.example.invalid",
                    password=PASSWORD,
                    host_key_policy=policy,
                    host_key_fingerprint=pin,
                    project_ids=[project_id],
                ),
                owner,
            )
            assert saved["connection_contract"] == VPS_SSH_PROTOCOL
            assert saved["host_identity_verification"] == VPS_HOST_IDENTITY_VERIFICATION
            assert saved["credential_available"] is True
            assert "host_key_policy" not in saved
            assert "host_key_fingerprint" not in saved
            assert "saved_vps_host_verification" not in saved
            assert saved["legacy_host_trust"]["ignored"] is True

            # T06: Editing notes / legacy trust fields must NOT increment connection_revision.
            edited = runtime.vps.save(
                VPSInput(
                    name=f"Legacy-{idx}-renamed",
                    host=saved["host"],
                    port=saved["port"],
                    username=saved["username"],
                    host_key_policy="strict",
                    host_key_fingerprint="SHA256:changed-but-ignored",
                    notes="updated notes",
                    project_ids=[project_id],
                    expected_version=saved["version"],
                ),
                owner,
                saved["id"],
            )
            assert edited["version"] == saved["version"] + 1
            assert edited["connection_revision"] == saved["connection_revision"] == 1

            # Transport works for all legacy policies including confirm-change (no SSH_HOST_PIN_REQUIRES_WEB_TERMINAL).
            project = runtime.project(project_id, owner)
            ref = runtime.vps.reference({"vps": edited["id"]}, project)
            packet = runtime.vps.transport(
                {
                    "tool": "exec",
                    "args": {
                        "project": project_id,
                        "target": "vps:" + edited["id"],
                        "command": "echo ok",
                        "timeout_seconds": 30,
                        "idempotency_key": uuid.uuid4().hex,
                    },
                    "project": {"id": project_id, "root": project["root"]},
                    "vps_ref": ref,
                },
                project_id,
            )
            assert packet["vps_transport"]["protocol"] == VPS_SSH_PROTOCOL
            assert packet["vps_transport"]["host_identity_verification"] == VPS_HOST_IDENTITY_VERIFICATION
            assert packet["vps_target"]["vps_id"] == edited["id"]
            assert packet["core_ssh"]["host_key_policy"] == "disabled"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_t10_readiness_dual_grants_no_index_collision_or_revocation_revival(tmp_path):
    """T10: Dual active required+skip grants, legacy host-key failures, and revoked grant enforcement."""
    store, runtime, owner, _device_id, project_id = _make_runtime(tmp_path)
    try:
        owner.session_hash = "sess-hash-1"
        owner.user_epoch = iam.user_security(store, owner.user_id)["epoch"]
        store.execute(
            "INSERT INTO sessions(id_hash,user_id,csrf,expires) VALUES (?,?,?,?)",
            (owner.session_hash, owner.user_id, "csrf-1", time.time() + 3600),
        )
        store.execute(
            "INSERT INTO session_security(session_hash,epoch,identity_id,authenticated_at) VALUES (?,?,?,?)",
            (owner.session_hash, owner.user_epoch, None, time.time()),
        )
        v = runtime.vps.save(
            VPSInput(
                name="ReadinessVPS",
                host="readiness.example.invalid",
                password=PASSWORD,
                host_key_policy="strict",
                project_ids=[project_id],
            ),
            owner,
        )

        inspect_calls = []
        provision_calls = []

        class FakeExecutor:
            async def inspect(self, target):
                inspect_calls.append(target.vps_id)
                return "ready"

            async def provision(self, target, *, before_install):
                assert await before_install() is True
                provision_calls.append(target.vps_id)
                return "ok"

        wiring = VPSReadinessWiring(store, executor_factory=lambda _w: FakeExecutor())
        await wiring.start()
        try:
            now = time.time()
            # Seed dual active historical grants (required + skip) for the same actor/space/intent.
            req_grant = uuid.uuid4().hex
            skip_grant = uuid.uuid4().hex
            with store.transaction() as db:
                for gid, mode in ((req_grant, HOST_VERIFICATION_REQUIRED), (skip_grant, HOST_VERIFICATION_SKIP)):
                    db.execute(
                        "INSERT INTO vps_readiness_grants "
                        "(id,space_id,authorized_user_id,user_epoch,identity_id,vps_id,active,intent,host_verification,created) "
                        "VALUES (?,?,?,?,?,NULL,1,'scan',?,?)",
                        (gid, owner.space_id, owner.user_id, owner.user_epoch, None, mode, now),
                    )
                # Seed a historical failed job stopped solely by HOST_KEY_CHANGED.
                db.execute(
                    "INSERT INTO vps_readiness_jobs "
                    "(id,space_id,vps_id,owner_user_id,authorized_user_id,connection_revision,grant_id,"
                    "state,intent,host_verification,reason_code,verify_only,attempts,next_attempt,updated) "
                    "VALUES (?,?,?,?,?,?,?,'failed','scan','required','HOST_KEY_CHANGED',0,1,?,?)",
                    (
                        uuid.uuid4().hex,
                        owner.space_id,
                        v["id"],
                        owner.user_id,
                        owner.user_id,
                        v["connection_revision"],
                        req_grant,
                        now,
                        now,
                    ),
                )

            # Re-initializing queue marks HOST_KEY_CHANGED row as queued for re-probe.
            wiring.queue._initialize()
            job = store.one("SELECT * FROM vps_readiness_jobs WHERE vps_id=?", (v["id"],))
            assert job["state"] == "queued"
            assert job["reason_code"] is None
            assert job["host_verification"] == HOST_VERIFICATION_SKIP

            # Calling activate_space must reuse an existing valid grant without colliding with the unique index.
            res = await wiring.activate_space(owner)
            assert res["enabled"] is True
            assert wiring.fleet_summary(owner)["readiness_backfill_enabled"] is True
            assert wiring.fleet_summary(owner)["hub_native_vps_ssh"]["readiness"] is True

            # Background worker may already have processed the job; run_one if still queued.
            await wiring.queue.run_one()
            for _ in range(40):
                done = store.one("SELECT * FROM vps_readiness_jobs WHERE vps_id=?", (v["id"],))
                if done["state"] == "ready":
                    break
                await asyncio.sleep(0.05)
            assert v["id"] in inspect_calls
            assert provision_calls == []
            assert done["state"] == "ready"

            # Revoke the required grant; authorization for a job pinned to that revoked grant MUST fail.
            store.execute("UPDATE vps_readiness_grants SET active=0 WHERE id=?", (req_grant,))
            revoked_target = SimpleNamespace(
                id=done["id"],
                space_id=owner.space_id,
                vps_id=v["id"],
                owner_user_id=owner.user_id,
                authorized_user_id=owner.user_id,
                connection_revision=v["connection_revision"],
                grant_id=req_grant,
                host_verification=HOST_VERIFICATION_SKIP,
            )
            assert await wiring.authorize(revoked_target, "inspect") is False
        finally:
            await wiring.stop()
    finally:
        store.close()


def test_t11_t12_delegation_effective_projection_and_revocation(tmp_path):
    """T11/T12: Legacy strict snapshot matches v2 projection; real target changes and revocations still reject."""
    legacy_snap = {
        "target": "vps:abc123",
        "reference": {"id": "abc123", "binding_id": "b1", "connection_revision": 2},
        "endpoint": {
            "host": "198.51.100.10",
            "port": 22,
            "username": "root",
            "host_key_policy": "strict",
            "host_key_fingerprint": "SHA256:legacy",
        },
    }
    v2_snap = {
        "target": "vps:abc123",
        "reference": {"id": "abc123", "binding_id": "b1", "connection_revision": 2},
        "endpoint": {"host": "198.51.100.10", "port": 22, "username": "root"},
    }
    assert effective_target_projection(legacy_snap) == effective_target_projection(v2_snap)

    # Any real change to host, port, username, binding_id, or connection_revision must NOT match.
    for mutated in (
        {**v2_snap, "endpoint": {**v2_snap["endpoint"], "host": "198.51.100.11"}},
        {**v2_snap, "endpoint": {**v2_snap["endpoint"], "port": 2222}},
        {**v2_snap, "endpoint": {**v2_snap["endpoint"], "username": "deploy"}},
        {**v2_snap, "reference": {**v2_snap["reference"], "binding_id": "b2"}},
        {**v2_snap, "reference": {**v2_snap["reference"], "connection_revision": 3}},
    ):
        assert effective_target_projection(legacy_snap) != effective_target_projection(mutated)


@pytest.mark.asyncio
async def test_t17_t24_t28_queued_conversion_capability_gating_and_truthful_capabilities(
    tmp_path, monkeypatch
):
    """T17/T24/T28: Old agent rejected with VPS_TRANSPORT_UPGRADE_REQUIRED; queued legacy op converts; target change blocks."""
    store, runtime, owner, device_id, project_id = _make_runtime(tmp_path)
    try:
        v = runtime.vps.save(
            VPSInput(
                name="CapVPS",
                host="cap.example.invalid",
                password=PASSWORD,
                host_key_policy="strict",
                project_ids=[project_id],
            ),
            owner,
        )

        # T24 + T28: Old agent (no vps_ssh_v2) online -> capabilities.exec reports VPS_TRANSPORT_UPGRADE_REQUIRED.
        old_con, old_packets = _fake_agent(legacy=True)
        runtime.connections[device_id] = old_con
        monkeypatch.setattr(runtime, "online", lambda d: d == device_id)
        monkeypatch.setattr(runtime, "connection_authorized", lambda d, c: c is old_con)

        listed_old = runtime.vps.get(v["id"], owner)
        assert listed_old["capabilities"]["exec"] == {
            "supported": False,
            "reason_code": "VPS_TRANSPORT_UPGRADE_REQUIRED",
        }

        # Dispatching an unsent VPS exec to an old agent fails with VPS_TRANSPORT_UPGRADE_REQUIRED (no strict fallback).
        receipt = await runtime.invoke(
            "exec",
            {
                "project": project_id,
                "target": "vps:" + v["id"],
                "command": "echo hi",
                "yield_seconds": 0,
                "idempotency_key": uuid.uuid4().hex,
            },
            owner,
        )
        await runtime.deliver(receipt["operation_id"])
        assert old_packets == []
        op_old = store.one("SELECT * FROM operations WHERE id=?", (receipt["operation_id"],))
        assert op_old["state"] == "failed"
        assert runtime.operation_result(op_old)["error"]["code"] == "VPS_TRANSPORT_UPGRADE_REQUIRED"

        # T24: Agent with terminal=True but exec=False STILL rejects exec with VPS_TRANSPORT_UPGRADE_REQUIRED.
        term_only_con, term_only_packets = _fake_agent(exec_ok=False, terminal_ok=True, probe_ok=True)
        runtime.connections[device_id] = term_only_con
        monkeypatch.setattr(runtime, "connection_authorized", lambda d, c: c is term_only_con)
        assert runtime.vps.get(v["id"], owner)["capabilities"]["exec"]["supported"] is False
        receipt2 = await runtime.invoke(
            "exec",
            {
                "project": project_id,
                "target": "vps:" + v["id"],
                "command": "echo hi2",
                "yield_seconds": 0,
                "idempotency_key": uuid.uuid4().hex,
            },
            owner,
        )
        await runtime.deliver(receipt2["operation_id"])
        assert term_only_packets == []
        op_term_only = store.one("SELECT * FROM operations WHERE id=?", (receipt2["operation_id"],))
        assert runtime.operation_result(op_term_only)["error"]["code"] == "VPS_TRANSPORT_UPGRADE_REQUIRED"

        # T28 + queued conversion: v2 Agent online -> capabilities.exec is True and unsent op converts keeping op ID & idem.
        v2_con, v2_packets = _fake_agent(exec_ok=True, terminal_ok=True, probe_ok=True)
        runtime.connections[device_id] = v2_con
        monkeypatch.setattr(runtime, "connection_authorized", lambda d, c: c is v2_con)
        assert runtime.vps.get(v["id"], owner)["capabilities"]["exec"] == {
            "supported": True,
            "reason_code": None,
        }
        idem = uuid.uuid4().hex
        queued = await runtime.invoke(
            "exec",
            {
                "project": project_id,
                "target": "vps:" + v["id"],
                "command": "echo v2",
                "yield_seconds": 0,
                "idempotency_key": idem,
            },
            owner,
        )
        await runtime.deliver(queued["operation_id"])
        assert len(v2_packets) == 1
        sent = v2_packets[0]
        assert sent["id"] == queued["operation_id"]
        assert sent["args"]["idempotency_key"] == idem
        assert sent["vps_transport"]["protocol"] == VPS_SSH_PROTOCOL
        assert sent["vps_transport"]["host_identity_verification"] == VPS_HOST_IDENTITY_VERIFICATION

        # T17: Changing host while queued prevents delivery before connection.
        queued_change = await runtime.invoke(
            "exec",
            {
                "project": project_id,
                "target": "vps:" + v["id"],
                "command": "echo changed",
                "yield_seconds": 0,
                "idempotency_key": uuid.uuid4().hex,
            },
            owner,
        )
        runtime.vps.save(
            VPSInput(
                name=v["name"],
                host="changed.example.invalid",
                password=PASSWORD,
                project_ids=[project_id],
                expected_version=v["version"],
            ),
            owner,
            v["id"],
        )
        await runtime.deliver(queued_change["operation_id"])
        assert len(v2_packets) == 1  # No new packet sent.
        failed_change = store.one("SELECT * FROM operations WHERE id=?", (queued_change["operation_id"],))
        assert failed_change["state"] == "failed" and failed_change["attempts"] == 0
    finally:
        store.close()


def test_t30_trigger_migration_replaces_legacy_trigger_and_preserves_real_changes(tmp_path):
    """T30: Versioned migration replaces old vps_connections trigger; policy-only edits ignored, real edits recorded."""
    data_dir = tmp_path / "trigger-upgrade"
    store = Store(data_dir)
    try:
        # Simulate an existing pre-v2 database with the old trigger containing host_key_policy.
        with store.transaction() as db:
            db.execute("DROP TRIGGER IF EXISTS room_input_history_target_update")
            db.execute(
                """CREATE TRIGGER room_input_history_target_update
                AFTER UPDATE ON vps_connections
                WHEN OLD.host IS NOT NEW.host OR OLD.port IS NOT NEW.port
                  OR OLD.username IS NOT NEW.username OR OLD.host_key_policy IS NOT NEW.host_key_policy
                  OR OLD.enabled IS NOT NEW.enabled
                BEGIN
                    INSERT INTO room_input_resource_revisions(kind,resource_id,value)
                    VALUES ('target',NEW.id,json_object('host',NEW.host,'port',NEW.port,'username',NEW.username,'host_key_policy',NEW.host_key_policy,'enabled',NEW.enabled));
                END"""
            )
            db.execute("DELETE FROM meta WHERE key='vps_ssh_v2_trigger_migration'")
    finally:
        store.close()

    # Re-open Store -> idempotent migration must replace the old trigger in-transaction.
    upgraded = Store(data_dir)
    try:
        row = upgraded.one(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='room_input_history_target_update'"
        )
        assert row is not None
        assert row["sql"] == vps_target_update_trigger_sql()
        assert "host_key_policy" not in row["sql"]
        assert "host_key_fingerprint" not in row["sql"]
        assert "OLD.secret IS NOT NEW.secret" in row["sql"]
        assert (
            upgraded.one("SELECT value FROM meta WHERE key='vps_ssh_v2_trigger_migration'")["value"]
            == "1"
        )

        # Insert a VPS row and verify policy/fingerprint-only UPDATE emits 0 revision rows.
        vps_id = uuid.uuid4().hex
        now = time.time()
        upgraded.execute(
            "INSERT INTO vps_connections(id,name,name_key,host,port,username,secret,host_key_policy,host_key_fingerprint,created,updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (vps_id, "TrigVPS", "trigvps", "198.51.100.5", 22, "root", upgraded.encrypt(PASSWORD), "strict", "", now, now),
        )
        upgraded.execute(
            "UPDATE vps_connections SET host_key_policy='accept-new', host_key_fingerprint='SHA256:abc' WHERE id=?",
            (vps_id,),
        )
        assert (
            upgraded.all(
                "SELECT * FROM room_input_resource_revisions WHERE kind='target' AND resource_id=?",
                (vps_id,),
            )
            == []
        )

        # Real host/secret/enabled changes MUST still be recorded.
        upgraded.execute(
            "UPDATE vps_connections SET host='198.51.100.6' WHERE id=?",
            (vps_id,),
        )
        revs = upgraded.all(
            "SELECT * FROM room_input_resource_revisions WHERE kind='target' AND resource_id=?",
            (vps_id,),
        )
        assert len(revs) == 1
        assert json.loads(revs[0]["value"])["host"] == "198.51.100.6"

        upgraded.execute(
            "UPDATE vps_connections SET secret=? WHERE id=?",
            (upgraded.encrypt("rotated-pass"), vps_id),
        )
        revs2 = upgraded.all(
            "SELECT * FROM room_input_resource_revisions WHERE kind='target' AND resource_id=?",
            (vps_id,),
        )
        assert len(revs2) == 2
        assert "rotated-pass" not in revs2[1]["value"]
    finally:
        upgraded.close()


@pytest.mark.asyncio
async def test_t31_hub_native_readiness_unaffected_by_offline_or_legacy_project_agent(tmp_path):
    """T31: Hub-native readiness reports hub_native_vps_ssh available even when project Agent is offline/legacy."""
    store, runtime, owner, device_id, project_id = _make_runtime(tmp_path)
    try:
        # Leave project Agent completely offline (not in runtime.connections).
        native = NativeVPSReadiness(
            resolve_target=lambda _j, _a: None,
            secret=lambda _t: None,
            connector=lambda *_a, **_kw: None,
        )
        cap = native.capability()
        assert cap == {
            "protocol": VPS_SSH_PROTOCOL,
            "host_identity_verification": VPS_HOST_IDENTITY_VERIFICATION,
            "readiness": True,
        }

        wiring = VPSReadinessWiring(store, executor_factory=lambda _w: native)
        await wiring.start()
        try:
            runtime.vps_readiness = wiring
            assert hub_native_readiness_capability(runtime) == {
                "protocol": VPS_SSH_PROTOCOL,
                "host_identity_verification": VPS_HOST_IDENTITY_VERIFICATION,
                "readiness": True,
            }
            # Even when an old project Agent is connected, Hub-native readiness stays available.
            old_con, _ = _fake_agent(legacy=True)
            runtime.connections[device_id] = old_con
            assert hub_native_readiness_capability(runtime)["readiness"] is True
        finally:
            await wiring.stop()
    finally:
        store.close()
