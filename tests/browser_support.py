"""Real Chromium UI exercises; native wire processes are covered separately.

These deterministic browser tests mock the authenticated HTTP/event boundary, not
assistant rendering. No external model calls or persisted browser storage.
"""
from pathlib import Path
import json
import pytest
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def open_project_collaboration(page, project_id):
    action = page.locator(f'[data-action="project-collaboration"][data-id="{project_id}"]')
    action.locator('xpath=ancestor::details').locator(':scope > summary').click()
    action.click()


def open_project_codex(page, project_id):
    page.locator(f'[data-native-launch][data-project="{project_id}"]').click()
    dialog = page.locator('#chat-project-dialog')
    dialog.locator('[name="cli"][value="codex"]').check()
    dialog.locator(f'#chat-project-results [data-project-id="{project_id}"]').click()


def open_chat_settings(page):
    if not page.locator('#chat-options').is_visible():
        page.locator('#chat-options-toggle').click()
    expect(page.locator('#chat-options')).to_be_visible()


def open_chat_inspector(page, tab='overview'):
    if not page.locator('#chat-inspector').is_visible():
        page.locator('#chat-inspector-toggle').click()
    if tab != 'overview':
        page.locator(f'[data-chat-tab="{tab}"]').click()

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
    # Chat dialogs use the same layer host primitives as the production page.
    page.add_script_tag(path=str(ROOT / 'web/core/bundle.js'))
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


def login_panel(page, stack, route: str = "overview") -> None:
    page.goto(f"{stack.url}/#{route}", wait_until="domcontentloaded")
    expect(page.locator("#login-form")).to_be_visible()
    page.fill('#username', 'admin');page.locator("#password").fill(stack.password)
    page.locator("#login-form button[type=submit]").click()
    # Login and the first page load share the same bounded readiness budget.
    expect(page.locator(".shell")).to_be_visible(timeout=15_000)
    expect(page.locator(".skeleton")).to_have_count(0, timeout=15_000)

def set_appearance(page, scheme: str) -> None:
    result = page.evaluate(
        """scheme => {
          if (!window.CodePierAppearance) return {missing: true};
          window.CodePierAppearance.setPreference(scheme);
          return {
            preference: window.CodePierAppearance.getPreference(),
            scheme: window.CodePierAppearance.getScheme(),
            dataset: document.documentElement.dataset.appearance,
          };
        }""",
        scheme,
    )
    assert result == {"preference": scheme, "scheme": scheme, "dataset": scheme}
    page.wait_for_function(
        "scheme => document.documentElement.dataset.appearance === scheme", arg=scheme
    )
    controls = page.locator("[data-ui-appearance]")
    expect(controls).not_to_have_count(0)
    for index in range(controls.count()):
        expect(controls.nth(index)).to_have_value(scheme)
