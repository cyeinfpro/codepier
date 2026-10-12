"""Synthetic receipts and storage: no real account, subscription or external send."""
from itertools import product
from dataclasses import replace
from hub.collaboration.events import EventService
import asyncio
import json

import pytest

from hub.collaboration.channel_capabilities import (
    FEATURES, describe, diagnostic_summary, intersection, subscription_proposal,
)
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex
from tests.test_dot_telegram_channel import setup, ingest, update, post
from tests.test_channel_enrollment_flow import prepare
from tests.test_dot_channel_shares import approved, artifact
from tests.test_dot_weixin_channel import fixture_channel, ingest as wx_ingest


@pytest.mark.parametrize("facts", list(product([True, False, None], repeat=4)))
def test_intersection_requires_each_independent_fact(facts):
    actual = intersection(*facts)
    assert actual is (False if False in facts else True if all(v is True for v in facts) else None)


def test_compile_time_descriptor_is_truthful_and_unknowns_are_not_enabled():
    for channel in ("telegram", "weixin", "unsupported://adapter"):
        value = describe(channel, verified={name: True for name in FEATURES},
                         authorized={name: True for name in FEATURES},
                         available={name: True for name in FEATURES})
        for name in ("edit", "typing", "read_receipt", "receipt_lookup", "transcription", "preview", "voice"):
            assert value["features"][name]["enabled"] is False
        assert value["media"]["upload_is_delivery"] is False
    value = describe("telegram")
    assert value["features"]["text"]["effective"] is None
    assert value["features"]["text"]["enabled"] is False
    assert value["media"]["upload_limit"] == 50_000_000
    assert value["media"]["download_limit"] == 20_000_000
    assert value["text"]["unit"] == "utf16_code_units"
    assert describe("weixin")["text"]["mirror_unit"] == "utf16_code_units"


def test_sanitized_summary_uses_only_known_features_and_fact_values():
    value = describe("telegram")
    value.update(secret="SECRET", binding_id="PRIVATE", callback="https://private.invalid")
    value["features"]["arbitrary"] = {"effective": "SECRET"}
    value["features"]["text"]["effective"] = "SECRET"
    value["features"]["text"]["evidence_source"] = "PRIVATE"
    public = json.dumps(diagnostic_summary(value))
    assert "SECRET" not in public and "PRIVATE" not in public and "callback" not in public
    assert set(diagnostic_summary(value)["capabilities"]) == set(FEATURES)


def test_real_listing_changes_only_after_current_receipt_and_never_grants_access(collab):
    c, t, account, binding, scope = setup(duplex(collab))
    before = c.s.store.one("SELECT total_changes() AS n")["n"]
    capabilities = t.listing(scope, c.owner)["bindings"][0]["effective_capabilities"]
    assert capabilities["features"]["text"]["effective"] is None
    assert capabilities["features"]["file"]["authorized"] is None
    assert c.s.store.one("SELECT total_changes() AS n")["n"] == before
    source = ingest(c, t, account, [update(2)])["message_ids"][0]
    reply = post(c, source)["message"]["id"]

    class Adapter:
        async def send(self, *args, **kwargs):
            return "synthetic-provider-message"

    t.client_factory = lambda *args: Adapter()
    asyncio.run(c.s.channels.tick())
    capabilities = t.listing(scope, c.owner)["bindings"][0]["effective_capabilities"]
    assert capabilities["features"]["text"]["enabled"] is True
    assert capabilities["features"]["read_receipt"]["enabled"] is False
    c.s.store.execute("UPDATE telegram_bindings SET version=version+1 WHERE id=?", (binding["binding_id"],))
    assert t.listing(scope, c.owner)["bindings"][0]["effective_capabilities"]["features"]["text"]["effective"] is None
    c.s.store.execute("UPDATE telegram_bindings SET state='revoked' WHERE id=?", (binding["binding_id"],))
    assert t.listing(scope, c.owner)["bindings"][0]["effective_capabilities"]["features"]["text"]["enabled"] is False
    assert c.s.store.one("SELECT id FROM collaboration_messages WHERE id=?", (reply,))


