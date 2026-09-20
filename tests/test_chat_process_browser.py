"""Execution groups retain inspectability without flooding the transcript."""
from playwright.sync_api import expect
from tests.test_chat_browser import chat_page, event
from tests.test_chat_complete_browser import send


def seed(p):
    receipt=send(p)['args']['receipt']
    event(p,'chat',{'type':'user','receipt':receipt,'text':'检查项目'},10)
    for i in range(10):
        event(p,'chat',{'type':'tool','receipt':receipt,'tool_id':str(i),'name':'bash' if i%2 else 'edit','status':'completed','text':'output-'+str(i)},20+2*i)
        event(p,'chat',{'type':'reasoning','receipt':receipt,'item_id':str(i),'text':'继续检查'},21+2*i)
    return receipt


def test_many_steps_collapse_and_preserve_open_state_during_stream_and_switch(chat_page):
    p=chat_page;r=seed(p)
    expect(p.locator('.chat-process')).to_have_count(1)
    expect(p.locator('.chat-process > summary')).to_contain_text('10 项工具 · 10 段思考')
    expect(p.locator('.chat-message-tool').first).to_be_hidden()
    assert p.locator('.chat-process').bounding_box()['height']<60
    p.locator('.chat-process > summary').click()
    p.locator('.chat-message-tool summary').first.click()
    p.evaluate("window.processNode=document.querySelector('.chat-process');window.savedRow=ChatUI.selected")
    event(p,'chat',{'type':'tool','receipt':r,'tool_id':'next','name':'read','status':'started','text':'reading'},60)
    expect(p.locator('.chat-process > summary')).to_contain_text('read 执行中')
    expect(p.locator('.chat-message-tool pre').first).to_be_visible()
    assert p.evaluate("processNode===document.querySelector('.chat-process')")
    p.click('#chat-new');p.locator('#chat-project-results button:not(:disabled)').first.click();p.evaluate('chatSwitch(savedRow)')
    expect(p.locator('.chat-message-tool pre').first).to_be_visible()
    event(p,'chat',{'type':'done','receipt':r,'status':'completed'},70)
    expect(p.locator('.chat-process')).to_have_attribute('data-active','false')
    expect(p.locator('.chat-process')).to_have_attribute('open','')


def test_tool_errors_and_approvals_are_not_hidden_in_groups(chat_page):
    p=chat_page;r=seed(p)
    event(p,'chat',{'type':'tool','receipt':r,'tool_id':'0','name':'edit','status':'error','text':'permission denied'},60)
    expect(p.locator('.chat-tool-failed pre')).to_be_visible()
    assert p.locator('.chat-tool-failed').evaluate("el=>!el.closest('.chat-process')")
    event(p,'chat',{'type':'approval','receipt':r,'request_id':'a','method':'confirm','text':'允许执行？'},70)
    expect(p.locator('.chat-message-approval')).to_be_visible()
    expect(p.get_by_role('button',name='允许本次')).to_be_visible()


def test_search_reveals_collapsed_tool_output(chat_page):
    p=chat_page;seed(p)
    p.click('#chat-find-toggle');p.fill('#chat-find-input','output-7')
    expect(p.locator('.chat-message.search-match pre')).to_be_visible()
    expect(p.locator('.chat-process')).to_have_attribute('open','')


def test_groups_respect_messages_and_history_limit(chat_page):
    p=chat_page;r=seed(p)
    event(p,'chat',{'type':'delta','receipt':r,'text':'中间回复'},60)
    event(p,'chat',{'type':'tool','receipt':r,'tool_id':'later','name':'read','status':'completed','text':'later'},70)
    expect(p.locator('.chat-process')).to_have_count(2)
    expect(p.locator('.chat-message-assistant')).to_be_visible()
    p.evaluate('chatView().limit=5;chatRenderHistory(false)')
    expect(p.locator('#chat-messages .chat-message')).to_have_count(5)
    p.click('#chat-older')
    expect(p.locator('#chat-messages .chat-message')).to_have_count(23)
