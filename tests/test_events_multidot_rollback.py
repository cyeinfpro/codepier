"""Offline freeze on isolated current databases; no live or old-binary deployment."""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
import sqlite3

import pytest
import pytest_asyncio

from hub.collaboration.common import canonical, digest
from hub.collaboration.events import EventService
from hub.collaboration.network import Reply
from scripts import prepare_multidot_rollback as freeze
from shared.util import DevError
from tests.collaboration_support import collab as collab, key


@pytest_asyncio.fixture
async def rollback_case(collab, monkeypatch):
    s, owner, worker, dot_actor, room, _, clock, scope = collab
    s.runtime.collaboration = s
    s.config = replace(s.config, events_enabled=True)
    broad = replace(worker, grant_id='broad', actor='mcp:broad:rollback-fixture')
    def join(actor, label):
        created = s.dots.create({**scope, 'conversation_id': room['id'], 'label': label,
            'capabilities': ['read'], 'confirm_tasks': True, 'duplex': True,
            'idempotency_key': key()}, owner)['dot']
        return s.dots.join({'code': created['join_code'], 'idempotency_key': key()}, actor)['slot']
    mailbox_slot = join(broad, 'v2 member')
    unaffected = join(worker, 'unrelated v1')
    team_slots = [join(worker, 'team coordinator'), join(dot_actor, 'team worker')]
    configured = s.dot_mailbox.configure({**scope, 'dot_id': mailbox_slot['id'],
        'confirm_consumer_protocol': True, 'idempotency_key': key()}, broad)
    async def sender(url, body, headers):
        payload = json.loads(body)
        return (Reply(200, canonical({'challenge': payload['challenge']}).encode())
                if payload.get('type') == 'verification' else Reply(204))
    s.events = EventService(s, sender)
    await s.events.subscribe({**configured['subscription_request'], 'delivery': {
        'mode': 'webhook', 'url': 'https://rollback-fixture.example.invalid/events',
        'secret': 'whsec_' + base64.b64encode(b'rollback-fixture-secret-32-bytes!!').decode()}}, broad)
    team = s.coordination.teams.create({**scope, 'conversation_id': room['id'], 'label': 'rollback team',
        'dot_ids': [slot['id'] for slot in team_slots], 'coordinator_dot_id': team_slots[0]['id'],
        'project_ids': ['proj'], 'capabilities': ['read'], 'confirm_tasks': True,
        'idempotency_key': key()}, owner)['team']
    def source(text):
        result = s.chatroom.create({**scope, 'room_id': room['id'], 'conversation_id': room['id'],
            'body_text': text, 'mentions': [{'slot_id': team_slots[0]['id']}],
            'client_message_id': key(), 'idempotency_key': key()}, owner)['message']
        row = s.store.one('SELECT * FROM collaboration_messages WHERE id=?', (result['id'],))
        s.coordination.teams.bind_message(room, row, team['id'], owner)
        return row
    original = source('Read the approved current file')
    goal = s.coordination.teams.from_message({**scope, 'team_id': team['id'],
        'message_id': original['id'], 'source_version': original['version'],
        'source_digest': digest(json.loads(original['body'])['body_text']),
        'idempotency_key': key()}, worker)['goal']
    pending_source = source('A separately selected team source has no Goal yet')
    item = s.store.one('SELECT * FROM coordination_work WHERE goal_id=?', (goal['id'],))
    item = s.coordination.work_claim({**scope, 'goal_id': goal['id'], 'work_item_id': item['id'],
        'expected_version': item['version'], 'coordinator_epoch': 1, 'expected_intent_revision': 1,
        'idempotency_key': key()}, worker)['work_item']
    s.store.execute("""INSERT INTO operations
        (id,device_id,project_id,actor,grant_id,tool,args_summary,fingerprint,state,created,updated)
        VALUES ('unknown-rollback-operation','dev','proj','fixture','worker','exec','{}','fixture','running',?,?)""",
                    (clock[0], clock[0]))
    s.store.execute("""INSERT INTO coordination_operations
        (operation_id,goal_id,work_item_id,approval_id,attempt,fencing_token,project_id,grant_id,request_digest,created)
        VALUES ('unknown-rollback-operation',?,?,?,1,1,'proj','worker','fixture',?)""",
                    (goal['id'], item['id'], goal['approval_id'], clock[0]))
    # Keep source-version tests deterministic while other agents edit this shared
    # checkout. The real CLI separately hashes all source files and exact HEAD.
    stamp = {'revision': 'fixture-source', 'files': {'fixture': 'unchanged'}, 'fingerprint': 'fixture'}
    monkeypatch.setattr(freeze, 'source_stamp', lambda root=None: stamp)
    return SimpleNamespace(s=s, owner=owner, worker=worker, broad=broad, room=room, scope=scope,
        mailbox_slot=mailbox_slot, team_slots=team_slots, unaffected=unaffected, goal=goal,
        item=item, pending_source=pending_source, database=s.store.state_directory / 'hub.sqlite3')


