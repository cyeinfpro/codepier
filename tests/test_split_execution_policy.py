"""Split policy matrices and mocked dispatch; never start a real native provider."""
from __future__ import annotations

from itertools import product
import uuid
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from hub.runtime import Principal, remote_execution_denial
from shared.computer_contracts import ComputerOpen
from shared.execution_policy import (
    POLICY_VERSION, COMPUTER_DENIAL_CODE, agent_blocks_codex, agent_blocks_computer,
    codex_computer_target, computer_denial, enforce_argv, hub_blocks_codex,
    hub_blocks_computer, hub_execution_policy, local_blocks_computer, validate_policy,
)
from shared.util import DevError
from agent.runner import Agent
from agent.computer import validate_computer
from shared.crypto import token
from shared.util import atomic_json


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


def shell_request(root):
    return {"id": uuid.uuid4().hex, "tool": "shell_exec",
            "project": {"root": str(root), "alias": "fixture", "mode": "write", "allow_tasks": True},
            "args": {"project": "fixture", "command": "printf ok", "idempotency_key": uuid.uuid4().hex}}


@pytest.mark.parametrize("enabled", [None, False, True])
def test_native_default_requires_existing_local_allowlists(enabled):
    value = {"projects": ["fixture"], "allowed_apps": ["Preview"]}
    if enabled is not None:
        value["enabled"] = enabled
    normalized = validate_computer(value)
    assert normalized["enabled"] is (enabled is not False)
    assert normalized["projects"] == ["fixture"] and normalized["allowed_apps"] == ["Preview"]
    assert ("enabled" in value) is (enabled is not None)
    assert not validate_computer({})["enabled"]
    assert not validate_computer({"projects": ["fixture"]})["enabled"]
    assert not validate_computer({"allowed_apps": ["Preview"]})["enabled"]



def metadata(model=False, computer=False, origin="mcp"):
    return {"version": POLICY_VERSION, "origin": origin,
            "block_local_codex": model, "block_native_computer": computer}


def principal(admin=False):
    return Principal("panel:owner" if admin else "mcp:test:owner", "owner",
                     {"read", "write", "execute", "computer"}, ["*"], admin=admin)


@pytest.mark.parametrize("legacy,native", product([False, True], [None, False, True]))
def test_default_migration_separates_native_and_preserves_explicit_floors(monkeypatch, legacy, native):
    monkeypatch.setenv("MCP_BLOCK_LOCAL_CODEX", str(int(legacy)))
    monkeypatch.delenv("MCP_BLOCK_NATIVE_COMPUTER", raising=False)
    local = {"block_local_codex": legacy}
    if native is not None:
        monkeypatch.setenv("MCP_BLOCK_NATIVE_COMPUTER", str(int(native)))
        local["block_native_computer"] = native
    expected = False if native is None else native
    config = {"mcp_policy": validate_policy(local)}
    assert hub_blocks_codex() is legacy
    assert hub_blocks_computer() is expected
    assert local_blocks_computer(config) is expected
    assert hub_execution_policy(panel=False) == metadata(legacy, expected)
    assert agent_blocks_codex(config, {"_execution_policy": metadata()}) is legacy
    assert agent_blocks_computer(config, {"_execution_policy": metadata()}) is expected
    assert ("block_native_computer" in config["mcp_policy"]) is (native is not None)


@pytest.mark.parametrize("local_model,local_native,hub_model,hub_native", product([False, True], repeat=4))
def test_independent_hub_and_agent_floors_use_or(local_model, local_native, hub_model, hub_native):
    config = {"mcp_policy": {"block_local_codex": local_model, "block_native_computer": local_native}}
    project = {"_execution_policy": metadata(hub_model, hub_native)}
    assert agent_blocks_codex(config, project) is (local_model or hub_model)
    assert agent_blocks_computer(config, project) is (local_native or hub_native)


@pytest.mark.parametrize("value", ["", "flase", "disabled", "2"])
def test_invalid_native_environment_fails_closed(monkeypatch, value):
    monkeypatch.setenv("MCP_BLOCK_LOCAL_CODEX", "0")
    monkeypatch.setenv("MCP_BLOCK_NATIVE_COMPUTER", value)
    assert hub_blocks_computer()


@pytest.mark.parametrize("value", [None, 0, 1, "false", [], {}])
def test_native_config_requires_a_boolean(value):
    with pytest.raises(ValueError):
        validate_policy({"block_native_computer": value})


