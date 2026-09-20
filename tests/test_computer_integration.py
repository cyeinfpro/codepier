"""Real loopback Hub -> encrypted Agent -> stdio fixture -> native MCP images.

The fixture renders synthetic pixels and records input calls. No real user app is
controlled by these regression tests; native installation probing is separate.
"""
import json
import sqlite3
import time
import uuid
from pathlib import Path
import pytest
from jsonschema import Draft202012Validator
from playwright.sync_api import sync_playwright, expect
from shared.contracts import OUTPUT_SCHEMAS
from shared.computer_contracts import NATIVE_ACTIONS
from tests.support import running_stack, wait_for
from tests.catalog_assertions import assert_task_catalog
from tests.test_computer import fixture_provider

@pytest.fixture(scope='module')
def desktop(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('computer-real-stack')) as stack:
        provider=fixture_provider(stack.directory/'native-fixture')
        stack.stop_agent()
        config=json.loads(stack.config_path.read_text())
        config['computer']={'enabled':True,'projects':[stack.project['id']],'allowed_apps':['Fixture'],'plugin_root':str(provider),'call_timeout_seconds':5}
        stack.config_path.write_text(json.dumps(config))
        stack.start_agent()
        r=stack.must(stack.client.post('/api/grants',json={'label':'Desktop fixture','scopes':['read','computer'],'projects':[stack.project['id']],'days':1}))
        stack.computer_pat=r['token'];stack.computer_grant=r['grant_id'];stack.provider=provider
        yield stack


def rpc(stack,name,args=None,*,key=None,token=None,allow_error=False):
    args={'project':'Imago',**(args or {})}
    if name in {'computer_action','computer_session_open','computer_session_close'}:
        args.setdefault('idempotency_key',key or 'test-'+uuid.uuid4().hex)
    out=stack.mcp(name,args,token or stack.computer_pat)
    value=out['structuredContent']
    Draft202012Validator(OUTPUT_SCHEMAS[name]).validate(value)
    if value.get('pending'):
        for _ in range(12):
            out=stack.mcp('operations_wait',{'operation_id':value['operation_id'],'wait_seconds':2},token or stack.computer_pat)
            Draft202012Validator(OUTPUT_SCHEMAS['operations_wait']).validate(out['structuredContent'])
            if not out['structuredContent'].get('pending'):break
        value=out['structuredContent']
        if value.get('result',{}).get('ok'):
            data={'operation_id':value['operation_id'],**value['result']['data']}
        else:data=value
    else:data=value
    if not out['isError']:
        Draft202012Validator(OUTPUT_SCHEMAS[name]).validate(data)
    if not allow_error:assert not out['isError'],out
    return data,out


def release(stack,session):
    return rpc(stack,'computer_session_close',{'session_id':session})


def test_scope_catalog_and_status_never_expand_existing_grants(desktop):
    s=desktop
    out=s.mcp('computer_apps',{'project':'Imago'})
    assert out['isError'] and out['structuredContent']['error']['code']=='INSUFFICIENT_SCOPE'
    status,_=rpc(s,'computer_status',{'probe':True})
    assert status['capabilities_verified'] and len(status['native_tools'])==10
    assert status['screen_permissions_verified'] is False
    out=s.rpc('tools/list').json()['result']['tools']
    assert_task_catalog(out, 72)
    assert not {'integration_control','validations_accept'} & {t['name'] for t in out}
    definition=next(t for t in out if t['name']=='computer_action')
    assert set(definition['_meta']['securitySchemes'][0]['scopes'])=={'read','computer'}
    assert definition['securitySchemes']==definition['_meta']['securitySchemes']
    metadata=s.client.get('/.well-known/oauth-authorization-server').json()
    assert 'computer' in metadata['scopes_supported']


def test_real_mcp_images_actions_idempotency_and_polling(desktop):
    s=desktop
    opened,_=rpc(s,'computer_session_open',{'app':'Fixture'})
    session=opened['session_id']
    try:
        view,result=rpc(s,'computer_observe',{'session_id':session})
        assert any(b['type']=='image' for b in result['content'])
        encoded=next(b['data'] for b in result['content'] if b['type']=='image')
        assert encoded not in json.dumps(result['structuredContent'])
        assert view['images'][0]['width']==100
        args={'session_id':session,'observation_id':view['observation_id'],'action':{'type':'type_text','text':'Fixture 你好'},'idempotency_key':'fixture-once-'+uuid.uuid4().hex}
        before=int((s.provider/'state.txt').read_text())
        action,result=rpc(s,'computer_action',args)
        assert action['action_outcome']=='completed' and any(b['type']=='image' for b in result['content'])
        replay,_=rpc(s,'computer_action',args)
        assert action['operation_id']==replay['operation_id'] and int((s.provider/'state.txt').read_text())==before+1
        _,bad=rpc(s,'computer_action',{**args,'idempotency_key':'other-'+uuid.uuid4().hex},allow_error=True)
        assert bad['isError']
        for name in ['operations_get','operations_wait']:
            result=s.mcp(name,{'operation_id':action['operation_id']},s.computer_pat)
            assert any(b['type']=='image' for b in result['content'])
            assert encoded not in json.dumps(result['structuredContent'])
        for record in [s.client.get('/api/operations/'+action['operation_id']).json()]:
            assert 'Fixture 你好' not in json.dumps(record['args_summary'])
    finally:release(s,session)


