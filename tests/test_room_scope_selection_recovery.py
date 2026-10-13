"""Selection recovery is UX-only: synthetic stores, no grants or channels expanded."""
import json

import pytest

from tests.collaboration_support import key
from tests.test_collaboration_delegation_bridge import bridge as bridge, panel, rpc, tool
from tests.test_conversation_project_revision_http import setup_room, mutation, selection


TABLES = (
    'grants', 'room_scope_manifests', 'conversation_working_scopes',
    'collaboration_rooms', 'conversation_rooms', 'conversation_projects',
    'conversation_project_revocations', 'collaboration_dots', 'collaboration_join_slots',
    'delegation_policies', 'mcp_event_subscriptions', 'coordination_goals', 'coordination_work',
)


def saved_state(b):
    return {table: b.app.state.store.all('SELECT * FROM ' + table + ' ORDER BY rowid')
            for table in TABLES}


def setup_screen(b):
    joined = setup_room(b)
    b.app.state.store.execute("UPDATE projects SET alias='PrimaryFolder',alias_key='primaryfolder' WHERE id='project'")
    b.app.state.store.execute("UPDATE projects SET alias='SecondaryFolder',alias_key='secondaryfolder' WHERE id='otherproject'")
    return joined


def ceiling(b, joined, rules):
    return {**b.scope, 'conversation_id': joined['slot']['conversation_id'], 'grant_id': b.grant,
            'expected_version': 0, 'project_rules': rules,
            'confirm_connection_wide_ceiling': True, 'idempotency_key': key()}


def rule(identifier, capabilities=None):
    return {'project_id': identifier, 'capabilities': capabilities or ['read']}


@pytest.mark.parametrize('ids,code', [
    (['otherproject'], 'ROOM_SCOPE_ANCHOR_REQUIRED'), ([], 'ROOM_SCOPE_EMPTY'),
])
def test_reported_advanced_route_redirects_secondary_or_chat_only_selection_without_writes(bridge, ids, code):
    b = bridge
    joined = setup_screen(b)
    panel(b, 'conversation-projects', mutation(b, joined, ids))
    baseline = saved_state(b)
    changes = b.app.state.store.one('SELECT total_changes() AS n')['n']
    response = b.client.post('/api/collaboration/room-scope',
                             json=ceiling(b, joined, [rule(identifier) for identifier in ids]))
    assert response.status_code == 422
    error = response.json()['error']
    assert error['code'] == code
    assert error['remedy'] == 'edit_room_projects'
    assert error['conversation_id'] == joined['slot']['conversation_id']
    assert error['changes_applied'] is False
    assert '管理房间项目' in error['message']
    assert '必须包含当前房间项目' not in error['message']
    assert saved_state(b) == baseline
    assert b.app.state.store.one('SELECT total_changes() AS n')['n'] == changes


def test_working_duplicates_preview_and_same_key_replay_have_one_scope_revision(bridge):
    b = bridge
    joined = setup_screen(b)
    single = selection(b, joined, ['otherproject'])
    repeated = {**single, 'projects': single['projects'] * 3}
    preview = panel(b, 'conversation-projects-preview', single)
    assert panel(b, 'conversation-projects-preview', repeated) == preview
    args = mutation(b, joined, ['otherproject'])
    baseline = b.app.state.store.all('SELECT * FROM grants')
    saved = panel(b, 'conversation-projects', {**args, 'projects': args['projects'] * 3})
    assert panel(b, 'conversation-projects', args) == saved
    assert panel(b, 'conversation-projects', {**args, 'projects': args['projects'] * 2}) == saved
    assert len(b.app.state.store.all('SELECT * FROM conversation_working_scopes')) == 1
    assert [project['project_id'] for project in saved['conversation']['projects']] == ['otherproject']
    assert b.app.state.store.all('SELECT * FROM grants') == baseline
    assert not b.app.state.store.all('SELECT * FROM room_scope_manifests')


