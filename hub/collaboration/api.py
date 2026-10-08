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
                    'schema_version': 3, 'capabilities': service.chatroom.capabilities(),
                    'can_manage': bool(principal.admin and not principal.grant_id),
                    'delegation': service.delegation.authority_descriptor(),
                    'production_actions_enabled': False, 'platform_poc_verified': False}
        return await store.run(inspect)

    @router.get('/conversations')
    async def conversations(request: Request):
        return await store.run(lambda: service.conversations.listing(auth.panel(request)))

    @router.get('')
    async def read(request: Request, project: str, environment_id: str = 'production',
                   kind: str = 'overview', id: str = '', cursor: str = '',
                   room_id: str = '', conversation_id: str = '', after: str = '', query: str = '', client_message_id: str = '',
                   limit: int = Query(default=40, ge=1, le=100)):
        def inspect():
            principal = auth.panel(request)
            result = service.read({'project': project, 'environment_id': environment_id,
                                   'kind': kind, 'id': id, 'cursor': cursor, 'limit': limit,
                                   'room_id': room_id, 'conversation_id': conversation_id, 'after': after, 'query': query, 'client_message_id': client_message_id}, principal)
            return {**result, 'components': service.health}
        return await store.run(inspect)

    @router.get('/delegation-connection')
    async def delegation_connection(request: Request, project: str, policy_id: str, policy_version: int,
                                    environment_id: str = 'production', mode: str = 'notification_only'):
        return await store.run(lambda: service.delegation.consumer.connection({
            'project': project, 'environment_id': environment_id, 'policy_id': policy_id,
            'policy_version': policy_version, 'mode': mode}, auth.panel(request)))

    @router.get('/delegation-inbox')
    async def delegation_inbox(request: Request, project: str, policy_id: str, policy_version: int,
                               environment_id: str = 'production', mode: str = 'notification_only',
                               checkpoint: str = '', cursor: str = '', limit: int = Query(default=40, ge=1, le=100)):
        return await store.run(lambda: service.delegation.consumer.inbox({
            'project': project, 'environment_id': environment_id, 'policy_id': policy_id,
            'policy_version': policy_version, 'mode': mode, 'checkpoint': checkpoint,
            'cursor': cursor, 'limit': limit}, auth.panel(request)))

    @router.post('/{operation}')
    async def mutate(operation: str, body: dict, request: Request):
        def perform():
            principal = auth.admin(request, True)
            handlers = {'delegation-policy': service.delegation.set_policy,
                        'delegation-policy-control': service.delegation.control,
                        'delegation-remind': service.delegation.remind,
                        'message-remind': service.chatroom.remind,
                        'goal-create': service.coordination.create, 'goal-update': service.coordination.update,
                        'goal-approve': service.coordination.approve, 'goal-control': service.coordination.control,
                        'goal-message': service.coordination.message, 'work-create': service.coordination.work_create,
                        'work-assign': service.coordination.work_assign,
                        'conversation': service.conversations.create, 'conversation-project': service.conversations.add_project,
                        'message': service.chatroom.create, 'message-to-task': service.chatroom.to_task,
                        'message-access': service.chatroom.access, 'read-cursor': service.chatroom.set_read_cursor,
                        'room': service.room_create, 'agent': service.register_agent,
                        'join-slot': service.joining.create, 'join-slot-control': service.joining.control,
                        'command': lambda args, actor: service.command(args, actor, from_panel=True),
                        'control': service.control, 'probe': service.monitor.register_probe,
                        'plan-validate': service.monitor.validate_plan,
                        'plan-save': service.monitor.save_plan, 'plan-activate': service.monitor.activate_plan}
            if operation not in handlers:
                raise DevError('NOT_FOUND', '未知的协作管理操作', 404)
            return handlers[operation](body, principal)
        return await store.run(perform)

    return router
