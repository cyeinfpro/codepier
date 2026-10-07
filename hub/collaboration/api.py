"""Panel entry: authenticated sessions, current project scopes and CSRF checks."""
from dataclasses import asdict
from fastapi import APIRouter, Query, Request
from shared.util import DevError


def make_router(auth, runtime):
    router = APIRouter(prefix='/api/collaboration')
    service, store = runtime.collaboration, runtime.store

    @router.get('/status')
    async def status(request: Request):
        def inspect():
            principal = auth.panel(request)
            return {'features': asdict(service.config), 'components': service.health,
                    'can_manage': bool(principal.admin and not principal.grant_id),
                    'production_actions_enabled': False, 'platform_poc_verified': False}
        return await store.run(inspect)

    @router.get('')
    async def read(request: Request, project: str, environment_id: str = 'production',
                   kind: str = 'overview', id: str = '', cursor: str = '',
                   limit: int = Query(default=40, ge=1, le=100)):
        def inspect():
            principal = auth.panel(request)
            result = service.read({'project': project, 'environment_id': environment_id,
                                   'kind': kind, 'id': id, 'cursor': cursor, 'limit': limit}, principal)
            return {**result, 'components': service.health}
        return await store.run(inspect)

    @router.post('/{operation}')
    async def mutate(operation: str, body: dict, request: Request):
        def perform():
            principal = auth.admin(request, True)
            handlers = {'room': service.room_create, 'agent': service.register_agent,
                        'command': lambda args, actor: service.command(args, actor, from_panel=True),
                        'control': service.control, 'probe': service.monitor.register_probe,
                        'plan-validate': service.monitor.validate_plan,
                        'plan-save': service.monitor.save_plan, 'plan-activate': service.monitor.activate_plan}
            if operation not in handlers:
                raise DevError('NOT_FOUND', '未知的协作管理操作', 404)
            return handlers[operation](body, principal)
        return await store.run(perform)

    return router