def test_missing_dot_choice_recovers_the_existing_connection_without_replacement(bridge):
    b = bridge
    joined = setup_screen(b)
    panel(b, 'conversation-projects', mutation(b, joined, ['otherproject']))
    original = b.app.state.store.all('SELECT * FROM collaboration_dots')
    raw = selection(b, joined, [])
    raw.pop('dot_id')
    preview = panel(b, 'conversation-projects-preview', raw)
    assert preview['ready'] and preview['connection']['dot_id'] == joined['slot']['id']
    applied = panel(b, 'conversation-projects', {
        **raw, 'expected_version': preview['conversation_version'],
        'expected_scope_version': preview['scope_version'],
        'selection_digest': preview['selection_digest'], 'confirm_scope': True, 'idempotency_key': key(),
    })
    assert applied['conversation']['projects'] == []
    assert b.app.state.store.all('SELECT * FROM collaboration_dots') == original


def test_different_environments_are_not_silently_combined(bridge):
    b = bridge
    joined = setup_screen(b)
    raw = selection(b, joined, ['otherproject'])
    raw['projects'].append({'project': 'otherproject', 'environment_id': 'staging'})
    baseline = saved_state(b)
    preview = panel(b, 'conversation-projects-preview', raw)
    assert not preview['ready'] and preview['reason_code'] == 'DUPLICATE_PROJECT'
    assert '不同环境' in preview['message']
    assert saved_state(b) == baseline


def test_global_exact_duplicates_have_one_rule_and_one_idempotent_receipt(bridge):
    b = bridge
    joined = setup_screen(b)
    panel(b, 'conversation-projects', mutation(b, joined, ['otherproject']))
    first, second = rule('project'), rule('otherproject')
    args = ceiling(b, joined, [first, second])
    before_grants = b.app.state.store.all('SELECT * FROM grants')
    result = panel(b, 'room-scope', {**args, 'project_rules': [first, first, second, first, second]})
    assert result['affects_all_chats_using_this_grant']
    assert panel(b, 'room-scope', args) == result
    assert panel(b, 'room-scope', {**args, 'project_rules': [first, second, second]}) == result
    rows = b.app.state.store.all('SELECT * FROM room_scope_manifests')
    assert len(rows) == 1 and rows[0]['version'] == 1
    assert len(json.loads(rows[0]['manifest'])['project_rules']) == 2
    assert b.app.state.store.all('SELECT * FROM grants') == before_grants


@pytest.mark.parametrize('second', [
    {'project_id': 'project', 'capabilities': ['read', 'write']},
    {'project_id': 'project', 'capabilities': ['read'],
     'execution_targets': ['vps:' + 'a' * 32]},
])
def test_duplicate_conflicting_global_rules_never_union_permissions_or_targets(bridge, second):
    b = bridge
    joined = setup_screen(b)
    args = ceiling(b, joined, [rule('project'), second])
    before = saved_state(b)
    changes = b.app.state.store.one('SELECT total_changes() AS n')['n']
    response = b.client.post('/api/collaboration/room-scope', json=args)
    assert response.status_code == 422
    error = response.json()['error']
    assert error['code'] == 'ROOM_SCOPE_PROJECT_RULE_CONFLICT'
    assert error['remedy'] == 'review_connection_rules'
    assert error['changes_applied'] is False
    assert saved_state(b) == before
    assert b.app.state.store.one('SELECT total_changes() AS n')['n'] == changes


def test_working_selection_never_rewrites_existing_global_ceiling_or_history(bridge):
    b = bridge
    joined = setup_screen(b)
    panel(b, 'conversation-projects', mutation(b, joined, ['otherproject']))
    panel(b, 'room-scope', ceiling(b, joined, [rule('project'), rule('otherproject')]))
    original_ceiling = b.app.state.store.all('SELECT * FROM room_scope_manifests')
    original_revision = b.app.state.store.one('SELECT * FROM conversation_working_scopes WHERE version=1')
    original_grants = b.app.state.store.all('SELECT * FROM grants')
    panel(b, 'conversation-projects', mutation(b, joined, []))
    assert b.app.state.store.all('SELECT * FROM room_scope_manifests') == original_ceiling
    assert b.app.state.store.one('SELECT * FROM conversation_working_scopes WHERE version=1') == original_revision
    assert b.app.state.store.all('SELECT * FROM grants') == original_grants


