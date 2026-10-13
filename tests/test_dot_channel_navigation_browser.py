"""Synthetic channel responses in real browsers; no provider, account or outbound traffic."""

import pytest
from playwright.sync_api import expect

from tests.test_dot_weixin_owner_authority_browser import setup_owner
from tests.test_dot_weixin_qr_browser import start, writes
from tests.weixin_browser_support import full_styles, qr_page as qr_page

def act(page, suffix):
    return page.evaluate("(suffix) => clickAction('dot-channel-' + suffix)", suffix)


def overview(page):
    if page.locator('[data-channel-view="connection-scope"]').count():
        act(page, "view-advanced")
    act(page, "view-overview")
    expect(page.locator("[data-channel-view]")).to_have_attribute("data-channel-view", "overview")


def connection_scope(page):
    act(page, "view-advanced")
    expect(page.locator("[data-channel-form=scope]")).to_have_count(0)
    expect(page.locator("[data-cc-action=manage-room-permissions]")).to_be_visible()
    act(page, "view-connection-scope")
    expect(page.locator("[data-channel-view]")).to_have_attribute("data-channel-view", "connection-scope")
    expect(page.locator(".cc-connection-warning").first).to_contain_text("所有使用同一凭据的会话都会受影响")


def no_overflow(page):
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert page.locator(".cc-drawer").evaluate("(n) => n.scrollWidth <= n.clientWidth")
    assert page.locator("#drawer").evaluate("(n) => n.scrollWidth <= n.clientWidth")
    assert page.locator(".cc-channel-pane").evaluate("(n) => n.scrollWidth <= n.clientWidth")


@pytest.mark.parametrize("width", [360, 390, 1440])
def test_overview_single_view_safe_drafts_and_full_css_layout(qr_page, width, tmp_path):
    page, engine = qr_page
    page.set_viewport_size({"width": width, "height": 1000})
    full_styles(page)
    page.evaluate("data.scopes.eligible_connections=[{id:'grantA',label:'合成项目的现有受限连接',scope_version:3}]")
    act(page, "open")
    overview(page)
    expect(page.locator("[data-channel-form]")).to_have_count(0)
    expect(page.locator(".cc-channel-pane")).to_have_count(1)
    assert page.evaluate("writes.length") == 0
    no_overflow(page)
    page.screenshot(path=str(tmp_path / f"connections-overview-{engine}-{width}.png"), full_page=True)

    act(page, "view-weixin")
    expect(page.locator("[data-channel-view]")).to_have_attribute("data-channel-view", "weixin")
    expect(page.locator("[data-channel-form=tg-account]")).to_have_count(0)
    page.locator(".cc-connection-maintenance > summary").click()
    page.get_by_text("高级：登记已有授权机器人", exact=True).click()
    account = page.locator("[data-channel-form=account]")
    account.locator("[name=label]").fill("微信安全草稿")
    account.locator("[name=bot_id]").fill("synthetic-bot")
    account.locator("[name=token]").fill("SYNTHETIC-SECRET-DO-NOT-RETAIN")
    account.locator("[name=confirm_credentials]").check()
    page.locator("[name=qr_storage]").check()
    page.locator("[name=qr_terms]").check()
    no_overflow(page)
    overview(page)
    act(page, "view-weixin")
    page.locator(".cc-connection-maintenance > summary").click()
    page.get_by_text("高级：登记已有授权机器人", exact=True).click()
    expect(account.locator("[name=label]")).to_have_value("微信安全草稿")
    expect(account.locator("[name=bot_id]")).to_have_value("synthetic-bot")
    expect(account.locator("[name=token]")).to_have_value("")
    expect(account.locator("[name=confirm_credentials]")).not_to_be_checked()
    expect(page.locator("[name=qr_storage]")).not_to_be_checked()
    expect(page.locator("[name=qr_terms]")).not_to_be_checked()
    page.screenshot(path=str(tmp_path / f"connections-weixin-{engine}-{width}.png"), full_page=True)

    overview(page)
    act(page, "view-telegram")
    expect(page.locator("[data-channel-form=account]")).to_have_count(0)
    telegram = page.locator("[data-channel-form=tg-account]")
    telegram.locator("[name=label]").fill("Telegram安全草稿")
    telegram.locator("[name=bot_id]").fill("123456")
    telegram.locator("[name=token]").fill("SYNTHETIC-TELEGRAM-SECRET")
    telegram.locator("[name=credentials]").check()
    no_overflow(page)
    overview(page)
    act(page, "view-telegram")
    expect(telegram.locator("[name=label]")).to_have_value("Telegram安全草稿")
    expect(telegram.locator("[name=token]")).to_have_value("")
    expect(telegram.locator("[name=credentials]")).not_to_be_checked()
    no_overflow(page)
    page.screenshot(path=str(tmp_path / f"connections-telegram-{engine}-{width}.png"), full_page=True)
    overview(page)
    act(page, "view-advanced")
    expect(page.locator(".cc-advanced-settings")).to_contain_text("所有使用同一凭据的会话都会受影响")
    expect(page.locator("[data-channel-form=account]")).to_have_count(0)
    no_overflow(page)
    page.screenshot(path=str(tmp_path / f"connections-advanced-{engine}-{width}.png"), full_page=True)
    assert page.evaluate("writes.length") == 0


