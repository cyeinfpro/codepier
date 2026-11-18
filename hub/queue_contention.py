"""Instance-admin queue diagnostics. No commands, credentials or payload bodies."""

from __future__ import annotations
from collections import Counter, defaultdict
import json
import math
import time

from hub.worker_metrics import percentiles

WAIT_STAGES = {"waiting_resource", "waiting_worker", "waiting_project"}
TERMINAL = {"succeeded", "failed", "cancelled", "needs_review", "interrupted"}
WINDOW_SECONDS = 86400
ADMISSION_CONDITIONS = {
    "adaptive",
    "legacy_or_warming",
    "unavailable",
    "pressure",
    "recovering",
}
WORKLOADS = {
    "short_read": {"read", "fs_read", "fs_read_many", "fs_tree"},
    "file_write": {"write", "edit", "apply_patch", "fs_write", "fs_edit", "fs_upload"},
    "task_or_exec": {"tasks_run", "exec", "shell_exec", "ssh_exec", "vps_exec", "validation_run"},
}
TIMEOUTS = {
    "QUEUE_EXPIRED",
    "TIMEOUT",
    "ARTIFACT_TIMEOUT",
    "BROWSER_TIMEOUT",
    "COMPUTER_TIMEOUT",
    "PARSER_TIMEOUT",
    "PROJECT_DISCOVERY_TIMEOUT",
    "RUNTIME_PREPARATION_TIMEOUT",
    "SSH_TIMEOUT",
}


def queue_contention_report(store, *, now=None, top_n=10):
    """Rolling 24h accepted cohort; terminal/live waits, last reason attribution.

    Caller must enforce instance-admin authorization. Times are Hub observations,
    not exact cross-device clocks. Multiple blockers use first-blocker attribution.
    """
    now = time.time() if now is None else now
    if not isinstance(now, (int, float)) or not math.isfinite(now):
        raise ValueError("Finite observation time required")
    if type(top_n) is not int or not 1 <= top_n <= 100:
        raise ValueError("top_n must be between 1 and 100")
    since = now - WINDOW_SECONDS
    rows = store.all(
        """SELECT o.id,o.project_id,o.state,o.created,o.updated,o.accepted_at
        FROM operations o WHERE (o.accepted_at>=? AND o.accepted_at<=?)
           OR (o.state NOT IN ('succeeded','failed','cancelled','needs_review','interrupted')
               AND o.created<=?)""",
        (since, now, now),
    )
    # Include earlier blocker history, but only referenced operations and their
    # ancestors are loaded lazily. No broad operation payload read is needed.
    cache = {row["id"]: [] for row in rows}
    identifiers = list(cache)
    # Batch candidate event reads rather than one SQL call per operation.
    for offset in range(0, len(identifiers), 400):
        batch = identifiers[offset : offset + 400]
        marks = ",".join("?" for _ in batch)
        for event in store.all(
            "SELECT operation_id,stage,at,detail FROM operation_events "
            f"WHERE operation_id IN ({marks}) AND at<=? "
            "AND stage IN ('accepted','executing','waiting_resource','waiting_worker','waiting_project') "
            "ORDER BY at,id",
            (*batch, now),
        ):
            cache[event["operation_id"]].append(event)

    def events(identifier):
        if identifier not in cache:
            cache[identifier] = store.all(
                """SELECT stage,at,detail FROM operation_events
                WHERE operation_id=? AND at<=?
                AND stage IN ('accepted','executing','waiting_resource','waiting_worker','waiting_project')
                ORDER BY at,id""",
                (identifier, now),
            )
        return cache[identifier]

    def timing(identifier):
        ev = events(identifier)
        accepted = next((e["at"] for e in ev if e["stage"] == "accepted"), None)
        executing = next((e["at"] for e in ev if e["stage"] == "executing"), None)
        waits = [
            e
            for e in ev
            if e["stage"] in WAIT_STAGES and (executing is None or e["at"] <= executing)
        ]
        return accepted, executing, waits[-1] if waits else None

    def detail(event):
        try:
            data = json.loads(event["detail"]) if event else {}
            return data if isinstance(data, dict) else {}
        except (ValueError, TypeError):
            return {}

    def blocker(event):
        values = detail(event).get("blocked_by", [])
        if not isinstance(values, list) or not values:
            return None
        first = values[0]
        return (
            first
            if isinstance(first, str)
            else first.get("operation_id")
            if isinstance(first, dict)
            else None
        )

    reasons, roots, root_counts = Counter(), Counter(), Counter()
    active_projects, active_holders = defaultdict(set), defaultdict(set)
    slow, operations, over10, total_resource, convoy = [], 0, 0, 0.0, 0.0
    for row in rows:
        accepted, executing, wait = timing(row["id"])
        accepted = accepted if accepted is not None else row.get("accepted_at")
        # Ratio uses the accepted cohort, not only operations with wait events.
        cohort = accepted is not None and since <= accepted <= now
        if cohort:
            operations += 1
        active = row["state"] not in TERMINAL and executing is None and wait is not None
        end = executing if executing is not None else now if active else row["updated"]
        start = accepted if accepted is not None else wait["at"] if wait else end
        seconds = max(0.0, min(end, now) - max(start, since))
        if cohort and seconds > 10:
            over10 += 1
        if wait is not None:
            info = detail(wait)
            reasons[
                str(info.get("queue_reason") or wait["stage"])
                + ":"
                + str(info.get("lane") or "unknown")
            ] += seconds
            direct = blocker(wait)
            if wait["stage"] == "waiting_resource" and direct:
                total_resource += seconds
                _, direct_execution, _ = timing(direct)
                if direct_execution is None or direct_execution > wait["at"]:
                    convoy += seconds
                root, seen = direct, {row["id"]}
                while root not in seen:
                    seen.add(root)
                    next_root = blocker(timing(root)[2])
                    if not next_root or next_root in seen:
                        break
                    root = next_root
                roots[root] += seconds
                root_counts[root] += 1
                if active:
                    active_holders[root].add(row["id"])
            if active:
                active_projects[row["project_id"]].add(row["id"])
                if seconds > 60:
                    slow.append(row["id"])
    return {
        "window_seconds": WINDOW_SECONDS,
        "observed_at": now,
        "operations": operations,
        "waits_over_10s": over10,
        "waits_over_10s_ratio": over10 / operations if operations else 0,
        "wait_hours_by_reason": {key: value / 3600 for key, value in sorted(reasons.items())},
        "convoy_ratio": convoy / total_resource if total_resource else 0,
        "root_blockers": [
            {"operation_id": key, "wait_seconds": value, "blocked_operations": root_counts[key]}
            for key, value in roots.most_common(top_n)
        ],
        "alerts": {
            "operations_over_60s": sorted(slow),
            "projects_over_5": [
                {"project_id": key, "waiting_operations": len(value)}
                for key, value in active_projects.items()
                if len(value) > 5
            ],
            "holders_over_5": [
                {"operation_id": key, "blocked_operations": len(value)}
                for key, value in active_holders.items()
                if len(value) > 5
            ],
        },
        "method": "accepted cohort; clipped waits; last reason; first-blocker ancestry",
    }


