"""Offline browser interaction tests of the shipped call-log component."""
from tests.browser_launch import launch_browser
import copy
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from playwright.sync_api import sync_playwright
ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
const S={session:{user_id:'owner'},page:'audit',auditMode:'operations',renderSeq:0,auditSource:'',auditStatus:'',auditQuery:'',auditOffset:0};
const ACTIVE_STATES=['queued','running','reconnecting','cancelling','unknown'];
const TERMINAL_STATES=['succeeded','failed','cancelled','needs_review','interrupted'];
const stateNames={running:'执行中',succeeded:'已完成',failed:'失败'};
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const json=v=>JSON.stringify(v,null,2),icon=()=>'',badge=s=>`<span class="badge">${esc(stateNames[s]||s)}</span>`;
const empty=t=>`<p>${esc(t)}</p>`,heading=(t,s,b,actions='')=>`<h1>${esc(t)}</h1>${actions}`,uiHelp=(t,b)=>`<details><summary>${esc(t)}</summary><p>${esc(b)}</p></details>`;
const download=(name,content,type)=>{window.exported={name,content,type};};
const toast=m=>{window.lastToast=m;},copy=v=>{window.lastCopied=v;};
const api=async(path,options={})=>window.readCallLog(path);
async function renderPage(){const seq=++S.renderSeq;const html=await CodePierCallLog.html(seq);document.querySelector('#page').innerHTML=html;CodePierCallLog.bind();}
'''


@pytest.fixture(params=['chromium', 'webkit'])
def log_page(request):
    requests=[]
    rows=[{'id':'a'*32,'tool':'shell_exec','actor':'mcp:test:ChatGPT','state':'running','created':100,'updated':101,'elapsed_ms':1000,'summary':'command=pytest -q <img src=x onerror=alert(1)>','alias':'MCP','device_name':'Node'},
          {'id':'b'*32,'tool':'fs_read','actor':'panel:admin','state':'succeeded','created':90,'updated':91,'elapsed_ms':1000,'summary':'path=README.md','alias':'MCP','device_name':'Node'}]
    details={rows[0]['id']:{**rows[0],'attempts':1,'args_summary':{'command':'pytest -q','env':'<redacted>'},'output':'output line\npassword=<redacted>','result':{'ok':True,'data':{'exit_code':0}},'timing':{'wait_ms':20,'execution_ms':150},'trace':{'current':{'reason':'本机正在执行'},'events':[]}}}
    with sync_playwright() as p:
        executable=shutil.which('chromium') if request.param == 'chromium' else None
        browser=launch_browser(getattr(p, request.param), headless=True,**({'executable_path':executable} if executable else {}))
        page=browser.new_page(viewport={'width':1100,'height':900})
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        def read(path):
            requests.append(path)
            if urlsplit(path).path=='/api/call-log':
                return {'operations':copy.deepcopy(rows),'next_cursor':None,'projects':[{'id':'p1','alias':'MCP'}],'tools':['fs_read','shell_exec']}
            return copy.deepcopy(details.get(path.rsplit('/',1)[-1],{}))
        page.expose_function('readCallLog',read)
        page.set_content('<html lang="zh-CN"><body><main id="page"></main></body></html>')
        page.add_script_tag(content=HARNESS)
        page.add_style_tag(path=str(ROOT/'web/call-log.css'))
        page.add_script_tag(path=str(ROOT/'web/call-log.js'))
        page.evaluate('renderPage()')
        yield page,rows,details,requests
        page.evaluate('CodePierCallLog.clear()')
        assert not errors
        browser.close()


def test_native_expansion_copy_xss_and_mobile(log_page):
    page,rows,details,requests=log_page
    entry=page.locator('[data-call-id]').first
    assert not page.locator('img').count()
    assert entry.locator('.call-project strong').inner_text() == 'MCP'
    entry.locator('summary').first.focus()
    page.keyboard.press('Enter')
    page.get_by_text('实际执行（本次 Agent 计时）',exact=True).wait_for()
    assert len([u for u in requests if u.endswith(rows[0]['id'])])==1
    entry.get_by_role('button',name='复制调用详情').click()
    assert '<redacted>' in page.evaluate('window.lastCopied')
    assert 'pytest -q' in page.evaluate('window.lastCopied')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    entry.locator('summary').first.click()
    entry.locator('summary').first.click()
    assert len([u for u in requests if u.endswith(rows[0]['id'])])==1


def test_live_updates_do_not_move_open_rows_and_pause(log_page):
    page,rows,details,requests=log_page
    entry=page.locator('[data-call-id]').first
    entry.locator('summary').first.click()
    entry.locator('.call-facts').wait_for()
    before=entry.bounding_box()['y']
    rows.insert(0,{**rows[1],'id':'c'*32,'created':200})
    rows[1]['state']='failed'
    details['a'*32]['state']='failed'
    details['a'*32]['error']='command failed'
    page.evaluate('CodePierCallLog.refresh(true)')
    assert page.locator('[data-call-id]').count()==2
    assert entry.bounding_box()['y']==before
    assert entry.get_attribute('open') is not None
    assert page.locator('#call-new').is_visible()
    assert '失败' in entry.locator('.call-state').inner_text()
    page.get_by_role('button',name='暂停实时更新').click()
    count=len(requests)
    page.evaluate('CodePierCallLog.refresh(true)')
    assert len(requests)==count
    page.get_by_role('button',name='列表有更新，显示最新').click()
    page.wait_for_function('document.querySelectorAll("[data-call-id]").length===3')


def test_filters_and_search_reset_pagination(log_page):
    page,_,_,requests=log_page
    page.locator('#call-project').select_option('p1')
    page.wait_for_function('S.callLog.project==="p1" && document.querySelector("#call-project").value==="p1"')
    page.locator('#audit-query').fill('path/a_%.py')
    page.locator('#call-search').get_by_role('button',name='搜索',exact=True).click()
    page.wait_for_function('S.auditQuery==="path/a_%.py"')
    page.wait_for_timeout(100)
    query=parse_qs(urlsplit(requests[-1]).query)
    assert query['q']==['path/a_%.py']
    assert query['project']==['p1']
    assert page.evaluate('S.callLog.history.length')==0


def test_output_scroll_and_nested_folds_survive_update(log_page):
    page,rows,details,_=log_page
    data=details[rows[0]['id']]
    data['output']='\n'.join(f'line {i}' for i in range(1000))
    entry=page.locator('[data-call-id]').first
    entry.locator('summary').first.click()
    output=entry.locator('[data-call-section="output"]')
    output.locator('summary').click()
    output.locator('pre').evaluate('(el)=>{el.scrollTop=500}')
    data['output']+='\nnew output'
    data['updated']=102
    page.evaluate('CodePierCallLog.refresh(true)')
    assert output.get_attribute('open') is not None
    assert output.locator('pre').evaluate('(el)=>el.scrollTop')==500


def test_automatic_latest_when_not_reading_and_session_clear(log_page):
    page,rows,details,_=log_page
    rows.insert(0,{**rows[1],'id':'c'*32,'created':200})
    page.evaluate('CodePierCallLog.refresh(true)')
    assert page.locator('[data-call-id]').count()==3
    assert '3 项' in page.locator('#call-count').inner_text()
    page.evaluate("S.session={user_id:'another'};CodePierCallLog.clear();document.querySelector('#page').replaceChildren()")
    assert page.locator('[data-call-id]').count()==0
    assert page.evaluate('S.callLog===null')


def test_manual_detail_refresh_and_truncated_trace_are_usable(log_page):
    page,rows,details,_=log_page
    data=details[rows[0]['id']]
    data['state']='succeeded'
    data['output']='old output'
    data['trace']['events']=[{'stage':'waiting_project','source':'agent','blocked_by':'<truncated>'}, '<truncated>']
    entry=page.locator('[data-call-id]').first
    entry.locator('summary').first.click()
    entry.locator('.call-facts').wait_for()
    data['output']='new output after manual refresh'
    # WebKit does not focus buttons on pointer clicks; exercise keyboard focus restoration.
    refresh = entry.get_by_role('button',name='刷新详情',exact=True)
    refresh.focus()
    refresh.press('Enter')
    page.wait_for_function('document.querySelector("[data-call-section=output] pre").textContent.includes("new output")')
    assert entry.get_by_role('button',name='刷新详情',exact=True).evaluate('(el)=>el===document.activeElement')


def test_network_error_preserves_rows_and_retry(log_page):
    page,_,_,_=log_page
    entry=page.locator('[data-call-id]').first
    page.evaluate('window.savedRead=window.readCallLog;window.readCallLog=()=>Promise.reject(new Error("offline"));void 0')
    entry.locator('summary').first.click()
    entry.get_by_role('button',name='重试详情').wait_for()
    assert page.locator('[data-call-id]').count()==2
    page.evaluate('CodePierCallLog.refresh(true)')
    assert '连接暂不可用' in page.locator('#call-sync').inner_text()
    page.evaluate('window.readCallLog=window.savedRead;void 0')
    entry.get_by_role('button',name='重试详情').click()
    entry.locator('.call-facts').wait_for()


def test_late_detail_after_logout_cannot_repopulate_the_page(log_page):
    page,_,_,_=log_page
    page.evaluate('window.readCallLog=()=>new Promise(resolve=>window.resolveOldDetail=resolve);void 0')
    page.locator('[data-call-id]').first.locator('summary').first.click()
    page.wait_for_function('typeof window.resolveOldDetail==="function"')
    page.evaluate('CodePierCallLog.clear();S.session=null;document.querySelector("#page").innerHTML="signed out";window.resolveOldDetail({id:"a".repeat(32),args_summary:{command:"private old command"}})')
    page.wait_for_timeout(30)
    assert page.locator('#page').inner_text()=='signed out'


def test_export_is_explicitly_limited_to_the_visible_sanitized_page(log_page):
    page,rows,_,_=log_page
    page.get_by_role('button',name='导出本页调用',exact=True).click()
    exported=page.evaluate('JSON.parse(window.exported.content)')
    assert exported['scope']=='visible_page' and len(exported['calls'])==len(rows)
    assert 'args_summary' not in exported['calls'][0]


def test_project_names_stay_visible_with_long_names_and_missing_metadata(log_page):
    page, rows, _, _ = log_page
    rows[0]['alias'] = 'LongProject-' * 20 + '<img src=x onerror=alert(1)>'
    rows[1]['alias'] = None
    page.evaluate('renderPage()')
    for width in [320, 390, 1280]:
        page.set_viewport_size({'width': width, 'height': 900})
        project = page.locator('.call-project strong').first
        assert project.inner_text() == rows[0]['alias']
        assert page.locator('.call-project strong').nth(1).inner_text() == '未记录'
        assert not page.locator('img').count()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
        assert project.evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')


def test_project_colors_are_distinct_stable_and_readable(log_page):
    import re
    page, rows, _, _ = log_page
    template = dict(rows[0])
    rows[:] = [{**template, 'id': f'{i + 1:02x}' + '0' * 30, 'project_id': f'project-{i}',
                'summary': 'command=pytest -q', 'created': 1790307840,
                'alias': ['ProjectAlpha', 'ProjectBeta', 'ProjectGamma', 'MCP'][i] if i < 4 else f'Project {i}'} for i in range(12)]
    page.add_style_tag(path=str(ROOT/'web/tokens.css'))
    page.add_style_tag(path=str(ROOT/'web/styles.css'))
    page.evaluate('CodePierCallLog.clear();renderPage()')
    def colors():
        return page.locator('.call-entry').evaluate_all('''nodes => Object.fromEntries(nodes.map(node => {
          const project=node.querySelector('.call-project');
          return [node.dataset.callId, project.style.getPropertyValue('--call-project-hue')];
        }))''')
    original = colors()
    assert len(set(original.values())) == 12
    rows.reverse()
    page.evaluate('renderPage()')
    assert colors() == original
    page.evaluate('CodePierCallLog.clear();renderPage()')
    assert colors() == original
    for theme in ['light', 'dark']:
        page.evaluate('(theme)=>document.documentElement.dataset.appearance=theme', theme)
        def luminance(color):
            rgb = [int(part) / 255 for part in re.findall(r'\d+', color)[:3]]
            linear = [x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in rgb]
            return sum(x * weight for x, weight in zip(linear, [0.2126, 0.7152, 0.0722]))
        for item in page.locator('.call-project').all():
            style = item.evaluate('(el)=>({fg:getComputedStyle(el).color,bg:getComputedStyle(el).backgroundColor})')
            a, b = sorted([luminance(style['fg']), luminance(style['bg'])])
            assert (b + 0.05) / (a + 0.05) >= 4.5, (theme, style)
    # Page changes keep each project's assigned color.
    rows[:] = sorted(rows, key=lambda row: row['id'])[:4]
    page.evaluate('renderPage()')
    assert all(original[key] == value for key, value in colors().items())
    page.set_viewport_size({'width': 1100, 'height': 900})
    screenshots = Path('.work/audit-projects'); screenshots.mkdir(parents=True, exist_ok=True)
    for theme in ['light', 'dark']:
        page.evaluate('(theme)=>document.documentElement.dataset.appearance=theme', theme)
        page.locator('.call-log').screenshot(path=str(screenshots/f'project-colors-{theme}.png'))


def test_authorization_snapshot_is_visible_and_escaped(log_page):
    page, rows, details, requests = log_page
    rows[0]['actor'] = 'mcp:0123456789abcdef0123456789abcdef:开发授权:旧名称<img src=x onerror=alert(1)>'
    details[rows[0]['id']]['actor'] = rows[0]['actor']
    page.evaluate('renderPage()')
    entry = page.locator('[data-call-id]').first
    grant = entry.locator('.call-authorization')
    assert '开发授权:旧名称<img' in grant.inner_text()
    assert grant.locator('code').get_attribute('title') == '0123456789abcdef0123456789abcdef'
    assert not page.locator('img').count()
    entry.locator('summary').first.click()
    detail = entry.locator('.call-authorization-detail')
    detail.wait_for()
    assert '0123456789abcdef0123456789abcdef' in detail.inner_text()
    assert '开发授权:旧名称' in detail.inner_text()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')


@pytest.mark.parametrize('actor,expected', [
    ('mcp:grant-legacy', '名称未记录'),
    ('mcp::', '编号未知'),
    ('', '授权未知'),
    ('panel:admin', '面板操作（不使用 MCP 授权）'),
])
def test_authorization_missing_history_is_not_invented(log_page, actor, expected):
    page, rows, details, requests = log_page
    rows[0]['actor'] = actor
    page.evaluate('renderPage()')
    assert expected in page.locator('.call-authorization').first.inner_text()

def test_maintenance_origin_is_separate_from_authorization(log_page):
    page, rows, details, requests = log_page
    row = rows[0]
    row.update(tool='project_knowledge_build', trigger_source='system_maintenance',
               trigger_reason='automatic_refresh', model_invocation=False, state='failed')
    details[row['id']].update(row, knowledge_mode='incremental',
                             knowledge_stats={'entries_count': 1363, 'changed_entries': 0,
                                              'reused_entries': 1363, 'skipped_files': 1},
                             error='地图维护失败', output='maintenance log')
    page.evaluate('CodePierCallLog.reset()')
    entry = page.locator('[data-call-id]').first
    assert entry.locator('.call-tool strong').inner_text() == '项目地图更新'
    assert '自动维护' in entry.locator('.call-tool').inner_text()
    assert '无需模型' in entry.locator('.call-tool').inner_text()
    assert 'ChatGPT' in entry.locator('.call-authorization').inner_text()
    assert '失败' in entry.locator('.call-state').inner_text()
    entry.locator('summary').first.click()
    entry.locator('.call-facts').wait_for()
    facts = entry.locator('.call-facts').inner_text()
    assert '授权依据（调用时记录）' in facts
    assert '本地程序 · 无模型调用' in facts
    assert '增量' in facts and '1363' in facts
    assert 'project_knowledge_build' in facts
    assert entry.locator('.call-error').inner_text() == '地图维护失败'
    assert '程序输出' in entry.locator('[data-call-section="output"] > summary').inner_text()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.locator('#call-more-filters > summary').click()
    page.locator('#call-trigger-source').select_option('system_maintenance')
    page.wait_for_function('S.callLog.triggerSource === "system_maintenance" && document.querySelector("#call-trigger-source").value === "system_maintenance"')
    assert parse_qs(urlsplit(requests[-1]).query)['trigger_source'] == ['system_maintenance']
    assert page.evaluate('S.callLog.history.length') == 0
    page.locator('#call-trigger-source').select_option('unknown')
    page.wait_for_function('document.querySelector("#call-trigger-source").value === "unknown"')
    assert parse_qs(urlsplit(requests[-1]).query)['trigger_source'] == ['unknown']


def test_active_more_filters_stay_collapsed_after_rerender(log_page):
    page, _, _, requests = log_page
    page.locator('#call-more-filters > summary').click()
    page.locator('#call-trigger-source').select_option('system_maintenance')
    page.wait_for_function('S.callLog.triggerSource === "system_maintenance" && document.querySelector("#call-trigger-source").value === "system_maintenance"')
    page.locator('#call-more-filters > summary').click()
    page.wait_for_function('S.callLog.moreFilters === false')
    page.evaluate('renderPage()')
    assert not page.locator('#call-more-filters').evaluate('(node) => node.open')
    assert page.locator('.call-more-count').inner_text() == '1'
    assert parse_qs(urlsplit(requests[-1]).query)['trigger_source'] == ['system_maintenance']
    page.locator('#call-more-filters > summary').click()
    page.wait_for_function('S.callLog.moreFilters === true')
    page.evaluate('renderPage()')
    assert page.locator('#call-more-filters').evaluate('(node) => node.open')


def test_missing_origin_and_mode_are_not_inferred_from_actor_or_request(log_page):
    page, rows, details, _ = log_page
    row = rows[0]
    row.update(tool='project_knowledge_build', summary='incremental=true knowledge-auto-fake')
    details[row['id']].update(row, args_summary={'incremental': True},
                             trigger_reason='<img src=x onerror=alert(1)>',
                             knowledge_stats={'entries_count': -1})
    page.evaluate('CodePierCallLog.reset()')
    entry = page.locator('[data-call-id]').first
    assert '触发来源未记录' in entry.locator('.call-tool').inner_text()
    assert '无需模型' not in entry.locator('.call-tool').inner_text()
    entry.locator('summary').first.click()
    entry.locator('.call-facts').wait_for()
    assert not page.locator('img').count()
    facts = entry.locator('.call-facts').inner_text()
    assert '增量' not in facts
    assert '条目数' not in facts
    assert '触发来源未记录' in facts
    assert '未记录' in facts


def test_filter_change_preserves_queue_panel_and_search_focus(log_page):
    page, rows, _, requests = log_page
    page.add_style_tag(path=str(ROOT / 'web/queue.css'))
    page.add_script_tag(path=str(ROOT / 'web/queue.js'))
    page.evaluate(
        '''() => {
        window.queueFetches = 0;
        const origRead = window.readCallLog;
        window.readCallLog = async (path) => {
          if (path === '/api/operations/queue') {
            window.queueFetches++;
            return {operations: [], observed_at: 100, diagnostics_available: false};
          }
          const res = await origRead(path);
          if (path.includes('include_queue=true')) {
            res.queue = {operations: [], observed_at: 100, diagnostics_available: false};
          }
          return res;
        };
      }'''
    )
    page.evaluate('renderPage()')
    page.locator('#operation-queue').wait_for()
    assert page.evaluate('window.queueFetches') == 0
    page.evaluate('window.origQueueEl = document.querySelector("#operation-queue")')

    # Switch project filter and verify #operation-queue is NOT wiped or refetched.
    page.locator('#call-project').select_option('p1')
    page.wait_for_function('S.callLog.project === "p1"')
    assert page.evaluate('document.querySelector("#operation-queue") === window.origQueueEl')
    assert page.evaluate('window.queueFetches') == 0

    # Instant IME-safe search preserves input focus and cursor position.
    query_input = page.locator('#audit-query')
    query_input.focus()
    page.keyboard.type('README')
    page.wait_for_function('S.auditQuery === "README"')
    assert page.evaluate('document.activeElement.id === "audit-query"')
    assert page.evaluate('document.querySelector("#operation-queue") === window.origQueueEl')


def test_sse_operation_terminal_event_reconciles_both_queue_and_list_under_502(log_page):
    page, rows, _, requests = log_page
    page.add_style_tag(path=str(ROOT / 'web/queue.css'))
    page.add_script_tag(path=str(ROOT / 'web/queue.js'))
    op_id = rows[0]['id']
    page.evaluate(
        f'''() => {{
        const origRead = window.readCallLog;
        window.fail502 = false;
        window.readCallLog = async (path) => {{
          if (window.fail502) throw new Error('HTTP 502');
          const res = await origRead(path);
          if (path.includes('include_queue=true')) {{
            res.queue = {{
              observed_at: 101,
              diagnostics_available: false,
              operations: [{{id: '{op_id}', tool: 'shell_exec', state: 'running', queue_stage: 'running', summary: 'pytest -q', alias: 'MCP', device_name: 'Node'}}],
            }};
          }}
          return res;
        }};
      }}'''
    )
    page.evaluate('renderPage()')
    page.locator(f'[data-queue-id="{op_id}"]').wait_for()
    assert '执行中' in page.locator('[data-call-id]').first.locator('.call-state').inner_text()

    # Collapsed output SSE events must NOT trigger /api/call-log HTTP requests.
    before_count = len(requests)
    page.evaluate(
        f'''() => {{
        for (let i = 0; i < 30; i++) {{
          CodePierCallLog.onServerEvent({{type: 'output', data: {{id: '{op_id}'}}}});
        }}
      }}'''
    )
    page.wait_for_timeout(500)
    assert len(requests) == before_count

    # Simulate transient HTTP 502 window while SSE delivers terminal succeeded frame.
    page.evaluate(
        f'''() => {{
        window.fail502 = true;
        CodePierCallLog.onServerEvent({{
          type: 'operation',
          at: 105,
          data: {{id: '{op_id}', state: 'succeeded', queue_stage: 'terminal', exit_code: 0}},
        }});
      }}'''
    )
    assert page.locator(f'[data-queue-id="{op_id}"]').count() == 0
    assert '已完成' in page.locator('[data-call-id]').first.locator('.call-state').inner_text()


def test_burst_grouping_diff_waterfall_and_dense_table(log_page):
    page, rows, details, _ = log_page
    base = {'actor': 'mcp:g1:Claude', 'project_id': 'p1', 'device_id': 'd1', 'alias': 'MCP', 'device_name': 'Node', 'state': 'succeeded', 'elapsed_ms': 40, 'summary': 'path=README.md'}
    rows[:] = [
        {**base, 'id': f'{i + 1:02x}' + 'b' * 30, 'tool': 'read' if i % 2 == 0 else 'search', 'created': 200 - i}
        for i in range(4)
    ] + [
        {'id': 'e' * 32, 'tool': 'edit', 'actor': 'mcp:g1:Claude', 'project_id': 'p1', 'device_id': 'd1', 'alias': 'MCP', 'device_name': 'Node', 'state': 'succeeded', 'created': 150, 'elapsed_ms': 320, 'summary': 'path=hub/app.py'}
    ]
    details['e' * 32] = {
        **rows[-1],
        'args_summary': {'path': 'hub/app.py'},
        'result': {'ok': True, 'data': {'added_lines': 3, 'removed_lines': 1, 'diff': '@@ -1 +1,3 @@\n+new line'}},
        'timing': {'wait_ms': 50, 'execution_ms': 210},
        'trace': {'current': {'reason': '操作已结束'}, 'events': []},
    }
    page.evaluate('CodePierCallLog.clear(); renderPage()')
    burst = page.locator('.call-burst')
    assert burst.count() == 1
    assert '连续探查 4 次' in burst.locator('.call-burst-summary').inner_text()

    # Toggle dense table mode and expand edit entry to verify Waterfall & Diff.
    page.locator('[data-call-action="density"][data-density="table"]').click()
    assert page.locator('.call-log.call-log-table').count() == 1
    edit_entry = page.locator(f'[data-call-id="{"e" * 32}"]')
    edit_entry.locator('summary').first.click()
    edit_entry.locator('[data-call-section="diff"]').wait_for()
    assert '+ 3 行' in edit_entry.locator('[data-call-section="diff"]').inner_text()
    assert edit_entry.locator('.call-waterfall').count() == 1


def test_sse_terminal_frame_shows_final_duration_not_last_polled_value(log_page):
    page, rows, _, _ = log_page
    op_id = rows[0]['id']
    assert '已历时' in page.locator(f'[data-call-id="{op_id}"] .call-state').inner_text()
    # State frames carry created/updated but no elapsed_ms (hub/runtime.py).
    page.evaluate(
        f"""CodePierCallLog.onServerEvent({{type: 'operation', at: 105,
            data: {{id: '{op_id}', state: 'succeeded', created: 100, updated: 104.5}}}})"""
    )
    text = page.locator(f'[data-call-id="{op_id}"] .call-state').inner_text()
    assert '总历时 4.50 秒' in text and '1.00 秒' not in text


def test_incremental_detail_that_finishes_is_settled_with_full_timing(log_page):
    page, rows, details, requests = log_page
    op_id = rows[0]['id']
    details[op_id] = {**details[op_id], 'output_seq': 3, 'timing': {'wait_ms': None, 'execution_ms': None}}
    entry = page.locator(f'[data-call-id="{op_id}"]')
    entry.locator('summary').first.click()
    entry.get_by_text('实际执行（本次 Agent 计时）', exact=True).wait_for()
    # The call finishes. An incremental read (after_output_seq/after_event_id)
    # carries no full trace, so it cannot provide final wait/execution timing.
    rows[0].update(state='succeeded', updated=104)
    details[op_id] = {**details[op_id], 'state': 'succeeded', 'updated': 104, 'output_seq': 4,
        'timing': {'wait_ms': 20, 'execution_ms': 900}}
    page.evaluate('CodePierCallLog.refresh(false)')
    entry.locator('.call-waterfall').wait_for()
    assert '实际执行 900 毫秒' in entry.locator('.call-waterfall').inner_text()
    assert any('after_output_seq=3' in u for u in requests)
    assert requests[-1].endswith(op_id)


def test_late_pinned_detail_cannot_cross_session_or_space(log_page):
    page, rows, details, _ = log_page
    private = {**details[rows[0]['id']], 'id': 'c' * 32, 'actor': 'private-old-space'}
    page.evaluate("""() => {
      window.originalRead = window.readCallLog;
      window.readCallLog = () => new Promise(resolve => window.finishOldPin = resolve);
      void CodePierCallLog.focusOperation('c'.repeat(32));
    }""")
    page.wait_for_function("typeof finishOldPin === 'function'")
    page.evaluate("""async () => {
      CodePierCallLog.clear();
      S.session = {user_id:'new-owner'};
      S.space_id = 'new-space';
      window.readCallLog = window.originalRead;
      await renderPage();
    }""")
    page.evaluate("(d) => window.finishOldPin(d)", private)
    page.wait_for_timeout(30)
    assert page.locator('.call-pinned-inspector').count() == 0
    assert page.evaluate("S.callLog.pinnedOp === null")
    assert 'private-old-space' not in page.locator('#page').inner_text()


def test_pinned_detail_latest_request_close_and_manual_refresh(log_page):
    page, rows, details, _ = log_page
    page.evaluate("""() => {
      window.finishPins = {};
      window.originalRead = window.readCallLog;
      window.readCallLog = (path) => new Promise(resolve => {
        window.finishPins[path.split('/').pop()] = resolve;
      });
      void CodePierCallLog.focusOperation('c'.repeat(32));
      void CodePierCallLog.focusOperation('d'.repeat(32));
    }""")
    first = {**details[rows[0]['id']], 'id': 'c' * 32, 'output': 'old request'}
    latest = {**details[rows[0]['id']], 'id': 'd' * 32, 'output': 'latest request'}
    page.evaluate("(d) => finishPins[d.id](d)", latest)
    page.locator('.call-pinned-inspector').wait_for()
    page.evaluate("(d) => finishPins[d.id](d)", first)
    assert page.evaluate("S.callLog.pinnedOp.id") == latest['id']
    page.evaluate("void CodePierCallLog.focusOperation('e'.repeat(32))")
    page.get_by_role('button', name='关闭定位', exact=True).click()
    page.evaluate("(d) => finishPins['e'.repeat(32)](d)", {**latest, 'id':'e'*32})
    page.wait_for_timeout(30)
    assert page.locator('.call-pinned-inspector').count() == 0
    # Manual refresh works even though a pinned inspector has no list-entry ancestor.
    page.evaluate("(d) => { window.readCallLog = async () => d; }", latest)
    page.evaluate("CodePierCallLog.focusOperation('d'.repeat(32))")
    latest['output'] = 'refreshed pinned evidence'
    page.evaluate("(d) => { window.readCallLog = async () => d; }", latest)
    page.locator('.call-pinned-inspector').get_by_role('button',name='刷新详情',exact=True).click()
    page.wait_for_function("document.querySelector('.call-pinned-inspector pre').textContent.includes('refreshed pinned evidence')")
    page.evaluate("window.readCallLog = window.originalRead; void 0")


def test_incremental_output_keeps_terminal_identity_and_has_search(log_page):
    page, rows, details, requests = log_page
    op_id = rows[0]['id']
    data = details[op_id]
    data['output'] = '\n'.join(f'line {i}' for i in range(100))
    data['output_seq'] = 1
    data['output_clean_chars'] = len(data['output'])
    data['output_clean_sha'] = 'a' * 64
    page.evaluate("""() => {
      const original = window.readCallLog;
      window.readCallLog = path => original(path.startsWith('/api/call-log/') ? path.split('?')[0] : path);
    }""")
    entry = page.locator('[data-call-id]').first
    entry.locator('summary').first.click()
    entry.locator('.call-facts').wait_for()
    output = entry.locator('[data-call-section="output"]')
    output.locator('summary').click()
    output.locator('pre').evaluate("(el) => { window.originalTerminal = el; el.scrollTop = el.scrollHeight; }")
    page.evaluate("document.activeElement.blur()")
    data['output'] += '\nunique-needle\nnext line'
    data['output_seq'] = 2
    data['updated'] = 103
    page.evaluate('CodePierCallLog.refresh(true)')
    assert output.locator('pre').evaluate('(el) => el === window.originalTerminal')
    assert output.locator('pre').inner_text().endswith('unique-needle\nnext line')
    assert output.locator('.call-output-lines').inner_text().splitlines()[-1] == '102'
    output.get_by_role('searchbox',name='查找当前输出').fill('unique-needle')
    assert '1 / 1' in output.locator('.call-output-matches').inner_text()
    assert output.locator('pre').evaluate('(el) => el.scrollTop > 0')


def test_stale_sse_and_snapshot_cannot_revert_visible_terminal_row(log_page):
    page, rows, _, _ = log_page
    page.add_script_tag(path=str(ROOT / 'web/queue.js'))
    done = {**rows[0], 'state':'succeeded', 'updated':200}
    page.evaluate("(d) => CodePierCallLog.onServerEvent({type:'operation',data:d})", done)
    page.evaluate("(d) => CodePierCallLog.onServerEvent({type:'operation',data:d})", rows[0])
    entry = page.locator('[data-call-id]').first
    assert '已完成' in entry.locator('.call-state').inner_text()
    # The list response is also stale; expanded reading prevents a full replacement.
    page.evaluate("document.querySelector('[data-call-id]').open = true")
    page.evaluate('CodePierCallLog.refresh(true)')
    assert '已完成' in entry.locator('.call-state').inner_text()
    assert page.evaluate("(id) => CodePierOperationStore.get(id).state", rows[0]['id']) == 'succeeded'


def test_missing_timing_stage_is_not_inferred_as_return_time(log_page):
    page, rows, details, _ = log_page
    details[rows[0]['id']]['timing'] = {'wait_ms': None, 'execution_ms': 100}
    page.locator('[data-call-id]').first.locator('summary').first.click()
    page.locator('.call-waterfall').wait_for()
    text = page.locator('.call-waterfall').inner_text()
    assert '未记录阶段 未记录' in text
    assert '回传与落盘' not in text


def test_keyboard_intent_prefetch_is_reused_on_open(log_page):
    page, rows, _, requests = log_page
    summary = page.locator('[data-call-id]').first.locator('summary').first
    summary.focus()
    for _ in range(60):
        if any(u.endswith(rows[0]['id']) for u in requests):
            break
        page.wait_for_timeout(50)
    assert len([u for u in requests if u.endswith(rows[0]['id'])]) == 1
    summary.press('Enter')
    page.locator('.call-facts').wait_for()
    assert len([u for u in requests if u.endswith(rows[0]['id'])]) == 1


def test_shell_binds_before_initial_snapshot_without_separate_queue_fetch(log_page):
    page, _, _, _ = log_page
    page.add_script_tag(path=str(ROOT / 'web/queue.js'))
    page.evaluate("""() => {
      CodePierCallLog.clear();
      window.originalRead = window.readCallLog;
      window.queueFetchCount = 0;
      window.readCallLog = path => {
        if (path === '/api/operations/queue') { window.queueFetchCount++; return {}; }
        return new Promise(resolve => window.finishInitial = resolve);
      };
      document.querySelector('#page').innerHTML = CodePierCallLog.renderShellHTML();
      CodePierCallLog.bind({shellOnly:true});
      void CodePierCallLog.load();
    }""")
    page.locator('#audit-query').focus()
    assert page.evaluate("document.activeElement.id") == 'audit-query'
    assert page.evaluate("window.queueFetchCount") == 0
    page.evaluate("""async () => {
      const data = await window.originalRead('/api/call-log');
      data.queue = {operations:[], observed_at:101, diagnostics_available:false};
      window.finishInitial(data);
      window.readCallLog = window.originalRead;
    }""")
    page.wait_for_function("document.querySelectorAll('[data-call-id]').length === 2")
    assert page.evaluate("document.activeElement.id") == 'audit-query'
    assert page.evaluate("window.queueFetchCount") == 0


def test_dense_table_alignment_failed_subrow_burst_and_wide_inspector(log_page):
    page, rows, details, _ = log_page
    base = {
        'actor': 'mcp:grant-1234567890ab:ChatGPT',
        'project_id': 'p1',
        'device_id': 'd1',
        'alias': 'Imago',
        'device_name': 'Studio',
        'state': 'succeeded',
        'elapsed_ms': 276,
    }
    fail_id = 'f' * 32
    empty_id = 'e' * 32
    ok_id = 'a' * 32
    rows[:] = [
        {**base, 'id': fail_id, 'tool': 'exec', 'state': 'failed', 'exit_code': 7,
         'error': '项目命令退出码 7：请查看原操作详情输出', 'created': 300, 'elapsed_ms': 1570,
         'summary': 'command=set -eu; python3 -c "import json; p=json.load(open(\'/opt/Imago/.imago\'))"',
         'alias': 'Imago'},
        {**base, 'id': empty_id, 'tool': 'open_workspace', 'state': 'succeeded', 'created': 290,
         'elapsed_ms': 1650, 'summary': '', 'alias': 'SVG'},
        {**base, 'id': ok_id, 'tool': 'read', 'state': 'succeeded', 'created': 280,
         'elapsed_ms': 202, 'summary': 'path=backend/app/db/postgres_schema.py', 'alias': 'Nexus'},
        *[
            {**base, 'id': f'{i + 1:02x}' + 'c' * 30, 'tool': 'read' if i % 2 == 0 else 'search',
             'created': 250 - i, 'elapsed_ms': 50, 'summary': 'path=README.md', 'alias': 'Imago'}
            for i in range(4)
        ],
    ]
    for r in rows:
        details[r['id']] = {
            **r,
            'attempts': 1,
            'args_summary': {'summary': r['summary'] or 'none'},
            'output': f'output for {r["tool"]}',
            'timing': {'wait_ms': 10, 'execution_ms': r['elapsed_ms']},
            'trace': {'current': {'reason': '完成'}, 'events': []},
        }
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.evaluate('CodePierCallLog.clear(); renderPage()')
    page.locator('[data-call-action="density"][data-density="table"]').click()

    # P0: Sticky table header visible; zero horizontal overflow.
    assert page.locator('.call-table-header').is_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')

    # P0: Failed row project & tool stay single-line (no vertical letter stacking),
    # and empty-args row aligns its time/state/duration columns with normal rows.
    fail_entry = page.locator(f'[data-call-id="{fail_id}"]')
    empty_entry = page.locator(f'[data-call-id="{empty_id}"]')
    ok_entry = page.locator(f'[data-call-id="{ok_id}"]')
    fail_proj_box = fail_entry.locator('.call-project').bounding_box()
    fail_tool_box = fail_entry.locator('.call-tool').bounding_box()
    assert fail_proj_box['height'] < 32
    assert fail_tool_box['height'] < 32
    fail_err_box = fail_entry.locator('.call-inline-error').bounding_box()
    assert fail_err_box['y'] > fail_proj_box['y'] + fail_proj_box['height'] - 4

    # Vertical column alignment across failed, empty-args, and normal rows.
    empty_state_box = empty_entry.locator('.call-state-badge').bounding_box()
    ok_state_box = ok_entry.locator('.call-state-badge').bounding_box()
    assert abs(empty_state_box['x'] - ok_state_box['x']) <= 2
    empty_dur_box = empty_entry.locator('.call-duration').bounding_box()
    ok_dur_box = ok_entry.locator('.call-duration').bounding_box()
    assert abs((empty_dur_box['x'] + empty_dur_box['width']) - (ok_dur_box['x'] + ok_dur_box['width'])) <= 2

    # P1: Burst row duration aligns with table duration column.
    burst_dur_box = page.locator('.call-burst .call-burst-meta').bounding_box()
    assert abs((burst_dur_box['x'] + burst_dur_box['width']) - (ok_dur_box['x'] + ok_dur_box['width'])) <= 4

    # P2: Wide-screen (>=1280px) table click opens right-hand inspector drawer without opening <details>.
    fail_entry.locator('summary').first.click()
    drawer = page.locator('.call-inspector')
    drawer.wait_for(state='visible')
    assert page.locator('.call-workspace.has-inspector').count() == 1
    assert fail_entry.get_attribute('open') is None
    assert 'output for exec' in drawer.inner_text()

    # P2: Keyboard ArrowDown steps to next row (open_workspace); Escape closes drawer.
    page.keyboard.press('ArrowDown')
    page.wait_for_function(f'S.callLog.inspectedId === "{empty_id}"')
    assert 'open_workspace' in drawer.locator('.call-inspector-title').inner_text()
    page.keyboard.press('Escape')
    assert drawer.is_hidden()
    assert page.locator('.call-workspace.has-inspector').count() == 0


def test_wide_inspector_live_guard_incremental_resize_and_inline_error_patch(log_page):
    page, rows, details, requests = log_page
    run_id = '1' * 32
    ok_id = '2' * 32
    rows[:] = [
        {'id': run_id, 'tool': 'exec', 'actor': 'mcp:g1:ChatGPT', 'project_id': 'p1',
         'alias': 'MCP', 'device_name': 'Node', 'state': 'running', 'created': 200,
         'updated': 201, 'elapsed_ms': 1200, 'summary': 'command=pytest -q'},
        {'id': ok_id, 'tool': 'read', 'actor': 'mcp:g1:ChatGPT', 'project_id': 'p1',
         'alias': 'MCP', 'device_name': 'Node', 'state': 'succeeded', 'created': 190,
         'updated': 191, 'elapsed_ms': 80, 'summary': 'path=README.md'},
    ]
    details[run_id] = {
        **rows[0],
        'attempts': 1,
        'args_summary': {'command': 'pytest -q'},
        'output': '\n'.join(f'line {i}' for i in range(60)),
        'output_seq': 2,
        'output_clean_chars': 400,
        'output_clean_sha': 'b' * 64,
        'timing': {'wait_ms': 10, 'execution_ms': None},
        'trace': {'latest_event_id': 1, 'current': {'reason': '运行中'}, 'events': []},
    }
    details[ok_id] = {
        **rows[1],
        'attempts': 1,
        'args_summary': {'path': 'README.md'},
        'output': 'readme content',
        'timing': {'wait_ms': 5, 'execution_ms': 75},
        'trace': {'current': {'reason': '完成'}, 'events': []},
    }
    page.set_viewport_size({'width': 1440, 'height': 900})
    page.evaluate("""() => {
      window.rawRequests = [];
      const original = window.readCallLog;
      window.readCallLog = path => {
        window.rawRequests.push(path);
        return original(path.startsWith('/api/call-log/') ? path.split('?')[0] : path);
      };
    }""")
    page.evaluate('CodePierCallLog.clear(); renderPage()')
    page.locator('[data-call-action="density"][data-density="table"]').click()

    # Open running row in wide inspector drawer.
    run_entry = page.locator(f'[data-call-id="{run_id}"]')
    run_entry.locator('summary').first.click()
    drawer = page.locator('.call-inspector')
    drawer.locator('.call-facts').wait_for()
    output_sec = drawer.locator('[data-call-section="output"]')
    output_sec.locator('summary').click()
    output_sec.locator('pre').evaluate('(el) => { window.drawerPre = el; }')
    page.evaluate('document.activeElement?.blur?.()')

    # B1 + B2: New incoming row during background poll must NOT overwrite list while inspector is open,
    # and active drawer uses incremental after_output_seq while preserving <pre> identity.
    rows.insert(0, {**rows[1], 'id': '3' * 32, 'created': 250})
    details[run_id]['output'] += '\nappended-line'
    details[run_id]['output_seq'] = 3
    details[run_id]['updated'] = 205
    page.evaluate('CodePierCallLog.refresh(true)')
    assert page.locator('[data-call-id]').count() == 2
    assert page.locator('#call-new').is_visible()
    assert page.evaluate('window.rawRequests.some(u => u.includes("after_output_seq=2"))')
    assert output_sec.locator('pre').evaluate('(el) => el === window.drawerPre')
    assert output_sec.locator('pre').inner_text().endswith('appended-line')

    # B6 + B4: SSE failure frame dynamically injects .call-inline-error sub-row & updates data-call-state.
    page.evaluate(
        f"""CodePierCallLog.onServerEvent({{
          type: 'operation',
          at: 210,
          data: {{id: '{run_id}', state: 'failed', exit_code: 2, error: '断言失败：退出码 2', created: 200, updated: 210}}
        }})"""
    )
    assert run_entry.get_attribute('data-call-state') == 'failed'
    assert '退出码 2' in run_entry.locator('.call-inline-error').inner_text()

    # B5: Shrinking viewport below 1280px seamlessly hands off drawer to inline <details open>,
    # and widening back restores the drawer and collapses inline <details>.
    page.set_viewport_size({'width': 1024, 'height': 900})
    page.wait_for_function(f'document.querySelector(\'[data-call-id="{run_id}"]\').open === true')
    assert drawer.is_hidden()
    page.set_viewport_size({'width': 1440, 'height': 900})
    drawer.wait_for(state='visible')
    assert run_entry.get_attribute('open') is None
