#!/usr/bin/env python3
"""Opt-in real loopback benchmark. Creates only disposable fixture Hub/Agent data.

Run the SAME file/interpreter/workload against --repository before/after checkouts.
No external endpoint, new dependency, production credential, browser or model call.
Do not run alongside a user's latency-sensitive work. Measurements are evidence,
not a promise: retain every error and host-load sample, alternate variant order.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import re
import sqlite3
import statistics
import subprocess
import sys
import time
import uuid


def percentiles(values):
    values = sorted(v for v in values if v is not None)
    return {key: round(values[max(0, math.ceil(len(values)*p)-1)], 3) if values else None
            for key, p in [('p50', .5), ('p95', .95), ('p99', .99)]}


def fairness(samples):
    groups = {}
    for sample in samples:
        groups.setdefault(sample['worker'], []).append(sample['latency_ms'])
    rates = [1000/statistics.mean(values) for values in groups.values() if statistics.mean(values)>0]
    return sum(rates)**2/(len(rates)*sum(v*v for v in rates)) if rates else None


def summarize(samples, seconds, *, include_kinds=True):
    good = [s for s in samples if s['ok']]
    result = {'samples':len(samples), 'succeeded':len(good), 'errors':len(samples)-len(good),
            'error_rate':(len(samples)-len(good))/len(samples) if samples else None,
            'duration_seconds':seconds, 'throughput_success_per_second':len(good)/seconds if seconds > 0 else None,
            'latency_ms_all':percentiles([s['latency_ms'] for s in samples]),
            'latency_ms_success':percentiles([s['latency_ms'] for s in good]),
            'hub_queue_ms':percentiles([s.get('hub_queue_ms') for s in samples]),
            'agent_queue_ms':percentiles([s.get('agent_queue_ms') for s in samples]),
            'jain_inverse_worker_mean_latency':fairness(samples),
            'queue_sample_count':sum(s.get('hub_queue_ms') is not None for s in samples),
            'poll_requests':sum(s.get('polls',0) for s in samples)}
    if include_kinds:
        result['by_kind'] = {kind: summarize([s for s in samples if s.get('kind', 'unspecified') == kind],
            seconds, include_kinds=False) for kind in sorted({s.get('kind', 'unspecified') for s in samples})}
    return result


def git_identity(repository):
    """Hash all nonignored changes; never copy diff contents into the report."""
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repository), *args])
    diff = git('diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD')
    status = git('status', '--porcelain=v1', '-z')
    digest = hashlib.sha256(diff)
    for raw in sorted(filter(None, git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0'))):
        path = repository / os.fsdecode(raw)
        payload = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        digest.update(len(raw).to_bytes(8, 'big'))
        digest.update(raw)
        digest.update(len(payload).to_bytes(8, 'big'))
        digest.update(payload)
    return {'commit':git('rev-parse','HEAD').decode().strip(), 'dirty':bool(status),
            'tracked_diff_and_untracked_source_sha256':digest.hexdigest()}


UNRESOLVED = {'queued', 'running', 'reconnecting', 'cancelling', 'unknown'}
TERMINAL = {'succeeded', 'failed', 'cancelled', 'needs_review', 'interrupted'}


def safe_code(value):
    return value if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value) else 'unclassified_error'


def lookup_original(database, key):
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as db:
        rows = db.execute('SELECT id FROM operations WHERE idem=?', (key,)).fetchall()
    if len(rows) != 1:
        raise ValueError('original_operation_missing_or_ambiguous')
    return rows[0][0]


def preflight(seconds, max_load_per_cpu):
    samples = []
    deadline = time.monotonic() + seconds
    while True:
        samples.append({'at':time.time(), 'loadavg':list(os.getloadavg())})
        if time.monotonic() >= deadline:
            break
        time.sleep(min(1, max(0, deadline-time.monotonic())))
    overloaded = any(row['loadavg'][0] / (os.cpu_count() or 1) > max_load_per_cpu for row in samples)
    return samples, overloaded


def extract(response):
    response.raise_for_status()
    body = response.json()
    if 'error' in body:
        raise ValueError('JSONRPC_'+str(body['error'].get('code','unknown')))
    result = body['result']
    data = result.get('structuredContent')
    if data is None:
        texts = [x.get('text','') for x in result.get('content',[]) if x.get('type')=='text']
        data = json.loads(texts[0]) if texts else {}
    return data, bool(result.get('isError'))


async def rpc(client, name, arguments):
    return extract(await client.post('/mcp', json={'jsonrpc':'2.0','id':uuid.uuid4().hex,
        'method':'tools/call','params':{'name':name,'arguments':arguments}}))


async def finish_original(client, data, failed, deadline):
    polls = 0
    identifier = data.get('operation_id') or data.get('id')
    while data.get('pending') or data.get('state') in UNRESOLVED:
        if time.monotonic() >= deadline:
            raise TimeoutError('original_operation_pending')
        if not identifier:
            raise ValueError('missing_original_operation_id')
        async with asyncio.timeout(max(.001, deadline-time.monotonic())):
            receipt, poll_failed = await rpc(client, 'task_query', {
                'operation':'wait', 'operation_ids':[identifier], 'wait_seconds':1,
                'include_output':False, 'include_result':True})
        polls += 1
        if poll_failed:
            raise RuntimeError('original_operation_poll_denied')
        operations = receipt.get('operations')
        if not isinstance(operations, list) or len(operations) != 1:
            raise ValueError('invalid_original_operation_receipt')
        operation = operations[0]
        returned_id = operation.get('operation_id') or operation.get('id')
        if returned_id is not None and returned_id != identifier:
            raise ValueError('original_operation_id_changed')
        state = operation.get('state')
        if operation.get('pending') or state in UNRESOLVED:
            data = operation
            await asyncio.sleep(min(.05, max(0, deadline-time.monotonic())))
            continue
        if state not in TERMINAL:
            raise ValueError('original_operation_state_missing')
        result = operation.get('result') or {}
        return identifier, result.get('data') or {}, not (
            state == 'succeeded' and result.get('ok') is True), polls, state
    state = data.get('state', 'failed' if failed else 'succeeded')
    if state not in TERMINAL:
        raise ValueError('invalid_operation_state')
    return identifier, data, failed or state != 'succeeded', polls, state


def enrich(database, samples):
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as connection:
        connection.row_factory = sqlite3.Row
        for sample in samples:
            identifier = sample.get('operation_id')
            if not identifier:
                continue
            row = connection.execute('SELECT created,accepted_at,updated,state,attempts FROM operations WHERE id=?',
                                     (identifier,)).fetchone()
            if row is None:
                continue
            events = [dict(e) for e in connection.execute(
                'SELECT source,seq,stage,at,elapsed_ms FROM operation_events WHERE operation_id=? ORDER BY id',
                (identifier,))]
            sample['operation'] = dict(row)
            sample['events'] = events
            dispatch = [e['at'] for e in events if e['source']=='hub' and e['stage']=='dispatched']
            sample['hub_queue_ms'] = max(0, (min(dispatch)-row['created'])*1000) if dispatch else None
            agent = [e for e in events if e['source']=='agent' and e['elapsed_ms'] is not None]
            starts = [e for e in agent if e['stage']=='executing']
            # Elapsed times belong to one Agent handling attempt. Never subtract
            # an Agent wall clock from a Hub wall clock or merge restarted attempts.
            if row['attempts'] != 1:
                sample['agent_queue_unavailable'] = 'multiple_attempts'
            elif starts:
                start = min(starts, key=lambda e:e['seq'])
                before = [e for e in agent if e['seq']<=start['seq'] and e['stage'] in
                          {'accepted','waiting_resource','waiting_project','waiting_worker'}]
                if before:
                    sample['agent_queue_ms'] = max(0, start['elapsed_ms']-min(e['elapsed_ms'] for e in before))


async def sample_pids(pids, stop, output):
    """Sample explicitly owned PIDs only; CPU is ps lifetime-average percent."""
    while not stop.is_set():
        process = await asyncio.create_subprocess_exec('ps','-o','pid=,pcpu=,rss=','-p',
            ','.join(str(pid) for pid in pids), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 3)
        except TimeoutError:
            process.kill()
            await process.communicate()
            output.append({'at':time.time(), 'loadavg':list(os.getloadavg()), 'error':'ps_timeout'})
            return
        entries = []
        for line in stdout.decode().splitlines():
            fields = line.split()
            if len(fields)==3:
                entries.append({'role':pids[int(fields[0])],
                                'cpu_percent_lifetime':float(fields[1]),'rss_kib':int(fields[2])})
        output.append({'at':time.time(),'loadavg':list(os.getloadavg()),'processes':entries,
                       'complete':process.returncode == 0 and len(entries) == len(pids)})
        try:
            await asyncio.wait_for(stop.wait(), .5)
        except TimeoutError:
            pass


async def resource_samples(stack, stop, output):
    await sample_pids({stack.hub.pid:'hub', stack.agent.pid:'agent'}, stop, output)


async def workload(stack, output, workload_name, concurrency, operations, request_timeout):
    import httpx
    headers = {'Authorization':'Bearer '+stack.pat, 'MCP-Protocol-Version':'2025-11-25',
               'Accept':'application/json, text/event-stream'}
    samples, resources = [], []
    stop, abort = asyncio.Event(), asyncio.Event()
    async with httpx.AsyncClient(base_url=stack.url, headers=headers, trust_env=False,
            timeout=request_timeout, limits=httpx.Limits(max_connections=concurrency,
            max_keepalive_connections=concurrency)) as client:
        warm, warm_failed = await rpc(client, 'read', {
            'project':'ProjectAlpha', 'path':'README.md', 'idempotency_key':uuid.uuid4().hex})
        _, _, warm_failed, _, _ = await finish_original(
            client, warm, warm_failed, time.monotonic()+request_timeout)
        if warm_failed:
            raise RuntimeError('warmup_failed')
        monitor = asyncio.create_task(resource_samples(stack, stop, resources))
        start = time.monotonic()
        async def worker(index):
            path = f'bench/worker-{index}.txt'
            current_sha = hashlib.sha256(b'initial\n').hexdigest()
            for step in range(operations):
                if abort.is_set():
                    break
                key = uuid.uuid4().hex
                name = 'read'
                args = {'project':'ProjectAlpha', 'path':path}
                kind = 'read'
                if workload_name=='read_write' and step%4==0:
                    name, kind = 'write', 'write'
                    content = f'worker {index} step {step}\n'
                    args.update(content=content,expected_sha256=current_sha,idempotency_key=key)
                elif workload_name=='long_short' and index%8==0 and step%4==0:
                    name, kind = 'exec', 'long'
                    args = {'project':'ProjectAlpha','task':'bench_long',
                            'yield_seconds':1,'idempotency_key':key}
                else:
                    args['idempotency_key'] = key
                began = time.monotonic()
                sample = {'worker':index,'step':step,'kind':kind,'started_at':time.time(),
                          'ok':False,'error':None,'polls':0}
                try:
                    data, failed = await rpc(client, name, args)
                    sample['operation_id'] = data.get('operation_id') or data.get('id')
                    identifier, result, failed, polls, state = await finish_original(
                        client, data, failed, began+request_timeout)
                    if kind=='long' and (result.get('exit_code')!=0 or result.get('timed_out')):
                        failed = True
                    sample.update(operation_id=identifier,polls=polls,state=state,ok=not failed)
                    if not failed and kind == 'read' and result.get('sha256') != current_sha:
                        raise ValueError('read_content_hash_mismatch')
                    if not failed and kind == 'write':
                        expected = hashlib.sha256(content.encode()).hexdigest()
                        if result.get('sha256') != expected:
                            raise ValueError('write_content_hash_mismatch')
                        current_sha = result['sha256']
                    if failed:
                        error = result.get('error')
                        sample['error'] = safe_code(error.get('code', state) if isinstance(error, dict) else state)
                except Exception as exc:
                    # Stop admission for every worker. A missing HTTP response
                    # never authorizes another operation; recover this ID only.
                    abort.set()
                    sample['unresolved'] = True
                    sample['ok'] = False
                    sample['error'] = type(exc).__name__
                    try:
                        identifier = sample.get('operation_id') or lookup_original(
                            stack.hubdir/'hub.sqlite3', key)
                        sample['operation_id'] = identifier
                        _, _, recovery_failed, polls, state = await finish_original(
                            client, {'operation_id':identifier, 'pending':True}, False,
                            time.monotonic()+request_timeout)
                        sample.update(recovered=True, unresolved=False, polls=polls,
                                      state=state, recovered_failed=recovery_failed)
                    except Exception as recovery:
                        sample['recovery_error'] = type(recovery).__name__
                    # Retain type only: never put bearer, URL/query, response body,
                    # file content, command text or credential in measurement data.
                    sample['error'] = type(exc).__name__
                sample['latency_ms'] = (time.monotonic()-began)*1000
                samples.append(sample)
                with (output/'samples.jsonl').open('a') as raw:
                    raw.write(json.dumps(sample, sort_keys=True)+'\n')
        try:
            await asyncio.gather(*(worker(index) for index in range(concurrency)))
        finally:
            duration = time.monotonic()-start
            stop.set()
            await monitor
    enrich(stack.hubdir/'hub.sqlite3', samples)
    for label, rows in [('samples',samples),('resources',resources)]:
        with (output/(label+'.jsonl')).open('w') as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True)+'\n')
    summary = summarize(samples, duration)
    summary.update(workload=workload_name, concurrency=concurrency, operations_per_worker=operations,
                   admission_stopped=abort.is_set(), expected_samples=concurrency*operations,
                   unresolved=sum(bool(s.get('unresolved')) for s in samples),
                   resource_sampling_complete=bool(resources) and all(r.get('complete', False) for r in resources))
    (output/'summary.json').write_text(json.dumps(summary,indent=2)+'\n')
    return summary


async def disconnect_check(stack):
    """Disconnect an HTTP waiter after admission; recover ONLY its original ID."""
    import httpx
    key = uuid.uuid4().hex
    headers = {'Authorization':'Bearer '+stack.pat,'MCP-Protocol-Version':'2025-11-25'}
    counter = stack.projectalpha/'bench'/'disconnect-count.txt'
    args = {'project':'ProjectAlpha','task':'bench_disconnect','yield_seconds':10,'idempotency_key':key}
    async with httpx.AsyncClient(base_url=stack.url,headers=headers,trust_env=False,timeout=90) as client:
        call = asyncio.create_task(rpc(client,'exec',args))
        row = None
        try:
            deadline = time.monotonic()+20
            while time.monotonic()<deadline and not call.done():
                with sqlite3.connect(f'file:{stack.hubdir/"hub.sqlite3"}?mode=ro',uri=True) as db:
                    db.row_factory = sqlite3.Row
                    row = db.execute('SELECT id,state FROM operations WHERE idem=?',(key,)).fetchone()
                if row is not None and row['state']=='running':
                    break
                await asyncio.sleep(.05)
            if row is None or row['state']!='running' or call.done():
                return {'exercised':False,'passed':False,'reason':'did_not_observe_active_waiter'}
            identifier = row['id']
            call.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await call
            _, _, failed, polls, state = await finish_original(
                client, {'pending':True,'operation_id':identifier}, False, time.monotonic()+90)
            with sqlite3.connect(f'file:{stack.hubdir/"hub.sqlite3"}?mode=ro',uri=True) as db:
                count = db.execute('SELECT COUNT(*) FROM operations WHERE idem=?',(key,)).fetchone()[0]
            effects = len(counter.read_text().splitlines()) if counter.exists() else 0
            return {'exercised':True,'original_operation_id':identifier,'state':state,'polls':polls,
                    'operation_rows':count,'side_effect_count':effects,'passed':not failed and count==effects==1}
        finally:
            if not call.done():
                call.cancel()
            with contextlib.suppress(asyncio.CancelledError,Exception):
                await call


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--variant',choices=['before','after'],required=True)
    parser.add_argument('--concurrency',default='8,16,32,64,128')
    parser.add_argument('--workloads',default='read,read_write,long_short')
    parser.add_argument('--operations-per-worker',type=int,default=4)
    parser.add_argument('--request-timeout',type=float,default=120)
    parser.add_argument('--skip-disconnect-check',action='store_true')
    parser.add_argument('--preflight-seconds',type=float,default=5)
    parser.add_argument('--max-load-per-cpu',type=float,default=1)
    parser.add_argument('--allow-noisy',action='store_true',help='Record a noisy diagnostic, never a clean comparison')
    parser.add_argument('--host-context',required=True,help='Known concurrent production/test activity, never assume quiet')
    options = parser.parse_args()
    repository = options.repository.resolve()
    output = options.output.resolve()
    if not (repository/'.git').exists():
        parser.error('repository must be an existing Git checkout')
    if not output.is_relative_to(Path.cwd().resolve()/'.work') or output.exists():
        parser.error('output must be a NEW directory under current workspace .work')
    levels = [int(v) for v in options.concurrency.split(',')]
    names = options.workloads.split(',')
    if (not levels or len(set(levels))!=len(levels) or len(set(names))!=len(names)
            or any(v not in {1,8,16,32,64,128} for v in levels)
            or not set(names)<={'read','read_write','long_short'}):
        parser.error('invalid workload or concurrency')
    if (not 1<=options.operations_per_worker<=32 or not 10<=options.request_timeout<=300
            or not 0<=options.preflight_seconds<=60 or not .1<=options.max_load_per_cpu<=10):
        parser.error('invalid operation count or timeout')
    # The chosen checkout owns every product import and subprocess cwd. The
    # harness remains byte-identical across variants and records its own SHA.
    sys.path.insert(0,str(repository))
    from tests.support import running_stack
    from shared.util import atomic_json
    output.mkdir(parents=True)
    manifest = {'variant':options.variant,'repository':git_identity(repository),
        'harness_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'python':sys.version,'platform':platform.platform(),'cpu_count':os.cpu_count(),
        'host_context':options.host_context,'started_at':time.time(),'loadavg_start':list(os.getloadavg()),
        'workloads':names,'concurrency':levels,'operations_per_worker':options.operations_per_worker,
        'measurement_notes':['Loopback fixture, not ChatGPT host or public WAN',
            'Hub queue: admission to first dispatch, Hub clock only',
            'Agent queue: relative accepted/wait to executing within one attempt',
            'Resource CPU is ps lifetime-average; RSS KiB only Hub+Agent, not aggregate child tree',
            'Percentiles use nearest rank; errors remain in all-latency samples']}
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    load_samples, overloaded = preflight(options.preflight_seconds, options.max_load_per_cpu)
    manifest.update(preflight=load_samples, overloaded=overloaded,
                    max_load_per_cpu=options.max_load_per_cpu, allow_noisy=options.allow_noisy)
    if overloaded and not options.allow_noisy:
        manifest.update(result='blocked_by_host_load', ended_at=time.time())
        (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        return 2
    summaries = []
    usage_start = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        with running_stack(output/'fixture') as stack:
            bench = stack.projectalpha/'bench'
            bench.mkdir()
            stack.stop_agent()
            for task, seconds, filename in [('bench_long',.4,'long-count.txt'),
                                             ('bench_disconnect',2,'disconnect-count.txt')]:
                command = 'import pathlib,time; time.sleep('+str(seconds)+'); p=pathlib.Path("bench/'+filename+'"); f=p.open("a"); f.write("executed\\n"); f.close(); print("done")'
                stack.config['tasks'][task] = {'command':[sys.executable,'-u','-c',command],
                    'projects':['ProjectAlpha'],'timeout':30,'allow_read_concurrency':True}
            atomic_json(stack.config_path,stack.config)
            stack.start_agent()
            for name in names:
                for concurrency in levels:
                    for index in range(concurrency):
                        (bench/f'worker-{index}.txt').write_bytes(b'initial\n')
                    directory = output/(name+'-'+str(concurrency))
                    directory.mkdir()
                    summary = asyncio.run(workload(stack,directory,name,concurrency,
                        options.operations_per_worker,options.request_timeout))
                    summaries.append(summary)
                    print(json.dumps(summary),flush=True)
                    with sqlite3.connect(f'file:{stack.hubdir/"hub.sqlite3"}?mode=ro',uri=True) as db:
                        active = db.execute("SELECT COUNT(*) FROM operations WHERE state IN ('queued','running','reconnecting','cancelling','unknown')").fetchone()[0]
                    if active or summary['admission_stopped']:
                        manifest.update(result='stopped_with_unresolved_operations', active_operations=active,
                                        ended_at=time.time())
                        (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
                        (output/'summary.json').write_text(json.dumps(summaries,indent=2)+'\n')
                        raise RuntimeError('Fixture operations remain unresolved; do not contaminate the next workload')
            if not options.skip_disconnect_check:
                manifest['disconnect'] = asyncio.run(disconnect_check(stack))
            else:
                manifest['disconnect'] = {'exercised':False, 'passed':False, 'reason':'explicitly_skipped'}
    except Exception as exc:
        manifest.setdefault('result', 'failed')
        manifest['error'] = type(exc).__name__
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    manifest.update(ended_at=time.time(),loadavg_end=list(os.getloadavg()),
                    child_cpu_user_seconds=usage.ru_utime-usage_start.ru_utime,
                    child_cpu_system_seconds=usage.ru_stime-usage_start.ru_stime,
                    child_maxrss_native_units=usage.ru_maxrss)
    manifest.setdefault('result', 'completed_with_errors' if any(s['errors'] or not s['resource_sampling_complete'] for s in summaries)
                    or not manifest.get('disconnect', {}).get('passed') else 'completed')
    manifest['repository_end'] = git_identity(repository)
    manifest['source_unchanged'] = manifest['repository'] == manifest['repository_end']
    if not manifest['source_unchanged']:
        manifest['result'] = 'source_changed_during_run'
    (output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (output/'summary.json').write_text(json.dumps(summaries,indent=2)+'\n')

    return 0 if manifest['result'] == 'completed' else 1


if __name__=='__main__':
    raise SystemExit(main())