def test_enrollment_readiness_restores_exact_pending_and_detects_digest_change(collab):
    c, transport, scope, account, raw = prepare(collab, "telegram")
    binding = transport.bind(raw, c.owner)
    before = c.s.store.one("SELECT total_changes() AS n")["n"]
    first = c.s.channel_enrollment.view(binding["binding_id"])
    second = c.s.channel_enrollment.view(binding["binding_id"])
    assert first == second and first["readiness"]["same_pending_object"]
    assert first["readiness"]["ready_for_private_proof"]
    assert not first["readiness"]["identity_verified"]
    assert c.s.store.one("SELECT total_changes() AS n")["n"] == before
    c.s.store.execute("UPDATE telegram_bindings SET disable_notification=1 WHERE id=?", (binding["binding_id"],))
    changed = c.s.channel_enrollment.view(binding["binding_id"])
    assert changed["readiness"]["scope_unchanged"] is False
    assert changed["readiness"]["changed_fields"] == ["binding"]
    assert not changed["auto_complete"]
    assert not c.s.channel_owner.current(binding["binding_id"])


@pytest.mark.parametrize("host,account,approval", product([True, False, None], repeat=3))
def test_long_lived_proposal_stays_read_only_and_finite(host, account, approval):
    result = subscription_proposal(requested_ttl_ms=None, host_verified=host,
                                   account_verified=account, independent_approval=approval)
    assert result["effective_ttl_ms"] == 86_400_000
    assert result["downgraded"] and not result["long_lived"]["enabled"]
    assert not result["changes_subscription"] and not result["changes_grant"]
    assert not result["changes_work_lease"] and not result["resumes_paused_subscription"]


def test_event_catalog_exposes_proposal_without_mutating_subscription(collab):
    c = duplex(collab)
    before = c.s.store.one("SELECT total_changes() AS n")["n"]
    c.s.config = replace(c.s.config, events_enabled=True)
    result = EventService(c.s).catalog({}, c.actor)
    assert result["_meta"]["codepier/subscription_lifetime"]["long_lived"]["implemented"] is False
    assert c.s.store.one("SELECT total_changes() AS n")["n"] == before


def test_manifest_separates_upload_from_send_and_does_not_load_bytes(collab, monkeypatch):
    c, w, account, binding, scope, source, args = approved(collab)
    message = c.s.invoke("collaboration", args, c.actor)["message"]
    attachment = message["attachments"][0]
    transfer = c.s.store.one("SELECT * FROM channel_attachment_transfers WHERE attachment_id=?", (attachment["id"],))
    assert transfer and transfer["upload_state"] == "unknown"
    c.s.store.execute("UPDATE channel_attachment_transfers SET upload_state='accepted',upload_observed_at=? WHERE attachment_id=?",
                      (c.clock[0], attachment["id"]))
    c.s.store.execute("UPDATE weixin_parts SET state='unknown' WHERE id=?", (transfer["part_id"],))
    captured, original = [], c.s.store.all
    def record(sql, parameters=()):
        captured.append(sql)
        return original(sql, parameters)
    monkeypatch.setattr(c.s.store, "all", record)
    item = c.s.channels.media.for_message(message["id"])[0]
    assert item["shares"][0]["upload_state"] == "accepted"
    assert item["shares"][0]["send_state"] == "unknown"
    assert not item["shares"][0]["delivered_verified"]
    assert item["sha256"] and item["expires_at"] and item["origin"]["artifact_id"]
    assert item["preview"]["available"] is False
    assert "a.content" not in " ".join(captured) and "private_source" not in " ".join(captured)
    assert len(captured) == 1


def test_bounded_batch_parts_query_replaces_per_message_queries(collab, monkeypatch):
    c, t, account, binding, scope = setup(duplex(collab))
    ids = [post(c, text="synthetic-" + str(i))["message"]["id"] for i in range(12)]
    queries, original = [], c.s.store.all
    def record(sql, parameters=()):
        queries.append(sql)
        return original(sql, parameters)
    monkeypatch.setattr(c.s.store, "all", record)
    result = c.s.channels.for_messages(ids + ids)
    assert set(result) == set(ids)
    assert all(len(result[identifier]) == 1 for identifier in ids)
    assert len([sql for sql in queries if "FROM channel_mirror_parts" in sql]) == 1


def reviewed_file(c, w, account, scope):
    source = wx_ingest(c, w, account)["message_ids"][0]
    reply = post(c, source)["message"]
    identifier = artifact(c)
    selected = {**scope, "message_id": reply["id"], "expected_message_version": reply["version"],
                "artifact_ids": [identifier], "idempotency_key": key()}
    return selected, identifier


