"""Conflicting writes preserve the first committed update and block later readers."""

import asyncio
import uuid

import pytest

from agent.resource_queue import Claim, canonical_path
from shared.crypto import digest
from tests.review_support import eventually, local_agent, request, waiting


@pytest.mark.asyncio
async def test_queued_same_path_conditional_writes_preserve_first_update(local_agent):
    agent, root = local_agent
    path = root / "shared.txt"
    original, first_update = b"original\n", b"first update\n"
    old_sha = digest(original)
    path.write_bytes(original)
    first_writer = request(
        root, "write", path=path.name, expected_sha256=old_sha,
        content=first_update.decode(), idempotency_key=uuid.uuid4().hex,
    )
    second_writer = request(
        root, "write", path=path.name, expected_sha256=old_sha,
        content="second update\n", idempotency_key=uuid.uuid4().hex,
    )
    later_read = request(root, "read", path=path.name)

    async with asyncio.timeout(5), asyncio.TaskGroup() as jobs:
        # The shared reader blocks both writers. A later reader could run
        # alongside it if the queue forgot the earlier conflicting writers.
        async with agent.resources.slot(
            "existing-reader", [Claim("agent", "path", canonical_path(path), False)],
            lambda _: None,
        ):
            for call in (first_writer, second_writer, later_read):
                jobs.create_task(agent.handle(call))
                await eventually(lambda: waiting(agent, call))
            assert path.read_bytes() == original

    first = agent.journal.status(first_writer["id"])["result"]
    second = agent.journal.status(second_writer["id"])["result"]
    read = agent.journal.status(later_read["id"])["result"]
    assert first["ok"] and first["data"]["sha256"] == digest(first_update)
    assert not second["ok"] and second["error"]["code"] == "SHA_CONFLICT"
    assert read["ok"] and read["data"]["content"] == first_update.decode()
    assert path.read_bytes() == first_update
    backups = agent.journal.history(str(root.resolve()), path.name)
    assert [backup["id"] for backup in backups] == [first["data"]["backup_id"]]
    assert not agent.resources.active and not agent.resources.waiting
