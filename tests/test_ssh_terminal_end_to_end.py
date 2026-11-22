"""Real TCP Hub → authenticated Agent → SSH/PTY integration, synthetic identity only."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import time
import uuid

import asyncssh
import httpx
import pytest
import uvicorn
import websockets

from agent.runner import Agent
from agent.ssh_terminal_transport import AsyncSSHTransport
from hub.app import create_app
from hub.ssh_terminal.server_protocol import TerminalAwareWebSocketProtocol
from shared.crypto import digest, token
from shared.ssh_terminal_protocol import FrameKind, TerminalFrame, decode_frame, encode_frame
from shared.util import atomic_json
from tests.fixture_hub import bind_listener
from tests.legacy_iam_fixture import seed_owner


async def until(predicate, seconds=8):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("authentication", ["password", "keyboard-interactive"])
async def test_full_tcp_terminal_flow_control_reconnect_input_and_final_output(tmp_path, monkeypatch, authentication):
    chunk = bytes(range(256)) * 64
    bulk = chunk * 64 + b"\r\nbulk-complete\r\n"
    seen = {"input": [], "sizes": [], "passwords": [], "responses": []}
    otp = "482916-synthetic-one-time-code"
    producers = set()

    class RemoteTerminal(asyncssh.SSHServerSession):
        def connection_made(self, channel):
            self.channel = channel
            self.writable = asyncio.Event()
            self.writable.set()

        def pty_requested(self, term, size, modes):
            seen["term"] = term
            seen["sizes"].append(size[:2])
            return True

        def terminal_size_changed(self, cols, rows, *rest):
            seen["sizes"].append((cols, rows))

        def shell_requested(self):
            return True

        def session_started(self):
            self.channel.write(b"synthetic-ready\r\n")

        def pause_writing(self):
            self.writable.clear()

        def resume_writing(self):
            self.writable.set()

        async def send_bulk(self):
            for offset in range(0, len(bulk), len(chunk)):
                await self.writable.wait()
                self.channel.write(bulk[offset:offset + len(chunk)])
                await asyncio.sleep(0)

        def data_received(self, data, datatype):
            seen["input"].append(bytes(data))
            if data == b"bulk\r":
                task = asyncio.create_task(self.send_bulk())
                producers.add(task)
                task.add_done_callback(producers.discard)
            elif data == b"exit\r":
                self.channel.write(b"sentinel-final\r\n")
                self.channel.exit(0)
            else:
                self.channel.write(data)

    class SSHPeer(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return authentication == "password"

        def validate_password(self, username, password):
            seen["passwords"].append(password)
            return username == "fixture" and password == "synthetic-terminal-password"

        def kbdint_auth_supported(self):
            return authentication == "keyboard-interactive"

        def get_kbdint_challenge(self, username, lang, submethods):
            return "Verification", "Enter a one-time code", "", [("Verification code: ", False)]

        def validate_kbdint_response(self, username, responses):
            seen["responses"].append(tuple(responses))
            return username == "fixture" and responses == [otp]

        def session_requested(self):
            return RemoteTerminal()

    peer = await asyncssh.create_server(SSHPeer, "127.0.0.1", 0,
                                        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
                                        encoding=None, line_editor=False)
    listener = bind_listener()
    port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("HUB_PUBLIC_URL", base)
    monkeypatch.setenv("HUB_OIDC_ENABLED", "0")
    app = create_app(str(tmp_path / "hub"))
    store, auth, runtime = app.state.store, app.state.auth, app.state.runtime
    seed_owner(store)
    device_id, project_id, secret = uuid.uuid4().hex, uuid.uuid4().hex, token()
    root = tmp_path / "project"
    root.mkdir()
    store.execute("INSERT INTO devices(id,name,secret,created,space_id,owner_user_id) VALUES (?,?,?,?,?,?)",
                  (device_id, "Synthetic SSH Agent", store.encrypt(secret), time.time(), "legacy", "owner"))
    store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created,space_id,owner_user_id) VALUES (?,?,?,?,?,'write',1,?,'legacy','owner')",
                  (project_id, "TerminalFixture", "terminalfixture", device_id, str(root), time.time()))
    with store.lock:
        login = auth.new_session("owner")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                         ws=TerminalAwareWebSocketProtocol,
                                         access_log=False, timeout_graceful_shutdown=3))
    hub_task = asyncio.create_task(server.serve(sockets=[listener]))
    config_path = tmp_path / "agent.json"
    atomic_json(config_path, {"device_id": device_id, "name": "Synthetic SSH Agent", "secret": secret,
                              "hub_url": base, "state_dir": str(tmp_path / "agent-state"),
                              "allowed_roots": [{"path": str(root), "writable": True, "allow_tasks": True}],
                              "tasks": {}, "shell": {"enabled": True, "projects": ["*"]}})
    agent = Agent(config_path)
    agent.ssh_terminal.manager.connector = lambda: AsyncSSHTransport(
        agent.ssh_terminal.config, trust_sources=[tmp_path / "synthetic_known_hosts"])
    agent_task = None
    client = httpx.AsyncClient(base_url=base, trust_env=False, cookies={"rd_session": login["cookie"]},
                               headers={"Origin": base, "x-rd-csrf": login["csrf"], "x-codepier-space": "legacy"})
    try:
        await until(lambda: server.started)
        agent_task = asyncio.create_task(agent.connect_once())
        await until(lambda: device_id in runtime.ssh_terminal.agents)
        response = await client.post("/api/vps", json={"name": "Synthetic terminal VPS", "host": "127.0.0.1",
                                                       "port": peer.get_port(), "username": "fixture",
                                                       "password": "synthetic-terminal-password", "host_key_policy": "accept-new",
                                                       "project_ids": [project_id]})
        assert response.status_code == 200, response.text
        vps = response.json()
        capability = await client.get("/api/vps-terminal/capabilities", params={"vps_id": vps["id"], "project_id": project_id})
        assert capability.status_code == 200 and capability.json()["available"], capability.text
        request = {"vps_id": vps["id"], "project_id": project_id, "request_id": uuid.uuid4().hex,
                   "mode": "direct", "cols": 120, "rows": 30}
        created = await client.post("/api/vps-terminal/sessions", json=request)
        assert created.status_code == 200, created.text
        sid = created.json()["id"]
        retry = await client.post("/api/vps-terminal/sessions", json=request)
        assert retry.json()["id"] == sid
        renderer = uuid.uuid4().hex
        output = bytearray()
        controls = []
        attached = None

        async def ticket(resume=None):
            response = await client.post(f"/api/vps-terminal/sessions/{sid}/attach-ticket",
                                         json={"renderer_id": renderer, **(resume or {})})
            assert response.status_code == 200, response.text
            return response.json()["ticket"]

        async def receive(socket):
            raw = await asyncio.wait_for(socket.recv(), 8)
            if isinstance(raw, bytes):
                frame = decode_frame(raw)
                assert frame.kind == FrameKind.OUTPUT and frame.session_id == sid
                assert frame.sequence == len(output)
                output.extend(frame.payload)
                await socket.send(json.dumps({"type": "output_ack", "attachment_id": attached["attachment_id"],
                                               "render_epoch": attached["render_epoch"], "stream_epoch": attached["stream_epoch"],
                                               "runtime_generation": attached["runtime_generation"], "rendered_offset": str(len(output))}))
                return None
            message = json.loads(raw)
            controls.append(message)
            assert message["type"] != "error", message
            if message["type"] == "auth_challenge":
                assert message["prompts"] == [{"text": "Verification code: ", "echo": False}]
                await socket.send(json.dumps({"type": "auth_answer", "challenge_id": message["challenge_id"],
                                               "responses": [otp]}))
            return message

        url = f"ws://127.0.0.1:{port}/ws/vps-terminal"
        headers = {"Cookie": "rd_session=" + login["cookie"]}
        first_ticket = await ticket()
        async with websockets.connect(url, origin=base, additional_headers=headers, proxy=None) as browser:
            await browser.send(json.dumps({"type": "authenticate", "ticket": first_ticket, "renderer_id": renderer, "protocol_version": 1}))
            while attached is None:
                message = await receive(browser)
                if message and message["type"] == "attached":
                    attached = message
            frame = TerminalFrame(FrameKind.INPUT, sid, attached["runtime_generation"], int(attached["stream_epoch"]),
                                  int(attached["writer_epoch"]), 1, b"bulk\r")
            await browser.send(encode_frame(frame))
            async with asyncio.timeout(15):
                while not output.endswith(b"bulk-complete\r\n") or not any(m["type"] == "input_ack" for m in controls):
                    await receive(browser)
            assert bytes(output) == b"synthetic-ready\r\n" + bulk
            assert seen["input"].count(b"bulk\r") == 1
            await browser.send(json.dumps({"type": "resize", "attachment_id": attached["attachment_id"],
                                           "runtime_generation": attached["runtime_generation"], "stream_epoch": attached["stream_epoch"],
                                           "writer_epoch": attached["writer_epoch"], "resize_seq": "1", "cols": 90, "rows": 26}))
            await until(lambda: (90, 26) in seen["sizes"])
        await until(lambda: not agent.ssh_terminal.manager.sessions[sid].attached)
        resume = {"runtime_generation": attached["runtime_generation"], "known_epoch": attached["stream_epoch"], "rendered_offset": str(len(output))}
        second_ticket = await ticket(resume)
        previous_writer = attached["writer_epoch"]
        attached = None
        async with websockets.connect(url, origin=base, additional_headers=headers, proxy=None) as browser:
            await browser.send(json.dumps({"type": "authenticate", "ticket": second_ticket, "renderer_id": renderer, "protocol_version": 1}))
            while attached is None:
                message = await receive(browser)
                if message and message["type"] == "attached":
                    attached = message
            assert attached["resume_strategy"] == "resume_unconsumed"
            assert int(attached["writer_epoch"]) > int(previous_writer)
            assert attached["start_offset"] == str(len(output))
            text = "中文操作".encode() + b"\x03"
            await browser.send(encode_frame(TerminalFrame(FrameKind.INPUT, sid, attached["runtime_generation"],
                                                          int(attached["stream_epoch"]), int(attached["writer_epoch"]), 1, text)))
            while not output.endswith(text):
                await receive(browser)
            await browser.send(encode_frame(TerminalFrame(FrameKind.INPUT, sid, attached["runtime_generation"],
                                                          int(attached["stream_epoch"]), int(attached["writer_epoch"]), 2, b"exit\r")))
            exited = None
            while exited is None:
                message = await receive(browser)
                if message and message["type"] == "exit":
                    exited = message
            assert output.endswith(b"sentinel-final\r\n")
            assert exited["output_complete"] and int(exited["final_offset"]) == len(output)
        assert seen["passwords"] == (["synthetic-terminal-password"] if authentication == "password" else [])
        assert seen["responses"] == ([(otp,)] if authentication == "keyboard-interactive" else [])
        assert seen["input"].count(b"bulk\r") == 1
        assert hashlib.sha256(output[:len(bulk) + len(b"synthetic-ready\r\n")]).digest() == hashlib.sha256(b"synthetic-ready\r\n" + bulk).digest()
        metadata = json.dumps(store.all("SELECT * FROM vps_terminal_sessions"))
        audit = json.dumps(store.all("SELECT * FROM audit"))
        assert "synthetic-terminal-password" not in metadata + audit
        assert login["cookie"] not in metadata + audit
        assert otp not in metadata + audit
        assert "bulk-complete" not in metadata + audit
        assert not store.all("SELECT * FROM operations WHERE tool LIKE '%terminal%'")
        assert store.one("SELECT id_hash FROM sessions WHERE id_hash=?", (digest(login["cookie"]),))
        # A real panel logout revokes the old, still-open socket. Possessing
        # its old attachment/writer does not authorize another SSH write.
        created = await client.post("/api/vps-terminal/sessions", json={**request, "request_id": uuid.uuid4().hex})
        assert created.status_code == 200, created.text
        sid, renderer, attached = created.json()["id"], uuid.uuid4().hex, None
        output.clear()
        live_ticket = await ticket()
        async with websockets.connect(url, origin=base, additional_headers=headers, proxy=None) as browser:
            await browser.send(json.dumps({"type": "authenticate", "ticket": live_ticket, "renderer_id": renderer, "protocol_version": 1}))
            while attached is None:
                message = await receive(browser)
                if message and message["type"] == "attached":
                    attached = message
            logout = await client.post("/api/logout")
            assert logout.status_code == 200, logout.text
            forbidden = b"must-not-reach-SSH-after-logout\r"
            await browser.send(encode_frame(TerminalFrame(FrameKind.INPUT, sid, attached["runtime_generation"],
                                                          int(attached["stream_epoch"]), int(attached["writer_epoch"]), 1, forbidden)))
            async with asyncio.timeout(5):
                while True:
                    raw = await browser.recv()
                    if isinstance(raw, str) and json.loads(raw)["type"] == "error":
                        assert json.loads(raw)["reason_code"] == "LOGIN_REQUIRED"
                        break
            await until(lambda: runtime.ssh_terminal.sessions[sid].attachment is None)
            assert forbidden not in seen["input"]
        denied = await client.post(f"/api/vps-terminal/sessions/{sid}/attach-ticket", json={"renderer_id": renderer})
        assert denied.status_code == 401
        assert not store.one("SELECT id_hash FROM sessions WHERE id_hash=?", (digest(login["cookie"]),))
        assert seen["passwords"] == (["synthetic-terminal-password"] * 2 if authentication == "password" else [])
        assert seen["responses"] == ([(otp,)] * 2 if authentication == "keyboard-interactive" else [])
    finally:
        await client.aclose()
        agent.stop_event.set()
        if agent_task:
            agent_task.cancel()
            await asyncio.gather(agent_task, return_exceptions=True)
        await agent.ssh_terminal.close()
        await agent.integrations.close()
        await agent.computer.close()
        agent.journal.db.close()
        agent.instance_lock.close()
        server.should_exit = True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(hub_task, 10)
        peer.close()
        await peer.wait_closed()
        for task in tuple(producers):
            task.cancel()
        await asyncio.gather(*tuple(producers), return_exceptions=True)
