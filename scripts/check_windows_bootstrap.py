#!/usr/bin/env python3
"""Run the real PowerShell bootstrap against owned, synthetic installer fixtures.

Only loopback HTTP fixtures; no external requests, UAC, scheduled tasks, real Agent configs, or model calls.
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


def probe_download(directory, executable):
    """Validate native PowerShell here-string/file/argument behavior on loopback."""
    import hashlib
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    directory = Path(directory)
    payload = b"synthetic Agent package" * 4096
    sha = hashlib.sha256(payload).hexdigest()
    bootstrap = (ROOT / "deploy/install-from-hub.ps1").read_text(encoding="utf-8")
    block = bootstrap.split("  $CodePierDownload = @'", 1)[1].split(
        "  if ($LASTEXITCODE -ne 0) { throw 'Panel Agent package download", 1)[0]
    wrapper = directory / "download probe [test].ps1"
    wrapper.write_text(
        "param([string]$CodePierPython,[string]$CodePierTemp,[string]$Hub,[string]$Sha256,[string]$Archive)\n"
        "$ErrorActionPreference = 'Stop'\n$DownloadTimeoutSec = 5\n$DownloadAttempts = 2\n"
        "$CodePierDownload = @'" + block + "exit $LASTEXITCODE\n", encoding="utf-8")
    state = {"mode": "retry", "requests": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"] += 1
            assert self.path == "/agent/agent.zip?sha256=" + sha
            assert self.headers.get("Authorization") is None and self.headers.get("Range") is None
            status = 503 if state["mode"] == "retry" and state["requests"] == 1 else 200
            body = payload if state["mode"] == "retry" else b"wrong-package"
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", '"' + sha + '"')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    results = []
    try:
        for mode in ("retry", "bad-sha"):
            state.update(mode=mode, requests=0)
            archive = directory / (mode + " Agent [test].zip")
            result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(wrapper), "-CodePierPython", sys.executable, "-CodePierTemp", str(directory),
                "-Hub", f"http://127.0.0.1:{server.server_port}", "-Sha256", sha, "-Archive", str(archive)],
                capture_output=True, timeout=30)
            if mode == "retry":
                assert result.returncode == 0, (result.stdout[-4000:], result.stderr[-4000:])
                assert archive.read_bytes() == payload and state["requests"] == 2
            else:
                assert result.returncode != 0 and not archive.exists()
                assert state["requests"] == 1 and b"checksum mismatch" in result.stderr
            assert not archive.with_name(archive.name + ".part").exists()
            results.append({"case": mode, "passed": True, "requests": state["requests"],
                            "exit_code": result.returncode})
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    return results


def probe_archive_handoff(directory, executable):
    """Real PowerShell and filesystem, synthetic data; no service/API calls."""
    import hashlib
    directory=Path(directory)
    source=(ROOT/'deploy/install-from-hub.ps1').read_text(encoding='utf-8')
    block=source[source.index('function Get-CodePierSHA256 {'):source.index("if ($Action -ne 'install' -and -not $Hub) {")]
    script=directory/'archive-finalize.ps1'
    script.write_text("param([string]$PlanPath,[string]$Source)\n$ErrorActionPreference='Stop'\n"+block+
                      "Complete-CodePierArchive -PlanPath $PlanPath -Source $Source\n",encoding='utf-8')
    results=[]
    for case in ('success','existing-target','changed-config','wrong-lock','missing-snapshot','foreign-parent'):
        base=directory/('archive-'+case)
        base.mkdir()
        (base/'config.json').write_bytes(b'{"synthetic":true}')
        lock=base/'.install.lock';lock.mkdir()
        token='b'*32
        (lock/'archive.json').write_text(json.dumps({'schema':1,'lock_token':token}))
        target=base.with_name(base.name+'.recovery-20000101-000000-01234567')
        files={'config.json':hashlib.sha256((base/'config.json').read_bytes()).hexdigest(),
               'management.json':None,'service.xml':None}
        plan={'schema':1,'source':str(base),'backup':str(target),'lock_token':token,'files':files}
        if case=='existing-target':
            target.mkdir();(target/'keep.txt').write_text('preserve')
        elif case=='changed-config':
            (base/'config.json').write_bytes(b'{"edited":true}')
        elif case=='wrong-lock':
            plan['lock_token']='c'*32
        elif case=='missing-snapshot':
            del plan['files']['config.json']
        elif case=='foreign-parent':
            plan['backup']=str(directory.parent/target.name)
        handoff=directory/(case+'.json');handoff.write_text(json.dumps(plan))
        result=subprocess.run([executable,'-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass',
                               '-File',str(script),'-PlanPath',str(handoff),'-Source',str(base)],
                              capture_output=True,timeout=30)
        assert (result.returncode==0)==(case=='success'), (case,result.stdout[-4000:],result.stderr[-4000:])
        if case=='success':
            assert not base.exists() and (target/'config.json').read_bytes()==b'{"synthetic":true}'
            assert not (target/'.install.lock').exists()
        else:
            assert base.exists()
            if case=='existing-target':
                assert (target/'keep.txt').read_text()=='preserve'
                assert not (target/base.name).exists()
        results.append({'case':case,'passed':True,'exit_code':result.returncode})
    return results


def probe(directory):
    directory = Path(directory)
    (directory / 'deploy').mkdir();(directory / 'scripts').mkdir()
    script = directory / 'deploy/install-from-hub.ps1'
    shutil.copyfile(ROOT / 'deploy/install-from-hub.ps1', script)
    helper = directory / 'scripts/install_agent.py'
    helper.write_text("import hashlib,json,os,sys,shutil\nfrom pathlib import Path\nPath(os.environ['FIXTURE_LOG']).write_text(json.dumps(sys.argv[1:]),encoding='utf-8')\nbase=Path(sys.argv[sys.argv.index('--install-dir')+1])\nif '--uninstall' in sys.argv and not os.getenv('FIXTURE_KEEP'): shutil.rmtree(base)\nif '--archive-install' in sys.argv and not int(os.getenv('FIXTURE_EXIT','0')):\n    lock=base/'.install.lock';lock.mkdir()\n    token='a'*32\n    (lock/'archive.json').write_text(json.dumps({'schema':1,'lock_token':token}))\n    target=base.with_name(base.name+'.recovery-20000101-000000-01234567')\n    files={name:hashlib.sha256((base/name).read_bytes()).hexdigest() if (base/name).exists() else None for name in ('config.json','management.json','service.xml')}\n    plan={'schema':1,'source':str(base),'backup':str(target),'lock_token':token,'files':files}\n    Path(sys.argv[sys.argv.index('--archive-handoff-file')+1]).write_text(json.dumps(plan))\nraise SystemExit(int(os.getenv('FIXTURE_EXIT','0')))\n", encoding='utf-8')
    executable = shutil.which('powershell.exe')
    if executable is None:
        raise RuntimeError('Windows PowerShell 5.1 is required')
    results = []
    # Native PowerShell must fail before it creates tool directories, downloads
    # Python or calls the synthetic installer when a fresh-node ticket targets
    # a machine that already belongs to another panel node.
    existing = directory / 'existing-node'
    existing.mkdir()
    existing_config = existing / 'config.json'
    original = json.dumps({'name': 'Original Agent', 'device_id': 'original-node',
                           'hub_url': 'https://hub.invalid', 'secret': 'keep-private'})
    existing_config.write_text(original, encoding='utf-8')
    allow = directory / 'allowed'
    allow.mkdir()
    mismatch = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', str(script), '-Action', 'install', '-InstallDir', str(existing),
        '-Hub', 'https://hub.invalid', '-Token', 'rdi_fixture', '-Sha256', 'a' * 64,
        '-AllowRoot', str(allow), '-ExpectedDevice', 'different-node'],
        cwd=directory, capture_output=True, timeout=30)
    assert mismatch.returncode != 0, mismatch.stdout
    assert b'ticket was not used' in mismatch.stderr, mismatch.stderr
    assert existing_config.read_text(encoding='utf-8') == original
    assert not (existing / 'tools').exists()
    results.append({'case': 'wrong-device-early-rejection', 'passed': True,
                    'exit_code': mismatch.returncode})
    for index, (action, extra, exit_code, should_call, keep) in enumerate([
        ('status', [], 0, True, False), ('start', [], 0, True, False),
        ('upgrade', [], 0, True, False), ('uninstall', [], 0, True, False),
        ('archive', [], 0, True, False),
        ('start', [], 37, True, False), ('uninstall', [], 0, True, True),
        ('invalid-action', [], 0, False, False), ('start', ['-NoService'], 0, False, False),
        ('start', ['-Hub', 'https://invalid.example'], 0, False, False),
    ]):
        base = directory / (f'fixture-{index} & cash $ [test]')
        base.mkdir();config = base / 'config.json';config.write_bytes(b'{"synthetic":true,"device_id":"fixture-device"}')
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
            prefix = ['--start-service' if action == 'start' else '--archive-install' if action == 'archive' else '--' + action,
                      '--install-dir', str(base), '--expected-device', 'fixture-device']
            # PowerShell canonicalizes the absolute -InstallDir with .NET on
            # Windows. Compare the resolved path, not its spelling/case; all
            # flags and the exact target device must still match byte-for-byte.
            assert len(arguments) >= len(prefix), (index, action, arguments)
            assert arguments[:2] == prefix[:2], (index, action, arguments, prefix)
            assert Path(arguments[2]).resolve() == base.resolve(), (index, action, arguments[2], str(base))
            assert arguments[3:len(prefix)] == prefix[3:], (index, action, arguments, prefix)
            if action == 'archive':
                assert arguments[-2] == '--archive-handoff-file'
                assert not Path(arguments[-1]).is_relative_to(base)
            else:
                assert len(arguments) == len(prefix), (index, action, arguments, prefix)
        if action == 'archive':
            target=base.with_name(base.name+'.recovery-20000101-000000-01234567')
            assert not base.exists() and (target/'config.json').read_bytes() == b'{"synthetic":true,"device_id":"fixture-device"}'
            assert not (target/'.install.lock').exists()
        elif action != 'uninstall' or keep:
            assert config.read_bytes() == b'{"synthetic":true,"device_id":"fixture-device"}'
        else:
            assert not base.exists()
        if keep:
            assert b'cleanup did not complete' in result.stderr
        results.append({'case': index, 'action': action, 'passed': True, 'exit_code': result.returncode})
    return {'passed': True, 'cases': results, 'real_powershell': True,
            'download_cases': probe_download(directory, executable),
            'archive_cases': probe_archive_handoff(directory, executable),
            'python_installer': 'synthetic recorder; no services or external network used'}


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
