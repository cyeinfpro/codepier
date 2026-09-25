"""ConPTY controller contract tests with explicit fake native handles.

These exercise the real worker controller and SQLite journal on this host. They
are not Windows kernel/ConPTY runtime certification; the Windows smoke script is
separate and requires Windows plus the pinned dependency.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import ctypes
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import uuid

import pytest
from agent.native_windows import run, resolve_argv, create_pty
from shared.native_cli import database
from tests.support import wait_for


def test_real_agent_survives_legacy_redirected_log_encoding(tmp_path, monkeypatch):
    from tests.support import running_stack
    monkeypatch.setenv('PYTHONIOENCODING', 'cp1252:strict')
    with running_stack(tmp_path / 'legacy-logs') as stack:
        reply = stack.mcp('read', {'project': 'Imago', 'path': 'README.md'})
        assert not reply.get('isError'), reply
        data = reply['structuredContent']
        if data.get('pending'):
            operation = stack.poll(data['operation_id'], timeout=30)
            assert operation['state'] == 'succeeded', operation
            data = operation['result']['data']
        assert 'Integration fixture' in data['content']
        log = (stack.directory / 'agent.log').read_text(encoding='cp1252')
        assert 'UnicodeEncodeError' not in log
        assert '\\u5df2\\u8fde\\u63a5' in log


class FakePTY:
    def __init__(self, *, stall=False, no_eof=False):
        self.pid = 23456
        self.output = queue.Queue()
        self.output.put('CONPTY_READY\r\n世界😀\r\n')
        self.writes = []
        self.sizes = []
        self.live = True
        self.release = threading.Event()
        self.stall = stall
        self.no_eof = no_eof
        self.cancelled = False
        self.closed = False

    def read(self, blocking=False):
        try: return self.output.get_nowait()
        except queue.Empty: return ''

    def iseof(self):
        return not self.no_eof and not self.live and self.output.empty()

    def write(self, text):
        self.writes.append(text)
        if self.stall and text.startswith('BLOCK'):
            assert self.release.wait(8)
        self.output.put('ECHO:' + text + '\r\n')
        return len(text.encode('utf-8'))

    def set_size(self, cols, rows):
        self.sizes.append((cols, rows))

    def cancel_io(self):
        self.cancelled = True
        self.release.set()


class FakeGuard:
    def __init__(self, pty):
        self.pty = pty
        self.terminations = 0
        self.closed = False

    def alive(self): return self.pty.live
    def exit_code(self): return None if self.pty.live else 1
    def terminate(self):
        self.terminations += 1
        self.pty.live = False
        self.pty.release.set()
    def close(self): self.closed = True


def seeded(tmp_path):
    directory = tmp_path / 'spool'
    sid = uuid.uuid4().hex
    with closing(database(directory)) as db, db:
        db.execute('INSERT INTO sessions(id,project_id,device_id,root,cwd,provider,title,status,created,updated,heartbeat,argv) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                   (sid,'project','device',str(tmp_path),str(tmp_path),'codex','fixture','starting',time.time(),time.time(),time.time(),json.dumps(['fixture.exe','arg with space'])))
    return directory, sid


def state(directory, sid):
    with closing(database(directory)) as db:
        return dict(db.execute('SELECT * FROM sessions WHERE id=?',(sid,)).fetchone())


def command(directory, sid, kind, payload):
    receipt = uuid.uuid4().hex
    with closing(database(directory)) as db, db:
        db.execute('INSERT INTO commands(id,session,kind,payload,created) VALUES (?,?,?,?,?)',
                   (receipt,sid,kind,json.dumps(payload),time.time()))
    return receipt


def output(directory, sid):
    with closing(database(directory)) as db:
        return b''.join(r[0] for r in db.execute('SELECT data FROM output WHERE session=? ORDER BY offset',(sid,)))


def test_conpty_controller_persists_unicode_resizes_and_confirms_exit(tmp_path):
    directory,sid=seeded(tmp_path);pty=FakePTY();guard=FakeGuard(pty)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(run,directory,sid,pty_factory=lambda *a:pty,guard_factory=lambda pid:guard)
        wait_for(lambda:state(directory,sid)['status']=='running')
        wait_for(lambda:'世界😀'.encode() in output(directory,sid))
        receipt=command(directory,sid,'input',{'text':'你好🙂'})
        command(directory,sid,'resize',{'rows':27,'cols':91})
        wait_for(lambda:(91,27) in pty.sizes)
        wait_for(lambda:'ECHO:你好🙂'.encode() in output(directory,sid))
        stop=command(directory,sid,'stop',{})
        future.result(timeout=8)
    final=state(directory,sid)
    assert final['status']=='exited' and final['exit_code']==1
    assert guard.terminations==1 and guard.closed and pty.cancelled
    with closing(database(directory)) as db:
        for item in (receipt,stop):
            assert db.execute('SELECT state FROM commands WHERE id=?',(item,)).fetchone()[0]=='applied'


def test_conpty_blocked_input_does_not_block_output_or_stop(tmp_path):
    directory,sid=seeded(tmp_path);pty=FakePTY(stall=True);guard=FakeGuard(pty)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(run,directory,sid,pty_factory=lambda *a:pty,guard_factory=lambda pid:guard)
        wait_for(lambda:state(directory,sid)['status']=='running')
        command(directory,sid,'input',{'text':'BLOCK'+'x'*12000})
        wait_for(lambda:bool(pty.writes))
        pty.output.put('OUTPUT_DURING_BLOCK\r\n')
        wait_for(lambda:b'OUTPUT_DURING_BLOCK' in output(directory,sid))
        command(directory,sid,'stop',{})
        future.result(timeout=8)
    assert guard.closed and not pty.live
    assert state(directory,sid)['status']=='exited'


def test_conpty_native_eof_gap_is_explicit_not_false_complete(tmp_path):
    directory,sid=seeded(tmp_path);pty=FakePTY(no_eof=True);guard=FakeGuard(pty)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(run,directory,sid,pty_factory=lambda *a:pty,guard_factory=lambda pid:guard)
        wait_for(lambda:b'CONPTY_READY' in output(directory,sid))
        pty.live=False
        future.result(timeout=8)
    assert state(directory,sid)['status']=='exited'
    assert 'TAIL_UNCONFIRMED' in state(directory,sid)['error']


def test_npm_shim_resolves_to_node_without_shell_interpolation(tmp_path):
    bin=tmp_path/'npm directory';bin.mkdir();(bin/'node.exe').write_bytes(b'fixture')
    shim=bin/'pi.cmd';shim.write_text('not executed')
    package=bin/'node_modules/@earendil-works/pi-coding-agent';package.mkdir(parents=True)
    entry=package/'cli.js';entry.write_text('// fixture')
    (package/'package.json').write_text(json.dumps({'bin':{'pi':'cli.js'}}))
    args=resolve_argv([str(shim),'--model','a & b;$(text)','@C:\\a b.png'],{'PATH':''})
    assert args==[str(bin/'node.exe'),str(entry),'--model','a & b;$(text)','@C:\\a b.png']
    assert 'cmd.exe' not in str(args)
    (package/'package.json').write_text(json.dumps({'bin':{'pi':'../outside.js'}}))
    with pytest.raises(RuntimeError,match='outside'):
        resolve_argv([str(shim)],{'PATH':''})


def test_raw_conpty_spawn_preserves_argv_cwd_and_environment(monkeypatch):
    captured={}
    class Raw:
        def __init__(self,cols,rows,backend):captured.update(cols=cols,rows=rows,backend=backend)
        def spawn(self,exe,**kwargs):captured.update(exe=exe,**kwargs)
    backend=object()
    monkeypatch.setitem(sys.modules,'winpty',SimpleNamespace(PTY=Raw,Backend=SimpleNamespace(ConPTY=backend)))
    args=['codex.exe','--model','quoted value','x&y']
    create_pty(args,'C:\\project with spaces',{'PATH':'C:\\bin','FIXTURE':'世界'})
    assert captured['backend'] is backend
    assert captured['cmdline']==' '+subprocess.list2cmdline(args[1:])
    assert captured['cwd']=='C:\\project with spaces'
    assert 'FIXTURE=世界\0' in captured['env']


def test_windows_liveness_observes_handles_never_os_kill(monkeypatch):
    import shared.native_cli as shared
    calls=[]
    class Function:
        def __init__(self,name,result):self.name=name;self.result=result
        def __call__(self,*args):calls.append((self.name,args));return self.result
    kernel=SimpleNamespace(OpenProcess=Function('open',99),WaitForSingleObject=Function('wait',0x102),CloseHandle=Function('close',True))
    monkeypatch.setattr(shared,'os',SimpleNamespace(name='nt',kill=lambda *a:pytest.fail('os.kill called on Windows')))
    monkeypatch.setattr(ctypes,'WinDLL',lambda *a,**kw:kernel,raising=False)
    assert shared.process_exists(23456)
    kernel.WaitForSingleObject.result=0
    assert not shared.process_exists(23456)
    assert len([x for x in calls if x[0]=='close'])==2
