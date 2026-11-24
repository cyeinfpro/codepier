"""Tests for native provider adapters, instance registry, and catalog capability isolation."""

from __future__ import annotations

import json
import os
import stat
from types import SimpleNamespace

import pytest

from agent.chat_catalog import (
    CAPABILITIES,
    COMMANDS,
    CatalogCache,
    commands,
    compute_action_availabilities,
    describe_provider_capabilities,
    public_model,
    validate_settings,
)
from agent.chat_worker import Protocol, _AdapterProtocolBridge, _dispatch_worker_command
from agent.claude_protocol import ClaudeProtocol
from agent.native_adapters import (
    AdapterProtocolError,
    LegacyProtocolFacade,
    NativeAdapterRegistry,
    ProviderInstanceRegistry,
    SessionFence,
)
from agent.native_adapter_base import compute_approval_digest
from shared.native_contracts import NativeCommand, NativeEventEnvelope, NativeSelection


def _hex32(ch: str) -> str:
    return ch * 32


def _cmd(receipt: str, sid: str, pid: str, payload: dict) -> NativeCommand:
    return NativeCommand.model_validate(
        {
            "receipt": receipt,
            "session_id": sid,
            "provider_instance_id": pid,
            "payload": payload,
        }
    )


def _validate_envelopes(events: tuple[dict, ...]) -> None:
    for ev in events:
        NativeEventEnvelope.model_validate(ev)


def test_registry_supports_fixed_providers_only():
    assert NativeAdapterRegistry.supported_providers() == ("pi", "codex", "claude")
    for kind in ("pi", "codex", "claude"):
        adapter = NativeAdapterRegistry.create(kind, provider_instance_id=_hex32("a"))
        assert adapter.describe().provider_kind == kind
    with pytest.raises(AdapterProtocolError) as exc:
        NativeAdapterRegistry.create("custom_remote_plugin", provider_instance_id=_hex32("a"))
    assert exc.value.code == "UNSUPPORTED_PROVIDER"


def test_agent_bundle_preserves_pinned_installer_flat_adapter_layout(tmp_path):
    from pathlib import Path
    from zipfile import ZipFile

    from hub.agent_install import AgentPackage
    from scripts.install_agent import unpack

    source_root = Path(__file__).resolve().parents[1]
    package = AgentPackage(source_root).build()
    archive = tmp_path / "agent.zip"
    archive.write_bytes(package.content)
    names = {
        "agent/native_adapters.py",
        *(f"agent/native_adapter_{flavor}.py" for flavor in ("base", "claude", "codex", "pi", "registry")),
    }
    with ZipFile(archive) as bundled:
        assert names <= set(bundled.namelist())
        assert not any(name.startswith("agent/native_adapters/") for name in bundled.namelist())
    extracted = tmp_path / "extracted"
    unpack(archive, package.sha256, extracted)
    for name in names:
        assert (extracted / name).read_bytes() == (source_root / name).read_bytes()


