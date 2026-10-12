"""Synthetic pairing status UI; provider traffic is never used."""
import pytest
from playwright.sync_api import expect
from tests.weixin_browser_support import qr_page as qr_page


def pairing(page, expired=False):
    page.evaluate("""(expired) => {
      const now=Date.now()/1000;
      data.weixin.accounts=[{id:'accountA',bot_id:'botA',label:'合成微信连接',state:'active',
        last_sync_at:null,next_poll:now+640,reason_code:'WEIXIN_UPDATES_REJECTED'}];
      data.weixin.bindings=[{id:'bindingA',account_id:'accountA',dot_id:'dotA',state:'pairing',
        version:1,generation:1,expires_at:now+86400,pairing_until:now+(expired?-1:900),
        pairing_challenge:'SYNTHETIC-PAIRING-ONLY'}];
      const original=api;
      window.pollReads=0;window.pollSignals=[];window.pollHold=false;window.pollFail=false;
      window.pendingReads=[];
      window.api=async(url,options={})=>{
        if(url.includes('/weixin?')){
          pollReads++;pollSignals.push(options.signal);
          if(pollFail)throw Object.assign(Error('synthetic unavailable'),{status:503});
          if(pollHold){
            const snapshot=structuredClone(data.weixin);
            return await new Promise(resolve=>pendingReads.push(()=>resolve(snapshot)));
          }
        }
        return original(url,options);
      };
    }""", expired)
    page.evaluate("channels.act('dot-channel-open',document.querySelector('#open'))")


def advance(page, ms=5001):
    page.clock.run_for(ms)
    page.evaluate("Promise.resolve()")


def ready(page):
    page.evaluate("""() => {
      data.weixin.accounts[0].last_sync_at=Date.now()/1000;
      data.weixin.accounts[0].reason_code='';
      Object.assign(data.weixin.bindings[0],{state:'awaiting_owner',version:2,
        pending_peer_id:'synthetic-peer-only',pairing_challenge:null});
    }""")


def test_pairing_errors_retry_and_first_success_are_visible(qr_page):
    page, _ = qr_page
    pairing(page)
    health = page.locator(".cc-wx-health")
    expect(health).to_contain_text("尚未成功完成收件查询")
    expect(health).to_contain_text("WEIXIN_UPDATES_REJECTED")
    expect(health).to_contain_text("下次收件尝试不早于")
    expect(health).not_to_contain_text("已启用")
    ready(page)
    advance(page)
    expect(health).to_contain_text("最近成功收件查询")
    expect(health).not_to_contain_text("WEIXIN_UPDATES_REJECTED")
    expect(page.locator(".cc-wx-bindings")).to_contain_text("等待核对联系人")
    expect(page.locator('[data-cc-action="dot-channel-activate"]')).to_be_visible()
    assert page.evaluate("writes.length") == 0
    assert page.evaluate("pollReads") == 2


def test_refresh_preserves_unrelated_inputs_focus_and_qr(qr_page):
    page, _ = qr_page
    pairing(page)
    page.get_by_text("高级：登记已有授权机器人", exact=True).click()
    name = page.locator('[data-channel-form="account"] [name="label"]')
    name.fill("未提交的合成名称")
    page.locator('[data-channel-form="account"] [name="confirm_credentials"]').check()
    name.focus()
    page.evaluate("window.qrNode=document.querySelector('.cc-wx-qr')")
    ready(page)
    advance(page)
    expect(name).to_have_value("未提交的合成名称")
    expect(name).to_be_focused()
    expect(page.locator('[data-channel-form="account"] [name="confirm_credentials"]')).to_be_checked()
    assert page.evaluate("qrNode===document.querySelector('.cc-wx-qr')")
    expect(page.locator(".cc-wx-bindings")).to_contain_text("等待核对联系人")
    assert page.evaluate("writes.length") == 0


@pytest.mark.parametrize("fence", ["clear", "drawer", "room", "session", "owner"])
def test_inflight_refresh_is_single_aborted_and_never_restores_old_scope(qr_page, fence):
    page, _ = qr_page
    pairing(page)
    page.evaluate("pollHold=true")
    advance(page)
    assert page.evaluate("pendingReads.length") == 1
    advance(page, 30000)
    assert page.evaluate("pollReads") == 2
    page.evaluate("""(fence) => {
      if(fence==='clear')channels.clear();
      if(fence==='drawer')state.drawerEpoch++;
      if(fence==='room'){state.generation++;state.project='projectB';state.conversation='conversationB';}
      if(fence==='session')S.session={id:'replacement-session'};
      if(fence==='owner')state.snapshot.can_manage=false;
      document.querySelector('#drawer').textContent='已离开原配对';
    }""", fence)
    advance(page, 501)
    assert page.evaluate("pollSignals.at(-1).aborted")
    page.evaluate("pendingReads.forEach(resolve=>resolve())")
    advance(page, 40000)
    expect(page.locator("#drawer")).to_have_text("已离开原配对")
    assert page.evaluate("pollReads") == 2
    assert page.evaluate("writes.length") == 0


