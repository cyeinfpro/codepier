"""Room-first panel resources. All writes use live session + CSRF validation."""
from typing import Annotated
from fastapi import Query, Request, Response
from pydantic import Field
from shared.collaboration_contracts import Key, Model
from shared.util import DevError


class RoomDeliveryRetry(Model):
    expected_version: int = Field(strict=True, ge=1)
    idempotency_key: Key


def install_routes(router, auth, runtime):
    service, store = runtime.collaboration, runtime.store

    async def connection_read(request, response, action, args):
        response.headers['Cache-Control'] = 'private, no-store'
        return await store.run(lambda: service.rooms.invoke(action, args, auth.panel(request)))

    async def connection_write(request, response, action, path_args=None):
        response.headers['Cache-Control'] = 'private, no-store'
        body = await request.json()
        return await store.run(lambda: service.rooms.invoke(action,
            {**body, **(path_args or {})}, auth.admin(request, True)))

    @router.get('/connections')
    async def connections(request: Request, response: Response, cursor: str = '',
                          limit: int = Query(40, ge=1, le=100), host_connections: bool = False):
        args = {'cursor': cursor, 'limit': limit}
        if host_connections:
            return await connection_read(request, response, 'room_connection_list', args)
        response.headers['Cache-Control'] = 'private, no-store'
        return await store.run(lambda: service.room_connections.listing(args, auth.panel(request)))

    @router.post('/connections/rebind-batch')
    async def rebind_connections(request: Request, response: Response):
        return await connection_write(request, response, 'room_connection_rebind_batch')

    @router.get('/connections/rebind-batch/{batch_id}')
    async def rebind_status(request: Request, response: Response, batch_id: str, cursor: str = '',
                            limit: int = Query(40, ge=1, le=100)):
        return await connection_read(request, response, 'room_connection_rebind_status',
            {'batch_id': batch_id, 'cursor': cursor, 'limit': limit})

    @router.post('/connections/route-prepare')
    async def prepare_route(request: Request, response: Response):
        return await connection_write(request, response, 'room_connection_route_prepare')

    @router.get('/connections/{identifier}')
    async def host_connection(request: Request, response: Response, identifier: str):
        return await connection_read(request, response, 'room_connection_view', {'host_connection_id': identifier})

    @router.get('/connections/{identifier}/impact')
    async def connection_impact(request: Request, response: Response, identifier: str, cursor: str = '',
                                limit: int = Query(40, ge=1, le=100)):
        return await connection_read(request, response, 'room_connection_impact',
            {'host_connection_id': identifier, 'cursor': cursor, 'limit': limit})

    @router.post('/connections/{identifier}/revoke')
    async def revoke_connection(request: Request, response: Response, identifier: str):
        return await connection_write(request, response, 'room_connection_revoke', {'host_connection_id': identifier})

    @router.get('/rooms')
    async def listing(request: Request, cursor: str = '', limit: int = Query(40, ge=1, le=100), include_archived: bool = False):
        return await store.run(lambda: service.rooms.listing({'cursor': cursor, 'limit': limit,
            'include_archived': include_archived}, auth.panel(request)))

    @router.post('/rooms')
    async def create(request: Request):
        body = await request.json()
        return await store.run(lambda: service.rooms.create(body, auth.admin(request, True)))

    @router.get('/rooms/{identifier}')
    async def room(request: Request, identifier: str):
        return await store.run(lambda: service.rooms.read({'conversation_id': identifier}, auth.panel(request)))

    @router.post('/rooms/{identifier}/weixin-deliveries/{delivery_id}/retry')
    async def retry_weixin_delivery(request: Request, identifier: str, delivery_id: str, body: RoomDeliveryRetry):
        return await store.run(lambda: service.weixin.delivery.retry_room(
            identifier, delivery_id, body.model_dump(), auth.admin(request, True)))

    @router.get('/rooms/{identifier}/context')
    async def context(request: Request, response: Response, identifier: str, cursor: str = '',
                      limit: int = Query(20,ge=1,le=50),byte_budget: int = Query(16000,ge=4096,le=24000)):
        response.headers['Cache-Control']='private, no-store'
        return await store.run(lambda: service.room_context.owner_read(identifier,auth.panel(request),
            cursor=cursor,limit=limit,byte_budget=byte_budget))

    @router.patch('/rooms/{identifier}')
    async def update(request: Request, identifier: str):
        body = await request.json()
        return await store.run(lambda: service.rooms.update({**body, 'conversation_id': identifier}, auth.admin(request, True)))

    @router.get('/rooms/{identifier}/turn-policy')
    async def turn_policy(request: Request, response: Response, identifier: str):
        response.headers['Cache-Control'] = 'private, no-store'
        return await store.run(lambda: service.rooms.turn_policy({'conversation_id': identifier}, auth.instance(request)))

    @router.put('/rooms/{identifier}/turn-policy')
    async def update_turn_policy(request: Request, identifier: str):
        body = await request.json()
        return await store.run(lambda: service.rooms.update_turn_policy(
            {**body, 'conversation_id': identifier}, auth.instance(request, True)))

    @router.get('/rooms/{identifier}/permissions')
    async def permissions(request: Request, response: Response, identifier: str):
        response.headers['Cache-Control'] = 'private, no-store'
        return await store.run(lambda: service.room_permissions.read(identifier, auth.panel(request)))

    @router.post('/rooms/{identifier}/permissions/continuation-preview')
    async def continuation_preview(request: Request, response: Response, identifier: str):
        response.headers['Cache-Control'] = 'private, no-store'
        body = await request.json()
        return await store.run(lambda: service.room_goal_recovery.continuations.preview(
            {**body, 'conversation_id': identifier}, auth.admin(request, True)))

    @router.put('/rooms/{identifier}/permissions')
    async def permissions_update(request: Request, identifier: str):
        body = await request.json()
        return await store.run(lambda: service.room_goal_recovery.continuations.save(
            {**body, 'conversation_id': identifier}, auth.admin(request, True)))

    @router.get('/rooms/{identifier}/permissions/continuations/{batch_id}')
    async def continuation_status(request: Request, response: Response, identifier: str, batch_id: str):
        response.headers['Cache-Control'] = 'private, no-store'
        return await store.run(lambda: service.room_goal_recovery.continuations.status(
            identifier, batch_id, auth.panel(request)))

    @router.put('/rooms/{identifier}/projects')
    async def projects(request: Request, identifier: str):
        body = await request.json()
        return await store.run(lambda: service.rooms.projects({**body, 'conversation_id': identifier}, auth.admin(request, True)))

    @router.post('/rooms/{identifier}/migration')
    async def migrate_room(request: Request, identifier: str):
        body = await request.json()
        return await store.run(lambda: service.rooms.migrate_room({**body, 'conversation_id': identifier}, auth.admin(request, True)))

    @router.get('/rooms/{identifier}/projects/impact')
    async def project_impact(request: Request, response: Response, identifier: str,
                             expected_version: int = Query(ge=1),
                             removed_partition_ids: Annotated[list[str] | None, Query()] = None):
        response.headers['Cache-Control'] = 'private, no-store'
        return await store.run(lambda: service.rooms.project_impact({'conversation_id': identifier,
            'expected_version': expected_version, 'removed_partition_ids': removed_partition_ids or []}, auth.panel(request)))

    @router.post('/rooms/{identifier}/members')
    async def member(request: Request, identifier: str):
        body = await request.json()
        return await store.run(lambda: service.rooms.create_member({**body, 'conversation_id': identifier}, auth.admin(request, True)))

    @router.get('/connect-flows/{identifier}')
    async def flow(request: Request, response: Response, identifier: str):
        response.headers['Cache-Control'] = 'private, no-store'
        return await store.run(lambda: service.rooms.connect_read({'flow_id': identifier}, auth.panel(request)))

    @router.post('/connect-flows/{identifier}/actions')
    async def flow_action(request: Request, identifier: str):
        body = await request.json()
        return await store.run(lambda: service.rooms.connect_action({**body, 'flow_id': identifier}, auth.admin(request, True)))

    @router.get('/rooms/{identifier}/changes')
    async def changes(request: Request, identifier: str, cursor: str = '', limit: int = Query(40, ge=1, le=100)):
        return await store.run(lambda: service.rooms.changes({'conversation_id': identifier, 'cursor': cursor, 'limit': limit}, auth.panel(request)))

    @router.get('/rooms/{identifier}/events')
    async def events(request: Request, identifier: str, cursor: str = Query(default='', max_length=64)):
        from fastapi.responses import StreamingResponse
        if request.headers.get('sec-fetch-site') == 'cross-site':
            raise DevError('SAME_ORIGIN_REQUIRED', '实时提示需要当前站点会话', 403)
        resume = request.headers.get('last-event-id') or cursor
        if resume and (len(resume) != 64 or any(ch not in '0123456789abcdef' for ch in resume)):
            raise DevError('INVALID_CURSOR', '实时提示游标无效', 400)
        initial = await store.run(lambda: service.rooms.hint_snapshot(identifier, auth.panel(request)))
        queue = service.relay_hints.subscribe(initial['owner'])

        class RoomHintStream(StreamingResponse):
            async def __call__(self, scope, receive, send):
                try:
                    await super().__call__(scope, receive, send)
                finally:
                    service.relay_hints.unsubscribe(queue)

        return RoomHintStream(service.rooms.hint_stream(request, identifier, auth, queue, initial, resume),
            media_type='text/event-stream', headers={'Cache-Control': 'private, no-store',
            'X-Accel-Buffering': 'no', 'Vary': 'Cookie', 'X-Content-Type-Options': 'nosniff'})

    @router.get('/rooms/{identifier}/diagnostics')
    async def diagnostics(request: Request, response: Response, identifier: str):
        response.headers['Cache-Control'] = 'private, no-store'
        return await store.run(lambda: service.rooms.diagnostics({'conversation_id': identifier}, auth.panel(request)))
