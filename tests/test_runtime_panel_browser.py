"""Real panel runtime settings journeys on Chromium/WebKit, desktop and phone."""
from pathlib import Path
import pytest
from playwright.sync_api import expect
from tests.test_settings_center_browser import login
from tests.test_runtime_panel_http import snapshot, set_mode, applied, endpoint
from agent.project_runtime import default_profile

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=[('chromium', 1280, 850), ('webkit', 1280, 850),
                        ('chromium', 390, 844), ('webkit', 390, 844)])
def runtime_page(request, chat_browser_pool, stack):
    response, _ = set_mode(stack, 'project_clean_v1', path=default_profile()['path'])
    operation = stack.must(response)['operation_id']
    assert stack.poll(operation, timeout=25)['state'] == 'succeeded'
    applied(stack, 'project_clean_v1')
    engine, width, height = request.param
    context = chat_browser_pool(engine).new_context(viewport={'width': width, 'height': height})
    page = context.new_page(); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    login(page, stack)
    page.evaluate("navigate('projects')")
    page.locator('[data-runtime-project="' + stack.project['id'] + '"]').click()
    expect(page.locator('#project-runtime-form')).to_be_visible()
    expect(page.locator('[data-runtime-state]')).to_have_text('已生效', timeout=25000)
    yield page, engine, width
    context.close()
    assert not errors, errors


def test_mode_save_is_single_write_and_verified_after_reload(runtime_page, stack):
    page, engine, width = runtime_page
    form = page.locator('#project-runtime-form')
    expect(form.locator('[value="project_clean_v1"]')).to_be_checked()
    expect(form.locator('.runtime-option').first).to_contain_text('默认')
    writes = []
    page.on('request', lambda req: writes.append(req.post_data) if req.method == 'PUT' and req.url.endswith(endpoint(stack)) else None)
    form.locator('[value="native_compat"]').check()
    with page.expect_response(lambda response: response.request.method == 'PUT' and response.url.endswith(endpoint(stack))) as submitted:
        form.evaluate('(form) => {form.requestSubmit(); form.requestSubmit();}')
    response = submitted.value
    assert response.ok, (response.status, response.text())
    expect(page.locator('[data-runtime-current]')).to_contain_text('兼容模式', timeout=35000)
    expect(page.locator('[data-runtime-state]')).to_have_text('已生效', timeout=35000)
    assert len(writes) == 1
    assert snapshot(stack)['runtime']['mode'] == 'native_compat'
    assert page.evaluate("document.querySelector('.modal').dataset.editorDirty") == 'false'
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    output = ROOT / '.work/runtime-panel-20261017/screenshots'; output.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(output / f'runtime-{engine}-{width}.png'))


def test_container_requirements_and_cancel_do_not_change_mode(runtime_page, stack):
    page, _, _ = runtime_page
    form = page.locator('#project-runtime-form')
    before = snapshot(stack)['revision']
    form.locator('[value="container_v1"]').check()
    expect(form.locator('[data-runtime-container]')).to_be_visible()
    expect(form.locator('[name="engine"]')).to_be_visible()
    expect(form.locator('[data-runtime-hint]')).to_contain_text('不会回退')
    writes = []
    page.on('request', lambda req: writes.append(req) if req.method == 'PUT' and req.url.endswith(endpoint(stack)) else None)
    page.locator('[data-runtime-save]').click()
    assert len(writes) == 0  # Required fields are enforced before submission.
    page.once('dialog', lambda dialog: dialog.accept())
    page.locator('[data-runtime-cancel]').click()
    expect(form).to_have_count(0)
    assert snapshot(stack)['revision'] == before


