"""Protocol, original-fence, and approval regressions in synthetic fixtures."""
import json
import time
from types import SimpleNamespace

import pytest

from agent.runner import Agent
from shared.bounded_execution import execution_timeout, make_budget, validate_budget
from shared.collaboration_contracts import CoordinationBudget, WorkArchiveResult
from shared.tool_protocol import advertisement, catalog_digest, negotiate
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_coordination import setup_goal, make_work, claim, lease
from tests.test_collaboration_dot_chat import duplex
from tests.test_collaboration_dots import lease as dot_lease
from tests.test_dot_task_liveness_recovery import task, read_operation, settle


def budget_fixture(seconds=1800):
    goal = {'id': 'goal', 'approval_id': 'approval', 'expires_at': 5000.0}
    item = {'id': 'work', 'project_id': 'project', 'attempt': 1, 'fencing_token': 2, 'lease_until': 1300.0}
    return make_budget(goal, item, 'grant', 1000.0, seconds, seconds)


def peer(service, protocol=1):
    value = SimpleNamespace(unusable=False, last_seen=time.time(), journal_id='synthetic-journal',
        protocol=2, cancel_pending_protocol=1, bounded_execution_protocol=protocol,
        tool_protocol=negotiate(advertisement(catalog_digest())),
        device_secret=service.store.one("SELECT secret FROM devices WHERE id='dev'")['secret'])
    service.runtime.connections['dev'] = value
    return value


def admitted(fixture, ceiling=1800, protocol=1):
    budget = {'lease_seconds': 300, **({'operation_timeout_seconds': ceiling} if ceiling else {})}
    c, s, owner, worker, actor, goal, scope, raw = setup_goal(fixture, budget=budget)
    item = claim(c, scope, goal, make_work(c, scope, goal, owner), actor)
    if protocol is not None:
        peer(s, protocol)
    command = {**lease(scope, goal, item), 'tool': 'exec',
               'arguments': {'command': 'printf synthetic-budget-test', 'timeout_seconds': 1800}}
    return c, s, actor, goal, item, command


def test_legacy_defaults_do_not_change_approval_or_retry_serialization():
    before = {'max_work_items': 20, 'max_steps': 50, 'max_messages': 40, 'max_attempts': 3, 'lease_seconds': 300}
    assert CoordinationBudget().model_dump() == before
    assert CoordinationBudget(operation_timeout_seconds=0).model_dump() == before
    assert CoordinationBudget(operation_timeout_seconds=1800).model_dump()['operation_timeout_seconds'] == 1800
    args = {'project': 'proj', 'goal_id': 'goal', 'work_item_id': 'item', 'attempt': 1,
            'fencing_token': 1, 'idempotency_key': key(), 'expected_version': 3, 'outcome': 'succeeded', 'summary': 'done'}
    assert 'result_schema_version' not in WorkArchiveResult(**args).model_dump()


def test_hard_deadline_and_lease_are_independent_and_nonrenewable():
    value = budget_fixture()
    assert value['consumer_lease_until'] == 1300
    assert value['hard_until'] == 2800
    assert execution_timeout(value, project_id='project', requested_seconds=1800, now=1301) == 1499
    assert execution_timeout(value, project_id='project', requested_seconds=1800, now=2799.5) == .5
    with pytest.raises(DevError, match='预算已用尽'):
        execution_timeout(value, project_id='project', requested_seconds=1800, now=2800)


@pytest.mark.parametrize('field,value', [
    ('version', True), ('version', 2), ('attempt', 4), ('fencing_token', False),
    ('timeout_seconds', 86401), ('timeout_seconds', float('inf')),
    ('hard_until', float('nan')), ('hard_until', 6000), ('admitted_at', -1),
    ('consumer_lease_until', 1000), ('consumer_loss_policy', 'continue_new_steps'), ('grant_id', ''),
])
def test_malformed_or_widened_budgets_fail_closed(field, value):
    with pytest.raises(DevError):
        validate_budget({**budget_fixture(), field: value})


def test_agent_preserves_local_ceiling_and_rechecks_before_process_start(monkeypatch):
    value = budget_fixture()
    monkeypatch.setattr('agent.runner.time.time', lambda: 1400)
    project = {'id': 'project', '_bounded_execution': value}
    assert Agent.bounded_timeout(project, 60) == 60
    assert Agent.bounded_timeout(project, 1800) == 1400
    with pytest.raises(DevError):
        Agent.bounded_timeout({**project, 'id': 'other-project'}, 1800)
    monkeypatch.setattr('agent.runner.time.time', lambda: 2801)
    with pytest.raises(DevError):
        Agent.bounded_timeout(project, 1800)
    assert Agent.bounded_timeout({'id': 'project'}, 42) == 42


