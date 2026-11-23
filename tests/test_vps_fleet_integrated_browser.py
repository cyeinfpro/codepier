from __future__ import annotations

import asyncio
from collections import Counter
from threading import Event, Thread
from types import SimpleNamespace
import time

from playwright.sync_api import expect
import pytest
import uvicorn

from hub.app import create_app
from shared.crypto import password_hash
from tests.fixture_hub import bind_listener
from tests.legacy_iam_fixture import seed_owner
from tests.support import fixture_http_client, wait_for
from tests.test_ssh_terminal_api import BrowserIdentity
from tests.test_vps_readiness_wiring import add
from tests.test_vps_ui import browser  # noqa: F401

LOGIN = "isolated-vps-browser-test-login"


class LoopbackIdentity(BrowserIdentity):
    origin: str = ""

    def headers(self, **changes):
        return super().headers(**{"Origin": self.origin, **changes})


class TestExecutor:
    __test__ = False

    def __init__(self):
        self.ready = set()
        self.scan_gate = Event()
        self.install_gate = Event()
        self.inspect_calls = []
        self.install_calls = []

    async def inspect(self, target):
        self.inspect_calls.append(target.vps_id)
        assert await asyncio.to_thread(self.scan_gate.wait, 20)
        return "ready" if target.vps_id in self.ready else "missing"

    async def provision(self, target):
        self.install_calls.append(target.vps_id)
        assert await asyncio.to_thread(self.install_gate.wait, 20)
        self.ready.add(target.vps_id)
        return "ok"


@pytest.fixture
def controlled_live_stack(tmp_path, monkeypatch):
    native = TestExecutor()
    with bind_listener() as listener:
        url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        for key in ("HUB_PUBLIC_URL", "MCP_PUBLIC_URL"):
            monkeypatch.setenv(key, url)
        app = create_app(str(tmp_path / "hub"), readiness_executor_factory=lambda _: native)
        store = app.state.store
        seed_owner(store, "fleet-owner", "admin")
        store.execute(
            "UPDATE users SET password_hash=? WHERE id=?", (password_hash(LOGIN), "fleet-owner")
        )
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            with fixture_http_client(url) as client:
                wait_for(lambda: client.get("/healthz", timeout=1).is_success, timeout=15)
                login = client.post(
                    "/api/login",
                    json={"username": "admin", "password": LOGIN},
                    headers={"Origin": url},
                )
                assert login.is_success, login.text
                client.cookies.clear()
                with store.transaction() as db:
                    db.execute(
                        "INSERT INTO spaces (id,label,kind,created) VALUES ('fleet-space','Fleet','team',?)",
                        (time.time(),),
                    )
                    db.execute(
                        "INSERT INTO memberships (space_id,user_id,level) VALUES ('fleet-space','fleet-owner','owner')"
                    )
                owner = LoopbackIdentity(
                    client, login.cookies["rd_session"], login.json()["csrf"], "fleet-space"
                )
                owner.origin = url
                yield SimpleNamespace(
                    url=url,
                    password=LOGIN,
                    store=store,
                    executor=native,
                    client=client,
                    owner=owner,
                )
        finally:
            native.scan_gate.set()
            native.install_gate.set()
            server.should_exit = True
            thread.join(timeout=15)
            assert not thread.is_alive()


@pytest.mark.parametrize("synthetic_space", ["fleet-space"])
def test_two_phase_through_real_browser_hub_queue_and_executor(
    browser, controlled_live_stack, synthetic_space
):
    engine, browser_name = browser
    native = controlled_live_stack.executor
    s = controlled_live_stack
    labels = {
        "ready": add(s.owner, "ready")["id"],
        "fresh": add(s.owner, "fresh")["id"],
        "expired": add(s.owner, "expired")["id"],
        "untrusted": add(s.owner, "untrusted", pin=False)["id"],
        "disabled": add(s.owner, "disabled", enabled=False)["id"],
    }
    native.ready.add(labels["ready"])
    assert not native.inspect_calls and not native.install_calls

    page = engine.new_page(
        viewport={"width": 1440 if browser_name == "chromium" else 390, "height": 844}
    )
    posts = []
    page.on(
        "request",
        lambda req: (
            posts.append(req.url.split("/")[-1])
            if req.method == "POST" and "/api/vps/readiness/" in req.url
            else None
        ),
    )
    try:
        page.goto(s.url + "/#overview")
        page.fill("#username", "admin")
        page.fill("#password", s.password)
        page.click("#login-form button")
        expect(page.locator("#page h1")).to_have_text("总览", timeout=15000)
        if browser_name == "webkit":
            page.locator(".mobile-menu").click()
        page.locator("#iam-active-space").select_option(synthetic_space)
        page.wait_for_function(
            "(id) => S.space_id === id && S.page === 'overview'", arg=synthetic_space
        )
        if browser_name == "webkit" and not page.locator('.sidebar [data-nav="vps"]').is_visible():
            page.locator(".mobile-menu").click()
        page.locator('.sidebar [data-nav="vps"]').click()
        expect(page.locator("#page h1")).to_have_text("VPS", timeout=15000)
        summary = page.locator(".vps-backfill > summary")
        scan = page.get_by_role("button", name="扫描全部 SSH")
        install = page.get_by_role("button", name="一键安装")
        counts = page.locator("[data-vps-fleet-counts]")
        reasons = page.locator("[data-vps-fleet-reasons]")
        expect(counts).to_contain_text("未检查 4")
        summary.click()
        expect(install).to_be_disabled()
        scan.click()
        expect(counts).to_contain_text("扫描中", timeout=10000)
        assert not native.install_calls
        page.reload()
        expect(counts).not_to_contain_text("扫描中 0", timeout=10000)
        summary.click()
        native.scan_gate.set()
        expect(counts).to_contain_text("缺少 2", timeout=12000)
        expect(counts).to_contain_text("就绪 1")
        expect(reasons).to_contain_text("1 台：面板尚未核验主机指纹")
        assert posts == ["scan"]
        assert not native.install_calls
        assert Counter(native.inspect_calls) == Counter(
            [labels["ready"], labels["fresh"], labels["expired"]]
        )

        # Repeatable test-only clock seam: age just one authoritative scan result.
        s.store.execute(
            "UPDATE vps_readiness_jobs SET updated=? WHERE vps_id=?",
            (time.time() - 1801, labels["expired"]),
        )
        page.reload()
        expect(counts).to_contain_text("缺少 2")
        summary.click()
        expect(page.locator("[data-vps-backfill-status]")).to_contain_text("1 台的缺失结果已过期")
        expect(install).to_be_enabled()
        install.click()
        expect(counts).not_to_contain_text("安装中 0", timeout=10000)
        assert native.install_calls == [labels["fresh"]]
        native.install_gate.set()
        expect(counts).to_contain_text("就绪 2", timeout=12000)
        expect(counts).to_contain_text("缺少 1")
        expect(install).to_be_disabled()
        assert posts == ["scan", "install"]
        assert native.install_calls == [labels["fresh"]]
        assert Counter(native.inspect_calls)[labels["fresh"]] == 3
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    finally:
        page.close()
