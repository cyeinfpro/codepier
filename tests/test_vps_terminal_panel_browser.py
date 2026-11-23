"""Real panel routes and real xterm with browser-local terminal HTTP/WS fixtures.

These tests exercise the app's lazy loader, inventory, route lifecycle and DOM.
They do not open a real SSH connection; the transport/SSH integration suite owns
that boundary. No credentials or terminal input are persisted by this fixture.
"""

import os
import re
from pathlib import Path
import uuid
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from shared.ssh_terminal_protocol import FrameKind, TerminalFrame, encode_frame
from tests.browser_launch import launch_browser
from tests.browser_support import login_panel
from tests.screenshot_support import capture_painted_page


WIRE = r"""(() => {
  const OriginalWebSocket = window.WebSocket;
  window.terminalFixtureSockets = [];
  window.terminalFixtureErrors = [];
  const fail = message => { terminalFixtureErrors.push(message); throw new Error(message); };
  class TerminalSocket {
    constructor(url) {
      if (new URL(url).pathname !== '/ws/vps-terminal') return new OriginalWebSocket(url);
      this.readyState = 0; this.bufferedAmount = 0; this.controls = [];
      terminalFixtureSockets.push(this);
      setTimeout(() => { this.readyState = 1; this.onopen?.(); }, 0);
    }
    send(data) {
      if (typeof data !== 'string') fail('Unexpected input in route-only fixture');
      const message = JSON.parse(data);
      this.controls.push(message);
      if (message.type === 'authenticate') {
        this.id = message.ticket.slice(7);
        if (message.ticket.startsWith('gap----')) {
          this.emit({type:'error', reason_code:'HISTORY_GAP', recoverability:'none'});
          return;
        }
        const liveTail = message.ticket.startsWith('live---');
        this.emit({type:'attached', session_id:this.id,
          runtime_generation:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          attachment_id:this.id, render_epoch:'1', stream_epoch:'1', writer_epoch:'1',
          start_offset:liveTail ? '555' : '0', output_end_offset:liveTail ? '555' : '0',
          next_input_seq:'1', cols:100, rows:30, mode:liveTail ? 'direct' : 'tmux',
          resume_strategy:'live', state:'ready',
          ...(liveTail ? {history_recovery:'live_tail'} : {})});
      }
      if (message.type === 'ping') this.emit({type:'pong', nonce:message.nonce});
      if (!['authenticate', 'ping', 'pong', 'output_ack', 'resize', 'detach'].includes(message.type))
        fail('Unexpected terminal control: ' + message.type);
    }
    emit(message) { queueMicrotask(() => this.onmessage?.({data:JSON.stringify(message)})); }
    close(code = 1000) { this.readyState = 3; this.onclose?.({code}); }
  }
  window.WebSocket = TerminalSocket;
})();"""


def capture_evidence(page, name):
    directory = os.environ.get("CODEPIER_BROWSER_EVIDENCE_DIR")
    if directory:
        output = Path(directory)
        output.mkdir(parents=True, exist_ok=True)
        capture_painted_page(page, output / name)


def assert_mobile_keys_are_unobscured(page):
    # scrollWidth alone cannot detect a fixed app dock covering the entire row.
    # Check the real hit target before Playwright can scroll a button into view.
    page.wait_for_function("""() => {
      const key = document.querySelector('.vt-primary-slot .vt-mobile-keys button');
      const dock = document.querySelector('.mobile-dock');
      if (!key || !dock) return false;
      const box = key.getBoundingClientRect(), navigation = dock.getBoundingClientRect();
      const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
      return box.width > 0 && box.height > 0 && key.contains(hit) &&
        box.bottom <= navigation.top - 6 && box.bottom <= innerHeight;
    }""")


