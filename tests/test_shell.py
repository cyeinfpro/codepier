"""Full-access execution: permissions, real processes, MCP and durable recovery."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.config import validate_config
from agent.runner import Agent
from agent.journal import Journal
from agent.shell import execution_info
from hub.runtime import Principal, Runtime, remote_codex_denial
from shared.contracts import ShellExec, tool_definitions
from shared.crypto import token
from shared.util import DevError, atomic_json, safe_summary
from tests.support import wait_for


@pytest.fixture
def shell_agent(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    path = tmp_path / "config.json"
    atomic_json(path, {"hub_url": "http://127.0.0.1:9", "device_id": "fixture", "secret": token(),
                      "state_dir": str(tmp_path / "state"),
                      "allowed_roots": [{"path": str(root), "writable": True, "allow_tasks": True}],
                      "shell": {"enabled": True, "projects": ["fixture"], "command": ["/bin/sh", "-c"]}})
    agent = Agent(path)
    yield agent, root
    agent.journal.db.close()
    agent.instance_lock.close()


def shell_request(root, command="printf ok", **overrides):
    return {"id": uuid.uuid4().hex, "tool": "shell_exec",
            "project": {"root": str(root), "alias": "fixture", "mode": "write", "allow_tasks": True},
            "args": {"project": "fixture", "command": command, "idempotency_key": uuid.uuid4().hex, **overrides}}


@pytest.mark.parametrize("setting,value", [("enabled", "true"), ("inherit_env", 1), ("projects", "*"),
    ("command", []), ("command", ["sh", "\x00"]), ("max_timeout_seconds", 0),
    ("max_timeout_seconds", True), ("env", {"A=B": "x"}), ("env", {"X": "\x00"}), ("typo", True)])
def test_invalid_local_shell_config_rejected(shell_agent, setting, value):
    agent, _ = shell_agent
    c = copy.deepcopy(agent.config)
    c["shell"][setting] = value
    with pytest.raises(ValueError):
        validate_config(c, agent.config_path)


@pytest.mark.parametrize("override", [{"command": "\x00"}, {"command": "  "}, {"cwd": "\x00"},
    {"env": {"A=B": "x"}}, {"env": {"X": 1}}, {"env": {"X": "x" * 65537}}, {"timeout_seconds": 86401}])
def test_bad_remote_shell_args_rejected(override):
    with pytest.raises(ValidationError):
        ShellExec(project="fixture", idempotency_key="shell-validation", **{"command": "echo ok", **override})


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["disabled", "unlisted", "local_tasks", "project_tasks", "local_readonly", "project_readonly"])
async def test_permission_denial_never_starts_command(shell_agent, denial):
    agent, root = shell_agent
    req = shell_request(root, "touch should-not-exist")
    if denial == "disabled": agent.config["shell"]["enabled"] = False
    elif denial == "unlisted": agent.config["shell"]["projects"] = []
    elif denial == "local_tasks": agent.config["allowed_roots"][0]["allow_tasks"] = False
    elif denial == "project_tasks": req["project"]["allow_tasks"] = False
    elif denial == "local_readonly": agent.config["allowed_roots"][0]["writable"] = False
    else: req["project"]["mode"] = "read"
    await agent.handle(req)
    result = agent.journal.status(req["id"])["result"]
    assert not result["ok"] and not (root / "should-not-exist").exists()
    assert result["error"]["code"] in {"READ_ONLY", "TASKS_DISABLED", "SHELL_DISABLED", "SHELL_NOT_ALLOWED"}


@pytest.mark.asyncio
async def test_external_cwd_env_and_idempotent_execution(shell_agent, tmp_path, monkeypatch):
    agent, root = shell_agent
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setenv("CODEPIER_INHERITED_TEST", "inherited")
    agent.config["shell"]["env"] = {"CODEPIER_LOCAL_TEST": "local", "CODEPIER_OVERRIDE_TEST": "local"}
    command = 'printf "%s/%s/%s" "$CODEPIER_INHERITED_TEST" "$CODEPIER_LOCAL_TEST" "$CODEPIER_OVERRIDE_TEST"; printf once >> count.txt; printf stderr >&2; exit 7'
    req = shell_request(root, command, cwd=str(outside), env={"CODEPIER_OVERRIDE_TEST": "remote"})
    await agent.handle(req)
    await agent.handle(req)
    data = agent.journal.status(req["id"])["result"]["data"]
    assert data["exit_code"] == 7 and data["cwd"] == str(outside.resolve())
    assert "inherited/local/remote" in data["output"] and "stderr" in data["output"]
    assert (outside / "count.txt").read_text() == "once"


@pytest.mark.asyncio
async def test_local_timeout_cap_and_bad_directory_fail_before_spawn(shell_agent):
    agent, root = shell_agent
    agent.config["shell"]["max_timeout_seconds"] = 10
    for overrides, error in [({"timeout_seconds": 11}, "SHELL_TIMEOUT_LIMIT"),
                             ({"timeout_seconds": 1, "cwd": "missing"}, "INVALID_CWD")]:
        req = shell_request(root, "touch should-not-exist", **overrides)
        await agent.handle(req)
        assert agent.journal.status(req["id"])["result"]["error"]["code"] == error
    assert not (root / "should-not-exist").exists()


@pytest.mark.asyncio
async def test_shell_timeout_cleans_up(shell_agent):
    agent, root = shell_agent
    req = shell_request(root, "printf started; sleep 10; touch should-not-exist", timeout_seconds=1)
    await asyncio.wait_for(agent.handle(req), 6)
    data = agent.journal.status(req["id"])["result"]["data"]
    assert data["timed_out"] and not agent.processes and not (root / "should-not-exist").exists()


def test_diagnostics_and_cli_redact_values_and_default_to_disabled(shell_agent):
    agent, root = shell_agent
    agent.config["shell"]["env"] = {"CODEPIER_SAMPLE": "private-value"}
    project = shell_request(root)["project"]
    info = execution_info(agent.config, project, agent.config["allowed_roots"][0], root)
    assert info["shell"]["enabled"] and info["shell"]["environment_keys"] == ["CODEPIER_SAMPLE"]
    assert "private-value" not in json.dumps(info)
    assert safe_summary({"env": {"CODEPIER_SAMPLE": "private-value"}, "token": "private-token"})["env"] == {"CODEPIER_SAMPLE": "<redacted>"}
    c = copy.deepcopy(agent.config)
    del c["shell"]
    assert not validate_config(c, agent.config_path)["shell"]["enabled"]
    atomic_json(agent.config_path, agent.config)
    result = subprocess.run([sys.executable, "-m", "agent", "--config", str(agent.config_path), "show"], capture_output=True, text=True, check=True)
    assert "private-value" not in result.stdout and "<redacted>" in result.stdout


def test_cli_full_access_keeps_identity_and_task_config(shell_agent):
    agent, _ = shell_agent
    old = copy.deepcopy(agent.config)
    subprocess.run([sys.executable, "-m", "agent", "--config", str(agent.config_path), "configure", "--shell", "full"], capture_output=True, check=True, timeout=5)
    c = json.loads(agent.config_path.read_text())
    assert c["shell"]["enabled"] and c["shell"]["projects"] == ["*"]
    assert c["secret"] == old["secret"] and c["tasks"] == old["tasks"] and c["state_dir"] == old["state_dir"]


def test_shell_mcp_is_an_execute_write_action():
    definition = next(t for t in tool_definitions() if t["name"] == "shell_exec")
    assert not definition["annotations"]["readOnlyHint"]
    assert definition["annotations"]["destructiveHint"] and definition["annotations"]["openWorldHint"]
    assert set(definition["_meta"]["securitySchemes"][0]["scopes"]) == {"read", "execute"}
    assert definition["securitySchemes"] == definition["_meta"]["securitySchemes"]


@pytest.mark.asyncio
async def test_remote_codex_policy_blocks_before_dispatch_but_preserves_admin_and_shell(monkeypatch):
    class AuditStore:
        def __init__(self):
            self.rows = []

        def audit(self, *args, **kwargs):
            self.rows.append((args, kwargs))

    store = AuditStore()
    runtime = object.__new__(Runtime)
    runtime.store = store
    principal = Principal("mcp:test:ChatGPT", "user", {"read", "execute", "computer"}, ["*"])
    monkeypatch.setenv("MCP_BLOCK_LOCAL_CODEX", "true")

    attempts = [
        ("shell_exec", {"project": "MCP", "command": "/Users/test/.local/bin/codex exec task",
                        "cwd": ".", "timeout_seconds": 60, "env": {}, "idempotency_key": "codex-block-shell"}),
        ("computer_status", {"project": "MCP", "probe": True}),
        ("computer_apps", {"project": "MCP"}),
    ]
    for name, args in attempts:
        with pytest.raises(DevError) as caught:
            await runtime.invoke(name, args, principal)
        assert caught.value.code == "CODEX_REMOTE_DISABLED"

    assert len(store.rows) == len(attempts)
    assert all(row[1]["status"] == "denied" for row in store.rows)
    assert remote_codex_denial("shell_exec", {"command": "printf ordinary-shell"}, principal) is None
    assert remote_codex_denial("computer_status", {"probe": False}, principal) is None
    assert remote_codex_denial("computer_session_close", {}, principal) is None
    admin = Principal("panel:admin", "user", {"read", "execute", "computer"}, ["*"], admin=True)
    assert remote_codex_denial("shell_exec", {"command": "codex --version"}, admin) is None


@pytest.mark.asyncio
async def test_revocation_while_waiting_for_project_prevents_shell(shell_agent):
    agent, root = shell_agent
    req = shell_request(root, "touch should-not-exist")
    async with agent.project_slot(root.resolve()):
        job = asyncio.create_task(agent.handle(req))
        while agent.journal.status(req["id"])["status"] == "missing":
            await asyncio.sleep(.01)
        agent.config["shell"]["enabled"] = False
    await asyncio.wait_for(job, 3)
    assert agent.journal.status(req["id"])["result"]["error"]["code"] == "SHELL_DISABLED"
    assert not (root / "should-not-exist").exists()


@pytest.mark.asyncio
async def test_diagnostics_not_blocked_by_long_project_job(shell_agent):
    agent, root = shell_agent
    req = shell_request(root)
    req["tool"] = "execution_info"
    req["args"] = {"project": "fixture"}
    async with agent.project_slot(root.resolve()):
        await asyncio.wait_for(agent.handle(req), .5)
    assert agent.journal.status(req["id"])["result"]["data"]["shell"]["enabled"]


@pytest.mark.asyncio
async def test_shell_can_disable_environment_inheritance(shell_agent, monkeypatch):
    agent, root = shell_agent
    monkeypatch.setenv("CODEPIER_PRIVATE_PARENT", "should-not-inherit")
    agent.config["shell"]["inherit_env"] = False
    req = shell_request(root, 'printf "%s" "${CODEPIER_PRIVATE_PARENT-unset}"')
    await agent.handle(req)
    assert agent.journal.status(req["id"])["result"]["data"]["output"] == "unset"


def test_agent_restart_does_not_replay_started_shell(tmp_path):
    journal = Journal(tmp_path / "state")
    req = {"tool": "shell_exec", "args": {"command": "publish"}}
    journal.start("started-shell", req)
    journal.mark_running("started-shell")
    journal.db.close()
    resumed = Journal(tmp_path / "state")
    try:
        result = resumed.start("started-shell", req)
        assert result["error"]["code"] == "INTERRUPTED"
    finally:
        resumed.db.close()


def test_real_mcp_shell_streaming_replay_restart_cancel_and_scope(stack):
    stack.stop_agent()
    stack.config["shell"] = {"enabled": True, "projects": ["Imago"], "command": ["/bin/sh", "-c"]}
    atomic_json(stack.config_path, stack.config)
    stack.start_agent()
    info = stack.mcp("execution_info", {"project": "Imago"})
    assert info["structuredContent"]["shell"]["enabled"]
    args = {"project": "Imago", "command": "printf started; printf once >> shell-count; sleep 3; printf done; exit 7", "idempotency_key": uuid.uuid4().hex}
    receipt = stack.mcp("shell_exec", args)["structuredContent"]
    opid = receipt["operation_id"]
    assert receipt["pending"]
    wait_for(lambda: "started" in stack.client.get('/api/operations/' + opid).json()["output"])
    assert stack.mcp("shell_exec", args)["structuredContent"]["operation_id"] == opid
    stack.hub.terminate(); stack.hub.wait(timeout=12); stack.start_hub(); stack.login()
    completed = stack.poll(opid, timeout=15)
    assert completed["state"] == "failed" and completed["result"]["data"]["exit_code"] == 7
    assert "done" in completed["output"] and (stack.imago / "shell-count").read_text() == "once"
    cancelled = stack.mcp("shell_exec", {"project": "Imago", "command": "printf cancel-start; sleep 20; touch must-not-exist", "idempotency_key": uuid.uuid4().hex})["structuredContent"]["operation_id"]
    wait_for(lambda: "cancel-start" in stack.client.get('/api/operations/' + cancelled).json()["output"])
    stack.mcp("operations_cancel", {"operation_id": cancelled})
    assert stack.poll(cancelled)["state"] == "cancelled"
    assert not (stack.imago / "must-not-exist").exists()
    grant = stack.must(stack.client.post('/api/grants', json={"label": "shell-read-only", "scopes": ["read"], "projects": [stack.project['id']], "days": 1}))
    denied = stack.mcp("shell_exec", {**args, "idempotency_key": uuid.uuid4().hex}, token_value=grant["token"])
    assert denied["isError"] and "execute" in denied["content"][0]["text"]


def test_offline_shell_revoked_before_delivery_does_not_execute(stack):
    stack.stop_agent()
    grant = stack.must(stack.client.post('/api/grants', json={"label": "shell-revoke", "scopes": ["read", "execute"], "projects": [stack.project['id']], "days": 1}))
    receipt = stack.mcp("shell_exec", {"project": "Imago", "command": "touch revoked-shell", "idempotency_key": uuid.uuid4().hex}, token_value=grant["token"])["structuredContent"]
    stack.must(stack.client.delete('/api/grants/' + grant['grant_id']))
    stack.start_agent()
    assert stack.poll(receipt["operation_id"])["state"] == "failed"
    assert not (stack.imago / "revoked-shell").exists()