def test_provider_instance_registry_persistence_and_legacy_unresolved(tmp_path):
    reg = ProviderInstanceRegistry(tmp_path)
    secret_token = "sk-ant-super-secret-token-value"
    cfg_a = reg.compute_config_digest("/opt/bin/codex", secret_token + ":profile-a")
    cfg_b = reg.compute_config_digest("/opt/bin/codex", "profile-b")
    rec_a1 = reg.resolve(
        execution_account="acct-1",
        provider_kind="codex",
        executable_and_config_digest=cfg_a,
    )
    rec_a2 = reg.resolve(
        execution_account="acct-2",
        provider_kind="codex",
        executable_and_config_digest=cfg_a,
    )
    rec_b = reg.resolve(
        execution_account="acct-1",
        provider_kind="codex",
        executable_and_config_digest=cfg_b,
    )
    assert len(rec_a1.provider_instance_id) == 32
    assert rec_a1.provider_instance_id != rec_a2.provider_instance_id
    assert rec_a1.provider_instance_id != rec_b.provider_instance_id
    assert rec_a1.instance_revision == 1

    persisted = tmp_path / "provider-instances.json"
    assert stat.S_IMODE(os.stat(persisted).st_mode) == 0o600
    raw_text = persisted.read_text(encoding="utf-8")
    assert secret_token not in raw_text
    assert "/opt/bin/codex" not in raw_text

    # Re-loading preserves exact ID; updating existing instance bumps revision monotonically.
    reg_reloaded = ProviderInstanceRegistry(tmp_path)
    same = reg_reloaded.resolve(
        execution_account="acct-1",
        provider_kind="codex",
        executable_and_config_digest=cfg_a,
    )
    assert same == rec_a1

    bumped = reg_reloaded.resolve(
        execution_account="acct-1",
        provider_kind="codex",
        executable_and_config_digest=reg.compute_config_digest("/opt/bin/codex-v2", "rotated"),
        existing_instance_id=rec_a1.provider_instance_id,
    )
    assert bumped.provider_instance_id == rec_a1.provider_instance_id
    assert bumped.instance_revision == 2

    assert reg_reloaded.resolve_legacy_session({"provider_instance_id": ""}) == "legacy_unresolved"
    assert (
        reg_reloaded.resolve_legacy_session({"provider_instance_id": _hex32("f")})
        == "legacy_unresolved"
    )
    assert (
        reg_reloaded.resolve_legacy_session({"provider_instance_id": rec_a1.provider_instance_id})
        == rec_a1.provider_instance_id
    )


def test_legacy_protocol_facade_zero_regression_parity():
    def run_pair(provider: str, cls: type, init_reply: dict, done_frame: dict):
        sent_direct, ev_direct, fin_direct = [], [], []
        sent_facade, ev_facade, fin_facade = [], [], []
        row = {"cwd": ".", "chat_settings": "{}"}
        direct = cls(
            provider,
            row,
            sent_direct.append,
            lambda k, **v: ev_direct.append((k, v)),
            lambda *a: fin_direct.append(a),
            lambda _: None,
        )
        wrapped = cls(
            provider,
            dict(row),
            sent_facade.append,
            lambda k, **v: ev_facade.append((k, v)),
            lambda *a: fin_facade.append(a),
            lambda _: None,
        )
        fence = SessionFence(_hex32("1"), _hex32("2"), 1)
        facade = LegacyProtocolFacade(wrapped, provider_instance_id=_hex32("2"), fence=fence)

        direct.start()
        t_init = facade.initialize(fence)
        assert list(t_init.wire_requests) == sent_direct

        direct.receive(init_reply)
        facade.receive(init_reply, fence)
        if provider == "codex":
            ts_id = next(w["id"] for w in sent_direct if w.get("method") == "thread/start")
            ts = {"id": ts_id, "result": {"thread": {"id": "th-1"}}}
            direct.receive(ts)
            facade.receive(ts, fence)

        direct.prompt(_hex32("a"), {"text": "hello", "attachments": []})
        facade.command(
            _cmd(
                _hex32("a"),
                fence.session_id,
                fence.provider_instance_id,
                {"action": "send", "text": "hello", "selection_mode": "inherit_at_dispatch"},
            )
        )
        assert sent_facade[-1] == sent_direct[-1] or (
            provider == "claude" and sent_facade[-1]["message"] == sent_direct[-1]["message"]
        )
        direct.receive(done_frame)
        facade.receive(done_frame, fence)
        assert fin_facade == fin_direct

    run_pair(
        "pi",
        Protocol,
        {"id": "codepier-1", "type": "response", "success": True, "data": {"model": {"provider": "p", "id": "m1"}, "thinkingLevel": "low"}},
        {"type": "agent_settled"},
    )
    run_pair(
        "claude",
        ClaudeProtocol,
        {"type": "control_response", "response": {"subtype": "success", "request_id": "codepier-claude-1", "response": {"models": [{"value": "sonnet"}], "session_state": {"model": "sonnet"}}}},
        {"type": "result", "uuid": "res-1", "subtype": "success", "result": "ok"},
    )
    run_pair(
        "codex",
        Protocol,
        {"id": "codepier-1", "result": {}},
        {"method": "turn/completed", "params": {"turn": {"id": "t-1", "status": "completed"}}},
    )


