"""Durable scheduler and live authorization; no credentials or real services."""
import asyncio
import json
import time

import pytest

from hub.runtime import Runtime, Principal
from hub.store import Store
from hub.knowledge_maintenance import KnowledgeMaintenance, key_for
from shared.contracts import TOOLS
from shared.crypto import token
from shared.util import DevError
from tests.legacy_iam_fixture import seed_owner, seed_grant
from tests.collaboration_support import collab as collab, key

BUILD = 'project_knowledge_build'


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / 'hub')
    seed_owner(store, 'owner', 'admin')
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('dev','fixture',?,?)", (store.encrypt(token()), time.time()))
    store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,mode,created) VALUES ('p','Fixture','fixture','dev','/synthetic/project','write',?)", (time.time(),))
    seed_grant(store, 'writer', 'owner', scopes=('read', 'write'), projects=('p',))
    instance = Runtime(store)
    instance.wait_seconds = 0
    principal = Principal('mcp:writer:test', 'owner', {'read', 'write'}, ['p'], grant_id='writer')
    yield instance, principal
    store.close()


def register(runtime, *, enabled=True, key='fixture-initial-build'):
    instance, principal = runtime
    args = TOOLS[BUILD].model.model_validate({'project': 'p', 'idempotency_key': key, 'auto_refresh': enabled}).model_dump()
    project = instance.project('p', principal)
    identifier, _ = instance._admit_operation(BUILD, args, project, principal)
    return identifier, key_for(project, args), args, project


def due(instance):
    instance.store.execute('UPDATE knowledge_maintenance SET next_at=0')


def succeed(instance, identifier):
    from hub.operation_history import seal

    result = seal(instance.store, '{"ok":true,"data":{"state":"ready"}}')
    instance.store.execute("UPDATE operations SET state='succeeded',result=? WHERE id=?", (result, identifier))


def test_read_status_missing_is_pure(runtime):
    instance, principal = runtime
    with instance.store.lock:
        before = instance.store.db.total_changes
    assert instance.knowledge_maintenance.status(instance.project('p', principal), {})['state'] == 'disabled'
    with instance.store.lock:
        assert instance.store.db.total_changes == before
    assert not instance.knowledge_maintenance.exists()


def test_initial_build_registration_is_atomic_and_idempotent(runtime):
    instance, principal = runtime
    operation, identifier, args, project = register(runtime)
    row = instance.store.one('SELECT * FROM knowledge_maintenance')
    assert row['state'] == 'initializing' and row['operation_id'] == operation
    again, _ = instance._admit_operation(BUILD, args, project, principal)
    assert again == operation
    assert instance.store.one('SELECT count(*) AS n FROM knowledge_maintenance')['n'] == 1
    assert instance.store.one('SELECT * FROM knowledge_maintenance')['generation'] == row['generation']
    due(instance)
    assert instance.knowledge_maintenance.prepare(identifier) is None
    assert instance.store.one('SELECT count(*) AS n FROM operations')['n'] == 1


def test_only_successful_initial_build_activates_and_current_grant_required(runtime, monkeypatch):
    instance, _ = runtime
    operation, identifier, _, _ = register(runtime)
    succeed(instance, operation)
    due(instance)
    monkeypatch.setattr(instance, 'online', lambda _: True)
    prepared = instance.knowledge_maintenance.prepare(identifier)
    assert prepared and prepared[0]['incremental'] is True
    assert prepared[0]['auto_refresh'] is None
    instance.store.execute("UPDATE grants SET revoked=1 WHERE id='writer'")
    due(instance)
    assert instance.knowledge_maintenance.prepare(identifier) is None
    row = instance.store.one('SELECT * FROM knowledge_maintenance')
    assert row['state'] == 'paused' and not row['enabled']


@pytest.mark.parametrize('change', ["UPDATE grants SET scopes='[\"read\"]' WHERE id='writer'", "UPDATE projects SET mode='read' WHERE id='p'", "UPDATE projects SET root='/different' WHERE id='p'", "UPDATE projects SET device_id='second-dev' WHERE id='p'"])
def test_current_policy_and_mapping_rechecked(runtime, monkeypatch, change):
    instance, _ = runtime
    operation, identifier, _, _ = register(runtime)
    succeed(instance, operation)
    instance.store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('second-dev','synthetic second device',?,?)",
                           (instance.store.encrypt('synthetic-second-device'), time.time()))
    instance.store.execute(change)
    due(instance)
    monkeypatch.setattr(instance, 'online', lambda _: True)
    prepared = instance.knowledge_maintenance.prepare(identifier)
    assert prepared is None
    assert instance.store.one('SELECT state,enabled FROM knowledge_maintenance') == {'state': 'paused', 'enabled': 0}


