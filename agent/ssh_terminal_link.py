"""Dedicated Hub↔Agent terminal channel; no operation journal or plaintext logs."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import random
from dataclasses import replace

import websockets

from shared.crypto import pack_channel, unpack_channel
from shared.ssh_terminal_channel import (
    TerminalQueueClosed,
    TerminalSendQueue,
    terminal_secure_channel,
)
from shared.ssh_terminal_config import TerminalConfig
from shared.ssh_terminal_protocol import TerminalProtocolError, canonical_uuid, decode_frame
from .ssh_terminal_manager import TerminalSessionManager


class AgentTerminalLink:
    def __init__(self, agent):
        self.agent = agent
        self.config = TerminalConfig.from_env()
        self.manager = TerminalSessionManager(agent, emit=self.emit, config=self.config)
        self.socket = None
        self.queue = None
        self.tasks = set()
        self.closed = False

    def capabilities(self):
        return {
            **self.manager.capabilities(),
            "channel": "dedicated-v1",
            "limits": self.config.public_limits(),
        }

    def _direct_vps_allowed(self):
        config = getattr(self.agent, "config", None)
        if not isinstance(config, dict):
            return True
        from .shell import direct_vps_denial

        return direct_vps_denial(config) is None

    def _hello(self):
        return {
            "type": "terminal_hello",
            "protocol": 1,
            "runtime_generation": self.manager.generation,
            "limits": self.config.public_limits(),
            "direct_vps": True,
            "direct_vps_allowed": self._direct_vps_allowed(),
            "probe": True,
            "host_pin": True,
        }

    async def emit(self, event):
        queue = self.queue
        if queue is None or queue.closed:
            raise ConnectionError("Terminal channel is unavailable")
        packet = dict(event)
        data = packet.pop("payload", None)
        try:
            if data is not None:
                packet["data"] = base64.b64encode(data).decode("ascii")
                await queue.put_output(packet, len(data))
            elif packet.get("type") in {"terminal_exit", "terminal_replay_complete"}:
                await queue.put_barrier(packet)
            else:
                await queue.put_control(packet)
        except TerminalQueueClosed as exc:
            raise ConnectionError("Terminal channel disconnected") from exc

    async def run(self, config, main_socket):
        if not self.manager.capabilities().get("protocol"):
            return
        attempt = 0
        while (
            not self.closed
            and not self.agent.stop_event.is_set()
            and self.agent.socket is main_socket
        ):
            try:
                await self._connect(config, main_socket)
                attempt = 0
            except asyncio.CancelledError:
                raise
            except Exception:
                # SSH text, passwords, challenges and ticket values must never
                # be included in the normal Agent diagnostic log.
                attempt += 1
            if self.agent.socket is main_socket and not self.agent.stop_event.is_set():
                await asyncio.sleep(
                    min(8.0, 0.5 * 2 ** min(attempt, 4)) * random.uniform(0.75, 1.25)
                )

    async def _connect(self, config, main_socket):
        base = config["hub_url"].rstrip("/")
        url = ("wss://" if base.startswith("https://") else "ws://") + base.split("://", 1)[1]
        url += "/agent/vps-terminal/" + config["device_id"]
        async with websockets.connect(
            url,
            open_timeout=10,
            ping_interval=15,
            ping_timeout=15,
            max_size=192 * 1024,
            max_queue=16,
            write_limit=64 * 1024,
            compression=None,
            proxy=None,
            close_timeout=3,
        ) as socket:
            raw = await asyncio.wait_for(socket.recv(), self.config.auth_timeout_seconds)
            if not isinstance(raw, str) or len(raw) > self.config.control_bytes:
                raise TerminalProtocolError("INVALID_HANDSHAKE", "终端握手无效")
            challenge = json.loads(raw)
            if challenge.get("type") != "terminal_challenge" or challenge.get("protocol") != 1:
                raise TerminalProtocolError("PROTOCOL_MISMATCH", "终端协议不兼容")
            channel = terminal_secure_channel(
                config["secret"], challenge["challenge"], config["device_id"], "agent"
            )
            await socket.send(await pack_channel(channel, self._hello()))
            ready = await unpack_channel(
                channel, await asyncio.wait_for(socket.recv(), self.config.auth_timeout_seconds)
            )
            if (
                ready.get("type") != "terminal_ready"
                or ready.get("runtime_generation") != self.manager.generation
                or self.agent.socket is not main_socket
                or self.agent.config is not config
            ):
                raise TerminalProtocolError("STALE_GENERATION", "终端握手已过期")
            queue = TerminalSendQueue(
                replace(self.config, control_bytes=64 * 1024),
                max_bytes=self.config.hub_relay_bytes * 2,
            )
            self.socket, self.queue = socket, queue
            sender = asyncio.create_task(
                self._send(socket, channel, queue), name="terminal-channel-send"
            )
            receiver = asyncio.create_task(
                self._receive(socket, channel, queue), name="terminal-channel-receive"
            )
            heartbeat = asyncio.create_task(
                self._heartbeat(queue), name="terminal-channel-heartbeat"
            )
            try:
                done, _ = await asyncio.wait(
                    (sender, receiver, heartbeat), return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    task.result()
            finally:
                if self.queue is queue:
                    self.socket = self.queue = None
                queue.close()
                for task in (sender, receiver, heartbeat, *tuple(self.tasks)):
                    task.cancel()
                await asyncio.gather(
                    sender, receiver, heartbeat, *tuple(self.tasks), return_exceptions=True
                )

    async def _send(self, socket, channel, queue):
        while not self.closed:
            message = await queue.get()
            await asyncio.wait_for(
                socket.send(await pack_channel(channel, message)), self.config.write_timeout
            )

    async def _heartbeat(self, queue):
        while not self.closed:
            await asyncio.sleep(self.config.heartbeat_seconds)
            await queue.put_control({"type": "terminal_ping"})

    def _task(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)

        def finished(item):
            self.tasks.discard(item)
            if not item.cancelled():
                item.exception()

        task.add_done_callback(finished)

    async def _queue_control(self, body, queue):
        canonical_uuid(body.get("request_id"))
        if len(self.tasks) >= 256:
            await queue.put_control(
                {
                    "type": "terminal_reply",
                    "request_id": body["request_id"],
                    "error": {"code": "AGENT_BUSY", "message": "终端控制队列已满"},
                }
            )
        else:
            self._task(self._control(body, queue))

    async def _queue_input(self, body, queue):
        encoded = body.get("data")
        if not isinstance(encoded, str) or len(encoded) > (self.config.input_frame_bytes + 62) * 2:
            raise TerminalProtocolError("FRAME_TOO_LARGE", "终端输入帧过大")
        frame = decode_frame(base64.b64decode(encoded, validate=True), self.config)
        if len(self.tasks) >= 256:
            await queue.put_control(
                {
                    "type": "terminal_error",
                    "session_id": frame.session_id,
                    "runtime_generation": self.manager.generation,
                    "stream_epoch": str(frame.stream_epoch),
                    "writer_epoch": str(frame.writer_epoch),
                    "reason_code": "INPUT_BACKPRESSURE",
                }
            )
        else:
            self._task(self._input(frame, queue))

    async def _receive(self, socket, channel, queue):
        while not self.closed:
            raw = await asyncio.wait_for(socket.recv(), self.config.heartbeat_seconds * 4)
            if not isinstance(raw, str) or len(raw) > 192 * 1024:
                raise TerminalProtocolError("FRAME_TOO_LARGE", "终端通道帧过大")
            body = await unpack_channel(channel, raw)
            kind = body.get("type")
            if kind == "terminal_pong":
                continue
            if kind == "terminal_ping":
                await queue.put_control({"type": "terminal_pong"})
            elif kind == "terminal_output_ack":
                await self.manager.acknowledge(body)
            elif kind == "terminal_control":
                await self._queue_control(body, queue)
            elif kind == "terminal_input":
                await self._queue_input(body, queue)
            else:
                raise TerminalProtocolError("INVALID_CONTROL", "未知终端通道帧")

    async def _control(self, body, queue):
        try:
            if getattr(
                getattr(self.agent, "lifecycle", None), "handoff_pending", False
            ) and body.get("action") not in {
                "close",
                "revoke",
                "detach",
                "status",
                "list",
                "auth_answer",
            }:
                raise TerminalProtocolError("AGENT_MAINTENANCE", "节点正在交接更新")
            # The manager permits auth_answer during handoff only for an
            # in-flight explicit close, never a new SSH login/recovery.
            result = await self.manager.control(
                body.get("action"), body.get("payload"), body.get("project")
            )
            reply = {"type": "terminal_reply", "request_id": body["request_id"], "result": result}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reply = {
                "type": "terminal_reply",
                "request_id": body["request_id"],
                "error": {
                    "code": getattr(exc, "code", "TERMINAL_INTERNAL_ERROR"),
                    "message": getattr(exc, "message", "终端操作失败"),
                },
            }
        await queue.put_control(reply)

    async def _input(self, frame, queue):
        try:
            if getattr(getattr(self.agent, "lifecycle", None), "handoff_pending", False):
                raise TerminalProtocolError(
                    "AGENT_MAINTENANCE", "节点正在交接维护，不能继续终端输入"
                )
            result = await self.manager.input(frame)
            await queue.put_control(
                {
                    **result,
                    "type": "terminal_input_ack",
                    "session_id": frame.session_id,
                    "runtime_generation": frame.runtime_generation,
                    "stream_epoch": str(frame.stream_epoch),
                    "writer_epoch": str(frame.writer_epoch),
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await queue.put_control(
                {
                    "type": "terminal_error",
                    "session_id": frame.session_id,
                    "runtime_generation": frame.runtime_generation,
                    "stream_epoch": str(frame.stream_epoch),
                    "writer_epoch": str(frame.writer_epoch),
                    "reason_code": getattr(exc, "code", "INPUT_UNCERTAIN"),
                }
            )

    async def close(self):
        self.closed = True
        if self.queue:
            self.queue.close()
        if self.socket:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.socket.close(), self.config.shutdown_timeout)
        await self.manager.close_all()
