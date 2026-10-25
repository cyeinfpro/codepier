"""Count-calibrated model-round scenarios over sanitized replay contributions.

The prior is a population average from Astra API calls, not observed host usage.
A large visible context/output can raise it; never add the whole prior twice.
Only numeric aggregates leave this module. All monetary arithmetic is pico-USD.
"""
from __future__ import annotations

from decimal import Decimal
from dataclasses import replace

from shared.token_cost import DEFAULT_CACHE_READ_PERCENT, DEFAULT_REFERENCE_MODEL, PICO_USD, pricing
from shared.context_estimate import SCENARIOS

VERSION = "codepier-call-prior-v1"
# Low/high are scenarios, not confidence intervals or measured error bounds.
PRIORS = ((63_861, 200), (135_431, 487), (211_018, 2_000))
# Visible retained history can grow past the population prior. These are
# scenario compression triggers, not the model's actual context capacity.
CALL_SCENARIOS = tuple(replace(s, context_trigger=trigger, retention=1)
                       for s, trigger in zip(SCENARIOS, (128_000, 240_000, 272_000)))
CALIBRATION = {
    "version": "astra-codex-calls-2026-10-25-v1",
    "source_model": "gpt-6-astra",
    "source_cohort": "codex_client_token_records",
    "training_start": "2026-09-25T15:40:00+08:00",
    "training_end_exclusive": "2026-10-18T15:40:00+08:00",
    "training_calls": 23_310,
    "validation_calls": 8_079,
    "input_tokens_per_round": PRIORS[1][0],
    "output_tokens_per_round": PRIORS[1][1],
    "historical_usd_per_round": 0.209832079708,
    "validation_cost_daily_wape_percent": 14.02,
    "validation_uses_actual_api_counts": True,
    "project_accuracy_verified": False,
}


def summarize_calls(groups, coverage, *, cache_read_percent=DEFAULT_CACHE_READ_PERCENT,
                    reference_model=DEFAULT_REFERENCE_MODEL):
    """Reprice each evaluation once; fresh returned text caps cache allocation.

    A population prior is also usable when payload counters are absent. That
    explicit count-only estimate never fills or changes legacy text metrics.
    Groups must already be permission-filtered, warmed and cut to the period.
    """
    rates = pricing(cache_read_percent, reference_model)
    rate = {key: int(Decimal(str(value)) * 10_000)
            for key, value in rates["usd_per_million"].items()}
    results = []
    for items, (prior_input, prior_output) in zip(groups, PRIORS):
        value = dict(inputs=0, outputs=0, cached_hundredths=0,
                     fresh=0, input_adjustments=0, output_adjustments=0,
                     count_only_rounds=0, cost_pico=0)
        for row in items:
            inputs = max(prior_input, row["input_tokens"])
            outputs = max(prior_output, row["output_tokens"])
            fresh = min(inputs, max(0, round(row["fresh_tool_input_tokens"])))
            # The cache percentage is a ceiling. It cannot discount all newly
            # consumed tool text; the prior's ordinary-input budget covers
            # small fresh returns without charging that budget twice.
            cached = min(inputs * cache_read_percent, (inputs - fresh) * 100)
            ordinary = inputs * 100 - cached
            value["inputs"] += inputs
            value["outputs"] += outputs
            value["fresh"] += fresh
            value["cached_hundredths"] += cached
            value["input_adjustments"] += inputs > prior_input
            value["output_adjustments"] += outputs > prior_output
            value["count_only_rounds"] += row["observed_payload_sides"] == 0
            value["cost_pico"] += (ordinary * rate["input"] + cached * rate["cached_input"]
                                   + outputs * 100 * rate["output"])
        results.append(value)
    mid = results[1]
    available = bool(groups[1])

    def metric(key):
        values = [r["inputs"] + r["outputs"] if key == "total" else r[key] for r in results]
        return {"estimated_tokens": values[1] if available else None,
                "low": min(values) if available else None,
                "high": max(values) if available else None, "partial": True}

    def amount(pico):
        return {"amount_pico_usd": pico, "amount_pico_usd_exact": None if pico is None else str(pico),
                "amount_nano_usd": None if pico is None else pico // 1000}

    input_hundredths = mid["inputs"] * 100 - mid["cached_hundredths"]
    breakdown = {}
    for name, count, per_hundredth in (
        ("uncached_input", input_hundredths, rate["input"]),
        ("cached_input", mid["cached_hundredths"], rate["cached_input"]),
        ("output", mid["outputs"] * 100, rate["output"]),
    ):
        breakdown[name] = {
            "estimated_tokens": count / 100 if available else None,
            **amount(count * per_hundredth if available else None),
        }
    pico = mid["cost_pico"] if available else None
    cost = {
        "kind": "reference_estimate", "scope": "call_round_equivalent",
        "version": rates["version"], "calibration_version": CALIBRATION["version"],
        "currency": "USD",
        "pricing": {**rates, "cache_read_is_upper_bound": True,
                    "fresh_tool_text_uncached_is_assumption": True},
        **amount(pico), "estimated_usd": None if pico is None else pico / PICO_USD,
        "partial": True, "breakdown": breakdown,
        "model_input_tokens": mid["inputs"] if available else None,
        "model_output_tokens": mid["outputs"] if available else None,
        "estimated_cached_read_tokens": mid["cached_hundredths"] / 100 if available else None,
        "actual_usage": None, "actual_cache_hit_rate": None,
        "cache_write_tokens": None, "reasoning_tokens": None, "billing_total": None,
    }
    for prefix, p in (("low", min(r["cost_pico"] for r in results)),
                      ("high", max(r["cost_pico"] for r in results))):
        for suffix, value in amount(p if available else None).items():
            cost[prefix + suffix.removeprefix("amount")] = value
    rounds = [len(items) for items in groups]
    return {
        "kind": "call_calibration", "version": VERSION,
        "scope": "retained_authorized_call_estimate",
        "state": "partial" if available else "unavailable",
        "input": metric("inputs"), "output": metric("outputs"), "total": metric("total"),
        "reference_cost": cost,
        "estimated_model_rounds": rounds[1] if available else None,
        "round_range": {"low": min(rounds), "high": max(rounds)} if available else None,
        "grouped_tool_rounds": sum(not r["tail"] for r in groups[1]) if available else None,
        "provisional_tails": sum(r["tail"] for r in groups[1]) if available else None,
        "calibration": dict(CALIBRATION),
        "adjustments": {
            "large_context_rounds": mid["input_adjustments"],
            "large_output_rounds": mid["output_adjustments"],
            "fresh_tool_input_tokens": mid["fresh"],
        },
        "coverage": {**coverage, "count_only_rounds": mid["count_only_rounds"],
                     "range_is_error_bound": False, "complete_history": False},
        "assumptions": {"range_kind": "scenario_envelope",
                        "context_trigger": CALL_SCENARIOS[1].context_trigger,
                        "older_retention_percent": 100,
                        "cache_read_percent_is_ceiling": True,
                        "scope_is_inferred_model_rounds": True},
        "actual_usage": None, "billing_total": None,
    }
