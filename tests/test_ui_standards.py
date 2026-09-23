"""2026-09-16 UI standards: isolated data, real Chromium/WebKit, no model calls."""
from pathlib import Path
from scripts.check_release import check_web_assets
from shared.util import VERSION
import time

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.test_chat_browser import chat_page
from tests.test_ui_unification import _login, _layout, _set_scheme

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/evidence/ui-standards-20260916/screenshots"


def test_dimension_tokens_and_cache_contract():
    tokens = (ROOT / "web/tokens.css").read_text()
    for name in ["space-1", "space-6", "control-height", "control-compact", "touch-target", "text-input", "panel-inset", "focus-width"]:
        assert f"--ui-{name}:" in tokens
    html = (ROOT / "web/index.html").read_text()
    for asset in ["tokens.css", "styles.css", "workspace.css", "computer.css", "chat.css", "app.js", "ui.js", "integrations.js", "integrations.css"]:
        assert asset in check_web_assets(ROOT, VERSION), asset
    assert "maximum-scale" not in html and "user-scalable=no" not in html


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_field_semantics_invalid_reset_and_clear_search(stack, engine):
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        _login(page, stack, "projects")
        page.fill("#project-query", "no-match-ui-standards")
        page.get_by_role("button", name="清除筛选").click()
        expect(page.locator("#project-query")).to_have_value("")
        expect(page.locator("#project-query")).to_be_focused()
        expect(page.locator("#project-no-match")).to_be_hidden()
        page.locator('[data-action="edit-project"]').first.click()
        expect(page.locator("#project-form")).to_be_visible()
        alias = page.locator('#project-form input[name="alias"]')
        root = page.locator('#project-form input[name="root"]')
        assert alias.get_attribute("id")
        expect(page.locator(f'label[for="{alias.get_attribute("id")}"] .field-required')).to_have_text("必填")
        hint = root.get_attribute("aria-describedby")
        assert hint and "allowed_roots" in page.locator("#" + hint).inner_text()
        page.evaluate("uiLabelFields(document.querySelector('.modal'))")
        expect(root).to_have_attribute("aria-describedby", hint)
        expect(page.locator(f'label[for="{alias.get_attribute("id")}"] .field-required')).to_have_count(1)
        alias.fill("")
        alias.evaluate("el=>el.reportValidity()")
        expect(alias).to_have_attribute("aria-invalid", "true")
        alias.fill("Valid alias")
        assert alias.get_attribute("aria-invalid") is None
        assert alias.evaluate("el=>getComputedStyle(el).fontSize") == "16px"
        hidden = page.evaluate("""() => {
          const field=document.createElement('div');
          field.innerHTML='<button id="negative-tab" tabindex="-1">Skip</button><button id="invisible" style="visibility:hidden">Skip</button><input type="hidden"><fieldset disabled><input id="fieldset-disabled"></fieldset>';
          document.querySelector('.modal-body').append(field);
          return uiFocusable(document.querySelector('.modal')).map(el=>el.id);
        }""")
        assert not set(hidden) & {"negative-tab", "invisible", "fieldset-disabled"}
        page.keyboard.press("Escape")
        assert not page.locator("#app").evaluate("el=>el.inert")
        page.evaluate("newGrant()")
        groups = page.locator('.check-list[role="group"]')
        expect(groups).to_have_count(2)
        for group in groups.all():
            label_id = group.get_attribute("aria-labelledby")
            assert label_id
            assert not page.locator("#" + label_id).get_attribute("for")
        browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_busy_preserves_geometry_name_nodes_and_duplicate_guard(stack, engine):
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        _login(page, stack, "settings")
        button = page.locator('#settings-form button[type="submit"]')
        original = button.bounding_box()
        page.evaluate("""() => {
          const b=document.querySelector('#settings-form button[type="submit"]');
          window.savedButton=b;window.savedButtonChild=b.firstChild;window.busyCalls=0;
          busy(b,()=>{busyCalls++;return new Promise(resolve=>window.finishBusy=resolve)});
          busy(b,()=>{busyCalls++;});
        }""")
        expect(button).to_be_disabled()
        expect(button).to_have_attribute("aria-busy", "true")
        assert button.get_attribute("class").endswith("ui-busy")
        expect(button).to_have_accessible_name("保存地址")
        current = button.bounding_box()
        assert abs(original["width"] - current["width"]) <= 1
        assert abs(original["height"] - current["height"]) <= 1
        assert page.evaluate("busyCalls") == 1
        page.evaluate("finishBusy()")
        expect(button).to_be_enabled()
        assert button.get_attribute("aria-busy") is None
        assert page.evaluate("savedButton.firstChild===savedButtonChild")
        assert button.evaluate("el=>el.style.minWidth") == ""
        browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_command_first_arrow_node_identity_and_filter_reset(stack, engine):
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        _login(page, stack)
        page.evaluate("""() => {
          const original=window.api;
          window.api=(path,...args)=>path==='/api/projects'
            ? new Promise(resolve=>window.releaseProjects=()=>{
                window.api=original;
                resolve({projects:[...S.projects,{id:'refresh-probe',alias:'刷新项目',device_name:'测试'}]});
              })
            : original(path,...args);
        }""")
        page.locator('[data-ui="command"]').click()
        query = page.locator("#command-query")
        page.wait_for_function("typeof window.releaseProjects==='function'")
        expect(query).to_have_attribute("role", "combobox")
        query.press("ArrowDown")
        expect(query).to_have_attribute("aria-activedescendant", "command-option-0")
        expect(page.locator("#command-option-0")).to_have_attribute("aria-selected", "true")
        page.evaluate("window.firstOption=document.querySelector('#command-option-0')")
        query.press("ArrowDown")
        expect(query).to_have_attribute("aria-activedescendant", "command-option-1")
        page.evaluate("""async () => {
          window.releaseProjects();
          await new Promise(resolve=>setTimeout(resolve,0));
        }""")
        expect(page.get_by_role("option", name="刷新项目")).to_be_visible()
        assert page.evaluate("firstOption===document.querySelector('#command-option-0')")
        query.fill("no-such-location")
        assert query.get_attribute("aria-activedescendant") is None
        query.press("Enter")
        expect(page.locator(".command-dialog")).to_be_visible()
        query.fill("系统设置")
        query.press("Enter")
        expect(page.locator("#page h1")).to_have_text("系统设置")
        browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("width,height", [(320, 568), (390, 844), (768, 1024), (1440, 1000), (667, 375)])
