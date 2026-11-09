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
    assert build['max_files'] == 0 and build['entries'] == [] and build['expected_revision'] == ''
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
    (BUILD, 'max_files', -1), (BUILD, 'max_files', 100001), (BUILD, 'max_files', True),
    (BUILD, 'max_files', '20'), (BUILD, 'expected_revision', 'A' * 64),
    (BUILD, 'expected_revision', '../index'), (BUILD, 'entries', [{}] * 33),
    (QUERY, 'query', 'a' * 301), (QUERY, 'paths', ['file.py'] * 17),
    (QUERY, 'offset', -1), (BUILD, 'batch_size', 5001), (QUERY, 'limit', 0),
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
    names = {item['name'] for item in router.list_tools(reader, profile='core')['tools']}
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
    assert wire_version(name) == (4 if name == BUILD else 2)
    with pytest.raises(DevError) as denied:
        require_compatible(negotiate({'version': '1.13.0'}), name)
    assert denied.value.code == 'AGENT_UPGRADE_REQUIRED'
    with pytest.raises(DevError):
        validate_call_epoch(name, None)
    validate_call_epoch(name, wire_version(name))


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


@pytest.mark.asyncio
@pytest.mark.parametrize('unified,name,view', [('query', QUERY, 'query'), ('act', BUILD, 'build')])
async def test_unified_knowledge_mapping_help_and_runtime(runtime, monkeypatch, unified, name, view):
    from hub import unified_tools as ut
    from shared import unified_contracts as u
    instance, principal = runtime
    raw = {'action': 'knowledge', 'project': 'Fixture', 'wait': 0}
    if name == BUILD:
        raw['idempotency_key'] = 'unified-knowledge-build'
        raw['args'] = {'max_files': 7}
    else:
        raw['args'] = {'query': 'synthetic', 'paths': ['app.py']}
    plan = await ut.prepare(instance, unified, raw, principal)
    assert plan.tool == name and plan.view == view
    assert 'operation' not in plan.args and 'options' not in plan.args
    TOOLS[name].model.model_validate(plan.args)
    assert u.unified_hint(name, plan.args) == {
        'name': unified, 'arguments': {k: v for k, v in raw.items() if k != 'wait'}}
    help_plan = await ut.prepare(instance, 'query', {
        'action': 'help', 'args': {'action': 'knowledge', 'view': view}}, principal)
    help_value, _, _ = await ut.execute(instance, help_plan, principal)
    assert 'inputSchema' not in help_value
    assert help_value['tool'] == unified and help_value['view'] == view
    assert u.translate(unified, help_value['example']).tool == name
    detailed = await ut.prepare(instance, 'query', {'action': 'help', 'args': {
        'tool': unified, 'action': 'knowledge', 'view': view, 'detail': 'schema'}}, principal)
    schema, _, _ = await ut.execute(instance, detailed, principal)
    assert set(schema['inputSchema']['required']) >= {'action', 'project'}
    assert 'outputSchema' in schema
    captured = []
    async def dispatch(tool, args, project, caller, **options):
        captured.append((tool, args, project))
        return {'operation_id': OP, 'pending': True, 'state': 'queued'}
    monkeypatch.setattr(instance, 'dispatch', dispatch)
    value, routed, _ = await ut.execute(instance, plan, principal)
    assert routed == name and value['operation_id'] == OP
    assert len(captured) == 1 and captured[0][0] == name
    assert captured[0][2]['id'] == 'proj'


@pytest.mark.asyncio
async def test_unified_read_scope_cannot_build_or_discover_build(runtime):
    from hub import unified_tools as ut
    instance, _ = runtime
    reader = reader_principal(instance)
    catalog = ut.catalog(reader.scopes)
    assert catalog['tools']['query']['knowledge'] == ['query']
    assert 'knowledge' not in catalog['tools']['act']
    plan = await ut.prepare(instance, 'act', {'action': 'knowledge', 'project': 'Fixture',
        'idempotency_key': 'unified-denied-build', 'wait': 0}, reader)
    with pytest.raises(DevError) as denied:
        await ut.execute(instance, plan, reader)
    assert denied.value.code == 'INSUFFICIENT_SCOPE'
    assert denied.value.details['required_scope'] == 'write'
    with pytest.raises(DevError) as denied_help:
        await ut.prepare(instance, 'query', {'action': 'help',
            'args': {'action': 'knowledge', 'view': 'build'}}, reader)
    assert denied_help.value.code == 'INSUFFICIENT_SCOPE'
    assert not instance.store.one("SELECT id FROM operations WHERE tool=?", (BUILD,))



