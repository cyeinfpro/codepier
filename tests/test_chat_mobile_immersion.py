"""Mobile reading space and progressive settings, using real browser engines."""

import pytest
from playwright.sync_api import expect

from tests.browser_support import chat_page, event


MOBILE_VIEWPORTS = [(390, 844, .65), (320, 568, .55), (667, 375, .40)]
PRIMARY_CONTROLS = [
    '#chat-back', '#chat-attach', '#chat-send',
    '#chat-model-picker', '#chat-options-toggle',
]


def resize(page, width, height):
    page.emulate_media(reduced_motion='reduce')
    page.set_viewport_size({'width': width, 'height': height})
    page.wait_for_function('''() => {
        const bounds = document.querySelector('#chat-root').getBoundingClientRect();
        return bounds.width <= innerWidth + 1 && bounds.bottom <= innerHeight + 1;
    }''')


def bounds_in_viewport(page, selector, width, height, touch=False):
    element = page.locator(selector)
    expect(element).to_be_visible()
    bounds = element.bounding_box()
    assert bounds and bounds['x'] >= -1 and bounds['y'] >= -1, (selector, bounds)
    assert bounds['x'] + bounds['width'] <= width + 1, (selector, bounds)
    assert bounds['y'] + bounds['height'] <= height + 1, (selector, bounds)
    if touch:
        assert bounds['width'] >= 44 and bounds['height'] >= 44, (selector, bounds)
    return bounds


def reading_geometry(page):
    return page.evaluate('''() => Object.fromEntries(
        ['#chat-scroll', '.chat-composer-wrap'].map(selector => {
            const {x, y, width, height} = document.querySelector(selector).getBoundingClientRect();
            return [selector, {x, y, width, height}];
        })
    )''')


def assert_same_reading_geometry(before, after):
    for selector, bounds in before.items():
        for dimension, value in bounds.items():
            assert after[selector][dimension] == pytest.approx(value, abs=1), (
                selector, dimension, before, after,
            )


def send_and_stream(page):
    page.fill('#chat-compose', 'Inspect the current changes')
    page.click('#chat-send')
    expect(page.locator('#chat-compose')).to_have_value('')
    request = page.evaluate("requests.filter(r => r.path.endsWith('/chat_prompt')).at(-1)")
    receipt = request['args']['receipt']
    event(page, 'chat', {
        'type': 'user', 'receipt': receipt, 'text': 'Inspect the current changes',
    }, 10)
    event(page, 'chat', {
        'type': 'delta', 'receipt': receipt,
        'text': 'The project is ready for review.\n\nI am checking the current changes.',
    }, 20)
    expect(page.locator('.chat-message-assistant')).to_contain_text('ready for review')
    return receipt


@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
@pytest.mark.parametrize('width,height,minimum_ratio', MOBILE_VIEWPORTS)
def test_mobile_conversation_preserves_reading_space(chat_page, width, height, minimum_ratio):
    page = chat_page
    resize(page, width, height)
    expect(page.locator('#chat-options')).to_be_hidden()
    expect(page.locator('#chat-new-context')).to_be_hidden()
    expect(page.locator('#chat-subtitle')).to_contain_text('Workspace one')

    for state in ['empty', 'streaming']:
        if state == 'streaming':
            send_and_stream(page)
            expect(page.get_by_role('button', name='中断当前', exact=True)).to_be_visible()
            bounds_in_viewport(page, '#chat-interrupt', width, height, touch=True)
        for selector in PRIMARY_CONTROLS:
            bounds_in_viewport(page, selector, width, height, touch=True)
        header = bounds_in_viewport(page, '.chat-header', width, height)
        assert header['height'] <= 66, (state, header)
        assert page.locator('.chat-header #chat-back').count() == 1
        assert page.locator('.chat-main #chat-back').count() == 0
        scroll = page.locator('#chat-scroll').bounding_box()
        assert scroll['height'] / height >= minimum_ratio, (state, scroll, height)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')


