"""Real HTTP/MCP goal admission and project file tools, using isolated fixture identities."""
from tests.collaboration_support import collaboration_stack, key  # noqa: F401


def mcp(s, name, arguments, token=None, *, ok=True):
    result = s.mcp(name, arguments, token_value=token)
    assert bool(result.get('isError')) is not ok, result
    return result.get('structuredContent', result)


def test_owner_goal_approval_csrf_and_real_file_execution(collaboration_stack):
    s = collaboration_stack
    scope = {'project': s.project['id'], 'environment_id': 'production'}
    room = s.must(s.client.post('/api/collaboration/room', json={**scope, 'idempotency_key': key()}))['room']
    reviewer = s.must(s.client.post('/api/grants', json={'label': 'readonly review fixture', 'scopes': ['read'],
                                                       'projects': [s.project['id']], 'days': 1}))
    options = s.must(s.client.get('/api/collaboration', params={**scope, 'conversation_id': room['id'], 'kind': 'coordination_options'}))
    assert options['can_approve'] and s.grant in {row['grant_id'] for row in options['participants']}
    proposal = {**scope, 'conversation_id': room['id'], 'objective': 'Create a fixture file, then review it.',
        'acceptance': 'An actual durable write and read with provenance', 'project_ids': [s.project['id']],
        'participant_grant_ids': [s.grant, reviewer['grant_id']], 'capabilities': ['read', 'write'],
        'idempotency_key': key()}
    goal = mcp(s, 'collaboration_goal_create', proposal)['goal']
    assert goal['state'] == 'proposed'
    approval = {**scope, 'goal_id': goal['id'], 'expected_version': goal['version'], 'digest': goal['digest'], 'idempotency_key': key()}
    assert s.client.post('/api/collaboration/goal-approve', json=approval, headers={'X-RD-CSRF': 'invalid'}).status_code == 403
    mcp(s, 'collaboration_goal_approve', approval, ok=False)
    goal = s.must(s.client.post('/api/collaboration/goal-approve', json=approval))['goal']
    assert goal['state'] == 'active'
    work = mcp(s, 'collaboration_work_create', {**scope, 'goal_id': goal['id'], 'objective': 'Create fixture.txt',
        'acceptance': 'File contains verified fixture', 'target_project_id': s.project['id'],
        'assignee_grant_id': s.grant, 'required_capabilities': ['read', 'write'], 'idempotency_key': key()})['work_item']
    work = mcp(s, 'collaboration_work_claim', {**scope, 'goal_id': goal['id'], 'work_item_id': work['id'],
        'expected_version': work['version'], 'idempotency_key': key()})['work_item']
    lease = {**scope, 'goal_id': goal['id'], 'work_item_id': work['id'], 'attempt': work['attempt'],
             'fencing_token': work['fencing_token']}
    write = mcp(s, 'collaboration_work_execute', {**lease, 'tool': 'write', 'arguments': {'path': 'fixture.txt',
        'expected_sha256': 'new', 'content': 'verified fixture\n'}, 'idempotency_key': key()})
    stored = s.poll(write['operation_id'])
    assert stored['state'] == 'succeeded', stored
    assert (s.projectalpha / 'fixture.txt').read_text() == 'verified fixture\n'
    read = mcp(s, 'collaboration_work_execute', {**lease, 'tool': 'read', 'arguments': {'path': 'fixture.txt'},
                                               'idempotency_key': key()})
    observed = s.poll(read['operation_id'])
    assert observed['state'] == 'succeeded' and observed['result']['data']['content'] == 'verified fixture\n'
    result = mcp(s, 'collaboration_work_result', {**lease, 'outcome': 'succeeded', 'summary': 'Fixture write and read verified',
        'operation_ids': [write['operation_id'], read['operation_id']], 'idempotency_key': key()})['work_item']
    assert result['result']['execution_verified'] and result['provenance_project_ids'] == [s.project['id']]
    review = mcp(s, 'collaboration_work_create', {**scope, 'goal_id': goal['id'], 'objective': 'Review the fixture evidence',
        'acceptance': 'Record limitations', 'target_project_id': s.project['id'], 'assignee_grant_id': reviewer['grant_id'],
        'dependencies': [work['id']], 'required_capabilities': ['read'], 'idempotency_key': key()}, reviewer['token'])['work_item']
    assert review['state'] == 'queued'
    detail = mcp(s, 'collaboration_goal_read', {**scope, 'goal_id': goal['id']}, reviewer['token'])
    assert len(detail['work_items']) == 3 and len(detail['operations']) == 2
    assert not detail['can_approve']
    paused = s.must(s.client.post('/api/collaboration/goal-control', json={**scope, 'goal_id': goal['id'],
        'expected_version': goal['version'], 'action': 'pause', 'idempotency_key': key()}))
    assert paused['goal']['state'] == 'paused'
    mcp(s, 'collaboration_work_claim', {**scope, 'goal_id': goal['id'], 'work_item_id': review['id'],
        'expected_version': review['version'], 'idempotency_key': key()}, reviewer['token'], ok=False)


def test_disabled_feature_does_not_expose_goal_execution(tmp_path, monkeypatch):
    from tests.support import running_stack
    monkeypatch.setenv('CODEPIER_COLLABORATION_ENABLED', 'false')
    with running_stack(tmp_path / 'goals-disabled') as s:
        tools = s.rpc('tools/list').json()['result']['tools']
        assert all(not item['name'].startswith(('collaboration_goal_', 'collaboration_work_')) for item in tools)
        mcp(s, 'collaboration_work_execute', {}, ok=False)
