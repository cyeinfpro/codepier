"""One-command onboarding in a real browser; installer commands are never run."""
import json
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright


@pytest.fixture(scope='module')
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        yield browser
        browser.close()


@pytest.fixture
def install_page(browser, stack):
    page = browser.new_page()
    state = {'creates': [], 'tickets': [], 'fail': False,
             'command': 'fixture-install --ticket PRIVATE-ONCE "</textarea><img src=x onerror=alert(1)>"',
             'expires': time.time() + 900, 'hold': False, 'held': []}

    def devices(route):
        if route.request.method == 'POST':
            state['creates'].append(route.request.post_data_json)
        if route.request.method == 'GET' and state.get('lifecycle_device'):
            response = route.fetch()
            data = response.json()
            for device in data['devices']:
                if device['id'] == state['lifecycle_device']:
                    device.update(enabled=True, online=True)
                    device['agent'] = {
                        'version': '1.6.2', 'target_version': '1.7.0',
                        'update_available': True, 'managed': True, 'service': True,
                        'service_kind': 'systemd', 'status': 'ready', 'reason': '', 'last_error': '',
                        'actions': ['agent_update', 'agent_restart', 'agent_uninstall'],
                        'can_update': True, 'can_restart': True, 'can_uninstall': True,
                        'latest_action': None,
                    }
            route.fulfill(response=response, json=data)
        else:
            route.continue_()

    def ticket(route):
        state['tickets'].append({'url': route.request.url, 'body': route.request.post_data_json})
        if state['hold']:
            state['held'].append(route)
        elif state['fail']:
            route.fulfill(status=503, json={'error': {'code': 'INSTALL_PACKAGE_UNAVAILABLE', 'message': '安装包暂时不可用'}})
        else:
            expires = state['expires']() if callable(state['expires']) else state['expires']
            route.fulfill(json={'command': state['command'], 'expires_at': expires,
                                'package': {'version': 'fixture', 'sha256': '0' * 64, 'bytes': 123, 'url': stack.url + '/fixture.zip'}})

    page.route('**/api/devices', devices)
    page.route('**/api/devices/*/install-ticket', ticket)
    page.goto(stack.url + '/#devices')
    page.fill('#password', stack.password)
    page.click('#login-form button')
    expect(page.locator('#page h1')).to_have_text('设备节点')
    assert page.evaluate('typeof agentInstallSetup==="function"'), 'agent-install.js must be loaded by index.html'
    try:
        yield page, state
    finally:
        # Drain already running route.fetch callbacks before closing their target.
        # Errors remain visible; never use ignoreErrors to hide a failed callback.
        page.unroute_all(behavior="wait")
        page.close()


def open_form(page):
    page.locator('.page-head [data-action="add-device"]').click()
    expect(page.locator('#device-form')).to_be_visible()


def fill_form(page, platform='posix', root='/home/me/Projects'):
    page.locator('#device-form input[name="name"]').fill('Install fixture')
    page.locator('#device-form select[name="platform"]').select_option(platform)
    page.locator('#device-form input[name="allow_root"]').fill(root)


@pytest.mark.parametrize('platform,root', [('posix', '/Users/test/My Projects'), ('windows', r'D:\My Projects')])
def test_create_collects_explicit_root_and_displays_literal_command(install_page, platform, root):
    page, state = install_page
    open_form(page)
    field = page.locator('#device-form input[name="allow_root"]')
    expect(field).to_have_value('')
    fill_form(page, platform, 'relative/path')
    page.click('#create-device')
    assert not state['creates']
    field.fill(root)
    page.click('#create-device')
    expect(page.locator('#agent-install-command')).to_have_value(state['command'])
    expect(page.locator('#agent-install-copy')).to_be_enabled()
    assert len(state['creates']) == 1 and set(state['creates'][0]) == {'name', 'hub_url'}
    assert state['tickets'][0]['body'] == {'hub_url': state['creates'][0]['hub_url'], 'platform': platform, 'allow_root': root}
    expect(page.locator('.modal img')).to_have_count(0)
    expect(page.locator('#agent-install-status')).to_contain_text('前执行')
    assert page.evaluate('JSON.stringify({...localStorage,...sessionStorage})').find('PRIVATE-ONCE') == -1
    with page.expect_download() as download:
        page.click('#download-pairing')
    pairing = json.loads(Path(download.value.path()).read_text())
    assert pairing['device_id'] in state['tickets'][0]['url'] and pairing['secret']


def test_ticket_failure_retries_without_creating_a_second_device(install_page):
    page, state = install_page
    state['fail'] = True
    open_form(page)
    fill_form(page)
    page.click('#create-device')
    expect(page.locator('#agent-install-status')).to_contain_text('设备已保留')
    expect(page.locator('#download-pairing')).to_be_enabled()
    expect(page.locator('#agent-install-copy')).to_be_disabled()
    state['fail'] = False
    page.click('#agent-install-refresh')
    expect(page.locator('#agent-install-command')).to_have_value(state['command'])
    assert len(state['creates']) == 1 and len(state['tickets']) == 2
    assert state['tickets'][0]['url'] == state['tickets'][1]['url']