@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
@pytest.mark.parametrize('width,height,_minimum_ratio', MOBILE_VIEWPORTS)
def test_mobile_settings_float_without_stealing_reading_space(chat_page, width, height, _minimum_ratio):
    page = chat_page
    resize(page, width, height)
    draft = 'Keep this draft while adjusting the conversation'
    page.fill('#chat-compose', draft)
    before = reading_geometry(page)
    page.click('#chat-options-toggle')
    expect(page.locator('#chat-options-toggle')).to_have_attribute('aria-expanded', 'true')
    bounds_in_viewport(page, '#chat-options', width, height)
    assert_same_reading_geometry(before, reading_geometry(page))

    for selector in [
        '#chat-provider', '#chat-effort-select', '#chat-cwd-button',
        '#chat-project', '#chat-change-project', '#chat-commands',
        '#chat-file-library', '#chat-usage',
    ]:
        assert page.locator('#chat-options ' + selector).count() == 1
        page.locator(selector).scroll_into_view_if_needed()
        bounds_in_viewport(page, selector, width, height)
    page.locator('#chat-options-close').scroll_into_view_if_needed()
    page.click('#chat-options-close')
    expect(page.locator('#chat-options')).to_be_hidden()
    expect(page.locator('#chat-options-toggle')).to_be_focused()
    expect(page.locator('#chat-compose')).to_have_value(draft)
    assert_same_reading_geometry(before, reading_geometry(page))

    page.click('#chat-options-toggle')
    sheet = page.locator('#chat-options').bounding_box()
    shade = page.locator('#chat-options-shade').bounding_box()
    # The persistent global header stays above the sheet so returning to the
    # panel remains possible. Dismiss through the exposed conversation backdrop.
    if sheet['y'] > shade['y'] + 2:
        page.mouse.click(width / 2, (shade['y'] + sheet['y']) / 2)
    else:
        # A short landscape viewport can be completely occupied by settings.
        page.click('#chat-options-close')
    expect(page.locator('#chat-options')).to_be_hidden()
    expect(page.locator('#chat-options-toggle')).to_be_focused()
    expect(page.locator('#chat-compose')).to_have_value(draft)


@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
@pytest.mark.parametrize('width,height,_minimum_ratio', MOBILE_VIEWPORTS)
def test_mobile_nested_directory_picker_escape_restores_settings_focus(chat_page, width, height, _minimum_ratio):
    page = chat_page
    resize(page, width, height)
    page.fill('#chat-compose', 'A draft kept through nested settings')
    page.click('#chat-options-toggle')
    page.click('#chat-cwd-button')
    expect(page.locator('#chat-cwd-input')).to_be_focused()
    bounds_in_viewport(page, '#chat-popover', width, height)
    page.keyboard.press('Escape')
    expect(page.locator('#chat-popover')).to_be_hidden()
    expect(page.locator('#chat-options')).to_be_visible()
    expect(page.locator('#chat-cwd-button')).to_be_focused()
    page.keyboard.press('Escape')
    expect(page.locator('#chat-options')).to_be_hidden()
    expect(page.locator('#chat-options-toggle')).to_be_focused()
    expect(page.locator('#chat-compose')).to_have_value('A draft kept through nested settings')


@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
@pytest.mark.parametrize('width,height,_minimum_ratio', MOBILE_VIEWPORTS)
def test_mobile_send_failure_and_retry_stay_visible_outside_settings(chat_page, width, height, _minimum_ratio):
    page = chat_page
    resize(page, width, height)
    page.evaluate('''() => {
        const original = api;
        window.failPromptOnce = true;
        window.api = async (path, opts) => {
            if (path.endsWith('/chat_prompt') && failPromptOnce) {
                failPromptOnce = false;
                requests.push({path, ...JSON.parse(opts.body)});
                throw Object.assign(new Error('Fixture reply lost'), {code: 'NETWORK_UNCERTAIN'});
            }
            return original(path, opts);
        };
    }''')
    page.fill('#chat-compose', 'Keep this message until delivery is confirmed')
    page.click('#chat-send')
    expect(page.locator('#chat-status')).to_contain_text('Fixture reply lost')
    expect(page.locator('#chat-options')).to_be_hidden()
    for selector in ['#chat-status', '#chat-retry', '#chat-compose']:
        bounds_in_viewport(page, selector, width, height)
    expect(page.locator('#chat-compose')).to_have_value('Keep this message until delivery is confirmed')
    first_receipt = page.evaluate("requests.find(r => r.path.endsWith('/chat_prompt')).args.receipt")
    page.click('#chat-retry')
    expect(page.locator('#chat-compose')).to_have_value('')
    expect(page.locator('#chat-retry')).to_be_hidden()
    assert page.evaluate("requests.filter(r => r.path.endsWith('/chat_prompt')).at(-1).args.receipt") == first_receipt

    page.evaluate("S.devices = [{id: 'node-1', name: 'Studio', online: false}]; chatSyncChrome()")
    expect(page.locator('#chat-target-notice')).to_be_visible()
    bounds_in_viewport(page, '#chat-target-refresh', width, height)
    expect(page.locator('#chat-options')).to_be_hidden()
    page.fill('#chat-compose', 'Keep this draft while the node reconnects')
    expect(page.locator('#chat-send')).to_be_disabled()


