from __future__ import annotations

import asyncio
import sqlite3
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from hub.mcp_request_audit import error_headers
from shared.util import DevError


DEFAULT_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; "
               "base-uri 'self'; frame-ancestors 'none'; form-action 'self'")
HSTS_SECONDS = 365 * 24 * 3600


def secure_response(request, response):
    """Use the same policy for normal responses and the outer 500 handler."""
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # Attachment routes deliberately use a stricter sandbox/default-src policy.
    # A general panel policy must never replace that route's explicit boundary.
    response.headers.setdefault("Content-Security-Policy", DEFAULT_CSP)
    if request.url.scheme == "https":
        # Trust the ASGI scheme, not a client-supplied forwarding header. The
        # server only applies proxy headers from FORWARDED_ALLOW_IPS.
        response.headers["Strict-Transport-Security"] = f"max-age={HSTS_SECONDS}"
    if request.url.path.startswith(("/api", "/oauth")):
        directives = {part.strip().lower() for part in response.headers.get("Cache-Control", "").split(",")}
        if "no-store" not in directives:
            response.headers["Cache-Control"] = "no-store"
    return response


class BodyLimit:
    """Bound body reads and retained bytes before any endpoint can act."""
    MAX_INFLIGHT = 256
    MAX_BUFFERED_BYTES = 64 * 1024 * 1024
    READ_TIMEOUT = 30

    def __init__(self, app, limit=6 * 1024 * 1024, *,
                 max_inflight=MAX_INFLIGHT, max_buffered_bytes=MAX_BUFFERED_BYTES,
                 timeout=READ_TIMEOUT):
        self.app, self.limit = app, limit
        self.max_inflight, self.max_buffered_bytes, self.timeout = max_inflight, max_buffered_bytes, timeout
        self.inflight = 0
        self.buffered_bytes = 0

    @staticmethod
    def busy():
        return JSONResponse({"error": {"code": "HTTP_BODY_BUSY", "message": "请求体接收繁忙，请稍后重试",
                                       "admitted": False, "retry_after_seconds": 1}},
                            status_code=429, headers={"Retry-After": "1"})

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = self.limit + 1
        if length < 0 or length > self.limit:
            return await JSONResponse({"error": {"code": "BODY_TOO_LARGE", "message": "请求超过 6 MiB"}}, status_code=413)(scope, receive, send)
        if self.inflight >= self.max_inflight:
            return await self.busy()(scope, receive, send)
        # Count the reading phase independently from handler/stream lifetime.
        # These counters are updated without an await on the one ASGI loop.
        self.inflight += 1
        body = bytearray()
        error = None
        deadline = asyncio.get_running_loop().time() + self.timeout
        try:
            try:
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    message = await asyncio.wait_for(receive(), max(0, remaining))
                    if message["type"] == "http.disconnect":
                        return
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > self.limit:
                        error = JSONResponse({"error": {"code": "BODY_TOO_LARGE", "message": "请求超过 6 MiB"}}, status_code=413)
                        break
                    if self.buffered_bytes + len(chunk) > self.max_buffered_bytes:
                        error = self.busy()
                        break
                    body.extend(chunk)
                    self.buffered_bytes += len(chunk)
                    if not message.get("more_body", False):
                        break
            except asyncio.TimeoutError:
                error = JSONResponse({"error": {"code": "BODY_TIMEOUT", "message": "请求体接收超时，请重新发送",
                                               "admitted": False}}, status_code=408)
            finally:
                self.inflight -= 1
            if error is not None:
                return await error(scope, receive, send)
            sent = False

            async def buffered_receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await receive()

            await self.app(scope, buffered_receive, send)
        finally:
            # Keep byte reservations until consumers release the request, even
            # when the endpoint raises or the client cancels after body parsing.
            self.buffered_bytes -= len(body)



def install_http_behaviors(app):
    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        # Preserve the framework default 500 body and exception propagation.
        # ServerErrorMiddleware sits outside user middleware, so attach the
        # already-generated ID here as well. Never expose exception text.
        headers = error_headers(request.scope) if request.url.path == "/mcp" else {}
        return secure_response(request, PlainTextResponse("Internal Server Error", status_code=500, headers=headers))

    @app.exception_handler(DevError)
    async def dev_error(request, exc: DevError):
        # OAuth authorization errors may be viewed as top-level browser pages.
        # Be explicit for browsers that otherwise guess a locale encoding.
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, **exc.details}},
                            status_code=exc.status, media_type="application/json; charset=utf-8")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Pydantic's default output includes raw password/token input; don't reflect it.
        issues = [{"field": ".".join(map(str, x["loc"])), "message": x["msg"]} for x in exc.errors()]
        return JSONResponse({"error": {"code": "INVALID_ARGUMENTS", "message": "参数校验失败", "issues": issues}}, status_code=422)

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request, exc):
        return JSONResponse({"error": {"code": "CONFLICT", "message": "名称已存在，或关联记录已改变，请刷新后重试"}}, status_code=409)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        return secure_response(request, await call_next(request))
