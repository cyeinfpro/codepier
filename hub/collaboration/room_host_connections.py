"""Verified room-host identity, independent of any one room or route."""

from __future__ import annotations

import hmac
import secrets
import uuid

from hub.collaboration.common import digest, read_cursor, sign_cursor, validate
from hub.principal import refresh_principal
from shared import room_contracts as contracts
from shared.util import DevError


CONTROL_TTL = 30 * 86400
EXCHANGE_RECOVERY_TTL = 1800


def migrate(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(room_members)')}
    for name, definition in (
        ('host_connection_id', "TEXT NOT NULL DEFAULT ''"),
        ('route_generation', 'INTEGER NOT NULL DEFAULT 0'),
        ('connection_identity_version', 'INTEGER NOT NULL DEFAULT 1'),
    ):
        if name not in columns:
            db.execute(f'ALTER TABLE room_members ADD COLUMN {name} {definition}')
    db.execute('''CREATE TABLE IF NOT EXISTS room_host_connections (
        id TEXT PRIMARY KEY,space_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,
        grant_id TEXT NOT NULL,label TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'active'
        CHECK(state IN ('active','revoked')),host_revocation_epoch INTEGER NOT NULL DEFAULT 0
        CHECK(host_revocation_epoch>=0),authority_version INTEGER NOT NULL DEFAULT 1,
        health_version INTEGER NOT NULL DEFAULT 1,established_by_operation_id TEXT NOT NULL UNIQUE,
        established_by_member_id TEXT NOT NULL,established_by_generation INTEGER NOT NULL,
        verified_at REAL NOT NULL,revoked_at REAL,revoked_reason TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,updated_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_host_binding_adoptions (
        member_id TEXT NOT NULL,binding_generation INTEGER NOT NULL,
        host_connection_id TEXT NOT NULL REFERENCES room_host_connections(id),
        evidence_kind TEXT NOT NULL,evidence_id TEXT NOT NULL,created_at REAL NOT NULL,
        PRIMARY KEY(member_id,binding_generation))''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_host_attach_selections (
        member_id TEXT NOT NULL,binding_generation INTEGER NOT NULL,
        host_connection_id TEXT NOT NULL REFERENCES room_host_connections(id),
        host_revocation_epoch INTEGER NOT NULL,owner_operation_id TEXT NOT NULL,
        expires_at REAL NOT NULL,claimed_at REAL,
        PRIMARY KEY(member_id,binding_generation))''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_host_control_handles (
        id TEXT PRIMARY KEY,host_connection_id TEXT NOT NULL UNIQUE REFERENCES room_host_connections(id),
        host_revocation_epoch INTEGER NOT NULL,secret_digest TEXT NOT NULL,
        exchange_secret TEXT NOT NULL,exchange_digest TEXT NOT NULL,exchange_expires_at REAL NOT NULL,
        expires_at REAL NOT NULL,created_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_host_revoke_jobs (
        host_connection_id TEXT PRIMARY KEY REFERENCES room_host_connections(id),
        host_revocation_epoch INTEGER NOT NULL,operation_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','done')),after_member_id TEXT NOT NULL DEFAULT '',
        processed_count INTEGER NOT NULL DEFAULT 0,updated_at REAL NOT NULL)''')
    db.execute('''CREATE UNIQUE INDEX IF NOT EXISTS room_member_host_slot
        ON room_members(conversation_id,host_connection_id)
        WHERE host_connection_id!='' AND status!='removed' ''')
    db.execute('''CREATE INDEX IF NOT EXISTS room_members_by_host
        ON room_members(host_connection_id,id)''')
    db.execute('''CREATE INDEX IF NOT EXISTS room_hosts_by_owner
        ON room_host_connections(space_id,owner_user_id,created_at,id)''')
    db.execute('''CREATE INDEX IF NOT EXISTS room_host_attach_by_host
        ON room_host_attach_selections(host_connection_id,member_id,binding_generation)''')
    db.execute('''CREATE INDEX IF NOT EXISTS room_host_exchange_expiry
        ON room_host_control_handles(exchange_expires_at,id) WHERE exchange_secret!='' ''')


class RoomHostConnections:
    def __init__(self, connections):
        self.connections = connections
        self.c, self.store = connections.c, connections.store

    def record(self, host_id):
        return self.store.one('SELECT * FROM room_host_connections WHERE id=?', (host_id,))

    @staticmethod
    def require_active(host, expected_epoch=None):
        if host['state'] != 'active' or (expected_epoch is not None and
                host['host_revocation_epoch'] != expected_epoch):
            raise DevError('ROOM_CONNECTION_REVOKED', '这个 dot 的连接已撤销', 409)
        return host

    def require_host(self, member, host_id, *, grant_id=None, expected_epoch=None):
        room = self.connections.conversation(member)
        host = self.record(host_id)
        if (not host or host['space_id'] != room['space_id']
                or host['owner_user_id'] != room['owner_user_id']
                or host['grant_id'] != (grant_id or member['grant_id'])):
            raise DevError('ROOM_CONNECTION_IDENTITY_REQUIRED', '连接身份需要重新核对', 403)
        return self.require_active(host, expected_epoch)

    def identity(self, member):
        host_id = member.get('host_connection_id', '')
        if not host_id:
            if (member.get('connection_identity_version', 1) == 2 and member['probe_replied_at']
                    and not member.get('_pending_binding')):
                raise DevError('ROOM_CONNECTION_INVALID', '已验证成员缺少原连接身份', 409)
            return {'host_connection_id': '', 'host_revocation_epoch': 0}
        host = self.require_host(member, host_id)
        adopted = self.store.one('''SELECT 1 FROM room_host_binding_adoptions
            WHERE member_id=? AND binding_generation=? AND host_connection_id=?''',
            (member['id'], member['binding_generation'], host_id))
        if not adopted:
            raise DevError('ROOM_CONNECTION_INVALID', '没有此成员绑定的原连接证据', 409)
        return {'host_connection_id': host_id, 'host_revocation_epoch': host['host_revocation_epoch']}

    def selection(self, member):
        return self.store.one('''SELECT * FROM room_host_attach_selections
            WHERE member_id=? AND binding_generation=?''', (member['id'], member['binding_generation']))

    def select(self, owner, member, host_id, owner_request_key):
        host = self.owned(owner, host_id)[1]
        self.require_active(host)
        exists = self.store.one('''SELECT m.id FROM room_members m
            LEFT JOIN room_host_attach_selections a ON a.member_id=m.id AND a.binding_generation=m.binding_generation
            WHERE m.conversation_id=? AND m.status!='removed' AND (m.host_connection_id=?
                OR (a.host_connection_id=? AND (a.claimed_at IS NOT NULL OR a.expires_at>?)))''',
            (member['conversation_id'], host_id, host_id, self.c.clock()))
        if exists:
            raise DevError('ROOM_CONNECTION_ALREADY_PRESENT', '这个 dot 已在当前房间，请继续原成员',
                           409, member_id=exists['id'])
        self.store.execute('''INSERT INTO room_host_attach_selections
            (member_id,binding_generation,host_connection_id,host_revocation_epoch,owner_operation_id,expires_at)
            VALUES (?,?,?,?,?,?)''', (member['id'], member['binding_generation'], host_id,
            host['host_revocation_epoch'], digest([owner.user_id, member['id'], owner_request_key]),
            self.c.clock() + 1800))
        self.bump_authority(host_id)

    def control_digest(self, value):
        return hmac.digest(self.c.secret, b'room-host-binding-control:' + value.encode(), 'sha256').hex()

    def claim(self, member, principal, control_handle):
        selected = self.selection(member)
        if not selected:
            if control_handle:
                raise DevError('ROOM_CONNECTION_SELECTION_REQUIRED', '房主尚未为此成员选择复用连接', 409)
            return
        host = self.require_host(member, selected['host_connection_id'], grant_id=principal.grant_id,
                                 expected_epoch=selected['host_revocation_epoch'])
        if selected['expires_at'] <= self.c.clock() and selected['claimed_at'] is None:
            raise DevError('ROOM_CONNECTION_SELECTION_EXPIRED', '原复用授权已过期，请在房间重新选择', 409)
        control = self.store.one('SELECT * FROM room_host_control_handles WHERE host_connection_id=?',
                                 (host['id'],))
        if (not control_handle or not control or control['expires_at'] <= self.c.clock()
                or control['host_revocation_epoch'] != host['host_revocation_epoch']
                or not hmac.compare_digest(control['secret_digest'], self.control_digest(control_handle))):
            raise DevError('ROOM_CONNECTION_CONTROL_REQUIRED', '请由所选原 dot 使用其连接控制证明完成加入', 403)
        self.store.execute('''UPDATE room_host_attach_selections SET claimed_at=COALESCE(claimed_at,?)
            WHERE member_id=? AND binding_generation=?''',
            (self.c.clock(), member['id'], member['binding_generation']))

    def selected_current(self, member, principal):
        selected = self.selection(member)
        if selected:
            self.require_host(member, selected['host_connection_id'], grant_id=principal.grant_id,
                              expected_epoch=selected['host_revocation_epoch'])
        return selected

    @staticmethod
    def probe_digest(member, args):
        return digest([member['id'], member['binding_generation'], member['subscription_id'],
                       member['probe_nonce_hash'], args['idempotency_key']])

    def adopt_verified(self, member, args):
        self.store.require_transaction()
        if member.get('host_connection_id'):
            return self.identity(member)
        if member.get('connection_source_member_id'):
            # The cached legacy adapter remains strict until it can exchange
            # a real host control proof. Its endpoint never proves adoption.
            return self.identity(member)
        if not (member['probe_read_at'] and member['probe_replied_at'] and member['subscription_verified_at']
                and member['subscription_id'] and member['grant_id']):
            raise DevError('ROOM_PROBE_REQUIRED', '需要当前成员完成真实连接检查', 409)
        selected = self.selection(member)
        now, operation = self.c.clock(), self.probe_digest(member, args)
        if selected:
            if selected['claimed_at'] is None or selected['expires_at'] <= now:
                raise DevError('ROOM_CONNECTION_CONTROL_REQUIRED', '此房间复用连接尚未取得有效控制证明', 403)
            host = self.require_host(member, selected['host_connection_id'],
                                     expected_epoch=selected['host_revocation_epoch'])
            host_id = host['id']
            evidence_kind = 'control_handle_and_probe'
        else:
            room = self.connections.conversation(member)
            host_id = 'host_' + uuid.uuid4().hex
            self.store.execute('''INSERT INTO room_host_connections
                (id,space_id,owner_user_id,grant_id,label,established_by_operation_id,established_by_member_id,
                 established_by_generation,verified_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (host_id, room['space_id'], member['owner_user_id'], member['grant_id'], member['name'],
                 operation, member['id'], member['binding_generation'], now, now, now))
            token = 'cph_' + secrets.token_urlsafe(32)
            self.store.execute('''INSERT INTO room_host_control_handles
                (id,host_connection_id,host_revocation_epoch,secret_digest,exchange_secret,exchange_digest,
                 exchange_expires_at,expires_at,created_at) VALUES (?,?,0,?,?,?,?,?,?)''',
                (uuid.uuid4().hex, host_id, self.control_digest(token), self.store.encrypt(token),
                 operation, now + EXCHANGE_RECOVERY_TTL, now + CONTROL_TTL, now))
            evidence_kind = 'native_probe'
        self.store.execute('INSERT INTO room_host_binding_adoptions VALUES (?,?,?,?,?,?)',
            (member['id'], member['binding_generation'], host_id, evidence_kind, operation, now))
        self.store.execute('''UPDATE room_members SET host_connection_id=?,connection_identity_version=2,
            route_generation=1 WHERE id=? AND binding_generation=?''',
            (host_id, member['id'], member['binding_generation']))
        if selected:
            self.bump_authority(host_id)
        return self.identity(self.c.rooms.member(member['id']))

    def exchange(self, member, args):
        host_id = self.identity(member)['host_connection_id']
        if not host_id:
            return {}
        row = self.store.one('''SELECT exchange_secret FROM room_host_control_handles
            WHERE host_connection_id=? AND exchange_digest=? AND exchange_expires_at>?''',
            (host_id, self.probe_digest(member, args), self.c.clock()))
        return {'host_connection_id': host_id, **({'host_control_handle':
            self.store.decrypt(row['exchange_secret'])} if row and row['exchange_secret'] else {})}

    def bump_authority(self, host_id):
        self.store.execute('''UPDATE room_host_connections SET authority_version=authority_version+1,updated_at=?
            WHERE id=? AND state='active' ''', (self.c.clock(), host_id))

    def owned(self, principal, host_id):
        self.c.guard()
        principal = refresh_principal(self.store, principal)
        self.c.owner(principal)
        host = self.record(host_id)
        if not host or (host['owner_user_id'], host['space_id']) != (principal.user_id, principal.space_id):
            raise DevError('ROOM_CONNECTION_NOT_FOUND', '此账号没有这个连接', 404)
        return principal, host

    def members(self, host):
        return self.store.all('''SELECT m.id AS member_id,m.binding_generation,m.conversation_id,m.host_connection_id
            FROM room_members m LEFT JOIN room_host_attach_selections a
              ON a.member_id=m.id AND a.binding_generation=m.binding_generation
            WHERE m.status!='removed' AND (m.host_connection_id=? OR (a.host_connection_id=?
                AND (a.claimed_at IS NOT NULL OR a.expires_at>?)))
            ORDER BY m.id''', (host['id'], host['id'], self.c.clock()))

    def snapshot(self, host, members):
        return {'host_connection_id': host['id'], 'label': host['label'], 'state': host['state'],
                'authority_version': host['authority_version'], 'health_version': host['health_version'],
                'host_revocation_epoch': host['host_revocation_epoch'], 'verified_at': host['verified_at'],
                'room_count': len({member['conversation_id'] for member in members}),
                'connection_capabilities': ['host_reference', 'verified_reuse', 'host_revoke']}

    def listing(self, raw, principal):
        args = validate(contracts.RoomConnectionList, raw)
        self.c.guard()
        principal = refresh_principal(self.store, principal)
        self.c.owner(principal)
        cursor_scope = digest(['host-connections-v1', principal.space_id, principal.user_id])
        offset = read_cursor(self.c.secret, cursor_scope, args['cursor']) if args['cursor'] else 0
        rows = self.store.all('''SELECT * FROM room_host_connections WHERE space_id=? AND owner_user_id=?
            ORDER BY created_at,id LIMIT ? OFFSET ?''',
            (principal.space_id, principal.user_id, args['limit'] + 1, offset))
        return {'items': [self.snapshot(host, self.members(host)) for host in rows[:args['limit']]],
                'next_cursor': sign_cursor(self.c.secret, cursor_scope, offset + args['limit'])
                if len(rows) > args['limit'] else None}

    def view(self, raw, principal):
        args = validate(contracts.RoomConnectionRead, raw)
        _, host = self.owned(principal, args['host_connection_id'])
        return self.snapshot(host, self.members(host))

    def impact_digest(self, host, members):
        return digest([host['id'], host['host_revocation_epoch'], members])

    def impact(self, raw, principal):
        args = validate(contracts.RoomConnectionImpact, raw)
        with self.store.transaction(immediate=False):
            _, host = self.owned(principal, args['host_connection_id'])
            members = self.members(host)
            impact = self.impact_digest(host, members)
            cursor_scope = digest(['host-impact-v1', host['id'], impact])
            offset = read_cursor(self.c.secret, cursor_scope, args['cursor']) if args['cursor'] else 0
            end = offset + args['limit']
            return {**self.snapshot(host, members), 'impact_digest': impact, 'members': members[offset:end],
                    'next_cursor': sign_cursor(self.c.secret, cursor_scope, end) if len(members) > end else None}

    def revoke(self, raw, principal):
        args = validate(contracts.RoomConnectionRevoke, raw)
        with self.store.transaction():
            owner, host = self.owned(principal, args['host_connection_id'])

            def perform():
                self.require_active(host)
                members = self.members(host)
                if (host['authority_version'] != args['expected_authority_version']
                        or self.impact_digest(host, members) != args['impact_digest']):
                    raise DevError('ROOM_CONNECTION_IMPACT_CHANGED', '受影响的连接已变化，请重新核对', 409)
                now = self.c.clock()
                changed = self.store.execute('''UPDATE room_host_connections SET state='revoked',
                    host_revocation_epoch=host_revocation_epoch+1,authority_version=authority_version+1,
                    revoked_at=?,revoked_reason='owner_revoked',updated_at=?
                    WHERE id=? AND state='active' AND authority_version=?''',
                    (now, now, host['id'], host['authority_version']))
                if changed.rowcount != 1:
                    raise DevError('ROOM_CONNECTION_IMPACT_CHANGED', '连接状态已变化，请重新核对', 409)
                current = self.record(host['id'])
                operation_id = digest([owner.space_id, owner.user_id, host['id'], args['idempotency_key']])
                self.store.execute('''INSERT INTO room_host_revoke_jobs
                    (host_connection_id,host_revocation_epoch,operation_id,state,updated_at)
                    VALUES (?,?,?,'pending',?)''',
                    (host['id'], current['host_revocation_epoch'], operation_id, now))
                self.store.execute("UPDATE room_host_control_handles SET exchange_secret='' WHERE host_connection_id=?",
                                   (host['id'],))
                self.store.audit(owner.actor, 'room_host.revoked', host['id'], 'ok',
                    {'operation_id': operation_id, 'epoch': current['host_revocation_epoch'], 'member_count': len(members)})
                return {**self.snapshot(current, members), 'operation_id': operation_id, 'cleanup_queued': True}

            return self.c.rooms.mutation(owner, 'room_connection_revoke', args, perform)

    def cleanup(self):
        """Stop one bounded batch; the terminal gate already stopped new effects."""
        with self.store.transaction():
            self.store.execute('''UPDATE room_host_control_handles SET exchange_secret='' WHERE id IN (
                SELECT id FROM room_host_control_handles WHERE exchange_secret!='' AND exchange_expires_at<=?
                ORDER BY exchange_expires_at,id LIMIT 16)''', (self.c.clock(),))
            job = self.store.one("SELECT * FROM room_host_revoke_jobs WHERE state='pending' ORDER BY updated_at,host_connection_id")
            if not job:
                return 0
            host = self.record(job['host_connection_id'])
            if host['state'] != 'revoked' or host['host_revocation_epoch'] != job['host_revocation_epoch']:
                raise RuntimeError('Revocation job identity no longer matches the terminal host')
            members = self.store.all('''SELECT m.* FROM room_members m JOIN (
                SELECT id FROM room_members WHERE host_connection_id=? AND id>?
                UNION
                SELECT a.member_id FROM room_host_attach_selections a JOIN room_members current
                  ON current.id=a.member_id AND current.binding_generation=a.binding_generation
                WHERE a.host_connection_id=? AND a.member_id>? AND current.host_connection_id=''
                ) affected ON affected.id=m.id ORDER BY m.id LIMIT 8''',
                (host['id'], job['after_member_id'], host['id'], job['after_member_id']))
            now = self.c.clock()
            for member in members:
                self.c.rooms.stop_member_work(member, 'ROOM_CONNECTION_REVOKED', revoke=True)
                self.c.rooms.renewals.stop(member['subscription_id'], 'host_revoked')
                self.c.rooms.change(member['conversation_id'], 'connection', member['id'], member['version'])
            self.store.execute('''UPDATE room_host_revoke_jobs SET state=?,after_member_id=?,
                processed_count=processed_count+?,updated_at=? WHERE host_connection_id=?''',
                ('pending' if len(members) == 8 else 'done', members[-1]['id'] if members else job['after_member_id'],
                 len(members), now, host['id']))
            return len(members)