def test_navigation_cancel_retains_qr_then_leaving_cancels_exact_request(qr_page):
    page, _ = qr_page
    start(page)
    started = writes(page, "weixin-qr-start")[0]
    page.evaluate("confirmResult=false")
    act(page, "view-overview")
    expect(page.locator("[data-channel-view]")).to_have_attribute("data-channel-view", "weixin")
    expect(page.locator(".cc-wx-qr-image")).to_be_visible()
    assert len(writes(page, "weixin-qr-cancel")) == 0
    assert "离开" in page.evaluate("confirmations.at(-1)")
    page.evaluate("confirmResult=true")
    overview(page)
    cancelled = writes(page, "weixin-qr-cancel")
    assert len(cancelled) == 1
    assert cancelled[0]["args"]["enrollment_id"] == "enrollment-1"
    assert cancelled[0]["args"]["expected_version"] == 1
    for key in ("project", "environment_id", "conversation_id"):
        assert cancelled[0]["args"][key] == started["args"][key]
    before = page.evaluate("statuses.length")
    page.clock.run_for(6000)
    assert page.evaluate("statuses.length") == before
    act(page, "view-weixin")
    expect(page.locator("[name=qr_storage]")).not_to_be_checked()
    expect(page.locator("[name=qr_terms]")).not_to_be_checked()
    assert len(writes(page, "weixin-qr-start")) == 1
    assert not writes(page, "weixin-qr-accept")


def test_navigation_during_duplicate_start_discards_late_response(qr_page):
    page, _ = qr_page
    page.evaluate("pauseStart=true")
    page.locator("[name=qr_storage]").check()
    page.locator("[name=qr_terms]").check()
    page.evaluate("""() => {
      window.pending=clickAction('dot-channel-qr-start');
      window.duplicate=clickAction('dot-channel-qr-start').catch(()=>({local:true}));
    }""")
    assert len(writes(page, "weixin-qr-start")) == 1
    overview(page)
    page.evaluate("releaseStart(); pending")
    expect(page.locator("[data-channel-view]")).to_have_attribute("data-channel-view", "overview")
    expect(page.locator(".cc-wx-qr-image")).to_have_count(0)
    assert len(writes(page, "weixin-qr-cancel")) == 1
    assert not writes(page, "weixin-qr-accept")
    page.clock.run_for(6000)
    assert page.evaluate("statuses.length") == 0


