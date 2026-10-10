"""Context dashboard integration: live authority, warm-up and bounded caching."""
from datetime import datetime
from dataclasses import replace
import json
import sqlite3
import time
from zoneinfo import ZoneInfo

import pytest

from hub.token_dashboard import token_dashboard, CACHE_ENTRIES
from hub.mcp_usage import RETENTION_SECONDS
from shared.token_estimate import usage, summarize
from shared.util import DevError
from tests.test_audit_api import api  # noqa: F401
from tests.test_mcp_usage import principal


def metric(n, **extra):
    return dict(state="available", estimated_tokens=n, low=n, high=n,
                characters=n, utf8_bytes=n, source_truncated=False, truncated=False, **extra)


def seed(app, token, started, *, request=100, response=1000, window="window",
         tool="read", record=True, service_ms=100):
    store = app.state.store
    p = principal(app, token)
    project = store.one("SELECT * FROM projects WHERE id='project'")
    cursor = store.execute(
        "INSERT INTO mcp_activity(project,root,device,grant_id,actor,window_key,tool,started,service_ms,status,transition,meaningful) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (project["id"], project["root"], project["device_id"], p.grant_id, p.actor,
         window, tool, started, service_ms, "complete", "serial", 1))
    identifier = cursor.lastrowid
    if record:
        app.state.runtime.integrations.usage_metrics.record(identifier, usage(metric(request), metric(response)))
    return identifier


def read(app, token, now, **kw):
    return token_dashboard(app.state.runtime, principal(app, token), ZoneInfo("UTC"), now=now, **kw)


def context(value):
    return value["summary"]["context_estimate"]


def clock():
    return datetime(2026, 10, 10, 12, tzinfo=ZoneInfo("UTC")).timestamp()


def test_context_contract_additive_legacy_compatibility_and_cost(api):
    app, client, token = api
    now = clock()
    seed(app, token, now - 60)
    value = read(app, token, now)
    result = context(value)
    assert value["schema_version"] == 3
    assert result["kind"] == "context_scenario"
    assert result["version"] == "codepier-context-v1"
    assert result["scope"] == "retained_authorized_context_estimate"
    assert result["reference_cost"]["scope"] == "context_scenario_equivalent"
    assert result["actual_usage"] is result["billing_total"] is None
    assert result["total"]["estimated_tokens"] > value["summary"]["total"]["estimated_tokens"]
    assert result["total"]["estimated_tokens"] == result["input"]["estimated_tokens"] + result["output"]["estimated_tokens"]
    assert result["reference_cost"]["amount_nano_usd"] == (
        result["input"]["estimated_tokens"] * 1900 + result["output"]["estimated_tokens"] * 50000)
    legacy = summarize([{"token_usage": usage(metric(100), metric(1000))}])
    for key in ("input", "output", "total", "reference_cost", "actual_usage"):
        assert value["summary"][key] == legacy[key]
    assert result["coverage"]["range_is_error_bound"] is False
    assert result["coverage"]["complete_history"] is False


def test_prior_day_warms_today_without_counting_old_wire_again(api):
    app, client, token = api
    midnight = datetime(2026, 10, 10, tzinfo=ZoneInfo("UTC")).timestamp()
    older = seed(app, token, midnight - 60)
    seed(app, token, midnight + 60)
    warm = read(app, token, midnight + 120)
    assert warm["summary"]["wire_attempts"] == 1
    assert context(warm)["coverage"]["warmup_attempts"] == 1
    app.state.store.execute("DELETE FROM mcp_activity WHERE id=?", (older,))
    cold = read(app, token, midnight + 120)
    assert warm["summary"]["total"] == cold["summary"]["total"]
    assert context(warm)["total"]["estimated_tokens"] > context(cold)["total"]["estimated_tokens"]


def test_trend_context_midpoints_and_cost_are_additive(api):
    app, client, token = api
    now = clock()
    for index in range(8):
        seed(app, token, now - (8-index)*1800)
    value = read(app, token, now)
    points = [row["context_estimate"] for row in value["trend"]]
    assert context(value)["total"]["estimated_tokens"] == sum(p["total"]["estimated_tokens"] for p in points)
    assert context(value)["reference_cost"]["amount_pico_usd"] == sum(p["reference_cost"]["amount_pico_usd"] for p in points)


