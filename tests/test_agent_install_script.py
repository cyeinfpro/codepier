from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("install_agent_helper", ROOT / "scripts/install_agent.py")
assert SPEC and SPEC.loader
install_agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(install_agent)


REQUIRED = {
    "agent/__main__.py": b"print('agent')\n",
    "agent/lifecycle.py": b"# lifecycle\n",
    "shared/util.py": b"VERSION = 'test'\n",
    "shared/agent_lifecycle.py": b"# contract\n",
    "scripts/install_agent.py": b"# installer\n",
    "scripts/agent_lifecycle.py": b"# helper\n",
    "requirements-agent.txt": b"\n",
}


def archive_bytes(files=None, *, compression=zipfile.ZIP_DEFLATED):
    files = dict(REQUIRED if files is None else files)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=compression) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return output.getvalue()


def write_archive(tmp_path, files=None, *, compression=zipfile.ZIP_DEFLATED):
    raw = archive_bytes(files, compression=compression)
    path = tmp_path / "agent.zip"
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


def test_unpack_validates_hash_and_extracts_required_files(tmp_path):
    archive, digest = write_archive(tmp_path)
    destination = tmp_path / "runtime"

    install_agent.unpack(archive, digest, destination)

    assert (destination / "agent/__main__.py").read_bytes() == REQUIRED["agent/__main__.py"]
    with pytest.raises(ValueError, match="checksum"):
        install_agent.unpack(archive, "0" * 64, tmp_path / "bad")


@pytest.mark.parametrize(
    "name",
    ["../outside.py", "/absolute.py", "agent\\escape.py", "agent/bad:name.py"],
)
def test_unpack_rejects_traversal_and_unsafe_names(tmp_path, name):
    files = dict(REQUIRED)
    files[name] = b"escape"
    archive, digest = write_archive(tmp_path, files)
    destination = tmp_path / "runtime"

    with pytest.raises(ValueError, match="Unsafe|Unexpected"):
        install_agent.unpack(archive, digest, destination)
    assert not (tmp_path / "outside.py").exists()


def test_unpack_rejects_symlink_member(tmp_path):
    archive_path = tmp_path / "symlink.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        for name, data in REQUIRED.items():
            archive.writestr(name, data)
        info = zipfile.ZipInfo("agent/link.py")
        info.external_attr = (0o120777 << 16) | 0xA000
        archive.writestr(info, b"target")

    with pytest.raises(ValueError, match="Unsafe"):
        install_agent.unpack(archive_path, hashlib.sha256(archive_path.read_bytes()).hexdigest(), tmp_path / "runtime")


def test_unpack_rejects_uncompressed_size_over_limit(tmp_path):
    # Highly compressible content keeps the package under the 8 MiB transport cap,
    # while exercising the separate 32 MiB extracted-size guard.
    files = dict(REQUIRED)
    files["agent/large.py"] = b"x" * (32 * 1024 * 1024 + 1)
    archive, digest = write_archive(tmp_path, files)

    assert archive.stat().st_size < 8 * 1024 * 1024
    with pytest.raises(ValueError, match="Unsafe"):
        install_agent.unpack(archive, digest, tmp_path / "runtime")


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, limit=-1):
        return self.payload


class _Opener:
    def __init__(self, response):
        self.response = response
        self.request = None

    def open(self, request, timeout):
        self.request = request
        if isinstance(self.response, BaseException):
            raise self.response
        return _Response(self.response)


def test_enroll_posts_bearer_and_returns_only_pairing_fields(monkeypatch):
    opener = _Opener(json.dumps({
        "device_id": "dev-1", "name": "node", "secret": "secret",
        "hub_url": "https://hub.example", "extra": "discard",
    }).encode())
    monkeypatch.setattr(install_agent.urllib.request, "build_opener", lambda *_: opener)

    pairing = install_agent.enroll("https://hub.example/", "rdi_token")

    assert pairing == {"device_id": "dev-1", "name": "node", "secret": "secret", "hub_url": "https://hub.example"}
    assert opener.request.full_url == "https://hub.example/agent/enroll"
    assert opener.request.get_header("Authorization") == "Bearer rdi_token"


