"""Saved VPS inventory. Public metadata is separate from encrypted credentials.

The Hub stores passwords using its existing master key. Only the durable worker
resolves them for the encrypted Agent channel; model calls and queue payloads
contain references, never the password. Existing Agent ssh_exec handles process
permissions, streamed redaction, timeouts, cancellation and journal recovery.
"""
from __future__ import annotations

import ipaddress
import sqlite3
import time
import unicodedata
import uuid

from fastapi import APIRouter, Query, Request
from pydantic import Field, model_validator

from shared.contracts import Args, SSHExec, VPSList
from shared.util import DevError
from hub import iam


PUBLIC_FIELDS = ('id', 'name', 'host', 'port', 'username', 'host_key_policy',
                 'provider', 'region', 'system', 'notes', 'enabled', 'version',
                 'connection_revision', 'created', 'updated')
PUBLIC_SQL = ','.join('v.' + key for key in PUBLIC_FIELDS)


def name_key(value):
    return unicodedata.normalize('NFKC', value.strip()).casefold()


def canonical_host(value):
    value = value.strip()
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return value.rstrip('.').lower()


class VPSInput(Args):
    name: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(default='root', min_length=1, max_length=128)
    password: str | None = Field(default=None, min_length=1, max_length=4096, repr=False)
    host_key_policy: str = Field(default='strict', pattern=r'^(strict|accept-new)$')
    provider: str = Field(default='', max_length=120)
    region: str = Field(default='', max_length=120)
    system: str = Field(default='', max_length=160)
    notes: str = Field(default='', max_length=2000)
    enabled: bool = True
    project_ids: list[str] = Field(default_factory=list, max_length=500)
    expected_version: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def validate_connection(self):
        self.name = self.name.strip()
        self.host = canonical_host(self.host)
        if not self.name or any(ord(c) < 32 for c in self.name):
            raise ValueError('VPS 名称不能为空或含控制字符')
        # Reuse the transport's host/user/password validation; never echo inputs.
        SSHExec(project='validation', host=self.host, port=self.port,
                username=self.username, password=self.password or 'validation-only',
                command='true', host_key_policy=self.host_key_policy,
                idempotency_key='validation-only')
        if len(set(self.project_ids)) != len(self.project_ids):
            raise ValueError('项目分配列表不能重复')
        return self


class VPSAssignments(Args):
    project_ids: list[str] = Field(max_length=500)
    expected_version: int = Field(ge=1)


class ProjectVPSAssignments(Args):
    vps_ids: list[str] = Field(max_length=500)
    expected_vps_ids: list[str] = Field(max_length=500)


