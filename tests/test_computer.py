import asyncio,base64,copy,json,sqlite3,sys,time
from pathlib import Path
import pytest
from pydantic import ValidationError
from agent.computer import Computer,NativeClient,validate_computer,discover_provider
from shared.computer_contracts import ComputerAction,ComputerOpen,COMPUTER_TOOLS,NATIVE_ACTIONS,NATIVE_TOOLS
from shared.computer_media import normalize_content,mcp_result,purge_database,scrub_expired,MEDIA_TTL_SECONDS
from shared.contracts import TOOLS,tool_definitions
from shared.util import DevError,safe_summary
from tests.fake_computer_provider import frame,png,schema

PROJECT={'id':'p1','alias':'Test','root':'/fixture','_computer_owner':'grant:g1','_computer_admin':False}
CONFIG={'computer':{'enabled':True,'projects':['p1','p2'],'allowed_apps':['Fixture']}}

class FakeClient:
    instances=[]
    def __init__(self,*args):
        self.closed=False;self.tools={n:{'inputSchema':schema(n)} for n in NATIVE_TOOLS};self.server_info={};self.calls=[];self.value=0;self.fail=False
        self.instances.append(self)
    async def start(self):return self
    def validate_call(self,name,args):
        from jsonschema import Draft202012Validator
        Draft202012Validator(self.tools[name]['inputSchema']).validate(args)
    async def call(self,name,args):
        self.calls.append((name,copy.deepcopy(args)))
        if name=='get_app_state':return frame(self.value)
        if name=='list_apps':return {'content':[{'type':'text','text':'Fixture'}]}
        self.value+=1
        if self.fail:raise DevError('COMPUTER_TIMEOUT','fixture timeout')
        return {'content':[{'type':'text','text':'done'}]}
    async def close(self):self.closed=True

async def opened(tmp_path):
    config=copy.deepcopy(CONFIG);manager=Computer(lambda:config,tmp_path,client_factory=FakeClient)
    r=await manager.execute('computer_session_open',PROJECT,{'app':'Fixture','ttl_seconds':300})
    r=await manager.execute('computer_observe',PROJECT,{'session_id':r['session_id']})
    return manager,config,r

def fixture_provider(path):
    path=Path(path);(path/'bin').mkdir(parents=True)
    launcher=path/'bin/computer-use-client-launcher'
    launcher.write_text('#!'+sys.executable+'\nimport sys\nsys.path.insert(0,'+repr(str(Path(__file__).resolve().parents[1]))+')\nfrom tests.fake_computer_provider import main\nmain()\n')
    launcher.chmod(0o700)
    return path

@pytest.mark.parametrize('value',[None,[],{'enabled':'true'},{'enabled':True},{'projects':'*'},{'allowed_apps':[1]},{'call_timeout_seconds':True},{'plugin_root':'relative'},{'max_session_seconds':3601},{'unknown':True}])
def test_invalid_local_config(value):
    with pytest.raises(ValueError):validate_computer(value)

def test_default_off_and_explicit_config():
    assert not validate_computer({})['enabled']
    assert validate_computer(CONFIG['computer'])['enabled']
    assert not discover_provider(validate_computer({'plugin_root':'/nonexistent-computer-plugin'}))['available']

@pytest.mark.parametrize('action',[
    {'type':'click'}, {'type':'click','x':1}, {'type':'click','x':1,'y':2,'element_index':'3'},
    {'type':'click','x':-1,'y':2}, {'type':'click','x':1,'y':2,'click_count':0},
    {'type':'click','element_index':'3','click_count':True}, {'type':'click','element_index':'3','mouse_button':'extra'},
    {'type':'scroll','element_index':'3','direction':'diagonal'}, {'type':'scroll','element_index':'3','direction':'up','pages':0},
    {'type':'press_key','key':''}, {'type':'drag','from_x':1,'from_y':1,'to_x':float('nan'),'to_y':2},
    {'type':'type_text','text':'hello','app':'Other'}, {'type':'shell_exec','command':'echo nope'},
    {'type':'select_text','element_index':'1','text':'abc','selection':'everything'}])
def test_invalid_actions_rejected_before_dispatch(action):
    with pytest.raises(ValidationError):ComputerAction.model_validate({'project':'Test','session_id':'a'*32,'observation_id':'b'*32,'idempotency_key':'test-key','action':action})

