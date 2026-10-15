"""Evidence-only UI/recovery contracts in Chromium and WebKit.

All network responses and accounts are deterministic fixtures. No real Dot,
external messages, production authorization or provider delivery is exercised.
"""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.test_dot_task_liveness_browser import SETUP

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.fixture(params=['chromium', 'webkit'])
def ecosystem_page(request, chat_browser_pool):
    context = chat_browser_pool(request.param).new_context(
        viewport={'width': 390 if request.param == 'webkit' else 1440, 'height': 920},
        reduced_motion='reduce')
    page = context.new_page()
    page.route('https://ecosystem.invalid/**', lambda route: route.fulfill(
        content_type='text/html', body='<html lang="zh-CN"><meta charset="utf-8"><body>'
        '<main class="collaboration"><div id="controls"></div><div id="timeline" class="cc-message-list"></div>'
        '<form id="cc-command"><textarea name="request" id="cc-message-input"></textarea>'
        '<button type="submit">发送</button></form><div id="cc-relay-outbox"></div>'
        '<div id="cc-relay-network" hidden></div></main></body></html>'))
    page.goto('https://ecosystem.invalid/')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    for name in ['styles.css', 'collaboration.css', 'collaboration-relay.css']:
        page.add_style_tag(content=(ROOT/'web'/name).read_text())
    page.add_style_tag(content='body{margin:0;padding:12px;display:block;overflow:auto}.collaboration{display:block;min-width:0}textarea{max-width:100%}')
    for name in ['collaboration-runtime.js', 'collaboration-relay.js', 'collaboration-workspace.js']:
        page.add_script_tag(content=(ROOT/'web'/name).read_text())
    setup = SETUP.replace("room: {state: 'active'}", "room: {id: 'room-fixture', state: 'active'}")
    setup = setup.replace("window.calls = [];", "window.calls = []; window.patches = [];")
    setup = setup.replace("patchTimeline: ()=>{", "patchTimeline: (force,dirty)=>{ patches.push(dirty ? [...dirty] : null);")
    setup = setup.replace("relay.renderMessage(source)", "relay.renderMessage(c.messages[0])")
    setup = setup.replace("holder.firstElementChild,source", "holder.firstElementChild,c.messages[0]")
    setup = setup.replace("if(existing) relay.patchMessage(existing,holder.firstElementChild,c.messages[0]);",
                          "if(existing) document.querySelector(\'#timeline\')._ccDeferred = relay.patchMessage(existing,holder.firstElementChild,c.messages[0]) === \'defer\';")
    setup = setup.replace("recoverAccess: async", "onWorkspaceChange: ()=>{window.workspaceRefreshes=(window.workspaceRefreshes||0)+1;window.ecosystemWorkspace?.refresh();}, recoverAccess: async")
    setup = setup.replace("mergeMessages: ()=>{}", "mergeMessages: items=>{c.messages=CodePierCollaborationRuntime.merge(c.messages,items);}")
    setup = setup.replace("requestId: ()=>{throw new Error('A read must not allocate a new mutation ID')}",
                          "requestId: ()=>'synthetic-new-key'")
    page.evaluate(setup)
    try:
        yield page
        assert not errors, errors
    finally:
        context.close()


def test_one_truthful_state_single_navigation_and_redacted_preview(ecosystem_page):
    page = ecosystem_page
    page.evaluate("""() => {
      const m=c.messages[0];
      m.project_id='project';m.environment_id='production';
      m.body_text='PRIVATE-BODY /Users/private/key TOKEN-123';
      m.relay_receipts=[
        {dot_id:'private-dot-a', state:'completed', experience:{read_only:true,
          primary_state:'completed', observed_at:900}},
        {dot_id:'private-dot-b', state:'replied', experience:{read_only:true,
          primary_state:'result_saved_channel_uncertain',reason_code:'channel_delivery_uncertain',
          callback_url:'https://secret.invalid/TOKEN', secret:'PRIVATE-SECRET',
          next_action:{kind:'inspect_original_receipt',target_ref:'original-delivery',mutates:false}}}
      ];
      document.querySelector('#timeline').innerHTML=relay.renderMessage(m);
      window.rows=CodePierCollaborationWorkspace.receiptRows(c.messages,'project','production');
    }""")
    status = page.locator('.cc-experience-status')
    expect(status).to_have_count(1)
    expect(status).to_contain_text('结果已保存，部分通道待核对')
    expect(status.locator('button')).to_have_count(1)
    assert page.evaluate("rows[0].state") == 'result_saved_channel_uncertain'
    page.locator('.cc-receipt-detail > summary').click()
    page.locator('.cc-diagnostic-preview > summary').click()
    preview = page.locator('.cc-diagnostic-preview pre').inner_text()
    for secret in ['PRIVATE-BODY', '/Users/', 'TOKEN', 'PRIVATE-SECRET', 'private-dot', 'secret.invalid']:
        assert secret not in preview
    assert 'message-' in preview
    assert '外部客户端显示或已读' in preview
    page.evaluate("""() => {
      c.messages[0].relay_receipts[1].experience.next_action={
        kind:'run_arbitrary_script',mutates:false,target_ref:'https://secret.invalid'};
      document.querySelector('#timeline').innerHTML=relay.renderMessage(c.messages[0]);
    }""")
    expect(page.locator('.cc-experience-status button')).to_have_count(0)


