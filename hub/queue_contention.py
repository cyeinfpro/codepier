"""Instance-admin queue diagnostics. No commands, credentials or payload bodies."""

from __future__ import annotations
from collections import Counter, defaultdict
import json
import math
import time

WAIT_STAGES = {"waiting_resource", "waiting_worker", "waiting_project"}
TERMINAL = {"succeeded", "failed", "cancelled", "needs_review", "interrupted"}


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
    since = now - 86400
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
        "window_seconds": 86400,
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
