"""Actual fixture process lifetime and headless terminal regression coverage."""
from contextlib import closing
import base64
import hashlib
import os
from pathlib import Path
import signal
import time
import uuid

import pytest
from agent.native_terminal import TerminalQueries
from shared.native_cli import database,worker_present,process_exists,LIVE,retained_bytes
from shared.util import DevError
from tests.test_native_cli import native,start,output
from tests.support import wait_for


def lease(obj,p,sid):
    writer=uuid.uuid4().hex
    obj.action('lease',p,{'id':sid,'writer':writer})
    return writer


def row(obj,sid):
    with closing(database(obj.directory)) as db:
        return dict(db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone())


def test_stop_is_real_process_exit_and_requires_writer(native):
    obj,p=native;sid=start(native);writer=lease(obj,p,sid)
    wait_for(lambda:'TICK:1' in output(obj,sid))
    worker=next(child for child in obj.children if child.pid==row(obj,sid)['worker_pid'])
    child_pid=row(obj,sid)['child_pid']
    assert process_exists(child_pid) and worker_present(obj.directory,sid)
    with pytest.raises(DevError,match='租约'):
        obj.action('stop',p,{'id':sid,'writer':uuid.uuid4().hex,'receipt':uuid.uuid4().hex})
    receipt=uuid.uuid4().hex;args={'id':sid,'writer':writer,'receipt':receipt}
    result=obj.action('stop',p,args)
    assert result['state']=='queued'
    with closing(database(obj.directory)) as db:
        assert db.execute('SELECT kind FROM commands WHERE id=?',(receipt,)).fetchone()[0]=='stop'
    worker.wait(timeout=8)
    assert not process_exists(child_pid)
    assert not worker_present(obj.directory,sid)
    assert row(obj,sid)['status']=='exited'
    assert row(obj,sid)['exit_code'] is not None
    before=output(obj,sid);time.sleep(.3);assert output(obj,sid)==before
    assert obj.action('stop',p,args)['state']=='applied'


def test_stale_heartbeat_with_live_ownership_stays_busy(native):
    obj,p=native;sid=start(native);wait_for(lambda:row(obj,sid)['child_pid']>0)
    with closing(database(obj.directory)) as db,db:
        db.execute('UPDATE sessions SET heartbeat=? WHERE id=?',(time.time()-600,sid))
    assert any(r['id']==sid for r in obj.live())
    assert row(obj,sid)['status']=='running'
    with pytest.raises(DevError,match='退出'):
        obj.action('delete',p,{'id':sid,'confirm':sid})


def test_partial_upload_catalog_live_binding_and_logical_quota(native):
    obj,p=native;data=b'file sent in a live session';fid=uuid.uuid4().hex
    begin={'file':fid,'name':'project notes.txt','size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
    obj.action('upload_begin',p,begin)
    listed=obj.action('upload_list',p,{})['files'];assert listed[0]['file']==fid and not listed[0]['ready']
    obj.action('upload_chunk',p,{'file':fid,'offset':0,'data':base64.b64encode(data).decode(),'sha256':hashlib.sha256(data).hexdigest()})
    assert obj.action('upload_list',p,{})['files'][0]['ready']
    sid=start(native);writer=lease(obj,p,sid)
    obj.action('upload_bind',p,{'id':sid,'writer':writer,'file':fid})
    with pytest.raises(DevError,match='使用'):
        obj.action('upload_delete',p,{'file':fid,'confirm':fid})
    with closing(database(obj.directory)) as db:
        assert retained_bytes(db)>=len(data)


def test_headless_queries_split_unicode_cursor_and_escape_safety():
    t=TerminalQueries(rows=10,cols=20)
    assert t.feed(b'abc\x1b[')==b''
    assert t.feed(b'6n')==b'\x1b[1;4R'
    for part in ['\r\n中文'.encode()[:5],'\r\n中文'.encode()[5:]]: result=t.feed(part)
    assert t.feed(b'\x1b[6n')==b'\x1b[2;5R'
    assert t.feed(b'\x1b[4;7H\x1b[6n')==b'\x1b[4;7R'
    assert t.feed(b'\x1b[?1049h\x1b[6n\x1b[?1049l\x1b[6n')==b'\x1b[1;1R\x1b[4;7R'
    assert t.feed(b'\x1b]52;c;c2VjcmV0\x07\x1b]2;title\x07')==b''
    assert t.feed(b'\x1b[?u\x1b[c\x1b[18t')==b'\x1b[?1;2c\x1b[8;10;20t'
    t.resize(4,5);assert t.feed(b'\x1b[6n')==b'\x1b[4;5R'
    assert t.feed(b'\x1b]10;?\x07').startswith(b'\x1b]10;rgb:')
    t.feed(b'\x1b]52;'+b'x'*200000+b'\x07');assert len(t.buffer)<=8192


def test_real_pty_answers_queries_without_browser(native):
    obj,p=native
    executable=Path(obj.environment()['PATH'].split(os.pathsep)[0])/'codex'
    executable.write_text('''#!/usr/bin/env python3
import os,sys,tty,select,time
if '--version' in sys.argv:print('query-fixture');sys.exit()
tty.setraw(0)
os.write(1,b'abc\\x1b[6n')
reply=b''
end=time.monotonic()+4
while b'R' not in reply and time.monotonic()<end:
 if select.select([0],[],[],.1)[0]:reply+=os.read(0,1024)
os.write(1,b'HEADLESS_REPLY:'+repr(reply).encode()+b'\\r\\n')
while True:time.sleep(.1)
''');executable.chmod(0o700)
    sid=start(native)
    wait_for(lambda:'HEADLESS_REPLY:' in output(obj,sid),6)
    assert "b'\\x1b[1;4R'" in output(obj,sid)


def test_large_input_drains_output_and_stop_interrupts_pending_write(native):
    obj,p=native
    executable=Path(obj.environment()['PATH'].split(os.pathsep)[0])/'codex'
    executable.write_text('''#!/usr/bin/env python3
import os,sys,tty,time
if '--version' in sys.argv:print('blocked-fixture');sys.exit()
tty.setraw(0)
os.write(1,b'READY-BLOCKED\\r\\n')
# Deliberately never read input; simultaneous output must keep draining.
while True:
 os.write(1,b'OUTPUT-WHILE-INPUT-BLOCKED\\r\\n');time.sleep(.04)
''');executable.chmod(0o700)
    sid=start(native);writer=lease(obj,p,sid)
    worker=obj.children[-1]
    wait_for(lambda:'READY-BLOCKED' in output(obj,sid),6)
    obj.action('input',p,{'id':sid,'writer':writer,'receipt':uuid.uuid4().hex,'text':'x'*16384})
    # Wait for actual progress, not a fixed scheduler-throughput assumption on
    # a busy host. The previous 1s-fatal worker cannot pass this sustained check.
    def advancing():
        assert row(obj,sid)['status']=='running'
        return output(obj,sid).count('OUTPUT-WHILE-INPUT-BLOCKED')>20
    wait_for(advancing,8)
    assert row(obj,sid)['status']=='running'
    obj.action('stop',p,{'id':sid,'writer':writer,'receipt':uuid.uuid4().hex})
    worker.wait(timeout=8)
    assert row(obj,sid)['status']=='exited'


def test_same_start_id_cannot_silently_change_parameters(native):
    obj,p=native;sid=uuid.uuid4().hex;args={'id':sid,'cli':'codex'}
    obj.action('start',p,args)
    assert obj.action('start',p,args)['id']==sid
    with pytest.raises(DevError,match='不同启动参数'):
        obj.action('start',p,{**args,'model':'different'})
