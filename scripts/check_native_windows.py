#!/usr/bin/env python3
"""Windows-only, isolated real ConPTY/Job smoke test for the CodePier worker.

Runs only a disposable Python echo child, never Pi/Codex login or model calls.
Does not touch production Agent configuration, services, or user projects.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shared.native_cli import database, process_exists
from agent.native_windows import capabilities


def wait(predicate, seconds=20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = predicate()
        if result:
            return result
        time.sleep(.1)
    raise AssertionError('Native ConPTY fixture condition timed out')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='Optional JSON evidence destination')
    args = parser.parse_args()
    if os.name != 'nt':
        raise SystemExit('This acceptance must run on Windows; no mock/platform fallback is used.')
    backend = capabilities()
    if not backend['available']:
        raise SystemExit(backend['reason'])
    result = {'scope':'real Windows ConPTY and Job with isolated Python echo child',
              'native_cli_model_test':False,'production_changed':False,'backend':backend}
    with tempfile.TemporaryDirectory(prefix='codepier-conpty-smoke-') as temporary:
        root = Path(temporary)
        directory = root / 'state'
        sid = uuid.uuid4().hex
        child = root / 'echo.py'
        child.write_text('''import os,sys,time
print('CONPTY_SMOKE_READY',flush=True)
for line in sys.stdin:
 print('CONPTY_ECHO:'+line.strip(),flush=True)
''', encoding='utf-8')
        with closing(database(directory)) as db, db:
            db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,heartbeat,argv) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (sid,'smoke','smoke',str(root),str(root),'codex','ConPTY smoke','starting',time.time(),time.time(),time.time(),json.dumps([sys.executable,'-u',str(child)])))
        def state():
            with closing(database(directory)) as db:
                return dict(db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone())
        def output():
            with closing(database(directory)) as db:
                return b''.join(r[0] for r in db.execute('SELECT data FROM output WHERE session=? ORDER BY offset',(sid,)))
        def command(kind, payload):
            receipt = uuid.uuid4().hex
            with closing(database(directory)) as db, db:
                db.execute('INSERT INTO commands(id,session,kind,payload,created) VALUES (?,?,?,?,?)',
                           (receipt,sid,kind,json.dumps(payload),time.time()))
            return receipt
        env = {**os.environ,'PYTHONIOENCODING':'utf-8','PYTHONUNBUFFERED':'1'}
        worker = subprocess.Popen([sys.executable,str(ROOT/'agent/native_worker.py'),str(directory),sid],
                                  cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                  creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        native_pid = 0
        try:
            wait(lambda:b'CONPTY_SMOKE_READY' in output())
            native_pid = state()['child_pid']
            receipt = command('input',{'text':'WIN_NATIVE_HELLO\r\n'})
            wait(lambda:b'CONPTY_ECHO:WIN_NATIVE_HELLO' in output())
            command('resize',{'rows':31,'cols':105})
            time.sleep(.3)
            stop = command('stop',{})
            worker.wait(timeout=12)
            final = state()
            result.update(started=True,input_echo=True,worker_exit=worker.returncode,
                          native_exit=final['exit_code'],native_process_gone=not process_exists(native_pid),
                          session_status=final['status'],tail_status=final['error'])
            with closing(database(directory)) as db:
                result['input_receipt'] = db.execute('SELECT state FROM commands WHERE id=?',(receipt,)).fetchone()[0]
                result['stop_receipt'] = db.execute('SELECT state FROM commands WHERE id=?',(stop,)).fetchone()[0]
            assert result['native_process_gone'] and final['status']=='exited'
            assert result['stop_receipt']=='applied'
        finally:
            if worker.poll() is None:
                command('stop',{})
                try:
                    worker.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    # Only the fixture worker is terminated. Its private
                    # kill-on-close Job owns and closes its native child tree.
                    worker.terminate();worker.wait(timeout=5)
    text = json.dumps(result,ensure_ascii=False,indent=2)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(text+'\n',encoding='utf-8')


if __name__ == '__main__':
    main()
