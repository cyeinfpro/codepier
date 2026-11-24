"""Mobile first-principles static contracts and multi-viewport browser gates.

Covers SITE-24, SITE-25 through SITE-35 and Chapter 20 geometry/interaction
invariants across source contracts, 320x480 embedded MCP App / browser extension
surfaces, and authenticated panel routes, login and OAuth 2.1 consent sheets.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]

FULL_ROUTES = (
    "overview",
    "projects",
    "workbench",
    "native",
    "collaboration",
    "workflows",
    "artifacts",
    "audit",
    "connect",
    "profiles",
    "mcp-gateway",
    "integrations",
    "identity",
    "members",
    "roles",
    "devices",
    "vps",
    "identity-admin",
    "diagnostics",
    "settings",
)

CORE_ROUTES = (
    "overview",
    "projects",
    "workbench",
    "vps",
    "audit",
    "connect",
    "profiles",
    "integrations",
    "mcp-gateway",
    "settings",
    "native",
    "collaboration",
)


# ---------------------------------------------------------------------------
# Part A: Fast static & source contract assertions (zero browser dependency)
# ---------------------------------------------------------------------------


def test_static_safe_area_tokens_contract():
    tokens = (ROOT / "web/tokens.css").read_text(encoding="utf-8")
    for name, env_key in (
        ("--safe-top", "safe-area-inset-top"),
        ("--safe-right", "safe-area-inset-right"),
        ("--safe-bottom", "safe-area-inset-bottom"),
        ("--safe-left", "safe-area-inset-left"),
    ):
        assert name in tokens, f"Missing {name} in web/tokens.css"
        assert env_key in tokens, f"Missing {env_key} in web/tokens.css"


def test_static_styles_overscroll_breakpoint_and_coarse_input_contract():
    styles = (ROOT / "web/styles.css").read_text(encoding="utf-8")
    assert re.search(r"overscroll-behavior-y\s*:\s*contain", styles)
    assert re.search(r"max-width\s*:\s*900px", styles)
    assert not re.search(
        r"@media\s*\(\s*min-width\s*:\s*641px\s*\)\s*\{\s*\.mobile-work-tabs",
        styles,
    )
    assert re.search(r"pointer\s*:\s*coarse", styles)
    assert re.search(
        r"font-size\s*:\s*(?:max\(\s*16px|16px|var\(--ui-text-input\))",
        styles,
    )


def test_static_pwa_manifest_and_index_link_contract():
    manifest_path = ROOT / "web/manifest.json"
    assert manifest_path.is_file(), "Missing web/manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.get("display") == "standalone"
    index_source = (ROOT / "web/index.source.html").read_text(encoding="utf-8")
    assert "/static/manifest.json" in index_source
    assert 'rel="manifest"' in index_source


def test_static_tmux_controller_mobile_flags_contract():
    tmux_source = (ROOT / "agent/ssh_terminal_tmux.py").read_text(encoding="utf-8")
    for token in (
        "escape-time 10",
        "status off",
        "mouse on",
        "window-size latest",
        "-f /dev/null",
    ):
        assert token in tmux_source, f"Missing {token!r} in agent/ssh_terminal_tmux.py"


def test_static_mcp_apps_and_browser_extension_contracts():
    app_css = (ROOT / "web/mcp-apps/app.css").read_text(encoding="utf-8")
    assert re.search(r"max-width\s*:\s*420px", app_css)
    assert re.search(r"overscroll-behavior-x\s*:\s*contain", app_css)
    assert ".is-wrapped" in app_css
    assert re.search(r"hover\s*:\s*hover", app_css)
    assert re.search(r"touch-action\s*:\s*manipulation", app_css)

    review_js = (ROOT / "web/mcp-apps/review.js").read_text(encoding="utf-8")
    assert "aria-pressed" in review_js
    assert "is-wrapped" in review_js
    assert "自动折行" in review_js

    popup_css = (ROOT / "web/browser-extension/popup.css").read_text(encoding="utf-8")
    assert "min(380px, 100vw)" in popup_css
    assert re.search(r"pointer\s*:\s*coarse", popup_css)
    assert "16px" in popup_css
    assert re.search(r"hover\s*:\s*hover", popup_css)
    assert re.search(r"touch-action\s*:\s*manipulation", popup_css)

    popup_html = (ROOT / "web/browser-extension/popup.html").read_text(encoding="utf-8")
    assert 'autocapitalize="none"' in popup_html
    assert 'autocorrect="off"' in popup_html
    assert 'spellcheck="false"' in popup_html
    assert 'inputmode="url"' in popup_html


def test_static_chat_approvals_and_streaming_uploads_contracts():
    approvals = (ROOT / "web/chat/approvals.js").read_text(encoding="utf-8")
    assert "<select multiple" not in approvals
    assert "select.multiple" not in approvals

    uploads = (ROOT / "web/chat/uploads.js").read_text(encoding="utf-8")
    assert ".slice(" in uploads
    assert "await file.arrayBuffer()" not in uploads


# ---------------------------------------------------------------------------
# Part B: Real-browser multi-viewport geometry & interaction gates
# ---------------------------------------------------------------------------


def _pw():
    return pytest.importorskip("playwright.sync_api")


class _InProcessHubFixture:
    def __init__(self, url: str, password: str, client, project: dict):
        self.url = url
        self.password = password
        self.client = client
        self.project = project

    @staticmethod
    def must(resp):
        assert resp.is_success, f"{resp.status_code}: {resp.text[:800]}"
        return resp.json()


@pytest.fixture(scope="module")
def panel_hub(tmp_path_factory):
    import os
    import socket
    import threading
    import uuid
    import httpx
    import uvicorn
    from hub.app import create_app
    from hub.store import Store
    from shared.crypto import password_hash, token

    data_dir = tmp_path_factory.mktemp("mobile-panel-hub")
    password = "Mobile-Gate-Fixture-" + token(16)
    seed_store = Store(data_dir)
    admin_id = uuid.uuid4().hex
    now = time.time()
    seed_store.execute(
        "INSERT INTO users VALUES (?,?,?,?)",
        (admin_id, "admin", password_hash(password), now),
    )
    project_id = uuid.uuid4().hex
    device_id = "dev-mobile-fixture"
    seed_store.execute(
        "INSERT INTO devices(id,name,secret,enabled,created,last_seen,info) VALUES (?,?,?,?,?,?,?)",
        (device_id, "MacStudioFixture", password_hash("dev-secret"), 1, now, now, "{}"),
    )
    seed_store.execute(
        "INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            project_id,
            "ProjectAlpha",
            "projectalpha",
            device_id,
            "/workspace/alpha",
            "移动端门禁项目",
            "write",
            1,
            now,
        ),
    )
    seed_store.close()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    prev_env = {k: os.environ.get(k) for k in ("HUB_DATA_DIR", "HUB_PUBLIC_URL", "MCP_PUBLIC_URL")}
    os.environ["HUB_DATA_DIR"] = str(data_dir)
    os.environ["HUB_PUBLIC_URL"] = url
    os.environ["MCP_PUBLIC_URL"] = url
    app = create_app(str(data_dir))
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", access_log=False)
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    client = httpx.Client(base_url=url, timeout=15, trust_env=False)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                r = client.get("/healthz")
                if r.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        login_res = client.post("/api/login", json={"username": "admin", "password": password})
        assert login_res.is_success, login_res.text
        client.headers["X-RD-CSRF"] = login_res.json()["csrf"]
        client.post(
            "/api/vps",
            json={
                "name": "Tokyo Edge VPS",
                "host": "198.51.100.24",
                "port": 22,
                "username": "ubuntu",
                "password": "Fixture-Password-1234",
                "host_key_policy": "accept-new",
                "region": "ap-northeast-1",
                "system": "Ubuntu 24.04",
            },
        )
        yield _InProcessHubFixture(
            url=url,
            password=password,
            client=client,
            project={"id": project_id, "alias": "ProjectAlpha"},
        )
    finally:
        client.close()
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()
        for k, v in prev_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _new_mobile_context(chat_browser_pool, width: int, height: int, engine: str = "chromium"):
    return chat_browser_pool(engine).new_context(
        viewport={"width": width, "height": height},
        is_mobile=(engine == "chromium"),
        has_touch=True,
    )


def _assert_zero_overflow(page, label: str) -> None:
    metrics = page.evaluate(
        """() => ({
          scrollWidth: document.documentElement.scrollWidth,
          innerWidth: window.innerWidth,
        })"""
    )
    assert metrics["scrollWidth"] <= metrics["innerWidth"], (label, metrics)


def _assert_frame_zero_overflow(frame_locator, label: str) -> None:
    metrics = frame_locator.locator("html").evaluate(
        """el => ({
          scrollWidth: el.scrollWidth,
          innerWidth: el.ownerDocument.defaultView.innerWidth,
        })"""
    )
    assert metrics["scrollWidth"] <= metrics["innerWidth"], (label, metrics)


def _collect_small_inputs(page) -> list[dict]:
    return page.evaluate(
        """() => {
          const selector = [
            'input:not([type="checkbox"]):not([type="radio"]):not([type="range"])' +
              ':not([type="file"]):not([type="hidden"]):not([type="color"])' +
              ':not([type="button"]):not([type="submit"]):not([type="reset"])',
            'select',
            'textarea',
          ].join(',');
          const small = [];
          for (const el of document.querySelectorAll(selector)) {
            if (el.disabled || el.hidden || el.closest('[hidden], [inert]')) continue;
            const style = getComputedStyle(el);
            if (
              style.display === 'none' ||
              style.visibility === 'hidden' ||
              parseFloat(style.opacity) === 0
            ) continue;
            const rect = el.getBoundingClientRect();
            if (rect.width <= 1 || rect.height <= 1) continue;
            const fontSize = parseFloat(style.fontSize);
            if (fontSize < 16) {
              small.push({
                tag: el.tagName.toLowerCase(),
                id: el.id || '',
                name: el.getAttribute('name') || '',
                cls: String(el.className || ''),
                fontSize,
              });
            }
          }
          return small;
        }"""
    )


def _assert_overscroll_guarded(page, label: str) -> None:
    values = page.evaluate(
        """() => ({
          html: getComputedStyle(document.documentElement).overscrollBehaviorY,
          body: getComputedStyle(document.body).overscrollBehaviorY,
        })"""
    )
    assert values["html"] != "auto", (label, values)
    assert values["body"] != "auto", (label, values)


def _is_mobile_dock_visible(page) -> bool:
    return bool(
        page.evaluate(
            """() => {
              const dock = document.querySelector('.mobile-dock');
              if (!dock || dock.hidden) return false;
              const s = getComputedStyle(dock);
              if (s.display === 'none' || s.visibility === 'hidden') return false;
              return dock.getBoundingClientRect().height > 0;
            }"""
        )
    )


def _assert_hit_test(
    page, selector: str, label: str, *, min_size: float = 0, include_disabled: bool = False
) -> None:
    page.evaluate(
        """async () => {
          const anims = document.getAnimations ? document.getAnimations({ subtree: true }) : [];
          await Promise.all(anims.map((a) => a.finished.catch(() => {})));
        }"""
    )
    result = page.evaluate(
        """({ selector, minSize, includeDisabled }) => {
          const nodes = [...document.querySelectorAll(selector)].filter((el) => {
            if ((!includeDisabled && el.disabled) || el.hidden || el.closest('[hidden], [inert]')) {
              return false;
            }
            const s = getComputedStyle(el);
            if (s.display === 'none' || s.visibility === 'hidden' || parseFloat(s.opacity) === 0) {
              return false;
            }
            const r = el.getBoundingClientRect();
            return r.width > 1 && r.height > 1;
          });
          if (!nodes.length) return { count: 0, failures: [] };
          const failures = [];
          for (const el of nodes) {
            if (el.closest('#computer-approval-inbox')) {
              el.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            } else if (!el.closest('.mobile-dock, .topbar')) {
              el.scrollIntoView({ block: 'center', inline: 'nearest' });
            }
            const r = el.getBoundingClientRect();
            if (minSize > 0 && (r.width < minSize || r.height < minSize)) {
              failures.push({
                reason: 'size',
                text: (el.innerText || el.getAttribute('aria-label') || el.id).trim(),
                width: r.width,
                height: r.height,
              });
              continue;
            }
            const cx = r.left + r.width / 2;
            const cy = r.top + r.height / 2;
            if (cx < 0 || cy < 0 || cx >= window.innerWidth || cy >= window.innerHeight) {
              failures.push({
                reason: 'offscreen',
                cx,
                cy,
                vw: window.innerWidth,
                vh: window.innerHeight,
              });
              continue;
            }
            const hit = document.elementFromPoint(cx, cy);
            const owner = hit ? hit.closest('button, a, summary, label, [role="button"]') : null;
            const matched = Boolean(hit && (hit === el || el.contains(hit) || owner === el));
            if (!matched) {
              failures.push({
                reason: 'occluded',
                target: el.id || el.className,
                hitTag: hit ? hit.tagName : null,
                hitId: hit ? hit.id : null,
                hitClass: hit ? String(hit.className || '') : null,
              });
            }
          }
          return { count: nodes.length, failures };
        }""",
        {"selector": selector, "minSize": min_size, "includeDisabled": include_disabled},
    )
    assert result["count"] > 0, f"No visible controls matched {selector!r} on {label}"
    assert not result["failures"], (label, selector, result["failures"])


def _login_and_check_entry(page, stack, route: str = "overview") -> None:
    expect = _pw().expect
    page.goto(f"{stack.url}/#{route}", wait_until="domcontentloaded")
    expect(page.locator("#login-form")).to_be_visible()
    _assert_zero_overflow(page, f"login@{page.viewport_size}")
    assert not _collect_small_inputs(page), f"login small inputs: {_collect_small_inputs(page)}"
    _assert_overscroll_guarded(page, "login")
    _assert_hit_test(page, '#login-form button[type="submit"]', "login-submit", min_size=40)
    attrs = page.locator("#username").evaluate(
        """el => ({
          autocapitalize: el.getAttribute('autocapitalize'),
          spellcheck: el.getAttribute('spellcheck'),
        })"""
    )
    assert attrs["autocapitalize"] == "none", attrs
    assert attrs["spellcheck"] == "false", attrs

    page.fill("#username", "admin")
    page.fill("#password", stack.password)
    page.click('#login-form button[type="submit"]')
    expect(page.locator("#page .skeleton")).to_have_count(0, timeout=15_000)
    expect(page.locator("#page h1")).to_be_visible(timeout=15_000)


def _navigate_route(page, route: str) -> None:
    expect = _pw().expect
    page.evaluate("(target) => navigate(target)", route)
    expect(page.locator("#page .skeleton")).to_have_count(0, timeout=15_000)
    expect(page.locator("#page h1")).to_be_visible(timeout=15_000)
    page.evaluate("() => document.getElementById('toasts')?.replaceChildren()")


def _check_route_invariants(page, route: str, width: int, height: int) -> None:
    label = f"{route}@{width}x{height}"
    _assert_zero_overflow(page, label)
    small_inputs = _collect_small_inputs(page)
    assert not small_inputs, (label, small_inputs)
    _assert_overscroll_guarded(page, label)
    assert page.locator("select[multiple]:visible").count() == 0, label

    if _is_mobile_dock_visible(page):
        _assert_hit_test(
            page,
            ".mobile-dock button, .mobile-dock a",
            f"{label}-dock",
            min_size=40,
        )
    if route == "audit" and page.locator("#audit-query").is_visible():
        page.fill("#audit-query", "probe")
        if page.locator(".call-search-clear").is_visible():
            _assert_hit_test(page, ".call-search-clear", f"{label}-clear", min_size=36)
            page.click(".call-search-clear")
    if route == "native" and page.locator("#chat-send").is_visible():
        _assert_hit_test(
            page, "#chat-send", f"{label}-chat-send", min_size=40, include_disabled=True
        )


def _open_oauth_consent(page, stack) -> None:
    expect = _pw().expect
    registration = stack.must(
        stack.client.post(
            "/oauth/register",
            json={"redirect_uris": ["http://localhost:12345/callback"]},
        )
    )
    verifier = "v" * 64
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    response = stack.client.get(
        "/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": registration["client_id"],
            "redirect_uri": "http://localhost:12345/callback",
            "scope": "read write execute computer",
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "resource": stack.url + "/mcp",
        },
    )
    request_id = parse_qs(urlsplit(response.headers["location"]).query)["authorize"][0]
    page.evaluate("(id) => consentModal(id)", request_id)
    expect(page.locator("#allow-dev")).to_be_visible()


def _check_vps_modal_invariants(page, label: str) -> None:
    expect = _pw().expect
    page.evaluate("() => vpsEdit()")
    expect(page.locator("#vps-form")).to_be_visible()
    focus_info = page.evaluate(
        """() => {
          const active = document.activeElement;
          if (!active) return { autoKeyboard: false, tag: null };
          const autoKeyboard =
            active.matches('input, textarea, select') && !active.hasAttribute('autofocus');
          return {
            autoKeyboard,
            tag: active.tagName,
            name: active.getAttribute('name') || '',
          };
        }"""
    )
    assert not focus_info["autoKeyboard"], (label, focus_info)
    _assert_zero_overflow(page, f"{label}-vps-form")
    assert not _collect_small_inputs(page), (label, _collect_small_inputs(page))
    _assert_hit_test(page, "#vps-save", f"{label}-vps-save", min_size=40)
    ssh_attrs = page.evaluate(
        """() => ['host', 'username'].map((name) => {
          const el = document.querySelector(`#vps-form [name="${name}"]`);
          return {
            name,
            autocapitalize: el?.getAttribute('autocapitalize'),
            spellcheck: el?.getAttribute('spellcheck'),
          };
        })"""
    )
    for item in ssh_attrs:
        assert item["autocapitalize"] in ("none", "off"), item
        assert item["spellcheck"] == "false", item
    page.evaluate("() => closeModal()")
    expect(page.locator("#vps-form")).to_have_count(0)


def _check_oauth_and_approval_invariants(page, stack, label: str) -> None:
    expect = _pw().expect
    _open_oauth_consent(page, stack)
    _assert_zero_overflow(page, f"{label}-oauth")
    _assert_hit_test(page, "#allow-dev", f"{label}-oauth-allow-dev", min_size=40)
    page.locator("#oauth-options > summary").click()
    expect(page.locator("#oauth-days")).to_be_visible()
    assert not _collect_small_inputs(page), (label, _collect_small_inputs(page))
    _assert_zero_overflow(page, f"{label}-oauth-expanded")
    page.evaluate("() => closeModal()")
    expect(page.locator("#allow-dev")).to_have_count(0)

    _navigate_route(page, "overview")
    approval_payload = {
        "approvals": [
            {
                "request_id": "a" * 32,
                "session_id": "b" * 32,
                "project_id": stack.project["id"],
                "owner": "panel:admin",
                "app": "Terminal",
                "message": "Allow desktop inspection?",
                "expires_at": time.time() + 300,
            }
        ]
    }
    page.route("**/api/computer/approvals*", lambda route: route.fulfill(json=approval_payload))
    page.evaluate("() => refreshComputerApprovals()")
    expect(page.locator("#computer-approval-inbox")).to_be_visible()
    _assert_zero_overflow(page, f"{label}-approval-inbox")
    decision_btns = page.locator("#computer-approval-inbox [data-request-id] button")
    if decision_btns.first.is_visible():
        _assert_hit_test(
            page,
            "#computer-approval-inbox [data-request-id] button",
            f"{label}-approval-buttons",
        )
    if _is_mobile_dock_visible(page):
        _assert_hit_test(
            page,
            ".mobile-dock button, .mobile-dock a",
            f"{label}-dock-with-inbox",
            min_size=40,
        )
    page.unroute("**/api/computer/approvals*")
    page.evaluate("() => stopComputerApprovals()")


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_mcp_changes_embed_320x480_geometry_and_wrap_toggle(chat_browser_pool, engine):
    expect = _pw().expect
    context = _new_mobile_context(chat_browser_pool, 320, 480, engine=engine)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    try:
        changes_html = (ROOT / "web/mcp-apps/changes-v1.html").read_text(encoding="utf-8")
        host_html = (
            "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'></head>"
            "<body style='margin:0;padding:0'></body></html>"
        )
        page.route(
            "http://mcp-host.local/host.html",
            lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=host_html),
        )
        page.route(
            "http://mcp-app.local/changes-v1.html",
            lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=changes_html),
        )
        page.goto("http://mcp-host.local/host.html", wait_until="domcontentloaded")
        page.evaluate(
            """() => {
              const frame = document.createElement('iframe');
              frame.id = 'app-frame';
              frame.setAttribute('sandbox', 'allow-scripts');
              frame.style.cssText = 'width:100%;height:480px;border:0;display:block';
              const ref = 'a'.repeat(32);
              const diffText = [
                '@@ -1,2 +1,2 @@',
                '-const oldToken = 1;',
                '+const narrowBubbleLongConfigurationIdentifier = "' + 'x'.repeat(160) + '";',
              ].join('\\n');
              window.addEventListener('message', (event) => {
                const msg = event.data;
                if (event.source !== frame.contentWindow || !msg || msg.jsonrpc !== '2.0') return;
                if (msg.method === 'ui/initialize' && 'id' in msg) {
                  frame.contentWindow.postMessage({
                    jsonrpc: '2.0',
                    id: msg.id,
                    result: {
                      protocolVersion: msg.params?.protocolVersion || '2026-01-26',
                      hostCapabilities: {},
                      hostContext: { theme: 'light', displayMode: 'inline' },
                    },
                  }, '*');
                  return;
                }
                if (msg.method === 'ui/notifications/initialized') {
                  frame.contentWindow.postMessage({
                    jsonrpc: '2.0',
                    method: 'ui/notifications/tool-result',
                    params: {
                      structuredContent: {
                        review_ref: ref,
                        summary: { files: 1, added_lines: 1, removed_lines: 1 },
                        coverage: { complete: true, captured_files: 1, captured_bytes: 256 },
                        files: [{
                          path: 'web/mcp-apps/deep_nested_mobile_bubble_dir/changes_review_card.js',
                          status: 'modified',
                        }],
                        next_offset: null,
                      },
                      _meta: {
                        'com.codepier/binding': {
                          kind: 'changes',
                          catalog_mode: 'unified',
                          project: 'ProjectAlpha',
                          review_ref: ref,
                        },
                      },
                    },
                  }, '*');
                  return;
                }
                if (msg.method === 'tools/call' && 'id' in msg) {
                  frame.contentWindow.postMessage({
                    jsonrpc: '2.0',
                    id: msg.id,
                    result: {
                      structuredContent: {
                        review_ref: ref,
                        diff: diffText,
                        next_offset: null,
                        diff_truncated: false,
                      },
                    },
                  }, '*');
                }
              });
              document.body.append(frame);
              frame.src = 'http://mcp-app.local/changes-v1.html';
            }"""
        )
        app = page.frame_locator("#app-frame")
        expect(app.get_by_role("heading", name="本轮改动")).to_be_visible()
        wrap_btn = app.get_by_role("button", name="自动折行")
        expect(wrap_btn).to_be_visible()
        expect(wrap_btn).to_have_attribute("aria-pressed", "false")
        assert wrap_btn.bounding_box()["height"] >= 40

        app.locator(".files details > summary").first.click()
        pre = app.locator(".files pre")
        expect(pre).to_contain_text("narrowBubbleLongConfigurationIdentifier")
        assert pre.evaluate("el => getComputedStyle(el).overscrollBehaviorX") == "contain"

        for width in (320, 310):
            page.set_viewport_size({"width": width, "height": 480})
            _assert_zero_overflow(page, f"mcp-host-{width}")
            _assert_frame_zero_overflow(app, f"mcp-frame-{width}")

        wrap_btn.scroll_into_view_if_needed()
        wrap_btn.dispatch_event("click")
        expect(wrap_btn).to_have_attribute("aria-pressed", "true")
        is_wrapped = pre.evaluate(
            """el => (
              el.classList.contains('is-wrapped') &&
              getComputedStyle(el).whiteSpace === 'pre-wrap'
            )"""
        )
        assert is_wrapped
        _assert_frame_zero_overflow(app, "mcp-frame-wrapped-310")
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_browser_extension_popup_320x480_geometry_and_input_hygiene(chat_browser_pool, engine):
    expect = _pw().expect
    context = _new_mobile_context(chat_browser_pool, 320, 480, engine=engine)
    context.add_init_script(
        """Object.defineProperty(globalThis, 'chrome', {
          configurable: true,
          writable: true,
          value: {
            runtime: {
              sendMessage: async () => ({
                ok: true,
                data: {
                  extension_id: 'e'.repeat(32),
                  profile_id: 'f'.repeat(32),
                  pool_size: 4,
                  available: 3,
                  leased: 1,
                  connection: '已连接本机桥接',
                  connection_observed_at: 1795590000,
                  origins: ['https://very-long-mobile-safari-extension-origin.example.com:8443'],
                },
              }),
            },
          },
        });"""
    )
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    try:
        ext_dir = ROOT / "web/browser-extension"
        mime_map = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
        }
        for name in ("popup.html", "popup.css", "popup.js", "workspace.js", "page.js"):
            asset_path = ext_dir / name
            body = asset_path.read_text(encoding="utf-8")
            content_type = mime_map[asset_path.suffix]
            page.route(
                f"http://ext.local/{name}",
                lambda route, *, b=body, ct=content_type: route.fulfill(content_type=ct, body=b),
            )
        page.goto("http://ext.local/popup.html", wait_until="domcontentloaded")
        expect(page.locator("#origins li")).to_have_count(1)
        _assert_zero_overflow(page, "extension-popup-320x480")
        assert not _collect_small_inputs(page), _collect_small_inputs(page)
        _assert_hit_test(
            page,
            "#prepare, #release, #allow-form button",
            "popup-buttons",
            min_size=40,
        )
        _assert_hit_test(page, "#origins li button", "popup-revoke", min_size=40)
        origin_attrs = page.locator("#origin").evaluate(
            """el => ({
              autocapitalize: el.getAttribute('autocapitalize'),
              autocorrect: el.getAttribute('autocorrect'),
              spellcheck: el.getAttribute('spellcheck'),
              inputmode: el.getAttribute('inputmode'),
            })"""
        )
        assert origin_attrs == {
            "autocapitalize": "none",
            "autocorrect": "off",
            "spellcheck": "false",
            "inputmode": "url",
        }
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.browser
@pytest.mark.parametrize(
    ("width", "height", "landscape_safe_area"),
    [
        (390, 844, False),
        (844, 390, True),
        (390, 420, False),
        (768, 1024, False),
    ],
)
def test_panel_routes_geometry_inputs_hittest_and_overscroll(
    panel_hub, chat_browser_pool, width, height, landscape_safe_area
):
    context = _new_mobile_context(chat_browser_pool, width, height)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    routes = FULL_ROUTES if (width, height) == (390, 844) else CORE_ROUTES
    try:
        _login_and_check_entry(page, panel_hub)
        if landscape_safe_area:
            page.evaluate(
                """() => {
                  const root = document.documentElement.style;
                  root.setProperty('--safe-top', '12px');
                  root.setProperty('--safe-left', '44px');
                  root.setProperty('--safe-right', '44px');
                  root.setProperty('--safe-bottom', '21px');
                }"""
            )
            resolved_left = page.evaluate(
                """() => getComputedStyle(document.documentElement)
                  .getPropertyValue('--safe-left')
                  .trim()"""
            )
            assert resolved_left == "44px"

        for route in routes:
            _navigate_route(page, route)
            _check_route_invariants(page, route, width, height)

        assert not errors, errors
    finally:
        context.close()


@pytest.mark.browser
@pytest.mark.parametrize(("width", "height"), [(390, 844), (390, 420), (844, 390), (768, 1024)])
def test_coarse_modals_oauth_sheet_and_approval_inbox_hittest(
    panel_hub, chat_browser_pool, width, height
):
    context = _new_mobile_context(chat_browser_pool, width, height)
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    label = f"modals@{width}x{height}"
    try:
        _login_and_check_entry(page, panel_hub, route="vps")
        assert page.evaluate("() => window.matchMedia('(pointer: coarse)').matches") is True
        _check_vps_modal_invariants(page, label)
        _check_oauth_and_approval_invariants(page, panel_hub, label)
        assert not errors, errors
    finally:
        context.close()
