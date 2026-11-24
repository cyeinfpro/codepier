"""Cross-language RFC 8785 JCS, safe integer, command, and event contract tests."""

from __future__ import annotations

import json
import subprocess
import pytest
from pydantic import ValidationError

from shared.native_contracts import (
    MAX_SAFE_INT,
    NativeCommand,
    NativeEventEnvelope,
    ProviderCapabilities,
    canonical_jcs,
    decode_raw_source_line,
    semantic_batch_digest,
    strict_safe_int,
)
from shared.native_flags import NativeFeatureFlags, resolve_native_flags
from scripts import build_native_contracts


def test_jcs_and_batch_digest_match_javascript_across_edge_vectors(tmp_path):
    build_native_contracts.check(write=True)
    sid = "a" * 32
    batches = [
        {
            "kind": "batch",
            "schema_version": 2,
            "scope_key": "scope-1",
            "session_id": sid,
            "stream_epoch": "ep-1",
            "from_revision": 0,
            "to_revision": 1,
            "from_cursor": 0,
            "to_cursor": 128,
            "resume_token": "tok-alpha",
            "changes": [
                {
                    "op": "upsert",
                    "key": "item:1",
                    "z": 1,
                    "a": "hello 🚀 café \u00e9",
                    "\U0001f680_emoji": True,
                    "\uffff_bmp_edge": 1,
                    "nested": {"b": None, "a": [1, 0, -0.0, 2.5]},
                }
            ],
            "summary_patch": {},
        },
        {
            "kind": "batch",
            "schema_version": 2,
            "scope_key": "scope-2-different-auth",
            "session_id": sid,
            "stream_epoch": "ep-1",
            "from_revision": 0,
            "to_revision": 1,
            "from_cursor": 0,
            "to_cursor": 128,
            "resume_token": "tok-beta-different",
            "changes": [
                {
                    "op": "upsert",
                    "key": "item:1",
                    "z": 1,
                    "a": "hello 🚀 café \u00e9",
                    "\U0001f680_emoji": True,
                    "\uffff_bmp_edge": 1,
                    "nested": {"a": [1, 0, 0, 2.5], "b": None},
                }
            ],
            "summary_patch": {},
        },
        {
            "kind": "batch",
            "schema_version": 2,
            "scope_key": "scope-1",
            "session_id": sid,
            "stream_epoch": "ep-1",
            "from_revision": MAX_SAFE_INT - 1,
            "to_revision": MAX_SAFE_INT,
            "from_cursor": MAX_SAFE_INT - 10,
            "to_cursor": MAX_SAFE_INT,
            "resume_token": "tok-boundary",
            "changes": [],
            "summary_patch": {"session_title": "元数据变更"},
        },
    ]

    py_digests = [semantic_batch_digest(b) for b in batches]
    # scope_key and resume_token must not change semantic batch digest.
    assert py_digests[0] == py_digests[1]

    js_probe = tmp_path / "probe.mjs"
    js_probe.write_text(
        f"""
import {{ canonicalJcs, computeSemanticBatchDigestSync, decodeAndVerifyBatch }} from {json.dumps(str((build_native_contracts.ROOT / "web/core/native-contracts.mjs").resolve()))};
const batches = {json.dumps(batches, ensure_ascii=False)};
const digests = batches.map((b) => computeSemanticBatchDigestSync(b));
const verified = await decodeAndVerifyBatch({{ ...batches[0], payload_digest: digests[0] }}, {{ scopeKey: 'scope-1', sessionId: {json.dumps(sid)} }});
console.log(JSON.stringify({{ digests, jcs0: canonicalJcs(batches[0].changes[0]), verifiedDigest: verified.digest }}));
""",
        encoding="utf-8",
    )
    res = subprocess.run(["node", str(js_probe)], capture_output=True, text=True, check=True)
    out = json.loads(res.stdout)
    assert out["digests"] == py_digests
    assert out["verifiedDigest"] == py_digests[0]
    assert out["jcs0"] == canonical_jcs(batches[0]["changes"][0])


def test_strict_safe_int_rejects_bool_float_string_and_overflow():
    assert strict_safe_int(0) == 0
    assert strict_safe_int(MAX_SAFE_INT) == MAX_SAFE_INT
    for bad in (True, False, -1, 1.0, "1", MAX_SAFE_INT + 1, None):
        with pytest.raises(ValueError):
            strict_safe_int(bad)


def test_command_and_envelope_contracts_enforce_boundaries():
    sid = "b" * 32
    rec = "c" * 32
    inst = "d" * 32
    cmd = NativeCommand.model_validate(
        {
            "receipt": rec,
            "session_id": sid,
            "provider_instance_id": inst,
            "payload": {
                "action": "send",
                "text": "hello",
                "attachment_ids": [],
                "selection_mode": "inherit_at_dispatch",
            },
        }
    )
    assert cmd.payload.action == "send"

    with pytest.raises(ValidationError):
        NativeCommand.model_validate(
            {
                "receipt": rec,
                "session_id": sid,
                "provider_instance_id": inst,
                "payload": {
                    "action": "send",
                    "text": "hello",
                    "selection_mode": "inherit_at_dispatch",
                    "selection": {"model": "gpt-5"},
                },
            }
        )

    with pytest.raises(ValidationError):
        NativeCommand.model_validate(
            {
                "receipt": rec,
                "session_id": sid,
                "provider_instance_id": inst,
                "payload": {
                    "action": "interrupt",
                    "target_run_id": "run-1",
                    "provider_turn_id": "turn-1",
                    "worker_epoch": 1,
                    "extra_rpc": "forbidden",
                },
            }
        )

    unknown = decode_raw_source_line(
        json.dumps(
            {
                "schema_version": 1,
                "session_id": sid,
                "worker_epoch": 2,
                "kind": "future.control.action",
                "body": {"foo": "bar"},
            }
        ).encode("utf-8"),
        stream_epoch="ep-1",
        start_cursor=0,
        end_cursor=100,
    )
    assert unknown.is_unknown_control is True
    assert unknown.category == "critical_control"

    legacy = decode_raw_source_line(
        json.dumps({"type": "delta", "text": "hi"}).encode("utf-8"),
        stream_epoch="ep-1",
        start_cursor=100,
        end_cursor=130,
    )
    assert legacy.legacy_unknown is True
    assert legacy.is_unknown_control is False
    assert legacy.category == "ordered_content"


def test_native_flags_default_safe_and_capabilities_bounded():
    flags = resolve_native_flags()
    assert flags == NativeFeatureFlags()
    assert flags.native_projection_shadow is True
    assert flags.native_capture_v2 is False
    assert flags.native_windows_structured_chat is False

    caps = ProviderCapabilities(
        provider_instance_id="e" * 32,
        revision=1,
        provider_kind="codex",
        provider_version="0.1.0",
        structured_chat={"supported": True, "available": True},
        resume={"supported": True, "available": True},
        steer={"supported": True, "available": True},
        conversation_rollback={"supported": False, "available": False, "reason_code": "UNSUPPORTED"},
        conversation_fork={"supported": False, "available": False, "reason_code": "UNSUPPORTED"},
        context_import={"supported": True, "available": True},
        model_change="next_turn",
        effort_change="next_turn",
        options_schema={"enum": ["a", "b"]},
    )
    assert caps.model_change == "next_turn"
    assert NativeEventEnvelope(session_id="f" * 32, worker_epoch=1, kind="notice").schema_version == 1
