"""End-to-end saved-tail paging and presentation invariants."""

from copy import deepcopy
import hashlib
import json

import pytest

from hub.core_tools import result, bound_batch_response
from hub.operation_output import output_window
from shared.core_contracts import READ_BATCH_PAYLOAD_BYTES, Process, output_policy
from shared.mcp_presentation import compact_output_text, raw_output_sources
from shared.output_fidelity import output_page, output_next
from shared.runtime_results import decorate_result
from shared.unified_contracts import present_unified, translate
from shared.util import DevError
from tests.test_operation_output_storage import runtime, start, emit, finish

OP = "a" * 32


def exec_plan():
    return translate(
        "act",
        {
            "action": "exec",
            "project": "P",
            "command": "echo ok",
            "idempotency_key": "output-fidelity-test",
        },
    )


def receipt(text="ok\n"):
    return {
        "operation_id": OP,
        "state": "succeeded",
        "pending": False,
        "exit_code": 0,
        "command_ok": True,
        "output": text,
        "failure_context": {"category": "succeeded"},
        "continuation": {"operation_id": OP},
        "log_ref": {"operation_id": OP},
        "recovery": {"task_state": "succeeded", "task_outcome_confirmed": True},
    }


@pytest.mark.parametrize(
    "view,text_mode,maximum",
    [(2, "json", 931), (2, "compact", 919), (3, "json", 390), (3, "compact", 378)],
)
def test_complete_success_byte_guard(view, text_mode, maximum):
    runtime_ref = {
        "schema_version": 1,
        "runtime_ref": "b" * 64,
        "revision": "c" * 64,
        "mode": "project_clean_v1",
        "grants_authority": False,
        "filesystem_scope": "execution_account",
        "isolation": "none",
    }
    raw = decorate_result(
        {
            "exit_code": 0,
            "command_ok": True,
            "output": "ok\n",
            "duration_ms": 1,
            "output_truncated": False,
            "timed_out": False,
            "cancelled": False,
            "target": "agent",
        },
        {"id": "d" * 32},
        OP,
        runtime_ref,
        ".",
    )
    shown = result("exec", {}, raw, unified=exec_plan(), view=view, text_mode=text_mode)
    assert len(json.dumps(shown, ensure_ascii=False).encode()) <= maximum


@pytest.mark.parametrize(
    "changes,fold,keep_pointer",
    [
        ({}, True, False),
        ({"output_truncated": True}, True, True),
        ({"output_normalized": True}, True, True),
        ({"state": "failed", "exit_code": 1, "command_ok": False}, False, True),
        ({"timed_out": True}, False, True),
        ({"cancelled": True}, False, True),
        ({"state": "running", "pending": True}, False, True),
        ({"state": "needs_review"}, False, True),
    ],
)
@pytest.mark.parametrize("surface", ["compact", "exec", "job"])
def test_success_folding_matrix(changes, fold, keep_pointer, surface):
    raw = {**receipt(), **changes}
    saved = deepcopy(raw)
    if surface == "compact":
        shown = compact_output_text(raw, "exec")
    elif surface == "exec":
        shown = present_unified(raw, plan=exec_plan())
    else:
        shown = present_unified({"operations": [{**raw, "tool": "shell_exec"}]})["operations"][0]
    for key in ("failure_context", "recovery"):
        assert (key in shown) != fold
    for key in ("continuation", "log_ref"):
        assert (key in shown) == keep_pointer
    assert raw == saved


