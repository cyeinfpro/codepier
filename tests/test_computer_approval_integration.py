import concurrent.futures
import json
import sys
import time
import uuid
from pathlib import Path
import pytest
import httpx
from playwright.sync_api import sync_playwright, expect
from tests.support import running_stack,wait_for
from tests.test_computer import fixture_provider

@pytest.fixture(scope='module')
def approval_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('approval-stack')) as s:
        s.stop_agent()
        provider=fixture_provider(s.directory/'provider')
        binary=s.directory/'appserver'
        binary.write_text('#!'+sys.executable+'\nimport sys\nsys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[1]))+')\nfrom tests.fake_computer_appserver import main\nmain()\n')
        binary.chmod(0o700)
        config=json.loads(s.config_path.read_text());config['computer']={'enabled':True,'projects':['Imago'],'allowed_apps':['Fixture'],'plugin_root':str(provider),'transport':'codex-app-server','app_server':str(binary),'call_timeout_seconds':15}
        s.config_path.write_text(json.dumps(config));s.start_agent()
        r=s.must(s.client.post('/api/grants',json={'label':'approval-test','scopes':['read','computer'],'projects':[s.project['id']],'days':1}))
        s.approval_pat=r['token']
        yield s

def tool(s,operation,args):
    out=s.mcp('computer',{'operation':operation,'project':'Imago',**args},s.approval_pat)
    assert not out.get('isError'),out
    return out['structuredContent']

def open_session(s):
    return tool(s,'open',{'app':'Fixture','ttl_seconds':60,'idempotency_key':uuid.uuid4().hex})['session_id']

def pending(s):
    return s.client.get('/api/computer/approvals').json()['approvals']

@pytest.mark.parametrize('decision',['accept','decline','cancel'])
def test_http_encrypted_codepier_requires_human_panel_decision(approval_stack,decision):
    s=approval_stack;sid=open_session(s)
    try:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            result=executor.submit(s.mcp,'computer',{'operation':'observe','project':'Imago','session_id':sid},s.approval_pat)
            rows=wait_for(lambda:pending(s),timeout=8);item=rows[0]
            assert item['session_id']==sid and item['app']=='Fixture'
            # Bearer MCP access cannot approve or list the human inbox.
            with httpx.Client(base_url=s.url) as outsider:
                assert outsider.get('/api/computer/approvals',headers={'Authorization':'Bearer '+s.approval_pat}).status_code==401
                assert outsider.post('/api/computer/approvals/'+item['request_id'],headers={'Authorization':'Bearer '+s.approval_pat},json={'action':'accept'}).status_code==401
            headers=dict(s.client.headers);headers.pop('x-rd-csrf',None)
            with httpx.Client(base_url=s.url,cookies=s.client.cookies,headers=headers) as no_csrf:
                assert no_csrf.post('/api/computer/approvals/'+item['request_id'],json={'action':'accept'}).status_code==403
            response=s.client.post('/api/computer/approvals/'+item['request_id'],json={'action':decision});assert response.status_code==200,response.text
            assert s.client.post('/api/computer/approvals/'+item['request_id'],json={'action':decision}).status_code==409
            out=result.result(timeout=10)
            assert bool(out.get('isError'))==(decision!='accept'),out
            if decision=='accept':assert out['structuredContent']['observation_id']
            wait_for(lambda:not pending(s),timeout=3)
    finally:tool(s,'close',{'session_id':sid,'idempotency_key':uuid.uuid4().hex})

def test_browser_consent_message_is_text_and_explicit(approval_stack):
    s=approval_stack
    with sync_playwright() as p:
        browser=p.chromium.launch();page=browser.new_page()
        try:
            page.goto(s.url);page.fill('#username', 'admin');page.fill('#password',s.password);page.click('#login-form button')
            sid=open_session(s)
            with concurrent.futures.ThreadPoolExecutor() as executor:
                result=executor.submit(s.mcp,'computer',{'operation':'observe','project':'Imago','session_id':sid},s.approval_pat)
                expect(page.locator('#computer-approval-inbox')).to_be_visible(timeout=8000)
                assert page.locator('#computer-approval-inbox img').count()==0
                expect(page.locator('#computer-approval-inbox')).to_contain_text('Allow Fixture?')
                page.get_by_role('button',name='拒绝',exact=True).click()
                assert result.result(timeout=10)['isError']
                expect(page.locator('#computer-approval-inbox')).not_to_be_visible(timeout=5000)
            tool(s,'close',{'session_id':sid,'idempotency_key':uuid.uuid4().hex})
        finally:browser.close()

@pytest.mark.parametrize('reason',['stop','disconnect'])
def test_pending_approval_invalidated_by_session_or_connection_end(approval_stack,reason):
    s=approval_stack;sid=open_session(s)
    with concurrent.futures.ThreadPoolExecutor() as executor:
        result=executor.submit(s.mcp,'computer',{'operation':'observe','project':'Imago','session_id':sid},s.approval_pat)
        item=wait_for(lambda:pending(s),timeout=8)[0]
        if reason=='stop':
            tool(s,'close',{'session_id':sid,'idempotency_key':uuid.uuid4().hex})
        else:
            s.stop_agent()
        wait_for(lambda:not pending(s),timeout=5)
        response=s.client.post('/api/computer/approvals/'+item['request_id'],json={'action':'accept'})
        assert response.status_code==409
        result.result(timeout=12)
    if reason=='disconnect':s.start_agent()
