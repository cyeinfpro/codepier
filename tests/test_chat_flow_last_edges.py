"""Final button-level regressions, using isolated native transport fixtures."""
import pytest
from playwright.sync_api import expect
from tests.test_chat_browser import chat_page,event
from tests.test_chat_complete_browser import send


def test_send_reenables_when_pending_model_setting_finishes_without_more_typing(chat_page):
    p=chat_page;send(p)
    p.evaluate("window.receiptState='queued'")
    p.select_option('#chat-effort-select','high')
    p.fill('#chat-compose','send after model confirmation')
    expect(p.locator('#chat-send')).to_be_disabled()
    p.evaluate("window.receiptState='completed'")
    p.wait_for_function('chatView().settingOp===null')
    expect(p.locator('#chat-send')).to_be_enabled(timeout=1000)
    expect(p.locator('#chat-compose')).to_have_value('send after model confirmation')


@pytest.mark.parametrize('width,height',[(667,375),(320,568)])
def test_every_sidebar_control_remains_visible_on_short_screens(chat_page,width,height):
    p=chat_page;p.set_viewport_size({'width':width,'height':height})
    p.click('#chat-history-toggle')
    p.select_option('#chat-history-filter','running')
    for selector in ['#chat-back','#chat-new','#chat-history-project','#chat-history-search','#chat-history-filter','#chat-history-clear','#chat-command-menu','#chat-appearance','.chat-sidebar-preferences [data-nav="projects"]']:
        box=p.locator(selector).bounding_box()
        assert box and box['height']>0 and box['y']>=0 and box['y']+box['height']<=height+1,(selector,box)
    assert p.locator('#chat-history').bounding_box()['height']>=40


def test_existing_offline_conversation_explains_send_state_and_recovers(chat_page):
    p=chat_page;send(p)
    p.evaluate("S.devices=[{id:'node-1',name:'Studio',online:false}];chatSyncChrome()")
    p.fill('#chat-compose','keep draft while disconnected')
    expect(p.locator('#chat-target-notice')).to_contain_text('离线')
    expect(p.locator('#chat-send')).to_be_disabled()
    p.evaluate("() => {const original=api;window.api=async(path,opts)=>path==='/api/projects'?{projects:S.projects}:path==='/api/devices'?{devices:[{id:'node-1',name:'Studio',online:true}]}:original(path,opts);}")
    p.click('#chat-target-refresh')
    expect(p.locator('#chat-target-notice')).to_be_hidden()
    expect(p.locator('#chat-send')).to_be_enabled()
    expect(p.locator('#chat-compose')).to_have_value('keep draft while disconnected')


def test_settings_queue_does_not_offer_text_edit_and_stopped_commands_refresh_catalog(chat_page):
    p=chat_page;send(p)
    p.evaluate("window.queue=[{receipt:'setting',kind:'chat_settings',state:'queued'}];chatInspector('queue')")
    expect(p.get_by_role('button',name='撤回并编辑',exact=True)).to_have_count(0)
    event(p,'session',{'status':'exited'},10)
    p.click('[data-chat-tab="overview"]')
    expect(p.get_by_role('button',name='压缩上下文',exact=True)).to_be_disabled()
    p.click('[data-chat-tab="commands"]')
    before=p.evaluate("requests.filter(r=>r.path.endsWith('/chat_catalog')).length")
    p.get_by_role('button',name='刷新本机指令',exact=True).click()
    p.wait_for_function('(n)=>requests.filter(r=>r.path.endsWith("/chat_catalog")).length>n',arg=before)
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/chat_command'))")


def test_picker_refresh_preserves_focused_project_and_palette_escape_closes_search(chat_page):
    p=chat_page;p.click('#chat-new')
    p.locator('#chat-project-results [data-project-id="p2"]').focus()
    p.evaluate('ChatUI.projectPickerRefresh()')
    expect(p.locator('#chat-project-results [data-project-id="p2"]')).to_be_focused()
    p.keyboard.press('Escape');p.click('#chat-command-menu')
    p.get_by_role('searchbox',name='搜索操作与会话').fill('搜索全部会话')
    p.keyboard.press('Escape')
    expect(p.get_by_role('dialog',name='快捷操作')).to_have_count(0)
