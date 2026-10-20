"""Project-view foundations only. Full MCP projection acceptance is separate."""
from copy import deepcopy
from types import SimpleNamespace
import json
import pytest
from pydantic import ValidationError
from jsonschema import Draft202012Validator
from tests.test_agentdock_context import workspace
from shared.contracts import TOOLS
from shared.project_view_contracts import ACTION_READINESS
from shared.util import DevError
from agent.action_readiness import action_readiness
from agent.integration_control import Controls
from hub.project_views import enrich_readiness, list_projects, prepare_view_packet


def open_project(workspace, **kwargs):
    engine, project, _ = workspace
    project = {**project, '_coding_owner': 'grant:test', '_coding_device': 'node',
               '_coding_scopes': ['read', 'write']}
    args = TOOLS['open_workspace'].model.model_validate({'project': 'P', **kwargs}).model_dump()
    return engine.call('open_workspace', project, args)


def test_minimal_rule_index_has_no_previews_or_environment_probe(workspace, monkeypatch):
    _, _, root = workspace
    for name in ('AGENTS.md', 'MEMORY.md', 'CLAUDE.md', 'README.md'):
        (root / name).write_text('PRIVATE_DOCUMENT_BODY_DO_NOT_AUTOPREVIEW')
    def unexpected(*args):
        raise AssertionError('default open must not probe execution')
    monkeypatch.setattr('agent.context.execution_info', unexpected)
    result = open_project(workspace, max_files=1)
    assert result['context_detail'] == 'minimal' and result['view_version'] == 1
    context = result['context']
    assert 'PRIVATE_DOCUMENT_BODY' not in json.dumps(result)
    assert 'execution' not in context and context['budget']['preview_chars'] == 0
    assert [item['path'] for item in context['instruction_index']] == ['AGENTS.md', 'MEMORY.md', 'CLAUDE.md']
    assert all(len(item['sha256']) == 64 and item['must_read_before_edit'] for item in context['instruction_index'])
    assert 'README.md' in context['remaining_documents']


def test_explicit_documents_retains_preview_but_does_not_probe_execution(workspace, monkeypatch):
    _, _, root = workspace
    (root / 'MEMORY.md').write_text('Applicable memory rules')
    def unexpected(*args):
        raise AssertionError('documents view must not probe execution')
    monkeypatch.setattr('agent.context.execution_info', unexpected)
    result = open_project(workspace, context_detail='documents')
    assert result['context']['documents'][0]['content'] == 'Applicable memory rules'
    assert 'execution' not in result['context']


def test_minimal_index_rejects_symlink_and_marks_absence(workspace, tmp_path):
    _, _, root = workspace
    outside = tmp_path / 'outside.txt'
    outside.write_text('DO_NOT_LEAK')
    (root / 'AGENTS.md').symlink_to(outside)
    result = open_project(workspace)['context']
    assert 'DO_NOT_LEAK' not in json.dumps(result)
    states = {item['path']: item['state'] for item in result['instruction_index']}
    assert states['AGENTS.md'] == 'unavailable' and states['MEMORY.md'] == 'absent'
    assert result['warnings'] and result['truncated']


def test_context_detail_and_memory_changes_invalidate_payload_reuse(workspace):
    _, _, root = workspace
    (root / 'MEMORY.md').write_text('one')
    minimal = open_project(workspace)
    assert open_project(workspace, context_id=minimal['context_id'])['context_unchanged']
    documents = open_project(workspace, context_detail='documents', context_id=minimal['context_id'])
    assert not documents['context_unchanged']
    (root / 'MEMORY.md').write_text('two')
    assert not open_project(workspace, context_id=minimal['context_id'])['context_unchanged']


def local(workspace, kind, scopes=('read', 'write', 'execute'), paused=False):
    engine, project, _ = workspace
    agent = SimpleNamespace(engine=engine, config=engine.config, journal=engine.journal)
    control = Controls(agent)
    if paused:
        control.set(project, True)
    return action_readiness(SimpleNamespace(agent=agent, control=control),
        {**project, '_coding_device': 'node', '_coding_scopes': list(scopes)},
        {'operation_kind': kind, 'workspace_id': ''})


@pytest.mark.parametrize('kind', ['file_read', 'file_write'])
def test_file_actions_are_independent_of_disabled_exec_and_computer(workspace, monkeypatch, kind):
    engine, project, _ = workspace
    project['allow_tasks'] = False
    engine.config['allowed_roots'][0]['allow_tasks'] = False
    engine.config['shell'] = {'enabled': False}
    engine.config['mcp_policy'] = {'block_local_codex': True, 'block_native_computer': True}
    def unexpected(*args, **kwargs):
        raise AssertionError('file readiness must not search executable paths')
    monkeypatch.setattr('agent.project_runtime.shutil.which', unexpected)
    result = local(workspace, kind, scopes=('read', 'write'))
    assert result['local_authorization']['state'] == 'allowed'
    assert result['execution_readiness']['state'] == 'ready'
    assert result['caller_platform_decision'] == {'state': 'not_observed', 'source': None, 'observed_at': None}
    assert result['operation_evidence'] is None
    Draft202012Validator(ACTION_READINESS).validate(result)


def test_command_requires_execution_opt_in(workspace):
    workspace[1]['allow_tasks'] = False
    result = local(workspace, 'command_exec')
    assert result['local_authorization']['state'] == 'denied'
    assert result['local_authorization']['code'] == 'TASKS_DISABLED'


