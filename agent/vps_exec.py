"""Non-interactive VPS command executor on top of shared vps_ssh_v2 AsyncSSH.

Enforces:
- Bounded connection-phase retries only while the remote command has not been sent,
  sharing a single monotonic deadline budget across connect, auth, backoff, and exec.
- Single-dispatch fencing before entering the remote exec send window
  (create_process(..., request_pty=False, encoding=None)); never replays a command
  that may have already been dispatched.
- Cross-chunk credential redaction via SecretOutputs/ProcessOutput and bounded tail
  output collection.
- Accurate classification of remote non-zero exits, exit signals, missing exit status
  (remote_outcome_unknown, never defaulting exit_code=0), timeout, and cancellation.
- Bounded diagnostic-only cleanup that never downgrades a confirmed remote terminal state.
"""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import hashlib
import inspect
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from agent.vps_ssh import (
    VPSRuntimeBudget,
    VPSSSHConnection,
    VPSSSHConnectionError,
    classify_vps_ssh_error,
    connect_agent_vps,
)
from shared.secret_output import ProcessOutput
from shared.ssh_diagnostics import SSH_ATTEMPT_LIMIT, safe_ssh_detail
from shared.vps_transport import (
    VPS_HOST_IDENTITY_VERIFICATION,
    VPS_SSH_PROTOCOL,
    VPS_TRANSPORT_RUNTIME,
)

SSH_RETRY_DELAYS = (1.0, 2.0)
OUTPUT_TAIL_LIMIT = 131072
DEFAULT_CLEANUP_TIMEOUT = 2.0


class ExecDispatchFence:
    """In-process single-dispatch guard keyed by operation_id.

    Ensures concurrent deliveries or recovery attempts for the same operation_id
    cannot both enter the remote create_process send window.
    """

    def __init__(self, *, max_entries: int = 4096):
        self._lock = threading.Lock()
        self._claimed: dict[str, float] = {}
        self._max_entries = max(64, int(max_entries))

    def claim(self, operation_id: str) -> bool:
        if not operation_id:
            return True
        with self._lock:
            if operation_id in self._claimed:
                return False
            if len(self._claimed) >= self._max_entries:
                oldest = next(iter(self._claimed))
                del self._claimed[oldest]
            self._claimed[operation_id] = time.monotonic()
            return True

    def is_claimed(self, operation_id: str) -> bool:
        with self._lock:
            return operation_id in self._claimed


_DEFAULT_DISPATCH_FENCE = ExecDispatchFence()


def _normalize_exit_signal(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, (tuple, list)):
        if not raw:
            return None
        raw = raw[0]
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if isinstance(raw, str):
        text = raw.strip()
        if text:
            return text[:32]
    return None


def _normalize_exit_code(raw: Any) -> int | None:
    if type(raw) is int:
        return raw
    return None