def test_default_poll_rounds_are_estimates_not_call_count(api):
    app, client, token = api
    now = clock()
    for index in range(20):
        seed(app, token, now-60+index*2, request=25, response=60, tool="operations_get")
    value = context(read(app, token, now))
    assert value["grouped_tool_rounds"] == 4
    assert value["provisional_tails"] == 1
    assert value["estimated_model_rounds"] == 5
    assert value["total"]["low"] < value["total"]["estimated_tokens"] < value["total"]["high"]


def test_cache_hit_does_not_query_activity_or_replay_and_returns_copy(api, monkeypatch):
    import hub.token_dashboard as module
    app, client, token = api
    now = clock()
    seed(app, token, now-60)
    calls = []
    real = module.context_usage
    def measured(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)
    monkeypatch.setattr(module, "context_usage", measured)
    first = read(app, token, now)
    all_method = app.state.store.all
    def guarded(sql, values=()):
        assert "FROM mcp_activity a" not in sql
        return all_method(sql, values)
    monkeypatch.setattr(app.state.store, "all", guarded)
    first["summary"]["context_estimate"]["total"]["estimated_tokens"] = -1
    second = read(app, token, now)
    assert context(second)["total"]["estimated_tokens"] > 0
    assert len(calls) == 1


def test_late_usage_for_existing_activity_invalidates_cache(api):
    app, client, token = api
    now = clock()
    identifier = seed(app, token, now-60, record=False)
    first = read(app, token, now)
    assert context(first)["total"]["estimated_tokens"] is None
    app.state.runtime.integrations.usage_metrics.record(identifier, usage(metric(100), metric(1000)))
    second = read(app, token, now)
    assert context(second)["total"]["estimated_tokens"] > 0
    assert first["summary"]["wire_attempts"] == second["summary"]["wire_attempts"] == 1


def test_same_id_metric_update_and_external_writer_invalidate_cache(api):
    app, client, token = api
    now = clock()
    identifier = seed(app, token, now-60)
    first = context(read(app, token, now))["total"]["estimated_tokens"]
    store = app.state.store
    with store.lock:
        before = store.db.total_changes
    with sqlite3.connect(store.directory / "hub.sqlite3") as connection:
        connection.execute("UPDATE mcp_usage SET metrics=? WHERE activity_id=?",
                           (json.dumps(usage(metric(100), metric(5000))), identifier))
    with store.lock:
        assert store.db.total_changes == before
    second = context(read(app, token, now))["total"]["estimated_tokens"]
    assert second > first


def test_cached_result_never_survives_revocation_or_mapping_change(api):
    app, client, token = api
    now = clock()
    seed(app, token, now-60)
    assert context(read(app, token, now))["total"]["estimated_tokens"] > 0
    app.state.store.execute("UPDATE projects SET root='/new-map' WHERE id='project'")
    assert context(read(app, token, now))["total"]["estimated_tokens"] is None
    p = principal(app, token)
    app.state.store.execute("UPDATE grants SET revoked=1 WHERE id=?", (p.grant_id,))
    with pytest.raises(DevError):
        token_dashboard(app.state.runtime, p, ZoneInfo("UTC"), now=now)


def test_grants_filters_and_anonymous_windows_never_share_cached_values(api):
    app, client, token = api
    now = clock()
    p = principal(app, token)
    other = app.state.auth.issue_grant(replace(p, actor="panel:admin", admin=True, token_hash=""),
                                       "other synthetic connection", ["read"], ["project"])["token"]
    seed(app, token, now-60, response=1000, window="first-window")
    seed(app, other, now-60, response=9000, window="second-window")
    mine, theirs = read(app, token, now), read(app, other, now)
    assert context(mine)["total"]["estimated_tokens"] != context(theirs)["total"]["estimated_tokens"]
    assert "second-window" not in json.dumps(mine)
    assert context(read(app, token, now, connection=principal(app, other).grant_id))["total"]["estimated_tokens"] is None
    assert context(read(app, token, now, session="second-window"))["total"]["estimated_tokens"] is None
    assert context(read(app, token, now)) == context(mine)


