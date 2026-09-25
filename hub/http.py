from __future__ import annotations

import sqlite3
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from shared.util import DevError


class BodyLimit:
    """Bound JSON/form bodies before validation, including chunked requests."""
    def __init__(self, app, limit=6 * 1024 * 1024):
        self.app, self.limit = app, limit

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
        # FastAPI translates receive exceptions during body parsing into a
        # generic 400. Read bounded chunks here so overflow reliably stays 413
        # and handlers cannot perform side effects before the cap is checked.
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.limit:
                return await JSONResponse({"error": {"code": "BODY_TOO_LARGE", "message": "请求超过 6 MiB"}}, status_code=413)(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        sent = False
        async def buffered_receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()
        await self.app(scope, buffered_receive, send)



def install_http_behaviors(app):
    @app.exception_handler(DevError)
    async def dev_error(request, exc: DevError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, **exc.details}}, status_code=exc.status)

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
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"
        if request.url.path.startswith(("/api", "/oauth")):
            response.headers["Cache-Control"] = "no-store"
        return response
