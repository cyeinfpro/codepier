"""Real browsers verify the current room task defaults, not the broader old policy."""
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_task_scope_ui_uses_common_room_capabilities_and_keeps_legacy_policy_ceiling(chat_browser_pool, engine):
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    try:
        page.set_content('<main id="drawer"></main>')
        page.add_script_tag(path=str(ROOT / 'web/collaboration-delegation.js'))
        page.evaluate("""() => {
          const policy = {
            id:'policy', slot_id:'slot', version:1, effective_active:true,
            lifetime_mode:'until_revoked', purpose:'Fixture room',
            capabilities:['read','write','execute'],
            execution_targets:['project_agent','vps:fixture'],
            task_defaults:{capabilities:['read'],execution_targets:['project_agent'],project_ids:['A','B']}
          };
          window.state={delegationPolicies:[policy],project:'A'};
          window.draft={mentions:['slot']};
          const escape = s => String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
          window.ui=CodePierCollaborationDelegation.create({
            state, E:escape, chat:()=>draft, projectLabel:id=>'Project '+id,
            field:(label,html)=>'<label>'+escape(label)+html+'</label>',
            button:()=>'', options:()=>'', when:()=>'', slotMember:()=>({label:'Fixture assistant'}),
            mentionSlots:()=>[{id:'slot'}], liveJoin:()=>true, getRecord:()=>null,
            readDrawer:()=>{}, showDrawer:(_title,html)=>document.querySelector('#drawer').innerHTML=html,
            closeDrawer:()=>{}, mutation:()=>{}, refreshChat:()=>{}, coordination:{},
            mergeMessages:()=>{}, patchTimeline:()=>{}
          });
          ui.selectPolicy(policy);
          window.policy=policy;
        }""")
        assert page.evaluate('draft.delegationCapabilities') == ['read']
        assert page.evaluate('draft.delegationTargets') == ['project_agent']
        assert page.evaluate('ui.selectedScopeValid()')
        page.evaluate('ui.openScope(null)')
        form = page.locator('#cc-delegation-scope')
        expect(form.locator('[name="capabilities"]')).to_have_count(1)
        expect(form.locator('[name="capabilities"][value="read"]')).to_be_checked()
        expect(form).to_contain_text('Project A、Project B')
        expect(form).to_contain_text('共同能力')
        expect(form).to_contain_text('明确选择项目')

        # A stale selection cannot retain capabilities removed from room scope
        # even if the original delegation policy version is unchanged.
        page.evaluate("""() => {
          draft.delegationCapabilities=['read','write','execute'];
          draft.delegationTargets=['project_agent','vps:fixture'];
          ui.selectPolicy(policy);
        }""")
        assert page.evaluate('draft.delegationCapabilities') == ['read']
        assert page.evaluate('draft.delegationTargets') == ['project_agent']

        # Older servers/rooms without working scope keep their original policy,
        # and multiple targets still require an explicit task selection.
        page.evaluate("""() => {
          delete policy.task_defaults; policy.version=2; ui.selectPolicy(policy);
          ui.openScope(null);
        }""")
        expect(form.locator('[name="capabilities"]')).to_have_count(3)
        assert page.evaluate('draft.delegationTargets') == []
        assert not page.evaluate('ui.selectedScopeValid()')
        page.evaluate('policy.effective_active=false')
        assert not page.evaluate('ui.selectedDelegationValid()')
    finally:
        context.close()
