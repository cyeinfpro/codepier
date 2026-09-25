"""Real loopback Hub/Agent and Chromium; no production services or CLI models."""
import json
import uuid
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright

from tests.support import running_stack


@pytest.fixture(scope='module')
def call_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('call-log-stack')) as stack:
        yield stack


def write_fixture(stack, name='call-log-evidence.txt'):
    content='call log fixture\n'
    receipt=stack.fs('fs_write',path=name,content=content,expected_sha256='new',idempotency_key=uuid.uuid4().hex)
    operation=stack.poll(receipt['operation_id'],timeout=30)
    assert operation['state']=='succeeded', operation
    return operation,content


def test_real_call_summary_trace_and_reads_do_not_create_more_calls(call_stack):
    stack=call_stack
    operation,content=write_fixture(stack)
    identifier=operation['id']
    assert (stack.imago/'call-log-evidence.txt').read_text()==content
    response=stack.client.get('/api/call-log',params={'q':'call-log-evidence.txt','project':stack.project['id'],'tool':'fs_write'})
    assert response.status_code==200, response.text
    assert response.headers['Cache-Control']=='no-store'
    row=next(r for r in response.json()['operations'] if r['id']==identifier)
    assert f'inputChars={len(content)}' in row['summary']
    assert row['alias']=='Imago' and row['device_name']
    assert 'args_summary' not in row and 'result' not in row
    before=stack.client.get('/api/audit').json()['events']
    detail=stack.client.get('/api/call-log/'+identifier)
    assert detail.status_code==200, detail.text
    data=detail.json()
    assert data['state']=='succeeded' and data['args_summary']['content']==f'<{len(content)} chars>'
    assert data['trace']['events']
    assert data['timing']['execution_ms'] is not None
    assert data['timing']['wait_ms'] is not None
    assert 'payload' not in data
    for _ in range(3):
        assert stack.client.get('/api/call-log/'+identifier).status_code==200
    assert stack.client.get('/api/audit').json()['events']==before


def test_real_http_success_is_not_command_success(call_stack):
    stack=call_stack
    receipt=stack.fs('tasks_run',task='failure',idempotency_key=uuid.uuid4().hex)
    operation=stack.poll(receipt['operation_id'],timeout=30)
    assert operation['state']=='failed'
    response=stack.client.get('/api/call-log/'+operation['id'])
    assert response.status_code==200
    data=response.json()
    assert data['state']=='failed' and data['exit_code']==3
    assert 'EXPECTED FAILURE' in data['output']
    response=stack.client.get('/api/call-log',params={'status':'running','watch':operation['id']})
    assert response.status_code==200
    assert response.json()['updates'][0]['state']=='failed'


def test_real_session_required_even_with_mcp_bearer(call_stack):
    stack=call_stack
    with httpx.Client(base_url=stack.url,trust_env=False,timeout=15) as anonymous:
        for endpoint in ('/api/call-log','/api/call-log/'+'a'*32):
            assert anonymous.get(endpoint).status_code==401
            assert anonymous.get(endpoint,headers={'Authorization':'Bearer '+stack.pat}).status_code==401
    assert stack.client.get('/api/call-log/'+'f'*32).status_code==404


def test_real_panel_navigation_live_details_export_themes_and_logout(call_stack):
    stack=call_stack
    operation,_=write_fixture(stack,'call-log-browser.txt')
    errors=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page(viewport={'width':1280,'height':960})
        page.on('pageerror',lambda e:errors.append(str(e)))
        try:
            page.goto(stack.url+'/#audit')
            page.locator('#username').fill('admin')
            page.locator('#password').fill(stack.password)
            page.locator('#login-form button[type=submit]').click()
            entry=page.locator('[data-call-id="'+operation['id']+'"]')
            entry.wait_for(timeout=20000)
            entry.locator('summary').first.click()
            entry.locator('.call-facts').wait_for()
            assert 'inputChars=' in entry.locator('.call-args').inner_text()
            assert '<17 chars>' in entry.locator('[data-call-section="arguments"] pre').inner_text()
            assert '未记录' not in entry.locator('.call-facts').inner_text()
            # An SSE message must update in place, not replace the expanded row.
            entry.evaluate('(el)=>window.callLogOriginalNode=el')
            page.evaluate("S.events.onmessage({data:JSON.stringify({type:'operation',data:{id:'fixture'}})})")
            page.wait_for_timeout(1200)
            assert entry.evaluate('(el)=>el===window.callLogOriginalNode && el.open')
            page.get_by_role('button',name='暂停实时更新',exact=True).click()
            entry.get_by_role('button',name='刷新详情',exact=True).click()
            entry.locator('[data-call-section="trace"] > summary').click()
            assert entry.locator('.call-timeline li').count()>0
            with page.expect_download() as downloaded:
                page.get_by_role('button',name='导出本页调用',exact=True).click()
            exported=json.loads(Path(downloaded.value.path()).read_text())
            assert exported['scope']=='visible_page'
            assert any(x['id']==operation['id'] for x in exported['calls'])
            page.evaluate("document.documentElement.dataset.appearance='dark'")
            assert entry.evaluate("el=>getComputedStyle(el).backgroundColor")!='rgb(255, 255, 255)'
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            # Switch through existing events view and back, with all handlers intact.
            page.locator('[data-action="audit-mode"][data-mode="events"]').click()
            page.locator('[data-event-index]').first.wait_for()
            page.locator('[data-action="audit-mode"][data-mode="operations"]').click()
            page.locator('.call-log').wait_for()
            page.locator('#call-tool').select_option('fs_write')
            page.wait_for_function('() => S.callLog.tool==="fs_write"')
            page.locator('#audit-query').fill('call-log-browser.txt')
            page.locator('#call-search button').click()
            page.wait_for_function('() => S.auditQuery==="call-log-browser.txt" && document.querySelectorAll("[data-call-id]").length===1')
            page.evaluate('endSession(true)')
            page.locator('#login-form').wait_for()
            assert page.evaluate('S.callLog===null')
            assert not errors
        finally:
            browser.close()
