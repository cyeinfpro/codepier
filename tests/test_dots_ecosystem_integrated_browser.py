"""Real isolated Hub/Agent HTTP and rendering measurements for ecosystem UI.

The browser serves the actual panel. Only response loss is injected; accepted
messages are committed by the real Hub and verified from its disposable database.
Performance history is a labelled DOM workload, not claimed server history.
"""
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from hub.store import Store
from tests import support
from tests.collaboration_support import collaboration_stack as collaboration_stack
from tests.test_collaboration_dot_relay_flow import prepare, send, no_work

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]
REPORT = Path(os.environ.get('CODEPIER_ECOSYSTEM_EVIDENCE', '.work/ecosystem-integrated'))
HOOK = r"""(() => {
  for (const [name, kind] of [['CodePierCollaborationRelay','Relay'],
      ['CodePierCollaborationWorkspace','Workspace']]) {
    let current;
    Object.defineProperty(window,name,{configurable:true,get(){return current;},set(value){
      current=value;const create=value.create;
      value.create=function(ctx){
        const instance=create.call(this,ctx);
        window['ecosystem'+kind]=instance;
        window['ecosystem'+kind+'Context']=ctx;
        if(kind==='Relay'){
          const render=instance.renderMessage;
          instance.renderMessage=function(m){window.ecosystemRenders ||= [];
            window.ecosystemRenders.push(m.id);return render.call(this,m);};
        }
        return instance;
      };
    }});
  }
})()"""


def record(name, data):
    REPORT.mkdir(parents=True, exist_ok=True)
    (REPORT / (name + '.json')).write_text(json.dumps(data, ensure_ascii=False, indent=2))


def wait_idle(page, field):
    # Playwright wait_for_function(string) uses eval inside the page, disallowed
    # by the production CSP. Direct protocol evaluation leaves CSP unchanged.
    deadline = time.monotonic() + 15
    while page.evaluate("(field) => !!ecosystemRelayContext.chat()[field]", field):
        assert time.monotonic() < deadline, field + ' did not settle'
        page.wait_for_timeout(50)


def enable_persistence(page):
    page.locator('.cc-relay-tools > summary').click()
    page.locator('.cc-pending-settings > summary').click()
    control = page.locator('[data-cc-action="relay-persistence-toggle"]')
    expect(control).to_contain_text('我同意')
    control.click()
    expect(control).to_contain_text('关闭并清理', timeout=3000)
    assert page.evaluate("JSON.parse(localStorage.getItem('codepier-relay-pending-v1')).enabled")


def count_intent(stack, client_id):
    store = Store(stack.hubdir)
    try:
        return store.one('SELECT COUNT(*) n FROM collaboration_messages WHERE source_id=?',
                         (client_id,))['n']
    finally:
        store.close()


