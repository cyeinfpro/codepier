"""Update recovery and unsaved-input protection in real Chromium and WebKit."""
import re

import pytest
from playwright.sync_api import expect

from shared.util import VERSION
from tests.test_panel_update_ui import open_settings, ready, show_update_controls, update_browser


def complete(state, version='1.11.0'):
    state.update(enabled=True, busy=False, request_found=True, running_version=version,
                 current_version=version, update_available=False,
                 operation={'id': 'f' * 32, 'kind': 'apply', 'state': 'succeeded',
                            'target_version': version, 'message': '更新成功', 'events': []})


def show_draft_field(page, dirty_field):
    if dirty_field == 'draft':
        return
    group = 'usage' if dirty_field == 'appearance' else 'connect'
    page.locator(f'.settings-groups [data-settings-group="{group}"]').click()
    if dirty_field == 'address':
        more = page.locator('#settings-more')
        if not more.evaluate('el => el.open'):
            more.locator('summary').first.click()
    selector = {
        'address': '#settings-form',
        'appearance': '[data-settings-form="appearance"]',
    }[dirty_field]
    expect(page.locator(selector)).to_be_visible()


@pytest.mark.parametrize('dirty_field', ['address', 'appearance', 'draft'])
def test_auto_refresh_defers_unsaved_values_and_resumes_after_resolution(update_browser, stack, dirty_field):
    browser, _ = update_browser
    state = ready()
    page, calls, errors = open_settings(browser, stack, state)
    try:
        show_draft_field(page, dirty_field)
        if dirty_field == 'address':
            page.fill('#settings-form [name="public_url"]', stack.url + '/unsaved')
        elif dirty_field == 'appearance':
            page.locator('[data-settings-form="appearance"] select').select_option('dark')
        else:
            page.evaluate('S.work.dirty=true')
        show_update_controls(page)
        complete(state)
        page.evaluate('CodePierPanelUpdate.resume()')
        expect(page.locator('#panel-update-note')).to_contain_text('暂缓自动刷新')
        assert '_codepier_updated=' not in page.url
        show_draft_field(page, dirty_field)
        if dirty_field == 'address':
            expect(page.locator('#settings-form [name="public_url"]')).to_have_value(stack.url + '/unsaved')
            page.fill('#settings-form [name="public_url"]', stack.url)
        elif dirty_field == 'appearance':
            expect(page.locator('[data-settings-form="appearance"] select')).to_have_value('dark')
            page.locator('[data-settings-form="appearance"] select').select_option('auto')
        else:
            page.evaluate('S.work.dirty=false')
        expect(page).to_have_url(re.compile(r'_codepier_updated=.*#settings$'), timeout=12000)
        show_update_controls(page)
        expect(page.locator('#panel-update-state')).to_contain_text('更新成功')
        assert not calls and not errors, errors
    finally:
        page.close()


def test_no_early_reload_and_monitor_survives_navigation_and_service_recovery(update_browser, stack):
    browser, _ = update_browser
    state = ready()
    complete(state)
    state['running_version'] = VERSION
    page, calls, errors = open_settings(browser, stack, state)
    try:
        expect(page.locator('#panel-update-note')).to_contain_text('等待目标版本')
        assert '_codepier_updated=' not in page.url
        page.evaluate("navigate('devices')")
        expect(page.locator('#page h1')).to_have_text('电脑')
        # A successful HTTP response with unavailable updater is not an update failure.
        state.update(enabled=False, code='UPDATER_UNAVAILABLE')
        page.wait_for_timeout(2400)
        assert '_codepier_updated=' not in page.url
        complete(state)
        expect(page).to_have_url(re.compile(r'_codepier_updated=.*#devices$'), timeout=12000)
        expect(page.locator('#page h1')).to_have_text('电脑')
        assert not calls and not errors, errors
    finally:
        page.close()


def test_completed_update_does_not_reload_loop_when_storage_is_unavailable(update_browser, stack):
    browser, _ = update_browser
    state = ready()
    script = """(() => {
      const get=Storage.prototype.getItem, set=Storage.prototype.setItem;
      Storage.prototype.getItem=function(key){if(String(key).startsWith('codepier-panel-update:'))throw new Error('storage unavailable');return get.call(this,key);};
      Storage.prototype.setItem=function(key,value){if(String(key).startsWith('codepier-panel-update:'))throw new Error('storage unavailable');return set.call(this,key,value);};
    })();"""
    page, calls, errors = open_settings(browser, stack, state, init_script=script)
    documents = []
    page.on('request', lambda request: documents.append(request.url) if request.resource_type == 'document' else None)
    try:
        complete(state)
        page.evaluate('CodePierPanelUpdate.resume()')
        expect(page).to_have_url(re.compile(r'_codepier_updated=.*#settings$'), timeout=12000)
        show_update_controls(page)
        expect(page.locator('#panel-update-state')).to_contain_text('更新成功')
        page.evaluate('CodePierPanelUpdate.resume()')
        page.wait_for_timeout(2400)
        assert len(documents) == 1, documents
        assert not calls and not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize('terminal_state', ['failed', 'rolled_back', 'recovery_required'])
