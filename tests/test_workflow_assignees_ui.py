"""Run the production assignee loader with controlled response ordering."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
HARNESS = r"""
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm');
const dialogs = [], requests = [], submissions = [];
const deferred = () => {let resolve, reject; const promise = new Promise((a,b) => {resolve=a;reject=b;}); return {promise,resolve,reject};};
const tick = () => new Promise(resolve => setImmediate(resolve));
function control(value='') {
  let html='';
  return {value, disabled:false, textContent:'', options:[], get innerHTML(){return html;},
    set innerHTML(value){html=value;this.options=[...value.matchAll(/<option value="([^"]*)"/g)].map(m=>({value:m[1]}));this.value=this.options[0]?.value||'';}};
}
const S = {session:{user_id:'owner'},page:'workflows',space_id:'legacy',
  projects:[{id:'p',mode:'write',alias:'P'},{id:'q',mode:'write',alias:'Q'}],work:{project:'p'}};
const context = vm.createContext({console, S, window:{}, document:{addEventListener(){}},
  esc:String, buttons:()=>'', toast(){}, closeModal(dialog){dialog.isConnected=false;},
  loadBasics:async()=>{}, api(path){const job=deferred();requests.push({path,...job});return job.promise;},
  modal(title,body){for(const old of dialogs)old.isConnected=false;const fields={};
    for(const id of ['wf-assignee','wf-assignee-status','wf-assignee-refresh','wf-project','workflow-create-form','workflow-create-save'])
      fields['#'+id]=control(id==='wf-project'?'p':'');
    const dialog={isConnected:true,fields};dialogs.push(dialog);return dialog;},
  $(selector,dialog){return (dialog||dialogs.at(-1)).fields[selector];},
  FormData:class {constructor(){this.items=[['project',dialogs.at(-1).fields['#wf-project'].value],
    ['assignee_grant_id',dialogs.at(-1).fields['#wf-assignee'].value]];}[Symbol.iterator](){return this.items[Symbol.iterator]();}},
});
vm.runInContext(fs.readFileSync('web/workflows.js','utf8'),context);
context.capture=(dialog,form,button,name,readArgs,done)=>submissions.push({readArgs,done});
vm.runInContext('bindWorkflowSubmit=(...args)=>capture(...args)',context);
const run=(text)=>vm.runInContext(text,context);
const reply=(job,project,ids)=>job.resolve({project_id:project,assignees:ids.map(id=>({id,label:id,authorization_mode:'role'}))});
async function main(name){
  const pending=run('workflowCreate()');await tick();
  const dialog=dialogs.at(-1), fields=dialog.fields, select=fields['#wf-assignee'];
  assert.equal(requests[0].path,'/api/projects/p/workflow-assignees');
  assert.throws(()=>submissions[0].readArgs(),/核实/);
  if(name==='role_candidates_and_panel_only'){
    reply(requests[0],'p',['role-connection']);await pending;
    assert.deepEqual(select.options.map(x=>x.value),['','role-connection']);
    select.value='role-connection';assert.equal(submissions[0].readArgs().assignee_grant_id,'role-connection');
    select.value='';assert.equal('assignee_grant_id' in submissions[0].readArgs(),false);
  }else if(name==='newer_project_wins'){
    fields['#wf-project'].value='q';const newer=fields['#wf-project'].onchange();await tick();
    reply(requests[1],'q',['q-grant']);await newer;reply(requests[0],'p',['old-grant']);await pending;
    assert.deepEqual(select.options.map(x=>x.value),['','q-grant']);assert.equal(select.disabled,false);
  }else if(name==='close_ignores_late_response'){
    dialog.isConnected=false;const before=select.innerHTML;
    reply(requests[0],'p',['old-grant']);await pending;
    assert.equal(select.innerHTML,before);assert.equal(select.disabled,true);
  }else if(name==='session_change_ignores_late_response'){
    S.session={user_id:'different'};reply(requests[0],'p',['old-grant']);await pending;
    assert.equal(select.innerHTML,'');assert.equal(select.disabled,true);
  }else if(name==='failed_read_retries_without_creation'){
    requests[0].reject(Error('offline'));await pending;
    assert.equal(select.disabled,true);assert.match(fields['#wf-assignee-status'].textContent,/offline/);
    assert.throws(()=>submissions[0].readArgs(),/核实/);
    const retry=fields['#wf-assignee-refresh'].onclick();await tick();reply(requests[1],'p',['current']);await retry;
    assert.equal(select.disabled,false);assert.deepEqual(select.options.map(x=>x.value),['','current']);
  }else if(name==='refresh_removes_revoked_selection'){
    reply(requests[0],'p',['revoked']);await pending;select.value='revoked';
    const refresh=fields['#wf-assignee-refresh'].onclick();await tick();reply(requests[1],'p',[]);await refresh;
    assert.equal(select.value,'');assert.equal('assignee_grant_id' in submissions[0].readArgs(),false);
  }else if(name==='mismatched_project_is_not_accepted'){
    reply(requests[0],'q',['wrong']);await pending;
    assert.equal(select.disabled,true);assert.match(fields['#wf-assignee-status'].textContent,/未核实/);
  }else throw Error(name);
}
main(process.argv[1]).then(()=>console.log('SCENARIO_COMPLETED')).catch(error=>{console.error(error);process.exitCode=1;});
"""


@pytest.mark.skipif(NODE is None, reason="Node.js is required for UI behavior tests")
@pytest.mark.parametrize("scenario", [
    "role_candidates_and_panel_only",
    "newer_project_wins",
    "close_ignores_late_response",
    "session_change_ignores_late_response",
    "failed_read_retries_without_creation",
    "refresh_removes_revoked_selection",
    "mismatched_project_is_not_accepted",
])
def test_workflow_assignee_loader(scenario):
    result = subprocess.run([NODE, "-e", HARNESS, scenario], cwd=ROOT, text=True,
                            capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SCENARIO_COMPLETED" in result.stdout
