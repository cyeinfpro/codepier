"""Real SDK sandbox interactions: real stack evidence plus controlled race/upload hosts.

The upload host is a deterministic contract fixture, not a ChatGPT file roundtrip.
"""
import copy
import hashlib
import json
import time
import uuid
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright, expect

from tests.support import BASE
from tests.test_integrations_stack import integrated_stack, resolved, modern
from tests.test_mcp_apps_host import host_bundle


def task(s, title):
    result = s.mcp('workspace', {'project': 'Imago', 'idempotency_key': uuid.uuid4().hex, 'operation': 'workflow_create', 'options': {'title': title, 'goal': '检查真实任务证据', 'template': 'custom', 'steps': [{'title': '核查改动', 'acceptance': '固定快照可读取'}, {'title': '验证与交付', 'acceptance': '验证新鲜度并核对文件 SHA'}]}})
    assert not result['isError'], result
    return result['structuredContent']


def checkpoint(s, workflow, evidence):
    response = s.mcp('workspace', {'idempotency_key': uuid.uuid4().hex, 'operation': 'workflow_update', 'options': {'workflow_id': workflow['workflow_id'], 'expected_version': workflow['version'], 'action': 'checkpoint', 'step_id': 's1', 'step_state': 'running', 'summary': '已关联真实回执；失败、历史通过与交付状态分别展示。', 'evidence': evidence}})
    assert not response['isError'], response
    return response['structuredContent']


def mount(page, bundle, result, args=None, theme='light', html=None):
    page.set_content('<!doctype html><html><body style="margin:0"></body></html>')
    page.evaluate('(theme)=>window.codepierHostTheme=theme', theme)
    page.add_script_tag(path=str(bundle))
    page.evaluate('html=>window.codepierMount(html)', html or (BASE/'web/mcp-apps/workspace-v1.html').read_text())
    page.wait_for_function('window.codepierHostReady')
    page.evaluate('v=>window.codepierDeliver(v)', {'args': args or {'project': 'Imago'}, 'result': result})
    return page.frame_locator('#app-frame')


def response(value):
    return {'structuredContent': value, 'content': [], 'isError': False}


@pytest.mark.parametrize('width,theme', [(1100, 'light'), (390, 'dark')])
def test_task_dashboard_real_receipts_diff_freshness_and_authenticated_download(integrated_stack, host_bundle, width, theme):
    s = integrated_stack
    workflow = task(s, '任务工作台 · ' + str(width))
    filename = 'dashboard-' + str(width) + '.txt'
    (s.imago/filename).write_text('original\n')
    opened = resolved(s, 'open_workspace', {'capture_baseline': True})
    original = 'frozen content <img src=x onerror="window.INJECTED=true">\n'
    (s.imago/filename).write_text(original)
    review = resolved(s, 'show_changes', {'baseline_ref': opened['baseline_ref']})
    artifact = resolved(s, 'artifacts_register', {'path': filename})
    validation = resolved(s, 'validation_run', {'command': 'printf dashboard_verified', 'label': '源码验证回执'})
    failed = resolved(s, 'shell_exec', {'command': 'printf real_failure_log; exit 7'}, expect='failed')
    workflow = checkpoint(s, workflow, [review['operation_id'], artifact['operation_id'], validation['operation_id'], failed['operation_id']])
    card = s.mcp('workspace', {'operation': 'workflow_get', 'options': {'workflow_id': workflow['workflow_id']}})
    calls = []
    def host_tool(params):
        calls.append(copy.deepcopy(params))
        return s.mcp(params['name'], params.get('arguments', {}))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': width, 'height': 1000})
        errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
        page.expose_function('codepierHostTool', host_tool)
        app = mount(page, host_bundle, card, {'workflow_id': workflow['workflow_id']}, theme)
        expect(app.get_by_role('heading', name='任务工作台 · ' + str(width))).to_be_visible(timeout=30000)
        app.get_by_label('自动刷新任务状态').uncheck()
        expect(app.locator('.stats')).to_contain_text('未提供部署证明')
        row = app.locator('.result-rows .result-row').filter(has_text=failed['operation_id'])
        row.get_by_role('button', name='查看日志与回执').click()
        expect(app.locator('.inspector')).to_contain_text('退出码 7', timeout=30000)
        expect(app.locator('.inspector > pre')).to_have_text('real_failure_log')
        app.get_by_role('button', name='刷新原操作', exact=True).click()
        expect(app.locator('.inspector > pre')).to_have_text('real_failure_log')
        app.get_by_role('button', name='改动', exact=True).click()
        app.get_by_role('button', name='查看固定差异').click()
        app.locator('.inspector .files summary').filter(has_text=filename).click()
        expect(app.locator('.inspector .files pre')).to_contain_text('frozen content', timeout=30000)
        assert app.locator('img').count() == 0
        app.get_by_role('button', name='验证', exact=True).click()
        app.get_by_role('button', name='核对源码与报告').click()
        expect(app.locator('.inspector > .state-passed')).to_be_visible(timeout=30000)
        (s.imago/filename).write_text('later unrelated source edit\n')
        app.get_by_role('button', name='核对源码与报告').click()
        expect(app.locator('.inspector > .state-stale')).to_be_visible(timeout=30000)
        expect(app.locator('.inspector')).to_contain_text('历史结果：通过')
        app.get_by_role('button', name='交付物', exact=True).click()
        expect(app.locator('.result-rows')).to_contain_text(artifact['sha256'])
        app.get_by_role('button', name='下载交付物').click()
        page.wait_for_function('window.codepierOpenedLinks.length === 1')
        requested = page.evaluate('window.codepierOpenedLinks[0]')
        assert urlparse(requested).path == artifact['download_path']
        binary = s.client.get(artifact['download_path'])
        assert binary.status_code == 200 and binary.content == original.encode()
        assert hashlib.sha256(binary.content).hexdigest() == artifact['sha256']
        assert not s.client.get(artifact['download_path'], headers={'Authorization': 'Bearer invalid'}).is_success
        app.get_by_text('附件导入', exact=True).click()
        expect(app.get_by_role('button', name='保存文件', exact=True)).to_be_disabled()
        expect(app.locator('#app')).to_contain_text('当前宿主没有提供卡片内文件上传能力')
        app.get_by_role('button', name='选择现有目录').click()
        expect(app.get_by_role('button', name='使用此目录')).to_be_visible(timeout=30000)
        app.get_by_role('button', name='使用此目录').click()
        app.get_by_text('附件导入', exact=True).click()
        assert app.locator('html').evaluate('(n)=>n.scrollWidth <= n.clientWidth + 1')
        assert app.locator('html').get_attribute('data-theme') == theme
        assert all(call['name'] in {'workspace', 'process', 'read'} for call in calls)
        assert all('baseline_ref' not in call.get('arguments', {}) for call in calls)
        assert not errors and not page.evaluate('window.codepierHostErrors')
        folder = BASE/'docs/evidence/workspace-dashboard-20260917/screenshots'; folder.mkdir(parents=True, exist_ok=True)
        page.locator('#app-frame').evaluate('(node)=>node.style.height="1350px"')
        page.screenshot(path=str(folder/f'dashboard-{width}-{theme}.png'), full_page=True)
        browser.close()


