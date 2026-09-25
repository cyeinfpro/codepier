from tests.evidence import evidence_path
"""Real browser chat behavior, using reusable isolated component fixtures."""
from pathlib import Path
import json
import pytest
from playwright.sync_api import expect
from tests.browser_support import chat_page, event, ROOT

def test_chat_first_send_incremental_safe_ime_and_mobile(chat_page):
    page=chat_page
    expect(page.locator('#chat-compose')).to_be_visible()
    assert page.get_by_text('取得输入权').count()==0
    page.fill('#chat-compose','Implement a small change')
    page.locator('#chat-compose').dispatch_event('keydown',{'key':'Enter','isComposing':True,'keyCode':229})
    assert not page.evaluate("requests.some(r=>r.path.endsWith('/start'))")
    page.press('#chat-compose','Enter')
    expect(page.locator('#chat-compose')).to_have_value('')
    req=page.evaluate("requests.find(r=>r.path.endsWith('/chat_prompt'))")
    assert req['args']['receipt'] and req['project']=='p1'
    assert page.evaluate("requests.find(r=>r.path.endsWith('/start')).args.mode")=='chat'
    event(page,'chat',{'type':'user','receipt':req['args']['receipt'],'text':'Implement a small change'},10)
    event(page,'chat',{'type':'delta','receipt':req['args']['receipt'],'text':'Hello '},20)
    expect(page.locator('.chat-message-assistant')).to_contain_text('Hello')
    page.evaluate("window.originalNode=document.querySelector('.chat-message-assistant')")
    event(page,'chat',{'type':'delta','receipt':req['args']['receipt'],'text':'<img src=x onerror=alert(1)>\n```js\nalert(2)\n```'},30)
    expect(page.locator('.chat-message-assistant pre')).to_contain_text('alert(2)')
    assert page.locator('.chat-message-assistant img').count()==0
    assert page.evaluate("originalNode===document.querySelector('.chat-message-assistant')")
    event(page,'chat',{'type':'tool','receipt':req['args']['receipt'],'tool_id':'t1','name':'read','text':'src/main.py'},40)
    expect(page.locator('.chat-message-tool details')).not_to_have_attribute('open','')
    event(page,'chat',{'type':'delta','receipt':req['args']['receipt'],'text':'DUPLICATE'},30)
    assert 'DUPLICATE' not in page.locator('.chat-message-assistant').inner_text()
    page.fill('#chat-compose','retained draft')
    page.evaluate('chatPage()')
    expect(page.locator('#chat-compose')).to_have_value('retained draft')
    page.screenshot(path=evidence_path(str(ROOT/'docs/evidence/chat-complete-20260915/desktop-chat-test.png')))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
    page.click('#chat-history-toggle')
    expect(page.locator('#chat-root')).to_have_class('chat-workspace drawer-open')
    page.click('#chat-shade',position={'x':350,'y':200})
    page.screenshot(path=evidence_path(str(ROOT/'docs/evidence/chat-complete-20260915/mobile-chat-test.png')))


def test_chat_pending_switch_drafts_reconnect_and_explicit_approval(chat_page):
    page=chat_page
    page.evaluate('window.delaySend=true')
    page.fill('#chat-compose','original project message')
    page.press('#chat-compose','Enter')
    page.wait_for_function('!!window.finishSend')
    page.select_option('#chat-project','p2')
    page.fill('#chat-compose','project two draft')
    page.evaluate('finishSend()')
    expect(page.locator('#chat-compose')).to_have_value('project two draft')
    assert page.evaluate('ChatUI.selected') is None
    assert page.evaluate('ChatUI.project')=='p2'
    page.evaluate('window.delaySend=false')
    page.press('#chat-compose','Enter')
    expect(page.locator('#chat-compose')).to_have_value('')
    req=page.evaluate("requests.filter(r=>r.path.endsWith('/chat_prompt')).at(-1)")
    event(page,'chat',{'type':'approval','receipt':req['args']['receipt'],'request_id':'approval-1','method':'confirm','text':'Allow this operation?'},100)
    expect(page.locator('.chat-message-approval')).to_contain_text('Allow this operation?')
    assert not page.evaluate("requests.some(r=>r.path.endsWith('/chat_answer'))")
    page.get_by_role('button',name='拒绝',exact=True).click()
    assert page.evaluate("requests.find(r=>r.path.endsWith('/chat_answer')).args.answer") is False
    page.fill('#chat-compose','remember this session')
    page.evaluate("window.savedRow=ChatUI.selected;chatSwitch(null)")
    page.evaluate('chatSwitch(savedRow)')
    expect(page.locator('#chat-compose')).to_have_value('remember this session')
    assert 'cursor=100' in page.evaluate('streams.at(-1).url')
    page.locator('#chat-back').click()
    expect(page.locator('#page')).to_have_text('Management page')
    assert page.evaluate('streams.at(-1).closed')
