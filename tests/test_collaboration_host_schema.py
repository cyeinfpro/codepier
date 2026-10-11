"""Host import contract, separate from authoritative server action validation.

The first-branch importer is a deliberately limited fixture, not OpenAI host
source. It reproduces the supplied screenshot's observable validation symptom.
"""
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from shared.public_collaboration import ACTIONS, ADAPTERS, resolve, tool_definitions
from shared.util import DevError
from tests.test_public_collaboration_contracts import example


def descriptor(name):
    return next(tool for tool in tool_definitions() if tool['name'] == name)


def first_branch_import(schema):
    """Model a host that can register an object, but cannot dispatch root unions."""
    node = schema
    while 'oneOf' in node:
        node = node['oneOf'][0]
        if '$ref' in node:
            node = schema['$defs'][node['$ref'].rsplit('/', 1)[-1]]
    return {**deepcopy(node), '$defs': deepcopy(schema.get('$defs', {}))}


@pytest.mark.parametrize('name', list(ACTIONS))
def test_host_has_direct_action_enum(name):
    schema = descriptor(name)['inputSchema']
    assert schema['type'] == 'object'
    assert set(schema.get('properties', {}).get('action', {}).get('enum', [])) == set(ACTIONS[name])
    assert 'action' in schema['required']
    assert schema['additionalProperties'] is False
    assert not {'oneOf', 'anyOf', 'discriminator', '$ref', '$defs'} & schema.keys()


def test_screenshot_symptom_reproduced_by_original_first_branch_import():
    raw = {'action': 'dot_inbox', 'project': 'fixture-project', 'dot_id': 'fixture-dot'}
    original = {'type': 'object', **ADAPTERS['collaboration_query'].json_schema()}
    Draft202012Validator(original).validate(raw)
    assert resolve('collaboration_query', raw)[0] == 'collaboration_dot_inbox'
    errors = list(Draft202012Validator(first_branch_import(original)).iter_errors(raw))
    assert any(error.validator == 'const' and error.validator_value == 'overview'
               and list(error.path) == ['action'] for error in errors)


@pytest.mark.parametrize('name,action', [(name, action) for name, actions in ACTIONS.items() for action in actions])
def test_every_action_survives_object_only_host_import(name, action):
    raw = example(name, action)
    schema = descriptor(name)['inputSchema']
    Draft202012Validator(first_branch_import(schema)).validate(raw)
    assert resolve(name, raw)


def schema_nodes(node):
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from schema_nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from schema_nodes(value)


@pytest.mark.parametrize('name', list(ACTIONS))
def test_no_branch_selectors_or_default_injection_even_inside_execute_arguments(name):
    schema = descriptor(name)['inputSchema']
    for node in schema_nodes(schema):
        assert not {'oneOf', 'anyOf', 'discriminator', '$defs', '$ref', 'const', 'default'} & node.keys()
    assert descriptor(name)['_meta']['codepier/inputSchemaVersion'] == 'object-envelope-v1'


@pytest.mark.parametrize('tool,args', [
    ('read', {'path': 'README.md'}),
    ('write', {'path': 'new.txt', 'content': '', 'expected_sha256': 'new'}),
    ('edit', {'path': 'a.txt', 'edits': [{'old_text': 'x', 'new_text': 'y'}], 'expected_sha256': 'a' * 64}),
    ('edit', {'changes': [{'action': 'delete', 'path': 'a.txt', 'expected_sha256': 'a' * 64}]}),
    ('exec', {'command': 'printf fixture', 'env': {'FIXTURE': 'value'}, 'yield_seconds': 0}),
])
def test_execute_all_steps_survive_flat_host_schema(tool, args):
    raw = {**example('collaboration_work', 'execute'), 'tool': tool, 'arguments': args}
    Draft202012Validator(descriptor('collaboration_work')['inputSchema']).validate(raw)
    resolve('collaboration_work', raw)


def test_missing_action_specific_fields_still_fail_server_before_dispatch():
    for name, actions in ACTIONS.items():
        for action in actions:
            raw = example(name, action)
            for field in raw:
                invalid = {key: value for key, value in raw.items() if key != field}
                with pytest.raises(DevError) as failure:
                    resolve(name, invalid)
                assert failure.value.code == 'INVALID_ARGUMENTS'
    # A field known to another action cannot be smuggled into a dot inbox call.
    with pytest.raises(DevError):
        resolve('collaboration_query', {'action': 'dot_inbox', 'project': 'fixture',
            'dot_id': 'dot', 'policy_id': 'unrelated-policy'})


def test_host_descriptor_mutation_does_not_poison_later_discovery():
    first = descriptor('collaboration_query')
    original = deepcopy(first['inputSchema'])
    first['inputSchema'].clear()
    assert descriptor('collaboration_query')['inputSchema'] == original
