"""V4 estimate is scoped before inference and remains additive to old fields."""
from dataclasses import replace

import pytest

from shared.util import DevError
from tests.test_audit_api import api  # noqa: F401
from tests.test_context_dashboard import clock, read, seed
from tests.test_mcp_usage import principal


def test_counts_without_payload_are_explicit_estimates_and_queries_are_read_only(api):
    app, client, token = api
    now = clock()
    seed(app, token, now - 60, record=False)
    with app.state.store.lock:
        before = app.state.store.db.total_changes
        result = read(app, token, now)
        assert app.state.store.db.total_changes == before
    assert result["schema_version"] == 4
    assert result["summary"]["total"]["estimated_tokens"] is None
    assert result["summary"]["context_estimate"]["state"] == "unavailable"
    calls = result["summary"]["call_estimate"]
    assert calls["estimated_model_rounds"] == 2
    assert calls["coverage"]["count_only_rounds"] == 2
    assert calls["total"]["estimated_tokens"] == 271836
    assert calls["actual_usage"] is calls["billing_total"] is None
    app.state.runtime.integrations.usage_metrics = None
    assert read(app, token, now)["summary"]["call_estimate"]["total"] == calls["total"]


def test_period_trends_and_reference_model_reprice_the_same_rounds(api):
    app, client, token = api
    now = clock()
    for index in range(5):
        seed(app, token, now - (5 - index) * 3600, response=30000)
    result = read(app, token, now)
    calls = result["summary"]["call_estimate"]
    points = [p["call_estimate"] for p in result["trend"]]
    assert calls["total"]["estimated_tokens"] == sum(p["total"]["estimated_tokens"] for p in points)
    assert calls["estimated_model_rounds"] == sum(p["estimated_model_rounds"] or 0 for p in points)
    assert calls["reference_cost"]["amount_pico_usd"] == sum(p["reference_cost"]["amount_pico_usd"] for p in points)
    cheaper = read(app, token, now, reference_model="gpt-6-luna")["summary"]["call_estimate"]
    assert cheaper["total"] == calls["total"]
    assert cheaper["reference_cost"]["estimated_usd"] < calls["reference_cost"]["estimated_usd"]
    uncached = read(app, token, now, cache_read_percent=0)["summary"]["call_estimate"]
    assert uncached["total"] == calls["total"]
    assert uncached["reference_cost"]["estimated_usd"] > calls["reference_cost"]["estimated_usd"]


def test_filters_revocation_and_project_remap_also_remove_empirical_estimates(api):
    app, client, token = api
    now = clock()
    p = principal(app, token)
    other = app.state.auth.issue_grant(replace(p, actor="panel:admin", admin=True, token_hash=""),
                                       "other", ["read"], ["project"])["token"]
    seed(app, token, now - 60, window="mine")
    seed(app, other, now - 60, window="theirs", response=200000)
    own = read(app, token, now)["summary"]["call_estimate"]
    their = read(app, other, now)["summary"]["call_estimate"]
    assert their["input"]["estimated_tokens"] > own["input"]["estimated_tokens"]
    assert read(app, token, now, session="theirs")["summary"]["call_estimate"]["state"] == "unavailable"
    assert read(app, token, now, connection=principal(app, other).grant_id)["summary"]["call_estimate"]["state"] == "unavailable"
    app.state.store.execute("UPDATE projects SET root='/new-map' WHERE id='project'")
    assert read(app, token, now)["summary"]["call_estimate"]["state"] == "unavailable"
    app.state.store.execute("UPDATE grants SET revoked=1 WHERE id=?", (p.grant_id,))
    with pytest.raises(DevError):
        read(app, token, now)


def test_inflight_returns_cannot_raise_current_cost_or_create_a_future_bucket(api):
    app, client, token = api
    now = clock()
    seed(app, token, now - 60, response=200000, service_ms=7_200_000)
    result = read(app, token, now)
    calls = result["summary"]["call_estimate"]
    assert calls["estimated_model_rounds"] == 1
    assert calls["input"]["estimated_tokens"] == 135431
    assert calls["adjustments"]["fresh_tool_input_tokens"] == 0
    assert all(p["started"] <= now for p in result["trend"])
