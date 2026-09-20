"""Regression cases discovered by independently reviewing the chat refactor."""
import base64
import hashlib
import json
import uuid
import pytest
from agent.chat_worker import Protocol, image_inputs
from tests.test_chat_admission import chat
from tests.test_native_cli import native


def test_transport_coalesces_a_thousand_tiny_durable_events_without_loss(chat):
    obj,project,sid=chat
    frames=[(json.dumps({'type':'delta','text':str(i)})+'\n').encode() for i in range(1000)]
    offset=0
    with obj.connect_db() as db:
        for raw in frames:
            db.execute('INSERT INTO output VALUES (?,?,?)',(sid,offset,raw));offset+=len(raw)
        db.execute('UPDATE sessions SET size=? WHERE id=?',(offset,sid))
    first=obj.snapshot()
    chunks=[x for x in first['chunks'] if x['id']==sid]
    assert len(chunks)<=2
    assert b''.join(base64.b64decode(x['data']) for x in chunks)==b''.join(frames)
    assert all(len(base64.b64decode(x['data']))<=65536 for x in chunks)
    assert obj.snapshot()['chunks']==first['chunks'], 'Unacknowledged data must replay identically'
    obj.offsets[sid]=offset
    assert not obj.snapshot()['chunks']


def test_snapshot_budget_preserves_unacknowledged_records(chat):
    obj,project,sid=chat
    frames=[bytes([i])*40000 for i in range(6)]
    with obj.connect_db() as db:
        for i,raw in enumerate(frames): db.execute('INSERT INTO output VALUES (?,?,?)',(sid,i*40000,raw))
    received=b''
    for _ in range(3):
        packet=obj.snapshot()
        for chunk in packet['chunks']:
            assert chunk['offset']==len(received)
            received+=base64.b64decode(chunk['data'])
        obj.offsets[sid]=len(received)
    assert received==b''.join(frames)


def test_native_final_messages_and_pi_tool_segments_keep_their_identity():
    events=[]
    p=Protocol('pi',{},lambda x:None,lambda k,**v:events.append((k,v)),lambda *v:None,lambda x:None)
    p.ready=True;p.prompt('receipt',{'text':'test'})
    for text in ('Before tool','After tool'):
        p.receive({'type':'message_start','message':{'role':'assistant'}})
        p.receive({'type':'message_update','assistantMessageEvent':{'type':'text_delta','delta':text}})
        p.receive({'type':'message_end','message':{'role':'assistant','content':[{'type':'text','text':text}]}})
    messages=[v for k,v in events if k=='message']
    assert [m['text'] for m in messages]==['Before tool','After tool']
    assert len({m['item_id'] for m in messages})==2
    events.clear()
    p=Protocol('codex',{},lambda x:None,lambda k,**v:events.append((k,v)),lambda *v:None,lambda x:None)
    p.receive({'method':'item/completed','params':{'item':{'id':'one','type':'agentMessage','text':'Completed-only reply'}}})
    assert events==[('message',{'text':'Completed-only reply','item_id':'one'})]


def test_settings_are_not_falsely_reported_as_applied_before_native_turn_confirmation():
    sent=[];events=[]
    p=Protocol('codex',{},sent.append,lambda k,**v:events.append((k,v)),lambda *v:None,lambda x:None)
    p.ready=True;p.thread='thread';p.settings={'model':'existing','models':[{'id':'new','model':'new'}]}
    p.change_settings('setting',{'model':'new','effort':'low'})
    assert not sent and events[-1][0]=='settings_pending'
    assert p.settings['model']=='existing'
    p.prompt('message',{'text':'test'})
    assert sent[-1]['method']=='turn/start'
    assert sent[-1]['params']['model']=='new' and sent[-1]['params']['effort']=='low'
    p.receive({'id':sent[-1]['id'],'result':{'turn':{'id':'turn'}}})
    assert p.settings['model']=='new'
    assert events[-1]==('settings_pending',{'cleared':True})


def test_upload_identity_is_verified_again_before_native_read(tmp_path):
    p=tmp_path/'image.png';p.write_bytes(b'original')
    attachment={'path':str(p),'mime':'image/png','size':8,'sha256':hashlib.sha256(b'original').hexdigest()}
    p.write_bytes(b'modified')
    with pytest.raises(ValueError,match='checksum'): image_inputs([attachment],'pi')


def test_pending_approval_is_visibly_closed_when_native_turn_settles():
    events=[]
    p=Protocol('pi',{},lambda x:None,lambda k,**v:events.append((k,v)),lambda *v:None,lambda x:None)
    p.ready=True;p.prompt('r',{'text':'test'})
    p.receive({'type':'extension_ui_request','id':'approval','method':'confirm','title':'Question'})
    p.receive({'type':'agent_settled'})
    assert any(k=='approval' and v.get('resolved') and v['request_id']=='approval' for k,v in events)
    with pytest.raises(ValueError):p.answer('approval',True)


def test_live_chat_never_disappears_behind_hundreds_of_historical_sessions(chat):
    obj,project,sid=chat
    with obj.connect_db() as db:
        row=db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
        for i in range(800):
            db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,mode) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
              (uuid.uuid4().hex,project['id'],project['device_id'],row['root'],row['cwd'],'pi','Old chat','exited',i,1,'chat'))
    for _ in range(105):
        packet=obj.snapshot()
        assert sid in {r['id'] for r in packet['sessions']}
        assert len(packet['sessions'])<=250
