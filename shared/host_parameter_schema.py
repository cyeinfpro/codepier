"""Object-only discovery envelope for clients that cannot import root unions.

This is a conservative parameter envelope, NOT an authorization or execution
validator. Exact per-action and per-step validation stays in the original
Pydantic adapter before dispatch. No unknown operation or field is introduced.
Only code-defined schemas are cached; callers receive independent copies.
"""
from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache


_ANNOTATIONS = {'title', 'description', 'default', 'examples', 'discriminator', '$defs'}
_STRUCTURAL = {'type', 'properties', 'required', 'additionalProperties', 'items', 'enum'}


def _types(schema):
    value = schema.get('type')
    return set(value if isinstance(value, list) else [value]) if value else set()


def _merge(choices):
    """Union alternatives without keeping anyOf/oneOf branch selection."""
    choices = [value for value in choices if value is not False]
    if not choices:
        return False
    if any(value is True or value == {} for value in choices):
        return {}
    if len(choices) == 1:
        return deepcopy(choices[0])
    # Preserve only constraints shared by every alternative. Action-specific
    # required fields are enforced by the server, not by a first-branch host.
    result = {key: deepcopy(value) for key, value in choices[0].items()
              if key not in _ANNOTATIONS | _STRUCTURAL
              and all(key in item and item[key] == value for item in choices[1:])}
    if all(item.get('description') == choices[0].get('description') for item in choices):
        if choices[0].get('description'):
            result['description'] = choices[0]['description']
    types = set().union(*(_types(item) for item in choices))
    if all(_types(item) for item in choices):
        result['type'] = next(iter(types)) if len(types) == 1 else sorted(types)
    if all('enum' in item for item in choices):
        values = {json.dumps(value, sort_keys=True): value for item in choices for value in item['enum']}
        result['enum'] = list(values.values())
    objects = [item for item in choices if 'object' in _types(item)]
    if objects:
        names = dict.fromkeys(name for item in objects for name in item.get('properties', {}))
        result['properties'] = {
            name: _merge([item.get('properties', {}).get(name, item.get('additionalProperties', True))
                          for item in objects]) for name in names
        }
        required = set.intersection(*(set(item.get('required', [])) for item in objects))
        result['required'] = sorted(required)
        result['additionalProperties'] = _merge([item.get('additionalProperties', True) for item in objects])
    arrays = [item for item in choices if 'array' in _types(item)]
    if arrays:
        result['items'] = _merge([item.get('items', True) for item in arrays])
    return result


def _expand(node, definitions, references=()):
    if isinstance(node, bool):
        return node
    if not isinstance(node, dict):
        raise ValueError('Expected a JSON Schema object or boolean')
    if '$ref' in node:
        reference = node['$ref']
        if not reference.startswith('#/$defs/') or reference in references:
            raise ValueError('Host envelope requires finite local schema references')
        name = reference[len('#/$defs/'):].replace('~1', '/').replace('~0', '~')
        if name not in definitions:
            raise ValueError('Missing local schema definition: ' + name)
        expanded = _expand(definitions[name], definitions, (*references, reference))
        siblings = {key: value for key, value in node.items() if key != '$ref'}
        if siblings:
            if not isinstance(expanded, dict):
                raise ValueError('Unsupported boolean reference with siblings')
            expanded.update(_expand(siblings, definitions, references))
        return expanded
    if 'allOf' in node:
        # Do not silently strip new intersecting schemas: extend with tests first.
        raise ValueError('Intersecting schemas need an explicit host projection')
    alternatives = node.get('oneOf', node.get('anyOf'))
    if alternatives is not None:
        result = _merge([_expand(item, definitions, references) for item in alternatives])
        siblings = {key: value for key, value in node.items()
                    if key not in {'oneOf', 'anyOf'} | _ANNOTATIONS}
        if siblings:
            if not isinstance(result, dict):
                raise ValueError('Unsupported boolean union with siblings')
            result.update(_expand(siblings, definitions, references))
        if isinstance(result, dict) and node.get('description'):
            result['description'] = node['description']
        return result
    result = {}
    for key, value in node.items():
        if key in _ANNOTATIONS - {'description'}:
            continue  # Never let a host inject a default belonging to another action.
        if key == 'const':
            result['enum'] = [deepcopy(value)]
        elif key == 'properties':
            result[key] = {name: _expand(spec, definitions, references) for name, spec in value.items()}
        elif key in ('items', 'additionalProperties') and isinstance(value, (dict, bool)):
            result[key] = _expand(value, definitions, references)
        else:
            result[key] = deepcopy(value)
    return result


@lru_cache(maxsize=8)
def _cached(serialized):
    schema = json.loads(serialized)
    result = _expand(schema, schema.get('$defs', {}))
    if not isinstance(result, dict) or result.get('type') != 'object':
        raise ValueError('Public tool parameters must be an object')
    # Explicit per-field guidance replaces opaque discriminator branches for
    # clients. No extra prompt/user input or generic execution payload is added.
    required_for = {}
    def collect(node):
        if '$ref' in node:
            collect(schema['$defs'][node['$ref'].rsplit('/', 1)[-1]])
        elif 'oneOf' in node or 'anyOf' in node:
            for child in node.get('oneOf', node.get('anyOf', [])):
                collect(child)
        else:
            action = node.get('properties', {}).get('action', {}).get('const')
            if action:
                for name in node.get('required', []):
                    required_for.setdefault(name, set()).add(action)
    collect(schema)
    for name, actions in required_for.items():
        if name == 'action' or name not in result.get('properties', {}):
            continue
        spec = result['properties'][name]
        if isinstance(spec, dict):
            prefix = (spec.get('description', '') + ' ').lstrip()
            spec['description'] = prefix + 'Required for action: ' + ', '.join(sorted(actions)) + '. Omit when not used by the selected action.'
    result['description'] = ('Object-only discovery envelope. Supply only fields for the selected action; '
                             'the server validates its exact required fields and typed step before dispatch.')
    return result


def host_parameter_schema(schema):
    """Return a copied, finite object envelope derived only from the exact schema."""
    return deepcopy(_cached(json.dumps(schema, sort_keys=True, separators=(',', ':'))))
