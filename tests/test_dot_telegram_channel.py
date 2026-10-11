"""Synthetic Telegram only; real stores, identities, fences and channel routing."""

import asyncio
from dataclasses import replace
import json

import pytest

from hub.collaboration.telegram_protocol import TelegramAdapter, split_text
from hub.collaboration.weixin_protocol import WireReply
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, inbox, no_work
from tests.test_dot_weixin_channel import fixture_channel, ingest as wx_ingest

TOKEN = "900001:synthetic_telegram_token_fixture_only"


def update(number, text="讨论项目", *, user=101, **extra):
    return {
        "update_id": number,
        "message": {
            "message_id": number,
            "from": {"id": user, "is_bot": False},
            "chat": {"id": user, "type": "private"},
            "text": text,
            **extra,
        },
    }


def setup(c):
    c.s.config = replace(c.s.config, telegram_enabled=True)
    t = c.s.telegram
    scope = {**c.scope, "conversation_id": c.slot["conversation_id"]}
    a = t.import_account(
        {
            **scope,
            "bot_id": "900001",
            "token": TOKEN,
            "label": "Fixture",
            "confirm_credential_storage": True,
            "confirm_provider_terms": True,
            "idempotency_key": key(),
        },
        c.owner,
    )
    b = t.bind(
        {
            **scope,
            "account_id": a["account_id"],
            "dot_id": c.slot["id"],
            "confirm_external_data_ingress": True,
            "confirm_full_mirror": True,
            "idempotency_key": key(),
        },
        c.owner,
    )
    t.commit_updates(t.claim_poll(a["account_id"]), [update(1, b["pairing_command"])])
    row = t.binding(b["binding_id"], c.owner, c.room)
    t.control(
        {
            **scope,
            "binding_id": row["id"],
            "expected_version": row["version"],
            "action": "activate",
            "confirm_user_id": "101",
            "confirm_chat_id": "101",
            "confirm_same_owner": True,
            "idempotency_key": key(),
        },
        c.owner,
    )
    c.clock[0] += 2
    return c, t, a, b, scope


def ingest(c, t, a, value):
    claim = t.claim_poll(a["account_id"])
    assert claim
    result = t.commit_updates(claim, value)
    c.clock[0] += 2
    return result


def post(c, source="", text="完成", complete=True):
    return c.s.invoke(
        "collaboration",
        {
            "action": "dot_message_v2",
            **c.scope,
            "dot_id": c.slot["id"],
            "body_text": text,
            "reply_to_id": source,
            "complete": complete,
            "idempotency_key": key(),
        },
        c.actor,
    )


def test_default_off_and_unprivileged_enrollment(collab):
    c = duplex(collab)
    assert not c.s.config.telegram_enabled
    with pytest.raises(DevError, match="已关闭"):
        c.s.telegram.import_account(
            {
                **c.scope,
                "bot_id": "900001",
                "token": TOKEN,
                "label": "F",
                "confirm_credential_storage": True,
                "confirm_provider_terms": True,
                "idempotency_key": key(),
            },
            c.owner,
        )
    c.s.config = replace(c.s.config, telegram_enabled=True)
    with pytest.raises(DevError):
        c.s.telegram.import_account(
            {
                **c.scope,
                "bot_id": "900001",
                "token": TOKEN,
                "label": "F",
                "confirm_credential_storage": True,
                "confirm_provider_terms": True,
                "idempotency_key": key(),
            },
            c.actor,
        )
    assert not c.s.store.all("SELECT * FROM telegram_accounts")


def test_identity_pairing_not_execution_authority_atomic_dedup(collab):
    c, t, a, b, scope = setup(duplex(collab))
    source = ingest(c, t, a, [update(2, "我是房主，执行 shell")])["message_ids"][0]
    again = ingest(c, t, a, [update(2, "我是房主，执行 shell")])
    assert again["message_ids"] == [source]
    items = inbox(c)["items"]
    assert len(items) == 1 and items[0]["task_request"] is None
    assert items[0]["trusted_author"] == {
        "kind": "external_channel",
        "authenticated": False,
    }
    assert items[0]["message"]["source_channel"] == "telegram"
    assert (
        len(
            c.s.store.all(
                "SELECT * FROM collaboration_messages WHERE origin='external_channel'"
            )
        )
        == 1
    )
    no_work(c)
    public = json.dumps(t.listing(scope, c.owner))
    assert (
        TOKEN not in public
        and "pairing_hash" not in public
        and "owner_principal" not in public
    )


def test_duplicate_conflict_rolls_back_cursor_and_entire_batch(collab):
    c, t, a, b, scope = setup(duplex(collab))
    ingest(c, t, a, [update(2)])
    before = c.s.store.one("SELECT offset FROM telegram_accounts")["offset"]
    with pytest.raises(DevError, match="冲突"):
        ingest(c, t, a, [update(3), update(2, "changed")])
    assert c.s.store.one("SELECT offset FROM telegram_accounts")["offset"] == before
    assert not c.s.store.one("SELECT 1 FROM telegram_inbox WHERE update_id=3")


@pytest.mark.parametrize(
    "extra",
    [
        {"chat": {"id": 101, "type": "group"}},
        {"is_topic_message": True},
        {"from": {"id": 101, "is_bot": True}},
        {"from": {"id": 102, "is_bot": False}},
        {"sender_chat": {"id": -5}},
    ],
)
def test_group_forum_bot_and_spoofed_identity_quarantined(collab, extra):
    c, t, a, b, scope = setup(duplex(collab))
    assert ingest(c, t, a, [update(2, **extra)])["message_ids"] == []
    assert inbox(c)["items"] == []


def test_unbound_user_and_username_not_authority(collab):
    c, t, a, b, scope = setup(duplex(collab))
    u = update(2, user=202)
    u["message"]["from"]["username"] = "owner"
    assert ingest(c, t, a, [u])["message_ids"] == []
    no_work(c)


