"""Presentation work budgets; deterministic counts rather than machine-dependent FPS.

Browser-only harnesses exercise the shipped modules. Real Hub/relay, permission,
IME and mobile flows remain covered by the adjacent integration test suites.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.browser


def boot_relay(page, source=None):
    page.set_content('''<div class="collaboration cc-workspace cc-relay-focus">
      <div class="cc-feed" style="height:200px;overflow:auto"><div class="cc-message-list">
        <article data-message-id="m"><div data-rich-message="m">select this message</div></article>
      </div><div id="cc-relay-outbox"></div></div>
      <form id="cc-command"><textarea name="request" id="cc-message-input" style="width:300px"></textarea>
      <button type="submit">send</button></form><div id="cc-relay-network" hidden></div></div>''')
    page.evaluate('''() => {
      window.S = {session:'s', space_id:'space', page:'collaboration'};
      window.metrics = {patches:0, heightReads:0};
      window.record = {id:'m',body_text:'select this message'};
      window.c = {messages:[record], mentions:['dot'], reply:'', draft:'', relayOutbox:[]};
      window.state = {session:'s',space:'space',project:'p',environment:'production',
        generation:1,refreshSequence:0,chats:new Map([['c',c]]),members:[],
        snapshot:{room:{state:'active'},can_manage:true,capabilities:{native_dot_relay:true}}};
      window.chatRichMarkdown = (node, value) => {node.textContent=value;node._chatSource=value;};
      Object.defineProperty(document.querySelector('textarea'), 'scrollHeight', {
        configurable:true, get(){metrics.heightReads++;return 64;}
      });
    }''')
    page.add_script_tag(path=str(ROOT / 'web/collaboration-runtime.js'))
    page.add_script_tag(path=str(source or ROOT / 'web/collaboration-relay.js'))
    page.evaluate('''() => {
      window.relay = CodePierCollaborationRelay.create({state,E:String,button:()=>'',
        chat:()=>c,chatScope:()=>({project:'p',environment_id:'production'}),mentionSlots:()=>[{id:'dot',duplex:true,state:'registered'}],
        messageById:()=>record,messageText:m=>m.body_text,requestId:()=>'',
        dots:{isConversationMessage:()=>true,refreshCards:()=>{}},
        delegation:{messageCard:()=>'',selectPolicy:()=>{}},
        patchTimeline:()=>metrics.patches++,patchDraftContext:()=>{},
        patchDelegationComposer:()=>{},mergeMessages:()=>{},recoverAccess:()=>{}});
      relay.bind(document.querySelector('.collaboration'));
      document.querySelector('textarea').focus();
    }''')
    page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')


def measure_editor(page):
    return page.evaluate('''async () => {
      metrics.patches=0;metrics.heightReads=0;
      const area=document.querySelector('textarea');
      for(let i=0;i<100;i++){
        area.value+='文';
        area.dispatchEvent(new Event('input',{bubbles:true}));
        document.dispatchEvent(new Event('selectionchange'));
      }
      await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
      return {...metrics};
    }''')


def boot_workspace(page, source=None):
    page.set_content('<main></main>')
    page.add_script_tag(path=str(ROOT / 'web/collaboration-runtime.js'))
    page.add_script_tag(path=str(source or ROOT / 'web/collaboration-workspace.js'))
    page.evaluate('''() => {
      const esc = value => String(value ?? '').replace(/[&<>"']/g, c=>
        ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      window.c = {draft:'',messages:Array.from({length:1000},(_,i)=>({id:'m'+i,
        author_kind:'owner',project_id:'p',environment_id:'production',body_text:'message '+i,
        server_sequence:i+1,relay_receipts:[{state:'saved'}]}))};
      window.state={space:'s',project:'p',environment:'production',view:'discussion',chats:new Map(),
        snapshot:{room:{state:'active'},can_manage:true,join_slots:[]}};
      window.renders={jobs:0,monitor:0};
      window.ui=CodePierCollaborationWorkspace.create({state,E:esc,
        button:(action,label)=>`<button data-cc-action="${action}">${label}</button>`,
        chat:()=>c,mentionSlots:()=>[],projectLabel:()=> 'Project',messageText:m=>m.body_text,
        renderJobs:()=>{renders.jobs++;return '<p>jobs</p>';},
        renderAgents:()=>'<form id="agent"><input value="retained"></form>',
        renderMonitor:()=>{renders.monitor++;return '<div class="cc-monitor-status">status</div><form id="probe"><input value="retained"></form>';},
        bindDetails:()=>{},onOpen:()=>{}});
      document.querySelector('main').innerHTML=`<div class="cc-workspace" data-project="p"
        data-environment="production"><div class="cc-room-body">${ui.navigation()}${ui.summary()}
        <div id="cc-discussion-host"><textarea id="cc-message-input"></textarea></div>
        ${ui.panel()}</div></div>`;
      ui.open('jobs');ui.open('monitor');ui.open('discussion');
    }''')


def measure_hidden(page):
    return page.evaluate('''() => {
      renders.jobs=0;renders.monitor=0;
      for(let i=0;i<100;i++)ui.refresh();
      return {...renders};
    }''')


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_caret_events_do_not_render_history_and_autosize_is_frame_bounded(chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context()
    page = context.new_page()
    try:
        boot_relay(page)
        metrics = measure_editor(page)
        assert metrics['patches'] == 0, metrics
        assert metrics['heightReads'] <= 1, metrics
        page.evaluate('''() => {
          window.metrics.patches=0;
          const sel=getSelection(), range=document.createRange();
          range.selectNodeContents(document.querySelector('[data-rich-message]'));
          sel.removeAllRanges();sel.addRange(range);
          document.dispatchEvent(new Event('selectionchange'));
          sel.removeAllRanges();document.dispatchEvent(new Event('selectionchange'));
        }''')
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        assert page.evaluate('metrics.patches') == 1
        page.evaluate('''() => {
          metrics.patches=0;
          const sel=getSelection(),range=document.createRange();
          range.selectNodeContents(document.querySelector('[data-rich-message]'));
          sel.addRange(range);document.dispatchEvent(new Event('selectionchange'));
          sel.removeAllRanges();document.dispatchEvent(new Event('selectionchange'));
          relay.detach();
        }''')
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
        assert page.evaluate('metrics.patches') == 0
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_hidden_panels_do_no_work_and_filter_controls_keep_identity(chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context()
    page = context.new_page()
    try:
        boot_workspace(page)
        assert measure_hidden(page) == {'jobs': 0, 'monitor': 0}
        assert page.evaluate('''() => {
          const form=document.querySelector('#probe');
          ui.open('jobs');
          document.querySelector('.cc-message-receipts').open=true;
          const button=document.querySelector('[data-cc-progress-filter="waiting"]');
          button.focus();ui.handleClick({target:button});
          const retained=button.isConnected && document.activeElement===button &&
            button.getAttribute('aria-pressed')==='true';
          ui.open('monitor');
          return retained && form===document.querySelector('#probe');
        }''')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_indexed_merge_preserves_identity_and_equal_version_receipt_updates(chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context()
    page = context.new_page()
    try:
        page.add_script_tag(path=str(ROOT / 'web/collaboration-runtime.js'))
        assert page.evaluate('''() => {
          const {merge,index}=CodePierCollaborationRuntime;
          const original=Object.freeze(Array.from({length:1000},(_,i)=>Object.freeze({
            id:'m'+i,version:2,server_sequence:i+1,relay_receipts:[{state:'saved'}]})));
          if(merge(original,[])!==original || merge(original,[{...original[500]}])!==original) return false;
          if(merge(original,[{...original[500],version:1}])!==original)return false;
          const changed=merge(original,[{...original[500],relay_receipts:[{state:'working'}]}]);
          if(changed===original || changed[0]!==original[0] || changed[500]===original[500])return false;
          if(index(changed)!==index(changed) || index(changed).get('m500').relay_receipts[0].state!=='working')return false;
          const next=merge(changed,[{id:'new',version:1,server_sequence:0},
            {id:'new',version:2,server_sequence:1001},{id:'new',version:1,server_sequence:0}]);
          return next.length===1001 && next.at(-1).id==='new' && next.at(-1).version===2 &&
            original[500].relay_receipts[0].state==='saved';
        }''')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_motion_is_interruptible_cleans_up_and_respects_reduced_motion(chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context(reduced_motion='no-preference')
    page = context.new_page()
    try:
        page.set_content('<section><button>Ready</button></section>')
        page.add_script_tag(path=str(ROOT / 'web/collaboration-runtime.js'))
        assert page.evaluate('''() => {
          const node=document.querySelector('section'),ui=CodePierCollaborationRuntime;
          ui.enter(node,{x:12,duration:500});ui.enter(node,{x:-12,duration:500});
          document.querySelector('button').focus();
          return node.getAnimations().length===1 && document.activeElement.tagName==='BUTTON';
        }''')
        page.emulate_media(reduced_motion='reduce')
        page.wait_for_function("document.querySelector('section').getAnimations().length === 0")
        page.evaluate("CodePierCollaborationRuntime.enter(document.querySelector('section'))")
        assert page.locator('section').evaluate('(n)=>n.getAnimations().length') == 0
    finally:
        context.close()
