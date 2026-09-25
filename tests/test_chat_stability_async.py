"""A delayed read or interrupted connection must not undo confirmed chat state."""
import pytest
from playwright.sync_api import expect

from tests.browser_support import chat_page, event
from tests.test_chat_complete_browser import send


pytestmark = pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)


@pytest.mark.parametrize('panel,action,property_name,selector', [
    ('queue', 'chat_queue', 'commands', '#chat-queue-list'),
    ('files', 'upload_list', 'files', '#chat-library-list'),
])
def test_late_panel_read_cannot_restore_old_items(chat_page, panel, action, property_name, selector):
    p = chat_page
    send(p)
    p.evaluate('''([panel,action,property]) => {
      const original=api;let reads=0;
      window.api=async(path,opts)=>{
        if(!path.endsWith('/'+action))return original(path,opts);
        if(++reads===1){await new Promise(r=>window.releasePanelRead=r);return {[property]:[
          {receipt:'removed',kind:'chat_prompt',state:'queued',text:'Already removed',
           file:'removed',name:'Already removed',size:5,ready:true}
        ]};}
        return {[property]:[]};
      };
      chatInspector(panel);
    }''', [panel, action, property_name])
    p.wait_for_function('!!window.releasePanelRead')
    p.evaluate('panel=>panel==="queue"?chatLoadQueue():chatLoadLibrary()', panel)
    p.evaluate('releasePanelRead()')
    p.wait_for_timeout(80)
    expect(p.locator(selector)).not_to_contain_text('Already removed')
    assert p.evaluate('panel=>panel==="queue"?chatView().queue.length:chatView().library.length', panel) == 0


@pytest.mark.parametrize('panel,action,property_name,selector', [
    ('queue', 'chat_queue', 'commands', '#chat-queue-list'),
    ('files', 'upload_list', 'files', '#chat-library-list'),
])
def test_late_panel_failure_cannot_replace_a_successful_refresh(chat_page, panel, action, property_name, selector):
    p = chat_page
    send(p)
    p.evaluate('''([panel,action,property]) => {
      const original=api;let reads=0;
      window.api=async(path,opts)=>{
        if(!path.endsWith('/'+action))return original(path,opts);
        if(++reads===1){await new Promise(r=>window.releasePanelRead=r);throw new Error('Obsolete failure');}
        return {[property]:[]};
      };
      chatInspector(panel);
    }''', [panel, action, property_name])
    p.wait_for_function('!!window.releasePanelRead')
    p.evaluate('panel=>panel==="queue"?chatLoadQueue():chatLoadLibrary()', panel)
    p.evaluate('releasePanelRead()')
    p.wait_for_timeout(80)
    expect(p.locator(selector)).not_to_contain_text('Obsolete failure')


def test_reconnect_verifies_uncertain_settings_without_resending(chat_page):
    p = chat_page
    send(p)
    p.evaluate('''() => {
      const original=api;
      window.api=async(path,opts)=>{
        if(path.endsWith('/chat_settings')){
          requests.push({path,...JSON.parse(opts.body)});
          throw Object.assign(new Error('Settings response lost'),{code:'NETWORK_UNCERTAIN'});
        }
        return original(path,opts);
      };
      return chatSubmitSettings({effort:'high'});
    }''')
    assert p.evaluate('chatView().settingOp.state') == 'uncertain'
    p.fill('#chat-compose', 'Continue after reconnect')
    p.evaluate('streams.at(-1).onopen()')
    p.wait_for_function('chatView().settingOp===null', timeout=1800)
    expect(p.locator('#chat-send')).to_be_enabled()
    assert p.evaluate("requests.filter(r=>r.path.endsWith('/chat_settings')).length") == 1
    receipt = p.evaluate("requests.find(r=>r.path.endsWith('/chat_settings')).args.receipt")
    assert p.evaluate("requests.find(r=>r.path.endsWith('/receipt')).args.receipt") == receipt


