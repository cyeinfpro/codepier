"""Verification-only replacement bindings, separate from the live member.

Preparing a different host must not revoke a working route. Only the verified
cutover transaction changes the member generation or expires its consumer lease.
"""
from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import asdict

from hub.collaboration.common import canonical, digest
from hub.principal import Principal, refresh_principal
from shared.util import DevError


ACTIVE_STATES = ('invited', 'bound', 'verifying')
BINDING_FIELDS = (
    'binding_generation', 'grant_id', 'principal', 'connection_proof_hash',
    'connection_proof_secret', 'subscription_id', 'subscription_verified_at',
    'endpoint_digest', 'credential_digest', 'probe_nonce_hash', 'probe_nonce_secret',
    'probe_created_at', 'probe_read_at', 'probe_replied_at',
)


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS room_pending_bindings (
        id TEXT PRIMARY KEY,member_id TEXT NOT NULL REFERENCES room_members(id),
        base_generation INTEGER NOT NULL,binding_generation INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'invited',version INTEGER NOT NULL DEFAULT 1,
        owner_principal TEXT NOT NULL,prepare_expires_at REAL NOT NULL,
        invite_digest TEXT NOT NULL UNIQUE,invite_secret TEXT NOT NULL,
        expires_at REAL NOT NULL,redeemed_at REAL,revoked_at REAL,
        join_request_key TEXT NOT NULL DEFAULT '',join_request_digest TEXT NOT NULL DEFAULT '',
        grant_id TEXT NOT NULL DEFAULT '',principal TEXT NOT NULL DEFAULT '{}',
        connection_proof_hash TEXT NOT NULL DEFAULT '',connection_proof_secret TEXT NOT NULL DEFAULT '',
        subscription_id TEXT NOT NULL DEFAULT '',subscription_verified_at REAL,
        endpoint_digest TEXT NOT NULL DEFAULT '',credential_digest TEXT NOT NULL DEFAULT '',
        probe_nonce_hash TEXT NOT NULL DEFAULT '',probe_nonce_secret TEXT NOT NULL DEFAULT '',
        probe_created_at REAL,probe_read_at REAL,probe_replied_at REAL,
        source_dirty_revision INTEGER NOT NULL,source_drained_revision INTEGER NOT NULL,
        source_inbox_sequence INTEGER NOT NULL,activated_at REAL,
        created_at REAL NOT NULL,updated_at REAL NOT NULL,
        UNIQUE(member_id,binding_generation),CHECK(binding_generation>base_generation))''')
    db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS room_pending_binding_active
        ON room_pending_bindings(member_id) WHERE status IN ('invited','bound','verifying')''')


