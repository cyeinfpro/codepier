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

// Test both the exported code path and the legacy fallback with the same inputs.
function uiFallbackContext(overrides = {}) {
  return vm.createContext({
    CP: {},
    document: { addEventListener() {} },
    window: {},
    ...overrides,
  });
}

function loadUiFallback(overrides) {
  const ctx = uiFallbackContext(overrides);
  const source = fs.readFileSync('web/ui.js', 'utf8');
  const end = source.indexOf('async function uiConfirmAction(');
  assert.ok(end > 0, 'legacy UI helper boundary exists');
  vm.runInContext(source.slice(0, end), ctx);
  return ctx;
}

function scrollFadeHarness() {
  const jobs = new Map();
  const scrollEvents = new Map();
  const windowEvents = new Map();
  const observers = [];
  let id = 0;
  const el = {
    dataset: {},
    classList: { add() {} },
    isConnected: true,
    clientHeight: 100,
    scrollHeight: 100,
    scrollTop: 0,
    addEventListener(name, fn, options) { scrollEvents.set(name, { fn, options }); },
    removeEventListener(name, fn, options) {
      assert.equal(scrollEvents.get(name)?.fn, fn);
      assert.equal(scrollEvents.get(name)?.options === true, options === true);
      scrollEvents.delete(name);
    },
  };
  const globals = {
    window: {
      addEventListener(name, fn) { windowEvents.set(name, fn); },
      removeEventListener(name, fn) {
        assert.equal(windowEvents.get(name), fn);
        windowEvents.delete(name);
      },
    },
    // Exercise the window-resize fallback rather than masking the missing
    // mutation signal with a fake ResizeObserver notification.
    ResizeObserver: undefined,
    MutationObserver: class {
      constructor(cb) { this.cb = cb; this.disconnected = false; observers.push(this); }
      observe(target, options) { assert.equal(target, el); this.options = options; }
      disconnect() { this.disconnected = true; }
    },
    requestAnimationFrame(fn) { jobs.set(++id, fn); return id; },
    cancelAnimationFrame(frame) { jobs.delete(frame); },
  };
  const flush = () => {
    const pending = Array.from(jobs.values());
    jobs.clear();
    pending.forEach(job => job());
  };
  return { el, globals, flush, jobs, scrollEvents, windowEvents, observers };
}

function withUiGlobals(globals, body) {
  const saved = new Map(Object.keys(globals).map(key =>
    [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  try {
    for (const [key, value] of Object.entries(globals)) {
      Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
    }
    return body();
  } finally {
    for (const [key, descriptor] of saved) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
}

for (const mode of ['module', 'legacy fallback']) {
  test(`scroll edge fade (${mode}) refreshes same-size growth and releases subscriptions`, async () => {
    const h = scrollFadeHarness();
    const bind = mode === 'module'
      ? (await import('../web/core/ui.mjs')).bindScrollEdgeFade
      : loadUiFallback(h.globals).uiBindScrollEdgeFade;
    withUiGlobals(h.globals, () => {
      const dispose = bind(h.el);
      h.flush();
      assert.deepEqual(h.el.dataset, { fadeTop: 'false', fadeBottom: 'false' });
      h.el.scrollHeight = 150;
      assert.equal(h.observers.length, 1, 'observe content even with a fixed clientHeight');
      assert.equal(h.observers[0].options.subtree, true);
      assert.equal(h.observers[0].options.characterData, true);
      h.observers[0].cb();
      h.observers[0].cb();
      assert.equal(h.jobs.size, 1);
      h.flush();
      assert.deepEqual(h.el.dataset, { fadeTop: 'false', fadeBottom: 'true' });
      h.el.scrollTop = 50;
      h.scrollEvents.get('scroll').fn();
      h.flush();
      assert.deepEqual(h.el.dataset, { fadeTop: 'true', fadeBottom: 'false' });
      h.el.scrollHeight = 180;
      h.scrollEvents.get('load').fn();
      h.flush();
      assert.deepEqual(h.el.dataset, { fadeTop: 'true', fadeBottom: 'true' });
      h.el.scrollTop = 0;
      h.el.scrollHeight = 100;
      h.observers[0].cb();
      h.flush();
      assert.deepEqual(h.el.dataset, { fadeTop: 'false', fadeBottom: 'false' });
      h.observers[0].cb();
      dispose();
      dispose();
      h.observers[0].cb();
      assert.equal(h.jobs.size, 0);
      assert.equal(h.observers[0].disconnected, true);
      assert.equal(h.scrollEvents.size, 0);
      assert.equal(h.windowEvents.size, 0);
      assert.deepEqual(h.el.dataset, {});
    });
  });

  test(`Undo Toast (${mode}) claims one action before callback and ignores leaving/detached clicks`, async () => {
    let reduced = false;
    const root = { current: null,
      querySelectorAll() { return this.current?.isConnected ? [this.current] : []; },
      append(node) { this.current = node; node.isConnected = true; },
    };
    const child = () => ({ setAttribute() {}, style: { setProperty() {} } });
    const item = () => ({
      dataset: {},
      isConnected: true,
      matches: { '.toast-message': child(), '.toast-action': child(), '.toast-dismiss': child() },
      classList: { contains: () => false },
      querySelector(s) { return this.matches[s] || null; },
      append(node) { if (node.className) this.matches['.' + node.className] = node; },
      setAttribute() {},
      addEventListener() {},
      contains: () => false,
      remove() { this.isConnected = false; },
    });
    const doc = {
      activeElement: null,
      addEventListener() {},
      querySelector(s) { return s === '#toasts' ? root : null; },
      createElement(tag) { return tag === 'div' ? item() : child(); },
    };
    const timers = new Map();
    let id = 0;
    const globals = {
      document: doc,
      window: { matchMedia: () => ({ matches: reduced }) },
      setTimeout(fn, ms) { timers.set(++id, { fn, ms }); return id; },
      clearTimeout(key) { timers.delete(key); },
    };
    const action = (node) => node.querySelector('.toast-action');
    const render = mode === 'module' ? (await import('../web/core/ui.mjs')).toast
      : loadUiFallback({ ...globals, CP: { ui: { toast() {
          const node = item();
          delete node.matches['.toast-action'];
          node.matches['.toast-dismiss'].before = button => node.append(button);
          root.append(node);
          return node;
        } } } }).uiToast;

    withUiGlobals(globals, () => {
      let calls = 0, first;
      first = render('Removed', { actionLabel: 'Undo', onAction: () => {
        calls += 1;
        if (calls === 1) action(first).onclick(); // synchronous re-entry
      } });
      action(first).onclick();
      action(first).onclick(); // keyboard activation while still connected
      assert.equal(calls, 1);
      assert.equal(first.dataset.leaving, 'true');
      first.remove();
      action(first).onclick();
      assert.equal(calls, 1);
      const alreadyLeaving = render('Other', { actionLabel: 'Undo', onAction: () => { calls += 1; } });
      alreadyLeaving.dataset.leaving = 'true';
      action(alreadyLeaving).onclick();
      assert.equal(calls, 1);
      reduced = true;
      const last = render('Last', { actionLabel: 'Undo', onAction: () => { calls += 1; } });
      action(last).onclick();
      assert.equal(last.isConnected, false);
      action(last).onclick();
      assert.equal(calls, 2);
    });
  });
}