def test_unchanged_sync_does_no_timeline_work_and_keeps_composition(ecosystem_page):
    page = ecosystem_page
    area = page.locator('#cc-message-input')
    area.fill('中文组合输入与保留选区')
    area.evaluate("(n)=>{n.setSelectionRange(2,5);n.dataset.identity='original'}")
    page.evaluate("state.composing=true")
    page.evaluate("relay.sync({readOnly:true})")
    page.wait_for_function('!c.relaySync')
    first = page.evaluate('patches.length')
    assert first == 1
    page.evaluate("relay.sync({readOnly:true})")
    page.wait_for_function('!c.relaySync')
    assert page.evaluate('patches.length') == first
    assert page.evaluate('c.relayRenderStats.unchanged') == 1
    expect(area).to_have_value('中文组合输入与保留选区')
    assert area.evaluate('(n)=>[n.selectionStart,n.selectionEnd,n.dataset.identity]') == [2,5,'original']
    assert page.evaluate('calls.every(x=>x.method==="GET")')
    assert page.evaluate('c.relayOutbox[0].attempts') == 0


def test_selected_stage_body_defers_patch_but_updates_state(ecosystem_page):
    page = ecosystem_page
    result = page.evaluate("""() => {
      const m=c.messages[0];
      m.relay_receipts=[{dot_id:'dot',state:'working',experience:{read_only:true,
        primary_state:'processing',projection_revision:'first'}}];
      document.querySelector('#timeline').innerHTML=relay.renderMessage(m);
      const node=document.querySelector('[data-message-id]'),body=node.querySelector('[data-rich-message]');
      const range=document.createRange();range.selectNodeContents(body);
      getSelection().removeAllRanges();getSelection().addRange(range);
      const selected=getSelection().toString();
      m.version=2;m.body_text='新的阶段正文';
      m.relay_receipts[0].experience={read_only:true,primary_state:'waiting_authorization',
        reason_code:'waiting_authorization',projection_revision:'second',
        next_action:{kind:'open_host_confirmation',mutates:false}};
      const holder=document.createElement('div');holder.innerHTML=relay.renderMessage(m);
      const patch=relay.patchMessage(node,holder.firstElementChild,m);
      const waiting=node.querySelector('.cc-experience-status').dataset.waitingId;
      const unchanged=getSelection().toString()===selected;
      m.relay_receipts[0].experience.projection_revision='poll-does-not-change-wait';
      holder.innerHTML=relay.renderMessage(m);
      return {patch,unchanged,waiting,sameWait:waiting===holder.querySelector('.cc-experience-status').dataset.waitingId};
    }""")
    assert result['patch'] == 'defer'
    assert result['unchanged'] and result['sameWait']
    status = page.locator('.cc-experience-status')
    expect(status).to_contain_text('授权待核对')
    expect(status).to_have_attribute('data-primary-state', 'waiting_authorization')
    expect(status.get_by_role('button', name='查看授权说明')).to_have_count(1)
    assert '回 Dot 确认' not in status.inner_text()
    assert page.locator('.cc-experience-status a').count() == 0
    assert page.evaluate("getComputedStyle(document.querySelector('.cc-relay-pending') || document.body).animationName") == 'none'


