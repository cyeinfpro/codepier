"""Current source UI checks with isolated HTTP/event adapters, not live providers."""
from playwright.sync_api import expect
from tests.browser_support import chat_page
from tests.test_ui_unification import _login, _navigate

def test_palette_exposes_active_option_and_empty_state(chat_page):
    p = chat_page
    p.evaluate("chatPalette()")
    search = p.locator(".chat-palette input")
    expect(search).to_have_attribute("role", "combobox")
    option_id = search.get_attribute("aria-activedescendant")
    expect(p.locator("#" + option_id)).to_have_attribute("aria-selected", "true")
    search.press("ArrowDown")
    assert search.get_attribute("aria-activedescendant") != option_id
    search.fill("no matching action 987654321")
    expect(search).not_to_have_attribute("aria-activedescendant")
    expect(p.locator(".chat-palette-results")).to_have_text("没有匹配结果")
    search.press("Escape")

def test_send_blocker_and_partial_settings_are_visible(chat_page):
    p = chat_page
    p.evaluate("""() => {
      const v=chatView();v.draft='keep me';v.settingOp={receipt:'r',state:'queued',patch:{model:'p/new'}};
      chatSyncChrome();
    }""")
    expect(p.locator("#chat-send")).to_be_disabled()
    expect(p.locator("#chat-send-blocker")).to_contain_text("请等待模型设置确认")
    expect(p.locator("#chat-status")).to_be_hidden()
    p.evaluate("""chatEvent({type:'settings_result',receipt:'r',outcome:'partial',
      actual:{model:{provider:'p',id:'new'},thinkingLevel:'low'}})""")
    expect(p.locator("#chat-setting-state")).to_have_text("部分生效")
    expect(p.locator("#chat-status")).to_contain_text("部分设置已生效")

def test_main_path_order_and_mobile_output_preserves_editor(stack, chat_browser_pool):
    context = chat_browser_pool("chromium").new_context(viewport={"width":390,"height":844})
    p = context.new_page()
    try:
        _login(p, stack)
        _navigate(p, "overview")
        expect(p.locator(".overview-summary")).not_to_have_attribute("open")
        assert p.locator(".workspace-recent-projects").bounding_box()["y"] < p.locator(".overview-summary").bounding_box()["y"]
        _navigate(p, "workbench")
        p.locator('[data-action="read-file"][data-path="src/main.py"]').click()
        expect(p.locator("#code-editor")).to_be_visible()
        p.locator("#code-editor").fill("draft survives\\n")
        assert p.evaluate("S.work.dirty")
        p.locator('[data-ui-pane="output"]').click()
        expect(p.locator("#task-output")).to_be_visible()
        expect(p.locator(".workspace")).to_be_hidden()
        p.locator('[data-ui-pane="editor"]').click()
        expect(p.locator("#task-output")).to_be_hidden()
        expect(p.locator("#code-editor")).to_have_value("draft survives\\n")
        assert p.evaluate("S.work.dirty")
        p.set_viewport_size({"width":1440,"height":1000})
        p.route("**/api/operations/scroll-fixture", lambda route: route.fulfill(
            json={"id":"scroll-fixture","state":"completed","output":"new line\\n" * 300,"pending":False}))
        p.evaluate("""async () => {
          S.work.operation='scroll-fixture';
          const output=document.querySelector('#task-output');
          output.textContent='old line\\n'.repeat(300);output.scrollTop=40;
          await pollTask();
        }""")
        assert p.locator("#task-output").evaluate("(el)=>el.scrollTop") == 40
        expect(p.locator("#task-new-output")).to_be_visible()
        p.locator("#task-new-output").click()
        expect(p.locator("#task-new-output")).to_be_hidden()
        p.evaluate("void navigate('overview')")
        p.get_by_role("button", name="继续离开", exact=True).click()
        expect(p.locator(".overview-continue")).to_contain_text("有未保存草稿")
        p.get_by_role("button", name="返回工作台", exact=True).click()
        expect(p.locator("#code-editor")).to_have_value("draft survives\\n")
        assert p.evaluate("S.work.dirty")

    finally:
        context.close()
