"""Versioned reference cost for visible MCP text, never host billing usage.

MCP request parameters are model OUTPUT; responses are subsequent model INPUT.
Cache reads are a scenario on that input, not extra observed tokens. Exact
integer pico-USD preserves sub-nano prices and additive row aggregation.
"""
from __future__ import annotations

from decimal import Decimal

VERSION = "gpt6-standard-tool-text-2026-10-10-v2"
DEFAULT_CACHE_READ_PERCENT = 90
DEFAULT_REFERENCE_MODEL = "gpt-6-astra"
NANO_USD = 1_000_000_000
PICO_USD = 1_000_000_000_000
# USD per million tokens, verified against the linked official API price pages.
MODELS = {
    "gpt-6-astra": ("GPT-6 Astra", ("10", "1", "12.5", "50")),
    "gpt-6.1-sol": ("GPT-6.1 Sol", ("2", "0.1", "2.5", "10")),
    "gpt-6-luna": ("GPT-6 Luna", ("0.1", "0.01", "0.125", "0.5")),
}


def model_options():
    return [{"id": key, "label": value[0]} for key, value in MODELS.items()]


def pricing(cache_read_percent=DEFAULT_CACHE_READ_PERCENT, reference_model=DEFAULT_REFERENCE_MODEL):
    if type(cache_read_percent) is not int or not 0 <= cache_read_percent <= 100:
        raise ValueError("Cache read assumption must be an integer from 0 to 100")
    if not isinstance(reference_model, str) or reference_model not in MODELS:
        raise ValueError("Unsupported reference model")
    label, values = MODELS[reference_model]
    rates = dict(zip(("input", "cached_input", "cache_write", "output"), map(Decimal, values)))
    effective = (rates["input"] * (100 - cache_read_percent) + rates["cached_input"] * cache_read_percent) / 100
    return {
        "version": VERSION, "model": reference_model, "model_label": label, "currency": "USD",
        "provider": "OpenAI", "source_date": "2026-10-10",
        "tier": "standard", "context": "short_reference",
        "sources": [f"https://developers.openai.com/api/docs/models/{reference_model}",
                    "https://developers.openai.com/api/docs/pricing"],
        "available_models": model_options(),
        "usd_per_million": {key: float(value) for key, value in rates.items()},
        "cache_read_percent": cache_read_percent,
        "effective_input_usd_per_million": float(effective),
        "long_context_input_threshold": 272_000,
        "long_context_usd_per_million": {
            key: float(value * (Decimal("1.5") if key == "output" else 2))
            for key, value in rates.items()
        },
        "tier_multipliers": {"fast": 2, **({"ultrafast": 6} if reference_model == "gpt-6.1-sol" else {})},
        "cache_read_is_assumption": True, "cache_write_usage": None,
        "actual_cache_hit_rate": None, "reasoning_tokens": None,
        "actual_usage": None, "billing_total": None,
    }


def _count(side, field="estimated_tokens"):
    value = side.get(field)
    return value if type(value) is int and value >= 0 else None


def combined_total(value):
    sides = [value.get(direction) or {} for direction in ("input", "output")]
    total = {}
    for field in ("estimated_tokens", "low", "high"):
        counts = [_count(side, field) for side in sides]
        total[field] = sum(n for n in counts if n is not None) if any(n is not None for n in counts) else None
    total["partial"] = any(_count(side) is None or side.get("unavailable_attempts", 0)
                           or side.get("partial_attempts", 0) or side.get("source_truncated_attempts", 0)
                           or side.get("state") == "partial" or side.get("source_truncated") for side in sides)
    return total


def reference_cost(value, cache_read_percent=DEFAULT_CACHE_READ_PERCENT, reference_model=DEFAULT_REFERENCE_MODEL):
    rates = pricing(cache_read_percent, reference_model)
    request, response = value.get("input") or {}, value.get("output") or {}
    # A catalog rate has at most three decimals. Weighted rates remain exact
    # integer pico-USD for every allowed integer percentage, including Luna.
    raw = dict(zip(("input", "cached_input", "cache_write", "output"), map(Decimal, MODELS[reference_model][1])))
    ordinary_rate = int(raw["input"] * 10_000) * (100 - cache_read_percent)
    cached_rate = int(raw["cached_input"] * 10_000) * cache_read_percent
    output_rate = int(raw["output"] * 1_000_000)
    costs = {}
    for field, prefix in (("estimated_tokens", "amount"), ("low", "low"), ("high", "high")):
        parts = [n * rate for side, rate in ((request, output_rate), (response, ordinary_rate + cached_rate))
                 if (n := _count(side, field)) is not None]
        pico = sum(parts) if parts else None
        costs[prefix + "_pico_usd"] = pico
        costs[prefix + "_pico_usd_exact"] = None if pico is None else str(pico)
        # Compatibility/display projection only; exact aggregation uses pico.
        costs[prefix + "_nano_usd"] = None if pico is None else pico // 1000
    response_count, request_count = _count(response), _count(request)
    breakdown = {}
    for name, count, ratio, rate in (
        ("uncached_input", response_count, (100 - cache_read_percent) / 100, ordinary_rate),
        ("cached_input", response_count, cache_read_percent / 100, cached_rate),
        ("output", request_count, 1, output_rate),
    ):
        pico = None if count is None else count * rate
        breakdown[name] = {"estimated_tokens": None if count is None else count * ratio,
                           "amount_pico_usd": pico, "amount_nano_usd": None if pico is None else pico // 1000}
    return {
        "kind": "reference_estimate", "scope": "visible_tool_text_equivalent",
        "version": VERSION, "currency": "USD", "pricing": rates, **costs,
        "estimated_usd": None if costs["amount_pico_usd"] is None else costs["amount_pico_usd"] / PICO_USD,
        "partial": combined_total(value)["partial"], "breakdown": breakdown,
        "model_input_tokens": response_count, "model_output_tokens": request_count,
        "estimated_cached_read_tokens": None if response_count is None else response_count * cache_read_percent / 100,
        "actual_usage": None, "actual_cache_hit_rate": None,
        "cache_write_tokens": None, "reasoning_tokens": None, "billing_total": None,
    }