def test_initial_failure_does_not_create_background_work(runtime):
    instance, _ = runtime
    operation, identifier, _, _ = register(runtime)
    instance.store.execute("UPDATE operations SET state='failed' WHERE id=?", (operation,))
    due(instance)
    assert instance.knowledge_maintenance.prepare(identifier) is None
    assert instance.store.one('SELECT state,enabled FROM knowledge_maintenance') == {'state': 'initial_build_failed', 'enabled': 0}


def test_building_cursor_and_scope_survive_offline_deferral(runtime, monkeypatch):
    import json
    instance, principal = runtime
    args = TOOLS[BUILD].model.model_validate({'project': 'p', 'idempotency_key': key(),
        'paths': ['src'], 'exclude_paths': ['src/generated'], 'batch_size': 2}).model_dump()
    project = instance.project('p', principal)
    operation, _ = instance._admit_operation(BUILD, args, project, principal)
    identifier = key_for(project, args)
    cursor = 'a' * 64
    instance.store.execute("UPDATE operations SET state='succeeded',result=? WHERE id=?",
        (json.dumps({'ok': True, 'data': {'state': 'building', 'cursor': cursor}}), operation))
    due(instance)
    monkeypatch.setattr(instance, 'online', lambda _: False)
    assert instance.knowledge_maintenance.prepare(identifier) is None
    assert instance.store.one('SELECT cursor FROM knowledge_maintenance')['cursor'] == cursor
    due(instance)
    monkeypatch.setattr(instance, 'online', lambda _: True)
    prepared = instance.knowledge_maintenance.prepare(identifier)
    assert prepared[0]['cursor'] == cursor and prepared[0]['batch_size'] == 2
    assert prepared[0]['paths'] == ['src'] and prepared[0]['exclude_paths'] == ['src/generated']
    assert instance.store.one('SELECT last_success FROM knowledge_maintenance')['last_success'] is None


def test_offline_and_user_work_defer_without_queueing(runtime, monkeypatch):
    instance, _ = runtime
    operation, identifier, _, _ = register(runtime)
    succeed(instance, operation)
    due(instance)
    assert instance.knowledge_maintenance.prepare(identifier) is None
    assert instance.store.one('SELECT state FROM knowledge_maintenance')['state'] == 'offline'
    instance.store.execute("UPDATE operations SET state='queued' WHERE id=?", (operation,))
    monkeypatch.setattr(instance, 'online', lambda _: True)
    due(instance)
    assert instance.knowledge_maintenance.prepare(identifier) is None
    assert instance.store.one('SELECT state FROM knowledge_maintenance')['state'] == 'deferred_busy'


def test_restart_resumes_same_pending_key_and_disable_fences_admission(runtime, monkeypatch):
    instance, principal = runtime
    operation, identifier, _, _ = register(runtime)
    succeed(instance, operation)
    due(instance)
    monkeypatch.setattr(instance, 'online', lambda _: True)
    first = instance.knowledge_maintenance.prepare(identifier)
    instance.knowledge_maintenance = KnowledgeMaintenance(instance)
    due(instance)
    second = instance.knowledge_maintenance.prepare(identifier)
    assert second[0]['idempotency_key'] == first[0]['idempotency_key']
    # Disable admission is an ordinary authorized write with a new generation.
    monkeypatch.setattr(instance, 'online', lambda _: False)
    register(runtime, enabled=False, key='fixture-disable-build')
    with pytest.raises(DevError, match='配置已变化'):
        instance._admit_operation(BUILD, first[0], first[1], principal, True, first[3])
    assert instance.store.one('SELECT state FROM knowledge_maintenance')['state'] == 'disabled'


def test_pending_outcome_never_replayed_and_uncertainty_pauses(runtime):
    instance, _ = runtime
    operation, identifier, _, _ = register(runtime)
    instance.store.execute("UPDATE operations SET state='needs_review' WHERE id=?", (operation,))
    due(instance)
    assert instance.knowledge_maintenance.prepare(identifier) is None
    assert instance.store.one('SELECT state FROM knowledge_maintenance')['state'] == 'needs_review'


@pytest.mark.asyncio
async def test_lifecycle_is_owned_and_stoppable(runtime):
    instance, _ = runtime
    service = instance.knowledge_maintenance
    await service.start()
    first = service.task
    await service.start()
    assert service.task is first
    await asyncio.sleep(0)
    await service.stop()
    assert first.done() and service.task is None


