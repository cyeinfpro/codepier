"""Exercise shipped timeline/relay logic with test-only state access and synthetic data.

The export is injected into the test page only. Production exposes no debug state.
"""
from pathlib import Path

import pytest

from tests.test_collaboration_fluid_ui import boot_relay

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.browser


def boot_timeline(page):
    page.set_content('''<div class="collaboration cc-workspace"><div class="cc-feed"
      style="height:300px;overflow:auto"><div class="cc-message-list"></div></div>
      <button class="cc-new-messages" hidden></button><textarea id="cc-message-input"></textarea></div>''')
    page.evaluate('''() => {
      window.S={session:'s',space_id:'s',projects:[{id:'p',alias:'Project'}],page:'collaboration'};
      window.CP={ui:{esc:value=>String(value??'').replace(/[&<>"']/g,c=>
        ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}};
      window.CodePierCollaborationGoals={create:()=>({load:async()=>{
        window.goalLoads=(window.goalLoads||0)+1;
      }})};
      window.CodePierCollaborationDelegation={create:()=>({messageCard:()=>''})};
      window.CodePierCollaborationDots={create:()=>({isConversationMessage:()=>true})};
      window.CodePierCollaborationWorkspace={create:()=>({welcome:()=>''})};
      window.chatRichMarkdown=(node,text)=>{node.textContent=text;node._chatSource=text;};
    }''')
    page.add_script_tag(path=str(ROOT / 'web/collaboration-runtime.js'))
    page.add_script_tag(path=str(ROOT / 'web/collaboration-relay.js'))
    page.add_script_tag(path=str(ROOT / 'web/collaboration-channels.js'))
    source = (ROOT / 'web/collaboration.js').read_text()
    export = 'return { html, bind, detach, clear, open };'
    assert source.count(export) == 1
    source = source.replace(export, 'return { html, state, chat, relay, patchTimeline, mergeMessages };')
    page.add_script_tag(content=source)
    page.evaluate('''() => {
      window.a=CodePierCollaboration;
      Object.assign(a.state,{session:S.session,space:S.space_id,project:'p',environment:'production',
        snapshot:{room:{id:'room',state:'active'},can_manage:true,jobs:[],join_slots:[],
          capabilities:{native_dot_relay:true}}});
      window.c=a.chat();
      window.metrics={renders:0};
      const render=a.relay.renderMessage;
      a.relay.renderMessage=(m)=>{metrics.renders++;return render(m);};
      a.mergeMessages(Array.from({length:1000},(_,i)=>({id:'m'+i,version:1,created:1000,
        server_sequence:i+1,author_kind:'owner',body_text:'Message '+i,
        ...(i===2?{reply_to_id:'m0'}:{})})));
      a.patchTimeline(true);
      window.originalNodes=[...document.querySelectorAll('[data-message-id]')];
      metrics.renders=0;
    }''')


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_unchanged_history_and_heartbeats_do_not_render_but_evidence_still_ages(chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context(reduced_motion='reduce')
    page = context.new_page()
    try:
        boot_timeline(page)
        result = page.evaluate('''() => {
          a.mergeMessages(c.messages.map(m=>({...m})));
          a.patchTimeline();
          a.state.snapshot.join_slots=[{id:'d',label:'Dot',task_dot:true,duplex:true,heartbeat:10}];
          a.patchTimeline();metrics.renders=0;
          a.state.snapshot.join_slots[0].heartbeat=20;
          a.patchTimeline();
          return metrics.renders;
        }''')
        assert result == 0
        result = page.evaluate('''() => {
          c.relayServerNow=1000;c.relayReceivedAt=performance.now();
          c.relayTaskStates={m0:[{work_item_id:'work',execution_state:'running',
            delivery_state:'pending',wait_reason:'original_operation_pending',last_operation_verified_at:999}]};
          metrics.renders=0;a.patchTimeline();
          return {renders:metrics.renders,text:document.querySelector('.cc-task-liveness').textContent};
        }''')
        assert result['renders'] == 1
        assert '近期收到执行节点回执' in result['text']
        result = page.evaluate('''() => {
          c.relayServerNow=1100;metrics.renders=0;a.patchTimeline();
          return {renders:metrics.renders,text:document.querySelector('.cc-task-liveness').textContent,
            preserved:originalNodes.every(n=>n.isConnected)};
        }''')
        assert result['renders'] == 1
        assert '执行证据已过时' in result['text']
        assert result['preserved']
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_parent_quote_changes_and_focus_deferrals_retry_without_new_data(chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context(reduced_motion='reduce')
    page = context.new_page()
    try:
        boot_timeline(page)
        result = page.evaluate('''() => {
          a.mergeMessages([{...c.messages[0],version:2,body_text:'Corrected source'}]);
          a.patchTimeline();
          return {renders:metrics.renders,quote:document.querySelector('[data-message-id="m2"] .cc-relay-quote').textContent,
            preserved:originalNodes.every(n=>n.isConnected)};
        }''')
        assert result['renders'] == 2
        assert 'Corrected source' in result['quote']
        assert result['preserved']
        result = page.evaluate('''() => {
          c.relayServerNow=1000;c.relayReceivedAt=performance.now();
          c.relayTaskStates={m0:[{work_item_id:'work',execution_state:'running',
            delivery_state:'pending',wait_reason:'original_operation_pending',last_operation_verified_at:999}]};
          a.patchTimeline();
          const summary=document.querySelector('.cc-task-liveness summary');summary.focus();
          c.relayTaskStates.m0[0].wait_reason='result_committed';
          a.patchTimeline();
          const deferred=document.querySelector('.cc-message-list')._ccDeferred;
          document.querySelector('textarea').focus();a.patchTimeline();
          return {deferred,text:document.querySelector('.cc-task-liveness summary').textContent,
            done:!document.querySelector('.cc-message-list')._ccDeferred};
        }''')
        assert result['deferred']
        assert result['done']
        assert '结果已保存' in result['text']
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_post_ack_is_immutable_and_visible_without_waiting_for_poll(chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context()
    page = context.new_page()
    try:
        boot_relay(page)
        page.evaluate('''() => {
          window.previous=Object.freeze(c.messages.slice());c.messages=previous;
          state.snapshot.room.id='room';
          window.api=async()=>({message:{id:'ack',version:1,server_sequence:2,body_text:'new message'}});
          document.querySelector('textarea').value='new message';
          metrics.patches=0;
          relay.send(document.querySelector('form'));
        }''')
        page.wait_for_function('!c.relayFlight && c.relayOutbox.length === 0')
        assert page.evaluate('''() => c.messages!==previous && previous.length===1 &&
          c.messages.length===2 && CodePierCollaborationRuntime.index(c.messages).has('ack') &&
          metrics.patches===1''')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_late_initial_grants_do_not_overwrite_new_scope(chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context(reduced_motion='reduce')
    page = context.new_page()
    try:
        boot_timeline(page)
        page.evaluate('''() => {
          window.api=async(url)=> {
            if(url.endsWith('/status'))return {features:{enabled:true}};
            if(url==='/api/grants')return new Promise(resolve=>window.finishGrant=resolve);
            return {room:null,can_manage:true,capabilities:{}};
          };
          window.loading=a.html();
        }''')
        page.wait_for_function('typeof finishGrant === "function"')
        result = page.evaluate('''async () => {
          a.state.generation++;
          a.state.project='new-project';
          a.state.snapshot={marker:'new-scope'};
          a.state.grants=[{id:'current'}];
          finishGrant({grants:[{id:'obsolete'}]});
          const rendered=await loading;
          return {rendered,grants:a.state.grants,marker:a.state.snapshot.marker,goalLoads:window.goalLoads||0};
        }''')
        assert result == {'rendered': '', 'grants': [{'id': 'current'}],
                          'marker': 'new-scope', 'goalLoads': 0}
    finally:
        context.close()