def test_reference_model_cache_assumption_preserve_tokens_and_cache_is_bounded(api):
    app, client, token = api
    now = clock()
    seed(app, token, now-60)
    normal = context(read(app, token, now))
    cheaper = context(read(app, token, now, reference_model="gpt-6-luna"))
    uncached = context(read(app, token, now, cache_read_percent=0))
    assert normal["total"] == cheaper["total"] == uncached["total"]
    assert cheaper["reference_cost"]["estimated_usd"] < normal["reference_cost"]["estimated_usd"] < uncached["reference_cost"]["estimated_usd"]
    assert normal["output"] == uncached["output"]
    for percentage in range(CACHE_ENTRIES + 3):
        read(app, token, now, cache_read_percent=percentage)
    assert len(app.state.runtime._token_dashboard_cache) <= CACHE_ENTRIES


def test_retention_deadline_expires_cached_result_without_database_write(api, monkeypatch):
    import hub.token_dashboard as module
    app, client, token = api
    now = clock()
    real_now = time.time()
    identifier = seed(app, token, now-60)
    app.state.store.execute("UPDATE mcp_usage SET created=? WHERE activity_id=?",
                            (real_now-RETENTION_SECONDS+2, identifier))
    first = read(app, token, now)
    assert context(first)["total"]["estimated_tokens"] is not None
    monkeypatch.setattr(module.time, "time", lambda: real_now + 3)
    second = read(app, token, now)
    assert context(second)["total"]["estimated_tokens"] is None


def test_missing_truncated_and_malformed_metadata_do_not_become_actual_usage(api):
    app, client, token = api
    now = clock()
    identifier = seed(app, token, now-60, window=None)
    encoded = usage(metric(100), {**metric(1000), "source_truncated": True})
    app.state.store.execute("UPDATE mcp_usage SET metrics=? WHERE activity_id=?", (json.dumps(encoded), identifier))
    value = context(read(app, token, now))
    assert value["coverage"]["inferred_context_attempts"] == 1
    assert value["coverage"]["truncated_payload_sides"] == 1
    assert value["total"]["partial"] is True
    assert value["actual_usage"] is None
    app.state.store.execute("UPDATE mcp_usage SET metrics=? WHERE activity_id=?", ('{"input": "bad"}', identifier))
    invalid = read(app, token, now)
    assert invalid["coverage"]["collection_unavailable"] is True
    assert context(invalid)["total"]["estimated_tokens"] is None
    assert invalid["summary"]["total"]["estimated_tokens"] is None


def test_replay_does_not_expose_opaque_row_fields_or_raw_payload(api):
    app, client, token = api
    now = clock()
    seed(app, token, now-60)
    value = context(read(app, token, now))
    text = json.dumps(value)
    for marker in ("window_key", "grant_id", "root", "actor", "contributions", "token_usage"):
        assert marker not in text
    assert value["reference_cost"]["actual_cache_hit_rate"] is None


def test_unknown_today_does_not_borrow_yesterdays_other_window_metrics(api):
    app, client, token = api
    midnight = datetime(2026, 10, 10, tzinfo=ZoneInfo("UTC")).timestamp()
    seed(app, token, midnight - 60, window="yesterday")
    seed(app, token, midnight + 60, window="today", record=False)
    result = read(app, token, midnight + 120)
    assert context(result)["coverage"]["warmup_attempts"] == 1
    assert context(result)["state"] == "unavailable"
    assert context(result)["total"]["estimated_tokens"] is None
    assert context(result)["reference_cost"]["amount_pico_usd"] is None
    assert all(point["context_estimate"]["state"] == "unavailable" for point in result["trend"])


def test_unknown_today_same_window_keeps_authorized_warm_history(api):
    app, client, token = api
    midnight = datetime(2026, 10, 10, tzinfo=ZoneInfo("UTC")).timestamp()
    seed(app, token, midnight - 60)
    seed(app, token, midnight + 60, record=False)
    result = context(read(app, token, midnight + 120))
    assert result["state"] == "partial"
    assert result["input"]["estimated_tokens"] > 0
    assert result["coverage"]["missing_payload_sides"] == 2


def test_unknown_new_window_does_not_inflate_measured_period_or_cost(api):
    app, client, token = api
    now = clock()
    seed(app, token, now - 7200, window="measured")
    before = context(read(app, token, now))
    seed(app, token, now - 60, window="unknown", record=False)
    value = read(app, token, now)
    after = context(value)
    assert after["total"] == before["total"]
    assert after["reference_cost"] == before["reference_cost"]
    assert after["coverage"]["unmeasured_rounds"] == 2
    assert value["trend"][-1]["context_estimate"]["state"] == "unavailable"
