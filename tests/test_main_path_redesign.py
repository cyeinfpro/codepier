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
        assert p.locator(".overview-summary").evaluate("e=>e.tagName") == "SECTION"
        expect(p.locator(".overview-summary .workspace-stats")).to_be_visible()
        assert p.locator(".overview-summary").bounding_box()["y"] < p.locator(".workspace-recent-projects").bounding_box()["y"]
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
        expect(p.locator(".overview-continue")).to_have_count(0)
        _navigate(p, "workbench")
        expect(p.locator("#code-editor")).to_have_value("draft survives\\n")
        assert p.evaluate("S.work.dirty")

    finally:
        context.close()


import pytest

@pytest.mark.parametrize('width,theme', [(390,'light'),(390,'dark'),(1440,'light'),(1440,'dark')])
def test_overview_statistics_always_first(stack, chat_browser_pool, width, theme):
    from pathlib import Path
    context=chat_browser_pool('chromium').new_context(viewport={'width':width,'height':1000 if width==1440 else 844})
    page=context.new_page()
    try:
        def synthetic_paths(route):
            response=route.fetch()
            data=response.json()
            for i,project in enumerate(data.get('projects',[])):
                project['root']='/workspace/demo/project-'+str(i+1)
            route.fulfill(response=response,json=data)
        page.route('**/api/overview',synthetic_paths)
        _login(page,stack)
        page.evaluate('t=>CodePierAppearance.setPreference(t)',theme)
        _navigate(page,'overview')
        section=page.locator('.overview-summary')
        expect(section.locator('h2')).to_have_text('工作区统计与用量')
        assert section.evaluate('e=>e.tagName')=='SECTION'
        assert section.evaluate('e=>e.parentElement.firstElementChild===e')
        expect(section.locator('.workspace-stats')).to_be_visible()
        expect(section.locator('.workspace-stat')).to_have_count(4)
        assert section.locator(':scope > summary').count()==0
        assert section.bounding_box()['y']<page.locator('.workspace-recent-projects').bounding_box()['y']
        expect(page.locator('.overview-continue')).to_have_count(0)
        projects=page.locator('.workspace-recent-projects').bounding_box()
        operations=page.locator('.workspace-operations').bounding_box()
        if width > 900:
            assert abs(projects['y']-operations['y'])<=1
            assert operations['x']+operations['width']<=projects['x']
            assert operations['width']>projects['width']
        else:
            assert projects['y']+projects['height']<=operations['y']+1
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        folder=Path('.work/overview-pair-restored');folder.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(folder/f'{theme}-{width}.png'),full_page=True)
        page.screenshot(path=str(folder/f'{theme}-{width}.jpg'),type='jpeg',quality=35,full_page=True)
        # A real attention row must not split the desktop card pair or overlap it.
        page.evaluate("""() => {
            S.overview.recent_operations = [{id:'attention-fixture',state:'needs_review',alias:'Review fixture',tool:'exec',created_at:0}];
            document.querySelector('#page').innerHTML = overviewHTML();
        }""")
        expect(page.locator('.overview-attention')).to_be_visible()
        projects=page.locator('.workspace-recent-projects').bounding_box()
        operations=page.locator('.workspace-operations').bounding_box()
        attention=page.locator('.overview-attention').bounding_box()
        assert attention['y']+attention['height']<=projects['y']+1
        assert attention['y']+attention['height']<=operations['y']+1
        if width>900:
            assert abs(projects['y']-operations['y'])<=1
        else:
            assert projects['y']+projects['height']<=operations['y']+1
    finally:context.close()