def test_cross_grant_force_and_result_access_boundaries(desktop):
    s=desktop
    opened,_=rpc(s,'computer_session_open',{'app':'Fixture'});session=opened['session_id']
    try:
        token=s.must(s.client.post('/api/grants',json={'label':'Other desktop fixture','scopes':['read','computer'],'projects':[s.project['id']],'days':1}))['token']
        _,denied=rpc(s,'computer_observe',{'session_id':session},token=token,allow_error=True)
        assert denied['isError']
        _,denied=rpc(s,'computer_session_close',{'force':True},allow_error=True)
        assert denied['isError'] and denied['structuredContent']['error']['code']=='COMPUTER_FORCE_DENIED'
        view,_=rpc(s,'computer_observe',{'session_id':session})
        db=sqlite3.connect(s.hubdir/'hub.sqlite3')
        try:
            db.execute('UPDATE grants SET scopes=? WHERE id=?',(json.dumps(['read']),s.computer_grant));db.commit()
            result=s.mcp('operations_get',{'operation_id':view['operation_id']},s.computer_pat)
            assert result['isError'] and result['structuredContent']['error']['code']=='INSUFFICIENT_SCOPE'
        finally:
            db.execute('UPDATE grants SET scopes=? WHERE id=?',(json.dumps(['read','computer']),s.computer_grant));db.commit();db.close()
    finally:release(s,session)


def test_native_read_error_and_ambiguous_input_not_reported_as_success(desktop):
    s=desktop
    opened,_=rpc(s,'computer_session_open',{'app':'Fixture'});session=opened['session_id']
    try:
        (s.provider/'read-error').touch()
        _,result=rpc(s,'computer_observe',{'session_id':session},allow_error=True)
        assert result['isError']
        (s.provider/'read-error').unlink()
        view,_=rpc(s,'computer_observe',{'session_id':session})
        (s.provider/'disconnect-action').touch()
        before=int((s.provider/'state.txt').read_text())
        args={'session_id':session,'observation_id':view['observation_id'],'action':{'type':'press_key','key':'a'},'idempotency_key':'uncertain-'+uuid.uuid4().hex}
        _,result=rpc(s,'computer_action',args,allow_error=True)
        assert result['isError']
        _,replay=rpc(s,'computer_action',args,allow_error=True)
        assert replay['isError'] and int((s.provider/'state.txt').read_text())==before+1
    finally:
        (s.provider/'read-error').unlink(missing_ok=True);(s.provider/'disconnect-action').unlink(missing_ok=True)
        release(s,session)


def test_panel_emergency_close_interrupts_an_inflight_action(desktop):
    s=desktop
    opened,_=rpc(s,'computer_session_open',{'app':'Fixture'});session=opened['session_id']
    view,_=rpc(s,'computer_observe',{'session_id':session})
    # A transport timeout in the fixture is short, but the panel close must work without waiting for its action lock.
    (s.provider/'hang-action').touch()
    import concurrent.futures
    try:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future=pool.submit(rpc,s,'computer_action',{'session_id':session,'observation_id':view['observation_id'],'action':{'type':'type_text','text':'fixture only'}},allow_error=True)
            wait_for(lambda:any(json.loads(line)['name']=='type_text' and json.loads(line)['args'].get('text')=='fixture only' for line in (s.provider/'calls.jsonl').read_text().splitlines()))
            start=time.monotonic()
            result=s.call('computer_session_close',{'project':'Imago','force':True,'idempotency_key':'force-'+uuid.uuid4().hex})
            if result.get('pending'):result=s.poll(result['operation_id'])['result']['data']
            assert result['closed'] and time.monotonic()-start<4
            assert future.result(timeout=10)[1]['isError']
    finally:(s.provider/'hang-action').unlink(missing_ok=True)


