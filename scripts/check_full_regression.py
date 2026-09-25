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
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts.regression_plan import available_workers, classify_events, fingerprint, plan_jobs, pytest_command
PLUGIN = '''import json, os
from pathlib import Path

def pytest_collection_finish(session):
    Path(os.environ['CODEPIER_COLLECTION']).write_text(json.dumps([item.nodeid for item in session.items]))
    if os.getenv('CODEPIER_MARKERS'):
        Path(os.environ['CODEPIER_MARKERS']).write_text(json.dumps({item.nodeid:[m.name for m in item.iter_markers()] for item in session.items}))

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
            path.name.startswith('requirements') and path.suffix in {'.txt', '.in'} or
            path.name in {'Dockerfile', 'compose.yml', 'codepier', 'codepier.ps1', 'install.sh',
                          '.dockerignore', '.gitignore', '.gitattributes', '.env.example', 'ruff.toml', 'pyproject.toml', '.prettierrc.json',
                          'README.md', 'CHANGELOG.md', 'SECURITY.md', 'CONTRIBUTING.md', 'RELEASE.json'}):
            result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    from scripts.build_source_bundle import PUBLIC_DOCS
    for name in sorted(PUBLIC_DOCS):
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
    parser.add_argument('--workers', type=int, default=1, help='Concurrent isolated jobs, bounded by available CPUs')
    parser.add_argument('--shard-count', type=int, default=1)
    parser.add_argument('--shard-index', type=int, default=0)
    parser.add_argument('--coverage', action='store_true', help='Capture parent/child Python coverage without a legacy whole-suite threshold')
    parser.add_argument('--timeout', type=int, default=900)
    args = parser.parse_args()
    if not 1 <= args.workers <= available_workers():
        parser.error(f'--workers must be between 1 and {available_workers()} available CPUs')
    if not 1 <= args.shard_count <= 64 or not 0 <= args.shard_index < args.shard_count:
        parser.error('Require 1 <= --shard-count <= 64 and 0 <= --shard-index < --shard-count')
    if args.coverage:
        import importlib.util
        if importlib.util.find_spec('pytest_cov') is None:
            parser.error('--coverage requires requirements-dev.txt (pytest-cov)')
    if args.timeout < 30:
        parser.error('--timeout must be at least 30 seconds')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output/'collection.json').exists():
        parser.error('Use a fresh output directory; existing evidence is never overwritten')
    plugin_dir = output/'instrumentation'; plugin_dir.mkdir()
    (plugin_dir/'codepier_regression_capture.py').write_text(PLUGIN)
    env = {**os.environ, 'PYTHONPATH':os.pathsep.join([str(plugin_dir),str(ROOT)]),
           'PYTHONUNBUFFERED':'1', 'PYTEST_ADDOPTS':''}
    compat = ROOT/('.venv-compat/Scripts/python.exe' if os.name == 'nt' else '.venv-compat/bin/python')
    if 'MCP_COMPAT_PYTHON' not in env and compat.is_file(): env['MCP_COMPAT_PYTHON'] = str(compat)
    env.update(CODEPIER_COLLECTION=str(output/'collection.json'),CODEPIER_EVENTS=str(output/'collection-events.jsonl'),CODEPIER_MARKERS=str(output/'markers.json'))
    prefix = [sys.executable,'-m','pytest','-p','codepier_regression_capture']
    baseline = snapshot(); write_json(output/'source-before.json', baseline)
    started = time.monotonic()
    with (output/'collection.log').open('w') as log:
        result = subprocess.run(prefix+['--collect-only','-q','-o','cache_dir='+str(output/'collect-cache'),'tests'],
            cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT,timeout=90)
    if result.returncode:
        print('Collection failed; see',output/'collection.log',flush=True)
        return result.returncode
    full_collection = json.loads((output/'collection.json').read_text())
    markers = json.loads((output/'markers.json').read_text())
    jobs = plan_jobs(full_collection, markers, shard_index=args.shard_index, shard_count=args.shard_count)
    selected = {nodeid for job in jobs for nodeid in job['nodeids']}
    expected = [nodeid for nodeid in full_collection if nodeid in selected]
    modules = sorted({job['module'] for job in jobs})
    write_json(output/'plan.json', jobs)
    exclusive_jobs = [job for job in jobs if job.get('exclusive')]
    limited_jobs = [job for job in jobs if not job.get('exclusive') and job.get('limited')]
    parallel_jobs = [job for job in jobs if not job.get('exclusive') and not job.get('limited')]
    limited_workers = min(2, args.workers)
    print(f'Collected {len(full_collection)} tests; selected {len(expected)} in {len(jobs)} jobs; workers={args.workers}; limited={len(limited_jobs)}@{limited_workers}; exclusive={len(exclusive_jobs)}; shard={args.shard_index}/{args.shard_count}',flush=True)
    lock = threading.Lock(); active = {}; results = []

    def run(job):
        module, name = job['module'], job['name']
        directory = output/name; directory.mkdir()
        local_env = {**env,'CODEPIER_COLLECTION':str(directory/'collected.json'),'CODEPIER_EVENTS':str(directory/'events.jsonl'),
                     'CODEPIER_MARKERS':str(directory/'markers.json'), 'COVERAGE_FILE':str(directory/'.coverage')}
        command = pytest_command(prefix, directory, job['selectors'], coverage=args.coverage)
        begin = time.monotonic(); timed_out = forced = False
        with (directory/'output.log').open('w') as log:
            process = subprocess.Popen(command,cwd=ROOT,env=local_env,stdout=log,stderr=subprocess.STDOUT,start_new_session=os.name!='nt')
            with lock: active[name] = process
            try:
                try: process.wait(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True; forced = stop(process)
            finally:
                with lock: active.pop(name,None)
        record = {'module':module,'job':name,'exit_code':process.returncode,'timed_out':timed_out,
                  'forced_termination':forced,'seconds':round(time.monotonic()-begin,3)}
        write_json(directory/'process.json', record)
        with lock:
            print(json.dumps(record),flush=True)
            if record['exit_code'] or record['timed_out']:
                print('FAILURE_LOG '+module+'\n'+(directory/'output.log').read_text(errors='replace')[-12000:],flush=True)
        return record

    def run_pool(selected_jobs, workers):
        if not selected_jobs:
            return
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run,job) for job in selected_jobs]
            try:
                for future in as_completed(futures): results.append(future.result())
            except BaseException:
                for future in futures: future.cancel()
                with lock: running = list(active.values())
                for process in running: stop(process)
                raise

    run_pool(parallel_jobs, args.workers)
    # Browser/real-process integration modules get a smaller shared pool. This
    # keeps coverage exhaustive without turning host saturation into false
    # product failures on local 5-15 second deadlines.
    run_pool(limited_jobs, limited_workers)
    # Timing/restart-sensitive jobs still run exactly once, but only after both
    # pools have drained so their local deadlines measure the product contract.
    try:
        for job in exclusive_jobs:
            results.append(run(job))
    except BaseException:
        with lock: running = list(active.values())
        for process in running: stop(process)
        raise
    records = {}; suites = ET.Element('testsuites')
    for job in jobs:
        directory = output/job['name']
        event_file = directory/'events.jsonl'
        if event_file.is_file():
            for line in event_file.read_text().splitlines():
                event = json.loads(line); records.setdefault(event['nodeid'],[]).append(event)
        xml = directory/'results.xml'
        if xml.is_file():
            document = ET.parse(xml).getroot()
            for suite in list(document) if document.tag=='testsuites' else [document]: suites.append(suite)
    outcomes, duplicate_phases = classify_events(expected, records)
    coverage_error = None
    if args.coverage:
        from scripts.coverage_reports import combine
        try:
            combine([output/job['name']/'.coverage' for job in jobs], output)
        except Exception as error:
            # Preserve the complete test receipts even when coverage fails.
            coverage_error = type(error).__name__ + ': ' + str(error)
    after = snapshot(); changed = sorted(path for path in baseline.keys()|after.keys() if baseline.get(path)!=after.get(path))
    counts = {state:sum(s==state for s in outcomes.values()) for state in ('passed','failed','skipped','missing')}
    summary = {'collected':len(expected),'total_collected':len(full_collection),'modules':len(modules),'jobs':len(jobs),'workers':args.workers,'limited_jobs':len(limited_jobs),'limited_workers':limited_workers,'exclusive_jobs':len(exclusive_jobs),'counts':counts,
               'shard':{'index':args.shard_index,'count':args.shard_count},
               'coverage_requested':args.coverage,'coverage_error':coverage_error,
               'scope':'complete' if args.shard_count == 1 else 'shard',
               'full_collection':full_collection,'collection_sha256':fingerprint(full_collection),
               'source_inventory_sha256':fingerprint(baseline),'duplicate_phases':duplicate_phases,
               'seconds':round(time.monotonic()-started,3),'source_changed_during_run':changed,
               'unexpected_tests':sorted(set(records)-set(expected)),
               'module_failures':[r for r in results if r['exit_code'] or r['timed_out']],
               'outcomes':outcomes,'runs':sorted(results,key=lambda r:r['job']),
               'method':'Deterministic exhaustive module/independent-case jobs, explicit shards, exactly-once evidence, no retries. Browser/integration jobs use a two-worker resource pool; timing/restart-sensitive jobs run once with exclusive host admission after both pools drain. A shard is not full verification until all shards merge. Native CLI tests use local substitutes, never real model probes.'}
    summary['verified'] = (counts['passed']==len(expected) and bool(expected) and not changed
                           and not summary['unexpected_tests'] and not summary['module_failures'] and not duplicate_phases and not coverage_error)
    write_json(output/'summary.json',summary); write_json(output/'source-after.json',after)
    ET.ElementTree(suites).write(output/'full-regression.xml',encoding='utf-8',xml_declaration=True)
    print(json.dumps({k:v for k,v in summary.items() if k not in {'outcomes','runs','full_collection'}},ensure_ascii=False,indent=2),flush=True)
    return 0 if summary['verified'] else 1

if __name__=='__main__':
    raise SystemExit(main())