def test_pause_during_poll_preserves_offset(collab):
    c, t, a, b, scope = setup(duplex(collab))
    claim = t.claim_poll(a["account_id"])
    row = t.binding(b["binding_id"], c.owner, c.room)
    t.control(
        {
            **scope,
            "binding_id": row["id"],
            "expected_version": row["version"],
            "action": "pause",
            "idempotency_key": key(),
        },
        c.owner,
    )
    with pytest.raises(DevError, match="世代"):
        t.commit_updates(claim, [update(2)])
    assert c.s.store.one("SELECT offset FROM telegram_accounts")["offset"] == 2


def test_reply_is_one_canonical_message_with_per_target_transport_only(collab):
    c, t, a, b, scope = setup(duplex(collab))
    source = ingest(c, t, a, [update(2)])["message_ids"][0]
    result = post(c, source)
    assert len(c.s.store.all("SELECT * FROM channel_mirrors")) == 1
    assert len(c.s.store.all("SELECT * FROM collaboration_messages")) == 2
    calls = []

    class Provider:
        async def send(self, chat, text, **kwargs):
            calls.append((chat, text, kwargs))
            return "500"

    t.client_factory = lambda token: Provider()
    asyncio.run(c.s.channels.tick())
    asyncio.run(c.s.channels.tick())
    assert len(calls) == 1 and calls[0][2]["reply_to"] == "2"
    assert c.s.store.one("SELECT state FROM channel_mirrors")["state"] == "accepted"
    assert len(c.s.store.all("SELECT * FROM collaboration_dot_messages")) == 1
    assert (
        c.s.store.one(
            "SELECT message_id FROM channel_message_map WHERE external_id=?",
            ("out:500",),
        )["message_id"]
        == result["message"]["id"]
    )
    no_work(c)


def test_lost_send_unknown_never_blindly_retried(collab):
    c, t, a, b, scope = setup(duplex(collab))
    source = ingest(c, t, a, [update(2)])["message_ids"][0]
    post(c, source)
    calls = []

    class Provider:
        async def send(self, *args, **kwargs):
            calls.append(1)
            raise TimeoutError("secret request URL must not leak")

    t.client_factory = lambda token: Provider()
    asyncio.run(c.s.channels.tick())
    c.clock[0] += 120
    asyncio.run(c.s.channels.tick())
    assert len(calls) == 1
    assert c.s.store.one("SELECT state FROM channel_mirror_parts")["state"] == "unknown"
    assert (
        c.s.store.one("SELECT state FROM channel_mirrors")["state"] == "needs_attention"
    )