def test_pi_explicit_vs_inherit_at_dispatch_and_settings_readback():
    now = [100.0]
    sid, pid = _hex32("1"), _hex32("2")
    fence = SessionFence(sid, pid, 1)
    adapter = NativeAdapterRegistry.create("pi", provider_instance_id=pid, clock=lambda: now[0])
    t0 = adapter.initialize(fence)
    get_state_id = t0.wire_requests[0]["id"]
    t_ready = adapter.receive(
        {
            "id": get_state_id,
            "type": "response",
            "success": True,
            "data": {"model": {"provider": "p", "id": "default"}, "thinkingLevel": "low"},
        },
        fence,
    )
    _validate_envelopes(t_ready.events)

    # Confirm settings change to p/confirmed + high.
    r_cfg = _hex32("b")
    t_cfg = adapter.command(
        _cmd(
            r_cfg,
            sid,
            pid,
            {"action": "change_model", "model": "p/confirmed", "options": {"effort": "high"}, "expected_settings_revision": 0},
        )
    )
    t_lvl_req = adapter.receive({"id": t_cfg.wire_requests[0]["id"], "type": "response", "success": True, "data": {}}, fence)
    t_lvl = adapter.receive({"id": t_lvl_req.wire_requests[0]["id"], "type": "response", "success": True, "data": {"levels": ["low", "high"]}}, fence)
    t_cfm_req = adapter.receive({"id": t_lvl.wire_requests[0]["id"], "type": "response", "success": True, "data": {}}, fence)
    t_done_cfg = adapter.receive(
        {
            "id": t_cfm_req.wire_requests[0]["id"],
            "type": "response",
            "success": True,
            "data": {"model": {"provider": "p", "id": "confirmed"}, "thinkingLevel": "high"},
        },
        fence,
    )
    assert any(e["kind"] == "settings.applied" and e["body"]["status"] == "applied" for e in t_done_cfg.events)

    # 1) inherit_at_dispatch picks up p/confirmed + high at dispatch time.
    t_inh = adapter.command(
        _cmd(_hex32("c"), sid, pid, {"action": "send", "text": "inherited", "selection_mode": "inherit_at_dispatch"})
    )
    _validate_envelopes(t_inh.events)
    assert t_inh.state_updates["effective_selection"] == {"model": "p/confirmed", "effort": "high"}
    adapter.receive({"type": "agent_settled"}, fence)

    # 2) explicit selection preserves p/pinned + low despite confirmed default being p/confirmed + high.
    t_exp = adapter.command(
        _cmd(
            _hex32("d"),
            sid,
            pid,
            {
                "action": "send",
                "text": "explicit",
                "selection_mode": "explicit",
                "selection": NativeSelection(model="p/pinned", options={"effort": "low"}).model_dump(),
            },
        )
    )
    assert t_exp.state_updates["effective_selection"] == {"model": "p/pinned", "effort": "low"}
    assert [w["type"] for w in t_exp.wire_requests] == ["set_model", "set_thinking_level", "prompt"]
    adapter.receive({"type": "agent_settled"}, fence)

    # 3) Partial settings readback bound to original receipt, stale generation ignored, timeout -> unknown.
    r_partial = _hex32("e")
    t_s1 = adapter.command(
        _cmd(r_partial, sid, pid, {"action": "change_model", "model": "p/partial", "options": {"effort": "max"}, "expected_settings_revision": 1})
    )
    t_s1_lvl = adapter.receive({"id": t_s1.wire_requests[0]["id"], "type": "response", "success": True, "data": {}}, fence)
    t_fail = adapter.receive({"id": t_s1_lvl.wire_requests[0]["id"], "type": "response", "success": True, "data": {"levels": ["low", "high"]}}, fence)
    readback_call_id = t_fail.wire_requests[0]["id"]
    t_rb = adapter.receive(
        {
            "id": readback_call_id,
            "type": "response",
            "success": True,
            "data": {"model": {"provider": "p", "id": "partial"}, "thinkingLevel": "high"},
        },
        fence,
    )
    applied_ev = next(e for e in t_rb.events if e["kind"] == "settings.applied")
    assert applied_ev["body"]["receipt"] == r_partial
    assert applied_ev["body"]["status"] == "partial"

    # Stale readback after newer request never overwrites newer request.
    r_old = _hex32("5")
    t_old = adapter.command(
        _cmd(r_old, sid, pid, {"action": "change_model", "model": "p/old", "options": {}, "expected_settings_revision": 2})
    )
    t_old_fail = adapter.receive({"id": t_old.wire_requests[0]["id"], "type": "response", "success": False, "error": "boom"}, fence)
    stale_rb_id = t_old_fail.wire_requests[0]["id"]

    r_new = _hex32("6")
    adapter.command(
        _cmd(r_new, sid, pid, {"action": "change_model", "model": "p/newer", "options": {}, "expected_settings_revision": 2})
    )
    t_stale = adapter.receive(
        {
            "id": stale_rb_id,
            "type": "response",
            "success": True,
            "data": {"model": {"provider": "p", "id": "old"}, "thinkingLevel": "high"},
        },
        fence,
    )
    assert t_stale.events == ()

    # Monotonic timeout expires active settings request as unknown.
    now[0] += 20.0
    t_exp_cfg = adapter.expire(now[0])
    assert any(
        e["kind"] == "settings.applied"
        and e["body"]["receipt"] == r_new
        and e["body"]["status"] == "unknown"
        for e in t_exp_cfg.events
    )


