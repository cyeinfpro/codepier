"""Regress private updater extraction and the non-root image boot boundary."""
import os
from pathlib import Path
import shlex
import stat
import socket
import sys

import pytest

from scripts.panel_update_runtime import STARTUP_PREFLIGHT
import subprocess

from scripts import panel_update_source as source
from tests.test_panel_update import bundle_bytes, release_for

ROOT = Path(__file__).resolve().parents[1]
SOURCE_PATHS = ['hub', 'shared', 'web', 'agent', 'scripts', 'deploy',
                'requirements-agent.txt', 'LICENSE']


def test_private_updater_context_is_normalized_without_opening_runtime_data(tmp_path):
    def add_nested(files):
        files['hub/gateway/service.py'] = b'# nested regression fixture\n'
        files['scripts/fixture.sh'] = b'#!/bin/sh\n'
    raw = bundle_bytes(change=add_nested)
    archive = tmp_path / 'source.zip'
    archive.write_bytes(raw)
    previous = os.umask(0o077)
    try:
        root = source.unpack_bundle(archive, tmp_path / 'source', release_for(raw))
    finally:
        os.umask(previous)
    nested = root / 'hub/gateway'
    assert stat.S_IMODE(nested.stat().st_mode) == 0o700
    executable = root / 'scripts/fixture.sh'
    executable.chmod(0o700)
    private = root / 'data'
    private.mkdir(mode=0o700)
    key = private / 'master.key'
    key.write_text('synthetic')
    key.chmod(0o600)
    recipe = (ROOT / 'Dockerfile').read_text().replace('\\\n', '')
    normalizers = [line.removeprefix('RUN ') for line in recipe.splitlines()
                   if line.startswith('RUN chmod -R ')]
    assert len(normalizers) == 1, 'Image must normalize private extraction modes'
    argv = shlex.split(normalizers[0])
    assert argv == ['chmod', '-R', 'a=rX,u+w', *SOURCE_PATHS]
    subprocess.run(argv, cwd=root, check=True, timeout=10)
    for name in SOURCE_PATHS:
        path = root / name
        entries = [path, *path.rglob('*')] if path.is_dir() else [path]
        for entry in entries:
            expected = 0o755 if entry.is_dir() or entry == executable else 0o644
            assert stat.S_IMODE(entry.stat().st_mode) == expected, entry
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


def test_image_import_check_runs_after_switching_to_non_root():
    recipe = (ROOT / 'Dockerfile').read_text()
    user = recipe.index('USER 10001:10001')
    assert recipe.index('RUN chmod -R ') < user
    assert recipe.index('RUN python -c "from hub.app import create_app"') > user
    for path in SOURCE_PATHS:
        assert 'COPY --chown=root:root ' + path + ' ' in recipe

def run_startup_probe(tmp_path, command=None, *, short_timeout=False, fixture_ready=False):
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    program = STARTUP_PREFLIGHT.replace('18765', str(port))
    if command is not None:
        original = "[sys.executable, '-m', 'hub', 'run', '--host',\n                                '127.0.0.1', '--port', '" + str(port) + "']"
        assert original in program
        program = program.replace(original, repr(command))
    if short_timeout:
        program = program.replace('time.monotonic() + 30', 'time.monotonic() + 0.5')
        program = program.replace('wait(timeout=5)', 'wait(timeout=0.5)')
    if fixture_ready:
        # Synthetic candidates must reach the condition under test before its
        # deadline starts. Keep this bounded setup inside the real try/finally
        # so failed setup still terminates and reaps the candidate.
        anchor = '        deadline = time.monotonic()'
        assert program.count(anchor) == 1
        preparation = """        ready_deadline = time.monotonic() + 60
        while not Path(environment['FIXTURE_READY_FILE']).exists():
            if process.poll() is not None or time.monotonic() >= ready_deadline:
                log.seek(0, 2)
                log.seek(max(0, log.tell()-4096))
                raise RuntimeError('Synthetic candidate did not become ready: '+log.read().decode(errors='replace'))
            time.sleep(0.05)
"""
        program = program.replace(anchor, preparation + anchor)
    scratch = tmp_path / 'tmp'
    scratch.mkdir()
    environment = dict(os.environ, TMPDIR=str(scratch), HUB_DATA_DIR=str(tmp_path / 'do-not-use'),
                       HUB_STATE_DIR=str(tmp_path / 'do-not-use-state'),
                       FIXTURE_READY_FILE=str(tmp_path / 'fixture-ready'))
    result = subprocess.run([sys.executable, '-c', program], cwd=ROOT, env=environment,
                            text=True, capture_output=True, timeout=110 if fixture_ready else 45)
    assert not list(scratch.iterdir()), 'Preflight must remove its disposable state'
    assert not (tmp_path / 'do-not-use').exists()
    assert not (tmp_path / 'do-not-use-state').exists()
    return result


def test_real_hub_boots_with_disposable_data(tmp_path):
    result = run_startup_probe(tmp_path)
    assert result.returncode == 0, result.stderr


def test_real_startup_import_failure_is_not_metadata_success(tmp_path):
    result = run_startup_probe(tmp_path, [sys.executable, '-c', "import missing_gateway_fixture"])
    assert result.returncode != 0
    assert 'Candidate Hub exited during startup' in result.stderr
    assert 'missing_gateway_fixture' in result.stderr


def test_updater_boot_acceptance_is_required_in_ci():
    workflow = (ROOT / '.github/workflows/ci.yml').read_text()
    assert 'Boot Hub from private updater extraction' in workflow
    assert 'previous = os.umask(0o077)' in workflow
    assert 'unpack_bundle(' in workflow
    assert 'runtime.build(job, source)' in workflow

@pytest.mark.parametrize('ignore_termination', [False, True])
def test_unhealthy_candidate_is_bounded_and_reaped(tmp_path, ignore_termination):
    pid_file = tmp_path / 'pid'
    # Deliberately exceed the short validation deadline before signalling ready.
    child = ("import os,signal,time; from pathlib import Path; time.sleep(0.75); "
             + ("signal.signal(signal.SIGTERM, signal.SIG_IGN); " if ignore_termination else "")
             + f"Path({str(pid_file)!r}).write_text(str(os.getpid())); "
             + "Path(os.environ['FIXTURE_READY_FILE']).touch(); time.sleep(600)")
    result = run_startup_probe(tmp_path, [sys.executable, '-c', child],
                               short_timeout=True, fixture_ready=True)
    assert result.returncode != 0
    assert 'startup health check timed out' in result.stderr
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_wrong_version_health_is_rejected(tmp_path):
    child = """import json,os
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit
from pathlib import Path
class Server(HTTPServer):
    def service_actions(self):
        Path(os.environ['FIXTURE_READY_FILE']).touch()
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({'status':'ok','version':'wrong-version'}).encode())
Server(('127.0.0.1',urlsplit(os.environ['HUB_PUBLIC_URL']).port),Handler).serve_forever(poll_interval=0.05)
"""
    result = run_startup_probe(tmp_path, [sys.executable, '-c', child], fixture_ready=True)
    assert result.returncode != 0
    assert 'startup health/version mismatch' in result.stderr