def test_long_modal_layout_targets_and_focus(stack, engine, scheme, width, height):
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        page = browser.new_page(viewport={"width": width, "height": height}, color_scheme=scheme)
        page.emulate_media(reduced_motion="reduce")
        _login(page, stack, "settings")
        _set_scheme(page, scheme)
        page.evaluate("""() => {
          const fields=Array.from({length:12},(_,i)=>`<div class="field"><label>字段 ${i+1}</label><input id="qa-field-${i}" required value="验证布局，不会提交数据"><small>说明文字与输入框关联，检查滚动和键盘焦点。</small></div>`).join('');
          modal('统一表单与长内容滚动',fields,buttons('qa-save','验证并保存'));
        }""")
        expect(page.locator("#qa-field-0")).to_be_focused()
        _layout(page, f"{engine}-{scheme}-{width}-modal")
        for selector in [".modal-header", ".modal-footer", "#qa-save"]:
            rect = page.locator(selector).bounding_box()
            assert rect and rect["y"] >= 0 and rect["y"] + rect["height"] <= height + 1, (selector, rect)
        if width <= 760:
            for selector in ["#qa-save", '.modal [data-action="close-modal"]']:
                for button in page.locator(selector).all():
                    assert button.bounding_box()["height"] >= 44
        page.locator("#qa-field-11").focus()
        # WebKit finishes native focus scrolling asynchronously (observed ~200ms).
        # Assert the actual end state, not an arbitrary synchronous browser frame.
        deadline = time.monotonic() + 5
        while True:
            field_box = page.locator("#qa-field-11").bounding_box()
            body_box = page.locator(".modal-body").bounding_box()
            if (field_box and body_box and field_box["y"] >= body_box["y"] - 1
                    and field_box["y"] + field_box["height"] <= body_box["y"] + body_box["height"] + 1):
                break
            assert time.monotonic() < deadline, (field_box, body_box)
            page.wait_for_timeout(50)
        field_box = page.locator("#qa-field-11").bounding_box()
        body_box = page.locator(".modal-body").bounding_box()
        assert field_box["y"] >= body_box["y"] - 1
        assert field_box["y"] + field_box["height"] <= body_box["y"] + body_box["height"] + 1
        page.locator("#qa-save").focus()
        page.keyboard.press("Tab")
        expect(page.locator('.modal-header [data-action="close-modal"]')).to_be_focused()
        page.screenshot(path=str(OUT / f"{engine}-{scheme}-{width}-modal.png"), animations="disabled")
        page.keyboard.press("Escape")
        expect(page.locator(".modal")).to_have_count(0)
        browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_mobile_toast_does_not_cover_dock_and_errors_are_explicit(stack, engine):
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        _login(page, stack)
        page.evaluate("toast('操作完成：保留当前工作区')")
        expect(page.locator('.toast[role="status"]')).to_contain_text("操作完成")
        toast_box = page.locator(".toast").bounding_box()
        dock_box = page.locator(".mobile-dock").bounding_box()
        assert toast_box["y"] + toast_box["height"] <= dock_box["y"]
        page.locator(".toast-dismiss").click()
        expect(page.locator(".toast")).to_have_count(0)
        timer_count = page.evaluate("""() => {
          const original=window.setTimeout;let calls=0;
          window.setTimeout=(...args)=>{calls++;return original(...args);};
          try {toast('<img src=x onerror=alert(1)>',true);toast('<img src=x onerror=alert(1)>',true);return calls;}
          finally {window.setTimeout=original;}
        }""")
        assert timer_count == 0
        expect(page.locator('.toast[role="alert"]')).to_contain_text("<img")
        expect(page.locator(".toast img")).to_have_count(0)
        expect(page.locator(".toast")).to_have_count(1)
        page.evaluate("endSession()")
        expect(page.locator(".toast")).to_have_count(0)
        browser.close()


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_password_mismatch_stays_at_field_without_submission(stack, engine):
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch()
        page = browser.new_page()
        submitted = []
        page.on("request", lambda req: submitted.append(req.url) if req.method == "POST" and "/api/account/password" in req.url else None)
        _login(page, stack, "settings")
        page.fill('[name="current_password"]', "only-a-test-password")
        page.fill('[name="new_password"]', "new-test-password-1")
        page.fill('[name="confirm_password"]', "new-test-password-2")
        page.locator('#password-form button[type="submit"]').click()
        expect(page.locator('[name="confirm_password"]')).to_be_focused()
        expect(page.locator("#password-match-error")).to_be_visible()
        expect(page.locator('[name="confirm_password"]')).to_have_attribute("aria-invalid", "true")
        page.fill('[name="confirm_password"]', "new-test-password-1")
        expect(page.locator("#password-match-error")).to_be_hidden()
        assert not submitted
        browser.close()


