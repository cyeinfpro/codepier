"""Independent, durable room host identities and owner revocation."""

from __future__ import annotations

import hashlib
import json
import uuid

from hub.collaboration.common import canonical, digest, read_cursor, sign_cursor, validate
from hub.principal import refresh_principal
from shared.room_contracts import HostConnectionImpact, HostConnectionList, HostConnectionRead, HostConnectionRevoke
from shared.util import DevError



def migrate(db):
    schema = """
    CREATE TABLE IF NOT EXISTS room_host_connections (
        id TEXT PRIMARY KEY,space_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,grant_id TEXT NOT NULL,
        label TEXT NOT NULL DEFAULT '',state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','revoked')),
        host_revocation_epoch INTEGER NOT NULL DEFAULT 0 CHECK(host_revocation_epoch>=0),
        authority_version INTEGER NOT NULL DEFAULT 1,health_version INTEGER NOT NULL DEFAULT 1,
        established_by_operation_id TEXT NOT NULL UNIQUE,established_by_member_id TEXT NOT NULL,
        established_by_generation INTEGER NOT NULL,verified_at REAL NOT NULL,revoked_at REAL,
        revoked_reason TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,updated_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS room_host_owner
        ON room_host_connections(space_id,owner_user_id,state,created_at,id);
    CREATE TABLE IF NOT EXISTS room_host_binding_adoptions (
        member_id TEXT NOT NULL,binding_generation INTEGER NOT NULL,
        host_connection_id TEXT NOT NULL REFERENCES room_host_connections(id),
        evidence_kind TEXT NOT NULL,evidence_id TEXT NOT NULL,created_at REAL NOT NULL,
        PRIMARY KEY(member_id,binding_generation)
    );
    CREATE TABLE IF NOT EXISTS room_host_revoke_jobs (
        host_connection_id TEXT NOT NULL REFERENCES room_host_connections(id),
        host_revocation_epoch INTEGER NOT NULL,operation_id TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('pending','running','done')),
        after_member_id TEXT NOT NULL DEFAULT '',after_candidate_id TEXT NOT NULL DEFAULT '',
        after_route_id TEXT NOT NULL DEFAULT '',
        processed_count INTEGER NOT NULL DEFAULT 0,version INTEGER NOT NULL DEFAULT 1,
        updated_at REAL NOT NULL,PRIMARY KEY(host_connection_id,host_revocation_epoch)
    );
    CREATE INDEX IF NOT EXISTS room_host_revoke_pending ON room_host_revoke_jobs(state,updated_at,host_connection_id);
    """
    for statement in schema.split(';'):
        if statement.strip():
            db.execute(statement)
    for table in ('room_members', 'room_pending_bindings'):
        columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        for name, declaration in (
            ('host_connection_id', "TEXT NOT NULL DEFAULT ''"),
            ('host_revocation_epoch', 'INTEGER NOT NULL DEFAULT 0'),
            ('route_generation', 'INTEGER NOT NULL DEFAULT 0'),
            ('connection_identity_version', 'INTEGER NOT NULL DEFAULT 1'),
            ('host_control_evidence_id', "TEXT NOT NULL DEFAULT ''"),
            ('host_control_verified_at', 'REAL'),
        ):
            if name not in columns:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS room_member_live_host
        ON room_members(conversation_id,host_connection_id)
        WHERE host_connection_id!='' AND status!='removed'""")
    db.execute("""CREATE INDEX IF NOT EXISTS room_member_host_lookup
        ON room_members(host_connection_id,id) WHERE host_connection_id!=''""")
    db.execute("""CREATE INDEX IF NOT EXISTS room_pending_host_lookup
        ON room_pending_bindings(host_connection_id,id) WHERE host_connection_id!=''""")
    db.execute("""CREATE INDEX IF NOT EXISTS room_host_revoked_delivery_leases
        ON mcp_event_deliveries(lease_until,id) WHERE state='leased' AND reason_code='host_revoked_in_flight'""")


class HostConnections:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def read_host(self, host_id):
        return self.store.one('SELECT * FROM room_host_connections WHERE id=?', (host_id,))

    def owner(self, principal):
        principal = refresh_principal(self.store, principal)
        self.c.guard()
        self.c.owner(principal)
        return principal

    def owned_host(self, principal, host_id):
        principal = self.owner(principal)
        host = self.read_host(host_id)
        if not host or host['space_id'] != principal.space_id or host['owner_user_id'] != principal.user_id:
            raise DevError('ROOM_CONNECTION_NOT_FOUND', '未找到此工作区的连接', 404)
        return principal, host

    @staticmethod
    def assert_current(host, expected_epoch=None):
        if host['state'] != 'active' or (expected_epoch is not None and
                host['host_revocation_epoch'] != expected_epoch):
            raise DevError('ROOM_CONNECTION_REVOKED', '这个 dot 的连接已撤销', 409)

    def require_member(self, room, member, *, expected_epoch=None):
        host_id = member.get('host_connection_id')
        if not host_id:
            raise DevError('ROOM_CONNECTION_INVALID', '已激活成员缺少连接身份', 409)
        host = self.read_host(host_id)
        if (not host or host['space_id'] != room['space_id'] or host['owner_user_id'] != room['owner_user_id']
                or host['owner_user_id'] != member['owner_user_id'] or host['grant_id'] != member['grant_id']):
            raise DevError('ROOM_CONNECTION_IDENTITY_REQUIRED', '连接身份需要重新核对', 403)
        self.assert_current(host, expected_epoch)
        return host

    def require_host_current(self, principal, room, member, *, expected_epoch=None):
        host = self.require_member(room, member, expected_epoch=expected_epoch)
        if (principal.user_id != host['owner_user_id'] or principal.space_id != host['space_id']
                or principal.grant_id != host['grant_id']):
            raise DevError('ROOM_CONNECTION_IDENTITY_REQUIRED', '连接身份需要重新核对', 403)
        return host

    def touch_authority(self, host_id):
        if host_id:
            self.store.execute("""UPDATE room_host_connections SET authority_version=authority_version+1,
                updated_at=? WHERE id=? AND state='active'""", (self.c.clock(), host_id))

    def touch_health(self, host_id):
        if host_id:
            self.store.execute("""UPDATE room_host_connections SET health_version=health_version+1,
                updated_at=? WHERE id=? AND state='active'""", (self.c.clock(), host_id))

    def register_verified(self, principal, room, member, *, verified_at, evidence_id):
        """Adopt exactly one identity per verified generation in the caller's activation transaction."""
        member_id, generation = member['id'], member['binding_generation']
        adoption = self.store.one('''SELECT * FROM room_host_binding_adoptions
            WHERE member_id=? AND binding_generation=?''', (member_id, generation))
        selected = member.get('host_connection_id', '')
        if adoption:
            host_id = adoption['host_connection_id']
            if selected and selected != host_id:
                raise DevError('ROOM_CONNECTION_INVALID', '成员已登记到另一个连接', 409)
            return self.require_host_current(principal, room, {**member, 'host_connection_id': host_id})
        if not member['subscription_id'] or not member['subscription_verified_at'] or not evidence_id:
            raise DevError('ROOM_CONNECTION_INVALID', '缺少当前订阅的验证证据', 409)
        if selected:
            host = self.require_host_current(principal, room, member, expected_epoch=member['host_revocation_epoch'])
            if not member['host_control_evidence_id'] or member['host_control_verified_at'] is None:
                raise DevError('ROOM_CONNECTION_CONTROL_REQUIRED', '请先验证原 dot 对连接的控制', 409)
            host_id = host['id']
            evidence_kind = 'verified_reuse'
        else:
            if member.get('connection_identity_version', 1) != 2:
                raise DevError('ROOM_CONNECTION_INVALID', '旧连接须通过迁移保留其绑定证明', 409)
            host_id = 'host_' + uuid.uuid4().hex
            evidence_kind = 'verified_probe'
            if member.get('_pending_binding'):
                operation_id = member['_pending_binding_id']
            else:
                flow = self.store.one('SELECT id FROM room_connect_flows WHERE member_id=?', (member_id,))
                if not flow:
                    raise DevError('ROOM_CONNECTION_INVALID', '缺少原接入流程', 409)
                operation_id = flow['id']
            if self.store.one('SELECT 1 FROM room_host_connections WHERE established_by_operation_id=?', (operation_id,)):
                raise DevError('ROOM_CONNECTION_INVALID', '原接入流程的连接认领记录不完整', 409)
            self.store.execute('''INSERT INTO room_host_connections
                (id,space_id,owner_user_id,grant_id,label,established_by_operation_id,
                 established_by_member_id,established_by_generation,verified_at,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
                (host_id, room['space_id'], room['owner_user_id'], principal.grant_id, member['name'],
                 operation_id, member_id, generation, verified_at, verified_at, verified_at))
            host = self.require_host_current(principal, room, {**member, 'host_connection_id': host_id})
        self.store.execute('''INSERT INTO room_host_binding_adoptions
            (member_id,binding_generation,host_connection_id,evidence_kind,evidence_id,created_at)
            VALUES (?,?,?,?,?,?)''', (member_id, generation, host_id, evidence_kind, evidence_id, verified_at))
        table = 'room_pending_bindings' if member.get('_pending_binding') else 'room_members'
        row_id = member.get('_pending_binding_id', member_id)
        changed = self.store.execute(f'''UPDATE {table} SET host_connection_id=?,host_revocation_epoch=?,
            connection_identity_version=2,route_generation=MAX(route_generation,1)
            WHERE id=? AND binding_generation=? AND host_connection_id=?''',
            (host_id, host['host_revocation_epoch'], row_id, generation, selected))
        if changed.rowcount != 1:
            raise DevError('ROOM_BINDING_CHANGED', '原绑定已变化，请重新继续接入', 409)
        if selected and member.get('_pending_binding'):
            self.touch_authority(host_id)
        return self.read_host(host_id)

    def bound_members(self, host, after='', limit=100):
        return self.store.all("""SELECT m.*,r.title AS room_title,r.state AS room_state
            FROM room_members m JOIN conversation_rooms r ON r.id=m.conversation_id
            WHERE m.host_connection_id=? AND m.host_connection_id!='' AND m.id>? AND m.status!='removed'
                AND r.space_id=? AND r.owner_user_id=?
            ORDER BY m.id LIMIT ?""",
            (host['id'], after, host['space_id'], host['owner_user_id'], limit))

    def project_host(self, host):
        state, next_action = 'recovering', 'wait_for_host_refresh'
        aggregate = self.store.one("""SELECT COUNT(DISTINCT m.conversation_id) AS room_count,
                MIN(r.title) AS single_room_title, MAX(m.probe_replied_at) AS last_round_trip_at,
                COUNT(CASE WHEN m.status='paused' OR r.state!='active' THEN 1 END) AS paused_count,
                COUNT(CASE WHEN m.status='invited' THEN 1 END) AS invited_count,
                COUNT(CASE WHEN m.status='bound' AND m.subscription_verified_at IS NOT NULL
                    AND m.probe_replied_at IS NULL THEN 1 END) AS verifying_count
            FROM room_members m JOIN conversation_rooms r ON r.id=m.conversation_id
            WHERE m.host_connection_id=? AND m.host_connection_id!='' AND m.status!='removed'
                AND r.space_id=? AND r.owner_user_id=?""", (host['id'], host['space_id'], host['owner_user_id']))
        anchor = self.store.one("""SELECT m.id,m.binding_generation FROM room_members m
            JOIN conversation_rooms r ON r.id=m.conversation_id
            JOIN mcp_event_subscriptions s ON s.id=m.subscription_id
            WHERE m.host_connection_id=? AND m.host_connection_id!='' AND m.grant_id=?
                AND m.status='bound' AND m.probe_replied_at IS NOT NULL AND r.state='active'
                AND r.space_id=? AND r.owner_user_id=? AND s.state='active'
                AND (s.expires_at IS NULL OR s.expires_at>?)
            ORDER BY m.probe_replied_at DESC,m.id LIMIT 1""",
            (host['id'], host['grant_id'], host['space_id'], host['owner_user_id'], self.c.clock()))
        grant = self.store.one('''SELECT g.revoked,e.expires FROM grants g
            LEFT JOIN grant_expirations e ON e.grant_id=g.id
            WHERE g.id=? AND g.user_id=? AND g.space_id=?''',
            (host['grant_id'], host['owner_user_id'], host['space_id']))
        if host['state'] == 'revoked':
            state, next_action, anchor = 'revoked', 'rebind', None
        elif not grant or grant['revoked'] or grant['expires'] is not None and grant['expires'] <= self.c.clock():
            state, next_action, anchor = 'authorization_expired', 'reauthorize', None
        elif anchor:
            state, next_action = 'connected', 'reuse'
        elif aggregate['room_count']:
            if aggregate['paused_count'] == aggregate['room_count']:
                state, next_action = 'paused', 'resume'
            elif aggregate['verifying_count']:
                state, next_action = 'verifying', 'view_details'
            elif aggregate['invited_count'] == aggregate['room_count']:
                state, next_action = 'waiting_host', 'continue_connection'
            else:
                state = 'waiting_host_renewal'
        proofs = getattr(self.c.rooms, 'host_proofs', None)
        rollout_active = self.c.rooms.host_connections_enabled()
        can_reuse = bool(rollout_active and state == 'connected' and proofs and proofs.can_challenge(host))
        return {
            'host_connection_id': host['id'], 'label': host['label'], 'state': host['state'],
            'display_state': state, 'next_action': next_action, 'verified_at': host['verified_at'],
            'last_round_trip_at': aggregate['last_round_trip_at'],
            'authority_version': host['authority_version'], 'health_version': host['health_version'],
            'room_count': aggregate['room_count'],
            'single_room_title': aggregate['single_room_title'] if aggregate['room_count'] == 1 else None,
            'turn_capacity': 'unverified',
            'reuse_member_id': anchor['id'] if anchor else None,
            'reuse_binding_generation': anchor['binding_generation'] if anchor else None,
            'connection_capabilities': {'host_reference': True, 'verified_reuse': can_reuse,
                'route_rotation': rollout_active and state == 'connected',
                'batch_rebind': rollout_active, 'host_revoke': host['state'] == 'active'},
        }

    def host_listing(self, raw, principal):
        args = validate(HostConnectionList, raw)
        principal = self.owner(principal)
        binding = digest(['room-host-connections-v2', principal.space_id, principal.user_id])
        offset = read_cursor(self.c.secret, binding, args['cursor']) if args['cursor'] else 0
        with self.store.transaction(immediate=False):
            rows = self.store.all('''SELECT * FROM room_host_connections WHERE space_id=? AND owner_user_id=?
                ORDER BY created_at,id LIMIT ? OFFSET ?''',
                (principal.space_id, principal.user_id, args['limit'] + 1, offset))
            items = [self.project_host(row) for row in rows[:args['limit']]]
        return {'items': items, 'next_cursor': sign_cursor(self.c.secret, binding, offset + args['limit'])
                if len(rows) > args['limit'] else None, 'connection_capabilities': {
                    'host_reference': True, 'verified_reuse': any(
                        item['connection_capabilities']['verified_reuse'] for item in items),
                    'batch_rebind': self.c.rooms.host_connections_enabled(), 'host_revoke': True}}

    def host_view(self, raw, principal):
        args = validate(HostConnectionRead, raw)
        with self.store.transaction(immediate=False):
            _, host = self.owned_host(principal, args['host_connection_id'])
            return self.project_host(host)

    @staticmethod
    def impact_scope(host):
        sql = """WITH bindings AS (
            SELECT m.id AS member_id,m.binding_generation,m.conversation_id,r.title AS room_title,0 AS candidate
            FROM room_members m JOIN conversation_rooms r ON r.id=m.conversation_id
            WHERE m.host_connection_id=:host_id AND m.host_connection_id!='' AND m.status!='removed'
                AND r.space_id=:space_id AND r.owner_user_id=:owner_user_id
            UNION ALL
            SELECT m.id,p.binding_generation,m.conversation_id,r.title,1
            FROM room_pending_bindings p JOIN room_members m ON m.id=p.member_id
            JOIN conversation_rooms r ON r.id=m.conversation_id
            WHERE p.host_connection_id=:host_id AND p.host_connection_id!=''
                AND p.status IN ('invited','bound','verifying') AND m.status!='removed'
                AND r.space_id=:space_id AND r.owner_user_id=:owner_user_id
        ) """
        return sql, {'host_id': host['id'], 'space_id': host['space_id'], 'owner_user_id': host['owner_user_id']}

    def impact_summary(self, host):
        fingerprint = hashlib.sha256(canonical([host['id'], host['host_revocation_epoch']]).encode())
        sql, params = self.impact_scope(host)
        cursor = {'member_id': '', 'generation': 0}
        while rows := self.store.all(sql + """SELECT member_id,binding_generation,conversation_id FROM bindings
                WHERE (member_id,binding_generation) > (:member_id,:generation)
                ORDER BY member_id,binding_generation LIMIT 100""", {**params, **cursor}):
            for row in rows:
                fingerprint.update(b'\n')
                fingerprint.update(canonical([row['member_id'], row['binding_generation'], row['conversation_id']]).encode())
            cursor = {'member_id': rows[-1]['member_id'], 'generation': rows[-1]['binding_generation']}
        count = self.store.one(sql + 'SELECT COUNT(DISTINCT conversation_id) AS n FROM bindings', params)['n']
        return fingerprint.hexdigest(), count

    def impact(self, raw, principal):
        args = validate(HostConnectionImpact, raw)
        with self.store.transaction(immediate=False):
            _, host = self.owned_host(principal, args['host_connection_id'])
            binding = digest(['room-host-impact-v2', host['id'], host['authority_version']])
            offset = read_cursor(self.c.secret, binding, args['cursor']) if args['cursor'] else 0
            checksum, count = self.impact_summary(host)
            sql, params = self.impact_scope(host)
            rows = self.store.all(sql + """SELECT * FROM bindings
                ORDER BY member_id,binding_generation LIMIT :limit OFFSET :offset""",
                {**params, 'limit': args['limit'] + 1, 'offset': offset})
            counts = self.store.one("""SELECT COUNT(DISTINCT CASE WHEN w.state IN ('queued','leased')
                    THEN w.id END) AS queued_work_count,
                COUNT(DISTINCT CASE WHEN o.state IN ('running','reconnecting','cancelling','unknown')
                    THEN o.id END) AS dispatched_operation_count
                FROM room_native_work_assignments x JOIN room_members m ON m.id=x.member_id
                JOIN coordination_work w ON w.id=x.work_id
                LEFT JOIN coordination_operations c ON c.work_item_id=w.id
                LEFT JOIN operations o ON o.id=c.operation_id
                WHERE m.host_connection_id=? AND m.host_connection_id!='' AND m.status!='removed'""", (host['id'],))
            return {'host_connection_id': host['id'], 'authority_version': host['authority_version'],
                'impact_digest': checksum, 'room_count': count, **counts,
                'items': [{**row, 'id': row['member_id'], 'candidate': bool(row['candidate'])}
                          for row in rows[:args['limit']]],
                'next_cursor': sign_cursor(self.c.secret, binding, offset + args['limit'])
                if len(rows) > args['limit'] else None}

    def revoke(self, raw, principal):
        args = validate(HostConnectionRevoke, raw)
        with self.store.transaction():
            owner, host = self.owned_host(principal, args['host_connection_id'])

            def perform():
                self.assert_current(host)
                checksum, count = self.impact_summary(host)
                if host['authority_version'] != args['expected_authority_version'] or checksum != args['impact_digest']:
                    raise DevError('ROOM_CONNECTION_IMPACT_CHANGED', '受影响的连接已变化', 409)
                now = self.c.clock()
                changed = self.store.execute("""UPDATE room_host_connections SET state='revoked',
                    host_revocation_epoch=host_revocation_epoch+1,authority_version=authority_version+1,
                    revoked_at=?,revoked_reason='owner_revoked',updated_at=?
                    WHERE id=? AND state='active' AND authority_version=?""",
                    (now, now, host['id'], args['expected_authority_version']))
                if changed.rowcount != 1:
                    raise DevError('STALE_VERSION', '连接状态已变化', 409)
                epoch = host['host_revocation_epoch'] + 1
                operation_id = 'host_revoke_' + uuid.uuid4().hex
                self.store.execute("""INSERT INTO room_host_revoke_jobs
                    (host_connection_id,host_revocation_epoch,operation_id,state,updated_at) VALUES (?,?,?,'pending',?)""",
                    (host['id'], epoch, operation_id, now))
                self.store.audit(owner.actor, 'room_host.revoke', host['id'], detail={
                    'operation_id': operation_id, 'host_revocation_epoch': epoch, 'affected_room_count': count})
                self.store.after_commit(self.c.relay_hints.wake)
                return {'host_connection_id': host['id'], 'state': 'revoked', 'operation_id': operation_id,
                    'host_revocation_epoch': epoch, 'authority_version': host['authority_version'] + 1,
                    'affected_room_count': count, 'cleanup_state': 'pending'}

            receipt = self.c.rooms.mutation(owner, 'host_revoke', args, perform)
            job = self.store.one('''SELECT state,processed_count FROM room_host_revoke_jobs
                WHERE host_connection_id=? AND host_revocation_epoch=?''',
                (host['id'], receipt['host_revocation_epoch']))
            return {**receipt, 'cleanup_state': job['state'], 'processed_count': job['processed_count']}

    def stop_subscription(self, subscription_id):
        if subscription_id:
            self.c.rooms.renewals.stop(subscription_id, 'host_revoked')
            self.store.execute("""UPDATE mcp_event_subscriptions SET state='unsubscribed',version=version+1,updated=?
                WHERE id=? AND state!='unsubscribed'""", (self.c.clock(), subscription_id))
            # Retain the exact leased fence for the original response. It may
            # already be in flight at the time of the terminal host transition.
            self.store.execute("""UPDATE mcp_event_deliveries SET reason_code='host_revoked_in_flight'
                WHERE subscription_id=? AND state='leased'""", (subscription_id,))
            self.store.execute("""UPDATE mcp_event_deliveries SET state='abandoned',reason_code='host_revoked',
                fence=fence+1,lease_until=NULL WHERE subscription_id=? AND state IN ('pending','retry_wait')""",
                (subscription_id,))

    def drain_revoke_jobs(self, *, page_size=32):
        """Process one bounded page with its durable cursor and exact binding scope."""
        with self.store.transaction():
            # A crash can leave a dispatch lease with an unknown HTTP outcome.
            # Record that uncertainty at expiry without replaying a revoked send.
            self.store.execute("""UPDATE mcp_event_deliveries SET state='abandoned',lease_until=NULL,
                status_code=NULL,reason_code='host_revoked_in_flight_unknown'
                WHERE id IN (SELECT id FROM mcp_event_deliveries WHERE state='leased'
                    AND reason_code='host_revoked_in_flight' AND lease_until<=?
                    ORDER BY lease_until,id LIMIT ?)""", (self.c.clock(), page_size))
            job = self.store.one("""SELECT j.* FROM room_host_revoke_jobs j JOIN room_host_connections h
                ON h.id=j.host_connection_id AND h.host_revocation_epoch=j.host_revocation_epoch
                WHERE h.state='revoked' AND j.state IN ('pending','running')
                ORDER BY j.updated_at,j.host_connection_id LIMIT 1""")
            if not job:
                return 0
            host = self.read_host(job['host_connection_id'])
            members = self.bound_members(host, job['after_member_id'], page_size)
            candidates = self.store.all("""SELECT p.*,m.conversation_id,m.version AS member_version
                FROM room_pending_bindings p JOIN room_members m ON m.id=p.member_id
                WHERE p.host_connection_id=? AND p.host_connection_id!='' AND p.id>?
                    AND p.status IN ('invited','bound','verifying') ORDER BY p.id LIMIT ?""",
                (host['id'], job['after_candidate_id'], page_size))
            routes = self.store.all("""SELECT p.*,m.conversation_id,m.version AS member_version
                FROM room_subscription_routes p JOIN room_members m ON m.id=p.member_id
                WHERE p.host_connection_id=? AND p.id>? AND p.state IN ('approved','controlled','verifying')
                ORDER BY p.id LIMIT ?""", (host['id'], job['after_route_id'], page_size))
            for member in members:
                # The page and these effects use the same write transaction, so
                # a newly activated generation cannot inherit the old cleanup.
                self.c.rooms.stop_member_work(member, 'ROOM_CONNECTION_REVOKED', revoke=True)
                self.stop_subscription(member['subscription_id'])
                if member['status'] == 'invited' and not self.c.rooms.pending.current(member['id']):
                    self.store.execute("""UPDATE room_connect_flows SET invite_digest='',invite_secret='',expires_at=NULL,
                        last_error_code='ROOM_CONNECTION_REVOKED',version=version+1,updated_at=? WHERE member_id=?""",
                        (self.c.clock(), member['id']))
                self.c.rooms.change(member['conversation_id'], 'connection', member['id'], member['version'])
            for candidate in candidates:
                self.stop_subscription(candidate['subscription_id'])
                self.store.execute("""UPDATE room_pending_bindings SET status='cancelled',revoked_at=?,
                    version=version+1,updated_at=? WHERE id=? AND binding_generation=?
                        AND host_connection_id=? AND subscription_id=?""",
                    (self.c.clock(), self.c.clock(), candidate['id'], candidate['binding_generation'],
                     host['id'], candidate['subscription_id']))
                self.c.rooms.change(candidate['conversation_id'], 'connection',
                    candidate['member_id'], candidate['member_version'])
            for route in routes:
                self.stop_subscription(route['subscription_id'])
                self.store.execute("""UPDATE room_subscription_routes SET state='cancelled',
                    ticket_secret=NULL,nonce_secret=NULL WHERE id=? AND binding_generation=?
                        AND host_connection_id=? AND subscription_id=?""",
                    (route['id'], route['binding_generation'], host['id'], route['subscription_id']))
                self.c.rooms.change(route['conversation_id'], 'connection', route['member_id'], route['member_version'])
            n = len(members) + len(candidates) + len(routes)
            done = max(len(members), len(candidates), len(routes)) < page_size
            self.store.execute('''UPDATE room_host_revoke_jobs SET state=?,after_member_id=?,after_candidate_id=?,
                after_route_id=?,processed_count=processed_count+?,version=version+1,updated_at=?
                WHERE host_connection_id=? AND host_revocation_epoch=?''',
                ('done' if done else 'running',
                 members[-1]['id'] if members else job['after_member_id'],
                 candidates[-1]['id'] if candidates else job['after_candidate_id'],
                 routes[-1]['id'] if routes else job['after_route_id'],
                 n, self.c.clock(), host['id'], job['host_revocation_epoch']))
            return n
