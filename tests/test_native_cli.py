"""Real PTY tests use isolated HOME/config and a fake interactive program, never paid prompts."""
import base64
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid
from types import SimpleNamespace

import pytest
from agent.native_cli import NativeCLI
from agent.filesystem import FileEngine
from agent.journal import Journal
from agent.shell import validate_shell
from shared.native_cli import database,launch_argv
from shared.util import DevError
from tests.support import wait_for

FAKE = '''#!/usr/bin/env python3
import os,sys,time,signal,threading,termios,tty,json
if '--version' in sys.argv: print('fixture-cli 1.0');sys.exit(0)
print('READY:'+os.getcwd(),flush=True)
print('ARGV:'+json.dumps(sys.argv[1:]),flush=True)
signal.signal(signal.SIGINT,lambda *a: print('INTERRUPTED',flush=True))
signal.signal(signal.SIGWINCH,lambda *a: print('SIZE:'+str(os.get_terminal_size()),flush=True))
def tick():
 for i in range(1000):
  print('TICK:'+str(i)+' 世界',flush=True);time.sleep(.15)
threading.Thread(target=tick,daemon=True).start()
while True:
 line=sys.stdin.readline()
 if not line:break
 print('ECHO:'+line.rstrip().replace(chr(27)+'[200~','').replace(chr(27)+'[201~',''),flush=True)
'''


@pytest.fixture
def native(tmp_path,monkeypatch):
    home=tmp_path/'home';home.mkdir();monkeypatch.setenv('HOME',str(home))
    bindir=tmp_path/'bin';bindir.mkdir()
    for cli in ('codex','pi'):
        file=bindir/cli;file.write_text(FAKE);file.chmod(0o700)
    root=tmp_path/'project';root.mkdir();(root/'sub').mkdir()
    state=tmp_path/'state';config=tmp_path/'config'/'test.json';config.parent.mkdir()
    cfg={'allowed_roots':[{'path':str(root),'writable':True,'allow_tasks':True}],
         'shell':validate_shell({'enabled':True,'projects':['*'],'inherit_env':False,'env':{'PATH':str(bindir)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.defpath,'HOME':str(home)}})}
    journal=Journal(state)
    agent=SimpleNamespace(state_dir=state,config=cfg,engine=FileEngine(cfg,journal,config))
    obj=NativeCLI(agent);project={'id':uuid.uuid4().hex,'device_id':uuid.uuid4().hex,'root':str(root),'alias':'fixture','mode':'write','allow_tasks':True}
    yield obj,project
    for row in obj.live():
        writer=uuid.uuid4().hex
        with closing(database(obj.directory)) as db,db: db.execute("UPDATE sessions SET lease='',lease_until=0 WHERE id=?",(row['id'],))
        obj.action('lease',project,{'id':row['id'],'writer':writer})
        obj.action('stop',project,{'id':row['id'],'writer':writer,'receipt':uuid.uuid4().hex})
    wait_for(lambda:not obj.live(),8)
    for child in obj.children:
        if child.poll() is None:
            child.terminate()
            try: child.wait(timeout=5)
            except Exception: child.kill()
    journal.db.close()


def start(native,**kwargs):
    obj,p=native;sid=uuid.uuid4().hex
    row=obj.action('start',p,{'id':sid,'cli':'codex',**kwargs})
    wait_for(lambda:next((x for x in obj.rows() if x['id']==sid and x['status']=='running'),None))
    return sid


def output(obj,sid):
    with closing(database(obj.directory)) as db:
        return b''.join(r[0] for r in db.execute('SELECT data FROM output WHERE session=? ORDER BY offset',(sid,))).decode('utf8')


def test_real_pty_detach_receipts_resize_interrupt_stop(native):
    obj,p=native;sid=start(native,cwd='sub',model='model with space;$(touch NO)',effort='high',argv=['--extension','with spaces'])
    wait_for(lambda:'TICK:2' in output(obj,sid))
    assert 'READY:'+p['root']+'/sub' in output(obj,sid)
    assert 'model with space;$(touch NO)' in output(obj,sid)
    before=len(output(obj,sid));writer=uuid.uuid4().hex
    obj.action('lease',p,{'id':sid,'writer':writer});obj.action('detach',p,{'id':sid,'writer':writer})
    wait_for(lambda:len(output(obj,sid))>before+30)
    obj.action('lease',p,{'id':sid,'writer':writer})
    args={'id':sid,'writer':writer,'receipt':uuid.uuid4().hex,'text':'hello\n'}
    obj.action('input',p,args);obj.action('input',p,args)
    wait_for(lambda:'ECHO:hello' in output(obj,sid));assert output(obj,sid).count('ECHO:hello')==1
    obj.action('resize',p,{'id':sid,'writer':writer,'receipt':uuid.uuid4().hex,'cols':91,'rows':29})
    wait_for(lambda:'columns=91, lines=29' in output(obj,sid))
    obj.action('input',p,{'id':sid,'writer':writer,'receipt':uuid.uuid4().hex,'text':'\x03'})
    wait_for(lambda:'INTERRUPTED' in output(obj,sid));assert obj.live()
    obj.action('stop',p,{'id':sid,'writer':writer,'receipt':uuid.uuid4().hex})
    wait_for(lambda:not obj.live())