def test_rate_limit_retries_only_same_part_after_wait(collab):
    c, t, a, b, scope = setup(duplex(collab))
    post(c)
    calls = []

    class Provider:
        async def send(self, *args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise DevError(
                    "TELEGRAM_RATE_LIMIT", "wait", 429, retry_after_seconds=30
                )
            return "400"

    t.client_factory = lambda token: Provider()
    asyncio.run(c.s.channels.tick())
    asyncio.run(c.s.channels.tick())
    assert len(calls) == 1
    c.clock[0] += 31
    asyncio.run(c.s.channels.tick())
    assert (
        len(calls) == 2
        and c.s.store.one("SELECT state FROM channel_mirrors")["state"] == "accepted"
    )


def test_revocation_and_mapping_change_fence_queued_egress(collab):
    c, t, a, b, scope = setup(duplex(collab))
    post(c)
    c.s.store.execute("UPDATE projects SET root='/tmp/changed' WHERE id='proj'")
    calls = []

    class Provider:
        async def send(self, *args, **kwargs):
            calls.append(1)

    t.client_factory = lambda token: Provider()
    asyncio.run(c.s.channels.tick())
    assert not calls
    assert (
        c.s.store.one("SELECT state FROM channel_mirror_parts")["state"] == "rejected"
    )


def test_long_plaintext_unicode_preserved_and_progress_coalesced(collab):
    c, t, a, b, scope = setup(duplex(collab))
    source = ingest(c, t, a, [update(2)])["message_ids"][0]
    post(c, source, "处理中1", False)
    post(c, source, "处理中2", False)
    post(c, source, "**完成** 😀 <script>" + ("长文😀" * 1800))
    rows = c.s.store.all("SELECT state FROM channel_mirrors ORDER BY created,id")
    assert sum(r["state"] == "superseded" for r in rows) == 2
    parts = c.s.store.all(
        "SELECT body FROM channel_mirror_parts WHERE state='queued' ORDER BY ordinal"
    )
    assert len(parts) > 1
    assert all(
        len(json.loads(p["body"])["text"].encode("utf-16-le")) // 2 <= 4096
        for p in parts
    )
    source = "😀中文\n" * 2000
    assert "".join(split_text(source)) == source


def test_reply_to_mirror_maps_original_thread_without_second_task(collab):
    c, t, a, b, scope = setup(duplex(collab))
    source = ingest(c, t, a, [update(2)])["message_ids"][0]
    response = post(c, source)

    class Provider:
        async def send(self, *args, **kwargs):
            return "700"

    t.client_factory = lambda token: Provider()
    asyncio.run(c.s.channels.tick())
    follow = ingest(c, t, a, [update(3, "继续", reply_to_message={"message_id": 700})])[
        "message_ids"
    ][0]
    row = c.s.store.one("SELECT * FROM collaboration_messages WHERE id=?", (follow,))
    assert (
        row["reply_to_id"] == response["message"]["id"]
        and row["thread_root_id"] == source
    )
    no_work(c)


def test_full_weixin_telegram_mirror_needs_exact_weixin_consent(collab):
    c, w, wa, wb, ws = fixture_channel(collab)
    c, t, a, b, scope = setup(c)
    wx_source = wx_ingest(c, w, wa)["message_ids"][0]
    assert len(c.s.store.all("SELECT * FROM channel_mirrors")) == 1
    tg_source = ingest(c, t, a, [update(2)])["message_ids"][0]
    assert not c.s.store.one("SELECT 1 FROM channel_mirrors WHERE channel='weixin'")
    row = w.binding(wb["binding_id"], c.owner, c.room)
    c.s.channels.consent_weixin(
        {
            **ws,
            "binding_id": row["id"],
            "expected_version": row["version"],
            "confirm_full_mirror": True,
            "confirm_same_owner": True,
            "idempotency_key": key(),
        },
        c.owner,
    )
    response = post(c, tg_source)
    rows = c.s.store.all(
        "SELECT channel FROM channel_mirrors WHERE message_id=?",
        (response["message"]["id"],),
    )
    assert {r["channel"] for r in rows} == {"weixin", "telegram"}
    assert len(c.s.store.all("SELECT * FROM collaboration_dot_messages")) == 2
    no_work(c)


def test_media_download_scoped_cached_no_token_url(collab):
    c, t, a, b, scope = setup(duplex(collab))
    u = update(
        2,
        "附件",
        document={"file_id": "fixture-file", "file_name": "a.txt", "file_size": 5},
    )
    source = ingest(c, t, a, [u])["message_ids"][0]
    attachment = c.s.channels.media.for_message(source)[0]

    class Provider:
        async def download(self, file_id):
            assert file_id == "fixture-file"
            return b"hello"

    t.client_factory = lambda token: Provider()
    asyncio.run(c.s.channels.media.tick())
    data, row, *_ = c.s.channels.media.content(attachment["id"], c.owner)
    assert data == b"hello" and row["state"] == "ready"
    assert "file_id" not in json.dumps(c.s.channels.media.for_message(source))
    c.s.store.execute(
        "UPDATE telegram_bindings SET state='revoked' WHERE id=?", (b["binding_id"],)
    )
    with pytest.raises(DevError):
        c.s.channels.media.content(attachment["id"], c.owner)


def test_retention_gap_visible_without_false_complete_claim(collab):
    c, t, a, b, scope = setup(duplex(collab))
    c.s.store.execute(
        "UPDATE telegram_accounts SET last_sync_at=?", (c.clock[0] - 90000,)
    )
    result = ingest(c, t, a, [])
    assert result["retention_gap_possible"]
    assert (
        t.listing(scope, c.owner)["accounts"][0]["reason_code"]
        == "retention_gap_possible"
    )


def test_protocol_fixed_origin_plaintext_and_safe_error():
    calls = []

    async def transport(method, url, headers, body, **kwargs):
        calls.append((url, json.loads(body)))
        return WireReply(
            200,
            json.dumps(
                {"ok": True, "result": {"message_id": 99, "chat": {"id": 101}}}
            ).encode(),
        )

    client = TelegramAdapter(TOKEN, transport=transport)
    assert asyncio.run(client.send("101", "<b>not markup</b>", silent=True)) == "99"
    assert (
        "parse_mode" not in calls[0][1] and calls[0][1]["disable_notification"] is True
    )
    assert calls[0][0].startswith("https://api.telegram.org/bot")

    async def lost(*args, **kwargs):
        raise RuntimeError(TOKEN)

    with pytest.raises(DevError) as exc:
        asyncio.run(TelegramAdapter(TOKEN, transport=lost).send("101", "text"))
    assert TOKEN not in str(exc.value)


def test_protocol_refuses_existing_webhook():
    async def transport(method, url, headers, body, **kwargs):
        data = (
            {"id": 900001, "is_bot": True}
            if url.endswith("getMe")
            else {"url": "https://existing.example/hook"}
        )
        return WireReply(200, json.dumps({"ok": True, "result": data}).encode())

    with pytest.raises(DevError, match="已有 webhook"):
        asyncio.run(TelegramAdapter(TOKEN, transport=transport).verify("900001"))


def test_unapproved_identity_activation_is_rejected(collab):
    c = duplex(collab)
    c.s.config = replace(c.s.config, telegram_enabled=True)
    t = c.s.telegram
    scope = {**c.scope, "conversation_id": c.slot["conversation_id"]}
    a = t.import_account(
        {
            **scope,
            "bot_id": "900001",
            "token": TOKEN,
            "label": "Fixture",
            "confirm_credential_storage": True,
            "confirm_provider_terms": True,
            "idempotency_key": key(),
        },
        c.owner,
    )
    b = t.bind(
        {
            **scope,
            "account_id": a["account_id"],
            "dot_id": c.slot["id"],
            "confirm_external_data_ingress": True,
            "confirm_full_mirror": True,
            "idempotency_key": key(),
        },
        c.owner,
    )
    t.commit_updates(t.claim_poll(a["account_id"]), [update(1, b["pairing_command"])])
    for user, chat, same in [
        ("102", "101", True),
        ("101", "102", True),
        ("101", "101", False),
    ]:
        with pytest.raises(DevError, match="核对"):
            t.control(
                {
                    **scope,
                    "binding_id": b["binding_id"],
                    "expected_version": 2,
                    "action": "activate",
                    "confirm_user_id": user,
                    "confirm_chat_id": chat,
                    "confirm_same_owner": same,
                    "idempotency_key": key(),
                },
                c.owner,
            )
    assert t.binding(b["binding_id"], c.owner, c.room)["state"] == "awaiting_owner"


def test_revoked_grant_blocks_ingress_without_advancing_offset(collab):
    c, t, a, b, scope = setup(duplex(collab))
    claim = t.claim_poll(a["account_id"])
    c.s.store.execute("UPDATE grants SET revoked=1 WHERE id=?", (c.actor.grant_id,))
    with pytest.raises(DevError):
        t.commit_updates(claim, [update(2)])
    assert c.s.store.one("SELECT offset FROM telegram_accounts")["offset"] == 2
    assert not c.s.store.one("SELECT 1 FROM telegram_inbox WHERE update_id=2")


def test_cross_project_provenance_never_mirrored(collab):
    c, t, a, b, scope = setup(duplex(collab))
    source = ingest(c, t, a, [update(2)])["message_ids"][0]
    row = c.s.store.one("SELECT * FROM collaboration_messages WHERE id=?", (source,))
    body = json.loads(row["body"])
    body["provenance_project_ids"] = ["proj", "otherproj"]
    with c.s.store.transaction():
        c.s.store.execute(
            "UPDATE collaboration_messages SET body=? WHERE id=?",
            (json.dumps(body), source),
        )
        with pytest.raises(DevError):
            c.s.channels.bindings.message(
                "telegram", b["binding_id"], {**row, "body": json.dumps(body)}
            )
    assert not c.s.store.all("SELECT * FROM channel_mirrors")


def test_credential_rotation_never_moves_old_pending_delivery(collab):
    c, t, a, b, scope = setup(duplex(collab))
    post(c)
    t.import_account(
        {
            **scope,
            "bot_id": "900001",
            "token": TOKEN + "replaced",
            "label": "New credential",
            "expected_version": 1,
            "confirm_credential_storage": True,
            "confirm_provider_terms": True,
            "idempotency_key": key(),
        },
        c.owner,
    )
    calls = []

    class Provider:
        async def send(self, *args, **kwargs):
            calls.append(1)

    t.client_factory = lambda token: Provider()
    asyncio.run(c.s.channels.tick())
    assert (
        not calls
        and c.s.store.one("SELECT state FROM channel_mirror_parts")["state"]
        == "rejected"
    )


def test_restart_turns_inflight_send_unknown_and_preserves_offset_on_disk(collab):
    from hub.store import Store
    from hub.runtime import Runtime
    from hub.collaboration.service import CollaborationService

    c, t, a, b, scope = setup(duplex(collab))
    source = ingest(c, t, a, [update(2)])["message_ids"][0]
    post(c, source)
    row = c.s.store.one("SELECT id FROM channel_mirror_parts")
    claim = c.s.channels.claim(row["id"])
    c.s.channels.begin_send(claim)
    c.clock[0] += 61
    # A second independent connection reads the committed on-disk ledger.
    reopened = Store(c.s.store.directory)
    try:
        restored = CollaborationService(
            Runtime(reopened), c.s.config, clock=lambda: c.clock[0]
        )
        restored.channels.reconcile()
        assert reopened.one("SELECT offset FROM telegram_accounts")["offset"] == 3
        assert (
            reopened.one("SELECT state FROM channel_mirror_parts")["state"] == "unknown"
        )
        assert restored.channels.claim(row["id"]) is None
    finally:
        reopened.close()


def test_unknown_first_part_does_not_starve_unrelated_message(collab):
    c, t, a, b, scope = setup(duplex(collab))
    first = post(c, text="长😀" * 3500)
    second = post(c, text="其他消息仍可送达")
    parts = c.s.store.all(
        "SELECT p.* FROM channel_mirror_parts p JOIN channel_mirrors d ON d.id=p.delivery_id WHERE d.message_id=? ORDER BY ordinal",
        (first["message"]["id"],),
    )
    with c.s.store.transaction():
        c.s.store.execute(
            "UPDATE channel_mirror_parts SET state='unknown' WHERE id=?",
            (parts[0]["id"],),
        )
        c.s.channels.refresh(parts[0]["delivery_id"])
    calls = []

    class Provider:
        async def send(self, chat, text, **kwargs):
            calls.append(text)
            return "801"

    t.client_factory = lambda token: Provider()
    asyncio.run(c.s.channels.tick())
    assert len(calls) == 1 and "其他消息" in calls[0]
    assert (
        c.s.store.one(
            "SELECT state FROM channel_mirrors WHERE message_id=?",
            (second["message"]["id"],),
        )["state"]
        == "accepted"
    )


def test_mirror_budget_is_visible_not_silently_dropped(collab, monkeypatch):
    import hub.collaboration.channel_router as router_module

    c, t, a, b, scope = setup(duplex(collab))
    monkeypatch.setattr(router_module, "MAX_DAILY_MIRRORS", 1)
    post(c)
    second = post(c, text="预算已满仍保存原文")
    row = c.s.store.one(
        "SELECT * FROM channel_mirrors WHERE message_id=?", (second["message"]["id"],)
    )
    assert row["state"] == "needs_attention"
    assert (
        c.s.store.one(
            "SELECT reason_code FROM channel_mirror_parts WHERE delivery_id=?",
            (row["id"],),
        )["reason_code"]
        == "daily_mirror_quota"
    )


def test_update_coalesces_original_version_before_send(collab):
    c, t, a, b, scope = setup(duplex(collab))
    source = ingest(c, t, a, [update(2)])["message_ids"][0]
    prior = post(c, source, "阶段", False)
    c.s.dot_relay.update(
        {
            **c.scope,
            "dot_id": c.slot["id"],
            "message_id": prior["message"]["id"],
            "expected_version": 1,
            "body_text": "最后回复",
            "complete": True,
            "idempotency_key": key(),
        },
        c.actor,
    )
    rows = c.s.store.all(
        "SELECT message_version,state FROM channel_mirrors ORDER BY message_version"
    )
    assert rows == [
        {"message_version": 1, "state": "superseded"},
        {"message_version": 2, "state": "queued"},
    ]


def test_stale_media_completion_cannot_publish_after_binding_change(collab):
    c, t, a, b, scope = setup(duplex(collab))
    source = ingest(
        c,
        t,
        a,
        [
            update(
                2, document={"file_id": "fixture", "file_name": "a.txt", "file_size": 5}
            )
        ],
    )["message_ids"][0]
    attachment = c.s.channels.media.for_message(source)[0]
    claim = c.s.channels.media.claim(attachment["id"])
    row = t.binding(b["binding_id"], c.owner, c.room)
    t.control(
        {
            **scope,
            "binding_id": row["id"],
            "expected_version": row["version"],
            "action": "pause",
            "idempotency_key": key(),
        },
        c.owner,
    )
    with pytest.raises(DevError):
        c.s.channels.media.commit(claim, b"hello")
    assert c.s.store.one("SELECT content FROM channel_attachments")["content"] is None


def test_missing_media_marks_reason_and_preserves_canonical_message(collab):
    c, t, a, b, scope = setup(duplex(collab))
    source = ingest(
        c,
        t,
        a,
        [
            update(
                2, document={"file_id": "fixture", "file_name": "a.txt", "file_size": 5}
            )
        ],
    )["message_ids"][0]

    class Provider:
        async def download(self, file_id):
            raise DevError("TELEGRAM_FILE_UNAVAILABLE", "missing", 404)

    t.client_factory = lambda token: Provider()
    asyncio.run(c.s.channels.media.tick())
    assert c.s.store.one("SELECT id FROM collaboration_messages")["id"] == source
    assert c.s.channels.media.for_message(source)[0]["state"] == "pending"
    c.clock[0] += 500
    for _ in range(5):
        asyncio.run(c.s.channels.media.tick())
        c.clock[0] += 500
    assert c.s.channels.media.for_message(source)[0]["state"] == "unavailable"


def test_protocol_429_is_explicit_retry_and_invalid_file_path_rejected():
    async def limited(*args, **kwargs):
        return WireReply(
            429, b'{"ok":false,"error_code":429,"parameters":{"retry_after":37}}'
        )

    with pytest.raises(DevError) as exc:
        asyncio.run(TelegramAdapter(TOKEN, transport=limited).send("101", "x"))
    assert exc.value.status == 429 and exc.value.details["retry_after_seconds"] == 37

    async def malicious(*args, **kwargs):
        return WireReply(
            200, b'{"ok":true,"result":{"file_path":"../secret.txt","file_size":1}}'
        )

    with pytest.raises(DevError, match="安全路径"):
        asyncio.run(TelegramAdapter(TOKEN, transport=malicious).download("fixture"))


def file_selection(c, t, b, scope, ids):
    message = post(c, text="已生成的文件")
    binding = t.binding(b["binding_id"], c.owner, c.room)
    return {
        **scope,
        "binding_id": binding["id"],
        "expected_binding_version": binding["version"],
        "message_id": message["message"]["id"],
        "expected_message_version": 1,
        "artifact_ids": ids,
        "idempotency_key": key(),
    }


def test_file_preview_is_read_only_and_approval_exact_idempotent(collab):
    from tests.test_dot_channel_shares import artifact

    c, t, a, b, scope = setup(duplex(collab))
    aid = artifact(c)
    args = file_selection(c, t, b, scope, [aid])
    preview = t.files.preview(args, c.owner)
    assert preview["recipient"] == {
        "channel": "telegram",
        "user_id": "101",
        "chat_id": "101",
    }
    assert not c.s.store.all(
        "SELECT * FROM channel_attachments WHERE direction='outbound'"
    )
    with pytest.raises(DevError):
        t.files.approve(
            {
                **args,
                "preview_digest": preview["preview_digest"],
                "confirm_file_egress": False,
            },
            c.owner,
        )
    approved = {
        **args,
        "preview_digest": preview["preview_digest"],
        "confirm_file_egress": True,
    }
    receipt = t.files.approve(approved, c.owner)
    assert t.files.approve(approved, c.owner) == receipt
    assert (
        len(
            c.s.store.all(
                "SELECT * FROM channel_attachments WHERE channel='telegram' AND direction='outbound'"
            )
        )
        == 1
    )
    assert (
        len(c.s.store.all("SELECT * FROM channel_mirror_parts WHERE kind='attachment'"))
        == 1
    )


def test_files_reject_changed_preview_wrong_project_and_overlimit(collab):
    from tests.test_dot_channel_shares import artifact

    c, t, a, b, scope = setup(duplex(collab))
    aid = artifact(c)
    args = file_selection(c, t, b, scope, [aid])
    preview = t.files.preview(args, c.owner)
    c.s.store.execute("UPDATE artifacts SET sha256=? WHERE id=?", ("f" * 64, aid))
    with pytest.raises(DevError, match="重新预览"):
        t.files.approve(
            {
                **args,
                "preview_digest": preview["preview_digest"],
                "confirm_file_egress": True,
            },
            c.owner,
        )
    other = artifact(c, project="otherproj")
    with pytest.raises(DevError):
        t.files.preview({**args, "artifact_ids": [other]}, c.owner)
    c.s.store.execute("UPDATE artifacts SET bytes=? WHERE id=?", (50_000_001, aid))
    with pytest.raises(DevError, match="50MB"):
        t.files.preview(args, c.owner)
    assert not c.s.store.all("SELECT * FROM channel_attachments")


def test_file_bytes_resume_hash_verify_then_send_original_once(collab, monkeypatch):
    from tests.test_dot_channel_shares import artifact

    c, t, a, b, scope = setup(duplex(collab))
    data = b"actual-original-file"
    aid = artifact(c, "proof.txt", content=data)
    args = file_selection(c, t, b, scope, [aid])
    preview = t.files.preview(args, c.owner)
    receipt = t.files.approve(
        {
            **args,
            "preview_digest": preview["preview_digest"],
            "confirm_file_egress": True,
        },
        c.owner,
    )
    offsets = []

    async def chunk(artifact_row, project, offset):
        offsets.append(offset)
        if offsets == [0]:
            return data[:5]
        if offsets == [0, 5]:
            raise DevError("AGENT_OFFLINE", "fixture offline", 503)
        return data[offset:]

    monkeypatch.setattr(c.s.runtime.artifacts, "chunk", chunk)
    attachment = receipt["attachment_ids"][0]
    asyncio.run(c.s.channels.media.materialize(attachment))
    assert c.s.store.one("SELECT bytes FROM channel_attachment_chunks")["bytes"] == 5
    c.clock[0] += 400
    asyncio.run(c.s.channels.media.materialize(attachment))
    assert offsets == [0, 5, 5]
    assert c.s.channels.media.content(attachment, c.owner)[0] == data
    calls = []

    class Provider:
        async def send(self, *args, **kwargs):
            calls.append("text")
            return "901"

        async def send_document(self, chat, content, name, **kwargs):
            assert chat == "101" and content == data and name == "proof.txt"
            calls.append("file")
            return "902"

    t.client_factory = lambda token: Provider()
    for _ in range(4):
        asyncio.run(c.s.channels.tick())
        c.clock[0] += 4
    assert calls == ["text", "file"]
    assert c.s.store.one("SELECT state FROM channel_mirrors")["state"] == "accepted"
    assert c.s.store.one("SELECT COUNT(*) AS n FROM artifacts")["n"] == 1


def test_unknown_file_outcome_is_not_reuploaded(collab, monkeypatch):
    from tests.test_dot_channel_shares import artifact

    c, t, a, b, scope = setup(duplex(collab))
    data = b"image-file"
    args = file_selection(c, t, b, scope, [artifact(c, "picture.png", content=data)])
    preview = t.files.preview(args, c.owner)
    receipt = t.files.approve(
        {
            **args,
            "preview_digest": preview["preview_digest"],
            "confirm_file_egress": True,
        },
        c.owner,
    )

    async def chunk(*args):
        return data

    monkeypatch.setattr(c.s.runtime.artifacts, "chunk", chunk)
    asyncio.run(c.s.channels.media.materialize(receipt["attachment_ids"][0]))
    calls = []

    class Provider:
        async def send(self, *args, **kwargs):
            return "903"

        async def send_document(self, *args, **kwargs):
            calls.append(1)
            raise TimeoutError("unknown")

    t.client_factory = lambda token: Provider()
    for _ in range(4):
        asyncio.run(c.s.channels.tick())
        c.clock[0] += 70
    assert calls == [1]
    assert (
        c.s.store.one("SELECT state FROM channel_mirror_parts WHERE kind='attachment'")[
            "state"
        ]
        == "unknown"
    )


def test_file_approval_cannot_survive_binding_pause_resume(collab):
    from tests.test_dot_channel_shares import artifact

    c, t, a, b, scope = setup(duplex(collab))
    args = file_selection(c, t, b, scope, [artifact(c)])
    preview = t.files.preview(args, c.owner)
    receipt = t.files.approve(
        {
            **args,
            "preview_digest": preview["preview_digest"],
            "confirm_file_egress": True,
        },
        c.owner,
    )
    for action in ["pause", "resume"]:
        row = t.binding(b["binding_id"], c.owner, c.room)
        t.control(
            {
                **scope,
                "binding_id": row["id"],
                "expected_version": row["version"],
                "action": action,
                "idempotency_key": key(),
            },
            c.owner,
        )
    with pytest.raises(DevError):
        c.s.channels.media.row(receipt["attachment_ids"][0], c.owner)
    asyncio.run(c.s.channels.tick())
    assert (
        c.s.store.one("SELECT state FROM channel_mirror_parts WHERE kind='attachment'")[
            "state"
        ]
        == "rejected"
    )


def test_file_protocol_multipart_has_no_external_url_and_keeps_exact_bytes():
    calls = []

    async def transport(method, url, headers, body, **kwargs):
        calls.append((url, headers, body))
        return WireReply(
            200, b'{"ok":true,"result":{"message_id":100,"chat":{"id":101}}}'
        )

    value = b"\x00original\xffbytes"
    result = asyncio.run(
        TelegramAdapter(TOKEN, transport=transport).send_document(
            "101", value, "picture.png", silent=True
        )
    )
    assert result == "100"
    assert calls[0][0].endswith("/sendDocument") and calls[0][1][
        "Content-Type"
    ].startswith("multipart/form-data")
    assert value in calls[0][2]
    assert b'name="document"; filename="picture.png"' in calls[0][2]
    assert b"https://" not in calls[0][2]


def test_weixin_final_and_files_keep_one_legacy_delivery_under_full_sync(collab):
    from tests.test_dot_channel_shares import artifact

    c, w, wa, wb, ws = fixture_channel(collab, media=True)
    c, t, a, b, scope = setup(c)
    row = w.binding(wb["binding_id"], c.owner, c.room)
    c.s.channels.consent_weixin(
        {
            **ws,
            "binding_id": row["id"],
            "expected_version": row["version"],
            "confirm_full_mirror": True,
            "confirm_same_owner": True,
            "idempotency_key": key(),
        },
        c.owner,
    )
    source = wx_ingest(c, w, wa)["message_ids"][0]
    response = post(c, source)
    assert len(c.s.store.all("SELECT * FROM weixin_deliveries")) == 1
    assert not c.s.store.one(
        "SELECT 1 FROM channel_mirrors WHERE channel='weixin' AND message_id=?",
        (response["message"]["id"],),
    )
    w.media.add_artifacts(
        {
            **ws,
            "message_id": response["message"]["id"],
            "expected_message_version": 1,
            "artifact_ids": [artifact(c)],
            "confirm_file_egress": True,
            "idempotency_key": key(),
        },
        c.owner,
    )
    assert len(c.s.store.all("SELECT * FROM weixin_deliveries")) == 1
    assert len(c.s.store.all("SELECT * FROM weixin_parts WHERE kind='text'")) == 1


def test_tg_file_approval_does_not_authorize_weixin_target(collab):
    from tests.test_dot_channel_shares import artifact

    c, w, wa, wb, ws = fixture_channel(collab, media=True)
    c, t, a, b, scope = setup(c)
    args = file_selection(c, t, b, scope, [artifact(c)])
    preview = t.files.preview(args, c.owner)
    receipt = t.files.approve(
        {
            **args,
            "preview_digest": preview["preview_digest"],
            "confirm_file_egress": True,
        },
        c.owner,
    )
    assert not c.s.store.all("SELECT * FROM weixin_parts")
    row = c.s.store.one(
        "SELECT * FROM channel_attachments WHERE id=?", (receipt["attachment_ids"][0],)
    )
    assert row["channel"] == "telegram" and row["binding_id"] == b["binding_id"]
    with pytest.raises(DevError):
        w.media.outbound_content(row["id"], wb["binding_id"])


def test_telegram_limits_use_conservative_decimal_bytes():
    from hub.collaboration.telegram_protocol import DOWNLOAD_LIMIT, UPLOAD_LIMIT

    assert DOWNLOAD_LIMIT == 20_000_000
    assert UPLOAD_LIMIT == 50_000_000


def quota_files(collab, count=3):
    from tests.test_dot_channel_shares import artifact

    c, t, a, b, scope = setup(duplex(collab))
    data = b"abcdefghij"
    args = file_selection(
        c,
        t,
        b,
        scope,
        [artifact(c, f"quota-{i}.txt", content=data) for i in range(count)],
    )
    preview = t.files.preview(args, c.owner)
    receipt = t.files.approve(
        {
            **args,
            "preview_digest": preview["preview_digest"],
            "confirm_file_egress": True,
        },
        c.owner,
    )
    return c, c.s.channels.media, receipt["attachment_ids"], data


def quota_chunk(c, aid, data=b"12345"):
    import base64
    import hashlib

    c.s.store.execute(
        "INSERT INTO channel_attachment_chunks VALUES (?,?,?,?,?)",
        (
            aid,
            0,
            len(data),
            hashlib.sha256(data).hexdigest(),
            c.s.store.encrypt(base64.b64encode(data).decode()),
        ),
    )


def test_quota_claim_counts_other_channel_chunks_and_defers(collab):
    c, media, ids, data = quota_files(collab)
    cap = 256 * 1024 * 1024
    c.s.store.execute(
        "UPDATE channel_attachments SET state='ready',size=? WHERE id=?",
        (cap - 12, ids[2]),
    )
    c.s.store.execute(
        "UPDATE channel_attachments SET channel='weixin' WHERE id=?", (ids[1],)
    )
    quota_chunk(c, ids[1])
    assert media.claim(ids[0]) is None
    row = c.s.store.one("SELECT * FROM channel_attachments WHERE id=?", (ids[0],))
    assert row["state"] == "pending" and row["next_attempt"] > c.clock[0]
    c.s.store.execute(
        "DELETE FROM channel_attachment_chunks WHERE attachment_id=?", (ids[1],)
    )
    c.clock[0] += 301
    assert media.claim(ids[0])


def test_quota_resume_own_chunks_not_double_counted_commit_atomic(collab):
    c, media, ids, data = quota_files(collab)
    cap = 256 * 1024 * 1024
    c.s.store.execute(
        "UPDATE channel_attachments SET state='ready',size=? WHERE id=?",
        (cap - len(data), ids[2]),
    )
    quota_chunk(c, ids[0], data[:5])
    claim = media.claim(ids[0])
    assert claim
    media.save_chunk(claim, 5, data[5:])
    media.save_chunk(claim, 5, data[5:])
    media.commit(claim, data)
    assert not c.s.store.all(
        "SELECT * FROM channel_attachment_chunks WHERE attachment_id=?", (ids[0],)
    )
    assert media.content(ids[0], c.owner)[0] == data


def test_quota_concurrent_chunk_saves_are_atomic(collab):
    from concurrent.futures import ThreadPoolExecutor

    c, media, ids, data = quota_files(collab)
    claims = [media.claim(aid) for aid in ids[:2]]
    cap = 256 * 1024 * 1024
    c.s.store.execute(
        "UPDATE channel_attachments SET state='ready',size=? WHERE id=?",
        (cap - 5, ids[2]),
    )

    def save(claim):
        try:
            media.save_chunk(claim, 0, data[:5])
            return "saved"
        except DevError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, claims))
    assert sorted(results) == ["ATTACHMENT_CACHE_LIMIT", "saved"]
    assert (
        c.s.store.one("SELECT SUM(bytes) AS n FROM channel_attachment_chunks")["n"] == 5
    )


