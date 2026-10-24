"""Panel-only bridge to the explicitly installed host updater; never a Docker API."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field

from shared.util import DevError, VERSION
from hub.panel_agent_rollout import PanelAgentRollouts
from scripts.panel_update_source import UpdateError, latest_release, repository_name, version_tuple


class CheckUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9._:-]+$')


class ApplyUpdate(CheckUpdate):
    update_agents: bool = False  # Old cached panels retain their original confirmation scope.
    version: str = Field(pattern=r'^[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}$')
    release_id: int = Field(gt=0)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirmation: str = Field(min_length=1, max_length=32)


class UpdaterClient:
    def __init__(self, socket_path=None):
        self.socket_path = socket_path if socket_path is not None else os.getenv('HUB_PANEL_UPDATE_SOCKET', '')

    async def request(self, method, path, body=None):
        if not self.socket_path or not Path(self.socket_path).is_absolute():
            raise DevError('UPDATER_NOT_CONFIGURED', '宿主机尚未启用面板更新服务', 503)
        transport = httpx.AsyncHTTPTransport(uds=self.socket_path, retries=0)
        try:
            async with httpx.AsyncClient(transport=transport, timeout=8, trust_env=False, follow_redirects=False) as client:
                async with client.stream(method, 'http://codepier-updater' + path, json=body) as response:
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 256 * 1024:
                            raise DevError('UPDATER_BAD_RESPONSE', '更新服务返回的数据超过限制', 502)
                    data = json.loads(content)
                    if not isinstance(data, dict):
                        raise ValueError('Invalid updater response')
                    if response.status_code >= 400:
                        error = data.get('error', {})
                        raise DevError(str(error.get('code', 'UPDATER_ERROR'))[:80],
                                       str(error.get('message', '更新服务拒绝请求'))[:1000], response.status_code)
                    if response.status_code not in (200, 202):
                        raise ValueError('Unexpected updater status')
                    return data
        except DevError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TypeError) as exc:
            raise DevError('UPDATER_UNAVAILABLE', '无法连接宿主机更新服务；已提交的更新结果需恢复连接后核实，请勿重复提交', 503) from exc


class ReleaseDiscovery:
    """One bounded, non-durable cache; never submits an updater operation."""
    ttl = 300

    def __init__(self):
        self.lock = asyncio.Lock()
        self.task = None
        self.repository = None
        self.cached = None
        self.expires = 0

    async def _refresh(self, repository):
        try:
            candidate = await asyncio.to_thread(latest_release, repository)
            result = {'discovery_candidate': candidate, 'discovery_checked_at': candidate['checked_at'],
                      'discovery_error': None}
        except UpdateError as exc:
            result = {'discovery_candidate': None, 'discovery_checked_at': None,
                      'discovery_error': {'code': exc.code, 'message': exc.message}}
        except Exception:
            result = {'discovery_candidate': None, 'discovery_checked_at': None,
                      'discovery_error': {'code': 'DISCOVERY_UNAVAILABLE', 'message': '暂时无法检查正式版本，请稍后重试'}}
        self.cached = result
        self.expires = time.monotonic() + self.ttl

    async def get(self, repository):
        repository_name(repository)
        while True:
            async with self.lock:
                if self.repository == repository and self.cached is not None and time.monotonic() < self.expires:
                    return copy.deepcopy(self.cached)
                if self.task is None or self.task.done():
                    self.repository, self.cached = repository, None
                    self.task = asyncio.create_task(self._refresh(repository))
                task = self.task
            # A disconnected browser must not cancel the shared fetch or start duplicate threads.
            await asyncio.shield(task)


def make_panel_update_router(auth, runtime, client=None):
    router = APIRouter(prefix='/api/panel-update')
    bridge = client or UpdaterClient()
    discovery = ReleaseDiscovery()
    rollouts = PanelAgentRollouts(runtime, bridge)
    runtime.panel_agent_rollouts = rollouts

    @router.get('/status')
    async def status(request: Request, request_key: str = Query(default='', max_length=128, pattern=r'^[A-Za-z0-9._:-]*$')):
        principal = auth.instance(request)
        rollout = await runtime.store.run(rollouts.view, principal, request_key)
        if request_key and len(request_key) < 8:
            raise DevError('INVALID_KEY', '更新请求编号无效')
        try:
            data = await bridge.request('GET', '/status' + ('?' + urlencode({'request_key': request_key}) if request_key else ''))
        except DevError as exc:
            if exc.code not in {'UPDATER_NOT_CONFIGURED', 'UPDATER_UNAVAILABLE'}:
                raise
            return {'enabled': False, 'running_version': VERSION, 'reason': exc.message,
                    'code': exc.code, 'agent_rollout': rollout, 'setup_command': 'sudo python3 scripts/panel_updater.py install --root "$PWD"'}
        operation = data.get('operation') or {}
        if not request_key and operation.get('kind') == 'apply':
            rollout = await runtime.store.run(rollouts.view, principal, operation.get('request_key', ''))
        capabilities = data.get('capabilities') or {}
        storage_ready = capabilities.get('default_split_state') is True
        host_ready = capabilities.get('verified_engine_updates') is True
        notice = ('' if storage_ready and host_ready else
            '当前宿主更新器不具备完整自动升级能力：可以直接更新 Hub，但存储分离和宿主更新器升级不会随此操作自动生效。需管理员安装新版宿主更新器；先更新过渡 Hub 也不能补足旧宿主权限。')
        return {**data, 'running_version': VERSION, 'agent_rollout': rollout,
                'host_setup_required': not (storage_ready and host_ready), 'host_update_notice': notice}

    @router.get('/available')
    async def available(request: Request):
        # Use the existing authenticated status path, including its pending operation.
        data = await status(request, '')
        if request.query_params:
            raise DevError('INVALID_REQUEST', '版本发现不接受自定义来源或其他参数')
        result = {'discovery_candidate': None, 'discovery_available': False,
                  'discovery_checked_at': None, 'discovery_error': None}
        if not data.get('enabled') or data.get('busy') or data.get('recovery_required'):
            return {**data, **result}
        try:
            repository = repository_name(data.get('repository'))
            result.update(await discovery.get(repository))
            candidate = result['discovery_candidate']
            result['discovery_available'] = bool(candidate and
                version_tuple(candidate['version']) > version_tuple(VERSION))
        except UpdateError as exc:
            result.update(discovery_candidate=None, discovery_available=False,
                          discovery_checked_at=None,
                          discovery_error={'code': exc.code, 'message': exc.message})
        return {**data, **result}

    async def submit(request, body, action):
        principal = auth.instance(request, True)
        payload = body.model_dump(exclude={'confirmation', 'update_agents'})
        if action == 'apply' and body.confirmation != body.version:
            raise DevError('CONFIRMATION_REQUIRED', '请明确确认所检查的目标版本', 409)
        payload.update(current_version=VERSION, actor=principal.actor)
        runtime.store.audit(principal.actor, 'panel_update.requested', status='started',
                            detail={'action': action, 'request_key': body.idempotency_key,
                                    'version': payload.get('version', '')})
        created = False
        if action == 'apply' and body.update_agents:
            consent = await runtime.store.run(runtime.store.one,
                'SELECT request_key FROM panel_update_consents WHERE request_key=?', (body.idempotency_key,))
            if not consent:
                previous = await bridge.request('GET', '/status?' + urlencode({'request_key': body.idempotency_key}))
                if previous.get('request_found') is True:
                    raise DevError('AGENT_CONSENT_NOT_RECORDED', '原更新没有记录 Agent 更新确认；不能扩大原请求，请查看原操作', 409)
                if previous.get('request_found') is not False:
                    raise DevError('UPDATER_UNAVAILABLE', '无法核实原更新请求；未发起 Agent 更新，请恢复连接后核实', 503)
        if action == 'apply':
            created = await runtime.store.run(rollouts.confirm, body.model_dump(exclude={'confirmation'}), principal)
        try:
            data = await bridge.request('POST', '/' + action, payload)
        except DevError as exc:
            if created and 400 <= exc.status < 500:
                await runtime.store.run(rollouts.rejected, body.idempotency_key, exc.message)
            raise
        return JSONResponse(data, status_code=202)

    @router.post('/check')
    async def check(request: Request, body: CheckUpdate):
        return await submit(request, body, 'check')

    @router.post('/apply')
    async def apply(request: Request, body: ApplyUpdate):
        return await submit(request, body, 'apply')

    return router