def test_browser_desktop_mobile_and_cleanup(desktop,tmp_path):
    s=desktop
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch()
        page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
        page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            page.goto(s.url+'/#workbench');page.fill('#password',s.password);page.click('#login-form button')
            expect(page.locator('[data-computer-use]')).to_be_visible();page.click('[data-computer-use]')
            expect(page.locator('#computer-state')).to_have_text('本机已授权')
            page.locator('#computer-open-form input').fill('Fixture');page.locator('#computer-open-form button').click()
            expect(page.locator('#computer-frame img')).to_be_visible(timeout=15000)
            assert page.locator('#computer-frame img').evaluate('(img)=>img.naturalWidth')==100
            assert page.evaluate('window.desktopXss') is None
            page.screenshot(path=str(tmp_path/'computer-desktop.png'),full_page=True)
            for width,height in [(390,844),(320,568)]:
                page.set_viewport_size({'width':width,'height':height})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(tmp_path/f'computer-mobile-{width}.png'),full_page=True)
            page.click('#computer-close');expect(page.locator('#computer-state')).to_have_text('联调已结束')
            assert not errors,errors
        finally:browser.close()


def test_stdio_bridge_preserves_screenshot_blocks(desktop,tmp_path):
    import os,subprocess,sys
    s=desktop
    opened,_=rpc(s,'computer_session_open',{'app':'Fixture'});session=opened['session_id']
    try:
        token=tmp_path/'computer-token.txt';token.write_text(s.computer_pat);token.chmod(0o600)
        requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'fixture-bridge','version':'1'}}},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'computer_observe','arguments':{'project':'Imago','session_id':session}}}]
        env={**os.environ,'CODEPIER_HUB_URL':s.url,'CODEPIER_TOKEN_FILE':str(token)}
        result=subprocess.run([sys.executable,'-m','scripts.mcp_stdio_bridge'],input='\n'.join(json.dumps(r) for r in requests)+'\n',text=True,capture_output=True,env=env,timeout=25)
        assert result.returncode==0,result.stderr
        assert s.computer_pat not in result.stdout+result.stderr
        responses=[json.loads(line) for line in result.stdout.splitlines()]
        output=responses[-1]['result'];data=output['structuredContent']
        if data.get('pending'):
            output=s.mcp('operations_wait',{'operation_id':data['operation_id'],'wait_seconds':5},s.computer_pat)
        assert not output['isError']
        assert any(b['type']=='image' for b in output['content'])
        encoded=next(b['data'] for b in output['content'] if b['type']=='image')
        assert encoded not in output['content'][0]['text']
    finally:release(s,session)


def test_agent_restart_keeps_input_receipt_but_not_desktop_lease(desktop):
    s=desktop
    opened,_=rpc(s,'computer_session_open',{'app':'Fixture'});session=opened['session_id']
    view,_=rpc(s,'computer_observe',{'session_id':session})
    args={'session_id':session,'observation_id':view['observation_id'],'action':{'type':'press_key','key':'Return'},'idempotency_key':'restart-once-'+uuid.uuid4().hex}
    action,_=rpc(s,'computer_action',args)
    before=(s.provider/'state.txt').read_text()
    s.stop_agent();s.start_agent()
    replay,_=rpc(s,'computer_action',args)
    assert replay['operation_id']==action['operation_id'] and (s.provider/'state.txt').read_text()==before
    _,stale=rpc(s,'computer_observe',{'session_id':session},allow_error=True)
    assert stale['isError']
    release(s,session)


def test_local_computer_cli_preserves_pairing_and_never_grants_by_default(desktop,tmp_path):
    import subprocess,sys
    s=desktop
    config=json.loads(s.config_path.read_text());config.pop('computer',None)
    config['state_dir']=str(tmp_path/'agent-state')
    path=tmp_path/'config.json';path.write_text(json.dumps(config));path.chmod(0o600)
    base=[sys.executable,'-m','agent','--config',str(path)]
    run=lambda *args:subprocess.run([*base,*args],text=True,capture_output=True,timeout=12)
    rejected=run('configure','--computer','enabled')
    assert rejected.returncode!=0
    assert json.loads(path.read_text())==config
    accepted=run('configure','--computer','enabled','--computer-project','Imago','--computer-app','Fixture')
    assert accepted.returncode==0,accepted.stderr
    saved=json.loads(path.read_text())
    assert saved['computer']['enabled'] and saved['computer']['projects']==['Imago'] and saved['computer']['allowed_apps']==['Fixture']
    for key in ('hub_url','device_id','device_token','shared_key','allowed_roots','tasks'):
        if key in config:assert saved[key]==config[key]
    stop=run('computer-stop');assert stop.returncode==0 and (Path(saved['state_dir'])/'computer-use.stopped').exists()
    resume=run('computer-resume');assert resume.returncode==0 and not (Path(saved['state_dir'])/'computer-use.stopped').exists()
    disabled=run('configure','--computer','disabled');assert disabled.returncode==0 and not json.loads(path.read_text())['computer']['enabled']
