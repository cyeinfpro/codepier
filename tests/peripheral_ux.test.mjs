import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

class Node {
  constructor(tag) { this.tag=tag; this.children=[]; this.disabled=false; this.isConnected=true; this.events={}; }
  setAttribute() {}
  append(child) { child.parentElement=this; this.children.push(child); }
  remove() { this.parentElement.children=this.parentElement.children.filter(x=>x!==this); }
  addEventListener(name, fn) { this.events[name]=fn; }
}
test('MCP app action errors replace only their own feedback and guard in-flight action',async()=>{
  globalThis.document={createElement:tag=>new Node(tag)};
  const {button}=await import('../web/mcp-apps/ui.js');
  const parent=new Node('section');
  let calls=0,finish;
  const action=button('act',async()=>{calls++;await new Promise(resolve=>finish=resolve);throw Error('Failed');});
  parent.append(action);
  const other=new Node('p');parent.append(other);
  let active=action.events.click();await action.events.click();assert.equal(calls,1);finish();await active;
  assert.equal(parent.children.length,3);
  active=action.events.click();assert.equal(parent.children.length,2);finish();await active;
  assert.equal(parent.children.length,3);assert.ok(parent.children.includes(other));
  delete globalThis.document;
});
test('connection result leads with next steps and collapses technical evidence',()=>{
  const context={window:{},CP:{createPanel:()=>({})},esc:String,timeText:String};
  vm.createContext(context);vm.runInContext(fs.readFileSync('web/connection-setup.js','utf8'),context);
  const html=context.window.CodePierConnectionSetup.renderStatus({space:{label:'space'},owner:{label:'owner'},server_info:{version:'1'},catalog:{note:'catalog'},layers:[{id:'agent',label:'Agent',status:'unknown'}],next_actions:['Test once']});
  assert.ok(html.indexOf('Test once')<html.indexOf('技术诊断与目录证据'));
  assert.ok(html.includes('尚未完整确认'));assert.ok(html.includes('<details><summary>技术诊断与目录证据</summary>'));
});
test('skills preserves query, source, pagination and catalog when returning from a resource',async()=>{
  const dialogs=[],requests=[];
  let current;
  const context={S:{session:'user',space_id:'space',work:{project:'p'}},workTarget:()=>({workspace_id:'w'}),toast:()=>{},esc:String,json:JSON.stringify,empty:String,notice:String,
    document:{addEventListener(){}},settled:async value=>value,
    modal(title){ if(current)current.isConnected=false;current={title,isConnected:true,nodes:{}};dialogs.push(current);return current;},
    $(selector,dialog){return dialog.nodes[selector]??=(selector==='#local-skills-form'?{elements:{query:{value:''},source:{value:''}}}:{});},
    $$(selector,area){if(selector==='[data-skill-open]')return area.opens=[{dataset:{skillOpen:'skill'}}];if(selector==='[data-skill-resource]')return area.resources=[{dataset:{skillResource:'ref.md'}}];return [];},
    tool:async(name,args)=>{requests.push({name,args});return name==='skills_list'?{catalog_sha256:'catalog',total:40,codex_enabled_for_project:true,skills:[],warnings:[],has_more:true,next_offset:args.offset+20}:{name:'skill',source:'project',resource_path:args.resource_path,start_line:1,end_line:1,content:'safe',skill_dir:'dir',resources:[],dependencies:[],next_offset:null};}
  };
  vm.createContext(context);vm.runInContext(fs.readFileSync('web/skills.js','utf8'),context);
  await context.localSkills('p','w',{query:'find',source:'project',catalog:'catalog',history:[0],offset:20});
  const listing=current;assert.equal(listing.nodes['#local-skills-form'].elements.query.value,'find');
  await listing.nodes['#local-skills-results'].opens[0].onclick();
  await current.resources[0].onclick();
  await current.nodes['#back-to-skills'].onclick();
  const final=requests.at(-1);assert.equal(final.name,'skills_list');assert.equal(final.args.offset,20);assert.equal(final.args.query,'find');assert.equal(final.args.source,'project');assert.equal(final.args.catalog_sha256,'catalog');
});
test('extension copy and operation feedback do not overwrite connection evidence',async()=>{
  const nodes=new Map();
  const get=id=>{if(!nodes.has(id))nodes.set(id,{textContent:'',disabled:false,hidden:false,replaceChildren(){},append(){},value:'4'});return nodes.get(id);};
  const navigatorDescriptor=Object.getOwnPropertyDescriptor(globalThis,'navigator');
  const makeInput=(id,type)=>{
    const attrs=new Map();
    return Object.assign(get(id),{id,type,attrs,hasAttribute:key=>attrs.has(key),setAttribute:(key,value)=>attrs.set(key,value)});
  };
  const origin=makeInput('origin','url'),size=makeInput('size','number');
  globalThis.document={getElementById:get,querySelectorAll:selector=>{
    assert.equal(selector,'input');return [origin,size];
  }};
  globalThis.chrome={runtime:{sendMessage:async()=>({ok:true,data:{extension_id:'extension',profile_id:'profile',pool_size:4,available:4,leased:0,connection:'桥接已核实',connection_observed_at:123,origins:[]}})}};
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{clipboard:{writeText:async()=>{}}}});
  try {
    await import('../web/browser-extension/popup.js');
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(origin.attrs.get('inputmode'),'url');
    assert.equal(origin.attrs.get('autocorrect'),'off');
    assert.equal(origin.attrs.get('spellcheck'),'false');
    assert.equal(size.attrs.get('inputmode'),'numeric');
    await get('copy').onclick();
    assert.equal(get('connection').textContent,'桥接已核实');
    assert.match(get('feedback').textContent,/已复制/);
    await get('prepare').onclick({currentTarget:get('prepare')});
    assert.equal(get('connection').textContent,'桥接已核实');
    assert.match(get('connection-observed').textContent,/最近观察/);
    assert.match(get('feedback').textContent,/操作已完成/);
  } finally {
    delete globalThis.document;delete globalThis.chrome;
    if(navigatorDescriptor)Object.defineProperty(globalThis,'navigator',navigatorDescriptor);else delete globalThis.navigator;
  }
});

