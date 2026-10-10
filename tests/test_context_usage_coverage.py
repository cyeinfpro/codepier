"""Missing context streams must not borrow availability from unrelated history."""
from hub.context_usage import context_usage


def activity(identifier, started, *, window="same", request=100, response=1000,
             duration=0.1):
    def metric(value):
        return None if value is None else dict(
            state="available", estimated_tokens=value, low=value, high=value)
    return dict(id=identifier, started=started, service_ms=duration * 1000,
                tool="read", grant_id="synthetic", project="synthetic",
                window_key=window,
                token_usage=dict(input=metric(request), output=metric(response)))


def measure(rows, lower=0, upper=10000):
    return context_usage(rows, lower, upper, lambda timestamp: int(timestamp // 3600) * 3600)


def assert_unknown(value):
    assert value["state"] == "unavailable"
    for direction in ("input", "output", "total"):
        assert value[direction]["estimated_tokens"] is None
    assert value["reference_cost"]["amount_pico_usd"] is None
    assert value["reference_cost"]["estimated_usd"] is None


def test_all_missing_stream_is_unknown():
    total, buckets = measure([activity(1, 100, request=None, response=None)])
    assert_unknown(total)
    assert_unknown(buckets[0])


def test_yesterday_measured_other_window_does_not_enable_today_missing_stream():
    rows = [activity(1, 100, window="yesterday-known"),
            activity(2, 86410, window="today-unknown", request=None, response=None)]
    total, buckets = measure(rows, lower=86400, upper=86500)
    assert total["coverage"]["warmup_attempts"] == 1
    assert_unknown(total)
    assert_unknown(buckets[86400])


def test_same_window_real_history_still_warms_missing_today():
    rows = [activity(1, 100),
            activity(2, 86410, request=None, response=None)]
    total, _ = measure(rows, lower=86400, upper=86500)
    cold, _ = measure(rows[1:], lower=86400, upper=86500)
    assert total["state"] == "partial"
    assert total["input"]["estimated_tokens"] > 0
    assert_unknown(cold)


def test_independent_unknown_hour_does_not_borrow_measured_hour_or_add_cost():
    known = activity(1, 100, window="known")
    unknown = activity(2, 7200, window="unknown", request=None, response=None)
    total, buckets = measure([known, unknown])
    measured_only, _ = measure([known])
    assert_unknown(buckets[7200])
    assert total["total"] == measured_only["total"]
    assert total["reference_cost"] == measured_only["reference_cost"]


def test_future_same_window_measurement_does_not_enable_earlier_unknown_hour():
    total, buckets = measure([
        activity(1, 100, request=None, response=None),
        activity(2, 7200)])
    assert total["state"] == "partial"
    assert_unknown(buckets[0])
    assert buckets[7200]["state"] == "partial"


def test_delayed_response_only_enables_its_actual_return_bucket():
    total, buckets = measure([
        activity(1, 100, request=None, response=1000, duration=7200)])
    assert total["state"] == "partial"
    assert_unknown(buckets[0])
    assert buckets[7200]["state"] == "partial"


def test_measured_zero_is_evidence_unlike_missing():
    total, buckets = measure([activity(1, 100, request=0, response=0)])
    assert total["state"] == "partial"
    assert total["total"]["estimated_tokens"] > 0
    assert buckets[0]["state"] == "partial"


def test_collection_failure_still_forces_unknown():
    total, buckets = context_usage(
        [activity(1, 100)], 0, 10000, lambda timestamp: 0,
        collection_unavailable=True)
    assert_unknown(total)
    assert_unknown(buckets[0])
