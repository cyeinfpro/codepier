"""Real asyncio scheduling tests, with no SSH server or browser dependencies."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from shared.ssh_terminal_channel import (
    TERMINAL_CHANNEL_PURPOSE,
    TerminalQueueClosed,
    TerminalSendQueue,
    terminal_secure_channel,
)
from shared.ssh_terminal_protocol import TerminalProtocolError

SID_A = "00112233445566778899aabbccddeeff"
SID_B = "102132435465768798a9bacbdcedfe0f"


class ChannelQueueTests(unittest.IsolatedAsyncioTestCase):
    def output(self, session_id=SID_A, value=0):
        return {"type": "terminal_output", "session_id": session_id, "payload": "eA==", "index": value}

    async def test_control_priority_has_finite_burst_and_data_round_robin(self):
        queue = TerminalSendQueue(control_burst=2)
        for index in range(4):
            await queue.put_output(self.output(SID_A, index), 1)
        for index in range(2):
            await queue.put_output(self.output(SID_B, index), 1)
        for index in range(5):
            await queue.put_control({"type": "control", "index": index})
        values = [await queue.get() for _ in range(11)]
        self.assertEqual(["control", "control", "terminal_output", "control", "control", "terminal_output", "control"], [v["type"] for v in values[:7]])
        data = [v for v in values if v["type"] == "terminal_output"]
        self.assertEqual([SID_A, SID_B, SID_A, SID_B, SID_A, SID_A], [v["session_id"] for v in data])
        self.assertEqual([0, 1, 2, 3], [v["index"] for v in data if v["session_id"] == SID_A])
        self.assertEqual((0, 0), (queue.queued_items, queue.queued_bytes))

    async def test_output_waits_for_count_budget_then_resumes(self):
        queue = TerminalSendQueue(max_items=4, max_control_items=1)
        for index in range(3):
            await queue.put_output(self.output(value=index), 1)
        pending = asyncio.create_task(queue.put_output(self.output(value=3), 1))
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        self.assertEqual(1, queue.waiting_producers)
        await queue.get()
        await asyncio.wait_for(pending, 1)
        self.assertEqual(3, queue.queued_items)
        self.assertEqual(0, queue.waiting_producers)

    async def test_exit_barrier_never_overtakes_its_last_output(self):
        queue = TerminalSendQueue()
        await queue.put_output(self.output(SID_A, 1), 1)
        await queue.put_output(self.output(SID_A, 2), 1)
        await queue.put_barrier({"type": "terminal_exit", "session_id": SID_A, "final_offset": "2"})
        await queue.put_output(self.output(SID_B, 1), 1)
        await queue.put_control({"type": "terminal_ping"})
        messages = [await queue.get() for _ in range(5)]
        self.assertEqual("terminal_ping", messages[0]["type"])
        session_messages = [message for message in messages if message.get("session_id") == SID_A]
        self.assertEqual(["terminal_output", "terminal_output", "terminal_exit"], [message["type"] for message in session_messages])
        self.assertEqual((0, 0), (queue.queued_items, queue.queued_bytes))

    async def test_byte_limit_blocks_output_but_keeps_control_admission(self):
        queue = TerminalSendQueue(max_bytes=2048)
        await queue.put_output(self.output(value=1), 500)
        waiting = asyncio.create_task(queue.put_output(self.output(value=2), 500))
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        await queue.put_control({"type": "revoke"})
        self.assertLessEqual(queue.queued_bytes, 2048)
        self.assertEqual("revoke", (await queue.get())["type"])
        self.assertFalse(waiting.done())
        await queue.get()
        await asyncio.wait_for(waiting, 1)

    async def test_try_put_full_queue_never_waits_or_changes_admission_budgets(self):
        attempts = {
            "control": lambda queue: queue.try_put_control({"type": "ping"}),
            "output": lambda queue: queue.try_put_output(self.output(), 1),
            "barrier": lambda queue: queue.try_put_barrier({"type": "terminal_exit", "session_id": SID_A}),
        }
        for limit in ("items", "bytes"):
            for kind, attempt in attempts.items():
                with self.subTest(limit=limit, kind=kind):
                    if limit == "items":
                        queue = TerminalSendQueue(max_items=2, max_control_items=1)
                        self.assertTrue(queue.try_put_output(self.output(), 1))
                        self.assertTrue(queue.try_put_control({"type": "state"}))
                    else:
                        queue = TerminalSendQueue(max_bytes=1024)
                        self.assertTrue(queue.try_put_control({"type": "state", "detail": "x" * 600}))
                    before = queue.queued_bytes, queue.queued_items, queue.waiting_bytes, queue.waiting_producers
                    for _ in range(100):
                        self.assertFalse(attempt(queue))
                        self.assertEqual(before, (queue.queued_bytes, queue.queued_items,
                                                  queue.waiting_bytes, queue.waiting_producers))
                    while queue.queued_items:
                        await queue.get()
                    self.assertTrue(attempt(queue))
                    await queue.get()
                    self.assertEqual((0, 0, 0, 0), (queue.queued_bytes, queue.queued_items,
                                                   queue.waiting_bytes, queue.waiting_producers))

    async def test_try_put_preserves_control_priority_fairness_and_exit_barrier(self):
        queue = TerminalSendQueue(control_burst=1)
        self.assertTrue(queue.try_put_output(self.output(SID_A, 1), 1))
        self.assertTrue(queue.try_put_output(self.output(SID_A, 2), 1))
        self.assertTrue(queue.try_put_barrier({"type": "terminal_exit", "session_id": SID_A}))
        self.assertTrue(queue.try_put_output(self.output(SID_B, 1), 1))
        self.assertTrue(queue.try_put_control({"type": "ping"}))
        self.assertTrue(queue.try_put_control({"type": "revoke"}))
        messages = [await queue.get() for _ in range(6)]
        self.assertEqual(["ping", "terminal_output", "revoke", "terminal_output",
                          "terminal_output", "terminal_exit"], [message["type"] for message in messages])
        self.assertEqual([SID_A, SID_B, SID_A, SID_A],
                         [message["session_id"] for message in messages if "session_id" in message])

    async def test_try_put_wakes_consumer_and_stores_immutable_snapshot(self):
        queue = TerminalSendQueue()
        waiting = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        self.assertEqual(1, queue.waiting_consumers)
        message = {"type": "state", "detail": {"name": "before"}}
        self.assertTrue(queue.try_put_control(message))
        message["detail"]["name"] = "after"
        self.assertEqual("before", (await asyncio.wait_for(waiting, 1))["detail"]["name"])
        self.assertEqual(0, queue.waiting_consumers)

    async def test_try_put_never_hides_validation_or_closed_queue_errors_as_capacity(self):
        queue = TerminalSendQueue(max_items=2, max_control_items=1)
        self.assertTrue(queue.try_put_output(self.output(), 1))
        self.assertTrue(queue.try_put_control({"type": "state"}))
        attempts = [
            lambda: queue.try_put_output({"session_id": "invalid"}, 1),
            lambda: queue.try_put_output(self.output(), 16385),
            lambda: queue.try_put_control({"data": "x" * 9000}),
            lambda: queue.try_put_barrier({"type": "exit", "session_id": "invalid"}),
            lambda: queue.try_put_barrier({"type": "exit", "session_id": SID_A, "data": "x" * 9000}),
        ]
        for attempt in attempts:
            with self.assertRaises(TerminalProtocolError):
                attempt()
        queue.close()
        for attempt in (
            lambda: queue.try_put_control({"type": "ping"}),
            lambda: queue.try_put_output(self.output(), 1),
            lambda: queue.try_put_barrier({"type": "exit", "session_id": SID_A}),
        ):
            with self.assertRaises(TerminalQueueClosed):
                attempt()
        self.assertEqual((0, 0, 0, 0), (queue.queued_bytes, queue.queued_items,
                                       queue.waiting_bytes, queue.waiting_producers))

    async def test_close_unblocks_waiting_producers_and_drops_unsent_output(self):
        queue = TerminalSendQueue(max_items=2, max_control_items=1)
        await queue.put_output(self.output(), 1)
        pending = asyncio.create_task(queue.put_output(self.output(value=1), 1))
        await asyncio.sleep(0)
        queue.close()
        with self.assertRaises(TerminalQueueClosed):
            await asyncio.wait_for(pending, 1)
        self.assertEqual((0, 0, 0), (queue.queued_items, queue.queued_bytes, queue.waiting_producers))
        with self.assertRaises(TerminalQueueClosed):
            await queue.get()
        with self.assertRaises(TerminalQueueClosed):
            await queue.put_control({"type": "revoke"})
        queue.close()

    async def test_close_unblocks_all_waiting_consumers(self):
        queue = TerminalSendQueue()
        pending = [asyncio.create_task(queue.get()) for _ in range(3)]
        await asyncio.sleep(0)
        self.assertEqual(3, queue.waiting_consumers)
        queue.close()
        results = await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 1)
        self.assertTrue(all(isinstance(result, TerminalQueueClosed) for result in results))
        self.assertEqual(0, queue.waiting_consumers)

    async def test_cancelled_waiting_producer_does_not_enqueue_or_leak_slots(self):
        queue = TerminalSendQueue(max_items=2, max_control_items=1)
        await queue.put_output(self.output(value=1), 1)
        pending = asyncio.create_task(queue.put_output(self.output(value=2), 1))
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(0, queue.waiting_producers)
        self.assertEqual(1, (await queue.get())["index"])
        self.assertEqual(0, queue.queued_items)

    async def test_cancelled_consumer_does_not_consume_future_data(self):
        queue = TerminalSendQueue()
        pending = asyncio.create_task(queue.get())
        await asyncio.sleep(0)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await queue.put_control({"type": "revoke"})
        self.assertEqual("revoke", (await queue.get())["type"])

    async def test_waiter_limit_prevents_unbounded_pending_tasks(self):
        queue = TerminalSendQueue(max_items=2, max_control_items=1, max_waiters=1)
        await queue.put_output(self.output(), 1)
        pending = asyncio.create_task(queue.put_output(self.output(value=1), 1))
        await asyncio.sleep(0)
        with self.assertRaises(TerminalProtocolError) as caught:
            await queue.put_output(self.output(value=2), 1)
        self.assertEqual("TERMINAL_QUEUE_SATURATED", caught.exception.code)
        queue.close()
        with self.assertRaises(TerminalQueueClosed):
            await pending

    async def test_enqueued_messages_are_immutable_snapshots(self):
        queue = TerminalSendQueue()
        message = {"type": "state", "details": {"name": "before"}}
        await queue.put_control(message)
        message["details"]["name"] = "after"
        self.assertEqual("before", (await queue.get())["details"]["name"])

    async def test_oversized_or_invalid_envelopes_never_enter_queue(self):
        queue = TerminalSendQueue()
        for message, size in [
            ({"session_id": "invalid", "data": "x"}, 1),
            (self.output(), 16385),
            ({**self.output(), "data": "x" * 50000}, 1),
            ({**self.output(), "data": float("nan")}, 1),
        ]:
            with self.assertRaises(TerminalProtocolError):
                await queue.put_output(message, size)
        with self.assertRaises(TerminalProtocolError):
            await queue.put_control({"data": "x" * 9000})
        self.assertEqual((0, 0), (queue.queued_items, queue.queued_bytes))


class TerminalPurposeTests(unittest.TestCase):
    def test_uses_existing_cipher_with_separate_device_namespace(self):
        # The project module is imported only inside the factory.  This test
        # checks domain separation without implementing substitute cryptography.
        with patch("shared.crypto.SecureChannel") as constructor:
            result = terminal_secure_channel("s" * 48, "c" * 40, "device-id", "hub")
            constructor.assert_called_once_with("s" * 48, "c" * 40, "device-id" + TERMINAL_CHANNEL_PURPOSE, "hub")
            self.assertIs(result, constructor.return_value)

    def test_real_cipher_rejects_main_channel_and_other_handshake_packets(self):
        from cryptography.exceptions import InvalidTag

        from shared.crypto import SecureChannel

        secret, challenge, device = "s" * 48, "c" * 40, "device-id"
        hub = terminal_secure_channel(secret, challenge, device, "hub")
        agent = terminal_secure_channel(secret, challenge, device, "agent")
        packet = hub.pack({"type": "terminal_ping"})
        self.assertEqual({"type": "terminal_ping"}, agent.unpack(packet))
        ordinary_agent = SecureChannel(secret, challenge, device, "agent")
        fresh_agent = terminal_secure_channel(secret, "n" * 40, device, "agent")
        for peer in (ordinary_agent, fresh_agent):
            with self.subTest(peer=peer), self.assertRaises(InvalidTag):
                peer.unpack(packet)
        with self.assertRaises(ValueError):
            agent.unpack(packet)

    def test_rejects_invalid_handshake_inputs_before_creating_cipher(self):
        for secret, challenge, device, role in [
            ("short", "c" * 40, "device", "hub"),
            ("s" * 48, "short", "device", "hub"),
            ("s" * 48, "c" * 40, "device/vps-terminal/v1", "hub"),
            ("s" * 48, "c" * 40, "device", "browser"),
        ]:
            with self.assertRaises(ValueError):
                terminal_secure_channel(secret, challenge, device, role)


if __name__ == "__main__":
    unittest.main()
