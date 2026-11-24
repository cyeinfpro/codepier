"""Cross-entrypoint Hub vps_ssh_v2 consistency tests.

Verifies shared target resolution (project_bound vs direct), probe, Web SSH
terminal, delegation target candidates/snapshots, readiness, and MCP exec all
share the unified vps_ssh_v2 no-host-key-verification contract.
"""

from __future__ import annotations

import time
import uuid

import pytest

from hub import iam
from hub.runtime import Principal, Runtime
from hub.ssh_terminal.service import TerminalService
from hub.store import Store
from hub.vps import VPSCheckInput, VPSInput
from hub.vps_connection import resolve_vps_connection_plan
from shared.util import DevError
from shared.vps_transport import (
    VPS_HOST_IDENTITY_VERIFICATION,
    VPS_SSH_PROTOCOL,
    agent_vps_ssh_capability,
)


PASSWORD = "fixture-entrypoint-pass-2026"


def _setup_env(tmp_path):
    store = Store(tmp_path / "hub-entrypoints")
    from tests.legacy_iam_fixture import seed_owner

    seed_owner(store, "owner", "owner")
    device_id = uuid.uuid4().hex
    store.execute(
        "INSERT INTO devices(id,name,secret,created) VALUES (?,?,?,?)",
        (device_id, "V2 Agent", store.encrypt("dev-secret"), time.time()),
    )
    project_id = uuid.uuid4().hex
    store.execute(
        "INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            project_id,
            "ProjectOne",
            "projectone",
            device_id,
            str(tmp_path / "p1"),
            "",
            "write",
            1,
            time.time(),
        ),
    )
    runtime = Runtime(store)
    owner = Principal(
        "panel:owner",
        "owner",
        {"read", "write", "execute"},
        [],
        admin=True,
        session_hash="panel-sess-hash",
    )
    owner.user_epoch = iam.user_security(store, owner.user_id)["epoch"]
    store.execute(
        "INSERT INTO sessions(id_hash,user_id,csrf,expires) VALUES (?,?,?,?)",
        (owner.session_hash, owner.user_id, "csrf-ep", time.time() + 3600),
    )
    store.execute(
        "INSERT INTO session_security(session_hash,epoch,identity_id,authenticated_at) VALUES (?,?,?,?)",
        (owner.session_hash, owner.user_epoch, None, time.time()),
    )
    return store, runtime, owner, device_id, project_id


def test_shared_connection_plan_distinguishes_project_bound_and_direct(tmp_path):
    """hub/vps_connection.py resolves project_bound vs direct cleanly without reading host_key_policy."""
    store, runtime, owner, _device_id, project_id = _setup_env(tmp_path)
    try:
        bound = runtime.vps.save(
            VPSInput(
                name="BoundVPS",
                host="bound.example.invalid",
                password=PASSWORD,
                host_key_policy="confirm-change",
                host_key_fingerprint="not-even-valid",
                project_ids=[project_id],
            ),
            owner,
        )
        direct = runtime.vps.save(
            VPSInput(
                name="DirectVPS",
                host="direct.example.invalid",
                password=PASSWORD,
                host_key_policy="accept-new",
                project_ids=[],
            ),
            owner,
        )
        project = runtime.project(project_id, owner)
        ref = runtime.vps.reference({"vps": bound["id"]}, project)
        plan_bound = resolve_vps_connection_plan(
            runtime.vps,
            request={"vps_ref": ref},
            source="project_bound",
            project_id=project_id,
        )
        assert plan_bound.source == "project_bound"
        assert plan_bound.target.source == "project_bound"
        assert plan_bound.target.project_id == project_id
        assert plan_bound.target.transport.protocol == VPS_SSH_PROTOCOL
        assert plan_bound.target.transport.host_identity_verification == VPS_HOST_IDENTITY_VERIFICATION
        assert plan_bound.credential.vps_id == bound["id"]
        assert "secret" not in plan_bound.row

        plan_direct = resolve_vps_connection_plan(
            runtime.vps,
            request={
                "vps_ref": {
                    "id": direct["id"],
                    "binding_id": "direct:" + direct["id"],
                    "connection_revision": direct["connection_revision"],
                }
            },
            source="direct",
            space_id=owner.space_id,
        )
        assert plan_direct.source == "direct"
        assert plan_direct.target.source == "direct"
        assert plan_direct.target.project_id is None
        assert plan_direct.target.binding_id == "direct:" + direct["id"]
        assert plan_direct.target.transport.protocol == VPS_SSH_PROTOCOL
    finally:
        store.close()


