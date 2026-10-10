"""Synthetic-only proof of accounting and replay behavior, not real accuracy."""
from dataclasses import replace
import json
import math
import unittest

from shared.context_estimate import SCENARIOS, estimate_context, normalize, reference_cost, replay


def side(n):
    return {"state": "available", "estimated_tokens": n, "low": n, "high": n}


def event(i, started=None, request=100, response=1000, **kwargs):
    return {"id": i, "started": i * 60 if started is None else started,
            "service_ms": 100, "tool": "fs_read", "grant_id": "g", "project": "p",
            "window_key": "w", "token_usage": {"input": side(request), "output": side(response)}, **kwargs}


def exact():
    return replace(SCENARIOS[1], base_tokens=1000, context_trigger=1_000_000,
                   retention=1, recent_rounds=100, hidden_initial=0, hidden_user=0,
                   hidden_output=0, tail_output=0, burst_seconds=0, parallel_seconds=0, poll_seconds=0)


class ContextReplayTests(unittest.TestCase):
    def test_hand_calculation_and_direction(self):
        value = replay([event(i) for i in range(3)], exact())
        self.assertEqual([r["input_tokens"] for r in value["contributions"]], [1000, 2100, 3200, 4300])
        self.assertEqual(value["input_tokens"], 10600)
        self.assertEqual(value["output_tokens"], 300)
        self.assertEqual(value["total_tokens"], 10900)
        self.assertEqual(sum(r["fresh_tool_input_tokens"] for r in value["contributions"]), 3000)

    def test_cached_input_only(self):
        value = {"input_tokens": 1000, "output_tokens": 100}
        cost = reference_cost(value, input_rate=10, cached_input_rate=1, output_rate=50)
        self.assertAlmostEqual(cost["estimated_usd"], .0069)
        self.assertEqual(cost["estimated_cached_input_tokens"], 900)
        self.assertIsNone(cost["actual_usage"])
        self.assertIsNone(cost["reasoning_tokens"])

    def test_tails_replaced_not_accumulated(self):
        first = replay([event(0)], exact())
        second = replay([event(0), event(1)], exact())
        self.assertEqual(first["provisional_tails"], second["provisional_tails"])
        self.assertEqual(len(second["contributions"]), 3)
        self.assertEqual(second["total_tokens"], 6500)
        self.assertEqual(second, replay([event(1), event(0)], exact()))

    def test_parallel_candidates_and_explicit_hint(self):
        rows = [event(i, i*.2, service_ms=2000) for i in range(3)]
        self.assertEqual(replay(rows)["tool_rounds"], 1)
        self.assertEqual(replay(rows, SCENARIOS[2])["tool_rounds"], 3)
        self.assertEqual(replay([dict(r, round_hint="turn1") for r in rows], SCENARIOS[2])["tool_rounds"], 1)

    def test_polling_is_not_certainly_one_round(self):
        rows = [event(i, i*2, tool="operations_get", request=25, response=60) for i in range(20)]
        result = estimate_context(rows)
        self.assertEqual([r["tool_rounds"] for r in result["scenarios"]], [2, 4, 20])
        self.assertLess(result["low"], result["estimated_tokens"])
        self.assertLess(result["estimated_tokens"], result["high"])

    def test_different_poll_targets_not_merged(self):
        rows = [event(0, 0, tool="operations_get", poll_key="a"),
                event(1, .1, tool="operations_get", poll_key="b")]
        self.assertEqual(replay(rows)["tool_rounds"], 2)

    def test_inflight_response_not_consumed_before_available(self):
        rows = [event(0, 0, service_ms=100_000, response=9000), event(1, 30, response=200)]
        value = replay(rows, exact())
        self.assertEqual([r["fresh_tool_input_tokens"] for r in value["contributions"]], [0, 0, 9200])
        self.assertEqual(value["contributions"][-1]["started"], 100)

    def test_deduplicate_activity_ids_only(self):
        a, b = event(1, operation_id="same"), event(2, operation_id="same")
        self.assertEqual(replay([a, a, b]), replay([a, b]))
        self.assertEqual(replay([a, b])["tool_rounds"], 2)

    def test_session_boundary_is_not_midnight(self):
        rows = [event(0, 86390), event(1, 86410)]
        self.assertEqual(replay(rows)["streams"], 1)
        self.assertEqual(replay([dict(r, window_key=None) for r in rows])["streams"], 1)

    def test_hub_restart_changed_salt_not_stitched(self):
        self.assertEqual(replay([event(0, window_key="before"), event(1, window_key="after")])["streams"], 2)

    def test_stable_window_resumes_after_days(self):
        self.assertEqual(replay([event(0, 0), event(1, 3*86400)])["streams"], 1)

    def test_missing_window_idle_scenarios(self):
        rows = [event(0, 0, window_key=None), event(1, 1200, window_key=None)]
        self.assertEqual([replay(rows, s)["streams"] for s in SCENARIOS], [2, 1, 1])

    def test_authority_and_mapping_boundaries_never_merge(self):
        for field in ("grant_id", "project", "root", "device"):
            rows = [event(0), event(1, **{field: "different"})]
            self.assertEqual(replay(rows)["streams"], 2)

    def test_missing_and_truncated_metrics_are_flags_not_extrapolation(self):
        row = event(0, token_usage={"input": None, "output": {"state": "partial", "estimated_tokens": 100, "source_truncated": True}})
        result = estimate_context([row])
        self.assertEqual(result["coverage"]["missing_payload_sides"], 1)
        self.assertEqual(result["coverage"]["truncated_payload_sides"], 1)
        self.assertEqual(result["scenarios"][1]["contributions"][-1]["fresh_tool_input_tokens"], 100)
        self.assertEqual(result["scenarios"][0]["contributions"][-1]["fresh_tool_input_tokens"], 100)

    def test_long_conversation_compresses_instead_of_unbounded_growth(self):
        value = replay([event(i, response=2500) for i in range(200)])
        self.assertGreater(value["compressions"], 0)
        self.assertLessEqual(value["max_input_tokens"], 64000)
        self.assertLess(value["input_tokens"], 201*64000)

    def test_fresh_large_payload_not_discarded_at_assumed_cap(self):
        value = replay([event(0, response=200_000)])
        self.assertEqual(value["contributions"][-1]["fresh_tool_input_tokens"], 200_000)
        self.assertGreaterEqual(value["max_input_tokens"], 212_000)
        self.assertTrue(value["contributions"][-1]["fresh_over_trigger"])

    def test_range_is_scenario_envelope_not_claimed_confidence(self):
        result = estimate_context([event(i) for i in range(30)])
        self.assertLessEqual(result["low"], result["estimated_tokens"])
        self.assertLessEqual(result["estimated_tokens"], result["high"])
        self.assertFalse(result["coverage"]["range_is_error_bound"])
        self.assertIsNone(result["actual_usage"])

    def test_input_order_and_purity(self):
        rows = [event(i) for i in range(10)]
        before = json.dumps(rows, sort_keys=True)
        self.assertEqual(estimate_context(rows), estimate_context(list(reversed(rows))))
        self.assertEqual(json.dumps(rows, sort_keys=True), before)

    def test_empty_is_zero_no_phantom_base(self):
        result = estimate_context([])
        self.assertEqual(result["estimated_tokens"], 0)
        self.assertEqual(result["scenarios"][1]["provisional_tails"], 0)

    def test_prefix_history_must_be_warmed_before_period_selection(self):
        rows = [event(0, 86390), event(1, 86410)]
        full = replay(rows, exact())
        today = [r for r in full["contributions"] if r["started"] >= 86400]
        cold = replay(rows[1:], exact())
        self.assertGreater(sum(r["input_tokens"] for r in today), cold["input_tokens"])

    def test_context_outputs_do_not_leak_identity_or_raw_fields(self):
        row = event(0, grant_id="sensitive-grant", raw_prompt="secret", arguments={"password": "secret"})
        serialized = json.dumps(estimate_context([row]))
        self.assertNotIn("sensitive-grant", serialized)
        self.assertNotIn("secret", serialized)

    def test_missing_id_rows_are_not_deduplicated(self):
        rows = [dict(event(0), id=None), dict(event(1), id=None)]
        self.assertEqual(len(normalize(rows)), 2)

    def test_invalid_time_and_size_fail_closed(self):
        for value in (-1, math.nan, math.inf, True, None):
            with self.assertRaises(ValueError):
                replay([event(0, value if value is not None else 0) | {"started": value}])
        with self.assertRaises(ValueError):
            replay([event(i) for i in range(10001)])

    def test_invalid_cache_and_rates_fail(self):
        for value in (-1, 101, True, 90.1):
            with self.assertRaises(ValueError):
                reference_cost({"input_tokens": 1, "output_tokens": 1}, input_rate=1, cached_input_rate=1, output_rate=1, cache_percent=value)
        for value in (-1, math.inf, math.nan):
            with self.assertRaises(ValueError):
                reference_cost({"input_tokens": 1, "output_tokens": 1}, input_rate=value, cached_input_rate=1, output_rate=1)


if __name__ == "__main__":
    unittest.main()
