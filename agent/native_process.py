"""Cross-platform JSON pipe transports and Windows structured-chat platform gates."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import contextlib
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import queue
import selectors
import signal
import subprocess
import sys
import threading
import time
from typing import Any, Protocol, runtime_checkable

from agent.native_terminal_wake import LocalWake, WindowsWake
from agent.native_windows import ProcessJob, resolve_argv
from shared.native_contracts import FeatureState
from shared.native_flags import NativeFeatureFlags, resolve_native_flags

MAX_LINE = 64 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_OUTGOING_BYTES = 32 * 1024 * 1024
MAX_OUTGOING_FRAMES = 4096
MAX_INCOMING_BYTES = 64 * 1024 * 1024
MAX_INCOMING_FRAMES = 4096


class PipeTransportError(RuntimeError):
    """Structured error raised on pipe quota, backpressure, or lifecycle failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OwnedExitState:
    """Distinct EOF, exit, and tail completeness facts for an owned pipe process."""

    eof_seen: bool
    process_exited: bool
    output_complete: bool
    exit_code: int | None
    cleaned: bool
    stderr_tail: str = ""
    error: str | None = None
    threads_joined: bool = True


@dataclass(frozen=True)
class StructuredChatPlatformSupport:
    """Platform support state gated by P4.3 Win-0..Win-3 validation rules."""

    supported: bool
    available: bool
    reason_code: str | None
    stage: str
    os_name: str
    windows_runtime_validated: bool = False

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and hasattr(self, key)

    def to_feature_state(self) -> FeatureState:
        return FeatureState(
            supported=self.supported, available=self.available, reason_code=self.reason_code
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def structured_chat_platform_support(
    flags: NativeFeatureFlags | Mapping[str, Any] | None = None,
    *,
    os_name: str | None = None,
    windows_runtime_validated: bool = False,
) -> StructuredChatPlatformSupport:
    """Evaluate P4.3 structured chat availability (Win-0 keeps Windows unsupported)."""
    resolved = resolve_native_flags(flags)
    current_os = os.name if os_name is None else os_name
    if current_os != "nt":
        return StructuredChatPlatformSupport(
            supported=True,
            available=True,
            reason_code=None,
            stage="posix",
            os_name=current_os,
            windows_runtime_validated=False,
        )
    if not resolved.native_windows_structured_chat or not windows_runtime_validated:
        stage = "Win-1" if windows_runtime_validated else "Win-0"
        return StructuredChatPlatformSupport(
            supported=True,
            available=False,
            reason_code="CLI_CHAT_PLATFORM",
            stage=stage,
            os_name=current_os,
            windows_runtime_validated=windows_runtime_validated,
        )
    return StructuredChatPlatformSupport(
        supported=True,
        available=True,
        reason_code=None,
        stage="Win-2",
        os_name=current_os,
        windows_runtime_validated=True,
    )


@runtime_checkable
class NativePipeTransport(Protocol):
    """Synchronous pipe transport contract for POSIX and Windows structured chat workers."""

    def start(
        self, argv: Sequence[str], cwd: str | Path, env: Mapping[str, str] | None = None
    ) -> None: ...

    def write(self, frame_bytes: bytes, receipt: str) -> None: ...

    def drain_events(self, deadline: float) -> list[dict[str, Any]]: ...

    def interrupt_run(self) -> None: ...

    def stop_and_drain(self, timeout: float = 3.0) -> OwnedExitState: ...

    def owned_exit_state(self) -> OwnedExitState: ...


class JsonlLineBuffer:
    """Raw-byte \\n JSONL splitter preserving split UTF-8, CRLF, and non-BMP emoji."""

    def __init__(self, max_line: int = MAX_LINE) -> None:
        self.max_line = max_line
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self._buf.extend(chunk)
        lines: list[bytes] = []
        while b"\n" in self._buf:
            line, _, rest = self._buf.partition(b"\n")
            self._buf[:] = rest
            if line.endswith(b"\r"):
                line = line[:-1]
            if len(line) > self.max_line:
                raise PipeTransportError("LINE_QUOTA_EXCEEDED", "Native JSON line quota exceeded")
            if line.strip():
                lines.append(bytes(line))
        if len(self._buf) > self.max_line:
            raise PipeTransportError("LINE_QUOTA_EXCEEDED", "Native JSON line quota exceeded")
        return lines

    def has_partial(self) -> bool:
        return bool(self._buf.strip())


def _decode_line_event(raw_line: bytes) -> dict[str, Any]:
    text = raw_line.decode("utf-8")
    parsed: Any = None
    with contextlib.suppress(ValueError):
        parsed = json.loads(text)
    return {"kind": "stdout_line", "text": text, "data": parsed}


def _append_ring(ring: bytearray, chunk: bytes, limit: int = MAX_STDERR_BYTES) -> None:
    if not chunk:
        return
    if len(chunk) >= limit:
        ring[:] = chunk[-limit:]
        return
    ring.extend(chunk)
    if len(ring) > limit:
        del ring[: len(ring) - limit]


class PosixPipeTransport:
    """Non-blocking POSIX selector pipe transport with process-group ownership."""

    def __init__(self, *, max_line: int = MAX_LINE) -> None:
        self._line_buffer = JsonlLineBuffer(max_line=max_line)
        self._selector: selectors.BaseSelector | None = None
        self._child: subprocess.Popen[bytes] | None = None
        self._outgoing = bytearray()
        self._outgoing_frames: list[tuple[int, str]] = []
        self._stderr_ring = bytearray()
        self._stdout_eof = False
        self._stderr_eof = False
        self._stdin_closed = False
        self._process_exited = False
        self._exit_code: int | None = None
        self._cleaned = False
        self._error: str | None = None
        self._pending_events: list[dict[str, Any]] = []

    def start(
        self, argv: Sequence[str], cwd: str | Path, env: Mapping[str, str] | None = None
    ) -> None:
        if os.name == "nt":
            raise PipeTransportError("PLATFORM_UNSUPPORTED", "PosixPipeTransport requires POSIX")
        self._child = subprocess.Popen(
            list(argv),
            cwd=str(cwd),
            env=dict(env) if env is not None else None,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
        )
        assert self._child.stdin and self._child.stdout and self._child.stderr
        self._selector = selectors.DefaultSelector()
        for pipe in (self._child.stdout, self._child.stderr):
            os.set_blocking(pipe.fileno(), False)
            self._selector.register(pipe, selectors.EVENT_READ)
        os.set_blocking(self._child.stdin.fileno(), False)

    def write(self, frame_bytes: bytes, receipt: str) -> None:
        if self._child is None or self._child.stdin is None or self._stdin_closed:
            raise PipeTransportError("PIPE_CLOSED", "Native stdin pipe is closed")
        if not isinstance(frame_bytes, (bytes, bytearray)):
            raise TypeError("frame_bytes must be bytes")
        if (
            len(self._outgoing) + len(frame_bytes) > MAX_OUTGOING_BYTES
            or len(self._outgoing_frames) >= MAX_OUTGOING_FRAMES
        ):
            raise PipeTransportError(
                "PIPE_INPUT_BACKPRESSURE", "Native input buffer quota exceeded"
            )
        self._outgoing.extend(frame_bytes)
        self._outgoing_frames.append((len(frame_bytes), receipt))
        self._flush_stdin()
        self._update_stdin_interest()

    def _flush_stdin(self) -> None:
        if (
            not self._outgoing
            or self._child is None
            or self._child.stdin is None
            or self._stdin_closed
        ):
            return
        try:
            written = os.write(self._child.stdin.fileno(), self._outgoing[:65536])
        except BlockingIOError:
            return
        except BrokenPipeError as exc:
            self._stdin_closed = True
            self._error = "BrokenPipeError"
            raise PipeTransportError("BROKEN_PIPE", "Native stdin closed unexpectedly") from exc
        del self._outgoing[:written]
        self._ack_written_frames(written)

    def _ack_written_frames(self, written: int) -> None:
        remaining = written
        while remaining > 0 and self._outgoing_frames:
            size, receipt = self._outgoing_frames[0]
            if remaining >= size:
                remaining -= size
                self._outgoing_frames.pop(0)
                self._pending_events.append({"kind": "write_ack", "receipt": receipt})
            else:
                self._outgoing_frames[0] = (size - remaining, receipt)
                remaining = 0

    def _update_stdin_interest(self) -> None:
        if self._selector is None or self._child is None or self._child.stdin is None:
            return
        stdin = self._child.stdin
        registered = stdin in self._selector.get_map()
        if self._outgoing and not self._stdin_closed and not registered:
            self._selector.register(stdin, selectors.EVENT_WRITE)
        elif (not self._outgoing or self._stdin_closed) and registered:
            self._selector.unregister(stdin)

    def drain_events(self, deadline: float) -> list[dict[str, Any]]:
        if self._child is None or self._selector is None:
            return []
        events = list(self._pending_events)
        self._pending_events.clear()
        while True:
            self._flush_stdin()
            self._update_stdin_interest()
            self._poll_child_exit()
            remaining = max(0.0, deadline - time.monotonic())
            ready = self._selector.select(min(0.05, remaining) if remaining > 0 else 0.0)
            self._consume_ready(ready, events)
            if self._stdout_eof and self._stderr_eof:
                break
            if time.monotonic() >= deadline or (not ready and remaining <= 0.0):
                break
        return events

    def _consume_ready(
        self, ready: Sequence[tuple[selectors.SelectorKey, int]], events: list[dict[str, Any]]
    ) -> None:
        assert self._child is not None and self._selector is not None
        for key, _ in ready:
            if key.fileobj is self._child.stdin:
                self._flush_stdin()
                self._update_stdin_interest()
                continue
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                self._handle_eof(key.fileobj, events)
                continue
            if key.fileobj is self._child.stderr:
                _append_ring(self._stderr_ring, chunk)
            else:
                for line in self._line_buffer.feed(chunk):
                    events.append(_decode_line_event(line))

    def _handle_eof(self, fileobj: Any, events: list[dict[str, Any]]) -> None:
        assert self._selector is not None and self._child is not None
        with contextlib.suppress(KeyError):
            self._selector.unregister(fileobj)
        if fileobj is self._child.stdout:
            self._stdout_eof = True
            events.append({"kind": "eof", "stream": "stdout"})
        else:
            self._stderr_eof = True

    def _poll_child_exit(self) -> None:
        if self._child is None or self._process_exited:
            return
        from agent.owned_process_group import child_exited

        if child_exited(self._child.pid):
            self._process_exited = True

    def interrupt_run(self) -> None:
        if self._child is None or self._process_exited:
            return
        with contextlib.suppress(ProcessLookupError):
            os.kill(self._child.pid, signal.SIGINT)

    def stop_and_drain(self, timeout: float = 3.0) -> OwnedExitState:
        if self._child is None:
            return self.owned_exit_state()
        from agent.owned_process_group import stop_owned_group

        self._drain_before_close(timeout=min(0.25, timeout))
        cleaned, code = stop_owned_group(self._child.pid, grace=min(1.0, timeout), grouped=True)
        self._cleaned = cleaned
        self._exit_code = code
        self._process_exited = code is not None or self._process_exited
        self._child.returncode = code if code is not None else -1
        self._drain_remaining_nonblocking()
        self._close_handles()
        return self.owned_exit_state()

    def _drain_before_close(self, timeout: float) -> None:
        if self._selector is None or self._stdout_eof:
            return
        self._drain_remaining_nonblocking()
        if not self._stdout_eof and timeout > 0:
            self.drain_events(time.monotonic() + timeout)

    def _drain_remaining_nonblocking(self) -> None:
        if self._child is None:
            return
        for stream, is_stderr in ((self._child.stdout, False), (self._child.stderr, True)):
            if stream is None:
                continue
            while True:
                try:
                    chunk = os.read(stream.fileno(), 65536)
                except (BlockingIOError, OSError):
                    break
                if not chunk:
                    if is_stderr:
                        self._stderr_eof = True
                    else:
                        self._stdout_eof = True
                    break
                if is_stderr:
                    _append_ring(self._stderr_ring, chunk)
                else:
                    for line in self._line_buffer.feed(chunk):
                        self._pending_events.append(_decode_line_event(line))

    def _close_handles(self) -> None:
        if self._selector is not None:
            with contextlib.suppress(OSError):
                self._selector.close()
            self._selector = None
        if self._child is not None:
            for pipe in (self._child.stdin, self._child.stdout, self._child.stderr):
                if pipe is not None:
                    with contextlib.suppress(OSError):
                        pipe.close()

    def owned_exit_state(self) -> OwnedExitState:
        output_complete = (
            self._stdout_eof and self._process_exited and not self._line_buffer.has_partial()
        )
        return OwnedExitState(
            eof_seen=self._stdout_eof,
            process_exited=self._process_exited,
            output_complete=output_complete,
            exit_code=self._exit_code,
            cleaned=self._cleaned,
            stderr_tail=bytes(self._stderr_ring).decode("utf-8", errors="replace"),
            error=self._error,
            threads_joined=True,
        )


class WindowsPipeTransport:
    """Bounded 3-thread pipe transport with Job-gated spawn and event wakeup."""

    def __init__(
        self,
        *,
        max_line: int = MAX_LINE,
        max_incoming_bytes: int = MAX_INCOMING_BYTES,
        max_incoming_frames: int = MAX_INCOMING_FRAMES,
        max_outgoing_bytes: int = MAX_OUTGOING_BYTES,
        max_outgoing_frames: int = MAX_OUTGOING_FRAMES,
        process_factory: Callable[..., Any] | None = None,
        job_factory: Callable[[int], Any] | None = None,
        wake_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._line_buffer = JsonlLineBuffer(max_line=max_line)
        self._max_incoming_bytes = max_incoming_bytes
        self._max_incoming_frames = max_incoming_frames
        self._max_outgoing_bytes = max_outgoing_bytes
        self._max_outgoing_frames = max_outgoing_frames
        self._process_factory = process_factory
        self._job_factory = job_factory
        self._wake_factory = wake_factory
        self._proc: Any = None
        self._job: Any = None
        self._wake: Any = None
        self._closing = threading.Event()
        self._cond = threading.Condition()
        self._incoming: list[bytes | None] = []
        self._incoming_bytes = 0
        self._outgoing: list[tuple[bytes, str] | None] = []
        self._outgoing_bytes = 0
        self._write_events: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=max_outgoing_frames)
        self._stderr_ring = bytearray()
        self._stderr_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._stdout_eof = False
        self._stderr_eof = False
        self._process_exited = False
        self._exit_code: int | None = None
        self._cleaned = False
        self._threads_joined = True
        self._error: str | None = None

    def start(
        self, argv: Sequence[str], cwd: str | Path, env: Mapping[str, str] | None = None
    ) -> None:
        env_dict = dict(os.environ if env is None else env)
        resolved = resolve_argv(list(argv), env_dict)
        self._wake = self._create_wake()
        proc, job = self._spawn_gated_process(resolved, str(cwd), env_dict)
        self._proc = proc
        self._job = job
        self._start_threads()

    def _create_wake(self) -> Any:
        if self._wake_factory is not None:
            return self._wake_factory()
        if os.name == "nt":
            try:
                return WindowsWake(str(Path.cwd()), f"pipe-{os.getpid()}-{id(self)}")
            except OSError:
                return LocalWake()
        return LocalWake()

    def _spawn_gated_process(
        self, resolved: list[str], cwd: str, env_dict: dict[str, str]
    ) -> tuple[Any, Any]:
        if self._process_factory is not None:
            proc = self._process_factory(resolved, cwd, env_dict)
        else:
            gate_argv = [
                sys.executable,
                "-I",
                "-S",
                str(Path(__file__).resolve()),
                "--windows-gate",
                *resolved,
            ]
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            proc = subprocess.Popen(
                gate_argv,
                cwd=cwd,
                env=env_dict,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=flags,
            )
        job_fn = self._job_factory or ProcessJob
        try:
            job = job_fn(proc.pid)
            proc.stdin.write(b"G")
            proc.stdin.flush()
            return proc, job
        except BaseException:
            with contextlib.suppress(Exception):
                proc.kill()
            raise

    def _start_threads(self) -> None:
        self._threads = [
            threading.Thread(target=self._stdout_loop, name="win-pipe-stdout", daemon=True),
            threading.Thread(target=self._stderr_loop, name="win-pipe-stderr", daemon=True),
            threading.Thread(target=self._stdin_loop, name="win-pipe-stdin", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _stdout_loop(self) -> None:
        try:
            while not self._closing.is_set():
                chunk = self._read_pipe(self._proc.stdout)
                if not chunk:
                    self._push_incoming(None)
                    return
                self._push_incoming(chunk)
        except Exception as exc:
            self._error = str(exc)
            self._push_incoming(None)

    def _stderr_loop(self) -> None:
        try:
            while not self._closing.is_set():
                chunk = self._read_pipe(self._proc.stderr)
                if not chunk:
                    self._stderr_eof = True
                    return
                with self._stderr_lock:
                    _append_ring(self._stderr_ring, chunk)
        except Exception:
            self._stderr_eof = True

    def _stdin_loop(self) -> None:
        while True:
            item = self._pop_outgoing()
            if item is None:
                return
            frame_bytes, receipt = item
            try:
                self._proc.stdin.write(frame_bytes)
                self._proc.stdin.flush()
                self._record_write_event({"kind": "write_ack", "receipt": receipt})
            except Exception as exc:
                self._error = str(exc)
                self._record_write_event(
                    {
                        "kind": "transport_error",
                        "code": "WRITE_FAILED",
                        "message": str(exc),
                        "receipt": receipt,
                    }
                )
                return

    @staticmethod
    def _read_pipe(stream: Any) -> bytes:
        if hasattr(stream, "fileno"):
            with contextlib.suppress(AttributeError, ValueError, OSError):
                return os.read(stream.fileno(), 65536)
        data = stream.read(65536)
        return data.encode("utf-8") if isinstance(data, str) else (data or b"")

    def _push_incoming(self, chunk: bytes | None) -> None:
        with self._cond:
            while (
                chunk is not None
                and not self._closing.is_set()
                and (
                    len(self._incoming) >= self._max_incoming_frames
                    or self._incoming_bytes + len(chunk) > self._max_incoming_bytes
                )
            ):
                self._cond.wait(timeout=0.05)
            if chunk is None:
                self._incoming.append(None)
            elif not self._closing.is_set():
                self._incoming.append(chunk)
                self._incoming_bytes += len(chunk)
            self._cond.notify_all()
        self._wake.set()

    def _pop_outgoing(self) -> tuple[bytes, str] | None:
        with self._cond:
            while not self._outgoing and not self._closing.is_set():
                self._cond.wait()
            if not self._outgoing:
                return None
            item = self._outgoing.pop(0)
            if item is None:
                return None
            self._outgoing_bytes -= len(item[0])
            self._cond.notify_all()
            return item

    def _record_write_event(self, event: dict[str, Any]) -> None:
        with contextlib.suppress(queue.Full):
            self._write_events.put_nowait(event)
        self._wake.set()

    def write(self, frame_bytes: bytes, receipt: str) -> None:
        if self._closing.is_set() or self._proc is None:
            raise PipeTransportError("PIPE_CLOSED", "Native transport is closed")
        if not isinstance(frame_bytes, (bytes, bytearray)):
            raise TypeError("frame_bytes must be bytes")
        raw = bytes(frame_bytes)
        with self._cond:
            if (
                len(self._outgoing) >= self._max_outgoing_frames
                or self._outgoing_bytes + len(raw) > self._max_outgoing_bytes
            ):
                raise PipeTransportError(
                    "PIPE_INPUT_BACKPRESSURE", "Native stdin bounded queue is full"
                )
            self._outgoing.append((raw, receipt))
            self._outgoing_bytes += len(raw)
            self._cond.notify_all()

    def drain_events(self, deadline: float) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        while True:
            self._drain_buffered(events)
            self._poll_job_exit()
            if events or self._stdout_eof:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._wake.wait(remaining)
        self._drain_buffered(events)
        return events

    def _drain_buffered(self, events: list[dict[str, Any]]) -> None:
        while not self._write_events.empty():
            events.append(self._write_events.get_nowait())
        chunks = self._pop_incoming_batch()
        for chunk in chunks:
            if chunk is None:
                self._stdout_eof = True
                events.append({"kind": "eof", "stream": "stdout"})
            else:
                for line in self._line_buffer.feed(chunk):
                    events.append(_decode_line_event(line))

    def _pop_incoming_batch(self) -> list[bytes | None]:
        with self._cond:
            batch = list(self._incoming)
            self._incoming.clear()
            self._incoming_bytes = 0
            self._cond.notify_all()
            return batch

    def _poll_job_exit(self) -> None:
        if self._job is None or self._process_exited:
            return
        if not self._job.alive():
            self._process_exited = True
            self._exit_code = self._job.exit_code()

    def interrupt_run(self) -> None:
        if self._wake is not None:
            self._wake.set()
        if self._proc is not None and hasattr(self._proc, "send_signal") and os.name == "nt":
            with contextlib.suppress(OSError, ValueError):
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)

    def stop_and_drain(self, timeout: float = 3.0) -> OwnedExitState:
        self._closing.set()
        with self._cond:
            self._outgoing.append(None)
            self._cond.notify_all()
        if self._job is not None and self._job.alive():
            with contextlib.suppress(OSError):
                self._job.terminate()
        self._close_pipes_for_unblock()
        deadline = time.monotonic() + timeout
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        self._threads_joined = all(not thread.is_alive() for thread in self._threads)
        self._drain_buffered([])
        self._poll_job_exit()
        if self._job is not None:
            with contextlib.suppress(OSError):
                self._job.close()
        if self._wake is not None and hasattr(self._wake, "close"):
            with contextlib.suppress(OSError):
                self._wake.close()
        self._cleaned = self._process_exited and self._threads_joined
        return self.owned_exit_state()

    def _close_pipes_for_unblock(self) -> None:
        if self._proc is None:
            return
        if hasattr(self._proc, "cancel_io"):
            with contextlib.suppress(OSError):
                self._proc.cancel_io()
        for pipe in (
            getattr(self._proc, "stdin", None),
            getattr(self._proc, "stdout", None),
            getattr(self._proc, "stderr", None),
        ):
            if pipe is not None and hasattr(pipe, "close"):
                with contextlib.suppress(OSError):
                    pipe.close()

    def owned_exit_state(self) -> OwnedExitState:
        output_complete = (
            self._stdout_eof
            and self._process_exited
            and self._threads_joined
            and not self._line_buffer.has_partial()
        )
        with self._stderr_lock:
            stderr_tail = bytes(self._stderr_ring).decode("utf-8", errors="replace")
        return OwnedExitState(
            eof_seen=self._stdout_eof,
            process_exited=self._process_exited,
            output_complete=output_complete,
            exit_code=self._exit_code,
            cleaned=self._cleaned,
            stderr_tail=stderr_tail,
            error=self._error,
            threads_joined=self._threads_joined,
        )


def _run_windows_gate(command: Sequence[str]) -> int:
    if os.read(0, 1) != b"G":
        return 125
    return subprocess.call(list(command), stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--windows-gate" and os.name == "nt":
        raise SystemExit(_run_windows_gate(sys.argv[2:]))
    raise SystemExit(127)
