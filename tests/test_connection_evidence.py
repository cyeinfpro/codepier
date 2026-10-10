"""Connection evidence and recovery use disposable records, never real credentials."""
import json
import time
from types import SimpleNamespace

import pytest

from hub.auth import Auth
from hub.gateway.service import Gateway
from hub.principal import Principal
from hub.runtime import Runtime
from hub.store import Store
from shared.operation_recovery import error_recovery, operation_recovery
from shared.util import DevError
from tests.legacy_iam_fixture import seed_owner


@pytest.fixture
def evidence(tmp_path):
    store = Store(tmp_path / 'hub')
    seed_owner(store, 'owner', 'owner')
    seed_owner(store, 'bob', 'bob')
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES('node','Fixture node',?,?)",
                  (store.encrypt('disposable-device-secret'), time.time()))
    store.execute("""INSERT INTO projects(id,alias,alias_key,device_id,root,mode,allow_tasks,created)
        VALUES('project','fixture','fixture','node',?,'write',1,?)""", (str(tmp_path / 'project'), time.time()))
    runtime = Runtime(store)
    runtime.gateway = Gateway(store)
    auth = Auth(store)
    viewer = Principal('panel:owner', 'owner', {'read','write','execute'}, ['*'], admin=True)
    grant = auth.issue_grant(viewer, 'Fixture connection', ['read'], ['project'], 30)
    row = store.one('SELECT * FROM grants WHERE id=?', (grant['grant_id'],))
    principal = runtime.grant_principal(row)
    yield SimpleNamespace(store=store, runtime=runtime, service=runtime.diagnostics.connection,
                          viewer=viewer, principal=principal, grant=grant)
    store.close()


def snapshot(e, **kwargs):
    return e.service.status(e.viewer, e.grant['grant_id'], project_id='project', **kwargs)


def test_empty_evidence_does_not_invent_client_scan_or_read(evidence):
    e = evidence
    with e.store.lock:
        before = e.store.db.total_changes
    value = snapshot(e)
    with e.store.lock:
        assert e.store.db.total_changes == before
    assert value['catalog']['client_cache_status'] == 'unknown'
    assert value['catalog']['host_scan_status'] == 'unknown'
    assert value['catalog']['current_effective_sha256']
    layers = {x['id']: x for x in value['layers']}
    assert layers['authentication']['status'] == 'allowed'
    assert layers['discovery']['status'] == layers['catalog']['status'] == layers['readonly']['status'] == 'unknown'
    assert layers['agent']['status'] == 'offline'
    assert layers['agent']['read_capability'] == 'unknown'
    assert e.grant['token'] not in json.dumps(value)
    assert not e.store.all('SELECT * FROM operations')


def test_observed_catalog_is_scoped_historical_and_not_host_cache(evidence):
    e = evidence
    e.service.observe(e.principal, 'discovery')
    e.service.observe(e.principal, 'catalog', catalog_sha256='a' * 64, catalog_has_more=True)
    value = snapshot(e, client_catalog_sha256='a' * 64)
    assert value['catalog']['client_report_matches_last_served'] is True
    assert value['catalog']['last_served_matches_current'] is False
    assert value['catalog']['client_cache_status'] == 'unknown'
    e.store.execute('UPDATE mcp_connection_evidence SET observed=?', (time.time() - 3600,))
    assert next(x for x in snapshot(e)['layers'] if x['id'] == 'discovery')['status'] == 'stale'
    e.store.execute("UPDATE grants SET projects='[]' WHERE id=?", (e.grant['grant_id'],))
    assert snapshot(e)['catalog']['last_served_sha256'] is None
    assert next(x for x in snapshot(e)['layers'] if x['id'] == 'readonly')['status'] == 'blocked'


def test_owner_space_grant_mapping_and_workspace_isolation(evidence):
    e = evidence
    project = e.runtime.project('project', e.principal)
    e.service.observe(e.principal, 'readonly', project=project, operation_id='read-operation')
    assert next(x for x in snapshot(e)['layers'] if x['id'] == 'readonly')['status'] == 'observed'
    e.store.execute("UPDATE projects SET root='/changed-fixture' WHERE id='project'")
    assert next(x for x in snapshot(e)['layers'] if x['id'] == 'readonly')['status'] == 'stale'
    other = Principal('panel:bob', 'bob', {'read'}, ['*'], admin=True)
    with pytest.raises(DevError):
        e.service.status(other, e.grant['grant_id'])
    with pytest.raises(DevError):
        snapshot(e, workspace_id='0' * 32)
    e.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (e.grant['grant_id'],))
    assert next(x for x in snapshot(e)['layers'] if x['id'] == 'authentication')['status'] == 'blocked'


def test_failed_telemetry_does_not_change_business_result(evidence, monkeypatch):
    e = evidence
    def broken(*args, **kwargs):
        raise RuntimeError('private exception')
    monkeypatch.setattr(e.store, 'execute', broken)
    assert e.service.observe(e.principal, 'catalog') is None
    assert e.service.write_errors == 1


@pytest.mark.parametrize('state,pending,confirmed', [
    ('running', True, False), ('reconnecting', True, False), ('unknown', True, False),
    ('cancelling', True, False), ('needs_review', False, False), ('interrupted', False, False),
    ('succeeded', False, True), ('failed', False, True), ('cancelled', False, True),
])
def test_recovery_preserves_original_identity_and_cursor(state, pending, confirmed):
    original = {'id':'original', 'state':state, 'pending':pending, 'cancel_requested':state == 'cancelling'}
    value = operation_recovery(original, online=False, after_output_seq=7)
    assert value['operation_id'] == 'original' and value['after_output_seq'] == 7
    assert value['task_state'] == state and value['task_outcome_confirmed'] is confirmed
    assert value['automatic_replay'] is False and value['new_operation_recommended'] is False
    assert value['next_action'] == ('query_original_operation' if pending else 'inspect_terminal_result')
    assert original['state'] == state


