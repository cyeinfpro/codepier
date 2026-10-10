"""Projection of context scenarios to an authorized period and trend buckets."""
from collections import defaultdict

from shared.context_estimate import VERSION, SCENARIOS, estimate_context
from shared.token_cost import reference_cost


def _counter(value):
    return type(value) is int and value >= 0


def _count(values, key):
    return sum(row[key] for row in values)


def _summary(groups, coverage, cache_read_percent, reference_model):
    eligible = [[row for row in items if row["observed_payload_sides"] > 0] for items in groups]
    coverage = {**coverage,
                "observed_rounds": len(eligible[1]),
                "unmeasured_rounds": len(groups[1]) - len(eligible[1])}
    groups = eligible
    available = coverage["measured_payload_sides"] > 0 and bool(groups[1])
    totals = []
    costs = []
    for items in groups:
        inputs, outputs = _count(items, "input_tokens"), _count(items, "output_tokens")
        totals.append((inputs, outputs))
        metric = lambda n: dict(estimated_tokens=n, low=n, high=n)
        costs.append(reference_cost({"input": metric(outputs), "output": metric(inputs)},
                                    cache_read_percent, reference_model))
    def metric(index):
        values = [sum(pair) if index is None else pair[index] for pair in totals]
        return {"estimated_tokens": values[1] if available else None,
                "low": min(values) if available else None,
                "high": max(values) if available else None,
                "partial": True}
    cost = costs[1]
    cost["scope"] = "context_scenario_equivalent"
    cost["context_version"] = VERSION
    cost["partial"] = True
    for prefix, value in (("low", min(c["amount_pico_usd"] for c in costs)),
                          ("high", max(c["amount_pico_usd"] for c in costs))):
        cost[prefix + "_pico_usd"] = value
        cost[prefix + "_pico_usd_exact"] = str(value)
        cost[prefix + "_nano_usd"] = value // 1000
    if not available:
        for key in ("amount_pico_usd", "amount_pico_usd_exact", "amount_nano_usd",
                    "low_pico_usd", "low_pico_usd_exact", "low_nano_usd",
                    "high_pico_usd", "high_pico_usd_exact", "high_nano_usd",
                    "estimated_usd", "model_input_tokens", "model_output_tokens",
                    "estimated_cached_read_tokens"):
            cost[key] = None
        for value in cost["breakdown"].values():
            for key in value:
                value[key] = None
    mid = SCENARIOS[1]
    return {
        "kind": "context_scenario", "version": VERSION,
        "scope": "retained_authorized_context_estimate",
        "state": "partial" if available else "unavailable",
        "total": metric(None), "input": metric(0), "output": metric(1),
        "reference_cost": cost,
        "estimated_model_rounds": len(groups[1]) if available else None,
        "grouped_tool_rounds": sum(not row["tail"] for row in groups[1]) if available else None,
        "provisional_tails": sum(row["tail"] for row in groups[1]) if available else None,
        "assumptions": {"base_tokens": mid.base_tokens, "context_trigger": mid.context_trigger,
                        "recent_rounds": mid.recent_rounds, "older_retention_percent": 98,
                        "cache_read_percent": cache_read_percent, "range_kind": "scenario_envelope"},
        "coverage": coverage, "actual_usage": None, "billing_total": None,
    }


def context_usage(rows, lower, upper, bucket_for, *, cache_read_percent=90,
                  reference_model="gpt-6-astra", collection_unavailable=False):
    """Warm retained history first; cut contributions only after one replay.

    All rows MUST already satisfy the current caller's live IAM, project,
    connection and window filters. No row content or correlation IDs are
    returned. Cost for the same visible text is never added twice.
    """
    selected = [row for row in rows if lower <= row["started"] <= upper]
    measured = missing = truncated = 0
    for row in rows:
        for direction in ("input", "output"):
            side = (row.get("token_usage") or {}).get(direction) or {}
            measured += _counter(side.get("estimated_tokens"))
            missing += not _counter(side.get("estimated_tokens"))
            truncated += bool(side.get("source_truncated") or side.get("truncated"))
    coverage = {
        "complete_history": False, "range_is_error_bound": False,
        "retained_history_attempts": len(rows), "warmup_attempts": len(rows) - len(selected),
        "visible_attempts": len(selected), "measured_payload_sides": 0 if collection_unavailable else measured,
        "missing_payload_sides": missing, "truncated_payload_sides": truncated,
        "inferred_context_attempts": sum(not row.get("window_key") for row in rows),
    }
    results = estimate_context(rows)["scenarios"] if rows else []
    groups = [[], [], []]
    buckets = defaultdict(lambda: [[], [], []])
    for index, result in enumerate(results):
        for item in result["contributions"]:
            if lower <= item["started"] <= upper:
                groups[index].append(item)
                buckets[bucket_for(item["started"])][index].append(item)
    return (_summary(groups, coverage, cache_read_percent, reference_model),
            {key: _summary(value, coverage, cache_read_percent, reference_model)
             for key, value in buckets.items()})
