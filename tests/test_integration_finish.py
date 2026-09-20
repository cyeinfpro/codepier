"""Final interoperability and distribution regressions, not UI-only assertions."""
import hashlib
import io
import json
from pathlib import Path
import uuid
import zipfile
import httpx
import pytest
from pydantic import ValidationError
from shared.integration_contracts import NativeFile
from shared.mcp_protocol import MODERN,PREFIX
from scripts.mcp_stdio_bridge import prepare_request,forward
from tests.support import BASE
from tests.test_agentdock_context import workspace
from tests.test_integrations_stack import integrated_stack,modern,resolved

@pytest.mark.parametrize('method,params',[
 ('server/discover',{}),('tools/list',{}),('prompts/list',{}),('resources/list',{}),
 ('resources/templates/list',{}),('resources/read',{'uri':'rd://projects'})])
def test_modern_cache_contract_is_required_and_private(integrated_stack,method,params):
    response=modern(integrated_stack,method,params)
    assert response.status_code==200,response.text
    result=response.json()['result']
    assert result['resultType']=='complete' and result['ttlMs']==0 and result['cacheScope']=='private'
    assert PREFIX+'serverInfo' in result['_meta']
    assert response.headers['cache-control']=='no-store'

def test_missing_resource_has_era_specific_error(integrated_stack):
    assert modern(integrated_stack,'resources/read',{'uri':'rd://missing'}).json()['error']['code']==-32602
    assert integrated_stack.rpc('resources/read',{'uri':'rd://missing'}).json()['error']['code']==-32002

def test_native_file_optional_metadata_never_weakens_identity():
    value={'download_url':'https://files.oaiusercontent.com/test','file_id':'file-fixture'}
    for extra in ({},{'mime_type':None,'file_name':None,'name':None},{'mime_type':'image/png'}):
        assert NativeFile.model_validate({**value,**extra}).file_id=='file-fixture'
    schema=NativeFile.model_json_schema()
    assert set(schema['required'])=={'download_url','file_id'}
    assert {'mime_type','file_name'}<=set(schema['properties'])
    for change in ({'file_id':None},{'download_url':None},{'file_id':''},{'cookie':'no'}):
        with pytest.raises(ValidationError):NativeFile.model_validate({**value,**change})

