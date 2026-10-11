"""Real browser controls with synthetic HTTP replies; no Weixin account is used."""
import os
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width', [1440, 390])
def test_owner_channel_controls_read_only_open_confirmed_write_and_stale_fences(tmp_path, engine, width):
    with sync_playwright() as playwright:
        browser_type = getattr(playwright, engine)
        executable = os.environ.get('CODEPIER_TEST_CHROMIUM') if engine == 'chromium' else None
        if not executable and not Path(browser_type.executable_path).is_file():
            pytest.skip(engine + ' runtime is not installed; no browser result claimed')
        browser = browser_type.launch(headless=True, **({'executable_path': executable} if executable else {}))
        try:
            page = browser.new_page(viewport={'width': width, 'height': 1000})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            css = (ROOT / 'web/collaboration-channels.css').read_text()
            page.set_content('<style>body{margin:16px;font:16px sans-serif}input,select,textarea{max-width:100%;box-sizing:border-box}.cc-field{display:grid;gap:4px}.btn{padding:8px}#drawer{max-width:800px;margin:auto}*{box-sizing:border-box}' + css + '</style><button id="open">管理</button><div id="drawer"></div><div id="files"></div>')
            page.add_script_tag(path=str(ROOT / 'web/collaboration-channels.js'))
            page.evaluate('''() => {
                window.S = {session:{id:'fixture-session'},space_id:'fixture-space'};
                window.state = {generation:1,drawerEpoch:0,project:'projectA',environment:'production',conversation:'conversationA',
                    snapshot:{can_manage:true,capabilities:{room_scope_v1:true},room:{id:'roomA',project_id:'projectA'},
                    conversation:{id:'conversationA',projects:[{project_id:'projectA',project_label:'当前测试项目',environment_id:'production'}]}}};
                window.reads=[]; window.writes=[]; window.delayed=[]; window.delayReads=false;
                window.data={scopes:{items:[],eligible_connections:[{id:'grantA',label:'当前原连接',scope_version:3}]},
                    weixin:{enabled:false,accounts:[],bindings:[],deliveries:[],artifact_candidates:[],reply_candidates:[]}};
                window.api = async (url) => { reads.push(url); if(delayReads) await new Promise(resolve => delayed.push(resolve)); return structuredClone(url.includes('room-scopes') ? data.scopes : data.weixin); };
                window.E = value => String(value ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
                window.channels = CodePierCollaborationChannels.create({state,E,
                    field:(label,control)=>'<label class="cc-field"><span>'+E(label)+'</span>'+control+'</label>',
                    button:(action,label,item={},attrs='')=>'<button type="button" class="btn" data-cc-action="'+E(action)+'" data-id="'+E(item.id||'')+'" '+attrs+'>'+E(label)+'</button>',
                    mentionSlots:()=>[],showDrawer:(title,body)=>{state.drawerEpoch++;document.querySelector('#drawer').innerHTML=body},
                    mutation:async(operation,args)=>{writes.push({operation,args});return {ok:true}}
                });
            }''')
            page.evaluate("channels.act('dot-channel-open', document.querySelector('#open'))")
            assert page.locator('.cc-channel-pane').count() == 1
            assert page.evaluate('reads.length') == 2 and page.evaluate('writes.length') == 0
            assert page.locator('[data-cc-action="dot-channel-import"]').is_disabled()
            assert page.locator('[data-cc-action="dot-channel-bind"]').is_disabled()
            page.get_by_text('明确设置连接范围', exact=True).click()
            group = page.locator('[data-channel-form="scope"]')
            group.locator('[name="grant_id"]').select_option('grantA')
            # Missing owner confirmation cannot send a mutation.
            page.evaluate("channels.act('dot-channel-scope-save', document.querySelector('[data-cc-action=dot-channel-scope-save]'))")
            assert page.evaluate('writes.length') == 0
            group.locator('[name="confirm_scope"]').check()
            page.evaluate("channels.act('dot-channel-scope-save', document.querySelector('[data-cc-action=dot-channel-scope-save]'))")
            saved = page.evaluate('writes[0]')
            assert saved['operation'] == 'room-scope'
            assert saved['args']['expected_version'] == 3 and saved['args']['grant_id'] == 'grantA'
            assert saved['args']['project_rules'] == [{'project_id': 'projectA', 'capabilities': ['read'], 'execution_targets': ['agent']}]
            assert saved['args']['execution_mode'] == 'file_tools_only'
            assert saved['args']['conversation_id'] == 'conversationA'
            assert page.evaluate('writes.length') == 1
            page.evaluate('''() => {
                const message={attachments:[
                    {id:'a'.repeat(32),name:'<img src=x onerror=alert(1)>.png',state:'ready',kind:'file',size:12,download_path:'https://attacker.invalid/private'},
                    {id:'b'.repeat(32),name:'第二件文件.pdf',state:'unavailable',kind:'file',reason_code:'download_failed'},
                    {id:'c'.repeat(32),name:'第三件文件.txt',state:'pending',kind:'file'}],
                    channel_deliveries:[{state:'needs_attention',parts:[{ordinal:0,kind:'text',state:'accepted'},{ordinal:1,kind:'attachment',state:'unknown'}]}]};
                document.querySelector('#files').innerHTML=CodePierCollaborationChannels.attachments(message,E);
            }''')
            assert page.locator('.cc-channel-file').count() == 3
            assert page.locator('#files img').count() == 0
            assert page.locator('#files a').count() == 1
            href = page.locator('#files a').get_attribute('href')
            assert href.startswith('/api/collaboration/attachments/') and 'attacker' not in href
            assert 'fixture-space' in href
            assert '发送结果未知' in page.locator('#files').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            evidence = Path(os.environ.get('CODEPIER_TEST_EVIDENCE_DIR', str(tmp_path)))
            evidence.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(evidence / f'channels-{engine}-{width}.png'), full_page=True)
            # Late old GET results cannot restore a replaced room or its controls.
            page.evaluate('''() => {
                delayReads=true;
                window.opening=channels.act('dot-channel-open',document.querySelector('#open'));
                state.generation++;state.project='projectB';state.conversation='conversationB';
                document.querySelector('#drawer').textContent='新房间';
                delayed.forEach(resolve=>resolve());
            }''')
            page.evaluate('opening')
            assert page.locator('#drawer').inner_text() == '新房间'
            page.evaluate('channels.clear(); state.snapshot.can_manage=false')
            refused = page.evaluate("channels.act('dot-channel-open',document.querySelector('#open')).then(()=>false,()=>true)")
            assert refused and page.evaluate('writes.length') == 1
            assert not errors
        finally:
            browser.close()