@pytest.mark.parametrize('engine,width', [('chromium',1440),('webkit',390)])
def test_real_http_lost_ack_reload_and_scope_generation(collaboration_stack, chat_browser_pool, engine, width):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width':width,'height':920})
    context.add_init_script(HOOK)
    page = context.new_page()
    page.on('dialog', lambda dialog: dialog.accept())
    events, posts, receipts, scoped_responses = [], [], [], []
    state = {'block': False, 'lose_once': True, 'phase': 'lost-ack', 'reloading': False}
    page.on('framenavigated', lambda frame: state.update(phase='after-reload')
            if state['reloading'] and frame == page.main_frame else None)

    def message(route):
        payload = route.request.post_data_json
        events.append([state['phase'], 'POST', payload['client_message_id']])
        posts.append(payload)
        if state['phase']=='scope-changed':
            response=route.fetch()
            scoped_responses.append({'status':response.status,'body':response.json()})
            route.fulfill(response=response)
        elif state['block']:
            route.abort('failed')
        elif state['lose_once']:
            state['lose_once'] = False
            response = route.fetch()
            assert response.ok, response.text()
            assert response.json()['message']['client_message_id'] == payload['client_message_id']
            route.abort('failed')
        else:
            route.continue_()

    def receipt(route):
        client_id = parse_qs(urlparse(route.request.url).query)['client_message_id'][0]
        events.append([state['phase'], 'GET', client_id])
        response = route.fetch()
        assert response.ok, response.text()
        data = response.json()
        receipts.append(data)
        route.fulfill(response=response)

    try:
        prepare(stack,page)
        page.route('**/api/collaboration/message',message)
        page.route('**/api/collaboration/message-receipt?*',receipt)
        send(page,'HTTP-ACK-LOSS synthetic fixture')
        assert len(posts)==1 and len(receipts)>=1
        assert receipts[-1]['authoritative'] and receipts[-1]['state']=='committed'
        assert count_intent(stack,posts[0]['client_message_id'])==1
        assert not posts[0].get('pending_scope_revision')

        enable_persistence(page)
        if os.environ.get('CODEPIER_ECOSYSTEM_EVIDENCE'):
            REPORT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(REPORT / f'http-p2-{engine}-{width}.png'), full_page=True)
        context.set_offline(True)
        send(page,'P2-RELOAD synthetic clicked intent',settle=False)
        persisted=page.evaluate("JSON.parse(localStorage.getItem('codepier-relay-pending-v1')).records[0]")
        original_id=persisted['id']
        assert persisted['authorization_revision']
        assert count_intent(stack,original_id)==0
        state.update(block=True,phase='before-reload')
        context.set_offline(False)
        state['reloading']=True
        page.reload()
        state['reloading']=False
        expect(page.locator('#cc-message-input')).to_be_enabled(timeout=20000)
        expect(page.locator('#cc-relay-outbox')).to_contain_text('P2-RELOAD',timeout=20000)
        wait_idle(page, 'relayFlight')
        after=[event for event in events if event[0]=='after-reload' and event[2]==original_id]
        assert after and after[0][1]=='GET', after
        state['block']=False
        page.evaluate("(id)=>ecosystemRelay.action('relay-retry',{dataset:{id}})",original_id)
        expect(page.locator('#cc-relay-outbox [data-pending-id]')).to_have_count(0,timeout=15000)
        assert count_intent(stack,original_id)==1
        assert all(p['pending_scope_revision']==persisted['authorization_revision']
                   for p in posts if p['client_message_id']==original_id)
        assert page.evaluate("JSON.parse(localStorage.getItem('codepier-relay-pending-v1')).records.length")==0

        # Narrow the disposable project's execution setting, changing the real scope digest.
        context.set_offline(True)
        send(page,'P2-OLD-SCOPE must remain unsent',settle=False)
        stale=page.evaluate("JSON.parse(localStorage.getItem('codepier-relay-pending-v1')).records[0]")
        project=page.evaluate("ecosystemRelayContext.state.project")
        store=Store(stack.hubdir)
        try:
            store.execute("UPDATE projects SET allow_tasks=0 WHERE id=?",(project,))
        finally:
            store.close()
        state['phase']='scope-changed'
        state['block']=True
        context.set_offline(False)
        page.evaluate("(id)=>ecosystemRelay.action('relay-retry',{dataset:{id}})",stale['id'])
        wait_idle(page, 'relayFlight')
        page.evaluate("ecosystemRelay.sync({readOnly:true})")
        wait_idle(page, 'relaySync')
        assert count_intent(stack,stale['id'])==0
        assert page.evaluate("localStorage.getItem('codepier-relay-pending-v1')===null")
        assert all(response['status']==409 for response in scoped_responses), scoped_responses
        no_work(stack)
        record('http-'+engine,{'engine':engine,'width':width,'fixture_root':str(support.BASE),
            'lost_ack_commits':1,'reload_commits':1,'scope_changed_commits':0,
            'events':events,'receipt_states':[r['state'] for r in receipts], 'scope_post_statuses':[r['status'] for r in scoped_responses],
            'native_host_verified':False,'external_delivery_verified':False})
    finally:
        context.close()


