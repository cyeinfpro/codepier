"""Only pure schemas are memoized; returned contracts remain isolated copies."""
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from pydantic import BaseModel, TypeAdapter

from shared.schema_cache import schema_for


def test_model_and_adapter_schemas_match_uncached_generation():
    class Value(BaseModel):
        text: str
    for subject, expected in [(Value, Value.model_json_schema()),
                              (TypeAdapter(list[Value]), TypeAdapter(list[Value]).json_schema())]:
        value = schema_for(subject)
        assert value == expected
        value.clear()
        assert schema_for(subject) == expected


def test_rebuild_invalidates_only_the_matching_schema():
    class Value(BaseModel):
        number: int
    first = schema_for(Value)
    old_generation = Value.__pydantic_core_schema__
    Value.model_rebuild(force=True)
    assert Value.__pydantic_core_schema__ is not old_generation
    assert schema_for(Value) == first


def test_singleflight_and_bounded_storage(monkeypatch):
    from shared import schema_cache
    class Builder:
        def __init__(self):
            self.core_schema = object()
            self.count = 0
            self.lock = Lock()
        def json_schema(self):
            with self.lock:
                self.count += 1
            return {'type': 'object', 'properties': {'value': {'type': 'string'}}}
    builder = Builder()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: schema_for(builder), range(32)))
    assert builder.count == 1
    assert all(value == results[0] and value is not results[0] for value in results[1:])
    builder.core_schema = object()
    schema_for(builder)
    assert builder.count == 2
    monkeypatch.setattr(schema_cache, '_LIMIT', 4)
    for _ in range(10):
        schema_for(Builder())
    assert len(schema_cache._CACHE) <= 4


def test_unknown_builder_is_not_assumed_version_pinned():
    class Builder:
        def __init__(self):
            self.calls = 0
        def json_schema(self):
            self.calls += 1
            return {'generation': self.calls}
    value = Builder()
    assert schema_for(value) != schema_for(value)


def test_cached_inputs_preserve_each_authorization_mode_and_mutation_isolation():
    from shared.contracts import TOOLS, tool_definitions, _compact_input_schema
    from shared.public_collaboration import tool_definitions as collaboration_definitions
    for factory in [tool_definitions, collaboration_definitions]:
        fixed = factory(authorization='fixed')
        role = factory(authorization='role')
        assert all(item['_meta']['securitySchemes'] != role[index]['_meta']['securitySchemes']
                   for index, item in enumerate(fixed))
        fixed[0]['inputSchema']['poison'] = 'not-shared'
        assert 'poison' not in factory(authorization='fixed')[0]['inputSchema']
        assert 'poison' not in factory(authorization='role')[0]['inputSchema']
    for item in tool_definitions():
        assert item['inputSchema'] == _compact_input_schema(TOOLS[item['name']].model.model_json_schema())
