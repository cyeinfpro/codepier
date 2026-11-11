"""Shared, recipient-scoped ordering and forward-only backfill for Weixin replies."""

from __future__ import annotations

import json
import logging
import sqlite3

from hub.collaboration.common import canonical
from hub.collaboration.delivery_compatibility import raise_floor
from shared.util import DevError

ORDERING_VERSION = 2
MIGRATION_NAME = "weixin_reply_order_v2"
BATCH_SIZE = 200
RECOVERY_QUARANTINE_REASONS = ("credential_changed", "credential_generation_changed")
LOG = logging.getLogger(__name__)

# All fragments use the fixed aliases prior (predecessor) and d (successor).
# The only cross-generation edge is a recorded, exact logical dependency.
SAME_GENERATION = """prior.binding_id=d.binding_id
    AND prior.generation=d.generation AND prior.peer_id=d.peer_id"""
EXPLICIT = "prior.id=d.depends_on_delivery_id"
PRIOR_EVIDENCE = "CASE WHEN json_valid(prior.ordering_evidence) THEN prior.ordering_evidence END"
EVIDENCE = "CASE WHEN json_valid(d.ordering_evidence) THEN d.ordering_evidence END"
SAME_RECIPIENT = f"""(({SAME_GENERATION}) OR (
    {EXPLICIT} AND prior.peer_id=d.peer_id
    AND json_extract({EVIDENCE},'$.recovery_predecessor_id')=prior.id
    AND EXISTS (SELECT 1 FROM weixin_bindings old JOIN weixin_bindings current
        ON old.account_id=current.account_id AND old.conversation_id=current.conversation_id
        WHERE old.id=prior.binding_id AND current.id=d.binding_id)))"""
SAME_SOURCE = f"""(
    COALESCE(CASE WHEN prior.ordering_schema_version>=2 THEN
        COALESCE(json_extract({PRIOR_EVIDENCE},'$.canonical_input_root_id'),NULLIF(prior.canonical_source_id,'')) END,
        NULLIF(prior.source_message_id,'')) =
    COALESCE(CASE WHEN d.ordering_schema_version>=2 THEN
        COALESCE(json_extract({EVIDENCE},'$.canonical_input_root_id'),NULLIF(d.canonical_source_id,'')) END,
        NULLIF(d.source_message_id,''))
    OR ((prior.ordering_schema_version<2 OR d.ordering_schema_version<2)
        AND prior.source_message_id=d.source_message_id))"""
UNRESOLVED = f"""prior.ordering_key='' OR d.ordering_key=''
    OR (prior.ordering_schema_version>=2 AND json_extract({PRIOR_EVIDENCE},'$.unresolved')=1)
    OR (d.ordering_schema_version>=2 AND json_extract({EVIDENCE},'$.unresolved')=1)"""
RELATION = f"""CASE
    WHEN COALESCE({SAME_RECIPIENT},0)=0 THEN NULL
    WHEN {EXPLICIT} THEN 'explicit_dependency'
    WHEN prior.kind='reply' AND d.kind='reply' AND {SAME_SOURCE} THEN 'same_source'
    WHEN prior.ordering_key!='' AND prior.ordering_key=d.ordering_key THEN 'same_group'
    WHEN prior.kind='reply' AND d.kind='reply' AND ({UNRESOLVED}) THEN 'legacy_relationship_unknown'
    END"""
REQUIRES_SUCCESS = f"""{EXPLICIT}
    AND COALESCE(json_extract({EVIDENCE},'$.dependency_mode'),d.dependency_mode)='requires_success' """
UNSETTLED = """prior.state='waiting_media' OR EXISTS (
    SELECT 1 FROM weixin_parts earlier WHERE earlier.delivery_id=prior.id
    AND earlier.state NOT IN ('accepted','owner_confirmed','cancelled','rejected'))"""
