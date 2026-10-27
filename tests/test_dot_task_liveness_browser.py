"""Actual Chromium rendering over an isolated component, not a live Dot host.

Set CODEPIER_TEST_CHROMIUM to a locally available Chromium executable when the
standard Playwright browser is not installed. No production endpoints are used.
"""
from tests.browser_launch import launch_browser
import os
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.browser, pytest.mark.integration]

SETUP = r"""() => {
  window.S = {session: 'synthetic-session', space_id: 'synthetic-space', page: 'collaboration'};
  window.calls = []; window.badMarkupRan = false;
  const source = {id: 'original-message', client_message_id: 'original-client', author_kind: 'owner',
    created: 900, version: 1, body_text: '原消息：请核对任务结果。', relay_receipts: [{state: 'working'}]};
  window.c = {messages: [source], mentions: [], after: '', visibility: 'visibility', draft: '尚未发送的草稿', relayEpoch: 0};
  window.state = {snapshot: {capabilities: {native_dot_relay: true}, room: {state: 'active'}, can_manage: true},
    view: 'discussion', project: 'project', environment: 'production', generation: 1, refreshSequence: 0,
    members: [], session: S.session, space: S.space_id, chats: new Map([['fixture', c]])};
  c.relayOutbox = [{id: 'unsent', chat: c, phase: 'retry', due: 0, attempts: 0, epoch: 0,
    session: S.session, space: S.space_id, payload: {project: 'project', environment_id: 'production',
      body_text: '不得由只读核对自动重发', client_message_id: 'unsent'}}];
  const E = (s) => String(s ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const task = {work_item_id: 'original-work', message_id: source.id, execution_state: 'running',
    delivery_state: 'pending', wait_reason: 'original_operation_pending', operation_count: 1, uncertain_count: 0,
    last_operation_verified_at: 900, last_output_at: null, last_consumer_heartbeat_at: 910, last_node_contact_at: 920,
    last_progress_summary: '<img src=x onerror="window.badMarkupRan=true">原步骤仍在核对'};
  window.api = async (url, options={}) => {
    calls.push({url, method: options.method || 'GET'});
    if (!url.startsWith('/api/collaboration/relay-sync?')) throw new Error('Unexpected mutation route: '+url);
    return {server_now: 1000, visibility_token: 'visibility', updates: [source], items: [], task_states: [task],
      room_state: 'active', can_manage: true, has_more: false};
  };
  window.relay = CodePierCollaborationRelay.create({state, E,
    button: (action,text,m={})=>`<button type="button" data-cc-action="${E(action)}" data-id="${E(m.id || '')}">${E(text)}</button>`,
    chat: ()=>c, chatScope: ()=>({project:'project',environment_id:'production'}),
    mentionSlots: ()=>[{id:'fixture-dot',duplex:true,state:'registered'}],
    messageById: (id)=>c.messages.find(m=>m.id===id), messageText: m=>m.body_text,
    requestId: ()=>{throw new Error('A read must not allocate a new mutation ID')},
    dots: {isConversationMessage: ()=>true, refreshCards: ()=>{}},
    delegation: {messageCard: ()=>'',selectPolicy: ()=>{}},
    patchTimeline: ()=>{
      const existing=document.querySelector('[data-message-id]');
      const holder=document.createElement('div');holder.innerHTML=relay.renderMessage(source);
      if(existing) relay.patchMessage(existing,holder.firstElementChild,source);
      else document.querySelector('#timeline').append(holder.firstElementChild);
    }, patchDraftContext: ()=>{}, patchDelegationComposer: ()=>{},
    mergeMessages: ()=>{}, recoverAccess: async ()=>{throw new Error('Unexpected access change')}
  });
  document.querySelector('#controls').innerHTML=relay.toolbar();
  document.querySelector('#timeline').innerHTML=relay.renderMessage(source);
  document.querySelector('[data-cc-action="relay-reconcile"]').addEventListener('click',event=>relay.action('relay-reconcile',event.currentTarget));
}"""


@pytest.mark.parametrize('width', [1440, 390])
def test_reconcile_is_read_only_and_evidence_is_honest_escaped_and_focus_safe(width):
    executable = os.environ.get('CODEPIER_TEST_CHROMIUM')
    with sync_playwright() as p:
        options = {'headless': True}
        if executable:
            options['executable_path'] = executable
        browser = launch_browser(p.chromium, **options)
        context = browser.new_context(viewport={'width': width, 'height': 920})
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.set_content('''<html lang="zh-CN"><meta charset="utf-8"><body>
              <main class="collaboration"><h1>任务反馈核对 · 隔离组件测试</h1><div id="controls"></div>
              <div id="timeline"></div><form id="cc-command"><textarea name="request" id="cc-message-input"></textarea>
              <button type="submit">发送</button></form><div id="cc-relay-network" hidden></div></main></body></html>''')
            for name in ('styles.css', 'collaboration.css', 'collaboration-relay.css'):
                page.add_style_tag(content=(ROOT/'web'/name).read_text())
            page.add_style_tag(content='body{margin:0;padding:16px;display:block;overflow:auto} .collaboration{display:block;max-width:960px;margin:auto;min-width:0} .cc-message{min-width:0} textarea{max-width:100%;width:100%;min-height:64px}')
            page.add_script_tag(content=(ROOT/'web/collaboration-relay.js').read_text())
            page.evaluate(SETUP)
            area = page.locator('#cc-message-input')
            area.fill('尚未发送的草稿')
            # Programmatic activation leaves focus on the composing control.
            page.evaluate("document.querySelector('[data-cc-action=relay-reconcile]').click()")
            expect(page.locator('.cc-task-liveness')).to_have_count(1)
            expect(page.locator('.cc-task-liveness summary')).to_contain_text('执行证据已过时')
            page.locator('.cc-task-liveness summary').click()
            expect(page.locator('.cc-task-liveness')).to_contain_text('不表示模型持续在线')
            expect(page.locator('.cc-task-liveness')).to_contain_text('不是操作系统沙箱')
            assert page.locator('.cc-task-liveness img').count() == 0
            area.focus()
            page.evaluate("document.querySelector('[data-cc-action=relay-reconcile]').click()")
            page.wait_for_function('calls.length === 2 && !c.relaySync')
            expect(area).to_be_focused()
            expect(area).to_have_value('尚未发送的草稿')
            assert page.locator('.cc-task-liveness').evaluate('(node)=>node.open')
            assert page.evaluate('calls.every(x=>x.method==="GET")')
            assert page.evaluate('c.relayOutbox[0].phase') == 'retry'
            assert page.evaluate('c.relayOutbox[0].attempts') == 0
            assert not page.evaluate('badMarkupRan')
            assert not errors, errors
            evidence_dir = os.environ.get('CODEPIER_TEST_EVIDENCE_DIR')
            if evidence_dir:
                output = Path(evidence_dir)
                output.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(output/f'dot-recovery-component-{width}.png'), full_page=True)
            page.evaluate('relay.purge(c)')
            assert page.evaluate('Object.keys(c.relayTaskStates).length') == 0
        finally:
            context.close()
            browser.close()