def test_unknown_send_receipt_never_reposts_and_absence_reuses_original_key(ecosystem_page):
    page = ecosystem_page
    page.evaluate("""() => {
      c.relayOutbox=[];c.draft='';c.mentions=['fixture-dot'];
      window.requestLog=[];window.receiptState='unknown';
      window.api=async(url,options={})=>{
        requestLog.push({url,method:options.method||'GET',body:options.body && JSON.parse(options.body)});
        if(options.method==='POST')throw Object.assign(new Error('response lost'),{status:503});
        if(url.startsWith('/api/collaboration/message-receipt?')){
          const id=new URL(url,location.href).searchParams.get('client_message_id');
          if(receiptState==='unknown')throw Object.assign(new Error('receipt unavailable'),{status:503});
          return {state:receiptState,authoritative:true,client_message_id:id,conversation_id:'room-fixture'};
        }
        throw new Error('Unexpected route '+url);
      };
      c.relayOutbox=[{id:'original-key',chat:c,epoch:0,session:S.session,space:S.space_id,
        payload:{project:'project',environment_id:'production',room_id:'room-fixture',body_text:'原点击发送内容'},
        dotId:'fixture-dot',phase:'error',attempts:1,due:0,created:Date.now(),needsReceipt:true}];
      relay.action('relay-retry',{dataset:{id:'original-key'}});
    }""")
    page.wait_for_function('!c.relayFlight')
    assert page.evaluate("requestLog.filter(r=>r.method==='POST').length") == 0
    assert page.evaluate("c.relayOutbox[0].phase") == 'error'
    # An ordinary 404 is intentionally not authoritative proof of absence.
    page.evaluate("""() => {
      window.api=async(url,options={})=>{requestLog.push({url,method:options.method||'GET'});
        throw Object.assign(new Error('not found or unauthorized'),{status:404,code:'NOT_FOUND'});};
      relay.action('relay-retry',{dataset:{id:'original-key'}});
    }""")
    page.wait_for_function('!c.relayFlight')
    assert page.evaluate("requestLog.filter(r=>r.method==='POST').length") == 0
    page.evaluate("""() => {
      api=async(url,options={})=>{
        requestLog.push({url,method:options.method||'GET',body:options.body&&JSON.parse(options.body)});
        if(options.method==='POST') return {message:{id:'server-original',body_text:'原点击发送内容',version:1}};
        return {state:'not_committed',authoritative:true,client_message_id:'original-key',conversation_id:'room-fixture'};
      };
      relay.action('relay-retry',{dataset:{id:'original-key'}});
    }""")
    page.wait_for_function('!c.relayFlight')
    assert page.evaluate("requestLog.filter(r=>r.method==='POST').length") == 1
    assert page.evaluate("requestLog.find(r=>r.method==='POST').body.client_message_id") == 'original-key'
    assert page.evaluate("!Object.hasOwn(requestLog.find(r=>r.method==='POST').body,'pending_scope_revision')")
    assert page.evaluate('c.relayOutbox.length') == 0


def test_pending_store_opt_in_identity_capacity_ttl_and_cleanup(ecosystem_page):
    page = ecosystem_page
    result = page.evaluate("""() => {
      localStorage.clear();
      const make=owner=>CodePierCollaborationRelay.pendingStore(localStorage,owner);
      const store=make('account-a'),now=Date.now();
      const entry={id:'clicked-intent',scope:'scope-generation-1',created:now,dotId:'fixture-dot',
        authorization_revision:'a'.repeat(64),
        payload:{project:'project',environment_id:'production',room_id:'room-fixture',
          mentions:[{slot_id:'fixture-dot'}],body_text:'already-clicked-send'}};
      const defaultOff=!store.enabled() && store.save(entry)===false;
      const before=localStorage.getItem('codepier-relay-pending-v1');
      store.enable();store.save(entry);
      const restored=store.recover('scope-generation-1').length===1;
      const isolated=store.recover('scope-generation-2').length===0;
      store.save({...entry,id:'expired',created:now-86400001});
      const ttl=store.recover('scope-generation-1').length===1;
      let bounded=false;
      try {store.save({...entry,id:'too-large',payload:{...entry.payload,body_text:'中'.repeat(100000)}});} catch {bounded=true;}
      const next=make('account-b');
      const account=!next.enabled() && next.recover('scope-generation-1').length===0;
      store.enable();store.save(entry);store.clear();
      return {defaultOff,before:!before||!before.includes('already-clicked-send'),restored,isolated,ttl,bounded,account,
        clear:localStorage.getItem('codepier-relay-pending-v1')===null};
    }""")
    assert all(result.values()), result