def apply(c, plan=None):
    plan = plan or freeze.create_plan(c.database)
    return freeze.apply_plan(c.database, plan, expected_plan_sha256=freeze.digest(plan), hub_stopped=True)


def snapshots(c):
    tables = ('operations', 'coordination_operations', 'coordination_work', 'coordination_attempts',
              'coordination_approvals', 'collaboration_messages', 'dot_mailbox_bindings',
              'dot_mailbox_entries', 'dot_consumer_sessions', 'mcp_event_deliveries', 'dot_wake_tickets')
    return {table: c.s.store.all(f'SELECT * FROM {table}') for table in tables}


def test_plan_is_read_only_and_contains_exact_sources_and_current_operations(rollback_case):
    c = rollback_case
    before = c.s.store.one('SELECT total_changes() AS n')['n']
    plan = freeze.create_plan(c.database)
    assert c.s.store.one('SELECT total_changes() AS n')['n'] == before
    assert c.s.store.one("SELECT 1 FROM sqlite_master WHERE name='multidot_rollback_manifests'") is None
    slots = {row['id'] for row in plan['targets']['collaboration_join_slots']}
    assert slots == {c.mailbox_slot['id'], *(slot['id'] for slot in c.team_slots)}
    assert c.unaffected['id'] not in slots
    assert c.pending_source['id'] in {row['message_id'] for row in plan['scope']['selected_team_sources']}
    assert plan['original_operations_for_read_only_tracking'][0]['state'] == 'running'
    assert not plan['recovery']['restore_database_snapshot']


def test_freeze_uses_old_guard_states_and_preserves_original_ledgers(rollback_case):
    c = rollback_case
    original = snapshots(c)
    unaffected = c.s.joining.slot(c.room, c.unaffected['id'])
    receipt = apply(c)
    assert receipt['state'] == 'admission_frozen' and receipt['operations_replayed'] == 0
    assert snapshots(c) == original
    assert c.s.joining.slot(c.room, c.unaffected['id']) == unaffected
    for identifier in (c.mailbox_slot['id'], *(slot['id'] for slot in c.team_slots)):
        slot = c.s.joining.slot(c.room, identifier)
        assert slot['state'] == 'revoked'
        with pytest.raises(DevError) as exc:
            c.s.joining.active(c.room, slot)
        assert exc.value.code == 'JOIN_SLOT_REVOKED'
    policy = c.s.store.one('SELECT * FROM delegation_policies WHERE slot_id=?', (c.mailbox_slot['id'],))
    assert policy['state'] == 'paused'
    with pytest.raises(DevError) as exc:
        c.s.delegation.authorize_policy(c.broad, c.room, policy)
    assert exc.value.code == 'DELEGATION_POLICY_INACTIVE'
    goal = c.s.coordination.object(c.room, c.goal['id'])
    assert goal['state'] == 'paused'
    # Current endpoints reach states also understood by the baseline guards;
    # this is deliberately not described as an old-binary execution.
    with pytest.raises(DevError) as exc:
        c.s.coordination.admit({**c.scope, 'goal_id': goal['id'], 'work_item_id': c.item['id'],
            'attempt': c.item['attempt'], 'fencing_token': c.item['fencing_token'], 'tool': 'read', 'arguments': {'path': 'README.md'},
            'idempotency_key': key()}, c.worker)
    assert exc.value.code in {'JOIN_SLOT_REVOKED', 'DELEGATION_POLICY_INACTIVE', 'GOAL_INACTIVE'}
    assert c.s.coordination.authorize(c.owner, c.room, goal, active=False).user_id == c.owner.user_id
    operation = c.s.runtime.operation_row('unknown-rollback-operation', c.owner, status_only=True)
    assert operation['state'] == 'running'