def _expanded_schema(schema):
    """Resolve local refs and retain every validation keyword for comparison."""
    def expand(node, active=()):
        if isinstance(node, list):
            return [expand(value, active) for value in node]
        if not isinstance(node, dict):
            return node
        if '$ref' in node:
            reference = node['$ref']
            assert reference.startswith('#/$defs/'), reference
            assert reference not in active, reference
            name = reference[len('#/$defs/'):].replace('~1', '/').replace('~0', '~')
            target = expand(schema['$defs'][name], (*active, reference))
            siblings = {key: expand(value, active) for key, value in node.items() if key != '$ref'}
            if not siblings:
                return target
            assert isinstance(target, dict)
            assert set(siblings) <= {'title', 'description', 'deprecated', 'readOnly', 'writeOnly'}
            return {**target, **siblings}
        return {key: expand(value, active) for key, value in node.items() if key != '$defs'}
    return expand(schema)


def _validation_fingerprint(schema):
    """Normalize only annotation/default syntax, never field constraints."""
    import json
    def normalize(node):
        if isinstance(node, list):
            return [normalize(value) for value in node]
        if not isinstance(node, dict):
            return node
        result = deepcopy(node)
        result.pop('title', None)
        result.pop('description', None)
        for field in ('properties', 'patternProperties', '$defs'):
            if field in node:
                result[field] = {key: normalize(value) for key, value in node[field].items()}
        for field in ('items', 'additionalProperties', 'not'):
            if isinstance(node.get(field), dict):
                result[field] = normalize(node[field])
        for field in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
            if field in node:
                result[field] = [normalize(value) for value in node[field]]
        if result.get('additionalProperties') is True:
            result.pop('additionalProperties')
        if result.get('required') == []:
            result.pop('required')
        # Independently expand type-array shorthand into the verbose union;
        # do not call factor or reproduce its constraint-hoisting algorithm.
        types = result.get('type')
        if isinstance(types, list) and types and 'anyOf' not in result:
            constraints = {}
            if set(types) == {'string', 'null'}:
                for keyword in ('minLength', 'maxLength', 'pattern'):
                    if keyword in result:
                        constraints[keyword] = result.pop(keyword)
            result.pop('type')
            result['anyOf'] = [
                {'type': kind, **(constraints if kind == 'string' else {})}
                for kind in types]
        return result
    return json.dumps(normalize(_expanded_schema(schema)), sort_keys=True, separators=(',', ':'))


def test_catalog_input_references_preserve_complete_model_constraints():
    for item in tool_definitions():
        original = TOOLS[item['name']].model.model_json_schema()
        advertised = item['inputSchema']
        assert _validation_fingerprint(original) == _validation_fingerprint(advertised), item['name']
        Draft202012Validator.check_schema(advertised)
        Draft202012Validator.check_schema(_expanded_schema(item['outputSchema']))


def test_lean_schema_preserves_types_and_additive_fields():
    from shared.contracts import LEAN_OUTPUT_SCHEMA
    original = {'type': 'object', 'additionalProperties': True,
        'properties': {'operation_id': {'type': 'string'}, 'pending': {'type': 'boolean'},
            'state': {'type': 'string'},
            'error': {'anyOf': [{'type': 'object'}, {'type': 'string'}, {'type': 'null'}]},
            'truncated': {'type': 'boolean'},
            'next_offset': {'anyOf': [{'type': 'integer'}, {'type': 'null'}]}}}
    before, after = Draft202012Validator(original), Draft202012Validator(LEAN_OUTPUT_SCHEMA)
    assert set(LEAN_OUTPUT_SCHEMA['properties']) == set(original['properties'])
    for field in original['properties']:
        for value in (None, True, False, -1, 0, 1.5, '', 'synthetic', [], {}, {'code': 'example'}):
            sample = {field: value, 'additive_future_field': {'nested': [True, None]}}
            assert before.is_valid(sample) == after.is_valid(sample)
    for value in ({}, [], None, False, 1):
        assert before.is_valid(value) == after.is_valid(value)
    after.validate({'additive_future_field': 'preserved'})