def baseline_admission_detail(online, budget):
    reason = "legacy_or_warming" if budget.reason == "legacy" else budget.reason
    return {
        "baseline_connection": "online" if online else "offline",
        "baseline_admission": reason if reason in ADMISSION_CONDITIONS else "unknown",
    }


def baseline_completion_detail(result):
    code = (result.get("error") or {}).get("code")
    timed_out = code in TIMEOUTS or (result.get("data") or {}).get("timed_out") is True
    return {"baseline_timeout": timed_out}


def _event_object(event):
    if event is None:
        return {}
    try:
        value = json.loads(event["detail"])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _event_duration(start, end, field, unit):
    if start and end and start[field] is not None and end[field] is not None:
        elapsed = (end[field] - start[field]) * unit
        if math.isfinite(elapsed) and elapsed >= 0:
            return elapsed
    return None


def _agent_startup(events):
    accepted = None
    for event in events:
        if event["stage"] == "accepted":
            accepted = event
        elif event["stage"] == "executing":
            # A recovery may create another acceptance for the same operation.
            # Pair the first observed execution with its nearest acceptance by
            # Agent sequence, using only its monotonic elapsed time.
            return _event_duration(accepted, event, "elapsed_ms", 1)
    return None


def _trace_samples(store, rows, now):
    """Fetch only the stage metadata required by the offline report."""
    for offset in range(0, len(rows), 400):
        batch = rows[offset : offset + 400]
        marks = ",".join("?" for _ in batch)
        events = defaultdict(list)
        for event in store.all(
            f"""SELECT operation_id,source,stage,at,elapsed_ms,
            CASE WHEN source='hub' THEN detail END AS detail FROM operation_events
            WHERE operation_id IN ({marks}) AND at<=? AND (
                (source='hub' AND stage IN ('hub_received','dispatched','hub_completed')) OR
                (source='agent' AND stage IN ('accepted','executing'))
            ) ORDER BY operation_id,source,seq""",
            (*(row["id"] for row in batch), now),
        ):
            events[event["operation_id"]].append(event)
        for row in batch:
            stages = events[row["id"]]
            agent = [event for event in stages if event["source"] == "agent"]
            hub = {}
            for event in stages:
                if event["source"] == "hub":
                    if event["stage"] == "hub_completed":
                        hub[event["stage"]] = event
                    else:
                        hub.setdefault(event["stage"], event)
            yield row, hub, agent


