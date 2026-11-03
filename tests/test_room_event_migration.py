"""Preserve existing event receipts while adding genuine zero-project rooms."""
import sqlite3

import pytest

from hub.collaboration.common import ROOM_INBOX_EVENT, validate
from hub.collaboration.event_contracts import FILTERS, PAYLOADS
from hub.collaboration.room_event_migration import migrate
from hub.collaboration.schema import SCHEMA
from shared.util import DevError


def legacy_database():
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    db.execute("CREATE TABLE collaboration_rooms (id TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE conversation_rooms (id TEXT PRIMARY KEY)")
    for statement in SCHEMA.split(";"):
        if statement.strip().startswith(("CREATE TABLE IF NOT EXISTS mcp_event_", "CREATE INDEX IF NOT EXISTS mcp_event_")):
            db.execute(statement)
    db.execute("INSERT INTO collaboration_rooms VALUES ('partition')")
    db.execute("INSERT INTO conversation_rooms VALUES ('empty-room')")
    db.execute("""INSERT INTO mcp_event_subscriptions
        (id,room_id,principal,grant_id,name,arguments,secret,expires_at,
         verified_until,key_digest,version,scan_seq,ack_seq,created,updated)
        VALUES ('subscription','partition','{}','grant','legacy','{}',
                'synthetic-encrypted-secret',99999,99999,'digest',7,41,39,1,2)""")
    db.execute("""INSERT INTO mcp_event_outbox
        (seq,id,room_id,name,object_id,object_version,data,created)
        VALUES (41,'event','partition','legacy','object',2,'{}',1)""")
    db.execute("""INSERT INTO mcp_event_outbox
        (seq,id,room_id,name,object_id,object_version,data,created)
        VALUES (99,'deleted-event','partition','legacy','deleted',2,'{}',1)""")
    db.execute("DELETE FROM mcp_event_outbox WHERE id='deleted-event'")
    db.execute("""INSERT INTO mcp_event_deliveries
        (id,subscription_id,event_id,event_seq,body,state,attempt,fence,created)
        VALUES ('delivery','subscription','event',41,'original-sealed-body','accepted',3,8,1)""")
    db.execute("CREATE TABLE fixture_receipts (event_id TEXT)")
    db.execute("""CREATE TRIGGER fixture_receipt AFTER UPDATE ON mcp_event_deliveries
        BEGIN INSERT INTO fixture_receipts SELECT id FROM mcp_event_outbox WHERE id=NEW.event_id; END""")
    db.commit()
    return db


def test_room_event_migration_preserves_populated_journals_and_sequence():
    db = legacy_database()
    saved_delivery = db.execute("SELECT * FROM mcp_event_deliveries").fetchall()
    with db:
        migrate(db)
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("SELECT * FROM mcp_event_deliveries").fetchall() == saved_delivery
    assert db.execute("SELECT version,scan_seq,ack_seq,secret FROM mcp_event_subscriptions").fetchone() == (
        7, 41, 39, "synthetic-encrypted-secret")
    assert db.execute("SELECT room_id,conversation_id,seq FROM mcp_event_outbox").fetchone() == (
        "partition", None, 41)
    db.execute("""INSERT INTO mcp_event_outbox
        (id,conversation_id,name,object_id,object_version,data,created)
        VALUES ('room-event','empty-room','room','member',1,'{}',3)""")
    assert db.execute("SELECT seq FROM mcp_event_outbox WHERE id='room-event'").fetchone()[0] == 100
    db.execute("UPDATE mcp_event_deliveries SET fence=fence+1 WHERE id='delivery'")
    assert db.execute("SELECT * FROM fixture_receipts").fetchall() == [("event",)]
    with db:
        migrate(db)
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("SELECT COUNT(*) FROM mcp_event_outbox").fetchone()[0] == 2
    db.close()


@pytest.mark.parametrize("partition,conversation", [(None, None), ("partition", "empty-room")])
def test_event_requires_exactly_one_scope(partition, conversation):
    db = legacy_database()
    with db:
        migrate(db)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("""INSERT INTO mcp_event_outbox
            (id,room_id,conversation_id,name,object_id,object_version,data,created)
            VALUES ('invalid',?,?,'room','member',1,'{}',3)""", (partition, conversation))
    db.close()


def test_room_wire_scope_cannot_inherit_or_expand_legacy_project_filters():
    filters = dict(owner_user_id="owner", conversation_id="room", member_id="member", binding_generation=1)
    assert validate(FILTERS[ROOM_INBOX_EVENT], filters) == filters
    for extra in (dict(project_id="project"), dict(environment_id="production"), dict(slot_id="old-slot")):
        with pytest.raises(DevError):
            validate(FILTERS[ROOM_INBOX_EVENT], {**filters, **extra})
    payload = validate(PAYLOADS[ROOM_INBOX_EVENT], {**filters, "schema_version": 1, "dirty_revision": 2})
    assert not {"body", "project_id", "secret", "callback_url"} & payload.keys()


@pytest.mark.parametrize("field", ["connection_proof", "member_proof", "consumer_proof", "invite_code", "probe_nonce"])
def test_room_proof_redaction_covers_structured_and_inline_diagnostics(field):
    from hub.collaboration.common import redact
    secret = "fixture-room-private-material"
    result = redact({"nested": [{field: secret}], "message": field + "=" + secret})
    assert secret not in str(result)
    assert result["nested"][0][field] == "[REDACTED]"
