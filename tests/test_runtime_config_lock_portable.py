"""Real POSIX/Windows lock ownership; no Docker, GUI, network or administrator access."""
import json
import os
import subprocess
import sys
import pytest
from agent.runtime_config_lock import config_lock, process_exists


def test_runtime_lock_current_process_exclusive_and_reusable(tmp_path):
    path = tmp_path / 'config.json'
    marker = tmp_path / 'config.json.runtime.lock'
    assert process_exists(os.getpid()) is True
    with config_lock(path):
        before = marker.read_bytes()
        with pytest.raises(FileExistsError):
            with config_lock(path):
                pytest.fail('Second writer entered the same configuration guard')
        assert marker.read_bytes() == before
    assert not marker.exists()
    with config_lock(path):
        assert json.loads(marker.read_text())['pid'] == os.getpid()


def test_runtime_lock_exited_process_recovery_is_real_and_cross_platform(tmp_path):
    path = tmp_path / 'config.json'
    script = "from agent.runtime_config_lock import config_lock;import sys,os;lock=config_lock(sys.argv[1]);lock.__enter__();os._exit(7)"
    result = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True, timeout=10)
    assert result.returncode == 7, result.stderr
    marker = tmp_path / 'config.json.runtime.lock'
    original = json.loads(marker.read_text())
    assert process_exists(original['pid']) is False
    with config_lock(path):
        current = json.loads(marker.read_text())
        assert current['nonce'] != original['nonce'] and current['pid'] == os.getpid()
    assert not marker.exists()



def test_config_binary_bytes_and_descriptor_identity_are_portable(tmp_path):
    from agent.runtime_config import read_json
    path = tmp_path / 'config.json'
    raw = b'\xef\xbb\xbf{\r\n  "project": "fixture"\r\n}\r\n'
    path.write_bytes(raw)
    value, actual, info = read_json(path)
    assert value == {'project': 'fixture'} and actual == raw
    assert info.st_size == len(raw)


def test_path_timestamp_representation_does_not_invalidate_same_open_file(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace
    from agent.runtime_config import read_json
    path = tmp_path / 'config.json'; path.write_text('{"value": 1}')
    original = Path.lstat
    def distinct_time(self):
        info = original(self)
        return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino, st_mode=info.st_mode,
            st_size=info.st_size, st_mtime_ns=info.st_mtime_ns+1, st_ctime_ns=info.st_ctime_ns+1)
    monkeypatch.setattr(Path, 'lstat', distinct_time)
    assert read_json(path)[0] == {'value': 1}
