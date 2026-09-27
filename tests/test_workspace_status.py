"""Task dashboard reads existing scoped records; it does not infer execution."""
import dataclasses
import json
import time
import uuid

import pytest

from hub.mcp_apps import attach
from shared.contracts import TOOLS, tool_definitions
from shared.integration_contracts import APP_ONLY_TOOLS, REMOTE_TOOLS
from shared.coding_contracts import CODING_TOOLS
from shared.util import DevError
from tests.test_agentdock_workflows import env, call, create, update, operation


def dashboard(env, receipt=None, **args):
    return call(env, 'workspace_status', **{'project': 'P',
        'workflow_id': receipt['workflow_id'] if receipt else '', **args})


def result_operation(env, tool, data, **opts):
    identifier = operation(env, finish=False, **opts)
    env[0].store.execute('UPDATE operations SET tool=? WHERE id=?', (tool, identifier))
    data = {**data}
    if tool == 'validation_run': data['validation_id'] = identifier
    env[0].complete({'id': identifier}, {'ok': True, 'data': data})
    return identifier


def test_read_does_not_choose_latest_workflow_or_create_operations(env):
    receipt = create(env)
    recent = operation(env)
    before = env[0].store.one('SELECT count(*) n FROM operations')['n']
    value = dashboard(env)
    assert value['workflow'] is None and value['workflows'][0]['workflow_id'] == receipt['workflow_id']
    assert not value['execution_started'] and not value['evidence']
    assert value['recent_operations'][0]['operation_id'] == recent
    assert env[0].store.one('SELECT count(*) n FROM operations')['n'] == before


def test_task_evidence_and_nearby_work_are_never_merged_and_no_logs_leak(env):
    receipt = create(env)
    linked = operation(env, output='PRIVATE_LOG_MUST_BE_EXPLICITLY_OPENED')
    nearby = operation(env)
    env[0].store.execute('UPDATE operations SET args_summary=? WHERE id=?',
        (json.dumps({'command': 'PRIVATE_COMMAND', 'env': {'SECRET': 'PRIVATE_ENV'}}), linked))
    receipt = update(env, receipt, step_id='s1', step_state='completed', evidence=[linked])
    value = dashboard(env, receipt)
    assert [e['operation_id'] for e in value['evidence']] == [linked]
    assert [e['operation_id'] for e in value['recent_operations']] == [nearby]
    assert 'PRIVATE_' not in json.dumps(value)
    assert value['workflow']['progress']['completed'] == 1


@pytest.mark.parametrize('change', ['grant', 'project', 'read_scope', 'wrong_selected_project'])
def test_dashboard_preserves_original_authorization(env, change):
    receipt = create(env)
    principal = env[1]
    args = {}
    if change == 'grant': principal = dataclasses.replace(principal, grant_id='other')
    if change == 'project': principal = dataclasses.replace(principal, projects=[])
    if change == 'read_scope': principal = dataclasses.replace(principal, scopes={'write'})
    if change == 'wrong_selected_project': args['project'] = 'P2'
    with pytest.raises(DevError): dashboard((env[0], principal), receipt, **args)
    if change == 'grant': assert dashboard((env[0], principal))['workflows'] == []


def test_worktree_filter_is_exact_and_mapping_changes_fence_task_results(env):
    receipt = create(env)
    original, worktree = operation(env), operation(env)
    wid = uuid.uuid4().hex
    env[0].store.execute('UPDATE operations SET args_summary=? WHERE id=?',
                         (json.dumps({'workspace_id': wid}), worktree))
    receipt = update(env, receipt, evidence=[original, worktree])
    first = dashboard(env, receipt)
    assert [e['operation_id'] for e in first['evidence']] == [original]
    assert first['unavailable_evidence'] == 1
    isolated = dashboard(env, receipt, workspace_id=wid)
    assert [e['operation_id'] for e in isolated['evidence']] == [worktree]
    assert isolated['root'] is None
    env[0].store.execute("UPDATE projects SET root='/new-mapping' WHERE id='p'")
    moved = dashboard(env, receipt)
    assert moved['workflow']['mapping_changed'] and moved['evidence'] == []


def test_historical_validation_pass_is_never_fresh_or_accepted(env):
    receipt = create(env)
    passed = result_operation(env, 'validation_run', {'label': 'Test run', 'state': 'passed', 'exit_code': 0})
    failed = result_operation(env, 'validation_run', {'label': 'Failure', 'state': 'failed', 'exit_code': 7})
    receipt = update(env, receipt, evidence=[passed, failed])
    values = {e['operation_id']: e for e in dashboard(env, receipt)['evidence']}
    assert values[passed]['validation']['historical_state'] == 'passed'
    assert values[passed]['validation']['freshness'] == 'not_checked'
    assert 'source_current' not in values[passed]['validation']
    assert values[failed]['state'] == 'failed' and values[failed]['exit_code'] == 7
    assert values[failed]['validation']['historical_state'] == 'failed'


def test_only_successful_saved_review_ref_is_projected(env):
    receipt = create(env)
    reference = uuid.uuid4().hex
    good = result_operation(env, 'show_changes', {'review_ref': reference, 'summary': {'files': 2},
                            'coverage': {'complete': False}, 'diff': 'PRIVATE_SOURCE_DIFF'})
    bad = result_operation(env, 'show_changes', {'review_ref': 'not-an-id', 'summary': {'files': 1}})
    receipt = update(env, receipt, evidence=[good, bad])
    values = {e['operation_id']: e for e in dashboard(env, receipt)['evidence']}
    assert values[good]['review']['review_ref'] == reference
    assert values[good]['review']['coverage']['complete'] is False
    assert 'review' not in values[bad] and 'PRIVATE_SOURCE_DIFF' not in json.dumps(values)


