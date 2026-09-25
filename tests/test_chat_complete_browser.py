"""Complete conversation controls using real browsers and a deterministic HTTP boundary.
No native model calls. Full-stack and installed-native smoke run separately.
"""
from pathlib import Path
import pytest
from playwright.sync_api import expect
from tests.browser_support import chat_page, event
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/evidence/chat-complete-20260915'


def send(page,text='Review this project'):
    page.fill('#chat-compose',text)
    page.press('#chat-compose','Enter')
    expect(page.locator('#chat-compose')).to_have_value('')
    return page.evaluate("requests.filter(x=>x.path.endsWith('/chat_prompt')).at(-1)")


def assert_composer_controls_fit(page,width,height):
    """Frequent actions stay visible; secondary mobile controls remain reachable."""
    def within_viewport(selector):
        control=page.locator(selector)
        expect(control).to_be_visible()
        box=control.bounding_box()
        assert box and box['x']>=0 and box['x']+box['width']<=width+1,(selector,box)
        assert box['y']>=0 and box['y']+box['height']<=height+1,(selector,box)

    for selector in ['#chat-compose','#chat-model-picker','#chat-send']:
        within_viewport(selector)
    if width<=760:
        expect(page.locator('#chat-options')).to_be_hidden()
        within_viewport('#chat-options-toggle')
        page.click('#chat-options-toggle')
        expect(page.get_by_role('dialog',name='会话设置')).to_be_visible()
        page.locator('#chat-effort-select').scroll_into_view_if_needed()
    within_viewport('#chat-effort-select')
    if width<=760:
        page.keyboard.press('Escape')
        expect(page.locator('#chat-options')).to_be_hidden()
        expect(page.locator('#chat-options-toggle')).to_be_focused()


def test_model_and_effort_are_selectable_before_first_message(chat_page):
    p=chat_page
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    expect(p.locator('#chat-effort-select')).to_be_enabled()
    p.click('#chat-model-picker')
    p.fill('#chat-model-search','Advanced')
    expect(p.locator('.chat-model-option')).to_have_count(1)
    p.locator('.chat-model-option').click()
    expect(p.locator('#chat-model-name')).to_have_text('Advanced Model')
    expect(p.locator('#chat-effort-select')).to_be_enabled()
    p.select_option('#chat-effort-select','high')
    assert not p.evaluate(r"requests.some(x=>/\/(start|chat_prompt|input|lease)$/.test(x.path))")
    send(p)
    start=p.evaluate("requests.find(x=>x.path.endsWith('/start')).args")
    assert start['model']=='remote/advanced-model'
    assert start['effort']=='high'
    assert start['mode']=='chat' and start['cwd']=='.'
    assert not p.evaluate("requests.some(x=>x.path.endsWith('/lease'))")


def test_settings_pending_ack_and_explicit_retry_after_known_rejection(chat_page):
    p=chat_page
    request=send(p)
    receipt=request['args']['receipt']
    event(p,'chat',{'type':'user','receipt':receipt,'text':'Review this project'},10)
    event(p,'chat',{'type':'done','receipt':receipt,'status':'completed'},20)
    p.evaluate("window.receiptState='queued'")
    p.select_option('#chat-effort-select','high')
    expect(p.locator('#chat-setting-state')).to_contain_text('切换')
    op=p.evaluate("requests.filter(x=>x.path.endsWith('/chat_settings')).at(-1).args")
    event(p,'chat',{'type':'error','receipt':op['receipt'],'text':'Model does not support high'},30)
    expect(p.locator('#chat-setting-state')).to_have_text('设置失败')
    expect(p.locator('#chat-status')).to_contain_text('does not support')
    p.click('#chat-settings-retry')
    retry=p.evaluate("requests.filter(x=>x.path.endsWith('/chat_settings')).at(-1).args")
    assert retry['receipt']!=op['receipt']
    event(p,'chat',{'type':'settings_pending','receipt':retry['receipt'],'effort':'high'},40)
    p.evaluate("window.receiptState='completed'")
    expect(p.locator('#chat-setting-state')).to_have_text('下轮生效',timeout=4000)
    event(p,'chat',{'type':'settings','model':'native-model','effort':'high','models':[{'id':'native-model','model':'native-model','supportedReasoningEfforts':[{'reasoningEffort':'low'},{'reasoningEffort':'high'}]}]},50)
    event(p,'chat',{'type':'settings_pending','cleared':True},60)
    expect(p.locator('#chat-effort-select')).to_have_value('high')


