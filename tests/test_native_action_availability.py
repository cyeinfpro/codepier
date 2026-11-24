"""Agent availability is an observation, not Provider registration."""

from pathlib import Path
from types import SimpleNamespace

from agent.native_adapters import ProviderInstanceRegistry
from agent.native_cli import NativeCLI
from shared.native_flags import NativeFeatureFlags


def _cli(tmp_path, monkeypatch, executable):
    cli = NativeCLI.__new__(NativeCLI)
    cli.agent = SimpleNamespace(config={})
    cli.environment = lambda: {"PATH": str(tmp_path / "bin")}
    cli._current_flags = lambda: NativeFeatureFlags()
    cli._windows_chat_validated = lambda: False
    monkeypatch.setattr(
        "agent.native_cli.structured_chat_platform_support",
        lambda *args, **kwargs: SimpleNamespace(available=True, reason_code=None),
    )
    monkeypatch.setattr("agent.native_cli.shutil.which", lambda name, path: executable)
    cli.instance_registry = ProviderInstanceRegistry(tmp_path / "native")
    return cli


def _start(cli, tmp_path):
    result = cli.query_action_availability({}, tmp_path, {"cli": "codex"})
    return result, next(row for row in result["actions"] if row["action"] == "start_session")


def test_missing_cli_does_not_mint_a_binding(tmp_path, monkeypatch):
    cli = _cli(tmp_path, monkeypatch, None)
    result, start = _start(cli, tmp_path)
    assert start["supported"] is True
    assert start["authorized"] is True
    assert start["ready"] is False
    assert start["reason_code"] == "CLI_MISSING"
    assert start["remedy"] == "select_node"
    assert result["provider_instance_id"] == "legacy_unresolved"
    assert not cli.instance_registry.path.exists()


def test_availability_reads_an_existing_binding_without_writing(tmp_path, monkeypatch):
    binary = str(Path("fixture") / "codex")
    cli = _cli(tmp_path, monkeypatch, binary)
    result, start = _start(cli, tmp_path)
    assert start["ready"] is True
    assert result["provider_instance_id"] == "legacy_unresolved"
    assert not cli.instance_registry.path.exists()

    real_id = cli._resolve_provider_instance_id({}, "codex", binary)
    original = cli.instance_registry.path.read_bytes()
    metadata = cli.instance_registry.path.stat()
    result, start = _start(cli, tmp_path)
    assert start["ready"] is True
    assert result["provider_instance_id"] == real_id
    assert cli.instance_registry.path.read_bytes() == original
    assert cli.instance_registry.path.stat().st_ino == metadata.st_ino
