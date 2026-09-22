#!/usr/bin/env python3
"""Run every test module in bounded independent processes; retain exact evidence.

No automatic retry, test exclusion or skipped-test-as-success. Each module gets
its own pytest cache and report. A stalled module receives SIGINT so fixtures can
clean up; forced termination is reported and never counted as verification.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = '''import json, os
from pathlib import Path

def pytest_collection_finish(session):
    Path(os.environ['CODEPIER_COLLECTION']).write_text(json.dumps([item.nodeid for item in session.items]))

def pytest_runtest_logreport(report):
    value = {'nodeid':report.nodeid,'when':report.when,'outcome':report.outcome,'duration':report.duration}
    if hasattr(report,'wasxfail'): value['xfail'] = True
    with open(os.environ['CODEPIER_EVENTS'],'a') as output: output.write(json.dumps(value)+'\\n')
'''


def snapshot():
    result = {}
    for directory in ('agent','hub','shared','scripts','tests','web','deploy','skills','.github'):
        for path in sorted((ROOT/directory).rglob('*')):
            relative = path.relative_to(ROOT)
            if (not path.is_file() or path.is_symlink() or
                any(part in {'node_modules','__pycache__','.git','.pytest_cache'} for part in relative.parts)):
                continue
            result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(ROOT.iterdir()):
        if path.is_file() and not path.is_symlink() and (
            path.name.startswith('requirements') and path.suffix == '.txt' or
            path.name in {'Dockerfile', 'compose.yml', 'codepier', 'codepier.ps1', 'install.sh',
                          '.dockerignore', '.gitignore', '.gitattributes', '.env.example', 'ruff.toml',
                          'README.md', 'CHANGELOG.md', 'SECURITY.md', 'CONTRIBUTING.md', 'RELEASE.json'}):
            result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ('docs/INTEGRATIONS-20260917.md', 'docs/MCP-WORKSPACE-DASHBOARD-20260917.md',
                 'docs/DEVTOOLS-FLOW-20260918.md', 'docs/ARCHITECTURE.md', 'docs/RELEASING.md', 'docs/VPS.md', 'docs/LONG_OPERATIONS.md'):
        path = ROOT / name
        if path.is_file() and not path.is_symlink():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def stop(process):
    if process.poll() is not None:
        return False
    process.send_signal(signal.SIGINT if os.name != 'nt' else signal.SIGTERM)
    try:
        process.wait(timeout=20)
        return False
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            process.kill()
        else:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        process.wait(timeout=10)
        return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--workers', type=int, choices=range(1,5), default=1)
    parser.add_argument('--timeout', type=int, default=900)
    args = parser.parse_args()
    if args.timeout < 30:
        parser.error('--timeout must be at least 30 seconds')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output/'collection.json').exists():
        parser.error('Use a fresh output directory; existing evidence is never overwritten')
    plugin_dir = output/'instrumentation'; plugin_dir.mkdir()
    (plugin_dir/'codepier_regression_capture.py').write_text(PLUGIN)
    env = {**os.environ, 'PYTHONPATH':os.pathsep.join([str(plugin_dir),str(ROOT)]),
           'PYTHONUNBUFFERED':'1'}
    compat = ROOT/'.venv-compat/bin/python'
    if 'MCP_COMPAT_PYTHON' not in env and compat.is_file(): env['MCP_COMPAT_PYTHON'] = str(compat)
    env.update(CODEPIER_COLLECTION=str(output/'collection.json'),CODEPIER_EVENTS=str(output/'collection-events.jsonl'))
    prefix = [sys.executable,'-m','pytest','-p','codepier_regression_capture']
    baseline = snapshot(); write_json(output/'source-before.json', baseline)
    started = time.monotonic()
    with (output/'collection.log').open('w') as log:
        result = subprocess.run(prefix+['--collect-only','-q','-o','cache_dir='+str(output/'collect-cache'),'tests'],
            cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=90)
    if result.returncode:
        print('Collection failed; see',output/'collection.log',flush=True)
        return result.returncode
    expected = json.loads((output/'collection.json').read_text())
    modules = sorted({nodeid.split('::',1)[0] for nodeid in expected})
    print(f'Collected {len(expected)} tests in {len(modules)} modules; workers={args.workers}',flush=True)
    lock = threading.Lock(); active = {}; results = []

    def run(module):
        name = Path(module).stem
        directory = output/name; directory.mkdir()
        local_env = {**env,'CODEPIER_COLLECTION':str(directory/'collected.json'),'CODEPIER_EVENTS':str(directory/'events.jsonl')}
        command = prefix+['-v','--tb=short','-o','cache_dir='+str(directory/'cache'),
                          '--junitxml='+str(directory/'results.xml'),'--basetemp='+str(directory/'tmp'),module]
        begin = time.monotonic(); timed_out = forced = False
        with (directory/'output.log').open('w') as log:
            process = subprocess.Popen(command,cwd=ROOT,env=local_env,stdout=log,stderr=subprocess.STDOUT,start_new_session=os.name!='nt')
            with lock: active[module] = process
            try:
                try: process.wait(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True; forced = stop(process)
            finally:
                with lock: active.pop(module,None)
        record = {'module':module,'exit_code':process.returncode,'timed_out':timed_out,
                  'forced_termination':forced,'seconds':round(time.monotonic()-begin,3)}
        write_json(directory/'process.json', record)
        with lock:
            print(json.dumps(record),flush=True)
            if record['exit_code'] or record['timed_out']:
                print('FAILURE_LOG '+module+'\n'+(directory/'output.log').read_text(errors='replace')[-12000:],flush=True)
        return record

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(run,module) for module in modules]
        try:
            for future in as_completed(futures): results.append(future.result())
        except BaseException:
            for future in futures: future.cancel()
            with lock: running = list(active.values())
            for process in running: stop(process)
            raise
    records = {}; suites = ET.Element('testsuites')
    for module in modules:
        directory = output/Path(module).stem
        event_file = directory/'events.jsonl'
        if event_file.is_file():
            for line in event_file.read_text().splitlines():
                event = json.loads(line); records.setdefault(event['nodeid'],[]).append(event)
        xml = directory/'results.xml'
        if xml.is_file():
            document = ET.parse(xml).getroot()
            for suite in list(document) if document.tag=='testsuites' else [document]: suites.append(suite)
    outcomes = {}
    for nodeid in expected:
        events = records.get(nodeid,[])
        if any(e['outcome']=='failed' for e in events): state = 'failed'
        elif any(e['outcome']=='skipped' or e.get('xfail') for e in events): state = 'skipped'
        elif all(any(e['when']==phase and e['outcome']=='passed' for e in events) for phase in ('setup','call','teardown')): state = 'passed'
        else: state = 'missing'
        outcomes[nodeid] = state
    after = snapshot(); changed = sorted(path for path in baseline.keys()|after.keys() if baseline.get(path)!=after.get(path))
    counts = {state:sum(s==state for s in outcomes.values()) for state in ('passed','failed','skipped','missing')}
    summary = {'collected':len(expected),'modules':len(modules),'workers':args.workers,'counts':counts,
               'seconds':round(time.monotonic()-started,3),'source_changed_during_run':changed,
               'unexpected_tests':sorted(set(records)-set(expected)),
               'module_failures':[r for r in results if r['exit_code'] or r['timed_out']],
               'outcomes':outcomes,'runs':sorted(results,key=lambda r:r['module']),
               'method':'All collected modules, independent pytest processes, no retries or exclusions. Native CLI tests use fixtures; real model probes are not run by this script.'}
    summary['verified'] = (counts['passed']==len(expected) and bool(expected) and not changed
                           and not summary['unexpected_tests'] and not summary['module_failures'])
    write_json(output/'summary.json',summary); write_json(output/'source-after.json',after)
    ET.ElementTree(suites).write(output/'full-regression.xml',encoding='utf-8',xml_declaration=True)
    print(json.dumps({k:v for k,v in summary.items() if k not in {'outcomes','runs'}},ensure_ascii=False,indent=2),flush=True)
    return 0 if summary['verified'] else 1

if __name__=='__main__':
    raise SystemExit(main())
