"""Bounded, transport-independent terminal flow primitives.

These classes do no I/O and grant no authority.  A session owner must serialize
mutations (normally on one event loop); no method waits or holds an async lock.
Offsets always count original terminal bytes, never encoded envelope bytes.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from .ssh_terminal_protocol import (
    TerminalProtocolError,
    canonical_uuid,
    checked_uint64,
)


def _positive_size(value: int, name: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


class HistoryGap(TerminalProtocolError):
    def __init__(self, requested_offset: int, start_offset: int, end_offset: int):
        self.requested_offset = requested_offset
        self.start_offset = start_offset
        self.end_offset = end_offset
        super().__init__("HISTORY_GAP", "Requested terminal output has left the bounded history")


class ByteRingBuffer:
    """Fixed allocation ring; even one-byte appends cannot grow object count.

    append() intentionally evicts the oldest bytes.  The session manager must
    apply live-session backpressure *before* it overwrites unconsumed output;
    detached draining may evict and must expose HistoryGap on recovery.
    """

    def __init__(self, capacity_bytes: int, initial_offset: int = 0):
        self.capacity_bytes = _positive_size(capacity_bytes, "capacity_bytes")
        if self.capacity_bytes > 64 * 1024 * 1024:
            raise ValueError("Ring capacity exceeds the supported per-session bound")
        self.start_offset = checked_uint64(initial_offset)
        self.end_offset = self.start_offset
        self._storage = bytearray(self.capacity_bytes)

    @property
    def size(self) -> int:
        return self.end_offset - self.start_offset

    def __len__(self) -> int:
        return self.size

    def append(self, data: bytes | bytearray | memoryview) -> tuple[int, int]:
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError("Ring accepts bytes only")
        view = memoryview(data)
        if view.ndim != 1 or view.itemsize != 1 or not view.c_contiguous:
            raise TypeError("Ring requires a contiguous byte buffer")
        start = self.end_offset
        end = checked_uint64(start + len(view))
        if not view:
            return start, end
        retained = min(len(view), self.capacity_bytes)
        tail = view[-retained:]
        index = (end - retained) % self.capacity_bytes
        first = min(retained, self.capacity_bytes - index)
        self._storage[index:index + first] = tail[:first]
        if first < retained:
            self._storage[:retained - first] = tail[first:]
        self.end_offset = end
        self.start_offset = max(self.start_offset, end - self.capacity_bytes)
        return start, end

    def read(self, offset: int, max_bytes: int | None = None) -> bytes:
        offset = checked_uint64(offset)
        limit = self.capacity_bytes if max_bytes is None else _positive_size(max_bytes, "max_bytes", allow_zero=True)
        if offset < self.start_offset:
            raise HistoryGap(offset, self.start_offset, self.end_offset)
        if offset > self.end_offset:
            raise TerminalProtocolError("OUTPUT_OFFSET_AHEAD", "Requested offset is ahead of received output")
        length = min(limit, self.end_offset - offset)
        if length == 0:
            return b""
        index = offset % self.capacity_bytes
        first = min(length, self.capacity_bytes - index)
        result = bytes(self._storage[index:index + first])
        if first < length:
            result += bytes(self._storage[:length - first])
        return result

    def clear(self) -> None:
        """Drop retained history without reusing offsets in the same stream."""
        self._storage[:] = b"\x00" * self.capacity_bytes
        self.start_offset = self.end_offset


class OutputCreditWindow:
    """Browser-consumption credit belonging to one immutable attachment.

    New renderers get new windows, even when replaying the same stream from 0.
    Stale attachment ACKs return False; impossible ACKs raise without mutation.
    """

    def __init__(
        self, attachment_id: str, render_epoch: int, stream_epoch: int,
        start_offset: int = 0, high_water_bytes: int = 256 * 1024,
        low_water_bytes: int = 64 * 1024,
    ):
        self.attachment_id = canonical_uuid(attachment_id)
        self.render_epoch = checked_uint64(render_epoch)
        self.stream_epoch = checked_uint64(stream_epoch)
        self.high_water_bytes = _positive_size(high_water_bytes, "high_water_bytes")
        self.low_water_bytes = _positive_size(low_water_bytes, "low_water_bytes", allow_zero=True)
        if self.low_water_bytes >= self.high_water_bytes:
            raise ValueError("Low output watermark must be below high watermark")
        self.acked_offset = checked_uint64(start_offset)
        self.sent_offset = self.acked_offset
        self._paused = False

    @property
    def highest_sent_offset(self) -> int:
        return self.sent_offset

    @property
    def acknowledged_offset(self) -> int:
        return self.acked_offset

    @property
    def in_flight(self) -> int:
        return self.sent_offset - self.acked_offset

    @property
    def available_bytes(self) -> int:
        return self.high_water_bytes - self.in_flight

    @property
    def is_paused(self) -> bool:
        return self._paused

    def record_sent(self, offset: int, count: int) -> None:
        offset = checked_uint64(offset)
        _positive_size(count, "count")
        end = checked_uint64(offset + count)
        if offset != self.sent_offset:
            raise TerminalProtocolError("OUTPUT_GAP_OR_OVERLAP", "Output send offsets must be contiguous")
        if count > self.available_bytes:
            raise TerminalProtocolError("OUTPUT_BACKPRESSURE", "Output exceeds the available consumption credit")
        self.sent_offset = end
        if self.in_flight >= self.high_water_bytes:
            self._paused = True

    def acknowledge(
        self, offset: int, *, attachment_id: str, render_epoch: int, stream_epoch: int,
    ) -> bool:
        attachment_id = canonical_uuid(attachment_id)
        checked_uint64(render_epoch)
        checked_uint64(stream_epoch)
        if (attachment_id, render_epoch, stream_epoch) != (
            self.attachment_id, self.render_epoch, self.stream_epoch,
        ):
            return False
        offset = checked_uint64(offset)
        if not self.acked_offset <= offset <= self.sent_offset:
            raise TerminalProtocolError("ACK_OUT_OF_RANGE", "Output ACK is outside the sent range")
        self.acked_offset = offset
        if self.in_flight <= self.low_water_bytes:
            self._paused = False
        return True


@dataclass(frozen=True)
class InputReceipt:
    sequence: int
    status: str
    is_new: bool = False

    @property
    def seq(self) -> int:
        return self.sequence


class InputSequenceTracker:
    """Claim before channel.write(), without retaining any secret input bytes.

    This is at-most-once admission within one Agent incarnation, not proof that
    the remote program executed an input.  Uncertain writes freeze new input.
    """

    def __init__(
        self, runtime_generation: str, stream_epoch: int, writer_epoch: int,
        receipt_limit: int = 256,
    ):
        self.runtime_generation = canonical_uuid(runtime_generation)
        self.stream_epoch = checked_uint64(stream_epoch)
        self.writer_epoch = checked_uint64(writer_epoch)
        self.receipt_limit = _positive_size(receipt_limit, "receipt_limit")
        if self.receipt_limit > 8192:
            raise ValueError("Input receipt retention exceeds its supported bound")
        self.last_claimed_input_seq = 0
        self.frozen = False
        self._receipts: OrderedDict[int, str] = OrderedDict()

    @property
    def next_input_seq(self) -> int:
        return checked_uint64(self.last_claimed_input_seq + 1)

    @property
    def receipt_count(self) -> int:
        return len(self._receipts)

    def _require_binding(self, generation: str, stream_epoch: int, writer_epoch: int) -> None:
        if canonical_uuid(generation) != self.runtime_generation:
            raise TerminalProtocolError("STALE_GENERATION", "Input belongs to another Agent generation")
        if checked_uint64(stream_epoch) != self.stream_epoch:
            raise TerminalProtocolError("STALE_STREAM", "Input belongs to another terminal stream")
        if checked_uint64(writer_epoch) != self.writer_epoch:
            raise TerminalProtocolError("STALE_WRITER", "Input belongs to another writer")

    def claim(
        self, sequence: int, *, runtime_generation: str, stream_epoch: int, writer_epoch: int,
    ) -> InputReceipt:
        self._require_binding(runtime_generation, stream_epoch, writer_epoch)
        sequence = checked_uint64(sequence, positive=True)
        if sequence <= self.last_claimed_input_seq:
            return InputReceipt(sequence, self._receipts.get(sequence, "already_claimed"))
        if self.frozen:
            raise TerminalProtocolError("INPUT_UNCERTAIN", "New input is frozen after an uncertain write")
        if sequence != self.last_claimed_input_seq + 1:
            raise TerminalProtocolError("INPUT_GAP", "Input sequence contains a gap")
        self.last_claimed_input_seq = sequence
        self._receipts[sequence] = "claimed"
        while len(self._receipts) > self.receipt_limit:
            self._receipts.popitem(last=False)
        return InputReceipt(sequence, "claimed", is_new=True)

    def mark_forwarded(self, sequence: int) -> InputReceipt:
        sequence = checked_uint64(sequence, positive=True)
        if sequence not in self._receipts:
            raise TerminalProtocolError("INPUT_RECEIPT_MISSING", "Input has no retained claim receipt")
        if self._receipts[sequence] == "uncertain":
            raise TerminalProtocolError("INPUT_UNCERTAIN", "An uncertain input cannot be silently marked forwarded")
        self._receipts[sequence] = "forwarded"
        return InputReceipt(sequence, "forwarded")

    def mark_uncertain(self, sequence: int) -> InputReceipt:
        sequence = checked_uint64(sequence, positive=True)
        if sequence not in self._receipts:
            raise TerminalProtocolError("INPUT_RECEIPT_MISSING", "Input has no retained claim receipt")
        self._receipts[sequence] = "uncertain"
        self.frozen = True
        return InputReceipt(sequence, "uncertain")

    def freeze(self) -> None:
        self.frozen = True


class ByteBudget:
    """Explicit admission accounting; users still include library overhead."""

    def __init__(self, limit_bytes: int):
        self.limit_bytes = _positive_size(limit_bytes, "limit_bytes")
        self.used_bytes = 0

    @property
    def available_bytes(self) -> int:
        return self.limit_bytes - self.used_bytes

    def reserve(self, count: int) -> None:
        _positive_size(count, "count", allow_zero=True)
        if count > self.available_bytes:
            raise TerminalProtocolError("BUFFER_LIMIT", "Terminal buffer budget is exhausted")
        self.used_bytes += count

    def release(self, count: int) -> None:
        _positive_size(count, "count", allow_zero=True)
        if count > self.used_bytes:
            raise TerminalProtocolError("BUFFER_ACCOUNTING", "Terminal buffer release exceeds its reservation")
        self.used_bytes -= count


class AuthorityLease:
    """Monotonic bounded lease, separate from detached resource retention.

    renew() is called only after external current-authorization checks.  Revoked
    leases cannot be revived; a new authorization needs a newly owned lease.
    """

    def __init__(
        self, authority_revision: str, *, max_ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not isinstance(authority_revision, str) or not 0 < len(authority_revision) <= 256:
            raise ValueError("authority_revision must be a bounded non-empty string")
        self._validate_duration(max_ttl_seconds)
        self.authority_revision = authority_revision
        self.max_ttl_seconds = max_ttl_seconds
        self._clock = clock
        self.expires_at = 0.0
        self.revoked = False
        self.frozen = False
        self._started = False

    @staticmethod
    def _validate_duration(value: float) -> None:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 120 or not math.isfinite(value):
            raise ValueError("Lease duration must be finite and between zero and 120 seconds")

    def _now(self, now: float | None) -> float:
        value = self._clock() if now is None else now
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("A finite monotonic clock value is required")
        return value

    def renew(self, ttl_seconds: float, authority_revision: str, now: float | None = None) -> None:
        self._validate_duration(ttl_seconds)
        if ttl_seconds > self.max_ttl_seconds:
            raise TerminalProtocolError("LEASE_TOO_LONG", "Authority lease exceeds its negotiated lifetime")
        if authority_revision != self.authority_revision:
            raise TerminalProtocolError("AUTHORITY_CHANGED", "Authority revision no longer matches")
        if self.revoked:
            raise TerminalProtocolError("AUTH_REVOKED", "Authority lease was revoked")
        self.expires_at = self._now(now) + ttl_seconds
        self._started = True
        self.frozen = False

    def valid(self, now: float | None = None) -> bool:
        return self._started and not self.revoked and not self.frozen and self._now(now) < self.expires_at

    def require_valid(self, now: float | None = None) -> None:
        if not self.valid(now):
            code = "AUTH_REVOKED" if self.revoked else "AUTHORITY_LEASE_EXPIRED"
            raise TerminalProtocolError(code, "Terminal has no currently valid authority lease")

    def freeze(self) -> None:
        self.frozen = True

    def revoke(self) -> None:
        self.revoked = True
        self.frozen = True