async def _invoke_maybe_async(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _close_bounded_diagnostic(
    conn: VPSSSHConnection | None,
    *,
    wait_seconds: float = DEFAULT_CLEANUP_TIMEOUT,
) -> str | None:
    if conn is None:
        return None
    try:
        return await conn.close(timeout=wait_seconds)
    except BaseException as exc:
        return f"{type(exc).__name__}: {exc}"


async def _terminate_remote_process(process: Any, *, wait_seconds: float = 1.0) -> None:
    for method_name in ("terminate", "kill", "close"):
        method = getattr(process, method_name, None)
        if callable(method):
            with contextlib.suppress(BaseException):
                method()
    wait_closed = getattr(process, "wait_closed", None)
    if callable(wait_closed):
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(wait_closed(), timeout=max(0.1, float(wait_seconds)))


async def _read_stream(
    stream: Any,
    *,
    on_chunk: Callable[[bytes], Awaitable[None]],
    chunk_size: int = 65536,
) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(chunk_size)
        if not chunk:
            return
        if isinstance(chunk, str):
            chunk = chunk.encode("utf-8", errors="replace")
        await on_chunk(bytes(chunk))


def _base_receipt(
    *,
    operation_id: str,
    credential_source: str,
    authentication_state: str = "not_observed",
    authentication_method: str = "unknown",
    remote_execution_state: str = "not_started",
    exit_code: int | None = None,
    exit_signal: str | None = None,
    output: str = "",
    output_truncated: bool = False,
    output_total_bytes: int = 0,
    output_total_sha256: str = "",
    timed_out: bool = False,
    cancelled: bool = False,
    duration_ms: int = 0,
    ssh_error: str | None = None,
    ssh_diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "transport_runtime": VPS_TRANSPORT_RUNTIME,
        "transport_contract": VPS_SSH_PROTOCOL,
        "host_identity_verification_applied": VPS_HOST_IDENTITY_VERIFICATION,
        "credential_source": credential_source,
        "authentication_state": authentication_state,
        "authentication_method": authentication_method,
        "remote_execution_state": remote_execution_state,
        "exit_code": exit_code,
        "exit_signal": exit_signal,
        "output": output,
        "output_truncated": output_truncated,
        "output_total_bytes": output_total_bytes,
        "output_total_sha256": output_total_sha256,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "duration_ms": duration_ms,
        "ssh_error": ssh_error,
        "ssh_diagnostics": ssh_diagnostics or {},
    }


@dataclass(repr=False)
class _ExecutionState:
    operation_id: str
    credential_source: str
    budget: VPSRuntimeBudget
    is_cancelled: Callable[[], bool]
    is_stopping: Callable[[], bool]
    emit_stage: Callable[..., Any]
    output_filter: ProcessOutput
    max_attempts: int
    clock: Callable[[], float]
    cleanup_timeout: float
    attempts: list[dict[str, Any]] = field(default_factory=list)
    empty_sha256: str = hashlib.sha256(b"").hexdigest()


async def _wait_before_retry(
    state: _ExecutionState, attempt: int, retry_delays: tuple[float, ...], remaining: float
) -> None:
    delay_idx = min(attempt - 1, len(retry_delays) - 1) if retry_delays else 0
    raw_delay = retry_delays[delay_idx] if retry_delays else 0.0
    delay = min(max(0.0, float(raw_delay)), max(0.0, remaining))
    if delay > 0:
        state.emit_stage("ssh_retry_wait", attempt=attempt, retry_delay_ms=round(delay * 1000))
        wait_until = float(state.clock()) + delay
        while (  # noqa: ASYNC110
            float(state.clock()) < wait_until
            and not state.is_cancelled()
            and not state.is_stopping()
        ):
            await asyncio.sleep(min(0.05, max(0.0, wait_until - float(state.clock()))))


def _record_connect_failure(
    state: _ExecutionState, err: VPSSSHConnectionError, attempt: int, attempt_started: float
) -> None:
    attempt_ms = max(0, round((float(state.clock()) - attempt_started) * 1000))
    auth_state = "failed" if err.failure_stage == "authentication" else "not_observed"
    auth_legacy = "failed" if auth_state == "failed" else "not_observed"
    summary = safe_ssh_detail(
        {
            "attempt": attempt,
            "duration_ms": attempt_ms,
            "ssh_error": err.code,
            "authentication": auth_legacy,
            "authentication_state": auth_state,
            "failure_stage": err.failure_stage,
            "transport_runtime": VPS_TRANSPORT_RUNTIME,
            "transport_contract": VPS_SSH_PROTOCOL,
            "host_identity_verification_applied": VPS_HOST_IDENTITY_VERIFICATION,
            "remote_execution_state": "not_started",
            "retryable": bool(err.retryable),
        }
    )
    state.attempts.append(summary)
    state.emit_stage("ssh_attempt_finished", **summary)


def _connection_failed_receipt(
    state: _ExecutionState, last_error: VPSSSHConnectionError | None, stop_reason: str
) -> dict[str, Any]:
    err = last_error or VPSSSHConnectionError(
        "SSH_CONNECTION_FAILED",
        "SSH 连接失败，请检查目标地址、端口和网络",
        failure_stage="connection",
        retryable=False,
    )
    auth_state = "failed" if err.failure_stage == "authentication" else "not_observed"
    auth_legacy = "failed" if auth_state == "failed" else "not_observed"
    diag_message = state.output_filter.redact(err.message)
    return _base_receipt(
        operation_id=state.operation_id,
        credential_source=state.credential_source,
        authentication_state=auth_state,
        authentication_method="unknown",
        remote_execution_state="not_started",
        exit_code=None,
        exit_signal=None,
        output=diag_message,
        output_total_sha256=state.empty_sha256,
        timed_out=err.code in {"SSH_CONNECT_TIMEOUT", "SSH_TIMEOUT"} or stop_reason == "timeout",
        cancelled=state.is_cancelled(),
        duration_ms=state.budget.elapsed_ms(),
        ssh_error=err.code,
        ssh_diagnostics={
            "schema_version": 2,
            "source": "asyncssh_vps_v2",
            "transport_runtime": VPS_TRANSPORT_RUNTIME,
            "transport_contract": VPS_SSH_PROTOCOL,
            "host_identity_verification_applied": VPS_HOST_IDENTITY_VERIFICATION,
            "credential_source": state.credential_source,
            "authentication": auth_legacy,
            "authentication_state": auth_state,
            "authentication_method": "unknown",
            "remote_execution_state": "not_started",
            "failure_stage": err.failure_stage,
            "attempt_count": len(state.attempts),
            "attempt_limit": state.max_attempts,
            "attempts": state.attempts,
            "retry_policy": "pre_command_failures",
            "retry_stop_reason": stop_reason,
            "retry_exhausted": stop_reason == "attempt_limit",
            "automatic_replay": False,
            "retryable": False,
        },
    )


def _preconnect_stop(state: _ExecutionState) -> dict[str, Any] | None:
    if state.is_stopping():
        raise asyncio.CancelledError
    if state.is_cancelled():
        stop_reason = "cancelled"
        return _base_receipt(
            operation_id=state.operation_id,
            credential_source=state.credential_source,
            authentication_state="not_observed",
            authentication_method="unknown",
            remote_execution_state="not_started",
            exit_code=None,
            exit_signal=None,
            output="",
            output_total_sha256=state.empty_sha256,
            cancelled=True,
            duration_ms=state.budget.elapsed_ms(),
            ssh_error="SSH_CANCELLED",
            ssh_diagnostics={
                "schema_version": 2,
                "source": "asyncssh_vps_v2",
                "transport_runtime": VPS_TRANSPORT_RUNTIME,
                "transport_contract": VPS_SSH_PROTOCOL,
                "host_identity_verification_applied": VPS_HOST_IDENTITY_VERIFICATION,
                "credential_source": state.credential_source,
                "authentication": "not_observed",
                "authentication_state": "not_observed",
                "authentication_method": "unknown",
                "remote_execution_state": "not_started",
                "failure_stage": "connection",
                "attempt_count": len(state.attempts),
                "attempt_limit": state.max_attempts,
                "attempts": state.attempts,
                "retry_policy": "pre_command_failures",
                "retry_stop_reason": stop_reason,
                "retry_exhausted": False,
                "automatic_replay": False,
                "retryable": False,
            },
        )
    if state.budget.remaining() <= 0:
        stop_reason = "timeout"
        return _base_receipt(
            operation_id=state.operation_id,
            credential_source=state.credential_source,
            authentication_state="not_observed",
            authentication_method="unknown",
            remote_execution_state="not_started",
            exit_code=None,
            exit_signal=None,
            output="",
            output_total_sha256=state.empty_sha256,
            timed_out=True,
            duration_ms=state.budget.elapsed_ms(),
            ssh_error="SSH_TIMEOUT",
            ssh_diagnostics={
                "schema_version": 2,
                "source": "asyncssh_vps_v2",
                "transport_runtime": VPS_TRANSPORT_RUNTIME,
                "transport_contract": VPS_SSH_PROTOCOL,
                "host_identity_verification_applied": VPS_HOST_IDENTITY_VERIFICATION,
                "credential_source": state.credential_source,
                "authentication": "not_observed",
                "authentication_state": "not_observed",
                "authentication_method": "unknown",
                "remote_execution_state": "not_started",
                "failure_stage": "connection",
                "attempt_count": len(state.attempts),
                "attempt_limit": state.max_attempts,
                "attempts": state.attempts,
                "retry_policy": "pre_command_failures",
                "retry_stop_reason": stop_reason,
                "retry_exhausted": False,
                "automatic_replay": False,
                "retryable": False,
            },
        )
    return None


async def _connect_before_dispatch(
    state: _ExecutionState,
    connect_fn: Callable[..., Awaitable[VPSSSHConnection]],
    retry_delays: tuple[float, ...],
    connect_args: dict[str, Any],
) -> tuple[VPSSSHConnection | None, dict[str, Any] | None]:
    last_error = None
    stop_reason = "not_retryable"
    for attempt in range(1, state.max_attempts + 1):
        stopped = _preconnect_stop(state)
        if stopped is not None:
            return None, stopped

        state.emit_stage("ssh_attempt", attempt=attempt, attempt_limit=state.max_attempts)
        attempt_started = float(state.clock())
        try:
            connection = await connect_fn(**connect_args, budget=state.budget)
            if connection is not None:
                return connection, None
            last_error = None
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            err = classify_vps_ssh_error(exc)
            last_error = err
            _record_connect_failure(state, err, attempt, attempt_started)
            if not err.retryable:
                stop_reason = "not_retryable"
                break
            if attempt == state.max_attempts:
                stop_reason = "attempt_limit"
                break
            remaining = state.budget.remaining()
            if remaining <= 0:
                stop_reason = "timeout"
                break
            await _wait_before_retry(state, attempt, retry_delays, remaining)
    return None, _connection_failed_receipt(state, last_error, stop_reason)


async def _admit_dispatch(
    state: _ExecutionState,
    auth_method: str,
    require_authority: Callable[[], Awaitable[None] | None] | None,
    claim_exec_dispatch: Callable[[str], Awaitable[bool] | bool] | None,
    fence: ExecDispatchFence,
) -> float | dict[str, Any]:
    # Re-check authority & remaining monotonic budget AFTER auth, BEFORE send window.
    if require_authority is not None:
        await _invoke_maybe_async(require_authority)

    if state.is_stopping():
        raise asyncio.CancelledError

    if state.is_cancelled():
        return _base_receipt(
            operation_id=state.operation_id,
            credential_source=state.credential_source,
            authentication_state="accepted",
            authentication_method=auth_method,
            remote_execution_state="not_started",
            exit_code=None,
            exit_signal=None,
            output="",
            output_total_sha256=state.empty_sha256,
            cancelled=True,
            duration_ms=state.budget.elapsed_ms(),
            ssh_error="SSH_CANCELLED",
            ssh_diagnostics={
                "schema_version": 2,
                "source": "asyncssh_vps_v2",
                "transport_runtime": VPS_TRANSPORT_RUNTIME,
                "transport_contract": VPS_SSH_PROTOCOL,
                "host_identity_verification_applied": VPS_HOST_IDENTITY_VERIFICATION,
                "credential_source": state.credential_source,
                "authentication": "succeeded",
                "authentication_state": "accepted",
                "authentication_method": auth_method,
                "remote_execution_state": "not_started",
                "failure_stage": "after_authentication",
                "attempt_count": len(state.attempts) + 1,
                "attempt_limit": state.max_attempts,
                "attempts": state.attempts,
                "retry_policy": "pre_command_failures",
                "retry_stop_reason": "cancelled",
                "retry_exhausted": False,
                "automatic_replay": False,
                "retryable": False,
            },
        )

    if state.budget.remaining() <= 0:
        return _base_receipt(
            operation_id=state.operation_id,
            credential_source=state.credential_source,
            authentication_state="accepted",
            authentication_method=auth_method,
            remote_execution_state="not_started",
            exit_code=None,
            exit_signal=None,
            output="",
            output_total_sha256=state.empty_sha256,
            timed_out=True,
            duration_ms=state.budget.elapsed_ms(),
            ssh_error="SSH_TIMEOUT",
            ssh_diagnostics={
                "schema_version": 2,
                "source": "asyncssh_vps_v2",
                "transport_runtime": VPS_TRANSPORT_RUNTIME,
                "transport_contract": VPS_SSH_PROTOCOL,
                "host_identity_verification_applied": VPS_HOST_IDENTITY_VERIFICATION,
                "credential_source": state.credential_source,
                "authentication": "succeeded",
                "authentication_state": "accepted",
                "authentication_method": auth_method,
                "remote_execution_state": "not_started",
                "failure_stage": "after_authentication",
                "attempt_count": len(state.attempts) + 1,
                "attempt_limit": state.max_attempts,
                "attempts": state.attempts,
                "retry_policy": "pre_command_failures",
                "retry_stop_reason": "timeout",
                "retry_exhausted": False,
                "automatic_replay": False,
                "retryable": False,
            },
        )

    # Phase 2: Atomic single-dispatch claim BEFORE entering remote exec send window.
    if claim_exec_dispatch is not None:
        won = bool(await _invoke_maybe_async(claim_exec_dispatch, state.operation_id))
    else:
        won = fence.claim(state.operation_id)

    if not won:
        return _base_receipt(
            operation_id=state.operation_id,
            credential_source=state.credential_source,
            authentication_state="accepted",
            authentication_method=auth_method,
            remote_execution_state="remote_outcome_unknown",
            exit_code=None,
            exit_signal=None,
            output="",
            output_total_sha256=state.empty_sha256,
            duration_ms=state.budget.elapsed_ms(),
            ssh_error="SSH_COMMAND_OR_CONNECTION_FAILED",
            ssh_diagnostics={
                "schema_version": 2,
                "source": "asyncssh_vps_v2",
                "transport_runtime": VPS_TRANSPORT_RUNTIME,
                "transport_contract": VPS_SSH_PROTOCOL,
                "host_identity_verification_applied": VPS_HOST_IDENTITY_VERIFICATION,
                "credential_source": state.credential_source,
                "authentication": "succeeded",
                "authentication_state": "accepted",
                "authentication_method": auth_method,
                "remote_execution_state": "remote_outcome_unknown",
                "failure_stage": "after_authentication",
                "dispatch_already_claimed": True,
                "attempt_count": len(state.attempts) + 1,
                "attempt_limit": state.max_attempts,
                "attempts": state.attempts,
                "retry_policy": "pre_command_failures",
                "retry_stop_reason": "dispatch_already_claimed",
                "retry_exhausted": False,
                "automatic_replay": False,
                "retryable": False,
            },
        )

    # Phase 3: Remote exec send window (NEVER retried once entered).
    rem = state.budget.remaining()
    if rem <= 0:
        # Dispatch was already claimed; cannot guarantee command was not started.
        return _base_receipt(
            operation_id=state.operation_id,
            credential_source=state.credential_source,
            authentication_state="accepted",
            authentication_method=auth_method,
            remote_execution_state="remote_outcome_unknown",
            exit_code=None,
            exit_signal=None,
            output="",
            output_total_sha256=state.empty_sha256,
            timed_out=True,
            duration_ms=state.budget.elapsed_ms(),
            ssh_error="SSH_TIMEOUT",
            ssh_diagnostics={
                "schema_version": 2,
                "source": "asyncssh_vps_v2",
                "transport_runtime": VPS_TRANSPORT_RUNTIME,
                "transport_contract": VPS_SSH_PROTOCOL,
                "host_identity_verification_applied": VPS_HOST_IDENTITY_VERIFICATION,
                "credential_source": state.credential_source,
                "authentication": "succeeded",
                "authentication_state": "accepted",
                "authentication_method": auth_method,
                "remote_execution_state": "remote_outcome_unknown",
                "failure_stage": "after_authentication",
                "attempt_count": len(state.attempts) + 1,
                "attempt_limit": state.max_attempts,
                "attempts": state.attempts,
                "retry_policy": "pre_command_failures",
                "retry_stop_reason": "timeout",
                "retry_exhausted": False,
                "automatic_replay": False,
                "retryable": False,
            },
        )
    return rem


class _OutputCapture:
    def __init__(
        self,
        output_filter: ProcessOutput,
        redaction_values: list[str],
        output_limit: int,
        on_chunk_text: Callable[[str], Awaitable[None] | None] | None,
        on_output: Callable[[str, bool], Awaitable[None] | None] | None,
    ):
        self.output_filter = output_filter
        self.redaction_values = redaction_values
        self.output_limit = output_limit
        self.on_chunk_text = on_chunk_text
        self.on_output = on_output
        self.output_total_bytes = 0
        self.output_total_hash = hashlib.sha256()
        self.output_truncated = False
        self.decoded_output = ""
        self.stream_lock = asyncio.Lock()
        self.stream_evidence: dict[str, dict[str, Any]] = {}
        self.stream_finalizers: list[Callable[[], Awaitable[None]]] = []

    async def append_filtered_text(self, text: str) -> None:
        if not text:
            return
        if self.on_chunk_text is not None:
            await _invoke_maybe_async(self.on_chunk_text, text)
        if len(self.decoded_output) + len(text) > self.output_limit:
            self.output_truncated = True
        self.decoded_output = (self.decoded_output + text)[-self.output_limit :]
        if self.on_output is not None:
            await _invoke_maybe_async(self.on_output, self.decoded_output, False)

    def start_reader(self, stream: Any, name: str) -> asyncio.Task[None]:
        # SSH stdout and extended-data stderr are independent byte streams.
        # Decode and filter each before merging their display text. Keep
        # the original combined filter as a final check on joined output.
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        stream_filter = ProcessOutput(self.redaction_values)
        digest = hashlib.sha256()
        evidence = self.stream_evidence[name] = {"bytes": 0, "state": "reading"}

        async def on_raw_chunk(chunk: bytes) -> None:
            async with self.stream_lock:
                evidence["bytes"] += len(chunk)
                digest.update(chunk)
                self.output_total_bytes += len(chunk)
                self.output_total_hash.update(chunk)
                text = stream_filter.feed(decoder.decode(chunk))
                await self.append_filtered_text(self.output_filter.feed(text))

        async def finalize_stream() -> None:
            text = stream_filter.feed(decoder.decode(b"", final=True), final=True)
            await self.append_filtered_text(self.output_filter.feed(text))

        self.stream_finalizers.append(finalize_stream)

        async def read() -> None:
            try:
                await _read_stream(stream, on_chunk=on_raw_chunk)
            except asyncio.CancelledError:
                evidence["state"] = "interrupted"
                raise
            except Exception:
                evidence["state"] = "read_failed"
                raise
            else:
                evidence["state"] = "eof"
            finally:
                evidence["sha256"] = digest.hexdigest()

        return asyncio.create_task(read())

    async def flush_output(self) -> None:
        async with self.stream_lock:
            for finalize_stream in self.stream_finalizers:
                await finalize_stream()
            await self.append_filtered_text(self.output_filter.feed("", final=True))
            if self.on_output is not None:
                await _invoke_maybe_async(self.on_output, self.decoded_output, True)


async def _spawn_once(
    state: _ExecutionState, connection: VPSSSHConnection, command: str, rem: float
) -> tuple[Any, bool, bool, str | None]:
    process = None
    timed_out = False
    was_cancelled = False
    exec_error_code: str | None = None

    try:
        process = await asyncio.wait_for(
            connection.connection.create_process(
                command,
                request_pty=False,
                encoding=None,
            ),
            timeout=rem,
        )
    except TimeoutError:
        timed_out = True
        exec_error_code = "SSH_TIMEOUT"
    except asyncio.CancelledError:
        was_cancelled = True
        exec_error_code = "SSH_CANCELLED"
        if state.is_stopping():
            raise
    except Exception as exc:
        err = classify_vps_ssh_error(exc)
        exec_error_code = err.code
    return process, timed_out, was_cancelled, exec_error_code


def _spawn_failed_receipt(
    state: _ExecutionState,
    auth_method: str,
    timed_out: bool,
    was_cancelled: bool,
    exec_error_code: str | None,
) -> dict[str, Any]:
    return _base_receipt(
        operation_id=state.operation_id,
        credential_source=state.credential_source,
        authentication_state="accepted",
        authentication_method=auth_method,
        remote_execution_state="timed_out"
        if timed_out
        else ("cancelled" if was_cancelled else "remote_outcome_unknown"),
        exit_code=None,
        exit_signal=None,
        output="",
        output_total_sha256=state.empty_sha256,
        timed_out=timed_out,
        cancelled=was_cancelled,
        duration_ms=state.budget.elapsed_ms(),
        ssh_error=exec_error_code or "SSH_CHANNEL_CLOSED",
        ssh_diagnostics={
            "schema_version": 2,
            "source": "asyncssh_vps_v2",
            "transport_runtime": VPS_TRANSPORT_RUNTIME,
            "transport_contract": VPS_SSH_PROTOCOL,
            "host_identity_verification_applied": VPS_HOST_IDENTITY_VERIFICATION,
            "credential_source": state.credential_source,
            "authentication": "succeeded",
            "authentication_state": "accepted",
            "authentication_method": auth_method,
            "remote_execution_state": "timed_out"
            if timed_out
            else ("cancelled" if was_cancelled else "remote_outcome_unknown"),
            "failure_stage": "after_authentication",
            "attempt_count": len(state.attempts) + 1,
            "attempt_limit": state.max_attempts,
            "attempts": state.attempts,
            "retry_policy": "pre_command_failures",
            "retry_stop_reason": "timeout"
            if timed_out
            else ("cancelled" if was_cancelled else "not_retryable"),
            "retry_exhausted": False,
            "automatic_replay": False,
            "retryable": False,
        },
    )


async def _watch_process_cancel(state: _ExecutionState, waiter: asyncio.Task[None]) -> None:
    while not waiter.done():
        if state.is_cancelled() or state.is_stopping():
            return
        await asyncio.sleep(0.05)


async def _wait_process(
    state: _ExecutionState,
    process: Any,
    waiter: asyncio.Task[None],
) -> tuple[bool, bool]:
    timed_out = was_cancelled = False
    cancel_watcher = asyncio.create_task(_watch_process_cancel(state, waiter))
    try:
        rem = state.budget.remaining()
        if rem <= 0:
            timed_out = True
        else:
            done, _ = await asyncio.wait(
                {waiter, cancel_watcher},
                timeout=rem,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                timed_out = True
            elif cancel_watcher in done and not waiter.done():
                was_cancelled = True
            else:
                # Waiter finished; observe any exception from process.wait()
                with contextlib.suppress(BaseException):
                    waiter.result()
    except asyncio.CancelledError:
        was_cancelled = True
        if state.is_stopping():
            await _terminate_remote_process(process, wait_seconds=min(1.0, state.cleanup_timeout))
            raise
    finally:
        if cancel_watcher is not None and not cancel_watcher.done():
            cancel_watcher.cancel()
            with contextlib.suppress(BaseException):
                await cancel_watcher
    return timed_out, was_cancelled


async def _drain_process(
    state: _ExecutionState, readers: list[asyncio.Task[None]], waiter: asyncio.Task[None]
) -> None:
    # Drain remaining buffered stdout/stderr within a bounded tail window.
    try:
        await asyncio.wait_for(
            asyncio.gather(*readers, return_exceptions=True),
            timeout=min(2.0, max(0.2, state.cleanup_timeout)),
        )
    except TimeoutError:
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)

    if not waiter.done():
        waiter.cancel()
        with contextlib.suppress(BaseException):
            await waiter


async def _collect_process(
    state: _ExecutionState,
    process: Any,
    capture: _OutputCapture,
) -> tuple[bool, bool]:
    stdout_stream = getattr(process, "stdout", None)
    stderr_stream = getattr(process, "stderr", None)
    readers = [capture.start_reader(stdout_stream, "stdout")]
    if stderr_stream is not None and stderr_stream is not stdout_stream:
        readers.append(capture.start_reader(stderr_stream, "stderr"))

    async def _wait_exit() -> None:
        # wait() also consumes stdout and stderr through communicate().
        await process.wait_closed()

    waiter = asyncio.create_task(_wait_exit())
    try:
        timed_out, was_cancelled = await _wait_process(state, process, waiter)
        if timed_out or was_cancelled:
            await _terminate_remote_process(process, wait_seconds=min(1.0, state.cleanup_timeout))
        await _drain_process(state, readers, waiter)
        await capture.flush_output()
        return timed_out, was_cancelled
    finally:
        tasks = [task for task in (*readers, waiter) if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _finished_receipt(
    state: _ExecutionState,
    process: Any,
    capture: _OutputCapture,
    auth_method: str,
    timed_out: bool,
    was_cancelled: bool,
    exec_error_code: str | None,
) -> dict[str, Any]:
    raw_exit_status = getattr(process, "exit_status", None)
    if raw_exit_status is None:
        raw_exit_status = getattr(process, "returncode", None)
    exit_code = _normalize_exit_code(raw_exit_status)
    exit_signal = _normalize_exit_signal(getattr(process, "exit_signal", None))

    # Determine monotonic remote execution state.
    # Confirmed exit_code or exit_signal ALWAYS yields remote_execution_state="completed"
    # and is never downgraded by subsequent cleanup issues.
    if exit_code is not None or exit_signal is not None:
        remote_state = "completed"
        ssh_error_code = None
        final_stop_reason = "succeeded" if exit_code == 0 else "not_retryable"
    elif was_cancelled or state.is_cancelled():
        remote_state = "cancelled"
        was_cancelled = True
        ssh_error_code = "SSH_CANCELLED"
        final_stop_reason = "cancelled"
    elif timed_out:
        remote_state = "timed_out"
        ssh_error_code = "SSH_TIMEOUT"
        final_stop_reason = "timeout"
    else:
        remote_state = "remote_outcome_unknown"
        ssh_error_code = exec_error_code or "SSH_CHANNEL_CLOSED"
        final_stop_reason = "not_retryable"

    summary = safe_ssh_detail(
        {
            "attempt": len(state.attempts) + 1,
            "exit_code": exit_code,
            "duration_ms": state.budget.elapsed_ms(),
            "ssh_error": ssh_error_code,
            "authentication": "succeeded",
            "authentication_state": "accepted",
            "authentication_method": auth_method,
            "transport_runtime": VPS_TRANSPORT_RUNTIME,
            "transport_contract": VPS_SSH_PROTOCOL,
            "host_identity_verification_applied": VPS_HOST_IDENTITY_VERIFICATION,
            "remote_execution_state": remote_state,
            "failure_stage": "after_authentication" if ssh_error_code else None,
            "retryable": False,
        }
    )
    state.attempts.append(summary)
    state.emit_stage("ssh_attempt_finished", **summary)

    confirmed_result = _base_receipt(
        operation_id=state.operation_id,
        credential_source=state.credential_source,
        authentication_state="accepted",
        authentication_method=auth_method,
        remote_execution_state=remote_state,
        exit_code=exit_code,
        exit_signal=exit_signal,
        output=capture.decoded_output,
        output_truncated=capture.output_truncated,
        output_total_bytes=capture.output_total_bytes,
        output_total_sha256=capture.output_total_hash.hexdigest(),
        timed_out=timed_out and remote_state != "completed",
        cancelled=was_cancelled and remote_state != "completed",
        duration_ms=state.budget.elapsed_ms(),
        ssh_error=ssh_error_code,
        ssh_diagnostics={
            "schema_version": 2,
            "source": "asyncssh_vps_v2",
            "transport_runtime": VPS_TRANSPORT_RUNTIME,
            "transport_contract": VPS_SSH_PROTOCOL,
            "host_identity_verification_applied": VPS_HOST_IDENTITY_VERIFICATION,
            "credential_source": state.credential_source,
            "authentication": "succeeded",
            "authentication_state": "accepted",
            "authentication_method": auth_method,
            "remote_execution_state": remote_state,
            "failure_stage": "after_authentication" if ssh_error_code else None,
            "attempt_count": len(state.attempts),
            "attempt_limit": state.max_attempts,
            "attempts": state.attempts,
            "retry_policy": "pre_command_failures",
            "retry_stop_reason": final_stop_reason,
            "retry_exhausted": False,
            "automatic_replay": False,
            "retryable": False,
        },
    )
    confirmed_result["output_capture"] = {
        "complete": all(item["state"] == "eof" for item in capture.stream_evidence.values()),
        "streams": capture.stream_evidence,
    }
    return confirmed_result


async def execute_vps_command(
    *,
    operation_id: str,
    host: str,
    port: int,
    username: str,
    password: str | None,
    command: str,
    timeout_seconds: float,
    credential_source: str = "saved_vps",
    connect_timeout: float = 10.0,
    login_timeout: float = 15.0,
    keepalive_interval: float | None = 15.0,
    keepalive_count_max: int = 3,
    attempt_limit: int = SSH_ATTEMPT_LIMIT,
    retry_delays: tuple[float, ...] = SSH_RETRY_DELAYS,
    cleanup_timeout: float = DEFAULT_CLEANUP_TIMEOUT,
    output_limit: int = OUTPUT_TAIL_LIMIT,
    extra_redaction_values: list[str] | tuple[str, ...] = (),
    cancelled: Callable[[], bool] | None = None,
    stopping: Callable[[], bool] | None = None,
    record: Callable[..., Any] | None = None,
    on_output: Callable[[str, bool], Awaitable[None] | None] | None = None,
    on_chunk_text: Callable[[str], Awaitable[None] | None] | None = None,
    claim_exec_dispatch: Callable[[str], Awaitable[bool] | bool] | None = None,
    require_authority: Callable[[], Awaitable[None] | None] | None = None,
    dispatch_fence: ExecDispatchFence | None = None,
    connector: Callable[..., Awaitable[VPSSSHConnection]] | None = None,
    ssh_module: Any = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Execute a single non-interactive command over vps_ssh_v2 AsyncSSH."""
    budget = VPSRuntimeBudget.start(
        timeout_seconds,
        connect_timeout=connect_timeout,
        login_timeout=login_timeout,
        ssh_keepalive_interval=keepalive_interval,
        ssh_keepalive_count=keepalive_count_max,
        clock=clock,
    )
    redaction_values = [v for v in (password, *extra_redaction_values) if isinstance(v, str) and v]
    state = _ExecutionState(
        operation_id=operation_id,
        credential_source=credential_source,
        budget=budget,
        is_cancelled=cancelled or (lambda: False),
        is_stopping=stopping or (lambda: False),
        emit_stage=record or (lambda *_a, **_kw: None),
        output_filter=ProcessOutput(redaction_values),
        max_attempts=max(1, min(SSH_ATTEMPT_LIMIT, int(attempt_limit))),
        clock=clock,
        cleanup_timeout=cleanup_timeout,
    )
    connection, stopped = await _connect_before_dispatch(
        state,
        connector or connect_agent_vps,
        retry_delays,
        dict(host=host, port=port, username=username, password=password, ssh_module=ssh_module),
    )
    if stopped is not None:
        return stopped
    assert connection is not None

    auth_method = getattr(connection, "authentication_method", "unknown") or "unknown"
    if auth_method not in {"password", "keyboard-interactive", "none", "unknown"}:
        auth_method = "unknown"
    confirmed_result = None
    try:
        admission = await _admit_dispatch(
            state,
            auth_method,
            require_authority,
            claim_exec_dispatch,
            dispatch_fence if dispatch_fence is not None else _DEFAULT_DISPATCH_FENCE,
        )
        if isinstance(admission, dict):
            return admission
        capture = _OutputCapture(
            state.output_filter,
            redaction_values,
            output_limit,
            on_chunk_text,
            on_output,
        )
        process, timed_out, was_cancelled, exec_error_code = await _spawn_once(
            state,
            connection,
            command,
            admission,
        )
        if process is None:
            return _spawn_failed_receipt(
                state,
                auth_method,
                timed_out,
                was_cancelled,
                exec_error_code,
            )
        timed_out, was_cancelled = await _collect_process(state, process, capture)
        confirmed_result = _finished_receipt(
            state,
            process,
            capture,
            auth_method,
            timed_out,
            was_cancelled,
            exec_error_code,
        )
        return confirmed_result
    finally:
        # Cleanup can annotate only a confirmed receipt; it never downgrades it.
        cleanup_warning = await _close_bounded_diagnostic(connection, wait_seconds=cleanup_timeout)
        if cleanup_warning and confirmed_result is not None:
            confirmed_result["ssh_diagnostics"]["cleanup_warning"] = state.output_filter.redact(
                cleanup_warning
            )
