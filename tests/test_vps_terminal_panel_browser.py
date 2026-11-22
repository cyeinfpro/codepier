"""Real panel routes and real xterm with browser-local terminal HTTP/WS fixtures.

These tests exercise the app's lazy loader, inventory, route lifecycle and DOM.
They do not open a real SSH connection; the transport/SSH integration suite owns
that boundary. No credentials or terminal input are persisted by this fixture.
"""

import os
from pathlib import Path
import uuid
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

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
      this.readyState = 0; this.bufferedAmount = 0;
      terminalFixtureSockets.push(this);
      setTimeout(() => { this.readyState = 1; this.onopen?.(); }, 0);
    }
    send(data) {
      if (typeof data !== 'string') fail('Unexpected input in route-only fixture');
      const message = JSON.parse(data);
      if (message.type === 'authenticate') {
        this.id = message.ticket.slice(7);
        this.emit({type:'attached', session_id:this.id,
          runtime_generation:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
          attachment_id:this.id, render_epoch:'1', stream_epoch:'1', writer_epoch:'1',
          start_offset:'0', output_end_offset:'0', next_input_seq:'1', cols:100, rows:30,
          mode:'tmux', resume_strategy:'live', state:'ready'});
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
    def __init__(self, page, stack, multiple=False):
        self.sessions = {}
        self.creates = []
        self.diagnostics_requests = []
        self.capability = {"available": True, "protocol_version": 1, "limits": {}}
        self.projects = [
            {**project, "mode": "write", "allow_tasks": True, "online": True,
             "device_name": project.get("device_name", "Fixture Agent")}
            for project in stack.projects[:2 if multiple else 1]
        ]
        self.vps = {
            "id": uuid.uuid4().hex, "name": "终端路由测试", "host": "fixture.invalid",
            "port": 22, "username": "fixture", "enabled": True,
            "host_key_policy": "strict", "provider": "", "region": "", "system": "Linux",
            "notes": "", "project_ids": [project["id"] for project in self.projects],
            "projects": self.projects,
        }
        page.add_init_script(WIRE)
        page.route("**/api/vps**", self.route)

    def route(self, route):
        path = urlsplit(route.request.url).path
        method = route.request.method
        if path == "/api/vps":
            route.fulfill(json={"vps": [self.vps], "next_offset": None})
        elif path == "/api/vps/" + self.vps["id"]:
            route.fulfill(json=self.vps)
        elif path == "/api/vps-terminal/capabilities":
            route.fulfill(json=self.capability)
        elif path == "/api/vps-terminal/sessions" and method == "POST":
            body = route.request.post_data_json
            self.creates.append(body)
            identifier = uuid.uuid4().hex
            project = next(item for item in self.projects if item["id"] == body["project_id"])
            session = {"session_id": identifier, "vps_id": self.vps["id"],
                       "project_id": project["id"], "vps_name": self.vps["name"],
                       "project_alias": project["alias"], "device_name": project["device_name"],
                       "username": "fixture", "mode": "tmux", "state": "ready"}
            self.sessions[identifier] = session
            route.fulfill(json=session)
        elif path == "/api/vps-terminal/sessions":
            route.fulfill(json={"sessions": list(self.sessions.values())})
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
            while page.locator(".vt-tab-close").count():
                page.locator(".vt-tab-close").last.click()
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
            picker = page.get_by_role("combobox", name="连接项目", exact=True)
            expect(picker).to_be_visible()
            picker.select_option(fixture.projects[-1]["id"])
            page.evaluate("window.firstPicker = document.querySelector('.vps-terminal-inline select')")
            page.evaluate("renderPage(false)")
            assert page.evaluate("document.querySelector('.vps-terminal-inline select') === firstPicker")
            expect(picker).to_have_value(fixture.projects[-1]["id"])
            page.locator(".vps-terminal-inline").get_by_role("button", name="连接", exact=True).click()
            expect(page.locator('.vps-terminal-inline [role="alert"]')).to_contain_text("需要更新")
            expect(page.get_by_role("button", name="查看连接电脑", exact=True)).to_be_visible()
            assert not fixture.creates
            capture_evidence(page, f"terminal-project-upgrade-{engine}.png")
            for project in fixture.projects:
                project["allow_tasks"] = False
            page.evaluate("renderPage(false)")
            expect(page.locator('[data-vps-action="terminal"]')).to_be_disabled()
            expect(page.locator(".vps-terminal-hint")).to_contain_text("写入和执行命令权限")
            assert page.evaluate("terminalFixtureErrors") == []
            assert not errors, errors
        finally:
            browser.close()
