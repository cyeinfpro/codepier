"""Synthetic browser consent tests; no provider, real credential or production mutation."""
import pytest
from playwright.sync_api import expect
from tests.weixin_browser_support import qr_page as qr_page


def setup_owner(page, active=False, polling=False):
    page.evaluate("""({active,polling}) => {
      const now=Date.now()/1000;
      data.weixin.accounts=[{id:'accountA',bot_id:'botA',label:'合成微信连接',
        state:'active',credential_version:4}];
      const option={binding_id:'bindingA',available:true,scope_digest:'scope-v1',
        capabilities:['read','write','execute'],execution_targets:['agent'],
        project_id:'projectA',policy_id:'policyA',policy_version:5,max_delegations:100};
      data.weixin.owner_task_options=[option];
      data.weixin.bindings=[{id:'bindingA',account_id:'accountA',dot_id:'dotA',state:'active',
        version:7,generation:2,peer_id:'synthetic-owner-peer',
        expires_at:now+86400,lifetime_mode:active?'until_revoked':'timed',
        owner_authorization:active?{...option,id:'authorityA',version:3,state:'active',
          lifetime_mode:'until_revoked',budget_mode:'lifetime'}:null}];
      if(polling)data.weixin.bindings.push({id:'pairingB',account_id:'accountA',dot_id:'dotA',
        state:'pairing',version:1,generation:1,expires_at:now+86400,
        pairing_until:now+900,pairing_challenge:'SYNTHETIC-PAIRING-ONLY'});
    }""", {"active": active, "polling": polling})
    page.evaluate("clickAction('dot-channel-open')")


def confirm_owner(page):
    page.locator(".cc-wx-owner-authority summary").click()
    form = page.locator('[data-channel-form="owner-authority"]')
    for purpose in ("本人身份", "未来任务与回传", "持续与撤销"):
        expect(form.locator('.cc-consent-purposes')).to_contain_text(purpose)
    form.locator('[name="confirm_owner_agreement"]').check()


@pytest.mark.parametrize("width", [1440, 390])
def test_exact_consent_default_budget_and_repeat_click(qr_page, width, tmp_path):
    page, engine = qr_page
    page.set_viewport_size({"width": width, "height": 1000})
    setup_owner(page)
    panel = page.locator(".cc-wx-owner-authority")
    panel.locator("summary").click()
    for text in ("projectA", "read / write / execute", "agent", "policyA",
                 "并非操作系统沙箱", "累计最多 100"):
        expect(panel).to_contain_text(text)
    assert page.evaluate("writes.length") == 0
    assert panel.locator("input:checked").count() == 0
    # The new UI has one explicit acknowledgement, not three separate clicks.
    # Every purpose remains visible and the exact independent API fields below
    # are unchanged; missing acknowledgement still prevents every mutation.
    for purpose in ("本人身份", "未来任务与回传", "持续与撤销"):
        expect(panel.locator('.cc-consent-purposes')).to_contain_text(purpose)
    page.evaluate("clickAction('dot-channel-owner-approve')")
    assert page.evaluate("writes.length") == 0
    panel.locator('[name="confirm_owner_agreement"]').check()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(tmp_path / f"weixin-owner-consent-{engine}-{width}.png"), full_page=True)
    page.evaluate("Promise.all([clickAction('dot-channel-owner-approve'),clickAction('dot-channel-owner-approve')])")
    assert page.evaluate("writes.length") == 1
    saved = page.evaluate("writes[0]")
    assert saved["operation"] == "weixin-owner-authority"
    assert saved["args"] == {
        "binding_id": "bindingA", "expected_binding_version": 7,
        "expected_authority_version": 0, "action": "approve",
        "confirm_peer_id": "synthetic-owner-peer", "expected_scope_digest": "scope-v1",
        "confirm_owner_tasks": True, "confirm_until_revoked": True, "budget_mode": "lifetime",
        "project": "projectA", "environment_id": "test", "conversation_id": "conversationA",
    }
    page.locator(".cc-wx-owner-authority > summary").click()
    page.locator('[name="budget_mode"]').select_option("rolling_24h")
    form = page.locator('[data-channel-form="owner-authority"]')
    expect(form.locator('[name="confirm_owner_agreement"]')).not_to_be_checked()
    form.locator('[name="confirm_owner_agreement"]').check()
    page.evaluate("clickAction('dot-channel-owner-approve')")
    assert page.evaluate("writes.length") == 1
    page.locator('[name="confirm_daily_budget"]').check()
    page.evaluate("clickAction('dot-channel-owner-approve')")
    args = page.evaluate("writes[1].args")
    assert args["budget_mode"] == "rolling_24h" and args["confirm_daily_budget"] is True