def test_catalog_output_factoring_matches_original_validators():
    from shared.mcp_presentation import output_schema
    from tests.test_core_output_schemas import SAMPLES, PENDING, ERROR
    candidates = [None, False, 0, '', [], {}, PENDING, ERROR, *SAMPLES.values(),
        {'operation_id': OP, 'state': 'ready', 'revision': SHA,
         'entries_count': 1, 'truncated': False},
        {'operation_id': OP, 'state': 'missing', 'revision': 'new', 'entries': [],
         'total_matches': 0, 'offset': 0, 'truncated': False,
         'next_offset': None, 'refresh_required': True}]
    for item in tool_definitions():
        original = output_schema(item['name'], OUTPUT_SCHEMAS[item['name']])
        before, after = Draft202012Validator(original), Draft202012Validator(item['outputSchema'])
        values = list(candidates)
        for value in candidates:
            if isinstance(value, dict) and before.is_valid(value):
                for field in value:
                    values.append({key: part for key, part in value.items() if key != field})
                    values.extend({**value, field: wrong} for wrong in (None, [], {}, True, 0, 'wrong'))
        for value in values:
            assert before.is_valid(value) == after.is_valid(value), (item['name'], value)


@pytest.mark.parametrize('keyword', ['const', 'enum'])
@pytest.mark.parametrize('left,right', [
    (True, 1), (False, 0), ([True], [1]), ({'nested': True}, {'nested': 1}),
])
def test_factor_does_not_confuse_booleans_with_numbers(keyword, left, right):
    from shared.core_output_schemas import factor
    a, b = ([left], [right]) if keyword == 'enum' else (left, right)
    schema = {'type': 'object', 'properties': {'x': {keyword: a}},
              'anyOf': [{'properties': {'x': {keyword: b}}}]}
    compact = factor(schema)
    for value in ({}, {'x': left}, {'x': right}, {'x': None}):
        assert Draft202012Validator(schema).is_valid(value) == Draft202012Validator(compact).is_valid(value)
    assert not Draft202012Validator(compact).is_valid({'x': left})


@pytest.mark.parametrize('additional', [False, {'type': 'integer'}])
def test_factor_preserves_branch_local_additional_properties(additional):
    from shared.core_output_schemas import factor
    schema = {'type': 'object', 'properties': {'x': {'type': 'string'}},
              'anyOf': [{'properties': {'x': {'type': 'string'}},
                         'additionalProperties': additional}]}
    compact = factor(schema)
    for value in ({}, {'x': 'ok'}, {'x': 1}, {'x': 'ok', 'extra': 1}, {'extra': 'bad'}):
        assert Draft202012Validator(schema).is_valid(value) == Draft202012Validator(compact).is_valid(value)
    Draft202012Validator(compact).validate({'x': 'ok'})


@pytest.mark.parametrize('schema', [
    {'$id': 'https://example.invalid/schema', 'type': 'object'},
    {'$defs': {'s': {'$anchor': 'entry', 'type': 'string'}}, '$ref': '#entry'},
    {'$dynamicAnchor': 'entry', '$dynamicRef': '#entry'},
    {'$ref': 'https://example.invalid/no-network'},
    {'$defs': {'s': {'$ref': '#/$defs/s'}}, '$ref': '#/$defs/s'},
    {'$defs': {'a': {'$ref': '#/$defs/b'}, 'b': {'$ref': '#/$defs/a'}}, '$ref': '#/$defs/a'},
    {'$defs': {'s': {'type': 'string'}}, 'if': True, 'then': {'$ref': '#/$defs/s'}},
    {'$defs': {'s': {'type': 'string'}}, 'contains': {'$ref': '#/$defs/s'}},
    {'$defs': {'s': {'type': 'string'}}, 'propertyNames': {'$ref': '#/$defs/s'}},
    {'$defs': {'s': {'type': 'string'}}, 'dependentSchemas': {'x': {'$ref': '#/$defs/s'}}},
    {'$defs': {'s': {'type': 'string'}}, 'contentSchema': {'$ref': '#/$defs/s'}},
    {'type': 'object', 'additionalProperties': True, 'unevaluatedProperties': False},
    {'type': 'object', 'anyOf': [{'additionalProperties': True}], 'unevaluatedProperties': False},
    {'prefixItems': [{'type': 'integer'}], 'unevaluatedItems': False},
])
def test_factor_conservatively_preserves_scope_and_unhandled_applicators(schema):
    from shared.core_output_schemas import factor
    before = deepcopy(schema)
    assert factor(schema) == before
    assert schema == before