class PanelFixture:
    def __init__(self, page, stack, multiple=False, other_vps=False):
        self.sessions = {}
        self.creates = []
        self.closes = []
        self.close_state = "closed"
        self.diagnostics_requests = []
        self.capability = {"available": True, "protocol_version": 1, "limits": {}, "saved_vps_host_verification": "skip"}
        self.projects = [
            {**project, "mode": "write", "allow_tasks": True, "online": True,
             "device_name": project.get("device_name", "Fixture Agent")}
            for project in stack.projects[:2 if multiple else 1]
        ]
        self.vps = {
            "saved_vps_host_verification": "skip",
            "id": uuid.uuid4().hex, "name": "终端路由测试", "host": "fixture.invalid",
            "port": 22, "username": "fixture", "enabled": True,
            "host_key_policy": "strict", "provider": "", "region": "", "system": "Linux",
            "notes": "", "project_ids": [project["id"] for project in self.projects],
            "projects": self.projects,
        }
        self.servers = {self.vps["id"]: self.vps}
        if other_vps:
            other = {
                **self.vps, "id": uuid.uuid4().hex,
                "name": "另一台测试 VPS", "host": "other.fixture.invalid",
            }
            self.servers[other["id"]] = other
        page.add_init_script(WIRE)
        page.route("**/api/vps**", self.route)

    def route(self, route):
        path = urlsplit(route.request.url).path
        method = route.request.method
        if path == "/api/vps":
            route.fulfill(json={"vps": list(self.servers.values()), "next_offset": None})
        elif path.startswith("/api/vps/"):
            route.fulfill(json=self.servers[path.split("/")[-1]])
        elif path == "/api/vps-terminal/capabilities":
            route.fulfill(json=self.capability)
        elif path == "/api/vps-terminal/sessions" and method == "POST":
            body = route.request.post_data_json or {}
            self.creates.append(body)
            identifier = uuid.uuid4().hex
            target = self.servers[body["vps_id"]]
            project_id = body.get("project_id")
            project = (
                next((item for item in self.projects if item["id"] == project_id), None)
                if project_id
                else (self.projects[0] if self.projects else None)
            )
            session = {
                "session_id": identifier,
                "vps_id": target["id"],
                "project_id": project["id"] if project else "",
                "vps_name": target["name"],
                "host": target["host"],
                "port": target["port"],
                "project_alias": project["alias"] if project else "",
                "device_name": project["device_name"] if project else "Fixture Agent",
                "username": target["username"],
                "mode": "tmux",
                "direct_vps": not bool(project_id),
                "state": "ready",
            }
            self.sessions[identifier] = session
            route.fulfill(json=session)
        elif path == "/api/vps-terminal/sessions":
            route.fulfill(json={"sessions": list(self.sessions.values())})
        elif path.endswith("/close") and method == "POST":
            identifier = path.split("/")[4]
            assert identifier in self.sessions
            self.closes.append((identifier, route.request.post_data_json["request_id"]))
            result = {**self.sessions[identifier], "state": self.close_state}
            if self.close_state == "closed":
                self.sessions[identifier] = result
            route.fulfill(json=result)
        elif path.endswith("/attach-ticket"):
            identifier = path.split("/")[4]
            route.fulfill(json={"ticket": "ticket-" + identifier,
                                "ws_path": "/ws/vps-terminal", "expires_in": 20})
        elif path.endswith("/diagnostics") and method == "GET":
            identifier = path.split("/")[4]
            assert identifier in self.sessions
            self.diagnostics_requests.append(identifier)
            route.fulfill(json={
                "session_id": identifier, "protocol_version": 1, "collected_at": 1805800000,
                "state": "ready", "mode": "tmux", "reason_code": None,
                "links": {"browser": {"connected": True, "status": "connected"},
                          "agent": {"connected": True, "status": "connected", "control_rtt_ms": 0},
                          "ssh": {"connected": True, "status": "connected"}},
                "bytes": {"input": "0", "output": "0"},
                "stages_ms": {"ssh_connect": 42, "pty_open": 1},
                "lifecycle": {"recover_count": 0, "version": {"hub": "fixture", "agent": "fixture", "protocol": 1}},
                "metrics": {"counters": {"reconnect_total": 0}, "gauges": {"browser_buffer_bytes": 0}},
            })
        elif path.startswith("/api/vps-terminal/sessions/") and method == "GET":
            route.fulfill(json=self.sessions[path.split("/")[4]])
        else:
            # A fixture must never fall through to real saved connection actions.
            route.abort()
            raise AssertionError("Unexpected terminal request: " + method + " " + path)


def detach_all_tabs(page):
    while page.locator(".vt-tab-select").count():
        tabs = page.locator(".vt-tab-select")
        size = tabs.count()
        tabs.last.click()
        page.locator(".vt-toolbar details summary").click()
        page.get_by_role("button", name="分离标签", exact=True).click()
        expect(tabs).to_have_count(size - 1)