def test_stale_selection_is_not_blindly_replayed_and_does_not_restore_removed_project(bridge):
    b = bridge
    joined = setup_screen(b)
    old_args = mutation(b, joined, ['otherproject'])
    panel(b, 'conversation-projects', old_args)
    panel(b, 'conversation-projects', mutation(b, joined, []))
    baseline = saved_state(b)
    response = b.client.post('/api/collaboration/conversation-projects',
        json={**old_args, 'projects': old_args['projects'] * 2, 'idempotency_key': key()})
    assert response.status_code == 409 and response.json()['error']['code'] == 'STALE_VERSION'
    assert saved_state(b) == baseline


@pytest.mark.parametrize('condition', ['grant_ceiling', 'root_overlap'])
def test_real_security_denials_remain_enforced_after_duplicate_normalization(bridge, condition):
    b = bridge
    joined = setup_screen(b)
    if condition == 'grant_ceiling':
        rules = [rule('project', ['read', 'write'])]
        expected = 'ROOM_SCOPE_CEILING_EXCEEDED'
    else:
        root = b.app.state.store.one("SELECT root FROM projects WHERE id='project'")['root']
        b.app.state.store.execute("UPDATE projects SET root=? WHERE id='otherproject'", (root + '/nested',))
        rules = [rule('project')]
        expected = 'ROOM_SCOPE_ROOT_OVERLAP'
    baseline = saved_state(b)
    response = b.client.post('/api/collaboration/room-scope', json=ceiling(b, joined, rules * 2))
    assert response.status_code in {403, 409}
    assert response.json()['error']['code'] == expected
    assert saved_state(b) == baseline


def test_revoked_grant_cannot_use_normalized_selection_or_read_message_history(bridge):
    b = bridge
    joined = setup_screen(b)
    panel(b, 'conversation-projects', mutation(b, joined, ['otherproject']))
    b.app.state.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (b.grant,))
    before = saved_state(b)
    denied = b.client.post('/api/collaboration/room-scope',
        json=ceiling(b, joined, [rule('project'), rule('project')]))
    assert denied.status_code == 403
    assert denied.json()['error']['code'] == 'ROOM_SCOPE_GRANT_INVALID'
    args = {'action': 'dot_inbox', **b.scope, 'dot_id': joined['slot']['id']}
    reply = rpc(b, 'tools/call', {'name': 'collaboration_query', 'arguments': args})
    assert reply.status_code in {401, 403} or (
        reply.status_code == 200 and ('error' in reply.json() or reply.json()['result'].get('isError')))
    assert saved_state(b) == before


def setup_connection_limit(b, kind):
    joined = setup_screen(b)
    if kind == 'capability':
        b.app.state.store.execute('UPDATE grants SET scopes=? WHERE id=?',
                                  (json.dumps(['read', 'write']), b.grant))
        dot = panel(b, 'dot', {
            **b.scope, 'conversation_id': b.room['id'], 'label': 'Writable fixture dot',
            'duplex': True, 'capabilities': ['read', 'write'], 'confirm_tasks': True,
            'idempotency_key': key(),
        })['dot']
        joined = tool(b, 'collaboration', {
            'action': 'join', 'code': dot['join_code'], 'idempotency_key': key(),
        })
    panel(b, 'conversation-projects', mutation(b, joined, ['otherproject']))
    panel(b, 'conversation-projects', mutation(b, joined, []))
    args = mutation(b, joined, ['otherproject'])
    rules = [rule('project', ['read', 'write'] if kind == 'capability' else ['read'])]
    if kind == 'capability':
        rules.append(rule('otherproject'))
    panel(b, 'room-scope', ceiling(b, joined, rules))
    return joined, args


