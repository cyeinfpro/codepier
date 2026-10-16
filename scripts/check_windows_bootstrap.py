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


def synthetic_bootstrap(source):
    """Replace only read-only live Task Scheduler checks in disposable fixtures."""
    start = source.index('function Assert-CodePierUninstallTasksAbsent {')
    end = source.index('\nfunction ', start + 1)
    return source[:start] + 'function Assert-CodePierUninstallTasksAbsent { }\n' + source[end:]


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
    source=synthetic_bootstrap((ROOT/'deploy/install-from-hub.ps1').read_text(encoding='utf-8'))
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


def probe_uninstall_handoff(directory, executable):
    """Run only disposable cleanup roots; no Agent service or external account."""
    import hashlib
    import re

    directory = Path(directory)
    source = synthetic_bootstrap((ROOT/'deploy/install-from-hub.ps1').read_text(encoding='utf-8'))
    block = source[source.index('function Get-CodePierSHA256 {'):source.index("if ($Action -ne 'install' -and -not $Hub) {")]
    script = directory/'uninstall-finalize.ps1'
    script.write_text(
        "param([string]$PlanPath,[string]$HeldFile)\n$ErrorActionPreference='Stop'\n" + block +
        "\n$Held=$null\ntry {\n"
        "if($HeldFile){$Held=[IO.File]::Open($HeldFile,[IO.FileMode]::Open,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)}\n"
        "Complete-CodePierUninstall -PlanPath $PlanPath\n"
        "} finally { if($Held){$Held.Dispose()} }\n", encoding='utf-8-sig')
    sid = re.search(rb'S-1-\d+(?:-\d+)+',
                    subprocess.check_output(['whoami.exe', '/user', '/fo', 'csv', '/nh'])).group().decode('ascii')

    # Keep SID authority checks intact and diagnose path spelling separately.
    # Windows TEMP may use an 8.3 path that .NET expands to its long spelling.
    import base64
    escaped_directory = str(directory).replace("'", "''")
    identity_script = (
        "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
        "@{sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value; "
        "canonical_directory=[IO.Path]::GetFullPath('"+escaped_directory+"')} | ConvertTo-Json -Compress")
    identity_run = subprocess.run([executable, '-NoLogo', '-NoProfile', '-NonInteractive',
        '-EncodedCommand', base64.b64encode(identity_script.encode('utf-16-le')).decode('ascii')],
        capture_output=True, text=True, encoding='utf-8', timeout=30, check=True)
    identity = json.loads(identity_run.stdout)
    assert identity['sid'] == sid, 'whoami and WindowsIdentity must identify the same fixture account'
    identity_evidence = {'same_account_sid': True,
                         'path_spelling_changed': identity['canonical_directory'].casefold() != str(directory).casefold()}

    def fixture(base):
        base.mkdir(exist_ok=True)
        if not (base/'config.json').exists():
            (base/'config.json').write_text('{"synthetic":true}')
        if not (base/'runtime').exists():
            (base/'runtime').mkdir()
        (base/'runtime/held.txt').write_text('synthetic runtime')
        lock = base/'.install.lock'
        lock.mkdir()
        token = 'b'*32
        (lock/'uninstall.json').write_text(json.dumps({'schema': 1, 'lock_token': token}))
        marker = {'schema': 1, 'kind': 'codepier-local-uninstall', 'source': str(base),
                  'owner_sid': sid, 'stage': 'files_pending'}
        (base/'.uninstall.json').write_text(json.dumps(marker))
        files = {name: hashlib.sha256((base/name).read_bytes()).hexdigest() if (base/name).exists() else None
                 for name in ('config.json', 'management.json', 'service.xml')}
        return {'source': str(base), 'lock_token': token, 'files': files,
                'entries': sorted(p.name for p in base.iterdir() if p.name != '.install.lock')}

    def invoke(plan, handoff, held=None, env=None, cwd=None):
        handoff.write_text(json.dumps(plan), encoding='utf-8')
        command = [executable, '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                   '-File', str(script), '-PlanPath', str(handoff)]
        if held is not None:
            command += ['-HeldFile', str(held)]
        return subprocess.run(command, capture_output=True, timeout=30, env=env, cwd=cwd)

    results = []
    for case in ('success', 'empty', 'cwd-inside-root', 'equivalent-source-spelling', 'changed-config', 'wrong-lock', 'missing-snapshot',
                 'extra-entry', 'path-traversal', 'duplicate-root', 'wrong-owner', 'partial-failure'):
        base = directory/('uninstall-'+case+" & $ [test]")
        roots = [] if case == 'empty' else [fixture(base)]
        other = None
        if case in {'success', 'changed-config'}:
            other = directory/('uninstall-'+case+'-second')
            roots.append(fixture(other))
        plan = {'schema': 1, 'roots': roots, 'aliases': []}
        if case == 'equivalent-source-spelling':
            marker = json.loads((base/'.uninstall.json').read_text())
            marker['source'] = str(base/'runtime'/'..')
            (base/'.uninstall.json').write_text(json.dumps(marker))
        elif case == 'changed-config':
            (other/'config.json').write_text('{"changed":true}')
        elif case == 'wrong-lock':
            roots[0]['lock_token'] = 'c'*32
        elif case == 'missing-snapshot':
            del roots[0]['files']['management.json']
        elif case == 'extra-entry':
            (base/'unknown-project.txt').write_text('preserve')
        elif case == 'path-traversal':
            roots[0]['entries'].append('../outside')
        elif case == 'duplicate-root':
            roots.append(dict(roots[0]))
        elif case == 'wrong-owner':
            marker = json.loads((base/'.uninstall.json').read_text())
            marker['owner_sid'] = 'S-1-5-21-999999'
            (base/'.uninstall.json').write_text(json.dumps(marker))
        held = base/'runtime/held.txt' if case == 'partial-failure' else None
        handoff = directory/('uninstall-'+case+'.json')
        result = invoke(plan, handoff, held, cwd=base if case == 'cwd-inside-root' else None)
        success = case in {'success', 'empty', 'cwd-inside-root', 'equivalent-source-spelling'}
        assert (result.returncode == 0) == success, (case, identity_evidence, result.stdout[-4000:], result.stderr[-4000:])
        if success:
            assert not base.exists()
            if other:
                assert not other.exists()
        else:
            assert (base/'config.json').exists() and (base/'.uninstall.json').exists()
            assert (base/'runtime/held.txt').read_text() == 'synthetic runtime'
            if case == 'changed-config':
                assert (other/'config.json').read_text() == '{"changed":true}'
            if case != 'wrong-lock':
                assert not (base/'.install.lock').exists()
            if case == 'partial-failure':
                stage = json.loads((base/'.uninstall.json').read_text())['stage']
                assert stage == 'files_failed', (case, stage, result.stdout[-4000:], result.stderr[-4000:])
                assert not list(base.glob('.uninstall-failure-*.tmp'))
                plan['roots'] = [fixture(base)]
                retry = invoke(plan, handoff)
                assert retry.returncode == 0 and not base.exists(), (retry.stdout[-4000:], retry.stderr[-4000:])
        results.append({'case': case, 'passed': True, 'exit_code': result.returncode,
                        'identity_evidence': identity_evidence})

    # uv minor-version junctions are allowed only in a verified managed layout.
    for case in ('managed-python', 'missing-snapshot', 'outside-target', 'changed-target',
                 'wrong-version', 'wrong-architecture', 'duplicate-junction', 'not-managed',
                 'linked-executable', 'partial-python-failure'):
        base = directory/('uninstall-python-'+case)
        base.mkdir()
        python = base/'python'
        python.mkdir()
        target = python/'cpython-3.13.12-windows-x86_64-none'
        target.mkdir()
        (target/'python.exe').write_bytes(b'synthetic Python')
        alias = python/'cpython-3.13-windows-x86_64-none'
        actual_target = target
        if case == 'outside-target':
            actual_target = directory/('python-outside-'+case)/target.name
        elif case == 'changed-target':
            actual_target = python/'cpython-3.13.13-windows-x86_64-none'
        elif case == 'wrong-version':
            actual_target = python/'cpython-3.12.12-windows-x86_64-none'
        elif case == 'wrong-architecture':
            actual_target = python/'cpython-3.13.12-windows-aarch64-none'
        if actual_target != target:
            actual_target.mkdir(parents=True)
            (actual_target/'python.exe').write_bytes(b'preserve Python')
        (base/'management.json').write_text(json.dumps({
            'managed': case != 'not-managed', 'layout': 'managed-runtime'}))
        root = fixture(base)
        root['python_junctions'] = [{'path': str(alias), 'target': str(target)}]
        if case in {'outside-target', 'wrong-version', 'wrong-architecture'}:
            root['python_junctions'][0]['target'] = str(actual_target)
        if case == 'duplicate-junction':
            root['python_junctions'] *= 2
        elif case == 'missing-snapshot':
            del root['python_junctions']
        link = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(alias), str(actual_target)],
                              capture_output=True, timeout=10)
        assert link.returncode == 0, link.stderr
        if case == 'linked-executable':
            # Junctions do not need Developer Mode. A directory reparse point
            # shaped like python.exe must not count as a regular executable.
            (target/'python.exe').unlink()
            link = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J',
                                   str(target/'python.exe'), str(base/'runtime')],
                                  capture_output=True, timeout=10)
            assert link.returncode == 0, link.stderr
        handoff = directory/('uninstall-python-'+case+'.json')
        held = target/'python.exe' if case == 'partial-python-failure' else None
        result = invoke({'schema': 1, 'roots': [root], 'aliases': []}, handoff, held)
        if case == 'managed-python':
            assert result.returncode == 0 and not base.exists(), (result.stdout[-4000:], result.stderr[-4000:])
        else:
            assert result.returncode != 0 and (base/'config.json').exists()
            if case == 'partial-python-failure':
                assert json.loads((base/'.uninstall.json').read_text())['stage'] == 'files_failed'
                assert (target/'python.exe').read_bytes() == b'synthetic Python'
                assert not os.path.lexists(alias)
                # After the lock is released, a new plain-tree snapshot retries safely.
                retry_root = fixture(base)
                retry = invoke({'schema': 1, 'roots': [retry_root], 'aliases': []}, handoff)
                assert retry.returncode == 0 and not base.exists(), (retry.stdout[-4000:], retry.stderr[-4000:])
            else:
                assert os.path.lexists(alias)
                if case != 'linked-executable':
                    assert (actual_target/'python.exe').read_bytes() in {b'synthetic Python', b'preserve Python'}
                else:
                    os.rmdir(target/'python.exe')
                os.rmdir(alias)
        results.append({'case': 'python-junction-'+case, 'passed': True, 'exit_code': result.returncode})

    # A junction pointing outside the owned root is refused without following it.
    outside = directory/'uninstall-outside'
    outside.mkdir(); (outside/'project.txt').write_text('preserve')
    linked = directory/'uninstall-linked'
    root = fixture(linked)
    junction = linked/'runtime/outside'
    link = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(outside)],
                          capture_output=True, timeout=10)
    assert link.returncode == 0, link.stderr
    result = invoke({'schema': 1, 'roots': [root], 'aliases': []}, directory/'uninstall-linked.json')
    assert result.returncode != 0 and (outside/'project.txt').read_text() == 'preserve'
    assert (linked/'config.json').exists() and not (linked/'.install.lock').exists()
    # Remove only the fixture junction itself so TemporaryDirectory never traverses it.
    os.rmdir(junction)
    results.append({'case': 'junction-outside-root', 'passed': True, 'exit_code': result.returncode})

    home = directory/'uninstall-alias-home'
    home.mkdir()
    canonical = home/'.codepier-agent'
    root = fixture(canonical)
    alias = home/'.remote-dev-agent'
    link = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(alias), str(canonical)],
                          capture_output=True, timeout=10)
    assert link.returncode == 0, link.stderr
    result = invoke({'schema': 1, 'roots': [root], 'aliases': [str(alias)]},
                    directory/'uninstall-alias.json', env={**os.environ, 'USERPROFILE': str(home)})
    assert result.returncode == 0, (result.stdout[-4000:], result.stderr[-4000:])
    assert not canonical.exists() and not alias.exists()
    results.append({'case': 'owned-migration-alias', 'passed': True, 'exit_code': result.returncode})

    # USERPROFILE, plan roots and junction targets can name the same home with
    # different absolute spellings. This must not relax the exact alias target.
    spelled_home = directory/'uninstall-normalized-alias-home'
    spelled_home.mkdir()
    (spelled_home/'placeholder').mkdir()
    spelled_canonical = spelled_home/'.codepier-agent'
    spelled_root = fixture(spelled_canonical)
    spelled_alias = spelled_home/'.remote-dev-agent'
    link = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(spelled_alias), str(spelled_canonical)],
                          capture_output=True, timeout=10)
    assert link.returncode == 0, link.stderr
    result = invoke({'schema': 1, 'roots': [spelled_root], 'aliases': [str(spelled_alias)]},
                    directory/'uninstall-normalized-alias.json',
                    env={**os.environ, 'USERPROFILE': str(spelled_home/'placeholder'/'..')})
    assert result.returncode == 0, (result.stdout[-4000:], result.stderr[-4000:])
    assert not spelled_canonical.exists() and not os.path.lexists(spelled_alias)
    results.append({'case': 'equivalent-userprofile-alias', 'passed': True, 'exit_code': result.returncode})

    # A prior run may have removed the canonical directory before alias removal failed.
    canonical.mkdir()
    link = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(alias), str(canonical)],
                          capture_output=True, timeout=10)
    assert link.returncode == 0, link.stderr
    canonical.rmdir()
    result = invoke({'schema': 1, 'roots': [], 'aliases': [str(alias)]},
                    directory/'uninstall-dangling-alias.json', env={**os.environ, 'USERPROFILE': str(home)})
    assert result.returncode == 0, (result.stdout[-4000:], result.stderr[-4000:])
    assert not os.path.lexists(alias)
    results.append({'case': 'dangling-migration-alias-retry', 'passed': True, 'exit_code': result.returncode})
    return results


