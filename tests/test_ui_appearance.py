"""Executable contract checks for the dependency-free appearance controller."""
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'web' / 'appearance.js'


NODE_HARNESS = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),testCase=process.argv[2];

function target(){
  const listeners=new Map();
  return {
    addEventListener(type,fn){if(!listeners.has(type))listeners.set(type,new Set());listeners.get(type).add(fn);},
    removeEventListener(type,fn){listeners.get(type)?.delete(fn);},
    dispatchEvent(event){for(const fn of [...(listeners.get(event.type)||[])])fn(event);return true;},
  };
}
function harness({saved=null,legacy=null,dark=false,storageFails=false}={}){
  const values=new Map();
  if(saved!==null)values.set('codepier-appearance',saved);
  if(legacy!==null)values.set('codepier-chat-appearance',legacy);
  const storage={
    getItem(key){if(storageFails)throw new Error('disabled');return values.has(key)?values.get(key):null;},
    setItem(key,value){if(storageFails)throw new Error('disabled');values.set(key,String(value));},
  };
  const root={dataset:{},style:{}},meta={content:'',name:'theme-color'};
  const controls=[];
  const document=Object.assign(target(),{
    documentElement:root,
    head:{append(node){if(node.name==='theme-color')meta.content=node.content||'';}},
    createElement(){return {name:'',content:''};},
    querySelector(selector){return selector==='meta[name="theme-color"]'?meta:null;},
    querySelectorAll(){return controls;},
  });
  const media=Object.assign(target(),{matches:dark,addListener(fn){this.addEventListener('change',fn);},removeListener(fn){this.removeEventListener('change',fn);}});
  const window=Object.assign(target(),{document,localStorage:storage,matchMedia:()=>media});
  class CustomEvent{constructor(type,options={}){this.type=type;this.detail=options.detail;}}
  class MutationObserver{constructor(fn){this.fn=fn;}observe(){window.observer=this;}}
  const context={window,document,CustomEvent,MutationObserver,Set,TypeError};
  vm.createContext(context);vm.runInContext(source,context);
  return {window,document,root,meta,controls,media,storage,values,api:window.CodePierAppearance};
}
function control(kind){
  return {
    value:'auto',attrs:{},
    matches(selector){return kind==='ui'&&selector==='[data-ui-appearance]';},
    setAttribute(name,value){this.attrs[name]=value;},
    closest(selector){return this.matches(selector)?this:null;},
  };
}

if(testCase==='preference'){
  const h=harness(),ui=control('ui'),chat=control('chat');h.controls.push(ui,chat);
  const events=[];h.window.addEventListener('codepier:appearance',event=>events.push(event.detail));
  ui.value='dark';h.document.dispatchEvent({type:'change',target:ui});
  assert.equal(h.api.getPreference(),'dark');assert.equal(h.api.getScheme(),'dark');
  assert.equal(h.root.dataset.appearance,'dark');assert.equal(h.root.style.colorScheme,'dark');
  assert.equal(h.meta.content,'#191919');assert.equal(ui.attrs['aria-label'],'全站外观');
  assert.equal(chat.value,'dark');assert.equal(h.values.get('codepier-appearance'),'dark');
  assert.deepEqual(events.at(-1),{preference:'dark',scheme:'dark'});
}else if(testCase==='migration'){
  const h=harness({legacy:'auto',dark:true});
  assert.equal(h.api.getPreference(),'auto');assert.equal(h.api.getScheme(),'dark');
  assert.equal(h.values.get('codepier-appearance'),'auto');assert.equal(h.root.dataset.appearance,'dark');
  h.media.matches=false;h.media.dispatchEvent({type:'change'});
  assert.equal(h.api.getScheme(),'light');assert.equal(h.meta.content,'#f4f4f4');
}else if(testCase==='storage'){
  const h=harness(),received=[];
  const unsubscribe=h.api.subscribe(value=>received.push(value));
  h.values.set('codepier-appearance','dark');
  h.window.dispatchEvent({type:'storage',key:'codepier-appearance',newValue:'dark',storageArea:h.storage});
  assert.equal(h.api.getPreference(),'dark');assert.deepEqual(received.at(-1),{preference:'dark',scheme:'dark'});
  const count=received.length;unsubscribe();
  h.values.set('codepier-appearance','light');
  h.window.dispatchEvent({type:'storage',key:'codepier-appearance',newValue:'light',storageArea:h.storage});
  assert.equal(h.api.getPreference(),'light');assert.equal(received.length,count);
}else if(testCase==='unavailable'){
  const h=harness({storageFails:true,dark:true});
  assert.equal(h.api.getPreference(),'auto');assert.equal(h.api.getScheme(),'dark');
  h.api.setPreference('light');
  assert.equal(h.api.getPreference(),'light');assert.equal(h.root.dataset.appearance,'light');
}else throw new Error('unknown case');
process.stdout.write(JSON.stringify({case:testCase,ok:true}));
"""


def run_case(name):
    result = subprocess.run(
        ['node', '-e', NODE_HARNESS, str(SCRIPT), name],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )
    assert json.loads(result.stdout) == {'case': name, 'ok': True}


def test_appearance_source_contract():
    source = SCRIPT.read_text()
    assert 'window.CodePierAppearance' in source
    for method in ['getPreference', 'getScheme', 'setPreference', 'subscribe']:
        assert method in source
    assert "'codepier-appearance'" in source
    assert "'codepier-chat-appearance'" in source
    assert 'codepier:appearance' in source
    assert 'data-ui-appearance' in source


def test_preference_controls_event_and_theme_color():
    run_case('preference')


def test_auto_and_legacy_migration():
    run_case('migration')


def test_cross_tab_storage_sync_and_unsubscribe():
    run_case('storage')


def test_storage_unavailable_keeps_in_memory_operation():
    run_case('unavailable')
