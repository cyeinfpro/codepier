"""Finish regressions: buttons, draft safety, late replies and reconnects."""
import pytest
from playwright.sync_api import expect
from tests.test_chat_browser import chat_page,event
from tests.test_chat_complete_browser import send


def test_withdraw_and_edit_returns_to_original_view_without_losing_other_draft(chat_page):
    p=chat_page;send(p)
    p.evaluate('''() => {window.first=ChatUI.selected;window.firstView=chatView();window.queue=[{receipt:'one',kind:'chat_prompt',state:'queued',text:'Recovered one'}];const original=api;window.api=async(path,opts)=>{if(path.endsWith('/chat_cancel'))await new Promise(r=>window.releaseCancel=r);return original(path,opts);};chatInspector('queue');}''')
    p.get_by_role('button',name='撤回并编辑',exact=True).click();p.wait_for_function('!!window.releaseCancel')
    p.evaluate("chatSwitch(null,'p2',{cwd:'.'})");p.fill('#chat-compose','keep two')
    p.evaluate('releaseCancel()');p.wait_for_function("firstView.recoveredQueueDraft==='Recovered one'")
    expect(p.locator('#chat-compose')).to_have_value('keep two')
    p.evaluate('chatSwitch(first)');p.click('#chat-recover-queue-draft')
    expect(p.locator('#chat-compose')).to_have_value('Recovered one')
    expect(p.locator('#chat-recover-queue-draft')).to_have_count(0)


def test_multiple_recovered_messages_and_existing_draft_are_retained(chat_page):
    p=chat_page;send(p);p.fill('#chat-compose','keep current')
    p.evaluate("async()=>{await chatEditQueued({receipt:'one',text:'one'});await chatEditQueued({receipt:'two',text:'two'});}")
    assert p.evaluate('chatView().recoveredQueueDrafts.length')==2
    p.click('#chat-recover-queue-draft');p.get_by_role('button',name='取消',exact=True).click()
    expect(p.locator('#chat-compose')).to_have_value('keep current')
    assert p.evaluate('chatView().recoveredQueueDrafts.length')==2
    p.fill('#chat-compose','');p.click('#chat-recover-queue-draft')
    expect(p.locator('#chat-compose')).to_have_value('one')
    assert p.evaluate('chatView().recoveredQueueDrafts.length')==1


def test_cwd_validates_early_and_enter_preserves_directory_drafts(chat_page):
    p=chat_page;p.fill('#chat-compose','root draft');p.click('#chat-cwd-button')
    p.fill('#chat-cwd-input','../outside');p.press('#chat-cwd-input','Enter')
    expect(p.locator('#chat-cwd-error')).to_contain_text('相对目录')
    assert p.evaluate('ChatUI.cwd')=='.'
    p.fill('#chat-cwd-input','src');p.press('#chat-cwd-input','Enter')
    assert p.evaluate('ChatUI.cwd')=='src'
    expect(p.locator('#chat-popover')).to_be_hidden()
    p.evaluate("chatSwitch(null,'p1',{cwd:'.'})")
    expect(p.locator('#chat-compose')).to_have_value('root draft')
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/start'))")


def test_repeated_resume_uses_new_session_id_for_each_stopped_process(chat_page):
    p=chat_page;send(p);ids=[p.evaluate('ChatUI.selected.id')]
    for i in range(2):
        event(p,'session',{'status':'exited'},10+i)
        p.locator('.chat-overflow summary').click();p.click('#chat-resume')
        p.wait_for_function('(id)=>ChatUI.selected.id!==id',arg=ids[-1])
        ids.append(p.evaluate('ChatUI.selected.id'))
    assert len(set(ids))==3
    starts=p.evaluate("requests.filter(r=>r.path.endsWith('/start')).map(r=>r.args)")
    assert starts[1]['continue_session']==ids[0] and starts[2]['continue_session']==ids[1]


def test_delete_does_not_recreate_deleted_view(chat_page):
    p=chat_page;send(p);key=p.evaluate('chatKey()');event(p,'session',{'status':'exited'},10)
    p.locator('.chat-overflow summary').click();p.click('#chat-delete');p.click('#chat-dialog-confirm')
    p.wait_for_function('ChatUI.selected===null')
    assert not p.evaluate('(key)=>ChatUI.views.has(key)',key)


def test_command_search_survives_catalog_updates_and_focus_returns(chat_page):
    p=chat_page;p.set_viewport_size({'width':390,'height':844});p.click('#chat-commands')
    expect(p.locator('#chat-inspector-close')).to_be_focused()
    p.fill('#chat-command-filter','review');p.evaluate('chatInspectorRender()')
    expect(p.locator('#chat-command-filter')).to_have_value('review')
    expect(p.locator('#chat-command-filter')).to_be_focused()
    p.click('#chat-inspector-close');expect(p.locator('#chat-commands')).to_be_focused()


def test_returning_online_enables_send_without_refresh_or_losing_draft(chat_page):
    p=chat_page
    p.evaluate("S.devices=[{id:'node-1',name:'Studio',online:false}];chatSyncChrome()")
    p.fill('#chat-compose','keep offline draft');expect(p.locator('#chat-send')).to_be_disabled()
    p.evaluate('''() => {const original=api;window.api=async(path,opts)=>path==='/api/projects'?{projects:S.projects}:path==='/api/devices'?{devices:[{id:'node-1',name:'Studio',online:true}]}:original(path,opts);return chatRefreshTargets();}''')
    expect(p.locator('#chat-send')).to_be_enabled();expect(p.locator('#chat-compose')).to_have_value('keep offline draft')
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/start'))")


def test_starter_requires_confirmation_before_replacing_a_draft(chat_page):
    p=chat_page;p.fill('#chat-compose','keep draft');p.locator('[data-chat-starter]').first.click()
    expect(p.get_by_role('dialog',name='替换当前草稿？')).to_be_visible()
    p.get_by_role('button',name='取消',exact=True).click();expect(p.locator('#chat-compose')).to_have_value('keep draft')
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/start'))")


def test_malformed_catalog_retry_keeps_last_successful_choices(chat_page):
    p=chat_page
    p.evaluate('''() => {const original=api;window.badCatalog=true;window.api=async(path,opts)=>path.endsWith('/chat_catalog')&&badCatalog?{models:null}:original(path,opts);return chatLoadCatalog(true);}''')
    expect(p.locator('#chat-catalog-notice')).to_contain_text('有效')
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    p.evaluate('badCatalog=false');p.click('#chat-catalog-retry');expect(p.locator('#chat-catalog-notice')).to_be_hidden()


def test_project_close_button_enter_never_selects_or_starts(chat_page):
    p=chat_page;p.fill('#chat-compose','saved draft');p.click('#chat-new')
    p.focus('#chat-project-dialog-close');p.keyboard.press('Enter')
    expect(p.locator('#chat-project-dialog')).to_have_count(0)
    expect(p.locator('#chat-compose')).to_have_value('saved draft')
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/start'))")


def test_logout_cancels_history_refresh_and_pending_directory_requests(chat_page):
    p=chat_page;p.evaluate('chatDetach(true);S.session=null;S.page="login"')
    assert p.evaluate('ChatUI.historyTimer') is None
    assert p.evaluate('ChatUI.catalogCache.size')==0


def test_windows_session_restores_its_relative_cwd(chat_page):
    p=chat_page
    p.evaluate(r'''chatSwitch({id:'windows',project_id:'p2',provider:'pi',status:'exited',root:'C:\\Work\\Project',cwd:'C:\\Work\\Project\\src',mode:'chat'},'p2')''')
    assert p.evaluate('ChatUI.cwd')=='src'
