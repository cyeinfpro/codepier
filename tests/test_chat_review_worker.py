"""Real standalone native pipe workers; upstream is deterministic, never a model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import pytest
from shared.native_cli import database
from tests.test_chat_worker import FAKE, wait


@pytest.mark.parametrize('provider',['pi','codex'])
def test_independent_worker_captures_turn_and_retains_review_after_exit(tmp_path,provider):
    root=tmp_path/'project';root.mkdir()
    state=tmp_path/'state';state.mkdir()
    spool=state/'native-cli';cfg=tmp_path/'private'/'config.json';cfg.parent.mkdir()
    cfg.write_text(json.dumps({'allowed_roots':[{'path':str(root),'writable':True}]}))
    script=tmp_path/'fake.py'
    script.write_text(FAKE.replace('def done():', 'def done():\n    with open("changed-by-native.txt","w") as source: source.write("native result\\n")'))
    sid=uuid.uuid4().hex;db=database(spool);now=time.time()
    db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,argv,mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
               (sid,'p','d',str(root),str(root),provider,'fixture','starting',now,now,json.dumps([sys.executable,str(script),provider]),'chat'));db.commit()
    process=subprocess.Popen([sys.executable,'-m','agent.chat_worker',str(spool),sid,str(cfg)],cwd=Path(__file__).resolve().parents[1],stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    def events():
        raw=b''.join(bytes(r[0]) for r in db.execute('SELECT data FROM output WHERE session=? ORDER BY offset',(sid,)))
        return [json.loads(x) for x in raw.splitlines()]
    def command(kind,payload):
        receipt=uuid.uuid4().hex
        db.execute('INSERT INTO commands(id,session,kind,payload,created) VALUES (?,?,?,?,?)',(receipt,sid,kind,json.dumps(payload),time.time()));db.commit();return receipt
    try:
        wait(lambda:any(e['type']=='settings' for e in events()))
        receipt=command('chat_prompt',{'text':'create source file'})
        wait(lambda:any(e['type']=='review' and e['receipt']==receipt for e in events()),seconds=15)
        review=next(e for e in events() if e['type']=='review')
        assert review['available'] and review['summary']['files']>=1
        assert (root/'changed-by-native.txt').read_text()=='native result\n'
        stored=json.loads((state/'coding-reviews'/(review['review_ref']+'.json')).read_text())['payload']
        assert any(f['path']=='changed-by-native.txt' for f in stored['files'])
        assert stored['binding']['owner']=='native:'+sid
        command('stop',{})
        stdout,stderr=process.communicate(timeout=10)
        assert process.returncode==0,(stdout,stderr)
        assert (state/'coding-reviews'/(review['review_ref']+'.json')).exists()
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.communicate(timeout=8)
            except subprocess.TimeoutExpired:process.kill();process.communicate()
        db.close()
