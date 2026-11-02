"""Closed Ubuntu source policy: synthetic files only, never apt or the network."""
import importlib.util
from pathlib import Path
import stat

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ci_ubuntu_direct_source", ROOT / ".github/scripts/ci_ubuntu_archive.py")
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)
SOURCE = f"""Types: deb deb-src
URIs: {archive.MIRROR_URI}
Suites: noble noble-updates noble-backports
Components: main restricted universe multiverse
Signed-By: {archive.UBUNTU_KEYRING}
"""


@pytest.fixture(autouse=True)
def ubuntu_amd64(monkeypatch):
    monkeypatch.setattr(archive.platform, "system", lambda: "Linux")
    monkeypatch.setattr(archive.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(archive.platform, "freedesktop_os_release", lambda: {
        "ID": "ubuntu", "VERSION_ID": "24.04", "VERSION_CODENAME": "noble",
    })


def write_source(root, text, *, deb822=True):
    source = root / "sources.list"
    if deb822:
        folder = root / "sources.list.d"
        folder.mkdir(exist_ok=True)
        source = folder / "ubuntu.sources"
    source.write_bytes(text.encode())
    return source


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("security", [False, True])
@pytest.mark.parametrize("deb822", [False, True])
def test_only_uri_changes_with_permissions_and_idempotence(tmp_path, monkeypatch, newline, security, deb822):
    suite = "noble-security" if security else "noble noble-updates noble-backports"
    target = archive.SECURITY if security else archive.NEW
    if deb822:
        before = SOURCE.replace("noble noble-updates noble-backports", suite)
        before = before.replace(f"URIs: {archive.MIRROR_URI}", f"uRIs:\t\n\t{archive.MIRROR_URI}  ")
        before += f"Architectures: amd64\nEnabled: yes\n# Keep {archive.MIRROR_URI} as a comment.\n"
    else:
        suite = "noble-security" if security else "noble-updates"
        before = (f" \tdeb-src  [arch=amd64 signed-by={archive.UBUNTU_KEYRING}]\t"
                  f"{archive.MIRROR_URI}  {suite}\tmain restricted  # {archive.MIRROR_URI}\n")
    before = before.replace("\n", newline).encode()
    source = write_source(tmp_path, before.decode(), deb822=deb822)
    source.chmod(0o640)
    expected = before.replace(archive.MIRROR_URI.encode(), target.encode(), 1)
    assert archive.configure(tmp_path, mode="direct-source") == 1
    assert source.read_bytes() == expected
    assert stat.S_IMODE(source.stat().st_mode) == 0o640

    def forbid_temporary(*args, **kwargs):
        raise AssertionError("Idempotent direct-source must not open a temporary file")

    monkeypatch.setattr(archive.tempfile, "mkstemp", forbid_temporary)
    assert archive.configure(tmp_path, mode="direct-source") == 0
    assert source.read_bytes() == expected


@pytest.mark.parametrize("mirror_state", ["unknown", "old", "missing", "symlink", "directory"])
def test_direct_policy_never_reads_or_changes_mirrorlist(tmp_path, monkeypatch, mirror_state):
    source = write_source(tmp_path, SOURCE)
    mirror = tmp_path / "apt-mirrors.txt"
    actual = tmp_path / "mirror-target"
    payload = b"unknown cloud format\x00\xff\n"
    if mirror_state == "old":
        payload = (archive.AZURE_MIRROR + archive.HTTPS_MIRRORS).encode()
    if mirror_state == "symlink":
        actual.write_bytes(payload)
        actual.chmod(0o600)
        mirror.symlink_to(actual)
    elif mirror_state == "directory":
        mirror.mkdir(mode=0o700)
    elif mirror_state != "missing":
        mirror.write_bytes(payload)
        mirror.chmod(0o600)
    real_read = Path.read_bytes

    def reject_mirror_read(path):
        if path in {mirror, actual}:
            raise AssertionError("Direct-source must not read a mirror list or its target")
        return real_read(path)

    monkeypatch.setattr(Path, "read_bytes", reject_mirror_read)
    assert archive.configure(tmp_path, mode="direct-source") == 1
    assert source.read_bytes() == SOURCE.replace(archive.MIRROR_URI, archive.NEW).encode()
    if mirror_state == "symlink":
        assert mirror.is_symlink()
        assert mirror.readlink() == actual
        assert real_read(actual) == payload
        assert stat.S_IMODE(actual.stat().st_mode) == 0o600
    elif mirror_state == "directory":
        assert mirror.is_dir()
        assert stat.S_IMODE(mirror.stat().st_mode) == 0o700
    elif mirror_state == "missing":
        assert not mirror.exists()
    else:
        assert real_read(mirror) == payload
        assert stat.S_IMODE(mirror.stat().st_mode) == 0o600


BAD_SOURCES = [
    (SOURCE.replace("noble-updates", "jammy-updates"), "unknown Suites"),
    (SOURCE.replace("noble-updates", "noble-security"), "mix security"),
    (SOURCE.replace("noble-updates", "noble"), "duplicate Suites"),
    (SOURCE.replace("Suites: noble noble-updates noble-backports\n", ""), "missing or duplicate Suites"),
    (SOURCE.replace("main restricted universe multiverse", "main unknown"), "unknown Ubuntu Components"),
    (SOURCE.replace("Components: main restricted universe multiverse\n", ""), "Ubuntu Components"),
    (SOURCE.replace("main restricted", "main main"), "Ubuntu Components"),
    (SOURCE.replace("Types: deb deb-src", "Types: binary"), "Types must be"),
    (SOURCE.replace("Types: deb deb-src", "Types: deb deb"), "Types must be"),
    (SOURCE.replace("Types: deb deb-src\n", ""), "Types must be"),
    (SOURCE.replace(archive.UBUNTU_KEYRING, "/keys/unknown.gpg"), "official Ubuntu Signed-By"),
    (SOURCE.replace(f"Signed-By: {archive.UBUNTU_KEYRING}\n", ""), "official Ubuntu Signed-By"),
    (SOURCE.replace(archive.UBUNTU_KEYRING, archive.UBUNTU_KEYRING + " /keys/extra.gpg"), "Signed-By"),
    (SOURCE + "Architectures: arm64\n", "Architectures"),
    (SOURCE + "Architectures: amd64 arm64\n", "Architectures"),
    (SOURCE + "Enabled: unknown\n", "Enabled"),
    (SOURCE + "Trusted: yes\n", "unsupported source fields"),
    (SOURCE + "Allow-Insecure: yes\n", "unsupported source fields"),
    (SOURCE + "Check-Valid-Until: no\n", "unsupported source fields"),
    (SOURCE + "Description: unknown custom field\n", "unsupported source fields"),
    (SOURCE.replace(archive.MIRROR_URI, archive.MIRROR_URI + "\n https://third.example/ubuntu"), "single exact"),
    (SOURCE + "Suites: noble\n", "Duplicate deb822 field"),
]


@pytest.mark.parametrize("same_file", [False, True])
@pytest.mark.parametrize("bad,diagnostic", BAD_SOURCES)
def test_every_target_is_validated_before_any_write(tmp_path, monkeypatch, bad, diagnostic, same_file):
    if same_file:
        sources = [write_source(tmp_path, SOURCE + "\n" + bad)]
    else:
        first = write_source(tmp_path, f"deb [signed-by={archive.UBUNTU_KEYRING}] {archive.MIRROR_URI} noble main\n",
                             deb822=False)
        sources = [first, write_source(tmp_path, bad)]
    before = {path: path.read_bytes() for path in sources}

    def forbid_temporary(*args, **kwargs):
        raise AssertionError("Validation must finish before any write")

    monkeypatch.setattr(archive.tempfile, "mkstemp", forbid_temporary)
    with pytest.raises(ValueError, match=diagnostic):
        archive.configure(tmp_path, mode="direct-source")
    assert {path: path.read_bytes() for path in sources} == before


@pytest.mark.parametrize("address", [
    "mirror+file:/elsewhere/mirrors", archive.MIRROR_URI + "/", archive.MIRROR_URI + "?query=1",
    "mirror+file:///etc/apt/apt-mirrors.txt", "MIRROR+FILE:/etc/apt/apt-mirrors.txt",
])
@pytest.mark.parametrize("deb822", [True, False])
def test_unknown_file_uris_fail_closed_without_reading_them(tmp_path, address, deb822):
    text = SOURCE.replace(archive.MIRROR_URI, address) if deb822 else f"deb {address} noble main\n"
    source = write_source(tmp_path, text, deb822=deb822)
    with pytest.raises(ValueError, match="Unknown mirror file URI"):
        archive.configure(tmp_path, mode="direct-source")
    assert source.read_bytes() == text.encode()


@pytest.mark.parametrize("options,diagnostic", [
    ("", "Signed-By"),
    ("signed-by=/keys/other.gpg", "Signed-By"),
    (f"signed-by={archive.UBUNTU_KEYRING} trusted=yes", "legacy options"),
    (f"signed-by={archive.UBUNTU_KEYRING} allow-insecure=yes", "legacy options"),
    (f"signed-by={archive.UBUNTU_KEYRING} check-valid-until=no", "legacy options"),
    (f"signed-by={archive.UBUNTU_KEYRING} arch=amd64 arch=amd64", "legacy options"),
    (f"signed-by={archive.UBUNTU_KEYRING} arch=amd64,arm64", "Architectures"),
    (f"signed-by={archive.UBUNTU_KEYRING} signed-by={archive.UBUNTU_KEYRING}", "legacy options"),
])
def test_legacy_requires_only_known_options(tmp_path, options, diagnostic):
    prefix = f"[{options}] " if options else ""
    text = f"deb {prefix}{archive.MIRROR_URI} noble main\n"
    source = write_source(tmp_path, text, deb822=False)
    with pytest.raises(ValueError, match=diagnostic):
        archive.configure(tmp_path, mode="direct-source")
    assert source.read_bytes() == text.encode()


@pytest.mark.parametrize("text", [
    f"deb {archive.MIRROR_URI}\n",
    f"deb [signed-by={archive.UBUNTU_KEYRING}] {archive.MIRROR_URI} noble # no components\n",
    f"deb [signed-by={archive.UBUNTU_KEYRING} {archive.MIRROR_URI} noble main\n",
    f"unknown {archive.MIRROR_URI} noble main\n",
])
def test_malformed_legacy_mirror_entry_fails_closed(tmp_path, text):
    source = write_source(tmp_path, text, deb822=False)
    with pytest.raises(ValueError):
        archive.configure(tmp_path, mode="direct-source")
    assert source.read_bytes() == text.encode()


def test_comments_disabled_and_third_party_sources_are_unchanged(tmp_path):
    third = SOURCE.replace(archive.MIRROR_URI, "https://third.example/ubuntu")
    third += "Description: mirror+file:/unused/mirrors\n"
    disabled = SOURCE.replace(archive.MIRROR_URI, "mirror+file:/disabled/mirrors") + "Enabled: no\n"
    text = third + "\n" + disabled + f"\n# URIs: {archive.MIRROR_URI}\n"
    deb822 = write_source(tmp_path, text)
    legacy_text = (f"# deb {archive.MIRROR_URI} noble main\n"
                   f"deb https://third.example/ubuntu stable main # {archive.MIRROR_URI}\n"
                   "deb [signed-by=mirror+file:/not/a/mirror] https://third.example/ubuntu stable main\n")
    legacy = write_source(tmp_path, legacy_text, deb822=False)
    assert archive.configure(tmp_path, mode="direct-source") == 0
    assert deb822.read_bytes() == text.encode()
    assert legacy.read_bytes() == legacy_text.encode()


@pytest.mark.parametrize("directory", [False, True])
def test_source_symlinks_remain_rejected(tmp_path, directory):
    actual = tmp_path / "actual"
    if directory:
        actual.mkdir()
        target = actual / "ubuntu.sources"
        target.write_text(SOURCE)
        (tmp_path / "sources.list.d").symlink_to(actual, target_is_directory=True)
    else:
        target = actual
        target.write_text(SOURCE)
        (tmp_path / "sources.list").symlink_to(actual)
    with pytest.raises(ValueError, match="symlink source"):
        archive.configure(tmp_path, mode="direct-source")
    assert target.read_bytes() == SOURCE.encode()


@pytest.mark.parametrize("system,machine,release,diagnostic", [
    ("Darwin", "x86_64", {}, "Linux x86_64"),
    ("Linux", "aarch64", {}, "Linux x86_64"),
    ("Linux", "x86_64", {"ID": "debian", "VERSION_ID": "24.04", "VERSION_CODENAME": "noble"}, "Ubuntu 24.04"),
    ("Linux", "x86_64", {"ID": "ubuntu", "VERSION_ID": "22.04", "VERSION_CODENAME": "jammy"}, "Ubuntu 24.04"),
    ("Linux", "x86_64", {"ID": "ubuntu", "VERSION_ID": "24.04", "VERSION_CODENAME": "unknown"}, "Ubuntu 24.04"),
    ("Linux", "x86_64", {}, "Ubuntu 24.04"),
])
def test_cli_direct_environment_guard_precedes_source_access(monkeypatch, system, machine, release, diagnostic):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_OS", "Linux")
    monkeypatch.setattr(archive.platform, "system", lambda: system)
    monkeypatch.setattr(archive.platform, "machine", lambda: machine)
    monkeypatch.setattr(archive.platform, "freedesktop_os_release", lambda: release)

    def forbid_source_access(*args, **kwargs):
        raise AssertionError("Environment rejection must precede source access")

    monkeypatch.setattr(Path, "is_symlink", forbid_source_access)
    monkeypatch.setattr(Path, "read_bytes", forbid_source_access)
    with pytest.raises(ValueError, match=diagnostic):
        archive.main(["--mode", "direct-source"])


def test_os_release_failure_has_bounded_diagnostic(tmp_path, monkeypatch):
    def broken_release():
        raise OSError("synthetic missing os-release")

    monkeypatch.setattr(archive.platform, "freedesktop_os_release", broken_release)
    with pytest.raises(ValueError, match="cannot establish the OS release"):
        archive.configure(tmp_path, mode="direct-source")


@pytest.mark.parametrize("args,mode", [([], "strict-mirror"), (["--mode", "strict-mirror"], "strict-mirror"),
                                       (["--mode", "direct-source"], "direct-source")])
def test_cli_mode_is_explicit_and_default_stays_strict(monkeypatch, capsys, args, mode):
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("RUNNER_OS", "Linux")
    calls = []

    def configure(root, *, mode):
        calls.append((root, mode))
        return 2

    monkeypatch.setattr(archive, "configure", configure)
    archive.main(args)
    assert calls == [("/etc/apt", mode)]
    assert f"({mode}): updated 2 source files" in capsys.readouterr().out


@pytest.mark.parametrize("github,runner", [("false", "Linux"), ("true", "Windows"), ("", "")])
def test_cli_direct_policy_still_requires_disposable_github_linux(monkeypatch, github, runner):
    monkeypatch.setenv("GITHUB_ACTIONS", github)
    monkeypatch.setenv("RUNNER_OS", runner)

    def forbid_configure(*args, **kwargs):
        raise AssertionError("CLI guard must precede configure")

    monkeypatch.setattr(archive, "configure", forbid_configure)
    with pytest.raises(SystemExit, match="Only disposable GitHub Linux CI"):
        archive.main(["--mode", "direct-source"])


def test_unknown_mode_is_rejected_before_source_access(tmp_path, monkeypatch):
    def forbid_access(*args, **kwargs):
        raise AssertionError("Unknown mode must not access sources")

    monkeypatch.setattr(Path, "is_symlink", forbid_access)
    with pytest.raises(ValueError, match="Unknown archive-selection mode"):
        archive.configure(tmp_path, mode="fallback")
    with pytest.raises(SystemExit) as error:
        archive.main(["--mode", "fallback"])
    assert error.value.code == 2


def test_diagnostic_never_echoes_source_values_or_unknown_field_names(tmp_path):
    secret = "SYNTHETIC-PRIVATE-VALUE"
    source = write_source(tmp_path, SOURCE + f"X-{secret}: https://user:{secret}@example.test/\n")
    with pytest.raises(ValueError) as error:
        archive.configure(tmp_path, mode="direct-source")
    assert str(error.value) == "Direct-source has unsupported source fields (1)"
    assert secret not in str(error.value)
    assert secret.encode() in source.read_bytes()
