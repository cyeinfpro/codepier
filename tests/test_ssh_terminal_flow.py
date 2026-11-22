"""Deterministic replay, admission and lease failure tests using stdlib only."""

from __future__ import annotations

import random
import unittest

from shared.ssh_terminal_flow import (
    AuthorityLease,
    ByteBudget,
    ByteRingBuffer,
    HistoryGap,
    InputSequenceTracker,
    OutputCreditWindow,
)
from shared.ssh_terminal_protocol import UINT64_MAX, TerminalProtocolError

GEN = "00112233445566778899aabbccddeeff"
ATTACHMENT = "102132435465768798a9bacbdcedfe0f"
OTHER = "ffeeddccbbaa99887766554433221100"


class FlowTests(unittest.TestCase):
    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(TerminalProtocolError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def test_ring_wrap_eviction_gap_and_partial_read(self):
        ring = ByteRingBuffer(8)
        self.assertEqual((0, 5), ring.append(b"abcde"))
        self.assertEqual((5, 11), ring.append(b"fghijk"))
        self.assertEqual(3, ring.start_offset)
        self.assertEqual(b"defghijk", ring.read(3))
        self.assertEqual(b"gh", ring.read(6, 2))
        self.assertEqual(b"", ring.read(11))
        with self.assertRaises(HistoryGap) as caught:
            ring.read(2)
        self.assertEqual((2, 3, 11), (caught.exception.requested_offset, caught.exception.start_offset, caught.exception.end_offset))
        self.assertCode("OUTPUT_OFFSET_AHEAD", ring.read, 12)

    def test_append_larger_than_capacity_keeps_only_tail(self):
        ring = ByteRingBuffer(4)
        ring.append(b"a")
        self.assertEqual((1, 13), ring.append(b"bcdefghijklm"))
        self.assertEqual(9, ring.start_offset)
        self.assertEqual(b"jklm", ring.read(9))
        self.assertEqual(4, len(ring))

    def test_incremental_unicode_and_ansi_bytes_remain_exact(self):
        data = "中🙂e\u0301".encode() + b"\x1b[?1049h\x1b[32m"
        ring = ByteRingBuffer(len(data))
        for byte in data:
            ring.append(bytes([byte]))
        self.assertEqual(data, ring.read(0))

    def test_ring_matches_reference_over_thousands_of_random_chunks(self):
        rng = random.Random(20261122)
        ring = ByteRingBuffer(97)
        reference = b""
        for _ in range(3000):
            data = rng.randbytes(rng.randrange(0, 200))
            before = len(reference)
            reference += data
            self.assertEqual((before, len(reference)), ring.append(data))
            start = max(0, len(reference) - 97)
            self.assertEqual(start, ring.start_offset)
            self.assertEqual(reference[start:], ring.read(start))
            offset = rng.randrange(start, len(reference) + 1)
            limit = rng.randrange(0, 120)
            self.assertEqual(reference[offset:offset + limit], ring.read(offset, limit))

    def test_many_small_appends_have_fixed_storage_not_one_object_per_byte(self):
        ring = ByteRingBuffer(1024)
        storage_id = id(ring._storage)
        for _ in range(10000):
            ring.append(b"x")
        self.assertEqual(storage_id, id(ring._storage))
        self.assertEqual(1024, len(ring._storage))
        self.assertEqual(1024, ring.size)
        self.assertEqual(b"x" * 1024, ring.read(8976))

    def test_ring_clear_does_not_reuse_stream_offsets(self):
        ring = ByteRingBuffer(8)
        ring.append(b"password")
        ring.clear()
        self.assertEqual((8, 8, 0), (ring.start_offset, ring.end_offset, ring.size))
        self.assertEqual((8, 9), ring.append(b"x"))
        self.assertEqual(b"x", ring.read(8))
        self.assertEqual(0, ring._storage[1])

    def test_ring_offset_overflow_does_not_mutate(self):
        ring = ByteRingBuffer(8, UINT64_MAX - 1)
        ring.append(b"x")
        self.assertCode("INVALID_COUNTER", ring.append, b"y")
        self.assertEqual(UINT64_MAX, ring.end_offset)
        self.assertEqual(b"x", ring.read(UINT64_MAX - 1))

    def window(self, **changes):
        args = {"attachment_id": ATTACHMENT, "render_epoch": 1, "stream_epoch": 2, "start_offset": 100, "high_water_bytes": 8, "low_water_bytes": 2}
        args.update(changes)
        return OutputCreditWindow(**args)

    def ack(self, window, offset, **changes):
        args = {"attachment_id": ATTACHMENT, "render_epoch": 1, "stream_epoch": 2}
        args.update(changes)
        return window.acknowledge(offset, **args)

    def test_output_credit_and_low_watermark_hysteresis(self):
        window = self.window()
        window.record_sent(100, 8)
        self.assertEqual((8, 0, True), (window.in_flight, window.available_bytes, window.is_paused))
        self.assertCode("OUTPUT_BACKPRESSURE", window.record_sent, 108, 1)
        self.ack(window, 103)
        self.assertTrue(window.is_paused)
        self.ack(window, 106)
        self.assertFalse(window.is_paused)
        self.assertEqual(6, window.available_bytes)

    def test_bad_ack_cannot_release_credit_or_move_offsets(self):
        window = self.window()
        window.record_sent(100, 8)
        for offset in [99, 109, UINT64_MAX]:
            self.assertCode("ACK_OUT_OF_RANGE", self.ack, window, offset)
            self.assertEqual((100, 108, 8), (window.acked_offset, window.sent_offset, window.in_flight))

    def test_stale_attachment_and_epoch_acks_are_ignored(self):
        window = self.window()
        window.record_sent(100, 8)
        for changes in [{"attachment_id": OTHER}, {"render_epoch": 0}, {"stream_epoch": 1}]:
            self.assertFalse(self.ack(window, 108, **changes))
        self.assertEqual(8, window.in_flight)

    def test_new_renderer_replays_same_stream_from_zero_independently(self):
        old = self.window(start_offset=1000)
        old.record_sent(1000, 8)
        self.ack(old, 1008)
        new = self.window(start_offset=0, render_epoch=2)
        new.record_sent(0, 8)
        self.assertFalse(self.ack(new, 1008))
        self.assertTrue(self.ack(new, 8, render_epoch=2))
        self.assertEqual(8, new.acked_offset)

    def test_output_send_is_contiguous_and_overflow_rejected(self):
        window = self.window()
        self.assertCode("OUTPUT_GAP_OR_OVERLAP", window.record_sent, 101, 1)
        window.record_sent(100, 1)
        self.assertCode("OUTPUT_GAP_OR_OVERLAP", window.record_sent, 100, 1)
        self.assertEqual(101, window.sent_offset)

    def tracker(self, **changes):
        args = {"runtime_generation": GEN, "stream_epoch": 2, "writer_epoch": 3, "receipt_limit": 4}
        args.update(changes)
        return InputSequenceTracker(**args)

    def claim(self, tracker, seq, **changes):
        args = {"runtime_generation": GEN, "stream_epoch": 2, "writer_epoch": 3}
        args.update(changes)
        return tracker.claim(seq, **args)

    def test_lost_input_ack_does_not_repeat_the_write(self):
        tracker = self.tracker()
        writes = []
        for seq, payload in [(1, b"touch /tmp/example\r"), (1, b"touch /tmp/example\r"), (2, b"pwd\r")]:
            receipt = self.claim(tracker, seq)
            if receipt.is_new:
                writes.append(payload)
                tracker.mark_forwarded(seq)
        self.assertEqual([b"touch /tmp/example\r", b"pwd\r"], writes)
        self.assertEqual("forwarded", self.claim(tracker, 1).status)

    def test_input_gap_is_not_implicitly_skipped(self):
        tracker = self.tracker()
        self.assertCode("INPUT_GAP", self.claim, tracker, 2)
        self.assertEqual(1, tracker.next_input_seq)
        self.assertTrue(self.claim(tracker, 1).is_new)

    def test_uncertain_input_freezes_new_data_but_preserves_receipt(self):
        tracker = self.tracker()
        self.claim(tracker, 1)
        tracker.mark_uncertain(1)
        self.assertEqual("uncertain", self.claim(tracker, 1).status)
        self.assertFalse(self.claim(tracker, 1).is_new)
        self.assertCode("INPUT_UNCERTAIN", self.claim, tracker, 2)
        self.assertCode("INPUT_UNCERTAIN", tracker.mark_forwarded, 1)

    def test_stale_writer_stream_or_generation_never_claims(self):
        tracker = self.tracker()
        for changes, code in [({"runtime_generation": OTHER}, "STALE_GENERATION"), ({"stream_epoch": 1}, "STALE_STREAM"), ({"writer_epoch": 2}, "STALE_WRITER")]:
            self.assertCode(code, self.claim, tracker, 1, **changes)
        self.assertEqual(0, tracker.last_claimed_input_seq)

    def test_receipts_are_bounded_and_evicted_input_is_still_not_replayed(self):
        tracker = self.tracker(receipt_limit=2)
        for seq in range(1, 100):
            self.claim(tracker, seq)
            tracker.mark_forwarded(seq)
        self.assertEqual(2, tracker.receipt_count)
        old = self.claim(tracker, 1)
        self.assertEqual("already_claimed", old.status)
        self.assertFalse(old.is_new)
        self.assertEqual(100, tracker.next_input_seq)

    def test_budget_overcommit_and_underflow_do_not_corrupt_accounting(self):
        budget = ByteBudget(100)
        budget.reserve(80)
        self.assertCode("BUFFER_LIMIT", budget.reserve, 21)
        self.assertEqual(80, budget.used_bytes)
        self.assertCode("BUFFER_ACCOUNTING", budget.release, 81)
        self.assertEqual(80, budget.used_bytes)
        budget.release(80)
        self.assertEqual(100, budget.available_bytes)


class LeaseTests(unittest.TestCase):
    def test_monotonic_expiry_does_not_depend_on_browser_attachment(self):
        now = [100.0]
        lease = AuthorityLease("revision-1", clock=lambda: now[0])
        self.assertFalse(lease.valid())
        lease.renew(30, "revision-1")
        now[0] = 129.9
        self.assertTrue(lease.valid())
        now[0] = 130
        self.assertFalse(lease.valid())
        with self.assertRaises(TerminalProtocolError) as caught:
            lease.require_valid()
        self.assertEqual("AUTHORITY_LEASE_EXPIRED", caught.exception.code)

    def test_revision_change_or_excessive_ttl_cannot_extend_lease(self):
        lease = AuthorityLease("revision-1", clock=lambda: 100)
        lease.renew(10, "revision-1")
        for duration, revision in [(31, "revision-1"), (30, "revision-2")]:
            with self.assertRaises(TerminalProtocolError):
                lease.renew(duration, revision)
            self.assertEqual(110, lease.expires_at)

    def test_freeze_can_be_renewed_but_revocation_is_final(self):
        lease = AuthorityLease("revision-1", clock=lambda: 100)
        lease.renew(30, "revision-1")
        lease.freeze()
        self.assertFalse(lease.valid())
        lease.renew(30, "revision-1")
        self.assertTrue(lease.valid())
        lease.revoke()
        self.assertFalse(lease.valid())
        with self.assertRaises(TerminalProtocolError) as caught:
            lease.renew(30, "revision-1")
        self.assertEqual("AUTH_REVOKED", caught.exception.code)

    def test_invalid_lease_values_do_not_create_authority(self):
        for duration in [0, -1, float("nan"), float("inf"), True, 121]:
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                AuthorityLease("r", max_ttl_seconds=duration)


if __name__ == "__main__":
    unittest.main()
