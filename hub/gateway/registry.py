"""Human-managed connectors, accounts, reviewed exports and explicit delegation."""
from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import Field, SecretStr

from hub import iam
from hub.access import AccessModel
from hub.gateway import catalog, network, policy
from shared.util import DevError

ID = r'^[A-Za-z0-9_-]{1,100}$'


class ConnectorCreate(AccessModel):
    label: str = Field(min_length=1, max_length=80)
    endpoint: str = Field(min_length=1, max_length=2048)
    protocol: Literal['auto', 'modern', 'legacy'] = 'auto'
    networks: list[str] = Field(default_factory=list, max_length=16)
    allow_http: bool = False


class Toggle(AccessModel):
    expected_version: int = Field(ge=1)
    enabled: bool


class AccountCreate(AccessModel):
    connector_id: str = Field(pattern=ID)
    label: str = Field(min_length=1, max_length=80)
    token: SecretStr = SecretStr('')
    sharing: Literal['private', 'space'] = 'private'


class AccountUpdate(AccessModel):
    expected_version: int = Field(ge=1)
    enabled: bool
    token: SecretStr | None = None


class Publish(AccessModel):
    catalog_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    tools: list[str] = Field(min_length=1, max_length=catalog.MAX_TOOLS)
    confirmed: Literal[True]


class BindingCreate(Publish):
    account_id: str = Field(pattern=ID)
    alias: str = Field(pattern=catalog.ALIAS.pattern)


class BindingPublish(Publish):
    expected_version: int = Field(ge=1)


class Consent(AccessModel):
    expected_role_version: int = Field(ge=1)
    confirmed: Literal[True]


def token_value(value):
    value = value.get_secret_value()
    if len(value) > 8192 or any(not 33 <= ord(char) <= 126 for char in value):
        raise DevError('GATEWAY_TOKEN_INVALID', 'Bearer 凭据必须是不含空白的 ASCII 字符，最多 8192 字符')
    return value


def managed_account(store, principal, identifier):
    row = store.one('SELECT * FROM gateway_accounts WHERE id=? AND space_id=?', (identifier, principal.space_id))
    if not row or (row['owner_user_id'] != principal.user_id and not (row['sharing'] == 'space' and principal.admin)):
        raise DevError('GATEWAY_ACCOUNT_NOT_FOUND', '找不到可管理的后端帐户', 404)
    return row


def public_account(row):
    return {key: row[key] for key in ('id', 'connector_id', 'label', 'sharing', 'owner_user_id', 'enabled', 'version', 'catalog_hash')}


def public_binding(row):
    return {**{key: row[key] for key in ('id', 'account_id', 'alias', 'enabled', 'version', 'catalog_hash')},
            'tools': [tool['name'] for tool in json.loads(row['tools'])]}


def selected_tools(account, body):
    if account['catalog_hash'] != body.catalog_hash:
        raise DevError('GATEWAY_CATALOG_CHANGED', '发现目录已改变；请重新阅读工具定义后批准', 409)
    tools = json.loads(account['catalog'])
    names = set(body.tools)
    if len(names) != len(body.tools) or not names <= {tool['name'] for tool in tools}:
        raise DevError('GATEWAY_TOOL_INVALID', '只能发布当前已发现且不重复的工具名称')
    return [tool for tool in tools if tool['name'] in names]


def limit(store, table, space_id, maximum):
    # Table is always a fixed internal constant, never an HTTP input.
    if store.one(f'SELECT count(*) AS n FROM {table} WHERE space_id=?', (space_id,))['n'] >= maximum:
        raise DevError('GATEWAY_CONFIG_LIMIT', '此空间的 MCP 配置数量达到上限', 409)