def test_navigation_rejects_replaced_authorization_and_never_activates(qr_page):
    page, _ = qr_page
    setup_owner(page)
    page.evaluate("state.snapshot.can_manage=false")
    refused = page.evaluate("clickAction('dot-channel-view-overview').then(()=>false,()=>true)")
    assert refused
    assert page.evaluate("writes.length") == 0


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("mode,maximum", [
    ("lifetime", 100), ("lifetime", 17), ("rolling_24h", 29), ("unlimited", None)
])
def test_budget_modes_exact_payload_each_missing_consent_blocks(qr_page, active, mode, maximum):
    page, _ = qr_page
    setup_owner(page, active=active)
    page.evaluate("""() => {
      data.weixin.owner_task_options[0].available_budget_modes=['lifetime','rolling_24h','unlimited'];
    }""")
    act(page, "open")
    panel = page.locator(".cc-wx-owner-authority")
    if active:
        panel.get_by_text("修改任务数量限制", exact=True).click()
    else:
        panel.locator("> summary").click()
    form = panel.locator("[data-channel-form=owner-authority]")
    form.locator("[name=budget_mode]").select_option(mode)
    if maximum is not None:
        form.locator("[name=max_delegations]").fill(str(maximum))
    # Initial purposes share one explicit UI acknowledgement, but the exact
    # backend fields below remain separate. Budget changes retain their own
    # additional acknowledgement and never silently renew owner authority.
    required = [] if active else ["confirm_owner_agreement"]
    if not active:
        for purpose in ("本人身份", "未来任务与回传", "持续与撤销"):
            expect(form.locator(".cc-consent-purposes")).to_contain_text(purpose)
    if mode == "rolling_24h":
        required.append("confirm_daily_budget")
    if mode == "unlimited":
        required.append("confirm_unlimited_budget")
        expect(form.locator("[name=max_delegations]")).to_be_disabled()
    elif maximum != 100:
        required.append("confirm_task_count_budget")
    if active:
        required.append("confirm_budget_change")
    for name in required:
        form.locator(f"[name={name}]").check()
    for missing in required:
        form.locator(f"[name={missing}]").uncheck()
        page.evaluate("clickAction('dot-channel-owner-approve').catch(()=>({local:true}))")
        assert page.evaluate("writes.length") == 0, missing
        form.locator(f"[name={missing}]").check()
    results = page.evaluate("Promise.all([clickAction('dot-channel-owner-approve'),clickAction('dot-channel-owner-approve')])")
    if active and mode == "lifetime" and maximum == 100:
        assert page.evaluate("writes.length") == 0
        assert all(result["local"] for result in results)
        assert all("已保留原授权与正在进行的任务" in result["message"] for result in results)
        return
    assert page.evaluate("writes.length") == 1
    expected = {
        "binding_id": "bindingA", "expected_binding_version": 7,
        "expected_authority_version": 3 if active else 0, "action": "approve",
        "confirm_peer_id": "synthetic-owner-peer", "expected_scope_digest": "scope-v1",
        "confirm_owner_tasks": True, "confirm_until_revoked": True, "budget_mode": mode,
        "project": "projectA", "environment_id": "test", "conversation_id": "conversationA",
    }
    if mode == "rolling_24h":
        expected["confirm_daily_budget"] = True
    if mode == "unlimited":
        expected["confirm_unlimited_budget"] = True
    elif maximum != 100:
        expected.update(max_delegations=maximum, confirm_task_count_budget=True)
    if active:
        expected["confirm_budget_change"] = True
    assert page.evaluate("writes[0]") == {"operation": "weixin-owner-authority", "args": expected}


def test_changing_budget_clears_independent_consents_and_old_modes_are_bounded(qr_page):
    page, _ = qr_page
    setup_owner(page)
    page.locator(".cc-wx-owner-authority > summary").click()
    form = page.locator("[data-channel-form=owner-authority]")
    expect(form.locator("[name=budget_mode] option[value=unlimited]")).to_have_count(0)
    form.locator("[name=confirm_owner_agreement]").check()
    form.locator("[name=budget_mode]").select_option("rolling_24h")
    assert form.locator("input:checked").count() == 0
    for name in ("confirm_owner_agreement", "confirm_daily_budget"):
        form.locator(f"[name={name}]").check()
    form.locator("[name=max_delegations]").fill("7")
    assert form.locator("input:checked").count() == 0
    page.evaluate("clickAction('dot-channel-owner-approve').catch(()=>({local:true}))")
    assert page.evaluate("writes.length") == 0


