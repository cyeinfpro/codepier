"""Pure MCP context scenarios over authorized, sanitized activity statistics.

Only sanitized activity counters/correlation fields are accepted. No provider
usage, conversation text, credential, actual cache behavior or bill is inferred.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import math
import heapq

VERSION = "codepier-context-v1"
POLL_TOOLS = frozenset({"operations_get", "operations_wait", "operations_list",
                       "process_get", "process_wait", "task_query", "process"})
MAX_EVENTS = 10_000


@dataclass(frozen=True)
class Scenario:
    name: str
    base_tokens: int
    context_trigger: int
    retention: float
    recent_rounds: int
    burst_seconds: float
    parallel_seconds: float
    poll_seconds: float
    unknown_idle_seconds: int
    hidden_initial: int
    hidden_user: int
    hidden_output: int
    tail_output: int
    wire_field: str = "estimated_tokens"


SCENARIOS = (
    Scenario("low", 6000, 32000, .95, 4, 4, 8, 30, 900, 160, 20, 40, 240, "low"),
    Scenario("mid", 12000, 64000, .98, 8, 1, 4, 10, 1800, 240, 40, 120, 400),
    Scenario("high", 24000, 128000, .995, 16, 0, 0, 0, 7200, 500, 100, 400, 900, "high"),
)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _metric(row, direction, field):
    side = (row.get("token_usage") or {}).get(direction) or {}
    value = side.get(field)
    if not _number(value):
        value = side.get("estimated_tokens")
    return float(value) if _number(value) else 0.0


def _id(value):
    # Correlation identifiers never leave this function/module in results.
    return value if isinstance(value, (str, int)) else None


def normalize(rows):
    """Deduplicate activity/page IDs, never operation IDs or repeated payloads."""
    if len(rows) > MAX_EVENTS:
        raise ValueError("Replay at most 10000 authorized activity rows")
    seen, result = set(), []
    for index, row in enumerate(rows):
        activity_id = _id(row.get("id"))
        if activity_id is not None and activity_id in seen:
            continue
        if activity_id is not None:
            seen.add(activity_id)
        started = row.get("started")
        if not _number(started):
            raise ValueError("Every activity requires a finite start timestamp")
        usage = row.get("token_usage") or {}
        metrics = {}
        for direction in ("input", "output"):
            side = usage.get(direction) or {}
            metrics[direction] = {key: side.get(key) for key in
                                  ("estimated_tokens", "low", "high", "state",
                                   "source_truncated", "truncated")}
        duration = row.get("service_ms")
        duration = float(duration) / 1000 if _number(duration) else 0.0
        result.append({
            "id": activity_id if activity_id is not None else ("missing", index),
            "started": float(started), "ended": float(started) + duration,
            "tool": str(row.get("tool") or ""),
            # Mapping boundaries and grants are strict isolation, not evidence
            # that they represent a single conversation.
            "scope": tuple(_id(row.get(key)) for key in
                           ("grant_id", "actor", "project", "root", "device")),
            "window_key": _id(row.get("window_key")),
            "round_hint": _id(row.get("round_hint")),
            "poll_key": _id(row.get("poll_key")),
            "token_usage": metrics,
        })
    return sorted(result, key=lambda r: (r["started"], str(r["id"])))


def sessions(rows, scenario):
    """Opaque-window streams; absent windows use labeled activity bursts.

    The existing window HMAC changes on Hub restart and includes project
    mapping. Never merge changed keys or join projects as a real host session.
    Calendar midnight is deliberately irrelevant. Missing-window interleaved
    conversations cannot be recovered: scenario replay bounds assumptions.
    """
    groups = defaultdict(list)
    for row in rows:
        groups[(row["scope"], row["window_key"])].append(row)
    result = []
    for (_, window), items in groups.items():
        current = []
        for row in items:
            # Stable host-window keys remain continuous for retained history.
            # Unknown host traffic resets after an assumed idle activity gap.
            if current and window is None and row["started"] - current[-1]["ended"] > scenario.unknown_idle_seconds:
                result.append(current)
                current = []
            current.append(row)
        if current:
            result.append(current)
    return sorted(result, key=lambda values: values[0]["started"])


def _rounds(items, scenario):
    result = []
    meta = None
    for row in items:
        poll = row["tool"] in POLL_TOOLS
        merge = False
        if meta:
            span = row["started"] - meta["started"]
            if row["round_hint"] is not None:
                # JSON-RPC request_id is NOT a trustworthy model-turn hint.
                merge = meta["round_hint"] == row["round_hint"]
            elif meta["round_hint"] is None:
                all_poll = poll and meta["all_poll"]
                same_poll = meta["poll_key"] == row["poll_key"]
                if all_poll and same_poll and scenario.poll_seconds > 0:
                    merge = span <= scenario.poll_seconds
                elif not meta["any_poll"] and not poll:
                    overlap = row["started"] < meta["ended"] and span <= scenario.parallel_seconds
                    burst = span <= scenario.burst_seconds
                    merge = (scenario.parallel_seconds > 0 and overlap) or (scenario.burst_seconds > 0 and burst)
        if merge:
            result[-1].append(row)
            meta["ended"] = max(meta["ended"], row["ended"])
            meta["all_poll"] = meta["all_poll"] and poll
            meta["any_poll"] = meta["any_poll"] or poll
        else:
            result.append([row])
            meta = dict(started=row["started"], ended=row["ended"],
                        round_hint=row["round_hint"], all_poll=poll, any_poll=poll,
                        poll_key=row["poll_key"])
    return result


class _History:
    def __init__(self, scenario):
        self.scenario = scenario
        self.recent = deque()
        self.old = 0.0
        self.compressions = 0

    def advance(self):
        self.old *= self.scenario.retention
        while len(self.recent) > self.scenario.recent_rounds:
            self.old += self.recent.popleft()

    def total(self):
        return self.old + sum(self.recent)

    def fit(self, budget):
        budget = max(0.0, budget)
        if self.total() <= budget:
            return
        self.compressions += 1
        # Preserve the newest consumed text up to 80% of a half-full target,
        # summarize older consumed text at 15%, and leave growth headroom.
        target = budget * .5
        kept, remaining = deque(), target * .8
        for value in reversed(self.recent):
            retained = min(value, remaining)
            if retained:
                kept.appendleft(retained)
            remaining -= retained
        older = max(0.0, self.total() - sum(kept))
        self.recent = kept
        self.old = min(older * .15, max(0.0, target - sum(kept)))

    def add(self, value):
        self.recent.append(value)


def replay(rows, scenario=SCENARIOS[1], *, include_tail=True):
    """Pure chronological rebuild: provisional tails are replaced on new data.

    No caller may add replay outputs to prior totals. Recompute from an
    authorized history prefix (including pre-period warm-up), then bucket
    model-evaluation contributions. This avoids midnight/page-reset inflation.
    """
    rows = normalize(rows)
    contributions = []
    grouped = sessions(rows, scenario)
    compressions = 0
    for stream_index, items in enumerate(grouped):
        history = _History(scenario)
        pending_returns = []
        observed_payload_sides = 0
        rounds = _rounds(items, scenario)
        for round_index, batch in enumerate(rounds):
            initial = round_index == 0
            poll = all(row["tool"] in POLL_TOOLS for row in batch)
            hidden_user = scenario.hidden_initial if initial else (0 if poll else scenario.hidden_user)
            arguments = sum(_metric(row, "input", scenario.wire_field) for row in batch)
            pending = 0.0
            observed_payload_sides += sum(
                _number((row["token_usage"].get("input") or {}).get("estimated_tokens"))
                for row in batch)
            while pending_returns and pending_returns[0][0] <= batch[0]["started"]:
                _, tokens, measured = heapq.heappop(pending_returns)
                pending += tokens
                observed_payload_sides += measured
            hidden_output = scenario.hidden_output * (.25 if poll else 1)
            output = arguments + hidden_output
            history.advance()
            fresh = pending + hidden_user
            history.fit(scenario.context_trigger - scenario.base_tokens - fresh)
            input_tokens = scenario.base_tokens + history.total() + fresh
            contributions.append({
                "started": batch[0]["started"], "stream": stream_index,
                "round": round_index, "calls": len(batch), "tail": False,
                # Evidence belongs to this stream and this chronological prefix.
                # Neither another window nor a future return can enable it.
                "observed_payload_sides": observed_payload_sides,
                "input_tokens": round(input_tokens),
                "output_tokens": round(output),
                "fresh_tool_input_tokens": pending,
                "history_input_tokens": round(history.total()),
                "base_input_tokens": scenario.base_tokens,
                "fresh_over_trigger": fresh + scenario.base_tokens > scenario.context_trigger,
            })
            # Request args are output now and prior assistant history later;
            # returned tool text is input only on the next evaluation.
            history.add(fresh + output)
            for row in batch:
                measured = _number((row["token_usage"].get("output") or {}).get("estimated_tokens"))
                heapq.heappush(pending_returns, (
                    row["ended"], _metric(row, "output", scenario.wire_field), measured))
        if include_tail and rounds:
            pending = sum(item[1] for item in pending_returns)
            observed_payload_sides += sum(item[2] for item in pending_returns)
            history.advance()
            history.fit(scenario.context_trigger - scenario.base_tokens - pending)
            contributions.append({
                "started": max(row["ended"] for row in items),
                "stream": stream_index, "round": len(rounds), "calls": 0, "tail": True,
                "observed_payload_sides": observed_payload_sides,
                "input_tokens": round(scenario.base_tokens + history.total() + pending),
                "output_tokens": scenario.tail_output,
                "fresh_tool_input_tokens": pending,
                "history_input_tokens": round(history.total()),
                "base_input_tokens": scenario.base_tokens,
                "fresh_over_trigger": pending + scenario.base_tokens > scenario.context_trigger,
            })
        compressions += history.compressions
    contributions.sort(key=lambda value: (value["started"], value["stream"], value["round"]))
    inputs = round(sum(row["input_tokens"] for row in contributions))
    outputs = round(sum(row["output_tokens"] for row in contributions))
    return {
        "scenario": scenario.name, "input_tokens": inputs, "output_tokens": outputs,
        "total_tokens": inputs + outputs, "streams": len(grouped),
        "tool_rounds": sum(not row["tail"] for row in contributions),
        "provisional_tails": sum(row["tail"] for row in contributions),
        "compressions": compressions,
        "max_input_tokens": max((row["input_tokens"] for row in contributions), default=0),
        "contributions": contributions,
    }


def reference_cost(value, *, input_rate, cached_input_rate, output_rate, cache_percent=90):
    """Injected USD/million rates. No internet, price catalog or side effects."""
    if type(cache_percent) is not int or not 0 <= cache_percent <= 100:
        raise ValueError("Cache percentage must be an integer from 0 to 100")
    rates = [Decimal(str(x)) for x in (input_rate, cached_input_rate, output_rate)]
    if any(not rate.is_finite() or rate < 0 for rate in rates):
        raise ValueError("Rates must be nonnegative finite USD per million")
    inputs, outputs = (Decimal(str(value[key])) for key in ("input_tokens", "output_tokens"))
    if any(not x.is_finite() or x < 0 for x in (inputs, outputs)):
        raise ValueError("Counts must be finite and nonnegative")
    effective = (rates[0] * (100-cache_percent) + rates[1] * cache_percent) / 100
    amount = (inputs * effective + outputs * rates[2]) / 1_000_000
    return {"amount_pico_usd": int((amount * 1_000_000_000_000).quantize(Decimal(1), rounding=ROUND_HALF_UP)),
            "estimated_usd": float(amount), "cache_read_percent": cache_percent,
            "estimated_cached_input_tokens": float(inputs * cache_percent / 100),
            "actual_usage": None, "actual_cache_hit_rate": None,
            "reasoning_tokens": None, "cache_write_tokens": None, "billing_total": None}


def estimate_context(rows):
    """Three plausible scenarios, an envelope, and explicit numeric coverage."""
    cleaned = normalize(rows)
    results = [replay(rows, scenario) for scenario in SCENARIOS]
    missing, truncated = 0, 0
    for row in cleaned:
        for side in row["token_usage"].values():
            missing += not _number(side.get("estimated_tokens"))
            truncated += bool(side.get("truncated") or side.get("source_truncated"))
    midpoint = results[1]["total_tokens"]
    return {
        "version": VERSION, "kind": "context_scenario", "actual_usage": None,
        "estimated_tokens": midpoint,
        "low": min(value["total_tokens"] for value in results),
        "high": max(value["total_tokens"] for value in results),
        "scenarios": results,
        "coverage": {
            "wire_attempts": len(cleaned),
            "window_correlated_attempts": sum(row["window_key"] is not None for row in cleaned),
            "inferred_context_attempts": sum(row["window_key"] is None for row in cleaned),
            "missing_payload_sides": missing, "truncated_payload_sides": truncated,
            "history_may_be_incomplete": True, "range_is_error_bound": False,
        },
    }