@pytest.mark.parametrize('engine',['chromium','webkit'])
def test_loaded_history_rendering_and_viewport_latency(collaboration_stack, chat_browser_pool, engine):
    stack=collaboration_stack
    context=chat_browser_pool(engine).new_context(viewport={'width':1440,'height':920},reduced_motion='reduce')
    context.add_init_script(HOOK)
    page=context.new_page()
    try:
        prepare(stack,page)
        send(page,'DOM workload seed, no execution.')
        wait_idle(page, 'relaySync')
        page.evaluate("""() => {
          const ctx=ecosystemRelayContext,c=ctx.chat();
          clearTimeout(ctx.state.timer);
          const seed=c.messages.find(m=>m.author_kind==='panel_owner'||m.author_kind==='owner');
          c.messages=Array.from({length:1000},(_,i)=>({...structuredClone(seed),
            id:i===0?seed.id:'dom-history-'+i,client_message_id:i===0?seed.client_message_id:'dom-client-'+i,server_sequence:i+1,
            body_text:'历史样本 '+i+'：这是中英文混合的已加载对话。 '+('bounded content '.repeat(i%4+1)),
            relay_receipts:[{dot_id:ctx.mentionSlots()[0].id,state:'replied'}]}));
          ctx.patchTimeline();
          window.ecosystemRenders=[];
          const before=[...document.querySelectorAll('.cc-message-list > [data-message-id]')];
          ctx.patchTimeline(false,new Set());
          const unchanged=before.every((n,i)=>n===document.querySelectorAll('.cc-message-list > [data-message-id]')[i]);
          const zero=ecosystemRenders.length===0;
          c.messages[500].version=(c.messages[500].version||1)+1;
          c.messages[500].body_text+=' 单条实际更新';
          ctx.patchTimeline(false,new Set([c.messages[500].id]));
          window.ecosystemDirty={unchanged,zero,rendered:[...ecosystemRenders],
            count:document.querySelectorAll('.cc-message-list > [data-message-id]').length};
          ecosystemWorkspace.open('agents');ecosystemWorkspace.open('discussion');
        }""")
        dirty=page.evaluate('ecosystemDirty')
        assert dirty=={'unchanged':True,'zero':True,'rendered':['dom-history-500'],'count':1000}
        context.set_offline(True)
        measurements=[]
        for width in [390,768,1024,1440,1920]:
            print('MEASURE', engine, width, flush=True)
            page.set_viewport_size({'width':width,'height':920})
            data=page.evaluate("""async (width) => {
              const ctx=ecosystemRelayContext,c=ctx.chat(),relay=ecosystemRelay,ui=ecosystemWorkspace;
              const form=document.querySelector('#cc-command'),area=form.elements.request;
              const original=area,feed=document.querySelector('.cc-feed');
              area.value='中文组合输入草稿';c.draft=area.value;area.setSelectionRange(2,5);
              ctx.state.composing=true;
              ctx.patchTimeline(false,new Set());
              const composition=area===original && area.value==='中文组合输入草稿' && area.selectionStart===2;
              ctx.state.composing=false;
              const stamp=()=>performance.now();
              const sends=[],details=[];
              for(let i=0;i<200;i++){
                c.relayOutbox=[];c.relayAnchor=null;relay.paintOutbox();
                area.value='离线性能样本 '+width+'-'+i;c.draft=area.value;
                const start=stamp();relay.send(form);
                const bubble=document.querySelector('#cc-relay-outbox [data-pending-id]');
                if(!bubble || bubble.getBoundingClientRect().height<=0)throw new Error('Local bubble missing');
                sends.push(stamp()-start);
                const at=stamp();ui.open('agents');
                const panel=document.querySelector('#cc-workspace-panel');
                if(panel.hidden || panel.getBoundingClientRect().height<=0)throw new Error('Detail missing');
                details.push(stamp()-at);ui.open('discussion');
                if(i%25===24) await new Promise(resolve=>setTimeout(resolve,0));
              }
              c.relayOutbox=[];c.relayAnchor=null;c.draft='中文组合输入草稿';area.value=c.draft;
              relay.paintOutbox();relay.detach();
              const q=xs=>[...xs].sort((a,b)=>a-b)[Math.ceil(xs.length*.95)-1];
              return {width,samples:sends.length,bubble_p95_ms:q(sends),detail_p95_ms:q(details),
                bubble_max_ms:Math.max(...sends),detail_max_ms:Math.max(...details),
                no_horizontal_overflow:document.documentElement.scrollWidth<=innerWidth,
                composition,composer_identity:area===original,feed_identity:feed.isConnected,
                motion:getComputedStyle(document.querySelector('.cc-relay-turn')).animationName};
            }""",width)
            assert data['no_horizontal_overflow'] and data['composition']
            assert data['composer_identity'] and data['feed_identity']
            measurements.append(data)
            record('render-partial-'+engine, {'measurements': measurements})
            print('RESULT', engine, json.dumps(data), flush=True)
            if width in {390, 1440} and os.environ.get('CODEPIER_ECOSYSTEM_EVIDENCE'):
                # Synthetic fixture only; capture the real reachable options.
                page.locator('.cc-relay-tools > summary').click()
                page.locator('.cc-pending-settings > summary').click()
                page.screenshot(path=str(REPORT / ('viewport-' + engine + '-' + str(width) + '.png')))
                page.locator('.cc-pending-settings > summary').click()
                page.locator('.cc-relay-tools > summary').click()
        record('render-'+engine,{'engine':engine,'browser_version':chat_browser_pool(engine).version,
            'fixture_root':str(support.BASE),'history':1000,'samples_per_width':200,
            'total_samples_each_measure':1000,'dirty_assertions':dirty,'measurements':measurements,
            'method':'Actual production handlers through forced layout; DOM-ready, not compositor paint. Loaded connection details, 25-sample yielding chunks. Offline local sends; no server/provider latency included.',
            'limits':'Synthetic bounded DOM history; not server 1000-message pagination. No native Dot or real IM verification. Reduced-motion mode.'})
        # Timing is reported rather than silently declared a pass against SLA.
        no_work(stack)
    finally:
        context.close()
