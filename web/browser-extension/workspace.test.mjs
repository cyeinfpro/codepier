import test from 'node:test';
import assert from 'node:assert/strict';
import {BrowserWorkspace,siteOf} from './workspace.js';
const id=n=>String(n).padStart(32,'0');
function fixture(){
 const data={},tabs=new Map(),events=[];let count=0,focused=true,now=100000;
 const permissions=new Set();
 const api={runtime:{id:'a'.repeat(32),getURL:p=>'chrome-extension://'+'a'.repeat(32)+'/'+p},
 storage:{local:{get:async k=>({[k]:structuredClone(data[k])}),set:async v=>Object.assign(data,structuredClone(v))}},
 permissions:{contains:async({origins})=>origins.every(x=>permissions.has(x)),remove:async({origins})=>{for(const x of origins)permissions.delete(x);}},
 windows:{getLastFocused:async()=>({id:1,focused,type:'normal'}),get:async()=>({id:1,focused,type:'normal'})},
 tabs:{get:async n=>{if(!tabs.has(n))throw Error('missing');return {...tabs.get(n)};},
 create:async v=>{const row={id:++count,windowId:v.windowId,active:v.active,url:v.url,status:'complete',groupId:-1};tabs.set(row.id,row);events.push(['create',v]);return row;},
 group:async({tabIds})=>{for(const n of tabIds)tabs.get(n).groupId=3;return 3;},
 update:async(n,v)=>{assert.ok(tabs.has(n));Object.assign(tabs.get(n),v);events.push(['update',n,v]);return {...tabs.get(n)};}},
 tabGroups:{update:async(...v)=>events.push(['group',...v])},
 scripting:{executeScript:async request=>{events.push(['script',request]);return [{result:{ok:true,data:{document_id:id(20),observation_token:id(21),url:'https://allowed.test/',title:'Fixture',text:'text',elements:[],input_dispatched:true}}}];}}};
 const workspace=new BrowserWorkspace(api,()=>now);
 const ready=async()=>{await workspace.load();permissions.add('https://allowed.test/*');await workspace.allow('https://allowed.test');await workspace.prepare(2);};
 const command=(action,extra={})=>({request_id:id(++count+100),lease_id:id(1),origins:['https://allowed.test'],action,...extra});
 return {api,workspace,ready,command,events,tabs,permissions,data,setFocused:x=>focused=x,tick:()=>now+=40000};
}

test('only local focused provisioning creates a bounded pool',async()=>{
 const f=fixture();await f.ready();assert.equal((await f.workspace.localStatus()).pool_size,2);
 f.setFocused(false);await assert.rejects(()=>f.workspace.prepare(3),e=>e.code==='BROWSER_POOL_EMPTY');
 assert.equal(f.events.filter(x=>x[0]==='create').length,2);
 await assert.rejects(()=>f.workspace.prepare(17));
});
test('remote open reuses tabs and never creates or activates one',async()=>{
 const f=fixture();await f.ready();f.events.length=0;
 const r=await f.workspace.execute(f.command('open',{url:'https://allowed.test/page',expires:130}));
 assert.equal(r.opened,true);assert.equal(r.focus_changed,false);
 assert.ok(f.events.every(x=>x[0]!=='create'&&!(x[2]&&'active' in x[2])));
 assert.equal((await f.workspace.localStatus()).leased,1);
});
test('no idle pool is an explicit failure, not a foreground browser launch',async()=>{
 const f=fixture();await f.workspace.load();f.permissions.add('https://allowed.test/*');await f.workspace.allow('https://allowed.test');
 await assert.rejects(()=>f.workspace.execute(f.command('open',{url:'https://allowed.test/',expires:130})),e=>e.code==='BROWSER_POOL_EMPTY');
 assert.equal(f.events.length,0);
});
test('both local and requested exact origins plus browser permission are required',async()=>{
 const f=fixture();await f.ready();
 for(const extra of [{url:'https://other.test/',origins:['https://other.test']},{url:'https://allowed.test/',origins:['https://other.test']}]){
  await assert.rejects(()=>f.workspace.execute(f.command('open',{expires:130,...extra})),e=>e.code==='BROWSER_ORIGIN_DENIED');
 }
 f.permissions.clear();await assert.rejects(()=>f.workspace.execute(f.command('open',{url:'https://allowed.test/',expires:130})),e=>e.code==='BROWSER_PERMISSION_REQUIRED');
});
test('duplicate requests are never repeated after storage reload',async()=>{
 const f=fixture();await f.ready();const cmd=f.command('open',{url:'https://allowed.test/',expires:130});
 await f.workspace.execute(cmd);const restored=new BrowserWorkspace(f.api,()=>100000);
 await assert.rejects(()=>restored.execute(cmd),e=>e.code==='BROWSER_ACTION_UNCERTAIN');
});
test('human-selected tab loses remote input authority and is never reset on cleanup',async()=>{
 const f=fixture();await f.ready();await f.workspace.execute(f.command('open',{url:'https://allowed.test/',expires:130}));
 const row=(await f.workspace.load()).pool.find(x=>x.lease_id);f.tabs.get(row.tab_id).active=true;f.events.length=0;
 await assert.rejects(()=>f.workspace.execute(f.command('snapshot')),e=>e.code==='BROWSER_TAB_CHANGED');
 const r=await f.workspace.execute(f.command('close'));assert.equal(r.released,true);assert.equal(r.tab_cleanup_confirmed,false);
 assert.ok(f.events.every(x=>x[0]!=='update'));
});
test('cross-origin navigation is observed as denied without executing page code',async()=>{
 const f=fixture();await f.ready();await f.workspace.execute(f.command('open',{url:'https://allowed.test/',expires:130}));
 const row=(await f.workspace.load()).pool.find(x=>x.lease_id);f.tabs.get(row.tab_id).url='https://unapproved.test/';f.events.length=0;
 await assert.rejects(()=>f.workspace.execute(f.command('snapshot')),e=>e.code==='BROWSER_ORIGIN_DENIED');
 assert.ok(!f.events.some(x=>x[0]==='script'));
 assert.equal((await f.workspace.execute(f.command('close'))).tab_cleanup_confirmed,true);
});
test('expired leases are reclaimed without touching other tabs',async()=>{
 const f=fixture();await f.ready();await f.workspace.execute(f.command('open',{url:'https://allowed.test/',expires:130}));
 f.tick();await f.workspace.expire();assert.equal((await f.workspace.localStatus()).leased,0);
 assert.ok([...f.tabs.values()].every(t=>t.url.endsWith('/idle.html')));
});
test('isolated top-frame scripting contains only the closed page operation',async()=>{
 const f=fixture();await f.ready();await f.workspace.execute(f.command('open',{url:'https://allowed.test/',expires:130}));
 await f.workspace.execute(f.command('snapshot'));
 const request=f.events.find(x=>x[0]==='script')[1];assert.equal(request.world,'ISOLATED');assert.deepEqual(request.target.frameIds,[0]);
 assert.equal(request.func.name,'pageCommand');assert.equal(request.args[0].action,'snapshot');
});
for(const value of ['file:///private','https://u:p@allowed.test','https://allowed.test/\nprivate','https://allowed.test\\evil']){
 test('invalid URL rejected '+JSON.stringify(value),()=>assert.throws(()=>siteOf(value)));
}
