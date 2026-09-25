"""Key rotation exercises only temporary stores, including interruption points."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import zipfile

from cryptography.fernet import Fernet, InvalidToken
import pytest

from hub import keyring
from hub.store import Store
from shared.instance_lock import InstanceLock


def populated(directory: Path):
    store = Store(directory)
    try:
        store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('d','fixture',?,?)", (store.encrypt('fixture-device-key'), time.time()))
        store.execute("INSERT INTO operations(id,actor,tool,args_summary,fingerprint,state,created,updated,payload) VALUES ('o','fixture','fs_read','{}','test','queued',?,?,?)", (time.time(), time.time(), store.encrypt('{"path":"fixture.py"}')))
        store.execute("INSERT INTO vps_connections(id,name,name_key,host,port,username,secret,created,updated) VALUES ('v','fixture','fixture','vps.invalid',22,'fixture',?,?,?)", (store.encrypt('fixture-password'), time.time(), time.time()))
        return (directory / 'master.key').read_bytes()
    finally:
        store.close()


def assert_readable(directory):
    store = Store(directory)
    try:
        assert store.decrypt(store.one("SELECT secret FROM devices WHERE id='d'")['secret']) == 'fixture-device-key'
        assert store.decrypt(store.one("SELECT payload FROM operations WHERE id='o'")['payload']) == '{"path":"fixture.py"}'
        assert store.decrypt(store.one("SELECT secret FROM vps_connections WHERE id='v'")['secret']) == 'fixture-password'
    finally:
        store.close()


def test_normal_startup_keeps_legacy_format_and_missing_key_fails_closed(tmp_path):
    directory = tmp_path / 'hub'
    old = populated(directory)
    assert not (directory / keyring.RING_FILE).exists()
    with sqlite3.connect(directory / 'hub.sqlite3') as db:
        ciphertext = db.execute("SELECT secret FROM devices").fetchone()[0]
    assert Fernet(old).decrypt(ciphertext.encode()) == b'fixture-device-key'
    (directory / 'master.key').unlink()
    with pytest.raises(RuntimeError, match='missing'):
        Store(directory)
    assert not (directory / 'master.key').exists()


def test_rotation_changes_all_ciphertexts_retains_recoverable_backup_and_can_repeat(tmp_path):
    directory = tmp_path / 'hub'
    original = populated(directory)
    result = keyring.rotate_key(directory)
    assert result['status'] == 'completed' and result['records'] == 3
    assert (directory / 'master.key').read_bytes() != original
    assert_readable(directory)
    backup = directory / result['backup']
    assert_readable(backup)
    with sqlite3.connect(directory / 'hub.sqlite3') as db:
        for table, column in keyring.CIPHER_COLUMNS:
            value = db.execute(f'SELECT {column} FROM {table}').fetchone()[0]
            assert value.startswith('cp1:' + result['key_id'] + ':')
            with pytest.raises(InvalidToken):
                Fernet(original).decrypt(value.split(':', 2)[-1].encode())
    ring = json.loads((directory / keyring.RING_FILE).read_text())
    assert list(ring['keys']) == [result['key_id']]
    second = keyring.rotate_key(directory)
    assert second['key_id'] != result['key_id']
    assert_readable(directory)
    assert_readable(directory / second['backup'])
    assert keyring.rotate_key(directory, resume=True)['status'] == 'already_completed'


@pytest.mark.parametrize('stage', ['prepared', 'keyring_published', 'database_committed', 'legacy_key_replaced', 'complete'])
def test_every_published_interruption_stage_remains_readable_and_resumable(tmp_path, monkeypatch, stage):
    directory = tmp_path / 'hub'
    populated(directory)
    original = keyring._stage
    class SimulatedPowerLoss(BaseException):
        pass
    def fault(path, journal, current):
        original(path, journal, current)
        if current == stage:
            raise SimulatedPowerLoss()
    monkeypatch.setattr(keyring, '_stage', fault)
    with pytest.raises(SimulatedPowerLoss):
        keyring.rotate_key(directory)
    assert_readable(directory)
    monkeypatch.setattr(keyring, '_stage', original)
    if stage != 'complete':
        with pytest.raises(RuntimeError, match='--resume'):
            keyring.rotate_key(directory)
    result = keyring.rotate_key(directory, resume=True)
    assert result['status'] in {'completed', 'already_completed'}
    assert_readable(directory)
    assert_readable(directory / result['backup'])


def test_unknown_cipher_version_or_key_fails_closed(tmp_path):
    store = Store(tmp_path / 'hub')
    try:
        for value in ('cp2:unknown:invalid', 'cp1:unknown:invalid', 'cp1:malformed'):
            with pytest.raises(InvalidToken):
                store.decrypt(value)
    finally:
        store.close()


def test_running_hub_and_backup_exclusion_locks_reject_rotation(tmp_path):
    directory = tmp_path / 'hub'
    populated(directory)
    for name in ('.hub.lock', '.keys.lock'):
        lock = InstanceLock(directory / name)
        try:
            with pytest.raises((RuntimeError, OSError)):
                keyring.rotate_key(directory)
        finally:
            lock.close()
    assert not (directory / keyring.JOURNAL_FILE).exists()
    assert_readable(directory)


def test_backup_contains_exact_current_key_generation_and_restores(tmp_path):
    from hub.__main__ import write_backup
    directory = tmp_path / 'hub'
    populated(directory)
    keyring.rotate_key(directory)
    store = Store(directory)
    archive = tmp_path / 'backup.zip'
    try:
        lock = InstanceLock(directory / '.keys.lock')
        try:
            with pytest.raises((RuntimeError, OSError)):
                write_backup(store, archive)
            assert not archive.exists()
        finally:
            lock.close()
        write_backup(store, archive)
    finally:
        store.close()
    restored = tmp_path / 'restored'
    restored.mkdir(mode=0o700)
    with zipfile.ZipFile(archive) as zipped:
        assert set(zipped.namelist()) == {'hub.sqlite3', 'master.key', keyring.RING_FILE}
        for name in zipped.namelist():
            keyring.atomic_private(restored / name, zipped.read(name))
    assert_readable(restored)


def test_rotation_rejects_symlink_and_malformed_metadata_without_replacement(tmp_path):
    directory = tmp_path / 'hub'
    old = populated(directory)
    bad = directory / keyring.RING_FILE
    keyring.atomic_private(bad, b'{"version":9}')
    with pytest.raises(RuntimeError, match='Invalid encryption keyring'):
        keyring.rotate_key(directory)
    assert (directory / 'master.key').read_bytes() == old
    assert bad.read_bytes() == b'{"version":9}'
    bad.unlink()
    # File symlinks do not need Windows directory-symlink privileges.
    if os.name != 'nt':
        target = tmp_path / 'untouched'; target.write_bytes(b'untouched')
        bad.symlink_to(target)
        with pytest.raises(RuntimeError, match='regular file'):
            keyring.rotate_key(directory)
        assert target.read_bytes() == b'untouched'


def test_rekey_cli_and_bad_port_have_readable_fail_fast_errors(tmp_path):
    directory = tmp_path / 'hub'
    populated(directory)
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, '-m', 'hub', '--data-dir', str(directory), 'rekey'], cwd=root, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'completed'
    assert_readable(directory)
    env = {**os.environ, 'HUB_PORT': 'not-a-number'}
    result = subprocess.run([sys.executable, '-m', 'hub', '--data-dir', str(tmp_path / 'bad'), 'run'], cwd=root, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert 'Traceback' not in result.stderr
    assert not (tmp_path / 'bad').exists()