def test_final_stage_stays_final_and_scope_change_blocks_persisted_send(ecosystem_page):
    page = ecosystem_page
    result = page.evaluate("""() => {
      const m=c.messages[0];m.body={dot_reply_complete:false};
      document.querySelector('#timeline').innerHTML=relay.renderMessage(m);
      const node=document.querySelector('[data-message-id]');
      m.version=2;m.body.dot_reply_complete=true;
      const holder=document.createElement('div');holder.innerHTML=relay.renderMessage(m);
      relay.patchMessage(node,holder.firstElementChild,m);
      const finalRemoved=node.querySelectorAll('.cc-stage-card').length===0;
      const old={...m,version:1,body:{dot_reply_complete:false}};
      holder.innerHTML=relay.renderMessage(old);relay.patchMessage(node,holder.firstElementChild,old);
      return {finalRemoved,oldDidNotReopen:node.querySelectorAll('.cc-stage-card').length===0,
        version:node.dataset.version};
    }""")
    assert result == {'finalRemoved': True, 'oldDidNotReopen': True, 'version': '2'}
    page.evaluate("""() => {
      window.S.session={user_id:'account-a'};state.session=S.session;
      c.relayOutbox=[{id:'persisted-key',chat:c,epoch:0,session:S.session,space:S.space_id,
        persistenceScope:'old-scope-generation',payload:{project:'project',
          environment_id:'production',body_text:'old-scope-text'},
        dotId:'fixture-dot',phase:'error',attempts:0,due:0,created:Date.now(),needsReceipt:true}];
      calls=[];api=async()=>{calls.push('unexpected');throw new Error('Must not read or write old scope');};
      relay.action('relay-retry',{dataset:{id:'persisted-key'}});
    }""")
    page.wait_for_function('!c.relayFlight')
    assert page.evaluate('calls.length') == 0
    assert page.evaluate("c.relayOutbox[0].error.includes('授权版本已变化')")


def test_restore_checks_original_receipt_then_purge_removes_private_storage(ecosystem_page):
    page = ecosystem_page
    page.evaluate("""() => {
      c.relayOutbox=[];c.draft='not-clicked-draft';
      window.S.session={user_id:'account-a'};state.session=S.session;
      state.snapshot.pending_scope_revision='a'.repeat(64);
      relay.toolbar(); // Establish this authenticated owner before simulating a prior page's storage.
      const scope=JSON.stringify([S.session.user_id,S.space_id,
        {project:'project',environment_id:'production'},c.visibility,state.snapshot.pending_scope_revision,[['fixture-dot',0,null]]]);
      const owner=JSON.stringify([S.session.user_id,S.space_id]);
      const store=CodePierCollaborationRelay.pendingStore(localStorage,owner);
      store.enable();store.save({id:'persisted-key',scope,created:Date.now(),authorization_revision:'a'.repeat(64),
        dotId:'fixture-dot',payload:{project:'project',environment_id:'production',room_id:'room-fixture',
        mentions:[{slot_id:'fixture-dot'}],body_text:'persisted clicked text'}});
      calls=[];api=async(url,options={})=>{
        calls.push({url,method:options.method||'GET'});
        if(options.method)throw new Error('Unknown receipt must not send');
        throw Object.assign(new Error('unknown'),{status:503});
      };
      relay.bind(document.querySelector('.collaboration'));
    }""")
    page.wait_for_function('!c.relayFlight')
    assert page.evaluate('c.relayOutbox.length') == 1
    assert page.evaluate("calls.length===1 && calls[0].url.startsWith('/api/collaboration/message-receipt?')")
    assert page.evaluate("calls.every(r=>r.method==='GET')")
    assert page.evaluate("!localStorage.getItem('codepier-relay-pending-v1').includes('not-clicked-draft')")
    page.evaluate('relay.purge(c);relay.detach()')
    assert page.evaluate("localStorage.getItem('codepier-relay-pending-v1')===null")


def test_pending_storage_scrubs_payload_and_rejects_utf8_oversize(ecosystem_page):
    result = ecosystem_page.evaluate("""() => {
      localStorage.clear();
      const store=CodePierCollaborationRelay.pendingStore(localStorage,'account-a');
      const entry={id:'id',scope:'scope',created:Date.now(),dotId:'dot',
        authorization_revision:'a'.repeat(64),secret:'OUTSIDE-ALLOWLIST',
        payload:{project:'project',environment_id:'production',room_id:'room',
          mentions:[{slot_id:'dot'}],body_text:'clicked text',capabilities:['execute'],
          idempotency_key:'INJECTED-KEY'}};
      store.enable();store.save(entry);
      const recovered=store.recover('scope')[0];
      const scrubbed=!localStorage.getItem('codepier-relay-pending-v1').includes('INJECTED') &&
        !localStorage.getItem('codepier-relay-pending-v1').includes('OUTSIDE-ALLOWLIST') &&
        !Object.hasOwn(recovered.payload,'capabilities');
      const normalized=recovered.payload.conversation_id==='room';
      for(let i=1;i<20;i++)store.save({...entry,id:'id'+i});
      let capacity=false;try{store.save({...entry,id:'over-capacity'});}catch{capacity=true;}
      localStorage.setItem('codepier-relay-pending-v1',JSON.stringify({version:1,owner:'account-a',
        enabled:true,records:[{...entry,payload:{...entry.payload,body_text:'中'.repeat(100000)}}]}));
      const oversize=store.recover('scope').length===0 &&
        localStorage.getItem('codepier-relay-pending-v1')===null;
      return {scrubbed,normalized,capacity,oversize};
    }""")
    assert all(result.values()), result