def test_registration_failure_rolls_back_operation(runtime, monkeypatch):
    instance, _ = runtime
    def fail(*args):
        raise DevError('SYNTHETIC_FAILURE', 'register failed')
    monkeypatch.setattr(instance.knowledge_maintenance, 'admitted', fail)
    with pytest.raises(DevError):
        register(runtime)
    assert not instance.store.one('SELECT id FROM operations')


def test_failed_refresh_backs_off_and_retries_with_new_key(runtime, monkeypatch):
    instance, principal = runtime
    operation, identifier, _, _ = register(runtime)
    succeed(instance, operation)
    monkeypatch.setattr(instance, 'online', lambda _: True)
    due(instance)
    prepared = instance.knowledge_maintenance.prepare(identifier)
    monkeypatch.setattr(instance, 'online', lambda _: False)
    refresh_id, _ = instance._admit_operation(BUILD, prepared[0], prepared[1], principal, True, prepared[3])
    instance.store.execute("UPDATE operations SET state='failed' WHERE id=?", (refresh_id,))
    due(instance)
    assert instance.knowledge_maintenance.prepare(identifier) is None
    row = instance.store.one('SELECT * FROM knowledge_maintenance')
    assert row['state'] == 'backoff' and row['next_at'] > time.time() + 200
    assert row['failures'] == 1 and row['pending_key'] == ''
    monkeypatch.setattr(instance, 'online', lambda _: True)
    due(instance)
    retried = instance.knowledge_maintenance.prepare(identifier)
    assert retried[0]['idempotency_key'] != prepared[0]['idempotency_key']


def test_admission_guard_preserves_single_pending_operation(runtime, monkeypatch):
    instance, principal = runtime
    operation, identifier, _, _ = register(runtime)
    succeed(instance, operation)
    monkeypatch.setattr(instance, 'online', lambda _: True)
    due(instance)
    prepared = instance.knowledge_maintenance.prepare(identifier)
    monkeypatch.setattr(instance, 'online', lambda _: False)
    refresh_id, _ = instance._admit_operation(BUILD, prepared[0], prepared[1], principal, True, prepared[3])
    assert instance.store.one('SELECT operation_id FROM knowledge_maintenance')['operation_id'] == refresh_id
    due(instance)
    assert instance.knowledge_maintenance.prepare(identifier) is None
    again, _ = instance._admit_operation(BUILD, prepared[0], prepared[1], principal, True, prepared[3])
    assert again == refresh_id
    assert instance.store.one('SELECT count(*) AS n FROM operations')['n'] == 2


@pytest.mark.asyncio
async def test_tick_dispatches_background_then_reconciles_original_receipt(runtime, monkeypatch):
    instance, principal = runtime
    operation, identifier, _, _ = register(runtime)
    succeed(instance, operation)
    monkeypatch.setattr(instance, 'online', lambda _: True)
    due(instance)
    calls = []
    async def dispatch(name, args, project, caller, **options):
        calls.append((args, options))
        # Exercise real durable admission without inventing a live Agent channel.
        monkeypatch.setattr(instance, 'online', lambda _: False)
        op, _ = instance._admit_operation(name, args, project, caller, options['background'], options['maintenance'])
        return {'operation_id': op, 'pending': True}
    monkeypatch.setattr(instance, 'dispatch', dispatch)
    await instance.knowledge_maintenance.tick()
    assert len(calls) == 1 and calls[0][1]['background'] is True
    due(instance)
    await instance.knowledge_maintenance.tick()
    assert len(calls) == 1
    assert instance.store.one('SELECT count(*) AS n FROM operations')['n'] == 2


