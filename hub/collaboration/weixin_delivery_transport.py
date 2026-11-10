"""Persistent Weixin admission and immutable dispatch evidence."""

import json
import secrets

from hub.collaboration.common import canonical, digest

MAX_PREPARATION_FAILURES = 5


def migrate(db):
    for statement in (
        """CREATE TABLE IF NOT EXISTS weixin_peer_admission(
            account_id TEXT NOT NULL, peer_id TEXT NOT NULL, last_admitted_sequence INTEGER NOT NULL,
            PRIMARY KEY(account_id,peer_id))""",
        """CREATE INDEX IF NOT EXISTS weixin_admission_sequence
            ON weixin_peer_admission(last_admitted_sequence)""",
        """CREATE TABLE IF NOT EXISTS weixin_account_cooldown(
            account_id TEXT PRIMARY KEY, retry_at REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS weixin_dispatch_attempts(
            part_id TEXT NOT NULL REFERENCES weixin_parts(id), lease_fence INTEGER NOT NULL,
            lease_owner TEXT NOT NULL, identity_digest TEXT NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(part_id,lease_fence))""",
        """CREATE TABLE IF NOT EXISTS weixin_late_observations(
            part_id TEXT NOT NULL, lease_fence INTEGER NOT NULL, identity_digest TEXT NOT NULL,
            receipt TEXT NOT NULL, observed_at REAL NOT NULL, PRIMARY KEY(part_id,lease_fence),
            FOREIGN KEY(part_id,lease_fence) REFERENCES weixin_dispatch_attempts(part_id,lease_fence))""",
        """CREATE INDEX IF NOT EXISTS weixin_late_observation_time
            ON weixin_late_observations(observed_at,part_id,lease_fence)""",
    ):
        db.execute(statement)
    columns = {row[1] for row in db.execute('PRAGMA table_info(weixin_parts)')}
    if 'preparation_failures' not in columns:
        db.execute('ALTER TABLE weixin_parts ADD COLUMN preparation_failures INTEGER NOT NULL DEFAULT 0')
    if 'retry_rejected' not in columns:
        db.execute('ALTER TABLE weixin_parts ADD COLUMN retry_rejected INTEGER NOT NULL DEFAULT 0')
        db.execute("""UPDATE weixin_parts SET retry_rejected=1
            WHERE attempts>0 AND reason_code='WEIXIN_RATE_LIMITED'
              AND state IN ('queued','waiting_context','waiting_media','preparing','attention')""")


def admit(store, account_id, peer_id):
    store.execute("""INSERT INTO weixin_peer_admission(account_id,peer_id,last_admitted_sequence)
        SELECT ?,?,COALESCE(MAX(last_admitted_sequence),0)+1 FROM weixin_peer_admission WHERE 1
        ON CONFLICT(account_id,peer_id) DO UPDATE SET last_admitted_sequence=excluded.last_admitted_sequence""",
        (account_id, peer_id))


def cooldown(store, account_id, retry_at):
    store.execute("""INSERT INTO weixin_account_cooldown(account_id,retry_at) VALUES (?,?)
        ON CONFLICT(account_id) DO UPDATE SET retry_at=MAX(retry_at,excluded.retry_at)""",
        (account_id, retry_at))


def delay(attempts, *, provider_delay=0):
    # Jitter bounded to five seconds preserves an exact longer Retry-After.
    return max(provider_delay, min(900, 15 * 2 ** attempts + secrets.randbelow(6)))


def dispatch_identity(claim, part, wire):
    delivery, route = claim['delivery'], claim['binding']
    return digest([delivery['binding_id'], delivery['generation'], route['id'], route['generation'],
                   claim['account']['id'], delivery['peer_id'], delivery['id'], part['id'], part['client_id'],
                   part['body_digest'], digest(wire)])


def record_attempt(store, claim, part, wire, now):
    store.execute("""INSERT INTO weixin_dispatch_attempts(part_id,lease_fence,lease_owner,identity_digest,created)
        VALUES (?,?,?,?,?)""", (part['id'], part['lease_fence'], part['lease_owner'],
                                 dispatch_identity(claim, part, wire), now))


def accepted_receipt(result):
    if not isinstance(result, dict) or result.get('provider_state') != 'accepted':
        return None
    message_id = result.get('provider_message_id')
    if message_id is not None and (not isinstance(message_id, str) or len(message_id) > 256):
        return None
    return canonical({'provider_state': 'accepted', 'provider_message_id': message_id,
                      'delivered_verified': False, 'read_verified': False})


def record_late(store, claim, row, result, now):
    receipt = accepted_receipt(result)
    if not row or not row['wire_body'] or not receipt:
        return
    wire = json.loads(store.decrypt(row['wire_body']))
    identity = dispatch_identity(claim, row, wire)
    store.execute("""INSERT INTO weixin_late_observations(part_id,lease_fence,identity_digest,receipt,observed_at)
        SELECT part_id,lease_fence,identity_digest,?,? FROM weixin_dispatch_attempts
        WHERE part_id=? AND lease_fence=? AND lease_owner=? AND identity_digest=?
        ON CONFLICT(part_id,lease_fence) DO NOTHING""",
        (receipt, now, row['id'], claim['lease_fence'], claim['lease_owner'], identity))


def adopt_late(store, now):
    # Only observations authenticated against the original dispatch are used.
    # No observation changes channel authority, the delivery destination or wire.
    rows = store.all("""SELECT p.id,p.delivery_id,o.receipt FROM weixin_late_observations o
        JOIN weixin_dispatch_attempts a ON a.part_id=o.part_id AND a.lease_fence=o.lease_fence
            AND a.identity_digest=o.identity_digest
        JOIN weixin_parts p ON p.id=o.part_id
        WHERE p.state='unknown' AND a.lease_fence=(
            SELECT MAX(lease_fence) FROM weixin_dispatch_attempts WHERE part_id=p.id)
        ORDER BY o.observed_at,p.id LIMIT 100""")
    for row in rows:
        store.execute("""UPDATE weixin_parts SET state='accepted',provider_receipt=?,
            reason_code='provider_accepted_late',updated=? WHERE id=? AND state='unknown'""",
            (row['receipt'], now, row['id']))
    return {row['delivery_id'] for row in rows}
