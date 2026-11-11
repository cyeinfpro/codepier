"""The Agent hashes pipe bytes incrementally, including bytes outside its tail."""

import asyncio
import hashlib
import sys
import uuid

import pytest

from agent.runner import Agent
from shared.tool_protocol import wire_version
from shared.util import atomic_json


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["success", "failure", "timeout", "cancel"])
async def test_stream_fingerprint_survives_terminal_paths(tmp_path, ending):
    root = tmp_path / "project"
    root.mkdir()
    # Includes a split multibyte sequence and invalid UTF-8: fingerprint bytes,
    # not the replacement-decoded display or the retained 131072-char suffix.
    raw = ("行😀\n".encode() * 60000) + b"\xfftail\n"
    source = "import sys,time,pathlib; data=('行😀\\n'.encode()*60000)+b'\\xfftail\\n'; sys.stdout.buffer.write(data); sys.stdout.buffer.flush(); pathlib.Path('ready').touch(); "
    source += (
        "time.sleep(30)"
        if ending in {"timeout", "cancel"}
        else "sys.exit(7)"
        if ending == "failure"
        else "sys.exit(0)"
    )
    config = tmp_path / "config.json"
    atomic_json(
        config,
        {
            "hub_url": "http://127.0.0.1:9",
            "device_id": "fingerprint-fixture",
            "secret": "synthetic-fingerprint-secret-" * 3,
            "state_dir": str(tmp_path / "state"),
            "allowed_roots": [{"path": str(root), "writable": True, "allow_tasks": True}],
            "tasks": {
                "fingerprint": {
                    "projects": ["fixture"],
                    "command": [sys.executable, "-c", source],
                    "timeout": 1 if ending == "timeout" else 30,
                }
            },
            "project_runtimes": {"fixture": {"mode": "project_clean_v1"}},
        },
    )
    agent = Agent(config)
    operation = uuid.uuid4().hex
    request = {
        "id": operation,
        "tool": "tasks_run",
        "tool_contract_version": wire_version("tasks_run"),
        "project": {"root": str(root), "alias": "fixture", "mode": "write", "allow_tasks": True},
        "args": {"project": "fixture", "task": "fingerprint", "idempotency_key": uuid.uuid4().hex},
    }
    job = asyncio.create_task(agent.handle(request))
    try:
        if ending == "cancel":
            async with asyncio.timeout(10):
                while not (root / "ready").exists():
                    await asyncio.sleep(0.01)
            agent.cancel_call(operation)
        await asyncio.wait_for(job, 15)
        data = agent.journal.status(operation)["result"]["data"]
        assert data["output_total_bytes"] == len(raw)
        assert data["output_total_sha256"] == hashlib.sha256(raw).hexdigest()
        assert data["output_truncated"]
        assert len(data["output"]) <= 131072
        assert data["output"].endswith("�tail\n")
        if ending == "timeout":
            assert data["timed_out"]
        elif ending == "cancel":
            assert data["cancelled"]
        else:
            assert data["exit_code"] == (7 if ending == "failure" else 0)
        # Reconnect/retry returns the immutable original fingerprint.
        await agent.handle(request)
        assert agent.journal.status(operation)["result"]["data"] == data
    finally:
        if not job.done():
            agent.cancel_call(operation)
            await asyncio.gather(job, return_exceptions=True)
        await asyncio.gather(*agent.kill_tasks, return_exceptions=True)
        agent.journal.db.close()
        agent.instance_lock.close()
