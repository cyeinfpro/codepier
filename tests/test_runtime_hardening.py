"""Real bounded config I/O, crash-recovered ownership and truthful save outcomes."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent import runtime_config, runtime_config_lock, runtime_panel
from agent.runtime_config_lock import config_lock
from shared.util import DevError
from tests.test_runtime_panel import project, get, save
from tests.test_shell import shell_agent  # noqa: F401


def test_runtime_writer_guard_serializes_even_threads_in_the_same_process(tmp_path):
    target = tmp_path / 'config.json'
    with config_lock(target):
        marker = target.with_name(target.name + '.runtime.lock')
        before = marker.read_bytes()
        with pytest.raises(FileExistsError):
            with config_lock(target):
                pytest.fail('Concurrent writer must not enter')
        assert marker.read_bytes() == before
    assert not marker.exists()
    with config_lock(target):
        assert marker.exists()
    assert target.with_name(target.name + '.runtime.guard').is_file()


def test_crashed_writer_is_recovered_without_terminating_or_reusing_its_operation(tmp_path):
    target = tmp_path / 'config.json'
    script = "from pathlib import Path; from agent.runtime_config_lock import config_lock; import os,sys; lock=config_lock(Path(sys.argv[1])); lock.__enter__(); os._exit(23)"
    child = subprocess.run([sys.executable, '-c', script, str(target)], timeout=10, capture_output=True)
    assert child.returncode == 23, child.stderr
    marker = target.with_name(target.name + '.runtime.lock')
    old = json.loads(marker.read_text())
    assert runtime_config_lock.process_exists(old['pid']) is False
    with config_lock(target):
        current = json.loads(marker.read_text())
        assert current['nonce'] != old['nonce'] and current['pid'] == os.getpid()
    assert not marker.exists()


@pytest.mark.parametrize('alive', [True, None])
def test_live_or_unknown_marker_owner_is_never_reclaimed(tmp_path, monkeypatch, alive):
    target = tmp_path / 'config.json'
    marker = target.with_name(target.name + '.runtime.lock')
    marker.write_text(json.dumps({'schema_version': 2, 'pid': 100, 'nonce': 'a'*32}))
    marker.chmod(0o600)
    original = marker.read_bytes()
    monkeypatch.setattr(runtime_config_lock, 'process_exists', lambda _: alive)
    with pytest.raises(FileExistsError):
        with config_lock(target):
            pytest.fail('Unknown process state is not lock recovery permission')
    assert marker.read_bytes() == original


def test_legacy_marker_and_symlinked_guard_remain_untouched(tmp_path):
    target = tmp_path / 'config.json'
    marker = target.with_name(target.name + '.runtime.lock')
    marker.write_text('legacy owner')
    with pytest.raises(FileExistsError):
        with config_lock(target):
            pytest.fail('Legacy marker lacks process-exit evidence')
    assert marker.read_text() == 'legacy owner'
    guard = tmp_path / 'another.json.runtime.guard'
    outside = tmp_path / 'unrelated'
    outside.write_text('do not touch')
    guard.symlink_to(outside)
    with pytest.raises((OSError, ValueError)):
        with config_lock(tmp_path / 'another.json'):
            pytest.fail('Guard links must be rejected')
    assert outside.read_text() == 'do not touch'


@pytest.mark.parametrize('raw', ['{"mode":1,"mode":2}', '{"outer":{"mode":1,"mode":1}}'])
def test_ambiguous_duplicate_configuration_keys_are_rejected(tmp_path, raw):
    target = tmp_path / 'config.json'
    target.write_text(raw)
    with pytest.raises(ValueError, match='Duplicate'):
        runtime_config.read_json(target)
    assert target.read_text() == raw


def test_fifo_configuration_cannot_block_a_control_worker(tmp_path):
    if os.name == 'nt':
        return  # Windows has no POSIX FIFO; this module is native POSIX qualification.
    target = tmp_path / 'config.json'
    os.mkfifo(target)
    script = "from agent.runtime_config import read_json; import sys\ntry: read_json(sys.argv[1])\nexcept ValueError: sys.exit(0)\nsys.exit(1)"
    child = subprocess.run([sys.executable, '-c', script, str(target)], timeout=5, capture_output=True)
    assert child.returncode == 0, child.stderr


def test_read_rejects_file_replaced_during_observation(tmp_path, monkeypatch):
    target = tmp_path / 'config.json'
    target.write_text('{"value":1}')
    original = os.fstat
    calls = 0
    def changed(descriptor):
        nonlocal calls
        calls += 1
        if calls == 2:
            replacement = tmp_path / 'replacement.json'
            replacement.write_text('{"value":2}')
            os.replace(replacement, target)
        return original(descriptor)
    monkeypatch.setattr(runtime_config.os, 'fstat', changed)
    with pytest.raises(DevError) as caught:
        runtime_config.read_json(target)
    assert caught.value.code == 'RUNTIME_CONFIG_CHANGED'
    assert json.loads(target.read_text()) == {'value': 2}


def test_oversized_replacement_cannot_make_config_unreadable(shell_agent):
    agent, _ = shell_agent
    _, raw, info = runtime_config.read_json(agent.config_path)
    with pytest.raises(ValueError, match='readable size'):
        runtime_config._replace_config(agent.config_path, raw, info, b' '* (runtime_config.MAX_BYTES+1), hashlib.sha256(raw).hexdigest())
    assert agent.config_path.read_bytes() == raw


def test_post_replace_fsync_failure_is_unknown_not_false_no_write(shell_agent, monkeypatch):
    agent, _ = shell_agent
    _, raw, info = runtime_config.read_json(agent.config_path)
    value = json.loads(raw)
    value['project_runtimes'] = {'fixture': {'mode': 'project_clean_v1'}}
    replacement = json.dumps(value).encode()
    calls = 0
    def fail_after_replace(_):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError('synthetic directory flush failure')
    monkeypatch.setattr(runtime_config, '_sync_directory', fail_after_replace)
    with pytest.raises(DevError) as caught:
        runtime_config._replace_config(agent.config_path, raw, info, replacement, hashlib.sha256(raw).hexdigest())
    assert caught.value.code == 'DURABILITY_UNCONFIRMED'
    assert agent.config_path.read_bytes() == replacement
    backups = list(agent.config_path.parent.glob('*.runtime-backup-*'))
    assert len(backups) == 1 and backups[0].read_bytes() == raw


def test_cli_does_not_claim_not_applied_for_a_durability_failure(monkeypatch, capsys):
    def uncertain(*args, **kwargs):
        raise DevError('DURABILITY_UNCONFIRMED', 'synthetic', 409)
    monkeypatch.setattr(runtime_config, 'inspect_or_configure', uncertain)
    assert runtime_config.main(['configure', '--config', 'synthetic.json', '--root', '.', '--project', 'P', '--profile-json', 'profile.json']) == 2
    value = json.loads(capsys.readouterr().out)
    assert value['state'] == 'needs_review' and value['applied'] is None
    assert not value['outcome_confirmed']


def test_save_performs_all_fallible_snapshot_work_before_committing(shell_agent, monkeypatch):
    agent, root = shell_agent
    value = project(agent, root)
    snapshot = get(agent, value)
    committed = False
    original_snapshot = runtime_panel._snapshot
    original_replace = runtime_panel._replace_config
    def checked_snapshot(*args, **kwargs):
        assert not committed, 'A post-commit diagnostic could misreport a successful save'
        return original_snapshot(*args, **kwargs)
    def commit(*args, **kwargs):
        nonlocal committed
        result = original_replace(*args, **kwargs)
        committed = True
        return result
    monkeypatch.setattr(runtime_panel, '_snapshot', checked_snapshot)
    monkeypatch.setattr(runtime_panel, '_replace_config', commit)
    result = save(agent, value, snapshot, {'mode': 'project_clean_v1', 'path': ['tools']})
    assert committed and result['saved']
    assert json.loads(agent.config_path.read_text())['project_runtimes'][value['id']]['path'] == ['tools']


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_control_output_is_bounded_while_reading_not_after_buffering(tmp_path, stream):
    from agent.runtime_control import run_control
    target = tmp_path / 'must-not-exist'
    source = "import os,sys,time,pathlib; os.write(" + ('1' if stream == 'stdout' else '2') + ",b'x'*1000000);time.sleep(2);pathlib.Path(sys.argv[1]).write_text('bad')"
    with pytest.raises(DevError) as caught:
        run_control([sys.executable, '-c', source, str(target)], dict(os.environ), timeout=5, max_bytes=16384)
    assert caught.value.code == 'RUNTIME_CONTROL_OVERSIZED'
    assert not target.exists()


def test_control_timeout_reaps_its_process_and_preserves_real_nonzero_result(tmp_path):
    from agent.runtime_control import run_control
    result = run_control([sys.executable, '-c', "import sys;print('out');print('err',file=sys.stderr);sys.exit(7)"], dict(os.environ))
    assert result.returncode == 7 and result.stdout == b'out\n' and result.stderr == b'err\n'
    target = tmp_path / 'must-not-exist'
    with pytest.raises(DevError) as caught:
        run_control([sys.executable, '-c', "import sys,time,pathlib;time.sleep(2);pathlib.Path(sys.argv[1]).write_text('bad')", str(target)], dict(os.environ), timeout=.2)
    assert caught.value.code == 'RUNTIME_CONTROL_UNCERTAIN' and not target.exists()


@pytest.mark.asyncio
async def test_original_isolation_requirement_rejects_native_ssh_before_dependency_probe(shell_agent, monkeypatch):
    from tests.test_shell import shell_request
    agent, root = shell_agent
    value = {**shell_request(root)['project'], '_runtime_requirement': {
        'protocol': 1, 'driver': 'docker_restricted_v1', 'project_id': 'fixture', 'revision': 'a'*64}}
    def unexpected(*args, **kwargs):
        pytest.fail('Strict original runtime must reject native SSH before dependency or credential setup')
    monkeypatch.setattr('agent.ssh.prepare_ssh', unexpected)
    with pytest.raises(DevError) as caught:
        await agent.execute('strict-ssh-check', 'ssh_exec', value, {})
    assert caught.value.code == 'RUNTIME_REMOTE_TARGET_UNSUPPORTED'


def test_loaded_profile_is_not_applied_before_authenticated_reconnect(shell_agent):
    agent, root = shell_agent
    value = project(agent, root)
    initial = get(agent, value)
    assert initial['state'] == 'applied'
    agent.runtime_reload_pending = True
    pending = get(agent, value)
    assert pending['state'] == 'pending_reload'
    assert pending['revision'] == initial['revision']
    agent.runtime_reload_pending = False
    assert get(agent, value)['state'] == 'applied'


@pytest.mark.asyncio
async def test_saved_target_cannot_override_required_isolation(shell_agent):
    from shared.contracts import SSHExec
    agent, root = shell_agent
    value = project(agent, root)
    args = SSHExec.model_validate({'project': value['id'], 'host': 'fixture.invalid',
        'username': 'fixture', 'password': 'synthetic-not-a-real-password',
        'command': 'touch forbidden', 'idempotency_key': 'strict-saved-target'}).model_dump()
    value.update(_runtime_requirement={'protocol': 1}, _core_ssh=args)
    with pytest.raises(DevError) as error:
        await agent.execute('strict-saved-target', 'ssh_exec', value, args, saved_vps_target='vps:' + 'a' * 32)
    assert error.value.code == 'RUNTIME_REMOTE_TARGET_UNSUPPORTED'
    assert not (root / 'forbidden').exists()


def test_equivalent_default_binding_is_pending_until_exact_config_loaded(shell_agent):
    from tests.test_runtime_panel import reload
    from shared.util import atomic_json
    agent, root = shell_agent
    raw = json.loads(agent.config_path.read_text())
    raw.pop('project_runtimes', None)
    atomic_json(agent.config_path, raw)
    reload(agent)
    value = project(agent, root)
    initial = get(agent, value)
    assert initial['profile']['mode'] == 'project_clean_v1'
    saved = save(agent, value, initial, {'mode': 'project_clean_v1'})
    assert saved['saved'] and not saved['unchanged']
    assert saved['runtime']['revision'] == saved['saved_runtime']['revision']
    assert saved['state'] == 'pending_reload'
    assert get(agent, value)['state'] == 'pending_reload'
    reload(agent)
    assert get(agent, value)['state'] == 'applied'
