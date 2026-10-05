"""Read-only entrypoints retain runtime authorization and never launch work."""
import asyncio
import json
import time

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from hub.core_tools import result as core_result
from hub.mcp_apps import attach
from hub.runtime import Runtime, Principal
from hub.store import Store
from shared.contracts import TOOLS, tool_definitions
from shared.crypto import token
from shared.query_contracts import QUERY_TOOLS
from shared.util import DevError
from tests.legacy_iam_fixture import seed_owner, seed_grant


@pytest.fixture
def runtime(tmp_path):
    store = Store(tmp_path / 'hub')
    seed_owner(store, 'owner', 'admin')
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('dev','home',?,?)", (store.encrypt(token()), time.time()))
    for identifier, alias in [('visible', 'Visible'), ('hidden', 'Hidden')]:
        store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,description,mode,allow_tasks,created) VALUES (?,?,?,'dev','/tmp/fixture','','write',1,?)", (identifier, alias, alias.lower(), time.time()))
    seed_grant(store, 'reader', 'owner', scopes=('read',), projects=('visible',))
    principal = Principal('mcp:reader:test', 'owner', {'read'}, ['visible'], grant_id='reader')
    instance = Runtime(store)
    instance.wait_seconds = 0
    yield instance, principal
    store.close()


@pytest.mark.parametrize('profile', ['core', 'full', 'coding'])
def test_entrypoint_metadata_and_conservative_facades(profile):
    definitions = {item['name']: item for item in tool_definitions(profile)}
    assert QUERY_TOOLS <= definitions.keys()
    for name in QUERY_TOOLS:
        item = definitions[name]
        assert item['annotations']['readOnlyHint'] is True
        assert item['annotations']['destructiveHint'] is False
        assert item['annotations']['openWorldHint'] is False
    entry = definitions['workbench']
    assert entry['title'] != 'CodePier'
    assert entry['_meta']['openai/ui']['entrypoints'] == [{'type': 'global'}, {'type': 'thread'}]
    assert entry['_meta']['ui']['resourceUri'] == 'ui://codepier/workspace-v1.html'
    assert entry['_meta']['ui']['visibility'] == ['model', 'app']
    assert entry['_meta'].get('openai/visibility', 'public') == 'public'
    assert entry['_meta']['openai/outputTemplate'] == entry['_meta']['ui']['resourceUri']
    assert entry['securitySchemes'] == [{'type': 'oauth2', 'scopes': ['read']}]
    Draft202012Validator(entry['inputSchema']).validate({})
    for name in ('workspace', 'process', 'browser', 'computer'):
        assert definitions[name]['annotations']['readOnlyHint'] is False
    for name, definition in definitions.items():
        if name != 'workbench':
            assert 'resourceUri' not in definition['_meta'].get('ui', {})
            assert 'openai/outputTemplate' not in definition['_meta']


@pytest.mark.parametrize('name,args', [
    ('workbench', {'project': 'Visible'}),
    ('project_query', {'operation': 'workflow_create'}),
    ('project_query', {'operation': 'project_create'}),
    ('project_query', {'operation': 'open', 'project': 'Visible', 'capture_baseline': True}),
    ('project_query', {'operation': 'open', 'project': 'Visible', 'options': {'capture_baseline': True}}),
    ('task_query', {'operation': 'cancel', 'operation_ids': ['original']}),
    ('task_query', {'operation': 'search_start', 'options': {'query': 'x'}}),
    ('task_query', {'operation': 'validate', 'options': {'command': 'true'}}),
])
def test_query_allowlists_reject_mutations(name, args):
    with pytest.raises(ValidationError):
        TOOLS[name].model.model_validate(args)


@pytest.mark.asyncio
async def test_workbench_filters_projects_and_revalidates_revoked_grants(runtime):
    instance, principal = runtime
    initial = await instance.invoke('workbench', {}, principal)
    assert [p['alias'] for p in initial['projects']] == ['Visible']
    assert not instance.store.all('SELECT id FROM operations')
    queried = await instance.invoke('project_query', {}, principal)
    assert queried == initial
    with pytest.raises(DevError):
        await instance.invoke('project_query', {'operation': 'open', 'project': 'Hidden'}, principal)
    assert not instance.store.all('SELECT id FROM operations')
    instance.store.execute("UPDATE grants SET revoked=1 WHERE id='reader'")
    with pytest.raises(DevError):
        await instance.invoke('workbench', {}, principal)


@pytest.mark.asyncio
async def test_query_keeps_batch_wait_parallel_and_existing_receipts(runtime, monkeypatch):
    instance, principal = runtime
    original = instance.invoke
    started = []
    both = asyncio.Event()
    async def invoke(name, args, user):
        if name == 'operations_wait':
            started.append(args['operation_id'])
            if len(started) == 2:
                both.set()
            await asyncio.wait_for(both.wait(), 1)
            return {'operation_id': args['operation_id'], 'state': 'running', 'pending': True}
        return await original(name, args, user)
    monkeypatch.setattr(instance, 'invoke', invoke)
    args = {'operation': 'wait', 'operation_ids': ['one', 'two']}
    value = await instance.invoke('task_query', args, principal)
    assert started == ['one', 'two']
    rendered = core_result('task_query', args, value)
    assert len(rendered['structuredContent']['operations']) == 2
    assert not instance.store.all('SELECT id FROM operations')


def test_workbench_binding_has_no_invented_project_or_task():
    result = attach({'structuredContent': {'projects': []}}, 'workbench', {},
                    {'projects': []}, lambda: 'https://example.invalid')
    binding = result['_meta']['com.codepier/binding']
    assert binding == {'kind': 'workbench', 'panel_url': 'https://example.invalid'}
    assert 'project' not in json.dumps(binding)
