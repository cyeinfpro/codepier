"""Cross-project navigation and recovery. Real browser, deterministic native HTTP/SSE."""
from urllib.parse import parse_qs, urlsplit
import pytest
from playwright.sync_api import expect
from tests.test_chat_browser import chat_page, event
from tests.test_chat_complete_browser import send


def seed_rows(page):
    page.evaluate('''() => {
      const base={mode:'chat',provider:'pi',status:'running',online:true,updated:Date.now()/1000};
      window.rows=[
        {...base,id:'a'.repeat(32),project_id:'p1',title:'One conversation',root:'/workspace/one',cwd:'/workspace/one'},
        {...base,id:'b'.repeat(32),project_id:'p2',title:'Two conversation',root:'/workspace/two',cwd:'/workspace/two/src'}
      ];return chatList();
    }''')


def test_global_history_default_and_cross_project_owner(chat_page):
    p=chat_page;seed_rows(p)
    path=p.evaluate("requests.filter(r=>r.path.startsWith('/api/native/sessions?')).at(-1).path")
    assert not parse_qs(urlsplit(path).query).get('project')
    two=p.locator('[data-session-id="'+'b'*32+'"]')
    expect(two).to_contain_text('Workspace two')
    two.click()
    assert p.evaluate('ChatUI.project')=='p2'
    assert p.evaluate('ChatUI.cwd')=='src'
    assert 'project=p2' in p.evaluate('streams.at(-1).url')
    expect(p.locator('#chat-history-project')).to_have_value('')


def test_catalog_timeout_releases_pending_and_retry_recovers(chat_page):
    p=chat_page;p.clock.install()
    p.evaluate('ChatUI.catalogCache.clear();window.delayCatalog=true;void chatLoadCatalog()')
    p.wait_for_function('!!window.finishCatalog')
    p.clock.fast_forward(25000)
    expect(p.locator('#chat-catalog-notice')).to_be_visible(timeout=1200)
    assert not p.evaluate('chatView().catalogLoading')
    assert p.evaluate('[...ChatUI.catalogCache.values()][0].pending.size')==0
    p.evaluate('window.delayCatalog=false');p.click('#chat-catalog-retry')
    expect(p.locator('#chat-model-name')).to_have_text('Native Model')
    expect(p.locator('#chat-catalog-notice')).to_be_hidden()
    p.evaluate('finishCatalog()')
    assert not p.evaluate('chatView().catalogLoading')


def test_refresh_does_not_join_an_older_stalled_read(chat_page):
    p=chat_page
    p.evaluate('ChatUI.catalogCache.clear();window.delayCatalog=true;void chatLoadCatalog()')
    p.wait_for_function('!!window.finishCatalog')
    p.evaluate('window.delayCatalog=false;void chatLoadCatalog(true)')
    expect(p.locator('#chat-model-name')).to_have_text('Native Model',timeout=1500)
    assert not p.evaluate('chatView().catalogLoading')
    p.evaluate('finishCatalog()')


def test_live_settings_do_not_hide_refreshed_model_choices(chat_page):
    p=chat_page;send(p)
    event(p,'chat',{'type':'settings','model':{'provider':'old','id':'retired'},'models':[{'provider':'old','id':'retired','name':'Old Model'}]},10)
    p.evaluate('chatLoadCatalog(true)');p.click('#chat-model-picker')
    expect(p.locator('[data-model="local/native-model"]')).to_be_visible()
    expect(p.locator('[data-model="remote/advanced-model"]')).to_be_visible()


def test_late_queue_edit_preserves_other_project_draft(chat_page):
    p=chat_page;send(p)
    p.evaluate('''() => {
      chatView().queue=[{receipt:'q1',kind:'prompt',state:'queued',text:'queued in one'}];
      const original=api;window.api=async(path,opts)=>{
        if(path.endsWith('/chat_queue'))return {commands:chatView().queue};
        if(path.endsWith('/chat_cancel'))await new Promise(r=>window.releaseCancel=r);
        return original(path,opts);
      };chatInspector('queue');
    }''')
    p.get_by_role('button',name='撤回并编辑',exact=True).click()
    p.wait_for_function('!!window.releaseCancel')
    p.evaluate("chatSwitch(null,'p2',{cwd:'.'})");p.fill('#chat-compose','project two must survive')
    p.evaluate('releaseCancel()');p.wait_for_timeout(400)
    expect(p.locator('#chat-compose')).to_have_value('project two must survive')


def test_upload_navigation_does_not_strand_attachment(chat_page):
    p=chat_page
    p.evaluate('''() => {
      window.uploadView=chatView();const original=api;
      window.api=async(path,opts)=>{
        const b=opts?.body?JSON.parse(opts.body):{};
        if(path.endsWith('/upload_begin')){await new Promise(r=>window.releaseUpload=r);return {received:0};}
        if(path.endsWith('/upload_chunk')){if(b.project!=='p1')throw new Error('wrong project');return {received:b.args.offset+atob(b.args.data).length};}
        if(path.endsWith('/upload_finish')){if(b.project!=='p1')throw new Error('wrong project');return {ready:true};}
        return original(path,opts);
      };
    }''')
    p.set_input_files('#chat-file-input',{'name':'one.txt','mimeType':'text/plain','buffer':b'hello'})
    p.wait_for_function('!!window.releaseUpload')
    p.evaluate("chatSwitch(null,'p2',{cwd:'.'})");p.evaluate('releaseUpload()')
    p.wait_for_function('uploadView.files[0].ready===true',timeout=1800)
    assert p.evaluate('chatView().files.length')==0
    p.evaluate("chatSwitch(null,'p1',{cwd:'.'})")
    expect(p.locator('#chat-files')).to_contain_text('已校验')
