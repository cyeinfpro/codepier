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
