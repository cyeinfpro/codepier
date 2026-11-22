"""Bounded terminal measurements and an explicit, content-free projection.

Metrics have a fixed set of names and buckets. Session IDs and other identities
are never metric labels, and no event payload is retained by the collector.
"""

from __future__ import annotations

import json
import math
import re
import time

MAX_COUNTER = (1 << 63) - 1
STAGES = frozenset({"ssh_connect", "pty_open", "hub_prepare", "attach"})
STATES = frozenset({"creating", "connecting", "ready", "detached", "closing", "exited", "failed", "frozen", "reconnecting", "close_unknown"})
COUNTERS = (
    "connect_total", "disconnect_total", "recover_total", "reconnect_total",
    "history_gap_total", "input_uncertain_total", "ssh_exit_total",
    "protocol_reject_total", "backpressure_total", "revocation_total", "diagnostics_total",
)
HISTOGRAMS = (
    "ssh_connect_ms", "pty_open_ms", "hub_prepare_ms", "attach_ms",
    "agent_control_rtt_ms", "auth_revocation_ms", "backpressure_ms",
)
BUCKETS_MS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000, 60000, 300000)
LIFECYCLE_TIMES = ("connected_at", "disconnected_at", "last_recovered_at", "revoked_at")
METADATA_COUNTERS = ("recover_count", "reconnect_count", "backpressure_count", "backpressure_duration_ms")
REASONS = frozenset({
    "AGENT_OFFLINE", "AGENT_UPGRADE_REQUIRED", "AGENT_RESPONSE_UNKNOWN", "AGENT_RECONNECTED",
    "STALE_GENERATION", "SESSION_NOT_FOUND", "SESSION_NOT_RECOVERABLE", "AUTH_REVOKED",
    "AUTH_LEASE_EXPIRED", "AUTHORITY_UNCONFIRMED", "LOGIN_REQUIRED", "SSH_AUTH_FAILED",
    "SSH_AUTH_CHALLENGE_REQUIRED", "AUTH_CHALLENGE_EXPIRED", "SSH_CONNECT_TIMEOUT",
    "SSH_CONNECT_FAILED", "SSH_CONNECTION_FAILED", "SSH_CONNECTION_CLOSED", "SSH_CHANNEL_CLOSED",
    "HOST_KEY_UNKNOWN", "HOST_KEY_CHANGED", "HOST_KEY_REVOKED", "HOST_TRUST_CONFIGURATION",
    "HOST_TRUST_UNAVAILABLE", "HOST_TRUST_BUSY", "PERSISTENCE_UNAVAILABLE", "PERSISTENCE_PROBE_FAILED",
    "REMOTE_SESSION_MISSING", "INPUT_UNCERTAIN", "INPUT_BACKPRESSURE", "OUTPUT_BACKPRESSURE",
    "HISTORY_GAP", "USER_CLOSED", "USER_DETACHED", "EXITED", "CLOSE_UNKNOWN", "WRITER_CONFLICT",
    "STALE_WRITER", "PROTOCOL_MISMATCH", "INVALID_FRAME", "INVALID_ACK", "RATE_LIMITED",
    "TARGET_REVISION_CHANGED", "PROJECT_MAPPING_CHANGED", "SSH_TERMINAL_FORBIDDEN",
    "TERMINAL_CHANNEL_CLOSED", "TERMINAL_CONFIG_MISMATCH", "CONNECTION_CLOSED", "HUB_RESTARTING",
})


