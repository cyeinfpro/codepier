"""Full navigation flow using real Chromium/WebKit and deterministic HTTP/SSE."""
from pathlib import Path
from urllib.parse import parse_qs,urlsplit
import pytest
from playwright.sync_api import expect
from tests.test_chat_browser import chat_page,event
from tests.test_chat_complete_browser import send
from tests.test_chat_global_flow import seed_rows

OUT=Path(__file__).resolve().parents[1]/'docs/evidence/cli-global-flow-20260917'


def test_history_filters_do_not_change_current_context(chat_page):
    p=chat_page;seed_rows(p)
    p.locator('[data-session-id="'+'a'*32+'"]').click()
    p.fill('#chat-compose','keep current draft')
    p.select_option('#chat-history-project','p2')
    expect(p.locator('#chat-compose')).to_have_value('keep current draft')
    assert p.evaluate('ChatUI.selected.project_id')=='p1'
    path=p.evaluate("requests.filter(r=>r.path.startsWith('/api/native/sessions?')).at(-1).path")
    assert parse_qs(urlsplit(path).query)['project']==['p2']
    p.click('#chat-history-clear')
    expect(p.locator('#chat-history-project')).to_have_value('')
    expect(p.locator('#chat-compose')).to_have_value('keep current draft')


def test_new_picker_cancel_and_select_do_not_start_cli(chat_page):
    p=chat_page;send(p);p.fill('#chat-compose','original draft')
    count=p.evaluate("requests.filter(r=>r.path.endsWith('/start')).length")
    sid=p.evaluate('ChatUI.selected.id')
    p.click('#chat-new');expect(p.locator('#chat-project-search')).to_be_focused()
    p.press('#chat-project-search','Escape')
    assert p.evaluate('ChatUI.selected.id')==sid
    expect(p.locator('#chat-compose')).to_have_value('original draft')
    p.click('#chat-new');p.fill('#chat-project-search','two')
    expect(p.locator('#chat-project-results button')).to_have_count(1)
    p.press('#chat-project-search','Enter')
    expect(p.locator('#chat-project-dialog')).to_have_count(0)
    assert p.evaluate('ChatUI.project')=='p2'
    assert p.evaluate('ChatUI.selected') is None
    assert p.evaluate("requests.filter(r=>r.path.endsWith('/start')).length")==count
    expect(p.locator('#chat-project')).to_have_value('p2')
    expect(p.locator('#chat-compose')).to_be_focused()


def test_history_error_has_local_retry_and_keeps_results(chat_page):
    p=chat_page;seed_rows(p)
    p.evaluate('''() => {
      window.failHistory=true;const original=api;
      window.api=async(path,opts)=>{
        if(failHistory&&path.startsWith('/api/native/sessions?'))throw new Error('History connection failed');
        return original(path,opts);
      };return chatList();
    }''')
    expect(p.locator('#chat-history-notice')).to_contain_text('History connection failed')
    expect(p.locator('.chat-session')).to_have_count(2)
    p.evaluate('window.failHistory=false');p.click('#chat-history-retry')
    expect(p.locator('#chat-history-notice')).to_be_hidden()
    expect(p.locator('.chat-session')).to_have_count(2)


def test_typing_query_invalidates_previous_response_before_debounce(chat_page):
    p=chat_page
    p.evaluate('''() => {
      const original=api;window.api=async(path,opts)=>{
        if(path.startsWith('/api/native/sessions?')&&!window.waitedList){
          window.waitedList=true;await new Promise(r=>window.releaseList=r);
          return {sessions:[{id:'old',mode:'chat',project_id:'p1',title:'Obsolete result',provider:'pi',status:'exited'}]};
        }return original(path,opts);
      };void chatList();
    }''')
    p.wait_for_function('!!window.releaseList');p.fill('#chat-history-search','new query');p.evaluate('releaseList()')
    expect(p.locator('#chat-history')).not_to_contain_text('Obsolete result')


