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