@pytest.mark.asyncio
async def test_end_to_end_external_edit_refreshes_without_any_query_mutation(runtime, tmp_path, monkeypatch):
    from agent.filesystem import FileEngine
    from agent.journal import Journal
    instance, principal = runtime
    root = tmp_path / 'source'
    root.mkdir()
    journal = Journal(tmp_path / 'agent-state')
    engine = FileEngine({'allowed_roots': [{'path': str(root), 'writable': True}]}, journal,
                        tmp_path / 'private' / 'config.json')
    instance.store.execute('UPDATE projects SET root=? WHERE id=?', (str(root), 'p'))
    try:
        (root / 'external.py').write_text('def before(): pass\n')
        operation, _, args, project = register(runtime)
        result = engine.call(BUILD, project, args)
        assert result['entries_count'] == 1
        succeed(instance, operation)
        (root / 'external.py').write_text('def after(): pass\n')
        (root / 'new.py').write_text('def new(): pass\n')
        before = journal.db.total_changes
        query = engine.call('project_knowledge_query', project, {})
        assert query['entries'][0]['state'] == 'stale'
        assert journal.db.total_changes == before
        monkeypatch.setattr(instance, 'online', lambda _: True)
        due(instance)
        async def dispatch(name, args, current_project, caller, **options):
            monkeypatch.setattr(instance, 'online', lambda _: False)
            identifier, _ = instance._admit_operation(name, args, current_project, caller,
                                                       options['background'], options['maintenance'])
            value = engine.call(name, current_project, args)
            succeed(instance, identifier)
            return {'operation_id': identifier, **value}
        monkeypatch.setattr(instance, 'dispatch', dispatch)
        await instance.knowledge_maintenance.tick()
        current = engine.call('project_knowledge_query', project, {})
        assert {entry['path'] for entry in current['entries']} == {'external.py', 'new.py'}
        assert all(entry['state'] == 'current' for entry in current['entries'])
        assert next(entry for entry in current['entries'] if entry['path'] == 'external.py')['symbols'] == ['after']
    finally:
        journal.db.close()


def test_disable_fences_already_queued_maintenance_delivery(runtime, monkeypatch):
    import json
    instance, principal = runtime
    operation, identifier, _, _ = register(runtime)
    succeed(instance, operation)
    monkeypatch.setattr(instance, 'online', lambda _: True)
    due(instance)
    args, project, caller, guard = instance.knowledge_maintenance.prepare(identifier)
    monkeypatch.setattr(instance, 'online', lambda _: False)
    automatic, _ = instance._admit_operation(BUILD, args, project, caller, True, guard)
    queued = instance.store.one('SELECT * FROM operations WHERE id=?', (automatic,))
    request = json.loads(instance.store.decrypt(queued['payload']))
    assert request['knowledge_maintenance'] == list(guard)
    assert instance.permission_error(queued, request) is None
    register(runtime, enabled=False, key='stop-queued-maintenance')
    assert instance.permission_error(queued, request)
    # The user-requested one-time disabling build has no background fence.
    latest = instance.store.one('SELECT * FROM operations WHERE idem=?', ('stop-queued-maintenance',))
    latest_request = json.loads(instance.store.decrypt(latest['payload']))
    assert 'knowledge_maintenance' not in latest_request
    assert instance.permission_error(latest, latest_request) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['not-fernet', 'invalid-json'])
async def test_corrupt_registration_does_not_starve_later_projects(runtime, monkeypatch, corruption):
    instance, _ = runtime
    operation, identifier, _, _ = register(runtime)
    succeed(instance, operation)
    row = instance.store.one('SELECT * FROM knowledge_maintenance')
    bad = {**row, 'id': '000-corrupt-record', 'principal': 'not-fernet', 'next_at': -1}
    if corruption == 'invalid-json':
        bad['principal'] = instance.store.encrypt('not-json')
    instance.store.execute('INSERT INTO knowledge_maintenance (' + ','.join(bad) + ') VALUES (' + ','.join('?' for _ in bad) + ')', tuple(bad.values()))
    due(instance)
    monkeypatch.setattr(instance, 'online', lambda _: True)
    calls = []
    async def dispatch(*args, **kwargs):
        calls.append(kwargs['maintenance'][0])
        return {'pending': True}
    monkeypatch.setattr(instance, 'dispatch', dispatch)
    await instance.knowledge_maintenance.tick()
    assert calls == [identifier]
    broken = instance.store.one('SELECT * FROM knowledge_maintenance WHERE id=?', (bad['id'],))
    assert broken['state'] == 'paused' and not broken['enabled']
    assert broken['last_error'] == 'KNOWLEDGE_REGISTRATION_INVALID'


@pytest.mark.parametrize('sql', [
    "UPDATE iam_users SET epoch=epoch+1 WHERE user_id='owner'",
    "UPDATE iam_users SET active=0 WHERE user_id='owner'",
    "UPDATE grants SET projects='[]' WHERE id='writer'",
])
def test_identity_epoch_and_project_scope_revocation_pause(runtime, sql):
    instance, _ = runtime
    operation, identifier, _, _ = register(runtime)
    succeed(instance, operation)
    instance.store.execute(sql)
    due(instance)
    assert instance.knowledge_maintenance.prepare(identifier) is None
    assert instance.store.one('SELECT enabled,state FROM knowledge_maintenance') == {'enabled': 0, 'state': 'paused'}


