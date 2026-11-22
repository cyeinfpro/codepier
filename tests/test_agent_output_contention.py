"""Exercise stdout receipt contention with an actual child and SQLite writer."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from agent.journal import output_journal_busy
from agent.runner import Agent
from shared.crypto import token
from shared.util import DevError, atomic_json


class OutputContentionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.project = root / "project"
        self.project.mkdir()
        config = root / "config.json"
        state = root / "state"
        atomic_json(
            config,
            {
                "hub_url": "http://127.0.0.1:9",
                "device_id": "fixture",
                "secret": token(),
                "state_dir": str(state),
                "allowed_roots": [
                    {"path": str(self.project), "writable": True, "allow_tasks": True}
                ],
                "tasks": {},
            },
        )
        self.agent = Agent(config)
        self.addCleanup(self.agent.instance_lock.close)
        self.addCleanup(self.agent.journal.db.close)
        self.request = {"tool": "tasks_run"}
        self.agent.journal.start("once", self.request)
        self.agent.journal.mark_running_db("once")
        self.connection = sqlite3.connect(state / "agent.sqlite3")
        self.addCleanup(self.connection.close)
        self.connection.execute("BEGIN IMMEDIATE")
        self.blocked = asyncio.Event()

    async def asyncSetUp(self):
        loop = asyncio.get_running_loop()
        update = self.agent.journal.update_output
        self.snapshots = []

        def record(identifier, output, sequence):
            try:
                update(identifier, output, sequence)
            except sqlite3.OperationalError as exc:
                if output_journal_busy(exc):
                    loop.call_soon_threadsafe(self.blocked.set)
                    self.snapshots.append((sequence, "busy"))
                else:
                    self.snapshots.append((sequence, "failed"))
                raise
            self.snapshots.append((sequence, "saved"))

        self.agent.journal.update_output = record

    async def run_child(self, trailer=""):
        child = (
            "from pathlib import Path;"
            "p=Path('marker'); p.write_text(p.read_text()+'once' if p.exists() else 'once');"
            "print('command-output', flush=True);" + trailer
        )
        return await self.agent.run_process(
            "once", [sys.executable, "-u", "-c", child], self.project, 5, stream=True
        )

    async def test_transient_lock_coalesces_progress_and_saves_final_tail(self):
        trailer = (
            "import time;\n"
            "while not Path('continue').exists(): time.sleep(0.01)\n"
            "print('command-finished', flush=True)"
        )
        work = asyncio.create_task(self.run_child(trailer))
        try:
            await asyncio.wait_for(self.blocked.wait(), 3)
            self.assertEqual(self.agent.journal.active_status("once")["output_seq"], 0)
            self.assertIn("once", self.agent.processes)
            self.connection.rollback()
            (self.project / "continue").touch()
            result = await asyncio.wait_for(work, 3)
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("command-output", result["output"])
        self.assertIn("command-finished", result["output"])
        durable = self.connection.execute(
            "SELECT output,output_seq FROM calls WHERE id='once'"
        ).fetchone()
        self.assertEqual(durable, (result["output"], self.snapshots[-1][0]))
        self.assertEqual(self.agent.journal.db.execute("PRAGMA busy_timeout").fetchone()[0], 30000)
        self.assertEqual((self.project / "marker").read_text(), "once")

    async def test_persistent_lock_fails_explicitly_without_replaying(self):
        with self.assertRaises(DevError) as raised:
            # Leave subprocess cleanup headroom on busy CI hosts, but still
            # require settlement well before SQLite's normal 30-second wait.
            await asyncio.wait_for(self.run_child(), 8)
        self.assertEqual(raised.exception.code, "JOURNAL_OUTPUT_BUSY")
        self.assertEqual([status for _, status in self.snapshots], ["busy"] * 5)
        self.assertFalse(self.agent.processes)
        self.connection.rollback()
        with self.assertRaises(DevError) as duplicate:
            self.agent.journal.start("once", self.request)
        self.assertEqual(duplicate.exception.code, "ALREADY_RUNNING")
        self.assertEqual((self.project / "marker").read_text(), "once")

    async def test_noncontention_sqlite_error_still_fails_without_retry(self):
        self.connection.rollback()
        self.agent.journal.db.execute("PRAGMA query_only=1")
        with self.assertRaises(sqlite3.OperationalError) as raised:
            await asyncio.wait_for(self.run_child(), 3)
        self.assertEqual(raised.exception.sqlite_errorcode, sqlite3.SQLITE_READONLY)
        self.assertEqual([status for _, status in self.snapshots], ["failed"])
        self.assertFalse(self.agent.processes)

    async def test_cancellation_during_lock_keeps_process_cleanup(self):
        work = asyncio.create_task(self.run_child("import time; time.sleep(30)"))
        await asyncio.wait_for(self.blocked.wait(), 3)
        work.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(work, 3)
        self.assertFalse(self.agent.processes)
        self.assertEqual((self.project / "marker").read_text(), "once")
