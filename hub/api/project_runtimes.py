"""Project mapping → execution environment. No shell-based config editing."""
from fastapi import APIRouter, Request, Path
from hub.db_worker import database_endpoint
from hub.api.context import HubContext
from hub.runtime_panel import authorize, availability, receipt
from shared.runtime_panel_contracts import DEFAULT_MODE, RuntimeSave
from shared.util import DevError


def make_project_runtimes_router(context: HubContext):
    router = APIRouter()
    store, runtime, auth = context.store, context.runtime, context.auth

    def resolve(identifier, request, write=False):
        principal = auth.admin(request, write)
        project = runtime.project(identifier, principal)
        if project['id'] != identifier:
            raise DevError('RUNTIME_PROJECT_MISMATCH', '请使用项目的稳定 ID 打开执行环境', 409)
        principal = authorize(runtime, principal, project)
        return principal, project

    @router.get('/api/projects/{identifier}/runtime')
    async def get_runtime(identifier: str, request: Request):
        principal, project = await store.run(resolve, identifier, request)
        state = await store.run(availability, runtime, project)
        if state != 'supported':
            return {'project_id': identifier, 'state': state, 'can_save': False,
                'default_mode': DEFAULT_MODE, 'profile': None, 'runtime': None,
                'revision': None, 'observed_runtime': False}
        value = await runtime.invoke('project_runtime_get', {'project': identifier}, principal)
        await store.run(resolve, identifier, request)  # Original session/Space after the await.
        return value

    @router.put('/api/projects/{identifier}/runtime')
    async def set_runtime(identifier: str, request: Request, body: RuntimeSave):
        principal, project = await store.run(resolve, identifier, request, True)
        # Recover an uncertain submission through the read-only receipt endpoint.
        # New saves require a connected, compatible Agent.
        state = await store.run(availability, runtime, project)
        if state != 'supported':
            raise DevError('AGENT_OFFLINE' if state == 'offline' else 'AGENT_UPGRADE_REQUIRED',
                '节点离线或尚未支持面板执行环境设置；未提交新的保存，请先核对节点版本', 409)
        value = await runtime.invoke('project_runtime_set', {'project': identifier, **body.model_dump()}, principal)
        await store.run(resolve, identifier, request, True)
        return value

    @router.get('/api/projects/{identifier}/runtime/receipts/{key}')
    @database_endpoint(store)
    def runtime_receipt(identifier: str, request: Request,
                        key: str = Path(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')):
        principal, project = resolve(identifier, request)
        return receipt(runtime, principal, project, key)

    return router