RECOVERABLE_QUARANTINE = f"""earlier.state='cancelled' AND earlier.attempts=0
    AND earlier.reason_code IN {RECOVERY_QUARANTINE_REASONS}
    AND prior.kind='reply' AND EXISTS (SELECT 1 FROM weixin_bindings recovery
        WHERE recovery.id=prior.binding_id AND recovery.native_member_id!=''
        AND recovery.generation>prior.generation AND recovery.state!='revoked'
        AND recovery.peer_id=prior.peer_id AND recovery.allow_replies)"""
FAILED = f"""EXISTS (
    SELECT 1 FROM weixin_parts earlier WHERE earlier.delivery_id=prior.id
    AND earlier.state IN ('cancelled','rejected') AND NOT ({RECOVERABLE_QUARANTINE}))"""
QUARANTINED_PREDECESSOR = f"""EXISTS (
    SELECT 1 FROM weixin_parts earlier WHERE earlier.delivery_id=prior.id
    AND ({RECOVERABLE_QUARANTINE}))"""
PARTS_EXIST = "EXISTS (SELECT 1 FROM weixin_parts earlier WHERE earlier.delivery_id=prior.id)"
DECISION = f"""CASE
    WHEN ({RELATION}) IS NULL THEN 'ready'
    WHEN ({REQUIRES_SUCCESS}) AND ({FAILED}) THEN 'failed'
    WHEN ({UNSETTLED}) OR (({REQUIRES_SUCCESS})
        AND (({QUARANTINED_PREDECESSOR}) OR NOT {PARTS_EXIST})) THEN 'waiting'
    ELSE 'ready' END"""
EARLIER = f"""(({EXPLICIT} AND prior.rowid<d.rowid)
    OR ((prior.created,prior.rowid)<(d.created,d.rowid) AND NOT ({EXPLICIT} IS TRUE)))"""

# The recipient prefilter gives SQLite the indexed normal path, while the exact
# id alternative permits the explicitly recorded cross-generation edge.
PEER_PRIOR = f"""prior.peer_id=d.peer_id
    AND ((prior.binding_id=d.binding_id AND prior.generation=d.generation) OR {EXPLICIT})"""
WAITING_PREDECESSOR = f"""EXISTS (
    SELECT 1 FROM weixin_deliveries prior WHERE {PEER_PRIOR} AND {EARLIER}
    AND ({DECISION})='waiting')"""
FAILED_PREDECESSOR = f"""EXISTS (
    SELECT 1 FROM weixin_deliveries prior WHERE {PEER_PRIOR} AND {EARLIER}
    AND ({DECISION})='failed')"""
SCOPE = f"""CASE
    WHEN ({RELATION})='same_group' AND d.ordering_key LIKE 'work:%' THEN 'same_work'
    WHEN ({RELATION})='same_group' AND d.ordering_key LIKE 'thread:%' THEN 'same_thread'
    WHEN ({RELATION})='legacy_relationship_unknown' THEN 'legacy_peer_unknown'
    WHEN ({RELATION})='explicit_dependency' THEN 'explicit_dependency'
    ELSE 'same_source' END"""


def dependency_state(store, delivery_id):
    row = store.one(
        f"""SELECT CASE WHEN ({FAILED_PREDECESSOR}) THEN 'failed'
        WHEN ({WAITING_PREDECESSOR}) THEN 'waiting' ELSE 'ready' END AS state
        FROM weixin_deliveries d WHERE d.id=?""",
        (delivery_id,),
    )
    return row["state"] if row else "failed"


def _evidence_object(evidence):
    try:
        value = json.loads(evidence) if evidence else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def ordering_reason(key, evidence):
    return _evidence_object(evidence).get("reason_code") or (
        "" if key else "historical_ordering_unverified"
    )


def blocking_predecessors(store, delivery_id, *, limit=51):
    """Nearest blocking delivery first. The caller applies message visibility."""
    return store.all(
        f"""SELECT prior.id,COALESCE(prior.projection_message_id,prior.message_id) AS message_id,
        {SCOPE} AS scope,{RELATION} AS reason_code,{DECISION} AS decision
        FROM weixin_deliveries d JOIN weixin_deliveries prior ON {PEER_PRIOR} AND {EARLIER}
        WHERE d.id=? AND ({DECISION}) IN ('waiting','failed')
        ORDER BY prior.created DESC,prior.rowid DESC LIMIT ?""",
        (delivery_id, limit),
    )