def fixture_workspace(workflow=None, evidence=None, workflows=None):
    return {'project': 'Imago', 'project_id': 'p', 'workspace_id': '', 'observed_at': time.time(),
        'device_online': False, 'workflow': workflow, 'workflows': workflows or [], 'evidence': evidence or [],
        'recent_operations': [], 'evidence_total': len(evidence or []), 'unavailable_evidence': 0,
        'next_workflow_cursor': None, 'next_evidence_offset': None, 'execution_started': False}


def fixture_task(identifier, title):
    return {'workflow_id': identifier, 'title': title, 'goal': 'Fixture', 'summary': '', 'state': 'active',
        'version': 1, 'steps': [], 'progress': {'completed': 0, 'total': 0, 'skipped': 0}, 'mapping_changed': False}


def test_sdk_task_switch_ignores_delayed_responses_and_failure_is_not_empty(host_bundle):
    a, b = fixture_task('a'*32, '任务 A'), fixture_task('b'*32, '任务 B')
    broken = {'active': False}
    def real_tool(params):
        if broken['active']: return {'structuredContent': {'error': {'code': 'CONNECTION_FAILED', 'message': 'fixture offline'}}, 'isError': True}
        selected = {'a'*32: a, 'b'*32: b}.get(params['arguments'].get('options', {}).get('workflow_id'))
        return response(fixture_workspace(selected, workflows=[a, b]))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True); page = browser.new_page()
        page.expose_function('codepierRealTool', real_tool)
        page.expose_function('codepierHostTool', real_tool)
        app = mount(page, host_bundle, response({'workspace': {'project': 'Imago', 'project_id': 'p'}}))
        expect(app.get_by_label('选择任务')).to_have_value('')
        page.evaluate('''() => { window.codepierHostTool = async p => {
          const result = await window.codepierRealTool(p);
          if (p.arguments.options.workflow_id === 'a'.repeat(32)) await new Promise(resolve=>setTimeout(resolve,900));
          return result;
        }; }''')
        app.get_by_label('自动刷新任务状态').uncheck()
        app.get_by_label('选择任务').select_option('a'*32)
        app.get_by_label('选择任务').select_option('b'*32)
        expect(app.locator('.task-hero h2')).to_have_text('任务 B')
        page.wait_for_timeout(1200)
        expect(app.locator('.task-hero h2')).to_have_text('任务 B')
        broken['active'] = True
        app.get_by_role('button', name='刷新状态', exact=True).click()
        expect(app.locator('#app')).to_contain_text('刷新失败，已暂停自动刷新')
        expect(app.locator('.task-hero h2')).to_have_text('任务 B')
        expect(app.get_by_label('自动刷新任务状态')).not_to_be_checked()
        page.evaluate('()=>window.codepierBridge.sendToolCancelled({reason:"fixture finished"})')
        expect(app.locator('#app')).to_contain_text('后台操作没有因此自动取消')
        browser.close()


