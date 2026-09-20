"""Agent lifecycle update/restart/uninstall safety and rollback tests.

All service operations are fault-injected in temporary directories; these tests
never stop or install a real operating-system service.
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import zipfile

import pytest

from agent.lifecycle import LifecycleManager
from hub.agent_install import AgentPackage
from shared.agent_lifecycle import DEVICE_ACTIONS, public_management
from shared.contracts import TOOLS
from shared.util import DevError, atomic_json


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agent_lifecycle_helper", ROOT / "scripts/agent_lifecycle.py")
assert SPEC and SPEC.loader
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


def write_runtime(path: Path, version: str) -> None:
    (path / "agent").mkdir(parents=True)
    (path / "shared").mkdir()
    (path / "scripts").mkdir()
    (path / "agent/__main__.py").write_text("# agent\n")
    (path / "shared/util.py").write_text(f'VERSION = "{version}"\n')
    (path / "scripts/agent_lifecycle.py").write_text("# helper\n")


def managed_manager(tmp_path: Path, monkeypatch) -> tuple[LifecycleManager, Path]:
    base = tmp_path / "managed-agent"
    runtime = base / "runtime"
    write_runtime(runtime, "old")
    helper_python = base / "python/cpython-test/bin/python3"
    helper_python.parent.mkdir(parents=True)
    helper_python.write_text("python")
    uv = base / "tools/uv"
    uv.parent.mkdir()
    uv.write_text("uv")
    service = tmp_path / "codepier-agent.service"
    service.write_text("[Service]\n")
    atomic_json(base / "management.json", {
        "schema": 1,
        "managed": True,
        "service": True,
        "service_kind": "systemd",
        "service_scope": "user",
        "installed_version": "old",
        "helper_python": str(helper_python),
        "uv": str(uv),
        "status": "ready",
    })
    manager = LifecycleManager(base / "config.json", base / "state", lambda: {"hub_url": "http://127.0.0.1:9"})
    manager.runtime_root = runtime
    manager.base = base
    monkeypatch.setattr(manager, "_service_target", lambda: service)
    monkeypatch.setattr(manager, "_service_kind", lambda: ("systemd", "user"))
    monkeypatch.setattr(manager, "_handoff_ready", lambda: True)
    return manager, base


def test_lifecycle_actions_are_private_and_management_is_path_free():
    assert DEVICE_ACTIONS.isdisjoint(TOOLS)
    public = public_management({
        "managed": True,
        "service": True,
        "service_kind": "systemd",
        "installed_version": "1.7.0",
        "layout": "managed-runtime",
        "status": "ready",
        "update_ready": True,
        "control_ready": True,
        "reason": "",
        "last_error": "",
        "helper_python": "/private/python",
        "uv": "/private/uv",
        "base": "/private/base",
    })
    encoded = json.dumps(public)
    assert public["managed"] and public["update_ready"] and public["control_ready"]
    assert "/private" not in encoded and "helper_python" not in public and "uv" not in public


def test_unmanaged_source_checkout_cannot_claim_one_click_actions(tmp_path):
    manager = LifecycleManager(tmp_path / "config.json", tmp_path / "state", lambda: {})
    status = manager.describe()
    assert status["managed"] is False and status["service"] is False
    assert manager.actions() == []
    assert "不是由面板一键安装器管理" in status["reason"]


def test_managed_service_declares_update_restart_and_uninstall(tmp_path, monkeypatch):
    manager, _ = managed_manager(tmp_path, monkeypatch)
    status = manager.describe()
    assert status["managed"] and status["service"]
    assert status["update_ready"] and status["control_ready"]
    assert manager.actions() == ["agent_restart", "agent_uninstall", "agent_update"]
    assert str(tmp_path) not in json.dumps(status)


@pytest.mark.parametrize('configured', ['missing', 'config-script', 'config-symlink'])
def test_python_discovery_skips_python_config_scripts(tmp_path, monkeypatch, configured):
    manager, base = managed_manager(tmp_path, monkeypatch)
    (base / 'python/cpython-test/bin/python3').unlink()
    directory = base / 'python/cpython-3.13.12-macos-aarch64-none/bin'
    directory.mkdir(parents=True)
    interpreter = directory / 'python3.13'
    interpreter.write_text('interpreter')
    config_script = directory / 'python3.13-config'
    config_script.write_text('configuration helper, not an interpreter')
    alias = directory / 'python3.14'
    alias.symlink_to(config_script)
    metadata = json.loads((base / 'management.json').read_text())
    metadata['helper_python'] = str({'missing': base / 'missing',
                                   'config-script': config_script,
                                   'config-symlink': alias}[configured])
    atomic_json(base / 'management.json', metadata)
    assert manager._external_python() == interpreter


def test_update_archive_accepts_only_pinned_public_agent_package(tmp_path):
    bundle = AgentPackage(ROOT).build()
    archive = tmp_path / "agent.zip"
    archive.write_bytes(bundle.content)
    destination = tmp_path / "runtime"
    destination.mkdir()
    LifecycleManager._extract(archive, bundle.sha256, destination)
    assert (destination / "agent/lifecycle.py").is_file()
    assert (destination / "shared/agent_lifecycle.py").is_file()
    assert (destination / "scripts/agent_lifecycle.py").is_file()

    malicious = tmp_path / "malicious.zip"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        package.writestr("agent/__main__.py", "")
        package.writestr("shared/util.py", 'VERSION = "x"')
        package.writestr("scripts/agent_lifecycle.py", "")
        package.writestr("requirements-agent.txt", "")
        package.writestr("unexpected/secret.txt", "no")
    malicious.write_bytes(output.getvalue())
    with pytest.raises(DevError) as failure:
        LifecycleManager._extract(malicious, hashlib.sha256(malicious.read_bytes()).hexdigest(), tmp_path / "bad")
    assert failure.value.code == "AGENT_PACKAGE_INVALID"


def test_atomic_update_preserves_old_runtime_and_records_ready(tmp_path, monkeypatch):
    base = tmp_path / "agent"
    runtime = base / "runtime"
    candidate = base / ".runtime-update-operation"
    write_runtime(runtime, "old")
    write_runtime(candidate, "new")
    atomic_json(base / "management.json", {"service_kind": "systemd", "service_scope": "user",
                                                   "installed_version": "old", "status": "ready"})
    calls = []
    monkeypatch.setattr(helper, "stop_service", lambda *_a, **_k: calls.append("stop"))
    monkeypatch.setattr(helper, "_wait_for_exit", lambda *_a, **_k: calls.append("wait"))
    monkeypatch.setattr(helper, "start_service", lambda *_a, **_k: calls.append("start"))
    monkeypatch.setattr(helper, "verify_service", lambda *_a, **_k: calls.append("verify"))

    helper.apply_update(base, candidate, 123)

    assert helper._version(runtime) == "new"
    assert helper._version(base / ".runtime-previous") == "old"
    metadata = json.loads((base / "management.json").read_text())
    assert metadata["installed_version"] == "new" and metadata["previous_version"] == "old"
    assert metadata["status"] == "ready" and not metadata["last_error"]
    assert calls == ["stop", "wait", "start", "verify"]


def test_failed_new_service_rolls_back_previous_runtime(tmp_path, monkeypatch):
    base = tmp_path / "agent"
    runtime = base / "runtime"
    candidate = base / ".runtime-update-operation"
    write_runtime(runtime, "old")
    write_runtime(candidate, "broken")
    atomic_json(base / "management.json", {"service_kind": "systemd", "service_scope": "user",
                                                   "installed_version": "old", "status": "ready"})
    starts = []
    monkeypatch.setattr(helper, "stop_service", lambda *_a, **_k: None)
    monkeypatch.setattr(helper, "_wait_for_exit", lambda *_a, **_k: None)

    def start(*_args, **_kwargs):
        starts.append(True)
        if len(starts) == 1:
            raise RuntimeError("injected start failure")

    monkeypatch.setattr(helper, "start_service", start)
    monkeypatch.setattr(helper, "verify_service", lambda *_a, **_k: None)

    with pytest.raises(RuntimeError, match="injected start failure"):
        helper.apply_update(base, candidate, 123)

    assert helper._version(runtime) == "old"
    assert helper._version(base / ".runtime-failed") == "broken"
    metadata = json.loads((base / "management.json").read_text())
    assert metadata["installed_version"] == "old" and metadata["status"] == "rollback"
    assert "恢复上一版本" in metadata["last_error"] and "injected start failure" in metadata["last_error"]
    assert len(starts) == 2

    # The top-level helper error handler must not overwrite a verified rollback
    # with a generic status after apply_update re-raises the original failure.
    helper.record_failure(base, RuntimeError("injected start failure"))
    preserved = json.loads((base / "management.json").read_text())
    assert preserved["status"] == "rollback"
    assert "恢复上一版本" in preserved["last_error"]


@pytest.mark.asyncio
async def test_handoff_starts_only_after_ack_and_failed_handoff_is_retryable(tmp_path, monkeypatch):
    manager, base = managed_manager(tmp_path, monkeypatch)
    helper_file = tmp_path / "handoff.py"
    helper_file.write_text("# helper")
    python = tmp_path / "python"
    python.write_text("python")
    manager._write_plan("operation", {
        "action": "agent_restart",
        "helper": str(helper_file),
        "python": str(python),
        "base": str(base),
        "created_at": 1,
    })
    captured = []
    monkeypatch.setattr(manager, "_launch_helper", lambda operation, command: captured.append((operation, command)))

    assert await manager.acknowledge("operation") is True
    assert captured and captured[0][0] == "operation" and "--restart" in captured[0][1]
    assert not manager._plan_path("operation").exists()
    assert not manager._plan_path("operation").with_suffix(".claimed").exists()

    manager._write_plan("retry", {
        "action": "agent_restart",
        "helper": str(helper_file),
        "python": str(python),
        "base": str(base),
        "created_at": 1,
    })

    def fail(*_args):
        raise OSError("handoff unavailable")

    monkeypatch.setattr(manager, "_launch_helper", fail)
    with pytest.raises(OSError, match="handoff unavailable"):
        await manager.acknowledge("retry")
    assert manager._plan_path("retry").is_file()


def test_launchd_handoff_is_one_shot(tmp_path, monkeypatch):
    import plistlib
    manager, base = managed_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(manager, "_service_kind", lambda: ("launchd", "user"))
    monkeypatch.setattr("agent.lifecycle.shutil.which", lambda _: "/bin/launchctl")
    captured = []
    def run(command, **kwargs):
        assert command[1] == "bootstrap"
        captured.append(plistlib.loads(Path(command[-1]).read_bytes()))
    monkeypatch.setattr("agent.lifecycle.subprocess.run", run)
    manager._launch_helper("oneshot", ["/python", "/helper", "--restart"])
    assert captured[0]["KeepAlive"] is False
    assert captured[0]["RunAtLoad"] is True
    assert captured[0]["ProgramArguments"][:3] == ["/python", "/helper", "--restart"]
    assert not (manager.plan_dir / "oneshot.plist").exists()
