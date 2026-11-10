"""Bounded, restartable adoption of previously verified room bindings."""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
import uuid
from collections import Counter
from dataclasses import dataclass

from hub.collaboration.common import ROOM_INBOX_EVENT, digest

LOG = logging.getLogger('codepier.collaboration.host_connections')
MAX_LINEAGE = 32
MAX_BATCH = 128


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS room_host_backfill_progress (
        id INTEGER PRIMARY KEY CHECK (id=1), after_member_id TEXT NOT NULL DEFAULT '',
        scanned_count INTEGER NOT NULL DEFAULT 0, completed INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS room_host_backfill_results (
        member_id TEXT NOT NULL, binding_generation INTEGER NOT NULL,
        outcome TEXT NOT NULL, reason TEXT NOT NULL, evaluated_at REAL NOT NULL,
        PRIMARY KEY (member_id, binding_generation)
    )""")


def record_shadow(legacy_error, host_error):
    """Use a fixed field and bounded reason values, without identities or secrets."""
    legacy_code = getattr(legacy_error, 'code', '')
    host_code = getattr(host_error, 'code', '')
    source = getattr(legacy_error, 'details', {}).get('source_reason')
    unavailable = source if source in {'source_archived', 'source_paused', 'source_removed'} else 'source_unavailable'
    if legacy_error is None and host_error is None:
        return
    reason = (
        'host_revoked' if host_code == 'ROOM_CONNECTION_REVOKED' else
        'identity_mismatch' if host_error is not None else
        'source_generation_changed' if legacy_code == 'ROOM_CONNECTION_CHANGED' else
        unavailable if legacy_code == 'ROOM_CONNECTION_UNAVAILABLE' else
        'identity_mismatch' if legacy_code in {'ROOM_CONNECTION_DENIED', 'ROOM_CONNECTION_GRANT_CHANGED',
                                               'ROOM_CONNECTION_IDENTITY_REQUIRED'} else
        'source_invalid'
    )
    LOG.info('room_host_shadow reason=%s', reason)


@dataclass(frozen=True)
class Decision:
    outcome: str
    reason: str
    host: dict | None = None


class HostBindingBackfill:
    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock

    def _member(self, identifier):
        return self.store.one("""SELECT m.id,m.conversation_id,m.owner_user_id,m.name,m.status,
            m.binding_generation,m.grant_id,m.subscription_id,m.subscription_verified_at,
            m.probe_created_at,m.probe_read_at,m.probe_replied_at,
            m.connection_proof_hash!='' AS has_proof,m.probe_nonce_hash!='' AS has_nonce,
            m.connection_source_member_id,m.connection_source_generation,
            m.host_connection_id,m.connection_identity_version,r.space_id,r.owner_user_id AS room_owner
            FROM room_members m JOIN conversation_rooms r ON r.id=m.conversation_id WHERE m.id=?""",
            (identifier,))

    def _existing(self, member):
        return self.store.one("""SELECT h.* FROM room_host_binding_adoptions a
            JOIN room_host_connections h ON h.id=a.host_connection_id
            WHERE a.member_id=? AND a.binding_generation=?""",
            (member['id'], member['binding_generation']))

    def _round_trip(self, member):
        times = [member[k] for k in ('probe_created_at', 'probe_read_at', 'probe_replied_at')]
        if (not member['has_proof'] or not member['has_nonce']
                or not isinstance(member['subscription_verified_at'], (int, float))
                or not all(isinstance(t, (int, float)) and math.isfinite(t) and t > 0 for t in times)
                or times != sorted(times) or times[-1] > self.clock()):
            return False
        sub = self.store.one("""SELECT name,arguments,state,grant_id,conversation_id
            FROM mcp_event_subscriptions WHERE id=?""", (member['subscription_id'],))
        if (not sub or sub['name'] != ROOM_INBOX_EVENT or sub['state'] not in {'active', 'paused', 'expired'}
                or sub['grant_id'] != member['grant_id'] or sub['conversation_id'] != member['conversation_id']):
            return False
        try:
            return json.loads(sub['arguments']) == {
                'owner_user_id': member['owner_user_id'], 'conversation_id': member['conversation_id'],
                'member_id': member['id'], 'binding_generation': member['binding_generation'],
            }
        except (TypeError, ValueError):
            return False

    def _lineage_reason(self, member):
        """A historical link helps classify recovery. It cannot prove shared control."""
        seen = {member['id']}
        current = member
        while current['connection_source_member_id']:
            source_id = current['connection_source_member_id']
            if source_id in seen or len(seen) >= MAX_LINEAGE:
                return 'source_cycle_or_depth'
            source = self._member(source_id)
            if source is None or current['connection_source_generation'] <= 0:
                return 'source_evidence_missing'
            if (source['owner_user_id'] != member['owner_user_id']
                    or source['room_owner'] != member['owner_user_id']
                    or source['space_id'] != member['space_id']):
                return 'source_scope_mismatch'
            if source['binding_generation'] != current['connection_source_generation']:
                return 'source_generation_changed'
            if source['grant_id'] != member['grant_id']:
                return 'source_scope_mismatch'
            seen.add(source_id)
            current = source
        return 'legacy_control_unverified' if current is not member else 'independent_round_trip'

    def classify(self, member):
        host = self._existing(member)
        if host and (host['owner_user_id'] != member['owner_user_id']
                or host['space_id'] != member['space_id'] or host['grant_id'] != member['grant_id']):
            return Decision('pending', 'adoption_identity_mismatch')
        if host and host['state'] == 'revoked':
            return Decision('preserved', 'host_revoked', host)
        if member['host_connection_id'] and (not host or host['id'] != member['host_connection_id']):
            return Decision('pending', 'adoption_identity_mismatch')
        if member['connection_identity_version'] != 1:
            return Decision('preserved', 'already_current') if host else Decision('pending', 'unsupported_identity_version')
        if member['status'] not in {'bound', 'paused'}:
            return Decision('pending', 'binding_not_verified')
        grant = self.store.one("""SELECT g.user_id,g.space_id,g.revoked,x.expires FROM grants g
            LEFT JOIN grant_expirations x ON x.grant_id=g.id WHERE g.id=?""", (member['grant_id'],))
        if (not grant or grant['revoked'] or (grant['expires'] is not None and grant['expires'] <= self.clock())
                or grant['user_id'] != member['owner_user_id'] or member['room_owner'] != member['owner_user_id']
                or grant['space_id'] != member['space_id']):
            return Decision('pending', 'grant_not_current')
        if not self._round_trip(member):
            return Decision('pending', 'round_trip_unverified')
        if host:
            return Decision('adopted', 'existing_adoption', host)
        reason = self._lineage_reason(member)
        if reason in {'source_cycle_or_depth', 'source_scope_mismatch'}:
            return Decision('pending', reason)
        return Decision('adopted', reason)

    def _adopt(self, member, decision):
        now = self.clock()
        host_id, epoch = (decision.host['id'], decision.host['host_revocation_epoch']) if decision.host else (
            'host_' + uuid.uuid4().hex, 0)
        if not decision.host:
            evidence = digest(['room-host-backfill', member['id'], member['binding_generation'], member['subscription_id']])
            self.store.execute("""INSERT INTO room_host_connections
                (id,space_id,owner_user_id,grant_id,label,established_by_operation_id,established_by_member_id,
                 established_by_generation,verified_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (host_id, member['space_id'], member['owner_user_id'], member['grant_id'], member['name'],
                 'backfill_' + evidence, member['id'], member['binding_generation'], member['probe_replied_at'], now, now))
            self.store.execute("""INSERT INTO room_host_binding_adoptions
                (member_id,binding_generation,host_connection_id,evidence_kind,evidence_id,created_at)
                VALUES (?,?,?,'legacy_round_trip',?,?)""",
                (member['id'], member['binding_generation'], host_id, evidence, now))
        changed = self.store.execute("""UPDATE room_members SET host_connection_id=?,connection_identity_version=2
            WHERE id=? AND binding_generation=? AND connection_identity_version=1""",
            (host_id, member['id'], member['binding_generation']))
        if changed.rowcount != 1:
            raise sqlite3.IntegrityError('Binding changed during adoption')
        self.store.execute("""UPDATE dot_consumer_sessions SET host_connection_id=?,host_revocation_epoch=?
            WHERE member_id=? AND generation=? AND grant_id=? AND subscription_id=?
            AND host_connection_id='' AND state='active'""",
            (host_id, epoch, member['id'], 'room:{}:{}'.format(member['id'], member['binding_generation']),
             member['grant_id'], member['subscription_id']))

    def batch(self, *, apply=False, after='', limit=64, restart=False, max_seconds=0.3):
        if not 1 <= limit <= MAX_BATCH:
            raise ValueError(f'Backfill batch size must be 1–{MAX_BATCH}')
        if not apply:
            return self._batch(apply=False, after=after, limit=limit, max_seconds=max_seconds)
        if after:
            raise ValueError('Apply resumes from its saved checkpoint')
        with self.store.transaction():
            checkpoint = self.store.one('SELECT * FROM room_host_backfill_progress WHERE id=1')
            scanned = checkpoint['scanned_count'] if checkpoint and not restart else 0
            after = checkpoint['after_member_id'] if checkpoint and not restart else ''
            if checkpoint and checkpoint['completed'] and not restart:
                return {'items': [], 'counts': {}, 'next_after': None, 'scanned_count': scanned, 'applied': True}
            report = self._batch(apply=True, after=after, limit=limit, max_seconds=max_seconds)
            scanned += len(report['items'])
            self.store.execute("""INSERT INTO room_host_backfill_progress VALUES (1,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET after_member_id=excluded.after_member_id,
                scanned_count=excluded.scanned_count,completed=excluded.completed,updated_at=excluded.updated_at""",
                (report['items'][-1]['member_id'] if report['items'] else after, scanned,
                 int(report['next_after'] is None), self.clock()))
            return {**report, 'scanned_count': scanned}

    def _batch(self, *, apply, after, limit, max_seconds):
        deadline = time.monotonic() + max_seconds
        ids = self.store.all("""SELECT id FROM room_members WHERE id>? AND connection_identity_version=1
            ORDER BY id LIMIT ?""", (after, limit + 1))
        items = []
        for row in ids[:limit]:
            if items and time.monotonic() >= deadline:
                break
            member = self._member(row['id'])
            if member is None:
                continue
            decision = self.classify(member)
            if apply:
                if decision.outcome == 'adopted':
                    try:
                        with self.store.savepoint():
                            self._adopt(member, decision)
                    except sqlite3.IntegrityError:
                        decision = Decision('error', 'adoption_conflict')
                self.store.execute("""INSERT INTO room_host_backfill_results VALUES (?,?,?,?,?)
                    ON CONFLICT(member_id,binding_generation) DO UPDATE SET outcome=excluded.outcome,
                    reason=excluded.reason,evaluated_at=excluded.evaluated_at""",
                    (member['id'], member['binding_generation'], decision.outcome, decision.reason, self.clock()))
            items.append({'member_id': member['id'], 'binding_generation': member['binding_generation'],
                          'outcome': decision.outcome, 'reason': decision.reason})
        next_after = items[-1]['member_id'] if items and len(ids) > len(items) else None
        return {'items': items, 'counts': dict(Counter(item['reason'] for item in items)),
                'next_after': next_after, 'applied': apply}
