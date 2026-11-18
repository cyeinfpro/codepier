"""Per-router, credential-scoped waits. A cancelled wait never cancels durable work."""

import asyncio
from contextvars import ContextVar
import hashlib

from fastapi.responses import StreamingResponse
from shared.util import DevError

wait_cancelled = ContextVar("mcp_wait_cancelled", default=None)


class Streams:
    def __init__(self):
        self.active = {}

    @staticmethod
    def key(request, principal, identifier):
        credential = hashlib.sha256(request.headers.get("authorization", "").encode()).digest()
        return principal.space_id, principal.user_id, principal.grant_id, credential, identifier

    def begin(self, request, principal, identifier):
        key = self.key(request, principal, identifier)
        if key in self.active:
            raise DevError("DUPLICATE_REQUEST", "此连接正在处理同一请求编号", 409)
        if len(self.active) >= 256 or sum(k[:-1] == key[:-1] for k in self.active) >= 16:
            raise DevError("TOO_MANY_STREAMS", "当前连接等待中的请求过多", 429)
        stop = asyncio.Event()
        self.active[key] = stop
        wait_cancelled.set(stop)
        task = asyncio.current_task()

        def finished(done):
            self.active.pop(key, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return stop

    def cancel(self, request, principal, identifier):
        stop = self.active.get(self.key(request, principal, identifier))
        if stop is not None:
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

        async def events():
            try:
                yield ": keepalive\n\n"
                while not work.done():
                    done, _ = await asyncio.wait({work}, timeout=5)
                    if not done:
                        yield ": keepalive\n\n"
                response = work.result()
                yield b"event: message\ndata: " + response.body + b"\n\n"
            finally:
                stop.set()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "private, no-store",
                "X-Accel-Buffering": "no",
                "Vary": "Authorization, MCP-Protocol-Version, Accept",
                "Access-Control-Allow-Origin": request.headers.get("origin") or "*",
            },
        )