@pytest.mark.parametrize('width,height',[(1440,1000),(1024,768),(390,844),(320,568)])
def test_controls_and_searchable_model_panel_fit_every_viewport(chat_page,width,height):
    p=chat_page
    p.set_viewport_size({'width':width,'height':height})
    # Model cache hits can finish before the browser dispatches resize.
    p.wait_for_function("document.querySelector('#chat-root').getBoundingClientRect().bottom<=innerHeight+1")
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    assert_composer_controls_fit(p,width,height)
    assert p.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    p.click('#chat-model-picker')
    expect(p.locator('#chat-model-search')).to_be_visible()
    p.fill('#chat-model-search','local')
    expect(p.locator('.chat-model-option')).to_have_count(1)
    box=p.locator('#chat-popover').bounding_box()
    assert box['x']>=0 and box['y']>=0 and box['x']+box['width']<=width+1
    p.screenshot(path=str(OUT/f'model-picker-{width}.png'))
    p.press('#chat-model-search','Escape')
    expect(p.locator('#chat-popover')).to_be_hidden()
    p.screenshot(path=str(OUT/f'complete-empty-{width}.png'))


def test_markdown_tools_usage_commands_and_queue_are_functional(chat_page):
    p=chat_page;request=send(p);r=request['args']['receipt']
    text='## Review results\n\nThe **upload queue** is ready.\n\n- First item\n- Second item\n\n| File | Result |\n| --- | --- |\n| queue.py | Passed |\n\n```python\nprint("ready")\n```\n\n<img src=x onerror=window.bad=1>'
    event(p,'chat',{'type':'user','receipt':r,'text':'Review this project'},10)
    event(p,'chat',{'type':'delta','receipt':r,'text':text},20)
    expect(p.locator('.chat-message-assistant strong')).to_have_text('upload queue')
    expect(p.locator('.chat-message-assistant li')).to_have_count(2)
    expect(p.locator('.chat-message-assistant table td').first).to_have_text('queue.py')
    assert p.locator('.chat-message-assistant img').count()==0 and not p.evaluate('window.bad')
    event(p,'chat',{'type':'tool','receipt':r,'tool_id':'t1','name':'read','status':'completed','text':'{"path":"queue.py"}'},30)
    expect(p.locator('.chat-message-tool summary')).to_contain_text('read')
    p.locator('.chat-process > summary').click()
    p.locator('.chat-message-tool summary').click()
    expect(p.locator('.chat-message-tool pre')).to_be_visible()
    event(p,'chat',{'type':'stats','stats':{'tokens':{'input':1000,'output':400,'total':1400},'contextUsage':{'percent':12,'tokens':15360,'contextWindow':128000}}},40)
    p.click('#chat-usage')
    expect(p.locator('#chat-inspector-body')).to_contain_text('1,400')
    p.click('[data-chat-tab="commands"]')
    expect(p.locator('.chat-command-list')).to_contain_text('/skill:review')
    p.locator('.chat-command-list button').filter(has_text='/skill:review').click()
    expect(p.locator('#chat-compose')).to_have_value('/skill:review ')
    p.evaluate("window.queue=[{receipt:'queued-1',kind:'chat_prompt',text:'Pending work',state:'queued'}]")
    p.click('[data-chat-tab="queue"]')
    expect(p.locator('#chat-queue-list')).to_contain_text('Pending work')
    p.locator('#chat-queue-list button').get_by_text('撤回',exact=True).click()
    assert p.evaluate("requests.find(x=>x.path.endsWith('/chat_cancel')).args.target")=='queued-1'
    p.screenshot(path=str(OUT/'complete-conversation-queue.png'))


