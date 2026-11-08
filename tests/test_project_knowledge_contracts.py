"""Synthetic knowledge contract, authorization and native routing coverage."""
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator, ValidationError as SchemaError
from pydantic import ValidationError

from agent.resource_queue import BOUNDED_FILE_TOOLS
from hub.admission_policy import READ_TOOLS
from hub.tool_discovery import call_visible, visible_definitions
from hub.tool_router import ToolRouter
from shared.contracts import TOOLS, OUTPUT_SCHEMAS, MUTATING, PROCESS_TOOLS, tool_definitions
from shared.knowledge_contracts import KNOWLEDGE_TOOLS
from shared.tool_protocol import negotiate, require_compatible, validate_call_epoch, wire_version
from shared.util import DevError
from tests.test_core_tools import agent, runtime, reader_principal

BUILD = 'project_knowledge_build'
QUERY = 'project_knowledge_query'
SHA = 'a' * 64
OP = 'b' * 32


def arguments(name, **extra):
    result = {'project': 'Fixture', **extra}
    if name == BUILD:
        result.setdefault('idempotency_key', 'knowledge-fixture-build')
    return result


def validate(name, **extra):
    return TOOLS[name].model.model_validate(arguments(name, **extra)).model_dump()


class Gateway:
    secret = b'knowledge-contract-synthetic-fixture'
    def tools(self, principal):
        return []


@pytest.mark.parametrize('profile', ['core', 'coding', 'full'])
def test_exactly_two_knowledge_tools_are_public_with_honest_annotations(profile):
    catalog = {item['name']: item for item in tool_definitions(profile)}
    assert {name for name in catalog if name.startswith('project_knowledge_')} == KNOWLEDGE_TOOLS
    assert TOOLS[BUILD].scope == 'write' and BUILD in MUTATING
    assert TOOLS[QUERY].scope == 'read' and QUERY not in MUTATING
    assert not KNOWLEDGE_TOOLS & PROCESS_TOOLS
    for name in KNOWLEDGE_TOOLS:
        assert TOOLS[name].local is False
        assert catalog[name]['annotations']['readOnlyHint'] is (name == QUERY)
        assert catalog[name]['annotations']['openWorldHint'] is False
        assert catalog[name]['_meta']['securitySchemes'] == [
            {'type': 'oauth2', 'scopes': ['read', 'write'] if name == BUILD else ['read']}]
        Draft202012Validator.check_schema(catalog[name]['inputSchema'])
        Draft202012Validator.check_schema(catalog[name]['outputSchema'])


def test_defaults_bound_work_and_build_requires_mutation_key():
    build, query = validate(BUILD), validate(QUERY)
    assert build['max_files'] == 2000 and build['entries'] == [] and build['expected_revision'] == ''
    assert (query['offset'], query['limit'], query['max_chars']) == (0, 8, 6000)
    assert query['paths'] == [] and query['query'] == '' and query['idempotency_key'] is None
    with pytest.raises(ValidationError):
        TOOLS[BUILD].model.model_validate({'project': 'Fixture'})
    for name in KNOWLEDGE_TOOLS:
        with pytest.raises(ValidationError):
            validate(name, command='echo must-not-execute')
        with pytest.raises(ValidationError):
            validate(name, workspace_id='../../another-workspace')


@pytest.mark.parametrize('name,field,value', [
    (BUILD, 'max_files', 0), (BUILD, 'max_files', 5001), (BUILD, 'max_files', True),
    (BUILD, 'max_files', '20'), (BUILD, 'expected_revision', 'A' * 64),
    (BUILD, 'expected_revision', '../index'), (BUILD, 'entries', [{}] * 33),
    (QUERY, 'query', 'a' * 301), (QUERY, 'paths', ['file.py'] * 17),
    (QUERY, 'offset', -1), (QUERY, 'offset', 5001), (QUERY, 'limit', 0),
    (QUERY, 'limit', 41), (QUERY, 'max_chars', 511), (QUERY, 'max_chars', 16001),
])
def test_bounds_and_strict_types(name, field, value):
    with pytest.raises(ValidationError):
        validate(name, **{field: value})