def test_private_computer_evidence_cannot_be_a_read_scope_shortcut(env):
    receipt = create(env)
    op = result_operation(env, 'computer_observe', {'images': ['PRIVATE_MEDIA']})
    # An owner might have saved the reference while it had computer permission.
    principal = dataclasses.replace(env[1], scopes=env[1].scopes | {'computer'})
    receipt = update((env[0], principal), receipt, evidence=[op])
    value = dashboard(env, receipt)
    assert not value['evidence'] and value['unavailable_evidence'] == 1
    assert 'PRIVATE_MEDIA' not in json.dumps(value)


def test_checkpoint_evidence_survives_step_replacement_and_paginates(env):
    receipt = create(env)
    identifiers = [operation(env) for _ in range(6)]
    for identifier in identifiers:
        receipt = update(env, receipt, step_id='s1', step_state='running', evidence=[identifier])
    pages = [dashboard(env, receipt, limit=2, evidence_offset=offset) for offset in (0, 2, 4)]
    assert len({e['operation_id'] for page in pages for e in page['evidence']}) == 6
    assert [p['next_evidence_offset'] for p in pages] == [2, 4, None]
    assert not any(p['history_truncated'] for p in pages)


def test_old_checkpoint_window_is_explicit_and_workflow_list_paginates(env):
    receipt = create(env)
    for _ in range(101): receipt = update(env, receipt)
    assert dashboard(env, receipt)['history_truncated']
    for _ in range(3): create(env)
    first = dashboard(env, limit=2)
    second = dashboard(env, limit=2, workflow_cursor=first['next_workflow_cursor'])
    assert len({w['workflow_id'] for p in (first, second) for w in p['workflows']}) == 4
    assert second['next_workflow_cursor'] is None


def test_artifact_metadata_rechecks_mapping_and_never_exposes_credentials(env):
    receipt = create(env)
    identifier = result_operation(env, 'tasks_run', {'exit_code': 0})
    runtime = env[0]
    runtime.store.execute('UPDATE operations SET tool=?,result=? WHERE id=?',
        ('artifacts_register', json.dumps({'ok': True, 'data': {'artifact_id': identifier}}), identifier))
    now = time.time()
    root = runtime.project('P', env[1])['root']
    runtime.store.execute('INSERT INTO artifacts(id,project_id,device_id,grant_id,actor,root,name,bytes,sha256,created,expires,source_operation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
        (identifier, 'p', 'd', 'g', env[1].actor, root, 'source.zip', 42, 'a' * 64, now, now + 60, ''))
    receipt = update(env, receipt, evidence=[identifier])
    artifact = dashboard(env, receipt)['evidence'][0]['artifact']
    assert artifact['download_path'] == '/api/artifacts/' + identifier + '/download?space_id=legacy'
    assert artifact['immutable'] and not artifact['expired']
    runtime.store.execute('UPDATE artifacts SET expires=? WHERE id=?', (now - 1, identifier))
    assert dashboard(env, receipt)['evidence'][0]['artifact']['expired']
    runtime.store.execute("UPDATE artifacts SET root='/other-root' WHERE id=?", (identifier,))
    assert dashboard(env, receipt)['evidence'][0]['artifact']['unavailable']


def test_distribution_allows_only_reviewed_dashboard_evidence():
    from pathlib import Path
    from scripts.build_source_bundle import include
    prefix = Path('docs/evidence/workspace-dashboard-20260917')
    assert include(prefix/'ACCEPTANCE.md')
    assert include(prefix/'verification-summary.json')
    assert include(prefix/'screenshots/dashboard-390-dark.png')
    for path in (prefix/'raw.log', prefix/'fixture-credentials.json', prefix/'screenshots/private.png',
                 Path('web/fonts/private.woff2'), Path('web/fonts/private.ttf'), Path('.env')):
        assert not include(path)
    for path in ('hub/workspace_status.py', 'web/mcp-apps/dashboard.js', 'web/mcp-apps/ui.js'):
        assert include(Path(path))


def test_app_only_catalog_and_explicit_workflow_widget_binding(env):
    assert TOOLS['workspace_status'].local and TOOLS['workspace_status'].scope == 'read'
    assert 'workspace_status' not in REMOTE_TOOLS
    for profile in ('full', 'coding'):
        tools = {tool['name']: tool for tool in tool_definitions(profile)}
        assert tools['workspace_status']['_meta']['ui']['visibility'] == ['app']
        assert tools['workspace_status']['annotations']['readOnlyHint']
    assert set(t['name'] for t in tool_definitions('coding')) == set(CODING_TOOLS) | APP_ONLY_TOOLS
    receipt = create(env)
    value = call(env, 'workflows_get', workflow_id=receipt['workflow_id'])
    bound = attach({'structuredContent': value}, 'workflows_get', {'workflow_id': receipt['workflow_id']}, value, lambda: 'https://panel.example')
    meta = bound['_meta']['com.codepier/binding']
    assert meta['project'] == 'P' and meta['workflow_id'] == receipt['workflow_id'] and meta['kind'] == 'workspace'
    assert tools['workspace_status']['securitySchemes'][0]['scopes'] == ['read']