def test_device_budget_rejects_new_registration_before_operation_commit(runtime, monkeypatch):
    from hub import knowledge_maintenance
    instance, _ = runtime
    _, identifier, _, _ = register(runtime)
    monkeypatch.setattr(knowledge_maintenance, 'MAX_DEVICE_RECORDS', 1)
    instance.store.execute("UPDATE projects SET root='/second-root' WHERE id='p'")
    with pytest.raises(DevError) as error:
        register(runtime, key='over-device-budget')
    assert error.value.code == 'KNOWLEDGE_DEVICE_MAINTENANCE_LIMIT'
    assert instance.store.one('SELECT count(*) AS n FROM operations')['n'] == 1
    assert instance.store.one('SELECT id FROM knowledge_maintenance')['id'] == identifier


def test_legacy_key_recovers_without_enabling_maintenance(runtime):
    instance, principal = runtime
    args = TOOLS[BUILD].model.model_validate({'project': 'p', 'max_files': 2000,
        'idempotency_key': 'pre-maintenance-build'}).model_dump()
    legacy = {key: value for key, value in args.items() if key not in {
        'auto_refresh', 'incremental', 'batch_size', 'time_budget_seconds', 'cursor', 'paths', 'exclude_paths'}}
    project = instance.project('p', principal)
    original, _ = instance._admit_operation(BUILD, legacy, project, principal)
    assert not instance.knowledge_maintenance.exists()
    recovered, _ = instance._admit_operation(BUILD, args, project, principal)
    assert recovered == original and not instance.knowledge_maintenance.exists()
    assert instance.store.one('SELECT count(*) AS n FROM operations')['n'] == 1


def test_no_iam_no_grant_manual_build_never_registers_durable_authority(runtime):
    instance, _ = runtime
    instance.store.iam_enabled = False
    principal = Principal('panel:legacy', 'owner', {'read', 'write'}, ['*'], admin=True)
    args = TOOLS[BUILD].model.model_validate({'project': 'p', 'idempotency_key': 'legacy-manual-build'}).model_dump()
    project = instance.project('p', principal)
    identifier, _ = instance._admit_operation(BUILD, args, project, principal)
    assert identifier and not instance.knowledge_maintenance.exists()
    # Revoking the only real identity cannot leave any background authority.
    instance.store.execute("UPDATE iam_users SET active=0 WHERE user_id='owner'")
    instance.knowledge_maintenance.migrate()
    assert instance.knowledge_maintenance.due() == []


def test_legacy_persisted_no_grant_row_pauses_before_trusting_admin_snapshot(runtime):
    import json
    instance, _ = runtime
    operation, identifier, _, _ = register(runtime)
    succeed(instance, operation)
    row = instance.store.one('SELECT * FROM knowledge_maintenance')
    data = json.loads(instance.store.decrypt(row['principal']))
    data.update(grant_id=None, admin=True, token_hash='', session_hash='')
    instance.store.execute('UPDATE knowledge_maintenance SET principal=?,next_at=0',
                           (instance.store.encrypt(json.dumps(data)),))
    instance.store.iam_enabled = False
    instance.store.execute("UPDATE iam_users SET active=0 WHERE user_id='owner'")
    assert instance.knowledge_maintenance.prepare(identifier) is None
    row = instance.store.one('SELECT * FROM knowledge_maintenance')
    assert row['state'] == 'paused' and not row['enabled']
    assert row['last_error'] == 'KNOWLEDGE_GRANT_REQUIRED'


@pytest.mark.asyncio
async def test_no_grant_build_receipt_explains_registration_not_supported(runtime, monkeypatch):
    instance, _ = runtime
    principal = Principal('panel:admin', 'owner', {'read', 'write'}, ['*'], admin=True)
    async def dispatch(*args, **kwargs):
        return {'operation_id': 'a' * 32, 'pending': True}
    monkeypatch.setattr(instance, 'dispatch', dispatch)
    result = await instance.invoke(BUILD, {'project': 'p', 'idempotency_key': 'manual-panel-build'}, principal)
    assert result['auto_refresh_request']['state'] == 'unsupported_subject'
    assert not instance.knowledge_maintenance.exists()


