"""Bounded, ack-clocked flow control for native transcript sync.

Background (docs/QUEUE_DIAGNOSIS_2026-11-06.md): when the Hub refused old
sessions (project root remapped), it returned no offset for them. The Agent
reset offsets on every reconnect and resent the same history blocks every
90 ms, ~2-3 MB/s, sharing the socket with operation receipts.

This module keeps the policy small and testable:

* **Window.** At most ``WINDOW`` transcript packets carrying chunks are
  unacknowledged at once; acknowledgements (``native_ack``) clock the next
  packet. Packets carry a ``seq``; new Hubs echo ``seqs``, legacy Hubs ACK one
  packet per frame in order, which is matched FIFO.
* **No-progress backoff.** A session whose chunk was ACKed without its offset
  advancing (legacy Hub rejection, cache quota, stale metadata, timeout) is
  skipped for an exponentially growing, bounded interval. This alone stops the
  hot loop even against an un-upgraded Hub.
* **Explicit rejection pause.** New Hubs report ``rejected`` per session
  (mapping changed, project removed, ownership). Those sessions stop sending
  chunks; the pause is persisted with the session's mapping fingerprint so a
  reconnect does not restart the loop. Paused sessions are re-offered as
  metadata-only probes with bounded backoff, so a restored mapping resumes
  automatically. Nothing is fabricated: offsets are only ever taken from the
  Hub, and no history is re-homed to another project.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import time
from collections import deque
from pathlib import Path

WINDOW = 2
INFLIGHT_TIMEOUT = 20.0
MAX_INFLIGHT_ENTRIES = 256
NO_PROGRESS_INITIAL = 1.0
NO_PROGRESS_MAX = 300.0
DEFERRED_INITIAL = {"cache_quota": 30.0}
DEFERRED_DEFAULT = 2.0
PAUSE_INITIAL = 120.0
PAUSE_MAX = 3600.0
MAX_PAUSED = 4096
REASON_CHARS = 64


def fingerprint(row) -> str:
    try:
        material = json.dumps(
            [row.get("project_id"), row.get("root"), row.get("device_id")],
            separators=(",", ":"),
            ensure_ascii=True,
        )
    except (TypeError, ValueError, AttributeError):
        return ""
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _decoded_length(data) -> int:
    if not isinstance(data, str):
        return 0
    try:
        return len(base64.b64decode(data, validate=True))
    except (binascii.Error, ValueError):
        return 0


def _codes(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        sid: (reason if isinstance(reason, str) else "rejected")[:REASON_CHARS]
        for sid, reason in list(value.items())[:4096]
        if isinstance(sid, str) and 0 < len(sid) <= 128
    }


class SyncFlow:
    def __init__(self, path=None, *, clock=time.time, monotonic=time.monotonic):
        self.path = Path(path) if path else None
        self.clock = clock
        self.monotonic = monotonic
        self.inflight: deque[dict] = deque()
        self.seq = 0
        self.backoff: dict[str, tuple[float, float]] = {}
        self.paused: dict[str, dict] = {}
        self.rows: dict[str, str] = {}
        self.dirty = False
        self.load()

    # -- persistence -----------------------------------------------------
    def load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, "r", encoding="utf-8") as stream:
                value = json.loads(stream.read(4 * 1024 * 1024))
        except (OSError, ValueError, UnicodeError):
            return
        sessions = (
            value.get("sessions") if isinstance(value, dict) and value.get("version") == 1 else None
        )
        if not isinstance(sessions, dict):
            return
        for sid, item in list(sessions.items())[:MAX_PAUSED]:
            if (
                isinstance(sid, str)
                and 0 < len(sid) <= 128
                and isinstance(item, dict)
                and isinstance(item.get("fp"), str)
                and isinstance(item.get("reason"), str)
                and type(item.get("failures")) is int
                and item["failures"] >= 1
                and type(item.get("until")) in (int, float)
                and type(item.get("at")) in (int, float)
            ):
                self.paused[sid] = {
                    "fp": item["fp"][:64],
                    "reason": item["reason"][:REASON_CHARS],
                    "until": float(item["until"]),
                    "failures": min(item["failures"], 64),
                    "at": float(item["at"]),
                }

    def dump(self) -> bytes:
        """Serialize on the event loop; write the bytes from a worker thread."""
        self.dirty = False
        return json.dumps(
            {"version": 1, "sessions": self.paused}, separators=(",", ":"), sort_keys=True
        ).encode()

    def write(self, payload: bytes) -> None:
        if not self.path:
            return
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError:
            # Pause state is an optimisation: the in-memory pause still holds and
            # the no-progress backoff bounds traffic after a restart.
            self.dirty = True

    # -- connection lifecycle ---------------------------------------------
    def reset_connection(self) -> None:
        self.inflight.clear()

    def next_seq(self) -> int:
        self.seq += 1
        return self.seq

    # -- per-cycle decisions ---------------------------------------------
    def expire(self) -> None:
        now = self.monotonic()
        while self.inflight and now - self.inflight[0]["sent"] > INFLIGHT_TIMEOUT:
            for sid in self.inflight.popleft()["chunks"]:
                self._no_progress(sid)

    def window_open(self) -> bool:
        self.expire()
        return sum(1 for entry in self.inflight if entry["chunks"]) < WINDOW

    def skip(self) -> frozenset:
        now = self.clock()
        for sid in [
            sid for sid, (until, delay) in self.backoff.items() if until + NO_PROGRESS_MAX < now
        ]:
            del self.backoff[sid]
        return frozenset(self.paused) | frozenset(
            sid for sid, (until, _) in self.backoff.items() if until > now
        )

    def due_probes(self) -> list:
        """Paused sessions due for a metadata-only re-offer to the Hub."""
        now, due = self.clock(), []
        for sid, item in self.paused.items():
            if item["until"] <= now:
                # Hold the probe slot until the Hub answers or the next interval.
                item["until"] = now + min(PAUSE_MAX, PAUSE_INITIAL * 2 ** (item["failures"] - 1))
                self.dirty = True
                due.append(sid)
        return due

    def observe_rows(self, rows) -> None:
        for row in rows or ():
            sid = row.get("id") if isinstance(row, dict) else None
            if not isinstance(sid, str):
                continue
            current = fingerprint(row)
            self.rows[sid] = current
            paused = self.paused.get(sid)
            if paused and paused["fp"] and paused["fp"] != current:
                # The session's own mapping changed; the old refusal no longer applies.
                del self.paused[sid]
                self.dirty = True

    def sent(self, seq: int, chunks) -> None:
        ends: dict[str, int] = {}
        for chunk in chunks or ():
            sid, offset = chunk.get("id"), chunk.get("offset")
            if isinstance(sid, str) and type(offset) is int:
                ends[sid] = max(ends.get(sid, 0), offset + _decoded_length(chunk.get("data")))
        self.inflight.append({"seq": seq, "sent": self.monotonic(), "chunks": ends})
        while len(self.inflight) > MAX_INFLIGHT_ENTRIES:
            self.inflight.popleft()

    # -- acknowledgements -------------------------------------------------
    def on_ack(self, data, offsets_store: dict) -> None:
        if not isinstance(data, dict):
            return
        raw = data.get("offsets", {})
        offsets = (
            {k: v for k, v in raw.items() if isinstance(k, str) and type(v) is int and v >= 0}
            if isinstance(raw, dict)
            else {}
        )
        offsets_store.update(offsets)
        seqs = data.get("seqs")
        entries = []
        if isinstance(seqs, list) and seqs:
            wanted = {s for s in seqs[:1024] if type(s) is int}
            entries = [entry for entry in self.inflight if entry["seq"] in wanted]
            if entries:
                self.inflight = deque(
                    entry for entry in self.inflight if entry["seq"] not in wanted
                )
        elif self.inflight:
            # Legacy Hub: one native_ack per native_sync frame, in wire order.
            entries = [self.inflight.popleft()]
        rejected, deferred = _codes(data.get("rejected")), _codes(data.get("deferred"))
        now = self.clock()
        for sid, reason in rejected.items():
            self._pause(sid, reason, now)
        for sid in offsets:
            if sid in self.paused and sid not in rejected:
                del self.paused[sid]
                self.dirty = True
        for entry in entries:
            for sid, end in entry["chunks"].items():
                if sid in rejected:
                    continue
                if offsets.get(sid, -1) >= end:
                    self.backoff.pop(sid, None)
                else:
                    self._no_progress(sid, deferred.get(sid))

    def _no_progress(self, sid, reason=None) -> None:
        initial = DEFERRED_INITIAL.get(reason, DEFERRED_DEFAULT) if reason else NO_PROGRESS_INITIAL
        _, delay = self.backoff.get(sid, (0.0, 0.0))
        delay = min(NO_PROGRESS_MAX, max(initial, delay * 2))
        self.backoff[sid] = (self.clock() + delay, delay)

    def _pause(self, sid, reason, now) -> None:
        prior = self.paused.get(sid)
        failures = min(64, prior["failures"] + 1) if prior else 1
        self.paused[sid] = {
            "fp": self.rows.get(sid) or (prior or {}).get("fp", ""),
            "reason": reason,
            "until": now + min(PAUSE_MAX, PAUSE_INITIAL * 2 ** (failures - 1)),
            "failures": failures,
            "at": now,
        }
        self.backoff.pop(sid, None)
        self.dirty = True
        if len(self.paused) > MAX_PAUSED:
            for oldest in sorted(self.paused, key=lambda key: self.paused[key]["at"])[
                : len(self.paused) - MAX_PAUSED
            ]:
                del self.paused[oldest]
