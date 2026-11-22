"""Real multiplexed SSH isolation and coalesced-close regression.

Sixteen synthetic loopback sessions exercise the production Hub and Agent.
Timing observations are not production performance claims. JSON evidence is
written below pytest's temporary directory unless CODEPIER_PARALLEL_EVIDENCE
explicitly selects another output file.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from pathlib import Path
import time
import traceback
import uuid

import asyncssh
import httpx
import pytest
import uvicorn
import websockets
from websockets.exceptions import ConnectionClosed

from tests.test_ssh_terminal_end_to_end import (
    Agent, AsyncSSHTransport, FrameKind, TerminalAwareWebSocketProtocol,
    TerminalFrame, atomic_json, bind_listener, create_app, decode_frame,
    encode_frame, seed_owner, token, until,
)


def exception_code(exc):
    return getattr(exc.rcvd, "code", None) if isinstance(exc, ConnectionClosed) else getattr(exc, "code", None)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.serial_regression
@pytest.mark.resource_regression
async def test_sixteen_real_sessions_keep_slow_consumer_isolated(tmp_path, monkeypatch):
    started = time.monotonic()
    evidence_setting = os.getenv("CODEPIER_PARALLEL_EVIDENCE")
    evidence_path = Path(evidence_setting) if evidence_setting else tmp_path / "parallel-evidence.json"
    count = 16
    # This override belongs to this fixture only. Production defaults stay 8/16.
    monkeypatch.setenv("CODEPIER_SSH_TERMINAL_MAX_SESSIONS_PER_USER", str(count))
    producers, ssh_connections, remote_channels = set(), set(), set()
    observed_commands = {}
    browser_sockets, session_ids = [], []
    results = {}
    protocol_refs = {}
    record = {"environment": "synthetic localhost TCP; real Hub, Agent and AsyncSSH",
              "production_benchmark": False, "sessions": count, "slow_consumers": 1,
              "fixture_max_sessions_per_user": count, "status": "running"}
    original_delivery = TerminalAwareWebSocketProtocol.send_receive_event_to_app

    def observed_delivery(protocol):
        if getattr(protocol, "scope", {}).get("path", "").startswith("/agent/vps-terminal/"):
            protocol_refs["agent"] = protocol
        if protocol.terminal_connection and not protocol.close_sent and protocol.queue.qsize() >= 16:
            record.setdefault("asgi_queue_at_or_above_16", []).append({
                "route": "agent" if getattr(protocol, "scope", {}).get("path", "").startswith("/agent/") else "browser",
                "count": protocol.queue.qsize(), "state": str(protocol.conn.state)})
        return original_delivery(protocol)

    monkeypatch.setattr(TerminalAwareWebSocketProtocol, "send_receive_event_to_app", observed_delivery)

    def bulk(index):
        marker = f"session-{index:02d}|".encode()
        size = 4 * 1024 * 1024 if index == 0 else 128 * 1024
        body = (marker * (size // len(marker) + 1))[:size]
        return body + f"\r\nbulk-done:{index:02d}\r\n".encode()

    class RemoteTerminal(asyncssh.SSHServerSession):
        def connection_made(self, channel):
            self.channel = channel
            self.writable = asyncio.Event()
            self.writable.set()
            self.closed = False
            self.pending = bytearray()
            remote_channels.add(self)

        def connection_lost(self, exc):
            self.closed = True
            self.writable.set()
            remote_channels.discard(self)

        def pty_requested(self, term, size, modes):
            return True

        def shell_requested(self):
            return True

        def session_started(self):
            self.channel.write(b"ready:parallel\r\n")

        def pause_writing(self):
            self.writable.clear()

        def resume_writing(self):
            self.writable.set()

        async def send_bulk(self, index):
            data = bulk(index)
            for offset in range(0, len(data), 16384):
                await self.writable.wait()
                if self.closed:
                    return
                self.channel.write(data[offset:offset + 16384])
                await asyncio.sleep(0)

        def data_received(self, data, datatype):
            self.pending.extend(data)
            while b"\r" in self.pending:
                command, _, remaining = self.pending.partition(b"\r")
                self.pending[:] = remaining
                action, value = command.decode("ascii").split(":")
                index = int(value)
                observed_commands.setdefault(index, []).append(action)
                if action == "run":
                    task = asyncio.create_task(self.send_bulk(index))
                    producers.add(task)
                    task.add_done_callback(producers.discard)
                elif action == "ping":
                    self.channel.write(f"pong:{index:02d}\r\n".encode())

    class SSHPeer(asyncssh.SSHServer):
        def connection_made(self, connection):
            ssh_connections.add(self)

        def connection_lost(self, exc):
            ssh_connections.discard(self)

        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == "fixture" and password == "synthetic-parallel-password"

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
                  (device_id, "Parallel fixture Agent", store.encrypt(secret), time.time(), "legacy", "owner"))
    store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created,space_id,owner_user_id) VALUES (?,?,?,?,?,'write',1,?,'legacy','owner')",
                  (project_id, "ParallelFixture", "parallelfixture", device_id, str(root), time.time()))
    with store.lock:
        login = auth.new_session("owner")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error",
                                         ws=TerminalAwareWebSocketProtocol, access_log=False,
                                         timeout_graceful_shutdown=3))
    hub_task = asyncio.create_task(server.serve(sockets=[listener]))
    config_path = tmp_path / "agent.json"
    atomic_json(config_path, {"device_id": device_id, "name": "Parallel fixture Agent", "secret": secret,
                              "hub_url": base, "state_dir": str(tmp_path / "agent-state"),
                              "allowed_roots": [{"path": str(root), "writable": True, "allow_tasks": True}],
                              "tasks": {}, "shell": {"enabled": True, "projects": ["*"]}})
    agent = Agent(config_path)
    agent.ssh_terminal.manager.connector = lambda: AsyncSSHTransport(
        agent.ssh_terminal.config, trust_sources=[tmp_path / "synthetic_known_hosts"])
    manager = agent.ssh_terminal.manager
    original_connect = agent.ssh_terminal._connect

    async def observed_connect(*args, **kwargs):
        record["terminal_channel_connect_attempts"] = record.get("terminal_channel_connect_attempts", 0) + 1
        return await original_connect(*args, **kwargs)

    agent.ssh_terminal._connect = observed_connect
    agent_task = None
    client = httpx.AsyncClient(base_url=base, trust_env=False, cookies={"rd_session": login["cookie"]},
                               headers={"Origin": base, "x-rd-csrf": login["csrf"], "x-codepier-space": "legacy"},
                               timeout=15)
    work_tasks = []
    try:
        assert manager.config.max_sessions_per_user == count
        assert runtime.ssh_terminal.config.max_sessions_per_user == count
        await until(lambda: server.started)
        agent_task = asyncio.create_task(agent.connect_once())
        await until(lambda: device_id in runtime.ssh_terminal.agents)
        control = runtime.ssh_terminal.agents[device_id]
        original_request = control.request

        async def traced_request(action, payload, project, **kwargs):
            try:
                return await original_request(action, payload, project, **kwargs)
            except Exception as exc:
                record.setdefault("control_errors", []).append({
                    "action": action, "session_id": payload.get("session_id"),
                    "code": exception_code(exc), "exception_type": type(exc).__name__})
                raise

        control.request = traced_request
        for owner, label in ((control, "hub_agent"), (agent.ssh_terminal, "agent_link")):
            for method in ("_receive", "_send"):
                original = getattr(owner, method)

                async def traced(*args, _original=original, _label=label + method, **kwargs):
                    try:
                        return await _original(*args, **kwargs)
                    except Exception as exc:
                        record.setdefault("transport_errors", []).append({
                            "side": _label, "phase": record.get("phase", "setup"), "code": exception_code(exc),
                            "exception_type": type(exc).__name__,
                            "frames": [f.name + ":" + str(f.lineno) for f in traceback.extract_tb(exc.__traceback__)[-8:]]})
                        raise

                setattr(owner, method, traced)
        def transport_done(task):
            if not task.cancelled() and (exc := task.exception()) is not None:
                record.setdefault("transport_errors", []).append({
                    "side": task.get_name(), "phase": record.get("phase", "setup"), "code": exception_code(exc),
                    "exception_type": type(exc).__name__,
                    "close_code": getattr(getattr(exc, "rcvd", None), "code", None),
                    "close_reason": getattr(getattr(exc, "rcvd", None), "reason", None),
                    "frames": [f.name + ":" + str(f.lineno) for f in traceback.extract_tb(exc.__traceback__)[-8:]]})
        for task in asyncio.all_tasks():
            if task.get_name() in {"terminal-agent-receive", "terminal-channel-receive",
                                    "terminal-agent-send", "terminal-channel-send"}:
                task.add_done_callback(transport_done)
        response = await client.post("/api/vps", json={"name": "Parallel fixture VPS", "host": "127.0.0.1",
                                                       "port": peer.get_port(), "username": "fixture",
                                                       "password": "synthetic-parallel-password", "host_key_policy": "accept-new",
                                                       "project_ids": [project_id]})
        assert response.status_code == 200, response.text
        vps_id = response.json()["id"]

        async def open_browser(index):
            response = await client.post("/api/vps-terminal/sessions", json={
                "vps_id": vps_id, "project_id": project_id, "request_id": uuid.uuid4().hex,
                "mode": "direct", "cols": 100, "rows": 30})
            assert response.status_code == 200, response.text
            sid = response.json()["id"]
            session_ids.append(sid)
            renderer = uuid.uuid4().hex
            response = await client.post(f"/api/vps-terminal/sessions/{sid}/attach-ticket", json={"renderer_id": renderer})
            assert response.status_code == 200, response.text
            socket = await websockets.connect(f"ws://127.0.0.1:{port}/ws/vps-terminal", origin=base,
                                               additional_headers={"Cookie": "rd_session=" + login["cookie"]},
                                               proxy=None, max_queue=4, close_timeout=0.5)
            browser_sockets.append(socket)
            await socket.send(json.dumps({"type": "authenticate", "ticket": response.json()["ticket"],
                                          "renderer_id": renderer, "protocol_version": 1}))
            while True:
                message = json.loads(await asyncio.wait_for(socket.recv(), 15))
                assert message["type"] != "error", message
                if message["type"] == "attached":
                    return {"index": index, "sid": sid, "socket": socket, "attached": message}

        attach_started = time.monotonic()
        browsers = await asyncio.gather(*(open_browser(index) for index in range(count)))
        record["attach_all_ms"] = round((time.monotonic() - attach_started) * 1000, 3)
        assert len(manager.sessions) == len(ssh_connections) == len(remote_channels) == count

        async def send_input(browser, sequence, command):
            attached = browser["attached"]
            await browser["socket"].send(encode_frame(TerminalFrame(
                FrameKind.INPUT, browser["sid"], attached["runtime_generation"],
                int(attached["stream_epoch"]), int(attached["writer_epoch"]), sequence, command)))

        # The slow browser stops consuming immediately after the attached control
        # event and never ACKs. Wait for actual SSH read pressure before peers run.
        slow = browsers[0]
        await send_input(slow, 1, b"run:00\r")
        await until(lambda: manager.sessions[slow["sid"]].output_paused_at is not None, seconds=10)
        slow_session = manager.sessions[slow["sid"]]
        record["slow_ring_bytes"] = slow_session.ring.size
        record["slow_unacknowledged_bytes"] = slow_session.credit.in_flight
        assert slow_session.credit.acked_offset == 0
        assert slow_session.ring.size <= manager.config.ring_bytes

        async def finish_fast(browser):
            began, output, acknowledgements = time.monotonic(), bytearray(), set()
            index, socket, attached = browser["index"], browser["socket"], browser["attached"]
            await send_input(browser, 1, f"run:{index:02d}\r".encode())

            async def consume():
                raw = await asyncio.wait_for(socket.recv(), 8)
                if isinstance(raw, bytes):
                    frame = decode_frame(raw)
                    assert frame.kind == FrameKind.OUTPUT and frame.session_id == browser["sid"]
                    assert frame.runtime_generation == attached["runtime_generation"]
                    assert frame.sequence == len(output)
                    output.extend(frame.payload)
                    await socket.send(json.dumps({"type": "output_ack", "attachment_id": attached["attachment_id"],
                                                   "runtime_generation": attached["runtime_generation"],
                                                   "render_epoch": attached["render_epoch"], "stream_epoch": attached["stream_epoch"],
                                                   "rendered_offset": str(len(output))}))
                else:
                    message = json.loads(raw)
                    assert message["type"] != "error", message
                    if message["type"] == "input_ack":
                        acknowledgements.add(message["input_seq"])

            async with asyncio.timeout(15):
                while not output.endswith(f"bulk-done:{index:02d}\r\n".encode()):
                    await consume()
                await send_input(browser, 2, f"ping:{index:02d}\r".encode())
                while not output.endswith(f"pong:{index:02d}\r\n".encode()) or len(acknowledgements) < 2:
                    await consume()
            expected = b"ready:parallel\r\n" + bulk(index) + f"pong:{index:02d}\r\n".encode()
            assert bytes(output) == expected  # Full stream identity, order and no cross-session bytes.
            assert observed_commands[index] == ["run", "ping"]
            results[index] = {"index": index, "bytes": len(output),
                              "sha256": hashlib.sha256(output).hexdigest(),
                              "elapsed_ms": round((time.monotonic() - began) * 1000, 3)}

        fast_started = time.monotonic()
        record["phase"] = "output"
        work_tasks = [asyncio.create_task(finish_fast(browser)) for browser in browsers[1:]]
        probe_started = time.monotonic()
        diagnostic = await client.get(f"/api/vps-terminal/sessions/{slow['sid']}/diagnostics")
        record["diagnostic_http_ms"] = round((time.monotonic() - probe_started) * 1000, 3)
        assert diagnostic.status_code == 200, diagnostic.text
        diagnostic = diagnostic.json()
        assert diagnostic["links"]["agent"]["connected"]
        assert diagnostic["links"]["ssh"]["connected"]
        record["agent_control_rtt_ms"] = diagnostic["links"]["agent"]["control_rtt_ms"]
        await asyncio.gather(*work_tasks)
        record["fast_group_ms"] = round((time.monotonic() - fast_started) * 1000, 3)
        assert len(results) == 15
        assert slow_session.output_paused_at is not None and slow_session.credit.acked_offset == 0
        assert slow_session.backpressure_count >= 1
        assert len(ssh_connections) == count
        record["fast_outputs"] = [results[index] for index in sorted(results)]
        record["fast_output_bytes_total"] = sum(item["bytes"] for item in results.values())
        record["slow_still_unacknowledged_after_peers_finished"] = True

        close_started = time.monotonic()
        record["phase"] = "close"
        # Coalesce a legitimate multiplexed close burst as a real network can:
        # pause only the Hub Agent socket's incoming TCP reads, allow all 16
        # actual Agent closes to finish, then release the accumulated replies.
        # No protocol limit, production behavior or message is replaced here.
        multiplexed_transport = protocol_refs["agent"].transport
        multiplexed_transport.pause_reading()
        pending_closes = asyncio.gather(*(client.post(f"/api/vps-terminal/sessions/{sid}/close",
                                                      json={"request_id": uuid.uuid4().hex}) for sid in session_ids))
        try:
            await until(lambda: all(s.stopped.is_set() and s.transport is None for s in manager.sessions.values()))
            await until(lambda: not agent.ssh_terminal.tasks)
        finally:
            multiplexed_transport.resume_reading()
        record["close_burst_coalesced"] = True
        responses = await pending_closes
        record["close_responses"] = [{"id": sid, "http_status": response.status_code,
                                      "state": response.json().get("state"),
                                      "reason_code": response.json().get("reason_code")}
                                     for sid, response in zip(session_ids, responses, strict=True)]
        record["agent_after_close"] = [{"id": sid, "state": session.state, "reason": session.reason,
                                        "stopped": session.stopped.is_set(), "transport_none": session.transport is None}
                                       for sid, session in manager.sessions.items()]
        assert all(response.status_code == 200 and response.json()["state"] == "exited" for response in responses), [r.text for r in responses]
        await until(lambda: all(s.stopped.is_set() and s.transport is None for s in manager.sessions.values()))
        await until(lambda: not ssh_connections and not remote_channels and not producers)
        await asyncio.gather(*(socket.close() for socket in browser_sockets))
        await until(lambda: all(s.attachment is None for s in runtime.ssh_terminal.sessions.values()))
        record["close_all_ms"] = round((time.monotonic() - close_started) * 1000, 3)
        record["residuals"] = {"active_agent_sessions": sum(not s.stopped.is_set() for s in manager.sessions.values()),
                               "ssh_connections": len(ssh_connections), "remote_channels": len(remote_channels),
                               "producer_tasks": len(producers), "browser_attachments": sum(s.attachment is not None for s in runtime.ssh_terminal.sessions.values())}
        assert not any(record["residuals"].values())
        record["status"] = "passed"
    finally:
        record["phase"] = "teardown"
        for task in work_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*work_tasks, return_exceptions=True)
        await asyncio.gather(*(socket.close() for socket in browser_sockets), return_exceptions=True)
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
        record["hub_task_finished"] = hub_task.done()
        if record["status"] != "passed" or not record["hub_task_finished"]:
            record["status"] = "failed"
        record["total_elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    assert record["hub_task_finished"] and record["status"] == "passed"
