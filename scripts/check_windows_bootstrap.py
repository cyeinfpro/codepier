#!/usr/bin/env python3
"""Run the real PowerShell bootstrap against owned, synthetic installer fixtures.

No network requests, UAC, scheduled tasks, real Agent configs, or model calls.
The called Python installer is a recorder; this validates shell argument/error
behavior, while Python installer/service correctness has separate tests.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def probe(directory):
    directory = Path(directory)
    (directory / 'deploy').mkdir();(directory / 'scripts').mkdir()
    script = directory / 'deploy/install-from-hub.ps1'
    shutil.copyfile(ROOT / 'deploy/install-from-hub.ps1', script)
    helper = directory / 'scripts/install_agent.py'
    helper.write_text("import json,os,sys,shutil\nfrom pathlib import Path\n"
                     "Path(os.environ['FIXTURE_LOG']).write_text(json.dumps(sys.argv[1:]),encoding='utf-8')\n"
                     "if '--uninstall' in sys.argv and not os.getenv('FIXTURE_KEEP'): shutil.rmtree(sys.argv[sys.argv.index('--install-dir')+1])\n"
                     "raise SystemExit(int(os.getenv('FIXTURE_EXIT','0')))\n", encoding='utf-8')
    executable = shutil.which('powershell.exe')
    if executable is None:
        raise RuntimeError('Windows PowerShell 5.1 is required')
    results = []
    for index, (action, extra, exit_code, should_call, keep) in enumerate([
        ('status', [], 0, True, False), ('start', [], 0, True, False),
        ('upgrade', [], 0, True, False), ('uninstall', [], 0, True, False),
        ('start', [], 37, True, False), ('uninstall', [], 0, True, True),
        ('invalid-action', [], 0, False, False), ('start', ['-NoService'], 0, False, False),
        ('start', ['-Hub', 'https://invalid.example'], 0, False, False),
    ]):
        base = directory / (f'fixture-{index} & cash $ [test]')
        base.mkdir();config = base / 'config.json';config.write_bytes(b'{"synthetic":true}')
        log = directory / f'args-{index}.json'
        env = {**os.environ, 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', ''),
               'FIXTURE_LOG': str(log), 'FIXTURE_EXIT': str(exit_code), 'PYTHONUTF8': '1'}
        if keep: env['FIXTURE_KEEP'] = '1'
        else: env.pop('FIXTURE_KEEP', None)
        result = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', str(script), '-Action', action, '-InstallDir', str(base), '-ExpectedDevice', 'fixture-device', *extra],
            cwd=directory, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        successful = should_call and exit_code == 0 and not keep
        if (result.returncode == 0) != successful or log.exists() != should_call:
            raise AssertionError(f'Bootstrap case {index} returned unexpected result: {result.returncode}; '
                                 f'stdout={result.stdout[-4000:]!r}; stderr={result.stderr[-4000:]!r}')
        if should_call:
            arguments = json.loads(log.read_text(encoding='utf-8'))
            assert arguments == ['--start-service' if action == 'start' else '--' + action,
                                 '--install-dir', str(base), '--expected-device', 'fixture-device']
        if action != 'uninstall' or keep:
            assert config.read_bytes() == b'{"synthetic":true}'
        else:
            assert not base.exists()
        if keep:
            assert b'cleanup did not complete' in result.stderr
        results.append({'case': index, 'action': action, 'passed': True, 'exit_code': result.returncode})
    return {'passed': True, 'cases': results, 'real_powershell': True,
            'python_installer': 'synthetic recorder; no services or network used'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Run this behavior probe on a Windows CI runner')
    with tempfile.TemporaryDirectory(prefix='codepier-bootstrap-test-') as temporary:
        result = probe(temporary)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
