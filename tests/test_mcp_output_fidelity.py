"""Output fidelity at the MCP presentation boundary, using synthetic receipts."""

from copy import deepcopy
import hashlib
import json

import pytest

from hub.core_tools import result
from shared.mcp_presentation import compact_output_text, output_records, token_view
from shared.unified_contracts import present_unified, translate

OP = "a" * 32
RECEIPT = {
    "continuation": {"operation_id": OP, "runtime_revision": "b" * 64},
    "log_ref": {"operation_id": OP, "source": "operation_output"},
    "failure_context": {"category": "succeeded", "grants_authority": False},
}


@pytest.mark.parametrize("wrapped", [False, True])
def test_compact_text_preserves_success_recovery_evidence(wrapped):
    data = {
        "operation_id": OP,
        "output": "ok",
        "exit_code": 0,
        "command_ok": True,
        **deepcopy(RECEIPT),
    }
    raw = (
        {
            "id": OP,
            "tool": "shell_exec",
            "state": "succeeded",
            "output": "ok",
            "result": {"ok": True, "data": data},
        }
        if wrapped
        else data
    )
    before = deepcopy(raw)
    shown = compact_output_text(raw, "exec")
    execution = shown["result"]["data"] if wrapped else shown
    for key, value in RECEIPT.items():
        assert key not in execution
    assert raw == before


@pytest.mark.parametrize("tool", ["fs_read", "unknown_tool"])
def test_compact_does_not_rewrite_non_execution_payloads(tool):
    raw = {
        "id": OP,
        "tool": tool,
        "state": "succeeded",
        "output": "source",
        "result": {
            "data": {"output": "source", "command_ok": True, "exit_code": 0, **deepcopy(RECEIPT)}
        },
    }
    assert compact_output_text(raw) == raw
    assert present_unified(raw) == raw
    assert output_records(raw) == []
    assert token_view("process", {}, raw, output_mode="normalized") == raw
    raw["output"] = "repeat\n" * 20000
    shown = result(
        "process", {"operation": "get"}, {"operations": [raw]}, output_mode="normalized"
    )["structuredContent"]["operations"][0]
    assert shown["output"] == raw["output"]
    assert "output_budget_limited" not in shown


def test_unknown_tool_nested_operations_are_not_visited_as_log_receipts():
    child = {
        "id": OP,
        "tool": "shell_exec",
        "state": "succeeded",
        "output": "repeat\nrepeat\n",
        "result": {"data": {"output": "repeat\nrepeat\n"}},
    }
    raw = {"id": OP, "tool": "unknown_tool", "state": "succeeded", "operations": [child]}
    assert output_records(raw) == []
    assert token_view("process", {}, raw, output_mode="normalized") == raw
    assert compact_output_text(raw) == raw
    assert present_unified(raw) == raw


def test_operation_id_alone_does_not_classify_arbitrary_output_as_a_log():
    raw = {"operation_id": OP, "output": "repeat\nrepeat\n"}
    assert output_records(raw) == []
    assert token_view("unknown_tool", {}, raw, output_mode="normalized") == raw


@pytest.mark.parametrize("view", [2, 3])
def test_unified_exec_preserves_success_recovery_evidence(view):
    raw = {
        "operation_id": OP,
        "state": "succeeded",
        "pending": False,
        "exit_code": 0,
        "command_ok": True,
        "output": "ok",
        "recovery": {"task_state": "succeeded", "task_outcome_confirmed": True},
        **deepcopy(RECEIPT),
    }
    before = deepcopy(raw)
    plan = translate(
        "act",
        {
            "action": "exec",
            "project": "P",
            "command": "true",
            "idempotency_key": "synthetic-fidelity",
        },
    )
    shown = present_unified(raw, plan=plan, view=view)
    for key in (*RECEIPT, "recovery"):
        assert key not in shown
    assert raw == before


@pytest.mark.parametrize("normalize", [False, True])
@pytest.mark.parametrize("text_mode,mirror", [("json", "full"), ("compact", "meta")])
def test_log_budget_keeps_exact_source_range(normalize, text_mode, mirror):
    output = "".join(f"\x1b[31mline {i} 😀\x1b[0m\n" for i in range(10000))
    raw = {
        "operation_id": OP,
        "exit_code": 0,
        "command_ok": True,
        "output": output,
        **deepcopy(RECEIPT),
    }
    shown = result(
        "exec",
        {},
        raw,
        text_mode=text_mode,
        mirror=mirror,
        output_mode="normalized" if normalize else "raw",
    )
    public = shown["structuredContent"]
    assert public["output_budget_limited"]
    start, end = public["output_range"]
    source = output[start:end]
    if normalize:
        assert public["output_original_chars"] == len(source)
        assert public["output_original_bytes"] == len(source.encode("utf-8"))
        assert (
            public["output_original_sha256"] == hashlib.sha256(source.encode("utf-8")).hexdigest()
        )
    else:
        assert "output_original_sha256" not in public
        body = (
            shown["content"][0]["text"].partition("\n")[2] if mirror == "meta" else public["output"]
        )
        assert body == source
    assert public["output_truncated_by"] == ["budget"]
    assert "output_original_scope" not in public
    assert public["output_next"]["arguments"]["operation_ids"] == [OP]
    assert raw["output"] == output
    full = result("exec", {}, raw, verbosity="full", output_mode="normalized")
    assert full["structuredContent"]["output"] == output
    assert "output_original_sha256" not in full["structuredContent"]


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("view", [2, 3])
def test_unified_flatten_keeps_output_and_fingerprint_from_same_source(large, view):
    outer = "".join(f"outer plain line {i}\n" for i in range(10000)) if large else "tail\n"
    inner = "\x1b[31mfull\x1b[0m\ntail\n"
    raw = {
        "id": OP,
        "tool": "shell_exec",
        "state": "succeeded",
        "output": outer,
        "result": {
            "ok": True,
            "data": {"operation_id": OP, "exit_code": 0, "command_ok": True, "output": inner},
        },
    }
    plan = translate(
        "act",
        {
            "action": "exec",
            "project": "P",
            "command": "true",
            "idempotency_key": "synthetic-flatten",
        },
    )
    shown = result("exec", {}, raw, unified=plan, view=view, output_mode="normalized")[
        "structuredContent"
    ]
    if large:
        start, end = shown["output_range"]
        assert shown["output"] == outer[start:end]
        assert shown["output_truncated_by"] == ["budget"]
    else:
        assert shown["output"] == outer
    assert "output_original_sha256" not in shown
    assert "output_normalized" not in shown
    assert "output_recovery" not in shown