def make_router(auth, runtime):
    gateway, store = runtime.gateway, runtime.store
    router = APIRouter(prefix='/api/mcp-gateway')

    def actor(request, write=False, instance=False):
        gateway.require_enabled()
        return auth.instance(request, write) if instance else auth.panel(request, write)

    def owned_grant(principal, identifier):
        row = store.one('SELECT * FROM grants WHERE id=? AND user_id=? AND space_id=?', (identifier, principal.user_id, principal.space_id))
        if not row:
            raise DevError('GATEWAY_GRANT_NOT_FOUND', '找不到此账号的连接授权', 404)
        return row

    @router.get('')
    async def overview(request: Request):
        principal = auth.panel(request)
        if not gateway.enabled:
            return {'enabled': False, 'connectors': [], 'accounts': [], 'bindings': [], 'grants': [], 'calls': []}
        connectors = store.all('SELECT * FROM gateway_connectors WHERE space_id=? ORDER BY created,id', (principal.space_id,))
        accounts = [r for r in store.all('SELECT * FROM gateway_accounts WHERE space_id=? ORDER BY created,id', (principal.space_id,)) if policy.accessible(r, principal)]
        allowed = {r['id'] for r in accounts}
        bindings = [public_binding(r) for r in store.all('SELECT * FROM gateway_bindings WHERE space_id=? ORDER BY alias,id', (principal.space_id,)) if r['account_id'] in allowed]
        grants = store.all('''SELECT g.id,g.label,g.role_id,r.version AS role_version,c.consent_version
            FROM grants g JOIN access_roles r ON r.id=g.role_id
            LEFT JOIN gateway_consents c ON c.grant_id=g.id
            WHERE g.space_id=? AND g.user_id=? AND g.authorization_mode='role' AND g.revoked=0 ORDER BY g.created DESC LIMIT 200''', (principal.space_id, principal.user_id))
        calls = store.all('''SELECT id,binding_id,tool,state,error_code,created,updated FROM gateway_calls
            WHERE space_id=? AND user_id=? ORDER BY created DESC,id DESC LIMIT 50''', (principal.space_id, principal.user_id))
        return {'enabled': True, 'instance_admin': principal.instance_admin, 'space_admin': principal.admin,
                'user_id': principal.user_id, 'connectors': connectors, 'accounts': [public_account(r) for r in accounts],
                'bindings': bindings, 'grants': grants, 'calls': calls}

    @router.post('/connectors', status_code=201)
    async def create_connector(request: Request, body: ConnectorCreate):
        principal = actor(request, True, instance=True)
        endpoint = network.endpoint(body.endpoint, body.networks, body.allow_http)
        identifier = 'gwc_' + uuid.uuid4().hex
        with store.transaction():
            limit(store, 'gateway_connectors', principal.space_id, 64)
            store.db.execute('''INSERT INTO gateway_connectors(id,space_id,label,endpoint,protocol,networks,allow_http,created)
                VALUES(?,?,?,?,?,?,?,?)''', (identifier, principal.space_id, body.label, endpoint, body.protocol,
                                             json.dumps(body.networks), int(body.allow_http), time.time()))
            store.audit(principal.actor, 'gateway.connector_created', identifier, detail={'protocol': body.protocol}, commit=False)
        return store.one('SELECT * FROM gateway_connectors WHERE id=?', (identifier,))

    @router.patch('/connectors/{identifier}')
    async def toggle_connector(identifier: str, request: Request, body: Toggle):
        principal = actor(request, True, instance=True)
        with store.transaction():
            changed = store.db.execute('UPDATE gateway_connectors SET enabled=?,version=version+1 WHERE id=? AND space_id=? AND version=?',
                                      (int(body.enabled), identifier, principal.space_id, body.expected_version)).rowcount
            if not changed:
                raise DevError('GATEWAY_CONFIG_CHANGED', '连接不存在或版本已变化', 409)
            store.audit(principal.actor, 'gateway.connector_changed', identifier, detail={'enabled': body.enabled}, commit=False)
        return {'ok': True}

    @router.post('/accounts', status_code=201)
    async def create_account(request: Request, body: AccountCreate):
        principal = actor(request, True)
        if body.sharing == 'space' and not principal.admin:
            raise DevError('SPACE_ADMIN_REQUIRED', '共享服务帐户必须由空间管理员建立', 403)
        if not store.one('SELECT id FROM gateway_connectors WHERE id=? AND space_id=? AND enabled=1', (body.connector_id, principal.space_id)):
            raise DevError('GATEWAY_CONNECTOR_NOT_FOUND', '找不到可用的 MCP 连接', 404)
        secret = store.encrypt(token_value(body.token))
        identifier = 'gwa_' + uuid.uuid4().hex
        with store.transaction():
            limit(store, 'gateway_accounts', principal.space_id, 128)
            store.db.execute('''INSERT INTO gateway_accounts(id,connector_id,space_id,owner_user_id,label,sharing,secret,created)
                VALUES(?,?,?,?,?,?,?,?)''', (identifier, body.connector_id, principal.space_id, principal.user_id, body.label, body.sharing, secret, time.time()))
            store.audit(principal.actor, 'gateway.account_created', identifier, detail={'sharing': body.sharing}, commit=False)
        return public_account(store.one('SELECT * FROM gateway_accounts WHERE id=?', (identifier,)))

    @router.patch('/accounts/{identifier}')
    async def update_account(identifier: str, request: Request, body: AccountUpdate):
        principal = actor(request, True)
        with store.transaction():
            row = managed_account(store, principal, identifier)
            if row['version'] != body.expected_version:
                raise DevError('GATEWAY_CONFIG_CHANGED', '帐户已被修改', 409)
            secret = store.encrypt(token_value(body.token)) if body.token is not None else row['secret']
            store.db.execute('UPDATE gateway_accounts SET enabled=?,secret=?,version=version+1 WHERE id=?', (int(body.enabled), secret, identifier))
            if body.token is not None:
                store.db.execute("UPDATE gateway_accounts SET catalog='[]',catalog_hash='' WHERE id=?", (identifier,))
            store.audit(principal.actor, 'gateway.account_changed', identifier, detail={'enabled': body.enabled, 'credential_rotated': body.token is not None}, commit=False)
        return public_account(store.one('SELECT * FROM gateway_accounts WHERE id=?', (identifier,)))

    @router.post('/accounts/{identifier}/discover')
    async def discover(identifier: str, request: Request):
        principal = actor(request, True)
        return await gateway.discover(principal, identifier, lambda: actor(request, True))

    @router.post('/bindings', status_code=201)
    async def create_binding(request: Request, body: BindingCreate):
        principal = actor(request, True)
        with store.transaction():
            account = managed_account(store, principal, body.account_id)
            tools = selected_tools(account, body)
            limit(store, 'gateway_bindings', principal.space_id, 128)
            identifier = 'gwb_' + uuid.uuid4().hex
            try:
                store.db.execute('''INSERT INTO gateway_bindings(id,account_id,space_id,alias,tools,catalog_hash,created)
                    VALUES(?,?,?,?,?,?,?)''', (identifier, account['id'], principal.space_id, body.alias,
                                                 catalog.encoded(tools), catalog.fingerprint(tools), time.time()))
            except sqlite3.IntegrityError as exc:
                raise DevError('GATEWAY_ALIAS_EXISTS', '此空间已有相同命名空间；停用后也不会重新分配旧名称', 409) from exc
            store.audit(principal.actor, 'gateway.tools_published', identifier, detail={'tools': body.tools}, commit=False)
        return public_binding(store.one('SELECT * FROM gateway_bindings WHERE id=?', (identifier,)))

    @router.post('/bindings/{identifier}/publish')
    async def publish_binding(identifier: str, request: Request, body: BindingPublish):
        principal = actor(request, True)
        with store.transaction():
            binding, _, _ = policy.binding_rows(store, identifier, principal.space_id)
            account = managed_account(store, principal, binding['account_id'])
            if binding['version'] != body.expected_version:
                raise DevError('GATEWAY_CONFIG_CHANGED', '工具发布已被修改', 409)
            tools = selected_tools(account, body)
            store.db.execute('UPDATE gateway_bindings SET tools=?,catalog_hash=?,version=version+1 WHERE id=?', (catalog.encoded(tools), catalog.fingerprint(tools), identifier))
            store.audit(principal.actor, 'gateway.tools_published', identifier, detail={'tools': body.tools}, commit=False)
        return public_binding(store.one('SELECT * FROM gateway_bindings WHERE id=?', (identifier,)))

    @router.patch('/bindings/{identifier}')
    async def toggle_binding(identifier: str, request: Request, body: Toggle):
        principal = actor(request, True)
        with store.transaction():
            binding, _, _ = policy.binding_rows(store, identifier, principal.space_id)
            managed_account(store, principal, binding['account_id'])
            if binding['version'] != body.expected_version:
                raise DevError('GATEWAY_CONFIG_CHANGED', '工具发布已被修改', 409)
            store.db.execute('UPDATE gateway_bindings SET enabled=?,version=version+1 WHERE id=?', (int(body.enabled), identifier))
            store.audit(principal.actor, 'gateway.binding_changed', identifier, detail={'enabled': body.enabled}, commit=False)
        return {'ok': True}

    @router.post('/grants/{identifier}/consent')
    async def consent(identifier: str, request: Request, body: Consent):
        principal = actor(request, True)
        with store.transaction():
            grant = owned_grant(principal, identifier)
            iam.validate_grant(store, grant)
            if grant['authorization_mode'] != 'role':
                raise DevError('GATEWAY_ROLE_REQUIRED', '固定项目授权不会升级为跨 MCP 委派', 403)
            from hub.roles import role_binding
            role, _, _ = role_binding(store, grant)
            if not role['enabled'] or role['version'] != body.expected_role_version:
                raise DevError('ROLE_CHANGED', '角色政策已改变，请重新阅读再同意', 409)
            store.db.execute('''INSERT INTO gateway_consents(grant_id,user_id,space_id,profile_id,role_id,consent_version,created)
                VALUES(?,?,?,?,?,1,?) ON CONFLICT(grant_id) DO UPDATE SET consent_version=1,created=excluded.created''',
                (identifier, principal.user_id, principal.space_id, grant['profile_id'], grant['role_id'], time.time()))
            store.audit(principal.actor, 'gateway.delegation_consented', identifier,
                        detail={'role_id': role['id'], 'role_version': role['version'], 'future_reviewed_connector_rules': True}, commit=False)
        return {'ok': True, 'consent_version': 1}

    @router.delete('/grants/{identifier}/consent')
    async def revoke_consent(identifier: str, request: Request):
        principal = actor(request, True)
        with store.transaction():
            owned_grant(principal, identifier)
            store.db.execute('DELETE FROM gateway_consents WHERE grant_id=?', (identifier,))
            store.audit(principal.actor, 'gateway.delegation_revoked', identifier, commit=False)
        return {'ok': True}

    return router