def test_repeated_apply_returns_durable_receipt_without_more_state_changes(rollback_case):
    c = rollback_case
    plan = freeze.create_plan(c.database)
    first = apply(c, plan)
    states = {table: c.s.store.all(f'SELECT * FROM {table}') for table in freeze.CHANGE_TABLES}
    assert apply(c, plan) == first
    assert {table: c.s.store.all(f'SELECT * FROM {table}') for table in freeze.CHANGE_TABLES} == states
    assert c.s.store.one('SELECT COUNT(*) AS n FROM multidot_rollback_manifests')['n'] == 1


def test_changed_binding_or_source_version_aborts_before_any_freeze(rollback_case):
    c = rollback_case
    plan = freeze.create_plan(c.database)
    c.s.store.execute('UPDATE collaboration_join_slots SET version=version+1 WHERE id=?',
                      (c.mailbox_slot['id'],))
    with pytest.raises(freeze.FreezeError, match='changed'):
        apply(c, plan)
    assert c.s.joining.slot(c.room, c.mailbox_slot['id'])['state'] == 'registered'
    assert c.s.coordination.object(c.room, c.goal['id'])['state'] == 'active'
    assert not c.s.store.one("SELECT 1 FROM sqlite_master WHERE name='multidot_rollback_manifests'")


def test_failure_in_later_update_rolls_back_every_earlier_freeze(rollback_case):
    c = rollback_case
    c.s.store.execute("""CREATE TRIGGER rollback_fixture_failure BEFORE UPDATE ON coordination_goals
        WHEN NEW.state='paused' BEGIN SELECT RAISE(ABORT,'fixture rollback'); END""")
    plan = freeze.create_plan(c.database)
    with pytest.raises(sqlite3.IntegrityError, match='fixture rollback'):
        apply(c, plan)
    assert c.s.joining.slot(c.room, c.mailbox_slot['id'])['state'] == 'registered'
    assert c.s.coordination.object(c.room, c.goal['id'])['state'] == 'active'
    assert native_subscription(c)['state'] == 'active'
    assert not c.s.store.one("SELECT 1 FROM sqlite_master WHERE name='multidot_rollback_manifests'")


def test_apply_requires_exact_source_and_database_marker(rollback_case, monkeypatch):
    c = rollback_case
    plan = freeze.create_plan(c.database)
    monkeypatch.setattr(freeze, 'source_stamp', lambda root=None: {'revision': 'changed'})
    with pytest.raises(freeze.FreezeError, match='Source revision'):
        apply(c, plan)
    monkeypatch.setattr(freeze, 'source_stamp', lambda root=None: plan['source'])
    c.s.store.execute("UPDATE meta SET value='9' WHERE key='schema'")
    with pytest.raises(freeze.FreezeError, match='schema 10'):
        apply(c, plan)


def test_digest_and_explicit_offline_confirmation_are_required(rollback_case):
    c = rollback_case
    plan = freeze.create_plan(c.database)
    with pytest.raises(freeze.FreezeError, match='confirmation'):
        freeze.apply_plan(c.database, plan, expected_plan_sha256=freeze.digest(plan), hub_stopped=False)
    with pytest.raises(freeze.FreezeError, match='digest'):
        freeze.apply_plan(c.database, plan, expected_plan_sha256='not-the-reviewed-plan', hub_stopped=True)
    assert c.s.joining.slot(c.room, c.mailbox_slot['id'])['state'] == 'registered'


def test_export_never_overwrites_database_or_existing_backup(rollback_case, tmp_path):
    c = rollback_case
    original = c.database.read_bytes()
    with pytest.raises(FileExistsError):
        freeze.exclusive_export(c.database, {'not': 'a database'})
    assert c.database.read_bytes() == original
    target = tmp_path / 'manifest.json'
    freeze.exclusive_export(target, {'reviewable': True})
    assert json.loads(target.read_text()) == {'reviewable': True}
    with pytest.raises(FileExistsError):
        freeze.exclusive_export(target, {'overwritten': True})