@pytest.mark.parametrize("chat_page", ["chromium", "webkit"], indirect=True)
@pytest.mark.parametrize("width,height", [(320, 568), (390, 844), (667, 375)])
def test_chat_touch_contract_and_search_font(chat_page, width, height):
    page = chat_page
    page.set_viewport_size({"width": width, "height": height})
    page.emulate_media(reduced_motion="reduce")
    # Resize events are asynchronous. Wait for the real page to settle, without
    # invoking its resize handler or relaxing the viewport geometry contract.
    page.wait_for_function("() => {const r=document.querySelector('#chat-root').getBoundingClientRect();return r.y>=0&&r.bottom<=innerHeight+1;}")
    for selector in ["#chat-send", "#chat-attach", "#chat-model-picker"]:
        rect = page.locator(selector).bounding_box()
        assert rect["height"] >= 44, (selector, rect)
        assert rect["x"] >= 0 and rect["x"] + rect["width"] <= width + 1, (selector, rect)
        assert rect["y"] >= 0 and rect["y"] + rect["height"] <= height + 1, (selector, rect)
    page.click("#chat-model-picker")
    expect(page.locator("#chat-model-search")).to_be_visible()
    assert page.locator("#chat-model-search").evaluate("el=>getComputedStyle(el).fontSize") == "16px"
    page.locator("#chat-popover-close").click()
    page.fill("#chat-compose", "保留草稿，不发送")
    page.screenshot(path=str(OUT / f"chat-{page.context.browser.browser_type.name}-{width}.png"), animations="disabled")
    assert not page.evaluate("requests.some(r=>r.path.endsWith('/start'))")
