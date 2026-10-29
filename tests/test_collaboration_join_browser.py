"""Real panel/Hub/MCP joining flow, with only clipboard and host delivery mocked.

Every project, grant, code and event is a disposable fixture. The host adapter
uses the production subscription/challenge/delivery code with a fake receiver;
it is never presented as proof that a real ChatGPT conversation received data.
"""
import asyncio
import base64
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from concurrent.futures import ThreadPoolExecutor

import pytest
from playwright.sync_api import expect

from hub.collaboration.config import CollaborationConfig
from hub.collaboration.events import EventService
from hub.collaboration.service import CollaborationService
from hub.runtime import Runtime
from hub.store import Store
from tests.test_collaboration_events import Receiver
from tests.collaboration_support import collaboration_stack, key  # noqa: F401
from tests.test_mcp_tasks_http import modern

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


def login(page, stack):
    page.goto(stack.url + '/#projects')
    page.fill('#username', 'admin')
    page.fill('#password', stack.password)
    page.click('#login-form button')
    expect(page.locator('#page h1')).to_have_text('项目映射')


def open_room(page, stack):
    page.locator(f'[data-action="project-collaboration"][data-id="{stack.project["id"]}"]').click()
    expect(page.locator('#page h1')).to_have_text('协作中心')
    page.locator('[data-cc-action="create-room"]').click()
    expect(page.locator('#cc-command')).to_be_visible()
    page.locator('[data-cc-open-joins]').click()
    # CPJ notification setup is intentionally secondary to the new CPD task-dot flow.
    page.locator('.cc-dot-legacy > summary').click()
    expect(page.locator('#cc-join-slot')).to_be_visible()


def overview(stack):
    return stack.must(stack.client.get('/api/collaboration', params={
        'project': stack.project['id'], 'environment_id': 'production'}))


def create_slot(page, label, kind='dot'):
    page.locator('#cc-join-slot [name="label"]').fill(label)
    page.locator('#cc-join-slot [name="kind"]').select_option(kind)
    page.locator('#cc-join-slot button[type="submit"]').evaluate(
        '(button) => { button.click(); button.click(); }')
    expect(page.locator('#cc-join-slot [name="label"]')).to_have_value('')
    return page.locator('[data-cc-slot]').filter(has=page.get_by_role('heading', name=label, exact=True))


def refresh(page):
    if not page.locator('[data-cc-action="refresh"]').is_visible():
        page.locator('[data-cc-more]').click()
    page.locator('[data-cc-action="refresh"]').click()
    expect(page.locator('#cc-join-slot')).to_be_visible()
    expect(page.locator('#cc-feedback')).to_have_text('状态已刷新。')
    open_monitor_details(page)


def open_monitor_details(page):
    expect(page.locator('#cc-join-slot')).to_be_visible()
    expect(page.locator('.collaboration')).not_to_have_attribute('aria-busy', 'true')
    # Monitor coverage remains an advanced, optional path for these legacy tests.
    for summary in page.locator('.cc-join details > summary').filter(
            has_text='高级：项目监控订阅与测试').all():
        if summary.locator('..').get_attribute('open') is None:
            summary.click()