def test_separate_revoke_and_cancel(qr_page):
    page, _ = qr_page
    setup_owner(page, active=True)
    expect(page.locator(".cc-wx-bindings")).to_contain_text("有效期：持续至面板撤销")
    page.evaluate("confirmResult=false; clickAction('dot-channel-owner-revoke')")
    assert page.evaluate("writes.length") == 0
    assert "原绑定聊天仍保留" in page.evaluate("confirmations.at(-1)")
    page.evaluate("confirmResult=true; clickAction('dot-channel-owner-revoke')")
    saved = page.evaluate("writes[0]")
    assert saved["operation"] == "weixin-owner-authority"
    assert saved["args"]["action"] == "revoke"
    assert saved["args"]["expected_authority_version"] == 3
    assert saved["args"]["expected_binding_version"] == 7
    page.evaluate("clickAction('dot-channel-revoke')")
    assert page.evaluate("writes[1].operation") == "weixin-control"
    assert "未完成任务" in page.evaluate("confirmations.at(-1)")


@pytest.mark.parametrize("fence", ["room", "session", "drawer", "owner", "clear"])
def test_checked_consent_cannot_escape_original_scope(qr_page, fence):
    page, _ = qr_page
    setup_owner(page)
    confirm_owner(page)
    page.evaluate("""fence => {
      if(fence==='room'){state.generation++;state.project='projectB';state.conversation='conversationB';}
      if(fence==='session')S.session={id:'other-session'};
      if(fence==='drawer')state.drawerEpoch++;
      if(fence==='owner')state.snapshot.can_manage=false;
      if(fence==='clear')channels.clear();
    }""", fence)
    rejected = page.evaluate("clickAction('dot-channel-owner-approve').then(()=>false,()=>true)")
    assert rejected and page.evaluate("writes.length") == 0


def test_poll_does_not_upgrade_checked_scope_digest(qr_page):
    page, _ = qr_page
    setup_owner(page, polling=True)
    confirm_owner(page)
    page.evaluate("""() => {
      Object.assign(data.weixin.owner_task_options[0], {scope_digest:'scope-v2',
        policy_version:6,execution_targets:['changed-target']});
    }""")
    page.clock.run_for(5001)
    page.evaluate("Promise.resolve()")
    expect(page.locator(".cc-wx-owner-authority")).not_to_contain_text("changed-target")
    page.evaluate("clickAction('dot-channel-owner-approve')")
    assert page.evaluate("writes[0].args.expected_scope_digest") == "scope-v1"


def test_incomplete_or_unavailable_scope_is_not_approvable(qr_page):
    page, _ = qr_page
    setup_owner(page)
    page.evaluate("data.weixin.owner_task_options[0].scope_digest=''; clickAction('dot-channel-open')")
    assert page.locator('[data-cc-action="dot-channel-owner-approve"]').count() == 0
    page.evaluate("""() => {
      Object.assign(data.weixin.owner_task_options[0], {scope_digest:'valid',available:false,
        reason_code:'WEIXIN_PAIRING_UNVERIFIED'});
    }""")
    page.evaluate("clickAction('dot-channel-open')")
    expect(page.locator(".cc-wx-owner-authority")).to_contain_text("WEIXIN_PAIRING_UNVERIFIED")
    assert page.locator('[data-cc-action="dot-channel-owner-approve"]').count() == 0
    assert page.evaluate("writes.length") == 0


def test_binding_lifetime_requires_specific_consent(qr_page):
    page, _ = qr_page
    setup_owner(page)
    for lifetime in ("timed", "until_revoked"):
        page.locator(".cc-wx-bind > summary").click()
        form = page.locator('[data-channel-form="bind"]')
        expect(form.locator('[name="lifetime_mode"]')).to_have_value("timed")
        form.locator('[name="account_id"]').select_option("accountA")
        form.locator('[name="dot_id"]').select_option("dotA")
        form.locator('[name="confirm_ingress"]').check()
        form.locator('[name="lifetime_mode"]').select_option(lifetime)
        if lifetime == "until_revoked":
            rejected = page.evaluate("clickAction('dot-channel-bind').then(()=>false,()=>true)")
            assert rejected and page.evaluate("writes.length") == 1
            form.locator('[name="bind_until_revoked"]').check()
        page.evaluate("clickAction('dot-channel-bind')")
        assert page.evaluate("writes.at(-1).args.lifetime_mode") == lifetime
        assert page.evaluate("writes.at(-1).args.confirm_until_revoked") == (lifetime == "until_revoked")