@pytest.mark.parametrize('kind', ['project', 'capability'])
def test_existing_connection_ceiling_explains_one_owner_confirmation_without_saving(bridge, kind):
    b = bridge
    joined, args = setup_connection_limit(b, kind)
    baseline = saved_state(b)
    changes = b.app.state.store.one('SELECT total_changes() AS n')['n']
    preview = panel(b, 'conversation-projects-preview', selection(b, joined, ['otherproject']))
    assert preview['ready'] is False
    assert preview['reason_code'] == 'ROOM_PROJECT_CONNECTION_LIMITED'
    assert preview['remedy'] == 'review_connection_scope'
    assert preview['grant_id'] == b.grant
    assert preview['conversation_id'] == joined['slot']['conversation_id']
    assert preview['changes_applied'] is False
    assert preview['changes_underlying_grant'] is False
    assert preview['restricted_projects'] == [{
        'project_id': 'otherproject',
        'capabilities': ['read'] if kind == 'project' else ['write'],
        'execution_targets': ['project_agent'] if kind == 'project' else [],
    }]
    denied = b.client.post('/api/collaboration/conversation-projects', json=args)
    assert denied.status_code == 403
    assert denied.json()['error']['code'] == 'ROOM_PROJECT_CONNECTION_LIMITED'
    assert denied.json()['error']['restricted_projects'] == preview['restricted_projects']
    assert saved_state(b) == baseline
    assert b.app.state.store.one('SELECT total_changes() AS n')['n'] == changes


@pytest.mark.parametrize('condition', ['grant_revoked', 'ceiling_revoked', 'unknown_project', 'grant_excludes_project'])
def test_ceiling_hint_never_masks_real_revocation_or_discloses_ungranted_projects(bridge, condition):
    b = bridge
    joined, _ = setup_connection_limit(b, 'project')
    raw = selection(b, joined, ['otherproject'])
    if condition == 'grant_revoked':
        b.app.state.store.execute('UPDATE grants SET revoked=1 WHERE id=?', (b.grant,))
    elif condition == 'ceiling_revoked':
        b.app.state.store.execute("UPDATE room_scope_manifests SET state='revoked' WHERE grant_id=?", (b.grant,))
    elif condition == 'grant_excludes_project':
        b.app.state.store.execute('UPDATE grants SET projects=? WHERE id=?', (json.dumps(['project']), b.grant))
    else:
        raw['projects'] = [{'project': 'ungranted-secret', 'environment_id': 'production'}]
    baseline = saved_state(b)
    preview = panel(b, 'conversation-projects-preview', raw)
    assert not preview['ready']
    assert preview['reason_code'] != 'ROOM_PROJECT_CONNECTION_LIMITED'
    assert 'restricted_projects' not in preview and 'remedy' not in preview
    assert saved_state(b) == baseline


def test_connection_limit_details_require_authenticated_owner_session(bridge):
    b = bridge
    joined, _ = setup_connection_limit(b, 'project')
    response = b.client.post('/api/collaboration/conversation-projects-preview',
        json=selection(b, joined, ['otherproject']),
        headers={'Cookie': 'rd_session=invalid', 'Authorization': 'Bearer ' + b.token})
    assert response.status_code in {401, 403}
    assert 'restricted_projects' not in response.text


def test_explicit_single_ceiling_confirmation_then_room_selection_needs_no_extra_membership_step(bridge):
    b = bridge
    joined, args = setup_connection_limit(b, 'project')
    original_grants = b.app.state.store.all('SELECT * FROM grants')
    history = b.app.state.store.all('SELECT * FROM conversation_working_scopes ORDER BY version')
    confirm = {**ceiling(b, joined, [rule('project'), rule('otherproject')]), 'expected_version': 1}
    panel(b, 'room-scope', confirm)
    preview = panel(b, 'conversation-projects-preview', selection(b, joined, ['otherproject']))
    assert preview['ready']
    assert 'remedy' not in preview
    applied = panel(b, 'conversation-projects', {**args, 'selection_digest': preview['selection_digest']})
    assert [p['project_id'] for p in applied['conversation']['projects']] == ['otherproject']
    assert b.app.state.store.all('SELECT * FROM grants') == original_grants
    assert b.app.state.store.all('SELECT * FROM conversation_working_scopes WHERE version<=2 ORDER BY version') == history


