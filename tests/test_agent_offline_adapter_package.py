"""Source and Hub Agent packages must boot using only the extracted modules."""

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from hub.agent_install import AgentPackage
from scripts import install_agent

SOURCE = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("source_kind", ["offline-checkout", "hub"])
def test_agent_package_keeps_legacy_shape_and_imports_adapters(tmp_path, monkeypatch, source_kind):
    archive = tmp_path / "agent.zip"
    checked = tmp_path / "checked"
    real_unpack = install_agent.unpack
    if source_kind == "offline-checkout":

        class ArchiveChecked(Exception):
            pass

        def capture(path, digest, destination):
            shutil.copyfile(path, archive)
            real_unpack(path, digest, checked)
            raise ArchiveChecked

        pairing = tmp_path / "unused-pairing-file"
        pairing.touch()
        monkeypatch.setattr(install_agent, "service_preflight", lambda _base: None)
        monkeypatch.setattr(install_agent, "unpack", capture)
        with pytest.raises(ArchiveChecked):
            install_agent.install_source(tmp_path / "base", SOURCE, pairing, tmp_path)
        assert not (tmp_path / "base").exists()
    else:
        package = AgentPackage(SOURCE).build()
        archive.write_bytes(package.content)
        real_unpack(archive, package.sha256, checked)

    with zipfile.ZipFile(archive) as package:
        files = set(package.namelist())
    assert all(p.count("/") == 1 for p in files if p.startswith(("agent/", "shared/"))), (
        "a previously installed Agent rejects nested Python paths"
    )

    imported = subprocess.run(
        [
            sys.executable,
            "-IB",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from agent.native_adapters import NativeAdapterRegistry; "
            "from agent.native_cli import NativeCLI; "
            "from agent import chat_worker; "
            "assert NativeAdapterRegistry.supported_providers() == ('pi', 'codex', 'claude')",
            str(checked),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert imported.returncode == 0, imported.stderr