def test_weixin_current_file_preview_is_read_only_and_exact_manifest_approval(collab):
    c, w, account, binding, scope = fixture_channel(collab, media=True)
    selected, identifier = reviewed_file(c, w, account, scope)
    before = c.s.store.one("SELECT total_changes() AS n")["n"]
    preview = w.media.review.preview(selected, c.owner)
    assert preview["files"][0]["artifact_id"] == identifier
    assert preview["recipient"]["peer_id"]
    assert preview["binding_version"] and preview["files"][0]["sha256"]
    assert c.s.store.one("SELECT total_changes() AS n")["n"] == before
    assert not c.s.store.all("SELECT * FROM channel_attachments")
    args = {**selected, "preview_digest": preview["preview_digest"], "confirm_file_egress": True}
    result = w.media.review.approve(args, c.owner)
    assert result == w.media.review.approve(args, c.owner)
    assert len(c.s.store.all("SELECT * FROM channel_attachments")) == 1


@pytest.mark.parametrize("change", ["digest", "recipient", "binding_version", "new_file", "expiry"])
def test_weixin_changed_file_review_cannot_send(collab, change):
    c, w, account, binding, scope = fixture_channel(collab, media=True)
    selected, identifier = reviewed_file(c, w, account, scope)
    preview = w.media.review.preview(selected, c.owner)
    if change == "digest":
        c.s.store.execute("UPDATE artifacts SET sha256=? WHERE id=?", ("f" * 64, identifier))
    elif change == "recipient":
        c.s.store.execute("UPDATE weixin_bindings SET peer_id='another' WHERE id=?", (binding["binding_id"],))
    elif change == "binding_version":
        c.s.store.execute("UPDATE weixin_bindings SET version=version+1 WHERE id=?", (binding["binding_id"],))
    elif change == "new_file":
        selected["artifact_ids"].append(artifact(c, "new-file.txt"))
    else:
        c.clock[0] += 4000
    with pytest.raises(DevError):
        w.media.review.approve({**selected, "preview_digest": preview["preview_digest"],
                                "confirm_file_egress": True}, c.owner)
    assert not c.s.store.all("SELECT * FROM channel_attachments")


def test_preapproved_files_are_bound_to_current_recipient_version(collab):
    c, w, account, binding, scope, source, args = approved(collab)
    c.s.store.execute("UPDATE weixin_bindings SET version=version+1 WHERE id=?", (binding["binding_id"],))
    with pytest.raises(DevError, match="当前版本"):
        c.s.invoke("collaboration", args, c.actor)
    assert not c.s.store.all("SELECT * FROM channel_attachments")


@pytest.mark.parametrize("allowed", [True, False])
def test_source_work_identity_requires_goal_authority(collab, monkeypatch, allowed):
    c = duplex(collab)
    row = {"source_operation_id": "source-op", "room_id": c.room["id"],
           "conversation_id": c.slot["conversation_id"], "project_id": c.room["project_id"]}
    goal = {"id": "source-goal", "operation_id": "source-op", "work_item_id": "private-work",
            "room_id": c.room["id"], "conversation_id": c.slot["conversation_id"],
            "source_project_id": c.room["project_id"]}
    original = c.s.store.all
    monkeypatch.setattr(c.s.store, "all", lambda sql, args=(): [goal] if "FROM coordination_operations op" in sql else original(sql, args))
    calls = []
    def authorize(principal, room, current):
        calls.append(current["id"])
        if not allowed:
            raise DevError("NOT_FOUND", "hidden goal", 404)
    monkeypatch.setattr(c.s.coordination, "authorize", authorize)
    c.s.weixin.media.source_work([row], c.owner)
    assert calls == ["source-goal"]
    assert (row.get("source_work_id") == "private-work") is allowed


def test_ready_cache_does_not_bypass_current_share_recipient_version(collab):
    c, w, account, binding, scope, source, args = approved(collab)
    result = c.s.invoke("collaboration", args, c.actor)
    identifier = result["message"]["attachments"][0]["id"]
    claim = w.media.claim(identifier)
    w.media.commit(claim, b"synthetic file only")
    assert w.media.outbound_content(identifier, binding["binding_id"])[0] == b"synthetic file only"
    c.s.store.execute("UPDATE weixin_bindings SET version=version+1 WHERE id=?", (binding["binding_id"],))
    with pytest.raises(DevError, match="绑定版本"):
        w.media.outbound_content(identifier, binding["binding_id"])
