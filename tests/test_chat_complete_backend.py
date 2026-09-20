"""No sockets or model requests: protocol wires, fake pipes, spool and HTTP handlers."""
import asyncio
import json
import os
from pathlib import Path
import sys
import threading
import time
import uuid
from types import SimpleNamespace

import pytest
from agent.chat_catalog import CatalogCache, Probe, commands, probe, public_model, validate_settings
from agent.chat_worker import Protocol
from tests.test_chat_admission import chat
from tests.test_native_cli import native
from tests.test_chat_sse import service, TestAuth, request, sync
from hub.native_cli import make_native_router


def protocol(cli='pi', saved=None):
    sent,events,finished=[],[],[]
    p=Protocol(cli,{'cwd':'.','chat_settings':json.dumps(saved or {})},sent.append,
               lambda k,**v:events.append((k,v)),lambda *v:finished.append(v),lambda _:None)
    return p,sent,events,finished


def reply(p,wire,data=None,success=True):
    p.receive({'id':wire['id'],**({'type':'response','success':success,'data':data or {},**({'error':'rejected'} if not success else {})} if p.provider=='pi' else ({'result':data or {}} if success else {'error':{'message':'rejected'}}))})


def test_recursive_catalog_sanitizer_and_skill_palette():
    model=public_model({'id':'x','baseUrl':'secret','headers':{'Authorization':'secret'},'name':{'key':'secret'},'supportedReasoningEfforts':[{'reasoningEffort':'low','headers':'secret'}]})
    assert 'secret' not in json.dumps(model)
    assert commands({'data':[{'skills':[{'name':'build','description':'Build','path':'private'}]}]},'codex')==[{'name':'build','description':'Build','source':'skill','invocation':'$build'}]


@pytest.mark.parametrize('cli,payload', [('pi',{'model':'bare'}),('pi',{'model':'p/'}),('codex',{'effort':'max'}),('pi',{'effort':'none'}),('codex',{'model':'x\n--flag'})])
def test_invalid_native_options(cli,payload):
    with pytest.raises(ValueError): validate_settings(payload,cli)


def test_canonical_provider_split_and_empty_defaults():
    assert validate_settings({'model':'provider/id/with/slash','effort':''},'pi')['model']=='provider/id/with/slash'
    p,w,e,f=protocol();p.ready=True;p.defaults={'model':'p/default','effort':'low'}
    p.change_settings('r',{'model':'','effort':''})
    assert w[-1]['provider']=='p' and w[-1]['modelId']=='default'


def test_pi_sequential_model_effort_confirmation_and_first_prompt_gate():
    p,w,e,f=protocol(saved={'next':{'model':'p/m/path','effort':'high'}});p.start()
    state=next(x for x in w if x['type']=='get_state')
    reply(p,state,{'model':{'id':'default','provider':'p'},'thinkingLevel':'low'})
    assert p.settings_busy and not any(x['type']=='prompt' for x in w)
    assert w[-1]['type']=='set_model' and w[-1]['modelId']=='m/path'
    reply(p,w[-1],{'id':'m/path','provider':'p'})
    assert w[-1]['type']=='get_available_thinking_levels'
    reply(p,w[-1],{'levels':['low','high']})
    assert w[-1]['type']=='set_thinking_level'
    reply(p,w[-1]);assert w[-1]['type']=='get_state'
    reply(p,w[-1],{'model':{'id':'m/path','provider':'p'},'thinkingLevel':'high'})
    assert not p.settings_busy
    p.prompt('first',{'text':'fixture'})
    assert w[-1]['type']=='prompt'
    assert json.loads(p.row['chat_settings'])['actual']['thinkingLevel']=='high'


@pytest.mark.parametrize('stage',['model','levels','effort','confirm'])
def test_settings_rejections_clear_busy_and_correlate(stage):
    p,w,e,f=protocol();p.ready=True
    p.change_settings('setting',{'model':'p/m','effort':'high'})
    if stage!='model': reply(p,w[-1],{'id':'m','provider':'p'})
    if stage in ('effort','confirm'): reply(p,w[-1],{'levels':['low','high']})
    if stage=='confirm': reply(p,w[-1])
    failed=w[-1];reply(p,failed,success=False)
    assert not p.settings_busy and f[-1]==('setting','error')
    assert any(k=='error' and v['receipt']=='setting' for k,v in e)
    reply(p,failed,{'thinkingLevel':'high'})
    assert f.count(('setting','error'))==1


def test_unsupported_effort_never_sent():
    p,w,e,f=protocol();p.ready=True
    p.change_settings('r',{'model':'p/m','effort':'max'})
    reply(p,w[-1]);reply(p,w[-1],{'levels':['low']})
    assert not any(x['type']=='set_thinking_level' for x in w)
    assert f[-1]==('r','error')