def setup_target_selection(b, requested):
    setup_screen(b)
    store = b.app.state.store
    store.execute('UPDATE grants SET scopes=? WHERE id=?',
                  (json.dumps(['read', 'write', 'execute']), b.grant))
    targets = {}
    for name in ('first', 'second'):
        response = b.client.post('/api/vps', json={
            'name': name, 'host': name + '.example.invalid', 'username': 'fixture',
            'password': 'synthetic-target-test-only', 'project_ids': ['project', 'otherproject'],
        })
        assert response.is_success, response.text
        targets[name] = response.json()['target']
    target = 'project_agent' if requested == 'agent' else targets[requested]
    dot = panel(b, 'dot', {
        **b.scope, 'conversation_id': b.room['id'], 'label': 'Target-bound fixture dot',
        'duplex': True, 'capabilities': ['read', 'execute'], 'confirm_tasks': True,
        'execution_target': target, 'acknowledge_unsandboxed_exec': True, 'idempotency_key': key(),
    })['dot']
    joined = tool(b, 'collaboration', {
        'action': 'join', 'code': dot['join_code'], 'idempotency_key': key(),
    })
    panel(b, 'conversation-projects', mutation(b, joined, ['otherproject']))
    panel(b, 'conversation-projects', mutation(b, joined, []))
    return joined, targets, target


@pytest.mark.parametrize('requested,allowed', [('agent', 'first'), ('first', 'agent'), ('first', 'second')])
def test_scope_preview_checks_exact_execution_target_ceiling_before_reporting_ready(bridge, requested, allowed):
    b = bridge
    joined, targets, target = setup_target_selection(b, requested)
    args = mutation(b, joined, ['otherproject'])
    actual_target = 'agent' if target == 'project_agent' else target
    allowed_target = 'agent' if allowed == 'agent' else targets[allowed]
    rules = [
        {**rule('project', ['read', 'execute']), 'execution_targets': [actual_target]},
        {**rule('otherproject', ['read', 'execute']), 'execution_targets': [allowed_target]},
    ]
    panel(b, 'room-scope', {
        **ceiling(b, joined, rules), 'execution_mode': 'account_permissions_acknowledged',
        'acknowledge_unsandboxed_exec': True,
    })
    before = saved_state(b)
    operations = b.app.state.store.all('SELECT * FROM operations')
    preview = panel(b, 'conversation-projects-preview', selection(b, joined, ['otherproject']))
    assert not preview['ready']
    assert preview['reason_code'] == 'ROOM_PROJECT_CONNECTION_LIMITED'
    assert preview['restricted_projects'] == [{
        'project_id': 'otherproject', 'capabilities': [], 'execution_targets': [target],
    }]
    denied = b.client.post('/api/collaboration/conversation-projects', json=args)
    assert denied.status_code == 403
    assert denied.json()['error']['code'] == 'ROOM_PROJECT_CONNECTION_LIMITED'
    assert saved_state(b) == before
    assert b.app.state.store.all('SELECT * FROM operations') == operations
    # This guard cannot prevent safety-only narrowing when execution is unavailable.
    empty = panel(b, 'conversation-projects', mutation(b, joined, []))
    assert empty['conversation']['projects'] == []


@pytest.mark.parametrize('requested', ['agent', 'first'])
def test_no_connection_ceiling_needs_no_second_target_configuration(bridge, requested):
    b = bridge
    joined, _, target = setup_target_selection(b, requested)
    baseline_grants = b.app.state.store.all('SELECT * FROM grants')
    preview = panel(b, 'conversation-projects-preview', selection(b, joined, ['otherproject']))
    assert preview['ready']
    assert preview['projects'][0]['execution_targets'] == [target]
    saved = panel(b, 'conversation-projects', mutation(b, joined, ['otherproject']))
    assert [p['project_id'] for p in saved['conversation']['projects']] == ['otherproject']
    assert b.app.state.store.all('SELECT * FROM grants') == baseline_grants
    assert not b.app.state.store.all('SELECT * FROM room_scope_manifests')
    assert not b.app.state.store.all('SELECT * FROM operations')


