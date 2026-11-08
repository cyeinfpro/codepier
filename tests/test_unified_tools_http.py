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


@pytest.mark.parametrize('modern', [False, True])
def test_native_attachment_catalog_and_source_check(api, modern):  # noqa: F811
    _, client, credential = api
    tools = structured(call(client, credential, 'tools/list', modern=modern))['tools']
    query = next(tool for tool in tools if tool['name'] == 'query')
    assert query['_meta']['openai/fileParams'] == ['file']
    assert {'download_url', 'file_id'} <= set(query['inputSchema']['properties']['file']['required'])
    file = {'file_id': 'synthetic-id', 'download_url': 'https://files.oaiusercontent.com/fixture'}
    unified = structured(call(client, credential, 'tools/call', {'name': 'query', 'arguments': {
        'action': 'source_check', 'project': 'fixture', 'file': file,
        'args': {'idempotency_key': 'synthetic-source-check'}}}, modern=modern))
    legacy = structured(call(client, credential, 'tools/call', {'name': 'write', 'arguments': {
        'operation': 'source_check', 'project': 'fixture', 'file': file,
        'idempotency_key': 'synthetic-source-check'}}, modern=modern))
    assert not unified.get('isError'), unified
    assert stable(unified['structuredContent']) == stable(legacy['structuredContent'])
    blocked = structured(call(client, credential, 'tools/call', {'name': 'act', 'arguments': {
        'action': 'import', 'project': 'fixture', 'file': file, 'idempotency_key': 'synthetic-import',
        'path': 'fixture.txt', 'expected_sha256': 'a' * 64}}, modern=modern))
    assert blocked['isError']
    assert json.loads(blocked['content'][0]['text'])['error']['code'] == 'INSUFFICIENT_SCOPE'


@pytest.mark.parametrize('modern', [False, True])
def test_open_capability_help_round_trip(api, monkeypatch, modern):  # noqa: F811
    from hub.runtime import Runtime
    from shared.runtime_results import capability_index
    original = Runtime.invoke

    async def synthetic_open(self, name, args, principal):
        if name == 'workspace' and args.get('operation') == 'open':
            return {'capability_index': capability_index(args['project'])}
        return await original(self, name, args, principal)

    # Only the absent Agent's project-open response is synthetic. HTTP rendering,
    # discovery filtering, help translation, schema lookup and authorization are real.
    monkeypatch.setattr(Runtime, 'invoke', synthetic_open)
    _, client, credential = api
    opened = structured(call(client, credential, 'tools/call', {'name': 'query', 'arguments': {
        'action': 'project', 'project': 'fixture'}}, modern=modern))
    assert not opened.get('isError'), opened
    index = opened['structuredContent']['capability_index']
    assert 'search' in index
    for hint in index.values():
        helped = structured(call(client, credential, 'tools/call', hint, modern=modern))
        assert not helped.get('isError'), (hint, helped)
        assert 'unified_call' in helped['structuredContent']


@pytest.mark.parametrize('modern', [False, True])
def test_knowledge_discovery_help_and_read_token_guards(api, modern):  # noqa: F811
    _, client, credential = api
    tools = structured(call(client, credential, 'tools/list', modern=modern))['tools']
    query = next(item for item in tools if item['name'] == 'query')
    assert 'knowledge' in query['inputSchema']['properties']['action']['enum']
    for profile in ('core', 'full', 'coding'):
        legacy = structured(call(client, credential, 'tools/list',
            query='?profile=' + profile, modern=modern))['tools']
        assert 'project_knowledge_query' in {item['name'] for item in legacy}
        assert 'project_knowledge_build' not in {item['name'] for item in legacy}
    helped = structured(call(client, credential, 'tools/call', {'name': 'query',
        'arguments': {'action': 'help', 'args': {'action': 'knowledge'}}}, modern=modern))
    assert not helped.get('isError'), helped
    assert helped['structuredContent']['unified_call']['view'] == 'query'
    assert 'paths' in helped['structuredContent']['inputSchema']['properties']
    for name, arguments in (
        ('act', {'action': 'knowledge', 'project': 'fixture', 'idempotency_key': 'http-denied-build'}),
        ('project_knowledge_build', {'project': 'fixture', 'idempotency_key': 'http-denied-build'}),
    ):
        result = structured(call(client, credential, 'tools/call',
            {'name': name, 'arguments': arguments}, modern=modern))
        assert result['isError']
        assert json.loads(result['content'][0]['text'])['error']['code'] == 'INSUFFICIENT_SCOPE'
