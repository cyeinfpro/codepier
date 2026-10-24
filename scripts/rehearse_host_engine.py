#!/usr/bin/env python3
"""Authorized synthetic host-engine acceptance; never mount production state.

Phase one runs the unchanged installed launcher. Phase two preserves launcher
supervision and pinned engine code, with test-only local release transport and a
separately built candidate image replacing network discovery/download/build.
No systemd unit is installed. All Docker state uses the split rehearsal manifest.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

# This explicit source-tree import is confined to the approved test harness.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import panel_update_engine as engine
from scripts import panel_update_launcher as launcher
from scripts import panel_update_runtime as rt
from scripts.rehearse_split_state import Rehearsal


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def request(home, method, path, body=None):
    payload = b'' if body is None else json.dumps(body).encode()
    wire = (method + ' ' + path + ' HTTP/1.0\r\nHost: localhost\r\nContent-Length: ' +
            str(len(payload)) + '\r\nContent-Type: application/json\r\n\r\n').encode() + payload
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(home / 'run/updater.sock'))
        connection.sendall(wire)
        chunks = []
        while True:
            part = connection.recv(65536)
            if not part:
                break
            chunks.append(part)
            if sum(map(len, chunks)) > 1024 * 1024:
                raise RuntimeError('Fixture response exceeds limit')
    head, raw = b''.join(chunks).split(b'\r\n\r\n', 1)
    status = int(head.split(b' ')[1])
    value = json.loads(raw)
    if status >= 400:
        raise RuntimeError('Fixture HTTP request failed: ' + str(status) + ' ' + str(value))
    return value


def wait_for(process, predicate, seconds=120):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('Fixture supervisor exited: ' + str(process.returncode))
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError) as exc:
            last = type(exc).__name__
        time.sleep(0.2)
    raise TimeoutError('Fixture acceptance deadline: ' + str(last))


def stop_owned(process):
    """Only the new process group created by this harness is signalled."""
    if process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


def child(context_path, arguments):
    context = rt.read_json(context_path)
    root = Path(context['root'])
    home = root / '.codepier-updater'
    identifier = arguments[arguments.index('--engine-id') + 1]
    directory = home / 'engines' / identifier
    engine.verify_engine(home, rt.read_json(directory / 'identity.json'))
    sys.path.insert(0, str(directory))
    module = load_module('rehearsal_pinned_updater', directory / 'panel_updater.py')
    candidate = context['new_image']
    rehearsal = Rehearsal(context['id'], context['old_image'])
    base_runtime = module.DockerRuntime

    class FixtureRuntime(base_runtime):
        def __init__(self, *args, **kwargs):
            kwargs['execute'] = rehearsal.execute
            super().__init__(*args, **kwargs)

        def prepare(self, job, source):
            result = super().prepare(job, source)
            target_path = self.home / 'jobs' / job['id'] / 'target.json'
            target = rt.read_json(target_path)
            target['services']['hub']['image'] = candidate
            rt.save_compose(target_path, target)
            result['image'] = candidate
            return result

        def command(self, command, **kwargs):
            if command[:2] == ['docker', 'build']:
                # Parent separately builds and records this exact public source.
                return 'fixture: separately built verified candidate image reused'
            return super().command(command, **kwargs)

    module.DockerRuntime = FixtureRuntime
    module.latest_release = lambda repository: dict(context['release'])
    def fetch(url, *, destination, limit, **kwargs):
        data = Path(context['bundle']).read_bytes()
        if len(data) != limit or hashlib.sha256(data).hexdigest() != context['release']['sha256']:
            raise RuntimeError('Synthetic local release changed')
        Path(destination).write_bytes(data)
    module.fetch = fetch
    sys.argv = [str(directory / 'panel_updater.py')] + arguments
    return module.main()


def supervise_fixture(context_path):
    context = rt.read_json(context_path)
    original_command = launcher.child_command
    def command(root, home, identity, descriptor, **kwargs):
        exact = original_command(root, home, identity, descriptor, **kwargs)
        return [exact[0], '-I', '-B', str(Path(__file__).resolve()), '--child',
                str(context_path), '--'] + exact[4:]
    launcher.child_command = command
    return launcher.supervise(Path(context['root']))


def acceptance(args):
    if os.geteuid() != 0:
        raise RuntimeError('Explicitly authorized isolated root test host required')
    source = Path(args.source).resolve()
    bundle = Path(args.bundle).resolve()
    metadata = rt.read_json(source / 'RELEASE.json')
    rehearsal = Rehearsal(args.id, args.old_image)
    if args.cleanup:
        rehearsal.cleanup()
        return
    if rehearsal.manifest['volumes'] or rehearsal.manifest['results']:
        raise RuntimeError('Existing test must be inspected, not replayed')
    # Both images must already exist; no pull, registry login or network setup.
    for image in (args.old_image, args.new_image):
        rehearsal.execute(['docker', 'image', 'inspect', image], timeout=15)
    manager, runtime, setup_job, original_volume = rehearsal.fixture('host')
    manager.save(setup_job, state='failed', phase='fixture-only',
                 message='Synthetic fixture initialization only; migration not executed')
    home = manager.home
    service = home / 'service'; service.mkdir(mode=0o700)
    for name in (*engine.ENGINE_FILES, 'panel_update_engine.py', 'panel_update_launcher.py'):
        rt.atomic_bytes(service / name, (source / 'scripts' / name).read_bytes())
    config = rt.read_json(home / 'config.json'); config['engine_launcher'] = 1
    rt.atomic_json(home / 'config.json', config)
    bootstrap = engine.stage_bootstrap(home, source)
    rt.atomic_json(home / 'engine-current.json', bootstrap)
    release = {'repository': config['repository'], 'version': metadata['version'],
               'release_id': 900000001, 'asset_id': 900000002,
               'sha256': engine.digest_file(bundle), 'bytes': bundle.stat().st_size,
               'url': 'https://github.com/cyeinfpro/codepier/releases/download/v' + metadata['version'] +
                      '/codepier-' + metadata['version'] + '-source.zip',
               'checked_at': time.time(), 'notes': 'Synthetic local release boundary', 'published_at': ''}
    context = {'id': args.id, 'root': str(manager.root), 'old_image': args.old_image,
               'new_image': args.new_image, 'source': str(source), 'bundle': str(bundle), 'release': release}
    context_path = rehearsal.root / 'host-context.json'; rt.atomic_json(context_path, context)
    log = (rehearsal.root / 'host-launcher.log').open('ab')
    process = None
    try:
        process = subprocess.Popen([sys.executable, '-B', str(service / 'panel_update_launcher.py'),
                                    '--root', str(manager.root)], cwd=service, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        wait_for(process, lambda: request(home, 'GET', '/status').get('host_engine', {}).get('ready'))
        stop_owned(process)
        phase_one = {'scenario': 'unchanged-launcher-ready', 'state': 'succeeded',
                     'actual_root_launcher': True, 'at': time.time()}
        rehearsal.manifest['results'].append(phase_one); rehearsal.save()
        print(json.dumps(phase_one), flush=True)
        process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()),
                                    '--supervise', str(context_path)], cwd=source, stdout=log,
                                   stderr=subprocess.STDOUT, start_new_session=True)
        wait_for(process, lambda: request(home, 'GET', '/status').get('host_engine', {}).get('ready'))
        old_version = rt.read_json(home / 'current.json')['version']
        base = {'current_version': old_version, 'actor': 'synthetic-panel'}
        request(home, 'POST', '/check', {**base, 'idempotency_key': 'host-check-' + args.id})
        def checked():
            value = request(home, 'GET', '/status')
            op = value.get('operation', {})
            if op.get('state') in ('failed', 'recovery_required'):
                raise RuntimeError('Synthetic check failed')
            return op.get('state') == 'succeeded'
        wait_for(process, checked)
        accepted = request(home, 'POST', '/apply', {
            **base, 'idempotency_key': 'host-apply-' + args.id,
            **{key: release[key] for key in ('version', 'release_id', 'sha256')}})
        job_id = accepted['operation']['id']
        def applied_and_ready():
            value = request(home, 'GET', '/status')
            op = value.get('operation', {})
            if op.get('state') in ('failed', 'rolled_back', 'recovery_required'):
                raise RuntimeError('Synthetic apply failed: ' + str(op.get('error')))
            selected = rt.read_json(home / 'engine-current.json')
            ready = rt.read_json(home / 'engine-ready.json', {})
            return (op.get('id') == job_id and op.get('state') == 'succeeded' and
                    selected['id'] != bootstrap['id'] and ready.get('id') == selected['id'])
        wait_for(process, applied_and_ready, seconds=240)
        job = rt.read_json(home / 'jobs' / job_id / 'status.json')
        # Reload because the child owns Docker-manifest writes during apply.
        rehearsal.manifest = rt.read_json(rehearsal.manifest_path)
        before = rehearsal.probe(original_volume)
        after = rehearsal.probe(job['new_volume'])
        assert before['attachment'] and before['archive']
        assert not after['attachment'] and not after['archive']
        assert before['proof'] == after['proof'] and before['native'] == after['native']
        effective = rt.load_compose_config(rehearsal.execute(
            ['docker', 'compose', 'config', '--format', 'json'], cwd=manager.root))
        assert rt.data_volume(effective)[1] == job['new_volume']
        result = {'scenario': 'panel-default-split-engine-promotion', 'state': 'succeeded',
                  'job': job_id, 'actual_docker': True, 'actual_launcher_supervision': True,
                  'local_release_transport_fixture': True, 'candidate_image_reused': args.new_image,
                  'original_assets_preserved': True, 'new_engine_ready': True, 'at': time.time()}
        rehearsal.manifest['results'].append(result); rehearsal.save()
        print(json.dumps(result), flush=True)
    finally:
        if process is not None:
            stop_owned(process)
        log.close()


def main():
    # Test-only shims are this harness, never installed production endpoints.
    if len(sys.argv) > 2 and sys.argv[1] == '--child':
        return child(Path(sys.argv[2]), sys.argv[4:])
    if len(sys.argv) == 3 and sys.argv[1] == '--supervise':
        return supervise_fixture(Path(sys.argv[2]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--id', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--old-image', required=True)
    parser.add_argument('--new-image', required=True)
    parser.add_argument('--cleanup', action='store_true')
    acceptance(parser.parse_args())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