class JoinCompletionDiagnostic:
    """Bounded failure-only metadata; never record URLs, payloads or credentials."""

    endpoints = (
        '/api/devices', '/api/projects', '/api/grants', '/api/collaboration/status',
        '/api/collaboration', '/api/collaboration/conversations',
        '/api/collaboration/join-slot-control',
    )
    kinds = ('overview', 'timeline', 'members', 'delegation_policies',
             'teams', 'coordination_options', 'coordination_goals')

    def __init__(self, page):
        self.page = page
        self.started = time.monotonic()
        self.network = []
        self.requests = {}
        self.callbacks = {
            'request': lambda request: self.record('request', request),
            'response': lambda response: self.record('response', response.request, response.status),
            'requestfinished': lambda request: self.record('finished', request),
            'requestfailed': lambda request: self.record('failed', request),
        }
        page.evaluate("""({endpoints, kinds}) => {
            const started = performance.now(), session = S.session, space = S.space_id;
            const initial = document.querySelector('.collaboration')?.dataset;
            const scope = initial ? [initial.project, initial.environment, initial.conversation] : [];
            const stages = [], pointers = [], restores = [];
            let serial = 0;
            const append = (items, value) => { items.push(value); if (items.length > 96) items.shift(); };
            const at = () => Math.round(performance.now() - started);
            const record = (stage, id, metadata = {}) =>
                append(stages, {stage, id, ms: at(), sequence: S.renderSeq, ...metadata});
            const wrap = (owner, name, stage, describe = () => ({})) => {
                const original = owner[name];
                const wrapped = function () {
                    'use strict';
                    const id = ++serial;
                    let metadata;
                    try { metadata = describe(arguments); }
                    catch { metadata = {metadataUnavailable: true}; }
                    record(stage + ':start', id, metadata);
                    let result;
                    try { result = Reflect.apply(original, this, arguments); }
                    catch (error) { record(stage + ':error', id, metadata); throw error; }
                    if (result && typeof result.then === 'function')
                        result.then(() => record(stage + ':end', id, metadata),
                            () => record(stage + ':error', id, metadata));
                    else record(stage + ':end', id, metadata);
                    return result;
                };
                owner[name] = wrapped;
                restores.push(() => { if (owner[name] === wrapped) owner[name] = original; });
            };
            wrap(window, 'api', 'api', args => {
                const parsed = new URL(args[0], location.href);
                const kind = parsed.searchParams.get('kind') || '';
                return {endpoint: endpoints.includes(parsed.pathname) ? parsed.pathname : 'other',
                    kind: kinds.includes(kind) ? kind : 'other',
                    method: ['GET', 'HEAD', 'POST'].includes(args[1]?.method || 'GET')
                        ? args[1]?.method || 'GET' : 'other'};
            });
            for (const name of ['loadBasics', 'renderPage', 'uiWaitForPagePointer'])
                wrap(window, name, name);
            for (const name of ['html', 'detach', 'clear', 'open'])
                wrap(CodePierCollaboration, name, 'collaboration.' + name);
            const pointer = event => append(pointers, {
                event: event.type, ms: at(), id: event.pointerId, buttons: event.buttons,
                tracked: uiPagePointers.size, inert: !!event.target.closest?.('[inert]')
            });
            for (const event of ['pointerdown', 'pointerup', 'pointercancel'])
                window.addEventListener(event, pointer, true);
            window.joinCompletionDiagnostic = {
                snapshot: () => {
                    const root = document.querySelector('.collaboration'), data = root?.dataset;
                    const rendering = window.joinTestRender;
                    return {stages, pointers, state: {
                        sequence: S.renderSeq, collaborationPage: S.page === 'collaboration',
                        sameSession: S.session === session, sameSpace: S.space_id === space,
                        sameScope: !!data && [data.project, data.environment, data.conversation]
                            .every((value, index) => value === scope[index]),
                        rootPresent: !!root, rootInert: !!root?.inert,
                        rootBusy: root?.getAttribute('aria-busy') === 'true',
                        pointers: [...uiPagePointers].slice(0, 16), pointerCount: uiPagePointers.size,
                        pointerWaiters: uiPagePointerWaiters.size,
                        renderCalls: rendering?.calls ?? 0,
                        renderSequence: rendering?.sequence ?? null,
                        renderSettled: !!rendering?.settled, renderRejected: !!rendering?.error
                    }};
                },
                restore: () => {
                    for (const event of ['pointerdown', 'pointerup', 'pointercancel'])
                        window.removeEventListener(event, pointer, true);
                    for (const restore of restores.reverse()) restore();
                }
            };
        }""", {'endpoints': self.endpoints, 'kinds': self.kinds})
        for event, callback in self.callbacks.items():
            page.on(event, callback)

    def record(self, event, request, status=None):
        parsed = urlsplit(request.url)
        if parsed.path not in self.endpoints:
            return
        if request not in self.requests:
            if len(self.requests) >= 128:
                return
            self.requests[request] = len(self.requests) + 1
        kind = parse_qs(parsed.query).get('kind', [''])[0]
        self.network.append({
            'event': event, 'id': self.requests[request],
            'endpoint': parsed.path, 'kind': kind if kind in self.kinds else 'other',
            'method': request.method if request.method in ('GET', 'HEAD', 'POST') else 'other',
            'status': status, 'ms': round((time.monotonic() - self.started) * 1000),
        })
        del self.network[:-96]

    def report(self):
        # A closed/crashed page must not replace the original test exception.
        try:
            browser = self.page.evaluate('() => window.joinCompletionDiagnostic.snapshot()')
        except Exception:
            browser = {'snapshotUnavailable': True}
        print('JOIN_COMPLETION_DIAGNOSTIC', json.dumps({
            'network': self.network, 'browser': browser}, ensure_ascii=True), flush=True)

    def close(self):
        for event, callback in self.callbacks.items():
            self.page.remove_listener(event, callback)
        if not self.page.is_closed():
            self.page.evaluate('() => window.joinCompletionDiagnostic.restore()')