@pytest.mark.parametrize("width", [1440, 390])
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_terminal_route_preserves_dom_and_closed_tabs_do_not_reopen(stack, width, engine):
    with sync_playwright() as pw:
        browser = launch_browser(getattr(pw, engine))
        page = browser.new_page(viewport={"width": width, "height": 960})
        fixture = PanelFixture(page, stack)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            login_panel(page, stack, "vps")
            expect(page.locator('[data-vps-action="terminal"]')).to_be_enabled()
            assert page.evaluate("typeof window.CodePierVpsTerminal") == "undefined"
            page.locator('[data-vps-action="terminal"]').click()
            expect(page.locator(".vt-state")).to_have_text("已连接")
            assert len(fixture.creates) == 1
            route = page.evaluate("location.hash")
            assert route.startswith("#vps?view=terminal&session=")
            page.evaluate("window.firstTerminalNode = document.querySelector('.xterm')")
            page.evaluate("renderPage(false)")
            assert page.evaluate("document.querySelector('.xterm') === firstTerminalNode")
            if width > 700:
                page.get_by_role("button", name="分屏", exact=True).click()
                expect(page.locator(".vt-slot .xterm")).to_have_count(2)
                expect(page.locator(".vt-state").last).to_have_text("已连接")
                page.evaluate("renderPage(false)")
                expect(page.locator(".vt-slot .xterm")).to_have_count(2)
                route = page.evaluate("location.hash")
            # Browser hash navigation changes only the query of the same page.
            page.evaluate("location.hash = 'vps'")
            expect(page.locator("#vps-query")).to_be_visible()
            assert page.evaluate("firstTerminalNode.isConnected")
            page.evaluate("value => location.hash = value", route)
            expect(page.locator(".vt-workspace")).to_be_visible()
            assert page.evaluate("document.querySelector('.xterm') === firstTerminalNode")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            assert page.evaluate("""() => {
              const canvas = document.querySelector('.vt-primary-slot .vt-canvas');
              const viewport = canvas.querySelector('.xterm-viewport');
              const terminalStyle = getComputedStyle(canvas.querySelector('.xterm'));
              return getComputedStyle(viewport).backgroundColor === getComputedStyle(canvas).backgroundColor &&
                terminalStyle.borderLeftWidth === '0px' && terminalStyle.borderTopLeftRadius === '0px';
            }""")
            if width <= 700:
                assert_mobile_keys_are_unobscured(page)
            capture_evidence(page, f"terminal-panel-{engine}-{width}.png")
            page.get_by_role("button", name="诊断", exact=True).click()
            diagnostic = page.get_by_role("region", name="当前会话连接诊断", exact=True)
            expect(diagnostic.locator(".vt-diagnostics-link strong")).to_have_text(["已连接"] * 3)
            expect(diagnostic.locator(".vt-diagnostics-content > .vt-diagnostics-values dd").last).to_have_text("0 ms")
            assert fixture.diagnostics_requests == [route.split("session=")[1]]
            assert page.evaluate("document.querySelector('.xterm') === firstTerminalNode")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            if width <= 700:
                assert_mobile_keys_are_unobscured(page)
            capture_evidence(page, f"terminal-diagnostics-{engine}-{width}.png")
            diagnostic.get_by_role("button", name="收起", exact=True).click()
            expect(diagnostic).not_to_be_visible()
            # On desktop the first tab is not the focused half of the split.
            identifier = next(iter(fixture.sessions))
            tab = page.locator(f'.vt-tab[data-session-id="{identifier}"]')
            lock = tab.locator(".vt-tab-lock")
            close = tab.locator(".vt-tab-close")
            lock.click()
            expect(lock).to_have_attribute("aria-pressed", "true")
            expect(close).to_be_disabled()
            tab.dispatch_event("auxclick", {"button": 1})
            dialog = page.get_by_role("alertdialog")
            expect(dialog).not_to_be_visible()
            assert not fixture.closes
            lock.click()
            expect(close).to_be_enabled()
            close.click()
            expect(dialog).to_contain_text(fixture.vps["name"])
            expect(dialog.get_by_role("button", name="取消")).to_be_focused()
            page.keyboard.press("Escape")
            expect(dialog).not_to_be_visible()
            expect(close).to_be_focused()
            assert not fixture.closes

            close.click()
            fixture.close_state = "close_unknown"
            with page.expect_response(lambda response: response.url.endswith(f"/{identifier}/close")):
                dialog.get_by_role("button", name="结束会话").click()
            expect(tab).to_have_attribute("data-state", "close_unknown")
            assert len(fixture.closes) == 1
            assert fixture.closes[0][0] == identifier
            fixture.close_state = "closed"
            close.click()
            with page.expect_response(lambda response: response.url.endswith(f"/{identifier}/close")):
                dialog.get_by_role("button", name="结束会话").click()
            expect(tab).to_have_attribute("data-state", "closing")
            assert fixture.closes == [fixture.closes[0]] * 2
            page.evaluate("""id => {
              const socket = terminalFixtureSockets.find(s => s.id === id && s.readyState === 1);
              socket.emit({type:'exit', session_id:id, state:'closed', output_complete:true});
            }""", identifier)
            expect(tab).to_have_count(0)
            if width > 700:
                assert page.evaluate("location.hash") == route
                expect(page.locator(".vt-tab")).to_have_count(1)
            # Create one remaining remote session in the single-tab case to
            # exercise explicit detach and logout cleanup without reopening an end.
            if width <= 700:
                page.evaluate("location.hash = 'vps'")
                page.locator('[data-vps-action="terminal"]').click()
                expect(page.locator(".vt-state")).to_have_text("已连接")
            detach_all_tabs(page)
            expect(page.locator(".xterm")).to_have_count(0)
            assert page.evaluate("location.hash") == "#vps?view=terminal"
            page.evaluate("renderPage(false)")
            expect(page.locator(".xterm")).to_have_count(0)
            # The shared Space/logout cleanup path removes all private renderers.
            page.get_by_role("button", name="最近会话", exact=True).click()
            page.locator(".vt-recent-item").first.click()
            expect(page.locator(".xterm")).to_have_count(1)
            page.evaluate("clearSpaceSnapshots()")
            expect(page.locator(".xterm")).to_have_count(0)
            assert page.evaluate("CodePierVpsTerminal.count()") == 0
            assert page.evaluate("terminalFixtureErrors") == []
            assert not errors, errors
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_project_picker_survives_refresh_and_old_agents_show_an_actionable_reason(stack, engine):
    with sync_playwright() as pw:
        browser = launch_browser(getattr(pw, engine))
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        fixture = PanelFixture(page, stack, multiple=True)
        fixture.capability = {"available": False, "reason_code": "AGENT_UPGRADE_REQUIRED"}
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            login_panel(page, stack, "vps")
            page.locator('[data-vps-action="terminal"]').click()
            expect(page.get_by_role("combobox", name="连接项目", exact=True)).to_have_count(0)
            alert = page.locator('.vps-terminal-inline [role="alert"]')
            expect(alert).to_contain_text("更新")
            expect(page.get_by_role("button", name="查看连接电脑", exact=True)).to_be_visible()
            assert not fixture.creates
            page.evaluate(
                "window.firstUpgradeAlert = document.querySelector('.vps-terminal-inline [role=\"alert\"]')"
            )
            page.evaluate("renderPage(false)")
            assert page.evaluate(
                "document.querySelector('.vps-terminal-inline [role=\"alert\"]') === window.firstUpgradeAlert"
            )
            capture_evidence(page, f"terminal-project-upgrade-{engine}.png")

            # Multi-project VPS connects directly without showing a project picker.
            fixture.capability = {"available": True, "protocol_version": 1, "limits": {}, "saved_vps_host_verification": "skip"}
            page.locator('[data-vps-action="terminal"]').click()
            expect(page.get_by_role("combobox", name="连接项目", exact=True)).to_have_count(0)
            expect(page.locator(".vt-state")).to_have_text("已连接")
            assert len(fixture.creates) == 1
            assert not fixture.creates[-1].get("project_id")
            detach_all_tabs(page)
            page.evaluate("location.hash = 'vps'")
            expect(page.locator("#vps-query")).to_be_visible()

            # A tab detaches without closing; the ordinary button resumes it.
            first_session_id = next(iter(fixture.sessions))
            page.locator('[data-vps-action="terminal"]').click()
            expect(page.locator(".vt-state")).to_have_text("已连接")
            assert f"session={first_session_id}" in page.url
            assert len(fixture.creates) == 1
            detach_all_tabs(page)
            page.evaluate("location.hash = 'vps'")
            expect(page.locator("#vps-query")).to_be_visible()

            # Explicit New creates a direct session even when a detached one exists.
            saved_projects = list(fixture.projects)
            fixture.vps["project_ids"] = []
            fixture.vps["projects"] = []
            page.evaluate("renderPage(false)")
            new_terminal = page.locator('[data-vps-action="terminal-new"]')
            expect(new_terminal).to_be_enabled()
            new_terminal.click()
            expect(page.get_by_role("combobox", name="连接项目", exact=True)).to_have_count(0)
            expect(page.locator(".vt-state")).to_have_text("已连接")
            assert len(fixture.creates) == 2
            assert not fixture.creates[-1].get("project_id")
            detach_all_tabs(page)
            page.evaluate("location.hash = 'vps'")
            expect(page.locator("#vps-query")).to_be_visible()

            # New-session preflight must not disable a return to a known terminal.
            fixture.vps["projects"] = saved_projects
            fixture.vps["project_ids"] = [project["id"] for project in saved_projects]
            for project in fixture.projects:
                project["allow_tasks"] = False
            page.evaluate("renderPage(false)")
            terminal = page.locator('[data-vps-action="terminal"]')
            expect(terminal).to_have_text("返回终端 · 2")
            expect(terminal).to_be_enabled()
            expect(page.locator('[data-vps-action="terminal-new"]')).to_have_count(0)
            terminal.click()
            expect(page.locator(".vt-state")).to_have_text("已连接")
            returned_session = re.search(r"[?&]session=([0-9a-f]+)", page.url)
            assert returned_session is not None
            assert returned_session.group(1) in fixture.sessions
            assert len(fixture.creates) == 2

            # Without a running terminal, launch is still gated, with the original hint.
            for record in fixture.sessions.values():
                record["state"] = "closed"
            page.evaluate("location.hash = 'vps'")
            expect(page.locator("#vps-query")).to_be_visible()
            page.reload()
            expect(terminal).to_have_text("打开终端")
            expect(terminal).to_be_disabled()
            expect(page.locator(".vps-terminal-hint")).to_contain_text("写入和执行命令权限")
            assert len(fixture.creates) == 2
            assert page.evaluate("terminalFixtureErrors") == []
            assert not errors, errors
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_new_terminal_requires_explicit_server_selection(stack, engine):
    with sync_playwright() as pw:
        browser = launch_browser(getattr(pw, engine))
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        fixture = PanelFixture(page, stack, other_vps=True)
        original, other = list(fixture.servers.values())
        try:
            login_panel(page, stack, "vps")
            page.locator(
                f'[data-vps-action="terminal"][data-id="{original["id"]}"]'
            ).click()
            expect(page.locator(".vt-state")).to_have_text("已连接")
            new = page.get_by_role("button", name="＋ 新建", exact=True)
            picker = page.get_by_role("dialog", name="快速连接服务器")
            new.click()
            expect(picker).to_be_visible()
            search = picker.get_by_role("searchbox")
            expect(search).to_be_focused()
            assert len(fixture.creates) == 1
            picker.get_by_role("button", name="取消", exact=True).click()
            expect(picker).not_to_be_visible()
            expect(new).to_be_focused()
            assert len(fixture.creates) == 1
            new.click()
            expect(picker).to_be_visible()
            search.fill(other["name"])
            expect(picker.locator(".vt-quick-row")).to_have_count(1)
            with page.expect_response(lambda response: (
                response.request.method == "POST" and
                response.url.endswith("/api/vps-terminal/sessions")
            )):
                picker.get_by_role("button", name=other["name"], exact=False).click()
            expect(picker).not_to_be_visible()
            expect(page.locator(".vt-tab")).to_have_count(2)
            assert [body["vps_id"] for body in fixture.creates] == [original["id"], other["id"]]
            assert page.evaluate("terminalFixtureErrors") == []
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_terminal_output_survives_tabs_diagnostics_and_viewport_reflow(stack, engine):
    generation = "a" * 32
    with sync_playwright() as pw:
        browser = launch_browser(getattr(pw, engine))
        page = browser.new_page(viewport={"width": 1200, "height": 900})
        fixture = PanelFixture(page, stack, other_vps=True)
        original, other = list(fixture.servers.values())
        offsets = {}

        def write(session_id, payload):
            offset = offsets.get(session_id, 0)
            frame = TerminalFrame(FrameKind.OUTPUT, session_id, generation, 1, 1, offset, payload)
            page.evaluate(
                """([id, bytes]) => {
                    const sockets = terminalFixtureSockets.filter(s => s.id === id && s.readyState === 1);
                    if (sockets.length !== 1) throw new Error('Expected one live fixture socket');
                    sockets[0].onmessage({data: Uint8Array.from(bytes).buffer});
                }""",
                [session_id, list(encode_frame(frame))],
            )
            offsets[session_id] = offset + len(payload)

        def rows():
            return page.locator(".vt-primary-slot .xterm-accessibility-tree")

        try:
            login_panel(page, stack, "vps")
            page.locator(f'[data-vps-action="terminal"][data-id="{original["id"]}"]').click()
            expect(page.locator(".vt-state")).to_have_text("已连接")
            first_id = next(iter(fixture.sessions))
            page.evaluate("window.outputFirstRenderer = document.querySelector('.vt-primary-slot .xterm')")
            page.evaluate("location.hash = 'vps'")
            expect(page.locator("#vps-query")).to_be_visible()
            page.locator(f'[data-vps-action="terminal"][data-id="{other["id"]}"]').click()
            expect(page.locator(".vt-state")).to_have_text("已连接")
            expect(page.locator(".vt-tab")).to_have_count(2)
            second_id = list(fixture.sessions)[1]
            first_tab = page.get_by_role("tab", name=original["name"], exact=True)
            second_tab = page.get_by_role("tab", name=other["name"], exact=True)
            page.evaluate("window.outputSecondRenderer = document.querySelector('.vt-primary-slot .xterm')")

            page.locator(".vt-settings > summary").click()
            page.get_by_role("checkbox", name="屏幕阅读器模式").check()
            page.locator(".vt-settings > summary").click()
            write(first_id, b"history-first\r\n" + b"older-row\r\n" * 72)
            payload = "连续中文第一行\r\n\x1b[32m连续中文第二行\x1b[0m\r\n".encode()
            write(first_id, payload[:2])
            write(second_id, b"other-session-only\r\n")
            write(first_id, payload[2:24])
            expect(rows()).to_contain_text("other-session-only")
            first_tab.click()
            expect(rows()).to_contain_text("连续中文第一行")
            page.get_by_role("button", name="诊断", exact=True).click()
            diagnostic = page.get_by_role("region", name="当前会话连接诊断", exact=True)
            expect(diagnostic).to_be_visible()
            page.set_viewport_size({"width": 930, "height": 810})
            write(first_id, payload[24:] + "连续中文最后一行\r\n".encode())
            expect(rows()).to_contain_text("连续中文最后一行")
            diagnostic.get_by_role("button", name="收起", exact=True).click()
            for _ in range(2):
                second_tab.click()
                expect(rows()).to_contain_text("other-session-only")
                assert page.evaluate("document.querySelector('.vt-primary-slot .xterm') === outputSecondRenderer")
                first_tab.click()
                expect(rows()).to_contain_text("连续中文最后一行")
                assert page.evaluate("document.querySelector('.vt-primary-slot .xterm') === outputFirstRenderer")
            text = rows().inner_text()
            labels = ["连续中文第一行", "连续中文第二行", "连续中文最后一行"]
            assert [text.count(label) for label in labels] == [1, 1, 1]
            assert [text.index(label) for label in labels] == sorted(text.index(label) for label in labels)
            assert "other-session-only" not in text

            page.locator(".vt-primary-slot .xterm-helper-textarea").focus()
            for _ in range(4):
                page.keyboard.press("Shift+PageUp")
            expect(rows()).to_contain_text("history-first")
            write(first_id, b"after-scrolling\r\n")
            expect(rows()).to_contain_text("history-first")
            for _ in range(4):
                page.keyboard.press("Shift+PageDown")
            expect(rows()).to_contain_text("after-scrolling")
            assert page.evaluate("terminalFixtureSockets.map(socket => socket.id)") == [first_id, second_id]
            resizes = page.evaluate(
                """terminalFixtureSockets.flatMap(
                    socket => socket.controls.filter(message => message.type === 'resize'))"""
            )
            assert all(message["cols"] > 0 and message["rows"] > 0 for message in resizes)
            assert page.evaluate("terminalFixtureErrors") == []
        finally:
            browser.close()