@pytest.mark.parametrize("view", [2, 3])
@pytest.mark.parametrize(
    "text_mode,mirror", [("json", "full"), ("compact", "full"), ("compact", "meta")]
)
def test_monotonic_contiguous_budget(view, text_mode, mirror):
    stored = "".join(f'{i:05d} 行😀 \\"\t payload\n' for i in range(6000))[-131072:]
    previous = 0
    for limit in sorted(set(range(1000, 51201, 1000)) | {19992, 24000, 30000, 51192, 51200}):
        raw = receipt()
        window = output_window(stored, limit)
        output_page(raw, window, len(stored), start=len(stored) - len(window), upstream=True)
        output_next(raw, project="P", unified=True)
        shown = result(
            "exec", {}, raw, unified=exec_plan(), view=view, text_mode=text_mode, mirror=mirror
        )
        public = shown["structuredContent"]
        body = (
            shown["content"][0]["text"].partition("\n")[2] if mirror == "meta" else public["output"]
        )
        a, b = public["output_range"]
        assert body == stored[a:b]
        assert len(body) >= previous, (limit, previous, len(body))
        previous = len(body)
        assert public["output_truncated_by"][:2] == ["upstream", "limit"]
        if public.get("output_budget_limited"):
            assert public["output_truncated_by"] == ["upstream", "limit", "budget"]
            assert a == 0 or stored[a - 1] == "\n"
            assert b == len(stored) or stored[b - 1] == "\n"
            assert "output_original_sha256" not in public
        envelope = {"jsonrpc": "2.0", "id": None, "result": shown}
        assert (
            len(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")).encode())
            <= READ_BATCH_PAYLOAD_BYTES
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("view", [2, 3])
async def test_follow_actual_query_hints_reconstructs_saved_tail(runtime, view):
    instance, principal, peer, identifier = await start(runtime)
    stored = ("text 数据😀\n" * 17000)[-131072:]
    emit(instance, peer, identifier, stored)
    finish(instance, identifier, output=stored, command_ok=True, output_truncated=True)
    request = {
        "action": "job",
        "project": "ProjectAlpha",
        "ids": [identifier],
        "args": {"output_limit": 51200},
    }
    end, chunks = len(stored), []
    for _ in range(32):
        plan = translate("query", request)
        value = await instance.invoke(plan.tool, plan.args, principal)
        shown = result(
            plan.tool, plan.args, value, unified=plan, view=view, output_mode="normalized"
        )["structuredContent"]["operations"][0]
        a, b = shown["output_range"]
        if shown.get("output_normalized"):
            # A normalized page first advertises the exact raw range.
            request = shown["output_next"]["arguments"]
            continue
        assert b == end
        assert shown["output"] == stored[a:b]
        chunks.append(shown["output"])
        end = a
        if not a:
            break
        request = shown["output_next"]["arguments"]
        assert request["project"] == "ProjectAlpha"
    assert end == 0
    assert "".join(reversed(chunks)) == stored


@pytest.mark.asyncio
async def test_range_rejects_live_operations_and_preserves_authorization(runtime):
    instance, principal, peer, identifier = await start(runtime)
    emit(instance, peer, identifier, "saved tail")
    with pytest.raises(DevError) as exc:
        instance.operation(identifier, principal, {"output_offset": 0})
    assert exc.value.code == "INVALID_ARGUMENTS"
    finish(instance, identifier, output="saved tail", output_truncated=True)
    page = instance.operation(
        identifier, principal, {"output_offset": 2, "output_limit": 3, "include_result": False}
    )
    assert page["output"] == "ved"
    assert page["output_range"] == [2, 5]
    assert page["output_truncated_by"] == ["upstream", "limit"]
    with pytest.raises(DevError):
        instance.operation(identifier, principal, {"output_offset": 100})


@pytest.mark.parametrize(
    "args",
    [
        {"operation": "list"},
        {"include_output": False},
        {"after_output_seq": 0},
        {"output_limit": 0},
    ],
)
def test_range_argument_combinations_are_validated(args):
    with pytest.raises(ValueError):
        Process.model_validate(
            {"operation": "get", "operation_ids": [OP], "output_offset": 0, **args}
        )


def test_normalized_budget_range_survives_final_protocol_envelope():
    output = "".join(f"\x1b[31mline {i} 😀\x1b[0m\n" for i in range(6000))
    raw = receipt(output)
    shown = result("exec", {}, raw, unified=exec_plan(), output_mode="normalized")
    shown["_meta"] = {"requestId": "x" * 800}
    final = bound_batch_response(
        {"jsonrpc": "2.0", "id": 1, "result": shown},
        output_budget=True,
        tool_name="exec",
        unified=True,
        project="P",
        sources=raw_output_sources(raw, "exec"),
    )["result"]["structuredContent"]
    a, b = final["output_range"]
    assert final["output_original_sha256"] == hashlib.sha256(output[a:b].encode()).hexdigest()
    assert final["output_next"]["arguments"]["args"]["output_offset"] == a


def test_output_policy_is_explicit():
    assert output_policy("shell_exec") == "log"
    assert output_policy("git_diff") == "exact"
    assert output_policy("unknown") == "exact"
    assert output_policy("shell_exec", external=True) == "passthrough"


def test_each_character_near_budget_transition_is_monotonic():
    stored = "x\n" * 50000

    def shown_length(limit):
        raw = receipt()
        window = output_window(stored, limit)
        output_page(raw, window, len(stored), start=len(stored) - len(window))
        output_next(raw, project="P", unified=True)
        shown = result("exec", {}, raw, unified=exec_plan())["structuredContent"]
        return len(shown["output"])

    cap = shown_length(51200)
    lengths = [shown_length(n) for n in range(cap - 10, cap + 401)]
    assert all(a <= b for a, b in zip(lengths, lengths[1:])), next(
        ((i, a, b) for i, (a, b) in enumerate(zip(lengths, lengths[1:])) if a > b), None
    )


def test_normalized_running_receipts_never_advertise_stable_offsets():
    data = receipt("repeat\nrepeat\n")
    row = {
        "id": OP,
        "tool": "shell_exec",
        "state": "running",
        "pending": True,
        "output": data["output"],
        "result": {"ok": True, "data": data},
    }
    shown = result("process", {}, {"operations": [row]}, output_mode="normalized")[
        "structuredContent"
    ]["operations"][0]
    assert "output_next" not in shown
    assert "output_next" not in shown["result"]["data"]


def test_budget_respects_line_end_for_an_explicit_middle_window():
    stored = "0123456789abcdef\n" * 8000
    raw = receipt()
    output_page(raw, stored[9:51209], len(stored), start=9)
    shown = result("exec", {}, raw, unified=exec_plan())["structuredContent"]
    a, b = shown["output_range"]
    assert shown["output"] == stored[a:b]
    assert stored[a - 1] == stored[b - 1] == "\n"


@pytest.mark.asyncio
async def test_terminal_ranges_survive_store_reopen(runtime):
    from hub.runtime import Runtime
    from hub.store import Store

    instance, principal, peer, identifier = await start(runtime)
    stored = "historical 数据\n" * 1000
    finish(instance, identifier, output=stored, command_ok=True)
    reopened = Store(instance.store.directory)
    try:
        page = Runtime(reopened).operation(
            identifier, principal, {"output_offset": 7, "output_limit": 503}
        )
        assert page["output"] == stored[7:510]
        assert page["output_range"] == [7, 510]
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_revoked_range_read_is_rejected_before_output_access(runtime, monkeypatch):
    from dataclasses import replace
    from tests.legacy_iam_fixture import seed_grant

    instance, owner = runtime
    seed_grant(instance.store, "range-reader", owner.user_id, projects=("proj",))
    reader = replace(owner, admin=False, actor="mcp:range-reader:fixture", grant_id="range-reader")
    instance, reader, peer, identifier = await start(runtime, reader)
    finish(instance, identifier, output="private saved tail", command_ok=True)
    instance.store.execute("UPDATE grants SET revoked=1 WHERE id='range-reader'")
    accessed = []
    original = instance.outputs.read

    def read(*args, **kwargs):
        accessed.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(instance.outputs, "read", read)
    with pytest.raises(DevError):
        instance.operation(identifier, reader, {"output_offset": 0, "output_limit": 5})
    assert not accessed


def test_budget_keeps_each_nested_receipt_window_addressable():
    rows = []
    originals = {}
    for i in range(2):
        identifier = str(i) * 32
        output = f"log {i} \u6570\u636e\n" * 10000
        originals[identifier] = output
        rows.append(
            {
                "id": identifier,
                "tool": "shell_exec",
                "state": "succeeded",
                "output": output,
                "result": {
                    "ok": True,
                    "data": {
                        "operation_id": identifier,
                        "exit_code": 0,
                        "command_ok": True,
                        "output": output,
                    },
                },
            }
        )
    shown = result("process", {}, {"operations": rows})
    assert (
        len(
            json.dumps(
                {"jsonrpc": "2.0", "id": None, "result": shown},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        )
        <= READ_BATCH_PAYLOAD_BYTES
    )
    for row in shown["structuredContent"]["operations"]:
        for slot in (row, row["result"]["data"]):
            a, b = slot["output_range"]
            assert slot["output"] == originals[row["id"]][a:b]
            assert slot["output_next"]["arguments"]["operation_ids"] == [row["id"]]