def test_codex_custom_model_first_turn_persistence_and_resume():
    p,w,e,f=protocol('codex',{'next':{'model':'custom','effort':'low'}})
    p.ready=True;p.thread='thread';p.prompt('r',{'text':'fixture'})
    assert w[-1]['method']=='turn/start' and w[-1]['params']['model']=='custom'
    assert w[-1]['params']['effort']=='low' and 'approvalPolicy' not in w[-1]['params']
    reply(p,w[-1],{'turn':{'id':'turn'}})
    saved=json.loads(p.row['chat_settings'])
    assert saved['actual']['model']=='custom'
    resumed,*_=protocol('codex',saved);assert resumed.next_settings=={'model':'custom','effort':'low'}
    assert not any(x.get('method')=='thread/settings/update' for x in w)


@pytest.mark.parametrize('cli',['pi','codex'])
def test_steering_preserves_parent_and_completes_on_acceptance(cli):
    p,w,e,f=protocol(cli)
    with pytest.raises(ValueError,match='active'):p.steer('s',{'text':'x'})
    p.active='parent';p.turn='observed';p.thread='thread'
    p.steer('s',{'text':'x'})
    assert not f and p.active=='parent'
    if cli=='codex': assert w[-1]['params']['expectedTurnId']=='observed'
    reply(p,w[-1]);assert f==[('s','completed')] and p.active=='parent'
    assert any(k=='user' and v['receipt']=='s' and v['parent_receipt']=='parent' for k,v in e)


@pytest.mark.parametrize('cli',['pi','codex'])
def test_command_whitelist_errors_and_refresh_receipts(cli):
    p,w,e,f=protocol(cli);p.ready=True;p.active='prompt';p.thread='thread'
    with pytest.raises(ValueError):p.command('bad',{'name':'bash'})
    p.command('cmd',{'name':'compact'})
    reply(p,w[-1],success=False)
    assert f[-1]==('cmd','error')
    assert any(k=='error' and v['receipt']=='cmd' for k,v in e)
    before=len(w);p.command('refresh',{'name':'refresh'})
    wires=w[before:]
    assert not any(x.get('method')=='turn/start' or x.get('type')=='prompt' for x in wires)
    for wire in wires:reply(p,wire,{} if cli=='codex' else {'levels':[]})
    assert ('refresh','completed') in f


def test_pi_global_toggles_are_explicitly_unavailable():
    p,w,e,f=protocol()
    for name in ('set_auto_compaction','set_auto_retry'):
        with pytest.raises(ValueError,match='globally'):p.command('r',{'name':name,'enabled':True})
    assert not w


def test_observed_session_and_structured_approval_copy():
    p,w,e,f=protocol('codex')
    decision={'acceptWithExecpolicyAmendment':{'execpolicy_amendment':['git','status']}}
    message={'id':7,'method':'item/commandExecution/requestApproval','params':{'availableDecisions':['acceptForSession',decision,'decline']}}
    p.receive(message)
    message['params']['availableDecisions'].append('forged')
    with pytest.raises(ValueError):p.answer(7,'forged')
    p.answer(7,decision);assert w[-1]['result']['decision']==decision
    with pytest.raises(ValueError):p.answer(7,decision)


def test_queue_cancel_idempotency_same_session_and_claim_cas(chat):
    obj,project,sid=chat
    target=uuid.uuid4().hex
    obj.action('chat_prompt',project,{'id':sid,'receipt':target,'text':'queued'})
    assert obj.action('chat_queue',project,{'id':sid})['commands'][0]['receipt']==target
    cancel={'id':sid,'receipt':uuid.uuid4().hex,'target':target}
    assert obj.action('chat_cancel',project,cancel)['target_state']=='cancelled'
    assert obj.action('chat_cancel',project,cancel)['state']=='completed'
    with obj.connect_db() as db:
        assert db.execute("UPDATE commands SET state='claimed' WHERE session=? AND id=? AND state='queued'",(sid,target)).rowcount==0
        db.execute("UPDATE commands SET state='claimed' WHERE id=?",(target,))
    with pytest.raises(Exception,match='cannot-cancel-use-interrupt'):obj.action('chat_cancel',project,{**cancel,'receipt':uuid.uuid4().hex})
    with pytest.raises(ValueError,match='Unknown'):obj.action('chat_cancel',project,{**cancel,'receipt':uuid.uuid4().hex,'target':uuid.uuid4().hex})


