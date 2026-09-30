"""Closeout regressions for effective readiness and trustworthy timing.

Uses isolated state and the ordinary authenticated Hub/Agent fixture, not a model.
"""
from __future__ import annotations
import asyncio
import json
import sqlite3
from types import SimpleNamespace
import uuid
import pytest

from hub.integrations import HubIntegrations
from tests.test_agentdock_context import workspace
from tests.test_integrations_stack import integrated_stack


class TimingStore:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row

    def execute(self, sql, args=()):
        return self.db.execute(sql, args)

    def all(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args)]


@pytest.mark.parametrize('finish_order', [(0, 1), (1, 0)])
def test_overlapping_calls_do_not_fabricate_a_serial_gap(finish_order):
    store = TimingStore()
    try:
        runtime = SimpleNamespace(store=store)
        service = HubIntegrations(runtime)
        principal = SimpleNamespace(grant_id='grant', actor='actor')
        project = {'id':'project', 'root':'/fixture', 'device_id':'device'}
        traces = [service.begin(principal, project, 'fs_read', {'openai/session':'same-window'}) for _ in range(2)]
        for index in finish_order:
            service.finish(traces[index])
        following = service.begin(principal, project, 'fs_read', {'openai/session':'same-window'})
        service.finish(following)
        rows = store.all('SELECT * FROM mcp_activity ORDER BY id')
        assert all(row['next_call_gap_ms'] is None for row in rows[:2])
        assert all(row['transition'] == 'overlap' for row in rows[:2])
        assert rows[2]['transition'] == 'gap_unavailable'
        assert not service.active
    finally:
        store.db.close()


def test_interrupted_call_breaks_timing_continuity_but_serial_calls_measure_it():
    store = TimingStore()
    try:
        service = HubIntegrations(SimpleNamespace(store=store))
        principal = SimpleNamespace(grant_id='grant', actor='actor')
        project = {'id':'project', 'root':'/fixture', 'device_id':'device'}
        def start():
            return service.begin(principal, project, 'fs_read', {'openai/session':'same-window'})
        first = start(); service.finish(first)
        second = start(); service.finish(second, status='interrupted')
        third = start(); service.finish(third)
        rows = store.all('SELECT * FROM mcp_activity ORDER BY id')
        assert rows[0]['next_call_gap_ms'] is not None
        assert rows[1]['next_call_gap_ms'] is None
        assert rows[2]['transition'] == 'gap_unavailable'
    finally:
        store.db.close()


def test_readiness_does_not_present_project_configuration_as_caller_authority(integrated_stack):
    s = integrated_stack
    grant = s.client.post('/api/grants', json={
        'label':'Readiness read-only caller', 'scopes':['read'],
        'projects':[s.project['id']], 'days':1,
    }).json()
    try:
        result = s.mcp('workspace', {'project': 'Imago', 'operation': 'readiness'}, token_value=grant['token'])['structuredContent']
        if result.get('pending'):
            operation = s.poll(result['operation_id'])
            assert operation['state'] == 'succeeded', operation
            result = operation['result']['data']
        checks = {item['name']:item for item in result['checks']}
        assert checks['project_read']['state'] == 'ready'
        assert checks['project_write']['state'] == 'denied'
        assert checks['shell']['state'] == 'denied'
        assert result['execution']['shell']['enabled'] is True  # local configuration is still shown, separately
        assert result['execution']['shell']['caller_permitted'] is False
    finally:
        s.client.delete('/api/grants/' + grant['grant_id'])


@pytest.mark.parametrize('enabled', [False, True])
def test_browser_listener_does_not_imply_owner_controls_enabled(workspace, enabled):
    from agent.integrations import Integrations
    engine, project, root = workspace
    service = object.__new__(Integrations)
    service.agent = SimpleNamespace(
        engine=engine, config={**engine.config, 'integrations':{'local_control':enabled}},
        build=SimpleNamespace(describe=lambda:{}),
    )
    service.local_server = object()  # It may exist solely for native browser messaging.
    service.browser = SimpleNamespace(status=lambda p:{'enabled':True, 'connected':True})
    service.control = SimpleNamespace(state=lambda p:{'paused':False})
    result = asyncio.run(service.execute(uuid.uuid4().hex, 'readiness_get',
        {**project, '_coding_scopes':['read'], '_integration_admin':False}, {}))
    assert result['local_control'] is enabled
    assert next(c for c in result['checks'] if c['name']=='browser')['state'] == 'denied'


def test_timing_storage_error_releases_inflight_ownership():
    store = TimingStore()
    try:
        service = HubIntegrations(SimpleNamespace(store=store))
        principal = SimpleNamespace(grant_id='grant', actor='actor')
        project = {'id':'project', 'root':'/fixture', 'device_id':'device'}
        trace = service.begin(principal, project, 'fs_read', {'openai/session':'same-window'})
        original = store.execute
        def broken(*args):
            raise sqlite3.OperationalError('controlled write failure')
        store.execute = broken
        service.finish(trace)
        assert service.write_errors == 1 and not service.active and not service.previous
        store.execute = original
        following = service.begin(principal, project, 'fs_read', {'openai/session':'same-window'})
        assert following['transition'] == 'gap_unavailable'
        service.finish(following)
    finally:
        store.db.close()


@pytest.mark.parametrize('change', [{'root':'/moved'}, {'device_id':'replacement'}])
def test_mapping_changes_never_inherit_previous_timing(change):
    store = TimingStore()
    try:
        service = HubIntegrations(SimpleNamespace(store=store))
        principal = SimpleNamespace(grant_id='grant', actor='actor')
        project = {'id':'project', 'root':'/fixture', 'device_id':'device'}
        first = service.begin(principal, project, 'fs_read', {'openai/session':'same-window'})
        service.finish(first)
        second = service.begin(principal, {**project, **change}, 'fs_read', {'openai/session':'same-window'})
        service.finish(second)
        assert second['transition'] == 'gap_unavailable'
        assert all(row['next_call_gap_ms'] is None for row in store.all('SELECT * FROM mcp_activity'))
    finally:
        store.db.close()


def test_source_package_keeps_closeout_evidence_but_excludes_private_runtime():
    from pathlib import Path
    from scripts.build_source_bundle import include
    for name in ('verification.json','full-regression.xml','feature-matrix.json','source-changes.json'):
        assert include(Path('docs/evidence/integration-closeout-20260917') / name)
    for path in ('docs/evidence/integration-closeout-20260917/before.zip',
                 'docs/evidence/integration-closeout-20260917/private/config.json',
                 'docs/evidence/integration-closeout-20260917/browser-profile/Cookies',
                 'docs/evidence/integration-closeout-20260917/raw.log',
                 'web/browser-extension/credentials.json','web/secret.woff2'):
        assert not include(Path(path))
