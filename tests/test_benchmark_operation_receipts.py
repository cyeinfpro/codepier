"""Benchmark failure accounting using the MCP operation receipt contract."""

import asyncio
import time

import httpx
import pytest

from scripts import benchmark_hub_agent as benchmark


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,data,error,expected",
    [
        ("failed", {"timed_out": True}, None, "TASK_TIMEOUT"),
        ("failed", None, {"code": "QUEUE_EXPIRED"}, "QUEUE_EXPIRED"),
        ("cancelled", None, {"code": "CANCELLED"}, "CANCELLED"),
        ("failed", None, {"code": "DEVICE_BUSY", "admitted": True}, "DEVICE_BUSY"),
    ],
)
async def test_failed_operation_is_a_valid_task_query_receipt(
    monkeypatch, state, data, error, expected
):
    seen = []

    async def rpc(client, name, args):
        seen.append((name, args))
        if name == "exec":
            return {"operation_id": "original", "pending": True, "state": "running"}, False
        return {
            "operations": [
                {
                    "operation_id": "original",
                    "pending": False,
                    "state": state,
                    "result": {"ok": error is None, "data": data, "error": error},
                }
            ]
        }, True

    monkeypatch.setattr(benchmark, "rpc", rpc)
    sample, abort = {}, asyncio.Event()
    await benchmark.measure_operation(
        None, None, benchmark.FileLane(0), "long", None, sample, 1, abort
    )
    assert [name for name, _ in seen] == ["exec", "task_query"]
    assert seen[-1][1]["operation_ids"] == ["original"]
    assert sample == {
        "ok": False,
        "operation_id": "original",
        "state": state,
        "polls": 1,
        "error": expected,
    }
    assert not abort.is_set()


@pytest.mark.asyncio
async def test_immediate_tool_error_confirms_original_id(monkeypatch):
    seen = []

    async def rpc(client, name, args):
        seen.append((name, args))
        if name == "write":
            return {"error": {"code": "SHA_CONFLICT", "operation_id": "original"}}, True
        return {
            "operations": [
                {
                    "id": "original",
                    "state": "failed",
                    "result": {"ok": False, "error": {"code": "SHA_CONFLICT"}},
                }
            ]
        }, True

    monkeypatch.setattr(benchmark, "rpc", rpc)
    lane, sample, abort = benchmark.FileLane(0), {}, asyncio.Event()
    initial = lane.current_sha
    await benchmark.measure_operation(None, None, lane, "write", "change", sample, 1, abort)
    assert [name for name, _ in seen] == ["write", "task_query"]
    assert seen[-1][1]["operation_ids"] == ["original"]
    assert sample["operation_id"] == "original" and sample["error"] == "SHA_CONFLICT"
    assert lane.current_sha == initial and not sample["ok"] and not abort.is_set()


@pytest.mark.asyncio
async def test_initial_request_has_one_deadline_and_never_replays(monkeypatch):
    seen = []

    async def rpc(client, name, args):
        seen.append(name)
        if name == "read":
            await asyncio.Event().wait()
        return {
            "operations": [
                {"id": "original", "state": "succeeded", "result": {"ok": True}}
            ]
        }, False

    monkeypatch.setattr(benchmark, "rpc", rpc)
    monkeypatch.setattr(benchmark, "lookup_original", lambda *args: "original")
    sample, abort = {}, asyncio.Event()
    await benchmark.measure_operation(
        None, None, benchmark.FileLane(0), "read", None, sample, 0.05, abort
    )
    assert seen == ["read", "task_query"] and abort.is_set()
    assert sample["error"] == "TimeoutError" and not sample["ok"]
    assert sample["recovered"] and not sample["unresolved"] and sample["recovery_polls"] == 1


def test_extract_checks_response_binding():
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "http://127.0.0.1/mcp"),
        json={"jsonrpc": "2.0", "id": "other-request", "result": {"structuredContent": {}}},
    )
    with pytest.raises(ValueError, match="invalid_jsonrpc_response"):
        benchmark.extract(response, "requested")


@pytest.mark.asyncio
async def test_poll_denial_with_same_id_is_still_unresolved(monkeypatch):
    async def rpc(client, name, args):
        return {
            "operations": [
                {"operation_id": "original", "error": {"code": "INSUFFICIENT_SCOPE"}}
            ]
        }, True

    monkeypatch.setattr(benchmark, "rpc", rpc)
    with pytest.raises(RuntimeError, match="original_operation_poll_denied"):
        await benchmark.finish_original(
            None, {"operation_id": "original", "pending": True}, False, time.monotonic() + 1
        )
