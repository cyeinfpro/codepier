"""Exercise only the current candidate UI against synthetic panel responses."""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.weixin_browser_support import qr_page as qr_page, full_styles
from tests.test_dot_weixin_qr_browser import action, start, writes


def setup(page, channel):
    page.set_default_timeout(5000)
    page.evaluate('''channel => {
      const originalApi = window.api;
      window.previewCalls = [];
      window.pausePreview = false;
      window.previewResult = args => ({...args, project_id: 'projectA',
        account_label: '合成账号', bot_id: 'synthetic-bot', text_version: 'channel-consent-v1',
        scope_digest: 'a'.repeat(64), capabilities: ['read','write','execute'],
        execution_targets: ['agent'], max_delegations: 50,
        result_destination: '原私聊及当前话题', revoke_path: '协作中心 → 连接 → 撤销任务授权'});
      window.api = async (url, options={}) => {
        if (url === '/api/collaboration/channel-enrollment-preview') {
          const args = JSON.parse(options.body);
          previewCalls.push(args);
          const result = previewResult(args);
          if (pausePreview) await new Promise(resolve => window.releasePreview = resolve);
          return result;
        }
        return originalApi(url, options);
      };
      data[channel].enabled = true;
      data[channel].accounts = [{id:'accountA', state:'active', bot_id:'synthetic-bot', label:'合成账号'},
        {id:'accountB', state:'active', bot_id:'other-bot', label:'另一个合成账号'}];
    }''', channel)
    action(page, 'open')
    if channel == 'telegram':
        action(page, 'view-overview')
        action(page, 'view-telegram')
    form = page.locator('[data-channel-form="enrollment"]')
    form.locator('[name="account_id"]').select_option('accountA')
    form.locator('[name="dot_id"]').select_option('dotA')
    expect(form.locator('.cc-enrollment-summary')).to_contain_text('累计最多 50 次')
    expect(form.locator('[name="enrollment_agreement"]')).not_to_be_checked()
    return form


@pytest.mark.parametrize('channel', ['weixin', 'telegram'])
@pytest.mark.parametrize('width', [390, 1440])
def test_unified_purposes_exact_payload_and_single_submit(qr_page, channel, width):
    page, engine = qr_page
    page.set_viewport_size({'width': width, 'height': 1100})
    form = setup(page, channel)
    full_styles(page)
    for text in ('projectA / test', 'read / write / execute', 'agent', '原私聊', '撤销任务授权'):
        expect(form).to_contain_text(text)
    assert form.locator('[data-consent-purpose]').count() == 5
    assert form.locator('input[type=checkbox]').count() == 1
    action(page, 'enrollment-approve')
    assert page.evaluate('writes.length') == 0
    form.locator('[name=enrollment_agreement]').check()
    evidence = Path('.work/enrollment-browser-evidence')
    evidence.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(evidence / f'consent-{channel}-{engine}-{width}.png'), full_page=True)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.evaluate("Promise.all([clickAction('dot-channel-enrollment-approve'),clickAction('dot-channel-enrollment-approve')])")
    submitted = page.evaluate('writes')
    assert len(submitted) == 1
    assert submitted[0]['operation'] == channel + '-bind'
    args = submitted[0]['args']
    assert args['project'] == 'projectA' and args['conversation_id'] == 'conversationA'
    assert args['account_id'] == 'accountA' and args['dot_id'] == 'dotA'
    assert args['owner_preapproval'] == {
        'text_version': 'channel-consent-v1', 'expected_scope_digest': 'a' * 64,
        'expected_peer_id': '', 'confirm_owner_identity': True, 'confirm_future_tasks': True,
        'confirm_result_delivery': True, 'confirm_until_revoked': True, 'confirm_pairing_notices': True}
    assert args['confirm_external_data_ingress'] is True
    assert not args.get('allow_media_outbound', False)
    assert 'confirm_provider_terms' not in args and 'confirm_credential_storage' not in args
    if channel == 'telegram':
        assert args['confirm_full_mirror'] is True


@pytest.mark.parametrize('channel', ['weixin', 'telegram'])
@pytest.mark.parametrize('change', ['account', 'contact', 'room', 'session', 'late_preview'])
def test_checked_consent_never_moves_to_another_scope(qr_page, channel, change):
    page, _ = qr_page
    form = setup(page, channel)
    form.locator('[name=enrollment_agreement]').check()
    if change == 'account':
        form.locator('[name=account_id]').select_option('accountB')
        expect(form.locator('[name=enrollment_agreement]')).not_to_be_checked()
    elif change == 'contact':
        form.locator('.cc-enrollment-peer > summary').click()
        form.locator('[name=expected_peer_id]').fill('another-peer')
        expect(form.locator('[name=enrollment_agreement]')).not_to_be_checked()
    elif change == 'room':
        page.evaluate("state.generation++; state.project='other-project'; state.conversation='other-topic'")
    elif change == 'session':
        page.evaluate("S.session={id:'different-session'}")
    else:
        # Return immediately; Playwright otherwise awaits the deliberately
        # unresolved preview promise before the test can release it.
        page.evaluate("() => { pausePreview=true; window.pendingPreview=clickAction('dot-channel-enrollment-preview'); }")
        action(page, 'view-overview')
        page.evaluate('releasePreview(); pendingPreview')
        assert page.locator('[data-channel-form=enrollment]').count() == 0
        assert page.evaluate('writes.length') == 0
        return
    page.evaluate("clickAction('dot-channel-enrollment-approve').catch(() => null)")
    assert page.evaluate('writes.length') == 0


def test_qr_saves_exact_receipt_without_second_credentials_or_terms_checkbox(qr_page):
    page, _ = qr_page
    page.evaluate("""() => {
      const original = makeEntry;
      window.makeEntry=()=>({...original(), consent_receipt_id:'a'.repeat(32)});
    }""")
    start(page)
    page.evaluate("statusResponses.push({state:'confirmed',version:2,scanner_id:'scanner-123'})")
    action(page, 'qr-status')
    form = page.locator('[data-channel-form=qr-accept]')
    expect(form).to_contain_text('凭据保存与服务条款已分别记录')
    assert form.locator('[name=qr_scanner]').count() == 0
    action(page, 'qr-accept')
    accepted = writes(page, 'weixin-qr-accept')
    assert len(accepted) == 1
    args = accepted[0]['args']
    assert args['confirm_scanner_id'] == 'scanner-123'
    assert args['consent_receipt_id'] == 'a' * 32
    assert 'confirm_credential_storage' not in args and 'confirm_provider_terms' not in args
    assert not writes(page, 'weixin-bind')
    enrollment = page.locator('[data-channel-form=enrollment]')
    expect(enrollment).to_be_visible()
    expect(enrollment.locator('[name=account_id]')).to_have_value('accountA')
    expect(enrollment.locator('[name=enrollment_agreement]')).not_to_be_checked()
    assert page.locator('.cc-wx-bind').get_attribute('open') is None
    expect(page.locator('.cc-wx-binding-guide')).to_contain_text('自动开通')
