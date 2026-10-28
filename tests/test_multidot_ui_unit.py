"""Deterministic multi-Dot presentation and hint transport contract tests.

No browser, server, model or native subscription is started by this suite.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node") or (
    "/opt/homebrew/bin/node" if Path("/opt/homebrew/bin/node").exists() else None
)
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is required for shipped JS contracts")

HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
global.window = global;
global.addEventListener = () => {};
global.S = {page:'collaboration',session:'session',space_id:'space'};
for (const name of ['collaboration-relay.js','collaboration-goals.js'])
  vm.runInThisContext(fs.readFileSync(path.join(process.argv[1], 'web', name), 'utf8'), {filename:name});
const Relay = CodePierCollaborationRelay, Goals = CodePierCollaborationGoals;
function clock() {
  let now = 0, serial = 0;
  const timers = new Map();
  return {
    timers,
    schedule(fn, ms) { const id=++serial;timers.set(id,{fn,due:now+ms,ms});return id; },
    cancel(id) { timers.delete(id); },
    advance(ms) {
      const target=now+ms;
      for(let n=0;n<1000;n++) {
        const next=[...timers].filter(([,t])=>t.due<=target).sort((a,b)=>a[1].due-b[1].due)[0];
        if(!next){now=target;return;}
        timers.delete(next[0]);now=next[1].due;next[1].fn();
      }
      throw Error('timer loop');
    },
  };
}
function streamFixture(overrides={}) {
  const time=clock(), sources=[], changes=[], states=[], access=[];
  const stream=Relay.hintStream({
    url:'/api/collaboration/relay-hints?project=p',
    schedule:time.schedule,cancel:time.cancel,random:()=>0,
    createSource(url) {
      const handlers={};
      const source={url,closed:0,onopen:null,onerror:null,
        addEventListener(name,fn){handlers[name]=fn;},
        close(){this.closed++;},
        emit(name,data){handlers[name]?.({data:typeof data==='string'?data:JSON.stringify(data)});}};
      sources.push(source);return source;
    },
    onChange:()=>changes.push('read-only-sync'),onState:value=>states.push(value),
    onAccessChanged:()=>access.push(true),...overrides,
  });
  return {stream,time,sources,changes,states,access};
}
function teamFixture() {
  const c={draft:'中文草稿',mentions:['old'],reply:'old-message',relayAnchor:{id:'old'},teamId:''};
  const team={id:'team',label:'Team',state:'active',version:2,
    expires_at:Date.now()/1000+3600,coordinator_dot_id:'a',
    participants:[{dot_id:'a',label:'Lead'},{dot_id:'b',label:'Member'}],
    project_ids:['p'],capabilities:['read']};
  const slots=['a','b'].map(id=>({id,label:id,task_dot:true,duplex:true,state:'registered'}));
  const state={generation:1,project:'p',environment:'production',conversation:'room',
    session:S.session,space:S.space_id,
    snapshot:{can_manage:true,room:{id:'room',state:'active'},
      teams:[team],capabilities:{multidot_teams:true,native_dot_relay:true}}};
  const calls=[],drawers=[];
  global.document={querySelector:()=>({focus:()=>{}})};
  const context={state,E:value=>String(value??''),field:(label,value)=>value,
    button:(action,label,row={})=>'<button data-action="'+action+'">'+label+'</button>',
    empty:value=>value,getRecord:async()=>({}),mutation:async(op,args)=>{
      calls.push({op,args});return {team:{...team,state:args.action==='pause'?'paused':args.action==='resume'?'active':'revoked',version:3}};
    },showDrawer:(title,html)=>drawers.push({title,html}),closeDrawer:()=>{},
    projectLabel:id=>id,roomProjects:()=>[{project_id:'p',environment_id:'production'}],
    when:value=>String(value),chat:()=>c,mentionSlots:()=>slots,
    selectPolicy:()=>{},patchDraftContext:()=>{},patchDelegationComposer:()=>{}};
  const ui=Goals.create(context);
  return {c,team,slots,state,calls,drawers,ui,context};
}
"""