@pytest.mark.asyncio
async def test_repeated_apply_refuses_new_active_scope_after_first_freeze(rollback_case):
    c = rollback_case
    plan = freeze.create_plan(c.database)
    apply(c, plan)
    created = c.s.dots.create({**c.scope, 'conversation_id': c.room['id'], 'label': 'post-freeze scope',
        'capabilities': ['read'], 'confirm_tasks': True, 'duplex': True,
        'idempotency_key': key()}, c.owner)['dot']
    joined = c.s.dots.join({'code': created['join_code'], 'idempotency_key': key()}, c.broad)['slot']
    config = c.s.dot_mailbox.configure({**c.scope, 'dot_id': joined['id'],
        'confirm_consumer_protocol': True, 'idempotency_key': key()}, c.broad)
    await c.s.events.subscribe({**config['subscription_request'], 'delivery': {
        'mode': 'webhook', 'url': 'https://rollback-fixture.example.invalid/new-scope',
        'secret': 'whsec_' + base64.b64encode(b'rollback-fixture-secret-32-bytes!!').decode()}}, c.broad)
    with pytest.raises(freeze.FreezeError, match='scope changed'):
        apply(c, plan)
    assert c.s.joining.slot(c.room, joined['id'])['state'] == 'registered'
    assert c.s.store.one('SELECT COUNT(*) AS n FROM multidot_rollback_manifests')['n'] == 1


def test_settled_original_operation_remains_readable_without_replaying_freeze(rollback_case):
    c = rollback_case
    plan = freeze.create_plan(c.database)
    receipt = apply(c, plan)
    c.s.store.execute("UPDATE operations SET state='succeeded',output_seq=1 WHERE id='unknown-rollback-operation'")
    assert apply(c, plan) == receipt
    assert c.s.store.one("SELECT state FROM operations WHERE id='unknown-rollback-operation'")['state'] == 'succeeded'


def test_terminal_goal_is_preserved_while_its_member_admission_is_frozen(rollback_case):
    c = rollback_case
    c.s.store.execute("UPDATE coordination_goals SET state='cancelled',version=version+1 WHERE id=?", (c.goal['id'],))
    before = c.s.coordination.object(c.room, c.goal['id'])
    apply(c)
    assert c.s.coordination.object(c.room, c.goal['id']) == before
    assert c.s.joining.slot(c.room, c.team_slots[0]['id'])['state'] == 'revoked'



def native_subscription(c):
    binding = c.s.dot_mailbox.binding(c.mailbox_slot['id'])
    return c.s.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?',
                         (binding['subscription_id'],))


def orphan_native_subscription(c):
    """Corrupt/stale route still selected by the old active-subscription scan."""
    row = native_subscription(c)
    row.update(id='orphan-' + key(), state='active', version=1,
               arguments=canonical({'project_id': 'proj', 'environment_id': 'production',
                    'conversation_id': c.room['id'], 'slot_id': 'missing-slot',
                    'binding_generation': 'missing-binding'}))
    fields = ','.join(row)
    c.s.store.execute('INSERT INTO mcp_event_subscriptions (' + fields + ') VALUES (' +
                      ','.join('?' for _ in row) + ')', tuple(row.values()))
    return row


def test_native_v2_route_is_frozen_without_touching_protocol_or_operation_ledgers(rollback_case):
    c = rollback_case
    sub = native_subscription(c)
    before = snapshots(c)
    plan = freeze.create_plan(c.database)
    targets = plan['targets']['mcp_event_subscriptions']
    assert [row['id'] for row in targets] == [sub['id']]
    assert targets[0]['name'] == sub['name'] and targets[0]['target_state'] == 'revoked'
    assert plan['recovery']['preserve_delivery_bodies_tickets_and_receipts']
    assert plan['recovery']['downgraded_key_maintenance_allowed'] is False
    receipt = apply(c, plan)
    changed = native_subscription(c)
    assert changed['state'] == 'revoked' and changed['version'] == sub['version'] + 1
    assert {key: value for key, value in changed.items() if key not in {'state', 'version', 'updated'}} == {
        key: value for key, value in sub.items() if key not in {'state', 'version', 'updated'}}
    assert snapshots(c) == before
    assert apply(c, plan) == receipt


@pytest.mark.parametrize('state,expired', [('active', True), ('expired', True), ('revoked', False),
                                         ('unsubscribed', False)])
