"""Public catalogs contain core tools, explicit queries and stable identity tools."""
from shared.core_contracts import CORE_TOOLS, REPLACED_MCP_TOOLS
from shared.query_contracts import QUERY_TOOLS


def assert_task_catalog(catalog, model_count=13, *, expected_names=None):
    names = {tool['name'] for tool in catalog}
    expected = (CORE_TOOLS | QUERY_TOOLS | {'get_profile','get_access_context'}
                if expected_names is None else set(expected_names))
    assert len(catalog) == len(names) == len(expected)
    assert names == expected
    assert not names.intersection(REPLACED_MCP_TOOLS)
    visible = [tool for tool in catalog if tool.get('_meta', {}).get('ui', {}).get('visibility', ['model', 'app']) != ['app']]
    assert len(visible) == model_count == len(expected)
    assert not [tool for tool in catalog if tool not in visible]
    for tool in catalog:
        assert 'password' not in tool['inputSchema']['properties']
        if tool.get('_meta', {}).get('ui', {}).get('resourceUri'):
            assert 'model' in tool['_meta']['ui']['visibility']
            assert tool['_meta'].get('openai/visibility', 'public') == 'public'