def test_legacy_admin_bit_cannot_bypass_revoked_grant(runtime):
    import json
    instance, _ = runtime
    operation, identifier, _, _ = register(runtime)
    succeed(instance, operation)
    row = instance.store.one('SELECT * FROM knowledge_maintenance')
    data = json.loads(instance.store.decrypt(row['principal']))
    data.update(admin=True, instance_admin=True)
    instance.store.execute('UPDATE knowledge_maintenance SET principal=?,next_at=0',
                           (instance.store.encrypt(json.dumps(data)),))
    instance.store.iam_enabled = False
    instance.store.execute("UPDATE grants SET revoked=1 WHERE id='writer'")
    assert instance.knowledge_maintenance.prepare(identifier) is None
    assert instance.store.one('SELECT enabled,state FROM knowledge_maintenance') == {'enabled': 0, 'state': 'paused'}


@pytest.mark.parametrize('revocation', ['room_ceiling', 'room_revoked', 'grant_scope'])
def test_maintenance_delivery_rechecks_room_and_grant_after_prepare(collab, monkeypatch, revocation):
    import json
    from tests.test_dot_room_scope import setup

    service, owner, actor, _, scope = setup(collab)
    service.room_scope.set(scope, owner)
    instance = service.runtime
    prepared = instance._invoke(BUILD, {'project': 'proj', 'idempotency_key': key()}, actor)
    initial, _ = instance._admit_operation(BUILD, prepared.arguments, prepared.project, actor)
    succeed(instance, initial)
    due(instance)
    monkeypatch.setattr(instance, 'online', lambda _: True)
    args, project, caller, guard = instance.knowledge_maintenance.prepare(key_for(prepared.project, prepared.arguments))
    monkeypatch.setattr(instance, 'online', lambda _: False)
    automatic, _ = instance._admit_operation(BUILD, args, project, caller, True, guard)
    queued = instance.store.one('SELECT * FROM operations WHERE id=?', (automatic,))
    request = json.loads(instance.store.decrypt(queued['payload']))
    assert instance.permission_error(queued, request) is None

    if revocation == 'room_ceiling':
        service.room_scope.set({**scope, 'expected_version': 1, 'idempotency_key': key(),
            'project_rules': [{'project_id': 'proj', 'capabilities': ['read']}]}, owner)
    elif revocation == 'room_revoked':
        service.room_scope.control({**{k: scope[k] for k in ('project', 'environment_id', 'grant_id')},
            'expected_version': 1, 'action': 'revoke', 'idempotency_key': key()}, owner)
    else:
        instance.store.execute("UPDATE grants SET scopes='[\"read\"]' WHERE id='broad'")
    # Replay the original durable request, not a newly narrowed principal.
    assert instance.permission_error(queued, request) is not None
    due(instance)
    assert instance.knowledge_maintenance.prepare(guard[0]) is None
    assert instance.store.one('SELECT state,enabled FROM knowledge_maintenance') == {'state': 'paused', 'enabled': 0}
    assert instance.store.one('SELECT count(*) AS n FROM operations')['n'] == 2


@pytest.mark.parametrize('change', ['replace_directory', 'retarget_record'])
def test_maintenance_workspace_change_cannot_initialize_another_map(runtime, tmp_path, monkeypatch, change):
    from types import SimpleNamespace
    from agent.filesystem import FileEngine
    from agent.integration_state import Records
    from agent.journal import Journal
    from agent.workspaces import Workspaces
    from agent import knowledge_refresh

    instance, principal = runtime
    source = tmp_path / 'source'
    workspace = tmp_path / 'workspace'
    source.mkdir()
    workspace.mkdir()
    (workspace / 'original.py').write_text('def original(): pass\n')
    instance.store.execute("UPDATE projects SET root=? WHERE id='p'", (str(source),))
    journal = Journal(tmp_path / 'agent-state')
    engine = FileEngine({'allowed_roots': [{'path': str(tmp_path), 'writable': True}]},
                        journal, tmp_path / 'private' / 'config.json')
    records = Records(journal)
    workspaces = Workspaces(SimpleNamespace(engine=engine), records)
    workspace_id = 'a' * 32
    try:
        prepared = instance._invoke(BUILD, {'project': 'p', 'workspace_id': workspace_id,
            'idempotency_key': 'workspace-initial-build'}, principal)
        initial, _ = instance._admit_operation(BUILD, prepared.arguments, prepared.project, principal)
        parent = {**prepared.project, '_integration_owner': 'grant:writer', '_coding_device': 'dev'}
        info = workspace.stat()
        record = {'state': 'ready', 'path': str(workspace), 'identity': [info.st_dev, info.st_ino]}
        records.save('workspace', workspace_id, parent, record)
        engine.call(BUILD, workspaces.resolve(parent, workspace_id), prepared.arguments)
        succeed(instance, initial)
        due(instance)
        monkeypatch.setattr(instance, 'online', lambda _: True)
        args, project, _, _ = instance.knowledge_maintenance.prepare(key_for(prepared.project, prepared.arguments))
        assert args['workspace_id'] == workspace_id and args['incremental'] is True
        if change == 'replace_directory':
            workspace.rename(tmp_path / 'original-workspace')
            workspace.mkdir()
            expected = 'WORKSPACE_CHANGED'
        else:
            workspace = tmp_path / 'retargeted-workspace'
            workspace.mkdir()
            info = workspace.stat()
            records.save('workspace', workspace_id, parent,
                         {**record, 'path': str(workspace), 'identity': [info.st_dev, info.st_ino]}, replace=True)
            expected = 'KNOWLEDGE_MISSING'
        (workspace / 'other.py').write_text('def other(): pass\n')
        # Exercise real owner-bound record lookup, directory identity and map identity.
        with pytest.raises(DevError) as error:
            actual = workspaces.resolve({**project, '_integration_owner': 'grant:writer', '_coding_device': 'dev'}, args['workspace_id'])
            engine.call(BUILD, actual, args)
        assert error.value.code == expected
        assert journal.db.execute('SELECT count(*) FROM project_knowledge').fetchone()[0] == 1
    finally:
        journal.db.close()
        knowledge_refresh._CACHES.clear()