def test_native_route_scope_includes_expired_and_nonactive_states(rollback_case, state, expired):
    c = rollback_case
    sub = native_subscription(c)
    c.s.store.execute('UPDATE mcp_event_subscriptions SET state=?,expires_at=? WHERE id=?',
        (state, c.s.clock() - 1 if expired else sub['expires_at'], sub['id']))
    plan = freeze.create_plan(c.database)
    expected = 'revoked' if state == 'active' else state
    assert plan['targets']['mcp_event_subscriptions'][0]['target_state'] == expected
    receipt = apply(c, plan)
    assert native_subscription(c)['state'] == expected
    assert apply(c, plan) == receipt


def test_orphan_active_v2_route_is_frozen_independently_of_mailbox_bindings(rollback_case):
    c = rollback_case
    orphan = orphan_native_subscription(c)
    plan = freeze.create_plan(c.database)
    assert orphan['id'] in {row['id'] for row in plan['targets']['mcp_event_subscriptions']}
    apply(c, plan)
    assert c.s.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (orphan['id'],))['state'] == 'revoked'


def test_native_route_changes_refuse_original_plan_before_any_atomic_update(rollback_case):
    c = rollback_case
    plan = freeze.create_plan(c.database)
    sub = native_subscription(c)
    c.s.store.execute('UPDATE mcp_event_subscriptions SET expires_at=expires_at+1 WHERE id=?', (sub['id'],))
    with pytest.raises(freeze.FreezeError, match='changed'):
        apply(c, plan)
    assert native_subscription(c)['state'] == 'active'
    assert c.s.joining.slot(c.room, c.mailbox_slot['id'])['state'] == 'registered'


def test_added_orphan_v2_route_refuses_a_previously_applied_plan(rollback_case):
    c = rollback_case
    plan = freeze.create_plan(c.database)
    apply(c, plan)
    orphan = orphan_native_subscription(c)
    with pytest.raises(freeze.FreezeError, match='scope changed'):
        apply(c, plan)
    assert c.s.store.one('SELECT state FROM mcp_event_subscriptions WHERE id=?', (orphan['id'],))['state'] == 'active'
    assert c.s.store.one('SELECT COUNT(*) AS n FROM multidot_rollback_manifests')['n'] == 1


@pytest.fixture
def source_proof_tree(tmp_path):
    """A real reviewed Git checkout plus a source archive nested inside it."""
    root = tmp_path / 'reviewed-source'
    root.mkdir()
    source = Path(freeze.__file__).resolve().parents[1]
    for name in freeze.SOURCE_FILES:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    commands = [
        ['git', '-c', 'init.templateDir=', 'init', '-q'],
        ['git', 'add', '--', *freeze.SOURCE_FILES],
        ['git', '-c', 'user.name=Rollback fixture', '-c', 'user.email=rollback@example.invalid',
         '-c', 'core.hooksPath=' + os.devnull, 'commit', '--no-verify', '--no-gpg-sign', '-qm', 'fixture'],
    ]
    for command in commands:
        subprocess.run(command, cwd=root, env=env, check=True, capture_output=True, timeout=10)
    proof = tmp_path / 'source-proof.json'
    exported = subprocess.run([sys.executable, str(root / 'scripts/prepare_multidot_rollback.py'),
        '--export-source-proof', str(proof)], cwd=root, env=env,
        check=True, capture_output=True, text=True, timeout=10)
    receipt = json.loads(exported.stdout)
    archive = root / 'archive-without-git'
    for name in freeze.SOURCE_FILES:
        target = archive / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / name, target)
    assert not (archive / '.git').exists()
    return SimpleNamespace(root=root, archive=archive, proof=proof, receipt=receipt, env=env,
        proof_sha256=receipt['source_proof_sha256'], source=receipt['source'])


def proof_options(tree):
    return {'source_proof': tree.proof, 'expected_source_proof_sha256': tree.proof_sha256}


