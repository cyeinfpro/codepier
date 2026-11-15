"""Focused contracts for receipt certainty, maintenance and stable audit windows."""

import sqlite3
from types import SimpleNamespace

import pytest

from hub import runtime_panel, agent_roots
from hub.api.activity import audit_rows
from shared.panel_maintenance import PanelMaintenance
from tests.test_file_import_settings import team, saved  # noqa: F401


@pytest.mark.parametrize("module", [runtime_panel, agent_roots])
@pytest.mark.parametrize(
    "state, data, terminal, confirmed",
    [
        ("succeeded", {"state": "needs_review"}, True, False),
        ("succeeded", {"outcome_confirmed": False}, True, False),
        ("succeeded", {"state": "applied"}, True, True),
        ("running", {}, False, False),
        ("needs_review", {}, True, False),
    ],
)
def test_original_receipt_separates_transport_from_effect(
    monkeypatch, module, state, data, terminal, confirmed
):
    principal = SimpleNamespace(space_id="s", actor="panel:u")
    monkeypatch.setattr(
        module, "authorize", lambda *_: (principal, {}) if module is agent_roots else principal
    )
    runtime = SimpleNamespace(
        store=SimpleNamespace(one=lambda *_: {"id": "original"}),
        operation=lambda *_: {"state": state, "result": {"ok": True, "data": data}},
    )
    value = module.receipt(runtime, principal, "d" if module is agent_roots else {"id": "p"}, "key")
    assert value["transport_terminal"] is terminal
    assert value["effect_confirmed"] is confirmed
    assert value["outcome_confirmed"] is confirmed
    assert value["operation_id"] == "original"
    assert value["replay_recommended"] is False


def test_maintenance_ready_is_not_service_health(monkeypatch):
    runtime = SimpleNamespace(store=SimpleNamespace(one=lambda *_: {"n": 0}))
    gate = PanelMaintenance(runtime)
    assert gate.status()["phase"] == "inactive"
    assert gate.status()["ready"] is False
    assert gate.status()["ready_semantics"] == "maintenance_drain_only"
    monkeypatch.setattr(gate, "active", lambda: True)
    assert gate.status()["drain_ready"] is True
    gate.inflight = 1
    assert gate.status()["phase"] == "draining"


def test_native_relay_dependency_preserves_configured_intent(team):
    _, browsers = team
    value = saved(browsers["owner"], {"streaming_enabled": False, "native_relay_enabled": True})
    relay = value["settings"]["native_relay_enabled"]
    assert relay["configured_value"] is True
    assert relay["configured_effective_value"] is True
    assert relay["effective_value"] is False
    assert relay["blocked_by"] == ["streaming_enabled"]
    value = saved(browsers["owner"], {"streaming_enabled": True})
    assert value["settings"]["native_relay_enabled"]["effective_value"] is True
    assert value["settings"]["native_relay_enabled"]["blocked_by"] == []


def test_audit_keyset_stays_stable_across_new_rows_and_filters_authority():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.execute(
        "CREATE TABLE audit(id INTEGER PRIMARY KEY, space_id TEXT, owner_user_id TEXT, actor TEXT, action TEXT, target TEXT, status TEXT, detail TEXT)"
    )

    class Store:
        def all(self, sql, args):
            return [dict(row) for row in db.execute(sql, args)]

    try:
        for row in [(1, "s", "u"), (2, "s", "v"), (3, "other", "u"), (4, "s", "u"), (5, "s", "u")]:
            db.execute(
                "INSERT INTO audit VALUES(?,?,?,?,?,?,?,?)",
                (*row, "panel:u", "read", "p", "ok", "{}"),
            )
        principal = SimpleNamespace(space_id="s", user_id="u", admin=False)
        first = audit_rows(Store(), principal, limit=2)
        assert [r["id"] for r in first] == [5, 4]
        db.execute("INSERT INTO audit VALUES(6,'s','u','panel:u','read','p','ok','{}')")
        rest = audit_rows(Store(), principal, snapshot_id=5, before_id=4)
        assert [r["id"] for r in rest] == [1]
        assert [r["id"] for r in audit_rows(Store(), principal, limit=2, offset=1)] == [5, 4]
    finally:
        db.close()


from tests.collaboration_support import collab as collab  # noqa: E402,F401
from tests.test_multidot_team_coordination import setup_team  # noqa: E402


def test_team_window_cannot_claim_full_list_at_scan_boundary(collab, monkeypatch):
    c = setup_team(collab)
    original = c.s.store.all

    def bounded(sql, args=()):
        rows = original(sql, args)
        if "SELECT * FROM collaboration_teams WHERE" in sql and "LIMIT 101" in sql:
            return rows * 101
        return rows

    monkeypatch.setattr(c.s.store, "all", bounded)
    result = c.t.listing({**c.scope, "conversation_id": c.room["id"]}, c.owner)
    assert len(result["items"]) == 100
    assert result["window"]["complete"] is False
    assert result["window"]["teams_complete"] is False
    assert "total" not in result["window"]
