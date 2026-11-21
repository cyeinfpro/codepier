"""Per-router, credential-scoped waits. A cancelled wait never cancels durable work."""

import asyncio
from contextvars import ContextVar
import hashlib
import json
import os

from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from shared.mcp_presentation import error_view
from shared.config import env_float, env_int
from shared.util import DevError

wait_cancelled = ContextVar("mcp_wait_cancelled", default=None)

DEFAULT_PER_PRINCIPAL_STREAMS = 64
DEFAULT_GLOBAL_STREAMS = 256
DEFAULT_KEEPALIVE_INTERVAL = 5.0
DEFAULT_SSE_GRACE_SECONDS = 5.0


def _env_int(name, default, *, minimum=1, maximum=65536):
    # An empty Compose value means "use the default"; anything else is validated
    # by the shared parser and an invalid value stops startup instead of being
    # silently replaced.
    if os.getenv(name, "").strip() == "":
        return default
    return env_int(name, default, minimum, maximum)


def _env_float(name, default, *, minimum=0.01, maximum=300.0):
    if os.getenv(name, "").strip() == "":
        return default
    return env_float(name, default, minimum, maximum)


class Streams:
    def __init__(
        self,
        *,
        per_principal_limit=None,
        global_limit=None,
        keepalive_interval=None,
        grace_seconds=None,
    ):
        self.active = {}
        self._tasks = {}
        self._stop_meta = {}
        self._pending_cancels = {}
        self.per_principal_limit = (
            max(1, int(per_principal_limit))
            if per_principal_limit is not None
            else _env_int("CODEPIER_MCP_STREAMS_PER_PRINCIPAL", DEFAULT_PER_PRINCIPAL_STREAMS)
        )
        self.global_limit = (
            max(1, int(global_limit))
            if global_limit is not None
            else _env_int("CODEPIER_MCP_MAX_STREAMS", DEFAULT_GLOBAL_STREAMS)
        )
        self.keepalive_interval = (
            max(0.01, float(keepalive_interval))
            if keepalive_interval is not None
            else _env_float("CODEPIER_MCP_SSE_KEEPALIVE_INTERVAL", DEFAULT_KEEPALIVE_INTERVAL)
        )
        self.grace_seconds = (
            max(0.0, float(grace_seconds))
            if grace_seconds is not None
            else _env_float(
                "CODEPIER_MCP_SSE_GRACE_SECONDS",
                DEFAULT_SSE_GRACE_SECONDS,
                minimum=0.0,
                maximum=300.0,
            )
        )

    @staticmethod
    def key(request, principal, identifier):
        credential = hashlib.sha256(request.headers.get("authorization", "").encode()).digest()
        return principal.space_id, principal.user_id, principal.grant_id, credential, identifier

    def _reap_stale(self):
        for key, stop in list(self.active.items()):
            task = self._tasks.get(key)
            if task is not None and task.done():
                self._release_key(key, stop)

    def _release_key(self, key, stop=None):
        existing = self.active.get(key)
        if stop is not None and existing is not stop:
            return
        self.active.pop(key, None)
        self._tasks.pop(key, None)
        if existing is not None:
            self._stop_meta.pop(id(existing), None)

    def register_cancel(self, request, principal, identifier, stop):
        """Track a pre-SSE wait Event so notifications/cancelled wakes even before SSE upgrade."""
        key = self.key(request, principal, identifier)
        bucket = self._pending_cancels.setdefault(key, set())
        bucket.add(stop)
        return key, stop

    def unregister_cancel(self, token):
        if token is None:
            return
        key, stop = token
        bucket = self._pending_cancels.get(key)
        if bucket is not None:
            bucket.discard(stop)
            if not bucket:
                self._pending_cancels.pop(key, None)

    def begin(self, request, principal, identifier, *, stop=None, cancel_token=None, task=None):
        self._reap_stale()
        key = self.key(request, principal, identifier)
        if key in self.active:
            raise DevError("DUPLICATE_REQUEST", "此连接正在处理同一请求编号", 409)
        principal_count = sum(
            k[:-1] == key[:-1] or (key[2] is not None and k[:3] == key[:3]) for k in self.active
        )
        if len(self.active) >= self.global_limit or principal_count >= self.per_principal_limit:
            raise DevError("TOO_MANY_STREAMS", "当前连接等待中的请求过多", 429)
        if cancel_token is not None:
            self.unregister_cancel(cancel_token)
        stop = stop or asyncio.Event()
        task = task or asyncio.current_task()
        self.active[key] = stop
        self._tasks[key] = task
        self._stop_meta[id(stop)] = (key, identifier)
        wait_cancelled.set(stop)

        def finished(done):
            self._release_key(key, stop)
            if not done.cancelled():
                done.exception()

        if task is not None:
            task.add_done_callback(finished)
        return stop

    def cancel(self, request, principal, identifier):
        key = self.key(request, principal, identifier)
        stop = self.active.get(key)
        if stop is not None:
            stop.set()
        for pending in tuple(self._pending_cancels.get(key, ())):
            pending.set()

    @staticmethod
    def _jsonrpc_error(identifier, code, message, data=None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return json.dumps(
            {"jsonrpc": "2.0", "id": identifier, "error": error},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    async def _response_events(self, work, stop, identifier):
        try:
            yield b": keepalive\n\n"
            while not work.done():
                done, _ = await asyncio.wait({work}, timeout=self.keepalive_interval)
                if not done:
                    yield b": keepalive\n\n"
            try:
                response = work.result()
            except asyncio.CancelledError:
                payload = self._jsonrpc_error(identifier, -32000, "Request cancelled")
                yield b"event: message\ndata: " + payload + b"\n\n"
            except DevError as exc:
                payload = self._jsonrpc_error(
                    identifier,
                    -32000,
                    error_view(exc.message),
                    {"code": exc.code},
                )
                yield b"event: message\ndata: " + payload + b"\n\n"
            except Exception:
                payload = self._jsonrpc_error(identifier, -32603, "Internal error")
                yield b"event: message\ndata: " + payload + b"\n\n"
            else:
                yield b"event: message\ndata: " + response.body + b"\n\n"
        finally:
            stop.set()

    async def respond(self, request, handler):
        ready = asyncio.get_running_loop().create_future()
        work = asyncio.create_task(handler(request, ready))
        try:
            done, _ = await asyncio.wait({ready, work}, return_when=asyncio.FIRST_COMPLETED)
            if work in done:
                return work.result()
        except BaseException:
            if ready.done() and not ready.cancelled():
                ready.result().set()
            else:
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            raise
        finally:
            if not ready.done():
                ready.cancel()

        stop = ready.result()
        meta = self._stop_meta.get(id(stop))
        _, identifier = meta if meta is not None else (None, None)

        return StreamingResponse(
            self._response_events(work, stop, identifier),
            background=BackgroundTask(stop.set),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "private, no-store",
                "X-Accel-Buffering": "no",
                "Vary": "Authorization, MCP-Protocol-Version, Accept",
                "Access-Control-Allow-Origin": request.headers.get("origin") or "*",
            },
        )