def test_persistence_requires_server_generation_and_blocks_newer_receipt(ecosystem_page):
    page = ecosystem_page
    page.evaluate("""() => {
      S.session={user_id:'account-a'};state.session=S.session;c.relayOutbox=[];
      const control={textContent:'',dataset:{}};
      relay.action('relay-persistence-toggle',control);
      window.missingRevisionBlocked=!localStorage.getItem('codepier-relay-pending-v1');
      state.snapshot.pending_scope_revision='a'.repeat(64);
      relay.action('relay-persistence-toggle',control);
      const scope=JSON.stringify([S.session.user_id,S.space_id,
        {project:'project',environment_id:'production'},c.visibility,
        state.snapshot.pending_scope_revision,[['fixture-dot',0,null]]]);
      c.relayOutbox=[{id:'original-key',chat:c,epoch:0,session:S.session,space:S.space_id,
        persistenceScope:scope,authorizationRevision:'a'.repeat(64),
        payload:{project:'project',environment_id:'production',room_id:'room-fixture',
          body_text:'clicked intent',mentions:[{slot_id:'fixture-dot'}]},
        dotId:'fixture-dot',phase:'error',attempts:0,due:0,created:Date.now(),needsReceipt:true}];
      calls=[];api=async(url,options={})=>{
        calls.push({url,method:options.method||'GET'});
        return {state:'not_committed',authoritative:true,client_message_id:'original-key',
          conversation_id:'room-fixture',pending_scope_revision:'b'.repeat(64)};
      };
      relay.action('relay-retry',{dataset:{id:'original-key'}});
    }""")
    page.wait_for_function('!c.relayFlight')
    assert page.evaluate('missingRevisionBlocked')
    assert page.evaluate("calls.length===1 && calls[0].method==='GET'")
    assert page.evaluate("c.relayOutbox[0].error.includes('授权世代已变化')")
    assert page.evaluate("localStorage.getItem('codepier-relay-pending-v1')===null")


def test_p2_write_carries_original_generation_and_409_stops_without_replay(ecosystem_page):
    page = ecosystem_page
    page.evaluate("""() => {
      S.session={user_id:'account-a'};state.session=S.session;
      state.snapshot.pending_scope_revision='a'.repeat(64);
      c.relayOutbox=[];c.mentions=['fixture-dot'];c.draft='original clicked text';
      relay.toolbar();
      relay.action('relay-persistence-toggle',{textContent:'',dataset:{}});
      window.requestLog=[];
      api=async(url,options={})=>{
        requestLog.push({url,method:options.method||'GET',body:options.body&&JSON.parse(options.body)});
        if(options.method==='POST')
          throw Object.assign(new Error('Scope changed between read and write'),
            {status:409,code:'PENDING_SCOPE_CHANGED'});
        return {state:'not_committed',authoritative:true,client_message_id:'synthetic-new-key',
          conversation_id:'room-fixture',pending_scope_revision:'a'.repeat(64)};
      };
      const form=document.querySelector('#cc-command');
      form.elements.request.value='original clicked text';relay.send(form);
    }""")
    page.wait_for_function('!c.relayFlight')
    assert page.evaluate("requestLog.length===1 && requestLog[0].method==='POST'")
    assert page.evaluate("requestLog[0].body.pending_scope_revision") == 'a' * 64
    assert page.evaluate("c.relayOutbox[0].payload.pending_scope_revision") == 'a' * 64
    assert page.evaluate("c.relayOutbox[0].phase") == 'error'
    assert page.evaluate("c.relayOutbox[0].payload.body_text") == 'original clicked text'
    assert page.evaluate("localStorage.getItem('codepier-relay-pending-v1')===null")
    # A deliberate retry first checks the original receipt. The same original
    # generation is fenced again in POST; a 409 never triggers another send.
    page.evaluate("relay.action('relay-retry',{dataset:{id:'synthetic-new-key'}})")
    page.wait_for_function('!c.relayFlight')
    assert page.evaluate("requestLog.map(r=>r.method)") == ['POST', 'GET', 'POST']
    assert page.evaluate("requestLog[2].body.pending_scope_revision") == 'a' * 64
    assert page.evaluate("requestLog[2].body.client_message_id") == 'synthetic-new-key'
    assert page.evaluate("c.relayOutbox[0].phase") == 'error'