@pytest.mark.parametrize('code', ['APPROVAL_DENIED', 'GRANT_REVOKED', 'INVALID_TOKEN',
    'INSUFFICIENT_SCOPE', 'ROLE_POLICY_DENIED', 'EXECUTION_POLICY_BLOCKED', 'WORK_APPROVAL_CHANGED',
    'CODEX_REMOTE_DISABLED', 'NATIVE_COMPUTER_REMOTE_DISABLED', 'AUTHORIZATION_CHANGED',
    'ACCOUNT_LOCKED', 'TOKEN_EXPIRED'])
def test_denial_never_retries_or_suggests_replay(code):
    value = error_recovery(code, 'original')
    assert value['retryable'] is False
    assert value['next_action'] == 'stop_and_review_permission'
    op = {'id':'original', 'state':'failed', 'pending':False, 'result':{'error':{'code':code}}}
    assert operation_recovery(op, online=True)['next_action'] == 'stop_and_review_permission'


def test_initial_pending_receipt_keeps_connection_separate_from_task(evidence):
    e = evidence
    identifier = completed_read(e)
    e.store.execute("UPDATE operations SET state='queued',result=NULL,cancel_requested=1 WHERE id=?", (identifier,))
    value = e.runtime._pending_receipt(identifier, e.principal)
    assert value['pending'] and value['state'] == 'queued'
    assert value['recovery']['connection_state'] == 'unavailable'
    assert value['recovery']['task_outcome_confirmed'] is False
    assert value['recovery']['next_action'] == 'query_original_operation'
    assert value['recovery']['operation_id'] == identifier
    assert '取消请求等待' in value['recovery']['message']


def test_execution_policy_codes_are_safe_audit_categories():
    from hub.mcp_request_audit import ERRORS
    assert {'CODEX_REMOTE_DISABLED', 'NATIVE_COMPUTER_REMOTE_DISABLED'} <= ERRORS


def completed_read(e, *, at=None, grant=None, root=None, data=None):
    from shared.contracts import TOOLS
    principal = grant or e.principal
    project = e.runtime.project('project', principal)
    args = TOOLS['read'].model.model_validate({'project':'fixture','path':'README.md',
        'idempotency_key':'evidence-' + str(time.time_ns())}).model_dump()
    identifier, _ = e.runtime._admit_operation('read', args, project, principal)
    value = data or {'path':'README.md','sha256':'a'*64,'content':'fixture'}
    if root:
        e.store.execute("UPDATE projects SET root=? WHERE id='project'", (root,))
    operation = e.store.one('SELECT * FROM operations WHERE id=?', (identifier,))
    e.runtime.complete(operation, {'ok':True,'data':value})
    assert e.store.one('SELECT payload FROM operations WHERE id=?', (identifier,))['payload'] is None
    if at is not None:
        e.store.execute('UPDATE operations SET updated=? WHERE id=?', (at, identifier))
        e.store.execute("UPDATE mcp_connection_evidence SET observed=? WHERE stage='readonly' AND grant_id=?",
                        (at, principal.grant_id))
    return identifier


def test_original_receipt_observation_preserves_completion_time_and_mapping(evidence):
    e = evidence
    identifier = completed_read(e, at=time.time()-3600)
    value = e.runtime.operation(identifier, e.principal)
    e.service.returned(e.principal, 'task_query', {}, {'operations':[value]})
    readonly = next(x for x in snapshot(e)['layers'] if x['id'] == 'readonly')
    assert readonly['status'] == 'stale'
    assert readonly['evidence']['operation_id'] == identifier
    assert readonly['evidence']['age_seconds'] >= 3600
    recent = completed_read(e)
    e.service.returned(e.principal, 'read', {}, {'operation_id':recent})
    assert next(x for x in snapshot(e)['layers'] if x['id'] == 'readonly')['status'] == 'observed'
    # Polling an older completed receipt cannot overwrite the newer evidence.
    e.service.returned(e.principal, 'task_query', {}, {'operations':[value]})
    assert next(x for x in snapshot(e)['layers'] if x['id'] == 'readonly')['evidence']['operation_id'] == recent
    moved = completed_read(e, root='/new-fixture-mapping')
    e.service.returned(e.principal, 'read', {}, {'operation_id':moved})
    assert next(x for x in snapshot(e)['layers'] if x['id'] == 'readonly')['status'] == 'stale'


def test_pending_and_error_reads_never_become_completion_evidence(evidence):
    e = evidence
    e.service.returned(e.principal, 'read', {}, {'operation_id':'unknown','pending':True})
    e.service.returned(e.principal, 'read', {}, {'error':{'code':'PROTECTED_PATH'}})
    assert next(x for x in snapshot(e)['layers'] if x['id'] == 'readonly')['status'] == 'unknown'


def test_all_error_batch_does_not_prove_read_success(evidence):
    e = evidence
    identifier = completed_read(e, data={'batch':True,'files':[
        {'ok':False,'path':'blocked','error':{'code':'PROTECTED_PATH'}}]})
    e.service.returned(e.principal, 'read', {}, {'operation_id':identifier})
    assert next(x for x in snapshot(e)['layers'] if x['id'] == 'readonly')['status'] == 'unknown'
