"""Final user-flow regressions: keyboard, recovery, and async view ownership."""
import pytest
from playwright.sync_api import expect
from tests.browser_support import chat_page, event
from tests.test_chat_complete_browser import send

pytestmark = pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)


def test_project_arrow_from_focused_option_selects_highlighted_target(chat_page):
    p=chat_page;p.click('#chat-new')
    p.locator('[data-project-id="p1"]').focus()
    p.keyboard.press('ArrowDown');p.keyboard.press('Enter')
    expect(p.locator('#chat-project-dialog')).to_have_count(0)
    assert p.evaluate('ChatUI.project')=='p2'
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/start'))")


def test_model_arrow_from_focused_option_confirms_highlight(chat_page):
    p=chat_page;p.click('#chat-model-picker')
    p.locator('[data-model="local/native-model"]').focus()
    p.keyboard.press('ArrowDown');p.keyboard.press('Enter')
    expect(p.locator('#chat-model-name')).to_have_text('Advanced Model')


def test_palette_arrow_from_focused_option_confirms_highlight(chat_page):
    p=chat_page;p.click('#chat-command-menu')
    p.locator('.chat-palette-results button').first.focus()
    p.keyboard.press('ArrowDown');p.keyboard.press('Enter')
    expect(p.locator('#chat-history-search')).to_be_focused()
    expect(p.locator('#chat-project-dialog')).to_have_count(0)


def test_explicit_default_effort_is_shown_as_default(chat_page):
    p=chat_page
    p.select_option('#chat-effort-select','high')
    p.select_option('#chat-effort-select','')
    expect(p.locator('#chat-effort-select')).to_have_value('')
    send(p)
    args=p.evaluate("requests.find(r=>r.path.endsWith('/start')).args")
    assert args['effort']==''


def test_offline_local_new_command_is_not_blocked_by_execution_status(chat_page):
    p=chat_page
    p.evaluate("S.devices=[{id:'node-1',online:false}];chatSyncChrome()")
    p.fill('#chat-compose','/new ');p.press('#chat-compose','Enter')
    expect(p.locator('#chat-project-dialog')).to_be_visible()
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/start'))")


def test_late_failed_settings_reply_is_retryable_after_returning_to_view(chat_page):
    p=chat_page;send(p)
    p.evaluate('''() => {
      window.originalRow=ChatUI.selected;window.originalView=chatView();const original=api;
      window.api=async(path,opts)=>{
        if(path.endsWith('/chat_settings')){await new Promise(r=>window.finishSettings=r);throw Object.assign(new Error('Native rejected setting'),{code:'CLI_INVALID'});}
        return original(path,opts);
      };
    }''')
    p.select_option('#chat-effort-select','high');p.wait_for_function('!!window.finishSettings')
    p.evaluate("chatSwitch(null,'p2')");p.fill('#chat-compose','must remain')
    p.evaluate('finishSettings()');p.wait_for_timeout(100)
    assert p.evaluate('originalView.settingOp.state')=='error'
    expect(p.locator('#chat-compose')).to_have_value('must remain')
    p.evaluate('chatSwitch(originalRow)')
    expect(p.locator('#chat-settings-retry')).to_be_visible()


def test_review_read_finishes_in_its_original_view_after_navigation(chat_page):
    p=chat_page;send(p)
    p.evaluate('''() => {
      window.reviewRow=ChatUI.selected;window.reviewView=chatView();const original=api;
      window.api=async(path,opts)=>{
        if(path.endsWith('/chat_review')){await new Promise(r=>window.finishReview=r);return {review_ref:'review',immutable:true,files:[{path:'src/main.py',status:'modified',text_diff_available:true,added_lines:1,removed_lines:0}],summary:{files:1},coverage:{complete:true}};}
        return original(path,opts);
      };
    }''')
    event(p,'chat',{'type':'review','receipt':'r','review_ref':'review','available':True,'summary':{'files':1}},10)
    p.locator('.chat-review > summary').click();p.wait_for_function('!!window.finishReview')
    p.evaluate("chatSwitch(null,'p2')");p.fill('#chat-compose','keep other draft');p.evaluate('finishReview()')
    p.wait_for_function("[...reviewView.items.values()].find(i=>i.kind==='review').reviewLoaded")
    expect(p.locator('#chat-compose')).to_have_value('keep other draft')
    p.evaluate('chatSwitch(reviewRow)')
    expect(p.locator('.chat-review-file')).to_contain_text('src/main.py')


def test_model_refresh_does_not_erase_pending_settings_failure(chat_page):
    p=chat_page;send(p)
    p.evaluate("chatView().settingOp={receipt:'setting',patch:{effort:'high'},state:'error'};chatSettings(chatView().settings)")
    p.evaluate('chatLoadCatalog(true)')
    expect(p.locator('#chat-settings-retry')).to_be_visible()
    assert p.evaluate('chatView().settingOp.state')=='error'


def test_catalog_pending_read_has_bounded_timeout_even_if_api_never_returns(chat_page):
    p=chat_page;p.clock.install()
    p.evaluate("() => {const original=api;window.api=(path,opts)=>path.endsWith('/chat_catalog')?new Promise(()=>{}):original(path,opts);return void chatLoadCatalog(true);}")
    p.clock.fast_forward(23000)
    expect(p.locator('#chat-catalog-notice')).to_contain_text('超时')
    assert p.evaluate('[...ChatUI.catalogCache.values()][0].pending.size')==0
    assert not p.evaluate('chatView().catalogLoading')
