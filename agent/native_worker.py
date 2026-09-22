"""Own one native PTY independently of browser and Agent transport lifetimes.

The worker alone signals/reaps its direct child. Session status is a consequence
of observed process exit, never a substitute for stopping that process.
"""
from __future__ import annotations
import contextlib
import errno
import json
import os
from pathlib import Path
import select
import signal
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.native_cli import database, identifier, SESSION_QUOTA, WorkerLock
from agent.native_terminal import TerminalQueries
from agent.owned_process_group import child_exited, stop_owned_group


def run(directory, sid):
    if os.name == 'nt':
        from agent.native_windows import run as run_windows
        return run_windows(directory, sid)
    sid = identifier(sid)
    ownership = WorkerLock(directory, sid)
    db = database(directory)
    row = db.execute('SELECT * FROM sessions WHERE id=?', (sid,)).fetchone()
    if not row or row['status'] != 'starting':
        db.close()
        ownership.close()
        return
    row = dict(row)
    argv = json.loads(row['argv'])
    # No sqlite connection crosses fork: inherited SQLite locks can stall both
    # processes even though the child intends to exec immediately.
    db.close()
    db = None
    pid = fd = None
    reaped = False
    exit_code = None
    status, error = 'exited', ''
    stop_requested = False
    stopping_at = None
    stop_receipts = []
    pending = None
    reply_buffer = bytearray()
    terminal = TerminalQueries()
    size = row['size']

    def request_stop(*_args):
        nonlocal stop_requested
        stop_requested = True

    def signal_child(number):
        if pid is None or reaped:
            return
        # A live, unreaped direct child cannot have its PID reused. The child
        # group is used only after verifying the PTY child owns that group.
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, number)
            else:
                os.kill(pid, number)
        except ProcessLookupError:
            pass

    def begin_stop():
        nonlocal stopping_at
        if stopping_at is None:
            stopping_at = time.monotonic()
            signal_child(signal.SIGTERM)
            db.execute("UPDATE sessions SET status='stopping',updated=? WHERE id=?", (time.time(), sid))
            db.commit()

    def record(data):
        nonlocal size
        db.execute('INSERT INTO output VALUES (?,?,?)', (sid, size, data))
        size += len(data)
        db.execute('UPDATE sessions SET size=?,updated=? WHERE id=?', (size, time.time(), sid))
        db.commit()
        # The boundary chunk is retained, rather than silently truncating text.
        if size >= SESSION_QUOTA:
            raise RuntimeError('TRANSCRIPT_QUOTA: export and clear this CodePier transcript before another launch')
        response = terminal.feed(data)
        if len(reply_buffer) + len(response) > 65536:
            raise RuntimeError('TERMINAL_QUERY_FLOOD: excessive terminal responses')
        reply_buffer.extend(response)

    try:
        import pty
        import fcntl
        import struct
        import termios
        pid, fd = pty.fork()
        if pid == 0:
            try:
                for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
                    signal.signal(number, signal.SIG_DFL)
                if hasattr(signal, 'pthread_sigmask'):
                    signal.pthread_sigmask(signal.SIG_SETMASK, [])
                ownership.close()
                os.chdir(row['cwd'])
                # The browser terminal is xterm, not the Agent's original Kitty,
                # iTerm or tmux window. Never advertise unsupported graphics.
                for key in ('TERM_PROGRAM', 'TERM_PROGRAM_VERSION', 'KITTY_WINDOW_ID',
                            'ITERM_SESSION_ID', 'WEZTERM_PANE', 'GHOSTTY_RESOURCES_DIR',
                            'TMUX', 'STY', 'WT_SESSION', 'WARP_SESSION_ID'):
                    os.environ.pop(key, None)
                os.environ['TERM'] = 'xterm-256color'
                os.environ['COLORTERM'] = 'truecolor'
                os.execvpe(argv[0], argv, os.environ)
            except BaseException as exc:
                # No argv/environment/credential values in bootstrap errors.
                os.write(2, ('CODEPIER_NATIVE_EXEC_FAILED: ' + type(exc).__name__ + '\r\n').encode())
                os._exit(127)
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGHUP, request_stop)
        os.set_blocking(fd, False)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', terminal.rows, terminal.cols, 0, 0))
        db = database(directory)
        db.execute("UPDATE sessions SET status='running',worker_pid=?,child_pid=?,heartbeat=?,updated=? WHERE id=?",
                   (os.getpid(), pid, time.time(), time.time(), sid))
        db.commit()
        last_heartbeat = 0.0
        eof = False
        while not reaped:
            now = time.time()
            if now - last_heartbeat >= 1:
                db.execute('UPDATE sessions SET heartbeat=? WHERE id=?', (now, sid))
                db.commit()
                last_heartbeat = now
            # A deliberate stop has priority over a blocked paste. Stop receipts
            # are applied only after waitpid confirms real process termination.
            stops = db.execute("SELECT * FROM commands WHERE session=? AND kind='stop' AND state='queued' ORDER BY rowid", (sid,)).fetchall()
            for command in stops:
                db.execute("UPDATE commands SET state='claimed' WHERE id=? AND state='queued'", (command['id'],))
                stop_receipts.append(command['id'])
            if stops:
                db.commit()
                stop_requested = True
            if stop_requested:
                begin_stop()
            if stopping_at is not None and time.monotonic() - stopping_at >= 2:
                signal_child(signal.SIGKILL)
            if pending is None and stopping_at is None:
                command = db.execute("SELECT * FROM commands WHERE session=? AND state='queued' AND kind!='stop' ORDER BY rowid LIMIT 1", (sid,)).fetchone()
                if command:
                    db.execute("UPDATE commands SET state='claimed' WHERE id=? AND state='queued'", (command['id'],))
                    db.commit()  # claim-before-side-effect, never replay uncertain input
                    payload = json.loads(command['payload'])
                    if command['kind'] == 'resize':
                        terminal.resize(payload['rows'], payload['cols'])
                        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack('HHHH', payload['rows'], payload['cols'], 0, 0))
                        db.execute("UPDATE commands SET state='applied' WHERE id=?", (command['id'],))
                        db.commit()
                    elif command['kind'] == 'input':
                        pending = [command['id'], payload['text'].encode('utf-8'), 0]
                    else:
                        db.execute("UPDATE commands SET state='cancelled' WHERE id=?", (command['id'],))
                        db.commit()
            want_write = not eof and stopping_at is None and (pending is not None or reply_buffer)
            readable, writable, _ = select.select([] if eof else [fd], [fd] if want_write else [], [], .025)
            if readable:
                try:
                    data = os.read(fd, 65536)
                except BlockingIOError:
                    data = None
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b''
                if data:
                    record(data)
                elif data == b'':
                    eof = True
            if writable:
                try:
                    # Keep a partial input atomic relative to terminal replies,
                    # while continuing to drain output between every small write.
                    if pending is not None and pending[2] > 0:
                        written = os.write(fd, pending[1][pending[2]:pending[2]+4096])
                        pending[2] += written
                    elif reply_buffer:
                        written = os.write(fd, reply_buffer[:4096])
                        del reply_buffer[:written]
                    elif pending is not None:
                        written = os.write(fd, pending[1][:4096])
                        pending[2] += written
                    if pending is not None and pending[2] == len(pending[1]):
                        db.execute("UPDATE commands SET state='applied' WHERE id=?", (pending[0],))
                        db.commit()
                        pending = None
                except BlockingIOError:
                    pass
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    eof = True
            if child_exited(pid):
                break  # Keep the leader unreaped until its whole group is stopped.
        # Drain only a bounded post-exit tail. A descendant holding the PTY open
        # must not keep a completed session/worker alive indefinitely.
        end = time.monotonic() + .25
        for _ in range(32):
            if eof or time.monotonic() >= end or not select.select([fd], [], [], .01)[0]:
                break
            try:
                data = os.read(fd, 65536)
            except (BlockingIOError, OSError):
                break
            if not data:
                break
            record(data)
        if exit_code == 127:
            error = 'Native executable could not start; inspect the classified bootstrap message'
    except BaseException as exc:
        status = 'quota_error' if 'QUOTA' in str(exc) else 'interrupted'
        error = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    finally:
        if pid is not None and not reaped:
            reaped, exit_code = stop_owned_group(pid, grouped=True)
            if not reaped:
                status, error = 'orphaned', 'Owned native process group cleanup is unverified; inspect Agent host'
            elif exit_code == 127:
                error = 'Native executable could not start; inspect the classified bootstrap message'
        if fd is not None:
            with contextlib.suppress(OSError):
                os.close(fd)
        if db is not None:
            try:
                if reaped:
                    for receipt in stop_receipts:
                        db.execute("UPDATE commands SET state='applied' WHERE id=?", (receipt,))
                db.execute("UPDATE commands SET state='uncertain' WHERE session=? AND state='claimed'", (sid,))
                db.execute("UPDATE commands SET state='cancelled' WHERE session=? AND state='queued'", (sid,))
                db.execute("UPDATE sessions SET status=?,error=?,exit_code=?,updated=?,heartbeat=?,lease='',lease_until=0 WHERE id=?",
                           (status, error[:500], exit_code, time.time(), time.time(), sid))
                db.commit()
            finally:
                db.close()
        ownership.close()


if __name__ == '__main__':
    run(sys.argv[1], sys.argv[2])
