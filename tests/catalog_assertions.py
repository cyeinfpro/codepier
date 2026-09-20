"""Assert transport catalogs separately from model-visible tool counts.

MCP Apps needs app-only tools in tools/list for UI dispatch. Keeping them in the
wire catalog must not silently add another model-facing tool or owner action.
"""


def assert_task_catalog(catalog, model_count):
    by_name = {tool['name']: tool for tool in catalog}
    assert len(by_name) == len(catalog), 'Duplicate tool names in transport catalog'
    model_names, app_only = set(), set()
    for name, tool in by_name.items():
        visibility = tool.get('_meta', {}).get('ui', {}).get('visibility', ['model', 'app'])
        assert isinstance(visibility, list) and visibility
        assert set(visibility) <= {'model', 'app'}
        if 'model' in visibility:
            model_names.add(name)
        else:
            app_only.add(name)
    assert app_only == {'workspace_status'}
    assert len(model_names) == model_count
    assert len(catalog) == model_count + 1
    assert not {'integration_control', 'validations_accept'} & set(by_name)
    dashboard = by_name['workspace_status']
    assert dashboard['_meta']['ui']['visibility'] == ['app']
    assert dashboard['_meta']['openai/visibility'] == 'private'
    assert dashboard['_meta']['openai/widgetAccessible'] is True
    assert dashboard['annotations']['readOnlyHint'] is True
