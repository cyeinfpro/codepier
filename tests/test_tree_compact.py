"""Compact tree discovery is an opt-in presentation, not a storage format."""
import copy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from agent.filesystem import FileEngine
from agent.journal import Journal
from hub.core_tools import help_result, invoke, invoke_query, result
from shared.contracts import TOOLS, Tree, tool_definitions
from shared.mcp_presentation import present


def test_tree_format_defaults_and_help():
    assert Tree(project="P").format == "structured"
    with pytest.raises(ValidationError):
        Tree(project="P", format="unknown")
    schema = help_result("workspace", "tree")["inputSchema"]
    Draft202012Validator(schema).validate({
        "operation": "tree", "project": "P", "options": {"format": "compact"},
    })
    assert schema["properties"]["options"]["properties"]["format"]["enum"] == ["structured", "compact"]


@pytest.mark.asyncio
async def test_tree_options_reach_validated_backend_without_scope_override():
    calls = []

    class Runtime:
        async def invoke(self, name, arguments, principal):
            if name == "workspace":
                return await invoke(self, name, arguments, principal)
            model = TOOLS[name].model.model_validate(arguments)
            calls.append((name, model.model_dump()))
            return {"entries": [], "truncated": False}

    runtime = Runtime()
    await invoke_query(runtime, "project_query", {
        "operation": "tree", "project": "P", "path": "src", "offset": 3,
        "options": {"format": "compact"},
    }, None)
    assert calls[0][0] == "fs_tree"
    assert calls[0][1]["format"] == "compact"
    assert calls[0][1]["offset"] == 3
    assert calls[0][1]["project"] == "P"
    from shared.util import DevError
    with pytest.raises(DevError):
        await invoke(runtime, "workspace", {
            "operation": "tree", "project": "P",
            "options": {"format": "compact", "project": "Other"},
        }, None)
    assert len(calls) == 1


@pytest.mark.parametrize("name", ["project_query", "workspace"])
def test_compact_tree_preserves_pagination_errors_and_raw_value(name):
    value = {
        "operation_id": "op", "entries": [
            {"path": "src/a.txt", "name": "a.txt", "type": "file", "size": 3},
            {"path": "src/sub", "name": "sub", "type": "directory", "size": None},
        ], "truncated": True, "next_offset": 7, "scan_error": "SCAN_LIMIT",
        "exclusions": "Protected files excluded.",
    }
    original = copy.deepcopy(value)
    args = {"project": "P", "path": "src", "offset": 5}
    args.update(operation="tree", options={"format": "compact"})
    public = result(name, args, value)["structuredContent"]
    assert public["entries"] == "a.txt\t3\nsub/"
    assert public["base"] == "src"
    for field in ("operation_id", "truncated", "next_offset", "scan_error"):
        assert public[field] == value[field]
    assert value == original
    assert isinstance(present("fs_tree", {"path": "src"}, value)["entries"], list)
    assert isinstance(result(name, args, value, view=1)["structuredContent"]["entries"], list)
    assert "exclusions" not in public


def test_compact_tree_published_schema_and_empty_page():
    value = {"operation_id": "op", "entries": [], "truncated": False,
             "next_offset": None, "scan_error": None, "exclusions": "Protected files excluded."}
    args = {"operation": "tree", "project": "P", "options": {"format": "compact"}}
    public = result("project_query", args, value)["structuredContent"]
    schema = next(row["outputSchema"] for row in tool_definitions() if row["name"] == "project_query")
    Draft202012Validator(schema).validate(public)
    assert public["entries"] == ""
    assert public["base"] == "."


@pytest.mark.parametrize("base", ["src", "./src", "src/"])
def test_compact_tree_normalizes_base_and_quotes_ambiguous_paths(base):
    value = {"operation_id": "op", "entries": [
        {"path": "src/a\tb.txt", "type": "file", "size": 0},
        {"path": "src/normal.txt", "type": "file", "size": 3},
    ], "truncated": False, "next_offset": None, "exclusions": "Protected files excluded."}
    args = {"operation": "tree", "project": "P", "path": base, "options": {"format": "compact"}}
    public = result("project_query", args, value)["structuredContent"]
    assert public["base"] == "src"
    assert public["entries"] == '"a\\tb.txt"\t0\nnormal.txt\t3'
    assert public["exclusions"] == value["exclusions"]
    assert isinstance(result("project_query", {**args, "options": {}}, value)["structuredContent"]["entries"], list)


def test_agent_tree_stays_structured_and_page_cursor_unchanged(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a.txt").write_text("one")
    (root / "b.txt").write_text("two")
    config = tmp_path / "config.json"
    config.write_text("{}")
    journal = Journal(tmp_path / "state")
    engine = FileEngine({"allowed_roots": [{"path": str(root), "writable": True}]}, journal, config)
    project = {"id": "p", "alias": "P", "root": str(root), "mode": "write"}
    try:
        args = Tree(project="P", limit=1).model_dump()
        normal = engine.tree(project, args)
        compact = engine.tree(project, {**args, "format": "compact"})
        assert compact == normal
        assert isinstance(compact["entries"], list)
        assert compact["next_offset"] == 1 and compact["truncated"]
        tail = engine.tree(project, {**args, "format": "compact", "offset": compact["next_offset"]})
        assert tail["entries"][0]["path"] != compact["entries"][0]["path"]
        assert tail["next_offset"] is None and not tail["truncated"]
    finally:
        journal.db.close()
