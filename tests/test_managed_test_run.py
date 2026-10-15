"""Local-only managed test run receipts, ownership and adversarial cleanup."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

import pytest

from scripts import managed_test_run as managed

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='fd-safe POSIX runner; other platforms fail closed')


OWNED_ALIAS_RECEIPTS = []


@pytest.fixture(autouse=True)
def clean_test_roots(monkeypatch):
    """Remove only roots allocated by this test, after its child has stopped."""
    original = managed._create_short_tmp
    allocated = []
    def create(data):
        info = original(data)
        allocated.append((data, {**data, 'short_tmp': json.loads(json.dumps(info))}))
        return info
    monkeypatch.setattr(managed, '_create_short_tmp', create)
    OWNED_ALIAS_RECEIPTS.clear()
    yield
    allocated.extend((data, data) for data in OWNED_ALIAS_RECEIPTS)
    for data, initial in allocated:
        state, pid = data.get('child_state'), data.get('child_pid')
        if state not in {'not_started', 'stopped'} and (
                not managed._dead(pid) or managed.live_group_members(pid)):
            continue
        saved = {**initial, 'cleanup_state': 'cleanup_pending'}
        managed._short_tmp(saved, remove=True)
    OWNED_ALIAS_RECEIPTS.clear()


def command(source):
    return [sys.executable, '-c', source]


def receipt(run):
    return json.loads(run.manifest_path.read_text())


def orphan(tmp_path):
    output = tmp_path / 'orphan'
    source = (
        'import os; from pathlib import Path; '
        'from scripts.managed_test_run import ManagedTestRun; '
        f'r=ManagedTestRun({str(output)!r},min_free_bytes=0); '
        '(r.scratch/"payload").write_text("owned"); os._exit(0)'
    )
    subprocess.run(command(source), cwd=managed.ROOT, check=True, timeout=10,
                   env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
    path = output.resolve() / managed.MANIFEST
    data = json.loads(path.read_text())
    OWNED_ALIAS_RECEIPTS.append(json.loads(json.dumps(data)))
    return path, data


@pytest.mark.parametrize('exit_code', [0, 7])
def test_success_and_failure_keep_evidence_remove_scratch(tmp_path, exit_code):
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
        scratch = run.scratch
        result = run.run(command(
            'import os,sys; from pathlib import Path; '
            'Path(os.environ["TMPDIR"],"cache").write_text("temporary"); '
            f'print("evidence"); sys.exit({exit_code})'
        ), timeout=5)
        assert result['exit_code'] == exit_code
        assert result['status'] == ('success' if exit_code == 0 else 'failed')
        assert not result['cleanup_pending']
        assert not scratch.exists()
        assert (run.evidence / 'output.log').read_text() == 'evidence\n'
        assert receipt(run)['cleanup_state'] == 'cleaned'
        assert receipt(run)['result']['cleanup_pending'] is False


def test_environment_cannot_be_overridden_with_inherited_values(tmp_path):
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
        scratch = str(run.scratch)
        result = run.run(command(
            'import os,json; print(json.dumps({k:os.environ[k] for k in '
            '["TMPDIR","TMP","TEMP","XDG_CACHE_HOME","PYTHONPYCACHEPREFIX"]}))'
        ), timeout=5, env={'TMPDIR': '/should-not-use', 'XDG_CACHE_HOME': '/wrong'})
        assert result['status'] == 'success'
        values = json.loads((run.evidence / 'output.log').read_text())
        assert all(values[key].startswith(scratch + '/') for key in ('XDG_CACHE_HOME', 'PYTHONPYCACHEPREFIX'))
        assert values['TMPDIR'] == values['TMP'] == values['TEMP']
        assert len(values['TMPDIR'].encode()) < 60


def test_timeout_and_descendants_are_stopped_before_cleanup(tmp_path):
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
        result = run.run(command(
            'import subprocess,sys,time; '
            'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
            'print(p.pid,flush=True); time.sleep(30)'
        ), timeout=.4)
        assert result['timed_out'] and result['forced_termination']
        assert result['status'] == 'timeout'
        assert not result['cleanup_pending']
        assert not run.scratch.exists()
        assert managed.live_group_members(receipt(run)['child_pid']) == []


def test_natural_leader_exit_stops_live_descendant(tmp_path):
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
        result = run.run(command(
            'import subprocess,sys; '
            'subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
            'print("leader done",flush=True)'
        ), timeout=5)
        assert result['exit_code'] == 0
        assert result['forced_termination']
        assert result['status'] == 'failed'
        assert not result['cleanup_pending']
        assert managed.live_group_members(receipt(run)['child_pid']) == []


def test_cancel_request_stops_owned_process(tmp_path):
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
        timer = threading.Timer(.2, run.cancel)
        timer.start()
        try:
            result = run.run(command('import time; time.sleep(30)'), timeout=5)
        finally:
            timer.join()
        assert result['cancelled'] and result['status'] == 'cancelled'
        assert not result['cleanup_pending']
        assert not run.scratch.exists()


def test_cancel_before_launch_still_cleans(tmp_path):
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
        run.cancel()
        result = run.run(command('raise AssertionError("must not launch")'), timeout=5)
        assert result['cancelled']
        assert result['exit_code'] is None
        assert not result['cleanup_pending']


def test_output_is_bounded_while_command_completes(tmp_path):
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0, max_log_bytes=1024) as run:
        result = run.run(command('import os; os.write(1,b"x"*2000000)'), timeout=5)
        assert result['status'] == 'success' and result['log_truncated']
        assert (run.evidence / 'output.log').stat().st_size == 1024


def test_disk_admission_starts_no_process(tmp_path):
    with pytest.raises(managed.AdmissionError):
        managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=10**30)
    assert list((tmp_path / 'run').iterdir()) == []


def test_scratch_budget_stops_and_cleans_owned_files(tmp_path):
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0, max_scratch_bytes=4096) as run:
        with pytest.raises(managed.AdmissionError):
            run.run(command(
                'import os,time; from pathlib import Path; '
                'Path(os.environ["TMPDIR"],"large").write_bytes(b"x"*10000); time.sleep(30)'
            ), timeout=5)
        assert not run.scratch.exists()
        assert receipt(run)['status'] == 'failed'
        assert not receipt(run)['cleanup_pending']


def test_missing_executable_records_failure_without_leak(tmp_path):
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
        with pytest.raises(FileNotFoundError):
            run.run(['/no/such/codepier-test-executable'], timeout=1)
        assert not run.scratch.exists()
        assert receipt(run)['child_state'] == 'not_started'


def test_context_exception_preserves_primary_and_cleans(tmp_path):
    run = None
    with pytest.raises(RuntimeError, match='primary'):
        with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
            (run.scratch / 'file').write_text('owned')
            raise RuntimeError('primary')
    assert not run.scratch.exists()
    assert receipt(run)['status'] == 'failed'


def test_foreign_evidence_and_existing_run_are_never_adopted(tmp_path):
    output = tmp_path / 'old'
    output.mkdir()
    sentinel = output / 'user-report.txt'
    sentinel.write_text('keep')
    with pytest.raises(managed.SafetyError, match='fresh'):
        managed.ManagedTestRun(output, min_free_bytes=0)
    assert sentinel.read_text() == 'keep'
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0):
        pass
    with pytest.raises(managed.SafetyError, match='fresh'):
        managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0)


def test_internal_symlink_is_unlinked_without_touching_target(tmp_path):
    outside = tmp_path / 'historical-worktree'
    outside.mkdir()
    (outside / 'user-data').write_text('keep')
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
        (run.scratch / 'pytest-current').symlink_to(outside, target_is_directory=True)
        run.finish('success')
        assert not run.scratch.exists()
        assert not receipt(run)['cleanup_pending']
    assert (outside / 'user-data').read_text() == 'keep'


@pytest.mark.parametrize('replacement', ['directory', 'symlink'])
def test_changed_scratch_root_is_never_removed(tmp_path, replacement):
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
        old = run.scratch.with_name('moved-owned-scratch')
        run.scratch.rename(old)
        outside = tmp_path / 'old-user-data'
        outside.mkdir()
        (outside / 'keep').write_text('keep')
        if replacement == 'directory':
            run.scratch.mkdir()
            (run.scratch / 'keep').write_text('keep')
        else:
            run.scratch.symlink_to(outside, target_is_directory=True)
        run.finish('failed')
        assert receipt(run)['cleanup_pending']
        assert run.scratch.exists() and old.exists()
        assert (outside / 'keep').read_text() == 'keep'


def test_replaced_evidence_root_does_not_redirect_cleanup(tmp_path):
    run = managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0)
    original = run.evidence
    moved = original.with_name('moved-evidence')
    original.rename(moved)
    original.mkdir()
    (original / 'keep').write_text('historical')
    try:
        run.finish('failed')
        assert run.data['cleanup_pending']
        assert (moved / run.scratch.name).is_dir()
        assert (original / 'keep').read_text() == 'historical'
    finally:
        run._close()


def test_initial_system_ancestor_symlink_is_canonicalized(tmp_path):
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'temporary'
    link.symlink_to(real, target_is_directory=True)
    with managed.ManagedTestRun(link / 'run', min_free_bytes=0) as run:
        assert run.evidence == real.resolve() / 'run'
        run.finish('success')
        assert not run.scratch.exists()


def test_recovery_inspects_then_cleans_dead_owner_without_signals(tmp_path, monkeypatch):
    path, data = orphan(tmp_path)
    scratch = path.parent / data['scratch_name']
    original_kill = os.kill
    signals = []
    def observe(pid, number):
        signals.append(number)
        return original_kill(pid, number)
    monkeypatch.setattr(managed.os, 'kill', observe)
    assert managed.recover(path)['status'] == 'ready'
    assert scratch.exists()
    assert managed.recover(path, dry_run=False)['status'] == 'cleaned'
    assert not scratch.exists()
    assert json.loads(path.read_text())['cleanup_pending'] is False
    assert signals and all(number == 0 for number in signals)


def test_recovery_refuses_live_owner_lock(tmp_path):
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
        result = managed.recover(run.manifest_path, dry_run=False)
        assert result['status'] == 'blocked'
        assert run.scratch.exists()


@pytest.mark.parametrize('state', ['launching', 'unknown'])
def test_recovery_refuses_unknown_child_state(tmp_path, state):
    path, data = orphan(tmp_path)
    data['child_state'] = state
    path.write_text(json.dumps(data))
    assert managed.recover(path, dry_run=False)['status'] == 'blocked'
    assert (path.parent / data['scratch_name']).exists()


def test_recovery_refuses_live_or_reused_child_pid(tmp_path):
    path, data = orphan(tmp_path)
    data['child_state'] = 'running'
    data['child_pid'] = os.getpid()
    path.write_text(json.dumps(data))
    result = managed.recover(path, dry_run=False)
    assert result['status'] == 'blocked'
    assert 'Child PID' in result['reasons'][0]


def test_recovery_refuses_unknown_process_observation(tmp_path, monkeypatch):
    path, data = orphan(tmp_path)
    def denied(*args):
        raise PermissionError('unknown')
    monkeypatch.setattr(managed.os, 'kill', denied)
    assert managed.recover(path, dry_run=False)['status'] == 'blocked'
    assert (path.parent / data['scratch_name']).exists()


def test_recovery_rejects_changed_inode_and_forged_path(tmp_path):
    path, data = orphan(tmp_path)
    scratch = path.parent / data['scratch_name']
    scratch.rename(path.parent / 'moved')
    scratch.mkdir()
    (scratch / 'keep').write_text('user')
    assert managed.recover(path, dry_run=False)['status'] == 'blocked'
    assert (scratch / 'keep').read_text() == 'user'
    data['scratch_name'] = '../historical'
    path.write_text(json.dumps(data))
    assert managed.recover(path, dry_run=False)['status'] == 'blocked'


def test_recovery_after_quarantine_marker_removed_before_rmdir(tmp_path):
    path, data = orphan(tmp_path)
    scratch = path.parent / data['scratch_name']
    quarantine = path.parent / data['cleanup_name']
    scratch.rename(quarantine)
    # Simulate a crash after deletion of all entries, immediately before rmdir.
    for child in quarantine.iterdir():
        child.rmdir() if child.is_dir() else child.unlink()
    data['cleanup_state'] = 'cleanup_pending'
    path.write_text(json.dumps(data))
    assert managed.recover(path, dry_run=False)['status'] == 'cleaned'
    assert not quarantine.exists()


def test_unverified_stop_retains_scratch(tmp_path, monkeypatch):
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
        run._stopped = False
        run.data['child_state'] = 'unknown'
        run.finish('failed')
        assert run.scratch.exists()
        assert receipt(run)['cleanup_pending']
        assert 'unverified' in receipt(run)['cleanup_error']
        # This fixture never launched a child; restore only its test proof.
        run._stopped = True
        run.data['child_state'] = 'not_started'


def test_cli_opt_in_for_arbitrary_project_with_unique_evidence(tmp_path):
    project = tmp_path / 'mapped-project'
    project.mkdir()
    (project / 'fixture.py').write_text(
        'import os; from pathlib import Path\n'
        'assert Path.cwd().name == "mapped-project"\n'
        'Path(os.environ["TMPDIR"],"fixture-cache").write_text("owned")\n'
        'print("arbitrary project passed")\n'
    )
    evidence = tmp_path / 'runs'
    for _ in range(2):
        result = subprocess.run([
            sys.executable, str(managed.ROOT / 'scripts/managed_test_run.py'),
            '--output-root', str(evidence), '--cwd', str(project), '--min-free-bytes', '0',
            '--project', 'opaque-project', '--workspace', 'opaque-workspace', '--task', 'fixture',
            '--timeout', '5', '--', sys.executable, 'fixture.py',
        ], capture_output=True, text=True, timeout=15,
           env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        assert result.returncode == 0, result.stderr + result.stdout
        report = json.loads(result.stdout)
        assert report['status'] == 'success' and not report['cleanup_pending']
    directories = list(evidence.iterdir())
    assert len(directories) == 2
    for directory in directories:
        data = json.loads((directory / managed.MANIFEST).read_text())
        assert data['metadata'] == {'project': 'opaque-project', 'workspace': 'opaque-workspace', 'task': 'fixture'}
        assert (directory / 'output.log').read_text() == 'arbitrary project passed\n'
        assert not (directory / data['scratch_name']).exists()
    assert (project / 'fixture.py').exists()


def test_unsupported_platform_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(managed.shutil.rmtree, 'avoids_symlink_attacks', False)
    with pytest.raises(managed.SafetyError, match='fd-safe'):
        managed.ManagedTestRun(tmp_path / 'run')
    assert not (tmp_path / 'run').exists()


def test_interrupted_launch_is_unknown_and_never_cleaned(tmp_path, monkeypatch):
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
        def interrupted(*args, **kwargs):
            raise KeyboardInterrupt()
        monkeypatch.setattr(managed.subprocess, 'Popen', interrupted)
        with pytest.raises(KeyboardInterrupt):
            run.run(['not-started-in-test'], timeout=1)
        assert run.scratch.exists()
        assert receipt(run)['child_state'] == 'launching'
        assert receipt(run)['cleanup_pending']
        # The mocked Popen raised before creating any child.
        run._stopped = True
        run.data['child_state'] = 'not_started'


def test_malformed_manifest_is_blocked(tmp_path):
    path, _ = orphan(tmp_path)
    path.write_text('[]')
    assert managed.recover(path, dry_run=False)['status'] == 'blocked'


def test_owned_fifo_and_socket_leaves_are_safe_to_unlink(tmp_path):
    import socket
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
        os.mkfifo(run.scratch / 'fifo')
        # Keep UNIX socket pathname short enough for Darwin's sockaddr_un.
        with socket.socket(socket.AF_UNIX) as server:
            original = os.getcwd()
            try:
                os.chdir(run.scratch)
                server.bind('socket')
            finally:
                os.chdir(original)
        run.finish('success')
        assert not run.scratch.exists()
        assert not receipt(run)['cleanup_pending']


def test_cleanup_write_failure_preserves_primary_exception(tmp_path, monkeypatch):
    run = None
    with pytest.raises(RuntimeError, match='primary'):
        with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
            def fail_write(*args, **kwargs):
                raise OSError('disk unavailable')
            monkeypatch.setattr(managed, '_json_write', fail_write)
            raise RuntimeError('primary')
    assert run.scratch.exists()
    assert run.data['cleanup_pending']


def test_long_evidence_path_can_bind_unix_socket(tmp_path):
    with managed.ManagedTestRun(tmp_path / ('deep-' * 35) / 'run', min_free_bytes=0) as run:
        temporary = Path(run.environment()['TMPDIR'])
        result = run.run(command(
            'import os,socket,tempfile; from pathlib import Path; '
            'p=Path(tempfile.mkdtemp(prefix="org.chromium.Chromium.")).resolve()/"SingletonSocket"; '
            's=socket.socket(socket.AF_UNIX); s.bind(str(p)); s.close(); print(p)'
        ), timeout=5)
        assert result['status'] == 'success'
        assert not result['cleanup_pending']
        assert not temporary.exists()
        assert not run.scratch.exists()


def test_short_root_replacement_blocks_without_touching_foreign_target(tmp_path):
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
        temporary = Path(run.environment()['TMPDIR'])
        original = temporary.with_name(temporary.name + '-saved')
        temporary.rename(original)
        temporary.symlink_to(tmp_path)
        try:
            run.finish('success')
            assert receipt(run)['cleanup_pending']
            assert run.scratch.is_dir()
            assert temporary.is_symlink()
        finally:
            temporary.unlink()
            original.rename(temporary)
        run.finish('success')
        assert not receipt(run)['cleanup_pending']


def test_recovery_after_short_root_removal(tmp_path):
    path, data = orphan(tmp_path)
    data['cleanup_state'] = 'cleanup_pending'
    fd = managed._directory(path.parent)
    try:
        managed._json_write(fd, managed.MANIFEST, data)
        managed._short_tmp(data, remove=True)
    finally:
        os.close(fd)
    assert managed.recover(path, dry_run=False)['status'] == 'cleaned'


def test_short_root_symlink_does_not_follow_foreign_target(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'user-file'
    sentinel.write_text('keep')
    with managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0) as run:
        root = Path(run.environment()['TMPDIR'])
        (root / 'outside-link').symlink_to(outside, target_is_directory=True)
        run.finish('success')
        assert not receipt(run)['cleanup_pending']
        assert not root.exists()
    assert sentinel.read_text() == 'keep'


@pytest.mark.parametrize('invalid', [[], 'forged', 5])
def test_malformed_short_metadata_is_blocked(tmp_path, invalid):
    path, data = orphan(tmp_path)
    data['short_tmp'] = invalid
    path.write_text(json.dumps(data))
    assert managed.recover(path, dry_run=False)['status'] == 'blocked'


def test_initial_manifest_failure_reclaims_just_created_temporary(tmp_path, monkeypatch):
    original = managed._json_write
    roots = []
    def fail(fd, name, data, **kwargs):
        if name == managed.MANIFEST and not kwargs.get('initial') and 'short_tmp' in data and data.get('status') != 'initialization_failed':
            roots.append(Path(data['short_tmp']['parent']) / ('cpt-' + data['run_id']))
            raise OSError('fixture manifest failure')
        return original(fd, name, data, **kwargs)
    monkeypatch.setattr(managed, '_json_write', fail)
    with pytest.raises(OSError, match='fixture manifest failure'):
        managed.ManagedTestRun(tmp_path / 'run', min_free_bytes=0)
    assert len(roots) == 1
    assert not roots[0].exists()


@pytest.mark.parametrize('fault', ['marker', 'fsync'])
def test_short_root_creation_failure_is_owned_and_reclaimed(tmp_path, monkeypatch, fault):
    original = managed._json_write
    roots = []
    def fail(fd, name, data, **kwargs):
        if name == managed.MARKER and data.get('run_id'):
            root = Path('/tmp').resolve() / ('cpt-' + data['run_id'])
            if root.exists() and managed._matches(os.fstat(fd), managed._identity(root.stat())):
                roots.append(root)
                if fault == 'fsync':
                    original(fd, name, data, **kwargs)
                raise OSError('fixture short-root creation ' + fault)
        return original(fd, name, data, **kwargs)
    monkeypatch.setattr(managed, '_json_write', fail)
    output = tmp_path / 'run'
    with pytest.raises(OSError, match='fixture short-root creation'):
        managed.ManagedTestRun(output, min_free_bytes=0)
    assert len(roots) == 1 and not roots[0].exists()
    data = json.loads((output / managed.MANIFEST).read_text())
    assert data['status'] == 'initialization_failed'
    assert data['short_tmp']['identity']
