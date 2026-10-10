"""Exercise the actual lifecycle modal and HTTP adapter without device actions."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
HARNESS = r"""
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const dialogs=[],requests=[],queries=[],tracked=[],toasts=[],saved=new Map();
const S={session:{user_id:'owner'},space_id:'legacy',page:'devices'};
const context=vm.createContext({console,S,window:{},AbortController,DOMException,setTimeout,clearTimeout,
  esc:String,notice:String,uid:(()=>{let i=0;return()=>String(++i);})(),
  sessionValue(key,...value){if(value.length){if(value[0]===null)saved.delete(key);else saved.set(key,value[0]);}return saved.get(key);},
  modal(title,body){for(const d of dialogs)d.isConnected=false;const fields={};
    for(const id of ['agent-lifecycle-form','agent-lifecycle-submit','agent-lifecycle-status','agent-uninstall-confirm'])
      fields['#'+id]={value:'Node',disabled:false,innerHTML:'Submit',textContent:'',setCustomValidity(){},reportValidity(){return true;}};
    const d={isConnected:true,fields};dialogs.push(d);return d;},
  $(selector,dialog){return dialog?.fields[selector]||null;},
  closeModal(dialog){dialog.isConnected=false;},toast(text){toasts.push(text);},
  invalidateBasics(){},loadBasics:async()=>{},renderPage:async()=>{},networkState(){},
  sessionChanged:()=>Object.assign(Error('session changed'),{code:'SESSION_CHANGED'}),
  endSession(){S.session=null;},pause:async()=>{},
  fetch(path,options){const task=deferred();requests.push({path,options,...task});return task.promise;},
  tool(name,args){const task=deferred();queries.push({name,args,...task});return task.promise;},
  recordTrack(...args){tracked.push(args);},
});
const app=fs.readFileSync('web/app.js','utf8');
vm.runInContext(app.slice(app.indexOf('async function api('),app.indexOf('\nconst post =')),context);
vm.runInContext(fs.readFileSync('web/agent-install.js','utf8'),context);
const run=source=>vm.runInContext(source,context);
run('savedTracker=trackAgentLifecycle;trackAgentLifecycle=(...args)=>recordTrack(...args)');
const open=()=>{run("agentLifecycleModal({id:'device',name:'Node',agent:{}},'agent_restart')");return dialogs.at(-1);};
const submit=d=>d.fields['#agent-lifecycle-form'].onsubmit({preventDefault(){}});
const key=()=>saved.get('codepier-agent-lifecycle:device:agent_restart');
const respond=(job,status,body)=>job.resolve({status,ok:status>=200&&status<300,json:async()=>body});
const deny=(job,status,extra={})=>respond(job,status,{error:{message:'Rejected',...extra}});
const receipt=(job)=>respond(job,200,{operation_id:'operation'});
async function main(name){
  const d=open();
  if(name==='monitor_session_fence'){
    const pending=run("savedTracker('operation',{id:'device'},'agent_restart')");
    S.session={user_id:'new'};
    queries[0].resolve({pending:false,state:'succeeded',result:{ok:true}});
    await pending;assert.equal(toasts.length,0);assert.equal(queries.length,1);return;
  }
  const first=submit(d);await tick();
  assert.equal(requests.length,1);const original=key();assert.ok(original);
  assert.equal(JSON.parse(requests[0].options.body).idempotency_key,original);
  if(name.startsWith('uncertain_')){
    const failure=name.slice('uncertain_'.length);
    if(failure==='network')requests[0].reject(Error('connection reset'));else deny(requests[0],Number(failure));
    await first;assert.equal(key(),original);assert.equal(d.fields['#agent-lifecycle-submit'].disabled,false);
    const again=submit(d);await tick();assert.equal(queries[0].name,'operations_list');
    assert.equal(queries[0].args.idempotency_key,original);queries[0].resolve({operations:[]});await tick();
    assert.equal(JSON.parse(requests[1].options.body).idempotency_key,original);
    receipt(requests[1]);await again;assert.equal(key(),undefined);assert.equal(tracked.length,1);
  }else if(name==='double_click'){
    const second=submit(d);await second;assert.equal(requests.length,1);
    receipt(requests[0]);await first;assert.equal(tracked.length,1);
  }else if(name==='error_receipt'){
    deny(requests[0],409,{operation_id:'original-operation'});await first;
    assert.equal(tracked[0][0],'original-operation');assert.equal(key(),undefined);
  }else if(name==='late_receipt_after_reopen'){
    const newer=open();receipt(requests[0]);await first;
    assert.equal(newer.isConnected,true);assert.equal(tracked.length,0);assert.equal(toasts.length,0);
    assert.equal(key(),original);const resumed=submit(newer);await tick();
    queries[0].resolve({operations:[{id:'operation',device_id:'device',tool:'agent_restart'}]});
    await resumed;assert.equal(requests.length,1);assert.equal(tracked[0][0],'operation');
  }else if(name==='late_receipt_after_login'){
    S.session={user_id:'new'};receipt(requests[0]);await first;
    assert.equal(tracked.length,0);assert.equal(toasts.length,0);assert.equal(key(),original);
  }else if(name==='definitive_unadmitted'){
    deny(requests[0],409,{admitted:false});await first;assert.equal(key(),undefined);
  }else if(name==='malformed_receipt'){
    respond(requests[0],200,{});await first;assert.equal(key(),original);assert.equal(tracked.length,0);
  }else{
    requests[0].reject(Error('connection reset'));await first;
    const retry=submit(d);await tick();assert.equal(queries.length,1);
    if(name==='recover_old_receipt'){
      queries[0].resolve({operations:[{id:'earlier',device_id:'device',tool:'agent_restart'}]});
      await retry;assert.equal(requests.length,1);assert.equal(tracked[0][0],'earlier');assert.equal(key(),undefined);
    }else if(name==='lookup_failure'){
      queries[0].reject(Error('lookup unavailable'));await retry;
      assert.equal(requests.length,1);assert.equal(key(),original);
    }else if(name==='close_during_lookup'){
      d.isConnected=false;queries[0].resolve({operations:[]});await retry;
      assert.equal(requests.length,1);assert.equal(key(),original);
    }else if(name==='prior_uncertainty_then_rejection'){
      queries[0].resolve({operations:[]});await tick();deny(requests[1],409,{admitted:false});await retry;
      assert.equal(key(),original);
    }else throw Error(name);
  }
}
main(process.argv[1]).then(()=>console.log('SCENARIO_COMPLETED')).catch(error=>{console.error(error);process.exitCode=1;});
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is required for lifecycle UI tests")
@pytest.mark.parametrize("scenario", [
    "uncertain_408", "uncertain_429", "uncertain_500", "uncertain_502", "uncertain_503", "uncertain_504",
    "uncertain_network", "double_click", "error_receipt", "late_receipt_after_reopen",
    "late_receipt_after_login", "definitive_unadmitted", "malformed_receipt",
    "recover_old_receipt", "lookup_failure", "close_during_lookup",
    "prior_uncertainty_then_rejection", "monitor_session_fence",
])
def test_lifecycle_idempotency_recovery(scenario):
    result = subprocess.run([NODE, "-e", HARNESS, scenario], cwd=ROOT, text=True,
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SCENARIO_COMPLETED" in result.stdout
