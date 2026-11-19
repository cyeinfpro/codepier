"""Regressions for bounded, cancellation-safe resident LSP state."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import lsp_navigation as lsp
from agent.owned_subprocess import create_owned_process
from shared.util import DevError


@pytest.mark.asyncio
async def test_ordinary_stdio_pipe_roundtrip_and_owned_cleanup():
    if os.name == "nt":
        pytest.skip("POSIX owned process adapter")
    process = await create_owned_process(
        sys.executable,
        "-c",
        "import sys; print(sys.stdin.readline().strip()); print('stderr',file=sys.stderr)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        process.stdin.write(b"fixture\n")
        await process.stdin.drain()
        assert await asyncio.wait_for(process.stdout.readline(), 3) == b"fixture\n"
        assert await asyncio.wait_for(process.stderr.readline(), 3) == b"stderr\n"
        assert await asyncio.wait_for(process.wait(), 3) == 0
    finally:
        try:
            await process.stop_owned()
        finally:
            process.close_stdio()
    assert process.stdin.is_closing()


@pytest.mark.asyncio
async def test_cancelled_entry_cleanup_still_closes_remaining_entries():
    entered = asyncio.Event()
    finish = asyncio.Event()
    closed = []

    async def close_slow():
        entered.set()
        await finish.wait()
        closed.append("slow")

    async def close_next():
        closed.append("next")

    pool = lsp.LspPool()
    cleanup = asyncio.create_task(
        pool._close_entries(
            [
                SimpleNamespace(close=close_slow),
                SimpleNamespace(close=close_next),
            ]
        )
    )
    await asyncio.wait_for(entered.wait(), 3)
    cleanup.cancel()
    await asyncio.sleep(0)
    assert not cleanup.done()
    cleanup.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(cleanup, 3)
    assert closed == ["slow", "next"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop_name", ["stop_event", "journal", "cancelled"])
async def test_cancelled_warm_query_never_respawns(stop_name):
    pool = lsp.LspPool()
    agent = SimpleNamespace(
        processes={},
        stop_event=asyncio.Event(),
        journal=SimpleNamespace(is_cancelled=lambda _: False),
        cancelled=set(),
    )
    candidate = SimpleNamespace(process=object(), notifications=42)
    entry = SimpleNamespace(peer=candidate)

    async def disconnected(peer):
        assert peer.notifications == 0
        if stop_name == "stop_event":
            agent.stop_event.set()
        elif stop_name == "journal":
            agent.journal.is_cancelled = lambda _: True
        else:
            agent.cancelled.add("query")
        raise DevError("LSP_DISCONNECTED", "Connection closed")

    with pytest.raises(DevError) as err:
        await pool._run_peer_request(
            entry, agent, "query", {}, Path("."), {}, "spec", disconnected, True
        )
    assert err.value.code == "CANCELLED"
    assert pool.misses == 0


@pytest.mark.asyncio
async def test_diagnostics_discard_old_versions_and_bound_lru(monkeypatch):
    monkeypatch.setattr(lsp, "MAX_OPEN_DOCUMENTS", 2)
    monkeypatch.setattr(lsp, "MAX_DIAGNOSTIC_REPORTS", 1)
    peer = lsp.Peer(SimpleNamespace(), "query", {}, Path("."), {})
    events = []

    async def notify(name, params):
        events.append((name, params))

    peer.notify = notify

    async def sync(uri, sha):
        return await peer.sync_document(uri=uri, language_id="python", source="# test", sha=sha)

    def publish(uri, version):
        peer._accept_diagnostics({"uri": uri, "version": version, "diagnostics": []})

    version_a = await sync("a", "first")
    version_b = await sync("b", "second")
    publish("foreign-doc", 1)
    assert not peer.diagnostics
    publish("a", version_a)
    publish("b", version_b)
    assert list(peer.diagnostics) == ["b"]
    version_c = await sync("c", "third")
    assert list(peer.documents) == ["b", "c"]
    assert ("textDocument/didClose", {"textDocument": {"uri": "a"}}) in events
    assert version_c > version_b
    reopened_a = await sync("a", "new-a")
    publish("a", version_a)
    publish("a", None)
    assert await peer.wait_diagnostics("a", reopened_a, 0) is None
    publish("a", reopened_a)
    assert (await peer.wait_diagnostics("a", reopened_a, 0))["version"] == reopened_a


@pytest.mark.asyncio
async def test_owned_cleanup_reaches_descendant_after_stdio_leader_exits():
    if os.name == "nt":
        pytest.skip("POSIX owned process adapter")
    process = await create_owned_process(
        sys.executable,
        "-c",
        "import subprocess, sys; subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)']); print('leader ready', flush=True)",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 3) == b"leader ready\n"
        assert await asyncio.wait_for(process.wait(), 3) == 0
        # The descendant inherits stdout. EOF before forcibly closing the
        # client transport proves the same owned session has been drained.
        await process.stop_owned()
        assert await asyncio.wait_for(process.stdout.read(), 3) == b""
    finally:
        try:
            await process.stop_owned()
        finally:
            process.close_stdio()