class VPSService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store

    def public(self, row, visible_ids=None):
        result = {key: row[key] for key in PUBLIC_FIELDS}
        result['enabled'] = bool(result['enabled'])
        result['has_password'] = True
        projects = self.store.all('''SELECT p.id,p.alias,p.device_id,d.name AS device_name,
            p.mode,p.allow_tasks FROM vps_projects vp JOIN projects p ON p.id=vp.project_id
            JOIN devices d ON d.id=p.device_id WHERE vp.vps_id=? ORDER BY p.alias_key''', (row['id'],))
        result['projects'] = [{**p, 'online': self.runtime.online(p['device_id']),
                               'allow_tasks': bool(p['allow_tasks'])}
                              for p in projects if visible_ids is None or p['id'] in visible_ids]
        result['project_ids'] = [p['id'] for p in result['projects']]
        return result

    def get(self, identifier, principal):
        principal=iam.live_principal(self.store,principal)
        row = self.store.one(f'SELECT {PUBLIC_SQL} FROM vps_connections v WHERE v.id=? AND v.space_id=?', (identifier,principal.space_id))
        if not row:
            raise DevError('VPS_NOT_FOUND', '未找到 VPS 连接', 404)
        visible={p['id'] for p in self.runtime.list_projects(principal)}
        if not principal.admin and not self.store.one('SELECT 1 AS ok FROM vps_projects WHERE vps_id=? AND project_id IN (%s)' % (','.join('?' for _ in visible) or 'NULL'),(identifier,*sorted(visible))):
            raise DevError('VPS_NOT_FOUND','未找到已授权 VPS',404)
        return self.public(row,visible)

    def manage(self,principal,identifier=None):
        principal=iam.live_principal(self.store,principal)
        if principal.grant_id or not principal.admin:
            raise DevError('SPACE_ADMIN_REQUIRED','VPS 管理需要当前空间管理员',403)
        if identifier and not self.store.one('SELECT 1 AS ok FROM vps_connections WHERE id=? AND space_id=?',(identifier,principal.space_id)):
            raise DevError('VPS_NOT_FOUND','未找到当前空间 VPS',404)
        return principal

    def list(self, args, principal):
        projects = self.runtime.list_projects(principal)
        visible = {p['id'] for p in projects}
        chosen = self.runtime.project(args['project'], principal)['id'] if args.get('project') else None
        query = name_key(args.get('query', ''))
        permitted = {chosen} if chosen else (None if principal.admin else visible)
        if permitted is not None and not permitted:
            return {'vps': [], 'total': 0, 'next_offset': None}
        params = tuple(sorted(permitted)) if permitted is not None else ()
        where = ' WHERE v.space_id=?'
        if permitted is not None:
            placeholders = ','.join('?' for _ in params)
            where += f' AND EXISTS (SELECT 1 FROM vps_projects vp WHERE vp.vps_id=v.id AND vp.project_id IN ({placeholders}))'
        rows = self.store.all(f'SELECT {PUBLIC_SQL} FROM vps_connections v{where} ORDER BY v.name_key,v.id', (principal.space_id,*params))
        results = []
        for row in rows:
            if query and not any(query in name_key(str(row[key])) for key in ('name', 'host', 'provider', 'region')):
                # IPv6 can be supplied in a different, equivalent representation.
                if canonical_host(args.get('query', '')) != row['host']:
                    continue
            results.append(row)
        offset, limit = args.get('offset', 0), args.get('limit', 50)
        end = offset + limit
        return {'vps': [self.public(row, None if principal.admin else visible) for row in results[offset:end]], 'total': len(results),
                'next_offset': end if end < len(results) else None}

    def _validate_projects(self, identifiers, principal):
        if len(set(identifiers)) != len(identifiers):
            raise DevError('INVALID_PROJECTS', '项目分配列表不能重复')
        for identifier in identifiers:
            if not self.store.db.execute('SELECT 1 FROM projects WHERE id=? AND space_id=?', (identifier,principal.space_id)).fetchone():
                raise DevError('PROJECT_NOT_FOUND', '选择的项目已不存在，请刷新后重试', 404)

    def _bind(self, identifier, project_ids):
        existing = {r[0] for r in self.store.db.execute('SELECT project_id FROM vps_projects WHERE vps_id=?', (identifier,))}
        for project_id in existing - set(project_ids):
            self.store.db.execute('DELETE FROM vps_projects WHERE vps_id=? AND project_id=?', (identifier, project_id))
        for project_id in set(project_ids) - existing:
            self.store.db.execute('INSERT INTO vps_projects VALUES (?,?,?)', (identifier, project_id, uuid.uuid4().hex))

    def save(self, body, principal, identifier=None):
        principal=self.manage(principal,identifier)
        data = body.model_dump()
        existing_id = identifier
        identifier = identifier or uuid.uuid4().hex
        now = time.time()
        try:
            with self.store.lock, self.store.db:
                self.store.db.execute('BEGIN IMMEDIATE')
                principal=self.manage(principal,existing_id)
                old = self.store.db.execute('SELECT * FROM vps_connections WHERE id=?', (identifier,)).fetchone()
                if existing_id and not old:
                    raise DevError('VPS_NOT_FOUND', 'VPS 已删除，请刷新列表', 404)
                if old and body.expected_version != old['version']:
                    raise DevError('VPS_VERSION_CONFLICT', 'VPS 已在其他窗口修改，请重新打开后保存；未覆盖现有配置', 409)
                if not old and body.password is None:
                    raise DevError('VPS_PASSWORD_REQUIRED', '新建 VPS 需要填写 SSH 密码')
                self._validate_projects(body.project_ids,principal)
                secret = self.store.encrypt(body.password) if body.password is not None else old['secret']
                version = old['version'] + 1 if old else 1
                connection_changed = old and (body.password is not None or any(data[k] != old[k] for k in ('host', 'port', 'username', 'host_key_policy', 'enabled')))
                revision = old['connection_revision'] + int(bool(connection_changed)) if old else 1
                values = (body.name, name_key(body.name), body.host, body.port, body.username, secret,
                          body.host_key_policy, body.provider, body.region, body.system, body.notes,
                          int(body.enabled), version, revision, now)
                if old:
                    self.store.db.execute('''UPDATE vps_connections SET name=?,name_key=?,host=?,port=?,username=?,secret=?,
                        host_key_policy=?,provider=?,region=?,system=?,notes=?,enabled=?,version=?,connection_revision=?,updated=?
                        WHERE id=?''', (*values, identifier))
                else:
                    self.store.db.execute('''INSERT INTO vps_connections(name,name_key,host,port,username,secret,
                        host_key_policy,provider,region,system,notes,enabled,version,connection_revision,updated,id,created,space_id,owner_user_id)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (*values, identifier, now,principal.space_id,principal.user_id))
                self._bind(identifier, body.project_ids)
        except sqlite3.IntegrityError as exc:
            raise DevError('VPS_EXISTS', 'VPS 名称或相同地址、端口、账号的连接已存在', 409) from exc
        self.store.audit(principal.actor, 'vps.updated' if existing_id else 'vps.created', identifier,
                         detail={'project_ids': body.project_ids, 'version': version,
                                 'credential_changed': body.password is not None})
        self.runtime.publish('vps', {'id': identifier})
        return self.get(identifier,principal)

    def assign(self, identifier, body, principal):
        principal=self.manage(principal,identifier)
        with self.store.lock, self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            principal=self.manage(principal,identifier)
            row = self.store.db.execute('SELECT version FROM vps_connections WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise DevError('VPS_NOT_FOUND', '未找到 VPS 连接', 404)
            if row['version'] != body.expected_version:
                raise DevError('VPS_VERSION_CONFLICT', '分配已被修改，请刷新后重试', 409)
            self._validate_projects(body.project_ids,principal)
            self._bind(identifier, body.project_ids)
            self.store.db.execute('UPDATE vps_connections SET version=version+1,updated=? WHERE id=?', (time.time(), identifier))
        self.store.audit(principal.actor, 'vps.assigned', identifier, detail={'project_ids': body.project_ids})
        self.runtime.publish('vps', {'id': identifier})
        return self.get(identifier,principal)

    def project_assign(self, project_id, body, principal):
        principal=self.manage(principal)
        project = self.runtime.project(project_id, principal)
        with self.store.lock, self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            self.manage(principal)
            self._validate_projects([project['id']],principal)
            existing = {r[0] for r in self.store.db.execute('SELECT vps_id FROM vps_projects WHERE project_id=?', (project['id'],))}
            if existing != set(body.expected_vps_ids):
                raise DevError('VPS_VERSION_CONFLICT', '项目的 VPS 分配已变化，请刷新后重试', 409)
            target = set(body.vps_ids)
            if len(target) != len(body.vps_ids):
                raise DevError('INVALID_VPS', 'VPS 列表不能重复')
            for identifier in target:
                if not self.store.db.execute('SELECT 1 FROM vps_connections WHERE id=? AND space_id=?', (identifier,principal.space_id)).fetchone():
                    raise DevError('VPS_NOT_FOUND', '所选 VPS 已删除，请刷新后重试', 404)
            for identifier in existing - target:
                self.store.db.execute('DELETE FROM vps_projects WHERE project_id=? AND vps_id=?', (project['id'], identifier))
            for identifier in target - existing:
                self.store.db.execute('INSERT INTO vps_projects VALUES (?,?,?)', (identifier, project['id'], uuid.uuid4().hex))
            for identifier in existing ^ target:
                self.store.db.execute('UPDATE vps_connections SET version=version+1,updated=? WHERE id=?', (time.time(), identifier))
        self.store.audit(principal.actor, 'project.vps_assigned', project['id'], detail={'vps_ids': sorted(target)})
        self.runtime.publish('vps', {'project_id': project['id']})
        return {'ok': True, 'vps_ids': sorted(target)}

    def delete(self, identifier, version, principal):
        principal=self.manage(principal,identifier)
        with self.store.lock, self.store.db:
            self.store.db.execute('BEGIN IMMEDIATE')
            principal=self.manage(principal,identifier)
            row = self.store.db.execute('SELECT version FROM vps_connections WHERE id=?', (identifier,)).fetchone()
            if not row:
                return {'ok': True}
            if row['version'] != version:
                raise DevError('VPS_VERSION_CONFLICT', 'VPS 已变化，请刷新后再删除', 409)
            self.store.db.execute('DELETE FROM vps_connections WHERE id=?', (identifier,))
        self.store.audit(principal.actor, 'vps.deleted', identifier, detail={'remote_server_deleted': False})
        self.runtime.publish('vps', {'id': identifier})
        return {'ok': True, 'note': '仅删除保存的连接及项目分配，不删除远程服务器；已下发的命令不会自动停止。'}

    def reference(self, args, project):
        rows = self.store.all(f'''SELECT {PUBLIC_SQL},vp.binding_id FROM vps_connections v
            JOIN vps_projects vp ON vp.vps_id=v.id WHERE vp.project_id=? AND v.enabled=1''', (project['id'],))
        value = args.get('vps', '').strip()
        if value:
            exact_id = [r for r in rows if r['id'] == value]
            rows = exact_id or [r for r in rows if name_key(r['name']) == name_key(value) or r['host'] == canonical_host(value)]
        if args.get('port') is not None:
            rows = [r for r in rows if r['port'] == args['port']]
        if args.get('username'):
            rows = [r for r in rows if r['username'] == args['username']]
        if not rows:
            raise DevError('VPS_NOT_FOUND', '未找到此项目已分配且启用的 VPS；请调用 vps_list 或在面板分配连接', 404)
        if len(rows) != 1:
            choices = ', '.join(f"{r['name']} ({r['host']}:{r['port']} / {r['username']})" for r in rows[:10])
            raise DevError('VPS_AMBIGUOUS', '匹配到多个 VPS，请用 vps_list 查询并明确选择名称或 ID：' + choices, 409)
        row = rows[0]
        return {'id': row['id'], 'connection_revision': row['connection_revision'], 'binding_id': row['binding_id']}

    def _authorized_row(self, request, project_id, *, credential=False):
        ref = request.get('vps_ref') or {}
        columns = PUBLIC_SQL + (',v.secret' if credential else '')
        row = self.store.one(f'''SELECT {columns},vp.binding_id FROM vps_connections v
            JOIN vps_projects vp ON vp.vps_id=v.id WHERE v.id=? AND vp.project_id=?''', (ref.get('id'), project_id))
        if not row or not row['enabled'] or row['binding_id'] != ref.get('binding_id'):
            raise DevError('VPS_AUTHORIZATION_CHANGED', 'VPS 已停用、删除或取消/重新分配；尚未下发的命令不能继续执行', 403)
        if row['connection_revision'] != ref.get('connection_revision'):
            raise DevError('VPS_CONFIGURATION_CHANGED', 'VPS 连接或凭据已变化；请核对原操作后明确重新提交', 409)
        return row

    def permission_error(self, request, project_id):
        try:
            self._authorized_row(request, project_id)
        except DevError as exc:
            return exc.message
        return None

    def transport(self, request, project_id):
        row = self._authorized_row(request, project_id, credential=True)
        try:
            password = self.store.decrypt(row['secret'])
        except Exception as exc:
            raise DevError('VPS_CREDENTIAL_UNAVAILABLE', 'VPS 凭据无法解密，请检查 Hub 数据库与 master.key 是否配套', 409) from exc
        args = request['args']
        command = SSHExec(project=args['project'], host=row['host'], port=row['port'],
                          username=row['username'], password=password,
                          command=args['command'], host_key_policy=row['host_key_policy'],
                          timeout_seconds=args['timeout_seconds'], idempotency_key=args['idempotency_key'])
        return {**{k: v for k, v in request.items() if k != 'vps_ref'},
                'tool': 'ssh_exec', 'args': command.model_dump()}


def make_vps_router(auth, runtime):
    router = APIRouter()
    service = runtime.vps

    @router.get('/api/vps')
    async def list_vps(request: Request, project: str = Query('', max_length=100), query: str = Query('', max_length=253), offset: int = Query(0, ge=0, le=100000), limit: int = Query(200, ge=1, le=200)):
        principal = auth.panel(request)
        args = VPSList(project=project, query=query, offset=offset, limit=limit).model_dump()
        return service.list(args, principal)

    @router.post('/api/vps')
    async def create_vps(request: Request, body: VPSInput):
        return service.save(body, auth.panel(request, True))

    @router.get('/api/vps/{identifier}')
    async def get_vps(identifier: str, request: Request):
        return service.get(identifier,auth.panel(request))

    @router.put('/api/vps/{identifier}')
    async def update_vps(identifier: str, request: Request, body: VPSInput):
        return service.save(body, auth.panel(request, True), identifier)

    @router.put('/api/vps/{identifier}/projects')
    async def assign_vps(identifier: str, request: Request, body: VPSAssignments):
        return service.assign(identifier, body, auth.panel(request, True))

    @router.put('/api/projects/{identifier}/vps')
    async def assign_project(identifier: str, request: Request, body: ProjectVPSAssignments):
        return service.project_assign(identifier, body, auth.panel(request, True))

    @router.delete('/api/vps/{identifier}')
    async def delete_vps(identifier: str, request: Request, expected_version: int = Query(..., ge=1)):
        return service.delete(identifier, expected_version, auth.panel(request, True))

    return router