@pytest.mark.parametrize('definition', [False, True])
def test_factor_handles_boolean_definitions_without_inlining_errors(definition):
    from shared.core_output_schemas import factor
    schema = {'$defs': {'s': definition}, 'type': 'object',
              'properties': {'x': {'$ref': '#/$defs/s'}}}
    compact = factor(schema)
    for value in ({}, {'x': 1}, {'x': None}, []):
        assert Draft202012Validator(schema).is_valid(value) == Draft202012Validator(compact).is_valid(value)


def test_required_factoring_preserves_missing_fields_and_oneof_multiplicity():
    from itertools import combinations
    from shared.core_output_schemas import factor
    fields = ['operation_id', 'long_required_field', 'a', 'b', 'c']
    branches = [{'required': fields[:2] + [name]} for name in fields[2:]]
    for keyword in ('anyOf', 'oneOf'):
        schema = {'type': 'object', keyword: [*branches, deepcopy(branches[0])]}
        before = deepcopy(schema)
        compact = factor(schema)
        assert schema == before
        assert factor(schema) == compact
        for count in range(len(fields) + 1):
            for selected in combinations(fields, count):
                value = dict.fromkeys(selected, 'synthetic')
                assert Draft202012Validator(schema).is_valid(value) == Draft202012Validator(compact).is_valid(value)
        if keyword == 'oneOf':
            assert len(compact['oneOf']) == len(branches) + 1
            assert not Draft202012Validator(compact).is_valid(dict.fromkeys(fields[:3], 'synthetic'))


def test_factor_keeps_literal_json_data_and_reference_sibling_constraints():
    from shared.core_output_schemas import factor
    literal = {'additionalProperties': True, 'required': []}
    schema = {'$defs': {'s': {'type': 'integer'}},
              'type': 'object', 'properties': {
                  'data': {'const': literal},
                  'number': {'$ref': '#/$defs/s', 'minimum': 2}},
              'required': ['data', 'number']}
    compact = factor(schema)
    for value in ({'data': literal, 'number': 2}, {'data': literal, 'number': 1},
                  {'data': {}, 'number': 2}, {'data': literal, 'number': True}):
        assert Draft202012Validator(schema).is_valid(value) == Draft202012Validator(compact).is_valid(value)


_UNION_VALUES = (None, '', 'a', 'ab', 'abc', 'abcd', 'abcde', 'ZZ', '\n',
                 True, False, 0, 1, 1.5, [], ['ab'], {}, {'value': 'ab'})


def _assert_union_factor_equivalent(schema):
    from shared.core_output_schemas import factor
    before = deepcopy(schema)
    compact = factor(schema)
    Draft202012Validator.check_schema(schema)
    Draft202012Validator.check_schema(compact)
    for value in _UNION_VALUES:
        assert Draft202012Validator(schema).is_valid(value) == Draft202012Validator(compact).is_valid(value), (schema, value)
    assert schema == before
    assert factor(schema) == compact
    return compact


@pytest.mark.parametrize('siblings', [
    {'title': 'Nullable value', 'description': 'Preserve this explanation',
     'default': None, 'deprecated': True, 'readOnly': True, 'writeOnly': False},
    {'minLength': 2, 'maxLength': 4, 'pattern': '^[a-z]+$'},
    {'enum': [None, 'ab']},
    {'not': {'const': 'ab'}},
])
def test_factor_pure_type_anyof_keeps_all_siblings(siblings):
    schema = {**deepcopy(siblings), 'anyOf': [{'type': 'string'}, {'type': 'null'}]}
    compact = _assert_union_factor_equivalent(schema)
    assert 'anyOf' not in compact
    assert set(compact['type']) == {'string', 'null'}
    for name, value in siblings.items():
        assert compact[name] == value


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('constraints', [
    {'minLength': 2}, {'maxLength': 3}, {'pattern': '^[a-z]+$'},
    {'minLength': 2, 'maxLength': 4, 'pattern': '^[a-z]+$'},
])
def test_factor_nullable_string_constraints_keep_type_and_metadata(reverse, constraints):
    branches = [{'type': 'string', **constraints}, {'type': 'null'}]
    if reverse:
        branches.reverse()
    metadata = {'title': 'Optional text', 'description': 'Keep explicit type and this hint',
                'default': None, 'examples': [None, 'ab'], 'deprecated': True,
                'readOnly': False, 'writeOnly': True}
    schema = {**deepcopy(metadata), 'anyOf': branches, 'not': {'const': 'ZZ'}}
    compact = _assert_union_factor_equivalent(schema)
    assert 'anyOf' not in compact
    assert set(compact['type']) == {'string', 'null'}
    for name, value in {**metadata, **constraints, 'not': {'const': 'ZZ'}}.items():
        assert compact[name] == value
    assert Draft202012Validator(compact).is_valid(None)