@pytest.mark.parametrize('terminal', ['succeeded', 'failed'])
def test_sdk_upload_recovery_uses_exact_key_and_locks_target(host_bundle, terminal):
    calls = []; imports = []
    identifier = 'c'*32
    def host_tool(params):
        calls.append(copy.deepcopy(params))
        name = params['name']
        if name == 'workspace': return response(fixture_workspace())
        if name == 'write':
            assert 'file' not in params['arguments']['options']
            assert params['arguments']['file']['file_id'] == 'fixture-file'
            imports.append({**copy.deepcopy(params['arguments']['options']), 'file':copy.deepcopy(params['arguments']['file']), 'idempotency_key':params['arguments']['idempotency_key']})
            if len(imports) == 1: return {'structuredContent': {'error': {'code': 'TRANSPORT_UNKNOWN', 'message': 'fixture uncertain'}}, 'isError': True}
            if len(imports) == 3: return {'structuredContent': {'error': {'code': 'INSUFFICIENT_SCOPE', 'message': 'fixture permission changed'}}, 'isError': True}
            return response({'pending': True, 'operation_id': identifier, 'state': 'running'})
        if name == 'process': return response({'operations':[{'id': identifier, 'operation_id': identifier,
            'tool': 'download_artifact', 'state': terminal, 'pending': False,
            'result': {'ok': terminal == 'succeeded', 'data': {'created': terminal == 'succeeded', 'path': imports[0]['path'], 'bytes': 4, 'sha256': 'd'*64}}}]})
        raise AssertionError(params)
    script = '<script>window.openai={uploadFile:async()=>({fileId:"fixture-file"}),getFileDownloadUrl:async()=>({downloadUrl:"https://files.oaiusercontent.com/fixture"})};</script>'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True); page = browser.new_page()
        page.expose_function('codepierHostTool', host_tool)
        html = (BASE/'web/mcp-apps/workspace-v1.html').read_text().replace('<script>', script+'<script>', 1)
        app = mount(page, host_bundle, response({'workspace': {'project': 'Imago', 'project_id': 'p', 'granted_scopes': ['read', 'write']}}), html=html)
        app.get_by_text('附件导入', exact=True).click()
        app.get_by_label('选择要保存到项目的文件').set_input_files({'name': 'sample.txt', 'mimeType': 'text/plain', 'buffer': b'test'})
        expect(app.get_by_label('项目内目标路径')).to_have_value('sample.txt')
        app.get_by_role('button', name='保存文件', exact=True).click()
        expect(app.get_by_role('button', name='用原回执恢复导入')).to_be_visible(timeout=10000)
        expect(app.get_by_label('项目内目标路径')).to_be_disabled()
        expect(app.get_by_role('button', name='保存文件', exact=True)).to_be_disabled()
        app.get_by_role('button', name='用原回执恢复导入').click()
        app.get_by_role('button', name='读取原导入结果').click()
        expect(app.locator('#app')).to_contain_text('已保存 sample.txt' if terminal == 'succeeded' else '导入没有确认成功')
        expect(app.get_by_label('项目内目标路径')).to_be_enabled()
        assert len(imports) == 2 and imports[0] == imports[1]
        assert [c['arguments']['operation_ids'][0] for c in calls if c['name'] == 'process'] == [identifier]
        app.get_by_role('button', name='保存文件', exact=True).click()
        expect(app.locator('#app')).to_contain_text('请求在执行前被拒绝')
        expect(app.get_by_label('项目内目标路径')).to_be_enabled()
        assert len(imports) == 3 and imports[2]['idempotency_key'] != imports[0]['idempotency_key']
        browser.close()


