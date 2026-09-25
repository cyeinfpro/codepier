"""Real MCP continuation after a caller pauses or abandons its wait request."""
from __future__ import annotations

import shlex
import sys
import uuid

import httpx
import pytest
from jsonschema import Draft202012Validator

from shared.util import atomic_json
from tests.support import running_stack, wait_for


def command(counter, *, exit_code=0, delay=2.5):
    return [sys.executable, "-u", "-c", (
        "import pathlib,time,sys; "
        f"p=pathlib.Path({counter!r}); "
        "p.open('a').write('once\\n'); "
        "print('CONTINUATION_STARTED',flush=True); "
        f"time.sleep({delay}); "
        "print('CONTINUATION_FINISHED',flush=True); "
        "p.with_suffix('.done').write_text('finished'); "
        f"sys.exit({exit_code})"
    )]


@pytest.fixture(scope="module")
def continuation_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp("operation-continuation")) as stack:
        stack.stop_agent()
        stack.config["shell"] = {"enabled": True, "projects": ["Imago"], "command": ["/bin/sh", "-c"]}
        stack.config["tasks"]["continuation"] = {
            "command": command("full-tasks_run.count"), "projects": ["Imago"], "timeout": 10,
        }
        atomic_json(stack.config_path, stack.config)
        stack.start_agent()
        yield stack


def headers(stack):
    return {
        "Authorization": "Bearer " + stack.pat,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }


def rpc(stack, profile, method, params=None):
    response = stack.client.post(
        "/mcp?profile=" + profile, headers=headers(stack),
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "error" not in body, body
    return body["result"]


@pytest.fixture(scope="module")
def catalog(continuation_stack):
    return {
        profile: {tool["name"]: tool for tool in rpc(continuation_stack, profile, "tools/list")["tools"]}
        for profile in ("full", "coding")
    }


def call(stack, catalog, profile, name, arguments):
    from hub.core_tools import public_call
    name, arguments = public_call(name, arguments)
    definition = catalog[profile][name]
    Draft202012Validator(definition["inputSchema"]).validate(arguments)
    result = rpc(stack, profile, "tools/call", {"name": name, "arguments": arguments})
    Draft202012Validator(definition["outputSchema"]).validate(result["structuredContent"])
    return result


def follow(stack, catalog, profile, continuation, **overrides):
    result = call(stack, catalog, profile, continuation["name"], {**continuation["arguments"], **overrides})
    return {**result, 'structuredContent': result['structuredContent']['operations'][0]}


@pytest.mark.parametrize("profile,name", [("full", "shell_exec"), ("coding", "shell_exec"), ("full", "tasks_run")])
def test_paused_polling_resumes_original_operation_once(continuation_stack, catalog, profile, name):
    stack = continuation_stack
    counter = f"{profile}-{name}.count"
    arguments = {"project": "Imago", "idempotency_key": uuid.uuid4().hex}
    arguments.update({"task": "continuation"} if name == "tasks_run" else {"command": shlex.join(command(counter))})
    submitted = call(stack, catalog, profile, name, arguments)
    assert not submitted["isError"], submitted
    receipt = submitted["structuredContent"]
    operation_id = receipt["operation_id"]
    assert receipt["pending"]
    assert receipt["next_call"]["name"] == "process"
    assert receipt["next_call"]["arguments"] == {
        "operation": "wait", "operation_ids": [operation_id], "wait_seconds": 10, "output_limit": 8000,
        "include_result": True, "include_output": True,
    }

    # Shorten only this test's first wait to exercise an actual pending response.
    pending = follow(stack, catalog, profile, receipt["next_call"], wait_seconds=1)["structuredContent"]
    assert pending["pending"] and pending["result"] is None
    assert pending["next_call"]["arguments"]["operation_ids"] == [operation_id]
    assert pending["next_call"]["arguments"]["after_output_seq"] == pending["output_seq"]

    # No operation requests while the external caller is absent. The local
    # marker proves work keeps running without a connected polling loop.
    wait_for(lambda: (stack.imago / counter).with_suffix(".done").exists(), timeout=8)
    completed = follow(stack, catalog, profile, pending["next_call"])
    assert not completed["isError"], completed
    terminal = completed["structuredContent"]
    assert terminal["operation_id"] == operation_id
    assert terminal["state"] == "succeeded" and not terminal["pending"]
    assert terminal["result"]["data"]["exit_code"] == 0
    assert "CONTINUATION_FINISHED" in terminal["output"]
    assert terminal["next_call"] is None
    assert (stack.imago / counter).read_text() == "once\n"


@pytest.mark.parametrize("profile", ["full", "coding"])
def test_compact_terminal_response_recovers_failure_evidence(continuation_stack, catalog, profile):
    stack = continuation_stack
    submitted = call(stack, catalog, profile, "shell_exec", {
        "project": "Imago", "command": "printf 'EXPECTED_CONTINUATION_FAILURE\\n'; exit 7",
        "idempotency_key": uuid.uuid4().hex,
    })
    assert not submitted["isError"], submitted
    receipt = submitted["structuredContent"]
    compact = follow(stack, catalog, profile, receipt["next_call"], include_output=False, include_result=False)["structuredContent"]
    assert compact["state"] == "failed" and not compact["pending"]
    assert "output" not in compact and "result" not in compact
    assert compact["next_call"]["name"] == "process" and compact["next_call"]["arguments"]["operation"] == "get"
    assert compact["next_call"]["arguments"]["operation_ids"] == [receipt["operation_id"]]
    assert "after_output_seq" not in compact["next_call"]["arguments"]

    recovered = follow(stack, catalog, profile, compact["next_call"])
    # Failed commands remain visibly failed when their durable evidence is read.
    assert recovered["isError"], recovered
    terminal = recovered["structuredContent"]
    assert terminal["state"] == "failed" and terminal["next_call"] is None
    assert terminal["result"]["data"]["exit_code"] == 7
    assert terminal["result"]["data"]["command_ok"] is False
    assert "EXPECTED_CONTINUATION_FAILURE" in terminal["output"]
    assert "EXPECTED_CONTINUATION_FAILURE" in terminal["result"]["data"]["output"]


def test_http_wait_timeout_does_not_cancel_local_execution(continuation_stack, catalog):
    stack = continuation_stack
    counter = "disconnected-wait.count"
    receipt = call(stack, catalog, "coding", "shell_exec", {
        "project": "Imago", "command": shlex.join(command(counter)), "idempotency_key": uuid.uuid4().hex,
    })["structuredContent"]
    wait_for(lambda: (stack.imago / counter).exists(), timeout=5)
    continuation = receipt["next_call"]
    # Abandon an in-flight HTTP wait while leaving the actual command running.
    with httpx.Client(base_url=stack.url, trust_env=False, timeout=httpx.Timeout(5, read=.2)) as client:
        with pytest.raises(httpx.ReadTimeout):
            client.post("/mcp?profile=coding", headers=headers(stack), json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": continuation,
            })
    wait_for(lambda: (stack.imago / counter).with_suffix(".done").exists(), timeout=8)
    completed = follow(stack, catalog, "coding", continuation)
    assert not completed["isError"], completed
    terminal = completed["structuredContent"]
    assert terminal["operation_id"] == receipt["operation_id"]
    assert terminal["state"] == "succeeded" and terminal["result"]["data"]["exit_code"] == 0
    assert terminal["next_call"] is None
    assert (stack.imago / counter).read_text() == "once\n"