def test_all_session_menu_actions_explain_disabled_states(chat_page):
    p=chat_page;p.locator('.chat-overflow summary').click()
    for name in ('chat-rename','chat-resume','chat-export','chat-export-json','chat-stop','chat-delete'):
        expect(p.locator('#'+name)).to_be_disabled()
        assert p.locator('#'+name).get_attribute('title')
    p.press('.chat-overflow summary','Escape');send(p)
    p.locator('.chat-overflow summary').click()
    expect(p.locator('#chat-rename')).to_be_enabled()
    expect(p.locator('#chat-stop')).to_be_enabled()
    expect(p.locator('#chat-resume')).to_be_disabled()
    expect(p.locator('#chat-delete')).to_be_disabled()


def test_usage_and_find_are_view_local(chat_page):
    p=chat_page;send(p)
    event(p,'chat',{'type':'stats','stats':{'contextUsage':{'percent':75}}},10)
    expect(p.locator('#chat-usage')).to_have_text('上下文 75%')
    p.click('#chat-find-toggle');p.fill('#chat-find-input','one')
    p.evaluate("chatSwitch(null,'p2',{cwd:'.'})")
    expect(p.locator('#chat-usage')).to_have_text('上下文')
    p.click('#chat-find-toggle');expect(p.locator('#chat-find-input')).to_have_value('')
    expect(p.locator('#chat-find-next')).to_be_disabled()


def test_mobile_sidebar_does_not_trap_focus_when_closed(chat_page):
    p=chat_page;p.set_viewport_size({'width':390,'height':844})
    expect(p.locator('.chat-sidebar')).to_have_attribute('inert','')
    p.click('#chat-history-toggle')
    expect(p.locator('.chat-sidebar')).not_to_have_attribute('inert','')
    expect(p.locator('#chat-new')).to_be_focused()
    p.keyboard.press('Escape')
    expect(p.locator('.chat-sidebar')).to_have_attribute('inert','')
    expect(p.locator('#chat-history-toggle')).to_be_focused()


@pytest.mark.parametrize('chat_page',['chromium','webkit'],indirect=True)
@pytest.mark.parametrize('width,height',[(320,568),(390,844),(667,375),(768,1024),(1440,1000)])
def test_project_picker_and_composer_fit_all_viewports(chat_page,width,height):
    p=chat_page;p.set_viewport_size({'width':width,'height':height});p.emulate_media(reduced_motion='reduce')
    seed_rows(p)
    for appearance in ['light','dark']:
        p.evaluate('chatApplyAppearance('+repr(appearance)+')')
        if width<=760:p.click('#chat-history-toggle')
        p.screenshot(path=str(OUT/f'history-{p.context.browser.browser_type.name}-{appearance}-{width}.png'))
        p.click('#chat-new');expect(p.locator('#chat-project-search')).to_be_focused()
        box=p.locator('#chat-project-dialog').bounding_box()
        assert box and box['x']>=0 and box['y']>=0
        assert box['x']+box['width']<=width+1 and box['y']+box['height']<=height+1
        assert p.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        p.screenshot(path=str(OUT/f'project-picker-{p.context.browser.browser_type.name}-{appearance}-{width}.png'))
        p.fill('#chat-project-search','not found');expect(p.locator('#chat-project-results')).to_contain_text('没有匹配')
        p.press('#chat-project-search','Escape')
        if width<=760:p.keyboard.press('Escape')
        for selector in ['#chat-compose','#chat-model-picker','#chat-send']:
            rect=p.locator(selector).bounding_box()
            assert rect and rect['y']>=0 and rect['y']+rect['height']<=height+1


def test_project_picker_searches_paths_and_keeps_permissions(chat_page):
    p=chat_page
    p.evaluate("S.projects.push({id:'locked',alias:'Read only',root:'/workspace/locked',mode:'read',allow_tasks:false});S.devices=[{id:'node-1',name:'Work machine',online:false}];chatTargetSync()")
    p.click('#chat-new');p.fill('#chat-project-search','/workspace/two')
    expect(p.locator('#chat-project-results button')).to_have_count(1)
    expect(p.locator('#chat-project-results')).to_contain_text('离线')
    p.fill('#chat-project-search','locked')
    expect(p.locator('[data-project-id="locked"]')).to_be_disabled()
    p.fill('#chat-project-search','two');p.press('#chat-project-search','Enter')
    p.fill('#chat-compose','offline draft')
    expect(p.locator('#chat-send')).to_be_disabled()
    expect(p.locator('#chat-target-info')).to_contain_text('离线')
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/start'))")