def test_sdk_active_polling_stops_when_card_is_cancelled(host_bundle):
    workflow = fixture_task('a'*32, '自动跟踪任务')
    calls = []
    def host_tool(params):
        calls.append(params['name'])
        return response(fixture_workspace(workflow, workflows=[workflow]))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True); page = browser.new_page()
        page.expose_function('codepierHostTool', host_tool)
        page.expose_function('codepierFixtureCount', lambda: len(calls))
        app = mount(page, host_bundle, response({'project_alias': 'Imago', 'project_id': 'p', 'workflow_id': workflow['workflow_id']}))
        expect(app.locator('.task-hero h2')).to_have_text('自动跟踪任务')
        page.wait_for_function('async()=>await window.codepierFixtureCount()>=2', timeout=12000)
        page.evaluate('()=>window.codepierBridge.sendToolCancelled({reason:"fixture stop"})')
        expect(app.locator('#app')).to_contain_text('后台操作没有因此自动取消')
        count = len(calls)
        page.wait_for_timeout(6500)
        assert len(calls) == count and set(calls) == {'workspace'}
        browser.close()


def test_coding_profile_app_tool_read_and_scope_enforcement(integrated_stack):
    s = integrated_stack
    headers = {'Authorization': 'Bearer ' + s.pat, 'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}
    result = s.client.post('/mcp?profile=coding', headers=headers, json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
        'params': {'name': 'workspace', 'arguments': {'operation':'dashboard','project': 'Imago'}}}).json()['result']
    assert not result['isError'] and result['structuredContent']['execution_started'] is False
    invalid = s.client.post('/api/grants', json={'label': 'dashboard-no-read', 'scopes': ['write'], 'projects': [s.project['id']], 'days': 1})
    assert not invalid.is_success  # The owner API itself requires the base read scope.
    grant = s.client.post('/api/grants', json={'label': 'dashboard-other-project', 'scopes': ['read'], 'projects': [s.projects[1]['id']], 'days': 1}).json()
    denied = s.mcp('workspace', {'project': 'Imago', 'operation': 'dashboard', 'options': {}}, token_value=grant['token'])
    assert denied['isError'] and denied['structuredContent']['error']['code'] == 'PROJECT_NOT_FOUND'
    s.client.delete('/api/grants/' + grant['grant_id'])


@pytest.mark.parametrize('delivery', ['immediate', 'pending'])
def test_sdk_terminal_source_denial_unlocks_without_replaying_upload(host_bundle, delivery):
    calls = []
    identifier = 'e' * 32
    failure = {'code': 'ARTIFACT_SOURCE_DENIED', 'message': '来源主机尚未批准',
               'source_host': 'unapproved.example.com', 'recovery': 'review_local_file_sources'}
    receipt = {'operation_id': identifier, 'id': identifier, 'tool': 'download_artifact',
               'state': 'failed', 'pending': False, 'result': {'ok': False, 'error': failure},
               'error': failure['message']}
    def host_tool(params):
        calls.append(copy.deepcopy(params))
        if params['name'] == 'workspace':
            return response(fixture_workspace())
        if params['name'] == 'write':
            assert 'file' in params['arguments'] and 'file' not in params['arguments']['options']
            return {'structuredContent': receipt, 'isError': True} if delivery == 'immediate' else response({'pending': True, 'operation_id': identifier, 'state': 'running'})
        if params['name'] == 'process':
            return {'structuredContent': {'operations': [receipt]}, 'isError': True}
        raise AssertionError(params)
    script = '<script>window.openai={uploadFile:async()=>({fileId:"fixture-file"}),getFileDownloadUrl:async()=>({downloadUrl:"https://unapproved.example.com/fixture?sig=PRIVATE_TICKET"})};</script>'
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.expose_function('codepierHostTool', host_tool)
        html = (BASE/'web/mcp-apps/workspace-v1.html').read_text().replace('<script>', script+'<script>', 1)
        app = mount(page, host_bundle, response({'workspace': {'project': 'Imago', 'project_id': 'p', 'granted_scopes': ['read', 'write']}}), html=html)
        app.get_by_text('附件导入', exact=True).click()
        app.get_by_label('选择要保存到项目的文件').set_input_files({'name': 'sample.txt', 'mimeType': 'text/plain', 'buffer': b'test'})
        app.get_by_role('button', name='保存文件', exact=True).click()
        if delivery == 'pending':
            app.get_by_role('button', name='读取原导入结果').click()
        expect(app.locator('#app')).to_contain_text('unapproved.example.com')
        expect(app.locator('#app')).to_contain_text('来源主机尚未批准')
        expect(app.get_by_label('项目内目标路径')).to_be_enabled()
        expect(app.get_by_role('button', name='保存文件', exact=True)).to_be_enabled()
        expect(app.get_by_role('button', name='用原回执恢复导入')).not_to_be_visible()
        assert len([call for call in calls if call['name'] == 'write']) == 1
        assert 'PRIVATE_TICKET' not in app.locator('#app').inner_text()
        browser.close()
