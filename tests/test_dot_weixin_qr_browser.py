"""Synthetic owner UI exercised in real Chromium/WebKit; never calls Tencent."""
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.weixin_browser_support import qr_page as qr_page


def action(page, name):
    return page.evaluate("(name)=>clickAction('dot-channel-'+name)", name)


def start(page):
    page.locator('[name="qr_storage"]').check()
    page.locator('[name="qr_terms"]').check()
    action(page, "qr-start")


def writes(page, name):
    return page.evaluate("(name)=>writes.filter(x=>x.operation===name)", name)


@pytest.mark.parametrize("width", [1440, 390])
def test_qr_consent_scan_verify_exact_owner_then_manual_room_binding(qr_page, width, tmp_path):
    page, engine = qr_page
    page.set_viewport_size({"width": width, "height": 1000})
    assert page.evaluate("writes.length") == 0
    assert page.locator('[data-channel-form="account"]').is_hidden()
    action(page, "qr-start")
    assert not writes(page, "weixin-qr-start")
    page.locator('[name="qr_storage"]').check()
    action(page, "qr-start")
    assert not writes(page, "weixin-qr-start")
    page.locator('[name="qr_terms"]').check()
    action(page, "qr-start")
    sent = writes(page, "weixin-qr-start")[0]["args"]
    assert sent["confirm_credential_storage"] and sent["confirm_provider_terms"]
    assert sent["project"] == "projectA" and sent["environment_id"] == "test"
    assert sent["conversation_id"] == "conversationA"
    assert page.locator(".cc-wx-qr-image").get_attribute("src").startswith("data:image/svg+xml;base64,")
    evidence = Path(os.environ.get("CODEPIER_TEST_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(evidence / f"qr-wait-{engine}-{width}.png"), full_page=True)
    assert "attacker.invalid" not in page.content()
    assert "MUST-NOT-RETAIN" not in page.content()
    page.evaluate("statusResponses.push({state:'scaned',version:2})")
    page.clock.fast_forward(2100)
    expect(page.locator(".cc-wx-qr")).to_contain_text("已扫码")
    page.evaluate("statusResponses.push({state:'need_verifycode',version:3})")
    action(page, "qr-status")
    page.locator('[name="verify_code"]').fill("12345678")
    page.evaluate("statusResponses.push({state:'confirmed',version:4,scanner_id:'scanner-123'})")
    action(page, "qr-verify")
    assert page.evaluate("statuses.at(-1).args.verify_code") == "12345678"
    assert page.evaluate("statuses.at(-1).retrySafe") is False
    assert not writes(page, "weixin-qr-status")
    assert "12345678" not in page.content()
    action(page, "qr-accept")
    assert not writes(page, "weixin-qr-accept")
    page.locator('[name="qr_scanner"]').check()
    page.locator('[data-channel-form="qr-accept"] [name="label"]').fill("本人微信")
    action(page, "qr-accept")
    accepted = writes(page, "weixin-qr-accept")[0]["args"]
    assert accepted["confirm_scanner_id"] == "scanner-123"
    assert accepted["expected_version"] == 4
    assert accepted["label"] == "本人微信"
    assert accepted["confirm_credential_storage"] and accepted["confirm_provider_terms"]
    assert page.locator('.cc-wx-bind [name="account_id"]').input_value() == "accountA"
    assert page.locator(".cc-wx-bind").get_attribute("open") is not None
    assert not page.locator('.cc-wx-bind [name="confirm_ingress"]').is_checked()
    assert not page.locator('.cc-wx-bind [name="allow_replies"]').is_checked()
    assert not page.locator('.cc-wx-bind [name="allow_media"]').is_checked()
    assert not writes(page, "weixin-bind")
    assert not writes(page, "room-scope")
    assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
    page.screenshot(path=str(evidence / f"qr-accepted-{engine}-{width}.png"), full_page=True)


def test_qr_unknown_start_recovers_same_key_refresh_cancels_first(qr_page):
    page, _ = qr_page
    page.evaluate("startFail=true")
    start(page)
    expect(page.locator(".cc-wx-qr")).to_contain_text("申请结果暂未确认")
    assert not page.locator('[data-cc-action="dot-channel-qr-start"]').count()
    page.evaluate("startFail=false")
    action(page, "qr-retry-start")
    attempts = writes(page, "weixin-qr-start")
    assert len(attempts) == 2 and attempts[0]["args"] == attempts[1]["args"]
    assert page.evaluate("counter") == 1
    page.evaluate("cancelFail=true")
    action(page, "qr-refresh")
    assert page.evaluate("counter") == 1
    expect(page.locator(".cc-wx-qr")).to_contain_text("未创建新二维码")
    page.evaluate("cancelFail=false")
    action(page, "qr-refresh")
    assert page.evaluate("counter") == 1
    start(page)
    assert page.evaluate("counter") == 2
    assert writes(page, "weixin-qr-start")[-1]["args"]["idempotency_key"] != attempts[0]["args"]["idempotency_key"]
    calls = page.evaluate("writes.map(x=>x.operation)")
    assert calls.index("weixin-qr-cancel") < len(calls)-1
    action(page, "qr-cancel")
    expect(page.locator(".cc-wx-qr")).to_contain_text("本次扫码已取消")


def test_qr_duplicate_clicks_and_close_discard_late_start(qr_page):
    page, _ = qr_page
    page.evaluate("pauseStart=true")
    page.locator('[name="qr_storage"]').check()
    page.locator('[name="qr_terms"]').check()
    page.evaluate("""() => {
      const button=document.querySelector('[data-cc-action="dot-channel-qr-start"]');
      window.pending=channels.act('dot-channel-qr-start',button);
      window.duplicate=channels.act('dot-channel-qr-start',button).catch(()=>({local:true}));
    }""")
    assert len(writes(page, "weixin-qr-start")) == 1
    page.evaluate("state.drawerEpoch++;document.querySelector('#drawer').textContent='已关闭'")
    page.clock.fast_forward(300)
    page.evaluate("releaseStart();pending")
    assert page.locator("#drawer").inner_text() == "已关闭"
    assert len(writes(page, "weixin-qr-start")) == 1
    assert len(writes(page, "weixin-qr-cancel")) == 1
    page.clock.fast_forward(5000)
    assert page.evaluate("statuses.length") == 0


@pytest.mark.parametrize("fence", ["room", "session", "owner"])
def test_qr_late_status_never_restores_replaced_scope(qr_page, fence):
    page, _ = qr_page
    start(page)
    page.evaluate("() => {pauseStatus=true;window.pending=clickAction('dot-channel-qr-status')}")
    if fence == "room":
        page.evaluate("state.generation++;state.project='projectB';state.conversation='conversationB'")
    elif fence == "session":
        page.evaluate("S.session={id:'new-session'}")
    else:
        page.evaluate("state.snapshot.can_manage=false")
    page.evaluate("state.drawerEpoch++;document.querySelector('#drawer').textContent='新视图'")
    page.clock.fast_forward(300)
    page.evaluate("entry={...entry,state:'confirmed',version:2,scanner_id:'old-scanner'};releaseStatus();pending")
    assert page.locator("#drawer").inner_text() == "新视图"
    page.clock.fast_forward(5000)
    assert len(writes(page, "weixin-qr-start")) == 1
    assert not writes(page, "weixin-qr-accept")
    assert not writes(page, "weixin-qr-cancel")
    assert page.evaluate("statuses.length") == 1


def test_qr_verify_unknown_never_replays_code_and_accept_unknown_is_exact_retry(qr_page):
    page, _ = qr_page
    page.evaluate("nextState='need_verifycode'")
    start(page)
    page.locator('[name="verify_code"]').fill("87654321")
    page.evaluate("statusFail=true")
    action(page, "qr-verify")
    assert not page.locator('[name="verify_code"]').count()
    page.clock.fast_forward(5000)
    assert page.evaluate("statuses.length") == 1
    page.evaluate("statusFail=false;statusResponses.push({state:'confirmed',version:2,scanner_id:'scanner-123'})")
    action(page, "qr-status")
    assert "verify_code" not in page.evaluate("statuses.at(-1).args")
    page.locator('[name="qr_scanner"]').check()
    page.evaluate("acceptFail=true")
    action(page, "qr-accept")
    expect(page.locator(".cc-wx-qr")).to_contain_text("保存结果暂未确认")
    assert page.locator('[data-channel-form="qr-accept"] [name="label"]').is_disabled()
    assert not page.locator('[data-cc-action="dot-channel-qr-refresh"]').count()
    page.evaluate("acceptFail=false")
    action(page, "qr-accept")
    attempts = writes(page, "weixin-qr-accept")
    assert len(attempts) == 2 and attempts[0]["args"] == attempts[1]["args"]
    assert not writes(page, "weixin-bind")


def test_qr_redirect_expiry_and_untrusted_image_fail_closed(qr_page):
    page, _ = qr_page
    start(page)
    page.evaluate("statusResponses.push({state:'scaned',version:2,qrcode_image:'https://attacker.invalid/tracker'})")
    action(page, "qr-status")
    assert not page.locator(".cc-wx-qr img").count()
    page.evaluate("statusResponses.push({state:'binded_redirect',version:3})")
    action(page, "qr-status")
    expect(page.locator(".cc-wx-qr")).to_contain_text("已有绑定")
    assert not page.locator('[data-cc-action="dot-channel-qr-accept"]').count()
    page.evaluate("statusResponses.push({state:'wait',version:4,expires_at:Date.now()/1000+1})")
    action(page, "qr-status")
    page.clock.fast_forward(1300)
    expect(page.locator(".cc-wx-qr")).to_contain_text("二维码已过期")
    assert not page.locator(".cc-wx-qr img").count()
    before = page.evaluate("statuses.length")
    page.clock.fast_forward(6000)
    assert page.evaluate("statuses.length") == before


def test_independent_channel_settings_require_exact_explicit_click(qr_page):
    page, _ = qr_page
    assert not writes(page, "channel-settings")
    assert page.locator('[data-cc-action="dot-channel-tg-import"]').is_disabled()
    page.evaluate("confirmResult=false")
    action(page, "setting-telegram")
    assert not writes(page, "channel-settings")
    page.evaluate("confirmResult=true")
    action(page, "setting-telegram")
    sent = writes(page, "channel-settings")[0]["args"]
    assert sent["channel"] == "telegram" and sent["enabled"] is True
    assert sent["expected_version"] == 0
    assert page.evaluate("data.weixin.enabled") is True
    assert page.locator('[data-cc-action="dot-channel-tg-import"]').is_enabled()
    action(page, "setting-weixin")
    sent = writes(page, "channel-settings")[-1]["args"]
    assert sent["channel"] == "weixin" and sent["enabled"] is False
    assert page.evaluate("data.telegram.enabled") is True
    assert page.locator('[data-cc-action="dot-channel-qr-start"]').is_disabled()
    assert not writes(page, "weixin-qr-start")
    assert not writes(page, "telegram-account")
    assert not writes(page, "telegram-bind")
    page.evaluate("state.snapshot.can_manage=false")
    refused = page.evaluate("clickAction('dot-channel-setting-telegram').then(()=>false,()=>true)")
    assert refused and len(writes(page, "channel-settings")) == 2


def test_qr_existing_account_rejection_allows_cancel_without_replacing_credentials(qr_page):
    page, _ = qr_page
    page.evaluate("nextState='confirmed';acceptError={status:409,code:'WEIXIN_ACCOUNT_EXISTS'}")
    start(page)
    page.locator('[name="qr_scanner"]').check()
    action(page, "qr-accept")
    expect(page.locator(".cc-wx-qr")).to_contain_text("此机器人已登记")
    assert not page.locator('[data-cc-action="dot-channel-qr-accept"]').count()
    assert page.locator('[data-cc-action="dot-channel-qr-cancel"]').is_enabled()
    action(page, "qr-cancel")
    expect(page.locator(".cc-wx-qr")).to_contain_text("本次扫码已取消")
    assert page.evaluate("data.weixin.accounts.length") == 0
    assert not writes(page, "weixin-account")


@pytest.mark.parametrize("resolution", ["expired", "accepted"])
def test_qr_unknown_save_can_query_terminal_outcome_without_repeating_save(qr_page, resolution):
    page, _ = qr_page
    page.evaluate("nextState='confirmed';acceptFail=true")
    start(page)
    page.locator('[name="qr_scanner"]').check()
    action(page, "qr-accept")
    page.clock.fast_forward(301000)
    # Local QR expiry does not turn an unconfirmed save into a new login.
    assert page.locator('[data-cc-action="dot-channel-qr-status"]').is_enabled()
    assert not page.locator('[data-cc-action="dot-channel-qr-new"]').count()
    if resolution == "accepted":
        page.evaluate("statusResponses.push({state:'accepted',version:2,account_id:'accountA'})")
    else:
        page.evaluate("statusResponses.push({state:'expired',version:2})")
    action(page, "qr-status")
    if resolution == "accepted":
        assert page.locator('.cc-wx-bind [name="account_id"]').input_value() == "accountA"
        expect(page.locator(".cc-wx-binding-guide")).to_contain_text("微信登录已保存")
    else:
        expect(page.locator(".cc-wx-qr")).to_contain_text("二维码已过期")
        assert page.locator('[data-cc-action="dot-channel-qr-new"]').is_enabled()
        action(page, "qr-new")
        assert page.locator('[data-cc-action="dot-channel-qr-start"]').is_enabled()
    assert len(writes(page, "weixin-qr-accept")) == 1
    assert len(writes(page, "weixin-qr-start")) == 1
    assert not writes(page, "weixin-bind")
