from __future__ import annotations
from fastapi import APIRouter
from hub.db_worker import database_endpoint
from hub.api.context import HubContext
import asyncio
from fastapi import Request
from fastapi.responses import JSONResponse
from hub.auth import SESSION_SECONDS
from shared.crypto import digest, password_hash, password_verify
from shared.util import DevError, VERSION
from hub.api.models import Login, PasswordInput



def make_accounts_router(context: HubContext):
    router = APIRouter()
    store, runtime, auth = context.store, context.runtime, context.auth
    config, maintenance = context.config, context.maintenance
    public_url, BASE = context.public_url, context.base

    @router.get("/api/session")
    @database_endpoint(store)
    def session(request: Request):
        configured = bool(store.one("SELECT id FROM users LIMIT 1"))
        try:
            row = auth.session(request)
            return {"authenticated": True, "configured": configured, "username": row["username"], "user_id": row["user_id"], "csrf": row["csrf"], "version": VERSION}
        except DevError:
            return {"authenticated": False, "configured": configured, "version": VERSION}

    @router.post("/api/login")
    async def login(request: Request, body: Login):
        value = await auth.login(request, body.username, body.password)
        response = JSONResponse({"username": value["username"], "user_id": value["user_id"], "csrf": value["csrf"]})
        response.set_cookie("rd_session", value["cookie"], httponly=True, samesite="lax", secure=request.url.scheme == "https", max_age=SESSION_SECONDS, path="/")
        return response

    @router.post("/api/logout")
    @database_endpoint(store)
    def logout(request: Request):
        principal = auth.admin(request, True)
        store.execute("DELETE FROM sessions WHERE id_hash=?", (digest(request.cookies.get("rd_session", "")),))
        store.audit(principal.actor, "auth.logout")
        response = JSONResponse({"ok": True})
        response.delete_cookie("rd_session", path="/")
        return response

    @router.post("/api/account/password")
    async def password(request: Request, body: PasswordInput):
        def read_account():
            principal = auth.admin(request, True)
            row = store.one("SELECT password_hash FROM users WHERE id=?", (principal.user_id,))
            return principal, row

        principal, row = await store.run(read_account)
        if not await asyncio.to_thread(password_verify, body.current_password, row["password_hash"]):
            raise DevError("PASSWORD_INCORRECT", "当前密码不正确", 403)
        hashed = await asyncio.to_thread(password_hash, body.new_password)
        def commit_password():
            with store.lock, store.db:
                # Verification and hashing yield to other requests. A competing
                # password reset or logout must invalidate this in-flight request.
                auth.admin(request, True)
                changed = store.db.execute("UPDATE users SET password_hash=? WHERE id=? AND password_hash=?", (hashed, principal.user_id, row["password_hash"])).rowcount
                if not changed:
                    raise DevError("PASSWORD_CHANGED", "密码已变化，请重新登录后重试", 409)
                store.db.execute("DELETE FROM sessions WHERE user_id=?", (principal.user_id,))
                store.db.execute("UPDATE grants SET revoked=1 WHERE user_id=?", (principal.user_id,))
            store.audit(principal.actor, "auth.password_changed", detail={"all_sessions_and_grants_revoked": True})
            return {"ok": True, "relogin_required": True}


        return await store.run(commit_password)

    return router