def probe_generated_commands(directory, executable):
    """Exercise the actual panel wrapper through native -File binding, offline."""
    import hashlib
    sys.path.insert(0, str(ROOT))
    from hub.agent_install import install_command

    directory = Path(directory)
    results = []
    for action in ('install', 'upgrade', 'uninstall', 'archive'):
        stub = """param(
          [string]$Hub, [string]$Token, [string]$Sha256, [string]$AllowRoot,
          [string]$Action = 'install', [string]$ExpectedDevice,
          [string]$InstallDir, [string]$Shell
        )
        [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
        $PSBoundParameters | ConvertTo-Json -Compress
        """
        stub_path = directory / 'synthetic installer.ps1'
        stub_path.write_text(stub, encoding='utf-8-sig')
        bootstrap_sha = hashlib.sha256(stub_path.read_bytes()).hexdigest()
        command = install_command(
            'windows', 'https://hub.invalid', 'rdi_synthetic' if action == 'install' else '',
            'a' * 64, r"C:\Projects O'Brien" if action == 'install' else '',
            bootstrap_sha, action=action,
            expected_device='' if action in {'archive', 'uninstall'} else 'fixture-node',
            install_dir=r"C:\Agent O'Brien", enable_execution=False)
        source = str(stub_path).replace("'", "''")
        driver = directory / 'driver.ps1'
        driver.write_text(
            "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)\n"
            "function Invoke-WebRequest { param([switch]$UseBasicParsing, [string]$Uri, [string]$OutFile) "
            "Copy-Item -LiteralPath '" + source + "' -Destination $OutFile }\n" + command,
            encoding='utf-8-sig')
        result = subprocess.run(
            [executable, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
             '-File', str(driver)], capture_output=True, text=True, encoding='utf-8', timeout=30)
        assert result.returncode == 0, result.stderr
        received = json.loads(result.stdout.strip())
        assert received['Hub'] == 'https://hub.invalid'
        assert received['Sha256'] == 'a' * 64
        assert received['InstallDir'] == r"C:\Agent O'Brien"
        if action == 'install':
            assert received['Token'] == 'rdi_synthetic'
            assert received['AllowRoot'] == r"C:\Projects O'Brien"
            assert received['Shell'] == 'disabled'
            assert 'Action' not in received
        else:
            assert received['Action'] == action
            assert not {'Token', 'AllowRoot', 'Shell'} & received.keys()
        if action in {'archive', 'uninstall'}:
            assert 'ExpectedDevice' not in received
        else:
            assert received['ExpectedDevice'] == 'fixture-node'
        results.append({'action': action, 'passed': True, 'exit_code': result.returncode})
    return results


