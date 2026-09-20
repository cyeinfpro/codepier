"""Chat admission never exposes arbitrary RPC, a writer lease, or unvalidated files."""
from contextlib import closing
import base64
import hashlib
import json
import uuid
from types import SimpleNamespace

import pytest
from shared.native_cli import database, public
from shared.util import DevError
from tests.test_native_cli import native


@pytest.fixture
def chat(native, monkeypatch):
    obj, project = native
    monkeypatch.setattr('agent.native_cli.subprocess.Popen', lambda *a, **k: SimpleNamespace(poll=lambda: 0))
    sid = uuid.uuid4().hex
    obj.action('start', project, {'id':sid, 'cli':'pi', 'mode':'chat'})
    try:
        yield obj, project, sid
    finally:
        with obj.connect_db() as db:
            db.execute("UPDATE sessions SET status='exited'")


def test_chat_prompt_receipt_immutable_and_no_lease(chat):
    obj,p,sid=chat
    args={'id':sid,'receipt':uuid.uuid4().hex,'text':'你好'}
    assert obj.action('chat_prompt',p,args)['state']=='queued'
    assert obj.action('chat_prompt',p,args)['receipt']==args['receipt']
    with obj.connect_db() as db:
        assert db.execute('SELECT count(*) FROM commands').fetchone()[0]==1
        db.execute("UPDATE commands SET state='completed'")
    assert obj.action('chat_prompt',p,args)['state']=='completed'
    with pytest.raises(DevError,match='同一回执'):
        obj.action('chat_prompt',p,{**args,'text':'changed'})
    with pytest.raises(DevError):
        obj.action('input',p,{'id':sid,'receipt':uuid.uuid4().hex,'text':'raw'})
    with pytest.raises(DevError):
        obj.action('lease',p,{'id':sid,'writer':uuid.uuid4().hex})
    assert obj.action('chat_interrupt',p,{'id':sid,'receipt':uuid.uuid4().hex})['state']=='queued'
    assert obj.action('stop',p,{'id':sid,'receipt':uuid.uuid4().hex})['state']=='queued'


def test_chat_attachments_verified_bound_without_lease(chat):
    obj,p,sid=chat
    raw=b'fixture image bytes';fid=uuid.uuid4().hex;sha=hashlib.sha256(raw).hexdigest()
    obj.action('upload_begin',p,{'file':fid,'name':'image.png','size':len(raw),'sha256':sha})
    prompt={'id':sid,'receipt':uuid.uuid4().hex,'text':'see image','attachments':[fid]}
    with pytest.raises(ValueError,match='incomplete'): obj.action('chat_prompt',p,prompt)
    obj.action('upload_chunk',p,{'file':fid,'offset':0,'data':base64.b64encode(raw).decode(),'sha256':sha})
    assert obj.action('upload_bind',p,{'file':fid,'id':sid})['bound']
    obj.action('chat_prompt',p,prompt)
    with obj.connect_db() as db:
        payload=json.loads(db.execute('SELECT payload FROM commands WHERE id=?',(prompt['receipt'],)).fetchone()[0])
    assert payload['attachments'][0]['mime']=='image/png'
    assert payload['attachments'][0]['path'].endswith(fid+'.png')
    with pytest.raises(DevError): obj.action('upload_delete',p,{'file':fid,'confirm':fid})
    with pytest.raises(DevError): obj.action('chat_prompt',{**p,'id':uuid.uuid4().hex},prompt)


def test_chat_native_defaults_and_protected_launch(chat):
    obj,p,sid=chat
    with obj.connect_db() as db:
        row=db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone()
        argv=json.loads(row['argv'])
    assert row['mode']=='chat'
    assert argv[1:4]==['--mode','rpc','--session']
    assert not any('approval' in a or 'dangerous' in a for a in argv)
    for options in ({'argv':['--dangerously-bypass-approvals-and-sandbox']},{'cwd':'../'},
                    {'effort':'not-a-native-level'}, {'resume':True},{'provider':'override'}):
        with pytest.raises((ValueError,DevError)):
            obj.action('start',p,{'id':uuid.uuid4().hex,'cli':'codex','mode':'chat',**options})
    with pytest.raises(ValueError): obj.action('chat_arbitrary_rpc',p,{'id':sid,'receipt':uuid.uuid4().hex})


def test_public_old_sync_rows_default_terminal(chat):
    obj,p,sid=chat
    row=next(r for r in obj.rows() if r['id']==sid)
    for key in ('mode','native_thread','chat_settings'): row.pop(key)
    assert public(row)['mode']=='terminal'


def test_resume_must_same_mapping_directory_and_stopped(chat):
    obj,p,sid=chat
    options={'id':uuid.uuid4().hex,'cli':'pi','mode':'chat','continue_session':sid}
    with pytest.raises(DevError): obj.action('start',p,options)
    with obj.connect_db() as db: db.execute("UPDATE sessions SET status='exited' WHERE id=?",(sid,))
    with pytest.raises(ValueError): obj.action('start',p,{**options,'cwd':'sub'})
    resumed=obj.action('start',p,options)
    with obj.connect_db() as db:
        args=[json.loads(r[0])[-1] for r in db.execute('SELECT argv FROM sessions')]
    assert len(set(args))==1
    assert resumed['mode']=='chat'
