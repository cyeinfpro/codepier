"""Space-bound multi-user OAuth authorization code + S256 PKCE, DCR and rotating refresh tokens.

Public clients only (token_endpoint_auth_method=none). Explicit per-authorization
consent; resource bound grants; no token forwarding; no wildcard redirect URLs.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from hub.auth import Auth
from hub.access_profiles import effective_grant, validate_profile_consent
from hub.access import access_defaults, project_selection
from hub.runtime import Runtime
from shared.crypto import digest, token
from shared.util import DevError, valid_json_value
from shared.role_contracts import ROLE_SCOPE
from hub.roles import validate_role_consent, public_role
from hub import iam


class OAuth:
    def __init__(self, auth: Auth, runtime: Runtime, public_url):
        self.auth, self.runtime, self.store = auth, runtime, auth.store
        self.public_url = public_url
        self.auth.resource = self.resource
        # Older databases did not persist the audience. Bind existing OAuth
        # grants once, before a later public URL change can retarget them.
        self.store.execute("UPDATE grants SET resource=? WHERE client_id IS NOT NULL AND resource IS NULL", (self.resource(),))
        self.redirect_hosts = {host.strip().lower() for host in os.getenv("OAUTH_REDIRECT_HOSTS", "chatgpt.com,chat.openai.com,platform.openai.com,localhost,127.0.0.1,::1").split(",") if host.strip()}
        self.router = APIRouter()
        self.routes()

    def resource(self):
        return self.public_url() + "/mcp"

    def throttle(self, request: Request):
        ip = "oauth:" + (request.client.host if request.client else "unknown")
        now = time.time()
        self.store.execute("DELETE FROM login_attempts WHERE at<?", (now - 900,))
        count = self.store.one("SELECT count(*) AS n FROM login_attempts WHERE ip=? AND at>?", (ip, now - 60))["n"]
        if count >= 30:
            raise DevError("OAUTH_THROTTLED", "授权请求过多，请稍后重试", 429)
        self.store.execute("INSERT INTO login_attempts VALUES (?,?)", (ip, now))
        with self.store.lock, self.store.db:
            self.store.db.execute("DELETE FROM oauth_requests WHERE expires<=? OR used=1", (now,))
            self.store.db.execute("DELETE FROM oauth_codes WHERE expires<=?", (now,))
            self.store.db.execute("DELETE FROM tokens WHERE expires<=?", (now,))

    def validate_redirect(self, value: str):
        try:
            if not isinstance(value, str) or not valid_json_value(value) or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in value) or "#" in value:
                raise ValueError()
            u = urlsplit(value)
            if not u.hostname or u.hostname not in self.redirect_hosts or u.username is not None or u.password is not None or u.fragment:
                raise ValueError()
            if u.scheme != "https" and not (u.scheme == "http" and u.hostname in {"127.0.0.1", "localhost", "::1"}):
                raise ValueError()
            if u.port is not None and not 1 <= u.port <= 65535:
                raise ValueError()
        except (ValueError, TypeError) as exc:
            raise DevError("INVALID_REDIRECT", "回调必须是允许主机上的完整 HTTPS 地址（本机回调可用 HTTP），不可使用通配符") from exc
        return value

    def auth_request(self, params):
        client_id = params.get("client_id", "")
        client = self.store.one("SELECT * FROM oauth_clients WHERE id=?", (client_id,))
        if not client:
            raise DevError("INVALID_CLIENT", "未知 OAuth 客户端")
        redirect = params.get("redirect_uri", "")
        if redirect not in json.loads(client["redirects"]):
            raise DevError("INVALID_REDIRECT", "回调地址与客户端登记地址不一致")
        self.validate_redirect(redirect)
        if params.get("response_type") != "code":
            raise DevError("UNSUPPORTED_RESPONSE_TYPE", "仅支持授权码流程")
        challenge = params.get("code_challenge", "")
        if params.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", challenge):
            raise DevError("INVALID_PKCE", "必须使用 S256 PKCE")
        resource = params.get("resource", self.resource())
        if resource != self.resource():
            raise DevError("INVALID_TARGET", "资源标识不匹配")
        scopes = params.get("scope", "read").split()
        if set(scopes) != {ROLE_SCOPE} and (not scopes or not set(scopes).issubset({"read", "write", "execute", "computer"}) or "read" not in scopes):
            raise DevError("INVALID_SCOPE", "使用传统 read/write/execute/computer，或单独申请 codepier.role_access；两种模式不能混用")
        state = params.get("state", "")
        if len(state) > 2048:
            raise DevError("INVALID_STATE", "state 过长")
        id = token(24)
        self.store.execute("INSERT INTO oauth_requests(id,client_id,redirect_uri,challenge,state,scopes,resource,expires,used) VALUES (?,?,?,?,?,?,?,?,0)",
             (id, client_id, redirect, challenge, state, json.dumps(sorted(set(scopes))), resource, time.time() + 600))
        return id

    def get_request(self, id):
        row = self.store.one("SELECT r.*,c.name AS client_name FROM oauth_requests r JOIN oauth_clients c ON c.id=r.client_id WHERE r.id=? AND r.used=0 AND r.expires>?", (id, time.time()))
        if not row:
            raise DevError("AUTH_REQUEST_EXPIRED", "授权请求已过期或已经处理，请从 ChatGPT 重新连接", 410)
        return row

    def bound_request(self, identifier, request, principal, *, select_space=False):
        """Pin consent to the authenticated human and browser session.

        A Space selection can change only while rendering a fresh consent page
        in the same login session. Approval must match that page's Space.
        Client identifiers or OAuth scopes never confer Space membership.
        """
        session=self.auth.session(request)
        row=self.get_request(identifier)
        if row['bound_user_id'] is not None and (row['bound_user_id']!=principal.user_id or row['bound_session_hash']!=session['id_hash']):
            raise DevError('CONSENT_IDENTITY_CHANGED','授权属于另一登录会话；请重新发起连接',403)
        if row['bound_user_id'] is None or select_space:
            self.store.db.execute('UPDATE oauth_requests SET bound_user_id=?,bound_session_hash=?,space_id=? WHERE id=? AND used=0',
                                  (principal.user_id,session['id_hash'],principal.space_id,identifier))
            row.update(bound_user_id=principal.user_id,bound_session_hash=session['id_hash'],space_id=principal.space_id)
        if row['space_id']!=principal.space_id:
            raise DevError('CONSENT_SPACE_CHANGED','空间已改变；请重新读取授权页面',409)
        return row

    @staticmethod
    def redirect(uri: str, params: dict):
        u = urlsplit(uri)
        extra = urlencode(params)
        return urlunsplit((u.scheme, u.netloc, u.path, u.query + ("&" if u.query else "") + extra, ""))

    def new_tokens(self, grant_id: str):
        grant = self.store.one("SELECT * FROM grants WHERE id=?", (grant_id,))
        scopes, _, _ = effective_grant(self.store, grant)
        if grant.get('authorization_mode') == 'role':
            scopes = {ROLE_SCOPE}  # OAuth delegation is stable; capabilities are live policy.
        now = time.time()
        access, refresh = "rda_" + token(), "rdr_" + token()
        self.store.db.execute("INSERT INTO tokens VALUES (?,?,?,'access',?,?)", (token(16), digest(access), grant_id, now + 3600, now))
        self.store.db.execute("INSERT INTO tokens VALUES (?,?,?,'refresh',?,?)", (token(16), digest(refresh), grant_id, now + 30 * 86400, now))
        return {"access_token": access, "token_type": "Bearer", "expires_in": 3600, "refresh_token": refresh, "scope": " ".join(sorted(scopes))}

    @staticmethod
    async def json_object(request: Request):
        try:
            body = await request.json()
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise DevError("INVALID_REQUEST", "Expected a JSON object") from exc
        if not isinstance(body, dict) or not valid_json_value(body):
            raise DevError("INVALID_REQUEST", "Expected a JSON object")
        return body

    async def form(self, request: Request):
        raw = await request.body()
        if len(raw) > 16384:
            raise DevError("BODY_TOO_LARGE", "请求过大", 413)
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/x-www-form-urlencoded":
            raise DevError("INVALID_REQUEST", "Token endpoint requires form encoding")
        try:
            values = parse_qs(raw.decode(), keep_blank_values=True, max_num_fields=30, errors="strict")
        except (UnicodeError, ValueError) as exc:
            raise DevError("INVALID_REQUEST", "无效表单") from exc
        if any(len(v) != 1 for v in values.values()):
            raise DevError("INVALID_REQUEST", "参数不能重复")
        return {k: v[0] for k, v in values.items()}

    def routes(self):
        router = self.router

        @router.get("/.well-known/oauth-protected-resource")
        @router.get("/.well-known/oauth-protected-resource/mcp")
        async def protected_metadata():
            return JSONResponse({"resource": self.resource(), "authorization_servers": [self.public_url()],
                "scopes_supported": ["read", "write", "execute", "computer", ROLE_SCOPE], "bearer_methods_supported": ["header"],
                "resource_name": "CodePier Agent"}, headers={"Access-Control-Allow-Origin": "*"})

        @router.get("/.well-known/oauth-authorization-server")
        async def server_metadata():
            base = self.public_url()
            return JSONResponse({"issuer": base, "authorization_endpoint": base + "/oauth/authorize", "token_endpoint": base + "/oauth/token",
                "registration_endpoint": base + "/oauth/register", "revocation_endpoint": base + "/oauth/revoke",
                "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"], "code_challenge_methods_supported": ["S256"],
                "scopes_supported": ["read", "write", "execute", "computer", ROLE_SCOPE]}, headers={"Access-Control-Allow-Origin": "*"})

        @router.post("/oauth/register")
        async def register(request: Request):
            self.throttle(request)
            body = await self.json_object(request)
            name = body.get("client_name", "MCP client")
            redirects = body.get("redirect_uris", [])
            if not isinstance(name, str) or not 1 <= len(name) <= 100:
                raise DevError("INVALID_CLIENT_METADATA", "client_name 长度应为 1–100")
            if not isinstance(redirects, list) or not 1 <= len(redirects) <= 8:
                raise DevError("INVALID_CLIENT_METADATA", "必须登记 1–8 个精确回调地址")
            for redirect in redirects:
                if not isinstance(redirect, str) or len(redirect) > 2048:
                    raise DevError("INVALID_REDIRECT", "无效回调地址")
                self.validate_redirect(redirect)
            if body.get("token_endpoint_auth_method", "none") != "none":
                raise DevError("INVALID_CLIENT_METADATA", "仅支持 public client + PKCE；token_endpoint_auth_method=none")
            id = token(24)
            self.store.execute("INSERT INTO oauth_clients VALUES (?,?,?,?)", (id, name, json.dumps(redirects), time.time()))
            return JSONResponse({"client_id": id, "client_name": name, "redirect_uris": redirects, "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}, status_code=201, headers={"Cache-Control": "no-store"})

        @router.get("/oauth/authorize")
        async def authorize(request: Request):
            self.throttle(request)
            params = dict(request.query_params)
            if len(request.query_params.multi_items()) != len(params):
                raise DevError("INVALID_REQUEST", "授权参数不能重复")
            id = self.auth_request(params)
            return RedirectResponse("/?authorize=" + id, status_code=303)

        @router.get("/api/oauth/requests/{id}")
        async def auth_details(id: str, request: Request):
            with self.store.lock,self.store.db:
                self.store.db.execute('BEGIN IMMEDIATE')
                principal = self.auth.panel(request)
                row = self.bound_request(id,request,principal,select_space=True)
            return {"id": id,"space_id":principal.space_id,"user_id":principal.user_id,"username":self.auth.session(request)['username'], "client_name": row["client_name"], "client_id": row["client_id"], "redirect_uri": row["redirect_uri"],
                    "scopes": json.loads(row["scopes"]), "resource": row["resource"], "expires": row["expires"],
                    "access_defaults": access_defaults(self.store, principal.user_id)}

        @router.post("/api/oauth/requests/{id}/decide")
        async def decide(id: str, request: Request):
            principal = self.auth.panel(request, True)
            body = await self.json_object(request)
            # Parsing yields; re-read the session before deciding whose consent this is.
            with self.store.lock,self.store.db:
                self.store.db.execute('BEGIN IMMEDIATE')
                principal=self.auth.panel(request,True)
                row=self.bound_request(id,request,principal)
            if type(body.get("allow")) is not bool:
                raise DevError("INVALID_REQUEST", "allow 必须为布尔值")
            if body.get("allow") is not True:
                with self.store.lock, self.store.db:
                    principal=self.auth.panel(request, True)
                    self.bound_request(id,request,principal)
                    used = self.store.db.execute("UPDATE oauth_requests SET used=1 WHERE id=? AND used=0 AND expires>?", (id, time.time())).rowcount
                    if not used:
                        raise DevError("AUTH_REQUEST_EXPIRED", "授权请求已处理", 410)
                self.store.audit(principal.actor, "oauth.consent", row["client_name"], "denied")
                return {"redirect": self.redirect(row["redirect_uri"], {"error": "access_denied", "state": row["state"]})}
            scopes = body.get("scopes", [])
            projects = body.get("projects", [])
            mode = body.get('authorization_mode', 'fixed')
            if (not isinstance(scopes, list) or any(not isinstance(x, str) for x in scopes)
                    or not set(scopes).issubset(set(json.loads(row['scopes'])))):
                raise DevError('INVALID_SCOPE', '不得超出客户端申请的 OAuth 范围')
            if mode == 'role':
                if scopes != [ROLE_SCOPE] or projects or body.get('all_projects', False) is not False:
                    raise DevError('INVALID_SCOPE', '角色授权仅使用 codepier.role_access；项目范围由角色政策决定')
                projects = []
            elif mode == 'fixed' and 'read' in scopes and set(scopes) <= {'read', 'write', 'execute', 'computer'}:
                projects = project_selection(self.store, projects, body.get('all_projects', False),space_id=principal.space_id)
            else:
                raise DevError('INVALID_SCOPE', '请明确选择传统固定授权或动态角色授权')
            profile_id = body.get('profile_id')
            gid, code = token(16), token()
            with self.store.lock, self.store.db:
                self.store.db.execute("BEGIN IMMEDIATE")
                principal=self.auth.panel(request, True)
                row=self.bound_request(id,request,principal)
                role_id = None
                if mode == 'role':
                    role = validate_role_consent(self.store, principal.user_id, profile_id, body.get('profile_version'),
                                                 body.get('role_version'), body.get('confirm_dynamic_role'),space_id=principal.space_id)
                    role_id = role['id']
                else:
                    validate_profile_consent(self.store, principal.user_id, profile_id, scopes, projects, body.get('profile_version'),space_id=principal.space_id)
                    if not principal.admin:
                        if '*' in projects:
                            raise DevError('ROLE_REQUIRED','普通成员的持续授权请选择获授予的动态角色',403)
                        permissions=iam.project_permissions(self.store,principal)
                        for project_id in projects:
                            if not set(scopes)<=permissions.get(project_id,set()):
                                raise DevError('DELEGATION_DENIED','不得委派超出当前账号的项目权限',403)
                if row["resource"] != self.resource():
                    raise DevError("INVALID_TARGET", "资源标识已变化，请重新发起授权")
                used = self.store.db.execute("UPDATE oauth_requests SET used=1 WHERE id=? AND used=0 AND expires>?", (id, time.time())).rowcount
                if not used:
                    raise DevError("AUTH_REQUEST_EXPIRED", "授权请求已处理", 410)
                self.store.db.execute("INSERT INTO grants(id,user_id,label,client_id,scopes,projects,revoked,created,resource,profile_id,authorization_mode,role_id,space_id,owner_user_id,identity_id,user_epoch) VALUES (?,?,?,?,?,?,0,?,?,?,?,?,?,?,?,?)", (gid, principal.user_id, row["client_name"], row["client_id"], json.dumps(sorted(set(scopes))), json.dumps(projects), time.time(), row["resource"], profile_id, mode, role_id,principal.space_id,principal.user_id,principal.identity_id,principal.user_epoch))
                if mode == 'role':
                    from hub.access_profiles import profile_audit
                    profile_audit(self.store, principal.actor, 'role.consent', gid,
                                  {'profile_id': profile_id, 'role': public_role(role), 'mode': mode, 'future_policy_changes_consented': True})
                self.store.db.execute("INSERT INTO oauth_codes VALUES (?,?,?,?,?,?,?)", (digest(code), row["client_id"], row["redirect_uri"], row["challenge"], row["resource"], gid, time.time() + 300))
            self.store.audit(principal.actor, "oauth.consent", row["client_name"], detail={"grant_id": gid, "scopes": scopes, "projects": projects, "profile_id": profile_id, "authorization_mode": mode, "role_id": role_id})
            return {"redirect": self.redirect(row["redirect_uri"], {"code": code, "state": row["state"]})}

        @router.post("/oauth/token")
        async def exchange(request: Request):
            self.throttle(request)
            try:
                form = await self.form(request)
                if form.get("resource", self.resource()) != self.resource():
                    return JSONResponse({"error": "invalid_target"}, status_code=400)
                if form.get("grant_type") == "authorization_code":
                    verifier = form.get("code_verifier", "")
                    if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
                        return JSONResponse({"error": "invalid_grant"}, status_code=400)
                    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
                    with self.store.lock, self.store.db:
                        row = self.store.db.execute("SELECT c.*,g.revoked FROM oauth_codes c JOIN grants g ON g.id=c.grant_id WHERE c.hash=?", (digest(form.get("code", "")),)).fetchone()
                        if not row or row["revoked"] or row["expires"] <= time.time() or row["client_id"] != form.get("client_id") or row["redirect_uri"] != form.get("redirect_uri") or row["resource"] != self.resource() or not hmac.compare_digest(row["challenge"], challenge):
                            return JSONResponse({"error": "invalid_grant"}, status_code=400)
                        self.store.db.execute("DELETE FROM oauth_codes WHERE hash=?", (row["hash"],))
                        result = self.new_tokens(row["grant_id"])
                elif form.get("grant_type") == "refresh_token":
                    with self.store.lock, self.store.db:
                        row = self.store.db.execute("SELECT t.*,g.client_id,g.revoked,g.resource FROM tokens t JOIN grants g ON g.id=t.grant_id WHERE t.hash=? AND t.kind IN ('refresh','refresh_used')", (digest(form.get("refresh_token", "")),)).fetchone()
                        if not row or row["revoked"] or row["expires"] <= time.time() or row["client_id"] != form.get("client_id") or row["resource"] != self.resource():
                            return JSONResponse({"error": "invalid_grant"}, status_code=400)
                        if row["kind"] == "refresh_used":
                            # Both holders are indistinguishable after a token is
                            # stolen. Reuse revokes the entire rotation family.
                            self.store.db.execute("UPDATE grants SET revoked=1 WHERE id=?", (row["grant_id"],))
                            return JSONResponse({"error": "invalid_grant"}, status_code=400)
                        granted = self.store.one('SELECT scopes FROM grants WHERE id=?', (row['grant_id'],))
                        if form.get('scope') is not None and set(form['scope'].split()) != set(json.loads(granted['scopes'])):
                            return JSONResponse({'error': 'invalid_scope', 'error_description': 'Refresh cannot change the consented OAuth scope.'}, status_code=400)
                        self.store.db.execute("UPDATE tokens SET kind='refresh_used' WHERE hash=?", (row["hash"],))
                        result = self.new_tokens(row["grant_id"])
                else:
                    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
                return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
            except DevError as exc:
                return JSONResponse({"error": "invalid_grant" if exc.code == "INVALID_TOKEN" else "invalid_request", "error_description": exc.message}, status_code=400 if exc.code == "INVALID_TOKEN" else exc.status)

        @router.post("/oauth/revoke")
        async def revoke(request: Request):
            self.throttle(request)
            form = await self.form(request)
            row = self.store.one("SELECT t.grant_id,g.client_id FROM tokens t JOIN grants g ON g.id=t.grant_id WHERE t.hash=?", (digest(form.get("token", "")),))
            if row and row["client_id"] == form.get("client_id"):
                self.store.execute("UPDATE grants SET revoked=1 WHERE id=?", (row["grant_id"],))
                self.store.audit("oauth:" + str(row["client_id"]), "oauth.revoke", row["grant_id"], target_kind="grant")
            return JSONResponse({})
