"""Apply terminal wire budgets before ASGI receives a complete message.

The pinned Uvicorn SansIO protocol negotiates extensions during handle_connect.
Other CodePier WS routes keep their original transport contract. Terminals get
their own message/queued-message limits, including fragmented input, and do not
negotiate compression. Run the production Hub entry point to apply this layer.
"""

import asyncio
from urllib.parse import unquote

from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
from websockets.protocol import State

from shared.ssh_terminal_config import TerminalConfig
from shared.ssh_terminal_protocol import HEADER_SIZE


class _TerminalReceiveQueue(asyncio.Queue):
    """Count payload while queued; ASGI consumers release it through normal get().

    Uvicorn also enqueues connect/disconnect events. Those contain no message
    payload and must remain enqueueable when closing an overloaded connection.
    Admission happens synchronously before Uvicorn assembles a receive event.
    """

    def __init__(self):
        super().__init__()
        self.payload_bytes = 0

    @staticmethod
    def _payload_size(item):
        if item["type"] != "websocket.receive":
            return 0
        data = item.get("bytes")
        return len(data) if data is not None else len(item.get("text", "").encode("utf-8"))

    def _put(self, item):
        super()._put(item)
        self.payload_bytes += self._payload_size(item)

    def _get(self):
        item = super()._get()
        self.payload_bytes -= self._payload_size(item)
        return item


class TerminalAwareWebSocketProtocol(WebSocketsSansIOProtocol):
    terminal_connection = False
    terminal_queue_messages = 16
    agent_queue_bytes = 1024 * 1024

    def _select_route(self, target):
        path = unquote(target.partition("?")[0])
        browser = path == "/ws/vps-terminal"
        agent = path.startswith("/agent/vps-terminal/")
        self.terminal_connection = browser or agent
        if self.terminal_connection:
            self.terminal_queue_messages = 256 if agent else 16
            if agent and not isinstance(self.queue, _TerminalReceiveQueue):
                # Route selection precedes handle_connect and every ASGI task.
                # Repeated selection in handle_connect must keep the same queue.
                self.queue = _TerminalReceiveQueue()
            limits = TerminalConfig.from_env()
            maximum = (
                max(limits.control_bytes, limits.input_frame_bytes + HEADER_SIZE)
                if browser
                else 192 * 1024
            )
            # websockets 17 splits the constructor's max_size into two
            # parser limits. Setting an unused max_size attribute is unsafe.
            self.conn.max_message_size = self.conn.max_fragment_size = maximum
            self.conn.available_extensions = []

    def data_received(self, data):
        # Configure before feeding the HTTP upgrade to SansIO. Its parser can
        # start reading the first WS frame in the same receive_data() call,
        # before Uvicorn dispatches handle_connect. A pipelined first frame
        # must obey the same limit as every subsequent frame.
        if not getattr(self, "_route_selected", False):
            prefix = getattr(self, "_request_prefix", b"") + data
            line, separator, _ = prefix.partition(b"\r\n")
            if len(line) > 8192:
                self.transport.close()
                return
            if not separator:
                self._request_prefix = prefix
                return
            parts = line.split(b" ")
            if len(parts) == 3:
                try:
                    self._select_route(parts[1].decode("ascii"))
                except UnicodeDecodeError:
                    self.transport.close()
                    return
            self._request_prefix = b""
            self._route_selected = True
            data = prefix
        super().data_received(data)

    def handle_connect(self, event):
        # Also select here for integrations which call this hook directly;
        # the normal production path already configured the parser above.
        self._select_route(event.path)
        super().handle_connect(event)

    def handle_parser_exception(self):
        if self.conn.close_sent is None:
            # A peer can pipeline WS bytes before the HTTP upgrade is accepted.
            # SansIO cannot send a WebSocket close frame in CONNECTING state;
            # do not let Uvicorn's normal OPEN-state assertion keep it alive.
            self.frames = []
            self.close_sent = True
            self.transport.close()
            return
        super().handle_parser_exception()

    def send_receive_event_to_app(self):
        # TCP may deliver many small messages in one read before pause_reading
        # takes effect. Bound their ASGI queue too, not only each message size.
        over_count = self.queue.qsize() >= self.terminal_queue_messages
        over_bytes = (
            isinstance(self.queue, _TerminalReceiveQueue)
            and self.queue.payload_bytes + sum(map(len, self.frames)) > self.agent_queue_bytes
        )
        if self.terminal_connection and not self.close_sent and (over_count or over_bytes):
            self.frames = []
            if self.conn.state is not State.OPEN:
                # Frames may arrive while ASGI authentication still awaits the
                # upgrade. A WS close frame is illegal before OPEN; fence and
                # close TCP without growing the authentication receive queue.
                self.close_sent = True
                self.transport.close()
                return
            self.conn.send_close(1008, "TERMINAL_QUEUE_SATURATED")
            self.handle_parser_exception()
            return
        super().send_receive_event_to_app()
