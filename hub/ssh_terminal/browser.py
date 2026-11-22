"""One authenticated browser attachment, with bounded output and writer fencing."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import time
import uuid
from urllib.parse import urlsplit
from starlette.websockets import WebSocketDisconnect

from shared.ssh_terminal_channel import TerminalQueueClosed, TerminalSendQueue
from shared.ssh_terminal_protocol import (
    FrameKind,
    TerminalProtocolError,
    decode_frame,
    encode_frame,
    parse_control,
    validate_binding,
    validate_control,
)
from shared.util import DevError

from .models import SessionAction


def check_websocket_origin(socket):
    """Browser WS has HTTP(S) Origin, while socket.url uses WS(S)."""
    origin = socket.headers.get("origin", "")
    try:
        parsed = urlsplit(origin)
        scheme = {"ws": "http", "wss": "https"}.get(socket.url.scheme, socket.url.scheme)
        expected = urlsplit(f"{scheme}://{socket.url.netloc}")
        normalized = lambda value: (
            value.scheme.lower(),
            (value.hostname or "").lower(),
            value.port or (443 if value.scheme == "https" else 80),
        )
        valid = (
            origin != "null"
            and parsed.scheme in {"http", "https"}
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and parsed.path in {"", "/"}
            and normalized(parsed) == normalized(expected)
        )
        secure = scheme == "https" or expected.hostname in {"localhost", "127.0.0.1", "::1"}
    except ValueError:
        valid = secure = False
    if not valid or not secure or socket.headers.get("sec-fetch-site") == "cross-site":
        raise DevError("ORIGIN_REJECTED", "终端仅接受同源 HTTPS 连接", 403)


class BrowserAttachment:
    def __init__(self, service, socket, session, renderer_id, *, principal=None):
        self.service, self.socket, self.session = service, socket, session
        self.config = service.config
        self.id, self.renderer_id = uuid.uuid4().hex, renderer_id
        self.principal = principal or session.principal
        self.queue = TerminalSendQueue(self.config, max_bytes=self.config.hub_relay_bytes)
        self.send_lock = asyncio.Lock()
        self.attached = asyncio.Event()
        self.closed = False
        self.close_task = None
        self.agent = None
        self.generation = None
        self.stream_epoch = self.writer_epoch = self.render_epoch = 0
        self.replay_boundary = 0
        self.resume_strategy = "live"
        self.replay_completed = False
        self.input_allowed = False
        self.input_blocked = False
        self.sent_offset = 0
        self.acked_offset = 0
        self.last_received = time.monotonic()
        self.tasks = []
        self.message_budget = 1000
        self.budget_at = time.monotonic()

    def bind(self, agent, generation, stream_epoch, writer_epoch, render_epoch, boundary, strategy):
        self.agent, self.generation = agent, generation
        self.stream_epoch, self.writer_epoch, self.render_epoch = (
            stream_epoch,
            writer_epoch,
            render_epoch,
        )
        self.replay_boundary, self.resume_strategy = boundary, strategy
        self.input_allowed = strategy != "history_replay"

    async def send_attached(self, message):
        validate_control(message, direction="server", config=self.config)
        if message["state"] in {"closing", "exited", "failed", "close_unknown", "frozen"}:
            self.input_blocked, self.input_allowed = True, False
        self.sent_offset = self.acked_offset = int(message["start_offset"])
        async with self.send_lock:
            await asyncio.wait_for(self.socket.send_json(message), self.config.write_timeout)
        self.attached.set()
        if self.resume_strategy == "live":
            self.replay_completed = True

    async def control(self, message, *, barrier=False):
        if self.closed:
            return
        validate_control(message, direction="server", config=self.config)
        packet = {"kind": "control", "session_id": self.session.row["id"], "message": message}
        enqueue = self.queue.try_put_barrier if barrier else self.queue.try_put_control
        try:
            if not enqueue(packet):
                self.begin_shutdown("OUTPUT_BACKPRESSURE", 1013)
        except TerminalQueueClosed:
            self.begin_shutdown("OUTPUT_BACKPRESSURE", 1013)

    async def output(self, frame):
        if self.closed:
            return
        # Attach RPC can emit output before its receipt; the sender waits for
        # send_attached, then validates against the authoritative result.
        packet = {
            "kind": "output",
            "session_id": frame.session_id,
            "data": base64.b64encode(encode_frame(frame, self.config)).decode("ascii"),
        }
        try:
            if not self.queue.try_put_output(packet, len(frame.payload)):
                self.begin_shutdown("OUTPUT_BACKPRESSURE", 1013)
        except TerminalQueueClosed:
            self.begin_shutdown("OUTPUT_BACKPRESSURE", 1013)

    async def complete_replay(self):
        if self.closed or self.replay_completed or self.input_blocked:
            return
        self.replay_completed = True
        self.input_allowed = True
        await self.control(
            {
                "type": "replay_complete",
                "attachment_id": self.id,
                "render_epoch": str(self.render_epoch),
                "stream_epoch": str(self.stream_epoch),
                "final_offset": str(self.replay_boundary),
            }
        )

    async def state(self, state):
        value = state["state"]
        if value in {"closing", "exited", "failed", "close_unknown", "frozen"}:
            self.input_blocked, self.input_allowed = True, False
        # Ready is not a renderer barrier. In a replay the browser becomes
        # writable only once its bound rendered ACK reaches the snapshot end.
        if value == "ready":
            return
        if value == "exited" and state.get("output_complete") is True:
            # Metadata can report EOF while the Agent still owns unread tail
            # bytes. Only terminal_exit carries the ordered final-offset
            # barrier which permits the renderer to finish.
            return
        message = {"type": "state", "session_id": self.session.row["id"], "state": value}
        for key in ("reason_code", "recoverability", "output_complete", "remote_session_state"):
            if state.get(key) is not None:
                message[key] = state[key]
        await self.control(message, barrier=value in {"exited", "failed", "close_unknown"})

    async def error(self, reason, *, fatal=False):
        # A later replay barrier cannot unfreeze this attachment. Recover by
        # obtaining a fresh, currently authorized attachment instead.
        self.input_blocked, self.input_allowed = True, False
        await self.control(
            {
                "type": "error",
                "reason_code": reason,
                "recoverability": "none" if fatal else "resume",
            }
        )

    async def exit(self, event):
        self.input_blocked, self.input_allowed = True, False
        message = {
            "type": "exit",
            "session_id": self.session.row["id"],
            "final_offset": str(event.get("final_offset", self.sent_offset)),
            "output_complete": bool(event.get("output_complete", False)),
        }
        for key in (
            "reason_code",
            "attachment_exit_status",
            "remote_session_state",
            "recoverability",
        ):
            if event.get(key) is not None:
                message[key] = event[key]
        await self.control(message, barrier=True)

    async def _sender(self):
        while not self.closed:
            packet = await self.queue.get()
            if packet["kind"] == "output":
                await self.attached.wait()
                raw = base64.b64decode(packet["data"], validate=True)
                frame = decode_frame(raw, self.config)
                validate_binding(
                    frame,
                    session_id=self.session.row["id"],
                    runtime_generation=self.generation,
                    stream_epoch=self.stream_epoch,
                    writer_epoch=self.writer_epoch,
                )
                if frame.sequence != self.sent_offset:
                    raise TerminalProtocolError("HISTORY_GAP", "终端输出偏移不连续")
                async with self.send_lock:
                    # Record before send so an immediately delivered client ACK
                    # never exceeds the locally accounted write boundary.
                    self.sent_offset = frame.end_offset
                    await asyncio.wait_for(self.socket.send_bytes(raw), self.config.write_timeout)
            else:
                async with self.send_lock:
                    await asyncio.wait_for(
                        self.socket.send_json(packet["message"]), self.config.write_timeout
                    )

    def _rate_limit(self):
        now = time.monotonic()
        self.message_budget = min(1000, self.message_budget + (now - self.budget_at) * 500)
        self.budget_at = now
        self.message_budget -= 1
        if self.message_budget < 0:
            raise TerminalProtocolError("RATE_LIMITED", "终端消息发送过快")

    async def _receive(self):
        while not self.closed:
            packet = await asyncio.wait_for(
                self.socket.receive(), self.config.heartbeat_seconds * 4
            )
            self.last_received = time.monotonic()
            if packet.get("type") == "websocket.disconnect":
                return
            await self.service.authorize(self.session, principal=self.principal)
            if self.closed or self.session.attachment is not self:
                raise TerminalProtocolError("STALE_WRITER", "终端已经由另一个窗口接管")
            if packet.get("bytes") is not None:
                await self._receive_input(packet["bytes"])
            else:
                await self._receive_control(packet.get("text", ""))

    async def _receive_input(self, raw):
        self._rate_limit()
        if not self.attached.is_set() or not self.input_allowed or self.agent is None:
            raise TerminalProtocolError("INPUT_NOT_READY", "终端尚不可输入")
        frame = decode_frame(raw, self.config)
        if frame.kind != FrameKind.INPUT:
            raise TerminalProtocolError("INVALID_FRAME", "浏览器不能提交输出帧")
        validate_binding(
            frame,
            session_id=self.session.row["id"],
            runtime_generation=self.generation,
            stream_epoch=self.stream_epoch,
            writer_epoch=self.writer_epoch,
        )
        if self.closed or not self.input_allowed or self.session.attachment is not self:
            raise TerminalProtocolError("STALE_WRITER", "终端已经由另一个窗口接管")
        await self.agent.input(frame)

    async def _receive_control(self, text):
        message = parse_control(text, config=self.config)
        kind = message["type"]
        if kind != "output_ack":
            self._rate_limit()
        if kind == "ping":
            await self.control({**message, "type": "pong"})
        elif kind == "pong":
            pass
        elif kind == "auth_answer":
            await self._receive_auth_answer(message)
        elif kind == "output_ack":
            await self._receive_output_ack(message)
        elif kind == "resize":
            await self._receive_resize(message)
        elif kind == "detach":
            return
        elif kind in {"close", "takeover"}:
            body = SessionAction.model_validate({k: v for k, v in message.items() if k != "type"})
            await self.service.action(self.session.row["id"], self.principal, kind, body)
        else:
            raise TerminalProtocolError("UNEXPECTED_CONTROL", "连接后不能再次认证")

    async def _receive_resize(self, message):
        if (
            not self.attached.is_set()
            or message["attachment_id"] != self.id
            or message["runtime_generation"] != self.generation
            or int(message["stream_epoch"]) != self.stream_epoch
            or int(message["writer_epoch"]) != self.writer_epoch
        ):
            raise TerminalProtocolError("STALE_WRITER", "过期窗口不能改变终端尺寸")
        await self.agent.request(
            "resize",
            {**self.service.target_ref(self.session), **message},
            self.session.project,
            wait_seconds=self.config.write_timeout,
        )

    async def _receive_output_ack(self, message):
        if (
            not self.attached.is_set()
            or message["attachment_id"] != self.id
            or int(message["render_epoch"]) != self.render_epoch
            or int(message["stream_epoch"]) != self.stream_epoch
            or message.get("runtime_generation", self.generation) != self.generation
        ):
            self._rate_limit()
            return
        offset = int(message["rendered_offset"])
        if not self.acked_offset <= offset <= self.sent_offset:
            raise TerminalProtocolError("INVALID_ACK", "终端确认越过已发送输出")
        # Output throughput must not consume the input/spam budget.
        # Only bound, actually advancing bulk ACKs qualify; tiny or
        # repeated ACKs retain the ordinary control-frame rate limit.
        if offset - self.acked_offset < min(4096, self.config.frame_bytes):
            self._rate_limit()
        self.acked_offset = offset
        await self.agent.acknowledge(
            {
                **message,
                "session_id": self.session.row["id"],
                "runtime_generation": self.generation,
            }
        )
        if offset >= self.replay_boundary and self.resume_strategy != "history_replay":
            await self.complete_replay()

    async def _receive_auth_answer(self, message):
        if (
            not self.session.challenge
            or message["challenge_id"] != self.session.challenge["challenge_id"]
        ):
            raise TerminalProtocolError("AUTH_CHALLENGE_EXPIRED", "认证请求已失效")
        agent = self.service.agent(self.session.row["device_id"])
        await agent.request(
            "auth_answer",
            {**self.service.target_ref(self.session), **message},
            self.session.project,
            wait_seconds=self.config.write_timeout,
        )
        self.session.challenge = None

    async def run(self, resume):
        sender = asyncio.create_task(self._sender(), name="terminal-browser-send")
        receiver = asyncio.create_task(self._receive(), name="terminal-browser-receive")
        attach = asyncio.create_task(
            self.service.attach(self.session, self, resume), name="terminal-browser-attach"
        )
        self.tasks = [sender, receiver, attach]
        try:
            done, _ = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
            if attach in done:
                attach.result()
                done, _ = await asyncio.wait(
                    (sender, receiver), return_when=asyncio.FIRST_COMPLETED
                )
            for task in done:
                task.result()
        except (DevError, TerminalProtocolError) as exc:
            observer = getattr(self.service, "observe_rejection", None)
            if observer:
                observer(self.session, exc.code)
            # Best-effort safe control, serialized with the sender; never include
            # decrypted SSH data or arbitrary exception repr in diagnostics.
            with contextlib.suppress(Exception):
                async with self.send_lock:
                    await asyncio.wait_for(
                        self.socket.send_json(
                            {"type": "error", "reason_code": exc.code, "recoverability": "none"}
                        ),
                        self.config.write_timeout,
                    )
        except (TimeoutError, ConnectionError, TerminalQueueClosed, WebSocketDisconnect):
            pass
        finally:
            await self.shutdown("CONNECTION_CLOSED", 1000)
            for task in self.tasks:
                task.cancel()
            await asyncio.gather(*self.tasks, return_exceptions=True)
            await self.service.detach(self.session, self)

    def begin_shutdown(self, reason, code):
        # The shared Agent receiver must never await a slow browser's socket
        # or queue. Fence it immediately and own at most one bounded cleanup
        # task for this attachment; the normal lifetime joins that task.
        if self.closed:
            return self.close_task
        observer = getattr(self.service, "observe_backpressure", None)
        if reason == "OUTPUT_BACKPRESSURE" and observer:
            observer(self.session)
        self.closed, self.input_blocked, self.input_allowed = True, True, False
        self.attached.set()  # Release a sender waiting on an abandoned attach.
        self.queue.close()
        self.close_task = asyncio.create_task(
            self._close_socket(reason, code), name="terminal-browser-close"
        )
        return self.close_task

    async def _close_socket(self, reason, code):
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self.socket.close(code=code, reason=reason[:100]), self.config.shutdown_timeout
            )

    async def shutdown(self, reason, code):
        task = self.begin_shutdown(reason, code)
        if task is not None and task is not asyncio.current_task():
            await asyncio.shield(task)
