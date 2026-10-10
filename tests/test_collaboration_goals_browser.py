"""Real goal UI + isolated Hub/Agent fixtures; no model or native subscriber is used."""
import json
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from shared.util import atomic_json
from tests.collaboration_support import collaboration_stack, key  # noqa: F401

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


def scope(stack):
    return {'project': stack.project['id'], 'environment_id': 'production'}


def read(stack, kind='overview', **extra):
    return stack.must(stack.client.get('/api/collaboration', params={**scope(stack), 'kind': kind, **extra}))


def post(stack, operation, **extra):
    return stack.must(stack.client.post('/api/collaboration/' + operation,
        json={**scope(stack), 'idempotency_key': key(), **extra}))


def mcp(stack, name, **args):
    result = stack.mcp(name, {**scope(stack), 'idempotency_key': key(), **args})
    assert not result.get('isError'), result
    return result.get('structuredContent') or json.loads(result['content'][0]['text'])


def login(page, stack):
    page.goto(stack.url + '/#projects')
    page.fill('#username', 'admin')
    page.fill('#password', stack.password)
    page.click('#login-form button')
    expect(page.locator('#page h1')).to_have_text('项目映射')
    page.locator(f'[data-action="project-collaboration"][data-id="{stack.project["id"]}"]').click()
    page.locator('[data-cc-action="create-room"]').click()
    expect(page.locator('#cc-message-input')).to_be_enabled()


def open_goals(page):
    page.locator('[data-cc-view="jobs"]').click()
    expect(page.locator('.cc-coordination-list')).to_be_visible()


def snapshot(page, label):
    destination = os.getenv('CODEPIER_COLLABORATION_SCREENSHOTS')
    if destination:
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        page.screenshot(animations='disabled', path=str(directory / (
            page.context.browser.browser_type.name + '-' + label + '.png')))


def create_in_ui(page, stack, objective='修复并验证合成测试目标', capabilities=('read', 'execute')):
    page.locator('#cc-command [data-cc-action="goal-new"]').click()
    form = page.locator('#cc-goal-draft')
    form.locator('[name="objective"]').fill(objective)
    form.locator('[name="acceptance"]').fill('提交实际操作编号和测试证据。')
    form.locator(f'[name="participant_grant_ids"][value="{stack.grant}"]').check()
    for capability in capabilities:
        form.locator(f'[name="capabilities"][value="{capability}"]').check()
    snapshot(page, 'goal-create-' + ('mobile' if page.viewport_size['width'] < 640 else 'desktop'))
    form.locator('button[type="submit"]').click()
    expect(page.locator('#cc-drawer')).not_to_be_visible()
    expect(page.locator('#cc-feedback')).to_contain_text('执行尚未启用')
    return read(stack, 'coordination_goals')['items'][0]


def goal_payload(goal):
    return {k: goal[k] for k in ('conversation_id', 'objective', 'acceptance', 'project_ids',
        'participant_grant_ids', 'coordinator_grant_id', 'capabilities', 'duration_seconds', 'budget')}


