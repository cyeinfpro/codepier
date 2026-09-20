"""Native ConPTY owner. Imports Windows-only dependencies only when invoked.

Raw pywinpty PTY avoids its convenience wrapper's extra loopback socket bridge.
Read/write channels use independent threads; no browser, Hub or Agent transport
owns this process. A Windows Job owns the native process tree, not a PID string.
"""
from __future__ import annotations
import contextlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

from shared.native_cli import database, identifier, SESSION_QUOTA, WorkerLock
from agent.native_terminal import TerminalQueries


def capabilities():
    if os.name != 'nt':
        return {'available': False, 'backend': 'conpty', 'reason': 'ConPTY requires Windows'}
    import sys
    if sys.getwindowsversion().build < 17763:
        return {'available': False, 'backend': 'conpty', 'reason': 'ConPTY requires Windows 10 build 17763 or later'}
    try:
        import winpty
        from importlib.metadata import version
        if not hasattr(winpty, 'PTY') or not hasattr(winpty.Backend, 'ConPTY'):
            raise ImportError('Native backend unavailable')
        return {'available': True, 'backend': 'conpty', 'dependency': 'pywinpty', 'version': version('pywinpty')}
    except (ImportError, AttributeError):
        return {'available': False, 'backend': 'conpty', 'reason': 'Install the Agent Windows dependencies (pywinpty==3.0.5)'}


def resolve_argv(argv, env):
    """Resolve known npm shims to Node entrypoints, never interpolate cmd.exe.

Executable .exe installs are unchanged. Unknown .cmd/.bat wrappers fail clearly
rather than applying POSIX/CRT quoting rules to the different cmd.exe grammar.
"""
    result = list(argv)
    first = Path(result[0])
    if first.suffix.lower() not in {'.cmd', '.bat', '.ps1'}:
        return result
    cli = first.stem.lower()
    packages = {'pi': ('@earendil-works/pi-coding-agent', '@mariozechner/pi-coding-agent'),
                'codex': ('@openai/codex',)}.get(cli, ())
    node = first.parent / 'node.exe'
    node_path = str(node) if node.is_file() else shutil.which('node.exe', path=env.get('PATH'))
    if not node_path:
        raise RuntimeError('NATIVE_NODE_MISSING: npm-installed CLI requires node.exe in the Agent PATH')
    for package in packages:
        folder = first.parent / 'node_modules' / package
        manifest = folder / 'package.json'
        if not manifest.is_file() or manifest.stat().st_size > 131072:
            continue
        info = json.loads(manifest.read_text(encoding='utf-8'))
        entries = info.get('bin', {})
        entry = entries if isinstance(entries, str) else entries.get(cli) if isinstance(entries, dict) else None
        if not isinstance(entry, str):
            continue
        target = (folder / entry).resolve()
        if folder.resolve() not in target.parents or not target.is_file():
            raise RuntimeError('NATIVE_NPM_ENTRY_INVALID: CLI package entry is outside its package')
        return [node_path, str(target), *result[1:]]
    raise RuntimeError('NATIVE_SHIM_UNSUPPORTED: use a native executable or the standard npm CLI installation')