@pytest.mark.parametrize("policy", [
    None, {}, [], True,
    {"version": 1, "origin": "mcp", "block_local_codex": False},
    {"version": 1, "origin": "panel", "block_local_codex": False, "block_native_computer": False},
    {"version": 2, "origin": "panel", "block_local_codex": False},
    {"version": 2, "origin": "panel", "block_local_codex": False, "block_native_computer": 0},
    {"version": True, "origin": "panel", "block_local_codex": False, "block_native_computer": False},
    {"version": 3, "origin": "panel", "block_local_codex": False, "block_native_computer": False},
])
def test_legacy_missing_or_malformed_metadata_cannot_grant_native_capability(policy):
    assert agent_blocks_computer({"mcp_policy": {"block_native_computer": False}},
                                 {"_execution_policy": policy})


def test_only_valid_current_panel_metadata_exempts_local_policy(monkeypatch):
    monkeypatch.setenv("MCP_BLOCK_LOCAL_CODEX", "1")
    monkeypatch.setenv("MCP_BLOCK_NATIVE_COMPUTER", "1")
    config = {"mcp_policy": {"block_local_codex": True, "block_native_computer": True}}
    policy = hub_execution_policy(panel=True)
    assert policy == metadata(False, False, "panel")
    assert not agent_blocks_codex(config, {"_execution_policy": policy})
    assert not agent_blocks_computer(config, {"_execution_policy": policy})
    assert remote_execution_denial("computer_apps", {}, principal(admin=True)) is None


@pytest.mark.parametrize("extra", [
    {"execution_policy": metadata(origin="panel")}, {"block_native_computer": False}, {"origin": "panel"},
])
def test_tool_arguments_cannot_supply_split_capability(extra):
    with pytest.raises(ValidationError):
        ComputerOpen(project="fixture", app="Preview", idempotency_key="split-policy-spoof", **extra)


@pytest.mark.parametrize("model,native", product([False, True], repeat=2))
def test_hub_errors_identify_independent_capability(monkeypatch, model, native):
    monkeypatch.setenv("MCP_BLOCK_LOCAL_CODEX", str(int(model)))
    monkeypatch.setenv("MCP_BLOCK_NATIVE_COMPUTER", str(int(native)))
    codex = remote_execution_denial("shell_exec", {"command": "codex exec fixture"}, principal())
    computer = remote_execution_denial("computer_apps", {}, principal())
    assert (codex[0] if codex else None) == ("CODEX_REMOTE_DISABLED" if model else None)
    assert (computer[0] if computer else None) == (COMPUTER_DENIAL_CODE if native else None)
    assert remote_execution_denial("shell_exec", {"command": "printf codex"}, principal()) is None
    assert remote_execution_denial("computer_status", {"probe": False}, principal()) is None
    assert remote_execution_denial("computer_session_close", {}, principal()) is None


@pytest.mark.parametrize("name,args,blocked", [
    ("computer_status", {"probe": False}, False), ("computer_session_close", {}, False),
    ("computer_status", {"probe": True}, True), ("computer_apps", {}, True),
    ("computer_session_open", {}, True), ("computer_observe", {}, True), ("computer_action", {}, True),
])
def test_native_gate_keeps_static_status_and_stop_available(name, args, blocked):
    assert computer_denial(name, args) is blocked


@pytest.mark.parametrize("app", ["Codex", "com.openai.codex", "/Applications/Codex.app",
                                "/Applications/Codex.app/Contents/MacOS/Codex", r"C:\\Tools\\codex.exe"])
def test_known_codex_ui_cannot_inherit_native_exemption(monkeypatch, app):
    monkeypatch.setenv("MCP_BLOCK_LOCAL_CODEX", "1")
    monkeypatch.setenv("MCP_BLOCK_NATIVE_COMPUTER", "0")
    assert codex_computer_target("computer_session_open", {"app": app})
    assert remote_execution_denial("computer_session_open", {"app": app}, principal())[0] == "CODEX_REMOTE_DISABLED"
    assert codex_computer_target("computer_action", {"session_id": "fixture"},
                                {"id": "fixture", "app": app})
    assert not codex_computer_target("computer_session_close", {"session_id": "fixture"},
                                    {"id": "fixture", "app": app})
    assert not codex_computer_target("computer_action", {"session_id": "other"},
                                    {"id": "fixture", "app": app})


