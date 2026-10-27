"""Synthetic archive-selection contract: never invoke sudo, apt or the network."""
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ci_ubuntu_archive", ROOT / ".github/scripts/ci_ubuntu_archive.py")
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)
OLD = "http://azure.archive.ubuntu.com/ubuntu"
NEW = "https://archive.ubuntu.com/ubuntu"
SOURCE = f"""Types: deb deb-src
URIs: {OLD}
Suites: noble noble-updates noble-backports
Components: main restricted universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
"""


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_deb822_only_uri_changes_and_is_idempotent(newline):
    before = (SOURCE + "\n# untouched\nTypes: deb\nURIs: http://security.ubuntu.com/ubuntu\n"
              "Suites: noble-security\nComponents: main\nSigned-By: /keys/security.gpg\n"
              "\nTypes: deb\nURIs: https://third.example/ubuntu\nSuites: stable\n"
              "Components: main\nSigned-By: /keys/third.gpg\n").replace("\n", newline)
    expected = before.replace(OLD, NEW)
    assert archive.transform(before, True) == expected
    assert archive.transform(expected, True) == expected


@pytest.mark.parametrize("prefix", ["deb ", "deb-src ", "deb [arch=amd64 signed-by=/keys/ubuntu.gpg] "])
def test_legacy_preserves_options_comments_security(prefix):
    line = f"{prefix}{OLD}/ noble main restricted # comment\r\n"
    before = line + f"deb {OLD} noble-security main\n# deb {OLD} noble main\n"
    assert archive.transform(before, False) == before.replace(OLD + "/", NEW + "/", 1)


def test_multiline_uris_inline_key_and_disabled_security_stanzas():
    source = SOURCE.replace(f"URIs: {OLD}", f"URIs: https://third.example/ubuntu\n {OLD}/")
    source = source.replace("Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg",
                            "Signed-By:\n -----BEGIN PGP PUBLIC KEY BLOCK-----\n .\n abc\n -----END PGP PUBLIC KEY BLOCK-----")
    assert archive.transform(source, True) == source.replace(OLD, NEW)
    for unchanged in [SOURCE + "Enabled: no\n", SOURCE.replace("noble-updates", "noble-security")]:
        assert archive.transform(unchanged, True) == unchanged


@pytest.mark.parametrize("address", [
    OLD + "/extra", OLD + "?query=yes", OLD + "//", "http://azure.archive.ubuntu.com.evil/ubuntu",
    "http://user@azure.archive.ubuntu.com/ubuntu", "ftp://azure.archive.ubuntu.com/ubuntu",
])
@pytest.mark.parametrize("deb822", [True, False])
def test_unexpected_uri_fails_closed(address, deb822):
    source = SOURCE.replace(OLD, address) if deb822 else f"deb {address} noble main\n"
    with pytest.raises(ValueError):
        archive.transform(source, deb822)


@pytest.mark.parametrize("source", [
    SOURCE + "URIs: https://other.example\n",
    SOURCE.replace("Suites: noble noble-updates noble-backports\n", ""),
    SOURCE.replace("Types: deb deb-src", "bad"),
    " orphan\n" + SOURCE,
])
def test_malformed_deb822_fails_closed(source):
    with pytest.raises(ValueError):
        archive.transform(source, True)


def test_plan_validation_precedes_writes_and_mode_preserved(tmp_path):
    first = tmp_path / "sources.list"
    first.write_text(f"deb {OLD} noble main\n")
    first.chmod(0o640)
    folder = tmp_path / "sources.list.d"
    folder.mkdir()
    bad = folder / "ubuntu.sources"
    bad.write_text(SOURCE + "URIs: bad\n")
    original = first.read_bytes()
    with pytest.raises(ValueError):
        archive.configure(tmp_path)
    assert first.read_bytes() == original
    bad.write_text(SOURCE)
    assert archive.configure(tmp_path) == 2
    assert stat.S_IMODE(first.stat().st_mode) == 0o640
    assert first.read_text() == original.decode().replace(OLD, NEW)
    assert archive.configure(tmp_path) == 0
    assert not list(tmp_path.glob(".sources.list.*"))


def test_symlink_refused_without_writing(tmp_path):
    actual = tmp_path / "actual"
    actual.write_text(SOURCE)
    folder = tmp_path / "sources.list.d"
    folder.mkdir()
    (folder / "ubuntu.sources").symlink_to(actual)
    with pytest.raises(ValueError):
        archive.configure(tmp_path)
    assert actual.read_text() == SOURCE


def test_cli_rejects_non_ci_without_accessing_system():
    env = dict(os.environ, GITHUB_ACTIONS="false", RUNNER_OS="Linux")
    result = subprocess.run([sys.executable, str(SPEC.origin)], env=env,
                            text=True, capture_output=True, timeout=5)
    assert result.returncode != 0
    assert "Only disposable GitHub Linux CI" in result.stderr


def test_compact_deb822_uri_and_empty_legacy_rejection():
    source = SOURCE.replace("URIs: ", "URIs:")
    assert archive.transform(source, True) == source.replace(OLD, NEW)
    with pytest.raises(ValueError):
        archive.transform(f"deb {OLD} noble # no components\n", False)


@pytest.mark.parametrize("failure", ["write", "replace", "readback", "readback-error"])
def test_write_replace_readback_fail_closed_and_clean_temporary(tmp_path, monkeypatch, failure):
    source = tmp_path / "sources.list"
    original = f"deb {OLD} noble main\n"
    source.write_text(original)
    if failure == "write":
        real_fdopen = archive.os.fdopen

        class BrokenWriter:
            def __init__(self, fd, mode):
                self.file = real_fdopen(fd, mode)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.file.close()

            def write(self, value):
                raise OSError("synthetic write failure")

        monkeypatch.setattr(archive.os, "fdopen", BrokenWriter)
    elif failure == "replace":
        def reject_replace(*args):
            raise OSError("synthetic replace failure")
        monkeypatch.setattr(archive.os, "replace", reject_replace)
    else:
        original_read = Path.read_bytes
        reads = 0

        def broken_read(path):
            nonlocal reads
            if path == source:
                reads += 1
                if reads == 3:
                    if failure == "readback-error":
                        raise OSError("synthetic readback failure")
                    return b"synthetic unexpected readback"
            return original_read(path)
        monkeypatch.setattr(Path, "read_bytes", broken_read)
    with pytest.raises((OSError, ValueError)):
        archive.configure(tmp_path)
    assert not list(tmp_path.glob(".sources.list.*"))
    if failure in {"write", "replace"}:
        assert source.read_text() == original
    else:
        # Replacement happened, but failed verification must never report success.
        assert source.read_text() == original.replace(OLD, NEW)


def test_no_azure_noop_never_writes(tmp_path, monkeypatch):
    source = tmp_path / "sources.list"
    before = "deb https://archive.ubuntu.com/ubuntu noble main\n"
    source.write_text(before)

    def refuse_write(*args, **kwargs):
        raise AssertionError("no-op must not create a temporary file")

    monkeypatch.setattr(archive.tempfile, "mkstemp", refuse_write)
    assert archive.configure(tmp_path) == 0
    assert source.read_text() == before


def test_source_directory_symlink_refused(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    source = actual / "ubuntu.sources"
    source.write_text(SOURCE)
    (tmp_path / "sources.list.d").symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink source directory"):
        archive.configure(tmp_path)
    assert source.read_text() == SOURCE