class _BaselineBucket:
    def __init__(self):
        self.counts = Counter()
        self.timings = {
            key: [] for key in ("hub_first_dispatch", "agent_to_executing", "hub_to_completion")
        }
        self.oldest = None

    def result(self):
        states = {state: self.counts[state] for state in sorted(TERMINAL)}
        return {
            "admitted": self.counts["admitted"],
            "completed": sum(states.values()),
            "terminal_by_state": states,
            "successful_per_second": self.counts["succeeded"] / WINDOW_SECONDS,
            "timeouts_observed": self.counts["timeouts"],
            "timeout_classification_unknown": self.counts["timeout_unknown"],
            "queued_now": self.counts["queued_now"],
            "oldest_queued_age_seconds": self.oldest,
            "success_latency_ms": {
                key: {"n": len(samples), **percentiles(samples)}
                for key, samples in self.timings.items()
            },
        }

    def observe(self, row, hub, agent, now, since):
        if row["created"] > since:
            self.counts["admitted"] += 1
        if row["state"] == "queued" and not any(e["stage"] == "executing" for e in agent):
            self.counts["queued_now"] += 1
            self.oldest = max(self.oldest or 0, now - row["created"])
        if row["state"] not in TERMINAL or not since < row["updated"] <= now:
            return
        self.counts[row["state"]] += 1
        completed = hub.get("hub_completed")
        # needs_review may retain an older completion, then be reconciled.
        # Use only a completion recorded after the current terminal update.
        if completed is not None and completed["at"] < row["updated"]:
            completed = None
        timeout = _event_object(completed).get("baseline_timeout")
        if timeout is True:
            self.counts["timeouts"] += 1
        elif timeout is not False and row["state"] != "succeeded":
            self.counts["timeout_unknown"] += 1
        if row["state"] == "succeeded":
            received = hub.get("hub_received")
            durations = {
                "hub_first_dispatch": _event_duration(received, hub.get("dispatched"), "at", 1000),
                "agent_to_executing": _agent_startup(agent),
                "hub_to_completion": _event_duration(received, completed, "at", 1000),
            }
            for key, duration in durations.items():
                if duration is not None:
                    self.timings[key].append(duration)


def baseline_window_report(store, *, now=None, device_id=None, project_id=None):
    """Read one snapshot of durable operations; each ID appears once per cohort."""
    now = time.time() if now is None else now
    if not isinstance(now, (float, int)) or not math.isfinite(now):
        raise ValueError("Finite observation time required")
    since = now - WINDOW_SECONDS
    scope = []
    params = [now, since, since, now]
    if device_id is not None:
        scope.append(" AND device_id=?")
        params.append(device_id)
    if project_id is not None:
        scope.append(" AND project_id=?")
        params.append(project_id)
    rows = store.all(
        """SELECT id,tool,state,created,updated FROM operations WHERE created<=? AND (
            created>? OR state='queued' OR (
                state IN ('succeeded','failed','cancelled','needs_review','interrupted')
                AND updated>? AND updated<=?
            )
        )"""
        + "".join(scope),
        params,
    )
    all_operations = _BaselineBucket()
    workloads = {key: _BaselineBucket() for key in (*WORKLOADS, "other")}
    by_condition = defaultdict(_BaselineBucket)
    for row, hub, agent in _trace_samples(store, rows, now):
        workload = next((key for key, tools in WORKLOADS.items() if row["tool"] in tools), "other")
        admission = _event_object(hub.get("hub_received"))
        connection = admission.get("baseline_connection")
        condition = admission.get("baseline_admission")
        connection = connection if connection in ("online", "offline") else "unknown"
        condition = (
            condition
            if isinstance(condition, str) and condition in ADMISSION_CONDITIONS
            else "unknown"
        )
        all_operations.observe(row, hub, agent, now, since)
        workloads[workload].observe(row, hub, agent, now, since)
        by_condition[workload, connection, condition].observe(row, hub, agent, now, since)
    return {
        "window_seconds": WINDOW_SECONDS,
        "window_start_exclusive": since,
        "observed_at": now,
        "scope": {"device_id": device_id, "project_id": project_id},
        "total": all_operations.result(),
        "by_workload": {key: bucket.result() for key, bucket in workloads.items()},
        "by_admission_condition": [
            {
                "workload": workload,
                "connection": connection,
                "admission": condition,
                **bucket.result(),
            }
            for (workload, connection, condition), bucket in sorted(by_condition.items())
        ],
        "not_observed": {
            "admission_rejections": "Rejected requests do not create a durable operation.",
            "client_timeouts_and_latency": "The database records Hub-side observations only.",
        },
    }
