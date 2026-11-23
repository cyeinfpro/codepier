"""A bounded, separately encrypted Agent connection for interactive terminals.

This channel never places terminal bytes in the operation journal or shares the
normal Agent command socket's bulk queue. Every handshake gets fresh keying
material and a terminal-specific associated-data purpose.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import uuid
from dataclasses import replace

from anyio import CancelScope

from shared.crypto import pack_channel, unpack_channel
from shared.ssh_terminal_channel import TerminalSendQueue
from shared.ssh_terminal_protocol import TerminalProtocolError, encode_frame


class TerminalAgentConnection:
    def __init__(self, socket, channel, generation, config, *, device_id, on_event):
        self.socket, self.channel, self.generation = socket, channel, generation
        self.config, self.device_id, self.on_event = config, device_id, on_event
        # Internal controls include an authorized project path and transient
        # credentials. Browser controls retain their smaller public limit.
        self.queue = TerminalSendQueue(
            replace(config, control_bytes=64 * 1024), max_bytes=config.hub_relay_bytes * 2
        )
        self.pending = {}
        self.closed = False
        self.sender = None
        self.direct_vps = False
        self.direct_vps_allowed = True
        self.probe_supported = False
        self.host_pin_supported = False
        self.saved_vps_password_only_supported = False

    async def request(self, action, payload, project, *, wait_seconds=None):
        if self.closed:
            raise TerminalProtocolError("AGENT_OFFLINE", "终端通道已断开")
        if len(self.pending) >= 256:
            raise TerminalProtocolError("AGENT_BUSY", "终端控制队列繁忙")
        rid = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        # Do not let a large project description, inventory or unrelated
        # settings consume the terminal channel's bounded control envelope.
        project = {
            key: project[key]
            for key in (
                "id",
                "alias",
                "root",
                "mode",
                "allow_tasks",
                "device_id",
                "space_id",
                "_terminal_owner",
                "_terminal_space",
                "_execution_policy",
                "_direct_vps",
            )
            if key in project
        }
        try:
            # Pending requests have a finite timeout. Never retry create or
            # input after an ambiguous response: their stable identity is the
            # only path to a later status/reconciliation request.
            async with asyncio.timeout(
                wait_seconds or self.config.connect_timeout + self.config.login_timeout + 5
            ):
                await self.queue.put_control(
                    {
                        "type": "terminal_control",
                        "request_id": rid,
                        "action": action,
                        "payload": payload,
                        "project": project,
                    }
                )
                return await asyncio.shield(future)
        except TimeoutError as exc:
            raise TerminalProtocolError(
                "AGENT_RESPONSE_UNKNOWN", "Agent 响应尚未确认，请查询原会话状态"
            ) from exc
        finally:
            self.pending.pop(rid, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # A concurrent close can settle the reply while the request
                # is still waiting to enqueue. Observe that unused exception
                # without replacing the queue's original failure.
                future.exception()

    async def input(self, frame):
        # A frame is not retried here. Input receipts are emitted by the Agent
        # only after its single-writer sequence has been claimed.
        packet = {
            "type": "terminal_input",
            "session_id": frame.session_id,
            "data": base64.b64encode(encode_frame(frame, self.config)).decode("ascii"),
        }
        await asyncio.wait_for(
            self.queue.put_output(packet, len(frame.payload)), self.config.write_timeout
        )

    async def acknowledge(self, payload):
        await asyncio.wait_for(
            self.queue.put_control({**payload, "type": "terminal_output_ack"}),
            self.config.write_timeout,
        )

    async def _send(self):
        while not self.closed:
            body = await self.queue.get()
            packet = await pack_channel(self.channel, body)
            await asyncio.wait_for(self.socket.send_text(packet), self.config.write_timeout)

    async def run(self):
        self.sender = asyncio.create_task(self._send(), name="terminal-agent-send")
        receiver = asyncio.create_task(self._receive(), name="terminal-agent-receive")
        try:
            done, _ = await asyncio.wait(
                (self.sender, receiver), return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                task.result()
        finally:
            with CancelScope(shield=True):
                receiver.cancel()
                try:
                    await self.close()
                finally:
                    await asyncio.gather(receiver, return_exceptions=True)

    async def _receive(self):
        while not self.closed:
            raw = await asyncio.wait_for(
                self.socket.receive_text(), self.config.heartbeat_seconds * 4
            )
            if len(raw) > 192 * 1024:
                raise TerminalProtocolError("FRAME_TOO_LARGE", "Agent 终端帧超出限制")
            body = await unpack_channel(self.channel, raw)
            if body.get("type") == "terminal_reply":
                future = self.pending.get(body.get("request_id"))
                if future is None or future.done():
                    continue
                error = body.get("error")
                if error:
                    future.set_exception(
                        TerminalProtocolError(
                            error.get("code", "AGENT_ERROR"), error.get("message", "终端操作失败")
                        )
                    )
                else:
                    future.set_result(body.get("result", {}))
            elif body.get("type") == "terminal_ping":
                await self.queue.put_control({"type": "terminal_pong"})
            elif body.get("type") != "terminal_pong":
                await self.on_event(self, body)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        self.queue.close()
        # Settle requests before the first checkpoint: a cancelled close
        # must not mark this connection closed and strand its pending RPCs.
        for future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(
                    TerminalProtocolError(
                        "AGENT_RESPONSE_UNKNOWN", "终端连接中断，操作结果尚未确认"
                    )
                )
        if self.sender and self.sender is not asyncio.current_task():
            self.sender.cancel()
            await asyncio.gather(self.sender, return_exceptions=True)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.socket.close(code=1012), self.config.shutdown_timeout)