def test_saved_advanced_scope_prefills_and_unchanged_submit_is_read_only(qr_page):
    page, _ = qr_page
    page.evaluate("""() => {
      data.scopes.eligible_connections=[{id:'grantA',label:'合成原连接',scope_version:3}];
      data.scopes.items=[{grant_id:'grantA',version:3,state:'active',expires_at:Date.now()/1000+48*3600,
        manifest:{project_rules:[{project_id:'projectA',capabilities:['read','write','execute'],
          execution_targets:['agent']}],execution_mode:'account_permissions_acknowledged'}}];
    }""")
    act(page, "open")
    overview(page)
    connection_scope(page)
    page.get_by_text("修改已有连接的限制", exact=True).click()
    form = page.locator("[data-channel-form=scope]")
    expect(form.locator("[name=grant_id]")).to_have_value("grantA")
    expect(form.locator("[name=hours]")).to_have_value("48")
    expect(form.locator("[name=write]")).to_be_checked()
    expect(form.locator("[name=execute]")).to_be_checked()
    expect(form.locator("[name=confirm_scope]")).not_to_be_checked()
    expect(form.locator("[name=unsandboxed]")).not_to_be_checked()
    form.locator("[name=confirm_scope]").check()
    form.locator("[name=unsandboxed]").check()
    act(page, "scope-save")
    assert page.evaluate("writes.length") == 0
    form.locator("[name=hours]").fill("72")
    form.locator("[name=confirm_scope]").uncheck()
    page.evaluate("clickAction('dot-channel-scope-save').catch(()=>({local:true}))")
    assert page.evaluate("writes.length") == 0
    form.locator("[name=confirm_scope]").check()
    form.locator("[name=unsandboxed]").uncheck()
    page.evaluate("clickAction('dot-channel-scope-save').catch(()=>({local:true}))")
    assert page.evaluate("writes.length") == 0
    form.locator("[name=unsandboxed]").check()
    act(page, "scope-save")
    assert page.evaluate("writes.length") == 1
    sent = page.evaluate("writes[0]")
    assert sent["operation"] == "room-scope"
    assert sent["args"]["expected_version"] == 3
    assert sent["args"]["project_rules"] == [{
        "project_id": "projectA", "capabilities": ["read", "write", "execute"],
        "execution_targets": ["agent"],
    }]
    assert sent["args"]["acknowledge_unsandboxed_exec"] is True
    assert sent["args"]["expires_seconds"] == 72 * 3600


@pytest.mark.parametrize("width", [360, 390, 1440])
def test_long_owner_confirmation_dialog_can_scroll_to_submit(qr_page, width, tmp_path):
    page, engine = qr_page
    page.set_viewport_size({"width": width, "height": 740})
    setup_owner(page)
    full_styles(page)
    panel = page.locator(".cc-wx-owner-authority")
    panel.locator("> summary").click()
    submit = panel.locator("[data-cc-action=dot-channel-owner-approve]")
    submit.scroll_into_view_if_needed()
    expect(submit).to_be_visible()
    scroller = page.locator("#drawer.cc-drawer-body")
    assert scroller.evaluate("(n) => n.scrollHeight > n.clientHeight")
    assert scroller.evaluate("(n) => n.scrollTop") > 0
    button_box, body_box = submit.bounding_box(), scroller.bounding_box()
    assert button_box["y"] >= body_box["y"] - 1
    assert button_box["y"] + button_box["height"] <= body_box["y"] + body_box["height"] + 1
    no_overflow(page)
    assert page.evaluate("writes.length") == 0
    page.screenshot(path=str(tmp_path / f"owner-scroll-submit-{engine}-{width}.png"), full_page=True)