def test_codex_next_turn_settings_late_interrupt_and_approval_guards():
    sid, pid = _hex32("1"), _hex32("3")
    fence = SessionFence(sid, pid, 2)
    adapter = NativeAdapterRegistry.create("codex", provider_instance_id=pid, cwd="/proj")
    assert adapter.describe().model_change == "next_turn"
    assert adapter.describe().effort_change == "next_turn"

    t0 = adapter.initialize(fence)
    t1 = adapter.receive({"id": t0.wire_requests[0]["id"], "result": {}}, fence)
    thread_call = next(w for w in t1.wire_requests if w["method"] == "thread/start")
    adapter.receive({"id": thread_call["id"], "result": {"thread": {"id": "th-1"}, "model": "m-init"}}, fence)

    # Next-turn change_model does NOT update actual until turn/start confirms.
    r_next = _hex32("a")
    t_next = adapter.command(
        _cmd(r_next, sid, pid, {"action": "change_model", "model": "m-next", "options": {"effort": "high"}, "expected_settings_revision": 0})
    )
    assert not any(e["kind"] == "settings.applied" for e in t_next.events)
    assert "actual_settings" not in t_next.state_updates

    # Dispatch send -> turn/start carries next-turn settings; confirmed when turn/start replies.
    r_run1 = _hex32("b")
    t_s1 = adapter.command(_cmd(r_run1, sid, pid, {"action": "send", "text": "turn 1", "selection_mode": "inherit_at_dispatch"}))
    ts1 = t_s1.wire_requests[0]
    assert ts1["params"]["model"] == "m-next" and ts1["params"]["effort"] == "high"
    t_ts1 = adapter.receive({"id": ts1["id"], "result": {"turn": {"id": "turn-1"}}}, fence)
    assert any(
        e["kind"] == "settings.applied" and e["body"]["receipt"] == r_next and e["body"]["status"] == "applied"
        for e in t_ts1.events
    )

    # Approval request validates digest, run_id, worker_epoch, and permission subset.
    t_app = adapter.receive(
        {
            "id": "srv-1",
            "method": "item/permissions/requestApproval",
            "params": {"permissions": {"network": ["api.example.com"], "read": True}, "turnId": "turn-1"},
        },
        fence,
    )
    _validate_envelopes(t_app.events)
    req_ev = next(e for e in t_app.events if e["kind"] == "approval.requested")
    digest = req_ev["body"]["request_digest"]

    # Wrong epoch rejected.
    with pytest.raises(AdapterProtocolError) as exc_epoch:
        adapter.command(
            _cmd(
                _hex32("c"),
                sid,
                pid,
                {
                    "action": "answer_approval",
                    "observed_request_id": "srv-1",
                    "target_run_id": r_run1,
                    "worker_epoch": 1,
                    "request_digest": digest,
                    "answer": {"permissions": {"read": True}},
                },
            )
        )
    assert exc_epoch.value.code == "STALE_WORKER_EPOCH"

    # Wrong digest rejected.
    with pytest.raises(AdapterProtocolError) as exc_dig:
        adapter.command(
            _cmd(
                _hex32("d"),
                sid,
                pid,
                {
                    "action": "answer_approval",
                    "observed_request_id": "srv-1",
                    "target_run_id": r_run1,
                    "worker_epoch": 2,
                    "request_digest": "0" * 64,
                    "answer": {"permissions": {"read": True}},
                },
            )
        )
    assert exc_dig.value.code == "APPROVAL_DIGEST_MISMATCH"

    # Exceeding observed permission subset rejected.
    with pytest.raises(AdapterProtocolError) as exc_perm:
        adapter.command(
            _cmd(
                _hex32("e"),
                sid,
                pid,
                {
                    "action": "answer_approval",
                    "observed_request_id": "srv-1",
                    "target_run_id": r_run1,
                    "worker_epoch": 2,
                    "request_digest": digest,
                    "answer": {"permissions": {"write": True}},
                },
            )
        )
    assert exc_perm.value.code == "APPROVAL_SCOPE_EXCEEDED"

    # Valid subset accepted.
    t_ans = adapter.command(
        _cmd(
            _hex32("f"),
            sid,
            pid,
            {
                "action": "answer_approval",
                "observed_request_id": "srv-1",
                "target_run_id": r_run1,
                "worker_epoch": 2,
                "request_digest": digest,
                "answer": {"permissions": {"read": True}, "scope": "turn"},
            },
        )
    )
    assert t_ans.wire_requests[0] == {"id": "srv-1", "result": {"permissions": {"read": True}, "scope": "turn"}}

    # Unknown server request never auto-approves; unknown notification does not fabricate done.
    t_unk_req = adapter.receive({"id": "srv-unknown", "method": "future/customRequest", "params": {"turnId": "turn-1"}}, fence)
    assert "error" in t_unk_req.wire_requests[0]
    assert any(e["kind"] == "notice" and e["body"].get("auto_approved") is False for e in t_unk_req.events)
    t_unk_notif = adapter.receive({"method": "future/unknownNotification", "params": {"turnId": "turn-1"}}, fence)
    assert not any(e["kind"] == "done" for e in t_unk_notif.events)

    # Interrupt run-1, finish run-1, start run-2, then deliver late turn-1 completion: run-2 stays active!
    adapter.command(
        _cmd(_hex32("7"), sid, pid, {"action": "interrupt", "target_run_id": r_run1, "provider_turn_id": "turn-1", "worker_epoch": 2})
    )
    adapter.receive({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "interrupted"}}}, fence)

    r_run2 = _hex32("8")
    t_s2 = adapter.command(_cmd(r_run2, sid, pid, {"action": "send", "text": "turn 2", "selection_mode": "inherit_at_dispatch"}))
    adapter.receive({"id": t_s2.wire_requests[0]["id"], "result": {"turn": {"id": "turn-2"}}}, fence)
    # Late turn-1 completed cannot settle turn-2; late interrupt targeting turn-1 rejected.
    t_late = adapter.receive({"method": "turn/completed", "params": {"turn": {"id": "turn-1", "status": "interrupted"}}}, fence)
    assert not any(e["kind"] == "done" for e in t_late.events)
    with pytest.raises(AdapterProtocolError) as exc_stale_int:
        adapter.command(
            _cmd(_hex32("9"), sid, pid, {"action": "interrupt", "target_run_id": r_run1, "provider_turn_id": "turn-1", "worker_epoch": 2})
        )
    assert exc_stale_int.value.code == "STALE_RUN_TARGET"


