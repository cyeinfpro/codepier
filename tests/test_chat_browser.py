"""Real Chromium UI exercises; native wire processes are covered separately.

These deterministic browser tests mock the authenticated HTTP/event boundary, not
assistant rendering. No external model calls or persisted browser storage.
"""
from pathlib import Path
import json
import pytest
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]

@pytest.fixture
def chat_page(request, chat_browser_pool):
    browser = chat_browser_pool(getattr(request,'param','chromium'))
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.set_content('<div id="page"></div>')
    page.add_style_tag(path=str(ROOT / 'web/chat.css'))
    page.evaluate('''() => {
      window.$ = s => document.querySelector(s);
      window.esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      window.S={session:{csrf:'fixture'},page:'native',work:{},projects:[{id:'p1',alias:'Workspace one',device_id:'node-1',root:'/workspace/one'},{id:'p2',alias:'Workspace two',device_id:'node-1',root:'/workspace/two'}]};
      window.loadBasics=async()=>{};
      window.uid=()=>[...crypto.getRandomValues(new Uint8Array(16))].map(b=>b.toString(16).padStart(2,'0')).join(''); window.requests=[];window.streams=[];window.nativeSha256=async()=> 'a'.repeat(64);
      window.EventSource=class {
        constructor(url){this.url=url;this.handlers={};streams.push(this);}
        addEventListener(type,fn){this.handlers[type]=fn;}
        close(){this.closed=true;}
        emit(type,data,id){this.handlers[type]?.({data:JSON.stringify(data),lastEventId:String(id)});}
      };
      window.api=async(path,opts={})=>{
        const body=opts.body?JSON.parse(opts.body):{};requests.push({path,...body});
        if(path.startsWith('/api/native/sessions?'))return {sessions:window.rows||[]};
        if(path.endsWith('/start'))return {id:body.args.id,provider:body.args.cli,mode:'chat',project_id:body.project,title:'First conversation',status:'running',root:'/workspace',cwd:'/workspace'};
        if(path.endsWith('/chat_catalog')){ if(window.delayCatalog)await new Promise(r=>window.finishCatalog=r); if(window.catalogFailure)throw new Error('Native catalog is offline');const cli=body.args.cli||'pi';return {cli,models:cli==='pi'?[{id:'native-model',provider:'local',name:'Native Model',reasoning:true,input:['text','image']},{id:'advanced-model',provider:'remote',name:'Advanced Model',reasoning:true,input:['text','image']}]:[{id:'native-model',model:'native-model',displayName:'Native Model',supportedReasoningEfforts:[{reasoningEffort:'low'},{reasoningEffort:'high'}]}],model:cli==='pi'?(body.args.model?{provider:body.args.model.split('/')[0],id:body.args.model.split('/').slice(1).join('/')}:{provider:'local',id:'native-model'}):body.args.model||'native-model',thinking_levels:['off','low','medium','high'],thinkingLevel:'medium',...(body.args.include_commands===false?{}:{commands:[{name:'skill:review',description:'Review project changes',source:'skill'}]}),capabilities:{steer:true,stats:true,commands:true,compact:true}};}
        if(path.endsWith('/receipt'))return {state:window.receiptState||'completed'};
        if(path.endsWith('/chat_queue'))return {commands:window.queue||[]};
        if(path.endsWith('/chat_cancel'))return {state:'completed',target_state:'cancelled'};
        if(path.endsWith('/upload_list'))return {files:window.library||[]};
        if(path.endsWith('/chat_prompt')&&window.delaySend)await new Promise(r=>window.finishSend=r);
        return {state:'queued'};
      };
      document.addEventListener('click',e=>{const b=e.target.closest('[data-nav]');if(b)navigate(b.dataset.nav);});
      window.navigate=async page=>{S.page=page;chatDetach();$('#page').textContent='Management page';};
    }''')
    page.add_script_tag(path=str(ROOT / 'web/chat-markdown.js'))
    page.add_script_tag(path=str(ROOT / 'web/chat-panels.js'))
    page.add_script_tag(path=str(ROOT / 'web/chat-chrome.js'))
    page.add_script_tag(path=str(ROOT / 'web/chat-history.js'))
    page.add_script_tag(path=str(ROOT / 'web/chat-catalog.js'))
    page.add_script_tag(path=str(ROOT / 'web/chat.js'))
    page.evaluate('chatPage()')
    try:
        yield page
        assert not errors,errors
    finally:
        page.context.close()


def event(page, kind, value, cursor):
    page.evaluate('([kind,value,cursor])=>streams.at(-1).emit(kind,value,cursor)', [kind,value,cursor])


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
    page.screenshot(path=str(ROOT/'docs/evidence/chat-complete-20260915/desktop-chat-test.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
    page.click('#chat-history-toggle')
    expect(page.locator('#chat-root')).to_have_class('chat-workspace drawer-open')
    page.click('#chat-shade',position={'x':350,'y':200})
    page.screenshot(path=str(ROOT/'docs/evidence/chat-complete-20260915/mobile-chat-test.png'))


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