def test_closing_then_reopening_discards_channel_drafts_and_consent(qr_page):
    page, _ = qr_page
    page.locator(".cc-connection-maintenance > summary").click()
    page.get_by_text("高级：登记已有授权机器人", exact=True).click()
    account = page.locator("[data-channel-form=account]")
    account.locator("[name=label]").fill("关闭后不保留")
    account.locator("[name=token]").fill("SYNTHETIC-SECRET")
    account.locator("[name=confirm_credentials]").check()
    page.evaluate("channels.clear(); channels.act('dot-channel-open',document.querySelector('#open'))")
    expect(page.locator("[data-channel-view]")).to_have_attribute("data-channel-view", "overview")
    act(page, "view-weixin")
    page.locator(".cc-connection-maintenance > summary").click()
    page.get_by_text("高级：登记已有授权机器人", exact=True).click()
    expect(account.locator("[name=label]")).to_have_value("")
    expect(account.locator("[name=token]")).to_have_value("")
    expect(account.locator("[name=confirm_credentials]")).not_to_be_checked()
    assert page.evaluate("writes.length") == 0


def scope_form(page, saved=False):
    page.evaluate("""(saved) => {
      data.scopes.eligible_connections=[{id:'grantA',label:'合成测试连接',scope_version:saved ? 3 : 0}];
      data.scopes.items=saved ? [{grant_id:'grantA',version:3,state:'active',expires_at:0,
        manifest:{lifetime_mode:'until_revoked',project_rules:[
          {project_id:'projectA',capabilities:['read'],execution_targets:['agent']}],
          execution_mode:'file_tools_only'}}] : [];
    }""", saved)
    act(page, "open")
    overview(page)
    connection_scope(page)
    if saved:
        page.get_by_text("修改已有连接的限制", exact=True).click()
    return page.locator("[data-channel-form=scope]")


@pytest.mark.parametrize("selection", ["select", "zero"])
def test_scope_long_term_is_explicit_and_resets_independent_confirmations(qr_page, selection, tmp_path):
    page, engine = qr_page
    page.set_viewport_size({"width": 390, "height": 1000})
    full_styles(page)
    form = scope_form(page)
    expect(form.locator("[name=hours]")).to_have_value("24")
    expect(form.locator("[name=scope_lifetime]")).to_have_value("bounded")
    form.locator("[name=confirm_scope]").check()
    form.locator("[name=unsandboxed]").check()
    if selection == "select":
        form.locator("[name=scope_lifetime]").select_option("until_revoked")
    else:
        form.locator("[name=hours]").fill("0")
    expect(form.locator("[name=scope_lifetime]")).to_have_value("until_revoked")
    expect(form.locator("[name=hours]")).to_have_value("0")
    expect(form.locator("[name=confirm_scope]")).not_to_be_checked()
    expect(form.locator("[name=unsandboxed]")).not_to_be_checked()
    act(page, "scope-save")
    assert page.evaluate("writes.length") == 0
    form.locator("[name=confirm_scope]").check()
    no_overflow(page)
    page.screenshot(path=str(tmp_path / f"scope-long-term-{selection}-{engine}.png"), full_page=True)
    act(page, "scope-save")
    sent = page.evaluate("writes[0]")
    assert sent["args"]["expires_seconds"] == 0
    assert sent["args"]["execution_mode"] == "file_tools_only"
    assert sent["args"]["acknowledge_unsandboxed_exec"] is False
    assert sent["args"]["confirm_connection_wide_ceiling"] is True


def test_saved_long_term_scope_reopens_as_zero_and_unchanged_does_not_write(qr_page):
    page, _ = qr_page
    form = scope_form(page, saved=True)
    expect(form.locator("[name=hours]")).to_have_value("0")
    expect(form.locator("[name=scope_lifetime]")).to_have_value("until_revoked")
    expect(page.locator(".cc-channel-binding").filter(has_text="连接 grantA")).to_contain_text("长期（持续至撤销）")
    form.locator("[name=confirm_scope]").check()
    act(page, "scope-save")
    assert page.evaluate("writes.length") == 0
    overview(page)
    connection_scope(page)
    page.get_by_text("修改已有连接的限制", exact=True).click()
    expect(form.locator("[name=hours]")).to_have_value("0")
    expect(form.locator("[name=confirm_scope]")).not_to_be_checked()
    form.locator("[name=scope_lifetime]").select_option("bounded")
    expect(form.locator("[name=hours]")).to_have_value("24")
    form.locator("[name=hours]").fill("72")
    form.locator("[name=confirm_scope]").check()
    act(page, "scope-save")
    assert page.evaluate("writes[0].args.expires_seconds") == 72 * 3600