def test_live_steering_and_full_export_use_real_backend_actions(chat_page):
    p=chat_page;r=send(p)['args']['receipt']
    event(p,'chat',{'type':'user','receipt':r,'text':'Review'},10)
    p.select_option('#chat-send-mode','steer')
    p.fill('#chat-compose','Focus on upload stability')
    p.press('#chat-compose','Enter')
    expect(p.locator('#chat-compose')).to_have_value('')
    assert p.evaluate("requests.filter(x=>x.path.endsWith('/chat_steer')).at(-1).args.text")=='Focus on upload stability'
    p.evaluate("window.exportHref=null;HTMLAnchorElement.prototype.click=function(){window.exportHref=this.href}")
    p.evaluate("chatExport('md')")
    assert '/export?format=md' in p.evaluate('exportHref')
    assert not p.evaluate("requests.some(x=>x.path.endsWith('/lease'))")


def test_catalog_failure_is_visible_and_can_be_retried_without_losing_draft(chat_page):
    p=chat_page;p.fill('#chat-compose','Keep my draft')
    p.evaluate('window.catalogFailure=true;chatLoadCatalog(true)')
    expect(p.locator('#chat-catalog-notice')).to_contain_text('offline')
    expect(p.locator('#chat-model-picker')).to_be_visible()
    p.evaluate('window.catalogFailure=false')
    p.click('#chat-catalog-retry')
    expect(p.locator('#chat-catalog-notice')).to_be_hidden()
    expect(p.locator('#chat-compose')).to_have_value('Keep my draft')


def test_model_catalog_and_reply_cannot_overwrite_new_project(chat_page):
    p=chat_page;p.evaluate('window.delayCatalog=true')
    p.evaluate('void chatLoadCatalog(true)');p.wait_for_function('!!window.finishCatalog')
    p.evaluate('window.oldCatalog=window.finishCatalog;window.delayCatalog=false')
    p.select_option('#chat-project','p2')
    p.fill('#chat-compose','Project two')
    p.evaluate('oldCatalog()')
    expect(p.locator('#chat-compose')).to_have_value('Project two')
    assert p.evaluate('ChatUI.project')=='p2'


def test_streaming_plaintext_preserves_text_node_and_user_scroll(chat_page):
    p=chat_page;r=send(p)['args']['receipt']
    event(p,'chat',{'type':'delta','receipt':r,'text':'A stable streamed paragraph'},10)
    expect(p.locator('.chat-message-assistant p')).to_have_text('A stable streamed paragraph')
    p.evaluate("window.nodeBefore=document.querySelector('.chat-message-assistant p').firstChild")
    event(p,'chat',{'type':'delta','receipt':r,'text':' continues naturally.'},20)
    expect(p.locator('.chat-message-assistant p')).to_contain_text('naturally')
    assert p.evaluate("nodeBefore===document.querySelector('.chat-message-assistant p').firstChild")


def test_stopped_session_can_change_model_before_resume_and_resets_stream_cursor(chat_page):
    p=chat_page;r=send(p)['args']['receipt']
    event(p,'chat',{'type':'user','receipt':r,'text':'Old message'},100)
    event(p,'chat',{'type':'done','receipt':r,'status':'completed'},200)
    old=p.evaluate('ChatUI.selected.id')
    event(p,'session',{'status':'exited'},201)
    p.click('#chat-model-picker');p.fill('#chat-model-search','Advanced')
    p.locator('.chat-model-option').click()
    expect(p.locator('#chat-effort-select')).to_be_enabled()
    p.select_option('#chat-effort-select','high')
    assert not p.evaluate("requests.some(x=>x.path.endsWith('/chat_settings'))")
    send(p,'Continue with another model')
    start=p.evaluate("requests.filter(x=>x.path.endsWith('/start')).at(-1).args")
    assert start['continue_session']==old
    assert start['model']=='remote/advanced-model' and start['effort']=='high'
    assert 'cursor=0' in p.evaluate('streams.at(-1).url')
    assert p.evaluate('ChatUI.selected.id')!=old
