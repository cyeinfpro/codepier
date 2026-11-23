"""Hub terminal boundary and race tests with real SQLite and in-memory peers.

The Store, IAM decisions and network peers are controlled test doubles.  The
repository, browser framing, service state machine and queue are production
implementations.  No SSH server or production credential is contacted.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import sqlite3
import threading
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from hub.principal import Principal
from hub.ssh_terminal.repository import TerminalRepository
from shared.ssh_terminal_config import TerminalConfig
from shared.ssh_terminal_protocol import (
    FrameKind,
    TerminalFrame,
    TerminalProtocolError,
    encode_frame,
    validate_control,
)

SID = "11" * 16
GEN = "22" * 16
OTHER_GEN = "33" * 16
RENDERER = "44" * 16
REQUEST = "55" * 16


class MemoryStore:
    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.audits = []

    def one(self, sql, params=()):
        row = self.db.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql, params=()):
        return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    @contextlib.contextmanager
    def transaction(self):
        with self.lock, self.db:
            yield self.db

    async def run(self, function, *args, **kwargs):
        # A real Store call yields to its database worker.  Preserve that race
        # boundary even though this fixture owns an in-memory database.
        await asyncio.sleep(0)
        return function(*args, **kwargs)

    def audit(self, *args):
        self.audits.append(args)

    def decrypt(self, value):
        assert value == "encrypted-fixture"
        return "fixture-password-never-persisted"


def principal(login="login-a", *, space="space-a", user="user-a"):
    return Principal(
        actor="panel:alice",
        user_id=user,
        scopes={"write", "execute"},
        projects=["project-a"],
        space_id=space,
        session_hash=login,
    )


def row(**changes):
    value = {
        "id": SID,
        "space_id": "space-a",
        "owner_user_id": "user-a",
        "project_id": "project-a",
        "device_id": "device-a",
        "vps_id": "vps-a",
        "connection_revision": 1,
        "binding_id": "binding-a",
        "request_id": REQUEST,
        "request_fingerprint": "fixture-fingerprint",
        "project_root": "/fixture/project",
        "runtime_generation": GEN,
        "requested_mode": "direct",
        "mode": "direct",
        "state": "ready",
        "reason_code": None,
        "writer_epoch": "1",
        "stream_epoch": "1",
        "cols": 100,
        "rows": 30,
        "created": 1000.0,
        "updated": 1000.0,
        "metadata": "{}",
    }
    value.update(changes)
    return value


class MemorySocket:
    def __init__(self, url="wss://panel.example/ws/terminal", *, origin="https://panel.example"):
        self.url = urlsplit(url)
        self.headers = {"origin": origin, "sec-fetch-site": "same-origin"}
        self.incoming = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.auth_session = {"id_hash": "login-a", "user_id": "user-a"}

    async def receive(self):
        return await self.incoming.get()

    async def send_json(self, message):
        self.sent.append(("json", json.loads(json.dumps(message))))

    async def send_bytes(self, data):
        self.sent.append(("bytes", bytes(data)))

    async def close(self, **kwargs):
        self.closed = True

    async def receive_controls(self, *messages):
        for message in messages:
            await self.incoming.put({"type": "websocket.receive", "text": json.dumps(message)})
        await self.incoming.put({"type": "websocket.disconnect"})


class MemoryAgent:
    def __init__(self, generation=GEN):
        self.device_id, self.generation = "device-a", generation
        self.closed = False
        self.requests, self.inputs, self.acks = [], [], []
        self.handler = None

    async def request(self, action, payload, project, **kwargs):
        self.requests.append((action, dict(payload), dict(project)))
        if self.handler is not None:
            return await self.handler(action, payload, project)
        return {
            "state": "ready",
            "mode": "direct",
            "runtime_generation": self.generation,
            "stream_epoch": "1",
            "writer_epoch": "1",
            "cols": 100,
            "rows": 30,
        }

    async def input(self, frame):
        self.inputs.append(frame)

    async def acknowledge(self, payload):
        self.acks.append(dict(payload))

    async def close(self):
        self.closed = True


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.addCleanup(self.store.db.close)
        self.repository = TerminalRepository(self.store)

    def test_request_identity_is_unique_per_owner_and_space(self):
        self.repository.insert(row())
        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.insert(row(id="66" * 16))
        self.repository.insert(row(id="77" * 16, owner_user_id="user-b"))
        self.repository.insert(row(id="88" * 16, space_id="space-b"))
        self.assertEqual(SID, self.repository.by_request("space-a", "user-a", REQUEST)["id"])
        self.assertIsNone(self.repository.by_request("space-c", "user-a", REQUEST))

    def test_lifecycle_updates_cannot_persist_terminal_content_or_secrets(self):
        self.repository.insert(row())
        self.repository.update(
            SID,
            {
                "state": "detached",
                "bytes_out": 19,
                "password": "do-not-store",
                "input": "private-input",
                "output": "private-output",
                "responses": ["otp"],
            },
        )
        stored = self.repository.get(SID)
        serialized = json.dumps(stored)
        self.assertEqual("detached", stored["state"])
        self.assertEqual({"bytes_out": 19}, json.loads(stored["metadata"]))
        for secret in ("do-not-store", "private-input", "private-output", "otp"):
            self.assertNotIn(secret, serialized)

    def test_listing_does_not_cross_owner_or_space(self):
        self.repository.insert(row())
        self.repository.insert(row(id="66" * 16, owner_user_id="user-b"))
        self.repository.insert(row(id="77" * 16, space_id="space-b"))
        self.assertEqual([SID], [r["id"] for r in self.repository.list("space-a", "user-a")])

    def test_direct_vps_metadata_persists_without_project_alias(self):
        self.repository.insert(
            row(
                project_id="",
                project_root="",
                binding_id="direct:vps-a",
                metadata=json.dumps({"project_alias": ""}),
            )
        )
        updated = self.repository.update(SID, {"direct_vps": True})
        projected = self.repository.public(updated)
        self.assertTrue(projected["direct_vps"])
        self.assertEqual("", projected["project_alias"])


class BrowserBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from hub.ssh_terminal.browser import BrowserAttachment

        self.session = SimpleNamespace(
            row=row(), principal=principal(), attachment=None, challenge=None, project={}
        )
        self.service = SimpleNamespace(config=TerminalConfig(), authorize=AsyncMock())
        self.socket, self.agent = MemorySocket(), MemoryAgent()
        self.attachment = BrowserAttachment(self.service, self.socket, self.session, RENDERER)
        self.session.attachment = self.attachment
        self.attachment.bind(self.agent, GEN, 1, 1, 1, 8, "live")
        await self.attachment.send_attached(
            {
                "type": "attached",
                "session_id": SID,
                "runtime_generation": GEN,
                "attachment_id": self.attachment.id,
                "render_epoch": "1",
                "stream_epoch": "1",
                "writer_epoch": "1",
                "mode": "direct",
                "resume_strategy": "live",
                "state": "ready",
                "start_offset": "0",
                "output_end_offset": "8",
                "next_input_seq": "1",
            }
        )

    async def asyncTearDown(self):
        await self.attachment.shutdown("TEST_FINISHED", 1000)

    def ack(self, offset, **changes):
        message = {
            "type": "output_ack",
            "attachment_id": self.attachment.id,
            "runtime_generation": GEN,
            "render_epoch": "1",
            "stream_epoch": "1",
            "rendered_offset": str(offset),
        }
        message.update(changes)
        return message

    async def test_exit_barrier_follows_all_previously_queued_bytes(self):
        frame = TerminalFrame(FrameKind.OUTPUT, SID, GEN, 1, 1, 0, b"tail-123")
        await self.attachment.output(frame)
        await self.attachment.exit({"final_offset": "8", "output_complete": True})
        sender = asyncio.create_task(self.attachment._sender())
        try:
            for _ in range(20):
                if len(self.socket.sent) >= 3:
                    break
                await asyncio.sleep(0)
            self.assertEqual(["json", "bytes", "json"], [kind for kind, _ in self.socket.sent])
            self.assertEqual(encode_frame(frame), self.socket.sent[1][1])
            self.assertEqual("exit", self.socket.sent[2][1]["type"])
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)

    async def test_ack_cannot_credit_output_the_hub_has_not_sent(self):
        await self.socket.receive_controls(self.ack(1))
        with self.assertRaises(TerminalProtocolError) as raised:
            await self.attachment._receive()
        self.assertEqual("INVALID_ACK", raised.exception.code)
        self.assertEqual(0, self.attachment.acked_offset)
        self.assertEqual([], self.agent.acks)

    async def test_old_attachment_ack_is_ignored_without_mutating_credit(self):
        self.attachment.sent_offset = 8
        await self.socket.receive_controls(self.ack(8, attachment_id="99" * 16))
        await self.attachment._receive()
        self.assertEqual(0, self.attachment.acked_offset)
        self.assertEqual([], self.agent.acks)

    async def test_old_runtime_ack_cannot_be_relabelled_as_current(self):
        self.attachment.sent_offset = 8
        await self.socket.receive_controls(self.ack(8, runtime_generation=OTHER_GEN))
        await self.attachment._receive()
        self.assertEqual(0, self.attachment.acked_offset)
        self.assertEqual([], self.agent.acks)

    async def test_history_replay_ack_and_ready_state_cannot_unlock_keyboard(self):
        self.attachment.resume_strategy = "history_replay"
        self.attachment.replay_completed = self.attachment.input_allowed = False
        self.attachment.sent_offset = 8
        await self.socket.receive_controls(self.ack(7))
        await self.attachment._receive()
        self.assertFalse(self.attachment.input_allowed)
        await self.socket.receive_controls(self.ack(8))
        await self.attachment._receive()
        await self.attachment.state({"state": "ready"})
        self.assertFalse(self.attachment.input_allowed)
        self.assertFalse(self.attachment.replay_completed)
        self.assertEqual(0, self.attachment.queue.queued_items)
        self.assertEqual(["7", "8"], [message["rendered_offset"] for message in self.agent.acks])

    async def test_only_advancing_bound_bulk_ack_bypasses_empty_spam_budget(self):
        step = min(4096, self.service.config.frame_bytes)
        self.attachment.sent_offset = step * 2
        self.attachment.message_budget = 0
        self.attachment.budget_at = 100
        await self.socket.receive_controls(self.ack(step), self.ack(step * 2))
        with patch("hub.ssh_terminal.browser.time.monotonic", return_value=100):
            await self.attachment._receive()
        self.assertEqual(step * 2, self.attachment.acked_offset)
        self.assertEqual(0, self.attachment.message_budget)
        self.assertEqual(2, len(self.agent.acks))

    async def test_duplicate_tiny_and_unbound_ack_cannot_bypass_rate_limit(self):
        step = min(4096, self.service.config.frame_bytes)
        cases = [
            ("duplicate", self.ack(0), True),
            ("tiny", self.ack(step - 1), True),
            ("attachment", self.ack(step, attachment_id="99" * 16), True),
            ("render", self.ack(step, render_epoch="2"), True),
            ("stream", self.ack(step, stream_epoch="2"), True),
            ("runtime", self.ack(step, runtime_generation=OTHER_GEN), True),
            ("not attached", self.ack(step), False),
            ("ordinary control", {"type": "ping", "nonce": "fixture-nonce"}, True),
        ]
        for name, message, attached in cases:
            with self.subTest(name=name):
                self.socket.incoming = asyncio.Queue()
                self.attachment.sent_offset = step
                self.attachment.acked_offset = 0
                self.attachment.message_budget = 0
                self.attachment.budget_at = 100
                self.attachment.attached.set() if attached else self.attachment.attached.clear()
                await self.socket.receive_controls(message)
                with patch("hub.ssh_terminal.browser.time.monotonic", return_value=100):
                    with self.assertRaises(TerminalProtocolError) as raised:
                        await self.attachment._receive()
                self.assertEqual("RATE_LIMITED", raised.exception.code)
                self.assertEqual(0, self.attachment.acked_offset)
                self.assertEqual([], self.agent.acks)

    async def test_closed_attachment_cannot_send_input_after_authorization_await(self):
        async def revoke_during_authorize(*args, **kwargs):
            await self.attachment.shutdown("AUTH_REVOKED", 4403)

        self.service.authorize.side_effect = revoke_during_authorize
        frame = TerminalFrame(FrameKind.INPUT, SID, GEN, 1, 1, 1, b"dangerous-command\r")
        await self.socket.incoming.put({"type": "websocket.receive", "bytes": encode_frame(frame)})
        with contextlib.suppress(TerminalProtocolError):
            await self.attachment._receive()
        self.assertEqual([], self.agent.inputs)


class OriginTests(unittest.TestCase):
    def test_origin_must_match_https_origin_including_effective_port(self):
        from hub.ssh_terminal.browser import check_websocket_origin
        from shared.util import DevError

        for origin in ("https://panel.example", "https://PANEL.example:443"):
            check_websocket_origin(MemorySocket(origin=origin))
        for origin in (
            "null",
            "https://evil.example",
            "https://panel.example:444",
            "https://panel.example/path",
            "http://panel.example",
            "https://x@panel.example",
        ):
            with self.subTest(origin=origin), self.assertRaises(DevError):
                check_websocket_origin(MemorySocket(origin=origin))

    def test_only_loopback_allows_plain_http_websocket(self):
        from hub.ssh_terminal.browser import check_websocket_origin
        from shared.util import DevError

        check_websocket_origin(
            MemorySocket("ws://localhost:8080/ws", origin="http://localhost:8080")
        )
        with self.assertRaises(DevError):
            check_websocket_origin(
                MemorySocket("ws://panel.example/ws", origin="http://panel.example")
            )


class AgentConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from hub.ssh_terminal.agent_transport import TerminalAgentConnection

        self.connection = TerminalAgentConnection(
            MemorySocket(),
            object(),
            GEN,
            TerminalConfig(),
            device_id="device-a",
            on_event=AsyncMock(),
        )

    async def asyncTearDown(self):
        await self.connection.close()

    async def test_browser_ack_is_translated_to_agent_control_type(self):
        await self.connection.acknowledge(
            {
                "type": "output_ack",
                "session_id": SID,
                "attachment_id": "66" * 16,
                "runtime_generation": GEN,
                "render_epoch": "1",
                "stream_epoch": "1",
                "rendered_offset": "1024",
            }
        )
        packet = await self.connection.queue.get()
        self.assertEqual("terminal_output_ack", packet["type"])
        self.assertEqual("1024", packet["rendered_offset"])

    async def test_disconnect_unblocks_pending_request_without_retry(self):
        request = asyncio.create_task(self.connection.request("close", {"session_id": SID}, {}))
        for _ in range(20):
            if self.connection.pending:
                break
            await asyncio.sleep(0)
        await self.connection.close()
        with self.assertRaises(TerminalProtocolError) as raised:
            await request
        self.assertEqual("AGENT_RESPONSE_UNKNOWN", raised.exception.code)
        self.assertEqual({}, self.connection.pending)

    async def test_response_timeout_clears_pending_future(self):
        with self.assertRaises(TerminalProtocolError) as raised:
            await self.connection.request("create", {"session_id": SID}, {}, wait_seconds=0.01)
        self.assertEqual("AGENT_RESPONSE_UNKNOWN", raised.exception.code)
        self.assertEqual({}, self.connection.pending)
        # The submitted command stays ambiguous; no retry command is added.
        self.assertEqual(1, self.connection.queue.queued_items)

    async def test_direct_vps_flag_projected_only_when_present(self):
        direct_project = {
            "id": "",
            "root": "",
            "_direct_vps": True,
            "_terminal_owner": "user:owner-a",
            "_terminal_space": "space-a",
        }
        pending = asyncio.create_task(
            self.connection.request("probe", {"host": "h"}, direct_project, wait_seconds=1)
        )
        try:
            packet = await asyncio.wait_for(self.connection.queue.get(), 1)
            self.assertEqual(direct_project, packet["project"])
            self.connection.pending[packet["request_id"]].set_result({"ok": True})
            self.assertEqual({"ok": True}, await pending)
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_internal_create_projects_only_execution_fields_and_fits_credentials(self):
        project = {
            "id": "project-a",
            "root": "/" + "r" * 4080,
            "mode": "write",
            "allow_tasks": True,
            "device_id": "device-a",
            "space_id": "space-a",
            "_terminal_owner": "user:user-a",
            "_terminal_space": "space-a",
            "description": "large project description" * 10000,
            "inventory": ["unrelated"] * 1000,
            "password": "unrelated-project-secret",
        }
        payload = {"session_id": SID, "password": "p" * 4096}
        pending = asyncio.create_task(
            self.connection.request("create", payload, project, wait_seconds=1)
        )
        try:
            packet = await asyncio.wait_for(self.connection.queue.get(), 1)
            self.assertEqual("terminal_control", packet["type"])
            self.assertEqual(
                {
                    key: project[key]
                    for key in (
                        "id",
                        "root",
                        "mode",
                        "allow_tasks",
                        "device_id",
                        "space_id",
                        "_terminal_owner",
                        "_terminal_space",
                    )
                },
                packet["project"],
            )
            self.assertEqual(payload, packet["payload"])
            encoded = json.dumps(packet).encode()
            self.assertGreater(len(encoded), self.connection.config.control_bytes)
            self.assertLess(len(encoded), 64 * 1024)
            self.assertNotIn(b"unrelated-project-secret", encoded)
            self.connection.pending[packet["request_id"]].set_result({"state": "creating"})
            self.assertEqual({"state": "creating"}, await pending)
            self.assertEqual({}, self.connection.pending)
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from hub.ssh_terminal.service import HubSession, TerminalService
        from shared.util import DevError

        self.DevError = DevError
        self.store = MemoryStore()
        self.project = {
            "id": "project-a",
            "device_id": "device-a",
            "root": "/fixture/project",
            "device_enabled": True,
            "mode": "write",
            "allow_tasks": True,
            "alias": "Fixture project",
            "device_name": "Fixture device",
        }
        self.target = {
            "id": "vps-a",
            "connection_revision": 1,
            "binding_id": "binding-a",
            "name": "Fixture VPS",
            "username": "fixture",
            "host": "fixture.invalid",
            "port": 22,
            "host_key_policy": "strict",
            "secret": "encrypted-fixture",
        }
        self.invalid_logins = set()

        def live_principal(store, value):
            if value.session_hash in self.invalid_logins:
                raise DevError("LOGIN_REQUIRED", "fixture login expired", 401)
            return value

        def get_project(pid, who):
            if pid != self.project["id"]:
                raise DevError("PROJECT_NOT_FOUND", "fixture unknown project", 404)
            return dict(self.project)

        def authorized_target(payload, pid, **kwargs):
            ref = payload["vps_ref"]
            if any(
                ref.get(key) != self.target[key]
                for key in ("id", "connection_revision", "binding_id")
            ):
                raise DevError("TARGET_REVISION_CHANGED", "fixture target changed", 409)
            return dict(self.target)

        vps = SimpleNamespace(
            reference=lambda payload, project: {
                key: self.target[key] for key in ("id", "connection_revision", "binding_id")
            },
            _authorized_row=authorized_target,
        )
        runtime = SimpleNamespace(
            store=self.store, vps=vps, project=get_project, authorize=lambda *args, **kwargs: None
        )
        auth = SimpleNamespace(session=lambda socket: dict(socket.auth_session))
        self.patch = patch(
            "hub.ssh_terminal.service.iam.live_principal", side_effect=live_principal
        )
        self.patch.start()
        self.service = TerminalService(runtime, auth, config=TerminalConfig())
        saved = self.service.repository.insert(row())
        self.session = HubSession(saved, principal(), dict(self.project))
        self.service.sessions[SID] = self.session
        self.agent = MemoryAgent()
        self.service.agents[self.agent.device_id] = self.agent

    async def asyncTearDown(self):
        await self.service.stop()
        self.patch.stop()
        self.store.db.close()

    def maintenance_gate(self, *, active=True):
        from shared.panel_maintenance import PanelMaintenance

        gate = PanelMaintenance(self.service.runtime)
        flag = SimpleNamespace(active=active)
        gate.active = lambda: flag.active
        self.service.runtime.panel_maintenance = gate
        return flag

    async def test_maintenance_uses_durable_remote_outcome_after_cache_eviction(self):
        self.service.sessions.clear()
        cases = [
            ("direct", state, {}, True)
            for state in (
                "creating",
                "connecting",
                "ready",
                "detached",
                "closing",
                "frozen",
                "close_unknown",
            )
        ] + [
            ("direct", "exited", {}, False),
            ("direct", "failed", {}, False),
            ("tmux", "exited", {"remote_session_state": "unknown"}, True),
            ("tmux", "failed", {"remote_session_state": "running"}, True),
            ("tmux", "failed", {}, True),
            ("tmux", "exited", {"remote_session_state": "ended"}, False),
            ("tmux", "failed", {"remote_session_state": "missing"}, False),
            ("tmux", "close_unknown", {"remote_session_state": "ended"}, True),
            ("unknown-mode", "exited", {}, True),
        ]
        for mode, state, metadata, busy in cases:
            with self.subTest(mode=mode, state=state, metadata=metadata):
                with self.store.transaction():
                    self.store.db.execute(
                        "UPDATE vps_terminal_sessions SET mode=?,state=?,metadata=? WHERE id=?",
                        (mode, state, json.dumps(metadata), SID),
                    )
                self.assertIs(busy, self.service.maintenance_busy())
        with self.store.transaction():
            self.store.db.execute(
                "UPDATE vps_terminal_sessions SET mode='tmux',state='exited',metadata='invalid json' WHERE id=?",
                (SID,),
            )
        self.assertTrue(self.service.maintenance_busy())

    async def test_maintenance_waits_for_eof_tail_tasks_and_detach_cleanup(self):
        await self.service.update(self.session, {"state": "exited", "output_complete": True})
        self.assertFalse(self.service.maintenance_busy())  # completed cache alone is harmless
        self.session.attachment = SimpleNamespace(closed=True)
        self.assertTrue(self.service.maintenance_busy())  # still owns its final output/exit
        self.session.attachment = None
        async with self.session.attachment_lock:
            self.assertTrue(self.service.maintenance_busy())  # detach RPC can still be in flight
        pending = self.service.spawn(asyncio.Event().wait(), "test-terminal-maintenance")
        try:
            self.assertTrue(self.service.maintenance_busy())
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        self.assertFalse(self.service.maintenance_busy())
        pending = asyncio.create_task(asyncio.Event().wait())
        self.session.create_task = pending
        try:
            self.assertTrue(self.service.maintenance_busy())
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        self.assertFalse(self.service.maintenance_busy())

    async def test_maintenance_rejects_new_admission_ticket_attach_and_takeover(self):
        from hub.ssh_terminal.browser import BrowserAttachment
        from hub.ssh_terminal.models import AttachTicket, CreateSession, SessionAction

        body = AttachTicket(renderer_id=RENDERER)
        ticket = await self.service.ticket(SID, principal(), body)
        socket = MemorySocket()
        attachment = BrowserAttachment(self.service, socket, self.session, RENDERER)
        self.maintenance_gate()
        actions = [
            lambda: self.service.create(
                principal(),
                CreateSession(project_id="project-a", vps_id="vps-a", request_id="ab" * 16),
            ),
            lambda: self.service.ticket(SID, principal(), body),
            lambda: self.service.consume_ticket(
                {"ticket": ticket["ticket"], "renderer_id": RENDERER}, socket
            ),
            lambda: self.service.ensure_ready(self.session),
            lambda: self.service.attach(self.session, attachment, {}),
            lambda: self.service.action(
                SID, principal(), "takeover", SessionAction(request_id="ac" * 16)
            ),
        ]
        for index, action in enumerate(actions):
            with self.subTest(action=index), self.assertRaises(self.DevError) as raised:
                await action()
            self.assertEqual("PANEL_UPDATING", raised.exception.code)
        with self.assertRaises(self.DevError) as raised:
            self.service.reserve_browser_socket()
        self.assertEqual("PANEL_UPDATING", raised.exception.code)
        self.assertEqual([], self.agent.requests)
        self.assertIsNone(self.session.attachment)

    async def test_maintenance_rechecks_pending_ticket_authentication(self):
        from hub.ssh_terminal.models import AttachTicket

        flag = self.maintenance_gate(active=False)
        authorize = self.service.authorize

        async def authorize_then_gate(*args, **kwargs):
            result = await authorize(*args, **kwargs)
            flag.active = True
            return result

        body = AttachTicket(renderer_id=RENDERER)
        with patch.object(self.service, "authorize", side_effect=authorize_then_gate):
            with self.assertRaises(self.DevError) as raised:
                await self.service.ticket(SID, principal(), body)
        self.assertEqual("PANEL_UPDATING", raised.exception.code)
        self.assertEqual({}, self.service.tickets)
        flag.active = False
        ticket = await self.service.ticket(SID, principal(), body)
        with patch.object(self.service, "authorize", side_effect=authorize_then_gate):
            with self.assertRaises(self.DevError) as raised:
                await self.service.consume_ticket(
                    {"ticket": ticket["ticket"], "renderer_id": RENDERER}, MemorySocket()
                )
        self.assertEqual("PANEL_UPDATING", raised.exception.code)
        self.assertEqual({}, self.service.tickets)

    async def test_maintenance_rechecks_status_and_fresh_credentials_before_tmux_recovery(self):
        await self.service.update(self.session, {"state": "detached", "mode": "tmux"})
        flag = self.maintenance_gate(active=False)
        authorize = self.service.authorize
        for boundary in ("status", "credentials"):
            with self.subTest(boundary=boundary):
                flag.active = False
                self.agent.requests.clear()

                async def handle(action, payload, project):
                    self.assertEqual("status", action)
                    if boundary == "status":
                        flag.active = True
                    return {"state": "detached", "mode": "tmux", "recoverability": "reauthenticate"}

                async def authorize_then_gate(*args, **kwargs):
                    result = await authorize(*args, **kwargs)
                    if kwargs.get("credentials"):
                        flag.active = True
                    return result

                self.agent.handler = handle
                with patch.object(self.service, "authorize", side_effect=authorize_then_gate):
                    with self.assertRaises(self.DevError) as raised:
                        await self.service.ensure_ready(self.session)
                self.assertEqual("PANEL_UPDATING", raised.exception.code)
                self.assertEqual(["status"], [item[0] for item in self.agent.requests])
                self.assertTrue(self.service.maintenance_busy())

    async def test_maintenance_rechecks_reservation_before_submitting_create(self):
        from hub.ssh_terminal.models import CreateSession

        flag = self.maintenance_gate(active=False)

        async def handle(action, payload, project):
            self.assertEqual("reserve_create", action)
            flag.active = True
            return {"admission_token": "synthetic-admission-token"}

        self.agent.handler = handle
        result = await self.service.create(
            principal(),
            CreateSession(
                project_id="project-a",
                vps_id="vps-a",
                request_id="ab" * 16,
            ),
        )
        created = self.service.sessions[result["session_id"]]
        await created.create_task
        self.assertEqual(["reserve_create"], [item[0] for item in self.agent.requests])
        self.assertEqual("failed", created.row["state"])
        self.assertEqual("PANEL_UPDATING", created.row["reason_code"])

    async def test_maintenance_rechecks_authorization_before_attach_rpc(self):
        from hub.ssh_terminal.browser import BrowserAttachment

        flag = self.maintenance_gate(active=False)
        attachment = BrowserAttachment(self.service, MemorySocket(), self.session, RENDERER)
        authorize = self.service.authorize
        authorized = 0

        async def authorize_then_gate(*args, **kwargs):
            nonlocal authorized
            result = await authorize(*args, **kwargs)
            authorized += 1
            if authorized == 2:
                flag.active = True
            return result

        with patch.object(self.service, "authorize", side_effect=authorize_then_gate):
            with self.assertRaises(self.DevError) as raised:
                await self.service.attach(self.session, attachment, {})
        self.assertEqual("PANEL_UPDATING", raised.exception.code)
        self.assertEqual(["status"], [item[0] for item in self.agent.requests])
        await self.service.detach(self.session, attachment)

    async def test_browser_detach_control_closes_without_waiting_for_disconnect(self):
        from hub.ssh_terminal.browser import BrowserAttachment

        socket = MemorySocket()
        attachment = BrowserAttachment(self.service, socket, self.session, RENDERER)
        self.session.attachment = attachment
        attachment.bind(self.agent, GEN, 1, 1, 1, 0, "live")
        await socket.incoming.put(
            {
                "type": "websocket.receive",
                "text": json.dumps(
                    {
                        "type": "detach",
                        "request_id": "80" * 16,
                    }
                ),
            }
        )
        await asyncio.wait_for(attachment._receive(), 0.25)
        self.assertTrue(socket.incoming.empty())

    async def test_browser_disconnect_cleans_up_unconfirmed_agent_attachment(self):
        from hub.ssh_terminal.browser import BrowserAttachment

        socket = MemorySocket()
        attachment = BrowserAttachment(self.service, socket, self.session, RENDERER)
        submitted = asyncio.Event()

        async def handle(action, payload, project):
            if action == "attach":
                submitted.set()
                await asyncio.Future()  # Agent can commit but lose the reply.
            return {
                "state": "ready",
                "mode": "direct",
                "runtime_generation": GEN,
                "stream_epoch": "1",
                "writer_epoch": "1",
                "cols": 100,
                "rows": 30,
            }

        self.agent.handler = handle
        lifetime = asyncio.create_task(attachment.run({}))
        try:
            await asyncio.wait_for(submitted.wait(), 1)
            await socket.incoming.put({"type": "websocket.disconnect"})
            await asyncio.wait_for(lifetime, 1)
        finally:
            if not lifetime.done():
                lifetime.cancel()
                await asyncio.gather(lifetime, return_exceptions=True)
        self.assertEqual(["status", "attach", "detach"], [item[0] for item in self.agent.requests])
        self.assertEqual(attachment.id, self.agent.requests[-1][1]["attachment_id"])
        self.assertIsNone(self.session.attachment)

    async def test_maintenance_keeps_existing_ack_otp_lease_and_explicit_close_usable(self):
        from hub.ssh_terminal.browser import BrowserAttachment

        socket = MemorySocket()
        attachment = BrowserAttachment(self.service, socket, self.session, RENDERER)
        self.session.attachment = attachment
        attachment.bind(self.agent, GEN, 1, 1, 1, 8, "live")
        attachment.attached.set()
        attachment.sent_offset = 8
        self.session.challenge = {"challenge_id": "ab" * 16}
        self.maintenance_gate()

        async def handle(action, payload, project):
            if action == "renew":
                return {"state": "ready"}
            if action == "auth_answer":
                return {}
            self.assertIn(action, {"close", "detach"})
            return {
                "state": "exited",
                "reason_code": "USER_CLOSED",
                "remote_session_state": "ended",
            }

        self.agent.handler = handle
        await self.service._renew_one(self.session)
        await socket.receive_controls(
            {
                "type": "output_ack",
                "attachment_id": attachment.id,
                "runtime_generation": GEN,
                "render_epoch": "1",
                "stream_epoch": "1",
                "rendered_offset": "8",
            },
            {"type": "auth_answer", "challenge_id": "ab" * 16, "responses": ["synthetic-otp"]},
            {"type": "close", "request_id": "ac" * 16, "expected_generation": GEN},
        )
        await attachment._receive()
        self.assertEqual(8, attachment.acked_offset)
        self.assertEqual(1, len(self.agent.acks))
        self.assertEqual(
            ["renew", "auth_answer", "close"], [item[0] for item in self.agent.requests]
        )
        self.assertEqual("exited", self.session.row["state"])
        self.assertTrue(self.service.maintenance_busy())  # exit tail still belongs to this socket
        await attachment.shutdown("USER_CLOSED", 1000)
        await self.service.detach(self.session, attachment)
        self.assertFalse(self.service.maintenance_busy())

    async def test_foreign_space_or_user_cannot_load_owned_session(self):
        for who in (principal(space="space-b"), principal(user="user-b")):
            with self.subTest(who=who), self.assertRaises(self.DevError) as raised:
                await self.service.load(SID, who)
            self.assertEqual("SESSION_NOT_FOUND", raised.exception.code)

    async def test_full_browser_queue_isolated_before_other_agent_reply_and_socket_close(self):
        from hub.ssh_terminal.agent_transport import TerminalAgentConnection
        from hub.ssh_terminal.browser import BrowserAttachment
        from shared.ssh_terminal_channel import TerminalSendQueue

        close_started, release_close = asyncio.Event(), asyncio.Event()

        class SlowBrowserSocket(MemorySocket):
            close_calls = 0

            async def close(self, **kwargs):
                self.close_calls += 1
                close_started.set()
                await release_close.wait()
                await super().close(**kwargs)

        class AgentSocket(MemorySocket):
            async def receive_text(self):
                return await self.incoming.get()

        browser_socket, agent_socket = SlowBrowserSocket(), AgentSocket()
        connection = TerminalAgentConnection(
            agent_socket,
            object(),
            GEN,
            self.service.config,
            device_id="device-a",
            on_event=self.service.on_agent_event,
        )
        self.service.agents["device-a"] = connection
        attachment = BrowserAttachment(self.service, browser_socket, self.session, RENDERER)
        self.session.attachment = attachment
        attachment.bind(connection, GEN, 1, 1, 1, 0, "live")
        attachment.attached.set()
        attachment.queue = TerminalSendQueue(max_items=2, max_control_items=1)
        first = TerminalFrame(FrameKind.OUTPUT, SID, GEN, 1, 1, 0, b"a")
        await attachment.output(first)
        event = {
            "type": "terminal_output",
            "session_id": SID,
            "runtime_generation": GEN,
            "attachment_id": attachment.id,
            "stream_epoch": "1",
            "writer_epoch": "1",
            "offset": "1",
            "data": base64.b64encode(b"b").decode("ascii"),
        }
        reply = asyncio.get_running_loop().create_future()
        connection.pending["other-session-control"] = reply
        agent_socket.incoming.put_nowait(json.dumps(event))
        agent_socket.incoming.put_nowait(
            json.dumps(
                {
                    "type": "terminal_reply",
                    "request_id": "other-session-control",
                    "result": {"state": "ready"},
                }
            )
        )

        async def decode(channel, raw):
            return json.loads(raw)

        receiver = None
        try:
            with patch("hub.ssh_terminal.agent_transport.unpack_channel", side_effect=decode):
                receiver = asyncio.create_task(connection._receive())
                result = await asyncio.wait_for(asyncio.shield(reply), 0.5)
                self.assertEqual({"state": "ready"}, result)
                self.assertTrue(attachment.closed)
                self.assertTrue(attachment.input_blocked)
                self.assertFalse(attachment.input_allowed)
                self.assertFalse(connection.closed)
                self.assertEqual(0, attachment.queue.waiting_producers)
                await asyncio.wait_for(close_started.wait(), 0.5)
                self.assertFalse(browser_socket.closed)
                # More notifications cannot create one cleanup task per frame.
                for _ in range(20):
                    await self.service.on_agent_event(connection, event)
                self.assertEqual(1, browser_socket.close_calls)
        finally:
            release_close.set()
            if receiver is not None:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            await attachment.shutdown("TEST_FINISHED", 1000)
            await connection.close()

    async def test_browser_socket_global_quota_counts_unbound_slots_and_releases(self):
        slots = [
            self.service.reserve_browser_socket()
            for _ in range(self.service.config.max_sessions_per_hub * 2)
        ]
        with self.assertRaises(self.DevError) as raised:
            self.service.reserve_browser_socket()
        self.assertEqual("TERMINAL_CONNECTION_LIMIT", raised.exception.code)
        self.service.release_browser_socket(slots[0])
        self.service.release_browser_socket(slots[0])
        replacement = self.service.reserve_browser_socket()
        self.assertEqual(len(slots), len(self.service.browser_socket_slots))
        for slot in [*slots, replacement]:
            self.service.release_browser_socket(slot)
        self.assertEqual({}, self.service.browser_socket_slots)

    async def test_agent_handshake_quota_counts_only_pending_and_releases_on_all_paths(self):
        first = self.service.reserve_agent_handshake("device-a")
        second = self.service.reserve_agent_handshake("device-a")
        with self.assertRaises(self.DevError) as raised:
            self.service.reserve_agent_handshake("device-a")
        self.assertEqual("TERMINAL_CONNECTION_LIMIT", raised.exception.code)
        self.service.release_agent_handshake(first)
        self.service.release_agent_handshake(first)
        replacement = self.service.reserve_agent_handshake("device-a")
        slots = [second, replacement]
        maximum = max(16, 2 * self.service.config.max_sessions_per_hub)
        for index in range(maximum - len(slots)):
            slots.append(self.service.reserve_agent_handshake(f"device-{index + 1}"))
        with self.assertRaises(self.DevError):
            self.service.reserve_agent_handshake("another-device")
        for slot in slots:
            self.service.release_agent_handshake(slot)
        for index in range(100):
            agent = MemoryAgent()
            agent.device_id = f"idle-{index}"
            self.service.agents[agent.device_id] = agent
        slot = self.service.reserve_agent_handshake("new-legitimate-device")
        self.assertEqual(1, len(self.service.agent_handshake_slots))
        self.service.release_agent_handshake(slot)

    async def test_diagnostic_rechecks_login_after_probe_without_reviving_or_rebinding(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def handle(action, payload, project):
            self.assertEqual("status", action)
            started.set()
            await release.wait()
            return {"runtime_generation": GEN, "state": "ready", "ssh_connected": True}

        self.agent.handler = handle
        original_principal = self.session.principal
        pending = asyncio.create_task(self.service.diagnostics(SID, principal("login-b")))
        try:
            await asyncio.wait_for(started.wait(), 1)
            with self.assertRaises(self.DevError) as busy:
                await self.service.diagnostics(SID, principal())
            self.assertEqual("DIAGNOSTIC_BUSY", busy.exception.code)
            self.invalid_logins.add("login-b")
            release.set()
            with self.assertRaises(self.DevError) as raised:
                await pending
            self.assertEqual("LOGIN_REQUIRED", raised.exception.code)
            self.assertIs(self.session.principal, original_principal)
            self.assertEqual({}, self.service.diagnostic_slots)
            self.assertEqual(["status"], [request[0] for request in self.agent.requests])
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_diagnostic_status_never_changes_state_or_revision(self):
        before = dict(self.session.row)

        async def handle(action, payload, project):
            self.assertEqual("status", action)
            return {
                "session_id": SID,
                "runtime_generation": GEN,
                "state": "failed",
                "state_revision": "999",
                "ssh_connected": False,
                "reason_code": "SSH_CONNECTION_FAILED",
            }

        self.agent.handler = handle
        snapshot = await self.service.diagnostics(SID, principal())
        self.assertFalse(snapshot["links"]["ssh"]["connected"])
        self.assertEqual(before, self.session.row)
        self.assertEqual(before, self.service.repository.get(SID))

    async def test_lifecycle_audit_contains_only_ids_and_records_recovery_and_revocation(self):
        from hub.ssh_terminal.browser import BrowserAttachment

        async def handle(action, payload, project):
            result = {
                "state": "ready",
                "mode": "direct",
                "runtime_generation": GEN,
                "stream_epoch": "1",
                "writer_epoch": payload.get("writer_epoch", "1"),
                "cols": 100,
                "rows": 30,
                "resume_strategy": "live",
                "start_offset": "0",
                "output_end_offset": "0",
                "next_input_seq": "1",
                "ssh_connected": True,
                "stage_timings_ms": {"ssh_connect": 12, "pty_open": 3},
                "agent_version": "1.33.37",
            }
            return result

        self.agent.handler = handle
        old_attachment = None
        for index in range(2):
            attachment = BrowserAttachment(self.service, MemorySocket(), self.session, RENDERER)
            await self.service.attach(self.session, attachment, {"renderer_id": RENDERER})
            await attachment.shutdown("TEST_FINISHED", 1000)
            if index == 0:
                # A reconnect can replace the closed attachment before its
                # old finally-block has reached service.detach().
                old_attachment = attachment
            else:
                await self.service.detach(self.session, attachment)
        await self.service.detach(self.session, old_attachment)
        await self.service._recovered(self.session)
        await self.service.revoke(self.session)
        audits = self.store.audits
        self.assertEqual(
            ["vps_terminal.connect", "vps_terminal.disconnect"] * 2
            + ["vps_terminal.recover", "vps_terminal.revoke"],
            [event[1] for event in audits],
        )
        for event in audits:
            self.assertEqual(
                {"space_id", "owner_user_id", "project_id", "device_id", "vps_id"}, set(event[4])
            )
            self.assertNotIn("fixture-password", json.dumps(event))
            self.assertNotIn("/fixture/project", json.dumps(event))
        metadata = json.loads(self.session.row["metadata"])
        self.assertEqual(1, metadata["reconnect_count"])
        self.assertEqual(1, metadata["recover_count"])
        self.assertGreater(metadata["disconnected_at"], metadata["connected_at"])
        self.assertIn("revoked_at", metadata)
        self.assertEqual({"ssh_connect", "pty_open", "attach"}, set(metadata["stage_timings_ms"]))

    async def test_browser_socket_user_quota_cannot_be_bypassed_by_new_login_or_space(self):
        identities = [
            principal("login-a"),
            principal("login-b"),
            principal("login-c", space="space-b"),
        ]
        count = self.service.config.max_sessions_per_user * 2
        slots = []
        try:
            for index in range(count):
                slot = self.service.reserve_browser_socket()
                slots.append(slot)
                self.service.bind_browser_socket(slot, identities[index % len(identities)].user_id)
            rejected = self.service.reserve_browser_socket()
            slots.append(rejected)
            with self.assertRaises(self.DevError) as raised:
                self.service.bind_browser_socket(rejected, principal("another-login").user_id)
            self.assertEqual("TERMINAL_CONNECTION_LIMIT", raised.exception.code)
            self.service.release_browser_socket(rejected)
            other_user = self.service.reserve_browser_socket()
            slots.append(other_user)
            self.service.bind_browser_socket(other_user, "user-b")
            self.assertEqual("user-b", self.service.browser_socket_slots[other_user])
        finally:
            for slot in slots:
                self.service.release_browser_socket(slot)
        self.assertEqual({}, self.service.browser_socket_slots)

    async def test_history_replay_waits_for_bound_agent_barrier_even_after_browser_ack(self):
        from hub.ssh_terminal.browser import BrowserAttachment

        socket = MemorySocket()
        attachment = BrowserAttachment(self.service, socket, self.session, RENDERER)
        self.session.attachment = attachment
        attachment.bind(self.agent, GEN, 1, 1, 1, 8, "history_replay")
        attachment.attached.set()
        attachment.sent_offset = 8
        await socket.receive_controls(
            {
                "type": "output_ack",
                "attachment_id": attachment.id,
                "runtime_generation": GEN,
                "render_epoch": "1",
                "stream_epoch": "1",
                "rendered_offset": "8",
            }
        )
        await attachment._receive()
        self.assertFalse(attachment.input_allowed)
        self.assertEqual(0, attachment.queue.queued_items)
        event = {
            "type": "terminal_replay_complete",
            "session_id": SID,
            "runtime_generation": GEN,
            "attachment_id": attachment.id,
            "render_epoch": "1",
            "stream_epoch": "1",
            "final_offset": "8",
        }
        await self.service.on_agent_event(self.agent, event)
        self.assertTrue(attachment.input_allowed)
        packet = await attachment.queue.get()
        self.assertEqual("replay_complete", packet["message"]["type"])
        self.assertEqual("8", packet["message"]["final_offset"])
        await self.service.on_agent_event(self.agent, event)
        self.assertEqual(0, attachment.queue.queued_items)

    async def test_empty_history_attach_does_not_unlock_without_agent_barrier(self):
        from hub.ssh_terminal.browser import BrowserAttachment

        async def handle(action, payload, project):
            return {
                "state": "ready",
                "mode": "direct",
                "runtime_generation": GEN,
                "stream_epoch": "1",
                "writer_epoch": payload.get("writer_epoch", "1"),
                "cols": 100,
                "rows": 30,
                "resume_strategy": "history_replay",
                "start_offset": "0",
                "output_end_offset": "0",
                "next_input_seq": "1",
            }

        self.agent.handler = handle
        attachment = BrowserAttachment(self.service, MemorySocket(), self.session, RENDERER)
        await self.service.attach(self.session, attachment, {"renderer_id": RENDERER})
        self.assertEqual(0, attachment.replay_boundary)
        self.assertFalse(attachment.input_allowed)
        self.assertEqual(0, attachment.queue.queued_items)
        await self.service.on_agent_event(
            self.agent,
            {
                "type": "terminal_replay_complete",
                "session_id": SID,
                "runtime_generation": GEN,
                "attachment_id": attachment.id,
                "render_epoch": str(attachment.render_epoch),
                "stream_epoch": "1",
                "final_offset": "0",
            },
        )
        self.assertTrue(attachment.input_allowed)

    async def test_replay_barrier_rejects_stale_binding_and_wrong_boundary(self):
        from hub.ssh_terminal.browser import BrowserAttachment

        attachment = BrowserAttachment(self.service, MemorySocket(), self.session, RENDERER)
        self.session.attachment = attachment
        attachment.bind(self.agent, GEN, 1, 1, 1, 8, "history_replay")
        attachment.attached.set()
        attachment.acked_offset = attachment.sent_offset = 8
        event = {
            "type": "terminal_replay_complete",
            "session_id": SID,
            "runtime_generation": GEN,
            "attachment_id": attachment.id,
            "render_epoch": "1",
            "stream_epoch": "1",
            "final_offset": "8",
        }
        for change in (
            {"runtime_generation": OTHER_GEN},
            {"attachment_id": "99" * 16},
            {"render_epoch": "2"},
            {"stream_epoch": "2"},
            {"final_offset": "0"},
            {"final_offset": "9"},
            {"final_offset": None},
        ):
            with self.subTest(change=change):
                candidate = {**event, **change}
                if candidate["final_offset"] is None:
                    candidate.pop("final_offset")
                await self.service.on_agent_event(self.agent, candidate)
                self.assertFalse(attachment.input_allowed)
                self.assertEqual(0, attachment.queue.queued_items)
        await self.service.on_agent_event(MemoryAgent(), event)
        self.assertFalse(attachment.input_allowed)
        attachment.acked_offset = 7
        await self.service.on_agent_event(self.agent, event)
        self.assertFalse(attachment.input_allowed)

    async def test_late_replay_barrier_cannot_reopen_ended_attachment(self):
        from hub.ssh_terminal.browser import BrowserAttachment

        attachment = BrowserAttachment(self.service, MemorySocket(), self.session, RENDERER)
        self.session.attachment = attachment
        attachment.bind(self.agent, GEN, 1, 1, 1, 8, "history_replay")
        attachment.attached.set()
        attachment.acked_offset = attachment.sent_offset = 8
        await attachment.exit({"final_offset": "8", "output_complete": True})
        event = {
            "type": "terminal_replay_complete",
            "session_id": SID,
            "runtime_generation": GEN,
            "attachment_id": attachment.id,
            "render_epoch": "1",
            "stream_epoch": "1",
            "final_offset": "8",
        }
        # Even before the persistent row catches up, exit is an irreversible
        # input fence for this attachment.
        self.assertEqual("ready", self.session.row["state"])
        await self.service.on_agent_event(self.agent, event)
        self.assertFalse(attachment.input_allowed)
        self.assertEqual(1, attachment.queue.queued_items)
        self.assertEqual("exit", (await attachment.queue.get())["message"]["type"])

    async def test_normal_eof_state_waits_for_final_offset_event_before_finishing_browser(self):
        from hub.ssh_terminal.browser import BrowserAttachment

        attachment = BrowserAttachment(self.service, MemorySocket(), self.session, RENDERER)
        self.session.attachment = attachment
        attachment.bind(self.agent, GEN, 1, 1, 1, 0, "live")
        attachment.attached.set()
        await self.service.on_agent_event(
            self.agent,
            {
                "type": "terminal_state",
                "session_id": SID,
                "runtime_generation": GEN,
                "state": "exited",
                "state_revision": "2",
                "output_complete": True,
                "reason_code": "EXITED",
            },
        )
        self.assertEqual("exited", self.session.row["state"])
        self.assertFalse(attachment.input_allowed)
        self.assertEqual(0, attachment.queue.queued_items)
        await self.service.on_agent_event(
            self.agent,
            {
                "type": "terminal_exit",
                "session_id": SID,
                "runtime_generation": GEN,
                "state": "exited",
                "state_revision": "3",
                "stream_epoch": "1",
                "attachment_id": attachment.id,
                "final_offset": "10",
                "output_complete": True,
                "reason_code": "EXITED",
            },
        )
        packet = await attachment.queue.get()
        self.assertEqual("exit", packet["message"]["type"])
        self.assertEqual("10", packet["message"]["final_offset"])

    async def test_close_cancels_inflight_admission_before_create_can_be_submitted(self):
        from hub.ssh_terminal.models import CreateSession, SessionAction

        entered, release = asyncio.Event(), asyncio.Event()

        async def handle(action, payload, project):
            if action == "reserve_create":
                entered.set()
                await release.wait()
                return {"admission_token": "synthetic-admission-token"}
            if action == "close":
                return {"state": "exited", "reason_code": "USER_CLOSED"}
            if action == "create":
                raise AssertionError("A cancelled start opened a shell after close")
            raise AssertionError(action)

        self.agent.handler = handle
        created = await self.service.create(
            principal(),
            CreateSession(
                project_id="project-a",
                vps_id="vps-a",
                request_id="ab" * 16,
            ),
        )
        await asyncio.wait_for(entered.wait(), 1)
        result = await asyncio.wait_for(
            self.service.action(
                created["session_id"],
                principal(),
                "close",
                SessionAction(request_id="cd" * 16),
            ),
            1,
        )
        release.set()
        await asyncio.sleep(0)
        self.assertEqual("exited", result["state"])
        self.assertEqual(
            ["reserve_create", "close"], [request[0] for request in self.agent.requests]
        )
        self.assertTrue(self.service.sessions[created["session_id"]].create_task.done())

    async def test_revoke_cancels_inflight_admission_before_create_can_be_submitted(self):
        from hub.ssh_terminal.models import CreateSession

        entered, release = asyncio.Event(), asyncio.Event()

        async def handle(action, payload, project):
            if action == "reserve_create":
                entered.set()
                await release.wait()
                return {"admission_token": "synthetic-admission-token"}
            if action == "revoke":
                return {"state": "exited", "reason_code": "AUTH_REVOKED"}
            if action == "create":
                raise AssertionError("A revoked start opened a shell")
            raise AssertionError(action)

        self.agent.handler = handle
        created = await self.service.create(
            principal(),
            CreateSession(
                project_id="project-a",
                vps_id="vps-a",
                request_id="ac" * 16,
            ),
        )
        await asyncio.wait_for(entered.wait(), 1)
        session = self.service.sessions[created["session_id"]]
        await asyncio.wait_for(self.service.revoke(session), 1)
        release.set()
        await asyncio.sleep(0)
        self.assertTrue(session.revoked)
        self.assertTrue(session.create_task.done())
        self.assertEqual("failed", session.row["state"])
        self.assertEqual("AUTH_REVOKED", session.row["reason_code"])
        self.assertEqual(
            ["reserve_create", "revoke"], [request[0] for request in self.agent.requests]
        )

    async def test_session_cache_prunes_only_idle_completed_objects_and_keeps_durable_identity(
        self,
    ):
        from hub.ssh_terminal.models import CreateSession
        from hub.ssh_terminal.service import HubSession

        body = CreateSession(project_id="project-a", vps_id="vps-a", request_id="bc" * 16)
        created = await self.service.create(principal(), body)
        victim = self.service.sessions[created["session_id"]]
        await victim.create_task
        await self.service.update(victim, {"state": "exited", "reason_code": "USER_CLOSED"})
        with self.store.transaction():
            self.store.db.execute(
                "UPDATE vps_terminal_sessions SET updated=0 WHERE id=?", (victim.row["id"],)
            )
        victim.row = self.service.repository.get(victim.row["id"])
        limit = max(128, 2 * self.service.config.max_sessions_per_hub)
        for index in range(1, limit + 9):
            saved = self.service.repository.insert(
                row(
                    id=f"{index:032x}",
                    request_id=f"{index + 1000:032x}",
                    state="exited",
                    updated=float(index),
                )
            )
            self.service.sessions[saved["id"]] = HubSession(saved, principal(), dict(self.project))
        keep, ticket, attached, starting, live = [f"{index:032x}" for index in range(1, 6)]
        self.service.tickets["fixture-ticket"] = {
            "session": self.service.sessions[ticket],
            "expires": self.service.clock() + 60,
        }
        self.service.sessions[attached].attachment = SimpleNamespace(shutdown=AsyncMock())
        pending = asyncio.create_task(asyncio.Event().wait())
        self.service.sessions[starting].create_task = pending
        await self.service.update(self.service.sessions[live], {"state": "detached"})
        count_before = self.store.one("SELECT COUNT(*) AS n FROM vps_terminal_sessions")["n"]
        try:
            self.service.prune_session_cache(keep=keep)
            self.assertEqual(limit, len(self.service.sessions))
            self.assertNotIn(victim.row["id"], self.service.sessions)
            self.assertTrue(
                {SID, keep, ticket, attached, starting, live} <= self.service.sessions.keys()
            )
            self.assertEqual(
                count_before, self.store.one("SELECT COUNT(*) AS n FROM vps_terminal_sessions")["n"]
            )
            self.assertEqual(
                victim.row["id"],
                self.service.repository.by_request("space-a", "user-a", body.request_id)["id"],
            )
            calls_before = len(self.agent.requests)
            replay = await self.service.create(principal(), body)
            self.assertEqual(victim.row["id"], replay["session_id"])
            self.assertEqual("exited", replay["state"])
            self.assertEqual(calls_before, len(self.agent.requests))
            loaded = await self.service.load(victim.row["id"], principal())
            self.assertEqual(victim.row["id"], loaded.row["id"])
            self.assertEqual(limit, len(self.service.sessions))
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_hub_budget_counts_active_devices_without_charging_idle_agent_channels(self):
        from hub.ssh_terminal.models import CreateSession

        one_session = 2 * self.service.config.hub_relay_bytes + self.service.config.frame_bytes
        one_device = 4 * self.service.config.hub_relay_bytes + self.service.config.frame_bytes
        self.service.config = replace(
            self.service.config, hub_buffer_bytes=4 * one_session + one_device
        )
        for index in range(100):
            idle = MemoryAgent()
            idle.device_id = f"idle-device-{index}"
            self.service.agents[idle.device_id] = idle
        first = await self.service.create(
            principal(),
            CreateSession(
                project_id="project-a",
                vps_id="vps-a",
                request_id="fa" * 16,
            ),
        )
        await self.service.sessions[first["session_id"]].create_task
        self.assertEqual(1, self.service.repository.active_device_count())
        other = self.service.repository.insert(
            row(
                id="fb" * 16,
                request_id="fc" * 16,
                device_id="active-device-b",
                owner_user_id="user-b",
            )
        )
        self.assertEqual(2, self.service.repository.active_device_count())
        proposed = CreateSession(project_id="project-a", vps_id="vps-a", request_id="fd" * 16)
        with self.assertRaises(self.DevError) as raised:
            await self.service.create(principal(), proposed)
        self.assertEqual("SESSION_LIMIT", raised.exception.code)
        self.service.repository.update(other["id"], {"state": "exited"})
        self.assertEqual(1, self.service.repository.active_device_count())
        allowed = await self.service.create(principal(), proposed)
        self.assertNotEqual(first["session_id"], allowed["session_id"])
        await self.service.sessions[allowed["session_id"]].create_task

    async def test_ticket_is_single_use_even_under_concurrent_authentication(self):
        from hub.ssh_terminal.models import AttachTicket

        issued = await self.service.ticket(SID, principal(), AttachTicket(renderer_id=RENDERER))
        body = {"type": "authenticate", "ticket": issued["ticket"], "renderer_id": RENDERER}
        socket = MemorySocket()
        outcomes = await asyncio.gather(
            self.service.consume_ticket(body, socket),
            self.service.consume_ticket(body, socket),
            return_exceptions=True,
        )
        self.assertEqual(1, sum(isinstance(value, tuple) for value in outcomes))
        errors = [value for value in outcomes if isinstance(value, Exception)]
        self.assertEqual(1, len(errors))
        self.assertEqual("ATTACH_TICKET_INVALID", errors[0].code)

    async def test_ticket_cannot_be_transferred_to_another_login(self):
        from hub.ssh_terminal.models import AttachTicket

        issued = await self.service.ticket(SID, principal(), AttachTicket(renderer_id=RENDERER))
        socket = MemorySocket()
        socket.auth_session["id_hash"] = "login-b"
        with self.assertRaises(self.DevError):
            await self.service.consume_ticket(
                {"type": "authenticate", "ticket": issued["ticket"], "renderer_id": RENDERER},
                socket,
            )

    async def test_new_login_lookup_cannot_reauthorize_old_browser_socket(self):
        from hub.ssh_terminal.browser import BrowserAttachment

        socket = MemorySocket()
        attachment = BrowserAttachment(self.service, socket, self.session, RENDERER)
        self.session.attachment = attachment
        attachment.bind(self.agent, GEN, 1, 1, 1, 0, "live")
        attachment.attached.set()
        await self.service.load(SID, principal("login-b"))
        self.invalid_logins.add("login-a")
        frame = TerminalFrame(FrameKind.INPUT, SID, GEN, 1, 1, 1, b"should-not-run\r")
        await socket.incoming.put({"type": "websocket.receive", "bytes": encode_frame(frame)})
        await socket.incoming.put({"type": "websocket.disconnect"})
        with self.assertRaises(self.DevError):
            await attachment._receive()
        self.assertEqual([], self.agent.inputs)

    async def test_input_ack_is_valid_and_scoped_to_current_attachment(self):
        from hub.ssh_terminal.browser import BrowserAttachment

        attachment = BrowserAttachment(self.service, MemorySocket(), self.session, RENDERER)
        self.session.attachment = attachment
        attachment.bind(self.agent, GEN, 1, 1, 1, 0, "live")
        await self.service.on_agent_event(
            self.agent,
            {
                "type": "terminal_input_ack",
                "session_id": SID,
                "runtime_generation": GEN,
                "writer_epoch": "1",
                "stream_epoch": "1",
                "input_seq": "1",
                "status": "forwarded",
            },
        )
        packet = await attachment.queue.get()
        message = packet["message"]
        validate_control(message, direction="server")
        self.assertEqual("input_ack", message["type"])
        self.assertEqual(attachment.id, message["attachment_id"])
        self.assertEqual(GEN, message["runtime_generation"])
        self.assertEqual("1", message["render_epoch"])

    async def test_new_agent_generation_cannot_update_old_session_before_recovery(self):
        new_agent = MemoryAgent(OTHER_GEN)
        self.service.agents[new_agent.device_id] = new_agent
        await self.service.on_agent_event(
            new_agent,
            {
                "type": "terminal_state",
                "session_id": SID,
                "runtime_generation": OTHER_GEN,
                "state": "exited",
                "reason_code": "FOREIGN_RUNTIME",
                "writer_epoch": "8",
            },
        )
        self.assertEqual(GEN, self.session.row["runtime_generation"])
        self.assertEqual("ready", self.session.row["state"])
        self.assertEqual("1", self.session.row["writer_epoch"])

    async def test_late_rpc_snapshot_cannot_overwrite_newer_agent_state(self):
        await self.service.on_agent_event(
            self.agent,
            {
                "type": "terminal_state",
                "session_id": SID,
                "runtime_generation": GEN,
                "state": "ready",
                "state_revision": "12",
                "writer_epoch": "3",
                "stream_epoch": "2",
            },
        )
        # A status request captured this snapshot before the event above, but
        # its receipt arrived later through the prioritized control queue.
        await self.service.update(
            self.session,
            {
                "runtime_generation": GEN,
                "state": "detached",
                "state_revision": "11",
                "writer_epoch": "2",
                "stream_epoch": "1",
            },
        )
        self.assertEqual("ready", self.session.row["state"])
        self.assertEqual("3", self.session.row["writer_epoch"])
        self.assertEqual("2", self.session.row["stream_epoch"])
        durable = self.service.repository.get(SID)
        self.assertEqual("ready", durable["state"])
        self.assertEqual("3", durable["writer_epoch"])

    async def test_concurrent_state_updates_compare_revision_inside_database_phase(self):
        await asyncio.gather(
            self.service.update(
                self.session,
                {
                    "runtime_generation": GEN,
                    "state": "ready",
                    "state_revision": "12",
                    "writer_epoch": "3",
                },
            ),
            self.service.update(
                self.session,
                {
                    "runtime_generation": GEN,
                    "state": "detached",
                    "state_revision": "11",
                    "writer_epoch": "2",
                },
            ),
        )
        self.assertEqual("ready", self.session.row["state"])
        self.assertEqual("3", self.session.row["writer_epoch"])
        durable = self.service.repository.get(SID)
        self.assertEqual("ready", durable["state"])
        self.assertEqual("12", json.loads(durable["metadata"])["state_revision"])

    async def test_old_rpc_generation_cannot_undo_an_explicit_recovery_transition(self):
        # Model a completed, authorized transition to a new Agent process.
        with self.store.transaction():
            self.store.db.execute(
                "UPDATE vps_terminal_sessions SET runtime_generation=?,metadata=? WHERE id=?",
                (OTHER_GEN, json.dumps({"state_revision": "1"}), SID),
            )
        self.session.row = self.service.repository.get(SID)
        await self.service.update(
            self.session,
            {
                "runtime_generation": GEN,
                "state": "exited",
                "state_revision": "100",
                "reason_code": "OLD_RUNTIME_EXIT",
            },
        )
        self.assertEqual(OTHER_GEN, self.session.row["runtime_generation"])
        self.assertEqual("ready", self.session.row["state"])
        self.assertEqual(OTHER_GEN, self.service.repository.get(SID)["runtime_generation"])

    async def test_tmux_attachment_exit_does_not_claim_remote_session_ended(self):
        await self.service.update(self.session, {"mode": "tmux"})
        await self.service.on_agent_event(
            self.agent,
            {
                "type": "terminal_exit",
                "session_id": SID,
                "runtime_generation": GEN,
                "stream_epoch": "1",
                "state": "detached",
                "remote_session_state": "running",
                "final_offset": "0",
                "output_complete": True,
                "reason_code": "DETACHED",
            },
        )
        self.assertEqual("detached", self.session.row["state"])
        self.assertEqual(
            "running", self.service.repository.public(self.session.row)["remote_session_state"]
        )

    async def test_recovery_does_not_clear_ready_event_after_agent_reply(self):
        await self.service.update(self.session, {"mode": "tmux", "state": "failed"})

        async def handle(action, payload, project):
            if action == "status":
                return {
                    "state": "failed",
                    "mode": "tmux",
                    "reason_code": "SSH_CONNECTION_LOST",
                    "recoverability": "reauthenticate",
                    "ssh_connected": False,
                }
            if action == "recover":
                await self.service.update(self.session, {"state": "ready", "reason_code": None})
                return {"state": "ready", "mode": "tmux"}
            raise AssertionError(action)

        self.agent.handler = handle
        actual = await asyncio.wait_for(self.service.ensure_ready(self.session), timeout=0.2)
        self.assertIs(self.agent, actual)
        self.assertEqual("ready", self.session.row["state"])

    async def test_detached_tmux_without_ssh_requires_fresh_authorized_recovery(self):
        await self.service.update(self.session, {"mode": "tmux", "state": "detached"})

        async def handle(action, payload, project):
            if action == "status":
                return {
                    "state": "detached",
                    "mode": "tmux",
                    "ssh_connected": False,
                    "reason_code": "AUTH_LEASE_EXPIRED",
                    "recoverability": "reauthenticate",
                }
            if action == "recover":
                self.assertEqual("fixture-password-never-persisted", payload.get("password"))
                self.assertNotEqual("1", str(payload.get("authority_revision")))
                await self.service.update(self.session, {"state": "ready", "reason_code": None})
                return {"state": "ready", "mode": "tmux", "ssh_connected": True}
            raise AssertionError(action)

        self.agent.handler = handle
        actual = await asyncio.wait_for(self.service.ensure_ready(self.session), timeout=0.2)
        self.assertIs(self.agent, actual)
        self.assertEqual(["status", "recover"], [request[0] for request in self.agent.requests])
        self.assertNotIn(
            "fixture-password-never-persisted", json.dumps(self.service.repository.get(SID))
        )

    async def test_replaced_agent_socket_forces_its_browser_to_reattach(self):
        attachment = SimpleNamespace(agent=self.agent, closed=False, shutdown=AsyncMock())
        self.session.attachment = attachment
        replacement = MemoryAgent()
        await self.service.register_agent(replacement)
        self.assertTrue(self.agent.closed)
        attachment.shutdown.assert_awaited()
        self.assertIs(replacement, self.service.agents["device-a"])

    async def test_concurrent_duplicate_create_opens_one_logical_session(self):
        from hub.ssh_terminal.models import CreateSession

        body = CreateSession(vps_id="vps-a", project_id="project-a", request_id="99" * 16)
        first, second = await asyncio.gather(
            self.service.create(principal(), body), self.service.create(principal(), body)
        )
        self.assertEqual(first["session_id"], second["session_id"])
        await asyncio.gather(*tuple(self.service.tasks))
        requests = [request for request in self.agent.requests if request[0] == "create"]
        self.assertEqual(1, len(requests))
        self.assertNotIn("fixture-password-never-persisted", json.dumps(self.store.audits))

    async def test_stale_binding_cannot_issue_attachment_ticket(self):
        from hub.ssh_terminal.models import AttachTicket

        self.target["binding_id"] = "binding-replaced"
        with self.assertRaises(self.DevError) as raised:
            await self.service.ticket(SID, principal(), AttachTicket(renderer_id=RENDERER))
        self.assertEqual("TARGET_REVISION_CHANGED", raised.exception.code)
        self.assertEqual({}, self.service.tickets)

    def _seed_routing_schema(self):
        with self.store.transaction():
            self.store.db.executescript("""
                CREATE TABLE IF NOT EXISTS devices (
                    id TEXT PRIMARY KEY, name TEXT, space_id TEXT, enabled INTEGER, created REAL
                );
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, alias TEXT, alias_key TEXT, device_id TEXT,
                    root TEXT, mode TEXT, allow_tasks INTEGER, space_id TEXT
                );
                CREATE TABLE IF NOT EXISTS vps_connections (
                    id TEXT PRIMARY KEY, space_id TEXT, connection_revision INTEGER, enabled INTEGER
                );
                CREATE TABLE IF NOT EXISTS vps_projects (
                    vps_id TEXT, project_id TEXT, binding_id TEXT
                );
            """)
            self.store.db.execute(
                "INSERT INTO devices VALUES ('device-a','Fixture device','space-a',1,1.0)"
            )
            self.store.db.execute(
                "INSERT INTO projects VALUES ('project-a','Fixture project','fixture project','device-a','/fixture/project','write',1,'space-a')"
            )
            self.store.db.execute(
                "INSERT INTO vps_connections VALUES ('vps-a','space-a',1,1),('vps-free','space-a',1,1)"
            )
            self.store.db.execute(
                "ALTER TABLE vps_connections ADD COLUMN host_key_policy TEXT NOT NULL DEFAULT 'strict'"
            )
            self.store.db.execute(
                "INSERT INTO vps_projects VALUES ('vps-a','project-a','binding-a')"
            )

        def direct_target(ref, space_id, **kwargs):
            if ref.get("binding_id") != "direct:" + ref.get("id", ""):
                raise self.DevError("VPS_AUTHORIZATION_CHANGED", "bad direct binding", 403)
            return {**self.target, "id": ref["id"], "binding_id": ref["binding_id"]}

        self.service.runtime.vps._authorized_direct_row = direct_target

    async def test_admin_direct_unassigned_vps_and_legacy_1340_carrier_fallback(self):
        from hub.ssh_terminal.models import CreateSession

        self._seed_routing_schema()
        admin = Principal(
            actor="panel:owner",
            user_id="owner-a",
            scopes={"read", "write", "execute"},
            projects=[],
            space_id="space-a",
            session_hash="login-owner",
            admin=True,
        )
        self.agent.direct_vps = True
        self.agent.direct_vps_allowed = True
        native = await self.service.create(
            admin,
            CreateSession(vps_id="vps-free", project_id=None, request_id="a1" * 16),
        )
        await self.service.sessions[native["session_id"]].create_task
        self.assertEqual("", native["project_id"])
        self.assertEqual("", native["project_alias"])
        self.assertTrue(native["direct_vps"])
        self.assertTrue(self.agent.requests[-1][2]["_direct_vps"])

        self.agent.direct_vps = False
        carrier = await self.service.create(
            admin,
            CreateSession(vps_id="vps-free", project_id=None, request_id="a2" * 16),
        )
        await self.service.sessions[carrier["session_id"]].create_task
        self.assertEqual("project-a", carrier["project_id"])
        self.assertEqual("", carrier["project_alias"])
        self.assertTrue(carrier["direct_vps"])
        self.assertEqual(
            "direct:vps-free", self.service.sessions[carrier["session_id"]].row["binding_id"]
        )

    async def test_admin_demotion_immediately_revokes_direct_session(self):
        from hub.ssh_terminal.models import CreateSession

        self._seed_routing_schema()
        self.agent.direct_vps = True
        admin = Principal(
            actor="panel:owner",
            user_id="owner-a",
            scopes={"read", "write", "execute"},
            projects=[],
            space_id="space-a",
            session_hash="login-owner",
            admin=True,
        )
        created = await self.service.create(
            admin,
            CreateSession(vps_id="vps-free", project_id=None, request_id="a3" * 16),
        )
        session = self.service.sessions[created["session_id"]]
        await session.create_task
        with patch(
            "hub.ssh_terminal.service.iam.live_principal",
            side_effect=lambda _s, p: replace(p, admin=False),
        ):
            await self.service._renew_one(session)
        self.assertTrue(session.revoked)
        self.assertEqual("failed", session.row["state"])
        self.assertEqual("AUTH_REVOKED", session.row["reason_code"])
        self.assertEqual("revoke", self.agent.requests[-1][0])

    async def test_member_auto_selects_executable_bound_project_without_denied_audit(self):
        from hub.ssh_terminal.models import CreateSession

        self._seed_routing_schema()
        with self.store.transaction():
            self.store.db.execute(
                "INSERT INTO projects VALUES ('project-ro','Readonly','a-readonly','device-a','/ro','write',1,'space-a')"
            )
            self.store.db.execute(
                "INSERT INTO vps_projects VALUES ('vps-a','project-ro','binding-ro')"
            )
        authorized_projects = []
        self.service.runtime.authorize = lambda _p, action, project_id=None: (
            authorized_projects.append((action, project_id))
        )
        member = Principal(
            actor="panel:alice",
            user_id="user-a",
            scopes={"read", "write", "execute"},
            projects=["project-ro", "project-a"],
            space_id="space-a",
            session_hash="login-a",
        )
        perms = {"project-ro": {"read"}, "project-a": {"read", "write", "execute"}}
        with patch("hub.ssh_terminal.service.iam.project_permissions", return_value=perms):
            result = await self.service.create(
                member,
                CreateSession(vps_id="vps-a", project_id=None, request_id="a4" * 16),
            )
        await self.service.sessions[result["session_id"]].create_task
        self.assertEqual("project-a", result["project_id"])
        self.assertEqual(
            [("write", "project-a"), ("execute", "project-a")] * 2, authorized_projects
        )


if __name__ == "__main__":
    unittest.main()
