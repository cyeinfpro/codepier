"""Navigation during delivery must not lose controls or leak into another login."""
import pytest
from playwright.sync_api import expect
from tests.test_chat_browser import chat_page, event
from tests.test_chat_complete_browser import send

pytestmark = pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)


def test_approval_failure_after_navigation_keeps_original_retry(chat_page):
    p=chat_page;send(p)
    event(p,'chat',{'type':'approval','receipt':'task','request_id':'approve','method':'confirm','text':'Allow fixture?'},10)
    p.evaluate('''() => {window.first=ChatUI.selected;const original=api;
      window.api=async(path,opts)=>{if(path.endsWith('/chat_answer')){
        requests.push({path,...JSON.parse(opts.body)});
        if(!window.answerFailed){await new Promise(r=>window.releaseAnswer=r);window.answerFailed=true;throw new Error('Answer receipt lost');}
        return {state:'queued'};
      }return original(path,opts);};}''')
    p.get_by_role('button',name='拒绝',exact=True).click();p.wait_for_function('!!window.releaseAnswer')
    p.evaluate("chatSwitch(null,'p2')");p.fill('#chat-compose','other draft');p.evaluate('releaseAnswer()')
    p.wait_for_timeout(80);p.evaluate('chatSwitch(first)')
    p.get_by_role('button',name='重试原回答',exact=True).click()
    expect(p.locator('.chat-approval-actions')).to_contain_text('回答已提交')
    answers=p.evaluate("requests.filter(r=>r.path.endsWith('/chat_answer')).map(r=>r.args)")
    assert len(answers)==2 and answers[0]['receipt']==answers[1]['receipt']
    assert answers[0]['answer'] is False and answers[1]['answer'] is False


def test_approval_success_after_navigation_updates_original_card(chat_page):
    p=chat_page;send(p)
    event(p,'chat',{'type':'approval','receipt':'task','request_id':'approve','method':'confirm','text':'Allow fixture?'},10)
    p.evaluate('''() => {window.first=ChatUI.selected;const original=api;
      window.api=async(path,opts)=>{if(path.endsWith('/chat_answer'))await new Promise(r=>window.releaseAnswer=r);return original(path,opts);};}''')
    p.get_by_role('button',name='拒绝',exact=True).click();p.wait_for_function('!!window.releaseAnswer')
    p.evaluate("chatSwitch(null,'p2')");p.evaluate('releaseAnswer()');p.wait_for_timeout(80)
    p.evaluate('chatSwitch(first)')
    expect(p.locator('.chat-approval-actions')).to_contain_text('回答已提交')
    expect(p.locator('.chat-approval-actions button')).to_have_count(0)


def test_first_send_returning_before_reply_adopts_created_session(chat_page):
    p=chat_page;p.evaluate('window.delaySend=true')
    p.fill('#chat-compose','send original');p.press('#chat-compose','Enter');p.wait_for_function('!!window.finishSend')
    p.evaluate("chatSwitch(null,'p2')");p.evaluate("chatSwitch(null,'p1')")
    p.fill('#chat-compose','next draft must survive');p.evaluate('finishSend()')
    p.wait_for_function('ChatUI.selected!==null',timeout=1800)
    expect(p.locator('#chat-compose')).to_have_value('next draft must survive')
    assert p.evaluate('chatView().busy') is False
    assert p.evaluate("requests.filter(r=>r.path.endsWith('/start')).length")==1


def test_logout_during_start_never_sends_prompt_with_another_login(chat_page):
    p=chat_page
    p.evaluate('''() => {const original=api;window.api=async(path,opts)=>{
      if(path.endsWith('/start'))await new Promise(r=>window.releaseStart=r);return original(path,opts);};}''')
    p.fill('#chat-compose','private first prompt');p.press('#chat-compose','Enter');p.wait_for_function('!!window.releaseStart')
    p.evaluate('chatDetach(true);S.session={csrf:"another-login"};S.page="login";releaseStart()')
    p.wait_for_timeout(150)
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/chat_prompt'))")
    assert p.evaluate('ChatUI.views.size')==0


def test_resume_finishes_in_original_view_after_leaving_and_returning(chat_page):
    p=chat_page;send(p);event(p,'session',{'status':'exited'},10)
    p.fill('#chat-compose','resume draft')
    p.evaluate('''() => {window.first=ChatUI.selected;const original=api;window.api=async(path,opts)=>{
      if(path.endsWith('/start'))await new Promise(r=>window.releaseStart=r);return original(path,opts);};void chatResume();}''')
    p.wait_for_function('!!window.releaseStart');p.evaluate("chatSwitch(null,'p2')");p.evaluate('chatSwitch(first)')
    p.evaluate('releaseStart()')
    p.wait_for_function('ChatUI.selected.id!==first.id',timeout=1800)
    expect(p.locator('#chat-compose')).to_have_value('resume draft')
    assert p.evaluate('chatView().resumeBusy') is False


def test_mapping_change_stops_remaining_upload_steps(chat_page):
    p=chat_page
    p.evaluate('''() => {const original=api;window.api=async(path,opts)=>{
      if(path.endsWith('/upload_begin')){await new Promise(r=>window.releaseUpload=r);return {received:0};}
      if(path.endsWith('/upload_chunk')){requests.push({path,...JSON.parse(opts.body)});return {received:5};}
      return original(path,opts);};}''')
    p.set_input_files('#chat-file-input',{'name':'one.txt','mimeType':'text/plain','buffer':b'hello'})
    p.wait_for_function('!!window.releaseUpload')
    p.evaluate("S.projects[0].root='/another-project';releaseUpload()")
    p.wait_for_timeout(100)
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/upload_chunk'))")
    expect(p.locator('#chat-files')).to_contain_text('上传失败')


def test_catalog_refresh_preserves_focused_model_option(chat_page):
    p=chat_page;p.click('#chat-model-picker')
    p.locator('[data-model="remote/advanced-model"]').focus()
    p.evaluate('chatLoadCatalog(true)')
    expect(p.locator('[data-model="remote/advanced-model"]')).to_be_focused()
    p.keyboard.press('Enter')
    expect(p.locator('#chat-model-name')).to_have_text('Advanced Model')


def test_attachment_library_cannot_delete_a_file_in_pending_delivery(chat_page):
    p=chat_page
    p.evaluate("window.library=[{file:'one',name:'one.txt',size:5,ready:true}];chatView().pending={attachments:['one']};chatInspector('files')")
    expect(p.locator('.chat-library-file').get_by_role('button',name='删除',exact=True)).to_be_disabled()
    p.evaluate('chatView().pending=null;chatLibraryRender()')
    expect(p.locator('.chat-library-file').get_by_role('button',name='删除',exact=True)).to_be_enabled()
