"""Behavior regressions for the November 2026 client audit."""
from pathlib import Path
import shutil
import subprocess

import pytest
from tree_sitter import Language, Parser
import tree_sitter_javascript

from tests.javascript_support import panel_function

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(not NODE, reason="Node is required")


def function(path, name):
    raw = (ROOT / path).read_bytes()
    tree = Parser(Language(tree_sitter_javascript.language())).parse(raw)
    candidates = [node for node in tree.root_node.named_children
                  if node.type == "function_declaration"
                  and node.child_by_field_name("name").text.decode() == name]
    assert len(candidates) == 1
    return raw[candidates[0].start_byte:candidates[0].end_byte].decode()


def run_js(source):
    result = subprocess.run([NODE, "-e", "const assert=require('node:assert/strict');\n" + source],
                            cwd=ROOT, text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_task_receipts_are_account_and_space_scoped_and_legacy_unowned_is_dropped():
    source = "\n".join(panel_function(name) for name in ("sessionValue", "sessionOwner", "taskSessionValue"))
    run_js("""
const values=new Map([['codepier-operation','unowned']]);
const sessionStorage={getItem:k=>values.get(k)||null,setItem:(k,v)=>values.set(k,v),removeItem:k=>values.delete(k)};
const S={session:{user_id:'alice'},space_id:'legacy'};
""" + source + """
assert.equal(taskSessionValue('codepier-operation'),null);
assert.equal(values.has('codepier-operation'),false);
taskSessionValue('codepier-operation','alice-operation');
const owner=sessionOwner(S.session);
S.session={user_id:'bob'};
assert.equal(taskSessionValue('codepier-operation'),null);
taskSessionValue('codepier-operation','bob-operation');
S.session={user_id:'alice'};S.space_id='team';
assert.equal(taskSessionValue('codepier-operation'),null);
S.space_id='legacy';
assert.equal(taskSessionValue('codepier-operation'),'alice-operation');
S.session=null;
taskSessionValue('codepier-operation',null,owner);
S.session={user_id:'bob'};
assert.equal(taskSessionValue('codepier-operation'),'bob-operation');
""")


def test_stop_receipt_retries_same_session_but_changes_after_resume():
    run_js("""
let n=0;const calls=[];const view={};
const ChatUI={selected:{id:'old'},generation:1,project:'project'};
const chatView=()=>view,uid=()=>String(++n),chatDialog=async()=>true,
chatCurrent=()=>true,chatStatus=()=>{};
let lost=true;
const chatAPI=async(action,args)=>{calls.push(args);if(lost){lost=false;throw new Error('lost response');}};
""" + function("web/chat.js", "chatStop") + """
(async()=>{
await assert.rejects(chatStop());await chatStop();
assert.equal(calls[0].receipt,calls[1].receipt);
ChatUI.selected={id:'resumed'};ChatUI.generation++;
await chatStop();assert.notEqual(calls[1].receipt,calls[2].receipt);
assert.equal(calls[2].id,'resumed');
})().catch(e=>{console.error(e);process.exit(1)});
""")


def test_image_budget_matches_native_suffixes_and_blocks_before_upload_or_start():
    run_js("""
const CHAT_ATTACHMENT_MAX_BYTES=20*1024*1024,CHAT_ATTACHMENT_MAX_COUNT=10,CHAT_MESSAGE_MAX_BYTES=65536;
const errors=[],view={draft:'hello',files:[{name:'one.PNG',source:{size:11*1024*1024},ready:true,file:'one'},
{name:'two.jpg',source:{size:11*1024*1024},ready:true,file:'two'}]};
const S={session:{}},ChatUI={project:'project',selected:{id:'session',status:'running'}};
const chatView=()=>view,chatStatus=(text)=>errors.push(text),chatViewVisible=()=>true,
chatLocalCommand=()=>false,chatProjectInfo=()=>({}),chatDraftBytes=()=>5,chatBuiltin=()=>false;
""" + function("web/chat.js", "chatImageBytes") + "\n" + function("web/chat.js", "chatUpload")
           + "\n" + function("web/chat.js", "chatSend") + """
(async()=>{
assert.equal(chatImageBytes([{name:'big.pdf',size:40*1024*1024}]),0);
assert.equal(chatImageBytes([{name:'raw.JPEG',size:10}]),10);
await chatSend();assert.equal(view.pending,undefined);assert.equal(errors.length,1);
view.files=[];
await chatUpload([{name:'a.png',size:11*1024*1024},{name:'b.gif',size:11*1024*1024}]);
assert.equal(view.files.length,0);assert.equal(errors.length,2);
})().catch(e=>{console.error(e);process.exit(1)});
""")


def test_production_javascript_has_no_syntax_errors():
    for name in ["app.js", "identity.js", "chat.js", "collaboration-room-store.js"]:
        raw = (ROOT / "web" / name).read_bytes()
        assert not Parser(Language(tree_sitter_javascript.language())).parse(raw).root_node.has_error


def test_sse_resync_refreshes_snapshots_without_crossing_session_boundaries():
    run_js("""
const listeners={},calls=[];
const S={session:{user_id:'one'},work:{operation:'task'},page:'overview'};
const EventSource=class {addEventListener(type,fn){listeners[type]=fn;}};
const stopEvents=()=>{},$=()=>null,networkState=()=>{},toast=()=>{};
const invalidateBasics=()=>calls.push('invalidate'),pollTask=async()=>calls.push('poll'),
refreshComputerApprovals=()=>calls.push('approvals'),loadBasics=async()=>calls.push('basics'),
renderPage=async()=>calls.push('page'),CodePierIdentity={refresh:async()=>calls.push('identity')};
const window={};
""" + panel_function('connectEvents') + """
(async()=>{
connectEvents();listeners.resync();await new Promise(resolve=>setImmediate(resolve));
assert.deepEqual(calls,['invalidate','poll','approvals','identity','basics','page']);
const length=calls.length;S.session={user_id:'two'};listeners.resync();
await new Promise(resolve=>setImmediate(resolve));assert.equal(calls.length,length);
})().catch(e=>{console.error(e);process.exit(1)});
""")
