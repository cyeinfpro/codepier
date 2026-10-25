"""Accounting, chronology and uncertainty for the sample-calibrated estimate."""
import json

import pytest

from hub.context_usage import context_usage
from shared.call_estimate import CALIBRATION, PRIORS
from tests.test_context_estimate import event


def project(rows, lower=0, upper=100000, **kw):
    return context_usage(rows, lower, upper, lambda t: int(t // 3600) * 3600,
                         include_call_estimate=True, **kw)


def estimate(rows, **kw):
    return project(rows, **kw)[2]


def test_prior_counts_model_evaluations_and_does_not_add_tool_totals_twice():
    value = estimate([event(1)])
    assert value["estimated_model_rounds"] == 2
    assert value["grouped_tool_rounds"] == value["provisional_tails"] == 1
    assert value["input"]["estimated_tokens"] == 2 * PRIORS[1][0]
    assert value["output"]["estimated_tokens"] == 2 * PRIORS[1][1]
    assert value["total"]["estimated_tokens"] == 2 * sum(PRIORS[1])
    assert value["actual_usage"] is value["billing_total"] is None
    assert value["calibration"] == CALIBRATION
    cost = value["reference_cost"]
    assert cost["scope"] == "call_round_equivalent"
    assert cost["amount_pico_usd"] == sum(p["amount_pico_usd"] for p in cost["breakdown"].values())


def test_recorded_calls_support_count_only_estimates_without_filling_old_metrics():
    context, _, value, _ = project([event(1, token_usage=None)])
    assert context["state"] == "unavailable"
    assert context["total"]["estimated_tokens"] is None
    assert value["state"] == "partial"
    assert value["total"]["estimated_tokens"] == 2 * sum(PRIORS[1])
    assert value["coverage"]["count_only_rounds"] == 2
    assert value["coverage"]["missing_payload_sides"] == 2
    assert estimate([])["state"] == "unavailable"
    assert estimate([])["total"]["estimated_tokens"] is None


def test_parallel_batch_serial_dependencies_and_unknown_poll_targets():
    parallel = [event(i, i * .2, service_ms=2000) for i in range(3)]
    serial = [event(i, i * .2, service_ms=100) for i in range(3)]
    assert estimate(parallel)["estimated_model_rounds"] == 2
    assert estimate(parallel)["round_range"]["high"] == 4
    assert estimate(serial)["estimated_model_rounds"] == 4
    unknown = [event(i, i * 2, tool="operations_get") for i in range(20)]
    known = [dict(r, operation_id="same-operation") for r in unknown]
    assert estimate(unknown)["grouped_tool_rounds"] == 20
    assert estimate(known)["grouped_tool_rounds"] == 4
    assert estimate([dict(r, operation_id=str(i)) for i, r in enumerate(unknown)])["grouped_tool_rounds"] == 20


def test_new_data_replaces_tail_and_duplicates_do_not_create_evaluations():
    first = event(1)
    before = estimate([first])
    after = estimate([first, first, event(2)])
    assert after["provisional_tails"] == before["provisional_tails"] == 1
    assert after["estimated_model_rounds"] == 3
    assert after["total"]["estimated_tokens"] == 3 * sum(PRIORS[1])


def test_large_output_raises_the_prior_without_adding_the_whole_prior_again():
    value = estimate([event(1, request=20_000)])
    assert value["output"]["estimated_tokens"] == 20_120 + PRIORS[1][1]
    assert value["adjustments"]["large_output_rounds"] == 1


def test_fresh_returns_are_not_discounted_even_under_100_percent_cache():
    small = estimate([event(1, response=1000)], cache_read_percent=100)
    large = estimate([event(1, response=20000)], cache_read_percent=100)
    assert small["total"] == large["total"]
    cost = large["reference_cost"]
    assert cost["breakdown"]["uncached_input"]["estimated_tokens"] == 20000
    assert cost["estimated_cached_read_tokens"] == large["input"]["estimated_tokens"] - 20000
    assert cost["amount_pico_usd"] - small["reference_cost"]["amount_pico_usd"] == 19000 * 9_000_000
    assert cost["pricing"]["cache_read_is_upper_bound"] is True
    assert cost["actual_cache_hit_rate"] is None


def test_large_pending_return_is_only_consumed_after_completion():
    context, _, value, buckets = project(
        [event(1, started=100, response=200000, service_ms=7_200_000)], upper=3600)
    assert value["estimated_model_rounds"] == 1
    assert value["input"]["estimated_tokens"] == PRIORS[1][0]
    assert value["adjustments"]["fresh_tool_input_tokens"] == 0
    complete = estimate([event(1, started=100, response=200000, service_ms=7_200_000)])
    assert complete["adjustments"]["large_context_rounds"] == 1
    assert complete["input"]["estimated_tokens"] > 2 * PRIORS[1][0]
    assert set(buckets) == {0}


def test_visible_retained_history_can_grow_past_the_mean_and_is_not_capped_at_64k():
    rows = [event(i, response=2500) for i in range(100)]
    value = estimate(rows)
    assert value["adjustments"]["large_context_rounds"] > 0
    assert value["input"]["estimated_tokens"] > value["estimated_model_rounds"] * PRIORS[1][0]
    assert value["assumptions"]["context_trigger"] == 240000


@pytest.mark.parametrize("percent", [0, 1, 95, 100])
@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-6.1-sol", "gpt-6-luna"])
def test_period_buckets_add_exactly_and_repricing_does_not_change_tokens(percent, model):
    rows = [event(1, started=3500, response=20000), event(2, started=3700)]
    _, _, value, buckets = project(rows, cache_read_percent=percent, reference_model=model)
    assert value["total"] == estimate(rows)["total"]
    assert value["total"]["estimated_tokens"] == sum(b["total"]["estimated_tokens"] for b in buckets.values())
    pico = value["reference_cost"]["amount_pico_usd"]
    assert pico == sum(b["reference_cost"]["amount_pico_usd"] for b in buckets.values())
    assert str(pico) == value["reference_cost"]["amount_pico_usd_exact"]
    assert value["reference_cost"]["pricing"]["model"] == model


def test_warmup_and_scope_do_not_leak_raw_fields_or_count_prior_period_calls():
    rows = [event(1, started=86300, response=200000),
            event(2, started=86410)]
    _, _, value, _ = project(rows, lower=86400, upper=86500)
    assert value["estimated_model_rounds"] == 2
    assert value["adjustments"]["large_context_rounds"] > 0
    assert value["coverage"]["warmup_attempts"] == 1
    text = json.dumps(value)
    for marker in ("window_key", "operation_id", "grant_id", "actor", "contributions"):
        assert marker not in text