def test_conflicting_save_retains_draft_then_refresh_loads_current(runtime_page, stack):
    page, _, _ = runtime_page
    form = page.locator('#project-runtime-form')
    form.locator('[value="native_compat"]').check()
    response, _ = set_mode(stack, 'project_clean_v1', path=['new-tools'])
    stack.must(response); applied(stack, 'project_clean_v1')
    page.locator('[data-runtime-save]').click()
    expect(page.locator('[data-runtime-feedback]')).to_contain_text('草稿', timeout=25000)
    expect(form.locator('[value="native_compat"]')).to_be_checked()
    assert snapshot(stack)['profile']['path'] == ['new-tools']
    page.once('dialog', lambda dialog: dialog.accept())
    page.locator('[data-runtime-refresh]').click()
    expect(form.locator('[value="project_clean_v1"]')).to_be_checked(timeout=25000)
    expect(form.locator('[name="runtime_path"]')).to_have_value('new-tools')


def test_lost_save_response_uses_original_receipt_not_another_put(runtime_page, stack):
    page, _, _ = runtime_page
    writes = []
    def lose(route):
        if route.request.method != 'PUT': route.continue_(); return
        writes.append(route.request.post_data)
        result = route.fetch()
        assert result.status == 200
        route.abort('failed')
    page.route('**' + endpoint(stack), lose)
    page.locator('[value="native_compat"]').check()
    page.locator('[data-runtime-save]').click()
    expect(page.locator('[data-runtime-state]')).to_have_text('保存结果未确认', timeout=20000)
    page.locator('[data-runtime-refresh]').click()
    expect(page.locator('[data-runtime-current]')).to_contain_text('兼容模式', timeout=35000)
    expect(page.locator('[data-runtime-state]')).to_have_text('已生效', timeout=35000)
    assert len(writes) == 1


def test_late_read_cannot_reopen_closed_dialog(runtime_page, stack):
    page, _, _ = runtime_page
    delayed = []
    page.route('**' + endpoint(stack), lambda route: delayed.append(route) if route.request.method == 'GET' else route.continue_())
    page.locator('[data-runtime-refresh]').click()
    page.wait_for_timeout(100)
    page.locator('[data-runtime-cancel]').click()
    expect(page.locator('#project-runtime-form')).to_have_count(0)
    for route in delayed:
        try: route.fulfill(json={'state': 'offline', 'can_save': False, 'profile': None})
        except Exception: pass
    expect(page.locator('#project-runtime-form')).to_have_count(0)



def test_page_reload_recovers_only_original_opaque_save_receipt(runtime_page, stack):
    page, _, _ = runtime_page
    writes = []
    def lose(route):
        if route.request.method != 'PUT':
            route.continue_(); return
        writes.append(route.request.post_data)
        result = route.fetch()
        assert result.status == 200
        route.abort('failed')
    page.route('**' + endpoint(stack), lose)
    page.locator('[value="native_compat"]').check()
    page.locator('[data-runtime-save]').click()
    expect(page.locator('[data-runtime-state]')).to_have_text('保存结果未确认', timeout=20000)
    stored = page.evaluate("Object.entries(sessionStorage).filter(([k]) => k.startsWith('codepier-runtime-pending-v1:')).map(([,v]) => JSON.parse(v))")
    assert len(stored) == 1 and len(stored[0]) == 1
    project_id, record = stored[0][0]
    assert project_id == stack.project['id']
    assert set(record) == {'idempotency_key', 'created_at'}
    page.reload()
    page.locator('[data-runtime-project="' + stack.project['id'] + '"]').click(timeout=25000)
    expect(page.locator('[data-runtime-state]')).to_have_text('已生效', timeout=35000)
    expect(page.locator('[data-runtime-current]')).to_contain_text('兼容模式')
    assert len(writes) == 1
    assert page.evaluate("Object.keys(sessionStorage).filter(k => k.startsWith('codepier-runtime-pending-v1:')).length") == 0