@pytest.mark.parametrize('schema', [
    {'type': 'string', 'anyOf': [{'type': 'string', 'minLength': 1}, {'type': 'null'}]},
    {'type': ['string'], 'anyOf': [{'type': 'string', 'minLength': 1}, {'type': 'null'}]},
    {'type': 'object', 'anyOf': [{'type': 'string'}, {'type': 'null'}]},
    {'minLength': 3, 'anyOf': [{'type': 'string', 'minLength': 1}, {'type': 'null'}]},
    {'maxLength': 1, 'anyOf': [{'type': 'string', 'maxLength': 4}, {'type': 'null'}]},
    {'pattern': '^a$', 'anyOf': [{'type': 'string', 'pattern': '^[a-z]+$'}, {'type': 'null'}]},
    {'anyOf': [{'type': 'string', 'minLength': 1, 'description': 'String branch hint'}, {'type': 'null'}]},
    {'anyOf': [{'type': 'string', 'minLength': 1}, {'type': 'null', 'description': 'Null branch hint'}]},
    {'anyOf': [{'type': 'string', 'minLength': 1, 'enum': ['ab']}, {'type': 'null'}]},
    {'anyOf': [{'type': 'string', 'minLength': 1}, {'type': 'null'}, {'type': 'integer'}]},
    {'oneOf': [{'type': 'string'}, {'type': 'string'}, {'type': 'null'}]},
    {'$defs': {'text': {'type': 'string', 'minLength': 2}},
     'anyOf': [{'$ref': '#/$defs/text', 'maxLength': 3}, {'type': 'null'}]},
    {'type': 'object', 'properties': {'literal': {'const': {
        'anyOf': [{'type': 'string'}, {'type': 'null'}], 'description': 'literal JSON'}}}},
])
def test_factor_type_union_guards_keep_intersections_and_branch_annotations(schema):
    compact = _assert_union_factor_equivalent(schema)
    if 'anyOf' in schema:
        assert 'anyOf' in compact
    if 'oneOf' in schema:
        assert compact['oneOf'] == schema['oneOf']
    for keyword in ('type', 'minLength', 'maxLength', 'pattern'):
        if keyword in schema:
            assert compact[keyword] == schema[keyword]
    for branch in schema.get('anyOf', []):
        if 'description' in branch:
            assert any(candidate.get('description') == branch['description']
                       for candidate in compact['anyOf'])


def test_validation_fingerprint_nullable_string_retains_each_constraint():
    constraints = {'minLength': 2, 'maxLength': 4, 'pattern': '^[a-z]+$'}
    verbose = {'anyOf': [{'type': 'string', **constraints}, {'type': 'null'}], 'default': None}
    shorthand = {'type': ['string', 'null'], **constraints, 'default': None}
    assert _validation_fingerprint(verbose) == _validation_fingerprint(shorthand)
    for keyword, replacement in {'minLength': 3, 'maxLength': 3, 'pattern': '^a+$'}.items():
        assert _validation_fingerprint(shorthand) != _validation_fingerprint({**shorthand, keyword: replacement})
        missing = {key: value for key, value in shorthand.items() if key != keyword}
        assert _validation_fingerprint(shorthand) != _validation_fingerprint(missing)
    assert _validation_fingerprint(shorthand) != _validation_fingerprint({**shorthand, 'type': 'string'})
    for schema in (verbose, shorthand):
        _assert_union_factor_equivalent(schema)