@pytest.mark.parametrize('path', ['/etc/passwd', '../outside.py', 'src/../x.py', 'C:/x.py',
    'src\\file.py', 'src//file.py', './file.py', '~/.ssh/config', 'bad\x00name', 'bad\nname', '.'])
def test_paths_cannot_escape_or_ambiguously_name_project_content(path):
    with pytest.raises(ValidationError):
        validate(QUERY, paths=[path])
    with pytest.raises(ValidationError):
        validate(BUILD, entries=[{'path': path, 'expected_sha256': SHA, 'summary': 'fixture'}])
    with pytest.raises(ValidationError):
        validate(BUILD, entries=[{'path': 'src/file.py', 'expected_sha256': SHA,
                                 'summary': 'fixture', 'related_paths': [path]}])


def test_summary_requires_source_hash_and_entries_are_distinct():
    entry = {'path': 'src/项目.py', 'expected_sha256': SHA, 'summary': 'Synthetic summary',
             'related_paths': ['tests/test_app.py']}
    assert validate(BUILD, entries=[entry])['entries'] == [entry]
    for change in ({'expected_sha256': 'new'}, {'summary': 'x' * 1201},
                   {'related_paths': ['x'] * 17}, {'related_paths': ['x', 'x']}):
        with pytest.raises(ValidationError):
            validate(BUILD, entries=[{**entry, **change}])
    with pytest.raises(ValidationError):
        validate(BUILD, entries=[entry, entry])
    with pytest.raises(ValidationError):
        validate(QUERY, paths=['file.py', 'file.py'])


def test_read_discovery_hides_build_without_changing_routing_authority(runtime):
    instance, _ = runtime
    reader = reader_principal(instance)
    router = ToolRouter(instance.store, Gateway())
    names = {item['name'] for item in router.list_tools(reader)['tools']}
    assert QUERY in names and BUILD not in names
    assert call_visible(BUILD, arguments(BUILD), {'read'}) is False
    assert call_visible(QUERY, arguments(QUERY), {'read'}) is True
    assert not {item['name'] for item in visible_definitions(tool_definitions(), set())} & KNOWLEDGE_TOOLS
    # Reserved native dispatch still performs the ordinary current-principal check.
    assert router.resolve(reader, BUILD).backend == 'native'
    assert router.resolve(reader, QUERY).backend == 'native'


@pytest.mark.asyncio
async def test_direct_build_cannot_bypass_read_only_grant(runtime):
    instance, _ = runtime
    reader = reader_principal(instance)
    with pytest.raises(DevError) as denied:
        await instance.invoke(BUILD, arguments(BUILD), reader)
    assert denied.value.code == 'INSUFFICIENT_SCOPE'
    assert denied.value.details['required_scope'] == 'write'
    assert not instance.store.one("SELECT id FROM operations WHERE tool=?", (BUILD,))


@pytest.mark.asyncio
@pytest.mark.parametrize('name', [BUILD, QUERY])
async def test_hub_routes_exact_typed_tool_without_shell_or_gateway(runtime, monkeypatch, name):
    instance, principal = runtime
    captured = []
    async def dispatch(tool, args, project, caller, **options):
        captured.append((tool, args, project, options))
        return {'operation_id': OP, 'pending': True, 'state': 'queued'}
    monkeypatch.setattr(instance, 'dispatch', dispatch)
    result = await instance.invoke(name, arguments(name), principal)
    assert result['operation_id'] == OP
    assert len(captured) == 1
    tool, args, project, options = captured[0]
    assert tool == name and project['id'] == 'proj'
    assert args['project'] == 'Fixture'
    assert options['background'] is False
    assert 'command' not in args and 'target' not in args