test('skills stale read failure does not reach a new space and stale detail callbacks do not act',async()=>{
  let rejectRead,requests=0,dialog;
  const context={S:{session:'user',space_id:'before'},esc:String,json:JSON.stringify,notice:String,document:{addEventListener(){}},settled:async x=>x,
    modal(){dialog={isConnected:true,nodes:{}};return dialog;},
    $(key,node){return node.nodes[key]??={};},$$:()=>[],
    tool:()=>{requests++;return new Promise((resolve,reject)=>rejectRead=reject);}};
  vm.createContext(context);vm.runInContext(fs.readFileSync('web/skills.js','utf8'),context);
  const pending=context.localSkillRead('project','skill');
  context.S.space_id='after';rejectRead(Error('old private error'));
  await assert.doesNotReject(pending);assert.deepEqual(dialog.nodes,{});
  context.tool=async()=>({name:'skill',source:'project',resource_path:'SKILL.md',start_line:1,end_line:1,content:'safe',skill_dir:'dir',resources:[],dependencies:[],next_offset:10});
  await context.localSkillRead('project','skill');
  context.S.space_id='third';
  context.tool=()=>{throw Error('stale callback must not request');};
  assert.equal(dialog.nodes['#back-to-skills'].onclick(),false);
  assert.equal(dialog.nodes['#skill-next-page'].onclick(),false);
});
test('MCP Apps action failure retains a validated original operation ID without replay',async()=>{
  globalThis.document={createElement:tag=>new Node(tag)};
  const {button}=await import('../web/mcp-apps/ui.js');
  let calls=0;
  const parent=new Node('section');
  const action=button('read',async()=>{calls++;const error=Error('结果待确认');error.operationId='a'.repeat(32);throw error;});
  parent.append(action);await action.events.click();
  assert.equal(calls,1);assert.match(parent.children[1].textContent,/原操作：a{32}/);
  assert.match(parent.children[1].textContent,/不重新执行/);
  delete globalThis.document;
});