def _successors(store, delivery_id, predicate, limit):
    return store.all(
        f"""SELECT d.id,COALESCE(d.projection_message_id,d.message_id) AS message_id,
        {SCOPE} AS scope,{RELATION} AS reason_code,{DECISION} AS decision
        FROM weixin_deliveries prior JOIN weixin_deliveries d ON {PEER_PRIOR} AND {EARLIER}
        WHERE prior.id=? AND {SAME_RECIPIENT} AND ({predicate}) AND
        (d.state='waiting_media' OR EXISTS (
            SELECT 1 FROM weixin_parts p WHERE p.delivery_id=d.id
            AND p.state NOT IN ('accepted','owner_confirmed','cancelled','rejected')))
        ORDER BY d.created,d.rowid LIMIT ?""",
        (delivery_id, limit),
    )


def blocked_successors(store, delivery_id, *, limit=51):
    return _successors(store, delivery_id, f"({DECISION}) IN ('waiting','failed')", limit)


def successors(store, delivery_id, *, limit=51):
    return _successors(store, delivery_id, REQUIRES_SUCCESS, limit)


def _inbound_identity(store, message):
    if not message or message["origin"] != "external_channel":
        return None
    origin = json.loads(message["body"]).get("external_origin", {})
    if origin.get("channel") != "weixin":
        return None
    key = (
        origin.get("account_id"),
        origin.get("generation"),
        origin.get("peer_id"),
        origin.get("external_message_id"),
    )
    if any(value in (None, "") for value in key):
        return None
    if store.one(
        """SELECT 1 FROM weixin_inbound_versions
        WHERE message_id=? AND account_id=? AND generation=? AND peer_id=? AND external_id=?""",
        (message["id"], *key),
    ):
        return key
    return None


def canonical_input(store, source):
    """Resolve only the server-proven edit chain, never an entire thread."""
    if not source:
        return None
    if source["origin"] == "panel_owner" and source["kind"] in {"text", "reply"}:
        return source["id"]
    identity = _inbound_identity(store, source)
    if not identity:
        return None
    current, seen = source, set()
    for _ in range(64):
        parent_id = current["reply_to_id"]
        if not parent_id:
            return current["id"]
        if parent_id in seen:
            return None
        seen.add(current["id"])
        parent = store.one(
            "SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?",
            (parent_id, source["conversation_id"]),
        )
        if _inbound_identity(store, parent) != identity:
            return None
        current = parent
    return None


def _prior_delivery(store, binding, parent_id, predecessor_id):
    if not (parent_id or predecessor_id):
        return None
    return store.one(
        """SELECT d.* FROM weixin_deliveries d
        JOIN weixin_bindings b ON b.id=d.binding_id
        WHERE b.account_id=? AND b.conversation_id=? AND d.peer_id=?
        AND ((? IS NULL AND d.message_id=?) OR d.id=?)
        ORDER BY (d.binding_id=? AND d.generation=?) DESC,d.message_version DESC,d.created DESC,d.rowid DESC
        LIMIT 1""",
        (
            binding["account_id"],
            binding["conversation_id"],
            binding["peer_id"],
            predecessor_id,
            parent_id,
            predecessor_id,
            binding["id"],
            binding["generation"],
        ),
    )


def _checked_prior(store, binding, parent_id, predecessor_id, dependency_mode):
    if dependency_mode not in {"after_settled", "requires_success"} or (
        dependency_mode == "requires_success" and not predecessor_id
    ):
        raise ValueError("An explicit delivery is required for a typed success dependency")
    prior = _prior_delivery(store, binding, parent_id, predecessor_id)
    if predecessor_id and not prior:
        raise DevError(
            "WEIXIN_DELIVERY_RELATION_INVALID", "原投递依赖不属于当前收件对象及话题", 409
        )
    return prior