def test_source_proof_export_is_database_free_exact_and_exclusive(source_proof_tree):
    t = source_proof_tree
    data = t.proof.read_bytes()
    assert hashlib.sha256(data).hexdigest() == t.proof_sha256
    assert json.loads(data) == {'format': freeze.SOURCE_PROOF_FORMAT, 'source': t.source}
    assert t.source == freeze.source_stamp(t.root)
    assert set(t.source['files']) == set(freeze.SOURCE_FILES)
    assert t.source['fingerprint'] == freeze.digest([t.source['revision'], t.source['files']])
    with pytest.raises(FileExistsError):
        freeze.export_source_proof(t.proof, source_root=t.root)
    assert t.proof.read_bytes() == data
    assert not list(t.root.rglob('*.sqlite3'))


def test_nested_archive_cannot_inherit_parent_git_identity(source_proof_tree):
    t = source_proof_tree
    actual_parent = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=t.archive,
        env=t.env, check=True, capture_output=True, text=True, timeout=10).stdout.strip()
    assert Path(actual_parent).resolve() == t.root.resolve()
    with pytest.raises(freeze.FreezeError, match='not the exact Git checkout'):
        freeze.checked_source(t.archive)
    with pytest.raises(freeze.FreezeError, match='not the exact Git checkout'):
        freeze.export_source_proof(t.archive / 'untrusted-proof.json', source_root=t.archive)
    assert freeze.checked_source(t.archive, **proof_options(t)) == t.source


@pytest.mark.parametrize('case', [
    'wrong_external_sha', 'missing_external_sha', 'malformed_json', 'duplicate_key',
    'extra_field', 'wrong_format', 'missing_file', 'extra_relative_path', 'absolute_path',
    'bad_revision', 'bad_file_digest', 'bad_fingerprint', 'oversized',
])
def test_source_proof_rejects_tampering_or_inexact_inventory(source_proof_tree, case, tmp_path):
    t = source_proof_tree
    proof = json.loads(t.proof.read_bytes())
    if case == 'extra_field':
        proof['unexpected'] = True
    elif case == 'wrong_format':
        proof['format'] = 'unreviewed'
    elif case == 'missing_file':
        proof['source']['files'].pop('scripts/prepare_multidot_rollback.py')
    elif case == 'extra_relative_path':
        proof['source']['files']['../outside.py'] = '0' * 64
    elif case == 'absolute_path':
        proof['source']['files']['/outside.py'] = '0' * 64
    elif case == 'bad_revision':
        proof['source']['revision'] = 'latest'
    elif case == 'bad_file_digest':
        proof['source']['files']['hub/store.py'] = 'not-a-sha'
    elif case == 'bad_fingerprint':
        proof['source']['fingerprint'] = '0' * 64
    payload = json.dumps(proof).encode()
    if case == 'malformed_json':
        payload = b'{incomplete'
    elif case == 'duplicate_key':
        payload = b'{"format":"duplicate",' + payload[1:]
    elif case == 'oversized':
        payload += b' ' * 65536
    path = tmp_path / 'changed-proof.json'
    path.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    if case == 'wrong_external_sha':
        expected = '0' * 64
    elif case == 'missing_external_sha':
        expected = None
    with pytest.raises(freeze.FreezeError, match='Source proof'):
        freeze.checked_source(t.archive, source_proof=path, expected_source_proof_sha256=expected)


@pytest.mark.parametrize('case', ['guard_drift', 'script_drift', 'missing_file', 'symlink'])
def test_source_proof_checks_real_fixed_files(source_proof_tree, case, tmp_path):
    t = source_proof_tree
    path = t.archive / ('scripts/prepare_multidot_rollback.py' if case == 'script_drift'
                        else 'hub/collaboration/joining.py')
    original = path.read_bytes()
    if case in {'guard_drift', 'script_drift'}:
        path.write_bytes(original + b'\n# archive source drift\n')
    elif case == 'missing_file':
        path.unlink()
    else:
        outside = tmp_path / 'outside.py'
        outside.write_bytes(original)
        path.unlink()
        path.symlink_to(outside)
    with pytest.raises(freeze.FreezeError, match='Source file'):
        freeze.checked_source(t.archive, **proof_options(t))


def test_source_proof_checks_the_executing_script_too(source_proof_tree, tmp_path, monkeypatch):
    t = source_proof_tree
    other = tmp_path / 'different-runner.py'
    other.write_bytes(Path(freeze.__file__).read_bytes() + b'\n# different executing implementation\n')
    monkeypatch.setattr(freeze, '__file__', str(other))
    with pytest.raises(freeze.FreezeError, match='executing rollback script'):
        freeze.checked_source(t.archive, **proof_options(t))