def test_quota_commit_counts_other_inflight_and_preserves_own_chunks(collab):
    c, media, ids, data = quota_files(collab)
    claim = media.claim(ids[0])
    media.save_chunk(claim, 0, data[:5])
    quota_chunk(c, ids[1])
    cap = 256 * 1024 * 1024
    c.s.store.execute(
        "UPDATE channel_attachments SET state='ready',size=? WHERE id=?",
        (cap - len(data), ids[2]),
    )
    with pytest.raises(DevError) as error:
        media.commit(claim, data)
    assert error.value.code == "ATTACHMENT_CACHE_LIMIT"
    assert len(c.s.store.all("SELECT * FROM channel_attachment_chunks")) == 2
    c.s.store.execute(
        "DELETE FROM channel_attachment_chunks WHERE attachment_id=?", (ids[1],)
    )
    media.commit(claim, data)
    assert not c.s.store.all("SELECT * FROM channel_attachment_chunks")


def test_quota_materialize_keeps_pending_after_many_attempts(collab, monkeypatch):
    c, media, ids, data = quota_files(collab)
    cap = 256 * 1024 * 1024
    offsets = []

    async def chunk(artifact, project, offset):
        offsets.append(offset)
        if offset == 0:
            return data[:5]
        c.s.store.execute(
            "UPDATE channel_attachments SET state='ready',size=? WHERE id=?",
            (cap - 5, ids[2]),
        )
        return data[offset:]

    monkeypatch.setattr(c.s.runtime.artifacts, "chunk", chunk)
    c.s.store.execute("UPDATE channel_attachments SET attempts=8 WHERE id=?", (ids[0],))
    asyncio.run(media.materialize(ids[0]))
    row = c.s.store.one("SELECT * FROM channel_attachments WHERE id=?", (ids[0],))
    assert row["state"] == "pending" and row["reason_code"] == "ATTACHMENT_CACHE_LIMIT"
    assert (
        media.store.one("SELECT SUM(bytes) AS n FROM channel_attachment_chunks")["n"]
        == 5
    )
    c.s.store.execute("UPDATE channel_attachments SET size=0 WHERE id=?", (ids[2],))
    c.clock[0] += 301

    async def resume(artifact, project, offset):
        offsets.append(offset)
        return data[offset:]

    monkeypatch.setattr(c.s.runtime.artifacts, "chunk", resume)
    asyncio.run(media.materialize(ids[0]))
    assert offsets == [0, 5, 5]
    assert media.content(ids[0], c.owner)[0] == data


