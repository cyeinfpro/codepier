#!/usr/bin/env python3
"""Opt-in Session benchmark: mock scheduling or real loopback HTTP/1.1.

No public endpoint, real account, Hub, Agent, browser, model or dependency install.
The baseline is the archived pre-parallel Session module, not a full old release.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import resource
import sys
import time

import httpx

try:
    from scripts import benchmark_hub_agent as common
except ModuleNotFoundError:
    import benchmark_hub_agent as common

BASELINE_SHA256 = '141d115d0b9fd636a6694ea2047a7acfb9181d47a3770331da23dd52bb8dc989'
WORKLOADS = {'read', 'read_write', 'long_short'}


def load_module(path, *, expected_sha256=None):
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise ValueError('baseline_module_sha256_mismatch')
    spec = importlib.util.spec_from_file_location('benchmark_remote_' + digest, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, digest


class Backend:
    """One disposable synthetic backend. Never records headers or payloads."""
    def __init__(self, protocol, short_seconds, long_seconds):
        self.protocol = protocol
        self.short_seconds, self.long_seconds = short_seconds, long_seconds
        self.events, self.values = [], {}
        self.active = self.peak = self.connections = 0
        self.probe_received = asyncio.Event()
        self.idle = asyncio.Event()
        self.idle.set()

    async def respond(self, body, connection=None):
        method = body['method']
        headers = {}
        if method == 'server/discover':
            if self.protocol == 'legacy':
                return 400, headers, {'jsonrpc':'2.0', 'id':body['id'], 'error':{'code':-32601}}
            result = {'supportedVersions':['2026-07-28'], 'capabilities':{'tools':{}}}
        elif method == 'initialize':
            headers['Mcp-Session-Id'] = 'synthetic-benchmark-session'
            result = {'protocolVersion':'2025-11-25', 'capabilities':{'tools':{}}}
        elif method == 'notifications/initialized':
            return 202, headers, None
        elif method == 'tools/call':
            args = body['params']['arguments']
            number, kind = args['number'], args['kind']
            event = {'number':number, 'kind':kind, 'received_at':time.time(),
                     'connection':connection, 'outcome':'active'}
            self.events.append(event)
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.idle.clear()
            began = time.monotonic()
            try:
                if number == -2:
                    self.probe_received.set()
                delay = max(.1, self.long_seconds) if number == -2 else (
                    self.long_seconds if kind == 'long' else self.short_seconds)
                await asyncio.sleep(delay)
                if kind == 'write':
                    worker = args['worker']
                    self.values[worker] = self.values.get(worker, 0) + 1
                result = {'content':[], 'structuredContent':{'number':number}}
                event['outcome'] = 'completed'
            except asyncio.CancelledError:
                event['outcome'] = 'cancelled_in_mock'
                raise
            finally:
                event.update(service_ms=(time.monotonic()-began)*1000, ended_at=time.time())
                self.active -= 1
                if not self.active:
                    self.idle.set()
        else:
            raise ValueError('unexpected_fixture_method')
        return 200, headers, {'jsonrpc':'2.0', 'id':body['id'], 'result':result}

    async def mock(self, request):
        status, headers, payload = await self.respond(json.loads(request.content))
        return httpx.Response(status, headers=headers, json=payload, request=request)


@contextlib.asynccontextmanager
async def fixture(module, transport_name, protocol, short_seconds, long_seconds):
    backend = Backend(protocol, short_seconds, long_seconds)
    server, handlers, writers, handler_errors = None, set(), set(), []
    port = 1

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        writers.add(writer)
        backend.connections += 1
        connection = backend.connections
        try:
            while await reader.readline():
                headers = {}
                while (line := await reader.readline()) not in (b'\r\n', b''):
                    key, value = line.decode('ascii').split(':', 1)
                    headers[key.lower()] = value.strip()
                length = int(headers.get('content-length', 0))
                if not 0 < length < 65536:
                    raise ValueError('fixture_body_size')
                body = json.loads(await reader.readexactly(length))
                status, extra, payload = await backend.respond(body, connection)
                raw = json.dumps(payload).encode() if payload is not None else b''
                extra_headers = ''.join(f'{key}: {value}\r\n' for key, value in extra.items()).encode()
                writer.write(f'HTTP/1.1 {status} Fixture\r\n'.encode()
                    + b'Content-Type: application/json\r\nConnection: keep-alive\r\n'
                    + extra_headers + f'Content-Length: {len(raw)}\r\n\r\n'.encode() + raw)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            # Expected only in the explicitly labelled cancellation probe.
            handler_errors.append('peer_disconnected')
        except Exception as exc:
            handler_errors.append(type(exc).__name__)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            handlers.discard(task)
            writers.discard(writer)

    if transport_name == 'loopback':
        server = await asyncio.start_server(handle, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]

    async def resolve(host, requested_port):
        if host != 'localhost' or requested_port != port:
            raise ValueError('non_fixture_destination')
        return ['127.0.0.1']

    config = {'endpoint':f'http://localhost:{port}/mcp', 'protocol':'auto',
              'networks':'["127.0.0.1/32"]', 'allow_http':1}
    transport = httpx.MockTransport(backend.mock) if transport_name == 'mock' else None
    session = module.Session(config, '', resolver=resolve, transport=transport)
    try:
        yield session, backend, handler_errors
    finally:
        await session.close()
        if server is not None:
            server.close()
            await server.wait_closed()
        for writer in list(writers):
            writer.close()
        if handlers:
            await asyncio.wait_for(asyncio.gather(*list(handlers)), 3)


def kind_for(workload, worker, step):
    if workload == 'read_write' and step % 4 == 0:
        return 'write'
    if workload == 'long_short' and worker % 8 == 0 and step % 4 == 0:
        return 'long'
    return 'read'


async def cancellation_probe(session, backend):
    """Cancel one sent waiter; observe the same request count, never replay it."""
    async def call(number):
        return await session.call('fixture', {'number':number, 'kind':'long', 'worker':0}, lambda:None)
    task = asyncio.create_task(call(-2))
    report = {'exercised':False, 'passed':False}
    try:
        await asyncio.wait_for(backend.probe_received.wait(), 3)
        report['exercised'] = True
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            report['caller_state'] = 'cancelled_result_unknown'
        await asyncio.wait_for(backend.idle.wait(), 3)
        await call(-3)
        original = [e for e in backend.events if e['number'] == -2]
        controls = [e for e in backend.events if e['number'] == -3]
        report.update(original_received=len(original), control_received=len(controls),
                      original_outcomes=[e['outcome'] for e in original])
        report['passed'] = len(original) == len(controls) == 1 and not backend.active
    except Exception as exc:
        report['error'] = type(exc).__name__
    finally:
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
    return report


async def run_phase(module, *, transport_name, protocol, workload, concurrency,
                    operations, short_seconds, long_seconds, sample_resources=True):
    samples, resources = [], []
    abort, stop = asyncio.Event(), asyncio.Event()
    async with fixture(module, transport_name, protocol, short_seconds, long_seconds) as (
            session, backend, handler_errors):
        # One transport warmup before timing, omitted from sample and resource sets.
        await session.call('fixture', {'number':-1, 'kind':'read', 'worker':0}, lambda:None)
        connections_after_warmup = backend.connections
        backend.events.clear()
        backend.peak = 0
        monitor = asyncio.create_task(common.sample_pids(
            {os.getpid():'gateway_and_fixture'}, stop, resources)) if sample_resources else None
        cpu_start = resource.getrusage(resource.RUSAGE_SELF)
        start = time.monotonic()

        async def worker(index):
            for step in range(operations):
                if abort.is_set():
                    break
                number = index * operations + step
                kind = kind_for(workload, index, step)
                began = time.monotonic()
                row = {'worker':index, 'step':step, 'number':number, 'kind':kind,
                       'started_at':time.time(), 'ok':False, 'error':None, 'queue_ms':None}
                def before_send():
                    row['queue_ms'] = (time.monotonic()-began)*1000
                try:
                    result = await session.call('fixture', {'number':number, 'kind':kind,
                                                'worker':index}, before_send)
                    if result.get('isError') or result.get('structuredContent', {}).get('number') != number:
                        raise ValueError('unexpected_fixture_result')
                    row['ok'] = True
                except Exception as exc:
                    row['error'] = type(exc).__name__
                    abort.set()
                row['latency_ms'] = (time.monotonic()-began)*1000
                samples.append(row)

        try:
            await asyncio.gather(*(worker(i) for i in range(concurrency)))
        finally:
            duration = time.monotonic()-start
            cpu_end = resource.getrusage(resource.RUSAGE_SELF)
            stop.set()
            if monitor:
                await monitor
        # Drain only the fixture; do not launch probes or another phase while
        # an HTTP-side request could still be running.
        try:
            await asyncio.wait_for(backend.idle.wait(), 3)
        except TimeoutError:
            abort.set()
        measured_events = list(backend.events)
        measured_errors = list(handler_errors)
        summary = common.summarize(samples, duration)
        counts = {number:sum(e['number'] == number for e in measured_events)
                  for number in {e['number'] for e in measured_events}}
        summary.update(transport=transport_name, protocol=protocol, workload=workload,
            concurrency=concurrency, operations_per_worker=operations,
            admission_stopped=abort.is_set(), expected_samples=concurrency*operations,
            queue_ms=common.percentiles([s['queue_ms'] for s in samples]),
            queue_sample_count=sum(s['queue_ms'] is not None for s in samples),
            backend_peak_active=backend.peak, backend_requests=len(measured_events),
            duplicate_request_numbers=sorted(number for number, count in counts.items() if count != 1),
            connections_after_warmup=connections_after_warmup,
            connections_after_workload=backend.connections,
            cpu_user_seconds=cpu_end.ru_utime-cpu_start.ru_utime,
            cpu_system_seconds=cpu_end.ru_stime-cpu_start.ru_stime,
            resource_sampling_complete=(bool(resources) and all(r.get('complete', False) for r in resources))
                if sample_resources else None,
            backend_handler_errors=measured_errors, measured_synthetic_write_count=sum(backend.values.values()))
        for kind, item in summary['by_kind'].items():
            item['queue_ms'] = common.percentiles([s['queue_ms'] for s in samples if s['kind'] == kind])
        if not abort.is_set():
            summary['cancellation'] = await cancellation_probe(session, backend)
        else:
            summary['cancellation'] = {'exercised':False, 'passed':False, 'reason':'workload_error'}
        summary['complete'] = (
            len(samples) == concurrency*operations and not summary['errors']
            and not abort.is_set() and not summary['duplicate_request_numbers']
            and len(measured_events) == len(samples) and not measured_errors
            and summary['cancellation']['passed']
            and (not sample_resources or summary['resource_sampling_complete']))
        return summary, samples, resources, backend.events


def write_rows(path, rows):
    with path.open('w', encoding='utf-8') as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True)+'\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--variant', choices=['before', 'after'], required=True)
    parser.add_argument('--before-module', type=Path,
                        help='Explicit local archived module; not included in release/source packages')
    parser.add_argument('--transport', choices=['mock', 'loopback'], required=True)
    parser.add_argument('--protocols', default='modern,legacy')
    parser.add_argument('--workloads', default='read,read_write,long_short')
    parser.add_argument('--concurrency', default='8,16,32,64,128')
    parser.add_argument('--operations-per-worker', type=int, default=4)
    parser.add_argument('--short-seconds', type=float, default=.02)
    parser.add_argument('--long-seconds', type=float, default=.2)
    parser.add_argument('--preflight-seconds', type=float, default=5)
    parser.add_argument('--max-load-per-cpu', type=float, default=1)
    parser.add_argument('--allow-noisy', action='store_true')
    parser.add_argument('--host-context', required=True)
    args = parser.parse_args()
    repository, output = args.repository.resolve(), args.output.resolve()
    if not (repository/'.git').exists():
        parser.error('repository must be an existing Git checkout')
    if not output.is_relative_to(Path.cwd().resolve()/'.work') or output.exists():
        parser.error('output must be a NEW directory under current workspace .work')
    try:
        levels = [int(x) for x in args.concurrency.split(',')]
    except ValueError:
        parser.error('invalid concurrency')
    names, protocols = args.workloads.split(','), args.protocols.split(',')
    if (not levels or len(set(levels)) != len(levels) or not set(levels) <= {1,8,16,32,64,128}
            or len(set(names)) != len(names) or not set(names) <= WORKLOADS
            or len(set(protocols)) != len(protocols) or not set(protocols) <= {'modern','legacy'}
            or not 1 <= args.operations_per_worker <= 32
            or not 0 <= args.short_seconds <= args.long_seconds <= 2
            or not 0 <= args.preflight_seconds <= 60 or not .1 <= args.max_load_per_cpu <= 10):
        parser.error('invalid benchmark parameters')
    sys.path.insert(0, str(repository))
    if args.variant == 'before' and args.before_module is None:
        parser.error('before requires --before-module; the local snapshot is not shipped in source packages')
    if args.variant == 'after' and args.before_module is not None:
        parser.error('--before-module is only valid with --variant before')
    module_path = (args.before_module.resolve() if args.variant == 'before'
                   else repository/'hub/gateway/remote.py')
    if not module_path.is_file():
        parser.error('selected module does not exist; obtain the reviewed baseline explicitly')

    module, module_sha = load_module(module_path,
        expected_sha256=BASELINE_SHA256 if args.variant == 'before' else None)
    output.mkdir(parents=True)
    source = common.git_identity(repository)
    manifest = {'variant':args.variant, 'scope':'Session transport microbenchmark',
        'transport':args.transport, 'repository':source, 'module_sha256':module_sha,
        'baseline_is_archived_module_not_full_checkout':True,
        'harness_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'shared_harness_sha256':hashlib.sha256(Path(common.__file__).read_bytes()).hexdigest(),
        'python':sys.version, 'platform':platform.platform(), 'cpu_count':os.cpu_count(),
        'host_context':args.host_context, 'started_at':time.time(),
        'workloads':names, 'protocols':protocols, 'concurrency':levels,
        'operations_per_worker':args.operations_per_worker,
        'short_seconds':args.short_seconds, 'long_seconds':args.long_seconds,
        'notes':['mock excludes socket/network overhead and cannot establish real throughput gains',
            'loopback is HTTP/1.1; no TLS, public WAN, Hub, Agent or real backend service',
            'queue_ms uses before_send callback, same instrument for both Session implementations',
            'CPU/RSS includes the harness and in-process backend; ps CPU is lifetime average',
            'read/write uses synthetic in-memory counters, not real repository I/O',
            'cancellation means unknown at client; raw backend evidence is never replayed',
            'authorization/account/binding-version isolation is covered by separate regression tests']}
    def save():
        (output/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    save()
    load, overloaded = common.preflight(args.preflight_seconds, args.max_load_per_cpu)
    manifest.update(preflight=load, overloaded=overloaded, allow_noisy=args.allow_noisy,
                    max_load_per_cpu=args.max_load_per_cpu)
    if overloaded and not args.allow_noisy:
        manifest.update(result='blocked_by_host_load', ended_at=time.time())
        save()
        return 2
    summaries = []
    try:
        for protocol in protocols:
            for name in names:
                for level in levels:
                    summary, samples, resources, events = asyncio.run(run_phase(module,
                        transport_name=args.transport, protocol=protocol, workload=name,
                        concurrency=level, operations=args.operations_per_worker,
                        short_seconds=args.short_seconds, long_seconds=args.long_seconds))
                    directory = output/f'{protocol}-{name}-{level}'
                    directory.mkdir()
                    for label, rows in [('samples',samples), ('resources',resources), ('backend',events)]:
                        write_rows(directory/(label+'.jsonl'), rows)
                    (directory/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
                    summaries.append(summary)
                    print(json.dumps(summary), flush=True)
                    if not summary['complete']:
                        raise RuntimeError('incomplete_phase_do_not_continue')
        manifest['result'] = 'completed'
    except Exception as exc:
        manifest.update(result='failed', error=type(exc).__name__)
    finally:
        manifest.update(ended_at=time.time(), loadavg_end=list(os.getloadavg()),
                        repository_end=common.git_identity(repository))
        manifest['source_unchanged'] = (source == manifest['repository_end']
            and module_sha == hashlib.sha256(module_path.read_bytes()).hexdigest())
        if not manifest['source_unchanged']:
            manifest['result'] = 'source_changed_during_run'
        save()
        (output/'summary.json').write_text(json.dumps(summaries, indent=2)+'\n')
    return 0 if manifest['result'] == 'completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