class PendingBindings:
    def __init__(self, rooms):
        self.rooms, self.c, self.store = rooms, rooms.c, rooms.store

    def current(self, member_id):
        return self.store.one('''SELECT * FROM room_pending_bindings WHERE member_id=?
            AND status IN ('invited','bound','verifying')''', (member_id,))

    def by_generation(self, member, generation):
        candidate = self.store.one('''SELECT * FROM room_pending_bindings WHERE member_id=?
            AND binding_generation=? AND status IN ('invited','bound','verifying')''',
            (member['id'], generation))
        if (not candidate or candidate['base_generation'] != member['binding_generation']
                or candidate['prepare_expires_at'] <= self.c.clock()):
            raise DevError('ROOM_BINDING_CHANGED', '候选连接已失效，请继续当前成员连接', 409)
        return self.facade(member, candidate)

    @staticmethod
    def facade(member, candidate):
        return {**member, **{key: candidate[key] for key in BINDING_FIELDS},
            'status': 'invited' if candidate['status'] == 'invited' else 'bound',
            '_pending_binding': True, '_pending_binding_id': candidate['id'],
            '_base_generation': candidate['base_generation'],
            '_prepare_expires_at': candidate['prepare_expires_at'],
            'connection_source_member_id': '', 'connection_source_generation': 0}

    def cancel(self, member_id):
        candidate = self.current(member_id)
        if candidate:
            self.store.execute("UPDATE mcp_event_subscriptions SET state='unsubscribed',version=version+1 WHERE id=?",
                (candidate['subscription_id'],))
            self.store.execute("UPDATE room_pending_bindings SET status='cancelled',revoked_at=?,version=version+1,updated_at=? WHERE id=?",
                (self.c.clock(), self.c.clock(), candidate['id']))

    def prepare(self, member, owner):
        self.cancel(member['id'])
        maximum = self.store.one('SELECT COALESCE(MAX(binding_generation),0) n FROM room_pending_bindings WHERE member_id=?',
            (member['id'],))['n']
        generation = max(maximum, member['binding_generation']) + 1
        owner_snapshot = asdict(owner)
        owner_snapshot.update(scopes=[], session_hash='', token_hash='')
        now, code = self.c.clock(), 'CPR-' + secrets.token_urlsafe(32)
        sequence = self.store.one('SELECT COALESCE(MAX(sequence),0) n FROM dot_mailbox_entries WHERE member_id=?',
            (member['id'],))['n']
        self.store.execute('''INSERT INTO room_pending_bindings
            (id,member_id,base_generation,binding_generation,owner_principal,prepare_expires_at,
             invite_digest,invite_secret,expires_at,source_dirty_revision,source_drained_revision,
             source_inbox_sequence,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (uuid.uuid4().hex, member['id'], member['binding_generation'], generation, canonical(owner_snapshot),
             now + 3600, digest(code), self.store.encrypt(code), now + 1800,
             member['dirty_revision'], member['drained_revision'], sequence, now, now))
        self.store.execute("UPDATE room_connect_flows SET status='invited',last_error_code='',version=version+1,updated_at=? WHERE member_id=?",
            (now, member['id']))
        return self.current(member['id'])

    def refresh(self, member):
        candidate = self.current(member['id'])
        if not candidate:
            return False
        if candidate['redeemed_at']:
            raise DevError('INVITE_ALREADY_REDEEMED', '候选成员已登记，请继续验证或重新更换绑定', 409)
        now, code = self.c.clock(), 'CPR-' + secrets.token_urlsafe(32)
        self.store.execute('''UPDATE room_pending_bindings SET invite_digest=?,invite_secret=?,
            expires_at=?,prepare_expires_at=?,version=version+1,updated_at=? WHERE id=?''',
            (digest(code), self.store.encrypt(code), now + 1800, now + 3600, now, candidate['id']))
        self.store.execute('UPDATE room_connect_flows SET version=version+1,updated_at=? WHERE member_id=?', (now, member['id']))
        return True

    def join(self, candidate, member, principal, args):
        if candidate['status'] == 'activated':
            if member['binding_generation'] != candidate['binding_generation']:
                raise DevError('ROOM_BINDING_CHANGED', '该加入说明不再属于当前连接', 409)
            binding = member
        elif candidate['status'] not in ACTIVE_STATES or candidate['revoked_at']:
            raise DevError('ROOM_INVITE_INVALID', '该候选加入说明已失效', 403)
        else:
            binding = self.by_generation(member, candidate['binding_generation'])
        fingerprint = digest([principal.grant_id, args['idempotency_key'], candidate['invite_digest']])
        if candidate['redeemed_at']:
            if binding['grant_id'] != principal.grant_id or candidate['join_request_digest'] != fingerprint:
                raise DevError('ROOM_INVITE_REDEEMED', '该加入说明已由原候选连接兑换', 409)
            return binding
        if candidate['expires_at'] <= self.c.clock():
            raise DevError('ROOM_INVITE_EXPIRED', '加入说明已过期，请在原成员中刷新', 410)
        proof = secrets.token_urlsafe(32)
        saved = asdict(principal)
        saved.update(scopes=[], session_hash='', token_hash='')
        self.store.execute('''UPDATE room_pending_bindings SET status='bound',grant_id=?,principal=?,
            connection_proof_hash=?,connection_proof_secret=?,redeemed_at=?,join_request_key=?,
            join_request_digest=?,version=version+1,updated_at=? WHERE id=?''',
            (principal.grant_id, canonical(saved), digest(proof), self.store.encrypt(proof), self.c.clock(),
             args['idempotency_key'], fingerprint, self.c.clock(), candidate['id']))
        self.store.execute("UPDATE room_connect_flows SET status='waiting_host',version=version+1,updated_at=? WHERE member_id=?",
            (self.c.clock(), member['id']))
        self.rooms.change(member['conversation_id'], 'connection', member['id'], member['version'])
        return self.by_generation(member, candidate['binding_generation'])

    def activate(self, candidate_member, principal):
        """Called after the genuine ticket/nonce round trip in the same transaction."""
        member = self.rooms.member(candidate_member['id'])
        candidate = self.current(member['id'])
        if (not candidate or candidate['id'] != candidate_member['_pending_binding_id']
                or candidate['base_generation'] != member['binding_generation']):
            raise DevError('ROOM_BINDING_CHANGED', '原连接已改变，不能切换过期候选', 409)
        saved = json.loads(candidate['owner_principal'])
        owner = refresh_principal(self.store, Principal(**{**saved, 'scopes': set()}))
        self.rooms.owner(owner, member['conversation_id'])
        # All destructive effects are deferred to this transaction. If current
        # authority or bridge setup fails, SQLite restores the original route.
        self.rooms.stop_member_work(member, 'ROOM_MEMBER_REBOUND', revoke=True)
        now = self.c.clock()
        fields = {key: candidate_member[key] for key in BINDING_FIELDS}
        fields['probe_replied_at'] = now
        fields.update(connection_source_member_id='', connection_source_generation=0)
        assignment = ','.join(key + '=?' for key in fields)
        changed = self.store.execute('UPDATE room_members SET ' + assignment +
            ",status='bound',version=version+1,updated_at=? WHERE id=? AND binding_generation=?",
            (*fields.values(), now, member['id'], candidate['base_generation']))
        if changed.rowcount != 1:
            raise DevError('ROOM_BINDING_CHANGED', '当前成员已更换连接，请重新读取', 409)
        self.store.execute("UPDATE room_pending_bindings SET status='activated',probe_replied_at=?,activated_at=?,version=version+1,updated_at=? WHERE id=?",
            (now, now, now, candidate['id']))
        self.store.execute("""UPDATE room_connect_flows SET status='ready',invite_digest='',invite_secret='',
            redeemed_at=?,expires_at=NULL,version=version+1,updated_at=? WHERE member_id=?""", (now, now, member['id']))
        current = self.rooms.member(member['id'])
        bridge = getattr(self.c, 'room_work_bridge', None)
        if bridge is not None:
            bridge.ensure(current, owner)
            current = self.rooms.member(member['id'])
            if current['legacy_dot_id']:
                bridge.bind(current, principal)
        consumers = getattr(self.c, 'room_consumers', None)
        if consumers is not None:
            consumers.binding_activated(self.rooms.member(member['id']))
        self.rooms.change(member['conversation_id'], 'member', member['id'], member['version'] + 1)
        return self.rooms.member(member['id'])