def test_never_joined_project_can_be_explicitly_confirmed_before_working_membership(bridge):
    from hub.room_scope import current
    b = bridge
    joined = setup_screen(b)
    store = b.app.state.store
    panel(b, 'conversation-projects', mutation(b, joined, ['project']))
    panel(b, 'room-scope', ceiling(b, joined, [rule('project')]))
    assert not store.one("SELECT 1 FROM collaboration_rooms WHERE project_id='otherproject'")
    limited = panel(b, 'conversation-projects-preview', selection(b, joined, ['otherproject']))
    assert limited['reason_code'] == 'ROOM_PROJECT_CONNECTION_LIMITED'
    frozen_tables = ('grants', 'collaboration_rooms', 'conversation_rooms', 'conversation_projects',
                     'conversation_working_scopes', 'conversation_project_revocations',
                     'collaboration_dots', 'collaboration_join_slots', 'mcp_event_subscriptions',
                     'delegation_policies')
    baseline = {table: store.all('SELECT * FROM ' + table + ' ORDER BY rowid') for table in frozen_tables}
    approval = {**ceiling(b, joined, [rule('project'), rule('otherproject')]), 'expected_version': 1}
    confirmed = panel(b, 'room-scope', approval)
    assert panel(b, 'room-scope', approval) == confirmed
    assert current(store, b.grant)['manifest']['project_membership_mode'] == 'grant_ceiling'
    assert {table: store.all('SELECT * FROM ' + table + ' ORDER BY rowid') for table in frozen_tables} == baseline
    assert not store.one("SELECT 1 FROM collaboration_rooms WHERE project_id='otherproject'")
    tool(b, 'collaboration_query', {'action': 'dot_connection', **b.scope, 'dot_id': joined['slot']['id']})
    saved = panel(b, 'conversation-projects', mutation(b, joined, ['otherproject']))
    assert [p['project_id'] for p in saved['conversation']['projects']] == ['otherproject']
    assert store.one("SELECT 1 FROM collaboration_rooms WHERE project_id='otherproject'")
    assert store.all('SELECT * FROM grants ORDER BY rowid') == baseline['grants']
    assert store.all('SELECT * FROM conversation_working_scopes WHERE version=1') == baseline['conversation_working_scopes']
    assert store.all('SELECT * FROM conversation_rooms ORDER BY rowid')[0]['id'] == baseline['conversation_rooms'][0]['id']
    assert len(store.all('SELECT * FROM conversation_rooms')) == len(baseline['conversation_rooms'])


def copy_fixture_row(store, table, source_id, **changes):
    row = {**store.one('SELECT * FROM ' + table + ' WHERE id=?', (source_id,)), **changes}
    store.execute('INSERT INTO ' + table + '(' + ','.join(row) + ') VALUES (' +
                  ','.join('?' for _ in row) + ')', tuple(row.values()))
    return row


def foreign_fixture_space(store):
    store.execute("INSERT OR IGNORE INTO spaces(id,label,kind,created) VALUES ('other-space','Other fixture','team',0)")