def test_copy_fallback_retains_dialog_and_expiry_clears_command(install_page):
    page, state = install_page
    # This test targets the copy fallback and expiry handler. Other browser
    # tests exercise real pointer actionability with normal 15-minute tickets.
    state['expires'] = lambda: time.time() + 5
    open_form(page)
    fill_form(page)
    page.click('#create-device')
    expect(page.locator('#agent-install-copy')).to_be_enabled()
    page.evaluate('Object.defineProperty(navigator,"clipboard",{value:undefined,configurable:true}); document.execCommand=()=>false')
    page.locator('#agent-install-copy').dispatch_event('click')
    expect(page.locator('#agent-install-status')).to_contain_text('已选中安装命令')
    assert page.locator('#agent-install-command').evaluate('(field)=>field.selectionEnd-field.selectionStart') == len(state['command'])
    expect(page.locator('#agent-install-copy')).to_be_disabled(timeout=8000)
    expect(page.locator('#agent-install-command')).to_have_value('')
    expect(page.locator('#agent-install-status')).to_contain_text('已过期')


def test_closing_install_dialog_fences_late_ticket_response(install_page):
    page, state = install_page
    state['hold'] = True
    open_form(page)
    fill_form(page)
    page.click('#create-device')
    expect(page.locator('#agent-install-command')).to_be_visible()
    page.wait_for_timeout(100)
    assert len(state['held']) == 1
    page.locator('.modal [data-action="close-modal"]').first.click()
    state['held'].pop().fulfill(json={'command': state['command'], 'expires_at': state['expires']})
    page.wait_for_timeout(100)
    expect(page.locator('.modal')).to_have_count(0)
    assert 'PRIVATE-ONCE' not in page.locator('body').inner_text()


@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 844), (320, 568)])
def test_install_dialog_mobile_layout_and_keyboard_focus(install_page, width, height):
    page, state = install_page
    state['command'] = 'fixture-install ' + 'x' * 1800
    page.set_viewport_size({'width': width, 'height': height})
    open_form(page)
    fill_form(page)
    page.click('#create-device')
    command = page.locator('#agent-install-command')
    expect(command).to_have_value(state['command'])
    expect(command).to_be_focused()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    bounds = command.bounding_box()
    assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
    page.keyboard.press('Escape')
    expect(page.locator('.modal')).to_have_count(0)


def test_existing_offline_device_issues_ticket_without_rotation(install_page, stack):
    page, state = install_page
    pairing = stack.must(stack.client.post('/api/devices', json={'name': 'Offline install fixture', 'hub_url': stack.url}))['pairing']
    page.evaluate('loadBasics()')
    page.evaluate('(id)=>deviceDetail(id)', pairing['device_id'])
    page.locator('.modal [data-action="agent-repair"]').click()
    expect(page.locator('#device-form input[name="name"]')).to_have_count(0)
    page.locator('#device-form input[name="allow_root"]').fill('/home/me/Projects')
    page.click('#create-device')
    expect(page.locator('#agent-install-command')).to_have_value(state['command'])
    assert not state['creates']
    assert pairing['device_id'] in state['tickets'][0]['url']


def enable_lifecycle_fixture(page, state):
    # Serve the fixture at the HTTP boundary: a background loadBasics() must
    # not replace an in-memory-only fixture with the real test Agent metadata.
    state['lifecycle_device'] = page.evaluate("() => {stopEvents();return S.devices[0].id;}")
    page.evaluate('loadBasics()')
    return page.evaluate("""() => {
      const device=S.devices.find(d=>d.agent?.can_uninstall);
      document.querySelector('#page').innerHTML=devicesHTML();
      uiPageReady(false,null);
      return {id:device.id,name:device.name};
    }""")


def test_one_click_update_posts_stable_idempotency_key(install_page):
    page, _state = install_page
    device = enable_lifecycle_fixture(page, _state)
    requests = []

    def update(route):
        requests.append(route.request.post_data_json)
        route.fulfill(json={'operation_id': 'fixture-update-operation', 'pending': True, 'state': 'queued'})

    page.route('**/api/devices/*/agent-update', update)
    assert page.evaluate("agentLifecycleButtons(S.devices[0],true).includes('data-action=\"agent-update\"')")
    page.evaluate("agentLifecycleModal(S.devices[0],'agent_update')")
    expect(page.locator('#agent-lifecycle-form')).to_contain_text('原子切换')
    expect(page.locator('#agent-lifecycle-form')).to_contain_text('启动检查失败会恢复上一版本')
    page.click('#agent-lifecycle-submit')
    expect(page.locator('.modal')).to_have_count(0)
    page.wait_for_timeout(100)
    assert len(requests) == 1
    assert requests[0]['confirmation'] == ''
    assert requests[0]['idempotency_key'].startswith('panel-')
    assert device['id'] in page.locator('body').inner_text() or device['name'] in page.locator('body').inner_text()