def test_failed_or_rolled_back_update_never_auto_reloads(update_browser, stack, terminal_state):
    browser, _ = update_browser
    state = ready()
    page, calls, errors = open_settings(browser, stack, state)
    try:
        complete(state)
        state['operation'].update(state=terminal_state, message=terminal_state)
        state['recovery_required'] = terminal_state == 'recovery_required'
        page.evaluate('CodePierPanelUpdate.resume()')
        expect(page.locator('#panel-update-state')).to_contain_text(terminal_state)
        expect(page.locator('#panel-update-reload')).to_be_hidden()
        assert '_codepier_updated=' not in page.url
        assert not calls and not errors, errors
    finally:
        page.close()


def test_already_current_document_does_not_reload_for_historic_success(update_browser, stack):
    browser, _ = update_browser
    state = ready()
    complete(state, VERSION)
    page, calls, errors = open_settings(browser, stack, state)
    try:
        expect(page.locator('#panel-update-state')).to_contain_text('更新成功')
        page.evaluate('CodePierPanelUpdate.resume()')
        assert '_codepier_updated=' not in page.url
        assert not calls and not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize(
    ("kind", "running", "committed", "target", "waiting"),
    [
        ("apply", "1.34.1", "1.34.1", "1.33.36", False),
        ("apply", "1.10.0", "1.10.0", "1.9.9", False),
        ("apply", "1.34.1", "1.33.36", "1.33.36", True),
        ("apply", "1.34.1", None, "1.33.36", True),
        ("apply", "1.34.1", "1.34.1", "1.34.2", True),
        ("apply", "1.34.1-dev", "1.34.1-dev", "1.33.36", True),
        ("rollback", "1.34.1", "1.34.1", "1.33.36", True),
    ],
)
def test_superseded_apply_releases_only_verified_version_gate(
    update_browser, stack, kind, running, committed, target, waiting
):
    browser, _ = update_browser
    state = ready()
    state["candidate"]["version"] = "1.34.3"
    state.update(
        running_version=running,
        current_version=committed,
        operation={
            "id": "f" * 32,
            "kind": kind,
            "state": "succeeded",
            "target_version": target,
            "message": "更新成功",
            "events": [],
        },
    )
    page, calls, errors = open_settings(browser, stack, state)
    try:
        show_update_controls(page)
        expect(page.locator("#panel-update-state")).to_contain_text("更新成功")
        if waiting:
            expect(page.locator("#panel-update-note")).to_contain_text("等待目标版本")
            expect(page.locator("#panel-update-check")).to_be_disabled()
            expect(page.locator("#panel-update-apply")).to_be_disabled()
        else:
            expect(page.locator("#panel-update-note")).not_to_contain_text("等待目标版本")
            expect(page.locator("#panel-update-check")).to_be_enabled()
            expect(page.locator("#panel-update-apply")).to_be_enabled()
        assert "_codepier_updated=" not in page.url
        assert not calls and not errors, errors
    finally:
        page.close()


def test_wait_hint_clears_when_old_success_becomes_verified_history(update_browser, stack):
    browser, _ = update_browser
    state = ready()
    state["candidate"]["version"] = "1.34.3"
    state.update(
        running_version="1.33.35",
        current_version="1.33.35",
        operation={
            "id": "f" * 32,
            "kind": "apply",
            "state": "succeeded",
            "target_version": "1.33.36",
            "message": "更新成功",
            "events": [],
        },
    )
    page, calls, errors = open_settings(browser, stack, state)
    try:
        show_update_controls(page)
        note = page.locator("#panel-update-note")
        check = page.locator("#panel-update-check")
        expect(note).to_contain_text("等待目标版本")
        expect(check).to_be_disabled()
        state.update(running_version="1.34.2", current_version="1.34.2")
        page.evaluate("CodePierPanelUpdate.resume()")
        expect(check).to_be_enabled()
        expect(note).not_to_contain_text("等待目标版本")
        assert "_codepier_updated=" not in page.url
        assert not calls and not errors, errors
    finally:
        page.close()