def _recovery_predecessor(prior, binding):
    if prior and (
        prior["binding_id"] != binding["id"] or prior["generation"] != binding["generation"]
    ):
        return {"recovery_predecessor_id": prior["id"]}
    return {}


def legacy_reply_dependency(
    store, message, binding, *, predecessor_id=None, dependency_mode="after_settled"
):
    """Keep v1 parent ordering and honor an explicitly authorized dependency."""
    if predecessor_id or dependency_mode != "after_settled":
        prior = _checked_prior(store, binding, None, predecessor_id, dependency_mode)
        evidence = _recovery_predecessor(prior, binding)
        return prior["id"], canonical(evidence) if evidence else ""
    prior = store.one(
        """SELECT id FROM weixin_deliveries
        WHERE binding_id=? AND generation=? AND peer_id=? AND message_id=?
        ORDER BY message_version DESC LIMIT 1""",
        (binding["id"], binding["generation"], binding["peer_id"], message["reply_to_id"]),
    )
    return prior["id"] if prior else None, ""


def reply_ordering(
    store, message, source, binding, *, predecessor_id=None, dependency_mode="after_settled"
):
    """Return the v2 group, evidence and original delivery dependency."""
    body = json.loads(message["body"])
    parent_id = message["reply_to_id"]
    parent = (
        store.one(
            "SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?",
            (parent_id, message["conversation_id"]),
        )
        if parent_id
        else None
    )
    actual_source = source
    if parent and (
        parent["origin"] == "external_channel"
        or (parent["origin"] == "panel_owner" and parent["kind"] in {"text", "reply"})
    ):
        actual_source = parent
    canonical_id = canonical_input(store, actual_source)
    reply_parent = parent_id if parent and parent["origin"] == "bound_connector" else None
    prior = _checked_prior(store, binding, reply_parent, predecessor_id, dependency_mode)
    evidence = {
        "canonical_input_root_id": canonical_id,
        "input_id": actual_source["id"] if actual_source else None,
        "reply_parent_id": parent_id or None,
        "work_id": message.get("goal_id"),
        "thread_root_id": message.get("thread_root_id"),
        "intent_revision": body.get("intent_revision"),
        "dependency_mode": dependency_mode,
        "rule": "persisted_reply_source_v2",
    }
    if not canonical_id or (parent_id and not parent):
        evidence.update(unresolved=True, reason_code="reply_relationship_unverified")
    evidence.update(_recovery_predecessor(prior, binding))
    return (
        ("reply:" + canonical_id if canonical_id else ""),
        canonical(evidence),
        prior["id"] if prior else None,
    )


def status_evidence(source, work_id, intent_revision, observed_at):
    return canonical(
        {
            "rule": "status_source_v2",
            "input_id": source["id"],
            "work_id": work_id,
            "intent_revision": intent_revision,
            "observed_at": observed_at,
        }
    )


def _retained_order(row, reason):
    # A failed inspection records a reason without inventing a relationship.
    evidence = _evidence_object(row["ordering_evidence"])
    if row["ordering_schema_version"] < ORDERING_VERSION:
        evidence.pop("unresolved", None)
    evidence["reason_code"] = reason
    return row["ordering_key"], canonical(evidence), row["ordering_schema_version"], False