def approve_in_ui(page, goal_id):
    open_goals(page)
    page.locator(f'.cc-coordination-list [data-cc-action="goal-review"][data-id="{goal_id}"]').click()
    form = page.locator('#cc-goal-approve')
    expect(form.locator('[name="confirm"]')).not_to_be_checked()
    footer = form.locator('button[type="submit"]')
    assert footer.evaluate('(n) => {const r=n.getBoundingClientRect(); return r.y >= 0 && r.bottom <= innerHeight;}')
    snapshot(page, 'goal-demo-approval-' + ('mobile' if page.viewport_size['width'] < 640 else 'desktop'))
    form.locator('[name="confirm"]').check()
    form.locator('button[type="submit"]').click()
    expect(page.locator('#cc-drawer')).not_to_be_visible()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_goal_draft_owner_confirmation_conflict_refresh_and_mobile(collaboration_stack, chat_browser_pool, tmp_path, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    shots = Path(os.getenv('CODEPIER_COLLABORATION_SCREENSHOTS', str(tmp_path / 'screenshots')))
    shots.mkdir(parents=True, exist_ok=True)
    try:
        login(page, stack)
        area = page.locator('#cc-message-input')
        area.fill('创建目标期间保留中文草稿')
        goal = create_in_ui(page, stack, '目标 <img src=x onerror="window.goalXss=true">')
        expect(area).to_have_value('创建目标期间保留中文草稿')
        assert goal['state'] == 'proposed'
        assert read(stack)['jobs'] == []
        assert read(stack, 'coordination_goal', id=goal['id'])['operations'] == []
        open_goals(page)
        expect(page.locator('.cc-coordination-list')).to_contain_text('尚未启用执行')
        assert not page.evaluate('Boolean(window.goalXss)')
        assert page.locator('.cc-coordination-list img').count() == 0
        review = page.locator(f'.cc-coordination-list [data-cc-action="goal-review"][data-id="{goal["id"]}"]')
        review.click()
        form = page.locator('#cc-goal-approve')
        expect(form.locator('[name="confirm"]')).not_to_be_checked()
        expect(page.locator('#cc-drawer')).to_contain_text('不是操作系统级的项目沙箱')
        expect(page.locator('#cc-drawer')).to_contain_text('独立的目标工作事件原生订阅')
        expect(page.locator('#cc-drawer')).to_contain_text('首位协调助手')
        snapshot(page, 'goal-approval-desktop')
        form.locator('button[type="submit"]').click()
        assert read(stack, 'coordination_goal', id=goal['id'])['goal']['state'] == 'proposed'
        form.locator('[data-cc-action="close-drawer"]').click()
        expect(review).to_be_focused()
        assert read(stack, 'coordination_goal', id=goal['id'])['goal']['state'] == 'proposed'
        review.click()
        revised = post(stack, 'goal-update', **{**goal_payload(goal), 'objective': '并发修订后的目标'},
                       goal_id=goal['id'], expected_version=goal['version'])['goal']
        form.locator('[name="confirm"]').check()
        form.locator('button[type="submit"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('重新审阅')
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        assert read(stack, 'coordination_goal', id=goal['id'])['goal']['state'] == 'proposed'
        review.click()
        expect(form).to_have_attribute('data-version', str(revised['version']))
        expect(form.locator('[name="confirm"]')).not_to_be_checked()
        form.locator('[name="confirm"]').check()
        form.locator('button[type="submit"]').click()
        expect(page.locator('#cc-feedback')).to_contain_text('已批准')
        assert read(stack, 'coordination_goal', id=goal['id'])['goal']['state'] == 'active'
        page.reload()
        expect(page.locator('.cc-chat-shell')).to_be_visible()
        open_goals(page)
        expect(page.locator('.cc-coordination-list')).to_contain_text('执行范围已批准')
        page.screenshot(animations='disabled', path=str(shots / f'{engine}-goals-desktop.png'))
        page.locator('[data-cc-view="discussion"]').click()
        area.fill('目标启用后普通消息仍不创建工作项')
        area.dispatch_event('compositionstart')
        area.dispatch_event('keydown', {'key': 'Enter', 'isComposing': True})
        initial_work = read(stack, 'coordination_goal', id=goal['id'])['work_items']
        assert len(initial_work) == 1 and initial_work[0]['responsibility'] == 'coordinator'
        assert initial_work[0]['required_capabilities'] == ['read']
        assert initial_work[0]['assignee_grant_id'] == stack.grant
        area.dispatch_event('compositionend')
        for width in (390, 360, 320, 390):
            page.set_viewport_size({'width': width, 'height': 844 if width == 390 else 800})
            page.wait_for_function("""() => {
                const root = document.querySelector('.collaboration');
                const bar = document.querySelector('.topbar');
                return Math.abs(parseFloat(getComputedStyle(root).getPropertyValue('--cc-topbar-height'))
                    - bar.getBoundingClientRect().height) < 1;
            }""")
            area.fill('手机草稿')
            area.press('Enter')
            expect(area).to_have_value('手机草稿\n')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            send = page.locator('#cc-command button[type="submit"]')
            assert send.evaluate('(n) => { const r=n.getBoundingClientRect(); return r.y >= 0 && r.bottom <= innerHeight && n.contains(document.elementFromPoint(r.x+r.width/2,r.y+r.height/2)); }')
            page.locator('[data-cc-more]').click()
            page.locator('.cc-room-menu [data-cc-action="goal-new"]').click()
            expect(page.locator('#cc-goal-draft')).to_be_visible()
            page.keyboard.press('Escape')
            expect(area).to_have_value('手机草稿\n')
            page.screenshot(animations='disabled', path=str(shots / f'{engine}-goals-mobile-{width}.png'))
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_goal_capability_gate_readonly_reviewer_and_connection_revocation(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    readonly = stack.must(stack.client.post('/api/grants', json={
        'label': '只读复核 fixture', 'scopes': ['read'], 'projects': [stack.project['id']], 'days': 1}))
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    try:
        login(page, stack)
        page.locator('#cc-command [data-cc-action="goal-new"]').click()
        form = page.locator('#cc-goal-draft')
        form.locator('[name="objective"]').fill('不同能力的助手连接协作')
        form.locator('[name="acceptance"]').fill('实际修复与独立复核证据')
        form.locator(f'[name="participant_grant_ids"][value="{readonly["grant_id"]}"]').check()
        form.locator('[name="capabilities"][value="execute"]').check()
        form.locator('button[type="submit"]').click()
        expect(form.locator('.cc-goal-gate')).to_contain_text('缺少')
        assert read(stack, 'coordination_goals')['items'] == []
        form.locator(f'[name="participant_grant_ids"][value="{stack.grant}"]').check()
        expect(form.locator('.cc-goal-gate')).to_be_empty()
        form.locator('button[type="submit"]').click()
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        goal = read(stack, 'coordination_goals')['items'][0]
        assert set(goal['participant_grant_ids']) == {readonly['grant_id'], stack.grant}
        approve_in_ui(page, goal['id'])
        page.locator(f'.cc-coordination-list [data-cc-action="goal-detail"][data-id="{goal["id"]}"]').click()
        expect(page.locator('.cc-goal-detail')).to_contain_text('不同能力的助手连接协作')
        stack.must(stack.client.delete('/api/grants/' + readonly['grant_id']))
        expect(page.locator('#cc-drawer')).not_to_be_visible(timeout=12000)
        expect(page.locator('#cc-drawer')).to_be_empty()
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_goal_room_project_add_does_not_widen_goal_and_back_keeps_draft(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 390, 'height': 844})
    page = context.new_page()
    try:
        login(page, stack)
        goal = create_in_ui(page, stack)
        area = page.locator('#cc-message-input')
        area.fill('跨项目目标范围需要明确修订')
        page.locator('[data-cc-action="add-project"]').click()
        form = page.locator('#cc-add-project')
        form.locator('[name="project"]').select_option(stack.projects[1]['id'])
        form.locator('[name="confirm"]').check()
        form.locator('button[type="submit"]').click()
        expect(page.locator('.cc-project-chip')).to_have_count(2)
        expect(area).to_have_value('跨项目目标范围需要明确修订')
        assert read(stack, 'coordination_goal', id=goal['id'])['goal']['project_ids'] == [stack.project['id']]
        page.locator(f'[data-cc-partition="{stack.projects[1]["id"]}"]').click()
        expect(page.locator(f'[data-cc-partition="{stack.projects[1]["id"]}"]')).to_have_attribute('aria-pressed', 'true')
        open_goals(page)
        page.locator(f'.cc-coordination-list [data-cc-action="goal-detail"][data-id="{goal["id"]}"]').click()
        expect(page.locator('.cc-goal-detail')).to_contain_text('新加入房间的项目不属于此目标')
        page.locator('#cc-drawer [data-cc-action="goal-edit"]').click()
        expect(page.locator(f'#cc-goal-draft [name="project_ids"][value="{stack.project["id"]}"]')).to_be_checked()
        expect(page.locator(f'#cc-goal-draft [name="project_ids"][value="{stack.projects[1]["id"]}"]')).not_to_be_checked()
        page.keyboard.press('Escape')
        page.locator('[data-cc-view="discussion"]').click()
        page.evaluate("navigate('projects')")
        expect(page.locator('#page h1')).to_have_text('项目映射')
        page.go_back()
        expect(area).to_have_value('跨项目目标范围需要明确修订')
        page.go_forward()
        expect(page.locator('#page h1')).to_have_text('项目映射')
        page.go_back()
        expect(area).to_have_value('跨项目目标范围需要明确修订')
        assert read(stack, 'coordination_goal', id=goal['id'])['goal']['state'] == 'proposed'
        approve_in_ui(page, goal['id'])
        page.locator(f'.cc-coordination-list [data-cc-action="goal-detail"][data-id="{goal["id"]}"]').click()
        page.locator('#cc-drawer [data-cc-action="goal-pause"]').click()
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        page.locator(f'.cc-coordination-list [data-cc-action="goal-review"][data-id="{goal["id"]}"]').click()
        snapshot(page, 'goal-approval-mobile')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_goal_work_cards_show_real_fixture_operations_results_and_dependencies(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    stack.stop_agent()
    stack.config['shell'] = {'enabled': True, 'projects': ['*'], 'command': ['/bin/sh', '-c']}
    atomic_json(stack.config_path, stack.config)
    stack.start_agent()
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    try:
        login(page, stack)
        goal = create_in_ui(page, stack, '整理项目检查的执行证据')
        approve_in_ui(page, goal['id'])
        work = post(stack, 'work-create', goal_id=goal['id'], objective='生成可复核的测试记录',
                    acceptance='实际退出码为 0', target_project_id=stack.project['id'],
                    assignee_grant_id=stack.grant, required_capabilities=['read', 'execute'])['work_item']
        dependent = post(stack, 'work-create', goal_id=goal['id'], objective='复核前置结果',
                         acceptance='引用已完成工作项', target_project_id=stack.project['id'],
                         assignee_grant_id=stack.grant, dependencies=[work['id']])['work_item']
        claimed = mcp(stack, 'collaboration_work_claim', goal_id=goal['id'], work_item_id=work['id'],
                      expected_version=work['version'])['work_item']
        lease = {'goal_id': goal['id'], 'work_item_id': work['id'],
                 'attempt': claimed['attempt'], 'fencing_token': claimed['fencing_token']}
        execution = mcp(stack, 'collaboration_work_execute', **lease, tool='exec',
                        arguments={'command': 'printf codepier-goal-fixture-evidence', 'timeout_seconds': 10})
        operation_id = execution['operation_id']
        operation = stack.poll(operation_id, timeout=20)
        assert operation['state'] == 'succeeded', operation
        mcp(stack, 'collaboration_work_result', **lease, outcome='succeeded',
            summary='实际输出与操作编号已保存，可供复核。未调用模型。',
            operation_ids=[operation_id], input_work_item_ids=[], limitations=['不代表原生宿主订阅已连接'])
        page.locator(f'.cc-coordination-list [data-cc-action="goal-detail"][data-id="{goal["id"]}"]').click()
        card = page.locator(f'[data-work-id="{work["id"]}"]')
        expect(card).to_contain_text('生成可复核的测试记录')
        expect(card).to_contain_text('已提交结果')
        expect(card).to_contain_text('未调用模型')
        expect(page.locator(f'[data-work-id="{dependent["id"]}"]')).to_contain_text('依赖：生成可复核的测试记录')
        snapshot(page, 'goal-work-desktop')
        card.locator('.cc-work-result').scroll_into_view_if_needed()
        snapshot(page, 'goal-demo-result-desktop')
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        snapshot(page, 'goal-work-mobile')
        card.locator('.cc-work-result').scroll_into_view_if_needed()
        snapshot(page, 'goal-demo-result-mobile')
        page.set_viewport_size({'width': 1440, 'height': 900})
        card.locator('summary').click()
        expect(card).to_contain_text(operation_id)
        held_operation = []

        def delay_operation(route):
            held_operation.append((route, route.fetch()))
            page.locator('html').evaluate('(n) => n.dataset.goalOperationHeld = "yes"')

        page.route('**/api/operations/' + operation_id, delay_operation)
        card.locator('[data-cc-action="goal-operation"]').click()
        expect(page.locator('html')).to_have_attribute('data-goal-operation-held', 'yes')
        page.keyboard.press('Escape')
        held_operation[0][0].fulfill(response=held_operation[0][1])
        page.unroute('**/api/operations/' + operation_id, delay_operation)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        page.locator(f'.cc-coordination-list [data-cc-action="goal-detail"][data-id="{goal["id"]}"]').click()
        card.locator('summary').click()
        card.locator('[data-cc-action="goal-operation"]').click()
        expect(page.locator('#cc-drawer')).to_contain_text('codepier-goal-fixture-evidence')
        page.locator('[data-cc-action="goal-detail"]').filter(has_text='返回目标步骤').click()
        subscription = page.locator('.cc-goal-subscription')
        subscription.locator('summary').click()
        active_goal = read(stack, 'coordination_goal', id=goal['id'])['goal']
        expect(subscription.locator('textarea')).to_contain_text(active_goal['approval_id'])
        expect(subscription.locator('textarea')).to_contain_text('重新批准后需要对应新批准')
        page.locator('#cc-goal-message [name="body_text"]').fill('请复核实际 fixture 证据')
        page.locator(f'#cc-goal-message [name="mention_grant_ids"][value="{stack.grant}"]').check()
        page.locator('#cc-goal-message button[type="submit"]').click()
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        assert len(read(stack, 'coordination_goal', id=goal['id'])['work_items']) == 3
        assert read(stack)['jobs'] == []
        page.locator('[data-cc-view="discussion"]').click()
        page.locator('#cc-message-input').fill('把这次检查的结论留在这里，方便后续复核。')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('.cc-feed')).to_contain_text('把这次检查的结论留在这里')
        snapshot(page, 'goal-demo-chat-desktop')
        page.set_viewport_size({'width': 390, 'height': 844})
        snapshot(page, 'goal-demo-chat-mobile')
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_goal_all_pages_and_stale_page_cannot_replace_new_navigation(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    try:
        login(page, stack)
        conversation = read(stack)['conversation']['id']
        for index in range(31):
            post(stack, 'goal-create', conversation_id=conversation,
                 objective=f'分页目标 {index:02d}', acceptance='隔离规划记录，不启动执行',
                 project_ids=[stack.project['id']], participant_grant_ids=[stack.grant],
                 coordinator_grant_id=stack.grant, capabilities=['read'])
        open_goals(page)
        expect(page.locator('.cc-coordination-list .cc-goal-summary')).to_have_count(31)
        held = []

        def delay_second_page(route):
            query = parse_qs(urlsplit(route.request.url).query)
            if query.get('kind') == ['coordination_goals'] and query.get('cursor') and not held:
                held.append((route, route.fetch()))
                page.locator('html').evaluate('(n) => n.dataset.goalPageHeld = "yes"')
            else:
                route.continue_()

        page.route('**/api/collaboration?*', delay_second_page)
        page.locator('[data-cc-view="discussion"]').click()
        expect(page.locator('html')).to_have_attribute('data-goal-page-held', 'yes')
        page.evaluate("navigate('projects')")
        expect(page.locator('#page h1')).to_have_text('项目映射')
        held[0][0].fulfill(response=held[0][1])
        page.unroute('**/api/collaboration?*', delay_second_page)
        expect(page.locator('#page h1')).to_have_text('项目映射')
        expect(page.locator('.cc-coordination-list')).to_have_count(0)
        page.go_back()
        expect(page.locator('#cc-message-input')).to_be_enabled()
        open_goals(page)
        expect(page.locator('.cc-coordination-list .cc-goal-summary')).to_have_count(31)
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_goal_delayed_detail_dismissal_and_authority_projection_recheck(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    try:
        login(page, stack)
        goal = create_in_ui(page, stack, '关闭后不得重开目标详情', capabilities=('read',))
        open_goals(page)
        held = []

        def delay_detail(route):
            query = parse_qs(urlsplit(route.request.url).query)
            if query.get('kind') == ['coordination_goal'] and not held:
                held.append((route, route.fetch()))
                page.locator('html').evaluate('(n) => n.dataset.goalDetailHeld = "yes"')
            else:
                route.continue_()

        page.route('**/api/collaboration?*', delay_detail)
        page.locator(f'.cc-coordination-list [data-cc-action="goal-detail"][data-id="{goal["id"]}"]').click()
        expect(page.locator('html')).to_have_attribute('data-goal-detail-held', 'yes')
        expect(page.locator('#cc-drawer')).to_be_visible()
        page.keyboard.press('Escape')
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        held[0][0].fulfill(response=held[0][1])
        page.unroute('**/api/collaboration?*', delay_detail)
        # Waiting on the API completion verifies the dismissed asynchronous read
        # cannot put its private detail back into the now-closed dialog.
        expect(page.locator('.cc-coordination-list [data-cc-action="goal-detail"]').first).to_be_enabled()
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('#cc-drawer')).not_to_contain_text('关闭后不得重开目标详情')

        # Exercise the actual module against deterministic authority response
        # ordering: pre-revocation goals may not be reused after new options.
        result = page.evaluate("""async () => {
          let phase = 0, released = false, optionsCalls = 0, goalsCalls = 0, release;
          const state = { snapshot: {room:{},can_manage:true,capabilities:{coordination_goals:true}},
            generation:1, project:'A', conversation:'room', session:'session', space:'space', drawerEpoch:0 };
          const oldOptions = {can_approve:true, participants:[{grant_id:'g',project_ids:['A','B'],scopes:['read']}]};
          const newOptions = {can_approve:true, participants:[{grant_id:'g',project_ids:['A'],scopes:['read']}]};
          const secret = {id:'old',objective:'Historical project B evidence',acceptance:'review',
            project_ids:['A','B'],participant_grant_ids:['g'],version:1,state:'proposed'};
          const ui = CodePierCollaborationGoals.create({state,E:(v)=>String(v??''),empty:(v)=>v,
            field:()=>'',button:()=>'',mutation:()=>{},showDrawer:()=>{},closeDrawer:()=>{},
            projectLabel:(v)=>v,roomProjects:()=>[],when:(v)=>v,
            getRecord:async(kind)=>{
              if(kind==='coordination_options'){
                optionsCalls++;
                if(phase===0)return oldOptions;
                if(!released)return new Promise((resolve)=>release=resolve);
                return newOptions;
              }
              goalsCalls++;
              return {items:phase && !released?[secret]:[]};
            }});
          await ui.load();
          phase=1;
          const pending=ui.load();
          await Promise.resolve();
          const beforeRelease=goalsCalls;
          released=true;release(newOptions);
          await pending;
          return {stale:ui.list().includes('Historical project B evidence'),beforeRelease,optionsCalls,goalsCalls};
        }""")
        assert result['beforeRelease'] == 1 and not result['stale'], result
        assert result['optionsCalls'] >= 3, result
    finally:
        context.close()


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_goal_approval_refresh_keeps_old_surface_inert_until_replacement(collaboration_stack, chat_browser_pool, engine):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1440, 'height': 900})
    page = context.new_page()
    try:
        login(page, stack)
        goal = create_in_ui(page, stack, '验证批准后的页面刷新', capabilities=('read',))
        open_goals(page)
        page.locator(f'.cc-coordination-list [data-cc-action="goal-review"][data-id="{goal["id"]}"]').click()
        approval_saved = []
        held = []
        late_details = []
        fresh_requests = []
        timeline = []
        releasing = False
        observed_at = time.monotonic()
        expected_scope = {}

        def request_scope(request):
            parsed = urlsplit(request.url)
            query = parse_qs(parsed.query)
            return parsed.path == '/api/collaboration' and query.get('kind') == ['coordination_goals'] and all(
                query.get(name) == value for name, value in expected_scope.items())

        def observe_request(request):
            parsed = urlsplit(request.url)
            if parsed.path.startswith('/api/collaboration'):
                query = parse_qs(parsed.query)
                timeline.append({'event': 'request', 'kind': query.get('kind', [''])[0],
                                 'elapsed': round(time.monotonic() - observed_at, 3)})
                if releasing and request_scope(request):
                    fresh_requests.append(request)

        def observe_response(response):
            parsed = urlsplit(response.url)
            if parsed.path.startswith('/api/collaboration'):
                timeline.append({'event': 'response', 'kind': parse_qs(parsed.query).get('kind', [''])[0],
                                 'status': response.status, 'elapsed': round(time.monotonic() - observed_at, 3)})

        page.on('request', observe_request)
        page.on('response', observe_response)

        def save_approval(route):
            response = route.fetch()
            approval_saved.append(True)
            route.fulfill(response=response)

        def hold_refresh(route):
            query = parse_qs(urlsplit(route.request.url).query)
            if approval_saved and query.get('kind') == ['coordination_goal']:
                late_details.append(route.request.url)
            if approval_saved and query.get('kind') == ['coordination_options'] and not held:
                held.append((route, route.fetch()))
                page.locator('html').evaluate('(n) => n.dataset.approvalRefreshHeld = "yes"')
            else:
                route.continue_()

        page.route('**/api/collaboration/goal-approve', save_approval)
        page.route('**/api/collaboration?*', hold_refresh)
        form = page.locator('#cc-goal-approve')
        form.locator('[name="confirm"]').check()
        form.locator('button[type="submit"]').click()
        expect(page.locator('html')).to_have_attribute('data-approval-refresh-held', 'yes')
        # Saving has succeeded, but the replacement is intentionally not ready.
        # Keep the modal present and visibly busy rather than exposing old
        # enabled controls that could be clicked and then removed by this render.
        expect(page.locator('#cc-drawer')).to_be_visible()
        expect(page.locator('.collaboration')).to_have_attribute('aria-busy', 'true')
        assert page.locator('.collaboration').evaluate('(n) => n.inert')
        assert page.locator('#cc-drawer').evaluate('(n) => n.inert')
        page.locator('.cc-coordination-list [data-cc-action="goal-detail"]').dispatch_event('click')
        assert late_details == []
        # Releasing options does not complete refresh: goals must be fetched next.
        # Accept only a request started after this release, for this same scope
        # and render generation, then retain the original five-second DOM checks.
        query = parse_qs(urlsplit(held[0][0].request.url).query)
        expected_scope = {name: query.get(name) for name in ('project', 'environment_id', 'conversation_id')}
        assert expected_scope['project'] == [stack.project['id']]
        assert expected_scope['conversation_id']
        generation = page.evaluate('S.renderSeq')
        releasing = True
        with page.expect_response(lambda response: response.request in fresh_requests, timeout=15000) as refreshed:
            with page.expect_event('requestfinished', predicate=lambda request: request in fresh_requests,
                                   timeout=15000) as finished:
                held[0][0].fulfill(response=held[0][1])
        response = refreshed.value
        assert response.request == finished.value and len(fresh_requests) == 1
        assert response.status == 200
        assert page.evaluate('S.renderSeq') == generation
        assert any(item['id'] == goal['id'] and item['state'] == 'active' for item in response.json()['items'])
        page.unroute('**/api/collaboration?*', hold_refresh)
        page.unroute('**/api/collaboration/goal-approve', save_approval)
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('.cc-coordination-list')).to_contain_text('执行范围已批准')
        page.locator(f'.cc-coordination-list [data-cc-action="goal-detail"][data-id="{goal["id"]}"]').click()
        expect(page.locator('.cc-goal-detail')).to_contain_text('验证批准后的页面刷新')
        page.locator('#cc-drawer [data-cc-action="goal-pause"]').click()
        expect(page.locator('#cc-drawer')).not_to_be_visible()
        expect(page.locator('.cc-coordination-list')).to_contain_text('已暂停')
    finally:
        if 'timeline' in locals():
            print('APPROVAL_REFRESH_TIMELINE', json.dumps(timeline), flush=True)
        context.close()
