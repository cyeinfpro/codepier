#!/usr/bin/env python3
"""Opt-in real loopback benchmark. Creates only disposable fixture Hub/Agent data.

Run the SAME file/interpreter/workload against --repository before/after checkouts.
No external endpoint, new dependency, production credential, browser or model call.
Do not run alongside a user's latency-sensitive work. Measurements are evidence,
not a promise: retain every error and host-load sample, alternate variant order.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import functools
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import re
import sqlite3
import statistics
import subprocess
import sys
import time
from typing import NamedTuple
import uuid


def percentiles(values):
    values = sorted(v for v in values if v is not None)
    return {
        key: round(values[max(0, math.ceil(len(values) * p) - 1)], 3) if values else None
        for key, p in [("p50", 0.5), ("p95", 0.95), ("p99", 0.99)]
    }


def fairness(samples):
    groups = {}
    for sample in samples:
        groups.setdefault(sample["worker"], []).append(sample["latency_ms"])
    rates = [
        1000 / statistics.mean(values) for values in groups.values() if statistics.mean(values) > 0
    ]
    return sum(rates) ** 2 / (len(rates) * sum(v * v for v in rates)) if rates else None


def stage_stats(samples):
    """Keep successful percentiles and their coverage together."""
    keys = ("hub_queue_ms", "hub_first_dispatch_ms", "hub_after_dispatch_ms", "agent_queue_ms")
    result = {}
    for key in keys:
        values = [sample[key] for sample in samples if sample.get(key) is not None]
        result[key] = percentiles(values)
        result[key + "_samples"] = len(values)
        result[key + "_max"] = round(max(values), 3) if values else None
        result[key + "_unavailable"] = dict(
            collections.Counter(
                sample.get(key.removesuffix("_ms") + "_unavailable", "missing")
                for sample in samples
                if sample.get(key) is None
            )
        )
    return result


def summarize(samples, seconds, *, include_kinds=True):
    good = [s for s in samples if s["ok"]]
    result = {
        "samples": len(samples),
        "succeeded": len(good),
        "errors": len(samples) - len(good),
        "error_rate": (len(samples) - len(good)) / len(samples) if samples else None,
        "duration_seconds": seconds,
        "throughput_success_per_second": len(good) / seconds if seconds > 0 else None,
        "latency_ms_all": percentiles([s["latency_ms"] for s in samples]),
        "latency_ms_success": percentiles([s["latency_ms"] for s in good]),
        "hub_queue_ms": percentiles([s.get("hub_queue_ms") for s in samples]),
        "agent_queue_ms": percentiles([s.get("agent_queue_ms") for s in samples]),
        "stage_ms_success": stage_stats(good),
        "jain_inverse_worker_mean_latency": fairness(samples),
        "queue_sample_count": sum(s.get("hub_queue_ms") is not None for s in samples),
        "poll_requests": sum(s.get("polls", 0) for s in samples),
    }
    outcomes = collections.Counter(operation_outcome(s) for s in samples)
    result["outcomes"] = {name: outcomes[name] for name in OPERATION_OUTCOMES}
    if include_kinds:
        result["by_kind"] = {
            kind: summarize(
                [s for s in samples if s.get("kind", "unspecified") == kind],
                seconds,
                include_kinds=False,
            )
            for kind in sorted({s.get("kind", "unspecified") for s in samples})
        }
    return result


def git_identity(repository):
    """Hash all nonignored changes; never copy diff contents into the report."""

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repository), *args])

    diff = git("diff", "--no-ext-diff", "--no-textconv", "--binary", "HEAD")
    status = git("status", "--porcelain=v1", "-z")
    digest = hashlib.sha256(diff)
    for raw in sorted(
        filter(None, git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0"))
    ):
        path = repository / os.fsdecode(raw)
        payload = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return {
        "commit": git("rev-parse", "HEAD").decode().strip(),
        "dirty": bool(status),
        "tracked_diff_and_untracked_source_sha256": digest.hexdigest(),
    }


UNRESOLVED = {"queued", "running", "reconnecting", "cancelling", "unknown"}
TERMINAL = {"succeeded", "failed", "cancelled", "needs_review", "interrupted"}


def safe_code(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value)
        else "unclassified_error"
    )


def lookup_original(database, key):
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as db:
        rows = db.execute("SELECT id FROM operations WHERE idem=?", (key,)).fetchall()
    if len(rows) != 1:
        raise ValueError("original_operation_missing_or_ambiguous")
    return rows[0][0]


def preflight(seconds, max_load_per_cpu):
    samples = []
    deadline = time.monotonic() + seconds
    while True:
        samples.append({"at": time.time(), "loadavg": list(os.getloadavg())})
        if time.monotonic() >= deadline:
            break
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    overloaded = any(
        row["loadavg"][0] / (os.cpu_count() or 1) > max_load_per_cpu for row in samples
    )
    return samples, overloaded


def bounded_number(value, low, high, *, integer=False):
    kinds = (int,) if integer else (int, float)
    return value if type(value) in kinds and low <= value <= high and math.isfinite(value) else None


def bounded_choice(value, choices, default=None):
    return value if isinstance(value, str) and value in choices else default


def admission_snapshot(value):
    """Retain bounded diagnostics, never scheduler config, identities or paths."""
    value = value if isinstance(value, dict) else {}
    states = {"offline", "unsupported", "applied", "rejected", "pending"}
    reasons = {"legacy", "unavailable", "pressure", "recovering", "adaptive"}
    reported_reasons = {
        "starting",
        "fixed",
        "metrics_unavailable",
        "memory_critical",
        "memory_recovery",
        "recovering",
        "resource_pressure",
        "healthy",
        "cooldown",
    }
    budget = value.get("durable_admission")
    budget = budget if isinstance(budget, dict) else {}
    result = {
        "state": bounded_choice(value.get("state"), states, "unknown"),
        "revision": value.get("revision")
        if isinstance(value.get("revision"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["revision"])
        else None,
        "durable_admission": {
            "reason": bounded_choice(budget.get("reason"), reasons, "unknown"),
            "node_pending_limit": bounded_number(
                budget.get("node_pending_limit"), 1, 512, integer=True
            ),
            "project_pending_limit": bounded_number(
                budget.get("project_pending_limit"), 1, 256, integer=True
            ),
            "new_heavy_admission_paused": budget.get("new_heavy_admission_paused")
            if type(budget.get("new_heavy_admission_paused")) is bool
            else None,
            "overload_read_reserve": bounded_number(
                budget.get("overload_read_reserve"), 0, 8, integer=True
            ),
        },
        "reported": None,
    }
    reported = value.get("reported")
    if isinstance(reported, dict):
        lanes = reported.get("lanes")
        lanes = lanes if isinstance(lanes, dict) else {}
        result["reported"] = {
            "reason": bounded_choice(reported.get("reason"), reported_reasons, "unknown"),
            "running": bounded_number(reported.get("running"), 0, 100000, integer=True),
            "queued": bounded_number(reported.get("queued"), 0, 100000, integer=True),
            "lanes": {},
        }
        for name in ("execution", "read", "remote"):
            lane = lanes.get(name)
            lane = lane if isinstance(lane, dict) else {}
            result["reported"]["lanes"][name] = {
                "capacity": bounded_number(lane.get("capacity"), 0, 64, integer=True),
                "running": bounded_number(lane.get("running"), 0, 100000, integer=True),
                "queued": bounded_number(lane.get("queued"), 0, 100000, integer=True),
            }
    return result


def admission_ready(snapshot, required_pending):
    if type(required_pending) is not int or not 1 <= required_pending <= 256:
        return False
    snapshot = admission_snapshot(snapshot)
    budget = snapshot["durable_admission"]
    return (
        snapshot["state"] == "applied"
        and snapshot["revision"] is not None
        and budget["reason"] == "adaptive"
        and budget["new_heavy_admission_paused"] is False
        and budget["node_pending_limit"] is not None
        and budget["node_pending_limit"] >= required_pending
        and budget["project_pending_limit"] is not None
        and budget["project_pending_limit"] >= required_pending
    )


def observe_admission(stack, *, timeout=10):
    # Reuse the fixture's authenticated owner client. No preferences are written.
    response = stack.client.get(
        "/api/settings/scheduler", params={"device": stack.device}, timeout=timeout
    )
    response.raise_for_status()
    return response.json()


class AdmissionReadinessError(RuntimeError):
    def __init__(self, observation):
        super().__init__("admission_readiness_not_met")
        self.observation = observation


def prepare_admission(
    probe, required_pending, *, profile="cold", timeout=300, clock=time.monotonic, sleep=time.sleep
):
    """Observe once, or wait for a real applied budget under one absolute deadline.

    Match the real-resource acceptance helper's 3s cadence and deadline check
    after each response. Slow/failed probes cannot reuse old readiness or extend
    the budget. At most 100 observations fit the maximum 300s preparation.
    """
    import httpx

    if (
        bounded_choice(profile, {"cold", "ready"}) is None
        or type(required_pending) is not int
        or not 1 <= required_pending <= 256
        or bounded_number(timeout, 0, 300) is None
        or timeout <= 0
    ):
        raise ValueError("invalid_admission_preparation")
    started = clock()
    deadline = started + timeout
    result = {
        "profile": profile,
        "required_pending": required_pending,
        "timeout_seconds": timeout,
        "elapsed_seconds": 0,
        "ready": False,
        "probe_count": 0,
        "observations": [],
    }
    while clock() < deadline:
        previous = clock()
        if previous >= deadline:
            break
        observation = {"elapsed_seconds": None, "snapshot": None, "error": None}
        try:
            observation["snapshot"] = admission_snapshot(
                probe(timeout=min(10, deadline - previous))
            )
        except (httpx.HTTPError, OSError, AssertionError, KeyError, TypeError, ValueError) as exc:
            observation["error"] = type(exc).__name__
        now = clock()
        observation["elapsed_seconds"] = round(now - started, 3)
        result["observations"].append(observation)
        result["probe_count"] += 1
        result["elapsed_seconds"] = round(now - started, 3)
        # Readiness arriving at or beyond the original deadline is not success.
        result["ready"] = (
            now < deadline
            and observation["snapshot"] is not None
            and admission_ready(observation["snapshot"], required_pending)
        )
        if profile == "cold" or result["ready"]:
            return result
        if now < deadline:
            sleep(min(max(0, previous + 3 - now), deadline - now))
    result["elapsed_seconds"] = round(clock() - started, 3)
    raise AdmissionReadinessError(result)


def rejection_metadata(result):
    """Keep exact known admission diagnostics without copying an error body."""
    error = result.get("error") if isinstance(result, dict) else None
    if not isinstance(error, dict):
        return {}
    retained = {}
    if bounded_choice(error.get("queue_scope"), {"hub", "node", "project"}) is not None:
        retained["queue_scope"] = error["queue_scope"]
    if (
        bounded_choice(
            error.get("queue_reason"),
            {
                "admission_count",
                "admission_bytes",
                "control_reserve",
                "resource_pressure",
                "project_fairness",
            },
        )
        is not None
    ):
        retained["queue_reason"] = error["queue_reason"]
    retry = bounded_number(error.get("retry_after_seconds"), 0, 300)
    if retry is not None:
        retained["retry_after_seconds"] = retry
    if type(error.get("admitted")) is bool:
        retained["admitted"] = error["admitted"]
    return retained


def extract(response, request_id=None):
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or (
        request_id is not None and (body.get("jsonrpc") != "2.0" or body.get("id") != request_id)
    ):
        raise ValueError("invalid_jsonrpc_response")
    if "error" in body:
        raise ValueError("jsonrpc_error")
    result = body.get("result")
    if not isinstance(result, dict) or type(result.get("isError", False)) is not bool:
        raise ValueError("invalid_tool_response")
    data = result.get("structuredContent")
    if data is None:
        texts = [x.get("text", "") for x in result.get("content", []) if x.get("type") == "text"]
        data = json.loads(texts[0]) if texts else None
    if not isinstance(data, dict):
        raise ValueError("invalid_tool_content")
    return data, result.get("isError", False)


async def rpc(client, name, arguments):
    request_id = uuid.uuid4().hex
    return extract(
        await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
        ),
        request_id,
    )


def receipt_identifier(data):
    error = data.get("error")
    identifiers = tuple(
        value
        for value in (
            data.get("operation_id"),
            data.get("id"),
            error.get("operation_id") if isinstance(error, dict) else None,
        )
        if value is not None
    )
    if not identifiers:
        return None
    if any(
        not isinstance(value, str)
        or re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value) is None
        or value != identifiers[0]
        for value in identifiers
    ):
        raise ValueError("invalid_original_operation_id")
    return identifiers[0]


def receipt_state(data):
    state = data.get("state")
    pending = data.get("pending", False)
    if (
        type(pending) is not bool
        or (
            state is not None and (not isinstance(state, str) or state not in TERMINAL | UNRESOLVED)
        )
        or (pending and state in TERMINAL)
    ):
        raise ValueError("invalid_original_operation_state")
    return state, pending or state in UNRESOLVED


def terminal_result(data, state, failed, *, polled):
    result = data.get("result")
    if result is None and (
        (not polled and "result" not in data) or (polled and state != "succeeded")
    ):
        return data, failed or state != "succeeded"
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        raise ValueError("invalid_terminal_operation_result")
    value = result.get("data")
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError("invalid_terminal_operation_data")
    if isinstance(result.get("error"), dict):
        value = {**value, "error": result["error"]}
    return value, failed or not (state == "succeeded" and result["ok"])


async def finish_original(client, data, failed, deadline, *, sample=None, on_unknown=None):
    polls = 0
    identifier = receipt_identifier(data)
    state, pending = receipt_state(data)
    # Synchronous tool errors can carry an admitted operation's ID but omit
    # its state. Resolve that receipt once through the same original-ID path.
    check_original = pending or (failed and identifier is not None and state is None)
    while check_original:
        if state == "unknown" and on_unknown is not None:
            on_unknown()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("original_operation_pending")
        if not identifier:
            raise ValueError("missing_original_operation_id")
        polls += 1
        if sample is not None:
            sample["polls"] = polls
        async with asyncio.timeout(remaining):
            receipt, poll_failed = await rpc(
                client,
                "task_query",
                {
                    "operation": "wait",
                    "operation_ids": [identifier],
                    "wait_seconds": int(remaining >= 1),
                    "include_output": False,
                    "include_result": True,
                },
            )
        if time.monotonic() >= deadline:
            raise TimeoutError("original_operation_pending")
        operations = receipt.get("operations")
        # task_query uses isError for both a denied query and a valid receipt
        # whose underlying operation failed. The latter still has a state.
        if poll_failed and "operations" not in receipt:
            raise RuntimeError("original_operation_poll_denied")
        if not isinstance(operations, list) or len(operations) != 1:
            raise ValueError("invalid_original_operation_receipt")
        operation = operations[0]
        if not isinstance(operation, dict):
            raise ValueError("invalid_original_operation_receipt")
        if receipt_identifier(operation) != identifier:
            raise ValueError("original_operation_id_changed")
        state, pending = receipt_state(operation)
        if state is None:
            if poll_failed:
                raise RuntimeError("original_operation_poll_denied")
            raise ValueError("original_operation_state_missing")
        if pending:
            if poll_failed:
                raise ValueError("pending_original_operation_error")
            await asyncio.sleep(min(0.05, max(0, deadline - time.monotonic())))
            continue
        value, bad = terminal_result(operation, state, failed or poll_failed, polled=True)
        if failed and "error" not in value and isinstance(data.get("error"), dict):
            value = {**value, "error": data["error"]}
        return identifier, value, bad, polls, state
    state = state or ("failed" if failed else "succeeded")
    if not failed and identifier is None:
        raise ValueError("missing_original_operation_id")
    value, bad = terminal_result(data, state, failed, polled=False)
    return identifier, value, bad, polls, state


def _stage_interval(sample, name, start, end, field, scale, *, missing):
    if not start or not end or start[field] is None or end[field] is None:
        sample[name + "_unavailable"] = missing
        return
    if start["seq"] >= end["seq"]:
        sample[name + "_unavailable"] = "stage_order"
        return
    interval = (end[field] - start[field]) * scale
    if not math.isfinite(interval) or interval < 0:
        sample[name + "_unavailable"] = "clock_order"
        return
    sample[name + "_ms"] = interval


def _trace_intervals(sample, row, events):
    # 'at' is a Hub observation. An Agent's elapsed_ms belongs to exactly one
    # local attempt; an observed wait is not a substitute for missing acceptance.
    hub = sorted((e for e in events if e["source"] == "hub"), key=lambda e: e["seq"])
    received = next((e for e in hub if e["stage"] == "hub_received"), None)
    dispatch = next((e for e in hub if e["stage"] == "dispatched"), None)
    completed = next((e for e in hub if e["stage"] == "hub_completed"), None)
    _stage_interval(
        sample,
        "hub_first_dispatch",
        received,
        dispatch,
        "at",
        1000,
        missing="missing_received_or_dispatch",
    )
    # Retain the separately named admission-to-dispatch series for historical
    # comparisons. Do not mistake a later retry in a pruned trace for the first send.
    if received or (dispatch and dispatch["seq"] == 1):
        _stage_interval(
            sample,
            "hub_queue",
            {"seq": 0, "at": row["created"]},
            dispatch,
            "at",
            1000,
            missing="missing_created_or_dispatch",
        )
    else:
        sample["hub_queue_unavailable"] = "missing_trace_prefix"
    if "hub_first_dispatch_ms" in sample:
        _stage_interval(
            sample,
            "hub_after_dispatch",
            dispatch,
            completed,
            "at",
            1000,
            missing="missing_completion",
        )
    else:
        sample["hub_after_dispatch_unavailable"] = "unverified_first_dispatch"

    agent = sorted((e for e in events if e["source"] == "agent"), key=lambda e: e["seq"])
    accepted = [e for e in agent if e["stage"] == "accepted"]
    if row["attempts"] == 0:
        sample["agent_queue_unavailable"] = "no_attempt"
        return
    if row["attempts"] != 1 or len(accepted) > 1:
        sample["agent_queue_unavailable"] = "multiple_attempts"
        return
    executing = next((e for e in agent if e["stage"] == "executing"), None)
    _stage_interval(
        sample,
        "agent_queue",
        accepted[0] if accepted else None,
        executing,
        "elapsed_ms",
        1,
        missing="missing_accepted_or_executing",
    )


def enrich(database, samples):
    uri = Path(database).resolve().as_uri()
    with contextlib.closing(sqlite3.connect(f"{uri}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        for sample in samples:
            identifier = sample.get("operation_id")
            if not identifier:
                sample["trace_unavailable"] = "no_operation_id"
                continue
            row = connection.execute(
                "SELECT created,accepted_at,updated,state,attempts FROM operations WHERE id=?",
                (identifier,),
            ).fetchone()
            if row is None:
                sample["trace_unavailable"] = "operation_not_found"
                continue
            # Same bounded timeline exposed by diagnostics; never persist event
            # detail, operation arguments, results, or workspace paths.
            events = [
                dict(e)
                for e in connection.execute(
                    "SELECT source,seq,stage,at,elapsed_ms FROM operation_events "
                    "WHERE operation_id=? ORDER BY id LIMIT 200",
                    (identifier,),
                )
            ]
            sample["operation"] = dict(row)
            sample["events"] = events
            _trace_intervals(sample, row, events)


async def sample_pids(pids, stop, output):
    """Sample explicitly owned PIDs only; CPU is ps lifetime-average percent."""
    while not stop.is_set():
        process = await asyncio.create_subprocess_exec(
            "ps",
            "-o",
            "pid=,pcpu=,rss=",
            "-p",
            ",".join(str(pid) for pid in pids),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), 3)
        except TimeoutError:
            process.kill()
            await process.communicate()
            output.append(
                {"at": time.time(), "loadavg": list(os.getloadavg()), "error": "ps_timeout"}
            )
            return
        entries = []
        for line in stdout.decode().splitlines():
            fields = line.split()
            if len(fields) == 3:
                entries.append(
                    {
                        "role": pids[int(fields[0])],
                        "cpu_percent_lifetime": float(fields[1]),
                        "rss_kib": int(fields[2]),
                    }
                )
        output.append(
            {
                "at": time.time(),
                "loadavg": list(os.getloadavg()),
                "processes": entries,
                "complete": process.returncode == 0 and len(entries) == len(pids),
            }
        )
        try:
            await asyncio.wait_for(stop.wait(), 0.5)
        except TimeoutError:
            pass


async def resource_samples(stack, stop, output):
    await sample_pids({stack.hub.pid: "hub", stack.agent.pid: "agent"}, stop, output)


class FileLane:
    """One fixture file. Its operations never overlap, so SHA checks stay exact."""

    def __init__(self, index):
        self.index = index
        self.path = f"bench/worker-{index}.txt"
        self.current_sha = hashlib.sha256(b"initial\n").hexdigest()


def closed_loop_kind(workload_name, index, step):
    if workload_name == "read_write" and step % 4 == 0:
        return "write"
    if workload_name == "long_short" and index % 8 == 0 and step % 4 == 0:
        return "long"
    return "read"


def open_loop_kind(workload_name, sequence):
    """Same mix as the closed loop by planned order: 1/4 writes, 1/32 long tasks."""
    if workload_name == "read_write" and sequence % 4 == 0:
        return "write"
    if workload_name == "long_short" and sequence % 32 == 0:
        return "long"
    return "read"


def operation_request(kind, path, key, content, current_sha):
    if kind == "write":
        return "write", {
            "project": "ProjectAlpha",
            "path": path,
            "content": content,
            "expected_sha256": current_sha,
            "idempotency_key": key,
        }
    if kind == "long":
        return "exec", {
            "project": "ProjectAlpha",
            "task": "bench_long",
            "yield_seconds": 1,
            "idempotency_key": key,
        }
    return "read", {"project": "ProjectAlpha", "path": path, "idempotency_key": key}


def verify_result(kind, result, lane, content):
    if kind == "read" and result.get("sha256") != lane.current_sha:
        raise ValueError("read_content_hash_mismatch")
    if kind == "write":
        expected = hashlib.sha256(content.encode()).hexdigest()
        if result.get("sha256") != expected:
            raise ValueError("write_content_hash_mismatch")
        lane.current_sha = result["sha256"]


def record_failure(sample, result, state):
    error = result.get("error")
    code = error.get("code", state) if isinstance(error, dict) else state
    if state == "cancelled" or result.get("cancelled") is True:
        sample["state"] = "cancelled"
        code = "CANCELLED"
    elif result.get("timed_out") is True:
        code = "TASK_TIMEOUT"
    sample["error"] = safe_code(code)
    admission_rejection = rejection_metadata(result)
    if admission_rejection.get("admitted") is False and not sample.get("operation_id"):
        sample["admission_rejection"] = admission_rejection


async def recover_sample(client, database, key, sample, request_timeout, exc):
    """Recover ONLY the original ID; never call the original tool again."""
    started = time.monotonic()
    sample["unresolved"] = True
    sample["ok"] = False
    sample["error"] = type(exc).__name__
    recovery_sample = {}
    try:
        identifier = sample.get("operation_id") or lookup_original(database, key)
        sample["operation_id"] = identifier
        _, _, recovery_failed, _, state = await finish_original(
            client,
            {"operation_id": identifier, "pending": True},
            False,
            started + request_timeout,
            sample=recovery_sample,
        )
        sample.update(
            recovered=True,
            unresolved=state in {"needs_review", "interrupted"},
            state=state,
            recovered_failed=recovery_failed,
        )
    except Exception as recovery:
        sample["recovery_error"] = type(recovery).__name__
    finally:
        # Retain safe outcomes and timing separately from failed caller timing.
        sample["recovery_polls"] = recovery_sample.get("polls", 0)
        sample["recovery_ms"] = (time.monotonic() - started) * 1000


async def measure_operation(client, database, lane, kind, content, sample, request_timeout, abort):
    """Run one original operation into ``sample``; shared by both load modes."""
    key = uuid.uuid4().hex
    name, args = operation_request(kind, lane.path, key, content, lane.current_sha)
    deadline = time.monotonic() + request_timeout
    try:
        async with asyncio.timeout(request_timeout):
            data, failed = await rpc(client, name, args)
        sample["operation_id"] = receipt_identifier(data)
        if time.monotonic() >= deadline:
            raise TimeoutError("operation_request_deadline")
        identifier, result, failed, polls, state = await finish_original(
            client, data, failed, deadline, sample=sample, on_unknown=abort.set
        )
        if kind == "long" and (
            result.get("exit_code") != 0
            or result.get("timed_out")
            or result.get("cancelled")
            or result.get("command_ok") is False
        ):
            failed = True
        sample.update(operation_id=identifier, polls=polls, state=state, ok=not failed)
        if failed:
            record_failure(sample, result, state)
            if state in {"needs_review", "interrupted"}:
                sample["unresolved"] = True
                abort.set()
            elif identifier is None and "admission_rejection" not in sample:
                abort.set()
        else:
            verify_result(kind, result, lane, content)
    except Exception as exc:
        # Stop admission for every sender. A missing HTTP response
        # never authorizes another operation; recover this ID only.
        abort.set()
        await recover_sample(client, database, key, sample, request_timeout, exc)


def measurement_client(stack, connections, request_timeout):
    import httpx

    headers = {
        "Authorization": "Bearer " + stack.pat,
        "MCP-Protocol-Version": "2025-11-25",
        "Accept": "application/json, text/event-stream",
    }
    return httpx.AsyncClient(
        base_url=stack.url,
        headers=headers,
        trust_env=False,
        timeout=request_timeout,
        limits=httpx.Limits(max_connections=connections, max_keepalive_connections=connections),
    )


async def warmup(client, request_timeout):
    deadline = time.monotonic() + request_timeout
    async with asyncio.timeout(request_timeout):
        warm, warm_failed = await rpc(
            client,
            "read",
            {"project": "ProjectAlpha", "path": "README.md", "idempotency_key": uuid.uuid4().hex},
        )
    _, _, warm_failed, _, _ = await finish_original(client, warm, warm_failed, deadline)
    if warm_failed or time.monotonic() >= deadline:
        raise RuntimeError("warmup_failed")


def append_jsonl(path, row):
    with path.open("a") as raw:
        raw.write(json.dumps(row, sort_keys=True) + "\n")


def write_jsonl(path, rows):
    with path.open("w") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")


async def workload(stack, output, workload_name, concurrency, operations, request_timeout):
    """Closed loop: fixed workers, each sends its next operation after the last ends."""
    samples, resources = [], []
    stop, abort = asyncio.Event(), asyncio.Event()
    database = stack.hubdir / "hub.sqlite3"
    async with measurement_client(stack, concurrency, request_timeout) as client:
        await warmup(client, request_timeout)
        monitor = asyncio.create_task(resource_samples(stack, stop, resources))
        start = time.monotonic()

        async def worker(index):
            lane = FileLane(index)
            for step in range(operations):
                if abort.is_set():
                    break
                kind = closed_loop_kind(workload_name, index, step)
                began = time.monotonic()
                sample = {
                    "worker": index,
                    "step": step,
                    "kind": kind,
                    "started_at": time.time(),
                    "ok": False,
                    "error": None,
                    "polls": 0,
                }
                content = f"worker {index} step {step}\n"
                await measure_operation(
                    client, database, lane, kind, content, sample, request_timeout, abort
                )
                sample["latency_ms"] = (time.monotonic() - began) * 1000
                samples.append(sample)
                append_jsonl(output / "samples.jsonl", sample)

        try:
            await asyncio.gather(*(worker(index) for index in range(concurrency)))
        finally:
            duration = time.monotonic() - start
            stop.set()
            await monitor
    enrich(database, samples)
    write_jsonl(output / "samples.jsonl", samples)
    write_jsonl(output / "resources.jsonl", resources)
    summary = summarize(samples, duration)
    summary.update(
        mode="closed_loop",
        workload=workload_name,
        concurrency=concurrency,
        operations_per_worker=operations,
        admission_stopped=abort.is_set(),
        expected_samples=concurrency * operations,
        unresolved=sum(bool(s.get("unresolved")) for s in samples),
        resource_sampling_complete=bool(resources)
        and all(r.get("complete", False) for r in resources),
    )
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


MAX_PLANNED_OPERATIONS = 100_000
BUSY_CODES = frozenset({"PROJECT_BUSY", "DEVICE_BUSY", "HUB_BUSY"})
OPERATION_OUTCOMES = ("succeeded", "rejected", "timeout", "cancelled", "failed")
OPEN_LOOP_OUTCOMES = OPERATION_OUTCOMES + (
    "client_dropped",
    "not_sent_after_stop",
)


class Arrival(NamedTuple):
    sequence: int
    offset: float
    phase: str


def arrival_schedule(rate, *, operations=None, duration=None, burst=0):
    """Plan every arrival before sending: ``burst`` at t0, then steady t0 + i/rate.

    ``operations`` is the total including the burst; ``duration`` is the steady
    arrival window. Exactly one of them bounds the plan.
    """
    if type(rate) not in (int, float) or not math.isfinite(rate) or rate <= 0:
        raise ValueError("invalid_arrival_rate")
    if (operations is None) == (duration is None):
        raise ValueError("arrival_needs_operations_or_duration")
    if type(burst) is not int or burst < 0:
        raise ValueError("invalid_burst")
    if operations is not None:
        if type(operations) is not int:
            raise ValueError("invalid_operations")
        steady = operations - burst
    elif type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("invalid_duration")
    else:
        # Round first so 0.1 * 30 plans 3 arrivals, not 4.
        steady = math.ceil(round(duration * rate, 9))
    if steady < 0 or not 1 <= burst + steady <= MAX_PLANNED_OPERATIONS:
        raise ValueError("invalid_arrival_count")
    planned = [Arrival(index, 0.0, "burst") for index in range(burst)]
    planned += [Arrival(burst + index, index / rate, "steady") for index in range(steady)]
    return planned


def planned_record(arrival, kind):
    return {
        "sequence": arrival.sequence,
        "phase": arrival.phase,
        "kind": kind,
        "scheduled_offset_ms": arrival.offset * 1000,
        "ok": False,
        "error": None,
        "polls": 0,
    }


def finish_record(record, completed_ms):
    """End-to-end latency starts at the planned arrival, never at the actual send."""
    record["completed_offset_ms"] = completed_ms
    record["latency_ms"] = completed_ms - record["scheduled_offset_ms"]
    record["service_ms"] = record["latency_ms"] - record["send_lag_ms"]


def ignore_record(record):
    return None


async def drive_open_loop(
    schedule,
    lanes,
    run,
    *,
    abort,
    kind_for,
    emit=ignore_record,
    clock=time.monotonic,
    sleep=asyncio.sleep,
):
    """Send each planned arrival on time; a full client records a drop, never waits.

    Waiting for capacity would silently delay later arrivals and hide queueing
    (coordinated omission). ``lanes`` bounds in-flight work: each lane is one
    fixture file used by at most one operation at a time.
    """
    free = collections.deque(lanes)
    records, tasks = [], []
    start = clock()

    async def launch(record, lane):
        try:
            if abort.is_set():
                record["not_sent"] = "admission_stopped"
                return
            # create_task only reserves capacity. Measure the lag until the
            # coroutine actually starts; queued tasks are still client-side.
            record["send_lag_ms"] = (clock() - start) * 1000 - record["scheduled_offset_ms"]
            await run(record, lane)
        finally:
            if was_sent(record):
                finish_record(record, (clock() - start) * 1000)
            free.append(lane)
            emit(record)

    for arrival in schedule:
        delay = start + arrival.offset - clock()
        if delay > 0 and not abort.is_set():
            await sleep(delay)
        record = planned_record(arrival, kind_for(arrival.sequence))
        records.append(record)
        if abort.is_set():
            record["not_sent"] = "admission_stopped"
        elif not free:
            record.update(
                client_dropped=True,
                error="client_in_flight_limit",
                dropped_offset_ms=(clock() - start) * 1000,
            )
        else:
            lane = free.popleft()
            record["lane"] = lane.index
            tasks.append(asyncio.create_task(launch(record, lane)))
            continue
        emit(record)
    await asyncio.gather(*tasks)
    return records


def operation_outcome(sample):
    if sample.get("ok"):
        return "succeeded"
    error = str(sample.get("error") or "")
    admitted = (sample.get("admission_rejection") or {}).get("admitted")
    if admitted is False or (
        admitted is None and error in BUSY_CODES and not sample.get("operation_id")
    ):
        return "rejected"
    if error in {"QUEUE_EXPIRED", "TIMED_OUT"} or "timeout" in error.lower():
        return "timeout"
    if sample.get("state") == "cancelled" or error in {"CANCELLED", "OPERATION_CANCELLED"}:
        return "cancelled"
    return "failed"


def open_loop_outcome(sample):
    if sample.get("client_dropped"):
        return "client_dropped"
    if sample.get("not_sent"):
        return "not_sent_after_stop"
    return operation_outcome(sample)


def was_sent(sample):
    return "send_lag_ms" in sample


def open_loop_stats(samples, seconds):
    sent = [s for s in samples if was_sent(s)]
    good = [s for s in sent if s.get("ok")]
    outcomes = collections.Counter(open_loop_outcome(s) for s in samples)
    rejected = collections.Counter(
        s.get("error") or "unclassified_error"
        for s in samples
        if open_loop_outcome(s) == "rejected"
    )
    result = {"scheduled": len(samples), "sent": len(sent)}
    result.update({name: outcomes[name] for name in OPEN_LOOP_OUTCOMES})
    result.update(
        errors=len(samples) - len(good),
        error_rate=(len(samples) - len(good)) / len(samples) if samples else None,
        outcome_rates={name: outcomes[name] / len(samples) for name in OPEN_LOOP_OUTCOMES}
        if samples
        else {},
        rejected_by_code=dict(sorted(rejected.items())),
        unresolved=sum(bool(s.get("unresolved")) for s in samples),
        throughput_success_per_second=len(good) / seconds if seconds > 0 else None,
        latency_ms_from_scheduled_all=percentiles([s["latency_ms"] for s in sent]),
        latency_ms_from_scheduled_success=percentiles([s["latency_ms"] for s in good]),
        send_lag_ms=percentiles([s["send_lag_ms"] for s in sent]),
        send_lag_ms_max=round(max(s["send_lag_ms"] for s in sent), 3) if sent else None,
        service_ms=percentiles([s["service_ms"] for s in sent]),
        hub_queue_ms=percentiles([s.get("hub_queue_ms") for s in sent]),
        agent_queue_ms=percentiles([s.get("agent_queue_ms") for s in sent]),
        stage_ms_success=stage_stats(good),
        poll_requests=sum(s.get("polls", 0) for s in samples),
    )
    return result


def sent_offset_ms(sample):
    return sample["scheduled_offset_ms"] + sample["send_lag_ms"]


def backlog_at(samples, at_ms):
    """Sent but incomplete operations at ``at_ms``; age counts from planned arrival."""
    active = [
        s
        for s in samples
        if was_sent(s) and sent_offset_ms(s) <= at_ms < s.get("completed_offset_ms", math.inf)
    ]
    oldest = min((s["scheduled_offset_ms"] for s in active), default=None)
    return {
        "in_flight": len(active),
        "oldest_age_ms": round(at_ms - oldest, 3) if oldest is not None else None,
    }


def peak_in_flight(samples):
    # At equal times a completion (-1) sorts before a send (+1): the driver frees
    # a lane before reusing it.
    events = []
    for sample in samples:
        if was_sent(sample) and sent_offset_ms(sample) < sample["completed_offset_ms"]:
            events.extend(((sent_offset_ms(sample), 1), (sample["completed_offset_ms"], -1)))
    level = peak = 0
    for _, delta in sorted(events):
        level += delta
        peak = max(peak, level)
    return peak


def burst_drain(samples):
    """Time from burst start (t0) until every burst operation has completed."""
    burst = [s for s in samples if s.get("phase") == "burst"]
    if not burst:
        return None
    completed = [
        s["completed_offset_ms"]
        for s in burst
        if "completed_offset_ms" in s and not s.get("unresolved")
    ]
    drained = len(completed) == len(burst)
    return {
        "operations": len(burst),
        "completed": len(completed),
        "succeeded": sum(bool(s.get("ok")) for s in burst),
        "all_completed": drained,
        # A dropped or never-sent burst operation has no drain time.
        "drain_ms": round(max(completed), 3) if drained else None,
    }


def summarize_open_loop(samples, *, arrival_rate, burst, max_in_flight, arrival_duration=None):
    last_arrival = max((s["scheduled_offset_ms"] for s in samples), default=0)
    steady_count = sum(s["phase"] == "steady" for s in samples)
    window = arrival_duration if arrival_duration is not None else steady_count / arrival_rate
    aborted = any(s.get("not_sent") for s in samples)
    observed_end_ms = max(
        (s.get("completed_offset_ms", s.get("dropped_offset_ms", 0)) for s in samples), default=0
    )
    seconds = max(observed_end_ms / 1000, 0 if aborted else window)
    latencies = [s["latency_ms"] for s in samples if was_sent(s)]
    result = {
        "mode": "open_loop",
        "arrival_rate_per_second": arrival_rate,
        "burst": burst,
        "max_in_flight": max_in_flight,
        "offered_operations": len(samples),
        "arrival_window_seconds": window,
        "offered_rate_mean_per_second": len(samples) / window if window > 0 else None,
        "duration_seconds": seconds,
    }
    result.update(open_loop_stats(samples, seconds))
    result.update(
        max_in_flight_observed=peak_in_flight(samples),
        oldest_backlog_age_ms_max=round(max(latencies), 3) if latencies else None,
        backlog_at_last_arrival=backlog_at(samples, last_arrival),
        burst_drain=burst_drain(samples),
        by_kind={
            kind: open_loop_stats([s for s in samples if s.get("kind") == kind], seconds)
            for kind in sorted({s.get("kind", "unspecified") for s in samples})
        },
    )
    return result


async def open_loop_workload(
    stack,
    output,
    *,
    workload_name,
    arrival_rate,
    duration,
    operations,
    burst,
    max_in_flight,
    request_timeout,
):
    """Open loop: fixed arrival rate; latency is measured from each planned arrival."""
    schedule = arrival_schedule(arrival_rate, operations=operations, duration=duration, burst=burst)
    samples, resources = [], []
    stop, abort = asyncio.Event(), asyncio.Event()
    database = stack.hubdir / "hub.sqlite3"
    async with measurement_client(stack, max_in_flight, request_timeout) as client:
        await warmup(client, request_timeout)
        monitor = asyncio.create_task(resource_samples(stack, stop, resources))

        async def run(record, lane):
            record["started_at"] = time.time()
            content = f"lane {lane.index} op {record['sequence']}\n"
            await measure_operation(
                client, database, lane, record["kind"], content, record, request_timeout, abort
            )

        try:
            samples = await drive_open_loop(
                schedule,
                [FileLane(index) for index in range(max_in_flight)],
                run,
                abort=abort,
                kind_for=functools.partial(open_loop_kind, workload_name),
                emit=functools.partial(append_jsonl, output / "samples.jsonl"),
            )
        finally:
            stop.set()
            await monitor
    enrich(database, samples)
    write_jsonl(output / "samples.jsonl", samples)
    write_jsonl(output / "resources.jsonl", resources)
    summary = summarize_open_loop(
        samples,
        arrival_rate=arrival_rate,
        burst=burst,
        max_in_flight=max_in_flight,
        arrival_duration=duration,
    )
    summary.update(
        workload=workload_name,
        steady_duration_seconds=duration,
        planned_operations=len(schedule),
        admission_stopped=abort.is_set(),
        resource_sampling_complete=bool(resources)
        and all(r.get("complete", False) for r in resources),
    )
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return summary


async def disconnect_check(stack):
    """Disconnect an HTTP waiter after admission; recover ONLY its original ID."""
    import httpx

    key = uuid.uuid4().hex
    headers = {
        "Authorization": "Bearer " + stack.pat,
        "MCP-Protocol-Version": "2025-11-25",
        "Accept": "application/json, text/event-stream",
    }
    counter = stack.projectalpha / "bench" / "disconnect-count.txt"
    args = {
        "project": "ProjectAlpha",
        "task": "bench_disconnect",
        "yield_seconds": 10,
        "idempotency_key": key,
    }
    async with httpx.AsyncClient(
        base_url=stack.url, headers=headers, trust_env=False, timeout=90
    ) as client:
        call = asyncio.create_task(rpc(client, "exec", args))
        row = None
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not call.done():
                with sqlite3.connect(
                    f"file:{stack.hubdir / 'hub.sqlite3'}?mode=ro", uri=True
                ) as db:
                    db.row_factory = sqlite3.Row
                    row = db.execute(
                        "SELECT id,state FROM operations WHERE idem=?", (key,)
                    ).fetchone()
                if row is not None and row["state"] == "running":
                    break
                await asyncio.sleep(0.05)
            if call.done():
                # Surface a rejected initial request instead of disguising it as
                # a timing miss; never submit a replacement operation.
                try:
                    _, rejected = await call
                except Exception as exc:
                    return {
                        "exercised": False,
                        "passed": False,
                        "reason": "initial_" + type(exc).__name__,
                    }
                return {
                    "exercised": False,
                    "passed": False,
                    "reason": "initial_request_rejected"
                    if rejected
                    else "initial_waiter_already_returned",
                }
            if row is None or row["state"] != "running":
                return {
                    "exercised": False,
                    "passed": False,
                    "reason": "did_not_observe_active_waiter",
                }
            identifier = row["id"]
            call.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await call
            _, _, failed, polls, state = await finish_original(
                client, {"pending": True, "operation_id": identifier}, False, time.monotonic() + 90
            )
            with sqlite3.connect(f"file:{stack.hubdir / 'hub.sqlite3'}?mode=ro", uri=True) as db:
                count = db.execute(
                    "SELECT COUNT(*) FROM operations WHERE idem=?", (key,)
                ).fetchone()[0]
            effects = len(counter.read_text().splitlines()) if counter.exists() else 0
            return {
                "exercised": True,
                "original_operation_id": identifier,
                "state": state,
                "polls": polls,
                "operation_rows": count,
                "side_effect_count": effects,
                "passed": not failed and count == effects == 1,
            }
        finally:
            if not call.done():
                call.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await call


def task_command(seconds, filename):
    """Synthetic fixture effect uses real newlines so duplicate counts remain exact."""
    return (
        "import pathlib,time; time.sleep("
        + str(seconds)
        + "); p=pathlib.Path("
        + repr("bench/" + filename)
        + '); f=p.open("a"); '
        'f.write("executed"+chr(10)); f.close(); print("done")'
    )


def add_open_loop_arguments(parser):
    parser.add_argument(
        "--mode",
        choices=["closed_loop", "open_loop"],
        default="closed_loop",
        help="closed_loop: fixed workers (default); open_loop: fixed arrival rate, "
        "latency from each planned arrival. Report the two modes separately",
    )
    parser.add_argument(
        "--arrival-rate",
        help="open_loop: comma-separated steady arrival rates, operations/second (0.1..1000)",
    )
    parser.add_argument(
        "--duration", type=float, help="open_loop: steady arrival window seconds (1..3600)"
    )
    parser.add_argument(
        "--operations", type=int, help="open_loop: total planned operations, burst included"
    )
    parser.add_argument(
        "--burst", type=int, help="open_loop: extra operations planned at t0 (0..1024, default 0)"
    )
    parser.add_argument(
        "--max-in-flight",
        type=int,
        help="open_loop: client in-flight bound (1..256, default 128); when full the "
        "arrival is recorded as client_dropped, never delayed",
    )


def record_open_loop_plan(manifest, options, rates):
    if not rates:
        return
    manifest["open_loop"] = {
        "arrival_rates_per_second": rates,
        "steady_duration_seconds": options.duration,
        "planned_operations": options.operations,
        "burst": options.burst,
        "max_in_flight": options.max_in_flight,
    }
    manifest["measurement_notes"].append(
        "Open loop: arrivals planned at t0 + i/rate before sending; latency from planned "
        "arrival; a full client records client_dropped instead of delaying; never pair "
        "with closed-loop cells"
    )


def open_loop_rates(parser, options):
    """Validate open-loop flags; return [] for the default closed-loop mode."""
    if options.mode == "closed_loop":
        flags = [options.arrival_rate, options.duration, options.operations, options.burst]
        if any(value is not None for value in flags + [options.max_in_flight]):
            parser.error("arrival options require --mode open_loop")
        return []
    options.burst = 0 if options.burst is None else options.burst
    options.max_in_flight = 128 if options.max_in_flight is None else options.max_in_flight
    try:
        rates = [float(value) for value in (options.arrival_rate or "").split(",")]
    except ValueError:
        parser.error("invalid arrival rate")
    if (
        len(set(rates)) != len(rates)
        or any(not 0.1 <= rate <= 1000 for rate in rates)
        or not 1 <= options.max_in_flight <= 256
        or not 0 <= options.burst <= 1024
        or (options.duration is not None and not 1 <= options.duration <= 3600)
        or (options.operations is not None and not 1 <= options.operations <= 100000)
    ):
        parser.error("invalid open-loop arrival options")
    for rate in rates:
        try:
            arrival_schedule(
                rate, operations=options.operations, duration=options.duration, burst=options.burst
            )
        except ValueError:
            parser.error("open_loop needs exactly one of --duration/--operations (>= --burst)")
    return rates


def benchmark_cases(options, names, levels, rates):
    """Ordered (label, lanes, runner) cases. A run measures exactly one mode."""
    cases = []
    for name in names:
        for rate in rates:
            runner = functools.partial(
                open_loop_workload,
                workload_name=name,
                arrival_rate=rate,
                duration=options.duration,
                operations=options.operations,
                burst=options.burst,
                max_in_flight=options.max_in_flight,
                request_timeout=options.request_timeout,
            )
            cases.append((f"{name}-open-{rate:g}", options.max_in_flight, runner))
        for concurrency in levels if not rates else []:
            runner = functools.partial(
                workload,
                workload_name=name,
                concurrency=concurrency,
                operations=options.operations_per_worker,
                request_timeout=options.request_timeout,
            )
            cases.append((name + "-" + str(concurrency), concurrency, runner))
    return cases


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        help="New report directory; default is the CodePier user state directory",
    )
    parser.add_argument("--variant", choices=["before", "after"], required=True)
    parser.add_argument("--concurrency", default="8,16,32,64,128")
    parser.add_argument("--workloads", default="read,read_write,long_short")
    parser.add_argument("--operations-per-worker", type=int, default=4)
    parser.add_argument("--request-timeout", type=float, default=120)
    add_open_loop_arguments(parser)
    parser.add_argument(
        "--admission-profile",
        choices=["cold", "ready"],
        default="cold",
        help="cold observes once; ready waits for the real applied adaptive budget",
    )
    parser.add_argument(
        "--readiness-timeout",
        type=float,
        default=300,
        help="Absolute ready preparation budget in seconds (1..300), never a capacity override",
    )
    parser.add_argument("--skip-disconnect-check", action="store_true")
    parser.add_argument("--preflight-seconds", type=float, default=5)
    parser.add_argument("--max-load-per-cpu", type=float, default=1)
    parser.add_argument(
        "--allow-noisy",
        action="store_true",
        help="Record a noisy diagnostic, never a clean comparison",
    )
    parser.add_argument(
        "--host-context",
        required=True,
        help="Known concurrent production/test activity, never assume quiet",
    )
    options = parser.parse_args()
    repository = options.repository.resolve()
    cache = (
        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        / "codepier"
        / "benchmarks"
    )
    output = (options.output or cache / time.strftime("hub-agent-%Y%m%dT%H%M%S")).resolve()
    if not (repository / ".git").exists():
        parser.error("repository must be an existing Git checkout")
    if output.exists():
        parser.error("output must be a new directory")
    levels = [int(v) for v in options.concurrency.split(",")]
    names = options.workloads.split(",")
    if (
        not levels
        or len(set(levels)) != len(levels)
        or len(set(names)) != len(names)
        or any(v not in {1, 8, 16, 32, 64, 128} for v in levels)
        or not set(names) <= {"read", "read_write", "long_short"}
    ):
        parser.error("invalid workload or concurrency")
    if (
        not 1 <= options.operations_per_worker <= 32
        or not 10 <= options.request_timeout <= 300
        or not 1 <= options.readiness_timeout <= 300
        or not 0 <= options.preflight_seconds <= 60
        or not 0.1 <= options.max_load_per_cpu <= 10
    ):
        parser.error("invalid operation count or timeout")
    rates = open_loop_rates(parser, options)
    # The chosen checkout owns every product import and subprocess cwd. The
    # harness remains byte-identical across variants and records its own SHA.
    sys.path.insert(0, str(repository))
    from tests.support import running_stack
    from shared.util import atomic_json

    output.mkdir(mode=0o700, parents=True)
    print(f"Benchmark report directory: {output}", file=sys.stderr)
    manifest = {
        "variant": options.variant,
        "repository": git_identity(repository),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "python": sys.version,
        "platform": platform.platform(),
        "cpu_count": os.cpu_count(),
        "host_context": options.host_context,
        "started_at": time.time(),
        "loadavg_start": list(os.getloadavg()),
        "mode": options.mode,
        "workloads": names,
        "concurrency": levels,
        "operations_per_worker": options.operations_per_worker,
        "request_timeout_seconds": options.request_timeout,
        "admission_profile": options.admission_profile,
        "readiness_timeout_seconds": options.readiness_timeout,
        "admission_cases": [],
        "measurement_notes": [
            "Loopback fixture, not ChatGPT host or public WAN",
            "cold observes admission once; ready waits only for a real authenticated adaptive budget",
            "Ready preparation and before/after budget observations are outside measured workload time",
            "The live admission budget may shrink during a case; every rejection remains an error",
            "Hub first dispatch: hub_received to first dispatched, Hub clock only",
            "Agent queue: relative accepted to executing within one attempt",
            "Resource CPU is ps lifetime-average; RSS KiB only Hub+Agent, not aggregate child tree",
            "Percentiles use nearest rank; errors remain in all-latency samples",
        ],
    }
    record_open_loop_plan(manifest, options, rates)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    load_samples, overloaded = preflight(options.preflight_seconds, options.max_load_per_cpu)
    manifest.update(
        preflight=load_samples,
        overloaded=overloaded,
        max_load_per_cpu=options.max_load_per_cpu,
        allow_noisy=options.allow_noisy,
    )
    if overloaded and not options.allow_noisy:
        manifest.update(result="blocked_by_host_load", ended_at=time.time())
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return 2
    summaries = []
    usage_start = resource.getrusage(resource.RUSAGE_CHILDREN)
    try:
        with running_stack(output / "fixture") as stack:
            bench = stack.projectalpha / "bench"
            bench.mkdir()
            stack.stop_agent()
            for task, seconds, filename in [
                ("bench_long", 0.4, "long-count.txt"),
                ("bench_disconnect", 2, "disconnect-count.txt"),
            ]:
                command = task_command(seconds, filename)
                stack.config["tasks"][task] = {
                    "command": [sys.executable, "-u", "-c", command],
                    "projects": ["ProjectAlpha"],
                    "timeout": 30,
                    "allow_read_concurrency": True,
                }
            atomic_json(stack.config_path, stack.config)
            stack.start_agent()
            for label, lanes, runner in benchmark_cases(options, names, levels, rates):
                for index in range(lanes):
                    (bench / f"worker-{index}.txt").write_bytes(b"initial\n")
                directory = output / label
                directory.mkdir()
                # One completed read is protocol warmup, not adaptive-budget
                # readiness. Record every case's actual preparation state.
                admission = {"case": label, "started_at": time.time()}
                manifest["admission_cases"].append(admission)
                try:
                    admission["before"] = prepare_admission(
                        lambda **kwargs: observe_admission(stack, **kwargs),
                        lanes,
                        profile=options.admission_profile,
                        timeout=options.readiness_timeout,
                    )
                except AdmissionReadinessError as exc:
                    admission["before"] = exc.observation
                    raise
                summary = asyncio.run(runner(stack, directory))
                admission["after"] = prepare_admission(
                    lambda **kwargs: observe_admission(stack, **kwargs),
                    lanes,
                    profile="cold",
                    timeout=options.readiness_timeout,
                )
                admission["ended_at"] = time.time()
                summary["admission"] = admission
                (directory / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
                summaries.append(summary)
                print(json.dumps(summary), flush=True)
                with sqlite3.connect(
                    f"file:{stack.hubdir / 'hub.sqlite3'}?mode=ro", uri=True
                ) as db:
                    active = db.execute(
                        "SELECT COUNT(*) FROM operations WHERE state IN ('queued','running','reconnecting','cancelling','unknown')"
                    ).fetchone()[0]
                if active or summary["admission_stopped"]:
                    manifest.update(
                        result="stopped_with_unresolved_operations",
                        active_operations=active,
                        ended_at=time.time(),
                    )
                    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
                    (output / "summary.json").write_text(json.dumps(summaries, indent=2) + "\n")
                    raise RuntimeError(
                        "Fixture operations remain unresolved; do not contaminate the next workload"
                    )
            if not options.skip_disconnect_check:
                manifest["disconnect"] = asyncio.run(disconnect_check(stack))
            else:
                manifest["disconnect"] = {
                    "exercised": False,
                    "passed": False,
                    "reason": "explicitly_skipped",
                }
    except Exception as exc:
        manifest.setdefault(
            "result", "readiness_not_met" if isinstance(exc, AdmissionReadinessError) else "failed"
        )
        manifest["error"] = type(exc).__name__
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    manifest.update(
        ended_at=time.time(),
        loadavg_end=list(os.getloadavg()),
        child_cpu_user_seconds=usage.ru_utime - usage_start.ru_utime,
        child_cpu_system_seconds=usage.ru_stime - usage_start.ru_stime,
        child_maxrss_native_units=usage.ru_maxrss,
    )
    manifest.setdefault(
        "result",
        "completed_with_errors"
        if any(s["errors"] or not s["resource_sampling_complete"] for s in summaries)
        or not manifest.get("disconnect", {}).get("passed")
        else "completed",
    )
    manifest["repository_end"] = git_identity(repository)
    manifest["source_unchanged"] = manifest["repository"] == manifest["repository_end"]
    if not manifest["source_unchanged"]:
        manifest["result"] = "source_changed_during_run"
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (output / "summary.json").write_text(json.dumps(summaries, indent=2) + "\n")

    return 0 if manifest["result"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