def test_claude_adapter_in_place_model_unsupported_steer_and_unknown_frames():
    sid, pid = _hex32("1"), _hex32("4")
    fence = SessionFence(sid, pid, 1)
    adapter = NativeAdapterRegistry.create("claude", provider_instance_id=pid, cwd="/proj")
    caps = adapter.describe()
    assert caps.steer.supported is False
    assert caps.model_change == "in_place"
    assert caps.effort_change == "unsupported"

    t0 = adapter.initialize(fence)
    adapter.receive(
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": t0.wire_requests[0]["request_id"],
                "response": {"models": [{"value": "sonnet"}], "session_state": {"model": "sonnet", "effort": "medium"}},
            },
        },
        fence,
    )
    with pytest.raises(AdapterProtocolError) as exc_opt:
        adapter.command(
            _cmd(_hex32("a"), sid, pid, {"action": "change_options", "options": {"effort": "high"}, "expected_settings_revision": 0})
        )
    assert exc_opt.value.code == "UNSUPPORTED_OPTION"

    r_run = _hex32("b")
    adapter.command(_cmd(r_run, sid, pid, {"action": "send", "text": "hi", "selection_mode": "inherit_at_dispatch"}))
    t_unk = adapter.receive({"type": "control_request", "request_id": "c-unk", "request": {"subtype": "future_perm"}}, fence)
    assert t_unk.wire_requests[0]["response"]["subtype"] == "error"
    assert any(e["kind"] == "notice" and e["body"].get("auto_approved") is False for e in t_unk.events)
    t_notif = adapter.receive({"type": "future_claude_event"}, fence)
    assert not any(e["kind"] == "done" for e in t_notif.events)

    # Approval digest validation
    t_req = adapter.receive(
        {
            "type": "control_request",
            "request_id": "c-1",
            "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "ls"}},
        },
        fence,
    )
    digest = next(e for e in t_req.events if e["kind"] == "approval.requested")["body"]["request_digest"]
    expected_digest = compute_approval_digest("confirm", {"tool_name": "Bash", "input": {"command": "ls"}, "cwd": "/proj"})
    assert digest == expected_digest
    t_ok = adapter.command(
        _cmd(
            _hex32("c"),
            sid,
            pid,
            {
                "action": "answer_approval",
                "observed_request_id": "c-1",
                "target_run_id": r_run,
                "worker_epoch": 1,
                "request_digest": digest,
                "answer": True,
            },
        )
    )
    assert t_ok.wire_requests[0]["response"]["response"]["behavior"] == "allow"