def test_refresh_transport_error_keeps_evidence_and_recovers(qr_page):
    page, _ = qr_page
    pairing(page)
    page.evaluate("pollFail=true")
    advance(page)
    expect(page.locator(".cc-wx-refresh-notice")).to_contain_text("上次读取结果")
    expect(page.locator(".cc-wx-health")).to_contain_text("WEIXIN_UPDATES_REJECTED")
    page.evaluate("""() => {
      pollFail=false;
      data.weixin.accounts[0].state='attention';
      data.weixin.accounts[0].reason_code='WEIXIN_SESSION_EXPIRED';
    }""")
    advance(page, 10001)
    expect(page.locator(".cc-wx-refresh-notice")).to_have_text("")
    expect(page.locator(".cc-wx-health")).to_contain_text("此连接已停止自动收件")
    expect(page.locator(".cc-wx-health")).to_contain_text("微信登录已失效")
    assert page.evaluate("writes.length") == 0


@pytest.mark.parametrize("already_expired", [False, True])
def test_expired_pairing_has_no_reusable_code_or_infinite_status_poll(qr_page, already_expired):
    page, _ = qr_page
    pairing(page, expired=already_expired)
    if not already_expired:
        page.evaluate("data.weixin.bindings[0].pairing_until=Date.now()/1000-1")
        advance(page)
    expect(page.locator(".cc-wx-bindings")).to_contain_text("配对码已过期")
    expect(page.get_by_role("textbox", name="微信配对码")).not_to_have_value("SYNTHETIC-PAIRING-ONLY")
    count = page.evaluate("pollReads")
    advance(page, 30000)
    assert page.evaluate("pollReads") == count
    assert page.evaluate("writes.length") == 0


def test_pairing_selection_and_checked_identity_are_not_replaced(qr_page):
    page, _ = qr_page
    pairing(page)
    code = page.get_by_role("textbox", name="微信配对码")
    code.focus()
    code.evaluate("(node)=>node.setSelectionRange(0,9)")
    ready(page)
    advance(page)
    expect(code).to_be_focused()
    assert code.evaluate("(node)=>node.selectionEnd") == 9
    page.locator('[data-cc-action="dot-channel-open"]').focus()
    advance(page)
    confirm = page.locator('[name="confirm_peer"]')
    confirm.check()
    page.evaluate("data.weixin.bindings[0].pending_peer_id='changed-synthetic-peer';data.weixin.bindings[0].version=3")
    advance(page)
    expect(confirm).to_be_checked()
    expect(page.locator(".cc-wx-bindings")).not_to_contain_text("changed-synthetic-peer")
    page.evaluate("clickAction('dot-channel-activate')")
    saved = page.evaluate("writes[0]")
    assert saved["args"]["expected_version"] == 2
    assert saved["args"]["confirm_peer_id"] == "synthetic-peer-only"

def test_refresh_authorization_denial_stops_without_retry(qr_page):
    page, _ = qr_page
    pairing(page)
    page.evaluate("""() => {
      const prior=api;
      window.api=async(url,options={})=>{
        if(url.includes('/weixin?')){pollReads++;throw Object.assign(Error('denied'),{status:403});}
        return prior(url,options);
      };
    }""")
    advance(page)
    expect(page.locator(".cc-wx-refresh-notice")).to_contain_text("已停止自动刷新")
    count = page.evaluate("pollReads")
    advance(page, 60000)
    assert page.evaluate("pollReads") == count


def test_unchanged_peer_new_version_does_not_upgrade_checked_approval(qr_page):
    page, _ = qr_page
    pairing(page)
    ready(page)
    advance(page)
    page.locator('[name="confirm_peer"]').check()
    page.evaluate("data.weixin.bindings[0].version=3")
    advance(page)
    page.evaluate("clickAction('dot-channel-activate')")
    assert page.evaluate("writes[0].args.expected_version") == 2


def test_natural_pairing_expiry_preserves_focus_but_clears_old_code(qr_page):
    page, _ = qr_page
    pairing(page)
    code = page.get_by_role("textbox", name="微信配对码")
    code.focus()
    page.clock.fast_forward(901000)
    page.evaluate('Promise.resolve()')
    expect(code).to_be_focused()
    expect(code).to_have_value("配对码已过期；请撤销原绑定后重新生成")
    expect(page.locator(".cc-wx-bindings")).to_contain_text("配对码已过期")
    count = page.evaluate("pollReads")
    advance(page, 30000)
    assert page.evaluate("pollReads") == count


@pytest.mark.parametrize("reason", ["WEIXIN_POLL_IDLE", "WEIXIN_POLL_FENCE_LOST",
    "WEIXIN_POLL_CANCELLED", "WEIXIN_BINDING_CHANGED"])
def test_transient_poll_reason_does_not_request_new_login(qr_page, reason):
    page, _ = qr_page
    pairing(page)
    page.evaluate("(reason)=>data.weixin.accounts[0].reason_code=reason", reason)
    advance(page)
    health = page.locator(".cc-wx-health")
    expect(health).to_contain_text(reason)
    expect(health).not_to_contain_text("需要重新")
    expect(health).not_to_contain_text("已停止自动收件")
    expect(health).to_contain_text("尚未成功完成收件查询")


def test_expired_code_is_cleared_even_when_status_transport_fails(qr_page):
    page, _ = qr_page
    pairing(page)
    page.evaluate("pollFail=true")
    page.clock.fast_forward(901000)
    advance(page, 30001)
    expect(page.get_by_role("textbox", name="微信配对码")).to_have_value(
        "配对码已过期；请撤销原绑定后重新生成")
    expect(page.locator(".cc-wx-refresh-notice")).to_contain_text("配对码已到期")
    count = page.evaluate("pollReads")
    advance(page, 30000)
    assert page.evaluate("pollReads") == count
