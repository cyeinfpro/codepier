from types import SimpleNamespace

import pytest

from scripts import panel_update_launcher as launcher


def setup(monkeypatch, tmp_path, identities, codes):
    home = tmp_path / '.codepier-updater'; home.mkdir()
    (home / 'service').mkdir()
    monkeypatch.setattr(launcher.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(launcher, 'secure_path', lambda path, **kwargs: path)
    iterator = iter(identities)
    monkeypatch.setattr(launcher, 'select_engine', lambda home: next(iterator))
    monkeypatch.setattr(launcher, 'pending_jobs', lambda home: [])
    monkeypatch.setattr(launcher, 'child_command', lambda root, home, identity, fd: [identity['id'], str(fd)])
    calls = []
    sequence = iter(codes)
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=next(sequence))
    monkeypatch.setattr(launcher.subprocess, 'run', run)
    return calls


def test_supervised_idle_transition_same_fd_and_clean_env(monkeypatch, tmp_path):
    calls = setup(monkeypatch, tmp_path, [{'id': 'old'}, {'id': 'new'}, {'id': 'new'}],
                  [launcher.RESTART_EXIT, 0])
    assert launcher.supervise(tmp_path) == 0
    assert len(calls) == 2
    assert calls[0][1]['pass_fds'] == calls[1][1]['pass_fds']
    assert 'PYTHONPATH' not in calls[0][1]['env']
    assert calls[0][1]['cwd'] == tmp_path / '.codepier-updater/service'


def test_child_failure_does_not_fallback_or_replay(monkeypatch, tmp_path):
    calls = setup(monkeypatch, tmp_path, [{'id': 'old'}], [1])
    assert launcher.supervise(tmp_path) == 1
    assert len(calls) == 1


def test_restart_without_new_selector_rejected(monkeypatch, tmp_path):
    setup(monkeypatch, tmp_path, [{'id': 'old'}, {'id': 'old'}], [launcher.RESTART_EXIT])
    with pytest.raises(launcher.EngineError):
        launcher.supervise(tmp_path)


def test_pending_job_blocks_restart(monkeypatch, tmp_path):
    calls = setup(monkeypatch, tmp_path, [{'id': 'old'}], [launcher.RESTART_EXIT])
    monkeypatch.setattr(launcher, 'pending_jobs', lambda home: [{'state': 'recovery_required'}])
    with pytest.raises(launcher.EngineError):
        launcher.supervise(tmp_path)
    assert len(calls) == 1


def test_non_root_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(launcher.os, 'geteuid', lambda: 123)
    with pytest.raises(launcher.EngineError):
        launcher.supervise(tmp_path)


def test_inherited_flock_survives_parent_descriptor_close(tmp_path):
    import fcntl
    import os
    import subprocess
    import sys
    path = tmp_path / 'service.lock'
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    process = subprocess.Popen(
        [sys.executable, '-c', 'import sys;print("ready",flush=True);sys.stdin.read(1)'],
        pass_fds=(descriptor,), stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    contender = None
    try:
        assert process.stdout.readline().strip() == 'ready'
        os.close(descriptor); descriptor = None
        contender = os.open(path, os.O_RDWR)
        with pytest.raises(BlockingIOError):
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
        process.communicate('x', timeout=5)
        assert process.returncode == 0
        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if contender is not None:
            os.close(contender)
        if process.poll() is None:
            process.kill(); process.wait()


def test_isolated_engine_imports_do_not_create_bytecode(monkeypatch, tmp_path):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    from scripts import panel_update_engine as engine
    directory = tmp_path / 'engine'; directory.mkdir()
    (directory / 'panel_update_source.py').write_text('VALUE = 1\n')
    (directory / 'panel_update_runtime.py').write_text('VALUE = 2\n')
    (directory / 'panel_updater.py').write_text(
        'import sys\nfrom pathlib import Path\nsys.path.insert(0,str(Path(__file__).parent))\n'
        'import panel_update_source, panel_update_runtime\n'
        'assert panel_update_source.VALUE + panel_update_runtime.VALUE == 3\n')
    monkeypatch.setattr(launcher, 'verify_engine', lambda *a, **k: directory)
    monkeypatch.setattr(launcher, 'secure_path', lambda path, **k: path)
    command = launcher.child_command(tmp_path, tmp_path, {'id': 'a' * 64}, 3, expected_uid=os.getuid())
    assert command[:3] == [str(Path(sys.executable).resolve()), '-I', '-B']
    subprocess.run(command, check=True, capture_output=True)
    assert set(p.name for p in directory.iterdir()) == set(engine.ENGINE_FILES)
    assert json.loads(json.dumps(command)) == command
