"""No-side-effect task preflight and real task launch regression coverage."""

from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import uuid

import pytest

from agent.config import validate_config
from agent.task_preflight import inspect_requirements, enforce_requirements, validate_requirements
from shared.util import DevError
from tests.test_shell import shell_agent, shell_request  # noqa: F401
from tests.test_project_runtime import clean


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"script": "echo x"},
        {"env": "A"},
        {"env": ["A=B"]},
        {"env": ["x"] * 33},
        {"executables": ["/bin/sh"]},
        {"executables": ["../sh"]},
        {"files": ["../a"]},
        {"files": ["/etc/passwd"]},
        {"files": [".env"]},
        {"files": ["a\\b"]},
        {"files": [""]},
        {"files": ["x" * 257]},
    ],
)
def test_invalid_requirements(value):
    with pytest.raises(ValueError):
        validate_requirements(value)


def test_absent_requirements_preserve_legacy_behavior(tmp_path):
    assert inspect_requirements({}, {}, tmp_path) == {"state": "not_configured", "checks": []}


def test_presence_only_and_no_environment_inheritance(tmp_path, monkeypatch):
    monkeypatch.setenv("PREFLIGHT_SYNTHETIC", "private-do-not-inherit")
    task = {"requirements": {"env": ["PREFLIGHT_SYNTHETIC"], "files": ["fixture.json"]}}
    before = dict(os.environ)
    blocked = inspect_requirements(task, {}, tmp_path)
    assert blocked["state"] == "blocked"
    (tmp_path / "fixture.json").write_text("private-fixture-content")
    ready = inspect_requirements(task, {"PREFLIGHT_SYNTHETIC": "private-value"}, tmp_path)
    assert ready["state"] == "ready"
    assert dict(os.environ) == before
    assert "private-" not in json.dumps(ready)


def test_file_links_cannot_probe_external_or_protected_files(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("secret")
    (root / "external.json").symlink_to(outside)
    (root / ".env").write_text("secret")
    (root / "alias.json").symlink_to(root / ".env")
    result = inspect_requirements(
        {"requirements": {"files": ["external.json", "alias.json"]}}, {}, root
    )
    assert result["state"] == "blocked"
    assert all(not row["available"] for row in result["checks"])


def test_executable_lookup_uses_only_prepared_path(tmp_path):
    name = Path(sys.executable).name
    task = {"requirements": {"executables": [name]}}
    assert (
        inspect_requirements(task, {"PATH": str(Path(sys.executable).parent)}, tmp_path)["state"]
        == "ready"
    )
    assert (
        inspect_requirements(task, {"PATH": str(tmp_path / "missing")}, tmp_path)["state"]
        == "blocked"
    )


def test_missing_root_and_directory_requirement_are_blocked(tmp_path):
    task = {"requirements": {"files": ["fixture.json"]}}
    assert inspect_requirements(task, {}, tmp_path / "absent")["state"] == "blocked"
    (tmp_path / "fixture.json").mkdir()
    assert inspect_requirements(task, {}, tmp_path)["state"] == "blocked"


def test_error_identifies_missing_names_without_values(tmp_path):
    task = {"requirements": {"env": ["NEXUS_STAGING"], "files": ["fixture.json"]}}
    with pytest.raises(DevError) as exc:
        enforce_requirements(task, {"OTHER": "secret-value"}, tmp_path)
    assert "env:NEXUS_STAGING" in exc.value.message
    assert "file:fixture.json" in exc.value.message
    assert "secret-value" not in exc.value.message


def test_container_cannot_be_reported_ready_from_host(tmp_path):
    task = {"requirements": {"env": ["A"]}}
    with pytest.raises(DevError) as exc:
        enforce_requirements(task, {"A": "present"}, tmp_path, mode="container_v1")
    assert exc.value.code == "TASK_PREFLIGHT_UNSUPPORTED"
    assert exc.value.details["workload_started"] is False


def test_config_validation_is_opt_in(shell_agent):
    agent, _ = shell_agent
    config = deepcopy(agent.config)
    config["tasks"]["check"] = {"command": ["python3"], "requirements": {"env": ["A", "A"]}}
    assert validate_config(config, agent.config_path)["tasks"]["check"]["requirements"]["env"] == [
        "A"
    ]
    config["tasks"]["check"]["requirements"] = {"probe": "anything"}
    with pytest.raises(ValueError):
        validate_config(config, agent.config_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["native_compat", "project_clean_v1"])
async def test_listing_and_launch_block_without_effects_then_recover(shell_agent, mode):
    agent, root = shell_agent
    if mode == "project_clean_v1":
        clean(agent)
    task = {
        "projects": ["fixture"],
        "command": [sys.executable, "-c", "from pathlib import Path; Path('ran').write_text('ok')"],
        "requirements": {"env": ["REQUIRED_FIXTURE"], "files": ["fixture.json"]},
    }
    agent.config["tasks"]["check"] = task
    project = shell_request(root)["project"]
    listed = await agent.execute("listing", "tasks_list", project, {})
    row = next(row for row in listed["tasks"] if row["name"] == "check")
    assert row["readiness"]["state"] == "blocked"
    request = {
        "id": uuid.uuid4().hex,
        "tool": "tasks_run",
        "project": project,
        "args": {"project": "fixture", "task": "check", "idempotency_key": uuid.uuid4().hex},
    }
    await agent.handle(request)
    result = agent.journal.status(request["id"])["result"]
    assert result["error"]["code"] == "TASK_PREREQUISITES_MISSING"
    assert not (root / "ran").exists()
    task["env"] = {"REQUIRED_FIXTURE": "not-in-report"}
    (root / "fixture.json").write_text("{}")
    request["id"] = uuid.uuid4().hex
    request["args"]["idempotency_key"] = uuid.uuid4().hex
    await agent.handle(request)
    assert agent.journal.status(request["id"])["result"]["data"]["exit_code"] == 0
    assert (root / "ran").read_text() == "ok"