@pytest.mark.parametrize('condition', [
    'unknown_project', 'cross_space_project', 'grant_excludes_project', 'capability',
    'unknown_vps', 'unbound_vps', 'cross_space_vps', 'grant_revoked', 'wrong_environment',
])
def test_independent_ceiling_keeps_real_denials_atomic_for_never_joined_projects(bridge, condition):
    b = bridge
    joined = setup_screen(b)
    store = b.app.state.store
    rules = [rule('project'), rule('otherproject')]
    args = ceiling(b, joined, rules)
    if condition == 'unknown_project':
        rules[1]['project_id'] = 'missing-fixture'
    elif condition == 'cross_space_project':
        foreign_fixture_space(store)
        copy_fixture_row(store, 'devices', 'device', id='foreign-device', space_id='other-space')
        copy_fixture_row(store, 'projects', 'otherproject', id='foreign-project',
                         space_id='other-space', device_id='foreign-device', alias='foreign', alias_key='foreign')
        rules[1]['project_id'] = 'foreign-project'
    elif condition == 'grant_excludes_project':
        store.execute('UPDATE grants SET projects=? WHERE id=?', (json.dumps(['project']), b.grant))
    elif condition == 'capability':
        rules[1]['capabilities'] = ['read', 'write']
    elif condition == 'unknown_vps':
        rules[1]['execution_targets'] = ['vps:' + 'f' * 32]
    elif condition in {'unbound_vps', 'cross_space_vps'}:
        response = b.client.post('/api/vps', json={
            'name': 'not-allowed', 'host': 'not-allowed.example.invalid', 'username': 'fixture',
            'password': 'synthetic-membership-test-only',
            'project_ids': ['project'] if condition == 'unbound_vps' else ['otherproject'],
        })
        assert response.is_success, response.text
        vps = response.json()
        if condition == 'cross_space_vps':
            foreign_fixture_space(store)
            copy_fixture_row(store, 'vps_connections', vps['id'], id='c' * 32,
                             space_id='other-space', name='foreign', name_key='foreign',
                             host='foreign.example.invalid')
            vps = {'target': 'vps:' + 'c' * 32}
        rules[1]['execution_targets'] = [vps['target']]
    elif condition == 'grant_revoked':
        store.execute('UPDATE grants SET revoked=1 WHERE id=?', (b.grant,))
    else:
        args['environment_id'] = 'unapproved-environment'
    assert not store.one("SELECT 1 FROM collaboration_rooms WHERE project_id='otherproject'")
    before = saved_state(b)
    changes = store.one('SELECT total_changes() AS n')['n']
    response = b.client.post('/api/collaboration/room-scope', json=args)
    assert response.status_code in {403, 404, 409}, response.text
    assert saved_state(b) == before
    assert store.one('SELECT total_changes() AS n')['n'] == changes
    assert not store.one("SELECT 1 FROM collaboration_rooms WHERE project_id='otherproject'")


def rewrite_fixture_manifest(store, grant_id, update):
    from hub.collaboration.common import canonical
    from hub.room_scope import fingerprint
    row = store.one('SELECT * FROM room_scope_manifests WHERE grant_id=?', (grant_id,))
    manifest = json.loads(row['manifest'])
    update(manifest)
    store.execute('UPDATE room_scope_manifests SET manifest=?,manifest_digest=? WHERE grant_id=?',
                  (canonical(manifest), fingerprint(row, manifest), grant_id))


def test_legacy_ceiling_membership_is_not_relaxed_without_fresh_owner_confirmation(bridge):
    from hub.room_scope import current
    from shared.util import DevError
    b = bridge
    joined = setup_screen(b)
    store = b.app.state.store
    args = ceiling(b, joined, [rule('project'), rule('otherproject')])
    panel(b, 'room-scope', args)
    rewrite_fixture_manifest(store, b.grant, lambda manifest: manifest.pop('project_membership_mode'))
    legacy = store.one('SELECT * FROM room_scope_manifests WHERE grant_id=?', (b.grant,))
    with pytest.raises(DevError) as denied:
        current(store, b.grant)
    assert denied.value.code == 'ROOM_SCOPE_CHANGED'
    assert store.one('SELECT * FROM room_scope_manifests WHERE grant_id=?', (b.grant,)) == legacy
    panel(b, 'room-scope', {**args, 'expected_version': 1, 'idempotency_key': key()})
    assert current(store, b.grant)['manifest']['project_membership_mode'] == 'grant_ceiling'
    assert not store.one("SELECT 1 FROM collaboration_rooms WHERE project_id='otherproject'")


