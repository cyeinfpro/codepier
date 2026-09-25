"""Cross-layer edge cases found during the complete workspace review."""
import asyncio
import base64
import hashlib
import json
import uuid
from types import SimpleNamespace
import pytest
from playwright.sync_api import expect
from tests.browser_support import chat_page,event
from tests.test_chat_complete_browser import send
from tests.test_chat_complete_backend import protocol,reply
from tests.test_chat_admission import chat
from tests.test_native_cli import native
from tests.test_chat_sse import service
from shared.util import DevError


def test_ambiguous_setting_retry_keeps_original_receipt(chat_page):
    p=chat_page;send(p)
    p.evaluate('''() => {const original=api;window.failSetting=true;window.api=async(path,opts)=>{
      if(path.endsWith('/chat_settings')&&failSetting){failSetting=false;requests.push({path,...JSON.parse(opts.body)});throw Object.assign(new Error('Reply lost'),{code:'NETWORK_UNCERTAIN'});}
      return original(path,opts);
    }}''')
    p.select_option('#chat-effort-select','high')
    expect(p.locator('#chat-setting-state')).to_have_text('设置待核实')
    first=p.evaluate("requests.filter(x=>x.path.endsWith('/chat_settings')).at(-1).args.receipt")
    p.click('#chat-settings-retry')
    expect(p.locator('#chat-settings-retry')).to_be_hidden(timeout=4000)
    retried=p.evaluate("requests.filter(x=>x.path.endsWith('/chat_settings')).at(-1).args.receipt")
    assert first==retried


def test_known_prompt_rejection_preserves_editable_draft_not_a_permanent_retry_loop(chat_page):
    p=chat_page
    p.evaluate('''() => {const original=api;window.rejectOnce=true;window.api=async(path,opts)=>{
      if(path.endsWith('/chat_prompt')&&rejectOnce){rejectOnce=false;requests.push({path,...JSON.parse(opts.body)});throw Object.assign(new Error('Fixture validation rejected'),{code:'CLI_INVALID'});}
      return original(path,opts);
    }}''')
    p.fill('#chat-compose','Keep this draft')
    p.press('#chat-compose','Enter')
    expect(p.locator('#chat-status')).to_contain_text('validation rejected')
    expect(p.locator('#chat-compose')).to_have_value('Keep this draft')
    assert p.evaluate('chatView().pending') is None
    send(p,'Revised draft')
    assert p.evaluate("requests.filter(x=>x.path.endsWith('/start')).length")==1
    prompts=p.evaluate("requests.filter(x=>x.path.endsWith('/chat_prompt')).map(x=>x.args)")
    assert len(prompts)==2 and prompts[0]['receipt']!=prompts[1]['receipt']


def test_logout_cannot_repopulate_private_views_from_late_send(chat_page):
    p=chat_page;p.evaluate('window.delaySend=true')
    p.fill('#chat-compose','Private draft')
    p.press('#chat-compose','Enter');p.wait_for_function('!!window.finishSend')
    p.evaluate('chatDetach(true);S.session=null;S.page="login";finishSend()')
    p.wait_for_timeout(100)
    assert p.evaluate('ChatUI.views.size')==0
    assert p.evaluate('ChatUI.selected') is None


@pytest.mark.asyncio
async def test_base_chat_agent_gets_explicit_complete_feature_upgrade_notice(service):
    obj,project,sid=service
    obj.runtime.connections['device']=SimpleNamespace(native_protocol=1,native_chat_protocol=1)
    for action,args in [('chat_catalog',{'cli':'pi'}),('chat_queue',{'id':sid}),('chat_cancel',{}),('chat_steer',{}),('start',{'mode':'chat','model':'p/model'})]:
        with pytest.raises(DevError,match='基础聊天版'):
            await obj.request(action,project,args)
    assert not obj.pending


def test_handled_pi_extension_command_finishes_only_after_native_idle_confirmation():
    p,wire,events,finished=protocol();p.ready=True
    p.prompt('command',{'text':'/fixture-command'})
    reply(p,wire[-1])
    assert wire[-1]['type']=='get_state' and not finished
    reply(p,wire[-1],{'isStreaming':False,'isCompacting':False,'pendingMessageCount':0})
    assert finished==[('command','completed')] and p.active is None


def test_streaming_pi_skill_still_waits_for_agent_settled():
    p,wire,events,finished=protocol();p.ready=True
    p.prompt('skill',{'text':'/skill:review'})
    reply(p,wire[-1]);reply(p,wire[-1],{'isStreaming':True})
    assert p.active=='skill' and not finished
    p.receive({'type':'agent_settled'})
    assert finished==[('skill','completed')]


def test_continuation_protects_parent_attachment_and_allows_explicit_new_settings(chat):
    obj,project,sid=chat
    raw=b'fixture';fid=uuid.uuid4().hex;digest=hashlib.sha256(raw).hexdigest()
    obj.action('upload_begin',project,{'file':fid,'name':'notes.txt','size':len(raw),'sha256':digest})
    obj.action('upload_chunk',project,{'file':fid,'offset':0,'data':base64.b64encode(raw).decode(),'sha256':digest})
    obj.action('upload_bind',project,{'file':fid,'id':sid})
    with obj.connect_db() as db:
        db.execute("UPDATE sessions SET status='exited',chat_settings=? WHERE id=?",(json.dumps({'next':{'model':'p/old','effort':'low'},'defaults':{'model':'p/default','effort':'medium'}}),sid))
    nextid=uuid.uuid4().hex
    obj.action('start',project,{'id':nextid,'cli':'pi','mode':'chat','continue_session':sid,'model':'p/new','effort':'high'})
    with obj.connect_db() as db:
        settings=json.loads(db.execute('SELECT chat_settings FROM sessions WHERE id=?',(nextid,)).fetchone()[0])
        assert settings['next']=={'model':'p/new','effort':'high'}
        assert db.execute('SELECT 1 FROM session_files WHERE session=? AND file=?',(nextid,fid)).fetchone()
    with pytest.raises(DevError):obj.action('upload_delete',project,{'file':fid,'confirm':fid})
