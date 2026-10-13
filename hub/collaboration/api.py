"""Panel entry: authenticated sessions, current project scopes and CSRF checks."""
from fastapi import APIRouter, Query, Request, Response
from urllib.parse import quote
from shared.util import DevError


def make_router(auth, runtime):
    router = APIRouter(prefix='/api/collaboration')
    service, store = runtime.collaboration, runtime.store

    @router.get('/status')
    async def status(request: Request):
        def inspect():
            principal = auth.panel(request)
            return {'features': service.features(principal), 'components': service.health,
                    'schema_version': 3, 'capabilities': service.chatroom.capabilities(principal),
                    'can_manage': bool(principal.admin and not principal.grant_id),
                    'delegation': service.delegation.authority_descriptor(),
                    'production_actions_enabled': False, 'platform_poc_verified': False}
        return await store.run(inspect)

    @router.get('/room-scopes')
    async def room_scopes(request: Request, project: str, environment_id: str = 'production', conversation_id: str = ''):
        return await store.run(lambda: service.room_scope.listing(
            {'project': project, 'environment_id': environment_id, 'conversation_id': conversation_id}, auth.panel(request)))

    @router.get('/weixin')
    async def weixin(request: Request, project: str, environment_id: str = 'production', conversation_id: str = ''):
        return await store.run(lambda: service.weixin.listing({'project': project,
            'environment_id': environment_id, 'conversation_id': conversation_id}, auth.panel(request)))

    @router.get('/telegram')
    async def telegram(request: Request, project: str, environment_id: str = 'production', conversation_id: str = ''):
        return await store.run(lambda: service.telegram.listing({'project': project,
            'environment_id': environment_id, 'conversation_id': conversation_id}, auth.panel(request)))

    def attachment_actor(request):
        # A supplied invalid Bearer token never falls back to a wider panel session.
        principal = auth.bearer(request) if request.headers.get('authorization') else auth.panel(request)
        if 'read' not in principal.scopes:
            raise DevError('INSUFFICIENT_SCOPE', '附件读取需要当前只读权限', 403)
        return principal

    @router.get('/attachments/{identifier}')
    async def attachment(request: Request, identifier: str):
        return await store.run(lambda: service.channels.media.public(
            service.channels.media.row(identifier, attachment_actor(request))))

    @router.get('/attachments/{identifier}/content')
    async def attachment_content(request: Request, identifier: str):
        content, row, start, end, partial = await store.run(lambda: service.channels.media.content(
            identifier, attachment_actor(request), request.headers.get('range')))
        headers = {'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
            'Accept-Ranges': 'bytes', 'Vary': 'Authorization, Cookie', 'ETag': '"' + row['sha256'] + '"',
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(row['name'], safe=''),
            'Content-Security-Policy': "sandbox; default-src 'none'"}
        if partial:
            headers['Content-Range'] = f"bytes {start}-{end}/{row['size']}"
        return Response(content, status_code=206 if partial else 200,
                        media_type='application/octet-stream', headers=headers)

    @router.get('/conversations')
    async def conversations(request: Request, include_archived: bool = False):
        return await store.run(lambda: service.conversations.listing(auth.panel(request), include_archived=include_archived))

    @router.get('')
    async def read(request: Request, project: str, environment_id: str = 'production',
                   kind: str = 'overview', id: str = '', cursor: str = '',
                   result_id: str = '', job_id: str = '',
                   room_id: str = '', conversation_id: str = '', after: str = '', query: str = '', client_message_id: str = '',
                   limit: int = Query(default=40, ge=1, le=100)):
        def inspect():
            principal = auth.panel(request)
            result = service.read({'project': project, 'environment_id': environment_id,
                                   'kind': kind, 'id': id, 'cursor': cursor, 'limit': limit,
                                   'result_id': result_id, 'job_id': job_id,
                                   'room_id': room_id, 'conversation_id': conversation_id, 'after': after, 'query': query, 'client_message_id': client_message_id}, principal)
            return {**result, 'components': service.health}
        return await store.run(inspect)

    @router.get('/relay-sync')
    async def relay_sync(request: Request, project: str, environment_id: str = 'production',
                         conversation_id: str = '', after: str = '', tracked: str = Query(default='', max_length=3100)):
        return await store.run(lambda: service.dot_relay.sync({
            'project': project, 'environment_id': environment_id,
            'conversation_id': conversation_id, 'after': after}, auth.panel(request), tracked))

    @router.get('/message-receipt')
    async def message_receipt(request: Request, response: Response, project: str, client_message_id: str,
                              environment_id: str = 'production', conversation_id: str = ''):
        response.headers['Cache-Control'] = 'private, no-store'
        return await store.run(lambda: service.dot_relay.message_receipt({
            'project': project, 'environment_id': environment_id, 'conversation_id': conversation_id,
            'client_message_id': client_message_id}, auth.panel(request)))

    @router.get('/diagnostics')
    async def diagnostics(request: Request, response: Response, project: str,
                          environment_id: str = 'production', conversation_id: str = ''):
        response.headers['Cache-Control'] = 'private, no-store'
        return await store.run(lambda: service.dot_relay.diagnostics({
            'project': project, 'environment_id': environment_id, 'conversation_id': conversation_id},
            auth.panel(request)))

    @router.get('/dot-recovery')
    async def dot_recovery(request: Request, project: str, dot_id: str, environment_id: str = 'production',
                           cursor: str = '', limit: int = Query(default=20, ge=1, le=50), include_completed: bool = False):
        return await store.run(lambda: service.task_liveness.recovery({
            'project': project, 'environment_id': environment_id, 'dot_id': dot_id,
            'cursor': cursor, 'limit': limit, 'include_completed': include_completed}, auth.panel(request)))

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
    async def mutate(operation: str, body: dict, request: Request, response: Response):
        response.headers['Cache-Control'] = 'private, no-store'
        # QR provider I/O must never hold the Store transaction/thread lock.
        if operation in {'weixin-qr-start', 'weixin-qr-status'}:
            principal = await store.run(lambda: auth.admin(request, True))
            handler = service.weixin.qr.start if operation == 'weixin-qr-start' else service.weixin.qr.status
            return await handler(body, principal)
        def perform():
            principal = auth.admin(request, True)
            handlers = {'dot': service.dots.create,
                        'conversation-archive-preview': service.conversations.archive.preview,
                        'conversation-archive': service.conversations.archive.archive,
                        'conversation-restore': service.conversations.archive.restore,
                        'conversation-project-preview': service.conversations.project_scopes.preview,
                        'conversation-projects-preview': service.conversations.working_scopes.preview,
                        'conversation-projects': service.conversations.working_scopes.apply,
                        'conversation-project-scope-control': service.conversations.project_scopes.control,
                        'room-scope': service.room_scope.set,
                        'room-scope-control': service.room_scope.control,
                        'telegram-account': service.telegram.import_account,
                        'telegram-bind': service.telegram.bind,
                        'channel-enrollment-preview': service.channel_enrollment.preview,
                        'telegram-owner-authority': lambda args, actor: service.channel_owner.control(args, actor, channel='telegram'),
                        'telegram-control': service.telegram.control,
                        'telegram-files-preview': service.telegram.files.preview,
                        'telegram-files': service.telegram.files.approve,
                        'channel-delivery-control': service.channels.control,
                        'weixin-mirror-consent': service.channels.consent_weixin,
                        'channel-settings': service.channel_settings.control,
                        'weixin-qr-accept': service.weixin.qr.accept,
                        'weixin-qr-cancel': service.weixin.qr.cancel,
                        'weixin-account': service.weixin.import_account,
                        'weixin-bind': service.weixin.bind,
                        'weixin-control': service.weixin.control,
                        'weixin-owner-request': service.weixin.owner_request,
                        'weixin-owner-authority': service.channel_owner.control,
                        'weixin-files': service.weixin.media.add_artifacts,
                        'weixin-files-preview': service.weixin.media.review.preview,
                        'weixin-files-reviewed': service.weixin.media.review.approve,
                        'weixin-approve-files': service.weixin.media.approve,
                        'weixin-delivery-control': service.weixin.delivery.control,
                        'delegation-policy': service.delegation.set_policy,
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
