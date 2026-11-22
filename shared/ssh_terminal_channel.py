"""Bounded scheduling for the independent encrypted Agent terminal socket.

The queue is owned by one asyncio event loop.  The caller awaits each actual
socket send and configures the WebSocket implementation's own queue limits.
It never schedules unbounded create_task(send(...)) calls.  On disconnect,
queued envelopes are discarded; recovery uses the Agent's original byte ring.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from dataclasses import dataclass
from typing import Any

from .ssh_terminal_config import DEFAULT_CONFIG, TerminalConfig
from .ssh_terminal_protocol import TerminalProtocolError, canonical_uuid

TERMINAL_CHANNEL_PURPOSE = "/vps-terminal/v1"
_RECORD_OVERHEAD = 256


class TerminalQueueClosed(TerminalProtocolError):
    def __init__(self):
        super().__init__("TERMINAL_CHANNEL_CLOSED", "Terminal send queue is closed")


@dataclass(frozen=True)
class _Record:
    # Keeping one immutable JSON representation avoids retaining mutable caller
    # dictionaries (or a second parsed copy) while the record is queued.
    encoded: str
    cost: int


def _integer(value: int, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    return value


class TerminalSendQueue:
    """Control-priority, round-robin output with count and byte admission.

    Byte accounting covers serialized UTF-8, one raw-payload allowance and a
    fixed per-record allowance.  It is an admission budget, not a measurement
    of Python RSS.  Waiting producer records have a separate max_bytes budget,
    so queued plus waiting accounted memory never exceeds 2 * max_bytes.
    Pending producers are also count-bounded; the connection
    owner should have at most one waiting output producer per live session.
    The consumer may hold one dequeued frame while awaiting socket.send().
    """

    def __init__(
        self,
        config: TerminalConfig | None = None,
        *,
        max_bytes: int | None = None,
        max_items: int = 256,
        max_control_items: int = 64,
        control_burst: int = 8,
        max_waiters: int = 128,
    ):
        self.config = config or DEFAULT_CONFIG
        self.max_bytes = _integer(
            self.config.hub_relay_bytes if max_bytes is None else max_bytes, "max_bytes", 512
        )
        self.max_items = _integer(max_items, "max_items", 2)
        self.max_control_items = min(
            _integer(max_control_items, "max_control_items"), self.max_items
        )
        self.control_burst = _integer(control_burst, "control_burst")
        self.max_waiters = _integer(max_waiters, "max_waiters")
        if self.max_bytes > 64 * 1024 * 1024 or self.max_items > 4096 or self.max_waiters > 4096:
            raise ValueError("Terminal queue bounds exceed their supported limits")
        self._control_reserve = min(self.max_bytes // 4, self.config.control_bytes * 4)
        self._data_capacity = self.max_bytes - self._control_reserve
        self._data_item_limit = self.max_items - min(
            self.max_control_items, max(1, self.max_items // 4)
        )
        self._control: deque[_Record] = deque()
        self._output: dict[str, deque[_Record]] = {}
        self._round_robin: deque[str] = deque()
        self._queued_bytes = 0
        self._data_bytes = 0
        self._queued_items = 0
        self._data_items = 0
        self._control_run = 0
        self._put_waiters = 0
        self._waiting_bytes = 0
        self._get_waiters = 0
        self._closed = False
        self._changed = asyncio.Event()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def queued_bytes(self) -> int:
        return self._queued_bytes

    @property
    def queued_items(self) -> int:
        return self._queued_items

    @property
    def waiting_producers(self) -> int:
        return self._put_waiters

    @property
    def waiting_consumers(self) -> int:
        return self._get_waiters

    @property
    def waiting_bytes(self) -> int:
        return self._waiting_bytes

    def _wake(self) -> None:
        previous = self._changed
        self._changed = asyncio.Event()
        previous.set()

    def _require_open(self) -> None:
        if self._closed:
            raise TerminalQueueClosed()

    def _record(self, message: dict[str, Any], payload_bytes: int, *, control: bool) -> _Record:
        if not isinstance(message, dict) or not 0 < len(message) <= 32:
            raise TerminalProtocolError(
                "INVALID_ENVELOPE", "Terminal envelope must be a bounded object"
            )
        try:
            encoded = json.dumps(message, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, RecursionError) as exc:
            raise TerminalProtocolError(
                "INVALID_ENVELOPE", "Terminal envelope is not bounded JSON"
            ) from exc
        encoded_bytes = len(encoded.encode("utf-8"))
        limit = (
            self.config.control_bytes
            if control
            else self.config.frame_bytes * 2 + self.config.control_bytes
        )
        if encoded_bytes > limit:
            raise TerminalProtocolError(
                "FRAME_TOO_LARGE", "Terminal envelope exceeds its size limit"
            )
        cost = encoded_bytes + payload_bytes + _RECORD_OVERHEAD
        capacity = self.max_bytes if control else self._data_capacity
        if cost > capacity:
            raise TerminalProtocolError(
                "FRAME_TOO_LARGE", "Terminal envelope cannot fit the queue budget"
            )
        return _Record(encoded, cost)

    def _fits(self, record: _Record, *, control: bool) -> bool:
        if (
            self._queued_bytes + record.cost > self.max_bytes
            or self._queued_items >= self.max_items
        ):
            return False
        if control:
            return len(self._control) < self.max_control_items
        return (
            self._data_bytes + record.cost <= self._data_capacity
            and self._data_items < self._data_item_limit
        )

    def _append(self, record: _Record, session_id: str | None) -> None:
        """Commit one already admitted record without yielding the event loop."""
        if session_id is None:
            self._control.append(record)
        else:
            if session_id not in self._output:
                self._output[session_id] = deque()
                self._round_robin.append(session_id)
            self._output[session_id].append(record)
            self._data_bytes += record.cost
            self._data_items += 1
        self._queued_bytes += record.cost
        self._queued_items += 1
        self._wake()

    def _try_put(self, record: _Record, session_id: str | None) -> bool:
        self._require_open()
        if not self._fits(record, control=session_id is None):
            return False
        self._append(record, session_id)
        return True

    async def _put(self, record: _Record, session_id: str | None) -> None:
        self._require_open()
        control = session_id is None
        waiting = False
        try:
            while not self._fits(record, control=control):
                if not waiting:
                    if (
                        self._put_waiters >= self.max_waiters
                        or self._waiting_bytes + record.cost > self.max_bytes
                    ):
                        raise TerminalProtocolError(
                            "TERMINAL_QUEUE_SATURATED",
                            "Too many terminal send producers are waiting",
                        )
                    self._put_waiters += 1
                    self._waiting_bytes += record.cost
                    waiting = True
                changed = self._changed
                await changed.wait()
                self._require_open()
            self._require_open()
            self._append(record, session_id)
        finally:
            if waiting:
                self._put_waiters -= 1
                self._waiting_bytes -= record.cost

    async def put_control(self, message: dict[str, Any]) -> None:
        self._require_open()
        await self._put(self._record(message, 0, control=True), None)

    def try_put_control(self, message: dict[str, Any]) -> bool:
        """Admit immediately or return False, without retaining a waiting record.

        A shared socket receiver uses this API to isolate a slow downstream
        attachment instead of waiting for it and blocking unrelated sessions.
        Invalid records and closed queues raise the same errors as put_control.
        """
        self._require_open()
        return self._try_put(self._record(message, 0, control=True), None)

    def _output_record(self, message: dict[str, Any], payload_bytes: int) -> tuple[_Record, str]:
        _integer(payload_bytes, "payload_bytes")
        if payload_bytes > self.config.frame_bytes:
            raise TerminalProtocolError(
                "FRAME_TOO_LARGE", "Terminal payload exceeds its frame limit"
            )
        if not isinstance(message, dict):
            raise TerminalProtocolError("INVALID_ENVELOPE", "Terminal envelope must be an object")
        session_id = canonical_uuid(message.get("session_id"))
        return self._record(message, payload_bytes, control=False), session_id

    async def put_output(self, message: dict[str, Any], payload_bytes: int) -> None:
        self._require_open()
        await self._put(*self._output_record(message, payload_bytes))

    def try_put_output(self, message: dict[str, Any], payload_bytes: int) -> bool:
        """Admit validated output immediately; False leaves all budgets intact."""
        self._require_open()
        return self._try_put(*self._output_record(message, payload_bytes))

    def _barrier_record(self, message: dict[str, Any]) -> tuple[_Record, str]:
        if not isinstance(message, dict):
            raise TerminalProtocolError("INVALID_ENVELOPE", "Terminal barrier must be an object")
        session_id = canonical_uuid(message.get("session_id"))
        record = self._record(message, 0, control=True)
        if record.cost > self._data_capacity:
            raise TerminalProtocolError(
                "FRAME_TOO_LARGE", "Terminal barrier cannot fit the session queue budget"
            )
        return record, session_id

    async def put_barrier(self, message: dict[str, Any]) -> None:
        """Order an exit/replay-complete event after this session's output.

        Priority controls such as revocation must use put_control().  An EOF or
        replay barrier must not jump over bytes already accepted for delivery.
        The barrier participates in the same fair session FIFO as output.
        """
        self._require_open()
        await self._put(*self._barrier_record(message))

    def try_put_barrier(self, message: dict[str, Any]) -> bool:
        """Admit immediately while preserving this session's output ordering."""
        self._require_open()
        return self._try_put(*self._barrier_record(message))

    async def get(self) -> dict[str, Any]:
        self._require_open()
        waiting = False
        try:
            while not self._queued_items:
                if not waiting:
                    if self._get_waiters >= self.max_waiters:
                        raise TerminalProtocolError(
                            "TERMINAL_QUEUE_SATURATED",
                            "Too many terminal send consumers are waiting",
                        )
                    self._get_waiters += 1
                    waiting = True
                changed = self._changed
                await changed.wait()
                self._require_open()
            self._require_open()
            if self._control and (not self._round_robin or self._control_run < self.control_burst):
                record = self._control.popleft()
                self._control_run = min(self.control_burst, self._control_run + 1)
            else:
                session_id = self._round_robin.popleft()
                records = self._output[session_id]
                record = records.popleft()
                if records:
                    self._round_robin.append(session_id)
                else:
                    del self._output[session_id]
                self._data_bytes -= record.cost
                self._data_items -= 1
                self._control_run = 0
            self._queued_bytes -= record.cost
            self._queued_items -= 1
            self._wake()
            return json.loads(record.encoded)
        finally:
            if waiting:
                self._get_waiters -= 1

    def close(self) -> None:
        """Synchronously reject new work, drop unsent frames and wake everyone."""
        if self._closed:
            return
        self._closed = True
        self._control.clear()
        self._output.clear()
        self._round_robin.clear()
        self._queued_bytes = self._data_bytes = self._queued_items = self._data_items = 0
        self._wake()


def terminal_secure_channel(secret: str, challenge: str, device_id: str, role: str):
    """Reuse the existing cipher with a separate purpose/handshake namespace.

    The Hub creates a fresh high-entropy challenge for *each* terminal socket.
    Callers authenticate its source and device binding before constructing this
    object; syntax checks do not turn an arbitrary peer into an authorized one.
    """
    if not isinstance(secret, str) or len(secret) < 40:
        raise ValueError("A valid device secret is required")
    if not isinstance(challenge, str) or not 32 <= len(challenge) <= 512:
        raise ValueError("A fresh bounded Hub challenge is required")
    if not isinstance(device_id, str) or not 0 < len(device_id) <= 256 or "/" in device_id:
        raise ValueError("A base device identifier is required")
    if role not in {"hub", "agent"}:
        raise ValueError("Invalid terminal channel role")
    from .crypto import SecureChannel

    return SecureChannel(secret, challenge, device_id + TERMINAL_CHANNEL_PURPOSE, role)
