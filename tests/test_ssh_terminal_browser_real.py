"""Real Chromium → Hub → Agent → SSH acceptance, synthetic loopback identity only.

Run this asynchronous case alone from a local checkout with the project's
development dependencies and matching Playwright Chromium already installed:

  PYTHONPATH="$PWD" CODEPIER_BROWSER_EVIDENCE_DIR="$PWD/.work/browser-evidence" \
    .venv/bin/python -m pytest --import-mode=importlib -q -s \
    tests/test_ssh_terminal_browser_real.py

Use the checkout as cwd and PYTHONPATH so Hub static assets and Agent build
fingerprints resolve to that same source tree. Prefer a local disk: synchronous
runtime fingerprint scans on a network volume can consume the real handshake
deadline. This case retains production deadlines and doesn't use the synchronous
--chat-browser-reuse option. Temporary data and credentials are isolated; named
screenshots are written to CODEPIER_BROWSER_EVIDENCE_DIR, or tmp_path by default.

No HTTP/WS interception, browser transport replacement, remote VPS, or user SSH files.
The SSH peer intentionally implements a synthetic shell; real tmux/OS shell lifecycle
is separately covered by tests/test_ssh_terminal_tmux_real.py. The browser uses
the actual login, VPS card/project picker, xterm renderer, and logout flow.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
import uuid
from pathlib import Path

import asyncssh
import httpx
import pytest
import uvicorn
from playwright.async_api import async_playwright, expect

from agent.runner import Agent
from agent.ssh_terminal_transport import AsyncSSHTransport
from hub.app import create_app
from hub.ssh_terminal.server_protocol import TerminalAwareWebSocketProtocol
from shared.crypto import password_hash, token
from shared.audit_redaction import redact_text
from shared.util import atomic_json
from tests.browser_launch import launch_options
from tests.fixture_hub import bind_listener
from tests.legacy_iam_fixture import seed_owner


async def until(predicate, seconds=15):
    async with asyncio.timeout(seconds):
        while not predicate():  # noqa: ASYNC110 -- bounded fixture observation
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.browser
@pytest.mark.slow
@pytest.mark.serial_regression
async def test_real_panel_terminal_through_real_agent_and_ssh(tmp_path, monkeypatch):
    seen = {"input": bytearray(), "commands": [], "sizes": [], "shells": 0, "probes": 0}
    forwarded_acks = []
    browser_terminal_sockets = []
    browser_socket_closed = asyncio.Event()
    ssh_password = "synthetic-ssh-" + uuid.uuid4().hex
    panel_password = "Synthetic-Panel-" + uuid.uuid4().hex

    class RemoteTerminal(asyncssh.SSHServerSession):
        def connection_made(self, channel):
            self.channel, self.command, self.pending = channel, None, bytearray()

        def exec_requested(self, command):
            self.command = command
            return True

        def pty_requested(self, term, size, modes):
            assert term == "xterm-256color"
            seen["sizes"].append(size[:2])
            return True

        def terminal_size_changed(self, cols, rows, *rest):
            seen["sizes"].append((cols, rows))

        def shell_requested(self):
            return True

        def session_started(self):
            if self.command is not None:
                # UI's actual auto mode probes persistence; this fixture has no tmux.
                if self.command == "if command -v tmux >/dev/null 2>&1; then tmux -V; else exit 127; fi":
                    seen["probes"] += 1
                    self.channel.exit(127)
                else:
                    self.channel.exit(126)
                return
            seen["shells"] += 1
            self.channel.write(b"SYNTHETIC_SSH_READY\r\nfixture$ ")

        def data_received(self, data, datatype):
            seen["input"].extend(data)
            for value in data:
                if value in (10, 13):
                    if self.pending:
                        command = self.pending.decode("utf-8", "strict")
                        seen["commands"].append(command)
                        self.pending.clear()
                        self.channel.write(b"\r\nREPLY_" + command.encode() + b"\r\nfixture$ ")
                else:
                    self.pending.append(value)
                    self.channel.write(bytes([value]))

    class SSHPeer(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == "fixture" and password == ssh_password

        def session_requested(self):
            return RemoteTerminal()

    peer = await asyncssh.create_server(
        SSHPeer, "127.0.0.1", 0,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        encoding=None, line_editor=False,
    )
    listener = bind_listener()
    port = listener.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    monkeypatch.setenv("HUB_PUBLIC_URL", base)
    monkeypatch.setenv("HUB_OIDC_ENABLED", "0")
    monkeypatch.setenv("CODEPIER_SSH_TERMINAL_ENABLED", "1")
    app = create_app(str(tmp_path / "hub"))
    store, auth, runtime = app.state.store, app.state.auth, app.state.runtime
    seed_owner(store)
    store.execute("UPDATE users SET password_hash=? WHERE id='owner'", (password_hash(panel_password),))
    device_id, secret = uuid.uuid4().hex, token()
    store.execute(
        "INSERT INTO devices(id,name,secret,created,space_id,owner_user_id) VALUES (?,?,?,?,?,?)",
        (device_id, "Synthetic browser SSH Agent", store.encrypt(secret), time.time(), "legacy", "owner"),
    )
    project_ids, allowed_roots = [], []
    for index in range(2):
        project_id = uuid.uuid4().hex
        project_root = tmp_path / f"project-{index}"
        project_root.mkdir()
        project_ids.append(project_id)
        allowed_roots.append({"path": str(project_root), "writable": True, "allow_tasks": True})
        store.execute(
            "INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created,space_id,owner_user_id) "
            "VALUES (?,?,?,?,?,'write',1,?,'legacy','owner')",
            (project_id, f"BrowserFixture{index}", f"browserfixture{index}", device_id, str(project_root), time.time()),
        )
    with store.lock:
        setup_login = auth.new_session("owner")
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="error",
        ws=TerminalAwareWebSocketProtocol, access_log=False, timeout_graceful_shutdown=3,
    ))
    hub_task = asyncio.create_task(server.serve(sockets=[listener]))
    config_path = tmp_path / "agent.json"
    atomic_json(config_path, {
        "device_id": device_id, "name": "Synthetic browser SSH Agent", "secret": secret,
        "hub_url": base, "state_dir": str(tmp_path / "agent-state"),
        "allowed_roots": allowed_roots, "tasks": {}, "shell": {"enabled": True, "projects": ["*"]},
    })
    agent = Agent(config_path)
    agent.ssh_terminal.manager.connector = lambda: AsyncSSHTransport(
        agent.ssh_terminal.config, trust_sources=[tmp_path / "synthetic_known_hosts"],
    )
    # Observe real errors before the production reconnect loop deliberately
    # suppresses them. Calls, retries, deadlines, and transport are unchanged.
    terminal_connect_errors = []
    original_terminal_connect = agent.ssh_terminal._connect

    def safe_error(error):
        message = str(error)
        for private in (ssh_password, panel_password, secret, setup_login["cookie"],
                        setup_login["csrf"], str(tmp_path), str(config_path)):
            message = message.replace(private, "<redacted>")
        code = getattr(error, "code", None)
        return {"type": type(error).__name__,
                "code": code if type(code) is int or type(code) is str and re.fullmatch(r"[A-Z0-9_]{1,64}", code) else None,
                "message": redact_text(message)[:400]}

    async def observed_terminal_connect(config, main_socket):
        try:
            return await original_terminal_connect(config, main_socket)
        except Exception as error:
            terminal_connect_errors.append(safe_error(error))
            del terminal_connect_errors[:-4]
            raise

    agent.ssh_terminal._connect = observed_terminal_connect
    client = httpx.AsyncClient(
        base_url=base, trust_env=False, cookies={"rd_session": setup_login["cookie"]},
        headers={"Origin": base, "x-rd-csrf": setup_login["csrf"], "x-codepier-space": "legacy"},
    )
    agent_task = browser = playwright = page = None
    evidence = Path(os.environ.get("CODEPIER_BROWSER_EVIDENCE_DIR", str(tmp_path / "browser-evidence")))
    await asyncio.to_thread(evidence.mkdir, parents=True, exist_ok=True)
    try:
        await until(lambda: server.started)
        agent_task = asyncio.create_task(agent.connect_once())
        ready_started = time.monotonic()

        def agent_snapshot(reason):
            tasks = []
            for task in asyncio.all_tasks():
                name = getattr(task.get_coro(), "__qualname__", "")
                if name in {"Agent.connect_once", "Agent.heartbeat", "AgentTerminalLink.run"}:
                    frames = task.get_stack(limit=1)
                    tasks.append({"coroutine": name, "done": task.done(),
                                  "line": frames[-1].f_lineno if frames else None})
            return {"reason": reason, "wait_elapsed_seconds": round(time.monotonic() - ready_started, 3),
                    "agent_task_done": agent_task.done(), "agent_task_cancelled": agent_task.cancelled(),
                    "main_socket_present": agent.socket is not None,
                    "terminal_socket_present": agent.ssh_terminal.socket is not None,
                    "hub_main_registered": device_id in runtime.connections,
                    "hub_terminal_registered": device_id in runtime.ssh_terminal.agents,
                    "connection_number": agent.connection_number,
                    "runtime_generation": agent.ssh_terminal.manager.generation,
                    "terminal_connect_errors": terminal_connect_errors, "pending_tasks": tasks[:6]}

        try:
            async with asyncio.timeout(15):
                while device_id not in runtime.ssh_terminal.agents:  # noqa: ASYNC110 -- bounded readiness observation
                    if agent_task.done():
                        snapshot = agent_snapshot("AGENT_TASK_FINISHED_BEFORE_TERMINAL_READY")
                        if not agent_task.cancelled() and (error := agent_task.exception()) is not None:
                            snapshot["agent_error"] = safe_error(error)
                        raise AssertionError(json.dumps(snapshot, ensure_ascii=False)) from None
                    await asyncio.sleep(0.02)
        except TimeoutError:
            snapshot = agent_snapshot("AGENT_TERMINAL_READY_TIMEOUT")
            if agent_task.done() and not agent_task.cancelled() and (error := agent_task.exception()) is not None:
                snapshot["agent_error"] = safe_error(error)
            raise AssertionError(json.dumps(snapshot, ensure_ascii=False)) from None
        created = await client.post("/api/vps", json={
            "name": "Synthetic real-chain VPS", "host": "127.0.0.1", "port": peer.get_port(),
            "username": "fixture", "password": ssh_password, "host_key_policy": "accept-new",
            "project_ids": project_ids,
        })
        assert created.status_code == 200, created.text
        vps_id = created.json()["id"]
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(**launch_options("chromium", {"headless": True}))
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})
        page = await context.new_page()
        page.set_default_timeout(20_000)

        def observe_socket(socket):
            if not socket.url.endswith("/ws/vps-terminal"):
                return
            browser_terminal_sockets.append(socket)
            socket.on("close", lambda *_: browser_socket_closed.set())

            def observe_frame(payload):
                if not isinstance(payload, str):
                    return
                message = json.loads(payload)
                if message.get("type") == "input_ack" and message.get("status") == "forwarded":
                    # Only keep safe sequence metadata, never authentication frames or tickets.
                    forwarded_acks.append(message.get("sequence"))

            socket.on("framereceived", observe_frame)

        page.on("websocket", observe_socket)
        await page.goto(base + "/#vps", wait_until="domcontentloaded")
        await expect(page.locator("#login-form")).to_be_visible()
        await page.locator("#username").fill("owner")
        await page.locator("#password").fill(panel_password)
        await page.locator("#login-form button[type=submit]").click()
        await expect(page.locator(".shell")).to_be_visible()
        await page.locator('.sidebar [data-nav="vps"]').click()
        card = page.locator(f'[data-vps-card="{vps_id}"]')
        await expect(card).to_be_visible()
        await card.locator('[data-vps-action="terminal"]').click()
        project_picker = card.get_by_label("连接项目")
        await expect(project_picker).to_be_visible()
        await project_picker.select_option(project_ids[1])
        await card.get_by_role("button", name="连接", exact=True).click()
        await expect(page.locator(".vt-state")).to_have_text("已连接")
        await until(lambda: seen["shells"] == 1)
        assert seen["probes"] == 1
        assert len(browser_terminal_sockets) == 1
        sid = next(iter(runtime.ssh_terminal.sessions))
        assert runtime.ssh_terminal.sessions[sid].row["project_id"] == project_ids[1]
        # Enable the real accessibility renderer through the actual settings UI.
        await page.locator(".vt-settings summary").click()
        await page.get_by_label("屏幕阅读器模式").check()
        await page.locator(".vt-settings summary").click()
        await expect(page.locator(".xterm-accessibility")).to_contain_text("SYNTHETIC_SSH_READY")
        probe = "probe_" + uuid.uuid4().hex[:16]
        await page.locator(".xterm-helper-textarea").focus()
        await page.keyboard.type(probe)
        await page.keyboard.press("Enter")
        await expect(page.locator(".xterm-accessibility")).to_contain_text("REPLY_" + probe)
        await until(lambda: bool(forwarded_acks))
        assert seen["commands"].count(probe) == 1
        assert bytes(seen["input"]).endswith(probe.encode() + b"\r")
        await page.screenshot(path=str(evidence / "real-chain-terminal.png"), full_page=True)
        await page.evaluate("window.acceptanceTerminalNode = document.querySelector('.xterm')")
        terminal_hash = await page.evaluate("location.hash")
        previous_size = seen["sizes"][-1]
        await page.set_viewport_size({"width": 1100, "height": 800})
        await until(lambda: seen["sizes"][-1] != previous_size)
        assert all(cols > 0 and rows > 0 for cols, rows in seen["sizes"])
        await page.locator('.sidebar [data-nav="vps"]').click()
        await expect(page.locator(f'[data-vps-card="{vps_id}"]')).to_be_visible()
        await page.evaluate("hash => { location.hash = hash; }", terminal_hash)
        await expect(page.locator(".vt-state")).to_have_text("已连接")
        assert await page.evaluate("window.acceptanceTerminalNode === document.querySelector('.xterm')")
        second_probe = "retained_" + uuid.uuid4().hex[:12]
        await page.locator(".xterm-helper-textarea").focus()
        await page.keyboard.type(second_probe)
        await page.keyboard.press("Enter")
        await expect(page.locator(".xterm-accessibility")).to_contain_text("REPLY_" + second_probe)
        assert seen["commands"].count(second_probe) == 1
        assert seen["shells"] == 1
        old_cookie = next(item["value"] for item in await context.cookies() if item["name"] == "rd_session")
        await page.locator('[data-action="logout"]').click()
        await expect(page.locator("#login-form")).to_be_visible()
        await asyncio.wait_for(browser_socket_closed.wait(), 10)
        await until(lambda: runtime.ssh_terminal.sessions[sid].attachment is None)
        assert await page.evaluate("!window.acceptanceTerminalNode.isConnected")
        before_logout_probe = bytes(seen["input"])
        # A physical keyboard after logout has no terminal receiver.
        await page.keyboard.type("blocked_after_logout_" + uuid.uuid4().hex[:8])
        await page.keyboard.press("Enter")
        async with httpx.AsyncClient(base_url=base, trust_env=False, cookies={"rd_session": old_cookie}) as stale:
            denied = await stale.get(f"/api/vps-terminal/sessions/{sid}")
        assert denied.status_code == 401
        assert bytes(seen["input"]) == before_logout_probe
        await page.screenshot(path=str(evidence / "real-chain-logged-out.png"), full_page=True)
        print(json.dumps({"real_browser_chain": "passed", "engine": "chromium", "ssh_shells": seen["shells"],
                          "forwarded_input_acks": len(forwarded_acks), "resize_events": len(seen["sizes"]),
                          "retained_terminal_dom": True, "logout_socket_closed": True, "old_cookie_status": 401}))
    except BaseException:
        if page is not None and not page.is_closed():
            with contextlib.suppress(Exception):
                await page.screenshot(path=str(evidence / "real-chain-failure.png"), full_page=True)
        raise
    finally:
        if browser:
            await browser.close()
        if playwright:
            await playwright.stop()
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