@pytest.mark.parametrize('state', ['stopping', 'orphaned'])
def test_enter_cannot_resume_a_process_whose_exit_is_unconfirmed(chat_page, state):
    p = chat_page
    send(p)
    event(p, 'session', {'status': state}, 10)
    before = p.evaluate('requests.length')
    p.fill('#chat-compose', 'Keep this draft until exit is confirmed')
    p.press('#chat-compose', 'Enter')
    p.wait_for_timeout(80)
    assert not p.evaluate('(before)=>requests.slice(before).some(r=>/\\/(start|chat_prompt)$/.test(r.path))', before)
    expect(p.locator('#chat-compose')).to_have_value('Keep this draft until exit is confirmed')


@pytest.mark.parametrize('retry_with_message', [True, False])
def test_retry_after_uncertain_resume_cannot_create_a_second_process(chat_page, retry_with_message):
    p = chat_page
    send(p)
    event(p, 'session', {'status': 'exited'}, 10)
    p.evaluate('''() => {
      const original=api;window.resumeFailed=false;
      window.api=async(path,opts)=>{
        if(path.endsWith('/start')&&!resumeFailed){
          requests.push({path,...JSON.parse(opts.body)});resumeFailed=true;
          throw Object.assign(new Error('Resume response lost'),{code:'NETWORK_UNCERTAIN'});
        }
        return original(path,opts);
      };
      return chatResume().catch(()=>{});
    }''')
    # The first launch might be live already. Even newly selected settings must
    # not change the launch signature when confirming that original request.
    p.evaluate("chatView().selectedModel='remote/advanced-model';chatView().selectedEffort='high'")
    if retry_with_message:
        p.fill('#chat-compose', 'Continue the resumed process')
        p.press('#chat-compose', 'Enter')
        expect(p.locator('#chat-compose')).to_have_value('')
    else:
        p.evaluate('chatResume()')
    starts = p.evaluate("requests.filter(r=>r.path.endsWith('/start')).map(r=>r.args)")
    assert len(starts) == 3
    assert starts[1] == starts[2]


def test_resume_cannot_bypass_an_uncertain_message_launch(chat_page):
    p = chat_page
    send(p)
    event(p, 'session', {'status': 'exited'}, 10)
    p.evaluate('''() => {
      const original=api;
      window.api=async(path,opts)=>{
        if(path.endsWith('/start')){
          requests.push({path,...JSON.parse(opts.body)});
          throw Object.assign(new Error('Launch response lost'),{code:'NETWORK_UNCERTAIN'});
        }
        return original(path,opts);
      };
    }''')
    p.fill('#chat-compose', 'Preserve uncertain message')
    p.press('#chat-compose', 'Enter')
    p.wait_for_function('chatView().pending && !chatView().busy')
    starts_before = p.evaluate("requests.filter(r=>r.path.endsWith('/start')).length")
    p.evaluate('chatResume()')
    assert p.evaluate("requests.filter(r=>r.path.endsWith('/start')).length") == starts_before
    expect(p.locator('#chat-compose')).to_have_value('Preserve uncertain message')
    expect(p.locator('#chat-retry')).to_be_visible()


def test_successful_attachment_delete_invalidates_an_older_library_read(chat_page):
    p = chat_page
    p.evaluate('''() => {
      window.library=[{file:'removed',name:'Remove this file',size:5,ready:true}];
      chatInspector('files');
    }''')
    expect(p.locator('#chat-library-list')).to_contain_text('Remove this file')
    p.evaluate('''() => {
      const original=api;
      window.api=async(path,opts)=>{
        if(path.endsWith('/upload_list')){
          await new Promise(r=>window.releaseLibrary=r);return {files:library};
        }
        return original(path,opts);
      };
      void chatLoadLibrary();
    }''')
    p.wait_for_function('!!window.releaseLibrary')
    p.locator('#chat-library-list').get_by_role('button', name='删除', exact=True).click()
    p.click('#chat-dialog-confirm')
    expect(p.locator('#chat-library-list')).not_to_contain_text('Remove this file')
    p.evaluate('releaseLibrary()')
    p.wait_for_timeout(80)
    expect(p.locator('#chat-library-list')).not_to_contain_text('Remove this file')