def test_long_operation_admission_keeps_original_budget_after_heartbeat(collab):
    c, s, actor, goal, item, command = admitted(collab)
    receipt = c.admit(command, actor)
    row = s.store.one('SELECT * FROM operations WHERE id=?', (receipt['operation_id'],))
    request = json.loads(s.store.decrypt(row['payload']))
    original = request['bounded_execution']
    assert request['args']['timeout_seconds'] == 1800
    assert original['hard_until'] > row['deadline'] + 1000
    assert original['consumer_lease_until'] == item['lease_until']
    assert c.admit(command, actor) == receipt
    collab[6][0] += 100
    c.heartbeat({**{k: command[k] for k in ('project', 'environment_id', 'goal_id', 'work_item_id', 'attempt', 'fencing_token')},
                 'idempotency_key': key()}, actor)
    current = s.store.one('SELECT * FROM operations WHERE id=?', (receipt['operation_id'],))
    assert json.loads(s.store.decrypt(current['payload']))['bounded_execution'] == original
    assert current['deadline'] == row['deadline']
    assert s.store.one('SELECT lease_until FROM coordination_work WHERE id=?', (item['id'],))['lease_until'] > original['consumer_lease_until']


@pytest.mark.parametrize('protocol', [None, 0, 2])
def test_positive_budget_needs_current_agent_protocol_without_silent_fallback(collab, protocol):
    c, s, actor, goal, item, command = admitted(collab, protocol=protocol)
    with pytest.raises(DevError, match='有界长命令协议'):
        c.admit(command, actor)
    assert not s.store.all('SELECT * FROM operations')


def test_legacy_agents_keep_legacy_lease_ceiling(collab):
    c, s, actor, goal, item, command = admitted(collab, ceiling=0, protocol=0)
    receipt = c.admit(command, actor)
    row = s.store.one('SELECT * FROM operations WHERE id=?', (receipt['operation_id'],))
    request = json.loads(s.store.decrypt(row['payload']))
    assert request['args']['timeout_seconds'] <= 300
    assert 'bounded_execution' not in request
    assert receipt['deadline_policy']['version'] == 0


def test_capability_downgrade_after_admission_cannot_dispatch_long_operation(collab):
    c, s, actor, goal, item, command = admitted(collab)
    receipt = c.admit(command, actor)
    peer(s, 0)
    s.runtime._prepare_delivery(receipt['operation_id'])
    row = s.store.one('SELECT * FROM operations WHERE id=?', (receipt['operation_id'],))
    assert row['state'] == 'failed' and row['attempts'] == 0
    assert json.loads(row['result'])['error']['code'] == 'AGENT_BOUNDED_EXECUTION_REQUIRED'


def test_expired_archive_v2_preserves_recovered_failures_without_reviving_lease(collab):
    c = duplex(collab)
    source, item = task(c)
    failed = read_operation(c, item, operation_role='exploratory')
    settle(c, failed, False)
    repaired = read_operation(c, item, supersedes_operation_id=failed)
    settle(c, repaired)
    c.clock[0] += 301
    c.s.coordination.reconcile()
    blocked = c.s.coordination.work({'id': item['goal_id']}, item['id'])
    raw = {**dot_lease(c, item), 'action': 'archive_result', 'expected_version': blocked['version'],
           'result_schema_version': 2, 'outcome': 'succeeded', 'summary': 'Original read failure repaired; archived after lease expiry.',
           'operation_ids': [failed, repaired]}
    archived = c.s.invoke('collaboration_work', raw, c.actor)['work_item']
    assert archived['lease_until'] is None and archived['fencing_token'] == blocked['fencing_token']
    assert archived['result']['late_archive']
    assert archived['result']['execution_health'] == 'recovered_failures'
    assert archived['result']['acceptance_status'] == 'unverified'
    assert c.s.store.one('SELECT state FROM operations WHERE id=?', (failed,))['state'] == 'failed'
    assert c.s.invoke('collaboration_work', raw, c.actor)['work_item'] == archived
    assert len(c.s.store.all("SELECT id FROM collaboration_messages WHERE kind='delegation_result' AND reply_to_id=?", (source['id'],))) == 1