@pytest.mark.parametrize('composing', [False, True])
def test_focused_receipt_defers_then_blur_flushes_without_new_sync(ecosystem_page, composing):
    page = ecosystem_page
    page.evaluate("""() => {
      c.relayOutbox=[];relay.bind(document.querySelector('.collaboration'));
      document.querySelector('.cc-receipt-detail > summary').focus();
      const next={...c.messages[0],relay_receipts:[{dot_id:'fixture-dot',
        state:'working',experience:{read_only:true,primary_state:'waiting_input'}}]};
      api=async()=>({server_now:1000,visibility_token:'visibility',updates:[next],items:[],
        task_states:[],room_state:'active',can_manage:true,has_more:false});
    }""")
    page.evaluate("relay.sync({readOnly:true})")
    assert page.evaluate("document.querySelector('#timeline')._ccDeferred === true")
    expect(page.locator('.cc-experience-status')).not_to_contain_text('等待你补充')
    count = page.evaluate('patches.length')
    # A composing session must not be disturbed, even when real deferred work exists.
    page.evaluate("""(composing) => {
      state.composing=composing;
      document.querySelector('#cc-message-input').focus();
      document.dispatchEvent(new Event('selectionchange'));
    }""", composing)
    if composing:
        page.wait_for_timeout(60)
        assert page.evaluate('patches.length') == count
        page.evaluate("""() => {
          state.composing=false;
          document.querySelector('#cc-message-input').dispatchEvent(new Event('compositionend',{bubbles:true}));
        }""")
    expect(page.locator('.cc-experience-status')).to_contain_text('等待你补充')
    assert page.evaluate("document.querySelector('#timeline')._ccDeferred === false")
    assert page.evaluate('patches.length') == count + 1
    flushed = page.evaluate('patches.length')
    page.evaluate("""() => {
      for(let i=0;i<10;i++)document.dispatchEvent(new Event('selectionchange'));
      document.querySelector('#cc-message-input').dispatchEvent(new FocusEvent('focusout',{bubbles:true}));
    }""")
    page.wait_for_timeout(60)
    assert page.evaluate('patches.length') == flushed
    page.evaluate('relay.detach()')


@pytest.mark.parametrize('held', ['focus', 'selection'])
def test_workspace_deferred_refresh_is_explicit_and_flushes_on_release(ecosystem_page, held):
    page = ecosystem_page
    page.evaluate("""() => {
      const root=document.querySelector('.collaboration');
      root.classList.add('cc-workspace');root.dataset.project='project';root.dataset.environment='production';
      c.relayOutbox=[];state.view='jobs';
      c.messages=CodePierCollaborationRuntime.merge(c.messages,[{...c.messages[0],
        project_id:'project',environment_id:'production'}]);
      const E=s=>String(s??'');
      window.ecosystemWorkspace=CodePierCollaborationWorkspace.create({state,E,
        button:(action,label,m={})=>'<button type="button" data-cc-action="'+action+
          '" data-id="'+(m.id||'')+'">'+label+'</button>',chat:()=>c,
        mentionSlots:()=>[],projectLabel:id=>id,messageText:m=>m.body_text,
        renderJobs:()=>'',renderAgents:()=>'',renderMonitor:()=>'',bindDetails:()=>{}});
      root.insertAdjacentHTML('beforeend',ecosystemWorkspace.panel());
      document.querySelector('.cc-message-receipts').open=true;
      window.uninitializedWasNotDeferred=root._ccWorkspaceDeferred!==true;
      ecosystemWorkspace.refresh();relay.bind(root);
    }""")
    assert page.evaluate('uninitializedWasNotDeferred')
    assert page.evaluate("document.querySelector('.cc-workspace')._ccWorkspaceDeferred === false")
    page.evaluate("""(held) => {
      const card=document.querySelector('[data-progress-message]');
      if(held==='focus')card.querySelector('button').focus();
      else{
        const range=document.createRange();range.selectNodeContents(card.querySelector('p'));
        getSelection().removeAllRanges();getSelection().addRange(range);
      }
      c.messages=CodePierCollaborationRuntime.merge(c.messages,[{...c.messages[0],
        relay_receipts:[{dot_id:'fixture-dot',state:'completed',
          experience:{read_only:true,primary_state:'completed'}}]}]);
      ecosystemWorkspace.refresh();
    }""", held)
    assert page.evaluate("document.querySelector('.cc-workspace')._ccWorkspaceDeferred === true")
    expect(page.locator('.cc-progress-state')).not_to_have_text('已完成')
    count = page.evaluate('patches.length')
    page.evaluate("""() => {
      getSelection().removeAllRanges();
      document.querySelector('#cc-message-input').focus();
    }""")
    expect(page.locator('.cc-progress-state')).to_have_text('已完成')
    assert page.evaluate("document.querySelector('.cc-workspace')._ccWorkspaceDeferred === false")
    # Releasing only workspace work does not repaint the conversation timeline.
    assert page.evaluate('patches.length') == count
    refreshes = page.evaluate('workspaceRefreshes')
    page.evaluate("""() => {
      for(let i=0;i<10;i++)document.dispatchEvent(new Event('selectionchange'));
      document.querySelector('#cc-message-input').dispatchEvent(new FocusEvent('focusout',{bubbles:true}));
    }""")
    page.wait_for_timeout(60)
    assert page.evaluate('workspaceRefreshes') == refreshes
    page.evaluate('relay.detach()')


