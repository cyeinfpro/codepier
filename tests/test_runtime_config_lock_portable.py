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