@pytest.mark.parametrize('action',[
    {'type':'click','element_index':'1','click_count':2}, {'type':'click','x':4,'y':5,'mouse_button':'right'},
    {'type':'press_key','key':'super+c'},{'type':'type_text','text':'你好，世界'},
    {'type':'scroll','element_index':'2','direction':'down','pages':0.5},
    {'type':'drag','from_x':1,'from_y':1,'to_x':90,'to_y':50},
    {'type':'set_value','element_index':'1','value':''},{'type':'select_text','element_index':'1','text':'hello','selection':'cursor_after'},
    {'type':'perform_secondary_action','element_index':'2','action':'AXShowMenu'}])
@pytest.mark.asyncio
async def test_all_native_actions_have_app_binding_and_new_observation(tmp_path,action):
    manager,_,r=await opened(tmp_path)
    try:
        parsed=ComputerAction.model_validate({'project':'Test','session_id':r['session_id'],'observation_id':r['observation_id'],'idempotency_key':'test-key','action':action}).model_dump()
        out=await manager.execute('computer_action',PROJECT,parsed)
        assert out['action_outcome']=='completed'
        assert out['observation_id']!=r['observation_id'] and out['images'][0]['width']==100
        inputs=[(n,a) for n,a in manager.session['client'].calls if n in NATIVE_ACTIONS]
        assert len(inputs)==1 and inputs[0][1]['app']=='Fixture'
        with pytest.raises(DevError,match='过期|消费|替换'):
            await manager.execute('computer_action',PROJECT,parsed)
    finally:await manager.close()

@pytest.mark.asyncio
async def test_device_lease_owner_app_and_project_isolation(tmp_path):
    manager,_,r=await opened(tmp_path)
    try:
        with pytest.raises(DevError) as error:await manager.execute('computer_session_open',{**PROJECT,'id':'p2'},{'app':'Fixture','ttl_seconds':300})
        assert error.value.code=='COMPUTER_BUSY'
        for project in [{**PROJECT,'_computer_owner':'other'},{**PROJECT,'id':'p2'},{**PROJECT,'root':'/different'}]:
            with pytest.raises(DevError):await manager.execute('computer_observe',project,{'session_id':r['session_id']})
        with pytest.raises(DevError):await manager.execute('computer_session_close',PROJECT,{'force':True})
        await manager.execute('computer_session_close',PROJECT,{'session_id':r['session_id']})
        with pytest.raises(DevError) as error:await manager.execute('computer_session_open',PROJECT,{'app':'Forbidden','ttl_seconds':300})
        assert error.value.code=='COMPUTER_APP_DENIED'
    finally:await manager.close()

@pytest.mark.asyncio
async def test_changed_stale_and_out_of_bounds_screens_no_input(tmp_path):
    manager,config,r=await opened(tmp_path)
    try:
        args={'session_id':r['session_id'],'observation_id':r['observation_id'],'action':{'type':'click','x':100,'y':1}}
        with pytest.raises(DevError) as error:await manager.execute('computer_action',PROJECT,args)
        assert error.value.code=='COMPUTER_COORDINATES'
        args['action']={'type':'click','element_index':'1'}
        manager.session['client'].value=5
        with pytest.raises(DevError) as error:await manager.execute('computer_action',PROJECT,args)
        assert error.value.code=='COMPUTER_SCREEN_CHANGED'
        r=await manager.execute('computer_observe',PROJECT,{'session_id':r['session_id']})
        manager.session['observation']['at']-=121;args['observation_id']=r['observation_id']
        with pytest.raises(DevError) as error:await manager.execute('computer_action',PROJECT,args)
        assert error.value.code=='COMPUTER_STALE_OBSERVATION'
        assert not any(n in NATIVE_ACTIONS for n,a in manager.session['client'].calls)
    finally:await manager.close()

@pytest.mark.asyncio
async def test_uncertain_input_stops_without_retry(tmp_path):
    manager,_,r=await opened(tmp_path);client=manager.session['client'];client.fail=True
    try:
        with pytest.raises(DevError) as error:await manager.execute('computer_action',PROJECT,{'session_id':r['session_id'],'observation_id':r['observation_id'],'action':{'type':'type_text','text':'once'}})
        assert error.value.code=='COMPUTER_ACTION_UNCERTAIN'
        assert client.value==1 and client.closed and manager.session is None
    finally:await manager.close()

@pytest.mark.asyncio
async def test_local_stop_config_revocation_and_expiry(tmp_path):
    manager,config,r=await opened(tmp_path);client=manager.session['client']
    try:
        config['computer']['enabled']=False
        with pytest.raises(DevError):await manager.execute('computer_observe',PROJECT,{'session_id':r['session_id']})
        await asyncio.sleep(1.1)
        assert client.closed and manager.session is None
        config['computer']['enabled']=True;manager.stop_file.touch()
        with pytest.raises(DevError) as error:await manager.execute('computer_session_open',PROJECT,{'app':'Fixture','ttl_seconds':300})
        assert error.value.code=='COMPUTER_STOPPED'
        manager.stop_file.unlink()
        await manager.execute('computer_session_open',PROJECT,{'app':'Fixture','ttl_seconds':30})
        manager.session['deadline']=time.monotonic()-1
        await asyncio.sleep(1.1)
        assert manager.session is None
    finally:await manager.close()

