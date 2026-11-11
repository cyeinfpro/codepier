"""Real chat functions under deterministic DOM, stream and history adapters."""

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
class Element {
  constructor(tag='div') {
    this.tagName=tag.toUpperCase();this.childNodes=[];this.parentNode=null;
    this.dataset={};this.attributes=new Map();this.textContent='';this.value='';
    this.hidden=false;this.disabled=false;this.open=false;
    const classes=new Set();
    this.classList={add:x=>classes.add(x),remove:x=>classes.delete(x),contains:x=>classes.has(x),
      toggle(x,value){if(value??!classes.has(x))classes.add(x);else classes.delete(x);}};
  }
  get firstChild(){return this.childNodes[0]||null;}
  get nextSibling(){return this.parentNode?.childNodes[this.parentNode.childNodes.indexOf(this)+1]||null;}
  get lastElementChild(){return this.childNodes.at(-1)||null;}
  remove(){if(this.parentNode){const nodes=this.parentNode.childNodes;nodes.splice(nodes.indexOf(this),1);}this.parentNode=null;}
  insertBefore(node,cursor){if(node===cursor)return;node.remove();const index=cursor?this.childNodes.indexOf(cursor):this.childNodes.length;assert.ok(index>=0);this.childNodes.splice(index,0,node);node.parentNode=this;}
  append(...nodes){for(const node of nodes)this.insertBefore(node,null);}
  setAttribute(name,value){this.attributes.set(name,String(value));}
  getAttribute(name){return this.attributes.get(name)??null;}
  addEventListener(){}
  querySelectorAll(){return [];}
  querySelector(selector){
    for(const child of this.childNodes){if(selector==='.'+child.className||selector.toUpperCase()===child.tagName)return child;const nested=child.querySelector(selector);if(nested)return nested;}
    return null;
  }
}
const elements=new Map();
const $=selector=>{if(!elements.has(selector))elements.set(selector,new Element());return elements.get(selector);};
const cancelled=[];
let encoded=0;
const context=vm.createContext({console,TextEncoder:class extends TextEncoder{encode(value){encoded++;return super.encode(value);}},
  AbortController,URL,URLSearchParams,innerWidth:1200,TextDecoder,
  document:{body:new Element(),addEventListener(){},createElement:tag=>new Element(tag)},window:{},
  $,S:{session:{user_id:'fixture'},page:'native',projects:[{id:'p',root:'.'}]},
  cancelAnimationFrame:id=>cancelled.push(id),requestAnimationFrame:()=>1,
  chatTargetSync(){},chatProjectInfo:()=>({}),chatCatalogAbortAll(){},
});
vm.runInContext(fs.readFileSync('web/chat.js','utf8'),context);
vm.runInContext(fs.readFileSync('web/chat-chrome.js','utf8'),context);
const run=code=>vm.runInContext(code,context);
run("ChatUI.project='p';ChatUI.selected={id:'s',status:'running'};chatScheduleLayout=()=>{};");
const chat=run('ChatUI'),view=run('chatView()');
function item(key,kind='assistant',data={}){return {key,kind,text:key,data,node:null,body:null};}
function renderer(){
  context.renderNode=value=>{value.node=new Element('article');value.body=new Element();};
  run('chatCreateItem=renderNode;chatUpdateItem=()=>{};');
}
async function scenario(name){
  if(name==='detach_releases_stream_when_render_throws'){
    let closed=0,disconnected=0;
    const controller=new AbortController();chat.listeners=controller;
    chat.source={close(){closed++;}};chat.frame=17;chat.dirty.add('dirty');
    chat.resizeObserver={disconnect(){disconnected++;}};view.draft='retained';
    run("chatRememberView=()=>{throw Error('render failed');}");
    assert.throws(()=>run('chatDetach()'),/render failed/);
    assert.equal(closed,1);assert.equal(chat.source,null);assert.equal(controller.signal.aborted,true);
    assert.equal(chat.listeners,null);assert.equal(chat.frame,0);assert.equal(chat.dirty.size,0);
    assert.equal(disconnected,1);assert.equal(view.draft,'retained');assert.ok(cancelled.includes(17));
  }else if(name==='switch_releases_stream_when_render_throws'){
    let closed=0;chat.source={close(){closed++;}};
    run("chatRememberView=()=>{throw Error('render failed');}");
    await assert.rejects(run("chatSwitch(null,'p')"),/render failed/);
    assert.equal(closed,1);assert.equal(chat.source,null);
  }else if(name==='connect_without_session_closes_previous_stream'){
    let closed=0;chat.source={close(){closed++;}};chat.selected=null;
    run('chatConnect()');assert.equal(closed,1);assert.equal(chat.source,null);
  }else if(name==='long_history_work_is_bounded_by_visible_window'){
    renderer();
    for(let i=0;i<20000;i++){const entry=item('m'+i);view.items.set(entry.key,entry);view.order.push(entry.key);}
    // Any full-history iteration is a regression. Random key access remains real.
    view.items[Symbol.iterator]=()=>{throw Error('full history iterated');};
    view.items.values=()=>{throw Error('full history values iterated');};
    let lookups=0;const get=view.items.get.bind(view.items);view.items.get=key=>{lookups++;return get(key);};
    run('chatRenderProcessGroups()');assert.equal($('#chat-messages').childNodes.length,100);assert.ok(lookups<=100);
    const removed=get('m19900'),retained=get('m19999').node;
    const next=item('new');view.items.set(next.key,next);view.order.push(next.key);lookups=0;
    run('chatRenderProcessGroups()');assert.equal(removed.node,null);assert.equal(removed.body,null);
    assert.equal(get('m19999').node,retained);assert.equal($('#chat-messages').lastElementChild,next.node);
    assert.equal($('#chat-messages').childNodes.length,100);assert.ok(lookups<=101);
  }else if(name==='group_disclosure_identity_survives_updates'){
    renderer();
    for(const entry of [item('u','user'),item('t1','tool',{receipt:'r',status:'end'}),item('t2','tool',{receipt:'r',status:'end'}),item('a')]){view.items.set(entry.key,entry);view.order.push(entry.key);}
    run('chatRenderProcessGroups()');const group=view.processGroups.get('t1');group.open=true;
    view.items.get('t2').text='updated';run('chatRenderProcessGroups()');
    assert.equal(view.processGroups.get('t1'),group);assert.equal(group.open,true);
    assert.deepEqual(group.lastElementChild.childNodes,[view.items.get('t1').node,view.items.get('t2').node]);
    view.limit=2;run('chatRenderProcessGroups()');assert.equal(view.items.get('t1').node,null);assert.equal(view.items.get('u').node,null);
    assert.equal($('#chat-messages').childNodes.length,2);
  }else if(name==='approval_index_tracks_resolution_without_history_scan'){
    for(let i=0;i<5000;i++)view.items.set('m'+i,item('m'+i));
    const approval=item('approval','approval',{});view.items.set(approval.key,approval);
    run('chatSyncChrome()');assert.equal($('#chat-activity').hidden,false);
    view.items.values=()=>{throw Error('full history values iterated');};
    view.items[Symbol.iterator]=()=>{throw Error('full history iterated');};
    approval.data.resolved=true;run('chatSyncChrome()');assert.equal($('#chat-activity').hidden,true);
    run("chatEvent({type:'approval',receipt:'r',request_id:'new',text:'confirm'})");
    run('chatSyncChrome()');assert.equal($('#chat-activity').hidden,false);
    view.items.get('r:approval:new').answered=true;run('chatSyncChrome()');assert.equal($('#chat-activity').hidden,true);
  }else if(name==='unicode_budget_prevents_overlong_send_without_losing_draft'){
    const over='你'.repeat(21846);assert.ok(over.length<65536);view.draft=over;
    run('chatSyncChrome()');assert.equal($('#chat-send').disabled,true);
    assert.equal($('#chat-compose').getAttribute('aria-invalid'),'true');assert.equal($('#chat-text-limit').hidden,false);
    assert.match($('#chat-text-limit').textContent,/65,538/);assert.equal(view.draft,over);
    const measured=encoded;run('chatSyncChrome()');assert.equal(encoded,measured);
    view.draft='😀'.repeat(16384);run('chatSyncChrome()');
    assert.equal($('#chat-send').disabled,false);assert.equal($('#chat-compose').getAttribute('aria-invalid'),'false');
    view.draft='😀'.repeat(16385);run('chatSyncChrome()');assert.equal($('#chat-send').disabled,true);
    view.draft='short';run('chatSyncChrome()');assert.equal($('#chat-send').disabled,false);assert.equal($('#chat-text-limit').hidden,true);
  }else throw Error(name);
}
scenario(process.argv[1]).then(()=>console.log('SCENARIO_COMPLETED')).catch(error=>{console.error(error);process.exitCode=1;});
'''


@pytest.mark.skipif(NODE is None, reason="Node.js is required for UI behavior tests")
@pytest.mark.parametrize("scenario", [
    "detach_releases_stream_when_render_throws",
    "switch_releases_stream_when_render_throws",
    "connect_without_session_closes_previous_stream",
    "long_history_work_is_bounded_by_visible_window",
    "group_disclosure_identity_survives_updates",
    "approval_index_tracks_resolution_without_history_scan",
    "unicode_budget_prevents_overlong_send_without_losing_draft",
])
def test_chat_report_regressions(scenario):
    result = subprocess.run(
        [NODE, "-e", HARNESS, scenario], cwd=ROOT, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SCENARIO_COMPLETED" in result.stdout