def test_isolated_provider_is_not_a_generic_cli_exemption(tmp_path):
    config = {"mcp_policy": {"block_local_codex": True, "block_native_computer": False}}
    project = {"_execution_policy": metadata(True, False)}
    assert not agent_blocks_computer(config, project)
    with pytest.raises(DevError) as caught:
        enforce_argv(config, project, ["codex", "app-server"], tmp_path, {})
    assert caught.value.code == "CODEX_REMOTE_DISABLED"


@pytest.mark.asyncio
@pytest.mark.parametrize("local_native,hub_native", product([False, True], repeat=2))
async def test_agent_native_probe_uses_independent_gate_before_provider(shell_agent, monkeypatch, local_native, hub_native):
    agent, root = shell_agent
    agent.config["mcp_policy"] = {"block_local_codex": True, "block_native_computer": local_native}
    req = shell_request(root)
    req.update(tool="computer_status", execution_policy=metadata(True, hub_native),
               integration_context={"owner":"grant:fixture","admin":False,"device_id":"fixture","scopes":["read","computer"]})
    req["args"] = {"project": "fixture", "probe": True}
    provider = AsyncMock(return_value={"mock_only": True})
    monkeypatch.setattr(agent.computer, "execute", provider)
    await agent.handle(req)
    result = agent.journal.status(req["id"])["result"]
    if local_native or hub_native:
        assert result["error"]["code"] == COMPUTER_DENIAL_CODE
        provider.assert_not_called()
    else:
        assert result["ok"] and result["data"] == {"mock_only": True}
        provider.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["computer_session_open", "computer_observe", "computer_action"])
async def test_agent_rechecks_model_floor_for_codex_app_session(shell_agent, monkeypatch, tool):
    agent, root = shell_agent
    agent.config["mcp_policy"] = {"block_local_codex": True, "block_native_computer": False}
    req = shell_request(root)
    req.update(tool=tool, execution_policy=metadata(True, False))
    sid = "a" * 32
    req["args"] = {"project": "fixture", "idempotency_key": "split-codex-ui-fixture"}
    if tool == "computer_session_open":
        req["args"]["app"] = "com.openai.codex"
    else:
        agent.computer.session = {"id": sid, "app": "Codex"}
        req["args"]["session_id"] = sid
        if tool == "computer_action":
            req["args"].update(observation_id="b" * 32, action={"type": "press_key", "key": "Return"})
    provider = AsyncMock(side_effect=AssertionError("No native invocation"))
    monkeypatch.setattr(agent.computer, "execute", provider)
    try:
        await agent.handle(req)
        assert agent.journal.status(req["id"])["result"]["error"]["code"] == "CODEX_REMOTE_DISABLED"
        provider.assert_not_called()
    finally:
        agent.computer.session = None


@pytest.mark.asyncio
@pytest.mark.parametrize("scopes", [None, ["read"], ["read", "computer"], "computer"])
@pytest.mark.parametrize("probe", [False, True])
async def test_agent_probe_requires_authenticated_computer_scope(shell_agent, monkeypatch, scopes, probe):
    agent, root = shell_agent
    req = shell_request(root)
    req.update(tool="computer_status", execution_policy=metadata(True, False))
    req["args"] = {"project":"fixture", "probe":probe}
    req["project"]["_computer_scopes"] = ["computer"]  # cannot inject a grant in project metadata
    if scopes is not None:
        req["integration_context"] = {"owner":"grant:fixture","admin":False,"device_id":"fixture","scopes":scopes}
    provider = AsyncMock(return_value={"mock_only":True})
    monkeypatch.setattr(agent.computer, "execute", provider)
    await agent.handle(req)
    result = agent.journal.status(req["id"])["result"]
    if probe and (not isinstance(scopes, list) or "computer" not in scopes):
        assert result["error"]["code"] == "INSUFFICIENT_SCOPE"
        provider.assert_not_called()
    else:
        assert result["ok"]
        provider.assert_awaited_once()


def test_settings_catalog_describes_split_without_exposing_an_editor():
    from shared.settings_catalog import settings_catalog
    policy = next(item for item in settings_catalog()["items"] if item["id"] == "execution_policy")
    assert set(policy["fields"]) == {
        "MCP_BLOCK_LOCAL_CODEX", "MCP_BLOCK_NATIVE_COMPUTER",
        "mcp_policy.block_local_codex", "mcp_policy.block_native_computer",
    }
    assert policy["source_kind"] == "environment_agent"
    assert policy["editor"] is None and policy["risk"] == "security"
    assert "原生策略默认允许" in policy["description"] and "不会授予桌面权限" in policy["description"]
