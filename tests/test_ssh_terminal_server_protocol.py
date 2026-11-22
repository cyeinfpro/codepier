"""Real transport admission before ASGI assembly, with synthetic echo endpoints."""

import asyncio
import contextlib
import struct

import pytest
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import ConnectionClosedError
from websockets.protocol import State

from hub.ssh_terminal.server_protocol import TerminalAwareWebSocketProtocol
from tests.fixture_hub import bind_listener


@contextlib.asynccontextmanager
async def server():
    received = []

    async def echo(socket):
        await socket.accept()
        try:
            while True:
                data = await socket.receive_bytes()
                received.append(len(data))
                await socket.send_text(str(len(data)))
        except WebSocketDisconnect:
            pass

    app = Starlette(routes=[WebSocketRoute("/ws/vps-terminal", echo),
                            WebSocketRoute("/agent/vps-terminal/{device_id}", echo),
                            WebSocketRoute("/ordinary", echo)])
    listener = bind_listener()
    uv = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False,
                                      ws=TerminalAwareWebSocketProtocol, ws_max_size=16 * 1024 * 1024))
    task = asyncio.create_task(uv.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not uv.started:
                await asyncio.sleep(0.01)
        yield f"ws://127.0.0.1:{listener.getsockname()[1]}", received
    finally:
        uv.should_exit = True
        await asyncio.wait_for(task, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("path,maximum", [
    ("/ws/vps-terminal", 16384 + 62),
    ("/%77s/vps-terminal", 16384 + 62),
    ("/agent/vps-terminal/fixture", 192 * 1024),
])
async def test_terminal_transport_rejects_oversized_message_before_asgi(path, maximum):
    async with server() as (url, received):
        async with websockets.connect(url + path, proxy=None) as client:
            assert not client.protocol.extensions
            await client.send(b"a" * maximum)
            assert await client.recv() == str(maximum)
            await client.send(b"b" * (maximum + 1))
            with pytest.raises(ConnectionClosedError) as error:
                await client.recv()
            assert error.value.rcvd.code == 1009
            assert received == [maximum]


@pytest.mark.asyncio
async def test_fragmented_browser_input_is_bounded_before_whole_message():
    async with server() as (url, received):
        async with websockets.connect(url + "/ws/vps-terminal", proxy=None) as client:
            await client.send([b"a" * 9000, b"b" * 9000])
            with pytest.raises(ConnectionClosedError) as error:
                await client.recv()
            assert error.value.rcvd.code == 1009
            assert not received


@pytest.mark.asyncio
async def test_pipelined_oversized_first_frame_is_rejected_before_payload_arrives():
    from unittest.mock import Mock
    from uvicorn.server import ServerState
    from websockets.exceptions import PayloadTooBig

    async def forbidden_app(scope, receive, send):
        raise AssertionError("Pipelined oversized input must never reach ASGI")

    state = ServerState()
    protocol = TerminalAwareWebSocketProtocol(uvicorn.Config(forbidden_app, log_level="error"), state, {})
    protocol.transport = Mock()
    request = ("GET /ws/vps-terminal HTTP/1.1\r\nHost: 127.0.0.1\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n").encode()
    # Feed the actual protocol directly: Uvicorn's H11 upgrade reconstructs
    # HTTP headers and discards its own trailing data. This also covers other
    # upgrade adapters which preserve pipelined bytes. No large payload exists.
    protocol.data_received(request + b"\x82\xff" + struct.pack("!Q", 2 * 1024 * 1024))
    assert isinstance(protocol.conn.parser_exc, PayloadTooBig)
    protocol.transport.close.assert_called_once_with()
    assert not state.tasks and protocol.queue.empty()


@pytest.mark.asyncio
async def test_ordinary_agent_route_retains_its_existing_large_message_contract():
    async with server() as (url, received):
        async with websockets.connect(url + "/ordinary", proxy=None) as client:
            assert client.protocol.extensions
            await client.send(b"ordinary-envelope" * 65536)
            assert int(await client.recv()) == len(b"ordinary-envelope") * 65536
            assert len(received) == 1


def test_single_tcp_read_cannot_grow_terminal_asgi_queue_without_limit():
    from types import SimpleNamespace
    from unittest.mock import Mock

    protocol = TerminalAwareWebSocketProtocol.__new__(TerminalAwareWebSocketProtocol)
    protocol.terminal_connection, protocol.close_sent, protocol.read_paused = True, False, False
    protocol.queue = asyncio.Queue()
    protocol.transport = Mock()
    protocol.conn = SimpleNamespace(send_close=Mock(), state=State.OPEN)
    protocol.handle_parser_exception = Mock()
    protocol.curr_msg_data_type = "bytes"
    for _ in range(16):
        protocol.frames = [b"one"]
        protocol.send_receive_event_to_app()
    assert protocol.queue.qsize() == 16
    protocol.frames = [b"overflow"]
    protocol.send_receive_event_to_app()
    assert protocol.queue.qsize() == 16 and not protocol.frames
    protocol.conn.send_close.assert_called_once_with(1008, "TERMINAL_QUEUE_SATURATED")
    protocol.handle_parser_exception.assert_called_once_with()


@pytest.mark.asyncio
async def test_pipelined_small_frames_are_bounded_before_asgi_accepts_upgrade():
    from unittest.mock import Mock
    from uvicorn.server import ServerState

    async def pending_authentication(scope, receive, send):
        await asyncio.Event().wait()

    state = ServerState()
    protocol = TerminalAwareWebSocketProtocol(uvicorn.Config(pending_authentication, log_level="error"), state, {})
    protocol.transport = Mock()
    request = ("GET /ws/vps-terminal HTTP/1.1\r\nHost: 127.0.0.1\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n").encode()
    # Real SansIO parser, still CONNECTING: no mock send_close hiding an
    # InvalidState exception and no unbounded queue before authentication.
    try:
        protocol.data_received(request + (b"\x82\x81" + b"\0" * 4 + b"x") * 64)
        assert protocol.conn.state is State.CONNECTING
        assert protocol.close_sent and protocol.conn.close_sent is None
        assert protocol.queue.qsize() <= 16 and not protocol.frames
        protocol.transport.close.assert_called_once_with()
    finally:
        tasks = tuple(state.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@contextlib.asynccontextmanager
async def open_parser(path):
    """Real HTTP upgrade and SansIO parser with ASGI reads deliberately paused."""
    from unittest.mock import Mock
    from uvicorn.server import ServerState

    accepted = asyncio.Event()

    async def pending_consumer(scope, receive, send):
        assert (await receive())["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})
        accepted.set()
        await asyncio.Event().wait()

    state = ServerState()
    protocol = TerminalAwareWebSocketProtocol(uvicorn.Config(pending_consumer, log_level="error"), state, {})
    protocol.transport = Mock()
    protocol.transport.is_closing.return_value = False
    request = (f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
               "Sec-WebSocket-Version: 13\r\n\r\n").encode()
    try:
        protocol.data_received(request)
        await asyncio.wait_for(accepted.wait(), 2)
        assert protocol.conn.state is State.OPEN and protocol.queue.empty()
        yield protocol
    finally:
        protocol.stop_keepalive()
        tasks = tuple(state.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def masked_frame(data, *, text=False):
    from websockets.frames import Frame, Opcode
    return Frame(Opcode.TEXT if text else Opcode.BINARY, data).serialize(mask=True)


@pytest.mark.asyncio
async def test_agent_legal_coalesced_multisession_burst_and_asgi_consumption_releases_budget():
    async with open_parser("/agent/vps-terminal/fixture") as protocol:
        # One actual parser receive: 16 sessions' data/control replies can exceed
        # the browser's 16-event queue without exhausting the shared wire budget.
        text = "真实回复".encode() * 256
        payloads = [text, b"b" * 4096] * 64
        protocol.data_received(b"".join(masked_frame(data, text=index % 2 == 0)
                                         for index, data in enumerate(payloads)))
        assert not protocol.close_sent
        assert protocol.queue.qsize() == 128
        assert protocol.queue.payload_bytes == sum(map(len, payloads))
        for index, data in enumerate(payloads):
            event = await protocol.receive()
            assert set(event) == {"type", "text" if index % 2 == 0 else "bytes"}
            assert event.get("bytes", event.get("text", "").encode()) == data
        assert protocol.queue.empty() and protocol.queue.payload_bytes == 0
        assert not protocol.read_paused
        # A second full burst can use the entire released payload budget.
        protocol.data_received(masked_frame(b"z" * 131072) * 8)
        assert not protocol.close_sent
        assert protocol.queue.payload_bytes == 1024 * 1024


@pytest.mark.asyncio
async def test_agent_total_byte_budget_rejects_before_enqueue_even_under_message_cap():
    async with open_parser("/agent/vps-terminal/fixture") as protocol:
        protocol.data_received(masked_frame(b"z" * 131072) * 8)
        assert not protocol.close_sent and protocol.queue.payload_bytes == 1024 * 1024
        protocol.data_received(masked_frame(b"overflow"))
        assert protocol.close_sent and protocol.conn.close_sent.code == 1008
        assert protocol.conn.close_sent.reason == "TERMINAL_QUEUE_SATURATED"
        assert protocol.queue.payload_bytes == 1024 * 1024
        # Only the existing payload plus one fixed disconnect marker is queued.
        assert protocol.queue.qsize() == 9


@pytest.mark.asyncio
async def test_agent_message_count_budget_rejects_tiny_message_flood():
    async with open_parser("/agent/vps-terminal/fixture") as protocol:
        protocol.data_received(masked_frame(b"x") * 256)
        assert not protocol.close_sent and protocol.queue.qsize() == 256
        protocol.data_received(masked_frame(b"x"))
        assert protocol.close_sent and protocol.conn.close_sent.code == 1008
        assert protocol.queue.payload_bytes == 256
        assert protocol.queue.qsize() == 257  # includes fixed disconnect marker


@pytest.mark.asyncio
async def test_ordinary_route_queue_is_unchanged_for_coalesced_bursts():
    async with open_parser("/ordinary") as protocol:
        original_queue = protocol.queue
        assert type(original_queue) is asyncio.Queue
        protocol.data_received(masked_frame(b"ordinary") * 300)
        assert not protocol.close_sent and protocol.queue is original_queue
        assert protocol.queue.qsize() == 300
