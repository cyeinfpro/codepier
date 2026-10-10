"""Resource names are validated before lookup in both supported MCP eras."""
import pytest

from shared.mcp_protocol import MODERN, PREFIX, request_headers
from tests.test_audit_api import api


def resource_request(client, credential, params, modern):
    params = dict(params)
    if modern:
        params['_meta'] = {
            PREFIX + 'protocolVersion': MODERN,
            PREFIX + 'clientCapabilities': {},
        }
    body = {'jsonrpc': '2.0', 'id': 'resource-validation',
            'method': 'resources/read', 'params': params}
    headers = {'Authorization': 'Bearer ' + credential,
               'Accept': 'application/json, text/event-stream'}
    if modern:
        headers.update(request_headers(body))
    return client.post('/mcp', json=body, headers=headers)


@pytest.mark.parametrize('modern', [False, True], ids=['legacy', 'modern'])
@pytest.mark.parametrize('params', [
    {'uri': []}, {'uri': {}}, {'uri': None}, {'uri': True},
    {'uri': 1}, {'uri': 1.5}, {},
], ids=['list', 'object', 'null', 'boolean', 'integer', 'number', 'missing'])
def test_resource_uri_requires_a_string(api, modern, params):
    _, client, credential = api
    response = resource_request(client, credential, params, modern)
    assert response.status_code == (400 if modern else 200)
    assert response.json()['id'] == 'resource-validation'
    assert response.json()['error']['code'] == -32602
    assert response.headers['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('modern', [False, True], ids=['legacy', 'modern'])
def test_string_resource_lookups_preserve_success_and_not_found(api, modern):
    _, client, credential = api
    response = resource_request(client, credential, {'uri': 'rd://workflow'}, modern)
    assert response.status_code == 200
    assert response.json()['result']['contents'][0]['uri'] == 'rd://workflow'
    missing = resource_request(client, credential, {'uri': 'rd://missing'}, modern)
    assert missing.status_code == (404 if modern else 200)
    assert missing.json()['error']['code'] == (-32602 if modern else -32002)