def test_read_only_root_and_pause_use_existing_local_gates(workspace):
    workspace[0].config['allowed_roots'][0]['writable'] = False
    assert local(workspace, 'file_write')['local_authorization']['code'] == 'READ_ONLY'
    workspace[0].config['allowed_roots'][0]['writable'] = True
    assert local(workspace, 'file_write', paused=True)['local_authorization']['code'] == 'REMOTE_PAUSED'
    assert local(workspace, 'file_read')['local_authorization']['state'] == 'allowed'


@pytest.mark.parametrize('payload', [
    {'query': 1}, {'limit': True}, {'limit': 0}, {'limit': 101},
    {'offset': -1}, {'offset': 1.5}, {'device_id': 'hidden'},
])
def test_project_search_rejects_invalid_and_unknown_arguments(payload):
    with pytest.raises(ValidationError):
        TOOLS['projects_list'].model.model_validate(payload)


@pytest.mark.parametrize('payload', [
    {'operation_kind': 'write_anything'}, {'operation_kind': 1},
    {'operation_kind': 'file_write', 'caller_platform_decision': 'approved'},
])
def test_readiness_rejects_invented_action_and_approval_fields(payload):
    with pytest.raises(ValidationError):
        TOOLS['readiness_get'].model.model_validate({'project': 'P', **payload})


@pytest.fixture
def hub_fixture(workspace, monkeypatch):
    _, project, _ = workspace
    project = {**project, 'device_id': 'node'}
    principal = SimpleNamespace(scopes={'read', 'write', 'execute'}, grant_id=None,
        projects=['p'], profile_id=None, role_id=None, user_epoch=1)
    monkeypatch.setattr('hub.project_views.refresh_principal', lambda store, who: who)
    monkeypatch.setattr('hub.room_scope.require_tool', lambda *args: None)
    def authorize(who, action, **kwargs):
        if action not in who.scopes:
            raise DevError('INSUFFICIENT_SCOPE', 'denied', 403)
    runtime = SimpleNamespace(store=object(), project=lambda value, who: project,
        authorize=authorize, online=lambda node: True,
        integrations=SimpleNamespace(guard=lambda *args: None))
    return runtime, principal, project


def test_hub_rechecks_revocation_on_original_observation(workspace, hub_fixture):
    runtime, principal, _ = hub_fixture
    snapshot = local(workspace, 'file_write')
    before = deepcopy(snapshot)
    args = {'project': 'P', 'operation_kind': 'file_write'}
    allowed = enrich_readiness(runtime, args, snapshot, principal)
    assert allowed['codepier_authorization']['state'] == 'allowed'
    principal.scopes = {'read'}
    denied = enrich_readiness(runtime, args, snapshot, principal)
    assert denied['codepier_authorization']['state'] == 'denied'
    assert denied['codepier_authorization']['code'] == 'INSUFFICIENT_SCOPE'
    assert snapshot == before and denied['operation_evidence'] is None
    Draft202012Validator(ACTION_READINESS).validate(denied)


def test_mapping_changes_and_unknown_agent_are_not_false_approval(workspace, hub_fixture):
    runtime, principal, project = hub_fixture
    snapshot = local(workspace, 'file_read')
    project['root'] += '-moved'
    args = {'project': 'P', 'operation_kind': 'file_read'}
    changed = enrich_readiness(runtime, args, snapshot, principal)
    assert changed['codepier_authorization']['state'] == 'unknown'
    unknown = enrich_readiness(runtime, args, {'compatibility': 'AGENT_VIEW_UPGRADE_REQUIRED',
        'caller_platform_decision': {'state': 'approved'}}, principal)
    assert unknown['codepier_authorization']['state'] == 'unknown'
    assert unknown['caller_platform_decision']['state'] == 'not_observed'
    Draft202012Validator(ACTION_READINESS).validate(unknown)


def test_authorization_precedes_search_pagination_and_counts(hub_fixture):
    runtime, principal, _ = hub_fixture
    rows = [dict(id=i, alias=name, description='', device_id='node', online=True) for i, name in
            [('a', 'MCP A'), ('hidden', 'MCP private'), ('b', 'mcp Z')]]
    runtime.iter_project_rows = lambda caller: iter(rows)
    runtime.project_public = lambda row, **kwargs: row
    def authorize(caller, action, **kwargs):
        if kwargs['project_id'] == 'hidden':
            raise DevError('ROLE_POLICY_DENIED', 'denied', 403)
    runtime.authorize = authorize
    first = list_projects(runtime, {'query': 'mCp', 'limit': 1, 'offset': 0}, principal)
    assert first['total'] == 2 and first['next_offset'] == 1
    assert [row['id'] for row in first['projects']] == ['a']
    second = list_projects(runtime, {'query': 'mCp', 'limit': 1, 'offset': 1}, principal)
    assert [row['id'] for row in second['projects']] == ['b'] and second['next_offset'] is None
    assert 'hidden' not in json.dumps(first) + json.dumps(second)


@pytest.mark.parametrize('version', [0, 1])
def test_view_negotiation_changes_only_outgoing_packet(hub_fixture, version):
    runtime, principal, _ = hub_fixture
    original = {'project': 'P', 'context_detail': 'documents'}
    packet = {'tool': 'open_workspace', 'args': original, 'integration_context': {}}
    prepare_view_packet(runtime, packet, principal, SimpleNamespace(project_view_protocol=version))
    assert original == {'project': 'P', 'context_detail': 'documents'}
    assert ('context_detail' in packet['args']) is (version == 1)
    assert len(packet['integration_context']['authority_revision']) == 64
