"""Bounded real-HTTP persistence measurements; no provider or native Dot traffic.

Keep the production 60-message/minute guard: five warmups plus fifty measured
writes per fresh fixture. This is a coarse local sample, not a 1000-request SLA.
"""
import json
import os
import platform
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from hub.store import Store
from tests.collaboration_support import collaboration_stack as collaboration_stack, key

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def hub_rss_kib(stack):
    if os.name != 'posix':
        return None
    result = subprocess.run(
        ['ps', '-o', 'rss=', '-p', str(stack.hub.pid)],
        capture_output=True, text=True, timeout=5, check=False,
    )
    value = result.stdout.strip()
    return int(value) if result.returncode == 0 and value.isdigit() else None


@pytest.mark.parametrize('clients', [1, 5, 20])
def test_bounded_authenticated_http_persistence(collaboration_stack, clients):
    stack = collaboration_stack
    scope = {'project': stack.project['id'], 'environment_id': 'production'}
    room = stack.must(stack.client.post(
        '/api/collaboration/room', json={**scope, 'idempotency_key': key()}))['room']
    samples = [
        '本地短文本 synthetic input',
        '中文长文本 mixed history。' * 200,
        'Code sample:\n\x60\x60\x60python\n' + 'print("fixture")\n' * 80 + '\x60\x60\x60',
        '| synthetic | bounded |\n|---|---|\n' + '| 中文 | text |\n' * 80,
    ]

    def payload(index):
        return {**scope, 'room_id': room['id'], 'conversation_id': room['id'],
                'body_text': samples[index % len(samples)],
                'client_message_id': key(), 'idempotency_key': key()}

    # Fixture project setup owns three validation operations. Preserve their
    # exact identity set; ordinary chat must not add execution of its own.
    initial_store = Store(stack.hubdir)
    try:
        original_operations = {row['id'] for row in initial_store.all('SELECT id FROM operations')}
    finally:
        initial_store.close()
    for index in range(5):
        stack.must(stack.client.post('/api/collaboration/message', json=payload(index)))
    before_rss = hub_rss_kib(stack)
    barrier = threading.Barrier(clients)

    def worker(worker_index):
        measured = []
        with httpx.Client(base_url=stack.url, cookies=stack.client.cookies,
                          headers=stack.client.headers, timeout=35, trust_env=False) as client:
            barrier.wait(timeout=20)
            for index in range(worker_index, 50, clients):
                body = payload(index)
                start = time.perf_counter()
                response = client.post('/api/collaboration/message', json=body)
                elapsed = (time.perf_counter() - start) * 1000
                assert response.status_code == 200, response.text
                result = response.json()
                assert result['message']['client_message_id'] == body['client_message_id']
                assert not result['scheduled']
                measured.append((elapsed, result['message']['id']))
        return measured

    with ThreadPoolExecutor(max_workers=clients) as executor:
        results = [row for group in executor.map(worker, range(clients)) for row in group]
    assert len(results) == 50 and len({row[1] for row in results}) == 50
    store = Store(stack.hubdir)
    try:
        assert store.one('SELECT COUNT(*) n FROM collaboration_messages WHERE room_id=?',
                         (room['id'],))['n'] == 55
        assert store.one('SELECT COUNT(*) n FROM coordination_work')['n'] == 0
        assert {row['id'] for row in store.all('SELECT id FROM operations')} == original_operations
    finally:
        store.close()
    timings = sorted(row[0] for row in results)
    after_rss = hub_rss_kib(stack)
    report = {
        'scenario': 'authenticated_message_persistence_http',
        'clients': clients, 'samples': 50, 'warmup_writes': 5,
        'bodies': ['short', 'long_mixed', 'code_block', 'table'],
        'p50_ms': statistics.median(timings), 'p95_ms': timings[47],
        'max_ms': max(timings), 'error_rate': 0,
        'hub_rss_before_kib': before_rss, 'hub_rss_after_kib': after_rss,
        'hub_rss_delta_kib': after_rss - before_rss if before_rss is not None and after_rss is not None else None,
        'python': platform.python_version(), 'platform': platform.platform(),
        'method': 'Real loopback HTTP, current authentication and transaction, same authorized owner/project; distinct pooled clients. No security or rate-limit override.',
        'limits': 'Fifty observations to remain under the unchanged sixty-per-minute author/project guard. No native host, provider, multi-project fairness, or public network; coarse local p95 only.',
    }
    print(json.dumps(report, sort_keys=True))
    destination = os.environ.get('CODEPIER_EXPERIENCE_BENCHMARK_REPORT')
    if destination:
        path = Path(destination)
        path.mkdir(parents=True, exist_ok=True)
        (path / ('http-persistence-' + str(clients) + '.json')).write_text(
            json.dumps(report, indent=2))
