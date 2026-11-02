"""Browser-independent room-store races. These exercise behavior, not markup."""
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which('node')
pytestmark = pytest.mark.skipif(not NODE, reason='Node is required for panel store tests')


def run_js(body):
    source = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const context = {window:{},crypto:require('node:crypto').webcrypto,AbortController,Map,Set,Date,Math,JSON};
vm.runInNewContext(fs.readFileSync('web/collaboration-room-store.js','utf8'),context);
const create=context.window.CodePierRoomStore.create;
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const plain=value=>JSON.parse(JSON.stringify(value));
(async()=>{
""" + body + "\n})().catch(error=>{console.error(error);process.exit(1);});\n"
    result = subprocess.run([NODE, '-e', source], cwd=ROOT, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_late_room_read_cannot_replace_current_room_or_messages():
    run_js(r"""
const calls=[];
const store=create((url,options)=>{const value=deferred();calls.push({url,options,...value});return value.promise;});
store.reset('owner');
const first=store.select('room-a');
const second=store.select('room-b');
assert.equal(calls[0].options.signal.aborted,true);
calls[2].resolve({room:{id:'room-b',title:'B',revision:2}});
calls[3].resolve({messages:[{id:'b',body_text:'B',created_at:2}],works:[],visibility_revision:'b'});
await second;
calls[0].resolve({room:{id:'room-a',title:'A',revision:20}});
calls[1].resolve({messages:[{id:'a',body_text:'A',created_at:1}],works:[],visibility_revision:'a'});
await first;
assert.equal(store.state.room.id,'room-b');
assert.deepEqual(plain(store.state.messages.map(m=>m.id)),['b']);
""")


def test_same_domain_refresh_is_single_flight_and_older_read_cannot_undo_write():
    run_js(r"""
const calls=[];
const store=create((url,options)=>{const value=deferred();calls.push({url,options,...value});return value.promise;});
store.state.roomId='room';
const first=store.refresh();
const second=store.refresh();
assert.equal(calls.length,2);
store.applyRoom({room:{id:'room',title:'Saved name',revision:9}});
calls[0].resolve({room:{id:'room',title:'Old name',revision:8}});
calls[1].resolve({messages:[],works:[]});
await Promise.all([first,second]);
assert.equal(store.state.room.title,'Saved name');
""")


def test_unknown_send_retains_one_identity_and_original_room_after_switch():
    run_js(r"""
const calls=[];
let fail=true;
const store=create(async(url,options)=>{
  calls.push({url,body:JSON.parse(options.body)});
  if(fail)throw Object.assign(new Error('lost response'),{code:'NETWORK_UNCERTAIN'});
  return {message:{id:'saved',client_message_id:calls.at(-1).body.client_message_id,body_text:'original'}};
});
store.reset('owner');store.state.roomId='room-a';store.draft().text='original';
await assert.rejects(store.send());
const receipt=[...store.state.pending.values()][0];
assert.equal(receipt.state,'unknown');
store.abort();store.state.roomId='room-b';store.draft().text='new room draft';
fail=false;await store.send(receipt);
assert.equal(calls[0].url,calls[1].url);
assert.equal(calls[0].body.client_message_id,calls[1].body.client_message_id);
assert.equal(calls[0].body.idempotency_key,calls[1].body.idempotency_key);
assert.equal(store.draft().text,'new room draft');
assert.equal(store.state.messages.length,0);
assert.equal(store.state.pending.size,0);
""")


def test_permission_revision_discards_cached_hidden_messages_and_work():
    run_js(r"""
let revision='visible', messages=[{id:'secret',body_text:'project-private',created_at:1}];
const store=create(async()=>({messages,works:[],visibility_revision:revision}));
store.state.roomId='room';
await store.messages();
store.applyWork({id:'old-work',intent_revision:1});
store.draft().reading={anchorId:'secret',visibilityRevision:'visible'};
assert.equal(store.state.messages.length,1);
revision='narrowed';messages=[{id:'public',body_text:'general',created_at:2}];
await store.messages();
assert.deepEqual(plain(store.state.messages.map(m=>m.id)),['public']);
assert.equal(store.state.works.length,0);
assert.equal(store.draft().reading,undefined);
""")


def test_split_read_revision_converges_immediately_after_newer_room_snapshot():
    run_js(r"""
const calls=[],store=create((url,options)=>{const value=deferred();calls.push({url,...value});return value.promise;});
store.reset('owner');store.state.roomId='room';
const refreshing=store.refresh();
calls[0].resolve({room:{id:'room',revision:12}});
calls[1].resolve({messages:[{id:'stale'}],revision:11,visibility_revision:'old'});
await new Promise(resolve=>setImmediate(resolve));
assert.equal(calls.length,3);assert.equal(store.state.messages.length,0);
assert.equal(calls[2].url,'/api/collaboration/rooms/room/messages?limit=50');
calls[2].resolve({messages:[{id:'general',body_text:'still visible'}],works:[],revision:12,visibility_revision:'new'});
await refreshing;
assert.deepEqual(plain(store.state.messages.map(message=>message.id)),['general']);
assert.equal(store.state.messageRevision,12);
""")


def test_owner_change_drops_pending_mutation_completion_and_private_drafts():
    run_js(r"""
const result=deferred(),store=create(()=>result.promise);
store.reset('alice');store.state.roomId='room';store.draft().text='private draft';
store.state.mutations.set('original-action',{body:'private form input',owner:'alice'});
const sending=store.send();
store.reset('bob');
result.resolve({message:{id:'saved',body_text:'private draft'}});
await sending;
assert.equal(store.state.messages.length,0);
assert.equal(store.state.pending.size,0);
assert.equal(store.state.drafts.size,0);
assert.equal(store.state.mutations.size,0);
""")


def test_file_upload_retry_keeps_original_bytes_key_and_room_after_navigation():
    run_js(r"""
const calls=[];let fail=true;
const store=create(async(url,options)=>{
  calls.push({url,options});
  if(fail)throw Object.assign(new Error('lost upload response'),{code:'NETWORK_UNCERTAIN'});
  return {attachment:{id:'stored-file',name:'input.txt'}};
});
store.reset('alice');store.state.roomId='room-a';
const file={name:'input.txt',size:24};
const [entry]=store.addFiles([file]);
await assert.rejects(store.upload(entry));
assert.equal(entry.state,'unknown');
store.abort();store.state.roomId='room-b';store.draft().text='different room';
fail=false;await store.upload(entry);
assert.equal(calls[0].url,'/api/collaboration/rooms/room-a/attachments');
assert.equal(calls[1].url,calls[0].url);
assert.equal(calls[0].options.body,file);assert.equal(calls[1].options.body,file);
assert.equal(calls[0].options.headers['X-Upload-Key'],calls[1].options.headers['X-Upload-Key']);
assert.equal(entry.state,'ready');
assert.equal(store.draft('room-a').files[0].attachment.id,'stored-file');
assert.equal(store.draft().files.length,0);assert.equal(store.draft().text,'different room');
""")


def test_late_upload_never_resurrects_removed_file_or_old_owner_draft():
    run_js(r"""
const pending=[];const store=create(()=>{const result=deferred();pending.push(result);return result.promise;});
store.reset('alice');store.state.roomId='room';
const [removed]=store.addFiles([{name:'removed.txt',size:5}]);
const first=store.upload(removed);store.removeFile(removed.id);
pending[0].resolve({attachment:{id:'removed'}});await first;
assert.equal(store.draft().files.length,0);
const [privateFile]=store.addFiles([{name:'private.txt',size:5}]);
const second=store.upload(privateFile);store.reset('bob');
pending[1].resolve({attachment:{id:'private'}});await second;
assert.equal(store.state.drafts.size,0);
assert.equal(store.state.pending.size,0);
""")


def test_attachment_send_snapshot_clears_only_confirmed_files_and_preserves_new_draft():
    run_js(r"""
const pending=deferred();let posted;
const store=create((url,options)=>{posted={url,body:JSON.parse(options.body)};return pending.promise;});
store.reset('alice');store.state.roomId='room';
const [original]=store.addFiles([{name:'first.txt',size:1}]);
original.state='ready';original.attachment={id:'first',name:'first.txt'};
const sending=store.send();
assert.equal(posted.body.body_text,'');assert.deepEqual(posted.body.attachment_ids,['first']);
const [newFile]=store.addFiles([{name:'next.txt',size:1}]);
newFile.state='ready';newFile.attachment={id:'next',name:'next.txt'};
store.draft().text='new text while sending';
pending.resolve({message:{id:'stored',client_message_id:posted.body.client_message_id,attachments:[{id:'first'}]}});
await sending;
assert.deepEqual(plain(store.draft().files.map(entry=>entry.attachment.id)),['next']);
assert.equal(store.draft().text,'new text while sending');
assert.equal(store.state.pending.size,0);
""")


def test_work_supplement_uses_frozen_intent_action_and_canonical_receipt_recovers_once():
    run_js(r"""
let posted;const store=create(async(url,options)=>{
  posted={url,body:JSON.parse(options.body)};
  throw Object.assign(new Error('lost response'),{code:'NETWORK_UNCERTAIN'});
});
store.reset('alice');store.state.roomId='room';
Object.assign(store.draft(),{text:'补充约束',work:'original-work',workVersion:7});
const [entry]=store.addFiles([{name:'constraint.txt',size:2}]);
entry.state='ready';entry.attachment={id:'constraint',name:'constraint.txt'};
await assert.rejects(store.send());
assert.equal(posted.url,'/api/collaboration/work/original-work/control');
assert.equal(posted.body.action,'supplement');assert.equal(posted.body.expected_version,7);
assert.deepEqual(posted.body.attachment_ids,['constraint']);
const receipt=[...store.state.pending.values()][0];
store.mergeMessages([{id:'canonical',client_message_id:'work-input:'+receipt.id,body_text:'补充约束'}]);
assert.equal(store.state.pending.size,0);assert.equal(store.draft().text,'');
assert.equal(store.draft().work,'');assert.equal(store.draft().files.length,0);
assert.equal(store.state.messages.length,1);
""")


def test_late_saved_send_does_not_restore_projection_before_permission_recheck():
    run_js(r"""
const pending=deferred();let reads=0;
const store=create((url,options={})=>{
  if(options.method==='POST')return pending.promise;
  reads++;return Promise.resolve({messages:[{id:'public'}],works:[],revision:9,visibility_revision:'current'});
});
store.reset('alice');store.state.roomId='room';store.draft().text='original';
const sending=store.send();
store.abort();store.state.messages=[];store.state.room={id:'room',revision:9};
pending.resolve({message:{id:'now-hidden',body_text:'old private projection'},work:{id:'hidden-work'}});
await sending;await new Promise(resolve=>setImmediate(resolve));
assert.equal(store.state.pending.size,0);assert.equal(reads,1);
assert.deepEqual(plain(store.state.messages.map(message=>message.id)),['public']);
assert.equal(store.state.works.length,0);
""")


def test_late_work_read_cannot_undo_same_intent_pause_or_newer_direction():
    run_js(r"""
const store=create(async()=>({}));
store.applyWork({id:'work',intent_revision:2,version:4,state:'running'});
store.applyWork({id:'work',intent_revision:2,version:5,state:'paused'});
store.applyWork({id:'work',intent_revision:2,version:4,state:'running'});
assert.equal(store.state.works[0].state,'paused');assert.equal(store.state.works[0].version,5);
store.applyWork({id:'work',intent_revision:3,version:6,state:'waiting_member'});
store.applyWork({id:'work',intent_revision:2,version:7,state:'completed'});
assert.equal(store.state.works[0].state,'waiting_member');
assert.equal(store.state.works[0].intent_revision,3);
""")


def test_context_correction_keeps_exact_targets_and_version_across_unknown_send():
    run_js(r"""
const calls=[];let uncertain=true;
const store=create(async(url,options)=>{
  calls.push({url,body:JSON.parse(options.body)});
  if(uncertain)throw Object.assign(new Error('lost response'),{code:'NETWORK_UNCERTAIN'});
  return {message:{id:'saved',client_message_id:calls.at(-1).body.client_message_id}};
});
store.reset('owner');store.state.roomId='room-a';
Object.assign(store.draft(),{text:'改成蓝色',contextCorrection:{ids:['fact-a'],version:3,visibilityRevision:'scope-a'}});
await assert.rejects(store.send());
const receipt=[...store.state.pending.values()][0];
assert.deepEqual(calls[0].body.supersedes_context_ids,['fact-a']);
assert.equal(calls[0].body.expected_context_version,3);
store.draft().contextCorrection={ids:['fact-new'],version:4,visibilityRevision:'scope-a'};
assert.equal(store.sameDraft(receipt),false);
store.abort();store.state.roomId='room-b';store.draft().text='另一房间草稿';
uncertain=false;await store.send(receipt);
assert.deepEqual(calls[0],calls[1]);
assert.equal(store.draft().text,'另一房间草稿');
assert.equal(store.draft('room-a').text,'改成蓝色');
assert.deepEqual(plain(store.draft('room-a').contextCorrection.ids),['fact-new']);
assert.equal(store.state.messages.length,0);
""")


def test_plain_message_omits_correction_authority_and_success_clears_only_sent_target():
    run_js(r"""
const bodies=[];const store=create(async(url,options)=>{
  const body=JSON.parse(options.body);bodies.push(body);
  return {message:{id:'message-'+bodies.length,client_message_id:body.client_message_id}};
});
store.reset('owner');store.state.roomId='room';store.draft().text='普通讨论';
await store.send();
assert.equal('supersedes_context_ids' in bodies[0],false);
assert.equal('expected_context_version' in bodies[0],false);
Object.assign(store.draft(),{text:'明确纠正',contextCorrection:{ids:['fact'],version:0,visibilityRevision:'visible'}});
await store.send();
assert.equal(bodies[1].expected_context_version,0);
assert.deepEqual(bodies[1].supersedes_context_ids,['fact']);
assert.equal(store.draft().contextCorrection,null);assert.equal(store.draft().text,'');
""")


def test_context_read_cannot_restore_older_fact_version_or_prior_visibility():
    run_js(r"""
const store=create(async()=>({}));store.reset('owner');store.state.roomId='room';
store.applyRoom({room:{id:'room',revision:10,context:{conversation_id:'room',version:3,visibility_revision:'scope-a',facts:[{id:'current'}]}}});
store.applyContext({conversation_id:'room',version:2,visibility_revision:'scope-a',facts:[{id:'old'}]},'scope-a');
assert.equal(store.state.room.context.facts[0].id,'current');
store.applyRoom({room:{id:'room',revision:11,context:{conversation_id:'room',version:3,visibility_revision:'scope-b',facts:[]}}});
store.applyContext({conversation_id:'room',version:3,visibility_revision:'scope-a',facts:[{id:'now-hidden'}]},'scope-a');
assert.deepEqual(plain(store.state.room.context.facts),[]);
store.draft().contextCorrection={ids:['current'],version:3};store.reset('different-owner');
assert.equal(store.state.drafts.size,0);
""")


def test_work_history_uses_its_own_visibility_domain_and_opaque_cursor():
    run_js(r"""
const calls=[];
const store=create(async(url)=>{
  calls.push(url);
  if(url.includes('/messages'))return {messages:[{id:'message'}],visibility_revision:'messages-a',
    works_visibility_revision:'works-a',works:[{id:'recent',version:2,intent_revision:1,state:'paused'}],
    works_next_cursor:'opaque/+cursor',works_pagination_complete:false,revision:4};
  return {works:[{id:'older',version:1,intent_revision:1,state:'completed'},{id:'recent',version:1,intent_revision:1,state:'running'}],
    visibility_revision:'works-a',pagination_complete:true,next_cursor:null,revision:4};
});
store.reset('owner');store.state.roomId='room';await store.messages();
await store.works(true);
assert.equal(calls[1],'/api/collaboration/rooms/room/work?limit=50&cursor=opaque%2F%2Bcursor');
assert.equal(store.state.visibilityRevision,'messages-a');assert.equal(store.state.worksVisibilityRevision,'works-a');
assert.deepEqual(plain(store.state.works.map(work=>work.id)),['recent','older']);
assert.equal(store.state.works[0].state,'paused');assert.equal(store.state.worksComplete,true);
assert.equal(store.state.worksCursor,'');assert.equal(store.state.messages[0].id,'message');
""")


def test_invalid_work_history_cursor_reloads_first_page_without_old_scope_rows():
    run_js(r"""
const calls=[];const store=create(async(url)=>{
  calls.push(url);
  if(url.includes('cursor='))throw Object.assign(new Error('scope changed'),{code:'INVALID_CURSOR',status:400});
  return {works:[{id:'visible',version:1}],visibility_revision:'new-scope',next_cursor:null,pagination_complete:true,revision:5};
});
store.reset('owner');store.state.roomId='room';
Object.assign(store.state,{works:[{id:'hidden'}],worksCursor:'old-signed-cursor',worksVisibilityRevision:'old-scope'});
await store.works(true);
assert.equal(calls.length,2);assert.equal(calls[1],'/api/collaboration/rooms/room/work?limit=50');
assert.deepEqual(plain(store.state.works.map(work=>work.id)),['visible']);
assert.equal(store.state.worksVisibilityRevision,'new-scope');
""")


def test_live_message_window_is_bounded_and_evicted_history_keeps_original_cursor():
    run_js(r"""
let latest=50;const seen=[];
const message=n=>({id:'m'+String(n).padStart(4,'0'),sequence:n,version:1,body_text:'message '+n});
const store=create(async(url)=>{
  seen.push(url);const match=url.match(/cursor=before-(\d+)/);
  const end=match?Number(match[1])-1:latest;const start=Math.max(1,end-49);
  return {messages:Array.from({length:end-start+1},(_,i)=>message(start+i)),next_cursor:start>1?'before-'+start:null,
    works:[],revision:1,visibility_revision:'messages'};
});
store.reset('owner');store.state.roomId='room';
for(latest=50;latest<=225;latest+=25){await store.messages();assert.ok(store.state.messages.length<=store.limits.messages);}
assert.equal(store.state.messages.length,50);assert.equal(store.state.cursor,'before-176');
store.draft().reading={atEnd:false,anchorId:'m0176'};
await store.messages(true);assert.ok(seen.at(-1).endsWith('cursor=before-176'));
assert.equal(store.state.messages[0].id,'m0126');
for(let n=0;n<3;n++){
  store.draft().reading={atEnd:false,anchorId:store.state.messages[0].id};
  await store.messages(true);assert.ok(store.state.messages.length<=200);
}
assert.equal(store.state.messages[0].id,'m0001');assert.equal(store.state.cursor,'');
assert.equal(store.state.messageHistory,true);assert.equal(store.state.newerMessages,true);
""")


def test_sustained_latest_reads_never_evict_current_reading_anchor_or_keep_unbounded_rows():
    run_js(r"""
let end=50;
const store=create(async()=>({messages:Array.from({length:50},(_,i)=>({id:'m'+(end-49+i),sequence:end-49+i,version:1})),
  next_cursor:end>50?'cursor-'+(end-49):null,works:[],revision:1,visibility_revision:'visible'}));
store.reset('owner');store.state.roomId='room';
for(end=50;end<=200;end+=25)await store.messages();
store.draft().reading={anchorId:'m75',atEnd:false,offset:12};store.draft().text='still editing';
for(end=250;end<=2000;end+=50){await store.messages();assert.equal(store.state.messages.length,200);}
assert.ok(store.state.messages.some(message=>message.id==='m75'));
assert.equal(store.state.messages.at(-1).id,'m200');assert.equal(store.state.newerMessages,true);
assert.equal(store.draft().text,'still editing');assert.equal(store.draft().reading.anchorId,'m75');
await store.latestMessages();
assert.equal(store.state.messages.length,50);assert.equal(store.state.messages.at(-1).id,'m2050');
assert.equal(store.state.messageHistory,false);assert.equal(store.state.newerMessages,false);
""")


def test_saved_message_anchor_uses_server_signed_history_cursor_after_arbitrary_gap():
    run_js(r"""
const calls=[];const store=create(async(url)=>{
  calls.push(url);
  if(url.includes('message_id='))return {message:{id:'original'},history_cursor:'opaque/+anchor',visibility_revision:'scope'};
  return {messages:[{id:'before',sequence:1},{id:'original',sequence:2}],next_cursor:'opaque-older',
    visibility_revision:'scope',works:[],revision:1};
});
store.reset('owner');store.state.roomId='room';store.state.messages=[{id:'far-newer',sequence:99999}];
store.draft().text='per-room draft';
await store.restoreMessage('original');
assert.equal(calls[0],'/api/collaboration/rooms/room/messages?message_id=original');
assert.equal(calls[1],'/api/collaboration/rooms/room/messages?limit=50&cursor=opaque%2F%2Banchor');
assert.deepEqual(plain(store.state.messages.map(message=>message.id)),['before','original']);
assert.equal(store.state.cursor,'opaque-older');assert.equal(store.state.messageHistory,true);
assert.equal(store.draft().text,'per-room draft');
""")


def test_work_window_bounds_history_and_keeps_original_cursor_and_separate_current_detail():
    run_js(r"""
let latest=200;const calls=[];const work=n=>({id:'w'+String(n).padStart(4,'0'),created_at:n,version:1,intent_revision:1,state:'completed'});
const store=create(async(url)=>{
  calls.push(url);
  if(url==='/api/collaboration/work/w0005')return {work:{...work(5),version:2,state:'paused'}};
  const match=url.match(/cursor=before-(\d+)/);const end=match?Number(match[1])-1:latest;
  const start=Math.max(1,end-49);
  return {works:Array.from({length:end-start+1},(_,i)=>work(end-i)),next_cursor:start>1?'before-'+start:null,
    pagination_complete:start===1,visibility_revision:'work-domain',revision:1};
});
store.reset('owner');store.state.roomId='room';await store.works();
for(let i=0;i<3;i++){
  store.draft().reading={atEnd:false,workId:store.state.works.at(-1).id};
  const anchor=store.draft().reading.workId;await store.works(true);
  assert.ok(store.state.works.length<=100);assert.ok(store.state.works.some(item=>item.id===anchor));
}
assert.equal(store.state.works.at(-1).id,'w0001');assert.equal(store.state.worksCursor,'');
assert.equal(store.state.workHistory,true);assert.equal(store.state.newerWorks,true);
latest=1000;for(let i=0;i<10;i++)await store.works();
assert.equal(store.state.works.length,100);assert.equal(store.state.works.at(-1).id,'w0001');
await store.latestWorks();assert.equal(store.state.works.length,50);assert.equal(store.state.works[0].id,'w1000');
store.state.workHistory=true;store.state.workDetail={...work(5),version:1};
await store.work('w0005');assert.equal(store.state.workDetail.version,2);
assert.equal(store.state.workDetail.state,'paused');assert.equal(store.state.works.length,50);
""")


def test_late_anchor_lookup_after_room_switch_does_not_fetch_or_restore_old_projection():
    run_js(r"""
const pending=deferred();const calls=[];const store=create((url)=>{calls.push(url);return pending.promise;});
store.reset('owner');store.state.roomId='old';const restoring=store.restoreMessage('old-source');
store.abort();store.state.roomId='new';store.state.messages=[{id:'new-room'}];
pending.resolve({message:{id:'old-source'},history_cursor:'original-signed-cursor'});await restoring;
assert.equal(calls.length,1);assert.equal(store.state.messages[0].id,'new-room');
""")


def test_first_nonempty_page_and_disjoint_refresh_keep_every_gap_reachable():
    run_js(r"""
let end=0;
const store=create(async()=>({messages:end?Array.from({length:50},(_,i)=>({id:'m'+(end-49+i),sequence:end-49+i})):[],
  works:end?Array.from({length:50},(_,i)=>({id:'w'+(end-i),created_at:end-i})):[],
  next_cursor:end?'before-'+(end-49):null,works_next_cursor:end?'work-before-'+(end-49):null,
  works_pagination_complete:!end,visibility_revision:'messages',works_visibility_revision:'works',revision:1}));
store.reset('owner');store.state.roomId='room';await store.messages();assert.equal(store.state.cursor,'');
end=550;await store.messages();assert.equal(store.state.cursor,'before-501');
assert.equal(store.state.worksCursor,'work-before-501');
store.draft().reading={atEnd:true};end=1050;await store.messages();
assert.equal(store.state.messages.length,50);assert.equal(store.state.cursor,'before-1001');
assert.equal(store.state.works.length,50);assert.equal(store.state.worksCursor,'work-before-1001');
""")


def test_saved_work_anchor_uses_original_server_cursor_without_scanning_newer_history():
    run_js(r"""
const calls=[];const store=create(async(url)=>{
  calls.push(url);
  if(url==='/api/collaboration/work/original')return {work:{id:'original'},history_cursor:'signed/+work-anchor',visibility_revision:'works'};
  return {works:[{id:'original',created_at:2,version:3},{id:'older',created_at:1,version:1}],
    next_cursor:'signed-older-work',pagination_complete:false,visibility_revision:'works',revision:1};
});
store.reset('owner');store.state.roomId='room';store.state.works=[{id:'latest',created_at:999999}];
store.draft().text='keep the room draft';store.draft().reading={workId:'original',atEnd:false,offset:8};
await store.restoreWork('original');
assert.deepEqual(plain(calls),['/api/collaboration/work/original',
  '/api/collaboration/rooms/room/work?limit=50&cursor=signed%2F%2Bwork-anchor']);
assert.deepEqual(plain(store.state.works.map(work=>work.id)),['original','older']);
assert.equal(store.state.worksCursor,'signed-older-work');assert.equal(store.state.worksVisibilityRevision,'works');
assert.equal(store.state.workHistory,true);assert.equal(store.state.newerWorks,true);
assert.equal(store.draft().text,'keep the room draft');
""")


def test_late_work_anchor_lookup_cannot_fetch_or_restore_previous_room():
    run_js(r"""
const pending=deferred();const calls=[];const store=create((url)=>{calls.push(url);return pending.promise;});
store.reset('owner');store.state.roomId='old';const restoring=store.restoreWork('old-work');
store.abort();store.state.roomId='new';store.state.works=[{id:'new-work'}];
pending.resolve({work:{id:'old-work'},history_cursor:'original-signed-cursor'});await restoring;
assert.equal(calls.length,1);assert.equal(store.state.works[0].id,'new-work');
""")


def test_work_overflow_keeps_reading_anchor_outside_actual_incoming_page():
    run_js(r"""
let end=100;const work=n=>({id:'w'+n,created_at:n,version:1,intent_revision:1});
const store=create(async()=>({works:Array.from({length:50},(_,i)=>work(end-i)),
  next_cursor:'before-'+(end-49),pagination_complete:false,visibility_revision:'works',revision:1}));
store.reset('owner');store.state.roomId='room';store.state.works=Array.from({length:100},(_,i)=>work(100-i));
store.state.worksCursor='original-older';store.draft().reading={workId:'w50',atEnd:false,offset:8};
end=125;await store.works();
assert.equal(store.state.works.length,100);assert.ok(store.state.works.some(item=>item.id==='w50'));
assert.equal(store.state.worksCursor,'original-older');assert.equal(store.state.newerWorks,true);
assert.equal(store.state.workHistory,true);
""")


def test_isolated_message_receipt_cannot_hide_gap_or_erase_original_history_cursor():
    run_js(r"""
let end=50;const message=n=>({id:'m'+n,sequence:n,version:1});
const store=create(async()=>({messages:Array.from({length:50},(_,i)=>message(end-49+i)),
  next_cursor:end>50?'before-'+(end-49):null,works:[],revision:1,visibility_revision:'messages'}));
store.reset('owner');store.state.roomId='room';await store.messages();
store.mergeMessages([message(651)]);store.draft().reading={atEnd:true};end=651;await store.messages();
assert.equal(store.state.messages.length,50);assert.equal(store.state.messages[0].id,'m602');
assert.equal(store.state.cursor,'before-602');
""")


def test_isolated_work_receipt_is_not_continuous_page_evidence():
    run_js(r"""
let end=50;const work=n=>({id:'w'+n,created_at:n,version:1,intent_revision:1});
const store=create(async()=>({works:Array.from({length:50},(_,i)=>work(end-i)),
  next_cursor:end>50?'before-'+(end-49):null,pagination_complete:end===50,visibility_revision:'works',revision:1}));
store.reset('owner');store.state.roomId='room';await store.works();
store.applyWork(work(651));store.draft().reading={atEnd:true};end=651;await store.works();
assert.equal(store.state.works.length,50);assert.equal(store.state.works.at(-1).id,'w602');
assert.equal(store.state.worksCursor,'before-602');assert.equal(store.state.worksComplete,false);
""")