@pytest.mark.parametrize("attachment_field", ["attachments", "attachment_ids"])
def test_worker_reports_rejected_commands_and_keeps_adapter_usable(attachment_field):
    sid, pid = _hex32("1"), _hex32("4")
    wire, finished, events = [], [], []
    adapter = NativeAdapterRegistry.create("claude", provider_instance_id=pid, cwd="/proj")
    bridge = _AdapterProtocolBridge(
        adapter,
        {"chat_settings": "{}"},
        sid,
        1,
        wire.append,
        lambda kind, **fields: events.append((kind, fields)),
        lambda receipt, status: finished.append((receipt, status)),
        lambda thread: None,
    )
    bridge.start()
    bridge.receive(
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": wire[0]["request_id"],
                "response": {"session_state": {"model": "sonnet"}},
            },
        }
    )
    assert bridge.ready
    wire.clear()
    v2 = SimpleNamespace(
        prepare_prompt=lambda *_: None,
        arm_prompt_bytes=lambda *_: None,
        is_v2_turn=lambda *_: False,
    )

    def dispatch(kind, receipt, payload):
        return _dispatch_worker_command(
            {"id": receipt, "kind": kind, "payload": json.dumps(payload)},
            {"provider": "claude"},
            bridge,
            None,
            v2,
            None,
            wire,
            bridge.finish,
            bridge.emit,
            None,
            None,
        )

    settings_receipt = _hex32("a")
    attachment_receipt = _hex32("b")
    assert dispatch("chat_settings", settings_receipt, {"effort": "high"}) is False
    assert dispatch("chat_prompt", attachment_receipt, {"text": "look", attachment_field: ["fixture"]}) is False
    assert finished == [(settings_receipt, "error"), (attachment_receipt, "error")]
    assert [fields["code"] for kind, fields in events if kind == "error"] == [
        "UNSUPPORTED_OPTION",
        "CLI_ADAPTER_ATTACHMENTS_UNSUPPORTED",
    ]
    assert not wire
    assert bridge.active is None

    text_receipt = _hex32("c")
    assert dispatch("chat_prompt", text_receipt, {"text": "text only"}) is False
    assert bridge.active == text_receipt
    assert len(wire) == 1 and wire[0]["type"] == "user"


