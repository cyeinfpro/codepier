"""The removed workspace UI must not be required for MCP development tools."""
import pytest
from hub import mcp_apps
from hub.core_tools import help_result
from shared.contracts import tool_definitions
from shared.core_contracts import CORE_TOOLS, REPLACED_MCP_TOOLS

@pytest.mark.parametrize('profile', ['core', 'coding', 'full'])
def test_text_catalog_keeps_core_queries_identity_and_attachment_contract(profile):
    definitions = {item['name']: item for item in tool_definitions(profile)}
    assert set(definitions) == CORE_TOOLS | {'project_query', 'task_query', 'get_profile', 'get_access_context'}
    for item in definitions.values():
        assert 'openai/ui' not in item['_meta']
        assert 'openai/outputTemplate' not in item['_meta']
        assert 'resourceUri' not in item['_meta'].get('ui', {})
    assert definitions['write']['_meta']['openai/fileParams'] == ['file']
    assert help_result('workspace', 'context')['scope'] == 'read'
    assert help_result('write', 'import')['scope'] == 'write'

def test_retired_opener_uses_existing_migration_error_contract():
    assert REPLACED_MCP_TOOLS['workbench'] == 'project_query'

@pytest.mark.parametrize('uri', ['ui://codepier/workspace-v1.html', 'ui://relay/workspace-v1.html'])
def test_saved_workspace_resource_is_small_inert_retirement_notice(uri):
    assert uri not in {item['uri'] for item in mcp_apps.list_resources()}
    resource = mcp_apps.read_resource(uri, lambda: 'https://hub.example')
    assert len(resource['text'].encode()) < 2048
    assert 'project_query' in resource['text']
    assert '<script' not in resource['text'].lower()
    assert 'callServerTool' not in resource['text']
    assert 'updateModelContext' not in resource['text']

@pytest.mark.parametrize('name,args', [
    ('workbench', {}),
    ('open_workspace', {'project': 'P'}),
    ('workspace', {'operation': 'open', 'project': 'P'}),
    ('project_query', {'operation': 'open', 'project': 'P'}),
    ('workflows_get', {'workflow_id': 'a' * 32}),
    ('workspace', {'operation': 'workflow_get', 'options': {'workflow_id': 'a' * 32}}),
])
def test_project_and_workflow_results_no_longer_attach_workspace_bindings(name, args):
    result = {'content': [], 'structuredContent': {'project_id': 'p'}, '_meta': {'existing': 'preserved'}}
    assert mcp_apps.attach(result.copy(), name, args, result['structuredContent'],
                           lambda: 'https://hub.example') == result