def test_new_room_tool_ceiling_blocks_prepared_workspace_maintenance(collab, monkeypatch):
    import json
    from hub.principal import refresh_principal
    from tests.test_dot_room_scope import setup

    service, owner, actor, _, scope = setup(collab)
    instance = service.runtime
    prepared = instance._invoke(BUILD, {'project': 'proj', 'workspace_id': 'b' * 32,
        'idempotency_key': key()}, actor)
    initial, _ = instance._admit_operation(BUILD, prepared.arguments, prepared.project, actor)
    succeed(instance, initial)
    due(instance)
    monkeypatch.setattr(instance, 'online', lambda _: True)
    args, project, caller, guard = instance.knowledge_maintenance.prepare(key_for(prepared.project, prepared.arguments))
    monkeypatch.setattr(instance, 'online', lambda _: False)
    automatic, _ = instance._admit_operation(BUILD, args, project, caller, True, guard)
    queued = instance.store.one('SELECT * FROM operations WHERE id=?', (automatic,))
    request = json.loads(instance.store.decrypt(queued['payload']))
    assert instance.permission_error(queued, request) is None
    service.room_scope.set(scope, owner)
    # Generic project write access remains valid; the tool's workspace gate must deny.
    assert 'write' in refresh_principal(instance.store, caller).scopes
    instance.authorize(caller, 'write', project_id='proj')
    assert '工作树' in instance.permission_error(queued, request)
    due(instance)
    assert instance.knowledge_maintenance.prepare(guard[0]) is None
    row = instance.store.one('SELECT state,enabled,last_error FROM knowledge_maintenance')
    assert row == {'state': 'paused', 'enabled': 0, 'last_error': 'ROOM_SCOPE_WORKSPACE_DENIED'}


def admit_fixture_write(instance, principal, key, project='p'):
    prepared = instance._invoke('write', {'project': project, 'path': 'new.py', 'content': 'value = 1\n',
        'expected_sha256': 'new', 'idempotency_key': key}, principal)
    return instance._admit_operation('write', prepared.arguments, prepared.project, principal)[0]