@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_terminal_mobile_toolbar_survives_height_change_and_rotation(stack, engine):
    with sync_playwright() as pw:
        browser = launch_browser(getattr(pw, engine))
        page = browser.new_page(viewport={"width": 390, "height": 820}, has_touch=True)
        PanelFixture(page, stack)
        try:
            login_panel(page, stack, "vps")
            page.locator('[data-vps-action="terminal"]').tap()
            expect(page.locator(".vt-state")).to_have_text("已连接")
            assert_mobile_keys_are_unobscured(page)
            page.evaluate("""window.touchToolbar = document.querySelector('.vt-primary-slot .vt-mobile-keys');
                             window.touchRenderer = document.querySelector('.vt-primary-slot .xterm');""")
            page.get_by_role("button", name="诊断", exact=True).tap()
            diagnostic = page.get_by_role("region", name="当前会话连接诊断", exact=True)
            expect(diagnostic).to_be_visible()
            diagnostic.get_by_role("button", name="收起", exact=True).tap()
            expect(diagnostic).not_to_be_visible()
            for width, height in ((390, 610), (844, 390), (390, 820)):
                page.set_viewport_size({"width": width, "height": height})
                expect(page.locator(".vt-state")).to_have_text("已连接")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                assert page.evaluate("""document.querySelector('.vt-primary-slot .vt-mobile-keys') === touchToolbar &&
                                         document.querySelector('.vt-primary-slot .xterm') === touchRenderer""")
                if width < 700:
                    assert_mobile_keys_are_unobscured(page)
            assert page.evaluate("terminalFixtureSockets.length") == 1
            assert page.evaluate("terminalFixtureErrors") == []
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_mobile_reopens_existing_desktop_terminal_without_new_session(stack, engine):
    with sync_playwright() as pw:
        browser = launch_browser(getattr(pw, engine))
        desktop = browser.new_page(viewport={"width": 1200, "height": 900})
        mobile = browser.new_page(viewport={"width": 390, "height": 820}, has_touch=True)
        desktop_panel = PanelFixture(desktop, stack)
        mobile_panel = PanelFixture(mobile, stack)
        mobile_panel.servers = desktop_panel.servers
        mobile_panel.sessions = desktop_panel.sessions
        mobile_panel.capability = {"available": False, "reason_code": "AGENT_UPGRADE_REQUIRED"}
        held_presence = []
        mobile_admission_reads = []
        mobile_panel.servers = {
            identifier: {
                **item, "terminal": {"available": False, "eligible": True, "check_ready": False,
                                    "reason_code": "AGENT_UPGRADE_REQUIRED"},
            }
            for identifier, item in desktop_panel.servers.items()
        }

        def pause_cold_discovery(route):
            if route.request.method == "GET" and urlsplit(route.request.url).path == "/api/vps-terminal/sessions":
                held_presence.append(route)
            else:
                route.fallback()

        mobile.route("**/api/vps-terminal/sessions*", pause_cold_discovery)
        mobile.on("request", lambda request: mobile_admission_reads.append(request.url)
                  if "/api/vps-terminal/capabilities" in request.url else None)
        try:
            login_panel(desktop, stack, "vps")
            desktop.locator('[data-vps-action="terminal"]').click()
            expect(desktop.locator(".vt-state")).to_have_text("已连接")
            active_id = next(iter(desktop_panel.sessions))
            assert len(desktop_panel.creates) == 1
            desktop_panel.sessions[active_id].update(
                {"direct_vps": True, "project_id": "direct:" + desktop_panel.vps["id"], "limits": {}}
            )

            login_panel(mobile, stack, "vps")
            card = mobile.locator(f'[data-vps-action="terminal"][data-id="{desktop_panel.vps["id"]}"]')
            expect(card).to_be_visible()
            expect(card).to_be_disabled()
            assert held_presence, "browser must request the saved session directory"
            mobile.unroute("**/api/vps-terminal/sessions*", pause_cold_discovery)
            for pending in held_presence:
                mobile_panel.route(pending)
            expect(card).to_contain_text("返回终端")
            expect(card).to_be_enabled()
            card.tap()
            expect(mobile.locator(".vt-state")).to_have_text("已连接")
            expect(mobile).to_have_url(re.compile(r"session=" + active_id + r"$"))
            assert mobile.evaluate("terminalFixtureSockets.map(socket => socket.id)") == [active_id]
            assert mobile_panel.creates == []
            assert mobile_admission_reads == []

            # A cold renderer opening the pasted mobile URL must also resume the
            # exact existing server-side session, not submit another create.
            mobile.reload()
            expect(mobile.locator(".vt-state")).to_have_text("已连接")
            expect(mobile).to_have_url(re.compile(r"session=" + active_id + r"$"))
            assert mobile.evaluate("terminalFixtureSockets.map(socket => socket.id)") == [active_id]
            assert mobile_panel.creates == []
            assert mobile_admission_reads == []
        finally:
            browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_history_gap_requires_explicit_live_tail_for_the_same_direct_session(stack, engine):
    with sync_playwright() as pw:
        browser = launch_browser(getattr(pw, engine))
        page = browser.new_page(viewport={"width": 390, "height": 820}, has_touch=True)
        fixture = PanelFixture(page, stack)
        identifier = uuid.uuid4().hex
        fixture.sessions[identifier] = {
            "session_id": identifier,
            "vps_id": fixture.vps["id"],
            "project_id": "direct:" + fixture.vps["id"],
            "vps_name": fixture.vps["name"],
            "host": fixture.vps["host"],
            "port": fixture.vps["port"],
            "username": fixture.vps["username"],
            "direct_vps": True,
            "state": "ready",
            "mode": "direct",
            "limits": {},
        }
        requests = []

        def attach_ticket(route):
            assert route.request.method == "POST"
            session_id = urlsplit(route.request.url).path.split("/")[-2]
            assert session_id == identifier
            body = route.request.post_data_json
            requests.append(body)
            prefix = "live---" if body.get("resume_from_live") else "gap----"
            route.fulfill(json={"ticket": prefix + session_id,
                                "ws_path": "/ws/vps-terminal", "expires_in": 20})

        page.route("**/api/vps-terminal/sessions/*/attach-ticket", attach_ticket)
        try:
            login_panel(page, stack, "vps")
            page.evaluate("id => { location.hash = 'vps?view=terminal&session=' + id; }", identifier)
            retry = page.get_by_role("button", name="继续原会话", exact=True)
            expect(retry).to_be_visible()
            expect(page.get_by_role("tabpanel").get_by_text(re.compile("旧画面无法完整恢复")).first).to_be_visible()
            assert len(requests) == 1
            assert not requests[0].get("resume_from_live")
            assert fixture.creates == []
            assert page.evaluate("terminalFixtureSockets.map(socket => socket.id)") == [identifier]

            retry.tap()
            expect(page.locator(".vt-state")).to_have_text("已连接")
            expect(page.get_by_text(
                "已继续原 SSH，会话未重建。旧画面和历史输出无法恢复，只显示之后的新输出。",
                exact=True,
            )).to_be_visible()
            assert len(requests) == 2
            assert requests[1]["resume_from_live"] is True
            assert requests[0]["renderer_id"] == requests[1]["renderer_id"]
            expect(page).to_have_url(re.compile(r"session=" + identifier + r"$"))
            expect(page.get_by_role("tab")).to_have_count(1)
            assert fixture.creates == []
            assert page.evaluate("terminalFixtureSockets.map(socket => socket.id)") == [identifier, identifier]

            # Observe the actual real xterm output after the accepted baseline.
            # The mock fails immediately on binary client input.
            page.locator(".vt-settings > summary").click()
            page.get_by_role("checkbox", name="屏幕阅读器模式").check()
            page.locator(".vt-settings > summary").click()
            for cursor, value in ((0, b"old-fixture-history"), (555, b"fresh-fixture-output\r\n")):
                frame = TerminalFrame(FrameKind.OUTPUT, identifier, "a" * 32, 1, 1, cursor, value)
                page.evaluate(
                    """bytes => terminalFixtureSockets.at(-1).onmessage({
                        data: Uint8Array.from(bytes).buffer
                    })""",
                    list(encode_frame(frame)),
                )
            rows = page.locator(".vt-primary-slot .xterm-accessibility-tree")
            expect(rows).to_contain_text("fresh-fixture-output")
            expect(rows).not_to_contain_text("old-fixture-history")
            assert page.evaluate("terminalFixtureErrors") == []
        finally:
            browser.close()