def test_uninstall_requires_exact_node_name_and_explains_preserved_files(install_page):
    page, _state = install_page
    device = enable_lifecycle_fixture(page, _state)
    requests = []

    def uninstall(route):
        requests.append(route.request.post_data_json)
        route.fulfill(json={'operation_id': 'fixture-uninstall-operation', 'pending': True, 'state': 'queued'})

    page.route('**/api/devices/*/agent-uninstall', uninstall)
    page.locator('[data-action="device-detail"]').first.click()
    page.locator('[data-action="agent-uninstall"]').click()
    expect(page.locator('#agent-lifecycle-form')).to_contain_text('不会删除授权目录中的项目文件')
    expect(page.locator('#agent-lifecycle-form')).to_contain_text('项目映射和审计记录也会保留')
    page.fill('#agent-uninstall-confirm', device['name'].lower())
    page.click('#agent-lifecycle-submit')
    page.wait_for_timeout(100)
    assert not requests
    expect(page.locator('#agent-uninstall-confirm')).to_be_visible()
    page.fill('#agent-uninstall-confirm', device['name'])
    page.click('#agent-lifecycle-submit')
    expect(page.locator('.modal')).to_have_count(0)
    page.wait_for_timeout(100)
    assert len(requests) == 1
    assert requests[0]['confirmation'] == device['name']
    assert requests[0]['idempotency_key'].startswith('panel-')


def test_modal_initial_focus_does_not_override_user_choice(install_page):
    page, _state = install_page
    page.evaluate("""() => {
        modal('Focus fixture', '<input id="first-focus"><input id="chosen-focus">');
        document.querySelector('#chosen-focus').focus();
    }""")
    page.wait_for_timeout(80)
    expect(page.locator('#chosen-focus')).to_be_focused()


@pytest.mark.parametrize('width,height', [(1440, 1000), (390, 844)])
def test_command_management_is_literal_copy_only_and_supports_custom_directory(install_page, width, height):
    page, state = install_page
    page.set_viewport_size({'width': width, 'height': height})
    requests = []
    commands = {'upgrade': 'fixture-upgrade </textarea><img src=x onerror=alert(1)>',
                'uninstall': 'fixture-uninstall --expected-device fixture'}
    def management(route):
        requests.append(route.request.post_data_json)
        route.fulfill(json={'commands': commands})
    page.route('**/api/devices/*/agent-commands', management)
    page.locator('[data-action="device-detail"]').first.click()
    page.locator('[data-action="agent-commands"]').first.click()
    expect(page.locator('#agent-command-upgrade')).to_have_value(commands['upgrade'])
    expect(page.locator('#agent-command-uninstall')).to_have_value(commands['uninstall'])
    expect(page.locator('.modal img')).to_have_count(0)
    expect(page.locator('#agent-command-form')).to_contain_text('不删除安装目录之外的项目文件')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.evaluate('Object.defineProperty(navigator,"clipboard",{value:undefined,configurable:true}); document.execCommand=()=>false')
    page.locator('[data-copy-command="uninstall"]').dispatch_event('click')
    expect(page.locator('#agent-command-status')).to_contain_text('已选中命令')
    field = page.locator('#agent-command-form input[name="install_dir"]')
    field.fill('/Users/test/Custom Agent')
    expect(page.locator('#agent-command-upgrade')).to_have_value('')
    expect(page.locator('[data-copy-command="upgrade"]')).to_be_disabled()
    page.click('#agent-command-generate')
    expect(page.locator('#agent-command-upgrade')).to_have_value(commands['upgrade'])
    assert requests[-1]['install_dir'] == '/Users/test/Custom Agent'
    assert len(requests) == 2 and not state['tickets'] and not state['creates']
    assert 'fixture-uninstall' not in page.evaluate('JSON.stringify({...localStorage,...sessionStorage})')


def test_command_management_closing_dialog_fences_late_response(install_page):
    page, _state = install_page
    held = []
    page.route('**/api/devices/*/agent-commands', lambda route: held.append(route))
    page.locator('[data-action="device-detail"]').first.click()
    page.locator('[data-action="agent-commands"]').first.click()
    expect(page.locator('#agent-command-form')).to_be_visible()
    page.wait_for_timeout(100)
    assert len(held) == 1
    page.locator('.modal [data-action="close-modal"]').first.click()
    held.pop().fulfill(json={'commands': {'upgrade': 'late-upgrade', 'uninstall': 'late-uninstall'}})
    page.wait_for_timeout(100)
    expect(page.locator('.modal')).to_have_count(0)
    assert 'late-uninstall' not in page.locator('body').inner_text()