def run_js(body):
    completed = subprocess.run(
        [NODE, "-e", HARNESS + "\n(async()=>{\n" + body +
         "\n})().catch(error=>{console.error(error);process.exitCode=1;});", str(ROOT)],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_hints_coalesce_only_valid_revision_changes_and_ignore_heartbeats():
    run_js(r"""
const f=streamFixture();f.stream.start();
assert.equal(f.sources.length,1);
const source=f.sources[0];source.onopen();
assert.equal(f.stream.healthy(),true);
source.emit('message',{revision:'heartbeat'});source.emit('heartbeat',{revision:'heartbeat'});
source.emit('change','malformed');source.emit('change',{revision:{}});
source.emit('change',{revision:'x'.repeat(257)});f.time.advance(100);
assert.equal(f.changes.length,0);
source.emit('change',{revision:'one',state:'completed'});
source.emit('change',{revision:'one'});source.emit('change',{revision:'two'});
f.time.advance(59);assert.equal(f.changes.length,0);
f.time.advance(1);assert.deepEqual(f.changes,['read-only-sync']);
source.emit('change',{revision:'two'});f.time.advance(100);
assert.equal(f.changes.length,1);
source.emit('change',{revision:'three'});f.time.advance(60);
assert.equal(f.changes.length,2);
assert.deepEqual(f.states,[true]);
f.stream.stop();
""")


def test_hint_retry_is_bounded_and_old_connections_cannot_deliver():
    run_js(r"""
const f=streamFixture();f.stream.start();
const first=f.sources[0];first.onopen();first.onerror();
assert.equal(first.closed,1);assert.equal(f.stream.healthy(),false);
assert.equal([...f.time.timers.values()][0].ms,1000);
first.emit('change',{revision:'stale'});f.time.advance(1000);
assert.equal(f.sources.length,2);assert.equal(f.changes.length,0);
for(let i=0;i<7;i++){
  const source=f.sources.at(-1);source.onopen();source.onerror();
  const delay=[...f.time.timers.values()][0].ms;
  assert.ok(delay>=1000 && delay<=30000);
  f.time.advance(delay);
}
f.sources.at(-1).emit('change',{revision:'good'});f.time.advance(60);
f.sources.at(-1).onerror();
assert.equal([...f.time.timers.values()][0].ms,1000);
f.stream.stop();assert.equal(f.time.timers.size,0);
""")


def test_hint_stop_visibility_scope_and_access_loss_are_fenced():
    run_js(r"""
let current=true;
const f=streamFixture({current:()=>current});f.stream.start();
const old=f.sources[0];old.onopen();old.emit('change',{revision:1});
current=false;f.stream.stop();f.time.advance(100);
assert.equal(f.changes.length,0);assert.equal(f.time.timers.size,0);
f.stream.start();assert.equal(f.sources.length,1);
current=true;f.stream.start();assert.equal(f.sources.length,2);
old.emit('change',{revision:2});f.time.advance(100);assert.equal(f.changes.length,0);
f.sources[1].emit('access_changed',{});
assert.equal(f.sources[1].closed,1);assert.deepEqual(f.access,[true]);
f.stream.start();assert.equal(f.sources.length,2);
assert.equal(f.stream.healthy(),false);
""")


def test_no_hint_capability_or_constructor_failure_keeps_polling_available():
    run_js(r"""
const disabled=streamFixture({enabled:()=>false});disabled.stream.start();
assert.equal(disabled.sources.length,0);assert.equal(disabled.stream.healthy(),false);
const failed=streamFixture({createSource:()=>{throw Error('unsupported');}});
failed.stream.start();assert.equal(failed.stream.healthy(),false);
assert.equal([...failed.time.timers.values()][0].ms,1000);
failed.stream.stop();assert.equal(failed.time.timers.size,0);
""")


def test_pending_message_keeps_team_identity_and_exact_single_coordinator():
    run_js(r"""
const value={project:'p',environment_id:'prod',room_id:'room',conversation_id:'room',
  body_text:'work',reply_to_id:'source',team_id:'team',mentions:[{slot_id:'lead'}],
  unsafe_extra:'not persisted'};
const saved=Relay.pendingPayload(value,'lead');
assert.equal(saved.team_id,'team');assert.equal(saved.reply_to_id,'source');
assert.deepEqual(saved.mentions,[{slot_id:'lead'}]);assert.equal(saved.unsafe_extra,undefined);
assert.equal(Relay.pendingPayload({...value,team_id:{}},'lead'),null);
assert.equal(Relay.pendingPayload({...value,team_id:'x'.repeat(257)},'lead'),null);
assert.equal(Relay.pendingPayload({...value,mentions:[{slot_id:'lead'},{slot_id:'member'}]},'lead'),null);
assert.equal(Relay.pendingPayload(value,'other'),null);
""")


def test_team_readiness_requires_current_approved_unexpired_scope():
    run_js(r"""
const now=100000;
assert.equal(Goals.teamState(null,now).available,false);
assert.equal(Goals.teamState({state:'active'},now).available,false);
assert.equal(Goals.teamState({state:'active',expires_at:99},now).available,false);
assert.equal(Goals.teamState({state:'paused',expires_at:101},now).available,false);
assert.equal(Goals.teamState({state:'proposed',expires_at:101},now).available,false);
assert.equal(Goals.teamState({state:'active',expires_at:101},now).available,true);
const f=teamFixture();
assert.equal(f.ui.teamAvailable('team','a'),true);
assert.equal(f.ui.teamAvailable('team','b'),false);
f.slots[0].status='authorization_unavailable';
assert.equal(f.ui.teamAvailable('team','a'),false);
""")


def test_member_completion_never_becomes_final_without_current_publication():
    run_js(r"""
const work=[{state:'succeeded'},{state:'completed'}];
assert.equal(Goals.teamProgress({},work).final,false);
assert.equal(Goals.teamProgress({},[{state:'running'}]).label,'成员正在协作');
assert.equal(Goals.teamProgress({},[{state:'blocked'}]).label,'等待处理或复核');
assert.equal(Goals.teamProgress({intent_revision:2,publication:{state:'committed',intent_revision:1}},work).final,false);
const view=Goals.teamProgress({intent_revision:2,publication:{state:'committed',intent_revision:2,
  outcome:'partial',summary:'one member is blocked'}},work);
assert.equal(view.final,true);assert.equal(view.label,'已提交部分结果');
assert.equal(view.summary,'one member is blocked');
""")


def test_team_selection_is_one_coordinator_and_retains_draft():
    run_js(r"""
const f=teamFixture();
await f.ui.act('team-pick',{dataset:{id:'team'}});
assert.equal(f.c.teamId,'team');assert.deepEqual(f.c.mentions,['a']);
assert.equal(f.c.draft,'中文草稿');assert.equal(f.c.reply,'');assert.equal(f.c.relayAnchor,null);
assert.equal(f.calls.length,0);
await assert.rejects(f.ui.act('team-pause',{dataset:{id:'team',version:'1'}}),/版本/);
assert.equal(f.calls.length,0);
await f.ui.act('team-pause',{dataset:{id:'team',version:'2'}});
assert.equal(f.calls.length,1);assert.equal(f.calls[0].args.expected_version,2);
assert.equal(f.ui.teamAvailable('team','a'),false);
assert.equal(f.c.teamId,'team');assert.equal(f.c.draft,'中文草稿');
""")


def test_team_send_cannot_fall_back_to_an_individual_after_revocation():
    run_js(r"""
const f=teamFixture();f.c.teamId='team';f.c.mentions=['a'];
const relay=Relay.create({...f.context,chatScope:()=>({project:'p'}),
  teamAvailable:f.ui.teamAvailable,dots:{},delegation:{}});
assert.equal(relay.canSend(),true);
f.state.snapshot.teams[0]={...f.team,state:'revoked'};
assert.equal(relay.canSend(),false);
f.c.teamId='';assert.equal(relay.canSend(),true);
""")


def test_bounded_team_projection_preserves_older_goals_and_unchanged_render_identity():
    run_js(r"""
const f=teamFixture();f.state.snapshot.capabilities.coordination_goals=true;
const goal=id=>({id,objective:id,state:'active',team:{team_id:'team',intent_revision:1}});
assert.equal(f.ui.updateProjection({team_goals:[goal('older')]}),true);
assert.equal(f.ui.updateProjection({team_goals:[goal('latest')]}),true);
assert.equal(f.ui.updateProjection({team_goals:[goal('latest')]}),false);
assert.match(f.ui.list(),/older/);assert.match(f.ui.list(),/latest/);
assert.equal(f.ui.updateProjection({team_goals:[]}),false);
assert.equal(f.ui.updateProjection({team_goals:[],team_goals_complete:true}),true);
assert.equal(Goals.teamProgress({publication:{state:'committed'}},[]).final,false);
""")


def test_paused_team_resumes_exact_version_without_replaying_work():
    run_js(r"""
const f=teamFixture();f.state.snapshot.teams[0]={...f.team,state:'paused'};
assert.match(f.ui.teamPicker(),/team-resume/);
assert.equal(f.ui.teamAvailable('team','a'),false);
await f.ui.act('team-resume',{dataset:{id:'team',version:'2'}});
assert.deepEqual(f.calls,[{op:'team-control',args:{team_id:'team',expected_version:2,action:'resume'}}]);
assert.equal(f.ui.teamAvailable('team','a'),true);
assert.equal(f.c.draft,'中文草稿');
assert.doesNotMatch(f.ui.teamPicker(),/team-resume/);
f.state.snapshot.teams[0]={...f.team,state:'revoked'};
assert.doesNotMatch(f.ui.teamPicker(),/team-resume/);
""")


def test_hint_url_carries_authorized_space_without_custom_headers():
    run_js(r"""
const url=new URL(Relay.hintURL({project:'p',environment_id:'prod',conversation_id:'room',
  space_id:'stale-space'},'selected-space'),'https://panel.example');
assert.equal(url.pathname,'/api/collaboration/relay-hints');
assert.equal(url.searchParams.get('space_id'),'selected-space');
assert.equal(url.searchParams.get('conversation_id'),'room');
assert.equal(url.searchParams.get('project'),'p');
assert.equal(url.origin,'https://panel.example');
""")


def test_goal_initial_load_has_one_page_budget_and_history_is_explicit():
    run_js(r"""
const f=teamFixture();f.state.snapshot.capabilities.coordination_goals=true;
const reads=[];
const ui=Goals.create({...f.context,getRecord:async(kind,id,cursor='',extra={})=>{
  reads.push({kind,cursor,extra});
  if(kind==='coordination_options')return {participants:[],can_approve:true};
  assert.equal(kind,'coordination_goals');assert.equal(extra.limit,'40');
  const start=cursor==='after-history'?80:cursor==='next'?40:0;
  return {items:Array.from({length:Math.min(40,100-start)},(_,i)=>({id:'goal-'+(start+i),objective:'goal-'+(start+i),state:'active'})),
    next_cursor:start===0?'next':start===40?'after-history':null};
}});
await ui.load();
assert.equal(reads.filter(read=>read.kind==='coordination_goals').length,1);
assert.match(ui.list(),/goal-more/);assert.doesNotMatch(ui.list(),/goal-79/);
await ui.act('goal-more',{});
assert.equal(reads.filter(read=>read.kind==='coordination_goals').length,2);
assert.match(ui.list(),/goal-79/);assert.match(ui.list(),/goal-0/);
await ui.load();
assert.equal(reads.filter(read=>read.kind==='coordination_goals').length,3);
assert.match(ui.list(),/goal-79/);
assert.equal(reads.some(read=>read.cursor==='after-history'),false);
await ui.act('goal-more',{});
assert.equal((ui.list().match(/class="cc-goal-summary"/g)||[]).length,100,
  'After a same-scope first-page refresh, an explicit next page must advance 80 to 100');
assert.deepEqual(reads.filter(read=>read.kind==='coordination_goals').map(read=>read.cursor),
  ['','next','','after-history']);
assert.doesNotMatch(ui.list(),/goal-more/);
await ui.load();
assert.equal(reads.filter(read=>read.kind==='coordination_goals').length,5);
assert.equal((ui.list().match(/class="cc-goal-summary"/g)||[]).length,100);
assert.doesNotMatch(ui.list(),/goal-more/);
""")



def test_expanded_goal_history_never_reuses_progress_across_scope_changes():
    run_js(r"""
const f=teamFixture();f.state.snapshot.capabilities.coordination_goals=true;
const reads=[];
const ui=Goals.create({...f.context,getRecord:async(kind,id,cursor='')=>{
  if(kind==='coordination_options')return {participants:[],can_approve:true};
  const scope=f.state.project==='p'?'old':'new';
  reads.push({scope,cursor});
  const goalId=scope+(cursor?'-history':'-first');
  return {items:[{id:goalId,objective:goalId}],next_cursor:scope+(cursor?'-last':'-next')};
}});
await ui.load();
await ui.act('goal-more',{});
assert.match(ui.list(),/old-history/);
f.state.generation++;f.state.project='another-project';f.state.session='another-session';
await ui.load();
assert.doesNotMatch(ui.list(),/old-first|old-history/);
await ui.act('goal-more',{});
assert.deepEqual(reads,[
  {scope:'old',cursor:''},{scope:'old',cursor:'old-next'},
  {scope:'new',cursor:''},{scope:'new',cursor:'new-next'},
]);
assert.match(ui.list(),/new-history/);assert.doesNotMatch(ui.list(),/old-history/);
""")


def test_goal_repeated_cursor_does_not_loop_or_erase_loaded_history():
    run_js(r"""
const f=teamFixture();f.state.snapshot.capabilities.coordination_goals=true;
let reads=0;
const ui=Goals.create({...f.context,getRecord:async(kind,id,cursor='',extra={})=>{
  if(kind==='coordination_options')return {participants:[],can_approve:true};
  reads++;
  return {items:[{id:cursor?'unexpected':'kept',objective:cursor?'unexpected':'kept'}],next_cursor:'same'};
}});
await ui.load();assert.equal(reads,1);
await assert.rejects(ui.act('goal-more',{}),/游标重复/);
assert.equal(reads,2);assert.match(ui.list(),/kept/);assert.doesNotMatch(ui.list(),/unexpected/);
""")


def test_late_history_page_rebases_after_projection_without_losing_user_intent():
    run_js(r"""
const f=teamFixture();f.state.snapshot.capabilities.coordination_goals=true;
let releaseOld,releaseProjection,initialPages=0;
const cursors=[];
const ui=Goals.create({...f.context,getRecord:async(kind,id,cursor='')=>{
  if(kind==='coordination_options')return {participants:[],can_approve:true};
  if(cursor){
    cursors.push(cursor);
    if(cursor==='old-cursor')return new Promise(resolve=>{releaseOld=resolve;});
    assert.equal(cursor,'fresh-cursor');
    return {items:[{id:'fresh-history',objective:'fresh-history',version:3}],next_cursor:null};
  }
  initialPages++;
  if(initialPages===2)return new Promise(resolve=>{releaseProjection=resolve;});
  return {items:[{id:'current',objective:'current',version:2}],next_cursor:'old-cursor'};
}});
await ui.load();
const history=ui.act('goal-more',{});
const projection=ui.load();
await new Promise(resolve=>setImmediate(resolve));
releaseOld({items:[{id:'stale',objective:'stale',version:1}],next_cursor:null});
await new Promise(resolve=>setImmediate(resolve));
assert.deepEqual(cursors,['old-cursor']); // Wait for the current authority projection.
releaseProjection({items:[{id:'current',objective:'current',version:3}],next_cursor:'fresh-cursor'});
await projection;
await history;
assert.deepEqual(cursors,['old-cursor','fresh-cursor']);
assert.match(ui.list(),/current/);assert.match(ui.list(),/fresh-history/);
assert.doesNotMatch(ui.list(),/stale/);
""")


def test_goal_history_rebase_is_bounded_and_exposes_retry_without_losing_records():
    run_js(r"""
const f=teamFixture();f.state.snapshot.capabilities.coordination_goals=true;
let histories=0,ui;
ui=Goals.create({...f.context,getRecord:async(kind,id,cursor='')=>{
  if(kind==='coordination_options')return {participants:[],can_approve:true};
  if(cursor){
    histories++;
    await ui.load();
    return {items:[{id:'stale',objective:'stale'}],next_cursor:null};
  }
  return {items:[{id:'current',objective:'current'}],next_cursor:'next'};
}});
await ui.load();
await assert.rejects(ui.act('goal-more',{}),/目标列表仍在更新/);
assert.equal(histories,2);
assert.match(ui.list(),/current/);assert.match(ui.list(),/goal-more/);
assert.doesNotMatch(ui.list(),/stale/);
""")


def test_goal_history_does_not_rebase_into_a_changed_project_or_session():
    run_js(r"""
const f=teamFixture();f.state.snapshot.capabilities.coordination_goals=true;
let release,histories=0;
const ui=Goals.create({...f.context,getRecord:async(kind,id,cursor='')=>{
  if(kind==='coordination_options')return {participants:[],can_approve:true};
  if(cursor){histories++;return new Promise(resolve=>{release=resolve;});}
  const goalId=f.state.project==='p'?'current':'new-scope';
  return {items:[{id:goalId,objective:goalId}],next_cursor:'next'};
}});
await ui.load();
const pending=ui.act('goal-more',{});
f.state.generation++;f.state.project='another-project';
await ui.load();
release({items:[{id:'stale-private',objective:'stale-private'}],next_cursor:null});
await pending;
assert.equal(histories,1);
assert.match(ui.list(),/new-scope/);assert.doesNotMatch(ui.list(),/stale-private/);
""")