@pytest.mark.parametrize("change", ["pause", "revoke", "generation"])
def test_quota_resume_rechecks_authority_before_any_fetch(collab, monkeypatch, change):
    c, media, ids, data = quota_files(collab)
    quota_chunk(c, ids[0], data[:5])
    row = c.s.store.one("SELECT * FROM channel_attachments WHERE id=?", (ids[0],))
    if change == "pause":
        c.s.store.execute(
            "UPDATE telegram_bindings SET state='paused',version=version+1 WHERE id=?",
            (row["binding_id"],),
        )
    elif change == "revoke":
        c.s.store.execute("UPDATE grants SET revoked=1 WHERE id=?", (c.actor.grant_id,))
    else:
        c.s.store.execute(
            "UPDATE telegram_bindings SET generation=generation+1,version=version+1 WHERE id=?",
            (row["binding_id"],),
        )
    fetched = []

    async def chunk(*args):
        fetched.append(1)
        return data

    monkeypatch.setattr(c.s.runtime.artifacts, "chunk", chunk)
    asyncio.run(media.materialize(ids[0]))
    assert not fetched
    assert (
        c.s.store.one("SELECT state FROM channel_attachments WHERE id=?", (ids[0],))[
            "state"
        ]
        != "ready"
    )
    assert (
        c.s.store.one(
            "SELECT bytes FROM channel_attachment_chunks WHERE attachment_id=?",
            (ids[0],),
        )["bytes"]
        == 5
    )