def test_connection_only_sync_rechecks_timeline_context_but_unchanged_skips(ecosystem_page):
    page = ecosystem_page
    page.evaluate('relay.sync({readOnly:true})')
    first = page.evaluate('patches.length')
    page.evaluate("""() => {
      const original=api;
      api=async(...args)=>({...await original(...args),can_manage:false});
    }""")
    page.evaluate('relay.sync({readOnly:true})')
    assert page.evaluate('patches.length') == first + 1
    assert page.evaluate('patches.at(-1)') is None
    page.evaluate('relay.sync({readOnly:true})')
    assert page.evaluate('patches.length') == first + 1


def test_prompt_cache_tracks_input_mutation_then_programmatic_draft_clear(ecosystem_page):
    page = ecosystem_page
    page.evaluate("""() => {
      const root=document.querySelector('.collaboration');
      root.classList.add('cc-workspace');root.dataset.project='project';root.dataset.environment='production';
      c.messages=[];c.draft='';c.relayOutbox=[];state.view='discussion';
      window.promptSlots=[];
      window.promptWorkspace=CodePierCollaborationWorkspace.create({state,E:s=>String(s??''),
        button:()=>'<button type="button">connect</button>',chat:()=>c,
        mentionSlots:()=>promptSlots,projectLabel:id=>id,messageText:m=>m.body_text,
        renderJobs:()=>'',renderAgents:()=>'',renderMonitor:()=>'',bindDetails:()=>{}});
      document.querySelector('#timeline').innerHTML=promptWorkspace.welcome();
      promptSlots=[{id:'dot',task_dot:true,state:'registered'}];
      promptWorkspace.refresh();
      window.cachedWelcome=typeof document.querySelector('.cc-workspace-welcome')._workspaceMarkup==='string';
      const area=document.querySelector('#cc-message-input');
      area.value='@dot';c.draft=area.value;promptWorkspace.input({target:area});
    }""")
    assert page.evaluate('cachedWelcome')
    expect(page.locator('[data-cc-prompt]').first).to_be_disabled()
    page.evaluate("""() => {
      const area=document.querySelector('#cc-message-input');
      area.value='';c.draft='';promptWorkspace.refresh();
    }""")
    expect(page.locator('[data-cc-prompt]').first).to_be_enabled()
    page.evaluate("""() => {
      state.snapshot.can_manage=false;promptWorkspace.refresh();
    }""")
    expect(page.locator('[data-cc-prompt]')).to_have_count(0)
    page.evaluate("""() => {
      state.snapshot.can_manage=true;promptWorkspace.refresh();
    }""")
    expect(page.locator('[data-cc-prompt]').first).to_be_enabled()