def test_start_selected_config_and_resume(chat):
    obj,project,sid=chat
    new=uuid.uuid4().hex
    obj.action('start',project,{'id':new,'mode':'chat','cli':'codex','model':'custom','effort':'low'})
    with obj.connect_db() as db:
        row=db.execute('SELECT * FROM sessions WHERE id=?',(new,)).fetchone()
        assert json.loads(row['chat_settings'])['next']=={'model':'custom','effort':'low'}
        db.execute("UPDATE sessions SET status='exited',native_thread='thread' WHERE id=?",(new,))
    resumed=uuid.uuid4().hex
    row=obj.action('start',project,{'id':resumed,'mode':'chat','cli':'codex','continue_session':new})
    assert json.loads(row['chat_settings'])['next']['model']=='custom'


def test_catalog_without_session_and_mapping_recheck(chat,monkeypatch):
    obj,project,sid=chat
    calls=[]
    def fake(*args, **kwargs):calls.append(args);return {'cli':'pi','models':[]}
    monkeypatch.setattr('agent.native_cli.probe',fake)
    result=obj.action('chat_catalog',project,{'cli':'pi','cwd':'.'})
    assert result['cli']=='pi' and len(calls)==1
    obj.action('chat_catalog',project,{'cli':'pi','cwd':'.'});assert len(calls)==1
    obj.action('chat_catalog',project,{'cli':'pi','cwd':'.','refresh':True});assert len(calls)==2


@pytest.mark.asyncio
@pytest.mark.parametrize('fmt',['md','json'])
async def test_full_export_click_boundary_sanitized_and_mapping_auth(service,fmt):
    obj,project,sid=service
    frames=[{'type':'user','text':f'line-{i}','details':{'headers':'secret'}} for i in range(350)]
    raw=b''.join((json.dumps(x)+'\n').encode() for x in frames)
    await sync(obj,sid,raw)
    auth=TestAuth();endpoint=next(r.endpoint for r in make_native_router(auth,obj.runtime).routes if r.path.endswith('/export'))
    response=await endpoint(sid,request(),fmt)
    await sync(obj,sid,b'{"type":"user","text":"too-late"}\n',len(raw))
    data=''.join([x async for x in response.body_iterator])
    assert 'line-349' in data and 'too-late' not in data and 'secret' not in data
    assert 'attachment;' in response.headers['content-disposition']
    if fmt=='json':assert len(json.loads(data))==350
    project['allow_tasks']=False
    with pytest.raises(Exception,match='revoked'):await endpoint(sid,request(),fmt)


def test_catalog_cache_coalesces_and_refresh_bypasses():
    cache=CatalogCache();entered=threading.Event();release=threading.Event();calls=[];results=[]
    def loader():calls.append(1);entered.set();release.wait(2);return {'models':[]}
    threads=[threading.Thread(target=lambda:results.append(cache.get('k',loader))) for _ in range(2)]
    for t in threads:t.start()
    assert entered.wait(1);time.sleep(.03);release.set()
    for t in threads:t.join(2)
    assert len(calls)==1 and len(results)==2
    cache.get('k',loader,True);assert len(calls)==2


def test_probe_timeout_reaps_owned_process(tmp_path):
    p=Probe([sys.executable,'-c','import time; time.sleep(10)'],tmp_path,dict(os.environ),timeout=.1)
    try:
        with pytest.raises(TimeoutError):p.call('get_state',pi=True)
    finally:p.close()
    assert p.child.poll() is not None


@pytest.mark.parametrize('cli',['pi','codex'])
def test_catalog_fake_pipe_never_prompts_or_creates_thread(tmp_path,cli):
    script=tmp_path/'native'
    wire=tmp_path/'wire'
    script.write_text('#!'+sys.executable+'\n'+'''import json,sys
for line in sys.stdin:
 m=json.loads(line)
 with open('wire','a') as f:f.write(line)
 method=m.get('type',m.get('method'))
 if 'id' not in m:continue
 assert method not in ('prompt','turn/start','thread/start')
 data={'get_state':{'model':{'id':'m','provider':'p'},'thinkingLevel':'low'},'get_available_models':{'models':[{'id':'m','provider':'p','headers':{'Authorization':'secret'}}]},'get_commands':{'commands':[]},'get_available_thinking_levels':{'levels':['low']},'set_model':{'id':'m','provider':'p'},'model/list':{'data':[{'id':'builtin'}]},'config/read':{'config':{'model':'custom','api_key':'secret'}},'skills/list':{'data':[]}}.get(method,{})
 print(json.dumps({'id':m['id'],**({'type':'response','success':True,'data':data} if 'type' in m else {'result':data})}),flush=True)
''')
    script.chmod(0o700)
    result=probe(cli,str(script),tmp_path,dict(os.environ),'p/m' if cli=='pi' else '')
    assert 'secret' not in json.dumps(result)
    assert 'thread/start' not in wire.read_text()
    if cli=='codex':
        assert result['model']['configured'] and result['thinking_levels']==[]