@pytest.mark.parametrize('mode', ['', 'future_mode', True, None])
def test_unknown_ceiling_membership_mode_fails_closed_even_with_valid_digest(bridge, mode):
    from hub.room_scope import current
    from shared.util import DevError
    b = bridge
    joined = setup_screen(b)
    panel(b, 'room-scope', ceiling(b, joined, [rule('project')]))
    rewrite_fixture_manifest(b.app.state.store, b.grant,
                             lambda manifest: manifest.update(project_membership_mode=mode))
    before = saved_state(b)
    with pytest.raises(DevError) as denied:
        current(b.app.state.store, b.grant)
    assert denied.value.code == 'ROOM_SCOPE_INVALID'
    assert saved_state(b) == before


def test_new_ceiling_still_binds_anchor_environment_and_membership(bridge):
    from hub.room_scope import current
    from shared.util import DevError
    b = bridge
    joined = setup_screen(b)
    panel(b, 'room-scope', ceiling(b, joined, [rule('project'), rule('otherproject')]))
    rewrite_fixture_manifest(b.app.state.store, b.grant,
                             lambda manifest: manifest.update(environment_id='staging'))
    with pytest.raises(DevError) as denied:
        current(b.app.state.store, b.grant)
    assert denied.value.code == 'ROOM_SCOPE_CHANGED'


def test_prepared_ceiling_cannot_read_or_subscribe_to_unjoined_conversation(bridge, monkeypatch):
    from tests.test_collaboration_delegation_bridge import SECRET
    b = bridge
    joined = setup_screen(b)
    other_scope = {**b.scope, 'project': 'otherproject'}
    other_room = panel(b, 'room', {**other_scope, 'idempotency_key': key()})['room']
    posted = panel(b, 'message', {
        **other_scope, 'room_id': other_room['id'], 'conversation_id': other_room['id'],
        'body_text': 'private-other-conversation-fixture', 'client_message_id': key(), 'idempotency_key': key(),
    })
    invited = panel(b, 'join-slot', {
        **other_scope, 'label': 'Independent conversation slot', 'kind': 'dot', 'idempotency_key': key(),
    })['slot']
    other_joined = tool(b, 'collaboration', {
        'action': 'join', 'code': invited['join_code'], 'idempotency_key': key(),
    })
    panel(b, 'room-scope', ceiling(b, joined, [rule('project'), rule('otherproject')]))
    store = b.app.state.store
    assert not store.one('SELECT 1 FROM conversation_projects WHERE conversation_id=? AND room_id=?',
                         (joined['slot']['conversation_id'], other_room['id']))
    before = saved_state(b)
    for explicit in (False, True):
        args = {'action': 'timeline', **other_scope}
        if explicit:
            args['conversation_id'] = other_room['id']
        denied = tool(b, 'collaboration_query', args, ok=False)
        assert 'ROOM_SCOPE_CONVERSATION_DENIED' in json.dumps(denied)
        assert posted['message']['body_text'] not in json.dumps(denied)
    calls = []
    async def should_not_send(*args):
        calls.append(args)
        pytest.fail('Unauthorized subscription reached callback verification')
    monkeypatch.setattr(b.app.state.runtime.collaboration.events, 'sender', should_not_send)
    for request in other_joined['subscription_requests']:
        response = rpc(b, 'events/subscribe', {
            **request,
            'delivery': {'mode': 'webhook', 'url': 'https://example.invalid/bridge', 'secret': SECRET},
        })
        assert 'ROOM_SCOPE_CONVERSATION_DENIED' in response.text, response.text
    assert not calls
    assert saved_state(b) == before
    # A separate, explicit working selection can add the partition afterward.
    saved = panel(b, 'conversation-projects', mutation(b, joined, ['otherproject']))
    assert [p['project_id'] for p in saved['conversation']['projects']] == ['otherproject']
    tool(b, 'collaboration_query', {'action': 'dot_connection', **b.scope, 'dot_id': joined['slot']['id']})
    assert store.all('SELECT * FROM grants ORDER BY rowid') == before['grants']