@pytest.mark.parametrize("hours", ["", "-1", "0.5", "721"])
def test_invalid_scope_hours_never_send_or_become_implicit_long_term(qr_page, hours):
    page, _ = qr_page
    form = scope_form(page)
    form.locator("[name=hours]").fill(hours)
    form.locator("[name=confirm_scope]").check()
    page.evaluate("clickAction('dot-channel-scope-save').catch(()=>({local:true}))")
    assert page.evaluate("writes.length") == 0
    expect(form.locator("[name=scope_lifetime]")).to_have_value("bounded")


def test_advanced_project_bulk_preserves_hidden_selections_and_capabilities(qr_page):
    page, _ = qr_page
    page.evaluate("""() => {
      S.projects=[{id:'projectA',alias:'PrimaryFolder'},{id:'projectB',alias:'SecondaryFolder'},{id:'projectC',alias:'TertiaryFolder'}];
      data.scopes.eligible_connections=[{id:'grantA',label:'合成原连接',scope_version:3}];
      data.scopes.items=[{grant_id:'grantA',version:3,expires_at:0,manifest:{
        lifetime_mode:'until_revoked',project_rules:[
          {project_id:'projectA',capabilities:['read','write','execute'],execution_targets:['agent']},
          {project_id:'projectC',capabilities:['read','write'],execution_targets:['agent']}]} }];
    }""")
    act(page, 'open')
    overview(page)
    connection_scope(page)
    open_scope_form(page)
    form = page.locator('[data-channel-form=scope]')
    form.locator('[name=grant_id]').select_option('grantA')
    expect(form.locator('[name=write]:visible')).to_have_count(0)
    expect(form.locator('[name=execute]:visible')).to_have_count(0)
    baseline = form.locator('[name=write], [name=execute]').evaluate_all('(nodes) => nodes.map(n => n.checked)')
    form.locator('[data-project-search]').fill('SecondaryFolder')
    act(page, 'project-all')
    expect(form.locator('[data-project-id=projectB] [name=project_selected]')).to_be_checked()
    expect(form.locator('[data-project-id=projectC] [name=project_selected]')).to_be_checked()
    act(page, 'project-none')
    expect(form.locator('[data-project-id=projectB] [name=project_selected]')).not_to_be_checked()
    expect(form.locator('[data-project-id=projectC] [name=project_selected]')).to_be_checked()
    assert form.locator('[name=write], [name=execute]').evaluate_all('(nodes) => nodes.map(n => n.checked)') == baseline
    expect(form.locator('[name=confirm_scope]')).not_to_be_checked()
    expect(form.locator('[name=unsandboxed]')).not_to_be_checked()
    assert page.evaluate('writes.length') == 0


@pytest.mark.parametrize('old_target', [None, 'vps:legacy', {'unknown': 'record'}])
def test_unknown_saved_target_is_visible_and_never_replaced_with_agent(qr_page, old_target):
    page, _ = qr_page
    page.evaluate("""(target) => {
      data.scopes.eligible_connections=[{id:'grantA',label:'合成原连接',scope_version:3}];
      data.scopes.items=[{grant_id:'grantA',version:3,expires_at:0,manifest:{
        lifetime_mode:'until_revoked',project_rules:[
          {project_id:'projectA',capabilities:['read'],execution_targets:target}]} }];
    }""", old_target)
    act(page, 'open')
    overview(page)
    connection_scope(page)
    open_scope_form(page)
    form = page.locator('[data-channel-form=scope]')
    form.locator('[name=grant_id]').select_option('grantA')
    form.locator('.cc-project-capabilities > summary').click()
    expect(form.locator('[data-project-target-summary]')).to_contain_text('原执行位置缺失')
    form.locator('[name=hours]').fill('2')
    form.locator('[name=confirm_scope]').check()
    message = page.evaluate("clickAction('dot-channel-scope-save').then(()=>'',e=>e.message)")
    assert '不会自动改为 Agent' in message
    assert page.evaluate('writes.length') == 0