def test_mapping_optin_and_exclusive_writer(native):
    obj,p=native
    obj.agent.config['shell']['enabled']=False
    with pytest.raises(DevError,match='shell.enabled'):start(native)
    obj.agent.config['shell']['enabled']=True
    with pytest.raises(DevError):start(native,cwd='../')
    with pytest.raises(DevError):start(native,cwd='missing')
    sid=start(native);writer=uuid.uuid4().hex
    obj.action('lease',p,{'id':sid,'writer':writer})
    with pytest.raises(DevError,match='标签页'):obj.action('lease',p,{'id':sid,'writer':uuid.uuid4().hex})
    with pytest.raises(DevError):obj.action('input',p,{'id':sid,'writer':uuid.uuid4().hex,'receipt':uuid.uuid4().hex,'text':'BAD'})
    with pytest.raises(DevError):obj.action('clear',p,{'id':sid,'confirm':sid})
    with pytest.raises(DevError):obj.action('rename',{**p,'id':uuid.uuid4().hex},{'id':sid,'title':'wrong'})
    obj.agent.config['allowed_roots'][0]['allow_tasks']=False
    with pytest.raises(DevError):obj.action('lease',p,{'id':sid,'writer':writer})
    obj.agent.config['allowed_roots'][0]['allow_tasks']=True


def test_upload_integrity_retry_traversal_and_owned_deletion(native):
    obj,p=native;raw=b'fake image\0\xff';fid=uuid.uuid4().hex
    args={'file':fid,'name':'../../outside image.png','size':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}
    obj.action('upload_begin',p,args);obj.action('upload_begin',p,args)
    chunk={'file':fid,'offset':0,'data':base64.b64encode(raw).decode(),'sha256':hashlib.sha256(raw).hexdigest()}
    with pytest.raises(ValueError):obj.action('upload_chunk',p,{**chunk,'sha256':'0'*64})
    reply=obj.action('upload_chunk',p,chunk);assert reply['ready']
    assert obj.action('upload_chunk',p,chunk)['received']==len(raw)
    assert Path(reply['path']).parent==obj.directory/'uploads'
    assert Path(reply['path']).name==fid+'.png'
    with pytest.raises(ValueError):obj.action('upload_begin',p,{**args,'file':'../escape'})
    path=Path(reply['path']);path.unlink();path.symlink_to(Path(p['root'])/'README.md')
    with pytest.raises(ValueError):obj.action('upload_finish',p,{'file':fid})
    path.unlink();path.write_bytes(raw)
    sid=start(native,attachments=[fid]);assert '--image' in output(obj,sid) or wait_for(lambda:'--image' in output(obj,sid))
    with pytest.raises(DevError):obj.action('upload_delete',p,{'file':fid,'confirm':fid})
    writer=uuid.uuid4().hex;obj.action('lease',p,{'id':sid,'writer':writer});obj.action('stop',p,{'id':sid,'writer':writer,'receipt':uuid.uuid4().hex});wait_for(lambda:not obj.live())
    marker=Path(p['root'])/'native-history';marker.write_text('preserve')
    obj.action('delete',p,{'id':sid,'confirm':sid});assert output(obj,sid)=='';assert marker.read_text()=='preserve'
    obj.action('upload_delete',p,{'file':fid,'confirm':fid});assert not path.exists()


def test_native_argv_preserves_literals_and_native_flags():
    args=launch_argv('/bin/codex','codex',{'resume':True,'model':'a " b','provider':'native-provider','effort':'high','argv':['--profile','a b']},['/tmp/a b.png'])
    assert args[1]=='resume' and args[-2:]==['--image','/tmp/a b.png']
    assert 'model_reasoning_effort="high"' in args
    assert launch_argv('/bin/pi','pi',{'effort':'max'},['/tmp/a b.png'])[-1]=='@/tmp/a b.png'
    with pytest.raises(ValueError):launch_argv('codex','codex',{'argv':'--shell hacked'})
    with pytest.raises(ValueError):launch_argv('codex','codex',{'argv':['--cd=/tmp']})