def test_queued_answer_is_bound_to_observed_request_and_parent(chat):
    import hashlib
    obj,project,sid=chat
    native_request={'id':'q','type':'extension_ui_request','method':'confirm','title':'Allow?'}
    event={'type':'approval','receipt':'parent','request_id':'q','method':'confirm','details':native_request}
    with obj.connect_db() as db:db.execute('INSERT INTO output VALUES (?,?,?)',(sid,0,(json.dumps(event)+'\n').encode()))
    receipt=uuid.uuid4().hex
    obj.action('chat_answer',project,{'id':sid,'receipt':receipt,'request_id':'q','answer':False})
    with obj.connect_db() as db:payload=json.loads(db.execute('SELECT payload FROM commands WHERE id=?',(receipt,)).fetchone()[0])
    p,w,e,f=protocol();p.active='parent';p.receive(native_request)
    p.active='new-parent'
    with pytest.raises(ValueError,match='changed'):p.answer('q',False,payload)
    p.active='parent';p.answer('q',False,payload)
    assert w[-1]['confirmed'] is False


def test_permissions_only_grant_observed_subset():
    p,w,e,f=protocol('codex')
    p.receive({'id':7,'method':'item/permissions/requestApproval','params':{'permissions':{'network':{'enabled':True},'fileSystem':{'write':['/tmp/allowed']}}}})
    with pytest.raises(ValueError):p.answer(7,{'permissions':{'fileSystem':{'write':['/etc']}}})
    p.answer(7,{'permissions':{'network':{'enabled':True}},'scope':'session'})
    assert w[-1]['result']['scope']=='session'


@pytest.mark.asyncio
async def test_catalog_hub_rechecks_mapping_after_await(service):
    obj,project,sid=service
    auth=TestAuth()
    endpoint=next(r.endpoint for r in make_native_router(auth,obj.runtime).routes if r.path=='/api/native/{action}')
    async def native_request(action,p,args):
        project['root']='/changed'
        return {'models':[]}
    obj.request=native_request
    req=request([(b'x-rd-csrf',b'valid')],json.dumps({'project':'project','args':{'cli':'pi'}}).encode())
    with pytest.raises(Exception,match='映射改变'):await endpoint('chat_catalog',req)


def test_old_protected_launch_checks_except_replaced_model_limitation(chat):
    obj,p,sid=chat
    for options in ({'argv':['--dangerously-bypass-approvals-and-sandbox']},{'cwd':'../'}, {'resume':True},{'provider':'override'}):
        with pytest.raises(Exception):obj.action('start',p,{'id':uuid.uuid4().hex,'cli':'codex','mode':'chat',**options})
    with pytest.raises(ValueError):obj.action('chat_arbitrary_rpc',p,{'id':sid,'receipt':uuid.uuid4().hex})


def test_codex_compact_acceptance_is_not_completion():
    p,w,e,f=protocol('codex');p.ready=True;p.thread='thread'
    p.command('compact',{'name':'compact'})
    reply(p,w[-1])
    assert not f and p.compaction_receipt=='compact'
    p.receive({'method':'item/completed','params':{'threadId':'thread','item':{'id':'c','type':'contextCompaction'}}})
    assert f==[('compact','completed')] and p.compaction_receipt is None


def test_optional_catalog_errors_keep_usable_configuration(tmp_path):
    script=tmp_path/'native'
    script.write_text('#!'+sys.executable+'\n'+'''import json,sys
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m:continue
 method=m.get('type')
 if method in ('get_commands','get_available_thinking_levels'):
  print(json.dumps({'id':m['id'],'type':'response','success':False,'error':'unsupported'}),flush=True)
 else:
  data={'model':{'id':'m','provider':'p'},'models':[{'id':'m','provider':'p'}]}
  print(json.dumps({'id':m['id'],'type':'response','success':True,'data':data}),flush=True)
''');script.chmod(0o700)
    result=probe('pi',str(script),tmp_path,dict(os.environ))
    assert result['models'] and len(result['warnings'])>=2


def test_codex_next_turn_setting_error_uses_setting_receipt():
    p,w,e,f=protocol('codex');p.ready=True;p.thread='thread'
    p.change_settings('setting',{'model':'custom','effort':'low'})
    p.prompt('prompt',{'text':'fixture'})
    reply(p,w[-1],success=False)
    assert ('setting','error') in f
    assert any(k=='error' and v.get('receipt')=='setting' for k,v in e)


def test_empty_start_settings_use_native_defaults(chat):
    obj,project,sid=chat
    row=obj.action('start',project,{'id':uuid.uuid4().hex,'cli':'codex','mode':'chat','model':'','effort':''})
    assert json.loads(row['chat_settings'])['next']=={}
