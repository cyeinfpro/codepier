"""Small HTTP/WS adapters; the service owns authority and lifetime policy."""

from __future__ import annotations

import asyncio
import contextlib

from anyio import CancelScope
from fastapi import APIRouter, Query, Request, Response, WebSocket
from starlette.websockets import WebSocketDisconnect

from shared.crypto import pack_channel, token, unpack_channel
from shared.ssh_terminal_channel import terminal_secure_channel
from shared.ssh_terminal_protocol import TerminalProtocolError, canonical_uuid, parse_control
from shared.util import DevError

from .agent_transport import TerminalAgentConnection
from .browser import BrowserAttachment, check_websocket_origin
from .models import AttachTicket, CreateSession, SessionAction


def make_terminal_router(auth, runtime):
    service = runtime.ssh_terminal
    store = runtime.store
    router = APIRouter()

    _register_http_routes(router, auth, store, service)
    _register_websockets(router, auth, runtime, store, service)
    return router


def _register_http_routes(router, auth, store, service):
    @router.get("/api/vps-terminal/capabilities")
    async def capabilities(
        request: Request,
        vps_id: str = Query(..., max_length=100),
        project_id: str = Query(..., max_length=100),
    ):
        principal = await store.run(auth.panel, request)
        return await service.capabilities(principal, vps_id, project_id)

    @router.get("/api/vps-terminal/sessions")
    async def sessions(request: Request):
        return await service.list(await store.run(auth.panel, request))

    @router.get("/api/vps-terminal/metrics")
    async def terminal_metrics(request: Request, response: Response):
        await store.run(auth.instance, request)
        response.headers["Cache-Control"] = "no-store"
        return service.metrics_snapshot()

    @router.post("/api/vps-terminal/sessions")
    async def create_session(request: Request, body: CreateSession):
        return await service.create(await store.run(auth.panel, request, True), body)

    @router.get("/api/vps-terminal/sessions/{session_id}")
    async def get_session(session_id: str, request: Request):
        session = await service.load(session_id, await store.run(auth.panel, request))
        return service.repository.public(session.row)

    @router.get("/api/vps-terminal/sessions/{session_id}/diagnostics")
    async def diagnostics(session_id: str, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        return await service.diagnostics(session_id, await store.run(auth.panel, request))

    @router.post("/api/vps-terminal/sessions/{session_id}/attach-ticket")
    async def attach_ticket(session_id: str, request: Request, body: AttachTicket):
        return await service.ticket(session_id, await store.run(auth.panel, request, True), body)

    @router.post("/api/vps-terminal/sessions/{session_id}/{action}")
    async def session_action(session_id: str, action: str, request: Request, body: SessionAction):
        if action not in {"close", "detach", "takeover"}:
            raise DevError("INVALID_ACTION", "未知终端动作", 404)
        return await service.action(
            session_id, await store.run(auth.panel, request, True), action, body
        )


def _register_websockets(router, auth, runtime, store, service):
    @router.websocket("/ws/vps-terminal")
    async def browser_socket(socket: WebSocket):
        await _browser_socket(socket, auth, store, service)

    @router.websocket("/agent/vps-terminal/{device_id}")
    async def agent_socket(socket: WebSocket, device_id: str):
        await _agent_socket(socket, device_id, runtime, store, service)


async def _browser_socket(socket, auth, store, service):
    slot = None
    try:
        check_websocket_origin(socket)
        slot = service.reserve_browser_socket()
        login = await store.run(auth.session, socket)
        service.bind_browser_socket(slot, login["user_id"])
        await socket.accept()
        first = await asyncio.wait_for(socket.receive(), service.config.auth_timeout_seconds)
        if first.get("type") == "websocket.disconnect":
            return
        if not isinstance(first.get("text"), str):
            raise TerminalProtocolError("INVALID_CONTROL", "终端首帧必须为认证消息")
        message = parse_control(
            first["text"], config=service.config, allowed_types={"authenticate"}
        )
        if message.get("protocol_version", 1) != 1:
            raise TerminalProtocolError("PROTOCOL_MISMATCH", "终端协议不兼容")
        session, resume, principal = await service.consume_ticket(message, socket)
        attachment = BrowserAttachment(
            service, socket, session, resume["renderer_id"], principal=principal
        )
        await attachment.run(resume)
    except (DevError, TerminalProtocolError) as exc:
        service.metrics.increment("protocol_reject_total")
        with contextlib.suppress(Exception):
            await socket.send_json(
                {"type": "error", "reason_code": exc.code, "recoverability": "none"}
            )
        with contextlib.suppress(Exception):
            status = getattr(exc, "status", 403)
            await socket.close(code=4401 if status == 401 else 1013 if status == 429 else 4403)
    except WebSocketDisconnect:
        pass
    except (TimeoutError, ConnectionError):
        with contextlib.suppress(Exception):
            await socket.close(code=4408)
    finally:
        service.release_browser_socket(slot)


def _agent_authority(device_id, main_connection, runtime, store):
    device = store.one("SELECT * FROM devices WHERE id=?", (device_id,))
    if (
        not device
        or not device["enabled"]
        or main_connection is None
        or not runtime.connection_authorized(device_id, main_connection)
    ):
        raise DevError("AGENT_UNAUTHORIZED", "节点终端通道未获授权", 403)
    return store.decrypt(device["secret"])


async def _receive_agent_hello(socket, channel, service):
    packet = await asyncio.wait_for(socket.receive_text(), service.config.auth_timeout_seconds)
    if len(packet) > 32 * 1024:
        raise TerminalProtocolError("FRAME_TOO_LARGE", "终端握手过大")
    hello = await unpack_channel(channel, packet)
    if hello.get("type") != "terminal_hello" or hello.get("protocol") != 1:
        raise TerminalProtocolError("PROTOCOL_MISMATCH", "终端协议不兼容")
    generation = canonical_uuid(hello.get("runtime_generation"))
    # Frame limits must be identical for a resumable byte stream. Other
    # local quotas may be stricter on either side. Configuration changes
    # require a drain/restart, never silently change an active protocol.
    limits = hello.get("limits", {})
    if any(
        limits.get(key) != getattr(service.config, key)
        for key in ("frame_bytes", "input_frame_bytes", "control_bytes")
    ):
        raise TerminalProtocolError("TERMINAL_CONFIG_MISMATCH", "Hub 与 Agent 终端帧配置不同")
    return generation


async def _watch_authority(connection, device_id, main_connection, runtime, store, service):
    while not connection.closed:
        await asyncio.sleep(min(5.0, service.config.lease_renew_seconds))
        if runtime.connections.get(device_id) is not main_connection:
            await connection.close()
            return
        try:
            await store.run(_agent_authority, device_id, main_connection, runtime, store)
        except DevError:
            await connection.close()
            return


async def _agent_socket(socket, device_id, runtime, store, service):
    connection = None
    watcher = None
    handshake_slot = None
    try:
        canonical_uuid(device_id)
        handshake_slot = service.reserve_agent_handshake(device_id)
        main_connection = runtime.connections.get(device_id)
        secret = await store.run(_agent_authority, device_id, main_connection, runtime, store)
        await socket.accept()
        challenge = token()
        await socket.send_json(
            {"type": "terminal_challenge", "protocol": 1, "challenge": challenge}
        )
        channel = terminal_secure_channel(secret, challenge, device_id, "hub")
        secret = None
        generation = await _receive_agent_hello(socket, channel, service)
        if runtime.connections.get(device_id) is not main_connection:
            raise TerminalProtocolError("AGENT_REPLACED", "主节点连接已变化")
        await store.run(_agent_authority, device_id, main_connection, runtime, store)
        connection = TerminalAgentConnection(
            socket,
            channel,
            generation,
            service.config,
            device_id=device_id,
            on_event=service.on_agent_event,
        )
        connection.main_connection = main_connection
        await socket.send_text(
            await pack_channel(
                channel,
                {"type": "terminal_ready", "protocol": 1, "runtime_generation": generation},
            )
        )
        await service.register_agent(connection)
        service.release_agent_handshake(handshake_slot)
        handshake_slot = None

        watcher = asyncio.create_task(
            _watch_authority(connection, device_id, main_connection, runtime, store, service),
            name="terminal-device-authority",
        )
        await connection.run()
    except (DevError, TerminalProtocolError):
        service.metrics.increment("protocol_reject_total")
    except Exception:
        # The normal Agent audit must not receive terminal packet content.
        pass
    finally:
        # ASGI may cancel at every checkpoint after a disconnect, not just
        # once. Shield only teardown; the original cancellation still escapes.
        with CancelScope(shield=True):
            service.release_agent_handshake(handshake_slot)
            await _cleanup_agent_socket(socket, service, connection, watcher)


async def _cleanup_agent_socket(socket, service, connection, watcher):
    if watcher:
        watcher.cancel()
    try:
        if connection:
            try:
                await connection.close()
            finally:
                await service.unregister_agent(connection)
    finally:
        if watcher:
            await asyncio.gather(watcher, return_exceptions=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(socket.close(), service.config.shutdown_timeout)
