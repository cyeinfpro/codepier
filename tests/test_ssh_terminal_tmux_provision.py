"""Deterministic SSH control fixtures; no package manager or real host is called."""

import asyncio
from collections import deque
from pathlib import Path
import re
import shlex
import subprocess
import uuid

import pytest

from agent.ssh_terminal_tmux import TmuxController, provision_tmux
from agent.ssh_terminal_transport import SSHTransportError
from shared.ssh_terminal_config import DEFAULT_CONFIG


class Peer:
    def __init__(self, replies):
        self.replies = deque(replies)
        self.calls = []

    async def run_control(self, command, **options):
        self.calls.append((command, options))
        reply = self.replies.popleft()
        if isinstance(reply, Exception):
            raise reply
        return reply


def controller(peer):
    return TmuxController(peer, uuid.uuid4().hex, DEFAULT_CONFIG)


MISSING = (127, b"")
BREW = (0, b"/opt/homebrew/bin/tmux\ntmux 3.5a\n")
SYSTEM = (0, b"/usr/bin/tmux\ntmux 3.4\n")


def endpoint():
    return uuid.uuid4().hex + ".invalid", 22, "fixture"


@pytest.mark.asyncio
async def test_fresh_probe_checks_standard_path_and_pins_across_tmux_commands():
    peer = Peer([BREW, (1, b"no server running"), (0, b""), (0, b"")])
    item = controller(peer)
    assert await item.probe() == {"available": True, "version": "3.5"}
    result = await item.create(90, 26)
    assert result["session"] == item.reference.session
    assert "/opt/homebrew/bin/tmux" in peer.calls[0][0]
    assert all(command.startswith("/opt/homebrew/bin/tmux ") for command, _ in peer.calls[1:])
    assert item.attach_command().startswith("/opt/homebrew/bin/tmux ")
    assert all("apt-get" not in command for command, _ in peer.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("mac_path", ["/opt/homebrew/bin/tmux", "/usr/local/bin/tmux"])
async def test_existing_homebrew_tmux_not_in_path_never_triggers_installer(tmp_path, mac_path):
    if not Path("/bin/sh").is_file():
        pytest.skip("POSIX shell fixture unavailable")
    paths = ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux", "/bin/tmux")
    translated = {path: shlex.quote(str(tmp_path / path.lstrip("/"))) for path in paths}
    executable = tmp_path / mac_path.lstrip("/")
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n[ \"$1\" = -V ] || exit 91\nprintf 'tmux 3.5a\\n'\n")
    executable.chmod(0o700)
    empty_path = tmp_path / "without-tmux"
    empty_path.mkdir()
    calls = []

    class IsolatedShell:
        async def run_control(self, command, **options):
            calls.append(command)
            assert "command -v tmux" in command and "install tmux" not in command
            # Replace just the fixed absolute candidates, keeping the product
            # shell's fallback/control flow and preventing any real host reads.
            candidates = "|".join(re.escape(path) for path in paths)
            command = re.sub(candidates, lambda match: translated[match[0]], command)
            result = await asyncio.to_thread(
                subprocess.run,
                ["/bin/sh", "-c", command],
                capture_output=True,
                env={"PATH": str(empty_path)},
                timeout=5,
            )
            return result.returncode, result.stdout.replace(
                str(executable).encode(), mac_path.encode()
            ) + result.stderr

    def forbid_install():
        pytest.fail("An existing off-PATH tmux must not request install authorization")

    assert await provision_tmux(IsolatedShell(), endpoint(), authorize=forbid_install) == {
        "available": True,
        "version": "3.5",
    }
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("discovered", ["custom-binary", "shell-alias"])
async def test_nonstandard_tmux_discovery_blocks_install_without_running_candidate(
    tmp_path, discovered
):
    if not Path("/bin/sh").is_file():
        pytest.skip("POSIX shell fixture unavailable")
    custom = tmp_path / "custom-bin"
    custom.mkdir()
    executable = custom / "tmux"
    marker = tmp_path / "candidate-ran"
    executable.write_text(f"#!/bin/sh\n: > {shlex.quote(str(marker))}\nexit 99\n")
    executable.chmod(0o700)
    standards = re.compile(
        "|".join(
            map(
                re.escape,
                ("/opt/homebrew/bin/tmux", "/usr/local/bin/tmux", "/usr/bin/tmux", "/bin/tmux"),
            )
        )
    )
    calls = []

    class IsolatedShell:
        async def run_control(self, command, **options):
            calls.append(command)
            assert "command -v tmux" in command and "install tmux" not in command
            command = standards.sub(
                lambda match: shlex.quote(str(tmp_path / "absent" / match[0].lstrip("/"))),
                command,
            )
            if discovered == "shell-alias":
                command = f"alias tmux={shlex.quote(str(executable))}; " + command
            path = custom if discovered == "custom-binary" else tmp_path / "empty-path"
            result = await asyncio.to_thread(
                subprocess.run,
                ["/bin/sh", "-c", command],
                capture_output=True,
                env={"PATH": str(path)},
                timeout=5,
            )
            return result.returncode, result.stdout + result.stderr

    def forbid_install():
        pytest.fail("An unverified location must not request install authorization")

    assert await provision_tmux(IsolatedShell(), endpoint(), authorize=forbid_install) == {
        "available": False,
        "reason": "TMUX_LOCATION_UNSUPPORTED",
    }
    assert len(calls) == 1 and not marker.exists()


@pytest.mark.asyncio
async def test_install_requires_fresh_authority_and_confirmation_probe():
    peer = Peer([MISSING])

    def deny():
        raise SSHTransportError("AUTH_REVOKED", "fixture denied")

    with pytest.raises(SSHTransportError) as rejected:
        await provision_tmux(peer, endpoint(), authorize=deny)
    assert rejected.value.code == "AUTH_REVOKED"
    assert len(peer.calls) == 1

    peer = Peer([MISSING, (0, b""), BREW])
    seen = []
    capability = await provision_tmux(peer, endpoint(), authorize=lambda: seen.append(True))
    assert capability["available"] and seen == [True]
    command, opts = peer.calls[1]
    assert "/usr/bin/apt-get -y --no-install-recommends" in command
    assert "/usr/bin/sudo -n -k --" in command and "sudo -S" not in command
    assert "HOMEBREW_NO_AUTO_UPDATE=1" in command
    assert "curl" not in command and "wget" not in command
    assert opts == {"max_bytes": 1024, "wait_seconds": 180}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, reason",
    [
        (69, "TMUX_INSTALL_UNAVAILABLE"),
        (77, "TMUX_INSTALL_PERMISSION"),
        (70, "TMUX_INSTALL_FAILED"),
    ],
)
async def test_unsuccessful_installer_never_selects_a_temporary_shell(status, reason):
    peer = Peer([MISSING, (status, b"never expose server output")])
    with pytest.raises(SSHTransportError) as rejected:
        await provision_tmux(peer, endpoint(), authorize=lambda: None)
    assert rejected.value.code == reason
    assert "server output" not in str(rejected.value)
    assert not any("new-session" in command for command, _ in peer.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_existing", [True, False])
async def test_resume_and_close_never_install(with_existing):
    peer = Peer([SYSTEM, (0, b""), (0, b""), (0, b"")] if with_existing else [MISSING, MISSING])
    item = controller(peer)
    if with_existing:
        assert (await item.recover())["session"] == item.reference.session
        assert await item.close() == {"ended": True}
    else:
        with pytest.raises(SSHTransportError):
            await item.recover()
        with pytest.raises(SSHTransportError):
            await item.close()
    assert all("apt-get" not in command for command, _ in peer.calls)


@pytest.mark.asyncio
async def test_timeout_is_not_automatically_replayed_even_by_another_ssh_account():
    key = endpoint()
    peer = Peer([MISSING, SSHTransportError("SSH_CONTROL_UNCERTAIN", "fixture interrupted")])
    with pytest.raises(SSHTransportError) as uncertain:
        await provision_tmux(peer, key, authorize=lambda: None)
    assert uncertain.value.code == "TMUX_INSTALL_UNCERTAIN"
    follower = Peer([MISSING])
    with pytest.raises(SSHTransportError) as still_unknown:
        await provision_tmux(
            follower, (key[0].upper() + ".", key[1], "second"), authorize=lambda: None
        )
    assert still_unknown.value.code == "TMUX_INSTALL_UNCERTAIN"
    assert len(follower.calls) == 1
    assert (await provision_tmux(Peer([SYSTEM]), key, authorize=lambda: None))["available"]


@pytest.mark.asyncio
async def test_concurrent_missing_endpoints_reprobe_and_install_exactly_once():
    entered, release = asyncio.Event(), asyncio.Event()
    installed = False
    events = []

    class SharedEndpoint:
        async def run_control(self, command, **_options):
            nonlocal installed
            if "command -v tmux" in command:
                return SYSTEM if installed else MISSING
            events.append("install")
            entered.set()
            await release.wait()
            installed = True
            return (0, b"")

    key = endpoint()
    first = asyncio.create_task(provision_tmux(SharedEndpoint(), key, authorize=lambda: None))
    await entered.wait()
    second = asyncio.create_task(provision_tmux(SharedEndpoint(), key, authorize=lambda: None))
    await asyncio.sleep(0)
    try:
        assert events == ["install"] and not second.done()
    finally:
        release.set()
        result = await asyncio.gather(first, second)
    assert events == ["install"] and all(item["available"] for item in result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [
        (0, b"/tmp/tmux\ntmux 3.5a\n"),
        (0, b"/usr/bin/tmux\ntmux 3.5a\nextra"),
        (127, b"/usr/bin/tmux"),
    ],
)
async def test_probe_does_not_execute_unapproved_paths_or_treat_corruption_as_absent(reply):
    with pytest.raises(SSHTransportError) as malformed:
        await controller(Peer([reply])).probe()
    assert malformed.value.code == "PERSISTENCE_PROBE_FAILED"
