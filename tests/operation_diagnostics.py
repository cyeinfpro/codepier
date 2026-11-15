"""Bounded failure evidence for synthetic fixture operations; never replay work."""

import json
import os
import time
from pathlib import Path

from shared.process_diagnostics import PROCESS_STAGES, safe_process_detail


def bounded_json(client, method, path, *, total_timeout=2.0, **kwargs):
    # A per-I/O timeout alone permits an endless trickle. Check elapsed time
    # between transport chunks, without waiting for a 4096-byte buffer to fill.
    # A blocked read remains bounded by its I/O timeout; this is not a thread kill.
    if total_timeout <= 0:
        raise ValueError("Diagnostic time budget must be positive")
    deadline = time.monotonic() + total_timeout
    body = bytearray()
    with client.stream(method, path, timeout=min(2.0, total_timeout), **kwargs) as response:
        response.raise_for_status()
        if time.monotonic() >= deadline:
            raise TimeoutError("Diagnostic response exceeds elapsed-time budget")
        for chunk in response.iter_bytes():
            if time.monotonic() >= deadline:
                raise TimeoutError("Diagnostic response exceeds elapsed-time budget")
            if len(body) + len(chunk) > 65536:
                raise ValueError("Diagnostic response exceeds byte budget")
            body.extend(chunk)
    if time.monotonic() >= deadline:
        raise TimeoutError("Diagnostic response exceeds elapsed-time budget")
    return json.loads(body)


def failure_note(client, identifier, *, save_first_status=False):
    evidence = {"operation_id": identifier}
    try:
        snapshot = bounded_json(client, "GET", "/api/operations/" + identifier)
        evidence["state"] = (
            snapshot.get("state")
            if snapshot.get("state")
            in {
                "queued",
                "running",
                "reconnecting",
                "unknown",
                "cancelling",
                "succeeded",
                "failed",
                "cancelled",
                "interrupted",
                "needs_review",
            }
            else "unavailable"
        )
    except Exception:
        evidence["snapshot"] = "unavailable"
    try:
        trace = bounded_json(
            client,
            "POST",
            "/api/tools/call",
            json={
                "tool": "operations_trace",
                "arguments": {"operation_id": identifier, "after_event_id": 0, "limit": 200},
            },
        )
        # Only already allowlisted owned-child observations, not command/output data.
        evidence["process_trace"] = [
            {
                "stage": event["stage"],
                "elapsed_ms": event.get("elapsed_ms"),
                "detail": safe_process_detail(event.get("detail") or {}),
            }
            for event in trace.get("events", [])[-32:]
            if isinstance(event, dict)
            and event.get("stage") in PROCESS_STAGES
            and type(event.get("elapsed_ms")) is int
        ]
    except Exception:
        evidence["process_trace"] = "unavailable"
    encoded = json.dumps(evidence, ensure_ascii=True)
    if save_first_status and os.environ.get("CODEPIER_TEST_PROCESS_EVIDENCE") == "1":
        try:
            if len(encoded.encode("utf-8")) <= 10000:
                destination = Path("dist/windows/first-git-status-trace.json")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(encoded + "\n", encoding="utf-8")
        except Exception:
            pass  # Evidence persistence cannot change the original test outcome.
    return "Owned-child diagnostics (no replay): " + encoded[:10000]
