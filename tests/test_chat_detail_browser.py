"""User-facing detail regressions. Real browser; mocked native transport."""
import pytest
from playwright.sync_api import expect
from tests.browser_support import chat_page, event
from tests.test_chat_complete_browser import send


def test_model_arrow_navigation_and_filter_reset(chat_page):
    p=chat_page
    p.click('#chat-model-picker')
    p.press('#chat-model-search','ArrowDown')
    expect(p.locator('[data-keyboard="true"]')).to_have_text('恢复本机默认模型')
    p.press('#chat-model-search','End')
    expect(p.locator('[data-keyboard="true"]')).to_contain_text('Advanced Model')
    p.fill('#chat-model-search','no-such-model')
    assert p.locator('#chat-model-search').get_attribute('aria-activedescendant') is None
    expect(p.locator('.chat-picker-empty')).to_contain_text('没有匹配')
    p.fill('#chat-model-search','Advanced')
    p.press('#chat-model-search','Enter')
    expect(p.locator('#chat-model-name')).to_have_text('Advanced Model')
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/start'))")


def test_tabbing_to_model_confirms_focused_option(chat_page):
    p=chat_page;p.click('#chat-model-picker')
    p.press('#chat-model-search','ArrowDown')
    p.locator('.chat-model-option').last.focus()
    p.keyboard.press('Enter')
    expect(p.locator('#chat-model-name')).to_have_text('Advanced Model')


def test_send_controls_follow_work_and_confirmation(chat_page):
    p=chat_page
    expect(p.locator('#chat-send-mode')).to_be_hidden()
    p.evaluate('window.delaySend=true')
    p.fill('#chat-compose','Review this project');p.click('#chat-send')
    expect(p.locator('#chat-form')).to_have_attribute('aria-busy','true')
    expect(p.locator('#chat-send')).to_be_disabled()
    p.evaluate('finishSend()')
    expect(p.locator('#chat-form')).to_have_attribute('aria-busy','false')
    receipt=p.evaluate("requests.find(r=>r.path.endsWith('/chat_prompt')).args.receipt")
    event(p,'chat',{'type':'user','receipt':receipt,'text':'Review this project'},10)
    expect(p.locator('#chat-send-mode')).to_be_visible()
    event(p,'chat',{'type':'done','receipt':receipt,'status':'completed'},20)
    expect(p.locator('#chat-send-mode')).to_be_hidden()


@pytest.mark.parametrize('width,height',[(320,568),(375,812),(768,1024),(1024,768),(1440,1000),(667,375)])
def test_composed_transcript_and_touch_controls_fit(chat_page,width,height):
    p=chat_page;p.set_viewport_size({'width':width,'height':height})
    p.emulate_media(reduced_motion='reduce')
    receipt=send(p)['args']['receipt']
    event(p,'chat',{'type':'delta','receipt':receipt,'text':'## Review\n\nReadable **result**.\n\n```python\n'+('long_identifier_'*35)+'\n```'},10)
    p.wait_for_function("document.querySelector('.chat-code') !== null")
    p.fill('#chat-compose','继续检查\n保留输入草稿')
    for selector in ['#chat-compose','#chat-send','#chat-attach','#chat-model-picker']:
        box=p.locator(selector).bounding_box()
        assert box and box['x']>=0 and box['x']+box['width']<=width+1
        assert box['y']>=0 and box['y']+box['height']<=height+1
    if width<=760:
        assert p.locator('#chat-send').bounding_box()['height']>=40
        assert p.locator('#chat-attach').bounding_box()['height']>=40
    assert p.evaluate('document.documentElement.scrollWidth<=innerWidth+1')


def test_model_close_button_enter_does_not_select_model(chat_page):
    p=chat_page;p.click('#chat-model-picker')
    p.focus('#chat-popover-close');p.keyboard.press('Enter')
    expect(p.locator('#chat-popover')).to_be_hidden()
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
