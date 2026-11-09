"""Real-browser contracts for the owner-facing directory authorization editor.

The transport is deterministic; the production module, DOM events, fetch errors,
timers and sessionStorage run unchanged in Chromium and WebKit. The separate
HTTP acceptance module exercises the real Hub/Agent implementation.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect


pytestmark = pytest.mark.browser

DEVICE = "a" * 32
OTHER_DEVICE = "b" * 32
FIRST_SHA = "1" * 64
NEXT_SHA = "2" * 64
ENDPOINT = f"/api/devices/{DEVICE}/roots"
SOURCE = Path(__file__).resolve().parents[1] / "web" / "agent-roots.js"

HARNESS = r"""
window.S = {
  session: 'owner-session', space_id: 'owner-space', identity: {id: 'owner'},
  devices: [], page: 'devices', modalIntent: 0
};
window.$ = (selector, root = document) => root.querySelector(selector);
window.$$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
window.icon = () => '';
window.esc = (value) => String(value).replace(/[&<>"']/g, (char) => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[char]));
window.pause = (duration) => new Promise(resolve => setTimeout(resolve, duration));
window.testCounter = 0;
window.uid = () => 'browser-' + String(++window.testCounter).padStart(12, '0');
window.applied = [];
window.invalidations = 0;
window.scopeCurrent = true;
window.invalidateBasics = () => { window.invalidations++; };
window.api = async (url, options = {}) => {
  const response = await fetch(url, options);
  const value = await response.json();
  if (!response.ok) {
    const detail = value.error || value;
    throw Object.assign(new Error(detail.message || 'Request failed'), detail,
      {status: response.status});
  }
  return value;
};
"""


def snapshot(device=DEVICE, *, state="applied", sha=FIRST_SHA, roots=None):
    return {
        "device_id": device,
        "state": state,
        "config_sha256": sha,
        "roots": roots if roots is not None else [
            {"path": "/projects", "writable": True, "allow_tasks": True}
        ],
    }


def reply(route, value, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(value))


class Transport:
    def __init__(self):
        self.requests = []
        self.snapshots = {DEVICE: snapshot(), OTHER_DEVICE: snapshot(OTHER_DEVICE)}
        self.handler = None

    def __call__(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if path == "/":
            route.fulfill(content_type="text/html", body="<!doctype html><html><body><main id='host'></main></body></html>")
            return
        body = json.loads(request.post_data) if request.post_data else None
        row = {"path": path, "method": request.method, "body": body}
        self.requests.append(row)
        if self.handler and self.handler(route, row):
            return
        for device, value in self.snapshots.items():
            if path == f"/api/devices/{device}/roots" and request.method == "GET":
                reply(route, value)
                return
        reply(route, {"error": {"message": f"Unhandled test request: {request.method} {path}"}}, 404)

    def matching(self, method, path):
        return [row for row in self.requests if row["method"] == method and row["path"] == path]


@pytest.fixture(params=["chromium", "webkit"])
def editor(request, chat_browser_pool):
    browser = chat_browser_pool(request.param)
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    transport = Transport()
    page.route("http://roots.test/**", transport)
    page.add_init_script(HARNESS)
    page.add_init_script(SOURCE.read_text(encoding="utf-8"))
    page.goto("http://roots.test/")
    page.add_style_tag(path=str(SOURCE.with_name("tokens.css")))
    page.add_style_tag(path=str(SOURCE.with_name("styles.css")))
    page.add_style_tag(path=str(SOURCE.with_name("agent-roots.css")))
    try:
        yield page, transport
        assert not errors, errors
    finally:
        context.close()


def mount(page, device=DEVICE, initial=None):
    page.evaluate("""({device, initial}) => {
      window.manager = CodePierRoots.mount(document.querySelector('#host'), device, {
        initial, current: () => window.scopeCurrent,
        onApplied: value => window.applied.push(value)
      });
    }""", {"device": device, "initial": initial})


def ready(page):
    expect(page.locator("[data-roots-state]")).to_have_text("已生效")
    expect(page.locator('[data-roots="add"]')).to_be_enabled()


def fill(page, path="/Volumes/Shared/新增项目"):
    page.locator('[data-roots="add"]').click()
    page.locator('[data-root-field="path"]').fill(path)
    page.locator('[data-root-field="tasks"]').check()


def preview_ok(route, row):
    if row["path"] == ENDPOINT + "/preview":
        reply(route, {"device_id": DEVICE, "valid": True})
        return True
    return False


def test_permission_editing_and_revoke_explain_inheritance(editor):
    page, _transport = editor
    mount(page)
    ready(page)
    page.locator('[data-root-action="upsert"]').click()
    expect(page.locator('[data-root-field="path"]')).to_have_attribute("readonly", "")
    page.locator('[data-root-field="mode"]').select_option("read")
    expect(page.locator('[data-root-field="tasks"]')).not_to_be_checked()
    expect(page.locator('[data-root-field="tasks"]')).to_be_disabled()
    expect(page.locator('[data-roots-scope]')).to_contain_text("只读")
    page.locator('[data-roots="cancel"]').click()
    page.locator('[data-root-action="remove"]').click()
    expect(page.locator('[data-roots-scope]')).to_contain_text("父目录授权仍可能覆盖此路径")
    expect(page.locator('[data-roots="save"]')).to_have_text("撤销此目录授权")
    expect(page.locator('[data-roots-permissions]')).to_be_hidden()


def test_save_is_single_submission_and_applied_callback_waits_for_reload(editor):
    page, transport = editor
    held_preview = []

    def handler(route, row):
        if row["path"] == ENDPOINT + "/preview":
            held_preview.append(route)
            return True
        if row["path"] == ENDPOINT and row["method"] == "POST":
            transport.snapshots[DEVICE] = snapshot(state="pending_reload", sha=NEXT_SHA)
            reply(route, snapshot(state="pending_reload", sha=NEXT_SHA))
            return True
        return False

    transport.handler = handler
    mount(page)
    ready(page)
    fill(page)
    page.locator('[data-roots="save"]').click()
    expect(page.locator('[data-roots-state]')).to_have_text("正在验证")
    page.locator('[data-roots="save"]').dispatch_event("click")
    assert len(transport.matching("POST", ENDPOINT + "/preview")) == 1
    assert not transport.matching("POST", ENDPOINT)
    expect(page.locator('[data-root-field="path"]')).to_be_disabled()
    reply(held_preview.pop(), {"valid": True, "device_id": DEVICE})
    expect(page.locator('[data-roots-state]')).to_have_text("已保存 · 生效待核对")
    assert page.evaluate("window.applied") == []
    assert len(transport.matching("POST", ENDPOINT)) == 1
    submitted = transport.matching("POST", ENDPOINT)[0]["body"]
    assert submitted["path"] == "/Volumes/Shared/新增项目"
    assert submitted["expected_sha256"] == FIRST_SHA
    assert submitted["writable"] and submitted["allow_tasks"]
    transport.snapshots[DEVICE] = snapshot(sha=NEXT_SHA)
    page.locator('[data-roots="refresh"]').click()
    expect(page.locator('[data-roots-feedback]')).to_have_text("目录授权已生效。")
    assert len(page.evaluate("window.applied")) == 1
    expect(page.locator('[data-roots-editor]')).to_be_hidden()
    assert page.evaluate("sessionStorage.length") == 0


def test_preview_failure_preserves_draft_and_never_submits_write(editor):
    page, transport = editor

    def handler(route, row):
        if row["path"] == ENDPOINT + "/preview":
            reply(route, {"error": {"code": "ROOT_MISSING", "message": "共享目录暂不可用"}}, 409)
            return True
        return False

    transport.handler = handler
    mount(page)
    ready(page)
    fill(page)
    page.locator('[data-roots="save"]').click()
    expect(page.locator('[data-roots-feedback]')).to_have_text("共享目录暂不可用")
    expect(page.locator('[data-root-field="path"]')).to_have_value("/Volumes/Shared/新增项目")
    expect(page.locator('[data-root-field="tasks"]')).to_be_checked()
    expect(page.locator('[data-roots="save"]')).to_be_enabled()
    assert not transport.matching("POST", ENDPOINT)
    assert page.evaluate("sessionStorage.length") == 0


@pytest.mark.parametrize("conflict_code", ["SETTINGS_CHANGED", "CONFIG_CONFLICT"])
def test_config_conflict_refreshes_version_without_losing_draft(editor, conflict_code):
    page, transport = editor

    def handler(route, row):
        if preview_ok(route, row):
            return True
        if row["path"] == ENDPOINT and row["method"] == "POST":
            transport.snapshots[DEVICE] = snapshot(sha=NEXT_SHA)
            reply(route, {"error": {"code": conflict_code, "message": "配置已更新，请重新核对"}}, 409)
            return True
        return False

    transport.handler = handler
    mount(page)
    ready(page)
    fill(page)
    with page.expect_response(lambda response: response.request.method == "POST" and
                              urlsplit(response.url).path == ENDPOINT):
        page.locator('[data-roots="save"]').click()
    expect(page.locator('[data-roots-feedback]')).to_have_text("配置已更新，请重新核对")
    expect(page.locator('[data-roots="save"]')).to_be_enabled()
    expect(page.locator('[data-root-field="path"]')).to_have_value("/Volumes/Shared/新增项目")
    expect(page.locator('[data-root-field="tasks"]')).to_be_checked()
    with page.expect_response(lambda response: response.request.method == "POST" and
                              urlsplit(response.url).path == ENDPOINT):
        page.locator('[data-roots="save"]').click()
    expect(page.locator('[data-roots-feedback]')).to_have_text("配置已更新，请重新核对")
    assert len(transport.matching("POST", ENDPOINT)) == 2
    assert transport.matching("POST", ENDPOINT)[1]["body"]["expected_sha256"] == NEXT_SHA
    assert page.evaluate("window.applied") == []


@pytest.mark.parametrize("receipt_state", ["succeeded", "needs_review"])
def test_uncertain_config_commit_keeps_recovery_identity(editor, receipt_state):
    page, transport = editor
    saved_keys = []
    observed = snapshot(sha=NEXT_SHA, roots=[{
        "path": "/verified-current-configuration", "writable": True, "allow_tasks": True,
    }])
    uncertain = {
        **observed, "state": "needs_review", "code": "ROOTS_SAVE_UNCERTAIN",
        "outcome_confirmed": False, "saved": True,
        "message": "配置已写入，但持久落盘尚未确认，请核对原保存回执",
    }

    def handler(route, row):
        if preview_ok(route, row):
            return True
        if row["path"] == ENDPOINT and row["method"] == "POST":
            saved_keys.append(row["body"]["idempotency_key"])
            transport.snapshots[DEVICE] = observed
            reply(route, {"operation_id": "c" * 32, "state": receipt_state,
                          "result": {"ok": True, "data": uncertain}})
            return True
        if row["path"].startswith(ENDPOINT + "/requests/"):
            assert row["path"].rsplit("/", 1)[-1] == saved_keys[0]
            reply(route, {"operation_id": "c" * 32, "state": receipt_state,
                          "result": {"ok": True, "data": uncertain}})
            return True
        return False

    transport.handler = handler
    mount(page)
    ready(page)
    fill(page)
    page.locator('[data-roots="save"]').click()
    expect(page.locator('[data-roots="recover"]')).to_be_enabled()
    expect(page.locator('[data-roots-list]')).to_contain_text(
        "/verified-current-configuration", timeout=7000
    )
    expect(page.locator('#host')).to_have_attribute("data-submission", "uncertain")
    expect(page.locator('[data-roots="save"]')).to_be_disabled()
    expect(page.locator('[data-roots="add"]')).to_be_disabled()
    expect(page.locator('[data-roots-recovery]')).to_be_visible()
    expect(page.locator('[data-roots-feedback]')).not_to_have_text("目录授权已生效。")
    assert page.evaluate("window.applied") == []
    stored = page.evaluate("sessionStorage.getItem('codepier-roots-pending-v1:owner:owner-space')")
    assert saved_keys[0] in stored
    assert len(transport.matching("POST", ENDPOINT)) == 1


def test_lost_save_response_recovers_original_receipt_after_page_reload(editor):
    page, transport = editor
    keys = []

    def handler(route, row):
        if preview_ok(route, row):
            return True
        if row["path"] == ENDPOINT and row["method"] == "POST":
            keys.append(row["body"]["idempotency_key"])
            route.abort("failed")
            return True
        if row["path"].startswith(ENDPOINT + "/requests/"):
            assert row["path"].rsplit("/", 1)[-1] == keys[0]
            reply(route, {"operation": {
                "operation_id": "c" * 32, "state": "succeeded",
                "result": {"ok": True, "data": snapshot(sha=NEXT_SHA)},
            }})
            return True
        return False

    transport.handler = handler
    mount(page)
    ready(page)
    fill(page)
    page.locator('[data-roots="save"]').click()
    expect(page.locator('[data-roots-state]')).to_have_text("保存结果待核对")
    expect(page.locator('[data-roots="save"]')).to_be_disabled()
    stored = page.evaluate("sessionStorage.getItem('codepier-roots-pending-v1:owner:owner-space')")
    assert keys[0] in stored
    assert "/Volumes/Shared" not in stored
    page.reload()
    mount(page)
    expect(page.locator('[data-roots-feedback]')).to_have_text("目录授权已生效。")
    assert len(transport.matching("POST", ENDPOINT)) == 1
    assert transport.matching("GET", ENDPOINT + "/requests/" + keys[0])
    assert page.evaluate("sessionStorage.length") == 0
    assert page.evaluate("window.applied") == [], "Reopening a receipt must not replay an old form submission"


def test_unknown_save_outcome_keeps_receipt_and_blocks_duplicate_write(editor):
    page, transport = editor

    def handler(route, row):
        if preview_ok(route, row):
            return True
        if (row["path"] == ENDPOINT and row["method"] == "POST") or \
                row["path"].startswith(ENDPOINT + "/requests/"):
            reply(route, {
                "operation_id": "c" * 32, "state": "needs_review",
                "result": {"ok": False, "error": {
                    "code": "CONFIG_OUTCOME_UNKNOWN", "message": "网络中断，配置结果需核对",
                }},
            })
            return True
        return False

    transport.handler = handler
    mount(page)
    ready(page)
    fill(page)
    page.locator('[data-roots="save"]').click()
    expect(page.locator('[data-roots-state]')).to_have_text("保存结果待核对")
    page.locator('[data-roots="recover"]').click()
    expect(page.locator('[data-roots-state]')).to_have_text("保存结果待核对")
    expect(page.locator('[data-roots="save"]')).to_be_disabled()
    expect(page.locator('[data-roots="add"]')).to_be_disabled()
    expect(page.locator('[data-roots-recovery]')).to_be_visible()
    assert len(transport.matching("POST", ENDPOINT)) == 1
    assert page.evaluate("sessionStorage.length") == 1
    assert page.evaluate("window.applied") == []


@pytest.mark.parametrize("state,label", [
    ("offline", "节点离线"),
    ("unsupported", "需要更新 Agent"),
    ("disabled", "节点已停用"),
])
def test_unavailable_nodes_never_enable_authorization(editor, state, label):
    page, transport = editor
    transport.snapshots[DEVICE] = snapshot(state=state)
    mount(page, initial={"path": "/Volumes/Shared/Draft", "writable": True, "allow_tasks": True})
    expect(page.locator('[data-roots-state]')).to_have_text(label)
    expect(page.locator('[data-root-field="path"]')).to_have_value("/Volumes/Shared/Draft")
    expect(page.locator('[data-roots="add"]')).to_be_disabled()
    expect(page.locator('[data-roots="save"]')).to_be_disabled()
    assert not [row for row in transport.requests if row["method"] == "POST"]


def test_late_node_snapshot_cannot_replace_new_node_editor(editor):
    page, transport = editor
    held = []

    def handler(route, row):
        if row["path"] == ENDPOINT and row["method"] == "GET":
            held.append(route)
            return True
        return False

    transport.handler = handler
    transport.snapshots[OTHER_DEVICE] = snapshot(OTHER_DEVICE, roots=[{
        "path": "/other-node", "writable": False, "allow_tasks": False,
    }])
    mount(page)
    page.wait_for_function("document.querySelector('[data-roots-state]').textContent === '正在读取'")
    mount(page, OTHER_DEVICE)
    ready(page)
    reply(held.pop(), snapshot(roots=[{"path": "/stale-node", "writable": True}]))
    expect(page.locator('[data-roots-list]')).to_contain_text("/other-node")
    expect(page.locator('[data-roots-list]')).not_to_contain_text("/stale-node")
    assert page.evaluate("window.applied") == []


@pytest.mark.parametrize("boundary", ["session", "space_id", "scopeCurrent"])
def test_changed_scope_stops_preview_before_authorization_write(editor, boundary):
    page, transport = editor
    held = []

    def handler(route, row):
        if row["path"] == ENDPOINT + "/preview":
            held.append(route)
            return True
        return False

    transport.handler = handler
    mount(page)
    ready(page)
    fill(page)
    page.locator('[data-roots="save"]').click()
    expect(page.locator('[data-roots-state]')).to_have_text("正在验证")
    page.evaluate("""boundary => {
      if (boundary === 'scopeCurrent') window.scopeCurrent = false;
      else S[boundary] = 'changed-owner-scope';
    }""", boundary)
    reply(held.pop(), {"device_id": DEVICE, "valid": True})
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    assert not transport.matching("POST", ENDPOINT)
    assert page.evaluate("window.applied") == []


def test_wrong_device_inside_success_receipt_preserves_original_recovery_key(editor):
    page, transport = editor

    def handler(route, row):
        if preview_ok(route, row):
            return True
        if row["path"] == ENDPOINT and row["method"] == "POST":
            reply(route, {
                "operation_id": "c" * 32, "state": "succeeded",
                "result": {"ok": True, "data": snapshot(OTHER_DEVICE, sha=NEXT_SHA)},
            })
            return True
        return False

    transport.handler = handler
    mount(page)
    ready(page)
    fill(page)
    page.locator('[data-roots="save"]').click()
    expect(page.locator('[data-roots-state]')).to_have_text("保存结果待核对")
    expect(page.locator('[data-roots-recovery]')).to_be_visible()
    expect(page.locator('[data-roots="save"]')).to_be_disabled()
    assert page.evaluate("sessionStorage.length") == 1
    assert page.evaluate("window.applied") == []


def test_directory_names_are_text_and_mobile_editor_has_no_horizontal_overflow(editor):
    page, transport = editor
    path = '/Volumes/<img src=x onerror="window.injected=true">/' + "很长的共享目录名称" * 12
    transport.snapshots[DEVICE] = snapshot(roots=[{
        "path": path, "writable": True, "allow_tasks": False,
    }])
    mount(page)
    ready(page)
    expect(page.locator('[data-roots-list] code')).to_have_text(path)
    assert page.locator('[data-roots-list] img').count() == 0
    assert page.evaluate("window.injected") is None
    page.locator('[data-root-action="remove"]').click()
    expect(page.locator('[data-roots-scope] code')).to_have_text(path)
    assert page.locator('[data-roots-scope] img').count() == 0
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


@pytest.fixture
def project_editor(editor):
    page, transport = editor
    page.add_script_tag(path=str(SOURCE.parent / "core" / "bundle.js"))
    page.evaluate("""({device, other}) => {
      document.body.innerHTML = '<div id="app"></div><div id="modal-root"></div><div id="toasts"></div>';
      S.page = 'projects';
      S.projects = [];
      S.devices = [{id: device, name: 'Main node', online: true},
        {id: other, name: 'Other node', online: true}];
      S.work = {};
      window.toasts = [];
      window.toast = value => window.toasts.push(value);
      window.renderPage = async () => {};
      window.loadBasics = async () => {};
      const dialogs = CP.ui.createModal({state: S, labelFields: () => {},
        trapTab: () => {}, focusable: root => [...root.querySelectorAll('button, input, select')]});
      window.modal = (...args) => dialogs.modal(...args);
      window.closeModal = (...args) => dialogs.closeModal(...args);
      window.buttons = (...args) => CP.ui.buttons(...args);
      window.busy = (button, fn) => CP.ui.busy(button, fn, toast);
      window.uiConfirmAction = options => CP.ui.confirmSheet(options);
    }""", {"device": DEVICE, "other": OTHER_DEVICE})
    app = SOURCE.with_name("app.js").read_text(encoding="utf-8")
    start = app.index("async function projectModal(")
    end = app.index("\nfunction resetWork(", start)
    page.add_script_tag(content=app[start:end])
    page.evaluate("device => projectModal(null, device)", DEVICE)
    page.locator('[name="alias"]').fill("NAS Project")
    page.locator('[name="root"]').fill("/Volumes/Shared/Project")
    page.locator('#project-form details > summary').click()
    page.locator('[name="description"]').fill("Keep this draft")
    page.locator('[name="access"]').select_option('full')
    return page, transport


def test_mapping_draft_authorization_then_save_is_one_continuous_flow(project_editor):
    page, transport = project_editor

    def handler(route, row):
        if row["path"] == "/api/projects":
            if len(transport.matching("POST", "/api/projects")) == 1:
                reply(route, {"error": {"code": "ROOT_NOT_ALLOWED", "message": "目录未授权"}}, 403)
            else:
                reply(route, {"id": "mapped-project"})
            return True
        if preview_ok(route, row):
            return True
        if row["path"] == ENDPOINT and row["method"] == "POST":
            reply(route, snapshot(sha=NEXT_SHA))
            return True
        return False

    transport.handler = handler
    page.locator('#save-project').click()
    expect(page.locator('.ui-sheet')).to_contain_text("/Volumes/Shared/Project")
    expect(page.locator('[name="alias"]')).to_have_value("NAS Project")
    expect(page.locator('[name="description"]')).to_have_value("Keep this draft")
    page.locator('.ui-sheet button[type="submit"]').click()
    expect(page.locator('.modal')).to_have_count(0)
    attempts = transport.matching("POST", "/api/projects")
    assert len(attempts) == 2
    first, second = (row["body"] for row in attempts)
    assert first["idempotency_key"] != second["idempotency_key"]
    assert {key: value for key, value in first.items() if key != "idempotency_key"} == {
        key: value for key, value in second.items() if key != "idempotency_key"
    }
    assert len(transport.matching("POST", ENDPOINT)) == 1
    assert page.evaluate("window.toasts") == ["项目已保存"]


@pytest.mark.parametrize("changed_field", ["alias", "device_id"])
def test_changed_mapping_draft_is_never_automatically_submitted(project_editor, changed_field):
    page, transport = project_editor
    held = []

    def handler(route, row):
        if row["path"] == "/api/projects":
            reply(route, {"error": {"code": "ROOT_NOT_ALLOWED", "message": "目录未授权"}}, 403)
            return True
        if preview_ok(route, row):
            return True
        if row["path"] == ENDPOINT and row["method"] == "POST":
            held.append(route)
            return True
        return False

    transport.handler = handler
    page.locator('#save-project').click()
    page.locator('.ui-sheet button[type="submit"]').click()
    expect(page.locator('[data-roots-state]')).to_have_text("正在保存")
    if changed_field == "alias":
        page.locator('[name="alias"]').fill("Changed draft")
    else:
        page.locator('[name="device_id"]').select_option(OTHER_DEVICE)
    reply(held.pop(), snapshot(sha=NEXT_SHA))
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    expect(page.locator('.modal')).to_have_count(1)
    expect(page.locator(f'[name="{changed_field}"]')).to_have_value(
        "Changed draft" if changed_field == "alias" else OTHER_DEVICE
    )
    assert len(transport.matching("POST", "/api/projects")) == 1


def test_pending_mapping_validation_reuses_original_key_and_finishes(project_editor):
    page, transport = project_editor
    held = []

    def handler(route, row):
        if row["path"] == "/api/projects" and row["method"] == "POST":
            if len(transport.matching("POST", "/api/projects")) == 1:
                reply(route, {"error": {"code": "VALIDATION_PENDING", "message": "等待节点验证"}}, 409)
            else:
                held.append(route)
            return True
        return False

    transport.handler = handler
    page.expose_function("heldPendingMappingSaveCount", lambda: len(held))
    page.evaluate("""() => {
      const button = document.querySelector('#save-project'), handler = button.onclick;
      button.onclick = event => {
        button.onclick = handler;
        const completion = handler.call(button, event);
        completion.then(() => {
          window.initialPendingMappingState = {
            text: document.querySelector('#project-form > [role="status"]').textContent,
            enabled: !button.disabled
          };
        });
        return completion;
      };
    }""")
    page.locator('#save-project').click()
    # Keep the real automatic retry in flight. Observing the held HTTP route
    # avoids racing Python assertions against the production 1800 ms timer.
    page.evaluate("""async () => {
      const deadline = performance.now() + 7000;
      while (!(await window.heldPendingMappingSaveCount())) {
        if (performance.now() >= deadline) throw new Error('Automatic mapping retry was not received');
        await new Promise(resolve => setTimeout(resolve, 25));
      }
    }""")
    assert page.evaluate("window.initialPendingMappingState") == {
        "text": "正在等待电脑检查目录…",
        "enabled": True,
    }
    expect(page.locator('.modal')).to_have_count(1)
    expect(page.locator('#save-project')).to_be_disabled()
    attempts = transport.matching("POST", "/api/projects")
    assert len(attempts) == 2
    assert len(held) == 1
    first = attempts[0]["body"]
    assert first["idempotency_key"].startswith("project-save-")
    assert first == {
        "alias": "NAS Project", "device_id": DEVICE,
        "root": "/Volumes/Shared/Project", "description": "Keep this draft",
        "mode": "write", "allow_tasks": True,
        "idempotency_key": first["idempotency_key"],
    }
    assert attempts[1]["body"] == first
    assert page.evaluate("window.toasts") == []
    reply(held.pop(), {"id": "mapped-project"})
    expect(page.locator('.modal')).to_have_count(0, timeout=7000)
    assert len(transport.matching("POST", "/api/projects")) == 2
    assert page.evaluate("window.toasts") == ["项目已保存"]


def test_space_change_stops_pending_mapping_validation_retry(project_editor):
    page, transport = project_editor

    def handler(route, row):
        if row["path"] == "/api/projects" and row["method"] == "POST":
            reply(route, {"error": {"code": "VALIDATION_PENDING", "message": "等待节点验证"}}, 409)
            return True
        return False

    transport.handler = handler
    page.evaluate("""() => {
      const button = document.querySelector('#save-project'), handler = button.onclick;
      button.onclick = event => {
        button.onclick = handler;
        const completion = handler.call(button, event);
        window.afterPendingMappingRetryWindow = completion.then(() => {
          window.initialPendingMappingState = {
            text: document.querySelector('#project-form > [role="status"]').textContent,
            enabled: !button.disabled, space: S.space_id
          };
          // busy has restored the button, and this microtask runs before the
          // scheduled retry. Scope changes therefore cannot race the test host.
          S.space_id = 'different-space';
          // Let the unchanged production timer get its turn after scope changed.
          return new Promise(resolve => setTimeout(resolve, 2100));
        });
        return completion;
      };
    }""")
    page.locator('#save-project').click()
    page.evaluate("() => window.afterPendingMappingRetryWindow")
    assert page.evaluate("window.initialPendingMappingState") == {
        "text": "正在等待电脑检查目录…",
        "enabled": True,
        "space": "owner-space",
    }
    assert page.evaluate("S.space_id") == "different-space"
    expect(page.locator('.modal')).to_have_count(1)
    expect(page.locator('#save-project')).to_be_enabled()
    assert len(transport.matching("POST", "/api/projects")) == 1
    assert page.evaluate("window.toasts") == []


def test_late_mapping_save_keeps_edits_made_while_request_was_running(project_editor):
    page, transport = project_editor
    held = []

    def handler(route, row):
        if row["path"] == "/api/projects":
            held.append(route)
            return True
        return False

    transport.handler = handler
    page.locator('#save-project').click()
    expect(page.locator('#save-project')).to_be_disabled()
    page.locator('[name="alias"]').fill("Newer unsaved draft")
    reply(held.pop(), {"id": "mapped-project"})
    expect(page.locator('#save-project')).to_be_enabled()
    expect(page.locator('.modal')).to_have_count(1)
    expect(page.locator('[name="alias"]')).to_have_value("Newer unsaved draft")
    assert len(transport.matching("POST", "/api/projects")) == 1


@pytest.mark.parametrize("failure", ["cancel", "offline", "preview", "uncertain"])
def test_confirmed_project_directory_grant_preserves_failure_boundaries(project_editor, failure):
    page, transport = project_editor
    if failure == "offline":
        transport.snapshots[DEVICE] = snapshot(state="offline", sha=None)

    def handler(route, row):
        path = row["path"]
        if path == "/api/projects":
            reply(route, {"error": {"code": "ROOT_NOT_ALLOWED", "message": "目录未授权"}}, 403)
            return True
        if failure == "preview" and path == ENDPOINT + "/preview":
            reply(route, {"valid": False, "reason": "目录预检失败"})
            return True
        if failure == "uncertain" and path == ENDPOINT and row["method"] == "POST":
            reply(route, {"error": {"code": "NETWORK_UNCERTAIN", "message": "网络中断"}}, 503)
            return True
        if failure == "uncertain" and path.startswith(ENDPOINT + "/requests/"):
            reply(route, {"state": "not_found"})
            return True
        return preview_ok(route, row)

    transport.handler = handler
    page.locator("#save-project").click()
    if failure == "cancel":
        page.locator(".ui-sheet .ghost").click()
        expect(page.locator("#project-roots-manager")).to_be_hidden()
        expect(page.locator("#save-project")).to_be_enabled()
    else:
        page.locator('.ui-sheet button[type="submit"]').click()
        if failure == "offline":
            expect(page.locator("[data-roots-state]")).to_contain_text("离线")
            expect(page.locator('[data-roots="save"]')).to_be_disabled()
        elif failure == "preview":
            expect(page.locator("[data-roots-feedback]")).to_contain_text("目录预检失败")
        else:
            expect(page.locator("[data-roots-recovery]")).to_be_visible()
            expect(page.locator('[data-roots="save"]')).to_be_disabled()
            page.wait_for_function("document.querySelector('[data-roots-state]').textContent.includes('未确认')")
    assert len(transport.matching("POST", "/api/projects")) == 1
    assert len(transport.matching("POST", ENDPOINT)) == int(failure == "uncertain")
    if failure == "uncertain":
        key = transport.matching("POST", ENDPOINT)[0]["body"]["idempotency_key"]
        assert transport.matching("GET", f"{ENDPOINT}/requests/{key}")
