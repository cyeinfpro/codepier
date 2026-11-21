"""Regression for the legacy fallback poll deadline."""

import asyncio

from hub import unified_tools


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
