"""Focused regressions for the 2026-09-22 flow audit.

All process trees, configuration files and journals are disposable test fixtures.
No installed Agent, account, real browser profile or external model is modified.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest


@pytest.mark.skipif(os.name == 'nt', reason='POSIX owned-group cleanup; Windows uses its Job Object')
def test_stopped_leader_does_not_leave_a_sigterm_ignoring_descendant(tmp_path):
    from agent.owned_process_group import stop_owned_group

    ticks = tmp_path / 'fixture-ticks'
    ready = tmp_path / 'fixture-ready'
    program = r'''
import os, signal, sys, time
from pathlib import Path
child = os.fork()
if child == 0:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    Path(sys.argv[2]).write_text('ready')
    while True:
        with open(sys.argv[1], 'ab', buffering=0) as stream:
            stream.write(b'x')
        time.sleep(.01)
else:
    while True:
        time.sleep(.02)
'''
    parent = subprocess.Popen([sys.executable, '-c', program, str(ticks), str(ready)],
                              start_new_session=True)
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert ready.exists(), 'the disposable descendant did not initialize'
        cleaned, parent.returncode = stop_owned_group(parent.pid, grouped=True)
        assert cleaned, 'a leader exit alone must never be reported as cleanup'
        count = ticks.stat().st_size
        time.sleep(.15)
        assert ticks.stat().st_size == count, 'the descendant still produced side effects'
    finally:
        if parent.returncode is None:
            _, parent.returncode = stop_owned_group(parent.pid, grouped=True)


def records_fixture():
    from agent.integration_state import Records

    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    return Records(SimpleNamespace(db=db, lock=threading.RLock())), db


def test_project_stop_releases_all_workspaces_but_not_other_mappings():
    from agent.background_browser import BrowserBroker

    records, db = records_fixture()
    project = {'id': 'project-one', 'root': '/fixture/project',
               '_coding_device': 'fixture-device', '_integration_owner': 'owner-a'}
    variants = [project, {**project, '_workspace_id': 'w' * 32, '_integration_owner': 'owner-b'},
                {**project, 'id': 'project-two'}, {**project, 'root': '/fixture/other'},
                {**project, '_coding_device': 'another-device'}]
    ids = [f'{index + 1:032x}' for index in range(len(variants))]
    try:
        for identifier, bound in zip(ids, variants):
            records.save('browser', identifier, bound,
                         {'lease_id': identifier, 'state': 'ready', 'expires': time.time() + 60,
                          'observation_id': 'fixture-observation', 'observation_token': 'fixture-token'})
        broker = BrowserBroker(SimpleNamespace(config={}), records)
        closed = []

        async def close(action, body, **kwargs):
            assert action == 'close'
            closed.append(body['lease_id'])
            return {'tab_cleanup_confirmed': True}

        broker.rpc = close
        result = asyncio.run(broker.release_project(project))
        assert set(closed) == set(ids[:2])
        assert len(result) == 2 and all(item['tab_cleanup_confirmed'] for item in result)
        for identifier, bound in zip(ids[:2], variants[:2]):
            row = records.load('browser', identifier, bound)
            assert row['state'] == 'closed' and row['observation_token'] is None
        for identifier, bound in zip(ids[2:], variants[2:]):
            assert records.load('browser', identifier, bound)['state'] == 'ready'
        assert asyncio.run(broker.release_project(project)) == []
    finally:
        db.close()


def test_cross_workspace_enumeration_still_requires_internal_owner_cleanup():
    from shared.util import DevError

    records, db = records_fixture()
    try:
        with pytest.raises(DevError) as error:
            records.project_entries('browser', {'id': 'p', 'root': '/fixture', '_integration_owner': 'owner'})
        assert error.value.status == 403
    finally:
        db.close()


def fixture_config(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    return {'device_id': 'd' * 32, 'secret': 'disposable-test-secret-' + 'a' * 64,
            'hub_url': 'http://127.0.0.1:8765', 'state_dir': str(tmp_path / 'state'),
            'allowed_roots': [{'path': str(root), 'writable': True, 'allow_tasks': False}],
            'tasks': {}, 'computer': {'enabled': False, 'projects': ['existing-project']}}


def test_integration_project_authorization_merge_is_additive_until_explicit_replace(tmp_path):
    from agent.setup_integrations import configure

    path = tmp_path / 'config.json'
    config = fixture_config(tmp_path)
    config['integrations'] = {'language_servers': {'python': {
        'command': [sys.executable, '--version'], 'projects': ['existing-project']}}}
    path.write_text(json.dumps(config), encoding='utf-8')
    configure(path, {'language_servers': {'python': {'projects': ['second-project']}}}, apply=True)
    assert json.loads(path.read_text(encoding='utf-8'))['integrations']['language_servers']['python']['projects'] == ['existing-project', 'second-project']
    configure(path, {'language_servers': {'python': {'projects': ['third-project']}}}, apply=True, replace_projects=True)
    assert json.loads(path.read_text(encoding='utf-8'))['integrations']['language_servers']['python']['projects'] == ['third-project']
    assert json.loads(path.read_text(encoding='utf-8'))['computer'] == config['computer']


def stale_fixture(tmp_path):
    state = tmp_path / 'state'
    state.mkdir()
    path = state / 'agent.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE calls (id TEXT PRIMARY KEY,status TEXT,result TEXT,acked INTEGER)')
        db.execute("INSERT INTO calls VALUES ('fixture-operation','running',NULL,0)")
    return state, path


def test_stale_journal_recovery_is_explicit_and_never_replays_or_reports_success(tmp_path):
    from scripts.install_agent import require_idle

    state, path = stale_fixture(tmp_path)
    with pytest.raises(ValueError, match='recover-stale-operations'):
        require_idle(tmp_path, {'state_dir': str(state)})
    require_idle(tmp_path, {'state_dir': str(state)}, recover_stale_operations=True)
    with sqlite3.connect(path) as db:
        rows = db.execute('SELECT id,status,result FROM calls').fetchall()
    assert len(rows) == 1 and rows[0][:2] == ('fixture-operation', 'interrupted')
    result = json.loads(rows[0][2])
    assert result['ok'] is False and result['error']['retryable'] is False
    evidence = list(state.glob('maintenance-recovery-*.json'))
    assert len(evidence) == 1
    assert json.loads(evidence[0].read_text(encoding='utf-8'))['commands_reexecuted'] is False


def test_stale_recovery_refuses_a_live_agent_instance(tmp_path):
    from scripts.install_agent import require_idle
    from shared.instance_lock import InstanceLock

    state, path = stale_fixture(tmp_path)
    with InstanceLock(state / '.agent.lock'):
        with pytest.raises(ValueError, match='instance lock'):
            require_idle(tmp_path, {'state_dir': str(state)}, recover_stale_operations=True)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT status FROM calls').fetchone()[0] == 'running'
    assert not list(state.glob('maintenance-recovery-*.json'))


def test_repair_preserves_local_policy_and_history_paths(tmp_path, monkeypatch):
    from agent.__main__ import main

    config = fixture_config(tmp_path)
    config['tasks'] = {'test': {'command': ['python', '-V']}}
    # Preserve even inactive owner-approved options rather than reconstructing
    # the file from the limited fields contained in a pairing document.
    path = tmp_path / 'config.json'
    path.write_text(json.dumps(config, ensure_ascii=False), encoding='utf-8')
    pairing = tmp_path / 'pairing.json'
    pairing.write_text(json.dumps({'device_id': config['device_id'],
                                  'secret': 'new-disposable-test-secret-' + 'b' * 64,
                                  'hub_url': 'http://127.0.0.1:9876'}), encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['agent', '--config', str(path), 'init', '--re-pair', '--pairing-file', str(pairing)])
    main()
    updated = json.loads(path.read_text(encoding='utf-8'))
    assert updated['secret'] != config['secret'] and updated['hub_url'].endswith(':9876')
    for key in ('device_id', 'allowed_roots', 'state_dir', 'tasks', 'computer'):
        assert updated[key] == config[key]
    backup = list(tmp_path.glob('config.json.before-repair-*'))
    assert len(backup) == 1 and json.loads(backup[0].read_text(encoding='utf-8')) == config
