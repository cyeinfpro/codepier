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


MIRROR_FIXTURE = ("http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
                  "https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
                  "https://security.ubuntu.com/ubuntu/\tpriority:3\n")


@pytest.mark.parametrize("deb822", [True, False])
def test_hosted_runner_144_byte_mirrorlist_preserves_source_and_metadata(tmp_path, deb822):
    assert len(MIRROR_FIXTURE.encode()) == 144
    if deb822:
        folder = tmp_path / "sources.list.d"
        folder.mkdir()
        source = folder / "ubuntu.sources"
        text = SOURCE.replace(OLD, archive.MIRROR_URI)
    else:
        source = tmp_path / "sources.list"
        text = f"deb [signed-by=/keys/ubuntu.gpg] {archive.MIRROR_URI} noble main\n"
    source.write_text(text)
    mirrors = tmp_path / "apt-mirrors.txt"
    mirrors.write_text(MIRROR_FIXTURE)
    assert archive.configure(tmp_path) == 1
    assert source.read_text() == text
    assert mirrors.read_text() == MIRROR_FIXTURE.split("\n", 1)[1]
    assert archive.configure(tmp_path) == 0


@pytest.mark.parametrize("bad", ["", MIRROR_FIXTURE.replace("priority:1", "priority:9"),
                                 MIRROR_FIXTURE + "https://third.example/\n",
                                 MIRROR_FIXTURE.replace("https://archive", "http://archive")])
def test_unknown_mirrorlist_fails_before_any_writes(tmp_path, bad):
    source = tmp_path / "sources.list"
    text = f"deb {OLD} noble main\ndeb {archive.MIRROR_URI} noble-updates main\n"
    source.write_text(text)
    mirrors = tmp_path / "apt-mirrors.txt"
    mirrors.write_text(bad)
    with pytest.raises(ValueError):
        archive.configure(tmp_path)
    assert source.read_text() == text
    assert mirrors.read_text() == bad


def test_other_file_uri_rejected_without_following(tmp_path):
    source = tmp_path / "sources.list"
    text = "deb mirror+file:/not/authorized/elsewhere noble main\n"
    source.write_text(text)
    mirrors = tmp_path / "apt-mirrors.txt"
    mirrors.write_text(MIRROR_FIXTURE)
    with pytest.raises(ValueError, match="Unknown mirror file URI"):
        archive.configure(tmp_path)
    assert mirrors.read_text() == MIRROR_FIXTURE


@pytest.mark.parametrize("symlink", [True, False])
def test_missing_or_symlink_mirrorlist_rejected(tmp_path, symlink):
    (tmp_path / "sources.list").write_text(f"deb {archive.MIRROR_URI} noble main\n")
    if symlink:
        actual = tmp_path / "actual"
        actual.write_text(MIRROR_FIXTURE)
        (tmp_path / "apt-mirrors.txt").symlink_to(actual)
    with pytest.raises(ValueError):
        archive.configure(tmp_path)



def test_unreferenced_mirror_file_is_not_read(tmp_path, monkeypatch):
    (tmp_path / "sources.list").write_text("deb https://archive.ubuntu.com/ubuntu noble main\n")
    mirrors = tmp_path / "apt-mirrors.txt"
    mirrors.write_text("unknown unrelated contents")
    real_read = Path.read_bytes

    def reject_mirror_read(path):
        if path == mirrors:
            raise AssertionError("Unreferenced mirror list must not be read")
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", reject_mirror_read)
    assert archive.configure(tmp_path) == 0


@pytest.mark.parametrize("field", ["Description", "Signed-By", "# URIs"])
@pytest.mark.parametrize("reference", [archive.MIRROR_URI, "mirror+file:/unrelated/file"])
def test_non_uri_deb822_field_never_reads_mirrors(tmp_path, field, reference):
    folder = tmp_path / "sources.list.d"
    folder.mkdir()
    base = SOURCE.replace(OLD, NEW)
    if field == "Signed-By":
        base = base.replace("Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n", "")
    text = base + f"{field}: {reference}\n"
    source = folder / "ubuntu.sources"
    source.write_text(text)
    assert archive.configure(tmp_path) == 0
    assert source.read_text() == text


@pytest.mark.parametrize("reference", [archive.MIRROR_URI, "mirror+file:/unrelated/file"])
def test_disabled_deb822_never_reads_mirrors(tmp_path, reference):
    folder = tmp_path / "sources.list.d"
    folder.mkdir()
    text = SOURCE.replace(OLD, reference) + "Enabled: no\n"
    source = folder / "ubuntu.sources"
    source.write_text(text)
    assert archive.configure(tmp_path) == 0
    assert source.read_text() == text


@pytest.mark.parametrize("line", [
    f"# deb {archive.MIRROR_URI} noble main\n",
    f"deb https://archive.ubuntu.com/ubuntu noble main # {archive.MIRROR_URI}\n",
    "deb [signed-by=mirror+file:/not/a/mirror] https://archive.ubuntu.com/ubuntu noble main\n",
])
def test_legacy_comments_and_options_are_not_source_addresses(tmp_path, line):
    source = tmp_path / "sources.list"
    source.write_text(line)
    assert archive.configure(tmp_path) == 0
    assert source.read_text() == line


def test_deb822_active_uri_continuation_is_recognized(tmp_path):
    folder = tmp_path / "sources.list.d"
    folder.mkdir()
    source = folder / "ubuntu.sources"
    source.write_text(SOURCE.replace(OLD, f"https://archive.ubuntu.com/ubuntu\n {archive.MIRROR_URI}"))
    mirrors = tmp_path / "apt-mirrors.txt"
    mirrors.write_text(MIRROR_FIXTURE)
    assert archive.configure(tmp_path) == 1
    assert mirrors.read_text() == archive.HTTPS_MIRRORS
