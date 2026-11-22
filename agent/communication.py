"""One owned, read-only metadata sampler for the Agent process.

The transport reads memory only. A slow filesystem observation may become stale,
but it never starts another scan or holds up a heartbeat or a reconnect. Exact
observation times are diagnostic-only: changing timestamps must not turn every
unchanged heartbeat into a Hub devices-table write.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import os
import sys
import time
from pathlib import Path


async def _settle_owned(future):
    """Finish the actual owned work even if its waiter is cancelled repeatedly."""
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            if future.cancelled():
                raise
            cancelled = True
        except Exception:
            break
    if cancelled:
        # Retrieve a failure without replacing the caller's cancellation.
        if not future.cancelled():
            future.exception()
        raise asyncio.CancelledError
    return future.result()


# Fixed child protocol: no credentials, executable inputs, or socket handles.
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
_WORKER_ENV_KEYS = (
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "SYSTEMDRIVE",
    "PROGRAMDATA",
    "XDG_CONFIG_HOME",
)
_WORKER_BOOT = (
    "import runpy, sys; sys.path.insert(0, sys.argv[1]); "
    "runpy.run_module('agent.communication', run_name='__main__')"
)


class _WorkerFailure(Exception):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(kind)


def _encode_message(data):
    payload = json.dumps(data, ensure_ascii=True).encode("ascii")
    if len(payload) > _MAX_MESSAGE_BYTES:
        raise ValueError("Metadata message exceeds its size limit")
    return payload


def _observe(request):
    # Hydrate only immutable Agent metadata needed for read-only description.
    # Constructing BuildIdentity normally would rehash runtime source first.
    from agent.lifecycle import LifecycleManager
    from shared.build_info import BuildIdentity

    build = BuildIdentity.__new__(BuildIdentity)
    build.component = request["component"]
    build.loaded = request["runtime"]
    build.catalog_sha256 = request["catalog_sha256"]
    lifecycle = LifecycleManager(Path(request["config_path"]), Path(request["state_dir"]), dict)
    current_build = build.describe()
    management = lifecycle.describe()
    actions = []
    if management.get("control_ready"):
        actions.extend(("agent_restart", "agent_uninstall"))
    if management.get("update_ready"):
        actions.append("agent_update")
    return {"build": current_build, "management": management, "device_actions": sorted(actions)}


def _worker_main():
    try:
        payload = sys.stdin.buffer.read(_MAX_MESSAGE_BYTES + 1)
        if len(payload) > _MAX_MESSAGE_BYTES:
            raise ValueError("Metadata request exceeds its size limit")
        result = _encode_message({"sample": _observe(json.loads(payload))})
    except Exception as exc:
        result = _encode_message({"error_type": type(exc).__name__})
    sys.stdout.buffer.write(result)


async def _exchange(worker, request):
    try:
        worker.stdin.write(request)
        await worker.stdin.drain()
    finally:
        worker.stdin.close()
    try:
        await worker.stdout.readexactly(_MAX_MESSAGE_BYTES + 1)
    except asyncio.IncompleteReadError as exc:
        response = exc.partial
    else:
        raise ValueError("Metadata response exceeds its size limit")
    if await worker.wait() != 0:
        raise _WorkerFailure("WorkerExit")
    message = json.loads(response)
    if "error_type" in message:
        kind = message["error_type"]
        if not isinstance(kind, str) or not kind.isidentifier() or len(kind) > 80:
            raise ValueError("Invalid metadata error type")
        raise _WorkerFailure(kind)
    return message["sample"]


class MetadataSampler:
    """Single-flight source and management observations, across socket epochs."""

    def __init__(
        self,
        build,
        lifecycle,
        *,
        refresh_interval=12.0,
        stale_after=36.0,
        read_timeout=10.0,
        stop_grace=1.0,
    ):
        self.build, self.lifecycle = build, lifecycle
        self.refresh_interval = refresh_interval
        self.stale_after = stale_after
        self.clock = time.monotonic
        self.wall_clock = time.time
        self.read_timeout = read_timeout
        self.stop_grace = stop_grace
        self._worker = None
        self._close_task = None
        self._refresh_task = None
        self._closed = False
        self._wake = asyncio.Event()
        self._sample = None
        self._started_mono = None
        self._started_at = None
        self._completed_at = None
        self._sample_started_mono = None
        self._error_type = None
        self._initial_build = {
            "component": build.component,
            "runtime": copy.deepcopy(build.loaded),
            "disk": {"version": None, "source_sha256": None, "errors": ["not_observed"]},
            "restart_required": False,
            "source_verified": False,
            "catalog_sha256": build.catalog_sha256,
            "note": "Disk and management observations run separately from transport liveness.",
        }

    def _request(self):
        return _encode_message(
            {
                "component": self.build.component,
                "runtime": self.build.loaded,
                "catalog_sha256": self.build.catalog_sha256,
                "config_path": str(self.lifecycle.config_path),
                "state_dir": str(self.lifecycle.state_dir),
            }
        )

    def _worker_command(self):
        return (
            sys.executable,
            "-I",
            "-B",
            "-c",
            _WORKER_BOOT,
            str(Path(__file__).parent.parent),
        )

    async def _stop_worker(self, worker):
        if worker.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                worker.kill()
            try:
                await asyncio.wait_for(worker.wait(), self.stop_grace)
            except TimeoutError:
                # A kernel-stalled child may not die yet even after SIGKILL.
                # Do not join it on Agent shutdown or launch a replacement on top
                # of it. Its asyncio child watcher reaps it when the OS permits.
                return
        if self._worker is worker:
            self._worker = None

    async def _read(self):
        if self._worker is not None:
            if self._worker.returncode is None:
                raise _WorkerFailure("WorkerStopPending")
            self._worker = None
        request = self._request()
        worker = await asyncio.create_subprocess_exec(
            *self._worker_command(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={key: os.environ[key] for key in _WORKER_ENV_KEYS if key in os.environ},
        )
        self._worker = worker
        try:
            return await _exchange(worker, request)
        finally:
            await self._stop_worker(worker)

    async def _collect(self):
        self._started_mono = self.clock()
        self._started_at = self.wall_clock()
        try:
            async with asyncio.timeout(self.read_timeout):
                sample = await self._read()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._closed:
                self._error_type = (
                    exc.kind if isinstance(exc, _WorkerFailure) else type(exc).__name__
                )
        else:
            if not self._closed:
                self._sample = sample
                self._sample_started_mono = self._started_mono
                self._completed_at = self.wall_clock()
                self._error_type = None

    async def refresh(self):
        if self._closed:
            return self.snapshot()
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._collect(), name="agent-metadata-read")
        # A caller or connection disappearing does not relinquish the owned
        # worker. run()/close() retain it, so another request cannot overlap it.
        try:
            await asyncio.shield(self._refresh_task)
        except asyncio.CancelledError:
            if not self._closed or asyncio.current_task().cancelling():
                raise
        return self.snapshot()

    def _state(self):
        if self._error_type is not None:
            return "failed"
        if self._sample is None:
            return "pending"
        if self.clock() - self._sample_started_mono > self.stale_after:
            return "stale"
        return "fresh"

    def snapshot(self):
        state = self._state()
        result = (
            copy.deepcopy(self._sample)
            if self._sample is not None
            else {
                "build": copy.deepcopy(self._initial_build),
                "management": {},
                "device_actions": [],
            }
        )
        result["build"]["observation_state"] = state
        if state != "fresh":
            result["build"]["source_verified"] = False
            result["device_actions"] = []
        # restart_required retains the last actual comparison; stale does not
        # claim that a restart is needed or that current disk bytes are verified.
        return result

    def observation(self):
        """Explicit diagnostics only; never part of the steady heartbeat."""
        return {
            "state": self._state(),
            "sample_started_at": self._started_at,
            "checked_at": self._completed_at,
            "age_seconds": max(0.0, self.clock() - self._sample_started_mono)
            if self._sample_started_mono is not None
            else None,
            "in_flight": self._refresh_task is not None and not self._refresh_task.done(),
            "error_type": self._error_type,
        }

    async def run(self, stop_event):
        while not self._closed and not stop_event.is_set():
            self._wake.clear()
            await self.refresh()
            if self._closed or stop_event.is_set():
                break
            try:
                await asyncio.wait_for(self._wake.wait(), self.refresh_interval)
            except asyncio.TimeoutError:
                pass

    async def _finish_close(self):
        if self._refresh_task is not None:
            if not self._refresh_task.done():
                self._refresh_task.cancel()
            await asyncio.gather(self._refresh_task, return_exceptions=True)

    async def close(self):
        self._closed = True
        self._wake.set()
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._finish_close(), name="agent-metadata-close"
            )
        await _settle_owned(self._close_task)


if __name__ == "__main__":
    _worker_main()
