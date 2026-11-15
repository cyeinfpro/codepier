"""Peripheral UX regressions; no native services or accounts are touched."""
import copy
import json
from agent.__main__ import config_summary
from scripts import install_agent as installer


def test_config_summary_recursively_projects_and_redacts():
    source = {"secret": "root-private", "shell": {"env": {"ORDINARY": "shell-private"}},
              "project_runtimes": {"demo": {"env": {"COLOR": "runtime-private"}}},
              "language_servers": {"python": {"env": {"VALUE": "lsp-private"}, "command": ["python", "secret-argument"]}},
              "tasks": {"check": {"env": {"NOT_SECRET_NAMED": "task-private"}, "description": "Check"}},
              "future_config": {"leak": "unknown-private"}}
    before = copy.deepcopy(source)
    result = config_summary(source)
    serialized = json.dumps(result)
    assert "private" not in serialized and "secret-argument" not in serialized
    assert result["tasks"]["check"]["description"] == "Check"
    assert result["project_runtimes"]["demo"]["env"] == {"COLOR": "<redacted>"}
    assert result["language_servers"]["python"]["env"] == {"VALUE": "<redacted>"}
    assert source == before


def test_status_does_not_claim_service_or_hub_readiness(tmp_path, monkeypatch, capsys):
    base = tmp_path / "isolated-agent"
    base.mkdir()
    (base / "config.json").write_text("{}")
    (base / "management.json").write_text(json.dumps({"status": "service_error", "updated_at": 123}))
    monkeypatch.setattr(installer, "read_installed_config", lambda base: {"device_id": "fixture"})
    monkeypatch.setattr(installer, "runtime_version", lambda path: "fixture")
    monkeypatch.setattr(installer.sys, "argv", ["install_agent.py", "--install-dir", str(base), "--status"])
    installer.main()
    value = json.loads(capsys.readouterr().out)
    assert value["config_present"] is True
    assert value["installation_stage"] == "service_error"
    assert value["stage_observed_at"] == 123
    assert value["service_state"] == "not_verified" and value["hub_state"] == "unknown"


def test_config_summary_malformed_secret_containers_fail_closed():
    for container in ("tasks", "project_runtimes", "language_servers"):
        for invalid in ("private-value", ["private-value"], 432198, None):
            assert config_summary({container: invalid})[container] == "<redacted>"
            result = config_summary({container: {"fixture": invalid}})
            assert result[container]["fixture"] == "<redacted>"
    for invalid in ("private-env", ["private-env"], 432198, None):
        result = config_summary({"language_servers": {"fixture": {"env": invalid}}})
        assert result["language_servers"]["fixture"]["env"] == "<redacted>"


def test_source_installer_noninteractive_permission_copy(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / 'deploy/install-agent.sh'
    fake_python = tmp_path / 'fixture-python'
    fake_python.write_text('#!' + sys.executable + '\nimport sys\nif sys.argv[1:2] == ["-c"]:\n if "_base_executable" in sys.argv[2]: print(__file__)\nelse: print("FIXTURE_INSTALL " + repr(sys.argv[1:]))\n')
    fake_python.chmod(0o700)
    env = {**os.environ, 'HOME': str(tmp_path / 'home'), 'PYTHON': str(fake_python)}
    for mode in ('full', 'disabled'):
        result = subprocess.run(['bash', str(script), 'fixture.json', str(tmp_path), mode], env=env, capture_output=True, text=True, check=True)
        assert '权限范围' in result.stdout
        assert result.stdout.index('权限范围') < result.stdout.index('FIXTURE_INSTALL')
        assert ('不是目录沙箱' if mode == 'full' else '关闭 Shell 和目录任务') in result.stdout
        assert mode in result.stdout
    config = tmp_path / 'home/.codepier-agent/config.json'
    config.parent.mkdir(parents=True)
    config.write_text('{}')
    result = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, check=True)
    assert '保留原权限' in result.stdout
    assert 'FIXTURE_INSTALL' in result.stdout
    rejected = subprocess.run(['bash', str(script), '', '', 'unexpected'], env=env, capture_output=True, text=True)
    assert rejected.returncode != 0 and 'FIXTURE_INSTALL' not in rejected.stdout


def test_powershell_permission_copy_precedes_invocation_and_keeps_default():
    from pathlib import Path
    script = (Path(__file__).resolve().parents[1] / 'deploy/install-agent.ps1').read_text()
    assert "[string]$Shell = 'full'" in script
    assert script.index('不是目录沙箱') < script.index('& $CodePierPython @CodePierArgs')
    assert '-Shell disabled' in script and '保留原权限' in script
    assert '后台服务配置与 Hub 在线是不同状态' in script
    assert 'Pairing JSON full path' not in script