def counter(value, default=0):
    if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 20:
        value = int(value)
    return min(MAX_COUNTER, value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def duration(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        return None
    return round(min(value, 7 * 24 * 60 * 60 * 1000), 3)


def reason_code(value):
    return None if value is None else value if isinstance(value, str) and value in REASONS else "UNKNOWN"


def version(value):
    return value if isinstance(value, str) and re.fullmatch(r"[0-9][A-Za-z0-9_.+\-]{0,63}", value) else None


def safe_metadata(changes):
    """Only numeric measurements and bounded version strings may be persisted."""
    result = {}
    stages = changes.get("stage_timings_ms")
    if isinstance(stages, dict):
        result["stage_timings_ms"] = {key: measured for key in STAGES
                                      if (measured := duration(stages.get(key))) is not None}
    for key in LIFECYCLE_TIMES:
        value = changes.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1e12:
            result[key] = value
    for key in METADATA_COUNTERS:
        if key in changes and (value := counter(changes[key], None)) is not None:
            result[key] = value
    for key in ("agent_version", "hub_version"):
        if key in changes and (value := version(changes[key])) is not None:
            result[key] = value
    if type(changes.get("protocol_version")) is int and changes["protocol_version"] == 1:
        result["protocol_version"] = 1
    return result


class TerminalMetrics:
    def __init__(self):
        self.started_at = time.time()
        self.counters = dict.fromkeys(COUNTERS, 0)
        self.histograms = {name: {"count": 0, "sum_ms": 0.0, "max_ms": 0.0,
                                  "buckets": [0] * len(BUCKETS_MS), "overflow": 0} for name in HISTOGRAMS}

    def increment(self, name, amount=1):
        self.counters[name] = min(MAX_COUNTER, self.counters[name] + counter(amount))

    def observe(self, name, milliseconds):
        measured = duration(milliseconds)
        if measured is None:
            return
        histogram = self.histograms[name]
        histogram["count"] = min(MAX_COUNTER, histogram["count"] + 1)
        histogram["sum_ms"] = min(float(MAX_COUNTER), histogram["sum_ms"] + measured)
        histogram["max_ms"] = max(histogram["max_ms"], measured)
        for index, bound in enumerate(BUCKETS_MS):
            if measured <= bound:
                histogram["buckets"][index] = min(MAX_COUNTER, histogram["buckets"][index] + 1)
                break
        else:
            histogram["overflow"] = min(MAX_COUNTER, histogram["overflow"] + 1)

    def snapshot(self):
        return {"since": self.started_at, "counters": dict(self.counters), "histograms": {
            name: {"count": value["count"], "sum_ms": round(value["sum_ms"], 3), "max_ms": value["max_ms"],
                   "buckets": [{"le_ms": bound, "count": sum(value["buckets"][:index + 1])}
                               for index, bound in enumerate(BUCKETS_MS)], "overflow": value["overflow"]}
            for name, value in self.histograms.items()}}


def queue_bytes(queue):
    if queue is None:
        return 0
    return counter(getattr(queue, "queued_bytes", 0)) + counter(getattr(queue, "waiting_bytes", 0))


def session_snapshot(session, *, agent, status, link_status, rtt_ms, link_reason, hub_version):
    """Project explicitly; never serialize a project, target or Agent response."""
    row = session.row
    confirmed = (link_status == "connected"
                 and status.get("runtime_generation", row["runtime_generation"]) == row["runtime_generation"]
                 and status.get("session_id", row["id"]) == row["id"])
    status = status if confirmed else {}
    metadata = json.loads(row["metadata"])
    diagnostic = safe_metadata(status)
    diagnostic["stage_timings_ms"] = {**safe_metadata(metadata).get("stage_timings_ms", {}),
                                      **diagnostic.get("stage_timings_ms", {})}
    metadata = {**metadata, **diagnostic}
    attachment = session.attachment
    attached = attachment is not None and not attachment.closed and attachment.attached.is_set()
    browser_buffer = queue_bytes(attachment.queue) if attached else 0
    ssh_connected = status.get("ssh_connected") if confirmed else None
    if not isinstance(ssh_connected, bool):
        ssh_connected = None
    browser = {"connected": attached, "status": "connected" if attached else "detached",
               "attachment_id": attachment.id if attached else None,
               "render_epoch": str(attachment.render_epoch) if attached else None,
               "stream_epoch": str(attachment.stream_epoch) if attached else None,
               "writer_epoch": str(attachment.writer_epoch) if attached else None,
               "rendered_offset": str(attachment.acked_offset) if attached else None,
               "sent_offset": str(attachment.sent_offset) if attached else None,
               "buffered_bytes": browser_buffer,
               "unacknowledged_bytes": max(0, attachment.sent_offset - attachment.acked_offset) if attached else 0}
    counters = session.metrics.counters
    selected_counters = {key: counters[key] for key in (
        "reconnect_total", "history_gap_total", "input_uncertain_total", "ssh_exit_total", "protocol_reject_total", "backpressure_total")}
    selected_counters["reconnect_total"] = counter(metadata.get("reconnect_count"))
    selected_counters["backpressure_total"] = min(MAX_COUNTER, selected_counters["backpressure_total"] + counter(metadata.get("backpressure_count")))
    byte_counts = {key: counter(status.get(source, metadata.get(source)), None)
                   for key, source in (("input", "bytes_in"), ("output", "bytes_out"))}
    remote_state = status.get("remote_session_state") if confirmed else metadata.get("remote_session_state")
    if not isinstance(remote_state, str) or remote_state not in {"running", "exists", "missing", "ended", "unknown", "not_applicable"}:
        remote_state = None
    state, mode = status.get("state", row["state"]), status.get("mode", row["mode"])
    state = state if isinstance(state, str) and state in STATES else "unknown"
    mode = mode if isinstance(mode, str) and mode in {"direct", "tmux"} else "unknown"
    return {
        "session_id": row["id"], "protocol_version": 1, "collected_at": time.time(),
        "state": state, "mode": mode, "reason_code": reason_code(status.get("reason_code", row.get("reason_code"))),
        "links": {"browser": browser,
                  "agent": {"connected": link_status == "connected", "status": link_status,
                            "runtime_generation": agent.generation if agent and not agent.closed else None,
                            "control_rtt_ms": rtt_ms, "reason_code": reason_code(link_reason)},
                  "ssh": {"connected": ssh_connected, "status": "unconfirmed" if ssh_connected is None else "connected" if ssh_connected else "disconnected",
                          "mode": mode, "remote_session_state": remote_state}},
        "bytes": {key: str(value) if value is not None else None for key, value in byte_counts.items()},
        "stages_ms": safe_metadata(metadata).get("stage_timings_ms", {}),
        "lifecycle": {"created_at": row["created"], "connected_at": safe_metadata(metadata).get("connected_at"),
                      "disconnected_at": safe_metadata(metadata).get("disconnected_at"), "last_recovered_at": safe_metadata(metadata).get("last_recovered_at"),
                      "recover_count": counter(metadata.get("recover_count")), "reconnect_count": counter(metadata.get("reconnect_count")),
                      "version": {"hub": hub_version, "agent": version(metadata.get("agent_version")), "protocol": 1}},
        "metrics": {"scope": "session", "since": session.metrics.started_at, "counters": selected_counters,
                    "gauges": {"browser_buffer_bytes": browser_buffer, "agent_queue_bytes": None,
                               "ssh_ring_bytes": counter(status.get("ring_bytes"), None) if confirmed else None,
                               "ssh_outstanding_bytes": counter(status.get("output_outstanding_bytes"), None) if confirmed else None},
                    "durations_ms": {"backpressure": counter(metadata.get("backpressure_duration_ms"))}},
    }