def test_historical_success_never_overwrites_newer_current_configuration(runtime_page, stack):
    page, _, _ = runtime_page
    saved = []
    def lose(route):
        if route.request.method != 'PUT':
            route.continue_(); return
        result = route.fetch()
        assert result.status == 200
        saved.append((route.request.post_data_json, result.json()))
        route.abort('failed')
    page.route('**' + endpoint(stack), lose)
    page.locator('[value="native_compat"]').check()
    page.locator('[data-runtime-save]').click()
    expect(page.locator('[data-runtime-state]')).to_have_text('保存结果未确认', timeout=20000)
    applied(stack, 'native_compat')
    response, _ = set_mode(stack, 'project_clean_v1', path=['newer-tools'])
    stack.must(response); applied(stack, 'project_clean_v1')
    key = saved[0][0]['idempotency_key']
    original = stack.must(stack.client.get(endpoint(stack) + '/receipts/' + key))
    # Even a historical no-op save can have state=applied. The UI must fetch a
    # current read rather than treating that old successful observation as live.
    original['result']['data']['state'] = 'applied'
    original['result']['data']['runtime'] = original['result']['data']['saved_runtime']
    page.route('**' + endpoint(stack) + '/receipts/' + key, lambda route: route.fulfill(json=original))
    page.locator('[data-runtime-refresh]').click()
    expect(page.locator('[data-runtime-current]')).to_contain_text('清洁模式', timeout=35000)
    expect(page.locator('[name="runtime_path"]')).to_have_value('newer-tools')
    expect(page.locator('[data-runtime-feedback]')).to_contain_text('再次发生变化')
    assert len(saved) == 1


def test_unknown_save_can_be_inspected_without_false_success_or_replay(runtime_page, stack):
    page, _, _ = runtime_page
    puts = []
    def uncertain(route):
        if route.request.method != 'PUT':
            route.continue_(); return
        puts.append(route.request.post_data_json)
        route.fulfill(status=409, json={'error': {'code': 'DURABILITY_UNCONFIRMED', 'message': '配置替换结果需要核查'}})
    page.route('**' + endpoint(stack), uncertain)
    page.route('**' + endpoint(stack) + '/receipts/*', lambda route: route.fulfill(json={
        'state': 'needs_review', 'pending': False, 'operation_id': 'a'*32,
        'outcome_confirmed': False, 'result': {'ok': False, 'error': {
            'code': 'DURABILITY_UNCONFIRMED', 'message': '配置已替换，落盘尚未确认'}},
        'replay_recommended': False}))
    page.locator('[value="native_compat"]').check()
    page.locator('[data-runtime-save]').click()
    expect(page.locator('[data-runtime-state]')).to_have_text('保存结果未确认')
    page.locator('[data-runtime-refresh]').click()
    expect(page.locator('[data-runtime-state]')).to_have_text('原保存需要核查')
    expect(page.locator('[data-runtime-review]')).to_be_visible()
    page.locator('[data-runtime-review]').click()
    expect(page.locator('[data-runtime-state]')).to_have_text('原保存仍待核查')
    expect(page.locator('[data-runtime-current]')).to_contain_text('清洁模式')
    expect(page.locator('[data-runtime-save]')).to_be_disabled()
    assert len(puts) == 1



def test_initial_offline_observation_recovers_without_any_write(runtime_page, stack):
    page, _, _ = runtime_page
    reads = []
    writes = []
    def temporary_offline(route):
        if route.request.method != 'GET':
            writes.append(route.request.method); route.continue_(); return
        reads.append(route.request.url)
        if len(reads) == 1:
            route.fulfill(json={'project_id': stack.project['id'], 'state': 'offline', 'can_save': False,
                'default_mode': 'project_clean_v1', 'profile': None, 'runtime': None, 'revision': None})
        else:
            route.continue_()
    page.route('**' + endpoint(stack), temporary_offline)
    with page.expect_response(lambda response: response.request.method == 'GET' and response.url.endswith(endpoint(stack))):
        page.locator('[data-runtime-refresh]').click()
    expect(page.locator('[data-runtime-state]')).to_have_text('节点离线')
    expect(page.locator('[data-runtime-state]')).to_have_text('已生效', timeout=20000)
    expect(page.locator('[data-runtime-current]')).to_contain_text('清洁模式')
    assert len(reads) >= 2 and not writes