@pytest.mark.parametrize("first", ["telegram", "weixin"])
def test_two_real_routers_share_atomic_quota(collab, first):
    from concurrent.futures import ThreadPoolExecutor
    from tests.test_dot_channel_shares import approved, artifact

    c, wx, account, binding, scope, source, args = approved(collab)
    c.s.invoke("collaboration", args, c.actor)
    wxids = [
        r["id"]
        for r in c.s.store.all(
            "SELECT id FROM channel_attachments WHERE channel='weixin'"
        )
    ]
    c, tg, a, b, tgs = setup(c)
    data = b"abcdefghij"
    selection = file_selection(
        c, tg, b, tgs, [artifact(c, "tg-share.txt", content=data)]
    )
    preview = tg.files.preview(selection, c.owner)
    receipt = tg.files.approve(
        {
            **selection,
            "preview_digest": preview["preview_digest"],
            "confirm_file_egress": True,
        },
        c.owner,
    )
    tid = receipt["attachment_ids"][0]
    media = c.s.channels.media
    assert wx.media.claim(tid) is None
    assert media.claim(wxids[0]) is None
    claims = {
        "weixin": (wx.media, wx.media.claim(wxids[0])),
        "telegram": (media, media.claim(tid)),
    }
    cap = 256 * 1024 * 1024
    c.s.store.execute(
        "UPDATE channel_attachments SET state='ready',size=? WHERE id=?",
        (cap - 5, wxids[1]),
    )

    def save(channel):
        router, claim = claims[channel]
        try:
            router.save_chunk(claim, 0, data[:5])
            return channel, "saved"
        except DevError as exc:
            return channel, exc.code

    order = [first, "weixin" if first == "telegram" else "telegram"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(save, order))
    assert sum(state == "saved" for _, state in result) == 1
    assert {state for _, state in result} <= {
        "saved",
        "ATTACHMENT_CACHE_QUOTA",
        "ATTACHMENT_CACHE_LIMIT",
    }
    assert (
        c.s.store.one("SELECT SUM(bytes) AS n FROM channel_attachment_chunks")["n"] == 5
    )
    winning = next(channel for channel, state in result if state == "saved")
    assert save(winning)[1] == "saved"
    loser = next(channel for channel, state in result if state != "saved")
    c.s.store.execute(
        "UPDATE channel_attachments SET size=? WHERE id=?", (cap - 10, wxids[1])
    )
    assert save(loser)[1] == "saved"
    assert (
        c.s.store.one("SELECT SUM(bytes) AS n FROM channel_attachment_chunks")["n"]
        == 10
    )
