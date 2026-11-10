"""Stable user rooms, members and recoverable connection flows.

Project partitions retain their historical identity. Room participation proves
neither project access nor execution authority; every resource use rechecks both.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import uuid
from dataclasses import asdict

from hub.collaboration.projection_cache import projection, memoized
from hub.collaboration.room_subscription_renewals import RoomSubscriptionRenewals, migrate as migrate_renewals
from hub.collaboration.room_waiting_inputs import fence_follow_poll, recover_waiting
from hub.principal import Principal, refresh_principal
from hub.collaboration.common import ROOM_INBOX_EVENT, canonical, digest, read_cursor, sign_cursor, validate
from shared import room_contracts as contracts
from shared.util import DevError


def migrate(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(conversation_rooms)')}
    for name, declaration in (
        ('state', "TEXT NOT NULL DEFAULT 'active'"),
        ('revision', 'INTEGER NOT NULL DEFAULT 1'),
        ('lead_member_id', "TEXT NOT NULL DEFAULT ''"),
        ('room_protocol', 'INTEGER NOT NULL DEFAULT 0'),
    ):
        if name not in columns:
            db.execute(f'ALTER TABLE conversation_rooms ADD COLUMN {name} {declaration}')
    db.execute("UPDATE conversation_rooms SET state='archived' WHERE archived_at IS NOT NULL")
    db.execute('''CREATE TABLE IF NOT EXISTS room_members (
        id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversation_rooms(id),
        owner_user_id TEXT NOT NULL,name TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'invited',
        binding_generation INTEGER NOT NULL DEFAULT 1 CHECK(binding_generation>=1),
        grant_id TEXT NOT NULL DEFAULT '',principal TEXT NOT NULL DEFAULT '{}',
        connection_proof_hash TEXT NOT NULL DEFAULT '',connection_proof_secret TEXT NOT NULL DEFAULT '',
        subscription_id TEXT NOT NULL DEFAULT '',subscription_verified_at REAL,
        endpoint_digest TEXT NOT NULL DEFAULT '',credential_digest TEXT NOT NULL DEFAULT '',
        probe_nonce_hash TEXT NOT NULL DEFAULT '',probe_nonce_secret TEXT NOT NULL DEFAULT '',
        probe_created_at REAL,probe_read_at REAL,probe_replied_at REAL,
        dirty_revision INTEGER NOT NULL DEFAULT 0,drained_revision INTEGER NOT NULL DEFAULT 0,
        scope_revision INTEGER NOT NULL DEFAULT 1,version INTEGER NOT NULL DEFAULT 1,
        legacy_dot_id TEXT NOT NULL DEFAULT '',created_at REAL NOT NULL,updated_at REAL NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS room_member_conversation ON room_members(conversation_id,status)')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS room_member_legacy ON room_members(legacy_dot_id) WHERE legacy_dot_id!=''")
    db.execute('''CREATE TABLE IF NOT EXISTS room_connect_flows (
        id TEXT PRIMARY KEY,conversation_id TEXT NOT NULL REFERENCES conversation_rooms(id),
        member_id TEXT NOT NULL REFERENCES room_members(id),owner_user_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'invited',version INTEGER NOT NULL DEFAULT 1,
        invite_digest TEXT NOT NULL DEFAULT '',invite_secret TEXT NOT NULL DEFAULT '',
        expires_at REAL,redeemed_at REAL,revoked_at REAL,last_error_code TEXT NOT NULL DEFAULT '',
        join_request_key TEXT NOT NULL DEFAULT '',join_request_digest TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL,updated_at REAL NOT NULL,UNIQUE(member_id))''')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS room_invite_digest ON room_connect_flows(invite_digest) WHERE invite_digest!=''")
    db.execute('''CREATE TABLE IF NOT EXISTS room_member_scopes (
        member_id TEXT NOT NULL REFERENCES room_members(id),project_id TEXT NOT NULL,
        environment_id TEXT NOT NULL,capabilities TEXT NOT NULL,execution_targets TEXT NOT NULL,
        lifetime_mode TEXT NOT NULL,expires_at REAL,version INTEGER NOT NULL DEFAULT 1,
        state TEXT NOT NULL DEFAULT 'active',approved_by TEXT NOT NULL,approved_at REAL NOT NULL,
        owner_principal TEXT NOT NULL DEFAULT '{}',resource_snapshot TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY(member_id,project_id,environment_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_mutations (
        id TEXT PRIMARY KEY,request_digest TEXT NOT NULL,result TEXT NOT NULL,created_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_mutation_scopes (
        mutation_id TEXT PRIMARY KEY REFERENCES room_mutations(id),
        conversation_id TEXT NOT NULL REFERENCES conversation_rooms(id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_changes (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,conversation_id TEXT NOT NULL,
        kind TEXT NOT NULL,object_id TEXT NOT NULL,version INTEGER NOT NULL,created_at REAL NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS room_change_conversation ON room_changes(conversation_id,sequence)')
    db.execute('''CREATE TABLE IF NOT EXISTS room_member_scope_revisions (
        member_id TEXT NOT NULL,project_id TEXT NOT NULL,environment_id TEXT NOT NULL,
        version INTEGER NOT NULL,snapshot TEXT NOT NULL,created_at REAL NOT NULL,
        PRIMARY KEY(member_id,project_id,environment_id,version))''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_member_paused_policies (
        member_id TEXT NOT NULL,policy_id TEXT NOT NULL,policy_version INTEGER NOT NULL,
        policy_digest TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(member_id,policy_id))''')
    for table, additions in (
        ('room_members', [('drained_revision', 'INTEGER NOT NULL DEFAULT 0'), ('scope_revision', 'INTEGER NOT NULL DEFAULT 1')]),
        ('room_member_scopes', [('owner_principal', "TEXT NOT NULL DEFAULT '{}'" ), ('resource_snapshot', "TEXT NOT NULL DEFAULT '{}'" )]),
    ):
        existing = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        for name, declaration in additions:
            if name not in existing:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {declaration}')
    # Immutable mapping only. Legacy subscriptions and timed approvals remain
    # legacy until an explicit owner migration creates a fresh native binding.
    private_adapters = (" WHERE NOT EXISTS (SELECT 1 FROM room_member_work_bridges b WHERE b.legacy_dot_id=d.id)"
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='room_member_work_bridges'").fetchone() else '')
    for dot in db.execute('''SELECT d.id,d.conversation_id,j.label,j.grant_id,j.state,d.created,r.owner_user_id
            FROM collaboration_dots d JOIN collaboration_join_slots j ON j.id=d.id
            JOIN conversation_rooms r ON r.id=d.conversation_id''' + private_adapters).fetchall():
        identifier = 'member_' + digest(['legacy', dot[0]])[:32]
        db.execute('''INSERT OR IGNORE INTO room_members
            (id,conversation_id,owner_user_id,name,status,grant_id,legacy_dot_id,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (identifier, dot[1], dot[6], dot[2], 'legacy' if dot[4] != 'revoked' else 'removed',
             dot[3] or '', dot[0], dot[5], dot[5]))
        db.execute('''INSERT OR IGNORE INTO room_connect_flows
            (id,conversation_id,member_id,owner_user_id,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?)''', ('flow_' + digest(['legacy', dot[0]])[:32],
            dot[1], identifier, dot[6], 'legacy', dot[5], dot[5]))
    from hub.collaboration.room_pending_bindings import migrate as migrate_pending_bindings
    migrate_pending_bindings(db)
    from hub.collaboration.room_connections import migrate as migrate_connections
    migrate_connections(db)
    from hub.collaboration.room_migration import migrate as migrate_room_migration
    migrate_room_migration(db)
    migrate_renewals(db)
    db.execute('''CREATE INDEX IF NOT EXISTS room_work_transient_pause
        ON coordination_work(id,goal_id) WHERE state='blocked'
        AND reason_code IN ('ROOM_PAUSED','ROOM_MEMBER_PAUSE')''')


class Rooms:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store
        from hub.collaboration.room_pending_bindings import PendingBindings
        self.pending = PendingBindings(self)
        from hub.collaboration.room_migration import RoomMigration
        self.migration = RoomMigration(self)
        self.renewals = RoomSubscriptionRenewals(self)

    @staticmethod
    def require_native(room):
        if not room['room_protocol']:
            raise DevError('ROOM_MIGRATION_REQUIRED', '旧房间在新协作中心仅可查看；请先迁移所有保留连接，再由房主明确激活此房间', 409)

    def owner(self, principal, conversation_id, *, include_archived=False):
        self.c.guard()
        principal = refresh_principal(self.store, principal)
        self.c.owner(principal)
        room = self.c.conversations.object(principal, conversation_id, include_archived=include_archived)
        return principal, room

    def member_scope(self, principal, conversation_id, member_id, binding_generation,
                     connection_proof=None, *, require_proof=False, stopping=False, pending=False,
                     check_connection=True):
        self.c.guard()
        principal = refresh_principal(self.store, principal)
        room = self.c.conversations.object(principal, conversation_id, include_archived=stopping)
        current = self.store.one('SELECT * FROM room_members WHERE id=? AND conversation_id=?', (member_id, conversation_id))
        member = (self.event_member(member_id, conversation_id, binding_generation)
            if current and pending else current)
        if (not member or member['owner_user_id'] != principal.user_id or not principal.grant_id
                or member['grant_id'] != principal.grant_id):
            raise DevError('ROOM_MEMBER_DENIED', '当前连接不属于这个房间成员', 403)
        self.c.notification_reader(principal)
        if member['binding_generation'] != binding_generation:
            raise DevError('ROOM_BINDING_CHANGED', '成员连接已更换，请恢复当前连接', 409)
        if not stopping and (member['status'] in {'paused', 'removed', 'legacy'}
                or current['status'] in {'paused', 'removed'} or room['state'] != 'active'):
            raise DevError('ROOM_MEMBER_INACTIVE', '成员或房间已暂停，不能处理新输入', 409)
        if require_proof and (not isinstance(connection_proof, str) or not member['connection_proof_hash']
                or not hmac.compare_digest(digest(connection_proof), member['connection_proof_hash'])):
            raise DevError('ROOM_MEMBER_PROOF_REQUIRED', '需要原成员连接证明，不能仅凭共享授权代替另一个成员', 403)
        if not stopping and not pending:
            self.require_native(room)
        connections = getattr(self.c, 'room_connections', None)
        if connections is not None and not stopping and check_connection:
            connections.guard_join(member, principal)
        return principal, room, member

    def event_member(self, member_id, conversation_id, binding_generation):
        """Verification adapter only; ordinary consumer APIs require live scope."""
        current = self.member(member_id, conversation_id)
        if current['binding_generation'] == binding_generation:
            return current
        return self.pending.by_generation(current, binding_generation)

    def member(self, identifier, conversation_id=None):
        row = self.store.one('SELECT * FROM room_members WHERE id=?', (identifier,))
        if not row or (conversation_id is not None and row['conversation_id'] != conversation_id):
            raise DevError('ROOM_MEMBER_NOT_FOUND', '此房间没有这个成员', 404)
        return row

    @memoized
    def principal(self, member):
        saved = json.loads(member['principal'])
        # Old join slots keep only an identity reference, not a native binding
        # proof or a complete authenticated snapshot. Preserve that history but
        # do not invent an actor/project scope to make it usable as a new member.
        if (not saved or not member['grant_id'] or not saved.get('actor')
                or not saved.get('user_id') or not isinstance(saved.get('projects'), list)):
            raise DevError('ROOM_MEMBER_UNBOUND', '成员尚未接入', 409)
        return refresh_principal(self.store, Principal(**{**saved, 'scopes': set()}))

    @memoized
    def scopes(self, member, principal=None):
        result = []
        for row in self.store.all("SELECT * FROM room_member_scopes WHERE member_id=? ORDER BY project_id,environment_id", (member['id'],)):
            active = self.member_scope_authorized(row)
            attached = self.store.one('''SELECT r.id FROM conversation_projects p JOIN collaboration_rooms r ON r.id=p.room_id
                WHERE p.conversation_id=? AND r.project_id=? AND r.environment_id=?''',
                (member['conversation_id'], row['project_id'], row['environment_id']))
            capabilities = json.loads(row['capabilities']) if active and attached else []
            reason = '' if capabilities else 'project_removed' if not attached else 'scope_needs_review'
            if capabilities and principal is not None:
                try:
                    project = self.c.runtime.project(row['project_id'], principal)
                    capabilities = [cap for cap in capabilities if cap in principal.scopes
                        and (cap != 'write' or project['mode'] == 'write')
                        and (cap != 'execute' or project['allow_tasks'])]
                    if not capabilities:
                        reason = 'connection_scope_changed'
                except DevError:
                    capabilities, reason = [], 'project_unavailable'
            result.append({'project_id': row['project_id'], 'environment_id': row['environment_id'],
                'capabilities': capabilities, 'execution_targets': json.loads(row['execution_targets']) if 'execute' in capabilities else [],
                'lifetime': {'mode': row['lifetime_mode'], 'expires_at': row['expires_at']},
                'version': row['version'], 'state': 'active' if capabilities else 'unavailable', 'reason_code': reason})
        return result

    @memoized
    def state(self, member, *, check_connection=True):
        if member['status'] in {'paused', 'removed'}:
            return member['status']
        room = self.store.one('SELECT state FROM conversation_rooms WHERE id=?', (member['conversation_id'],))
        if room and room['state'] != 'active':
            return 'paused'
        if member['status'] == 'legacy':
            return 'degraded'
        if not member['grant_id']:
            return 'invited'
        try:
            actor = self.principal(member)
            self.member_scope(actor, member['conversation_id'], member['id'], member['binding_generation'],
                pending=True, check_connection=check_connection)
        except DevError:
            return 'degraded'
        sub = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (member['subscription_id'],))
        if not sub:
            return 'waiting_host'
        if sub['state'] != 'active' or sub['expires_at'] <= self.c.clock():
            return 'degraded'
        if not member['subscription_verified_at'] or not member['probe_replied_at']:
            return 'verifying'
        return 'ready_work' if any(s['capabilities'] for s in self.scopes(member, actor)) else 'ready_chat'

    @staticmethod
    def display(state):
        title, action = {
            'invited': ('等待 dot 接入', 'copy_instructions'),
            'waiting_host': ('等待在 dot 中确认', 'continue_connection'),
            'verifying': ('正在验证回复', 'view_details'),
            'ready_chat': ('可以聊天', 'send_message'),
            'ready_work': ('可以协作', 'start_work'),
            'degraded': ('需要恢复连接', 'resume'),
            'paused': ('已暂停', 'resume'), 'removed': ('已移除', None),
        }[state]
        return {'title': title, 'primary_action': action}

    def member_view(self, member):
        state = self.state(member)
        flow = self.store.one('SELECT id FROM room_connect_flows WHERE member_id=?', (member['id'],))
        try:
            actor = self.principal(member)
        except DevError:
            actor = None
        return {'id': member['id'], 'member_id': member['id'], 'name': member['name'], 'label': member['name'],
            'state': state, 'status': state, 'display': self.display(state), 'version': member['version'],
            'binding_generation': member['binding_generation'], 'flow_id': flow['id'] if flow else '',
            'connection_source_member_id': member.get('connection_source_member_id', ''),
            'connection_source_generation': member.get('connection_source_generation', 0),
            'legacy': member['status'] == 'legacy', 'legacy_dot_id': member['legacy_dot_id'],
            'project_scopes': self.scopes(member, actor), 'verified_at': member['probe_replied_at'],
            'subscription_renewal': self.renewals.view(member),
            'evidence': {'member_created': True, 'host_bound': bool(member['grant_id']),
                'subscription_verified': bool(member['subscription_verified_at']),
                'round_trip_verified': bool(member['probe_replied_at'])}}

    @projection
    def view(self, principal, room, *, details=True):
        # Metadata is visible without any linked project. Resource labels are
        # still projected through the current account's project permissions.
        projects = self.c.conversations.visible(principal, room) if room['archived_at'] is None else []
        members = self.store.all('SELECT * FROM room_members WHERE conversation_id=? ORDER BY created_at,id', (room['id'],))
        if principal.grant_id:
            members = [m for m in members if m['grant_id'] == principal.grant_id]
        result = {'id': room['id'], 'conversation_id': room['id'], 'title': room['title'],
            'state': room['state'], 'version': room['version'], 'revision': room['revision'],
            'lead_member_id': room['lead_member_id'], 'legacy': not bool(room['room_protocol']),
            'read_only': not bool(room['room_protocol']) or room['state'] != 'active',
            'migration': self.migration.projection(room),
            'display': {'title': '已暂停' if room['state'] == 'paused' else '已归档' if room['state'] == 'archived' else '待命',
                        'primary_action': 'resume' if room['state'] == 'paused' else 'send_message'},
            'projects': [{'project_id': p['project_id'], 'project_label': p['project_label'], 'room_id': p['id'],
                'environment_id': p['environment_id'], 'state': p['state'], 'capabilities': []} for p in projects],
            'members': [self.member_view(m) for m in members],
            'visibility_token': digest([principal.grant_id or principal.user_id, [p['id'] for p in projects], room['revision']])}
        if not room['room_protocol']:
            result['display'].update(title='旧房间 · 只读', primary_action='migrate_room',
                reason='逐一迁移或移除原连接后，由房主明确激活此房间。')
        if details and room['state'] != 'archived':
            context=getattr(self.c,'room_context',None)
            if context is not None and not principal.grant_id:
                result['context']=context.project(principal,room)
            runtime = getattr(self.c, 'room_runtime', None)
            channels = getattr(self.c, 'room_channels', None)
            if runtime is not None:
                result['runtime'] = runtime.snapshot(principal, room['id'])
                result['works'] = result['runtime']['works']
                status = result['runtime']['state']
                result['display']['subtitle'] = {'running': '正在推进', 'waiting_user': '等待补充', 'idle': '待命'}.get(status, status)
            if channels is not None:
                try:
                    channel = channels.snapshot(principal, room['id'])
                except DevError as exc:
                    channel = {'kind': 'weixin', 'state': 'degraded', 'display': {'title': '微信需要恢复'}, 'reason_code': exc.code}
                if channel:
                    # The header needs only a status, not the QR/account flow.
                    result['channels'] = [{'kind': 'weixin', 'state': channel.get('stage', channel.get('state')),
                        'flow_id': channel.get('id', ''), 'display': channel.get('display') or
                        {'title': '微信已连接' if channel.get('stage') == 'ready' else '继续连接微信'}}]
        return result

    def read(self, raw, principal):
        args = validate(contracts.RoomRead, raw)
        principal, room = self.owner(principal, args['conversation_id'], include_archived=True)
        return {'room': self.view(principal, room), 'revision': room['revision']}

    def listing(self, raw, principal):
        args = validate(contracts.RoomList, raw)
        principal = refresh_principal(self.store, principal)
        self.c.guard()
        self.c.owner(principal)
        binding = digest(['room-list', principal.space_id, principal.user_id, args['include_archived']])
        offset = read_cursor(self.c.secret, binding, args['cursor']) if args['cursor'] else 0
        rows = self.store.all('SELECT * FROM conversation_rooms WHERE space_id=? AND owner_user_id=?' +
            ('' if args['include_archived'] else " AND state!='archived' AND archived_at IS NULL") +
            ' ORDER BY created,id LIMIT ? OFFSET ?', (principal.space_id, principal.user_id, args['limit'] + 1, offset))
        return {'items': [self.view(principal, row, details=False) for row in rows[:args['limit']]],
                'next_cursor': sign_cursor(self.c.secret, binding, offset + args['limit']) if len(rows) > args['limit'] else None}

    def mutation(self, principal, action, args, perform):
        identity = [principal.space_id, principal.user_id, principal.grant_id, action, args['idempotency_key']]
        fingerprint = digest(args)
        if args.get('member_id'):
            # Replay an older receipt only when its full request (including the
            # member) matches. Other members get their own independent identity.
            previous = self.store.one('SELECT * FROM room_mutations WHERE id=?', (digest(identity),))
            if previous and previous['request_digest'] == fingerprint:
                from hub.collaboration.history_retention import replay_response
                return replay_response(previous['result'])
            identity.append(args['member_id'])
        identifier = digest(identity)
        saved = self.store.one('SELECT * FROM room_mutations WHERE id=?', (identifier,))
        if saved:
            if saved['request_digest'] != fingerprint:
                raise DevError('IDEMPOTENCY_CONFLICT', '同一请求键不能用于不同内容', 409)
            from hub.collaboration.history_retention import replay_response
            return replay_response(saved['result'])
        result = perform()
        self.store.execute('INSERT INTO room_mutations VALUES (?,?,?,?)', (identifier, fingerprint, canonical(result), self.c.clock()))
        conversation_id = result.get('conversation_id') or args.get('conversation_id')
        if not conversation_id and result.get('flow_id'):
            flow = self.store.one('SELECT conversation_id FROM room_connect_flows WHERE id=?', (result['flow_id'],))
            conversation_id = flow['conversation_id'] if flow else None
        if conversation_id and self.store.one(
                'SELECT 1 FROM conversation_rooms WHERE id=? AND space_id=? AND owner_user_id=?',
                (conversation_id, principal.space_id, principal.user_id)):
            self.store.execute('INSERT INTO room_mutation_scopes VALUES (?,?)', (identifier, conversation_id))
        return result

    def change(self, conversation_id, kind, object_id, version=1):
        self.store.execute('''UPDATE conversation_rooms SET revision=revision+1,
            version=version+CASE WHEN room_protocol=0 AND ? IN ('member','connection') THEN 1 ELSE 0 END
            WHERE id=?''', (kind, conversation_id))
        self.store.execute('INSERT INTO room_changes(conversation_id,kind,object_id,version,created_at) VALUES (?,?,?,?,?)',
                           (conversation_id, kind, object_id, version, self.c.clock()))
        self.store.after_commit(self.c.relay_hints.wake)

    @projection
    def hint_snapshot(self, conversation_id, principal):
        with self.store.transaction(immediate=False):
            principal, room = self.owner(principal, conversation_id, include_archived=True)
            projects = self.c.conversations.visible(principal, room) if room['state'] != 'archived' else []
            identifiers = [p['id'] for p in projects]
            revisions = self.store.all('SELECT room_id,revision FROM collaboration_relay_revisions WHERE room_id IN (' +
                ','.join('?' for _ in identifiers) + ') ORDER BY room_id', identifiers) if identifiers else []
            members = self.store.all('SELECT * FROM room_members WHERE conversation_id=? ORDER BY id', (conversation_id,))
            access = digest([self.c.principal_snapshot(principal), principal.admin, sorted(principal.scopes),
                sorted(principal.projects), room['state'], [(p['id'], p['state']) for p in projects]])
            revision = digest([access, room['revision'], revisions,
                [(m['id'], m['version'], m['dirty_revision'], m['scope_revision'], self.state(m)) for m in members]])
            return {'revision': revision, 'access': access, 'owner': (principal.space_id, principal.user_id)}

    async def hint_stream(self, request, conversation_id, auth, queue, initial, cursor=''):
        access, previous = initial['access'], cursor
        try:
            while not self.c.runtime.stopping:
                if await request.is_disconnected():
                    return
                try:
                    current = await self.store.run(lambda: self.hint_snapshot(conversation_id, auth.panel(request)))
                except DevError:
                    yield 'event: access_changed\ndata: {}\n\n'
                    return
                if current['access'] != access:
                    yield 'event: access_changed\ndata: {}\n\n'
                    return
                if current['revision'] != previous:
                    previous = current['revision']
                    yield 'id: ' + previous + '\nevent: change\ndata: ' + canonical({'revision': previous}) + '\n\n'
                try:
                    await asyncio.wait_for(queue.get(), 5)
                except asyncio.TimeoutError:
                    yield ': heartbeat\n\n'
        finally:
            self.c.relay_hints.unsubscribe(queue)

    @staticmethod
    def check_version(row, expected):
        if expected is not None and row['version'] != expected:
            raise DevError('STALE_VERSION', '内容已更新，请保留当前选择并重新核对', 409, current_version=row['version'])

    def partitions(self, principal, projects):
        partitions, seen = [], set()
        for item in projects:
            project = self.c.runtime.project(item['project'], principal)
            key = (project['id'], item['environment_id'])
            if key in seen:
                raise DevError('DUPLICATE_PROJECT', '同一项目与环境不能重复关联', 422)
            seen.add(key)
            _, partition = self.c.scope(principal, item, create=True, default_conversation=False)
            partitions.append(partition)
        return partitions

    def create(self, raw, principal):
        args = validate(contracts.RoomCreate, raw)
        with self.store.transaction():
            principal = refresh_principal(self.store, principal)
            self.c.guard()
            self.c.owner(principal)
            for item in args['projects']:
                self.c.runtime.project(item['project'], principal)
            def perform():
                parts = self.partitions(principal, args['projects'])
                identifier = uuid.uuid4().hex
                self.store.execute('''INSERT INTO conversation_rooms
                    (id,space_id,owner_user_id,title,created,room_protocol) VALUES (?,?,?,?,?,1)''',
                    (identifier, principal.space_id, principal.user_id, args['title'], self.c.clock()))
                for part in parts:
                    self.store.execute('INSERT INTO conversation_projects VALUES (?,?,?)', (identifier, part['id'], self.c.clock()))
                self.c.room_permissions.initialize(identifier, parts, principal)
                self.change(identifier, 'room', identifier)
                return {'conversation_id': identifier}
            receipt = self.mutation(principal, 'room_create', args, perform)
            return self.read(receipt, principal)

    def update(self, raw, principal):
        args = validate(contracts.RoomPatch, raw)
        with self.store.transaction():
            principal, room = self.owner(principal, args['conversation_id'], include_archived=True)
            def perform():
                self.check_version(room, args['expected_version'])
                if args['lead_member_id'] is not None and args['lead_member_id'] != room['lead_member_id']:
                    self.require_native(room)
                if args['lead_member_id']:
                    member = self.member(args['lead_member_id'], room['id'])
                    if member['status'] == 'removed':
                        raise DevError('ROOM_MEMBER_INACTIVE', '已移除的成员不能担任主责', 409)
                state = args['state'] or room['state']
                if ((state == 'archived' and room['archived_at'] is None)
                        or (state == 'active' and room['archived_at'] is not None)):
                    # RoomPatch's explicit state transition is the legacy owner
                    # confirmation. Reuse the canonical versioned archive path,
                    # including its dependency checks and irreversible-authority
                    # shutdown; direct archive calls still require preview digest.
                    archive = self.c.conversations.archive
                    if state == 'archived':
                        preview = archive.preview({'conversation_id': room['id']}, principal)
                        archive.archive({'conversation_id': room['id'], 'expected_version': room['version'],
                            'impact_digest': preview['impact_digest'], 'confirm_archive': True,
                            'idempotency_key': 'room-archive:' + args['idempotency_key']}, principal)
                    else:
                        archive.restore({'conversation_id': room['id'], 'expected_version': room['version'],
                            'confirm_restore': True, 'idempotency_key': 'room-restore:' + args['idempotency_key']}, principal)
                    if args['title'] is not None or args['lead_member_id'] is not None:
                        self.store.execute('UPDATE conversation_rooms SET title=COALESCE(?,title),lead_member_id=COALESCE(?,lead_member_id) WHERE id=?',
                            (args['title'], args['lead_member_id'], room['id']))
                    self.change(room['id'], 'room', room['id'], room['version'] + 1)
                    return {'conversation_id': room['id']}
                if room['archived_at'] is not None and state != 'archived':
                    raise DevError('CONVERSATION_ARCHIVED', '请先通过恢复操作恢复归档房间', 409)
                if state != 'active' and state != room['state']:
                    for member in self.store.all("SELECT * FROM room_members WHERE conversation_id=? AND status!='removed'", (room['id'],)):
                        self.pending.cancel(member['id'])
                        self.stop_member_work(member, 'ROOM_' + state.upper(), revoke=state == 'archived')
                        if state == 'archived':
                            self.store.execute("UPDATE room_members SET status='paused',version=version+1 WHERE id=?", (member['id'],))
                elif state == 'active' and room['state'] == 'paused' and room['room_protocol']:
                    for member in self.store.all("SELECT * FROM room_members WHERE conversation_id=? AND status NOT IN ('paused','removed')", (room['id'],)):
                        self.resume_member_policies(member)
                self.store.execute('''UPDATE conversation_rooms SET title=?,state=?,lead_member_id=?,
                    archived_at=?,version=version+1 WHERE id=?''',
                    (args['title'] if args['title'] is not None else room['title'], state,
                     args['lead_member_id'] if args['lead_member_id'] is not None else room['lead_member_id'],
                     self.c.clock() if state == 'archived' else None, room['id']))
                if state != room['state']:
                    self.store.execute('''UPDATE collaboration_rooms SET state=?,version=version+1 WHERE id IN
                        (SELECT room_id FROM conversation_projects WHERE conversation_id=?)
                        AND (? != 'active' OR NOT EXISTS (
                            SELECT 1 FROM conversation_projects p JOIN conversation_rooms c ON c.id=p.conversation_id
                            WHERE p.room_id=collaboration_rooms.id AND c.state!='active'))''',
                        ('active' if state == 'active' else 'paused', room['id'], state))
                if state != 'active':
                    self.store.execute('''UPDATE mcp_event_subscriptions SET state='paused',version=version+1
                        WHERE state='active' AND (conversation_id=? OR (conversation_id IS NULL AND room_id IN
                        (SELECT room_id FROM conversation_projects WHERE conversation_id=?)))''', (room['id'], room['id']))
                elif room['state'] != 'active':
                    self.store.execute("""UPDATE mcp_event_subscriptions SET state='active',version=version+1
                        WHERE conversation_id=? AND state='paused' AND id IN
                        (SELECT subscription_id FROM room_members WHERE conversation_id=? AND status NOT IN ('paused','removed'))""", (room['id'], room['id']))
                lead_changed = args['lead_member_id'] is not None and args['lead_member_id'] != room['lead_member_id']
                if lead_changed:
                    fence_follow_poll(self.c, room['id'])
                if state == 'active' and (lead_changed or room['state'] != 'active'):
                    recover_waiting(self.c, conversation_id=room['id'])
                    if room['state'] != 'active':
                        self.recover_paused_work(conversation_id=room['id'])
                        for member in self.store.all("SELECT * FROM room_members WHERE conversation_id=? AND status NOT IN ('paused','removed')", (room['id'],)):
                            self.c.room_consumers.recover_connection(member)
                self.change(room['id'], 'room', room['id'], room['version'] + 1)
                return {'conversation_id': room['id']}
            return self.read(self.mutation(principal, 'room_update', args, perform), principal)

    def migrate_room(self, raw, principal):
        args = validate(contracts.RoomMigrate, raw)
        with self.store.transaction():
            principal, room = self.owner(principal, args['conversation_id'], include_archived=True)
            def perform():
                try:
                    return self.migration.activate(args, principal, room)
                except DevError as exc:
                    # This callback is never entered for a saved receipt. Only
                    # these two pre-admission failures prove this key did not
                    # commit; permission/conflict/internal failures do not.
                    if exc.code in {'STALE_VERSION', 'ROOM_MIGRATION_BLOCKED'}:
                        raise DevError(exc.code, exc.message, exc.status, **{**exc.details, 'not_saved': True}) from exc
                    raise
            result = self.mutation(principal, 'room_migrate', args,
                perform)
            return {**self.read({'conversation_id': room['id']}, principal), 'activated': result['activated']}

    def stop_member_work(self, member, reason, *, revoke=False):
        """Stop original policy work, retaining operation IDs and actual results."""
        bridges = self.store.all("SELECT * FROM room_member_work_bridges WHERE member_id=? AND state='active'", (member['id'],))
        identifiers = {row['legacy_dot_id'] for row in bridges}
        if member['legacy_dot_id']:
            identifiers.add(member['legacy_dot_id'])
        for identifier in identifiers:
            dot = self.c.dots.find(identifier)
            policy = self.store.one('SELECT * FROM delegation_policies WHERE id=?', (dot['policy_id'],)) if dot else None
            if policy:
                self.c.delegation.stop(policy, reason)
                if policy['state'] == 'active':
                    self.store.execute('INSERT OR IGNORE INTO room_member_paused_policies VALUES (?,?,?,?,?)',
                        (member['id'], policy['id'], policy['version'], policy['digest'], self.c.clock()))
                    self.store.execute("UPDATE delegation_policies SET state='paused',updated=? WHERE id=?", (self.c.clock(), policy['id']))
            if revoke:
                self.store.execute("UPDATE collaboration_join_slots SET state='revoked',version=version+1,updated=? WHERE id=?", (self.c.clock(), identifier))
        self.store.execute("UPDATE dot_consumer_sessions SET state='expired',updated=? WHERE member_id=? AND state='active'", (self.c.clock(), member['id']))
        if revoke:
            self.store.execute("UPDATE room_member_work_bridges SET state='superseded',updated=? WHERE member_id=? AND state='active'", (self.c.clock(), member['id']))
            self.store.execute('DELETE FROM room_member_paused_policies WHERE member_id=?', (member['id'],))
            self.store.execute("UPDATE mcp_event_subscriptions SET state='unsubscribed',version=version+1 WHERE id=?", (member['subscription_id'],))

    def resume_member_policies(self, member):
        # Restore only adapter policies paused by this room/member action.
        for saved in self.store.all('SELECT * FROM room_member_paused_policies WHERE member_id=?', (member['id'],)):
            policy = self.store.one('SELECT * FROM delegation_policies WHERE id=?', (saved['policy_id'],))
            if (policy and policy['state'] == 'paused' and policy['version'] == saved['policy_version']
                    and policy['digest'] == saved['policy_digest']):
                self.store.execute("UPDATE delegation_policies SET state='active',updated=? WHERE id=?", (self.c.clock(), policy['id']))
            self.store.execute('DELETE FROM room_member_paused_policies WHERE member_id=? AND policy_id=?', (member['id'], saved['policy_id']))

    def recover_paused_work(self, *, conversation_id=None):
        # Undispatched items may return to the same intent. Dispatched
        # attempts keep their fence and settle through original result archival.
        with self.store.transaction():
            after = getattr(self, '_transient_work_after', '') if conversation_id is None else ''
            rows = self.store.all('''SELECT w.*,a.member_id FROM coordination_work w
                JOIN room_native_work_assignments a ON a.work_id=w.id
                JOIN coordination_goals g ON g.id=w.goal_id AND g.approval_id=w.approval_id
                JOIN conversation_rooms r ON r.id=g.conversation_id
                LEFT JOIN room_work_intents i ON i.goal_id=g.id
                WHERE w.state='blocked' AND w.reason_code IN ('ROOM_PAUSED','ROOM_MEMBER_PAUSE')
                AND w.id>? AND g.state='active' AND r.state='active' AND r.room_protocol!=0
                AND (? IS NULL OR r.id=?) AND a.intent_revision=COALESCE(i.intent_revision,1)
                ORDER BY w.id LIMIT 32''', (after, conversation_id, conversation_id))
            if conversation_id is None:
                self._transient_work_after = rows[-1]['id'] if len(rows)==32 else ''
            native = self.c.room_work_bridge.native
            coordination = self.c.coordination
            for item in rows:
                goal = self.c.room_runtime.goal(item['goal_id'])
                partition = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (goal['room_id'],))
                try:
                    member = self.member(item['member_id'], goal['conversation_id'])
                    if self.state(member) != 'ready_work':
                        continue
                    actor = coordination.participant(partition, goal, member['grant_id'], member['id'])
                    native.check_work(actor, goal, item, member['id'])
                    dispatched = self.store.one("""SELECT 1 FROM coordination_operations WHERE work_item_id=?
                        AND COALESCE(cancel_note,'')!='cancelled_before_delivery' LIMIT 1""", (item['id'],))
                    if dispatched:
                        if coordination.paused_archive_evidence(actor, goal, item):
                            self.c.room_consumers.enqueue_paused_archive(member, item)
                        continue
                except DevError:
                    continue
                if item['attempt'] >= json.loads(goal['spec'])['budget']['max_attempts']:
                    continue
                changed = self.store.execute('''UPDATE coordination_work SET state='queued',reason_code='',
                    version=version+1,updated=? WHERE id=? AND version=? AND state='blocked' ''',
                    (self.c.clock(), item['id'], item['version'])).rowcount
                if changed:
                    coordination.emit_work(partition, goal, coordination.work(goal, item['id']))
                    self.change(goal['conversation_id'], 'work', goal['id'], goal['version'])

    def approve_scopes(self, principal, member_id, conversation_id, scopes, lifetime):
        if lifetime and lifetime['mode'] == 'timed' and lifetime['expires_at'] <= self.c.clock():
            raise DevError('AUTHORIZATION_EXPIRED', '授权期限已过，请选择未来时间', 422)
        seen, added, changed, revoked = set(), [], [], []
        owner_snapshot = asdict(principal)
        owner_snapshot.update(scopes=[], session_hash='', token_hash='')
        for scope in scopes:
            key = (scope['project_id'], scope['environment_id'])
            if key in seen:
                raise DevError('DUPLICATE_PROJECT_SCOPE', '成员项目范围不能重复', 422)
            seen.add(key)
            attached = self.store.one('''SELECT r.id FROM conversation_projects p JOIN collaboration_rooms r ON r.id=p.room_id
                WHERE p.conversation_id=? AND r.project_id=? AND r.environment_id=?''', (conversation_id, *key))
            if not attached:
                raise DevError('ROOM_PROJECT_NOT_ATTACHED', '请先把所选项目加入房间', 409)
            project = self.c.runtime.project(scope['project_id'], principal)
            for capability in scope['capabilities']:
                self.c.coordination.require_capability(principal, capability, project['id'])
            if 'write' in scope['capabilities'] and project['mode'] != 'write':
                raise DevError('PROJECT_READ_ONLY', '这个项目仅允许读取', 403)
            if 'execute' in scope['capabilities'] and not project['allow_tasks']:
                raise DevError('PROJECT_EXECUTION_DISABLED', '这个项目尚未允许执行', 403)
            resource_snapshot = {'project': self.c.coordination.project_snapshot(project), 'targets': {
                target: self.c.delegation.target_snapshot(principal, project, target) for target in scope['execution_targets']}}
            previous=self.store.one('SELECT * FROM room_member_scopes WHERE member_id=? AND project_id=? AND environment_id=?',
                (member_id,*key))
            effective_lifetime=scope.get('lifetime') or lifetime or ({'mode':previous['lifetime_mode'],'expires_at':previous['expires_at']}
                if previous else {'mode':'until_revoked','expires_at':None})
            capabilities=sorted(scope['capabilities'])
            targets=sorted(set(scope['execution_targets']))
            if (previous and previous['state']=='active' and sorted(json.loads(previous['capabilities']))==capabilities
                    and sorted(json.loads(previous['execution_targets']))==targets
                    and previous['lifetime_mode']==effective_lifetime['mode']
                    and previous['expires_at']==effective_lifetime['expires_at']
                    and json.loads(previous['resource_snapshot'])==resource_snapshot):
                continue  # Retain the exact original owner proof, version and deadline.
            if effective_lifetime['mode']=='timed' and effective_lifetime['expires_at']<=self.c.clock():
                raise DevError('AUTHORIZATION_EXPIRED','原范围已到期，请明确选择新的授权期限',422)
            self.store.execute('''INSERT INTO room_member_scopes
                (member_id,project_id,environment_id,capabilities,execution_targets,lifetime_mode,expires_at,approved_by,approved_at,owner_principal,resource_snapshot)
                VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(member_id,project_id,environment_id) DO UPDATE SET
                capabilities=excluded.capabilities,execution_targets=excluded.execution_targets,
                lifetime_mode=excluded.lifetime_mode,expires_at=excluded.expires_at,state='active',
                approved_by=excluded.approved_by,approved_at=excluded.approved_at,owner_principal=excluded.owner_principal,
                resource_snapshot=excluded.resource_snapshot,
                version=room_member_scopes.version+1''',
                (member_id, *key, canonical(capabilities), canonical(targets),
                 effective_lifetime['mode'], effective_lifetime['expires_at'], principal.actor, self.c.clock(), canonical(owner_snapshot), canonical(resource_snapshot)))
            self.scope_revision(member_id, *key)
            (changed if previous else added).append(key)
        for row in self.store.all("SELECT project_id,environment_id FROM room_member_scopes WHERE member_id=? AND state='active'", (member_id,)):
            if (row['project_id'], row['environment_id']) not in seen:
                self.store.execute("UPDATE room_member_scopes SET state='revoked',version=version+1 WHERE member_id=? AND project_id=? AND environment_id=?", (member_id, row['project_id'], row['environment_id']))
                self.scope_revision(member_id, row['project_id'], row['environment_id'])
                revoked.append((row['project_id'],row['environment_id']))
        for project_id,environment_id in changed+revoked:
            for bridge in self.store.all("SELECT * FROM room_member_work_bridges WHERE member_id=? AND project_id=? AND environment_id=? AND state='active'",
                    (member_id,project_id,environment_id)):
                policy=self.store.one('SELECT * FROM delegation_policies WHERE id=?',(bridge['policy_id'],))
                if policy:
                    self.c.delegation.stop(policy,'ROOM_MEMBER_SCOPE_REVISED')
                    self.store.execute("UPDATE delegation_policies SET state='paused',updated=? WHERE id=?",(self.c.clock(),policy['id']))
                self.store.execute("UPDATE room_member_work_bridges SET state='superseded',updated=? WHERE legacy_dot_id=?",(self.c.clock(),bridge['legacy_dot_id']))
                self.store.execute("UPDATE collaboration_join_slots SET state='revoked',version=version+1,updated=? WHERE id=?",(self.c.clock(),bridge['legacy_dot_id']))
        if added or changed or revoked:
            self.store.execute('UPDATE room_members SET scope_revision=scope_revision+1 WHERE id=?', (member_id,))
        return {'added':added,'changed':changed,'revoked':revoked}

    def scope_revision(self, member_id, project_id, environment_id):
        row = self.store.one('SELECT * FROM room_member_scopes WHERE member_id=? AND project_id=? AND environment_id=?',
                              (member_id, project_id, environment_id))
        if row:
            self.store.execute('INSERT OR IGNORE INTO room_member_scope_revisions VALUES (?,?,?,?,?,?)',
                (member_id, project_id, environment_id, row['version'], canonical(row), self.c.clock()))

    def project_effect_rows(self, room, partition, *, operations=False, scan_limit=None):
        """Page original work/operations and use one exact environment rule.

        The preview has a bounded inspection window; a None sentinel reports
        that it was truncated. The committed revocation visits every matching
        original in bounded SQL pages inside its existing transaction.
        """
        after, scanned = '', 0
        selection = '''SELECT w.*,g.spec AS saved_goal_spec,g.room_id AS saved_goal_room,
            g.conversation_id AS saved_conversation'''
        joins = ' FROM coordination_work w JOIN coordination_goals g ON g.id=w.goal_id'
        if operations:
            selection += ',o.id AS operation_id,o.state AS operation_state,o.cancel_requested'
            joins += ' JOIN coordination_operations c ON c.work_item_id=w.id AND c.goal_id=g.id JOIN operations o ON o.id=c.operation_id'
            condition = " AND o.project_id=w.project_id AND o.state IN ('queued','running','reconnecting','cancelling','unknown')"
            key = 'o.id'
        else:
            condition = " AND w.state IN ('queued','leased','running')"
            key = 'w.id'
        while True:
            maximum = min(64, scan_limit - scanned + 1) if scan_limit is not None else 64
            rows = self.store.all(selection + joins + ' WHERE g.conversation_id=? AND w.project_id=?'
                + condition + f' AND {key}>? ORDER BY {key} LIMIT ?',
                (room['id'], partition['project_id'], after, maximum))
            for row in rows:
                if scan_limit is not None and scanned >= scan_limit:
                    yield None
                    return
                scanned += 1
                after = row['operation_id'] if operations else row['id']
                goal = {'id': row['goal_id'], 'room_id': row['saved_goal_room'],
                        'conversation_id': row['saved_conversation'], 'spec': row['saved_goal_spec']}
                if self.c.room_work_bridge.native.work_environment(goal, row) != partition['environment_id']:
                    continue
                if operations:
                    yield {'id': row['operation_id'], 'state': row['operation_state'],
                           'cancel_requested': row['cancel_requested'], 'goal_id': row['goal_id'],
                           'work_item_id': row['id'], 'project_id': row['project_id']}
                else:
                    yield row
            if len(rows) < maximum:
                return

    def detach_effects(self, room, removed, principal):
        """Fence only affected project work and track each original cancellation."""
        operations, affected_work = [], []
        for partition_id in removed:
            partition = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (partition_id,))
            project_id, environment_id = partition['project_id'], partition['environment_id']
            scopes = self.store.all('''SELECT s.* FROM room_member_scopes s JOIN room_members m ON m.id=s.member_id
                WHERE m.conversation_id=? AND s.project_id=? AND s.environment_id=? AND s.state='active' ''',
                (room['id'], project_id, environment_id))
            for scope in scopes:
                self.store.execute("UPDATE room_member_scopes SET state='detached',version=version+1 WHERE member_id=? AND project_id=? AND environment_id=?",
                    (scope['member_id'], project_id, environment_id))
                self.scope_revision(scope['member_id'], project_id, environment_id)
                self.store.execute('UPDATE room_members SET scope_revision=scope_revision+1 WHERE id=?', (scope['member_id'],))
            for item in self.project_effect_rows(room, partition):
                self.store.execute("""UPDATE coordination_work SET state='blocked',reason_code='ROOM_PROJECT_REMOVED',
                    lease_until=NULL,fencing_token=fencing_token+1,version=version+1,updated=? WHERE id=?""", (self.c.clock(), item['id']))
                affected_work.append(item['id'])
            for operation in self.project_effect_rows(room, partition, operations=True):
                try:
                    result = self.c.runtime._cancel(operation['id'], principal)
                except DevError as exc:
                    if exc.code not in {'NOT_CANCELLABLE', 'INSUFFICIENT_SCOPE'}:
                        raise
                    result = {'operation_id': operation['id'], 'state': operation['state'],
                              'cancel_requested': False, 'reason_code': exc.code}
                operations.append(result)
            # Legacy callers retain their old parameter meanings, but cannot
            # keep enrolling work through a partition the owner just removed.
            for policy in self.store.all('''SELECT p.* FROM delegation_policies p
                    JOIN collaboration_dots d ON d.policy_id=p.id
                    WHERE d.conversation_id=? AND d.room_id=? AND p.state='active' ''', (room['id'], partition_id)):
                self.c.delegation.stop(policy, 'ROOM_PROJECT_REMOVED')
                self.store.execute("UPDATE delegation_policies SET state='paused',updated=? WHERE id=?", (self.c.clock(), policy['id']))
            # Detached approvals are already inactive before canonical member
            # synchronization. Retire their exact adapters here as well; they
            # must not remain advertised as active or accept a later join.
            for scope in scopes:
                for bridge in self.store.all("""SELECT legacy_dot_id FROM room_member_work_bridges
                        WHERE member_id=? AND project_id=? AND environment_id=? AND state='active'""",
                        (scope['member_id'], project_id, environment_id)):
                    self.store.execute("UPDATE room_member_work_bridges SET state='superseded',updated=? WHERE legacy_dot_id=?",
                        (self.c.clock(), bridge['legacy_dot_id']))
                    self.store.execute("UPDATE collaboration_join_slots SET state='revoked',version=version+1,updated=? WHERE id=?",
                        (self.c.clock(), bridge['legacy_dot_id']))
        return {'blocked_work_item_ids': affected_work, 'operations': operations}

    def member_scope_authorized(self, row):
        """Lifetime hook for existing policy/lease checks; no new approval."""
        if row['state'] != 'active' or (row['lifetime_mode'] != 'until_revoked' and row['expires_at'] <= self.c.clock()):
            return False
        member = self.member(row['member_id'])
        if member['status'] in {'removed', 'paused'}:
            return False
        owner = json.loads(row['owner_principal'])
        if not owner:
            return False
        try:
            principal = refresh_principal(self.store, Principal(**{**owner, 'scopes': set()}))
            _, room = self.owner(principal, member['conversation_id'])
            if room['state'] != 'active':
                return False
            project = self.c.runtime.project(row['project_id'], principal)
            snapshot = json.loads(row['resource_snapshot'])
            if snapshot.get('project') != self.c.coordination.project_snapshot(project):
                return False
            for target in json.loads(row['execution_targets']):
                if snapshot.get('targets', {}).get(target) != self.c.delegation.target_snapshot(principal, project, target):
                    return False
            for capability in json.loads(row['capabilities']):
                self.c.coordination.require_capability(principal, capability, project['id'])
            return bool(self.store.one('''SELECT 1 FROM conversation_projects p JOIN collaboration_rooms r ON r.id=p.room_id
                WHERE p.conversation_id=? AND r.project_id=? AND r.environment_id=?''',
                (member['conversation_id'], row['project_id'], row['environment_id'])))
        except DevError:
            return False

    def project_impact(self, raw, principal):
        args = validate(contracts.RoomProjectImpact, raw)
        with self.store.transaction(immediate=False):
            principal, room = self.owner(principal, args['conversation_id'])
            self.check_version(room, args['expected_version'])
            members, items, operations, projects = [], [], [], []
            truncated = {'members': False, 'work_items': False, 'operations': False}
            for identifier in args['removed_partition_ids']:
                partition = self.store.one('''SELECT r.* FROM collaboration_rooms r
                    JOIN conversation_projects p ON p.room_id=r.id WHERE p.conversation_id=? AND r.id=?''',
                    (room['id'], identifier))
                if not partition:
                    raise DevError('ROOM_PROJECT_NOT_LINKED', '所选项目已不属于当前房间，请刷新后核对', 404)
                self.c.runtime.project(partition['project_id'], principal)
                projects.append({k: partition[k] for k in ('id', 'project_id', 'environment_id')})
                scopes = self.store.all('''SELECT s.member_id,s.project_id,s.environment_id,s.version,
                    s.lifetime_mode,s.expires_at FROM room_member_scopes s JOIN room_members m ON m.id=s.member_id
                    WHERE m.conversation_id=? AND s.project_id=? AND s.environment_id=? AND s.state='active'
                    ORDER BY s.member_id LIMIT 65''', (room['id'], partition['project_id'], partition['environment_id']))
                members.extend(scopes[:64])
                truncated['members'] |= len(scopes) > 64
                for item in self.project_effect_rows(room, partition, scan_limit=1000):
                    if item is None or len(items) >= 100:
                        truncated['work_items'] = True
                        break
                    items.append({k: item[k] for k in ('id', 'goal_id', 'project_id', 'state', 'version')}
                        | {'title': '此项目的原工作项', 'effect': 'block_new_steps_preserve_receipts'})
                for operation in self.project_effect_rows(room, partition, operations=True, scan_limit=1000):
                    if operation is None or len(operations) >= 100:
                        truncated['operations'] = True
                        break
                    operations.append({**operation, 'effect': 'request_cancel_then_verify_original_receipt'})
            for name, values, maximum in (('members', members, 64), ('work_items', items, 100), ('operations', operations, 100)):
                truncated[name] |= len(values) > maximum
            return {'conversation_id': room['id'], 'expected_version': room['version'], 'revision': room['revision'],
                'removed_projects': projects, 'read_only': True, 'grants_authority': False,
                'impact': {'members': members[:64], 'work_items': items[:100], 'operations': operations[:100],
                    'project_files_deleted': False, 'room_retained': True, 'unaffected_project_work_retained': True,
                    'new_steps_blocked': bool(projects), 'running_operations_confirmed_stopped': False},
                'truncated': truncated, 'limits': {'members': 64, 'work_items': 100, 'operations': 100,
                                                  'scanned_per_project_kind': 1000},
                'note': '提交时重新核对房间版本并禁止相关新步骤；运行操作只请求停止，实际结果沿原回执核对。'}

    def projects(self, raw, principal, *, _canonical=False):
        args = validate(contracts.RoomProjects, raw)
        with self.store.transaction():
            principal, room = self.owner(principal, args['conversation_id'])
            if room['room_protocol'] and not _canonical:
                raise DevError('ROOM_PERMISSION_SETTINGS_REQUIRED', '请在房间唯一权限设置页修改项目与权限', 409,
                    endpoint='/api/collaboration/rooms/' + room['id'] + '/permissions')
            for item in args['projects']:
                self.c.runtime.project(item['project'], principal)
            def perform():
                self.check_version(room, args['expected_version'])
                if not room['room_protocol']:
                    current = {(p['project_id'], p['environment_id']) for p in self.store.all('''
                        SELECT r.project_id,r.environment_id FROM collaboration_rooms r
                        JOIN conversation_projects p ON p.room_id=r.id WHERE p.conversation_id=?''', (room['id'],))}
                    requested = {(self.c.runtime.project(p['project'], principal)['id'], p['environment_id'])
                        for p in args['projects']}
                    if not requested <= current or any(a['project_scopes'] or a['lifetime'] for a in args['member_approvals']):
                        self.require_native(room)
                parts = self.partitions(principal, args['projects'])
                before = {p['room_id'] for p in self.store.all('SELECT room_id FROM conversation_projects WHERE conversation_id=?', (room['id'],))}
                after = {p['id'] for p in parts}
                removed = sorted(before - after)
                effects = self.detach_effects(room, removed, principal)
                for part_id in removed:
                    self.store.execute('DELETE FROM conversation_projects WHERE conversation_id=? AND room_id=?', (room['id'], part_id))
                    self.store.execute('INSERT OR IGNORE INTO conversation_partition_migrations(room_id) VALUES (?)', (part_id,))
                for part in parts:
                    self.store.execute('INSERT OR IGNORE INTO conversation_projects VALUES (?,?,?)', (room['id'], part['id'], self.c.clock()))
                for approval in args['member_approvals']:
                    previous=self.member(approval['member_id'], room['id'])
                    previous_digest=None
                    if previous['grant_id'] and previous['status']!='legacy':
                        try:
                            previous_digest=self.c.room_consumers.scope_digest(room,previous)
                        except DevError:
                            pass
                    delta=self.approve_scopes(principal, approval['member_id'], room['id'], approval['project_scopes'], approval['lifetime'])
                    bridge = getattr(self.c, 'room_work_bridge', None)
                    if bridge is not None and approval['project_scopes']:
                        bridge.ensure(self.member(approval['member_id']), principal)
                        member = self.member(approval['member_id'])
                        if member['grant_id']:
                            bridge.bind(member, self.principal(member))
                    if any(delta.values()) and previous['grant_id'] and previous['status']!='legacy':
                        self.c.room_consumers.scope_updated(self.member(approval['member_id']),previous_digest,
                            additions_only=bool(delta['added'] and not delta['changed'] and not delta['revoked']))
                self.store.execute('UPDATE conversation_rooms SET version=version+1 WHERE id=?', (room['id'],))
                self.change(room['id'], 'projects', room['id'], room['version'] + 1)
                return {'conversation_id': room['id'], 'impact': {'removed_partition_ids': removed,
                    'project_files_deleted': False, 'new_steps_blocked': bool(removed), **effects}}
            result = self.mutation(principal, 'room_projects', args, perform)
            return {**self.read({'conversation_id': room['id']}, principal), 'impact': result['impact']}

    def _invite(self, member, *, flow_id=None):
        code, now = 'CPR-' + secrets.token_urlsafe(32), self.c.clock()
        flow_id = flow_id or uuid.uuid4().hex
        self.store.execute('''INSERT INTO room_connect_flows
            (id,conversation_id,member_id,owner_user_id,invite_digest,invite_secret,expires_at,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(member_id) DO UPDATE SET
            invite_digest=excluded.invite_digest,invite_secret=excluded.invite_secret,expires_at=excluded.expires_at,
            redeemed_at=NULL,revoked_at=NULL,status='invited',version=room_connect_flows.version+1,
            last_error_code='',join_request_key='',join_request_digest='',updated_at=excluded.updated_at''',
            (flow_id, member['conversation_id'], member['id'], member['owner_user_id'], digest(code),
             self.store.encrypt(code), now + 1800, now, now))
        return self.store.one('SELECT * FROM room_connect_flows WHERE member_id=?', (member['id'],))

    def create_member(self, raw, principal):
        args = validate(contracts.MemberCreate, raw)
        with self.store.transaction():
            principal, room = self.owner(principal, args['conversation_id'])
            if room['room_protocol']:
                if not self.c.room_permissions.is_configured(room['id']):
                    raise DevError('ROOM_PERMISSION_SETTINGS_REQUIRED', '请先在房间权限设置页确认权限', 409)
                if 'project_scopes' in raw or 'lifetime' in raw:
                    raise DevError('ROOM_PERMISSION_SETTINGS_REQUIRED', '成员跟随房间权限，不能单独设置', 409)
            def perform():
                self.require_native(room)
                self.check_version(room, args['expected_version'])
                # Derive live permissions only for first execution. The replay
                # fingerprint describes the wire request, never changing policy.
                scopes = self.c.room_permissions.effective_scopes(room['id'])
                lifetime = None
                if self.store.one("SELECT COUNT(*) n FROM room_members WHERE conversation_id=? AND status!='removed'", (room['id'],))['n'] >= 32:
                    raise DevError('ROOM_MEMBER_LIMIT', '房间最多可添加 32 个成员', 409)
                identifier, now = uuid.uuid4().hex, self.c.clock()
                self.store.execute('''INSERT INTO room_members(id,conversation_id,owner_user_id,name,created_at,updated_at)
                    VALUES (?,?,?,?,?,?)''', (identifier, room['id'], principal.user_id, args['name'], now, now))
                if args['reuse_member_id']:
                    self.c.room_connections.attach_source(principal, self.member(identifier),
                        args['reuse_member_id'], args['reuse_binding_generation'])
                self.approve_scopes(principal, identifier, room['id'], scopes, lifetime)
                member = self.member(identifier)
                bridge = getattr(self.c, 'room_work_bridge', None)
                if bridge is not None and scopes:
                    bridge.ensure(member, principal)
                    member = self.member(identifier)
                flow = self._invite(member)
                if args.get('keep_connected'):
                    self.renewals.set(member, principal, True)
                if not room['lead_member_id']:
                    self.store.execute('UPDATE conversation_rooms SET lead_member_id=? WHERE id=?', (identifier, room['id']))
                    fence_follow_poll(self.c, room['id'])
                    recover_waiting(self.c, conversation_id=room['id'])
                self.store.execute('UPDATE conversation_rooms SET version=version+1 WHERE id=?', (room['id'],))
                self.change(room['id'], 'member', identifier)
                return {'flow_id': flow['id']}
            result = self.mutation(principal, 'room_member_create', args, perform)
            return {**self.connect_read(result, principal), **self.read({'conversation_id': room['id']}, principal)}

    def flow_view(self, flow, member):
        from shared.public_collaboration import checked_request
        room = self.store.one('SELECT state FROM conversation_rooms WHERE id=?', (member['conversation_id'],))
        candidate = self.pending.current(member['id'])
        live_member = member
        if candidate:
            member = self.pending.facade(member, candidate)
            flow = {**flow, **{key: candidate[key] for key in ('expires_at', 'redeemed_at', 'revoked_at')}}
        state = self.state(member)
        if candidate and candidate['prepare_expires_at'] <= self.c.clock():
            state = 'degraded' if candidate['redeemed_at'] else 'invited'
        expired = not flow['redeemed_at'] and (flow['expires_at'] is None or flow['expires_at'] <= self.c.clock())
        display = self.display(state)
        if member['status'] == 'legacy':
            display = {'title': '继续原成员接入', 'primary_action': 'migrate'}
        if expired and state == 'invited':
            display = {'title': '加入说明已过期', 'primary_action': 'refresh_invite'}
        result = {'id': flow['id'], 'flow_id': flow['id'], 'member_id': member['id'], 'kind': 'dot',
            'conversation_id': member['conversation_id'], 'stage': state, 'state': state, 'version': flow['version'],
            'revision': flow['version'], 'display': display, 'evidence': self.member_view(member)['evidence'],
            'last_error_code': flow['last_error_code'], 'invite_expired': expired,
            'pending_binding': bool(candidate), 'current_binding_generation': live_member['binding_generation'],
            'subscription_renewal': self.renewals.view(live_member)}
        if room and room['state'] != 'active':
            result['display'] = {'title': '房间已暂停，请先继续房间' if room['state'] == 'paused' else '房间已归档',
                'primary_action': 'view_details' if room['state'] == 'paused' else None}
        if candidate:
            result['candidate_binding_generation'] = candidate['binding_generation']
            result['current_connection_preserved'] = True
            if candidate['prepare_expires_at'] <= self.c.clock() and candidate['redeemed_at']:
                result['display'] = {'title': '候选连接检查已过期，原连接保持有效', 'primary_action': 'rebind'}
        if state != 'removed' and room and room['state'] == 'active':
            action = 'migrate' if member['status'] == 'legacy' else 'refresh_invite' if expired and state == 'invited' else 'resume'
            if candidate and candidate['prepare_expires_at'] <= self.c.clock() and candidate['redeemed_at']:
                action = 'rebind'
            result['next_request'] = checked_request('collaboration_room_connect_resume', {
                'conversation_id': flow['conversation_id'], 'flow_id': flow['id'], 'action': action,
                'expected_version': flow['version'], 'idempotency_key': f"{flow['id']}:{action}:{flow['version']}"})
        return result

    def subscription_request(self, member):
        return {'name': ROOM_INBOX_EVENT, 'arguments': {'owner_user_id': member['owner_user_id'],
            'conversation_id': member['conversation_id'], 'member_id': member['id'],
            'binding_generation': member['binding_generation']}}

    def connect_read(self, raw, principal):
        args = validate(contracts.ConnectRead, raw)
        flow = self.store.one('SELECT * FROM room_connect_flows WHERE id=?', (args['flow_id'],))
        if not flow or (args.get('conversation_id') and args['conversation_id'] != flow['conversation_id']):
            raise DevError('CONNECT_FLOW_NOT_FOUND', '没有这个接入流程', 404)
        principal, _ = self.owner(principal, flow['conversation_id'], include_archived=True)
        member = self.member(flow['member_id'])
        result = {'flow': self.flow_view(flow, member), 'member': self.member_view(member)}
        candidate = self.pending.current(member['id'])
        if candidate:
            member = self.pending.facade(member, candidate)
            flow = {**flow, **{key: candidate[key] for key in
                ('expires_at', 'redeemed_at', 'revoked_at', 'invite_secret')}}
        if not flow['redeemed_at'] and not flow['revoked_at'] and flow['expires_at'] is not None and flow['expires_at'] > self.c.clock() and member['status'] != 'removed':
            code = self.store.decrypt(flow['invite_secret'])
            result['join_instructions'] = ('@CodePier 请加入房间协作。调用 collaboration，参数：' +
                canonical({'action': 'join', 'code': code, 'idempotency_key': flow['id'] + ':join'}) +
                '。这是兼容旧工具目录的加入入口，与 room_connect_join 使用同一身份校验。'
                '若宿主目录缺少 room_probe_read、room_probe_reply 或 room_inbox，先刷新 CodePier 工具目录（Rescan），不要换成旧任务协议。'
                '按返回的 subscription_request 查找并使用宿主真实原生订阅能力完成连接；不要仅因 CodePier 工具列表没有订阅工具就断定宿主不支持。保存 connection_proof，'
                '它只证明当前成员身份，不授予执行权限。完成后按返回步骤读取并回复连接检查。'
                '宿主续订模式下，支持后台续订的宿主保存原订阅、签名游标和 refreshBefore，提前续订；'
                '由房间托管时保持原回调与签名，禁止伪造 callback。')
        elif member['grant_id'] and member['status'] not in {'removed', 'legacy'}:
            result['subscription_request'] = self.subscription_request(member)
            result['resume_instructions'] = ('@CodePier 请在原 dot 恢复同一个房间成员，不新建成员或定时任务。'
                f"conversation_id={member['conversation_id']}，member_id={member['id']}，binding_generation={member['binding_generation']}。"
                '保留原 connection_proof；按宿主原生流程恢复或续订这条订阅：' + canonical(self.subscription_request(member)) +
                '。保持原回调与签名，能取到原签名游标时一并提交。原连接证据仍有效时直接处理待办；'
                '收到需要验证的通知时，调用 collaboration(action=room_probe_read)，带上述身份、'
                'connection_proof、wake_ticket 和新的 idempotency_key，再用 room_probe_reply 回复返回的 probe_nonce。'
                '如果原成员证明已丢失，请报告 ROOM_MEMBER_PROOF_REQUIRED，由房主在此成员里更换绑定，不能借用其他 dot 的证明。')
        return result

    def connect_action(self, raw, principal):
        args = validate(contracts.ConnectAction, raw)
        with self.store.transaction():
            flow = self.store.one('SELECT * FROM room_connect_flows WHERE id=?', (args['flow_id'],))
            if not flow or (args.get('conversation_id') and args['conversation_id'] != flow['conversation_id']):
                raise DevError('CONNECT_FLOW_NOT_FOUND', '没有这个接入流程', 404)
            principal, room = self.owner(principal, flow['conversation_id'])
            member = self.member(flow['member_id'])
            def perform():
                self.check_version(flow, args['expected_version'])
                action = args['action']
                if member['status'] == 'removed':
                    raise DevError('ROOM_MEMBER_REMOVED', '已移除的成员不能自动恢复', 410)
                if action == 'set_renewal':
                    if 'keep_connected' not in args:
                        raise DevError('INVALID_ARGUMENTS', '请选择是否由房间保持原连接', 422)
                    self.require_native(room)
                    self.renewals.set(member, principal, args['keep_connected'])
                    self.store.execute('UPDATE room_connect_flows SET version=version+1,updated_at=? WHERE id=?',
                        (self.c.clock(), flow['id']))
                elif action == 'refresh_invite':
                    if not self.pending.refresh(member):
                        if flow['redeemed_at']:
                            raise DevError('INVITE_ALREADY_REDEEMED', '成员已登记，请继续原连接', 409)
                        self._invite(member, flow_id=flow['id'])
                elif action in {'rebind', 'migrate'}:
                    if member['status'] == 'paused':
                        raise DevError('ROOM_MEMBER_INACTIVE', '先恢复原成员，再准备更换连接', 409)
                    self.pending.prepare(member, principal)
                elif action == 'resume' and self.pending.current(member['id']):
                    candidate = self.pending.current(member['id'])
                    binding = self.pending.by_generation(member, candidate['binding_generation'])
                    if binding['subscription_id']:
                        self._probe_subscription(binding)
                        self.c.room_consumers.connection_probe(binding)
                    self.store.execute('UPDATE room_connect_flows SET version=version+1,updated_at=? WHERE id=?',
                        (self.c.clock(), flow['id']))
                else:
                    if action in {'pause', 'remove'}:
                        self.pending.cancel(member['id'])
                        self.stop_member_work(member, 'ROOM_MEMBER_' + action.upper(), revoke=action == 'remove')
                    elif action == 'resume':
                        if room['room_protocol']:
                            self.resume_member_policies(member)
                    legacy = bool(member['legacy_dot_id'] and member['grant_id'] and not member['connection_proof_hash'])
                    status = ('removed' if action == 'remove' else 'paused' if action == 'pause'
                        else 'legacy' if legacy else 'bound' if member['grant_id'] else 'invited')
                    self.store.execute('UPDATE room_members SET status=?,version=version+1,updated_at=? WHERE id=?', (status, self.c.clock(), member['id']))
                    self.store.execute('UPDATE room_connect_flows SET status=?,version=version+1,updated_at=? WHERE id=?', (status, self.c.clock(), flow['id']))
                    if action in {'pause', 'remove'}:
                        self.store.execute("UPDATE mcp_event_subscriptions SET state=?,version=version+1 WHERE id=?", ('unsubscribed' if action == 'remove' else 'paused', member['subscription_id']))
                    elif action == 'resume':
                        self.store.execute("UPDATE mcp_event_subscriptions SET state='active',version=version+1 WHERE id=? AND state='paused'", (member['subscription_id'],))
                        bridge = getattr(self.c, 'room_work_bridge', None)
                        if bridge is not None and room['room_protocol']:
                            resumed = self.member(member['id'])
                            bridge.ensure(resumed, principal)
                            if resumed['grant_id'] and resumed['status'] != 'legacy':
                                bridge.bind(self.member(member['id']), self.principal(resumed))
                    if action == 'remove':
                        self.store.execute("UPDATE room_member_scopes SET state='revoked',version=version+1 WHERE member_id=?", (member['id'],))
                        self.store.execute('UPDATE room_connect_flows SET revoked_at=? WHERE id=?', (self.c.clock(), flow['id']))
                        self.store.execute("UPDATE conversation_rooms SET lead_member_id='' WHERE id=? AND lead_member_id=?", (member['conversation_id'], member['id']))
                        if room['lead_member_id']==member['id']:
                            fence_follow_poll(self.c, room['id'])
                            recover_waiting(self.c, conversation_id=room['id'])
                    elif action == 'resume':
                        recover_waiting(self.c, conversation_id=room['id'])
                        self.recover_paused_work(conversation_id=room['id'])
                        self.c.room_consumers.recover_connection(self.member(member['id']))
                self.change(member['conversation_id'], 'member', member['id'], member['version'] + 1)
                return {'flow_id': flow['id']}
            return self.connect_read(self.mutation(principal, 'room_connect_action', args, perform), principal)

    def join(self, raw, principal):
        args = validate(contracts.ConnectJoin, raw)
        with self.store.transaction():
            principal = refresh_principal(self.store, principal)
            self.c.notification_reader(principal)
            candidate = self.store.one('SELECT * FROM room_pending_bindings WHERE invite_digest=?', (digest(args['invite_code']),))
            if candidate:
                current = self.member(candidate['member_id'])
                if current['owner_user_id'] != principal.user_id:
                    raise DevError('ROOM_INVITE_INVALID', '加入说明不属于当前账号或已失效', 403)
                room = self.c.conversations.object(principal, current['conversation_id'])
                if current['status'] in {'paused', 'removed'} or room['state'] != 'active':
                    raise DevError('ROOM_MEMBER_INACTIVE', '此成员或房间已暂停', 409)
                connections = getattr(self.c, 'room_connections', None)
                if connections is not None:
                    connections.guard_join(self.pending.facade(current, candidate), principal)
                member = self.pending.join(candidate, current, principal, args)
                return self.join_result(member, room)
            flow = self.store.one('SELECT * FROM room_connect_flows WHERE invite_digest=?', (digest(args['invite_code']),))
            if not flow or flow['owner_user_id'] != principal.user_id:
                raise DevError('ROOM_INVITE_INVALID', '加入说明不属于当前账号或已失效', 403)
            room = self.c.conversations.object(principal, flow['conversation_id'])
            member = self.member(flow['member_id'])
            if member['status'] in {'paused', 'removed'} or room['state'] != 'active' or flow['revoked_at']:
                raise DevError('ROOM_MEMBER_INACTIVE', '此成员或房间已暂停', 409)
            connections = getattr(self.c, 'room_connections', None)
            if connections is not None:
                connections.guard_join(member, principal)
            fingerprint = digest([principal.grant_id, args['idempotency_key'], flow['invite_digest']])
            if flow['redeemed_at']:
                if member['grant_id'] != principal.grant_id or flow['join_request_digest'] != fingerprint:
                    raise DevError('ROOM_INVITE_REDEEMED', '已接入的成员只能恢复原连接，换宿主需在面板更换绑定', 409)
            else:
                if flow['expires_at'] <= self.c.clock():
                    raise DevError('ROOM_INVITE_EXPIRED', '加入说明已过期，请在原成员中刷新', 410)
                proof = secrets.token_urlsafe(32)
                saved = asdict(principal)
                saved.update(scopes=[], session_hash='', token_hash='')
                self.store.execute('''UPDATE room_members SET status='bound',grant_id=?,principal=?,
                    connection_proof_hash=?,connection_proof_secret=?,version=version+1,updated_at=? WHERE id=?''',
                    (principal.grant_id, canonical(saved), digest(proof), self.store.encrypt(proof), self.c.clock(), member['id']))
                self.store.execute("UPDATE room_connect_flows SET status='waiting_host',redeemed_at=?,join_request_key=?,join_request_digest=?,version=version+1,updated_at=? WHERE id=?", (self.c.clock(), args['idempotency_key'], fingerprint, self.c.clock(), flow['id']))
                self.change(room['id'], 'member', member['id'], member['version'] + 1)
                member = self.member(member['id'])
                bridge = getattr(self.c, 'room_work_bridge', None)
                if bridge is not None and member['legacy_dot_id']:
                    bridge.bind(member, principal)
                    member = self.member(member['id'])
            # Credentials only appear in this authenticated, idempotent exchange;
            # state reads, flow projections and audit entries never include them.
            return self.join_result(member, room)

    def join_result(self, member, room):
        return {'member': self.member_view(member), 'conversation_id': room['id'],
            'member_id': member['id'], 'binding_generation': member['binding_generation'],
            'connection_proof': self.store.decrypt(member['connection_proof_secret']),
            'subscription_request': self.subscription_request(member), 'grants_authority': False,
            'pending_binding': bool(member.get('_pending_binding')),
            'subscription_renewal': self.renewals.view(member),
            'required_host_actions': {'collaboration': ['room_probe_read', 'room_probe_reply',
                'room_consumer_begin', 'room_consumer_yield', 'room_reply'],
                'collaboration_query': ['room_inbox']},
            'next_step': '加入或重放这个接口本身不代表宿主订阅或往返通信成功；以 member 当前状态和验证证据为准。若 required_host_actions 中的操作未出现在宿主工具目录，先刷新 CodePier 工具目录（Rescan），保持原成员和 connection_proof，不重复兑换或改用旧任务协议。'
                '查找宿主真实原生订阅能力及事件源，使用实际发现的连接器标识注册 subscription_request；不要仅因 CodePier 工具列表没有订阅工具就断定宿主不支持。'
                '有原生订阅、但无后台续期能力时，可由房主在房间开通原连接托管续期。'
                '若缺少原生订阅，报告该连接阻塞，保留待办，不创建定时轮询、不伪造 callback。'
                '收到当前绑定通知中的 wake_ticket 后，连同当前成员 connection_proof 调用 room_probe_read，再将返回的 nonce 通过 room_probe_reply 回复。'}

    def event_scope(self, principal, filters, *, stopping=False):
        if principal.user_id != filters['owner_user_id']:
            raise DevError('ROOM_MEMBER_DENIED', '通知所有者与当前账号不一致', 403)
        actor, room, _ = self.member_scope(principal, filters['conversation_id'], filters['member_id'],
            filters['binding_generation'], stopping=stopping, pending=True)
        return actor, room

    def event_subscription_guard(self, principal, filters, delivery=None):
        _, _, member = self.member_scope(principal, filters['conversation_id'], filters['member_id'], filters['binding_generation'], pending=True)
        connections = getattr(self.c, 'room_connections', None)
        if connections is not None:
            connections.guard_route(member, delivery)
        if delivery:
            endpoint = digest(delivery['url'])
            routes = self.store.all('''SELECT m.id,s.secret FROM room_members m JOIN mcp_event_subscriptions s ON s.id=m.subscription_id
                    WHERE m.id!=? AND m.owner_user_id=? AND m.endpoint_digest=? AND m.status!='removed'
                    AND s.state IN ('active','paused') AND s.expires_at>?''', (member['id'], principal.user_id, endpoint, self.c.clock()))
            routes += self.store.all('''SELECT m.id,s.secret FROM room_pending_bindings p JOIN room_members m ON m.id=p.member_id
                    JOIN mcp_event_subscriptions s ON s.id=p.subscription_id WHERE m.id!=? AND m.owner_user_id=?
                    AND p.endpoint_digest=? AND p.status IN ('bound','verifying') AND p.prepare_expires_at>?
                    AND s.state IN ('active','paused') AND s.expires_at>?''',
                    (member['id'], principal.user_id, endpoint, self.c.clock(), self.c.clock()))
            for row in routes:
                stored = json.loads(self.store.decrypt(row['secret']))
                if delivery.get('secret') in {stored.get('current'), stored.get('retiring')}:
                    if connections is not None and connections.allows_shared_route(member, self.member(row['id'])):
                        continue
                    raise DevError('ROOM_ROUTE_SHARED', '此宿主通知身份已属于另一成员，请在目标 dot 创建独立订阅', 409)
            if member['endpoint_digest'] and member['endpoint_digest'] != endpoint:
                raise DevError('ROOM_HOST_CHANGED', '宿主通知路线改变，请在面板显式更换绑定', 409)
        return member

    def event_subscription_attached(self, principal, filters, subscription, *, reset_verification=False, restored=False, ttl=86400):
        member = self.event_subscription_guard(principal, filters)
        stored = json.loads(self.store.decrypt(subscription['secret']))
        changed = (member['subscription_id'] != subscription['id'] or reset_verification or
            (not member['probe_replied_at'] and member['probe_created_at'] is not None and member['probe_created_at'] + 1800 <= self.c.clock()))
        table, identifier = self.binding_storage(member)
        self.store.execute(f'''UPDATE {table} SET subscription_id=?,subscription_verified_at=?,endpoint_digest=?,
            credential_digest=?,version=version+1,updated_at=? WHERE id=?''',
            (subscription['id'], self.c.clock(), digest(stored['url']), subscription['key_digest'], self.c.clock(), identifier))
        if changed or not member['probe_nonce_hash']:
            nonce = secrets.token_urlsafe(32)
            self.store.execute(f'''UPDATE {table} SET probe_nonce_hash=?,probe_nonce_secret=?,probe_created_at=?,
                probe_read_at=NULL,probe_replied_at=NULL WHERE id=?''', (digest(nonce), self.store.encrypt(nonce), self.c.clock(), identifier))
        if member.get('_pending_binding'):
            self.store.execute("UPDATE room_pending_bindings SET status='verifying' WHERE id=?", (identifier,))
        if (changed or not member['probe_nonce_hash'] or not member['probe_replied_at']) and (
                member.get('_pending_binding') or not self.pending.current(member['id'])):
            self.store.execute("UPDATE room_connect_flows SET status='verifying',version=version+1,updated_at=? WHERE member_id=?", (self.c.clock(), member['id']))
        self.change(member['conversation_id'], 'member', member['id'], member['version'] + 1)
        current = self.event_member(member['id'], member['conversation_id'], member['binding_generation'])
        self.renewals.attach(current, subscription, ttl)
        consumers = getattr(self.c, 'room_consumers', None)
        if consumers is not None:
            if changed or not member['probe_nonce_hash']:
                consumers.connection_probe(current)
            elif restored and not current.get('_pending_binding'):
                if current['probe_replied_at']:
                    consumers.recover_connection(current)
                else:
                    consumers.connection_probe(current)

    @staticmethod
    def binding_storage(member):
        return (('room_pending_bindings', member['_pending_binding_id'])
            if member.get('_pending_binding') else ('room_members', member['id']))

    def event_authorize(self, subscription, filters, payload=None):
        member = self.event_member(filters['member_id'], filters['conversation_id'], filters['binding_generation'])
        actor = self.principal(member)
        self.event_scope(actor, filters)
        if member['subscription_id'] != subscription['id'] or subscription['grant_id'] != actor.grant_id:
            raise DevError('ROOM_SUBSCRIPTION_CHANGED', '成员订阅已改变', 409)
        if payload and any(payload.get(k) != filters[k] for k in ('conversation_id', 'member_id', 'binding_generation', 'owner_user_id')):
            raise DevError('ROOM_EVENT_SCOPE_CHANGED', '通知不属于当前成员绑定', 403)
        return actor

    def event_test(self, subscription):
        filters = json.loads(subscription['arguments'])
        self.event_authorize(subscription, filters)
        if subscription['state'] != 'active' or subscription['expires_at'] <= self.c.clock():
            raise DevError('SUBSCRIPTION_INACTIVE', '原订阅当前不可用', 409)
        member = self.event_member(filters['member_id'], filters['conversation_id'], filters['binding_generation'])
        payload = {**filters, 'schema_version': 1, 'dirty_revision': member['dirty_revision'],
                   'test': True, 'test_subscription_id': subscription['id']}
        event_id = 'room_probe_' + uuid.uuid4().hex
        self.store.execute('''INSERT INTO mcp_event_outbox
            (id,room_id,conversation_id,name,object_id,object_version,target_grant_id,data,created)
            VALUES (?,NULL,?,?,?,?,?,?,?)''', (event_id, filters['conversation_id'], ROOM_INBOX_EVENT,
            event_id, 1, subscription['grant_id'], canonical(payload), self.c.clock()))
        loops = getattr(self.c, 'loops', None)
        if loops is not None:
            self.store.after_commit(loops.wake_events)
        return {'id': subscription['id'], 'test_event_id': event_id, 'state': 'test_queued',
                'business_task_created': False, 'chat_response_verified': False}

    def probe_read(self, raw, principal):
        args = validate(contracts.ConnectProbeRead, raw)
        with self.store.transaction():
            actor, room, member = self.member_scope(principal, args['conversation_id'], args['member_id'],
                args['binding_generation'], args['connection_proof'], require_proof=True, pending=True)
            self._probe_subscription(member)
            if not member['probe_nonce_hash']:
                raise DevError('ROOM_PROBE_NOT_READY', '先完成当前成员的宿主订阅', 409)
            consumers = getattr(self.c, 'room_consumers', None)
            if consumers is None:
                raise DevError('ROOM_CONSUMER_UNAVAILABLE', '当前服务尚未启用房间消费者', 409)
            consumers.verify_probe_ticket(args, actor, member)
            table, identifier = self.binding_storage(member)
            self.store.execute(f'UPDATE {table} SET probe_read_at=COALESCE(probe_read_at,?) WHERE id=?', (self.c.clock(), identifier))
            return {'member_id': member['id'], 'conversation_id': room['id'], 'binding_generation': member['binding_generation'],
                'probe_nonce': self.store.decrypt(member['probe_nonce_secret']), 'purpose': 'connection_check',
                'business_task_created': False, 'grants_authority': False}

    def _probe_subscription(self, member):
        sub = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (member['subscription_id'],))
        if not sub or sub['state'] != 'active' or sub['expires_at'] <= self.c.clock() or not member['subscription_verified_at']:
            raise DevError('ROOM_SUBSCRIPTION_REQUIRED', '请先完成当前成员的原生订阅', 409)
        self.event_authorize(sub, json.loads(sub['arguments']))
        return sub

    def probe_reply(self, raw, principal):
        args = validate(contracts.ConnectProbeReply, raw)
        with self.store.transaction():
            actor, room, member = self.member_scope(principal, args['conversation_id'], args['member_id'],
                args['binding_generation'], args['connection_proof'], require_proof=True, pending=True)
            self._probe_subscription(member)
            if (not member['probe_read_at'] or not member['probe_nonce_hash'] or
                    not hmac.compare_digest(digest(args['probe_nonce']), member['probe_nonce_hash'])):
                raise DevError('ROOM_PROBE_MISMATCH', '需要先读取并回复当前绑定的连接检查', 409)
            if member['probe_created_at'] + 1800 <= self.c.clock() and not member['probe_replied_at']:
                raise DevError('ROOM_PROBE_EXPIRED', '连接检查已过期，请续订原订阅后重新验证', 410)
            if member.get('_pending_binding'):
                active = self.pending.activate(member, actor)
                return {**self.probe_result(active, args), 'binding_activated': True}
            if not member['probe_replied_at']:
                self.store.execute('UPDATE room_members SET probe_replied_at=?,version=version+1,updated_at=? WHERE id=?', (self.c.clock(), self.c.clock(), member['id']))
                if not self.pending.current(member['id']):
                    self.store.execute("UPDATE room_connect_flows SET status='ready',version=version+1,updated_at=? WHERE member_id=?", (self.c.clock(), member['id']))
                self.change(room['id'], 'member', member['id'], member['version'] + 1)
            if not member['probe_replied_at']:
                recover_waiting(self.c, conversation_id=room['id'])
            return self.probe_result(self.member(member['id']), args)

    def probe_result(self, member, verified_args):
        from shared.public_collaboration import checked_request
        # This authenticated mutation echoes only the caller's already-verified
        # proof into its next request. Passive room/flow reads still reveal none.
        identity = {key: verified_args[key] for key in
            ('conversation_id', 'member_id', 'binding_generation', 'connection_proof')}
        room = self.store.one('SELECT room_protocol FROM conversation_rooms WHERE id=?', (member['conversation_id'],))
        if not room['room_protocol']:
            return {'member': self.member_view(member), 'business_task_created': False, 'consumer_lease_created': False,
                'room_migration_required': True,
                'next_step': '当前成员的真实连接验证已完成。房间仍只读；等待房主在全部原连接处理完毕后明确激活房间。不要领取业务输入、创建任务或轮询。'}
        return {'member': self.member_view(member), 'business_task_created': False, 'consumer_lease_created': False,
            'next_request': checked_request('collaboration_room_inbox', identity),
            'next_step': '连接检查已完成。用 next_request 读取原收件箱。只在有当前可处理输入时，'
                '使用本次或之后实际回调中的 wake_ticket、当前成员身份和 connection_proof 调用 '
                'room_consumer_begin，保存返回的 consumer_session_id、consumer_fence 和 consumer_proof。'
                '仅当宿主工具协议声明 include_inbox 时，可传 true 读取第一页；缺少或读取失败时调用 room_inbox。'
                '使用本回合 inbox_revision 处理原输入，回复使用 room_reply，明确工作使用 '
                'room_work_start。已核验本人且在既有范围内的明确任务直接开始；范围不足时用 room_reply 说明原因。'
                '把阶段与最终答复关联原输入提交 room_reply，ChatGPT 内的文字不会回传微信。完成后调用 '
                'room_consumer_yield，携带 expected_inbox_revision；more=true 时继续读取原待办，'
                'more=false 时等待新通知。没有输入时不创建任务、租约或定时轮询。'}

    def changes(self, raw, principal):
        args = validate(contracts.RoomChanges, raw)
        principal, room = self.owner(principal, args['conversation_id'], include_archived=True)
        projects = self.c.conversations.visible(principal, room)
        binding = digest(['room-changes', room['id'], principal.user_id, [p['id'] for p in projects]])
        after = read_cursor(self.c.secret, binding, args['cursor']) if args['cursor'] else 0
        rows = self.store.all('SELECT * FROM room_changes WHERE conversation_id=? AND sequence>? ORDER BY sequence LIMIT ?',
                              (room['id'], after, args['limit'] + 1))
        selected = rows[:args['limit']]
        return {'items': selected, 'revision': room['revision'], 'reset_required': False,
            'next_cursor': sign_cursor(self.c.secret, binding, selected[-1]['sequence'] if selected else after),
            'has_more': len(rows) > args['limit']}

    def diagnostics(self, raw, principal):
        args = validate(contracts.RoomRead, raw)
        from hub.collaboration.room_diagnostics import projection
        with self.store.transaction(immediate=False):
            principal, room = self.owner(principal, args['conversation_id'], include_archived=True)
            return projection(self.c, room, principal)

    def invoke(self, action, raw, principal):
        handlers = {'room_list': self.listing, 'room_view': self.read, 'room_create': self.create,
            'room_migrate': self.migrate_room,
            'room_update': self.update, 'room_projects': self.projects, 'room_project_impact': self.project_impact,
            'room_member_create': self.create_member,
            'room_connect': self.connect_read, 'room_connect_resume': self.connect_action,
            'room_connect_join': self.join, 'room_probe_read': self.probe_read, 'room_probe_reply': self.probe_reply,
            'room_changes': self.changes, 'room_diagnostics': self.diagnostics}
        if action not in handlers:
            raise DevError('ROOM_ACTION_UNKNOWN', '未知的房间操作', 404)
        return handlers[action](raw, principal)
