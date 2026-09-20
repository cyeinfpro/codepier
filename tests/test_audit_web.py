"""Behavioral regression checks for delayed UI replies and uncertain submission.

Uses Node's VM and a small DOM/transport adapter; no external service or browser
installation is needed. The production request, editor and polling functions run
unchanged, while the adapter controls response ordering and input events.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")

HARNESS = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map(), timers = new Map(), storage = new Map();
let timerId = 0, fetchImpl;
function element(value='') { const children=new Map(),attributes=new Map();return {value, innerHTML:'', textContent:'', disabled:false, isConnected:true, readOnly:false, childNodes:[],
  style:{getPropertyValue(){return '';},getPropertyPriority(){return '';},setProperty(){},removeProperty(){}},
  focus(){}, remove(){this.isConnected=false;}, append(...nodes){this.childNodes.push(...nodes);}, replaceChildren(...nodes){children.clear();this.childNodes=nodes;},getBoundingClientRect(){return {width:100};},
  setAttribute(k,v){attributes.set(k,String(v));},getAttribute(k){return attributes.get(k)??null;},
  removeAttribute(k){attributes.delete(k);},addEventListener(){},contains(){return false;},classList:{toggle(){},add(){},remove(){},contains(){return false;}},
  querySelector(s){if(s==='.toast-message'||s==='.toast-dismiss'){if(!children.has(s))children.set(s,element());return children.get(s);}return elements.get(s)||null;},querySelectorAll(){return [];}}; }
const document = {body:element(),activeElement:null,querySelector:s=>elements.get(s)||null,querySelectorAll:()=>[],addEventListener(){},removeEventListener(){},createElement:()=>element()};
for(const s of ['#app','#modal-root','#toasts','#page','#editor-wrap','#file-tree','#code-editor','#dirty-state','#editor-lines','#task-select','#task-output','#task-status'])elements.set(s,element());
const context = vm.createContext({document, console, URLSearchParams, AbortController, Uint8Array, Blob, URL, Event, crypto:require('node:crypto').webcrypto,
  location:{hash:'',protocol:'http:',origin:'http://fixture.invalid',search:''},history:{replaceState(){}},navigator:{},confirm:()=>true,
  // Load the same presentation module as index.html; this VM models a desktop viewport.
  matchMedia:()=>({matches:false,addEventListener(){},removeEventListener(){}}),
  requestAnimationFrame:fn=>queueMicrotask(fn),
  sessionStorage:{getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},
  setTimeout(fn,ms){const id=++timerId;timers.set(id,{fn,ms});if([500,1000,2000,4000].includes(ms))queueMicrotask(()=>{if(timers.delete(id))fn();});return id;},
  clearTimeout:id=>timers.delete(id),clearInterval:id=>timers.delete(id),setInterval:()=>++timerId,
  fetch:(...args)=>fetchImpl(...args),window:{addEventListener(){},isSecureContext:false},
  EventSource:class {constructor(){this.closed=false;}close(){this.closed=true;}}
});
let source=fs.readFileSync('web/app.js','utf8');source=source.slice(0,source.lastIndexOf('(async()=>{try{const page=location.hash'));
vm.runInContext(fs.readFileSync('web/ui.js','utf8'),context);
vm.runInContext(source,context);
const run=s=>vm.runInContext(s,context);
const response=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body});
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b});return {promise,resolve,reject};};
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const work=()=>run('S.work');
function editor(content='base'){
  run("S.session={csrf:'initial'};S.page='workbench';S.projects=[{id:'p',alias:'P',mode:'write'}];Object.assign(S.work,{project:'p',path:'old.txt',sha:'sha-base',content:'base',original:'base',dirty:false});bindEditor();");
  elements.get('#code-editor').value=content;
}
function type(text){const el=elements.get('#code-editor');el.value=text;el.oninput();}
function modalAdapter(){
  context.makeModal=()=>{const dialog=element();elements.set('.modal',dialog);for(const s of ['#confirm-move','#move-destination','#confirm-save'])elements.set(s,element());return dialog;};
  run('modal=()=>makeModal();');
}
async function scenario(name){
  if(name==='editor_restores_html_stripped_first_newline'){
    editor();work().content="\nleading newline";run("bindEditor()");assert.equal(elements.get('#code-editor').value,'\nleading newline');
  }else if(name==='read_preserves_input'){
    editor();const read=deferred();fetchImpl=()=>read.promise;
    const pending=run("readFile('new.txt')");type('typed while reading');
    read.resolve(response({path:'new.txt',content:'remote',sha256:'new-sha',truncated:false}));await pending;
    assert.equal(work().path,'old.txt');assert.equal(work().content,'typed while reading');assert.equal(work().dirty,true);
  }else if(name==='move_preserves_input'){
    editor();modalAdapter();const move=deferred();fetchImpl=async(path,options)=>JSON.parse(options.body).tool==='fs_move'?move.promise:response({entries:[],next_offset:null});
    await run('moveFile()');elements.get('#move-destination').value='moved.txt';const pending=elements.get('#confirm-move').onclick();type('typed while moving');
    move.resolve(response({destination:'moved.txt',sha256:'sha-base'}));await pending;
    assert.equal(work().path,'moved.txt');assert.equal(work().content,'typed while moving');assert.equal(work().original,'base');assert.equal(work().dirty,true);
  }else if(name==='restore_failed_reload_keeps_dirty'){
    editor();type('unsaved draft');let reads=0;
    fetchImpl=async(path,options)=>{const tool=JSON.parse(options.body).tool;if(tool==='fs_read')return ++reads===1?response({sha256:'current'}):Promise.reject(Error('offline'));return response({sha256:'restored'});};
    await assert.rejects(run("restoreBackup('backup','old.txt')"),/网络暂时不可用/);
    assert.equal(work().content,'unsaved draft');assert.equal(work().dirty,true);
  }else if(name==='restore_preserves_input_during_mutation'){
    editor();const restore=deferred();fetchImpl=async(path,options)=>JSON.parse(options.body).tool==='fs_read'?response({sha256:'current'}):restore.promise;
    const pending=run("restoreBackup('backup','old.txt')");await tick();type('new draft during restore');restore.resolve(response({sha256:'restored'}));await pending;
    assert.equal(work().content,'new draft during restore');assert.equal(work().dirty,true);
  }else if(name==='save_does_not_rebase_reopened_file'){
    editor();type('first draft');modalAdapter();const write=deferred();
    fetchImpl=async(path,options)=>{const t=JSON.parse(options.body).tool;return t==='fs_write'?write.promise:t==='fs_read'?response({path:'old.txt',sha256:'external-sha',content:'external',truncated:false}):t==='fs_tree'?response({entries:[],next_offset:null}):response({diff:'-base\n+first draft'});};
    await run('previewSave()');const pending=elements.get('#confirm-save').onclick();await run("readFile('old.txt',true)");type('second draft');
    write.resolve(response({sha256:'first-sha'}));await pending;
    assert.equal(work().sha,'external-sha');assert.equal(work().original,'external');assert.equal(work().content,'second draft');
  }else if(name==='stale_preview_is_not_confirmable'){
    editor();modalAdapter();const preview=deferred();fetchImpl=()=>preview.promise;const pending=run('previewSave()');type('new draft');preview.resolve(response({diff:'old diff'}));await pending;assert.equal(elements.has('#confirm-save'),false);
  }else if(name==='stale_audit_response_keeps_current_details'){
    editor();const old=deferred();let calls=0;run("S.auditMode='events';S.renderSeq=1");
    fetchImpl=async()=>++calls===1?old.promise:response({events:[{id:'new',action:'new',at:1}],next_offset:null});
    const pending=run('auditHTML(1)');run('S.renderSeq=2');await run('auditHTML(2)');old.resolve(response({events:[{id:'old',action:'old',at:1}],next_offset:40}));await pending;
    assert.equal(run('S.auditRows[0].id'),'new');assert.equal(run('S.auditNext'),null);
  }else if(name==='settled_receipt_does_not_cross_sessions'){
    editor();const receipt=deferred();context.receipt=receipt.promise;const pending=run('settled(receipt)');run("S.session={csrf:'new'}");receipt.resolve({pending:true,operation_id:'old'});await assert.rejects(pending,/登录状态已变化/);
  }else if(name==='old_401_does_not_clear_new_session'){
    editor();const request=deferred();fetchImpl=()=>request.promise;const pending=run("api('/api/devices')");run("S.session={csrf:'new'}");request.resolve(response({error:{message:'expired'}},401));
    await assert.rejects(pending,/登录状态已变化/);assert.equal(run('S.session.csrf'),'new');
  }else if(name==='null_json_retries_are_bounded'){
    editor();let count=0;fetchImpl=async()=>{count++;return response(null);};await assert.rejects(run("api('/api/devices')"),/网络暂时不可用/);assert.equal(count,5);
  }else if(name==='uncertain_task_reuses_key_after_reload'){
    editor();elements.get('#task-select').value='once';const keys=[];let fail=true;
    fetchImpl=async(path,options)=>{if(path.startsWith('/api/operations/'))return response({id:'op',state:'succeeded',pending:false,output:'once'});const args=JSON.parse(options.body).arguments;keys.push(args.idempotency_key);if(fail)throw Error('lost reply');return response({operation_id:'op'});};
    await assert.rejects(run('runTask()'),/网络暂时不可用/);assert.ok(storage.has('codepier-task-submission'));
    run('S.taskSubmission=null');elements.get('#task-select').value='different';fail=false;await run('runTask()');
    assert.equal(new Set(keys).size,1);assert.equal(keys.length,6);assert.equal(work().operation,'op');assert.equal(storage.has('codepier-task-submission'),false);
  }else if(name==='missing_task_does_not_block_new_run'){
    editor();run("trackTask('gone')");elements.get('#task-select').value='once';
    fetchImpl=async path=>path.endsWith('/gone')?response({error:{code:'OPERATION_NOT_FOUND',message:'gone'}},404):path.startsWith('/api/operations/')?response({id:'new',state:'succeeded',pending:false}):response({operation_id:'new'});
    await run('pollTask()');assert.equal(work().operation,null);assert.equal(run('S.poll'),null);await run('runTask()');assert.equal(work().operation,'new');
  }else if(name==='new_task_poll_is_not_blocked_by_old_poll'){
    editor();const old=deferred();let newCalls=0;fetchImpl=async path=>{if(path.endsWith('/old'))return old.promise;newCalls++;return response({id:'new',state:'queued',pending:true});};
    run("trackTask('old')");const pending=run('pollTask()');run("trackTask('new')");await run('pollTask()');old.resolve(response({id:'old',state:'succeeded',pending:false}));await pending;
    assert.equal(newCalls,1);assert.ok(run('S.poll'));assert.equal(work().operation,'new');
  }else if(name==='old_poll_does_not_restart_after_logout'){
    editor();const old=deferred();fetchImpl=()=>old.promise;run("trackTask('op')");const pending=run('pollTask()');run('S.session=null');old.resolve(response({id:'op',state:'queued',pending:true}));await pending;assert.equal(run('S.poll'),null);
  }else if(name==='event_connections_release_timer'){
    editor();fetchImpl=async()=>response({});run('connectEvents()');const first=run('S.events');first.onmessage({data:JSON.stringify({type:'device'})});const id=run('S.eventTimer');assert.ok(timers.has(id));run('connectEvents()');assert.equal(first.closed,true);assert.equal(timers.has(id),false);
  }else if(name==='login_boot_failure_has_retry_surface'){
    editor();fetchImpl=async()=>{throw Error('offline');};await run('bootAuthenticated()');assert.match(elements.get('#page').innerHTML,/重试/);assert.ok(run('S.events'));
  }else throw Error(name);
}
scenario(process.argv[1]).then(()=>console.log('SCENARIO_COMPLETED')).catch(error=>{console.error(error);process.exitCode=1;});
'''


@pytest.mark.skipif(NODE is None, reason="Node.js is required for UI behavior tests")
@pytest.mark.parametrize("scenario", [
    "editor_restores_html_stripped_first_newline",
    "read_preserves_input",
    "move_preserves_input",
    "restore_failed_reload_keeps_dirty",
    "restore_preserves_input_during_mutation",
    "save_does_not_rebase_reopened_file",
    "stale_preview_is_not_confirmable",
    "stale_audit_response_keeps_current_details",
    "settled_receipt_does_not_cross_sessions",
    "old_401_does_not_clear_new_session",
    "null_json_retries_are_bounded",
    "uncertain_task_reuses_key_after_reload",
    "missing_task_does_not_block_new_run",
    "new_task_poll_is_not_blocked_by_old_poll",
    "old_poll_does_not_restart_after_logout",
    "event_connections_release_timer",
    "login_boot_failure_has_retry_surface",
])
def test_web_async_recovery(scenario):
    result = subprocess.run([NODE, "-e", HARNESS, scenario], cwd=ROOT, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SCENARIO_COMPLETED" in result.stdout, "Scenario exited with an unresolved async operation: " + result.stdout + result.stderr
