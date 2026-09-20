from __future__ import annotations

import asyncio
import json
import sys

import pytest

from agent.computer_appserver import AppServerClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {}, "required": "secret"},
        {"type": "object", "properties": {}, "required": {}},
        {"type": "object", "properties": {}, "$schema": {"url": "private"}},
        {"type": "object", "properties": {}, "unexpected": "input"},
    ],
)
async def test_callback_schema_must_be_the_empty_application_form(tmp_path, schema):
    script = tmp_path / "schema_callback.py"
    script.write_text(
        """import json, sys
print("FIXTURE_READY", flush=True)
request = json.loads(sys.stdin.readline())
print(json.dumps({'id': 'callback', 'method': 'mcpServer/elicitation/request',
  'params': {'threadId': 'thread', 'serverName': 'codepier_computer', 'mode': 'form',
             'message': 'Allow Fixture?', 'requestedSchema': SCHEMA}}), flush=True)
callback = json.loads(sys.stdin.readline())
print(json.dumps({'id': request['id'], 'result': {'callback': callback}}), flush=True)
""".replace("SCHEMA", json.dumps(schema))
    )
    client = AppServerClient({}, timeout=0.5)
    client.thread_id = "thread"
    client.approval_timeout_seconds = 0.1
    client.process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    calls = []

    async def approve(message):
        calls.append(message)
        return {"action": "accept"}

    client.approval_handler = approve
    try:
        # A protocol timeout measures the RPC, not interpreter cold startup.
        ready = await asyncio.wait_for(client.process.stdout.readline(), 10)
        assert ready == b"FIXTURE_READY\n"
        result = await client.request("mcpServer/tool/call", {})
        assert result["callback"]["result"] == {"action": "cancel"}
        assert calls == []
    finally:
        await client.close()