@pytest.mark.parametrize("response", [b"{}", b"x" * 16385])
def test_enroll_rejects_malformed_or_oversized_response(monkeypatch, response):
    opener = _Opener(response)
    monkeypatch.setattr(install_agent.urllib.request, "build_opener", lambda *_: opener)

    with pytest.raises(ValueError, match="Invalid pairing response"):
        install_agent.enroll("https://hub.example", "rdi_token")


def test_enroll_maps_transport_and_http_failures_to_retryable_errors(monkeypatch):
    import urllib.error

    for failure, message in [
        (urllib.error.HTTPError("https://hub.example/agent/enroll", 410, "expired", {}, io.BytesIO()), "rejected or expired"),
        (urllib.error.URLError("offline"), "response unavailable"),
    ]:
        opener = _Opener(failure)
        monkeypatch.setattr(install_agent.urllib.request, "build_opener", lambda *_: opener)
        with pytest.raises(ValueError, match=message):
            install_agent.enroll("https://hub.example", "rdi_token")


def _invoke_main(monkeypatch, *, archive, digest, base, run, enroll, no_service=True):
    monkeypatch.setattr(install_agent, "run", run)
    monkeypatch.setattr(install_agent, "enroll", enroll)
    monkeypatch.setenv("CODEPIER_INSTALL_TOKEN", "rdi_test-token")
    args = [
        "install_agent.py", "--archive", str(archive), "--sha256", digest,
        "--hub", "https://hub.example", "--allow", str(base), "--uv", "uv",
        "--install-dir", str(base),
    ]
    if no_service:
        args.append("--no-service")
    monkeypatch.setattr(sys, "argv", args)
    install_agent.main()


def test_main_removes_partial_runtime_after_dependency_failure(tmp_path, monkeypatch):
    archive, digest = write_archive(tmp_path)
    base = tmp_path / "install"
    base.mkdir()

    def fail_dependency(args, **kwargs):
        if "pip" in [str(item) for item in args]:
            raise subprocess.CalledProcessError(1, args)

    with pytest.raises(subprocess.CalledProcessError):
        _invoke_main(monkeypatch, archive=archive, digest=digest, base=base, run=fail_dependency,
                     enroll=lambda *_: pytest.fail("enroll must not run"))
    assert not (base / "runtime").exists()
    assert not (base / ".install.lock").exists()
    assert not (base / "config.json").exists()


def test_main_removes_partial_runtime_after_enrollment_failure(tmp_path, monkeypatch):
    archive, digest = write_archive(tmp_path)
    base = tmp_path / "install"
    base.mkdir()

    def fail_enroll(*_):
        raise ValueError("Pairing ticket rejected")

    with pytest.raises(ValueError, match="Pairing ticket"):
        _invoke_main(monkeypatch, archive=archive, digest=digest, base=base, run=lambda *_a, **_k: None,
                     enroll=fail_enroll)
    assert not (base / "runtime").exists()
    assert not (base / ".install.lock").exists()


def test_main_preserves_incomplete_existing_install_without_running_commands(tmp_path, monkeypatch):
    archive, digest = write_archive(tmp_path)
    base = tmp_path / "install"
    base.mkdir()
    config = base / "config.json"
    config.write_text('{"device_id":"existing"}')

    calls = []
    with pytest.raises(ValueError, match="incomplete"):
        _invoke_main(monkeypatch, archive=archive, digest=digest, base=base,
                     run=lambda *a, **k: calls.append((a, k)), enroll=lambda *_: None)
    assert config.read_text() == '{"device_id":"existing"}'
    assert calls == []
    assert not (base / ".install.lock").exists()