@pytest.mark.asyncio
async def test_probe_terminal_delegation_and_exec_all_use_vps_ssh_v2(tmp_path, monkeypatch):
    """Probe, terminal capabilities, delegation target_candidates/snapshot, and exec agree on vps_ssh_v2."""
    store, runtime, owner, device_id, project_id = _setup_env(tmp_path)
    try:
        v = runtime.vps.save(
            VPSInput(
                name="UnifiedVPS",
                host="unified.example.invalid",
                password=PASSWORD,
                host_key_policy="confirm-change",
                host_key_fingerprint="broken-pin",
                project_ids=[project_id],
            ),
            owner,
        )

        # 1. Attach v2 main connection + v2 terminal agent.
        probed_payloads = []

        class MainPeer:
            unusable = False
            closed = False
            last_seen = time.time()
            journal_id = "j-unified"
            agent_vps_ssh = agent_vps_ssh_capability(
                available=True, exec_supported=True, terminal_supported=True, probe_supported=True
            )

        class TermPeer:
            generation = "aa" * 16
            closed = False
            direct_vps = True
            direct_vps_allowed = True
            probe_supported = True
            vps_ssh_v2_supported = True
            agent_vps_ssh = agent_vps_ssh_capability(
                available=True, exec_supported=True, terminal_supported=True, probe_supported=True
            )

            async def request(self, action, payload, project, **kwargs):
                probed_payloads.append((action, dict(payload)))
                return {
                    "ok": True,
                    "state": "succeeded",
                    "output": "CODEPIER_VPS_OK",
                    "error": None,
                }

            async def close(self):
                self.closed = True

        main_peer = MainPeer()
        term_peer = TermPeer()
        term_peer.device_id = device_id
        term_peer.main_connection = main_peer
        runtime.connections[device_id] = main_peer
        monkeypatch.setattr(runtime, "online", lambda d: d == device_id)
        terminal_service = TerminalService(runtime, auth=None)
        runtime.ssh_terminal = terminal_service
        await terminal_service.register_agent(term_peer)

        # 2. Public VPS listing reports unified vps_ssh_v2 capabilities (exec/terminal/probe all supported).
        pub = runtime.vps.get(v["id"], owner)
        assert pub["connection_contract"] == VPS_SSH_PROTOCOL
        assert pub["host_identity_verification"] == VPS_HOST_IDENTITY_VERIFICATION
        assert pub["capabilities"] == {
            "exec": {"supported": True, "reason_code": None},
            "terminal": {"supported": True, "reason_code": None},
            "probe": {"supported": True, "reason_code": None},
        }

        # 3. Terminal capabilities endpoint reports vps_ssh_v2 without saved_vps_host_verification.
        term_caps = await terminal_service.capabilities(owner, v["id"], project_id)
        assert term_caps["available"] is True
        assert term_caps["connection_contract"] == VPS_SSH_PROTOCOL
        assert term_caps["host_identity_verification"] == VPS_HOST_IDENTITY_VERIFICATION
        assert "saved_vps_host_verification" not in term_caps

        # 4. Panel probe check sends vps_ssh_v2 payload without saved_vps_password_only or host_key_policy.
        check_res = await runtime.vps.check(
            v["id"],
            VPSCheckInput(project_id=project_id, idempotency_key="probe-unified-001"),
            owner,
        )
        assert check_res["ok"] is True
        assert probed_payloads[0][0] == "probe"
        probe_payload = probed_payloads[0][1]
        assert probe_payload["transport_contract"] == VPS_SSH_PROTOCOL
        assert probe_payload["host_identity_verification"] == VPS_HOST_IDENTITY_VERIFICATION
        assert "saved_vps_password_only" not in probe_payload
        assert "host_key_policy" not in probe_payload
        assert "host_key_fingerprint" not in probe_payload

        # 5. Delegation accepts non-strict / confirm-change VPS without DELEGATION_HOST_KEY_REQUIRED.
        room = {"project_id": project_id}
        candidates = runtime.collaboration.delegation.target_candidates(owner, room)
        vps_cand = next(c for c in candidates if c["id"] == "vps:" + v["id"])
        assert vps_cand["available"] is True
        assert vps_cand["reason_code"] == ""
        project = runtime.project(project_id, owner)
        snap = runtime.collaboration.delegation.target_snapshot(owner, project, "vps:" + v["id"])
        assert snap["endpoint"] == {"host": v["host"], "port": v["port"], "username": v["username"]}
        assert "host_key_policy" not in snap["endpoint"]

        # 6. Downgrading terminal agent (removing vps_ssh_v2) rejects probe and terminal cleanly.
        term_peer.agent_vps_ssh = None
        term_peer.vps_ssh_v2_supported = False
        downgraded_caps = await terminal_service.capabilities(owner, v["id"], project_id)
        assert downgraded_caps["available"] is False
        assert downgraded_caps["reason_code"] == "VPS_TRANSPORT_UPGRADE_REQUIRED"
        with pytest.raises(DevError) as caught:
            await runtime.vps.check(
                v["id"],
                VPSCheckInput(project_id=project_id, idempotency_key="probe-downgraded-001"),
                owner,
            )
        assert caught.value.code == "VPS_TRANSPORT_UPGRADE_REQUIRED"
    finally:
        store.close()