@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
@pytest.mark.parametrize('width,height,_minimum_ratio', MOBILE_VIEWPORTS)
def test_mobile_settings_failure_stays_actionable_after_sheet_closes(chat_page, width, height, _minimum_ratio):
    page = chat_page
    resize(page, width, height)
    receipt = send_and_stream(page)
    event(page, 'chat', {'type': 'done', 'receipt': receipt, 'status': 'completed'}, 30)
    page.evaluate('''() => {
        const original = api;
        window.rejectSettingOnce = true;
        window.api = async (path, opts) => {
            if (path.endsWith('/chat_settings') && rejectSettingOnce) {
                rejectSettingOnce = false;
                requests.push({path, ...JSON.parse(opts.body)});
                throw Object.assign(new Error('Fixture setting rejected'), {code: 'CLI_INVALID'});
            }
            return original(path, opts);
        };
    }''')
    page.click('#chat-options-toggle')
    page.select_option('#chat-effort-select', 'high')
    expect(page.locator('#chat-settings-retry')).to_be_visible()
    page.click('#chat-options-close')
    expect(page.locator('#chat-options')).to_be_hidden()
    expect(page.locator('#chat-status')).to_contain_text('Fixture setting rejected')
    for selector in ['#chat-setting-state', '#chat-settings-retry', '#chat-status']:
        bounds_in_viewport(page, selector, width, height)
    first_receipt = page.evaluate("requests.find(r => r.path.endsWith('/chat_settings')).args.receipt")
    page.click('#chat-settings-retry')
    expect(page.locator('#chat-settings-retry')).to_be_hidden()
    assert page.evaluate("requests.filter(r => r.path.endsWith('/chat_settings')).at(-1).args.receipt") != first_receipt
    expect(page.locator('#chat-options')).to_be_hidden()


@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
def test_open_mobile_settings_survive_orientation_and_desktop_resizes(chat_page):
    page = chat_page
    resize(page, 390, 844)
    draft = 'Preserve the draft through orientation changes'
    page.fill('#chat-compose', draft)
    page.click('#chat-options-toggle')
    resize(page, 667, 375)
    bounds_in_viewport(page, '#chat-options', 667, 375)
    page.locator('#chat-cwd-button').scroll_into_view_if_needed()
    bounds_in_viewport(page, '#chat-cwd-button', 667, 375)
    expect(page.locator('#chat-compose')).to_have_value(draft)

    resize(page, 1440, 1000)
    expect(page.locator('#chat-options-toggle')).to_be_hidden()
    expect(page.locator('#chat-options-close')).to_be_hidden()
    for selector in ['#chat-provider', '#chat-effort-select', '#chat-cwd-button', '#chat-find-toggle', '#chat-inspector-toggle']:
        bounds_in_viewport(page, selector, 1440, 1000)
    expect(page.locator('#chat-compose')).to_have_value(draft)
    page.locator('#chat-compose').click()
    expect(page.locator('#chat-compose')).to_be_focused()

    resize(page, 320, 568)
    expect(page.locator('#chat-options')).to_be_hidden()
    for selector in PRIMARY_CONTROLS:
        bounds_in_viewport(page, selector, 320, 568, touch=True)
    expect(page.locator('#chat-compose')).to_have_value(draft)


@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
def test_mobile_more_menu_preserves_search_and_inspector_access(chat_page):
    page = chat_page
    resize(page, 320, 568)
    expect(page.locator('#chat-find-toggle')).to_be_hidden()
    expect(page.locator('#chat-inspector-toggle')).to_be_hidden()
    page.click('#chat-history-toggle')
    expect(page.locator('#chat-root')).to_have_class('chat-workspace drawer-open')
    page.locator('.chat-overflow summary').click()
    expect(page.locator('#chat-root')).not_to_have_class('chat-workspace drawer-open')
    page.locator('[data-chat-action="chat-find-toggle"]').click()
    expect(page.locator('#chat-find-input')).to_be_focused()
    page.keyboard.press('Escape')
    expect(page.locator('#chat-find-bar')).to_be_hidden()
    page.locator('.chat-overflow summary').click()
    page.locator('[data-chat-action="chat-inspector-toggle"]').click()
    expect(page.locator('#chat-inspector')).to_be_visible()
    page.keyboard.press('Escape')
    expect(page.locator('#chat-inspector')).to_be_hidden()
    page.click('#chat-options-toggle')
    page.keyboard.press('Control+f')
    expect(page.locator('#chat-options')).to_be_hidden()
    expect(page.locator('#chat-find-input')).to_be_visible()
    expect(page.locator('#chat-find-input')).to_be_focused()