def test_fresh_install_records_lifecycle_metadata(tmp_path, monkeypatch):
    archive, digest = write_archive(tmp_path)
    base = tmp_path / "install"
    base.mkdir()

    def fake_run(args, **kwargs):
        if "init" in [str(item) for item in args]:
            (base / "config.json").write_text(json.dumps({"device_id": "fresh"}))

    _invoke_main(
        monkeypatch, archive=archive, digest=digest, base=base, run=fake_run,
        enroll=lambda *_: {"device_id": "fresh", "name": "Fresh node", "secret": "s" * 48,
                           "hub_url": "https://hub.example"},
    )

    metadata = json.loads((base / "management.json").read_text())
    assert metadata["managed"] and metadata["layout"] == "managed-runtime"
    assert metadata["service"] is False and metadata["service_kind"] == "none"
    assert metadata["installed_version"] == "test" and metadata["status"] == "ready"
    assert Path(metadata["helper_python"]).resolve() == Path(sys.executable).resolve()
    assert not (base / ".install.lock").exists()


def test_existing_managed_install_uses_atomic_repair_path(tmp_path, monkeypatch):
    archive, digest = write_archive(tmp_path)
    base = tmp_path / "install"
    runtime = base / "runtime"
    runtime.mkdir(parents=True)
    for name, data in REQUIRED.items():
        if name == "requirements-agent.txt" or name.startswith(("agent/", "shared/", "scripts/")):
            target = runtime / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    config = base / "config.json"
    config.write_text(json.dumps({"device_id": "existing", "secret": "old", "hub_url": "https://old.example",
                                  "allowed_roots": [{"path": str(base), "writable": True}]}))
    service = tmp_path / "service.marker"
    service.write_text("managed")
    monkeypatch.setattr(install_agent, "service_definition", lambda _: service)
    # This test isolates repair orchestration; concrete ownership validation is
    # covered with real temporary service definitions in test_installer_management.
    monkeypatch.setattr(install_agent, "verify_service_ownership", lambda *_a, **_k: None)
    calls = []

    def fake_run(args, **kwargs):
        calls.append([str(item) for item in args])

    _invoke_main(
        monkeypatch, archive=archive, digest=digest, base=base, run=fake_run,
        enroll=lambda *_: {"device_id": "existing", "name": "Updated node", "secret": "new-secret",
                           "hub_url": "https://hub.example"},
    )

    updated = json.loads(config.read_text())
    assert updated["device_id"] == "existing" and updated["secret"] == "new-secret"
    assert updated["allowed_roots"][0]["path"] == str(base)
    assert any("scripts/agent_lifecycle.py" in " ".join(command) and "--apply-update" in command for command in calls)
    metadata = json.loads((base / "management.json").read_text())
    assert metadata["service"] and metadata["status"] == "updating"
    assert not list(base.glob(".runtime-update-manual-*"))
    assert not (base / ".install.lock").exists()


def test_systemd_service_quotes_user_selected_paths(tmp_path, monkeypatch):
    home = tmp_path / "home"
    base = tmp_path / 'agent $x%"dir'
    runtime = base / "runtime"
    runtime.mkdir(parents=True)
    base.joinpath("logs").mkdir()
    python = runtime / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    monkeypatch.setattr(install_agent.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(install_agent.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(install_agent.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(install_agent, "run", lambda args, **kwargs: calls.append([str(x) for x in args]))

    command = install_agent.start_service(base, python)

    service = home / ".config/systemd/user/codepier-agent.service"
    content = service.read_text()
    # WorkingDirectory is parsed as an absolute systemd path, so only the
    # percent specifier needs escaping there; ExecStart arguments remain
    # explicitly quoted by systemd_quote.
    assert 'WorkingDirectory=' + str(runtime).replace('%', '%%') in content
    assert all('"' in line for line in content.splitlines() if line.startswith("ExecStart="))
    assert command[0] == str(python)
    assert calls[-1][-1] == "codepier-agent.service"