def test_auto_enrollment_bootstraps_then_switches_to_incremental(runtime, monkeypatch):
    from hub.knowledge_maintenance import IDLE_INTERVAL

    instance, principal = runtime
    maintenance = instance.knowledge_maintenance
    monkeypatch.setattr(instance, 'online', lambda _: True)
    write = admit_fixture_write(instance, principal, 'bootstrap-write')
    identifier = key_for(instance.project('p', principal), {})
    registration = instance.store.one('SELECT * FROM knowledge_maintenance WHERE id=?', (identifier,))
    assert registration['origin'] == 'auto' and registration['state'] == 'initializing'
    due(instance)
    assert maintenance.prepare(identifier) is None
    assert maintenance.status(instance.project('p', principal), {})['state'] == 'deferred_busy'
    succeed(instance, write)
    due(instance)
    args, project, identity, guard = maintenance.prepare(identifier)
    assert args['incremental'] is False and identity.grant_id == principal.grant_id
    initial, _ = instance._admit_operation(BUILD, args, project, identity, maintenance=guard)
    automatic_row = instance.store.one('SELECT actor,args_summary FROM operations WHERE id=?', (initial,))
    request_row = instance.store.one('SELECT actor,args_summary FROM operations WHERE id=?', (write,))
    assert automatic_row['actor'] == request_row['actor'] == principal.actor
    assert json.loads(automatic_row['args_summary'])['_log']['trigger_source'] == 'system_maintenance'
    assert json.loads(automatic_row['args_summary'])['_log']['trigger_reason'] == 'automatic_refresh'
    assert json.loads(request_row['args_summary'])['_log']['trigger_source'] == 'request'
    succeed(instance, initial)
    due(instance)
    args, _, _, _ = maintenance.prepare(identifier)
    assert args['incremental'] is True
    instance.store.execute("UPDATE knowledge_maintenance SET last_write=?,next_at=?,updated=?,state='refreshing' WHERE id=?",
                           (time.time() - 90000, time.time() + IDLE_INTERVAL, time.time() - 60, identifier))
    assert maintenance.status(project, {})['interval_seconds'] == IDLE_INTERVAL
    maintenance.nudge('p', 'dev', 'write')
    first = maintenance.status(project, {})
    assert first['interval_seconds'] < IDLE_INTERVAL and first['next_check_at'] < time.time() + 12
    maintenance.nudge('p', 'dev', 'write')
    assert maintenance.status(project, {})['next_check_at'] == first['next_check_at']


def test_auto_enrollment_respects_opt_out_and_isolates_failed_registration(runtime, monkeypatch):
    instance, principal = runtime
    maintenance = instance.knowledge_maintenance
    monkeypatch.setattr(maintenance, 'auto_enroll', False)
    admit_fixture_write(instance, principal, 'enrollment-feature-off')
    assert not maintenance.exists()
    monkeypatch.setattr(maintenance, 'auto_enroll', True)
    original = maintenance._register

    def fail_after_insert(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic registration failure')

    with monkeypatch.context() as patch:
        patch.setattr(maintenance, '_register', fail_after_insert)
        write = admit_fixture_write(instance, principal, 'enrollment-savepoint')
    assert instance.store.one('SELECT state FROM operations WHERE id=?', (write,))['state'] == 'queued'
    assert not maintenance.exists()
    _, identifier, _, _ = register(runtime, enabled=False)
    admit_fixture_write(instance, principal, 'enrollment-opt-out')
    assert instance.store.one('SELECT state FROM knowledge_maintenance WHERE id=?', (identifier,))['state'] == 'disabled'


def test_auto_enrollment_evicts_only_automatic_registration(runtime, monkeypatch):
    import hub.knowledge_maintenance as module

    instance, principal = runtime
    _, explicit, _, _ = register(runtime)
    for name in ('p2', 'p3'):
        instance.store.execute("""INSERT INTO projects(id,alias,alias_key,device_id,root,mode,created)
            VALUES (?,?,?,'dev',?,'write',?)""", (name, name, name, '/synthetic/' + name, time.time()))
    seed_grant(instance.store, 'writer2', 'owner', scopes=('read', 'write'), projects=('p2', 'p3'))
    identity = Principal('mcp:writer2:test', 'owner', {'read', 'write'}, ['p2', 'p3'], grant_id='writer2')
    monkeypatch.setattr(module, 'MAX_DEVICE_RECORDS', 2)
    admit_fixture_write(instance, identity, 'enrollment-second-project', 'p2')
    admit_fixture_write(instance, identity, 'enrollment-third-project', 'p3')
    rows = instance.store.all('SELECT id,project_id,origin FROM knowledge_maintenance ORDER BY project_id')
    assert [(row['project_id'], row['origin']) for row in rows] == [('p', 'explicit'), ('p3', 'auto')]
    assert rows[0]['id'] == explicit


def test_auto_maintenance_keeps_unreadable_success_receipt_for_review(runtime, monkeypatch):
    instance, _ = runtime
    initial, identifier, _, project = register(runtime)
    monkeypatch.setattr(instance, 'online', lambda _: True)
    instance.store.execute("UPDATE operations SET state='succeeded',result=? WHERE id=?",
                           ('history:v1:gAAAAcorrupt', initial))
    due(instance)
    assert instance.knowledge_maintenance.prepare(identifier) is None
    status = instance.knowledge_maintenance.status(project, {})
    assert status['state'] == 'needs_review' and status['operation_id'] == initial