def _historical_order(store, row):
    source = store.one(
        """SELECT m.* FROM collaboration_messages m JOIN weixin_bindings b
        ON b.id=? AND b.conversation_id=m.conversation_id WHERE m.id=?""",
        (row["binding_id"], row["source_message_id"]),
    )
    if row["kind"] == "status" and source and row["ordering_key"] == "status:" + source["id"]:
        evidence = status_evidence(source, source.get("goal_id"), None, row["created"])
        return row["ordering_key"], evidence, ORDERING_VERSION, True
    message = store.one(
        """SELECT m.* FROM collaboration_messages m JOIN weixin_bindings b
        ON b.id=? AND b.conversation_id=m.conversation_id
        WHERE m.id=? AND m.origin='bound_connector' """,
        (row["binding_id"], row["message_id"]),
    )
    binding = store.one("SELECT * FROM weixin_bindings WHERE id=?", (row["binding_id"],))
    if message and source and binding:
        original_route = {**binding, "generation": row["generation"], "peer_id": row["peer_id"]}
        old_predecessor = row["depends_on_delivery_id"]
        existing = _evidence_object(row["ordering_evidence"])
        mode = existing.get("dependency_mode", row["dependency_mode"])
        if old_predecessor:
            try:
                key, encoded, predecessor = reply_ordering(
                    store,
                    message,
                    source,
                    original_route,
                    predecessor_id=old_predecessor,
                    dependency_mode=mode,
                )
            except DevError:
                return _retained_order(row, "legacy_dependency_unverified")
        else:
            key, encoded, predecessor = reply_ordering(store, message, source, original_route)
        evidence = json.loads(encoded)
        if (
            key
            and not evidence.get("unresolved")
            and predecessor == old_predecessor
            and evidence.get("recovery_predecessor_id") == existing.get("recovery_predecessor_id")
        ):
            # v1 carries only within-generation explicit edges. Never derive a
            # new historical cross-generation association during backfill.
            evidence["migration_reason"] = "verified_historical_reply"
            return key, canonical(evidence), ORDERING_VERSION, True
        if evidence.get("canonical_input_root_id"):
            return _retained_order(row, "legacy_dependency_unverified")
    return _retained_order(row, "legacy_source_unverified")


def backfill(store, now):
    """Inspect a bounded batch, deferring in-flight records without losing the cursor."""
    with store.transaction():
        migration = store.one(
            "SELECT * FROM weixin_order_migrations WHERE name=?", (MIGRATION_NAME,)
        )
        cursor = migration["last_delivery_id"] if migration else ""
        rows = store.all(
            """SELECT * FROM weixin_deliveries WHERE ordering_checked_version<2 AND id>?
            ORDER BY id LIMIT ?""",
            (cursor, BATCH_SIZE),
        )
        if not rows:
            remaining = store.one(
                "SELECT id FROM weixin_deliveries WHERE ordering_checked_version<2 ORDER BY id LIMIT 1"
            )
            if not remaining and migration and migration["finished_at"] is not None:
                return
        else:
            remaining = True
        if not migration:
            store.execute(
                "INSERT INTO weixin_order_migrations(name,started_at) VALUES (?,?)",
                (MIGRATION_NAME, now),
            )
        checked = changed = unresolved = 0
        for row in rows:
            held = store.one(
                """SELECT 1 FROM weixin_parts WHERE delivery_id=?
                AND (state IN ('preparing','sending') OR lease_until>?) LIMIT 1""",
                (row["id"], now),
            )
            if held:
                continue
            try:
                key, evidence, version, verified = _historical_order(store, row)
            except sqlite3.Error:
                # A database failure affects the batch, so leave it resumable.
                raise
            except Exception as exc:
                LOG.warning("Weixin ordering row inspection failed (%s)", type(exc).__name__)
                key, evidence, version, verified = _retained_order(
                    row, "legacy_ordering_inspection_failed"
                )
            if verified and not changed:
                raise_floor(store)
            updated = store.execute(
                """UPDATE weixin_deliveries SET ordering_key=?,ordering_evidence=?,
                ordering_schema_version=?,ordering_checked_version=2,ordering_revision=ordering_revision+1
                WHERE id=? AND ordering_revision=? AND ordering_key=? AND version=? AND ordering_checked_version<2""",
                (
                    key,
                    evidence,
                    version,
                    row["id"],
                    row["ordering_revision"],
                    row["ordering_key"],
                    row["version"],
                ),
            )
            if updated.rowcount:
                checked += 1
                changed += int(verified)
                unresolved += int(not verified)
        store.execute(
            """UPDATE weixin_order_migrations SET last_delivery_id=?,checked_count=checked_count+?,
            changed_count=changed_count+?,unresolved_count=unresolved_count+?,finished_at=?
            WHERE name=?""",
            (
                rows[-1]["id"] if rows else "",
                checked,
                changed,
                unresolved,
                None if remaining else now,
                MIGRATION_NAME,
            ),
        )
