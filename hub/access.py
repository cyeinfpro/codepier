"""Explicit, durable project grants; defaults never bypass application consent."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.util import DevError, valid_json_value
from hub import iam


def access_defaults(store, user_id):
    row = store.one('SELECT value FROM meta WHERE key=?', ('mcp_access_defaults:' + user_id,))
    try:
        value = json.loads(row['value']) if row else {}
    except (ValueError, TypeError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    # Legacy installations remain opt-in. Invalid stored values fail closed.
    return {key: value.get(key) is True for key in ('all_projects', 'developer_scopes')}


def project_selection(store, projects, all_projects=False, *, space_id="legacy"):
    if type(all_projects) is not bool:
        raise DevError('INVALID_PROJECT', '全部项目选项必须是布尔值')
    if (not isinstance(projects, list) or len(projects) > 1000
            or any(not isinstance(item, str) or not item or len(item) > 100 for item in projects)):
        raise DevError('INVALID_PROJECT', '请选择有效的项目范围')
    # The wildcard requires an explicit UI/API decision; it is not a project ID.
    if '*' in projects:
        raise DevError('INVALID_PROJECT', '请通过“全部现有及未来项目”选项明确授权')
    available = {row['id'] for row in store.all('SELECT id FROM projects WHERE space_id=?', (space_id,))}
    if not set(projects).issubset(available):
        raise DevError('INVALID_PROJECT', '项目已删除或不存在，请刷新项目列表')
    if all_projects:
        return ['*']
    if not projects:
        raise DevError('INVALID_PROJECT', '至少选择一个项目，或明确授权全部现有及未来项目')
    return sorted(set(projects))


def grant_revision(store, grant_id):
    row = store.one('SELECT value FROM meta WHERE key=?', ('grant_projects_revision:' + grant_id,))
    if not row:
        return 0
    try:
        value = int(row['value'])
        if value < 0:
            raise ValueError()
        return value
    except (ValueError, TypeError) as exc:
        raise DevError('GRANT_METADATA_INVALID', '授权版本记录异常，请先核查，未更改权限', 409) from exc


def bump_grant_revision(store, grant_id):
    value = grant_revision(store, grant_id) + 1
    store.db.execute('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                     ('grant_projects_revision:' + grant_id, str(value)))
    return value


class AccessModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @model_validator(mode='before')
    @classmethod
    def json_safe(cls, value):
        if not valid_json_value(value):
            raise ValueError('授权参数必须是有效的 UTF-8 JSON')
        return value


class AccessDefaultsInput(AccessModel):
    all_projects: bool
    developer_scopes: bool = False
    apply_to_existing: bool = False


class GrantProjectsInput(AccessModel):
    projects: list[str] = Field(default_factory=list, max_length=1000)
    all_projects: bool = False
    expected_projects: list[str] = Field(max_length=1000)
    expected_revision: int = Field(ge=0)


def make_access_router(auth, runtime):
    router = APIRouter()
    store = runtime.store

    @router.put('/api/settings/access')
    async def save_defaults(request: Request, body: AccessDefaultsInput):
        principal = auth.panel(request, True)
        if body.apply_to_existing and not principal.admin:
            raise DevError('SPACE_ADMIN_REQUIRED', '批量授权需要空间管理员', 403)
        if body.apply_to_existing and not body.all_projects:
            raise DevError('INVALID_PROJECT', '批量应用仅用于明确开启全部项目；缩小授权请逐项调整')
        defaults = body.model_dump(exclude={'apply_to_existing'})
        with store.lock, store.db:
            auth.panel(request, True)
            store.db.execute('INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                             ('mcp_access_defaults:' + principal.user_id, json.dumps(defaults)))
            changed = []
            if body.apply_to_existing:
                rows = store.db.execute('''SELECT g.id,g.projects FROM grants g
                    WHERE g.user_id=? AND g.space_id=? AND g.revoked=0 AND g.client_id IS NOT NULL AND g.profile_id IS NULL
                    AND EXISTS (SELECT 1 FROM tokens t WHERE t.grant_id=g.id AND t.kind IN ('access','refresh') AND t.expires>?)''',
                                        (principal.user_id, principal.space_id, time.time())).fetchall()
                for row in rows:
                    if json.loads(row['projects']) == ['*']:
                        continue
                    store.db.execute('UPDATE grants SET projects=? WHERE id=?', ('["*"]', row['id']))
                    bump_grant_revision(store, row['id'])
                    changed.append(row['id'])
            store.audit(principal.actor, 'access.defaults_updated', detail={
                **defaults, 'existing_oauth_grants': changed,
                'scopes_unchanged': True, 'tokens_unchanged': True})
        runtime.wake.set()
        return {'access_defaults': defaults, 'updated_grants': len(changed),
                'note': '默认选项已保存；新连接仍需确认授权。已有连接的工具权限、凭据和有效期保持不变。'}

    @router.get('/api/grants/{grant_id}/projects')
    async def read_grant_projects(grant_id: str, request: Request):
        principal = auth.panel(request)
        with store.lock:
            row = store.one('SELECT * FROM grants WHERE id=? AND user_id=? AND space_id=?', (grant_id, principal.user_id, principal.space_id))
            if not row:
                raise DevError('NOT_FOUND', '授权不存在', 404)
            return {'id': grant_id, 'label': row['label'], 'scopes': json.loads(row['scopes']),
                    'projects': json.loads(row['projects']), 'revoked': bool(row['revoked']),
                    'project_revision': grant_revision(store, grant_id), 'profile_id': row['profile_id']}

    @router.put('/api/grants/{grant_id}/projects')
    async def update_grant_projects(grant_id: str, request: Request, body: GrantProjectsInput):
        principal = auth.panel(request, True)
        with store.lock, store.db:
            principal = auth.panel(request, True)
            if not principal.admin:
                raise DevError("ROLE_REQUIRED", "成员请使用已分配的动态角色；不能扩展旧固定凭据", 403)
            row = store.db.execute('SELECT * FROM grants WHERE id=? AND user_id=? AND space_id=?',
                                   (grant_id, principal.user_id, principal.space_id)).fetchone()
            if not row:
                raise DevError('NOT_FOUND', '授权不存在', 404)
            if row['profile_id']:
                raise DevError('PROFILE_MANAGED_GRANT', '此连接由访问 Profile 管理；扩大范围请重新 OAuth 授权', 409)
            if row['revoked']:
                raise DevError('GRANT_REVOKED', '授权已撤销，不能通过编辑恢复', 409)
            before = json.loads(row['projects'])
            revision = grant_revision(store, grant_id)
            selected = project_selection(store, body.projects, body.all_projects, space_id=principal.space_id)
            # Replaying the same successful update is harmless. A stale dialog
            # must not overwrite another window's intervening scope reduction.
            if (before != body.expected_projects or revision != body.expected_revision) and before != selected:
                raise DevError('GRANT_CHANGED', '授权范围已在其他窗口修改，请重新打开后核对', 409)
            if before != selected:
                store.db.execute('UPDATE grants SET projects=? WHERE id=?', (json.dumps(selected), grant_id))
                revision = bump_grant_revision(store, grant_id)
                store.audit(principal.actor, 'grant.projects_updated', grant_id,
                            detail={'before': before, 'projects': selected, 'scopes_unchanged': True})
        runtime.wake.set()
        return {'id': grant_id, 'projects': selected, 'all_projects': selected == ['*'], 'project_revision': revision,
                'note': '项目范围已更新，下次工具调用生效；无需重新连接。已开始的任务不会自动停止。'}

    return router