@pytest.mark.asyncio
@pytest.mark.parametrize('name', [BUILD, QUERY])
async def test_agent_routes_to_typed_file_engine_not_process(agent, monkeypatch, name):
    instance, project, _ = agent
    captured = []
    def call(tool, current, args):
        captured.append((tool, current, args))
        return {'synthetic': True}
    monkeypatch.setattr(instance.engine, 'call', call)
    from agent import workspace_probe
    async def probe(engine, tool, current, args, **options):
        assert engine is instance.engine
        return call(tool, current, args)
    monkeypatch.setattr(workspace_probe, 'probe', probe)
    async def no_process(*args, **kwargs):
        raise AssertionError('Knowledge must not execute a command')
    monkeypatch.setattr(instance, 'run_process', no_process)
    assert await instance.execute(OP, name, project, validate(name)) == {'synthetic': True}
    assert captured[0][0] == name and captured[0][1] == project


@pytest.mark.parametrize('name', [BUILD, QUERY])
def test_legacy_or_unadvertised_peers_fail_closed(name):
    assert wire_version(name) == 2
    with pytest.raises(DevError) as denied:
        require_compatible(negotiate({'version': '1.13.0'}), name)
    assert denied.value.code == 'AGENT_UPGRADE_REQUIRED'
    with pytest.raises(DevError):
        validate_call_epoch(name, None)
    validate_call_epoch(name, 2)


def test_query_uses_read_lane_and_build_retains_write_admission():
    assert QUERY in BOUNDED_FILE_TOOLS and QUERY in READ_TOOLS
    assert BUILD not in BOUNDED_FILE_TOOLS and BUILD not in READ_TOOLS


@pytest.mark.parametrize('name,value', [
    (BUILD, {'operation_id': OP, 'state': 'ready', 'revision': SHA,
             'entries_count': 1, 'truncated': False}),
    (QUERY, {'operation_id': OP, 'state': 'missing', 'revision': 'new', 'entries': [],
             'total_matches': 0, 'offset': 0, 'truncated': False,
             'next_offset': None, 'refresh_required': True}),
    (QUERY, {'operation_id': OP, 'state': 'ready', 'revision': SHA,
             'entries': [{'path': 'app.py', 'sha256': SHA, 'state': 'stale'}],
             'total_matches': 1, 'offset': 0, 'truncated': False,
             'next_offset': None, 'refresh_required': True}),
])
def test_typed_results_errors_and_pending_receipts(name, value):
    schema = OUTPUT_SCHEMAS[name]
    validator = Draft202012Validator(schema)
    validator.validate(value)
    validator.validate({'error': {'code': 'KNOWLEDGE_CONFLICT', 'message': 'Refresh required'}})
    validator.validate({'operation_id': OP, 'pending': True, 'state': 'queued'})
    for invalid in ({}, {**value, 'state': 123}, {'error': {'code': 'NO_MESSAGE'}}):
        with pytest.raises(SchemaError):
            validator.validate(invalid)
    copied = deepcopy(value)
    copied['truncated'] = 'false'
    with pytest.raises(SchemaError):
        validator.validate(copied)


def test_actual_file_engine_results_obey_public_output_contract(agent):
    instance, project, root = agent
    (root / 'app.py').write_text('def fixture_entry():\n    return 1\n', encoding='utf-8')
    for name in (QUERY, BUILD, QUERY):
        outcome = instance.engine.call(name, project, validate(name))
        Draft202012Validator(OUTPUT_SCHEMAS[name]).validate({'operation_id': OP, **outcome})
    (root / 'app.py').write_text('def changed_entry():\n    return 2\n', encoding='utf-8')
    outcome = instance.engine.call(QUERY, project, validate(QUERY))
    Draft202012Validator(OUTPUT_SCHEMAS[QUERY]).validate({'operation_id': OP, **outcome})
    assert outcome['entries'][0]['state'] == 'stale'
    assert 'summary' not in outcome['entries'][0]


def test_public_source_bundle_contains_knowledge_code_and_guide():
    from pathlib import Path
    from scripts.build_source_bundle import include, REQUIRED_FILES, PUBLIC_DOCS
    required = {'agent/project_knowledge.py', 'agent/native_project_knowledge.py',
                'shared/knowledge_contracts.py', 'docs/PROJECT_KNOWLEDGE.md'}
    assert required <= REQUIRED_FILES
    assert 'docs/PROJECT_KNOWLEDGE.md' in PUBLIC_DOCS
    assert all(include(Path(path), public=True) for path in required)
