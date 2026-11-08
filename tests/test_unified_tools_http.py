"""End-to-end: the default catalog is query/act/get_profile and calls reach the same handlers."""
import json

import pytest

from tests.test_audit_api import api  # noqa: F401  (fixture)
from tests.test_mcp_catalog_modes import call


def stable(value):
    """Drop live observation timestamps; everything else must match exactly."""
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items() if k != 'observed_at'}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


def structured(response):
    body = response.json()
    assert 'result' in body, body
    return body['result']


@pytest.mark.parametrize('modern', [False, True])
def test_default_catalog_is_unified_and_small(api, modern):  # noqa: F811
    _, client, credential = api
    result = structured(call(client, credential, 'tools/list', modern=modern))
    names = [tool['name'] for tool in result['tools']]
    assert {'query', 'get_profile'} <= set(names) <= {'query', 'act', 'get_profile'}
    assert len(json.dumps(result['tools'], ensure_ascii=False)) <= 12_000
    legacy = structured(call(client, credential, 'tools/list', query='?profile=core', modern=modern))
    assert {'workspace', 'read', 'process', 'project_query'} <= {tool['name'] for tool in legacy['tools']}


@pytest.mark.parametrize('modern', [False, True])
def test_unified_call_matches_legacy_call(api, modern):  # noqa: F811
    _, client, credential = api
    unified = structured(call(client, credential, 'tools/call',
                              {'name': 'query', 'arguments': {'action': 'project', 'view': 'list'}}, modern=modern))
    legacy = structured(call(client, credential, 'tools/call',
                             {'name': 'workspace', 'arguments': {'operation': 'list'}}, modern=modern))
    assert not unified.get('isError'), unified
    assert stable(unified['structuredContent']) == stable(legacy['structuredContent'])
    who = structured(call(client, credential, 'tools/call',
                          {'name': 'query', 'arguments': {'action': 'whoami'}}, modern=modern))
    assert not who.get('isError'), who
    legacy_who = structured(call(client, credential, 'tools/call',
                                 {'name': 'get_access_context', 'arguments': {}}, modern=modern))
    assert who['structuredContent'].keys() == legacy_who['structuredContent'].keys()


def test_help_and_guards(api):  # noqa: F811
    _, client, credential = api
    catalog = structured(call(client, credential, 'tools/call',
                              {'name': 'query', 'arguments': {'action': 'help'}}))
    assert not catalog.get('isError'), catalog
    assert 'file' in catalog['structuredContent']['tools']['query']
    schema = structured(call(client, credential, 'tools/call',
                             {'name': 'query', 'arguments': {'action': 'help', 'args': {'action': 'job', 'view': 'wait'}}}))
    assert not schema.get('isError'), schema
    assert schema['structuredContent']['unified_call']['view'] == 'wait'
    blocked = structured(call(client, credential, 'tools/call', {'name': 'query', 'arguments': {
        'action': 'project', 'project': 'x', 'args': {'capture_baseline': True}}}))
    assert blocked['isError']
    wrong = structured(call(client, credential, 'tools/call', {'name': 'query', 'arguments': {'action': 'exec'}}))
    assert wrong['isError']
    assert json.loads(wrong['content'][0]['text'])['error']['code'] == 'UNKNOWN_OPERATION'
