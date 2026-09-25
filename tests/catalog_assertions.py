"""All public MCP profiles contain exactly the same nine callable tools."""
from shared.core_contracts import CORE_TOOLS, REPLACED_MCP_TOOLS


def assert_task_catalog(catalog, model_count=9):
    names = {tool['name'] for tool in catalog}
    assert len(catalog) == len(names) == model_count == 9
    assert names == CORE_TOOLS
    assert not names.intersection(REPLACED_MCP_TOOLS)
    for tool in catalog:
        assert tool.get('_meta', {}).get('ui', {}).get('visibility', ['model', 'app']) != ['app']
        assert 'password' not in tool['inputSchema']['properties']
