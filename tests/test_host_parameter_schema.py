"""Projection must admit valid branches without inventing authorization."""
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from shared.host_parameter_schema import host_parameter_schema


def envelope(value):
    source = {'type': 'object', 'properties': {'value': value}, 'additionalProperties': False}
    result = host_parameter_schema(source)
    Draft202012Validator.check_schema(result)
    return result


@pytest.mark.parametrize('value', [None, '', 'x', 'longer'])
def test_nullable_scalars_keep_all_valid_alternatives_without_default_injection(value):
    schema = envelope({'anyOf': [{'type': 'string'}, {'type': 'null'}], 'default': None})
    Draft202012Validator(schema).validate({'value': value})
    assert schema['properties']['value']['type'] == ['null', 'string']
    assert 'default' not in schema['properties']['value']


@pytest.mark.parametrize('value', [{'path': 'a'}, {'command': 'pwd'}, {'changes': []}])
def test_nested_objects_keep_union_fields_and_only_common_required(value):
    branches = [{'type': 'object', 'properties': {name: spec}, 'required': [name], 'additionalProperties': False}
        for name, spec in [('path', {'type': 'string'}), ('command', {'type': 'string'}),
                           ('changes', {'type': 'array', 'items': {'type': 'object'}})]]
    schema = envelope({'oneOf': branches})
    Draft202012Validator(schema).validate({'value': value})
    assert schema['properties']['value']['required'] == []
    assert schema['properties']['value']['additionalProperties'] is False
    assert list(Draft202012Validator(schema).iter_errors({'value': {'unknown': 1}}))


@pytest.mark.parametrize('value', [{'path': 'a'}, {'path': 1}, {'other': 2}])
def test_open_object_alternative_cannot_be_accidentally_narrowed(value):
    schema = envelope({'anyOf': [
        {'type': 'object', 'properties': {'path': {'type': 'string'}}, 'additionalProperties': False},
        {'type': 'object', 'additionalProperties': True},
    ]})
    Draft202012Validator(schema).validate({'value': value})


@pytest.mark.parametrize('value', [[1, 2], ['x', 'y'], []])
def test_array_item_alternatives_are_merged(value):
    schema = envelope({'oneOf': [
        {'type': 'array', 'items': {'type': 'integer'}, 'minItems': 1},
        {'type': 'array', 'items': {'type': 'string'}, 'minItems': 0},
    ]})
    Draft202012Validator(schema).validate({'value': value})


def test_source_is_unmodified_and_cache_tracks_schema_content():
    source = {'type': 'object', 'properties': {'action': {'type': 'string', 'const': 'first', 'default': 'first'}}}
    saved = deepcopy(source)
    first = host_parameter_schema(source)
    assert source == saved
    first['properties']['action']['enum'].append('poison')
    assert host_parameter_schema(source)['properties']['action']['enum'] == ['first']
    source['properties']['action']['const'] = 'second'
    assert host_parameter_schema(source)['properties']['action']['enum'] == ['second']


@pytest.mark.parametrize('source', [
    {'type': 'object', '$ref': 'https://example.invalid/external'},
    {'type': 'object', '$ref': '#/$defs/missing'},
    {'type': 'object', '$defs': {'cycle': {'$ref': '#/$defs/cycle'}}, '$ref': '#/$defs/cycle'},
    {'type': 'object', 'allOf': [{'properties': {'x': {'type': 'string'}}}]},
])
def test_unsupported_new_shapes_fail_loudly_not_as_unconstrained_tools(source):
    with pytest.raises(ValueError):
        host_parameter_schema(source)