def test_proof_cannot_replace_a_git_checkouts_actual_revision(source_proof_tree, tmp_path):
    t = source_proof_tree
    proof = json.loads(t.proof.read_bytes())
    source = proof['source']
    source['revision'] = '0' * 40
    source['fingerprint'] = freeze.digest([source['revision'], source['files']])
    path = tmp_path / 'other-revision.json'
    expected = freeze.exclusive_export(path, proof)
    with pytest.raises(freeze.FreezeError, match='exact Git checkout identity'):
        freeze.checked_source(t.root, source_proof=path, expected_source_proof_sha256=expected)


def test_archive_cli_plan_apply_and_replay_require_the_same_source_identity(
        rollback_case, source_proof_tree, tmp_path):
    c, t = rollback_case, source_proof_tree
    before = snapshots(c)
    c.s.store.close()
    command = [sys.executable, str(t.archive / 'scripts/prepare_multidot_rollback.py'),
               '--database', str(c.database)]
    source_args = ['--source-proof', str(t.proof),
                   '--expected-source-proof-sha256', t.proof_sha256]

    def run(*args):
        return subprocess.run([*command, *args], cwd=t.archive, env=t.env,
                              capture_output=True, text=True, timeout=10)

    original_db = c.database.read_bytes()
    missing = run()
    assert missing.returncode != 0 and 'not the exact Git checkout' in missing.stderr
    unpinned = run('--source-proof', str(t.proof))
    assert unpinned.returncode != 0 and 'required together' in unpinned.stderr
    wrong = run('--source-proof', str(t.proof), '--expected-source-proof-sha256', '0' * 64)
    assert wrong.returncode != 0 and 'does not match' in wrong.stderr
    assert c.database.read_bytes() == original_db

    plan_path = tmp_path / 'archive-plan.json'
    dry = run(*source_args, '--plan-out', str(plan_path))
    assert dry.returncode == 0, dry.stderr
    review = json.loads(dry.stdout)
    assert review['state'] == 'dry_run' and review['plan']['source'] == t.source
    assert c.database.read_bytes() == original_db
    args = ['--apply', str(plan_path), '--expected-plan-sha256', review['plan_sha256'],
            '--confirm-hub-stopped']
    drift = t.archive / 'hub/collaboration/joining.py'
    original_source = drift.read_bytes()
    drift.write_bytes(original_source + b'\n# changed after plan review\n')
    changed = run(*source_args, *args)
    assert changed.returncode != 0 and 'Source files changed' in changed.stderr
    assert c.database.read_bytes() == original_db
    drift.write_bytes(original_source)

    other_proof = json.loads(t.proof.read_bytes())
    other_source = other_proof['source']
    other_source['revision'] = '0' * 40
    other_source['fingerprint'] = freeze.digest([other_source['revision'], other_source['files']])
    other_path = tmp_path / 'other-source-proof.json'
    other_sha = freeze.exclusive_export(other_path, other_proof)
    other_args = ['--source-proof', str(other_path), '--expected-source-proof-sha256', other_sha]
    crossed = run(*other_args, *args)
    assert crossed.returncode != 0 and 'Source revision or source files changed' in crossed.stderr
    assert c.database.read_bytes() == original_db

    applied = run(*source_args, *args)
    assert applied.returncode == 0, applied.stderr
    receipt = json.loads(applied.stdout)
    assert receipt['state'] == 'admission_frozen' and receipt['operations_replayed'] == 0
    replay = run(*source_args, *args)
    assert replay.returncode == 0 and json.loads(replay.stdout) == receipt
    crossed_replay = run(*other_args, *args)
    assert crossed_replay.returncode != 0 and 'Source revision or source files changed' in crossed_replay.stderr
    with sqlite3.connect(c.database) as db:
        db.row_factory = sqlite3.Row
        after = {table: [dict(row) for row in db.execute('SELECT * FROM ' + table)] for table in before}
        assert after == before
        assert db.execute('SELECT COUNT(*) FROM multidot_rollback_manifests').fetchone()[0] == 1
        assert db.execute('SELECT state FROM collaboration_join_slots WHERE id=?',
                          (c.mailbox_slot['id'],)).fetchone()[0] == 'revoked'
