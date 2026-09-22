"""Panel-only bridge to the explicitly installed host updater; never a Docker API."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field

from shared.util import DevError, VERSION


class CheckUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9._:-]+$')


class ApplyUpdate(CheckUpdate):
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


def make_panel_update_router(auth, runtime, client=None):
    router = APIRouter(prefix='/api/panel-update')
    bridge = client or UpdaterClient()

    @router.get('/status')
    async def status(request: Request, request_key: str = Query(default='', max_length=128, pattern=r'^[A-Za-z0-9._:-]*$')):
        auth.admin(request)
        if request_key and len(request_key) < 8:
            raise DevError('INVALID_KEY', '更新请求编号无效')
        try:
            data = await bridge.request('GET', '/status' + ('?' + urlencode({'request_key': request_key}) if request_key else ''))
        except DevError as exc:
            if exc.code not in {'UPDATER_NOT_CONFIGURED', 'UPDATER_UNAVAILABLE'}:
                raise
            return {'enabled': False, 'running_version': VERSION, 'reason': exc.message,
                    'code': exc.code, 'setup_command': 'sudo python3 scripts/panel_updater.py install --root "$PWD"'}
        return {**data, 'running_version': VERSION}

    async def submit(request, body, action):
        principal = auth.admin(request, True)
        payload = body.model_dump(exclude={'confirmation'})
        if action == 'apply' and body.confirmation != body.version:
            raise DevError('CONFIRMATION_REQUIRED', '请明确确认所检查的目标版本', 409)
        payload.update(current_version=VERSION, actor=principal.actor)
        runtime.store.audit(principal.actor, 'panel_update.requested', status='started',
                            detail={'action': action, 'request_key': body.idempotency_key,
                                    'version': payload.get('version', '')})
        data = await bridge.request('POST', '/' + action, payload)
        return JSONResponse(data, status_code=202)

    @router.post('/check')
    async def check(request: Request, body: CheckUpdate):
        return await submit(request, body, 'check')

    @router.post('/apply')
    async def apply(request: Request, body: ApplyUpdate):
        return await submit(request, body, 'apply')

    return router