@pytest.mark.parametrize('extensions', [True, False])
def test_factor_recompacts_type_union_after_short_reference_inlining(extensions):
    from shared.core_output_schemas import factor
    schema = {'$defs': {'short_text': {'type': 'string'}},
              'type': 'object', 'additionalProperties': extensions,
              'description': 'Keep the explicit root extension contract and hint.',
              'properties': {'operation_id': {
                  'description': 'Original operation ID, never execution authority.',
                  'anyOf': [{'$ref': '#/$defs/short_text'}, {'type': 'string'}]}},
              'required': ['operation_id']}
    before = deepcopy(schema)
    compact = factor(schema)
    field = compact['properties']['operation_id']
    assert 'anyOf' not in field
    assert field['type'] in ('string', ['string'])
    assert field['description'] == schema['properties']['operation_id']['description']
    assert compact['description'] == schema['description']
    assert compact['additionalProperties'] is extensions
    Draft202012Validator.check_schema(compact)
    values = [{}, [], None, {'operation_id': 'original-id', 'extra': 1}]
    values += [{'operation_id': value} for value in _UNION_VALUES]
    for value in values:
        assert Draft202012Validator(schema).is_valid(value) == Draft202012Validator(compact).is_valid(value), value
    assert schema == before
    assert factor(schema) == compact


def test_factor_short_nullable_nodes_use_cost_checks_without_changing_validation():
    import json
    from shared.core_output_schemas import factor
    nullable = {'type': ['string', 'null']}
    assert len(json.dumps(nullable, separators=(',', ':'))) == 26
    schema = {'type': 'object', 'additionalProperties': False,
              'properties': {**{f'field_{index}': deepcopy(nullable) for index in range(24)},
                             'plain_a': {'type': 'string'}, 'plain_b': {'type': 'string'}},
              'required': ['field_0']}
    before = deepcopy(schema)
    compact = factor(schema)
    # A frequent 26-byte node saves space; a tiny type:string node must stay inline.
    refs = [compact['properties'][f'field_{index}']['$ref'] for index in range(24)]
    assert len(set(refs)) == 1
    assert compact['properties']['plain_a'] == compact['properties']['plain_b'] == {'type': 'string'}
    assert len(json.dumps(compact)) < len(json.dumps(schema))
    Draft202012Validator.check_schema(compact)
    values = [{}, {'field_0': None}, {'field_0': 'ok', 'extra': 1}, [], None]
    for name in schema['properties']:
        values += [{**{'field_0': 'required'}, name: value} for value in _UNION_VALUES]
    for value in values:
        assert Draft202012Validator(schema).is_valid(value) == Draft202012Validator(compact).is_valid(value), value
    assert schema == before
    assert factor(schema) == compact


def test_factor_post_inline_nullable_string_keeps_minimum_null_and_hint():
    schema = {'$defs': {'text': {'type': 'string', 'minLength': 2}},
              'description': 'Optional identifier; retain the minimum length for strings.',
              'default': None,
              'anyOf': [{'$ref': '#/$defs/text'}, {'type': 'null'}]}
    compact = _assert_union_factor_equivalent(schema)
    assert 'anyOf' not in compact
    assert set(compact['type']) == {'string', 'null'}
    assert compact['minLength'] == 2
    assert compact['description'] == schema['description']
    assert compact['default'] is None
    validator = Draft202012Validator(compact)
    assert validator.is_valid(None)
    assert validator.is_valid('ab')
    assert not validator.is_valid('a')
    assert not validator.is_valid('')
    assert not validator.is_valid(2)


@pytest.mark.parametrize('closed', ['neither', 'parent', 'branch'])
def test_factor_shared_union_properties_preserves_closed_objects_and_values(closed):
    from shared.core_output_schemas import factor
    shared = {'type': 'string', 'minLength': 2}
    schema = {'type': 'object', 'anyOf': [
        {'properties': {'common': shared, 'left': {'const': True}}, 'required': ['common', 'left']},
        {'properties': {'common': shared, 'right': {'const': 1}}, 'required': ['common', 'right']},
    ]}
    if closed == 'parent':
        schema['additionalProperties'] = False
        schema['properties'] = {'left': {}, 'right': {}}
    elif closed == 'branch':
        schema['anyOf'][0]['additionalProperties'] = False
    before = deepcopy(schema)
    compact = factor(schema)
    for common in (None, '', 'a', 'ab', 1, True, [], {}):
        for fields in ({}, {'left': True}, {'left': 1}, {'right': 1}, {'right': True},
                       {'left': True, 'right': 1}, {'left': True, 'extra': 1}):
            for value in (fields, {'common': common, **fields}):
                assert Draft202012Validator(schema).is_valid(value) == Draft202012Validator(compact).is_valid(value)
    assert schema == before
    if closed == 'neither':
        assert compact['properties']['common'] == shared