def test_archived_outbox_cleanup_is_scoped_and_late_receipt_cannot_resend(ecosystem_page):
    page = ecosystem_page
    page.evaluate("""() => {
      S.session={user_id:'fixture-owner'}; state.session=S.session;
      state.conversation='room-a';state.snapshot.conversation={id:'room-a'};
      state.snapshot.pending_scope_revision='a'.repeat(64);
      c.relayOutbox=[];c.mentions=['fixture-dot'];c.relayEpoch=0;
      state.chats=new Map([[JSON.stringify([S.space_id,'room-a']),c]]);
      relay.toolbar();
      window.roomA=c;window.roomB={...c,messages:[],relayOutbox:[],relayEpoch:0};
      state.chats.set(JSON.stringify([S.space_id,'room-b']),roomB);
      const make=(id,room,conversation)=>({id,chat:room,epoch:0,session:S.session,space:S.space_id,
        payload:{project:'project',environment_id:'production',room_id:'room-fixture',
          conversation_id:conversation,body_text:id,mentions:[{slot_id:'fixture-dot'}]},
        dotId:'fixture-dot',phase:'error',attempts:0,due:0,needsReceipt:true});
      window.entryA=make('pending-a',roomA,'room-a');window.entryB=make('pending-b',roomB,'room-b');
      roomA.relayOutbox=[entryA];roomB.relayOutbox=[entryB];
      window.pendingStore=CodePierCollaborationRelay.pendingStore(localStorage,JSON.stringify([S.session.user_id,S.space_id]));
      pendingStore.enable();
      for(const entry of [entryA,entryB])pendingStore.save({...entry,scope:entry.payload.conversation_id,
        created:Date.now(),authorization_revision:'a'.repeat(64)});
      calls=[];
      api=(url,options={})=>{
        calls.push({url,method:options.method||'GET'});
        if(url.includes('message-receipt'))return new Promise(resolve=>window.lateReceipt=resolve);
        throw new Error('Archived queue attempted a send');
      };
      relay.action('relay-retry',{dataset:{id:entryA.id}});
    }""")
    page.wait_for_function("typeof lateReceipt==='function'")
    page.evaluate("""() => {
      relay.purgeConversation('room-a'); relay.purgeConversation('room-a');
      lateReceipt({authoritative:true,state:'not_committed',client_message_id:'pending-a',
        conversation_id:'room-a'});
    }""")
    page.wait_for_function('!roomA.relayFlight')
    assert page.evaluate('roomA.relayOutbox.length') == 0
    assert page.evaluate('roomA.messages[0].id') == 'original-message'
    assert page.evaluate('roomB.relayOutbox[0].id') == 'pending-b'
    assert page.evaluate('roomB.relayEpoch') == 0
    assert page.evaluate("pendingStore.recover('room-a').length") == 0
    assert page.evaluate("pendingStore.recover('room-b').length") == 1
    assert page.evaluate('pendingStore.enabled()')
    assert page.evaluate('calls.every(call=>call.method==="GET")')
    page.evaluate("""() => {
      state.snapshot.conversation={id:'room-a',restored_from_archive:true};
      state.snapshot.room.state='active'; // Same tuple and visibility; do not rely on paused.
      roomA.relayOutbox=[{...entryA,epoch:roomA.relayEpoch,phase:'queued'}];
      relay.bind(document.querySelector('.collaboration'));
      calls=[];window.dispatchEvent(new Event('online'));
      relay.action('relay-retry',{dataset:{id:'pending-a'}});
      document.querySelector('#cc-message-input').value='must not send from history';
      relay.send(document.querySelector('#cc-command'));
    }""")
    assert page.evaluate('roomA.relayOutbox.length') == 0
    assert page.evaluate('relay.canSend()') is False
    assert page.evaluate('calls.every(call=>call.method==="GET")')
    expect(page.locator('#cc-relay-outbox [data-pending-id]')).to_have_count(0)
    page.evaluate("""() => {
      relay.detach();window.c=roomB;state.conversation='room-b';
      state.snapshot.conversation={id:'room-b'};state.snapshot.room.state='active';
      api=async(url,options={})=>{
        calls.push({url,method:options.method||'GET'});
        if(url.includes('message-receipt'))return {authoritative:true,state:'committed',
          client_message_id:'pending-b',conversation_id:'room-b',
          message:{id:'committed-b',body_text:'pending-b',author_kind:'owner',created:1,version:1}};
        throw new Error('Unexpected send while checking other room');
      };
      relay.action('relay-retry',{dataset:{id:'pending-b'}});
    }""")
    page.wait_for_function('!roomB.relayFlight')
    assert page.evaluate('roomB.messages[0].id') == 'committed-b'
    assert page.evaluate('roomB.relayOutbox.length') == 0
    assert page.evaluate('roomA.messages[0].id') == 'original-message'
    assert page.evaluate('calls.every(call=>call.method==="GET")')