class ProcessJob:
    """Own process handles and a kill-on-close job; never terminate by stale PID."""
    def __init__(self, pid):
        import ctypes as c
        from ctypes import wintypes as w
        self.c = c
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        k = self.kernel
        signatures = {
            'OpenProcess': ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            'CreateJobObjectW': ([c.c_void_p, w.LPCWSTR], w.HANDLE),
            'SetInformationJobObject': ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD], w.BOOL),
            'AssignProcessToJobObject': ([w.HANDLE, w.HANDLE], w.BOOL),
            'TerminateJobObject': ([w.HANDLE, w.UINT], w.BOOL),
            'TerminateProcess': ([w.HANDLE, w.UINT], w.BOOL),
            'WaitForSingleObject': ([w.HANDLE, w.DWORD], w.DWORD),
            'GetExitCodeProcess': ([w.HANDLE, c.POINTER(w.DWORD)], w.BOOL),
            'CloseHandle': ([w.HANDLE], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(k, name); fn.argtypes = args; fn.restype = result
        class Basic(c.Structure):
            _fields_ = [('process_time', c.c_longlong), ('job_time', c.c_longlong),
                        ('flags', w.DWORD), ('minimum', c.c_size_t), ('maximum', c.c_size_t),
                        ('active', w.DWORD), ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class Counters(c.Structure):
            _fields_ = [(name, c.c_ulonglong) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
        class Limits(c.Structure):
            _fields_ = [('basic', Basic), ('io', Counters), ('process_memory', c.c_size_t),
                        ('job_memory', c.c_size_t), ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]
        self.process = k.OpenProcess(0x00100000 | 0x0400 | 0x0100 | 0x0001, False, int(pid))
        self.job = None
        if not self.process:
            raise c.WinError(c.get_last_error())
        try:
            self.job = k.CreateJobObjectW(None, None)
            if not self.job:
                raise c.WinError(c.get_last_error())
            limits = Limits(); limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k.SetInformationJobObject(self.job, 9, c.byref(limits), c.sizeof(limits)):
                raise c.WinError(c.get_last_error())
            if not k.AssignProcessToJobObject(self.job, self.process):
                raise c.WinError(c.get_last_error())
        except BaseException:
            # A failed ownership setup must not leave the just-created child
            # running without its guard. This is the retained child HANDLE.
            k.TerminateProcess(self.process, 1)
            k.WaitForSingleObject(self.process, 3000)
            self.close()
            raise

    def alive(self):
        result = self.kernel.WaitForSingleObject(self.process, 0)
        if result == 0xFFFFFFFF:
            raise self.c.WinError(self.c.get_last_error())
        return result == 0x102

    def exit_code(self):
        if self.alive():
            return None
        from ctypes import wintypes
        code = wintypes.DWORD()
        if not self.kernel.GetExitCodeProcess(self.process, self.c.byref(code)):
            raise self.c.WinError(self.c.get_last_error())
        return int(code.value)

    def terminate(self):
        if self.job and not self.kernel.TerminateJobObject(self.job, 1):
            raise self.c.WinError(self.c.get_last_error())

    def close(self):
        if self.job:
            self.kernel.CloseHandle(self.job); self.job = None
        if self.process:
            self.kernel.CloseHandle(self.process); self.process = None


def create_pty(argv, cwd, env, rows=30, cols=120):
    import winpty
    process = winpty.PTY(cols, rows, backend=winpty.Backend.ConPTY)
    arguments = resolve_argv(argv, env)
    environment = '\0'.join(f'{key}={value}' for key, value in env.items()) + '\0'
    options = {'cwd': cwd, 'env': environment}
    if len(arguments) > 1:
        options['cmdline'] = ' ' + subprocess.list2cmdline(arguments[1:])
    process.spawn(arguments[0], **options)
    return process


def run(directory, sid, *, pty_factory=None, guard_factory=None):
    """The injectable factories are for contract tests, not an alternate backend."""
    sid = identifier(sid)
    with WorkerLock(directory, sid):
        db = database(directory)
        row = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
        if not row or row['status'] != 'starting':
            db.close(); return
        row = dict(row)
        pty = guard = None
        closing = threading.Event()
        reader_done = threading.Event()
        incoming = queue.Queue(maxsize=32)
        outgoing = queue.Queue(maxsize=64)
        written = queue.Queue(maxsize=64)
        failures = queue.Queue(maxsize=4)
        threads = []
        size = row['size']
        status, error, exit_code = 'exited', '', None
        pending = None
        stopping = None
        stop_receipts = []
        terminal = TerminalQueries()

        def fail(kind):
            with contextlib.suppress(queue.Full):
                failures.put_nowait(kind)

        def read_channel():
            try:
                while not closing.is_set():
                    text = pty.read(blocking=False)
                    if text:
                        raw = text.encode('utf-8')
                        for position in range(0, len(raw), 65536):
                            while not closing.is_set():
                                try:
                                    incoming.put(raw[position:position+65536], timeout=.1); break
                                except queue.Full:
                                    pass
                    elif pty.iseof():
                        return
                    else:
                        time.sleep(.01)
            except Exception:
                if guard is not None and guard.alive() and not closing.is_set():
                    fail('CONPTY_READ_FAILED')
            finally:
                reader_done.set()

        def write_channel():
            while not closing.is_set():
                try:
                    receipt, text = outgoing.get(timeout=.05)
                except queue.Empty:
                    continue
                try:
                    # Native writes release the GIL. A blocked native input pipe
                    # cannot stop output draining or controller stop handling.
                    count = pty.write(text)
                    if count != len(text.encode('utf-8')):
                        raise RuntimeError('CONPTY_PARTIAL_WRITE')
                    if receipt:
                        written.put(receipt, timeout=1)
                except Exception:
                    if not closing.is_set():
                        fail('CONPTY_INPUT_UNCERTAIN')
                    return

        def record(raw):
            nonlocal size
            db.execute('INSERT INTO output VALUES (?,?,?)', (sid, size, raw))
            size += len(raw)
            db.execute('UPDATE sessions SET size=?,updated=? WHERE id=?', (size, time.time(), sid))
            db.commit()
            if size >= SESSION_QUOTA:
                raise RuntimeError('TRANSCRIPT_QUOTA: export/clear the retained transcript')
            reply = terminal.feed(raw)
            if reply and guard.alive():
                try:
                    outgoing.put_nowait(('', reply.decode('ascii')))
                except queue.Full as exc:
                    raise RuntimeError('CONPTY_QUERY_BACKPRESSURE') from exc

        try:
            env = dict(os.environ)
            for name in ('TERM_PROGRAM', 'KITTY_WINDOW_ID', 'ITERM_SESSION_ID', 'TMUX', 'STY', 'WT_SESSION'):
                env.pop(name, None)
            env['TERM'] = 'xterm-256color'; env['COLORTERM'] = 'truecolor'
            pty = (pty_factory or create_pty)(json.loads(row['argv']), row['cwd'], env)
            guard = (guard_factory or ProcessJob)(int(pty.pid))
            db.execute("UPDATE sessions SET status='running',worker_pid=?,child_pid=?,heartbeat=?,updated=? WHERE id=?",
                       (os.getpid(), int(pty.pid), time.time(), time.time(), sid)); db.commit()
            for target in (read_channel, write_channel):
                thread = threading.Thread(target=target, daemon=True)
                thread.start(); threads.append(thread)
            last_heartbeat = 0
            while guard.alive():
                if time.time() - last_heartbeat >= 1:
                    db.execute('UPDATE sessions SET heartbeat=? WHERE id=?', (time.time(), sid)); db.commit()
                    last_heartbeat = time.time()
                if not failures.empty():
                    raise RuntimeError(failures.get_nowait())
                for _ in range(16):
                    try:
                        record(incoming.get_nowait())
                    except queue.Empty:
                        break
                while not written.empty():
                    receipt = written.get_nowait()
                    db.execute("UPDATE commands SET state='applied' WHERE id=?", (receipt,)); db.commit()
                    if pending == receipt:
                        pending = None
                stops = db.execute("SELECT id FROM commands WHERE session=? AND kind='stop' AND state='queued' ORDER BY rowid", (sid,)).fetchall()
                if stops:
                    for command in stops:
                        db.execute("UPDATE commands SET state='claimed' WHERE id=?", (command['id'],))
                        stop_receipts.append(command['id'])
                    if stopping is None:
                        stopping = time.monotonic()
                        db.execute("UPDATE sessions SET status='stopping',updated=? WHERE id=?", (time.time(), sid))
                        with contextlib.suppress(queue.Full):
                            outgoing.put_nowait(('', '\x03'))
                    db.commit()
                if stopping is not None and time.monotonic() - stopping >= 2:
                    guard.terminate()
                if pending is None and stopping is None:
                    command = db.execute("SELECT * FROM commands WHERE session=? AND state='queued' AND kind!='stop' ORDER BY rowid LIMIT 1", (sid,)).fetchone()
                    if command:
                        db.execute("UPDATE commands SET state='claimed' WHERE id=?", (command['id'],)); db.commit()
                        payload = json.loads(command['payload'])
                        if command['kind'] == 'resize':
                            pty.set_size(payload['cols'], payload['rows'])
                            terminal.resize(payload['rows'], payload['cols'])
                            db.execute("UPDATE commands SET state='applied' WHERE id=?", (command['id'],)); db.commit()
                        elif command['kind'] == 'input':
                            pending = command['id']
                            outgoing.put_nowait((pending, payload['text']))
                        else:
                            db.execute("UPDATE commands SET state='cancelled' WHERE id=?", (command['id'],)); db.commit()
                time.sleep(.01)
            exit_code = guard.exit_code()
            # Some ConPTY versions do not signal EOF after child exit. Drain
            # until actual EOF or bounded silence and explicitly report a tail
            # whose completeness the native dependency could not confirm.
            deadline, quiet_until = time.monotonic()+5, time.monotonic()+1
            while time.monotonic() < deadline:
                try:
                    record(incoming.get(timeout=.025)); quiet_until = time.monotonic()+1
                except queue.Empty:
                    if reader_done.is_set() or time.monotonic() >= quiet_until:
                        break
            if not reader_done.is_set():
                error = 'CONPTY_TAIL_UNCONFIRMED: child exited; native output EOF was not confirmed'
        except BaseException as exc:
            status = 'quota_error' if 'QUOTA' in str(exc) else 'interrupted'
            error = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        finally:
            try:
                if guard is not None:
                    if guard.alive():
                        guard.terminate()
                        deadline = time.monotonic()+3
                        while guard.alive() and time.monotonic() < deadline:
                            try:
                                raw = incoming.get(timeout=.025)
                            except queue.Empty:
                                continue
                            # Cleanup must not re-enter a failed/quota-full DB
                            # and bypass job/handle release a second time.
                            if status != 'quota_error':
                                try:
                                    record(raw)
                                except Exception:
                                    status = 'interrupted'
                                    error = 'CONPTY_CLEANUP_STORAGE: output could not be retained completely'
                                    break
                    if guard.alive():
                        status, error = 'orphaned', 'Owned ConPTY process has not exited; inspect the node'
                    else:
                        exit_code = guard.exit_code()
                        for receipt in stop_receipts:
                            db.execute("UPDATE commands SET state='applied' WHERE id=?", (receipt,))
            except Exception:
                status, error = 'orphaned', 'CONPTY_CLEANUP_UNCONFIRMED: inspect process ownership on the node'
            finally:
                closing.set()
                if pty is not None:
                    with contextlib.suppress(Exception):
                        pty.cancel_io()
                for thread in threads:
                    thread.join(timeout=1)
                try:
                    db.execute("UPDATE commands SET state='uncertain' WHERE session=? AND state='claimed'", (sid,))
                    db.execute("UPDATE commands SET state='cancelled' WHERE session=? AND state='queued'", (sid,))
                    db.execute("UPDATE sessions SET status=?,error=?,exit_code=?,updated=?,heartbeat=?,lease='',lease_until=0 WHERE id=?",
                               (status, error[:500], exit_code, time.time(), time.time(), sid))
                    db.commit()
                finally:
                    db.close()
                    if guard is not None:
                        guard.close()
                    # Job close owns descendants; PTY destruction frees ConPTY.
                    pty = None