def start_renewal(page, confirmed=False):
    setup_owner(page, active=True)
    page.locator(".cc-wx-account-step > summary").click()
    page.locator('[name="renew_account_id"]').select_option("accountA")
    page.locator('[name="qr_storage"]').check()
    page.locator('[name="qr_terms"]').check()
    if confirmed:
        page.evaluate("nextState='confirmed'")
    page.evaluate("clickAction('dot-channel-qr-start')")


def test_reconnect_has_separate_consent_and_uses_scanner_not_old_peer(qr_page):
    page, _ = qr_page
    start_renewal(page, confirmed=True)
    args = page.evaluate("writes[0].args")
    assert args["renew_account_id"] == "accountA" and args["expected_credential_version"] == 4
    page.locator('[name="qr_scanner"]').check()
    page.evaluate("clickAction('dot-channel-qr-accept')")
    assert page.evaluate("writes.length") == 1
    page.locator('[name="qr_replace_existing"]').check()
    page.evaluate("clickAction('dot-channel-qr-accept')")
    saved = page.evaluate("writes[1].args")
    assert saved["confirm_replace_existing"] is True
    assert saved["confirm_scanner_id"] == "scanner-123"
    assert saved["confirm_scanner_id"] != "synthetic-owner-peer"
    assert not any(w["operation"] == "weixin-owner-authority" for w in page.evaluate("writes"))


def test_cancel_reconnect_does_not_replace_credentials(qr_page):
    page, _ = qr_page
    start_renewal(page)
    page.evaluate("clickAction('dot-channel-qr-cancel')")
    assert [w["operation"] for w in page.evaluate("writes")] == ["weixin-qr-start", "weixin-qr-cancel"]


def test_pending_revalidation_preserves_scope_without_reapproving(qr_page):
    page, _ = qr_page
    setup_owner(page, active=True)
    page.evaluate("""() => {
      const binding=data.weixin.bindings[0];
      binding.state='pairing';binding.pairing_until=Date.now()/1000+900;
      binding.pairing_challenge='SYNTHETIC-REVALIDATION-ONLY';
      binding.owner_authorization.state='pending_revalidation';
      data.weixin.owner_task_options[0].execution_targets=['not-the-approved-target'];
    }""")
    page.evaluate("clickAction('dot-channel-open')")
    panel = page.locator(".cc-wx-owner-authority")
    expect(panel).to_contain_text("原批准范围保留")
    expect(panel.locator("dt", has_text="执行目标").locator("xpath=following-sibling::dd[1]")).to_have_text("agent")
    expect(panel).not_to_contain_text("not-the-approved-target")
    assert panel.locator('[data-cc-action="dot-channel-owner-approve"]').count() == 0
    expect(panel.locator('[data-cc-action="dot-channel-owner-revoke"]')).to_be_visible()
    assert page.evaluate("writes.length") == 0
    page.evaluate("""() => {
      Object.assign(data.weixin.bindings[0], {state:'awaiting_owner',version:8,
        pending_peer_id:'synthetic-owner-peer'});
    }""")
    page.evaluate("clickAction('dot-channel-open')")
    expect(page.locator('[data-channel-form="activate"]')).to_contain_text("确认恢复原批准范围")
    page.locator('[name="confirm_peer"]').check()
    page.evaluate("clickAction('dot-channel-activate')")
    assert page.evaluate("writes[0].operation") == "weixin-control"
    assert page.evaluate("writes[0].args.confirm_peer_id") == "synthetic-owner-peer"
    assert page.evaluate("writes[0].args.expected_version") == 8


def test_successful_reconnect_guides_to_existing_binding_not_new_one(qr_page):
    page, _ = qr_page
    start_renewal(page, confirmed=True)
    expect(page.locator(".cc-wx-qr")).to_contain_text("重新验证同一联系人后恢复原批准范围")
    page.locator('[name="qr_scanner"]').check()
    page.locator('[name="qr_replace_existing"]').check()
    page.evaluate("""() => {
      const binding=data.weixin.bindings[0];
      binding.state='pairing';binding.pairing_until=Date.now()/1000+900;
      binding.pairing_challenge='SYNTHETIC-REVALIDATION-ONLY';
      binding.owner_authorization.state='pending_revalidation';
    }""")
    page.evaluate("clickAction('dot-channel-qr-accept')")
    expect(page.locator(".cc-wx-binding-guide")).to_contain_text("上方原绑定")
    assert not page.locator(".cc-wx-bind").evaluate("(node)=>node.open")
    expect(page.locator('[aria-label="微信配对码"]')).to_be_focused()
    assert page.locator('[data-cc-action="dot-channel-owner-approve"]').count() == 0


