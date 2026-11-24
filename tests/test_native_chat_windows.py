"""Tests for PosixPipeTransport, WindowsPipeTransport contracts, and P4.3 Win-0 gates."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest

from agent.native_process import (
    MAX_STDERR_BYTES,
    PipeTransportError,
    PosixPipeTransport,
    WindowsPipeTransport,
    structured_chat_platform_support,
)
from shared.native_cli import process_exists
from shared.native_flags import NativeFeatureFlags

ROOT = Path(__file__).resolve().parents[1]


def test_structured_chat_platform_support_win0_gates():
    posix = structured_chat_platform_support(NativeFeatureFlags(), os_name="posix")
    assert posix.available is True
    assert posix.reason_code is None

    win0 = structured_chat_platform_support(NativeFeatureFlags(), os_name="nt")
    assert win0.available is False
    assert win0.reason_code == "CLI_CHAT_PLATFORM"
    assert win0.stage == "Win-0"

    # Flag alone without real Windows runtime validation must remain unavailable!
    unvalidated = structured_chat_platform_support(
        NativeFeatureFlags(native_windows_structured_chat=True),
        os_name="nt",
        windows_runtime_validated=False,
    )
    assert unvalidated.available is False
    assert unvalidated.reason_code == "CLI_CHAT_PLATFORM"

    validated = structured_chat_platform_support(
        NativeFeatureFlags(native_windows_structured_chat=True),
        os_name="nt",
        windows_runtime_validated=True,
    )
    assert validated.available is True
    assert validated.reason_code is None


def test_check_native_chat_windows_script_honest_on_non_windows(tmp_path):
    if os.name == "nt":
        pytest.skip("Non-Windows host assertion")
    out = tmp_path / "win-check.json"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_native_chat_windows.py"), "--output", str(out)],
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(proc.stdout)
    assert report["status"] == "UNVERIFIED_NON_WINDOWS_HOST"
    assert report["structured_chat_available"] is False
    assert report["reason_code"] == "CLI_CHAT_PLATFORM"
    assert json.loads(out.read_text(encoding="utf-8")) == report


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess transport test")
def test_posix_pipe_transport_split_utf8_crlf_stderr_and_exit_state(tmp_path):
    child = tmp_path / "child.py"
    child.write_text(
        (
            "import json, sys, time\n"
            "payload = json.dumps({'text': '世界😀🚀'}, ensure_ascii=False).encode('utf-8') + b'\\r\\n'\n"
            "sys.stdout.buffer.write(payload[:11])\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(0.03)\n"
            "sys.stdout.buffer.write(payload[11:])\n"
            "sys.stdout.buffer.flush()\n"
            "sys.stderr.buffer.write(b'E' * 70000)\n"
            "sys.stderr.buffer.flush()\n"
            "line = sys.stdin.buffer.readline()\n"
            "sys.stdout.buffer.write(b'{\"echo\":' + line.strip() + b'}\\n')\n"
            "sys.stdout.buffer.flush()\n"
        ),
        encoding="utf-8",
    )
    transport = PosixPipeTransport()
    transport.start([sys.executable, "-u", str(child)], tmp_path, dict(os.environ))
    pid = transport._child.pid
    events = []
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not any(e["kind"] == "stdout_line" for e in events):
        events.extend(transport.drain_events(time.monotonic() + 0.1))
    first = next(e for e in events if e["kind"] == "stdout_line")
    assert first["data"] == {"text": "世界😀🚀"}

    transport.write(b'{"ok":true}\n', "r" * 32)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not any(e["kind"] == "eof" for e in events):
        events.extend(transport.drain_events(time.monotonic() + 0.1))
    echo = [e for e in events if e["kind"] == "stdout_line"][-1]
    assert echo["data"] == {"echo": {"ok": True}}

    state = transport.stop_and_drain(timeout=2.0)
    assert state.eof_seen is True
    assert state.process_exited is True
    assert state.output_complete is True
    assert state.cleaned is True
    assert len(state.stderr_tail) <= MAX_STDERR_BYTES
    assert not process_exists(pid)


@pytest.mark.skipif(os.name == "nt", reason="POSIX subprocess transport test")
def test_posix_pipe_transport_incomplete_line_marks_output_incomplete(tmp_path):
    child = tmp_path / "partial.py"
    child.write_text(
        "import sys\nsys.stdout.buffer.write(b'{\"partial\":true')\nsys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    transport = PosixPipeTransport()
    transport.start([sys.executable, "-u", str(child)], tmp_path, dict(os.environ))
    deadline = time.monotonic() + 5.0
    events = []
    while time.monotonic() < deadline and not any(e["kind"] == "eof" for e in events):
        events.extend(transport.drain_events(time.monotonic() + 0.1))
    state = transport.stop_and_drain(timeout=2.0)
    assert state.eof_seen is True
    assert state.output_complete is False


class _FakePipeStream:
    def __init__(self, chunks=None, *, stall_write=False, stall_read=False):
        self._chunks = queue.Queue()
        for item in chunks or []:
            self._chunks.put(item)
        self.stall_write = stall_write
        self.stall_read = stall_read
        self.entered_write = threading.Event()
        self.release = threading.Event()
        self.writes = []
        self.closed = False

    def read(self, _n=65536):
        if self.stall_read:
            self.release.wait(5.0)
            return b""
        return self._chunks.get()

    def write(self, data):
        if data == b"G":
            self.writes.append(data)
            return 1
        if self.stall_write:
            self.entered_write.set()
            self.release.wait(5.0)
        self.writes.append(data)
        return len(data)

    def flush(self):
        return None

    def close(self):
        self.closed = True
        self.release.set()
        self._chunks.put(b"")


class _FakeProc:
    def __init__(self, stdout_chunks=None, stderr_chunks=None, *, stall_write=False, stall_read=False):
        self.pid = 424242
        self.stdin = _FakePipeStream(stall_write=stall_write)
        self.stdout = _FakePipeStream(stdout_chunks, stall_read=stall_read)
        self.stderr = _FakePipeStream(stderr_chunks)
        self.killed = False

    def kill(self):
        self.killed = True
        self.stdout.close()
        self.stderr.close()
        self.stdin.close()


class _FakeJob:
    def __init__(self, pid):
        self.pid = pid
        self.is_alive = True
        self.terminated = False
        self.closed = False

    def alive(self):
        return self.is_alive

    def exit_code(self):
        return None if self.is_alive else 0

    def terminate(self):
        self.terminated = True
        self.is_alive = False

    def close(self):
        self.closed = True


def test_windows_pipe_transport_gated_spawn_and_env_preserved(tmp_path):
    bin_dir = tmp_path / "npm"
    bin_dir.mkdir()
    (bin_dir / "node.exe").write_bytes(b"node")
    shim = bin_dir / "codex.cmd"
    shim.write_text("echo shim")
    pkg = bin_dir / "node_modules/@openai/codex"
    pkg.mkdir(parents=True)
    entry = pkg / "bin/codex.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// codex")
    (pkg / "package.json").write_text(json.dumps({"bin": {"codex": "bin/codex.js"}}))

    order = []
    captured = {}

    def proc_factory(resolved, cwd, env):
        captured["resolved"] = resolved
        captured["env"] = env
        proc = _FakeProc([b'{"ok":"', b'\xe4\xb8\x96\xe7\x95\x8c\xf0\x9f\x98\x80"}\r\n', b""], [b"err\n", b""])
        orig_write = proc.stdin.write

        def tracked_write(data):
            if data == b"G":
                order.append("gate")
            return orig_write(data)

        proc.stdin.write = tracked_write
        return proc

    def job_factory(pid):
        order.append("job")
        return _FakeJob(pid)

    transport = WindowsPipeTransport(process_factory=proc_factory, job_factory=job_factory)
    transport.start([str(shim), "app-server"], tmp_path, {"PATH": "", "OPENAI_API_KEY": "secret-kept"})
    assert order == ["job", "gate"]
    assert captured["resolved"][0].endswith("node.exe")
    assert "cmd.exe" not in " ".join(captured["resolved"]).lower()
    assert captured["env"]["OPENAI_API_KEY"] == "secret-kept"

    events = transport.drain_events(time.monotonic() + 2.0)
    lines = [e for e in events if e["kind"] == "stdout_line"]
    assert lines[0]["data"] == {"ok": "世界😀"}

    state = transport.stop_and_drain(timeout=2.0)
    assert state.eof_seen is True
    assert state.process_exited is True
    assert state.output_complete is True
    assert state.stderr_tail == "err\n"


def test_windows_pipe_transport_backpressure_never_blocks_stdout_and_honest_incomplete_tail(tmp_path):
    proc = _FakeProc(
        stdout_chunks=[b'{"msg":"alive"}\n'],
        stderr_chunks=[b"diag\n"],
        stall_write=True,
    )
    job = _FakeJob(proc.pid)
    transport = WindowsPipeTransport(
        max_outgoing_frames=2,
        process_factory=lambda *_: proc,
        job_factory=lambda pid: job,
    )
    transport.start(["codex.exe"], tmp_path, {"PATH": ""})

    # Stalled writer takes item 1, queue holds at most 2 more -> 4th raises backpressure without blocking!
    transport.write(b'{"n":1}\n', "1" * 32)
    assert proc.stdin.entered_write.wait(2.0)
    transport.write(b'{"n":2}\n', "2" * 32)
    transport.write(b'{"n":3}\n', "3" * 32)
    with pytest.raises(PipeTransportError) as exc:
        transport.write(b'{"n":4}\n', "4" * 32)
    assert exc.value.code == "PIPE_INPUT_BACKPRESSURE"

    # Stdout still drains while stdin writer is stalled.
    events = transport.drain_events(time.monotonic() + 1.0)
    assert any(e.get("data") == {"msg": "alive"} for e in events)

    # Stop without stdout EOF -> output_complete MUST be False!
    # Keep stdout open across close to simulate tail drain timeout.
    proc.stdout.close = lambda: None
    proc.stdout.stall_read = True
    state = transport.stop_and_drain(timeout=0.05)
    assert state.output_complete is False
    proc.stdout.release.set()
    proc.stdin.release.set()
