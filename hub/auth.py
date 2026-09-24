from __future__ import annotations
import asyncio
import json
import time
from fastapi import Request
from hub.runtime import Principal
from hub.store import Store
from hub.access_profiles import effective_grant, validate_profile_consent
from shared.crypto import digest, token, password_verify
from shared.util import DevError
from shared.role_contracts import ROLE_SCOPE
from hub.roles import validate_role_consent

SESSION_SECONDS = 12 * 3600


class Auth:
    def __init__(self, store: Store):
        self.store = store
        # Unknown users still pay password verification cost.
        from shared.crypto import password_hash
        self.dummy = password_hash(token())
        self.login_inflight = 0
        self.resource = None

    def session(self, request: Request):
        cookie = request.cookies.get("rd_session", "")
        if not cookie:
            raise DevError("LOGIN_REQUIRED", "请先登录面板", 401)
        session = self.store.one("SELECT s.*,u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id_hash=? AND s.expires>?", (digest(cookie), time.time()))
        if not session:
            raise DevError("LOGIN_REQUIRED", "登录已过期，请重新登录", 401)
        return session

    @staticmethod
    def check_origin(request: Request):
        origin = request.headers.get("origin")
        expected = f"{request.url.scheme}://{request.url.netloc}"
        if origin and origin != expected:
            raise DevError("ORIGIN_REJECTED", "不接受跨站面板请求", 403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            raise DevError("ORIGIN_REJECTED", "不接受跨站面板请求", 403)

    def admin(self, request: Request, write=False):
        session = self.session(request)
        if write:
            self.check_origin(request)
            import hmac
            if not hmac.compare_digest(request.headers.get("x-rd-csrf", "").encode("utf-8"), session["csrf"].encode("utf-8")):
                raise DevError("CSRF_REJECTED", "页面会话已变化，请刷新后重试", 403)
        return Principal("panel:" + session["username"], session["user_id"], {"read", "write", "execute", "computer"}, ["*"], admin=True)

    def bearer(self, request: Request):
        header = request.headers.get("authorization", "")
        scheme, separator, value = header.partition(" ")
        if not separator or scheme.lower() != "bearer":
            raise DevError("TOKEN_REQUIRED", "需要 MCP Bearer Token 或 OAuth 授权", 401)
        value = value.strip()
        if not value or len(value) > 500:
            raise DevError("INVALID_TOKEN", "无效的凭据", 401)
        row = self.store.one("SELECT t.*,g.user_id,g.label,g.scopes,g.projects,g.revoked,g.resource,g.profile_id,g.authorization_mode,g.role_id FROM tokens t JOIN grants g ON g.id=t.grant_id WHERE t.hash=? AND t.kind IN ('access','pat') AND t.expires>? AND g.revoked=0", (digest(value), time.time()))
        if not row or (row["kind"] == "access" and self.resource and row["resource"] != self.resource()):
            raise DevError("INVALID_TOKEN", "凭据已过期或撤销", 401)
        scopes, projects, _ = effective_grant(self.store, row)
        return Principal("mcp:" + row["grant_id"] + ":" + row["label"], row["user_id"], scopes, projects,
                         grant_id=row["grant_id"], profile_id=row["profile_id"],
                         authorization_mode=row['authorization_mode'], role_id=row['role_id'])

    async def login(self, request: Request, username: str, password: str):
        self.check_origin(request)
        # Scrypt uses substantial RAM; per-IP limits alone do not bound work
        # queued in the shared thread pool by requests from many addresses.
        if self.login_inflight >= 4:
            raise DevError("LOGIN_THROTTLED", "登录服务繁忙，请稍后重试", 429)
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        self.store.execute("DELETE FROM login_attempts WHERE at<?", (now - 900,))
        count = self.store.one("SELECT count(*) AS n FROM login_attempts WHERE ip=? AND at>?", (ip, now - 300))["n"]
        if count >= 8:
            raise DevError("LOGIN_THROTTLED", "登录尝试过多，5 分钟后重试", 429)
        self.store.execute("INSERT INTO login_attempts VALUES (?,?)", (ip, now))
        user = self.store.one("SELECT * FROM users WHERE username=?", (username,))
        self.login_inflight += 1
        verification = asyncio.create_task(asyncio.to_thread(password_verify, password, user["password_hash"] if user else self.dummy))
        def finished(task):
            self.login_inflight -= 1
            if not task.cancelled():
                task.exception()
        verification.add_done_callback(finished)
        valid = await asyncio.shield(verification)
        if not user or not valid:
            self.store.audit("anonymous", "auth.login", username[:80], "failed", {"ip": ip})
            raise DevError("LOGIN_FAILED", "用户名或密码不正确", 401)
        secret, csrf = token(), token()
        now = time.time()
        with self.store.lock, self.store.db:
            current = self.store.db.execute("SELECT password_hash FROM users WHERE id=?", (user["id"],)).fetchone()
            if not current or current["password_hash"] != user["password_hash"]:
                raise DevError("LOGIN_FAILED", "密码已变化，请重新登录", 401)
            self.store.db.execute("DELETE FROM login_attempts WHERE ip=?", (ip,))
            self.store.db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            self.store.db.execute("INSERT INTO sessions VALUES (?,?,?,?)", (digest(secret), user["id"], csrf, now + SESSION_SECONDS))
        self.store.audit("panel:" + username, "auth.login", status="ok", detail={"ip": ip})
        return {"cookie": secret, "csrf": csrf, "username": username, "user_id": user['id']}

    def issue_grant(self, principal: Principal, label: str, scopes: list[str], projects: list[str], days: int = 30, client_id=None, *, profile_id=None, profile_version=None, authorization_mode='fixed', role_version=None, confirm_dynamic_role=False):
        if authorization_mode == 'role':
            if scopes != [ROLE_SCOPE] or projects:
                raise DevError('INVALID_SCOPE', '动态角色仅接受 codepier.role_access，资源清单由角色实时决定')
        elif authorization_mode != 'fixed':
            raise DevError('INVALID_SCOPE', '未知授权模式')
        elif "read" not in scopes or not set(scopes).issubset({"read", "write", "execute", "computer"}):
            raise DevError("INVALID_SCOPE", "权限必须包含 read，且只支持 read/write/execute/computer")
        if not projects and authorization_mode != 'role':
            raise DevError("NO_PROJECTS", "至少选择一个项目")
        if "*" not in projects:
            found = {x["id"] for x in self.store.all("SELECT id FROM projects")}
            if not set(projects).issubset(found):
                raise DevError("INVALID_PROJECT", "授权的项目已不存在")
        gid, tid, secret = token(16), token(16), "rd_" + token()
        now = time.time()
        with self.store.lock, self.store.db:
            self.store.db.execute("BEGIN IMMEDIATE")
            role_id = None
            if authorization_mode == 'role':
                role = validate_role_consent(self.store, principal.user_id, profile_id, profile_version, role_version, confirm_dynamic_role)
                role_id = role['id']
            else:
                validate_profile_consent(self.store, principal.user_id, profile_id, scopes, projects, profile_version)
            self.store.db.execute("INSERT INTO grants(id,user_id,label,client_id,scopes,projects,revoked,created,profile_id,authorization_mode,role_id) VALUES (?,?,?,?,?,?,0,?,?,?,?)", (gid, principal.user_id, label, client_id, json.dumps(sorted(set(scopes))), json.dumps(projects), now, profile_id, authorization_mode, role_id))
            self.store.db.execute("INSERT INTO tokens VALUES (?,?,?,'pat',?,?)", (tid, digest(secret), gid, now + days * 86400, now))
            if role_id:
                self.store.db.execute("INSERT INTO audit(at,actor,action,target,status,detail) VALUES (?,?,?,?,?,?)",
                    (now, principal.actor, 'role.consent', gid, 'ok', json.dumps({
                        'source': 'pat', 'profile_id': profile_id, 'role_id': role_id,
                        'role_version': role['version'], 'policy': json.loads(role['policy']),
                        'dynamic_resources_and_actions': True}, ensure_ascii=False)))
        return {"grant_id": gid, "token": secret, "expires": now + days * 86400}
