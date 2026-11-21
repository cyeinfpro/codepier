"""Regression tests for correctness bugs found reviewing the pending MCP changes."""

import asyncio

import pytest

from agent import core_files
from agent.filesystem import FileEngine
from hub import unified_tools
from shared.util import DevError


def test_edit_byte_limit_uses_original_crlf_span(monkeypatch):
    before = "头\r\n边界\r\n尾\n".encode()
    monkeypatch.setattr(core_files, "MAX_TEXT_EDIT", len(before))
    # This keeps every byte count unchanged, including the replaced CRLF.
    result = core_files.edited_bytes(
        FileEngine, before, [{"old_text": "边界\n", "new_text": "测试\n"}]
    )
    assert result == "头\r\n测试\r\n尾\n".encode()


def test_edit_byte_limit_still_rejects_real_growth(monkeypatch):
    before = "边界\r\n".encode()
    monkeypatch.setattr(core_files, "MAX_TEXT_EDIT", len(before))
    with pytest.raises(DevError) as raised:
        core_files.edited_bytes(
            FileEngine, before, [{"old_text": "边界", "new_text": "真正超长"}]
        )
    assert raised.value.code == "FILE_TOO_LARGE"


def test_polling_never_drops_the_caller_timeout_for_whole_seconds(monkeypatch):
    observed = []
    operation = {"operation": "wait", "operation_ids": ["same-op"]}
    final = {"operations": [{"operation_id": "same-op", "pending": False}]}

    class Runtime:
        async def invoke(self, tool, args, principal):
            assert tool == "process"
            assert args["operation_ids"] == ["same-op"]
            return final

    async def apply_deadline(read, stop, remaining):
        observed.append(remaining)
        return True, await read

    monkeypatch.setattr(unified_tools.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(unified_tools, "_poll_or_cancel", apply_deadline)
    assert asyncio.run(
        unified_tools._poll_wait_operation(Runtime(), operation, None, 11.25, None)
    ) == final
    assert observed == [1.25]
