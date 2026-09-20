"""Execute real JavaScript migrations against isolated legacy browser stores."""
from pathlib import Path
import json
import subprocess
import pytest
ROOT=Path(__file__).resolve().parents[1]


def run_node(script):
    result=subprocess.run(['node','--input-type=module','-e',script],text=True,capture_output=True,timeout=20,cwd=ROOT)
    assert result.returncode==0,result.stdout+result.stderr
    return json.loads(result.stdout)


def storage(values,fail=False):
    script='''import fs from 'node:fs';import vm from 'node:vm';
class Store{constructor(values){this.values={...values};}get length(){return Object.keys(this.values).length;}key(i){return Object.keys(this.values)[i];}getItem(k){return this.values[k]??null;}setItem(k,v){if(FAIL)throw new Error('storage unavailable');this.values[k]=String(v);}removeItem(k){delete this.values[k];}}
const local=new Store(VALUES),session=new Store({});const window={localStorage:local,sessionStorage:session};
vm.runInNewContext(fs.readFileSync('web/brand.js','utf8'),{window});
console.log(JSON.stringify({values:local.values,report:window.CodePierBrand.storageMigration,title:window.CodePierBrand.title,again:window.CodePierBrand.migrateStorage(local)}));'''
    return run_node(script.replace('FAIL','true' if fail else 'false').replace('VALUES',json.dumps(values)))


def test_preferences_and_pending_receipts_keep_exact_values():
    receipt=json.dumps({'project':'existing-project','idempotency_key':'existing-receipt','task':'build'})
    values={'relay-appearance':'dark','relay-task-submission':receipt,'relay-operation':'old-operation',
            'relay-agent-lifecycle:'+'d'*32+':update':'original-lifecycle-key','unrelated-store':'leave-alone'}
    result=storage(values)
    assert result['values']=={'codepier-appearance':'dark','codepier-task-submission':receipt,'codepier-operation':'old-operation',
                              'codepier-agent-lifecycle:'+'d'*32+':update':'original-lifecycle-key','unrelated-store':'leave-alone'}
    assert result['again']['moved']==0 and result['title']=='CodePier · 码头'


def test_new_preference_wins_and_ambiguous_old_receipt_is_not_erased():
    result=storage({'relay-appearance':'dark','codepier-appearance':'light'})
    assert result['values']=={'relay-appearance':'dark','codepier-appearance':'light'}
    assert result['report'][0]['conflicts']==1


def test_storage_failure_never_deletes_legacy_receipt():
    result=storage({'relay-operation':'pending-operation'},True)
    assert result['values']=={'relay-operation':'pending-operation'} and result['report'][0]['unavailable']


def test_disabled_storage_does_not_break_boot():
    result=run_node("import fs from 'node:fs';import vm from 'node:vm';const window={};for(const k of ['localStorage','sessionStorage'])Object.defineProperty(window,k,{get(){throw new Error('blocked')}});vm.runInNewContext(fs.readFileSync('web/brand.js','utf8'),{window});console.log(JSON.stringify(window.CodePierBrand.storageMigration));")
    assert all(x['unavailable'] for x in result)


def test_extension_keeps_profile_permissions_lease_and_replay_ledger():
    saved={'version':1,'profile_id':'a'*32,'origins':['https://example.test'],
           'pool':[{'tab_id':7,'window_id':4,'lease_id':'b'*32,'expires':9999999999999}],'seen':['c'*32]}
    script="""import {BrowserWorkspace} from './web/browser-extension/workspace.js';
const values={relayWorkspace:SAVED};const local={async get(k){return {[k]:values[k]}},async set(v){Object.assign(values,v)},async remove(k){delete values[k]}};
const workspace=new BrowserWorkspace({storage:{local}});const first=await workspace.load();const second=await new BrowserWorkspace({storage:{local}}).load();console.log(JSON.stringify({first,second,values}));"""
    result=run_node(script.replace('SAVED',json.dumps(saved)))
    assert result['first']==saved and result['second']==saved and result['values']=={'codepierWorkspace':saved}


def test_extension_write_failure_preserves_old_state():
    result=run_node("""import {BrowserWorkspace} from './web/browser-extension/workspace.js';
const values={relayWorkspace:{version:1,profile_id:'a'.repeat(32),origins:[],pool:[],seen:[]}};let removed=false;
const local={async get(k){return {[k]:values[k]}},async set(){throw new Error('blocked')},async remove(){removed=true}};
try{await new BrowserWorkspace({storage:{local}}).load()}catch{}
console.log(JSON.stringify({removed,hasLegacy:!!values.relayWorkspace}));""")
    assert result=={'removed':False,'hasLegacy':True}


def test_page_boots_brand_migration_before_reading_preferences():
    html=(ROOT/'web/index.html').read_text()
    assert html.index('/static/brand.js')<html.index('/static/appearance.js')
    assert '<title>CodePier · 码头</title>' in html
    assert 'AI 与本地代码对接、任务停靠的地方' in html and '>R</span>' not in html