def queue_join_test(page, card, before_render_completion=None):
    """Bind one native intent to its original authoritative replacement."""
    diagnostics = JoinCompletionDiagnostic(page)
    failed = False
    posts, responses, fresh_requests, fresh_responses = [], [], [], []
    root = page.locator('.collaboration')
    expected_scope = {
        'project': root.get_attribute('data-project'),
        'environment_id': root.get_attribute('data-environment'),
        'conversation_id': root.get_attribute('data-conversation') or '',
    }
    page.evaluate("""() => {
        const original = window.renderPage;
        const observed = window.joinTestRender = {
            beforeSequence: S.renderSeq, sequence: null, settled: false, error: null, calls: 0
        };
        observed.finished = new Promise(resolve => { observed.complete = resolve; });
        const wrapper = window.renderPage = function () {
            'use strict';
            window.renderPage = original;
            observed.calls++;
            const rendering = Reflect.apply(original, this, arguments);
            observed.sequence = S.renderSeq;
            observed.promise = rendering;
            const complete = () => { observed.settled = true; observed.complete(); };
            rendering.then(complete, error => {
                observed.error = String(error);
                complete();
            });
            return rendering;
        };
        observed.restore = () => {
            if (window.renderPage === wrapper) window.renderPage = original;
        };
    }""")

    def requested(request):
        parsed = urlsplit(request.url)
        if (parsed.path == '/api/collaboration/join-slot-control'
                and request.method == 'POST' and request.post_data_json.get('action') == 'test'):
            posts.append(request)
        elif (responses and responses[0].status == 200 and parsed.path == '/api/collaboration'
              and request.method == 'GET'):
            query = parse_qs(parsed.query)
            if (not fresh_requests and query.get('kind') == ['overview']
                    and all(query.get(name, ['']) == [value] for name, value in expected_scope.items())):
                # The first same-scope overview after the POST belongs to its
                # replacement; busy/detach fence polling until that render.
                # Reserve synchronously, before any response callback can run.
                # Older requests and later polls cannot replace this receipt.
                fresh_requests.append(request)

    def responded(response):
        if response.request in posts:
            responses.append(response)
        if response.request in fresh_requests:
            fresh_responses.append(response)

    page.on('request', requested)
    page.on('response', responded)
    try:
        # HTTP 200 is not replacement completion: html() must finish its
        # authoritative reads while the old DOM stays inert. Keep the native
        # one-intent gesture and the original five-second DOM assertion.
        with page.expect_response(lambda response: response.request in posts, timeout=5000) as queued:
            card.locator('[data-cc-action="join-test"]').dblclick(timeout=5000)
        assert queued.value.status == 200
        result = queued.value.json()
        payload = queued.value.request.post_data_json
        assert result['message'] == '测试已排队。请到对应聊天核对事件；接收回执不代表已读。'
        assert result['slot']['id'] == payload['slot_id']
        assert result['slot']['version'] == payload['expected_version'] + 1
        assert payload['project'] == expected_scope['project']
        assert payload['environment_id'] == expected_scope['environment_id']
        if before_render_completion is not None:
            before_render_completion(posts, result)
        # This is the exact renderer started by the original POST, bounded by
        # the existing API request budget, not a longer feedback expectation.
        page.evaluate("""async () => {
            let timer;
            try {
                await Promise.race([
                    window.joinTestRender.finished,
                    new Promise((_, reject) => {
                        timer = setTimeout(() => reject(new Error(
                            'Original join renderer exceeded the existing 15000ms API budget')), 15000);
                    })
                ]);
            } finally { clearTimeout(timer); }
        }""")
        observed = page.evaluate("""() => ({calls: window.joinTestRender.calls,
            error: window.joinTestRender.error, before: window.joinTestRender.beforeSequence,
            sequence: window.joinTestRender.sequence, current: S.renderSeq})""")
        assert observed['calls'] == 1 and observed['error'] is None
        assert observed['sequence'] == observed['before'] + 1 == observed['current']
        assert len(posts) == len(responses) == len(fresh_requests) == len(fresh_responses) == 1
        refreshed = fresh_responses[0]
        assert refreshed.status == 200 and refreshed.request == fresh_requests[0]
        fresh_slot = next(slot for slot in refreshed.json()['join_slots'] if slot['id'] == payload['slot_id'])
        assert fresh_slot['version'] == result['slot']['version']
        assert not fresh_slot['connection_complete']
        assert not any(route['chat_receipt_confirmed'] for route in fresh_slot['routes'])
        expect(page.locator('#cc-feedback')).to_contain_text('测试已排队')
        assert not root.evaluate('(node) => node.inert')
        assert all(root.get_attribute('data-' + name) == value
                   for name, value in (('project', expected_scope['project']),
                                       ('environment', expected_scope['environment_id']),
                                       ('conversation', expected_scope['conversation_id'])))
        assert len(posts) == 1
        return result
    except Exception:
        failed = True
        try:
            diagnostics.report()
        except Exception:
            pass  # Reporting failure cannot mask the original assertion/error.
        raise
    finally:
        page.remove_listener('request', requested)
        page.remove_listener('response', responded)
        try:
            if not page.is_closed():
                page.evaluate('window.joinTestRender.restore()')
        except Exception:
            if not failed:
                raise
        finally:
            try:
                diagnostics.close()
            except Exception:
                if not failed:
                    raise


def join_existing_connection(stack, code):
    result = modern(stack, 'tools/call', {'name': 'collaboration_join', 'arguments': {
        'code': code, 'idempotency_key': key()}}, token=stack.pat).json()['result']
    assert not result.get('isError'), result
    joined = result['structuredContent']
    assert joined['registered'] and not joined['permissions_changed']
    assert not joined['worker_authorized'] and not joined['chat_identity_verified']
    return joined