@pytest.mark.parametrize("binding_state", ["pairing", "awaiting_owner", "paused"])
def test_refresh_pairing_keeps_original_authority_and_cannot_resume(qr_page, binding_state):
    page, _ = qr_page
    setup_owner(page, active=True)
    page.evaluate("""stateName => {
      const binding=data.weixin.bindings[0];
      Object.assign(binding, {state:stateName,revalidation_required:true,
        pairing_until:Date.now()/1000-1,pending_peer_id:'synthetic-owner-peer'});
      binding.owner_authorization.state='pending_revalidation';
    }""", binding_state)
    page.evaluate("clickAction('dot-channel-open')")
    assert page.locator('[data-cc-action="dot-channel-resume"]').count() == 0
    assert page.locator('[data-cc-action="dot-channel-owner-approve"]').count() == 0
    page.evaluate("confirmResult=false; clickAction('dot-channel-refresh-pairing')")
    assert page.evaluate("writes.length") == 0
    page.evaluate("confirmResult=true; clickAction('dot-channel-refresh-pairing')")
    args = page.evaluate("writes[0].args")
    assert page.evaluate("writes[0].operation") == "weixin-control"
    assert args["binding_id"] == "bindingA" and args["expected_version"] == 7
    assert args["action"] == "refresh_pairing"
    assert "原批准范围保留" in page.evaluate("confirmations.at(-1)")


def test_missing_reply_authorization_explains_explicit_rebinding(qr_page):
    page, _ = qr_page
    setup_owner(page)
    page.evaluate("""() => {
      Object.assign(data.weixin.owner_task_options[0], {available:false,
        reason_code:'CHANNEL_OWNER_REPLIES_REQUIRED'});
    }""")
    page.evaluate("clickAction('dot-channel-open')")
    panel = page.locator(".cc-wx-owner-authority")
    expect(panel).to_contain_text("当前绑定未批准回复。需建立允许回复的绑定后才能开启任务。")
    assert panel.locator('[data-cc-action="dot-channel-owner-approve"]').count() == 0
    assert page.evaluate("writes.length") == 0


@pytest.mark.parametrize("budget_mode,window_count,expected", [
    ("rolling_24h", 2, "滚动24小时已用 2 / 100；累计 101"),
    ("rolling_24h", 0, "滚动24小时已用 0 / 100；累计 101"),
    ("lifetime", 2, "已用 101 / 100"),
    (None, 2, "已用 101 / 100"),
])
def test_delegation_budget_labels_preserve_window_and_total(qr_page, budget_mode, window_count, expected):
    from pathlib import Path

    page, _ = qr_page
    page.add_script_tag(path=str(Path(__file__).resolve().parents[1] / "web/collaboration-delegation.js"))
    page.evaluate("""({budget_mode,window_count}) => {
      state.snapshot.capabilities.direct_delegation=true;
      state.delegationPolicies=[{id:'policyA',slot_id:'slotA',version:5,
        purpose:'Synthetic fixture',capabilities:['read'],execution_targets:['project_agent'],
        effective_active:true,expires_at:Date.now()/1000+86400,
        usage:{delegations:101,total_delegations:101,window_delegations:window_count,
          max_delegations:100,budget_mode}}];
      const delegation=CodePierCollaborationDelegation.create({state,E,
        field:()=>'',button:()=>'',projectLabel:()=> '合成项目',when:()=> '未来日期'});
      document.querySelector('#drawer').innerHTML=delegation.policyCard({
        id:'slotA',label:'合成 dot',state:'registered'});
    }""", {"budget_mode": budget_mode, "window_count": window_count})
    expect(page.locator(".cc-connection-settings")).to_contain_text(expected)
    if budget_mode == "rolling_24h":
        expect(page.locator(".cc-connection-settings")).not_to_contain_text("已用 101 / 100")
    else:
        expect(page.locator(".cc-connection-settings")).not_to_contain_text("滚动24小时")
    assert page.evaluate("writes.length") == 0