def test_known_resume_rejection_allows_correcting_launch_settings(chat_page):
    p = chat_page
    send(p)
    event(p, 'session', {'status': 'exited'}, 10)
    p.evaluate('''async () => {
      const original=api;
      window.api=async(path,opts)=>{
        if(path.endsWith('/start')&&JSON.parse(opts.body).args.model==='invalid'){
          requests.push({path,...JSON.parse(opts.body)});
          throw Object.assign(new Error('Invalid model'),{code:'CLI_INVALID',status:400});
        }
        return original(path,opts);
      };
      chatView().selectedModel='invalid';await chatResume().catch(()=>{});
      chatView().selectedModel='remote/advanced-model';await chatResume();
    }''')
    starts = p.evaluate("requests.filter(r=>r.path.endsWith('/start')).map(r=>r.args)")
    assert starts[-1]['model'] == 'remote/advanced-model'
    assert starts[-1]['id'] != starts[-2]['id']


@pytest.mark.parametrize('code', ['CLI_LIMIT', 'CLI_INVALID'])
def test_send_rejection_releases_the_uncertain_resume_launch(chat_page, code):
    p = chat_page
    send(p)
    event(p, 'session', {'status': 'exited'}, 10)
    p.evaluate('''async code => {
      const original=api;let starts=0;
      window.api=async(path,opts)=>{
        if(path.endsWith('/start')&&++starts<=2){
          requests.push({path,...JSON.parse(opts.body)});
          throw Object.assign(new Error(starts===1?'Resume response lost':'Launch rejected'),
            {code:starts===1?'NETWORK_UNCERTAIN':code});
        }
        return original(path,opts);
      };
      chatView().selectedModel='local/native-model';await chatResume().catch(()=>{});
    }''', code)
    p.fill('#chat-compose', 'Keep this message after launch rejection')
    p.press('#chat-compose', 'Enter')
    p.wait_for_function('!chatView().busy && chatView().pending===null')
    assert p.evaluate('chatView().resumeRequest') is None
    assert p.evaluate('chatView().resumeId') is None
    expect(p.locator('#chat-compose')).to_have_value('Keep this message after launch rejection')
    p.evaluate("chatView().selectedModel='remote/advanced-model'")
    p.press('#chat-compose', 'Enter')
    expect(p.locator('#chat-compose')).to_have_value('')
    starts = p.evaluate("requests.filter(r=>r.path.endsWith('/start')).map(r=>r.args)")
    assert starts[-3] == starts[-2]
    assert starts[-1]['id'] != starts[-2]['id']
    assert starts[-1]['model'] == 'remote/advanced-model'


def test_prompt_rejection_after_resuming_keeps_the_started_process(chat_page):
    p = chat_page
    send(p)
    event(p, 'session', {'status': 'exited'}, 10)
    p.evaluate('''async () => {
      const original=api;let lostResume=false,rejectedPrompt=false;
      window.api=async(path,opts)=>{
        if(path.endsWith('/start')&&!lostResume){
          lostResume=true;requests.push({path,...JSON.parse(opts.body)});
          throw Object.assign(new Error('Resume response lost'),{code:'NETWORK_UNCERTAIN'});
        }
        if(path.endsWith('/chat_prompt')&&!rejectedPrompt){
          rejectedPrompt=true;requests.push({path,...JSON.parse(opts.body)});
          throw Object.assign(new Error('Queue is full'),{code:'CLI_BACKPRESSURE'});
        }
        return original(path,opts);
      };
      await chatResume().catch(()=>{});
    }''')
    p.fill('#chat-compose', 'Retry this message in the resumed process')
    p.press('#chat-compose', 'Enter')
    p.wait_for_function('!chatView().busy && chatView().pending===null')
    resumed_id = p.evaluate('ChatUI.selected.id')
    starts_before = p.evaluate("requests.filter(r=>r.path.endsWith('/start')).length")
    expect(p.locator('#chat-compose')).to_have_value('Retry this message in the resumed process')
    p.press('#chat-compose', 'Enter')
    expect(p.locator('#chat-compose')).to_have_value('')
    assert p.evaluate("requests.filter(r=>r.path.endsWith('/start')).length") == starts_before
    prompts = p.evaluate("requests.filter(r=>r.path.endsWith('/chat_prompt')).map(r=>r.args)")
    assert prompts[-2]['id'] == prompts[-1]['id'] == resumed_id