def probe(directory):
    directory = Path(directory)
    (directory / 'deploy').mkdir();(directory / 'scripts').mkdir()
    script = directory / 'deploy/install-from-hub.ps1'
    script.write_text(synthetic_bootstrap((ROOT / 'deploy/install-from-hub.ps1').read_text(encoding='utf-8')), encoding='utf-8')
    helper = directory / 'scripts/install_agent.py'
    helper.write_text("import csv,hashlib,json,os,re,subprocess,sys\nfrom pathlib import Path\nPath(os.environ['FIXTURE_LOG']).write_text(json.dumps(sys.argv[1:]),encoding='utf-8')\nbase=Path(sys.argv[sys.argv.index('--install-dir')+1])\nif '--uninstall' in sys.argv and '--uninstall-preview' not in sys.argv and not int(os.getenv('FIXTURE_EXIT','0')):\n    roots=[]\n    if base.exists():\n        lock=base/'.install.lock';lock.mkdir()\n        token='a'*32\n        (lock/'uninstall.json').write_text(json.dumps({'schema':1,'lock_token':token}))\n        sid=re.search(rb'S-1-\\d+(?:-\\d+)+',subprocess.check_output(['whoami.exe','/user','/fo','csv','/nh'])).group().decode('ascii')\n        (base/'.uninstall.json').write_text(json.dumps({'schema':1,'kind':'codepier-local-uninstall','source':str(base),'owner_sid':sid,'stage':'files_pending'}))\n        files={name:hashlib.sha256((base/name).read_bytes()).hexdigest() if (base/name).exists() else None for name in ('config.json','management.json','service.xml')}\n        roots=[{'source':str(base),'lock_token':token,'files':files,'entries':sorted(p.name for p in base.iterdir() if p.name!='.install.lock')}]\n        if os.getenv('FIXTURE_KEEP'): (base/'unexpected-project.txt').write_text('preserve')\n    Path(sys.argv[sys.argv.index('--uninstall-handoff-file')+1]).write_text(json.dumps({'schema':1,'roots':roots,'aliases':[]}))\nif '--archive-install' in sys.argv and not int(os.getenv('FIXTURE_EXIT','0')):\n    lock=base/'.install.lock';lock.mkdir()\n    token='a'*32\n    (lock/'archive.json').write_text(json.dumps({'schema':1,'lock_token':token}))\n    target=base.with_name(base.name+'.recovery-20000101-000000-01234567')\n    files={name:hashlib.sha256((base/name).read_bytes()).hexdigest() if (base/name).exists() else None for name in ('config.json','management.json','service.xml')}\n    plan={'schema':1,'source':str(base),'backup':str(target),'lock_token':token,'files':files}\n    Path(sys.argv[sys.argv.index('--archive-handoff-file')+1]).write_text(json.dumps(plan))\nraise SystemExit(int(os.getenv('FIXTURE_EXIT','0')))\n", encoding='utf-8')
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
        ('uninstall', ['-Preview'], 0, True, False), ('upgrade', ['-Preview'], 0, False, False),
        ('archive', [], 0, True, False),
        ('start', [], 37, True, False), ('uninstall', [], 0, True, True),
        ('invalid-action', [], 0, False, False), ('start', ['-NoService'], 0, False, False),
        ('start', ['-Hub', 'https://invalid.example'], 0, False, False),
    ]):
        base = directory / (f'fixture-{index} & cash $ [test]')
        base.mkdir();config = base / 'config.json';config.write_bytes(b'{"synthetic":true,"device_id":"fixture-device","hub_url":"https://hub.invalid"}')
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
                      '--install-dir', str(base)]
            if '-Preview' in extra:
                prefix += ['--uninstall-preview']
            if action != 'uninstall':
                prefix += ['--expected-device', 'fixture-device']
            # PowerShell canonicalizes the absolute -InstallDir with .NET on
            # Windows. Compare the resolved path, not its spelling/case; all
            # flags and the exact target device must still match byte-for-byte.
            assert len(arguments) >= len(prefix), (index, action, arguments)
            assert arguments[:2] == prefix[:2], (index, action, arguments, prefix)
            assert Path(arguments[2]).resolve() == base.resolve(), (index, action, arguments[2], str(base))
            assert arguments[3:len(prefix)] == prefix[3:], (index, action, arguments, prefix)
            if action in {'archive', 'uninstall'} and '-Preview' not in extra:
                assert arguments[-2] == ('--archive-handoff-file' if action == 'archive' else '--uninstall-handoff-file')
                assert not Path(arguments[-1]).is_relative_to(base)
            else:
                assert len(arguments) == len(prefix), (index, action, arguments, prefix)
        if action == 'archive':
            target=base.with_name(base.name+'.recovery-20000101-000000-01234567')
            assert not base.exists() and (target/'config.json').read_bytes() == b'{"synthetic":true,"device_id":"fixture-device","hub_url":"https://hub.invalid"}'
            assert not (target/'.install.lock').exists()
        elif action != 'uninstall' or keep or '-Preview' in extra:
            assert config.read_bytes() == b'{"synthetic":true,"device_id":"fixture-device","hub_url":"https://hub.invalid"}'
        else:
            assert not base.exists()
        if keep:
            assert b'cleanup did not complete' in result.stderr
            assert (base/'unexpected-project.txt').read_text() == 'preserve'
            assert (base/'.uninstall.json').is_file() and not (base/'.install.lock').exists()
        results.append({'case': index, 'action': action, 'passed': True, 'exit_code': result.returncode})
    # Absence is discovered by Python; the shell must not prematurely return.
    absent = directory/'absent-uninstall'
    log = directory/'absent-uninstall-args.json'
    env = {**os.environ, 'PATH': str(Path(sys.executable).parent) + os.pathsep + os.environ.get('PATH', ''),
           'FIXTURE_LOG': str(log), 'FIXTURE_EXIT': '0', 'PYTHONUTF8': '1'}
    result = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', str(script), '-Action', 'uninstall', '-InstallDir', str(absent), '-ExpectedDevice', 'other-node'],
        cwd=directory, env=env, capture_output=True, timeout=30)
    assert result.returncode == 0 and log.exists(), (result.stdout[-4000:], result.stderr[-4000:])
    assert '--expected-device' not in json.loads(log.read_text())
    assert not absent.exists()
    results.append({'case': 'absent-directory-reaches-discovery', 'passed': True, 'exit_code': result.returncode})

    # Both default names are valid discovery candidates for uninstall preview.
    shadow_home = directory/'synthetic-home'
    shadow_home.mkdir()
    for name in ('.codepier-agent', '.remote-dev-agent'):
        (shadow_home/name).mkdir()
    log = directory/'both-defaults-args.json'
    result = subprocess.run([executable, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', str(script), '-Action', 'uninstall', '-Preview'],
        cwd=directory, env={**env, 'USERPROFILE': str(shadow_home), 'FIXTURE_LOG': str(log)},
        capture_output=True, timeout=30)
    assert result.returncode == 0 and log.exists(), (result.stdout[-4000:], result.stderr[-4000:])
    assert all((shadow_home/name).is_dir() for name in ('.codepier-agent', '.remote-dev-agent'))
    assert '--uninstall-preview' in json.loads(log.read_text())
    results.append({'case': 'both-defaults-readonly-preview', 'passed': True, 'exit_code': result.returncode})
    return {'passed': True, 'cases': results, 'real_powershell': True,
            'generated_command_cases': probe_generated_commands(directory, executable),
            'download_cases': probe_download(directory, executable),
            'archive_cases': probe_archive_handoff(directory, executable),
            'uninstall_cases': probe_uninstall_handoff(directory, executable),
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