@pytest.mark.asyncio
async def test_queued_input_deadline(tmp_path):
    manager,_,r=await opened(tmp_path)
    try:
        with pytest.raises(DevError) as error:await manager.execute('computer_action',PROJECT,{'session_id':r['session_id'],'observation_id':r['observation_id'],'action':{'type':'press_key','key':'a'}},not_after=time.time()-1)
        assert error.value.code=='QUEUE_EXPIRED'
        assert manager.session['client'].value==0
    finally:await manager.close()

@pytest.mark.asyncio
async def test_real_stdio_catalog_unicode_and_disconnect(tmp_path):
    root=fixture_provider(tmp_path/'provider');c=validate_computer({'plugin_root':str(root)})
    client=NativeClient(discover_provider(c),5)
    try:
        await client.start();assert set(client.tools)==NATIVE_TOOLS
        view=normalize_content(await client.call('get_app_state',{'app':'Fixture'}));assert view['images'][0]['width']==100
        await client.call('type_text',{'app':'Fixture','text':'你好'})
        (root/'disconnect-action').touch()
        with pytest.raises(DevError):await client.call('click',{'app':'Fixture','element_index':'1'})
        assert client.closed
        assert (root/'state.txt').read_text()=='2'
        with pytest.raises(DevError):await client.call('click',{'app':'Fixture','element_index':'1'})
        assert (root/'state.txt').read_text()=='2'
    finally:await client.close()

@pytest.mark.parametrize('tool',['computer_observe','operations_wait','operations_get'])
def test_native_mcp_images_not_base64_text(tool):
    data=normalize_content(frame());base64_image=data['_computer_content'][0]['data']
    value={'operation_id':'o',**data} if tool=='computer_observe' else {'operation_id':'o','tool':'computer_observe','state':'succeeded','pending':False,'result':{'ok':True,'data':data}}
    result=mcp_result(tool,value)
    assert any(b['type']=='image' for b in result['content'])
    assert base64_image not in json.dumps(result['structuredContent']) and base64_image not in result['content'][0]['text']
    assert '_computer_content' in data # formatter did not mutate durable data

@pytest.mark.parametrize('block',[{'type':'image','mimeType':'image/png','data':'not-base64'}, {'type':'image','mimeType':'image/svg+xml','data':'AAAA'}, {'type':'resource_link','uri':'file:///not-allowed'}, {'type':'image','mimeType':'image/png','data':base64.b64encode(b'bad').decode()}])
def test_bad_media_never_promoted(block):
    with pytest.raises(DevError):normalize_content({'content':[block]})

def test_retention_sanitizer_and_journal_purge(tmp_path):
    data=normalize_content(frame());data['computer_expires_at']=time.time()-1
    value={'ok':True,'data':data}
    db=sqlite3.connect(tmp_path/'media.sqlite');db.row_factory=sqlite3.Row
    for table in ['calls','operations']:
        db.execute(f'CREATE TABLE {table} (id TEXT,tool TEXT,result TEXT)')
        db.execute(f'INSERT INTO {table} VALUES (?,?,?)',('id','computer_observe',json.dumps(value)))
        assert purge_database(db,table)==1
        retained=json.loads(db.execute(f'SELECT result FROM {table}').fetchone()[0])['data']
        assert retained['media_expired'] and 'text' not in retained and '_computer_content' not in retained
        assert purge_database(db,table)==0
    out=mcp_result('computer_observe',data)
    assert len(out['content'])==1 and out['structuredContent']['media_expired']
    db.close()

def test_catalog_scopes_and_audit_redaction():
    tools={t['name']:t for t in tool_definitions()}
    assert 'computer' in tools and not COMPUTER_TOOLS.intersection(tools)
    assert TOOLS['computer_observe'].scope=='computer' and not tools['computer']['annotations']['readOnlyHint']
    for name in NATIVE_ACTIONS:
        summary=safe_summary({'action':{'type':name,'text':'SECRET','value':'SECRET','key':'SECRET','prefix':'SECRET','suffix':'SECRET'}})
        assert 'SECRET' not in json.dumps(summary)
    assert TOOLS['computer_action'].destructive
