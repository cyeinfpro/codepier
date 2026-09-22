"""F19/F20/F21/F27: truthful recovery, cleanup and readable exports."""
import asyncio
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect
from tests.test_chat_sse import service, TestAuth, request, sync  # noqa: F401
from tests.test_devtools_browser_actions import browser_page  # noqa: F401
from tests.test_devtools_flow import tools_page  # noqa: F401
from tests.test_integrations_stack import integrated_stack  # noqa: F401
from tests.test_user_flow_audit_fixes_20260922 import records_fixture

ROOT=Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
@pytest.mark.parametrize('fmt',['md','json'])
async def test_export_reconciles_deltas_without_duplicating_the_final_answer(service,fmt):
    from hub.native_cli import make_native_router
    obj,_project,sid=service
    frames=[{'type':'delta','receipt':'one','item_id':'answer','text':'Hello '},
            {'type':'delta','receipt':'one','item_id':'answer','text':'world'},
            {'type':'message','receipt':'one','item_id':'answer','text':'Hello world'},
            {'type':'done','receipt':'one','status':'completed'},
            {'type':'delta','receipt':'two','item_id':'answer','text':'Next '},
            {'type':'message','receipt':'two','item_id':'answer','text':'Next answer'},
            {'type':'done','receipt':'two','status':'completed'}]
    await sync(obj,sid,''.join(json.dumps(frame)+'\n' for frame in frames).encode())
    endpoint=next(route.endpoint for route in make_native_router(TestAuth(),obj.runtime).routes if route.path.endswith('/export'))
    response=await endpoint(sid,request(),fmt)
    data=''.join([chunk async for chunk in response.body_iterator])
    if fmt=='md':
        assert data.count('Hello world')==1 and data.count('Next answer')==1
        assert data.count('Hello ')==1 and data.count('Next ')==1
        assert data.index('Hello world')<data.index('Next answer')
    else:
        decoded=json.loads(data)
        assert len(decoded)==len(frames)
        assert [event.get('text') for event in decoded]==[event.get('text') for event in frames]


def test_project_stop_skips_legacy_closed_receipts_but_rechecks_explicit_cleanup_failure():
    from agent.background_browser import BrowserBroker
    records,db=records_fixture()
    project={'id':'fixture','root':'/fixture','_coding_device':'device','_integration_owner':'owner'}
    broker=BrowserBroker(SimpleNamespace(config={}),records);calls=[]
    async def close(action,body,**kwargs):
        assert action=='close';calls.append(body['lease_id']);return {'tab_cleanup_confirmed':True}
    broker.rpc=close
    try:
        for index,extra in enumerate([{}, {'tab_cleanup_confirmed':True}, {'tab_cleanup_confirmed':False}],1):
            records.save('browser',f'{index:032x}',project,{'lease_id':f'{index:032x}','state':'closed',**extra})
        asyncio.run(broker.release_project(project))
        assert calls==[f'{3:032x}']
        assert asyncio.run(broker.release_project(project))==[]
    finally:db.close()


def test_terminal_vps_error_is_queried_then_allows_an_explicit_new_check():
    code=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const elements=Object.fromEntries(['#vps-check-run','#vps-check-state','#vps-check-output','#vps-check-project'].map(k=>[k,{value:'ProjectA',textContent:'',disabled:false}]));
const calls=[];let serial=0;
const context={S:{session:'fixture-session',page:'vps'},api:async()=>({id:'v',name:'fixture',host:'example.invalid',port:22,username:'root',enabled:true,projects:[{id:'ProjectA',alias:'A',device_name:'D',online:true,mode:'write',allow_tasks:true}]}),
modal:()=>({isConnected:true}),$:s=>elements[s],esc:String,icon:()=>'',notice:String,stateNames:{},uid:()=>String(++serial),busy:async(_,fn)=>fn(),document:{addEventListener(){}},
tool:async(name,args)=>{calls.push({name,args});if(name==='operations_wait')return {state:'failed',pending:false,result:{error:{message:'fixture dependency missing'}}};throw Object.assign(new Error('fixture dependency missing'),{code:'SSH_DEPENDENCY_MISSING',operation_id:'fixture-operation',retryable:false});}};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async()=>{
 await context.vpsCheck('v');await elements['#vps-check-run'].onclick();
 assert.equal(elements['#vps-check-run'].textContent,'恢复本次检查');
 await elements['#vps-check-run'].onclick();
 assert.equal(elements['#vps-check-run'].textContent,'重新检查');
 assert.equal(elements['#vps-check-project'].disabled,false);
 await elements['#vps-check-run'].onclick();
 assert.deepEqual(calls.map(c=>c.name),['vps_exec','operations_wait','vps_exec']);
 assert.equal(calls[1].args.operation_id,'fixture-operation');
 assert.notEqual(calls[0].args.idempotency_key,calls[2].args.idempotency_key);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    result=subprocess.run(['node','-e',code,str(ROOT/'web/vps.js')],capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stdout+result.stderr


def test_definite_browser_rejection_is_not_persisted_as_an_unknown_request(browser_page):
    page=browser_page
    page.evaluate('''() => {window.acceptBrowserOpen=toolHandlers.browser_open;
      toolHandlers.browser_open=async()=>{throw Object.assign(new Error('fixture device offline'),
        {code:'COMPUTER_OFFLINE',status:409,admitted:false});};}''')
    page.fill('[name=url]','https://example.test/first')
    page.click('[data-i-form=browser-open] button[type=submit]')
    expect(page.locator('[data-i-form=browser-open] .integration-form-error')).to_contain_text('fixture device offline')
    assert not page.evaluate('[...S.integrations.submissions.values()].some(row=>!row.done)')
    assert page.locator('#i-receipts button').count()==0
    page.evaluate('''() => {toolHandlers.browser_open=acceptBrowserOpen;}''')
    page.evaluate('renderPage(false)')
    page.fill('[name=url]','https://example.test/second')
    page.click('[data-i-form=browser-open] button[type=submit]')
    expect(page.locator('[data-i-form=browser-action]')).to_be_visible()
    calls=page.evaluate("toolCalls.filter(row=>row.tool==='browser_open').map(row=>row.arguments)")
    assert len(calls)==2 and calls[0]['idempotency_key']!=calls[1]['idempotency_key']