def open_scope_form(page):
    form = page.locator('[data-channel-form="scope"]')
    if not form.is_visible():
        form.locator('..').locator(':scope > summary').click()
    return form


def test_connection_scope_revoke_requires_specific_view_confirmation_and_version(qr_page):
    page, _ = qr_page
    scope_form(page, saved=True)
    page.evaluate("confirmResult=false")
    act(page, "scope-revoke")
    assert page.evaluate("writes.length") == 0
    assert "所有使用这条凭据的会话" in page.evaluate("confirmations.at(-1)")
    page.evaluate("window.oldRevoke=document.querySelector('[data-cc-action=dot-channel-scope-revoke]')")
    act(page, "view-advanced")
    refused = page.evaluate("channels.act('dot-channel-scope-revoke',oldRevoke).then(()=>'',e=>e.message)")
    assert "请先打开整条连接限制" in refused
    assert page.evaluate("writes.length") == 0
    act(page, "view-connection-scope")
    page.evaluate("confirmResult=true")
    act(page, "scope-revoke")
    sent = page.evaluate("writes")
    assert len(sent) == 1
    assert sent[0]["operation"] == "room-scope-control"
    assert sent[0]["args"] == {
        "grant_id": "grantA", "expected_version": 3, "action": "revoke",
        "project": "projectA", "environment_id": "test",
    }


@pytest.mark.parametrize("change", [
    "state.generation++",
    "S.session={id:'replacement-session'}",
    "S.space_id='replacement-space'",
    "state.snapshot.can_manage=false",
    "state.snapshot.conversation.restored_from_archive=true",
    "state.snapshot.conversation.archived_at=1",
])
def test_connection_scope_stale_or_history_context_cannot_save_or_revoke(qr_page, change):
    page, _ = qr_page
    form = scope_form(page, saved=True)
    form.locator("[name=hours]").fill("2")
    form.locator("[name=confirm_scope]").check()
    page.evaluate(change)
    for suffix in ("scope-save", "scope-revoke"):
        refused = page.evaluate("(suffix)=>clickAction('dot-channel-'+suffix).then(()=>false,()=>true)", suffix)
        assert refused
    assert page.evaluate("writes.length") == 0
    assert page.evaluate("confirmations.length") == 0


@pytest.mark.parametrize("targets", [["vps:original"], ["agent", "vps:original"]])
def test_connection_scope_explicit_save_preserves_original_vps_targets(qr_page, targets):
    page, _ = qr_page
    page.evaluate("""(targets) => {
      data.scopes.eligible_connections=[{id:'grantA',label:'合成原连接',scope_version:3}];
      data.scopes.items=[{grant_id:'grantA',version:3,state:'active',expires_at:0,
        manifest:{lifetime_mode:'until_revoked',project_rules:[
          {project_id:'projectA',capabilities:['read','write','execute'],execution_targets:targets}],
          execution_mode:'account_permissions_acknowledged'}}];
    }""", targets)
    act(page, "open")
    overview(page)
    connection_scope(page)
    form = open_scope_form(page)
    form.locator(".cc-project-capabilities > summary").click()
    expect(form.locator("[data-project-target-summary]")).to_contain_text("vps:original")
    assert page.evaluate("writes.length") == 0
    form.locator("[name=hours]").fill("2")
    form.locator("[name=confirm_scope]").check()
    form.locator("[name=unsandboxed]").check()
    act(page, "scope-save")
    sent = page.evaluate("writes")
    assert len(sent) == 1 and sent[0]["operation"] == "room-scope"
    assert sent[0]["args"]["project_rules"] == [{
        "project_id": "projectA", "capabilities": ["read", "write", "execute"],
        "execution_targets": targets,
    }]
    assert sent[0]["args"]["expected_version"] == 3
    assert sent[0]["args"]["expires_seconds"] == 7200