def screenshot(page, tmp_path, name):
    directory = Path(os.getenv('CODEPIER_COLLABORATION_SCREENSHOTS', str(tmp_path / 'screenshots')))
    directory.mkdir(parents=True, exist_ok=True)
    page.evaluate("() => { document.activeElement?.blur(); window.scrollTo({top: 0, behavior: 'instant'}); }")
    page.screenshot(path=str(directory / name), full_page=True, animations='disabled', caret='hide')
    page.screenshot(path=str(directory / name.replace('.png', '-viewport.png')),
                    full_page=False, animations='disabled', caret='hide')
    if page.viewport_size['width'] == 390:
        page.locator('.cc-join-card').first.screenshot(
            path=str(directory / name.replace('.png', '-card.png')), animations='disabled', caret='hide')


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_join_panel_create_copy_expiry_join_and_revoke(collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 1000})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        login(page, stack)
        open_room(page, stack)
        expect(page.locator('.cc-join-intro')).to_contain_text('确认一次处理范围，复制一条接入指令')
        label = '协调 dot <img src=x onerror="window.joinXss=true">'
        card = create_slot(page, label)
        expect(card).to_be_visible()
        assert len(overview(stack)['join_slots']) == 1  # double submit is one intent
        assert card.locator('img').count() == 0 and not page.evaluate('Boolean(window.joinXss)')
        instruction = card.locator('.cc-join-instruction').input_value()
        code = card.locator('.cc-join-code').inner_text()
        assert '@CodePier' in instruction and code in instruction
        for boundary in ('已有的 CodePier 授权', '原生确认', '实际调用错误', '项目共享'):
            assert boundary in instruction
        page.evaluate("""() => {
            Object.defineProperty(navigator, 'clipboard', {configurable: true,
                value: {writeText: async text => {window.joinCopied = text;}}});
        }""")
        card.locator('[data-cc-action="join-copy"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('已复制')
        assert page.evaluate('window.joinCopied') == instruction
        page.evaluate("""() => {
            navigator.clipboard.writeText = async () => { throw new Error('clipboard unavailable'); };
        }""")
        card.locator('[data-cc-action="join-copy"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('已选中完整指令')
        assert card.locator('.cc-join-instruction').evaluate(
            '(area) => area.selectionStart === 0 && area.selectionEnd === area.value.length')
        card.locator('[data-cc-action="join-select"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('系统复制')
        screenshot(page, tmp_path, f'{engine}-join-desktop.png')

        page.set_viewport_size({'width': 390, 'height': 844})
        work = create_slot(page, '分析 Work', 'work_cloud')
        expect(work).to_be_visible()
        assert 'Cloud' in work.locator('.cc-join-instruction').input_value()
        assert len(overview(stack)['join_slots']) == 2
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        screenshot(page, tmp_path, f'{engine}-join-mobile.png')
        page.reload()
        expect(page.locator('#page h1')).to_have_text('协作中心')
        page.locator('[data-cc-view="agents"]').click()
        expect(card.locator('.cc-join-code')).to_have_text(code)

        # Expiration is real persisted state, not a mocked overview response.
        dot = next(item for item in overview(stack)['join_slots'] if item['label'] == label)
        store = Store(stack.hubdir)
        try:
            store.execute('UPDATE collaboration_join_slots SET code_expires_at=? WHERE id=?',
                          (time.time() + 2, dot['id']))
        finally:
            store.close()
        refresh(page)
        card.locator('.cc-join-instruction').focus()
        # Local expiry clears a focused invitation without losing other drafts.
        expect(card.locator('.cc-status').first).to_have_text('加入码已过期')
        expect(card.locator('.cc-join-instruction')).to_have_count(0)
        card.locator('[data-cc-action="join-refresh_code"]').click()
        expect(card.locator('.cc-join-code')).not_to_have_text(code)
        new_code = card.locator('.cc-join-code').inner_text()
        expired = modern(stack, 'tools/call', {'name': 'collaboration_join', 'arguments': {
            'code': code, 'idempotency_key': key()}}, token=stack.pat).json()['result']
        assert expired['isError']
        joined = join_existing_connection(stack, new_code)
        assert joined['slot']['id'] == dot['id']
        assert len(joined['subscription_requests']) == 4
        page.reload()
        expect(page.locator('#page h1')).to_have_text('协作中心')
        page.locator('[data-cc-view="agents"]').click()
        open_monitor_details(page)
        expect(card.locator('.cc-status').first).to_have_text('等待宿主订阅')
        expect(card.locator('.cc-join-code')).to_have_count(0)
        resumed = card.locator('.cc-join-instruction').input_value()
        assert dot['id'] in resumed and 'connector_id' in resumed and new_code not in resumed
        page.evaluate("""() => {
            Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
                writeText: async () => { throw new Error('clipboard unavailable'); }
            }});
        }""")
        card.get_by_role('button', name='复制继续订阅指令').click()
        expect(page.locator('#cc-feedback')).to_contain_text('已选中完整指令')
        expect(card.locator('.cc-join-coverage')).to_contain_text('0 / 4')
        expect(card).to_contain_text('缺少事件')
        expect(card.locator('.cc-join-confirm')).to_have_count(0)
        assert overview(stack)['agents'] == [] and overview(stack)['jobs'] == []

        # Reusing a code does not establish a second chat or worker permission.
        assert join_existing_connection(stack, new_code)['slot']['id'] == dot['id']
        assert len(overview(stack)['join_slots']) == 2
        card.locator('[data-cc-action="join-revoke"]').click()
        expect(card.locator('.cc-status').first).to_have_text('位置已撤销')
        assert overview(stack)['join_slots'][1]['state'] == 'revoked'
        denied = modern(stack, 'tools/call', {'name': 'collaboration_join', 'arguments': {
            'code': new_code, 'idempotency_key': key()}}, token=stack.pat).json()['result']
        assert denied['isError']
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_join_panel_drafts_navigation_and_logout_fence(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        login(page, stack)
        open_room(page, stack)
        page.locator('#cc-join-slot [name="label"]').fill('Only this session draft')
        page.locator('#cc-join-slot [name="kind"]').select_option('work_cloud')
        page.locator('[data-cc-view="jobs"]').click()
        page.locator('[data-cc-view="agents"]').click()
        expect(page.locator('#cc-join-slot [name="label"]')).to_have_value('Only this session draft')
        expect(page.locator('#cc-join-slot [name="kind"]')).to_have_value('work_cloud')
        # Scope changes discard join drafts and never persist invitation text.
        page.locator('[data-cc-more]').click()
        page.locator('.cc-header-tools [data-cc-action=\"room-settings\"]').click()
        page.locator('#cc-scope [name="environment"]').fill('staging')
        page.locator('#cc-scope button[type="submit"]').click()
        expect(page.locator('[data-cc-action="create-room"]')).to_be_visible()
        page.locator('#cc-scope [name="environment"]').fill('production')
        page.locator('#cc-scope button[type="submit"]').click()
        expect(page.locator('#cc-join-slot [name="label"]')).to_have_value('')
        assert not page.evaluate("""() => [...Object.values(localStorage), ...Object.values(sessionStorage)]
            .some(value => value.includes('Only this session draft'))""")

        # The server saves once while its response is interrupted. A later page
        # must retain a newer draft, and retrying the original intent reuses it.
        page.locator('.cc-dot-legacy > summary').click()
        held = []
        def hold_creation(route):
            if not held:
                response = route.fetch()
                held.append((route, response))
                page.locator('html').evaluate('(node) => { node.dataset.joinServerSaved = "true"; }')
            else:
                route.continue_()
        page.route('**/api/collaboration/join-slot', hold_creation)
        page.locator('#cc-join-slot [name="label"]').fill('Interrupted server record')
        page.locator('#cc-join-slot button[type="submit"]').click()
        expect(page.locator('html')).to_have_attribute('data-join-server-saved', 'true')
        assert len(overview(stack)['join_slots']) == 1
        page.evaluate("navigate('projects')")
        expect(page.locator('#page h1')).to_have_text('项目映射')
        page.locator(f'[data-action="project-collaboration"][data-id="{stack.project["id"]}"]').click()
        expect(page.locator('#cc-join-slot')).to_be_visible()
        page.locator('#cc-join-slot [name="label"]').fill('A newer draft')
        held[0][0].fulfill(response=held[0][1])
        expect(page.locator('#cc-join-slot [name="label"]')).to_have_value('A newer draft')
        page.locator('#cc-join-slot [name="label"]').fill('Interrupted server record')
        page.locator('#cc-join-slot button[type="submit"]').click()
        expect(page.locator('#cc-join-slot [name="label"]')).to_have_value('')
        assert len(overview(stack)['join_slots']) == 1
        page.unroute('**/api/collaboration/join-slot', hold_creation)
        page.locator('#cc-join-slot [name="label"]').fill('Do not restore after logout')
        # Delay only a clipboard operation at the host boundary. Signing out
        # must fence its eventual completion and clear all in-memory drafts.
        page.evaluate("""() => {
            Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
                writeText: () => new Promise(resolve => { window.joinFinishCopy = resolve;
                    document.documentElement.dataset.joinCopyPending = 'true'; })
            }});
        }""")
        page.locator('[data-cc-action="join-copy"]').click()
        expect(page.locator('html')).to_have_attribute('data-join-copy-pending', 'true')
        page.locator('[data-action="logout"]').evaluate('(button) => button.click()')
        expect(page.locator('#login-form')).to_be_visible()
        page.evaluate('window.joinFinishCopy()')
        expect(page.locator('.collaboration')).to_have_count(0)
        page.fill('#username', 'admin')
        page.fill('#password', stack.password)
        page.click('#login-form button')
        expect(page.locator('.shell')).to_be_visible()
        page.evaluate('(project) => CodePierCollaboration.open(project)', stack.project['id'])
        expect(page.locator('#page h1')).to_have_text('协作中心')
        page.locator('[data-cc-view="agents"]').click()
        expect(page.locator('#cc-join-slot [name="label"]')).to_have_value('')
        expect(page.locator('#cc-feedback')).not_to_contain_text('已复制')
        assert not errors, errors
    finally:
        context.close()


def fixture_host(stack, slot, requests, deliver=True):
    # Playwright owns the sync caller's event loop; isolate the host adapter.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(seed_fixture_host, stack, slot, requests, deliver).result(timeout=20)


def seed_fixture_host(stack, slot, requests, deliver):
    """Persist real verified subscriptions with only external delivery replaced."""
    store = Store(stack.hubdir)
    service = CollaborationService(Runtime(store), CollaborationConfig(enabled=True, events_enabled=True))
    receiver = Receiver()
    events = EventService(service, receiver)
    service.events = events
    room = store.one('SELECT * FROM collaboration_rooms WHERE id=?', (
        store.one('SELECT room_id FROM collaboration_join_slots WHERE id=?', (slot['id'],))['room_id'],))
    principal = service.grant_reader(room, stack.grant)
    secret = 'whsec_' + base64.b64encode(secrets.token_bytes(32)).decode()

    async def seed():
        subscriptions = []
        for request in requests:
            result = await events.subscribe({**request, 'delivery': {
                'mode': 'webhook', 'url': 'https://fixture.example.invalid/chat', 'secret': secret}}, principal)
            subscriptions.append(store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (result['id'],)))
        if not deliver:
            return
        # Production permits one in-flight delivery per callback endpoint.
        # Queue and reserve each test atomically, then finish its original
        # delivery before queuing the next same-endpoint subscription. This
        # keeps the separate Hub loop from claiming fixture work without
        # bypassing endpoint serialization or holding a DB lock across await.
        for row in subscriptions:
            with store.transaction():
                events.queue_test(room, row)
                deliveries = events.reserve()
            assert len(deliveries) == 1
            delivery = deliveries[0]
            assert delivery['subscription_id'] == row['id']
            await events.deliver(delivery)
            receipt = store.one('SELECT state,attempt FROM mcp_event_deliveries WHERE id=?',
                                (delivery['delivery_id'],))
            assert receipt['state'] == 'accepted' and receipt['attempt'] == 1

    try:
        asyncio.run(seed())
        accepted = [json.loads(body) for _, body, _ in receiver.requests if json.loads(body).get('eventId')]
        assert len(accepted) == (len(requests) if deliver else 0)
    finally:
        store.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_join_panel_partial_routes_and_explicit_event_confirmation(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 1000})
    page = context.new_page()
    try:
        login(page, stack)
        open_room(page, stack)
        card = create_slot(page, 'Route verification Work', 'work_cloud')
        joined = join_existing_connection(stack, card.locator('.cc-join-code').inner_text())
        fixture_host(stack, joined['slot'], joined['subscription_requests'][:1], deliver=False)
        refresh(page)
        expect(card.locator('.cc-status').first).to_have_text('订阅尚未完整')
        expect(card.locator('.cc-join-coverage')).to_contain_text('1 / 2')
        resumed = card.locator('.cc-join-instruction').input_value()
        assert 'codepier.monitor.status_changed.v1' in resumed
        assert 'codepier.collaboration.task_available.v1' not in resumed
        expect(card.locator('.cc-join-confirm')).to_have_count(0)
        fixture_host(stack, joined['slot'], joined['subscription_requests'][:1])
        refresh(page)
        expect(card.locator('.cc-status').first).to_have_text('测试已获接收回执')
        assert not overview(stack)['join_slots'][0]['connection_complete']
        expect(card.locator('.cc-event-id')).to_have_count(1)
        checkbox = card.locator('.cc-join-confirm input[type="checkbox"]')
        expect(checkbox).not_to_be_checked()
        card.locator('.cc-join-confirm button').click()
        assert overview(stack)['join_slots'][0]['status'] == 'test_delivered'
        checkbox.check()
        card.locator('.cc-join-confirm button').click()
        expect(card.locator('.cc-status').first).to_have_text('部分订阅已确认收件')
        assert not overview(stack)['join_slots'][0]['connection_complete']

        fixture_host(stack, joined['slot'], joined['subscription_requests'][1:])
        refresh(page)
        expect(card.locator('.cc-join-coverage')).to_contain_text('2 / 2')
        expect(card.locator('.cc-event-id')).to_have_count(2)
        expect(card.locator('.cc-join-confirm input[type="checkbox"]')).not_to_be_checked()
        card.locator('.cc-join-confirm input[type="checkbox"]').check()
        card.locator('.cc-join-confirm button').click()
        expect(card.locator('.cc-status').first).to_have_text('用户已确认聊天收件')
        current = overview(stack)['join_slots'][0]
        assert current['connection_complete'] and not current['chat_identity_verified']
        assert not current['worker_authorized'] and overview(stack)['agents'] == []
        # Rotating the synthetic host route credentials produces a new receipt
        # generation. Previously checked consent must never carry over.
        fixture_host(stack, joined['slot'], joined['subscription_requests'])
        refresh(page)
        expect(card.locator('.cc-join-confirm input[type="checkbox"]')).not_to_be_checked()
        assert not overview(stack)['join_slots'][0]['connection_complete']
        tests_sent = []
        page.on('request', lambda request: tests_sent.append(request.post_data_json)
                if request.url.endswith('/api/collaboration/join-slot-control')
                and request.post_data_json.get('action') == 'test' else None)
        queue_join_test(page, card)
        current = overview(stack)['join_slots'][0]
        assert not current['connection_complete']
        assert len(tests_sent) == 1
        assert not any(route['chat_receipt_confirmed'] for route in current['routes'])
        expect(card.locator('.cc-join-confirm')).to_have_count(0)
    finally:
        context.close()

@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_join_test_waits_for_original_authoritative_replacement(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 1000})
    page = context.new_page()
    held, saved = [], []
    try:
        login(page, stack)
        open_room(page, stack)
        card = create_slot(page, 'Delayed authoritative join test', 'work_cloud')
        joined = join_existing_connection(stack, card.locator('.cc-join-code').inner_text())
        fixture_host(stack, joined['slot'], joined['subscription_requests'])
        refresh(page)
        card.locator('.cc-join-confirm input[type="checkbox"]').check()
        card.locator('.cc-join-confirm button').click()
        expect(card.locator('.cc-status').first).to_have_text('用户已确认聊天收件')
        refresh(page)

        def saved_test(response):
            if (urlsplit(response.url).path == '/api/collaboration/join-slot-control'
                    and response.request.post_data_json.get('action') == 'test'):
                assert response.status == 200
                saved.append(response)

        def hold_basics(route):
            if saved and not held:
                held.append(route)
                page.locator('html').evaluate('(node) => { node.dataset.joinReplacementHeld = "true"; }')
            else:
                route.continue_()

        page.on('response', saved_test)
        # Hold the original replacement's first dependency, not a later options
        # read that requires unrelated basics/status/overview reads to finish
        # within the five-second marker expectation. The test is about keeping
        # stale DOM inert until that same replacement finishes, not read latency.
        page.route(stack.url + '/api/devices', hold_basics)
        scope = {name: page.locator('.collaboration').get_attribute('data-' + name)
                 for name in ('project', 'environment', 'conversation')}

        def complete_original(posts, result):
            expect(page.locator('html')).to_have_attribute('data-join-replacement-held', 'true')
            assert len(held) == len(saved) == len(posts) == 1
            assert held[0].request.method == 'GET'
            assert held[0].request.url == stack.url + '/api/devices'
            rendering = page.evaluate("""() => ({
                calls: window.joinTestRender.calls, before: window.joinTestRender.beforeSequence,
                sequence: window.joinTestRender.sequence, current: S.renderSeq,
                settled: window.joinTestRender.settled
            })""")
            assert rendering['calls'] == 1 and not rendering['settled']
            assert rendering['sequence'] == rendering['before'] + 1 == rendering['current']
            assert scope['project'] == stack.project['id'] and scope['environment'] == 'production'
            assert all(page.locator('.collaboration').get_attribute('data-' + name) == value
                       for name, value in scope.items())
            response = held[0].fetch()
            assert response.status == 200
            # The mutation already cleared consent, but the old DOM cannot be
            # used while the authoritative replacement is deliberately held.
            assert not result['slot']['connection_complete']
            assert not any(route['chat_receipt_confirmed'] for route in result['slot']['routes'])
            expect(page.locator('#cc-feedback')).to_have_text('状态已刷新。')
            expect(page.locator('.collaboration')).to_have_attribute('aria-busy', 'true')
            assert page.locator('.collaboration').evaluate('(node) => node.inert')
            assert page.evaluate('window.joinTestRender.settled === false')
            card.locator('[data-cc-action="join-test"]').evaluate('(button) => button.click()')
            assert len(posts) == 1
            held[0].fulfill(response=response)

        queue_join_test(page, card, complete_original)
        # Keep this disposable interceptor until context.close(); removing it
        # can race a later poll whose continue_ callback is still in flight.
        expect(card.locator('.cc-join-confirm')).to_have_count(0)
        current = overview(stack)['join_slots'][0]
        assert not current['connection_complete']
        assert not any(route['chat_receipt_confirmed'] for route in current['routes'])
        assert len(saved) == 1
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_join_panel_old_overview_cannot_replace_new_scope(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    try:
        login(page, stack)
        open_room(page, stack)
        create_slot(page, 'Private to original project')
        held = []
        def hold_overview(route):
            if not held:
                # Reserve the real request before any network work can yield.
                held.append(route)
                page.locator('html').evaluate('(node) => { node.dataset.joinOldOverviewReady = "true"; }')
            else:
                route.continue_()
        page.route('**/api/collaboration?*kind=overview*', hold_overview)
        if not page.locator('[data-cc-action="refresh"]').is_visible():
            page.locator('[data-cc-more]').click()
        page.evaluate("""() => {
            const originalRenderPage = window.renderPage;
            window.joinOldRenderingSettled = false;
            window.renderPage = function () {
                'use strict';
                window.renderPage = originalRenderPage;
                const rendering = Reflect.apply(originalRenderPage, this, arguments);
                window.joinOldRendering = rendering;
                const settled = () => { window.joinOldRenderingSettled = true; };
                rendering.then(settled, settled);
                return rendering;
            };
        }""")
        with page.expect_request('**/api/collaboration?*kind=overview*') as old_request:
            page.locator('[data-cc-action="refresh"]').click()
        expect(page.locator('html')).to_have_attribute('data-join-old-overview-ready', 'true')
        assert len(held) == 1 and held[0].request.url == old_request.value.url
        assert old_request.value.method == 'GET'
        assert page.evaluate("""() => Boolean(window.joinOldRendering &&
            typeof window.joinOldRendering.then === 'function')""")
        response = held[0].fetch()
        assert response.ok
        assert any(slot['label'] == 'Private to original project'
                   for slot in response.json()['join_slots'])
        assert page.evaluate('window.joinOldRenderingSettled === false')
        page.evaluate('(project) => CodePierCollaboration.open(project)', stack.projects[1]['id'])
        expect(page.locator('#cc-scope [name="project"]')).to_have_value(stack.projects[1]['id'])
        expect(page.locator('[data-cc-action="create-room"]')).to_be_visible()
        held[0].fulfill(response=response)
        page.unroute('**/api/collaboration?*kind=overview*', hold_overview)
        # Wait for the actual old renderer, not merely HTTP response receipt.
        # detach() releases its busy marker before this renderer has settled.
        page.evaluate('() => window.joinOldRendering')
        assert page.evaluate('window.joinOldRenderingSettled === true')
        expect(page.locator('[data-cc-slot]')).to_have_count(0)
        expect(page.locator('#page')).not_to_contain_text('Private to original project')
        page.locator('#cc-scope [name="environment"]').fill('staging')
        page.locator('#cc-scope button[type="submit"]').click()
        expect(page.locator('#cc-scope [name="environment"]')).to_have_value('staging')
        expect(page.locator('#cc-feedback')).to_be_empty()
        expect(page.locator('[data-cc-slot]')).to_have_count(0)
        # Returning through browser history re-enters the current scope without
        # resurrecting the aborted response or the previous project's code.
        page.evaluate("navigate('projects')")
        expect(page.locator('#page h1')).to_have_text('项目映射')
        page.go_back()
        expect(page.locator('#page h1')).to_have_text('协作中心')
        expect(page.locator('#cc-scope [name="project"]')).to_have_value(stack.projects[1]['id'])
        expect(page.locator('#cc-scope [name="environment"]')).to_have_value('staging')
        expect(page.locator('[data-cc-slot]')).to_have_count(0)
    finally:
        context.close()



@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_join_panel_unknown_creation_reload_and_logout_during_hash(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    try:
        login(page, stack)
        open_room(page, stack)
        held = []
        def lose_creation_response(route):
            response = route.fetch()
            held.append((route, response))
            page.locator('html').evaluate('(node) => { node.dataset.joinSavedBeforeReload = "true"; }')
        page.route('**/api/collaboration/join-slot', lose_creation_response)
        label = 'Unknown outcome before reload'
        page.locator('#cc-join-slot [name="label"]').fill(label)
        page.locator('#cc-join-slot button[type="submit"]').click()
        expect(page.locator('html')).to_have_attribute('data-join-saved-before-reload', 'true')
        saved = overview(stack)['join_slots']
        assert len(saved) == 1
        # Only opaque request IDs are persisted. Neither the draft nor the code
        # enters browser storage, even while the server result is unknown.
        values = page.evaluate('() => Object.values(sessionStorage)')
        assert all(label not in value and saved[0]['join_code'] not in value for value in values)
        held[0][0].abort()
        page.unroute('**/api/collaboration/join-slot', lose_creation_response)
        page.reload()
        expect(page.locator('#page h1')).to_have_text('协作中心')
        page.locator('[data-cc-view="agents"]').click()
        expect(page.locator('#cc-join-slot [name="label"]')).to_have_value('')
        create_slot(page, label)
        retried = overview(stack)['join_slots']
        assert len(retried) == 1 and retried[0]['id'] == saved[0]['id']
        assert page.evaluate("""() => Object.keys(sessionStorage)
            .filter(key => key.startsWith('codepier-collaboration-request:')).length""") == 0

        # The async digest must not send an old intent under a new/ended login,
        # or repopulate request storage after the logout cleanup.
        posts = []
        page.on('request', lambda request: posts.append(request.url)
                if request.url.endswith('/api/collaboration/join-slot') else None)
        page.evaluate("""() => {
            window.joinOriginalDigest = crypto.subtle.digest.bind(crypto.subtle);
            crypto.subtle.digest = async (...args) => {
                const result = await window.joinOriginalDigest(...args);
                await new Promise(resolve => {
                    window.joinFinishHash = resolve;
                    document.documentElement.dataset.joinHashPending = 'true';
                });
                return result;
            };
        }""")
        page.locator('#cc-join-slot [name="label"]').fill('Must never be created after logout')
        page.locator('#cc-join-slot button[type="submit"]').click()
        expect(page.locator('html')).to_have_attribute('data-join-hash-pending', 'true')
        page.locator('[data-action="logout"]').evaluate('(button) => button.click()')
        expect(page.locator('#login-form')).to_be_visible()
        page.evaluate("""() => {
            crypto.subtle.digest = window.joinOriginalDigest;
            window.joinFinishHash();
        }""")
        expect(page.locator('.collaboration')).to_have_count(0)
        assert posts == []
        assert page.evaluate("""() => Object.keys(sessionStorage)
            .filter(key => key.startsWith('codepier-collaboration-request:')).length""") == 0
        assert len(overview(stack)['join_slots']) == 1
    finally:
        context.close()