@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
@pytest.mark.parametrize('origin', ['settings', 'more'])
def test_cancel_mobile_project_choice_returns_to_visible_origin(chat_page, origin):
    page = chat_page
    resize(page, 320, 568)
    draft = 'Keep the current project and this unsent draft'
    page.fill('#chat-compose', draft)
    for cancellation in ['button', 'escape']:
        if origin == 'settings':
            page.click('#chat-options-toggle')
            page.click('#chat-change-project')
            return_focus = '#chat-options-toggle'
        else:
            page.locator('.chat-overflow summary').click()
            page.locator('[data-chat-action="chat-new"]').click()
            return_focus = '.chat-overflow summary'
        expect(page.locator('#chat-project-search')).to_be_focused()
        page.fill('#chat-project-search', 'Workspace two')
        if cancellation == 'button':
            page.click('#chat-project-dialog-close')
        else:
            page.keyboard.press('Escape')
        expect(page.locator('#chat-project-dialog')).to_have_count(0)
        expect(page.locator(return_focus)).to_be_visible()
        expect(page.locator(return_focus)).to_be_focused()
        expect(page.locator('#chat-options')).to_be_hidden()
        expect(page.locator('#chat-compose')).to_have_value(draft)
        assert page.evaluate('ChatUI.project') == 'p1'
        assert not page.evaluate("requests.some(r => r.path.endsWith('/start'))")


@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
@pytest.mark.parametrize('width,initial_height,keyboard_height', [(390, 844, 360), (320, 568, 340)])
def test_long_draft_keeps_controls_and_messages_reachable_when_viewport_shrinks(chat_page, width, initial_height, keyboard_height):
    """Exercise a keyboard-sized viewport, without claiming a real iOS keyboard."""
    page = chat_page
    resize(page, width, initial_height)
    send_and_stream(page)
    draft = '\n'.join(f'Check item {index}: preserve this detailed follow-up.' for index in range(1, 15))
    page.fill('#chat-compose', draft)
    expect(page.locator('#chat-compose')).to_be_focused()
    resize(page, width, keyboard_height)
    expect(page.locator('#chat-compose')).to_have_value(draft)
    expect(page.locator('#chat-compose')).to_be_focused()
    for selector in PRIMARY_CONTROLS:
        bounds_in_viewport(page, selector, width, keyboard_height, touch=True)
    scroll = bounds_in_viewport(page, '#chat-scroll', width, keyboard_height)
    assert scroll['height'] >= 72, scroll
    textarea = bounds_in_viewport(page, '#chat-compose', width, keyboard_height)
    assert textarea['height'] <= keyboard_height * .25
    assert page.locator('#chat-compose').evaluate('(el) => el.scrollHeight > el.clientHeight')

    page.click('#chat-model-picker')
    expect(page.locator('#chat-model-search')).to_be_focused()
    bounds_in_viewport(page, '#chat-popover', width, keyboard_height)
    page.keyboard.press('Escape')
    expect(page.locator('#chat-model-picker')).to_be_focused()
    expect(page.locator('#chat-compose')).to_have_value(draft)
    resize(page, width, initial_height)
    page.click('#chat-compose')
    expect(page.locator('#chat-compose')).to_be_focused()
    expect(page.locator('#chat-compose')).to_have_value(draft)


@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
def test_directory_picker_does_not_restore_focus_to_hidden_control_after_mobile_resize(chat_page):
    page = chat_page
    draft = 'Keep the draft when the desktop settings move into a mobile sheet'
    page.fill('#chat-compose', draft)
    page.click('#chat-cwd-button')
    expect(page.locator('#chat-cwd-input')).to_be_focused()
    resize(page, 390, 844)
    expect(page.locator('#chat-popover')).to_be_hidden()
    expect(page.locator('#chat-options')).to_be_hidden()
    expect(page.locator('#chat-options-toggle')).to_be_focused()
    page.keyboard.press('Escape')
    expect(page.locator('#chat-options-toggle')).to_be_visible()
    expect(page.locator('#chat-options-toggle')).to_be_focused()
    expect(page.locator('#chat-compose')).to_have_value(draft)