def test_credential_draft_survives_mode_switch_and_submits_one_scoped_save(runtime_page, stack):
    page, engine, width = runtime_page
    form = page.locator('#project-runtime-form')
    keys = ('github_cli', 'git_config', 'ssh_agent')
    for key in keys:
        expect(form.locator('[name="runtime_credential_' + key + '"]')).to_be_checked()
    expect(form.locator('[data-runtime-credentials]')).to_contain_text('默认复用')
    expect(form.locator('[data-runtime-credentials]')).to_contain_text('逐项关闭')
    form.locator('[name="runtime_credential_git_config"]').uncheck()
    expect(form.locator('[data-runtime-credential-state="git_config"]')).to_have_text('待保存 · 将停用此来源')
    form.locator('[value="native_compat"]').check()
    expect(form.locator('[data-runtime-credentials]')).to_be_hidden()
    form.locator('[value="container_v1"]').check()
    expect(form.locator('[data-runtime-credentials]')).to_be_hidden()
    form.locator('[value="project_clean_v1"]').check()
    expect(form.locator('[name="runtime_credential_github_cli"]')).to_be_checked()
    expect(form.locator('[name="runtime_credential_ssh_agent"]')).to_be_checked()
    expect(form.locator('[name="runtime_credential_git_config"]')).not_to_be_checked()
    writes, dialogs = [], []
    # The real form submits to a deterministic endpoint response; no fixture
    # needs a personal GitHub account, native credential helper or SSH socket.
    observation = snapshot(stack)
    def endpoint_response(route):
        if route.request.method == 'PUT':
            payload = route.request.post_data_json
            writes.append(payload)
            observation['profile'] = payload['profile']
            observation['readiness'] = {'state': 'ready', 'credentials': {
                key: {'enabled': enabled, 'state': 'missing' if enabled else 'disabled', 'source': 'none'}
                for key, enabled in payload['profile']['credentials'].items()}}
            route.fulfill(json={**observation, 'saved': True, 'unchanged': False})
        else:
            route.fulfill(json=observation)
    page.route('**' + endpoint(stack), endpoint_response)
    page.on('dialog', lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    with page.expect_response(lambda response: response.request.method == 'PUT' and response.url.endswith(endpoint(stack))):
        form.evaluate('(form) => {form.requestSubmit(); form.requestSubmit();}')
    expect(page.locator('[data-runtime-state]')).to_have_text('已生效', timeout=20000)
    assert len(writes) == 1 and not dialogs
    assert writes[0]['profile']['mode'] == 'project_clean_v1'
    assert writes[0]['profile']['credentials'] == {'github_cli': True, 'git_config': False, 'ssh_agent': True}
    expect(form.locator('[data-runtime-credential-state="github_cli"]')).to_have_text('未找到可用来源')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    evidence = ROOT / '.work/runtime-credentials-20261019/screenshots'
    evidence.mkdir(parents=True, exist_ok=True)
    page.locator('[data-runtime-credentials]').scroll_into_view_if_needed()
    page.screenshot(path=str(evidence / f'credentials-{engine}-{width}.png'))


def test_credential_defaults_apply_to_missing_options_and_first_clean_selection(runtime_page, stack):
    page, _, _ = runtime_page
    form = page.locator('#project-runtime-form')
    keys = ('github_cli', 'git_config', 'ssh_agent')
    observed = snapshot(stack)
    clean = dict(observed['profile'])
    writes = []
    def observe(route):
        if route.request.method != 'GET':
            writes.append(route.request.method)
        route.fulfill(json=observed)
    page.route('**' + endpoint(stack), observe)
    for credentials in (None, {}, {'github_cli': False}):
        observed['profile'] = {key: value for key, value in clean.items() if key != 'credentials'}
        if credentials is not None:
            observed['profile']['credentials'] = credentials
        choices = {key: (credentials or {}).get(key, True) for key in keys}
        observed['readiness'] = {'state': 'ready', 'credentials': {
            key: {'enabled': enabled, 'state': 'missing' if enabled else 'disabled', 'source': 'none'}
            for key, enabled in choices.items()}}
        with page.expect_response(lambda response: response.request.method == 'GET' and response.url.endswith(endpoint(stack))):
            page.locator('[data-runtime-refresh]').click()
        for key, enabled in choices.items():
            checkbox = form.locator('[name="runtime_credential_' + key + '"]')
            if enabled:
                expect(checkbox).to_be_checked()
            else:
                expect(checkbox).not_to_be_checked()
            expect(form.locator('[data-runtime-credential-state="' + key + '"]')).to_have_text(
                '未找到可用来源' if enabled else '未启用')
        expect(page.locator('[data-runtime-save]')).to_be_disabled()
    observed['profile'] = {'mode': 'native_compat'}
    observed['runtime'] = {**observed['runtime'], 'mode': 'native_compat'}
    observed['readiness'] = {'state': 'ready'}
    observed['clean_default_profile'] = {key: value for key, value in clean.items() if key != 'credentials'}
    page.locator('[data-runtime-refresh]').click()
    expect(form.locator('[value="native_compat"]')).to_be_checked()
    expect(form.locator('[data-runtime-credentials]')).to_be_hidden()
    form.locator('[value="project_clean_v1"]').check()
    for key in keys:
        expect(form.locator('[name="runtime_credential_' + key + '"]')).to_be_checked()
        expect(form.locator('[data-runtime-credential-state="' + key + '"]')).to_have_text('待保存 · 将启用此来源')
    assert not writes


def test_credential_source_status_is_not_remote_login_proof_and_old_agent_disables_save(runtime_page, stack):
    page, engine, width = runtime_page
    observed = snapshot(stack)
    observed['profile']['credentials'] = {'github_cli': True, 'git_config': True, 'ssh_agent': False}
    observed['readiness'] = {'state': 'ready', 'credentials': {
        'github_cli': {'enabled': True, 'state': 'configured', 'source': 'configuration'},
        'git_config': {'enabled': True, 'state': 'missing', 'source': 'none'},
        'ssh_agent': {'enabled': False, 'state': 'disabled', 'source': 'none'}}}
    writes = []
    def observe(route):
        if route.request.method != 'GET':
            writes.append(route.request.method)
        route.fulfill(json=observed)
    page.route('**' + endpoint(stack), observe)
    page.locator('[data-runtime-refresh]').click()
    expect(page.locator('[data-runtime-credential-state="github_cli"]')).to_have_text('来源已配置')
    expect(page.locator('[data-runtime-credential-state="git_config"]')).to_have_text('未找到可用来源')
    expect(page.locator('[data-runtime-credential-state="ssh_agent"]')).to_have_text('未启用')
    expect(page.locator('[data-runtime-credentials]')).to_contain_text('由实际操作确认')
    evidence = ROOT / '.work/runtime-credentials-20261019/screenshots'
    evidence.mkdir(parents=True, exist_ok=True)
    page.locator('[data-runtime-credentials]').scroll_into_view_if_needed()
    page.screenshot(path=str(evidence / f'credential-sources-{engine}-{width}.png'))
    observed = {'project_id': stack.project['id'], 'state': 'upgrade_required',
        'can_save': False, 'default_mode': 'project_clean_v1', 'profile': None,
        'runtime': None, 'revision': None}
    page.locator('[data-runtime-refresh]').click()
    expect(page.locator('[data-runtime-state]')).to_have_text('需要更新 Agent')
    expect(page.locator('[name="runtime_credential_github_cli"]')).to_be_disabled()
    expect(page.locator('[data-runtime-save]')).to_be_disabled()
    assert not writes