def test_catalog_cache_instance_isolation_and_authorization_recheck(tmp_path):
    cache = CatalogCache(path=tmp_path / "catalog-cache.json")
    loads = []
    auth_ok = [True]

    def authorize():
        if not auth_ok[0]:
            raise PermissionError("authorization revoked")

    key = CatalogCache.make_key(
        project_id="proj-1",
        device_id="dev-1",
        root="/repo",
        cli="codex",
        cwd="/repo",
    )
    inst_a = _hex32("a")
    inst_b = _hex32("b")

    res_a = cache.get(
        key,
        lambda: (loads.append("a"), {"models": [{"id": "model-a"}]})[1],
        provider_instance_id=inst_a,
        instance_revision=1,
        authorize=authorize,
    )
    res_b = cache.get(
        key,
        lambda: (loads.append("b"), {"models": [{"id": "model-b"}]})[1],
        provider_instance_id=inst_b,
        instance_revision=1,
        authorize=authorize,
    )
    res_a_v2 = cache.get(
        key,
        lambda: (loads.append("a2"), {"models": [{"id": "model-a-v2"}]})[1],
        provider_instance_id=inst_a,
        instance_revision=2,
        authorize=authorize,
    )
    assert res_a["models"][0]["id"] == "model-a"
    assert res_b["models"][0]["id"] == "model-b"
    assert res_a_v2["models"][0]["id"] == "model-a-v2"
    assert loads == ["a", "b", "a2"]

    # Cache hit rechecks authorization even without calling loader.
    auth_ok[0] = False
    with pytest.raises(PermissionError, match="revoked"):
        cache.get(
            key,
            lambda: (loads.append("should-not-run"), {"models": []})[1],
            provider_instance_id=inst_a,
            instance_revision=1,
            authorize=authorize,
        )
    assert loads == ["a", "b", "a2"]


def test_compute_action_availabilities_separates_supported_authorized_ready():
    claude_caps = describe_provider_capabilities(
        "claude", provider_instance_id=_hex32("c"), instance_revision=1, platform_supported=True
    )
    rows = {
        row.action: row
        for row in compute_action_availabilities(
            capabilities=claude_caps,
            authorized_scopes={"chat"},
            device_enabled=True,
            mode="chat",
            allow_tasks=True,
            shell_enabled=True,
            session_status="running",
            has_active_run=True,
            pending_approvals=0,
            observed_revision="rev-7",
        )
    }
    # Unsupported by provider, even though authorized and ready!
    assert rows["steer"].supported is False
    assert rows["steer"].authorized is True
    assert rows["steer"].ready is True
    assert rows["steer"].reason_code == "UNSUPPORTED_BY_PROVIDER"
    assert rows["steer"].remedy == "choose_supported_option"

    # Supported and authorized, but not ready while run active!
    assert rows["change_model"].supported is True
    assert rows["change_model"].authorized is True
    assert rows["change_model"].ready is False
    assert rows["change_model"].reason_code == "ACTIVE_RUN_IN_PROGRESS"
    assert rows["change_model"].remedy == "resolve_current_run"

    # Authorized=False stays independent of supported & ready!
    denied = {
        row.action: row
        for row in compute_action_availabilities(
            capabilities=claude_caps,
            authorized_scopes={"chat"},
            device_enabled=True,
            mode="chat",
            allow_tasks=False,
            shell_enabled=True,
            session_status="running",
            has_active_run=False,
            pending_approvals=0,
        )
    }
    assert denied["send"].supported is True
    assert denied["send"].authorized is False
    assert denied["send"].ready is True
    assert denied["send"].reason_code == "TASKS_DISABLED"
    assert denied["send"].remedy == "review_access"

    # Existing public catalog API symbols preserved.
    assert "steer" in CAPABILITIES and "refresh" in COMMANDS["codex"]
    assert public_model({"id": "m1"}) == {"id": "m1"}
    assert validate_settings({"model": "p/m"}, "pi") == {"model": "p/m"}
    assert commands({"commands": [{"name": "help"}]}, "pi")[0]["name"] == "help"