def test_stdio_modern_retries_transport_id_not_business_operation():
    calls=[]
    def handler(request):
        body=json.loads(request.content);calls.append(body)
        assert request.headers['Mcp-Method']=='tools/call' and request.headers['Mcp-Name']=='validation_run'
        if len(calls)==1:return httpx.Response(503,request=request)
        return httpx.Response(200,request=request,json={'jsonrpc':'2.0','id':body['id'],'result':{'resultType':'complete','structuredContent':{'pending':True,'operation_id':'a'*32},'content':[]}})
    message=prepare_request({'jsonrpc':'2.0','id':'caller-id','method':'tools/call','params':{
        'name':'validation_run','arguments':{'project':'Imago','command':'printf fixture'},
        '_meta':{PREFIX+'protocolVersion':MODERN,PREFIX+'clientCapabilities':{}}}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=forward(client,'https://fixture.invalid',message,{},retry_seconds=5,sleep=lambda _:None)
    assert result['id']=='caller-id' and len(calls)==2
    assert calls[0]['id']!=calls[1]['id']
    assert calls[0]['params']['arguments']==calls[1]['params']['arguments']==message['params']['arguments']
    assert message['params']['arguments']['idempotency_key']

def test_validation_cannot_certify_an_unrelated_cwd(integrated_stack):
    s=integrated_stack;outside=s.directory/'unrelated';outside.mkdir(exist_ok=True)
    response=s.mcp('validation_run',{'project':'Imago','command':'printf no > MUST_NOT_EXIST','cwd':str(outside),'idempotency_key':uuid.uuid4().hex})['structuredContent']
    if response.get('pending'):
        op=s.poll(response['operation_id']);assert op['state']=='failed' and op['result']['error']['code']=='VALIDATION_CWD',op
    else:assert response['error']['code']=='VALIDATION_CWD'
    assert not (outside/'MUST_NOT_EXIST').exists()
    # Ordinary owner-enabled shell retains its existing full-access semantics.
    ordinary=resolved(s,'shell_exec',{'command':'pwd','cwd':str(outside)})
    assert ordinary['exit_code']==0 and str(outside) in ordinary['output']

def test_setup_links_download_complete_current_assets(integrated_stack):
    s=integrated_stack
    response=s.client.get('/static/browser-extension.zip');assert response.status_code==200
    from scripts.build_integration_assets import EXTENSION_FILES
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert set(archive.namelist())==set(EXTENSION_FILES)
        for name in EXTENSION_FILES:assert archive.read(name)==(BASE/'web/browser-extension'/name).read_bytes()
        manifest=json.loads(archive.read('manifest.json'))
        assert not manifest.get('host_permissions') and manifest['optional_host_permissions']
    guide=s.client.get('/static/integration-guide.md')
    assert guide.status_code==200 and guide.content==(BASE/'docs/INTEGRATIONS-20260917.md').read_bytes()
    meta=json.loads((BASE/'web/integration-assets.json').read_text())
    for name,entry in meta['outputs'].items():assert hashlib.sha256((BASE/'web'/name).read_bytes()).hexdigest()==entry['sha256']

def test_built_apps_and_source_bundle_include_distributable_assets():
    from scripts.build_source_bundle import include
    assert include(Path('web/browser-extension.zip'))
    assert not include(Path('web/private-backup.zip'))
    meta=json.loads((BASE/'web/mcp-apps/manifest.json').read_text())
    for name,entry in meta['outputs'].items():assert hashlib.sha256((BASE/'web/mcp-apps'/name).read_bytes()).hexdigest()==entry['sha256']
    assert (BASE/'web/mcp-apps/THIRD_PARTY_NOTICES.txt').is_file()


def test_installed_agent_package_contains_self_sufficient_owner_tools(tmp_path):
    import os,subprocess,sys
    from hub.agent_install import AgentPackage
    from scripts.install_agent import unpack
    bundle=AgentPackage(BASE).build();archive=tmp_path/'agent.zip';archive.write_bytes(bundle.content)
    runtime=tmp_path/'runtime';runtime.mkdir();unpack(archive,bundle.sha256,runtime)
    expected={'agent/setup_integrations.py','agent/install_browser_bridge.py','agent/codepier_browser_host.py','agent/codepier_control.py','agent/lsp_navigation.py','agent/background_browser.py'}
    with zipfile.ZipFile(archive) as z:assert expected<=set(z.namelist())
    env={k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','PYTHONHOME'}};env['PYTHONNOUSERSITE']='1'
    for module in ('setup_integrations','install_browser_bridge','codepier_browser_host','codepier_control'):
        command=[sys.executable,'-m','agent.'+module,'--help']
        result=subprocess.run(command,cwd=runtime,env=env,capture_output=True,text=True,timeout=10)
        assert result.returncode==0,result.stdout+result.stderr
        assert 'usage:' in result.stdout
    assert not (runtime/'scripts/setup_integrations.py').exists()


def test_local_owner_stop_releases_old_active_browser_lease_across_grants(workspace):
    import asyncio
    from types import SimpleNamespace
    from agent.integration_state import Records
    from agent.integration_control import Controls
    from agent.integration_local import LocalServer
    from agent.background_browser import BrowserBroker
    from tests.test_integrations_core import bound
    engine,base,root=workspace
    project=bound(base,device_id='device-test')
    records=Records(engine.journal)
    active=uuid.uuid4().hex
    records.save('browser',active,project,{'lease_id':active,'state':'ready','expires':9999999999})
    for _ in range(105):
        identifier=uuid.uuid4().hex
        records.save('browser',identifier,project,{'lease_id':identifier,'state':'closed','expires':1})
    agent=SimpleNamespace(engine=engine,journal=engine.journal,state_dir=engine.journal.directory,
        config={'integrations':{'local_control':True}},integration_projects={},integration_tool_names={},
        jobs={},processes={},native=SimpleNamespace(live=lambda:[]))
    broker=BrowserBroker(agent,records);calls=[]
    async def rpc(action,body,**kwargs):calls.append(body['lease_id']);return {'tab_cleanup_confirmed':True}
    broker.rpc=rpc
    agent.integrations=SimpleNamespace(browser=broker,known_projects=lambda:[project],control=Controls(agent))
    local=LocalServer(agent,broker)
    args={'project':project['alias'],'confirm':project['alias'],'action':'stop','idempotency_key':uuid.uuid4().hex}
    first=asyncio.run(local.route('/control',args));second=asyncio.run(local.route('/control',args))
    assert first==second and first['ok'] and first['data']['complete'],first
    assert calls==[active]
    assert records.load('browser',active,project)['state']=='closed'


def test_lsp_cold_index_requires_positive_protocol_readiness():
    import asyncio
    from types import SimpleNamespace
    from agent.lsp_navigation import Peer
    from shared.util import DevError
    async def scenario():
        peer=Peer(None,'fixture',{},Path('.'),{'timeout_seconds':.03})
        async def unchanged(*args):return {'kind':'unchanged','resultId':'not-requested'}
        peer.request=unchanged
        with pytest.raises(DevError,match='尚未确认'):await peer.await_source_diagnostics('file:///fixture',{'diagnosticProvider':True})
        peer.diagnostics['file:///fixture']={'version':0,'diagnostics':[]}
        with pytest.raises(DevError,match='冷索引'):await peer.await_source_diagnostics('file:///fixture',{})
        peer.diagnostics['file:///fixture']={'version':1,'diagnostics':[]}
        assert await peer.await_source_diagnostics('file:///fixture',{})=='source_diagnostics_observed'
    asyncio.run(scenario())


def test_legacy_project_frames_preserve_admission_without_bypassing_root_pause(workspace):
    from types import SimpleNamespace
    from agent.integration_control import Controls
    from shared.util import DevError
    engine,project,root=workspace
    control=Controls(SimpleNamespace(journal=engine.journal))
    legacy={'root':str(root),'alias':'legacy','mode':'write','_execution_policy':None}
    assert not control.state(legacy)['paused']
    control.guard('fs_write',legacy)
    control.set({**legacy,'id':'real-authenticated-project'},True)
    with pytest.raises(DevError,match='暂停'):control.guard('fs_write',legacy)
    control.guard('fs_read',legacy)
    control.set({**legacy,'id':'real-authenticated-project'},False)
    control.guard('fs_write',legacy)


def test_modern_does_not_advertise_a_retired_handshake_or_ping(integrated_stack):
    for method in ('initialize','ping'):
        result=modern(integrated_stack,method).json()
        assert result['error']['code']==-32601,result
    assert integrated_stack.rpc('ping').json()['result']=={}
