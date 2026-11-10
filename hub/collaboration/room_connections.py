"""Explicit reuse of a verified host, never reuse of room authority or secrets.

The source binding remains independently verified. Every destination member has
its own invitation, proof, scope, subscription filters and consumer watermarks.
An owner-selected relationship permits a shared native host route only while
its recorded source generation and current grant still match.
"""
from __future__ import annotations

from pydantic import Field

from hub.principal import refresh_principal
from hub.collaboration.common import digest, read_cursor, sign_cursor, validate
from hub.collaboration.room_host_connections import HostConnections, migrate as migrate_host_connections
from hub.collaboration.room_host_migration import record_shadow
from shared.collaboration_contracts import Model
from shared.util import DevError


class ConnectionList(Model):
    cursor: str = Field(default='', max_length=2048)
    limit: int = Field(default=40, ge=1, le=100)


def migrate(db):
    columns = {row[1] for row in db.execute('PRAGMA table_info(room_members)')}
    for name, declaration in (
        ('connection_source_member_id', "TEXT NOT NULL DEFAULT ''"),
        ('connection_source_generation', 'INTEGER NOT NULL DEFAULT 0'),
    ):
        if name not in columns:
            db.execute(f'ALTER TABLE room_members ADD COLUMN {name} {declaration}')
    db.execute('''CREATE INDEX IF NOT EXISTS room_connection_owner
        ON room_members(owner_user_id,connection_source_member_id,created_at,id)''')
    migrate_host_connections(db)
    from hub.collaboration.room_host_migration import migrate as migrate_host_backfill
    from hub.collaboration.room_host_proofs import migrate as migrate_host_proofs
    migrate_host_backfill(db)
    migrate_host_proofs(db)


class RoomConnections(HostConnections):
    def conversation(self, member):
        room = self.store.one('SELECT * FROM conversation_rooms WHERE id=?', (member['conversation_id'],))
        if not room or room['owner_user_id'] != member['owner_user_id']:
            raise DevError('ROOM_CONNECTION_UNAVAILABLE', '原连接已不可用，请重新核对成员', 409)
        return room

    def lineage(self, member, *, require_leaf_ready=False):
        """Validate every saved generation; a shared grant alone proves nothing."""
        seen, current = set(), member
        first_room = self.conversation(member)
        expected_grant = member.get('grant_id') or ''
        while True:
            if current['id'] in seen or len(seen) >= 32:
                raise DevError('ROOM_CONNECTION_INVALID', '原连接关系无法核对', 409)
            seen.add(current['id'])
            room = self.conversation(current)
            if current.get('host_connection_id'):
                self.require_member(room, current)
            if (current['owner_user_id'] != member['owner_user_id']
                    or room['space_id'] != first_room['space_id']):
                raise DevError('ROOM_CONNECTION_DENIED', '不能复用其他账号或工作区的连接', 403)
            if current['id'] != member['id'] or require_leaf_ready:
                if self.c.rooms.state(current, check_connection=False) not in {'ready_chat', 'ready_work'}:
                    reason = ('source_archived' if room['state'] == 'archived' else
                              'source_paused' if room['state'] == 'paused' or current['status'] == 'paused' else
                              'source_removed' if current['status'] == 'removed' else 'source_unavailable')
                    raise DevError('ROOM_CONNECTION_UNAVAILABLE', '原 dot 需要先恢复已验证连接', 409,
                                   source_reason=reason)
            if expected_grant and current.get('grant_id') and current['grant_id'] != expected_grant:
                raise DevError('ROOM_CONNECTION_GRANT_CHANGED', '原连接身份已变化，请重新确认', 409)
            expected_grant = expected_grant or current.get('grant_id', '')
            source_id = current.get('connection_source_member_id', '')
            if not source_id:
                return current
            source = self.c.rooms.member(source_id)
            if source['binding_generation'] != current.get('connection_source_generation'):
                raise DevError('ROOM_CONNECTION_CHANGED', '原 dot 已更换连接，请重新选择并确认', 409)
            current = source

    def listing(self, raw, principal):
        args = validate(ConnectionList, raw)
        principal = refresh_principal(self.store, principal)
        self.c.guard()
        self.c.owner(principal)
        binding = digest(['room-connections-v1', principal.space_id, principal.user_id])
        offset = read_cursor(self.c.secret, binding, args['cursor']) if args['cursor'] else 0
        rows = self.store.all('''SELECT m.*,r.title AS source_room_title FROM room_members m
            JOIN conversation_rooms r ON r.id=m.conversation_id
            WHERE m.owner_user_id=? AND r.space_id=? AND r.state='active'
            AND m.connection_source_member_id='' AND m.probe_replied_at IS NOT NULL
            AND m.status NOT IN ('removed','paused','legacy')
            ORDER BY m.created_at,m.id LIMIT ? OFFSET ?''',
            (principal.user_id, principal.space_id, args['limit'] + 1, offset))
        items = []
        for member in rows[:args['limit']]:
            try:
                self.lineage(member, require_leaf_ready=True)
            except DevError:
                continue
            items.append({'id': member['id'], 'member_id': member['id'],
                'name': member['name'], 'conversation_id': member['conversation_id'],
                'room_title': member['source_room_title'], 'binding_generation': member['binding_generation'],
                'verified_at': member['probe_replied_at'], 'state': self.c.rooms.state(member),
                'display': {'title': '已验证的 dot 连接', 'primary_action': 'reuse'}})
        return {'items': items, 'next_cursor': sign_cursor(self.c.secret, binding, offset + args['limit'])
                if len(rows) > args['limit'] else None}

    def attach_source(self, principal, target_member, reuse_member_id, expected_generation=None):
        if not reuse_member_id:
            return None
        target = self.c.rooms.member(target_member) if isinstance(target_member, str) else target_member
        principal, target_room = self.c.rooms.owner(principal, target['conversation_id'])
        source = self.c.rooms.member(reuse_member_id)
        source_room = self.conversation(source)
        if source['owner_user_id'] != principal.user_id or source_room['space_id'] != principal.space_id:
            raise DevError('ROOM_CONNECTION_DENIED', '此连接不属于当前账号与工作区', 403)
        if expected_generation is not None and source['binding_generation'] != expected_generation:
            raise DevError('ROOM_CONNECTION_CHANGED', '原 dot 已更换连接，请重新选择并确认', 409)
        canonical_source = self.lineage(source, require_leaf_ready=True)
        if source['conversation_id'] == target_room['id']:
            raise DevError('ROOM_CONNECTION_ALREADY_PRESENT', '这个 dot 已在当前房间，请继续原成员',
                           409, member_id=source['id'])
        for candidate in self.store.all("SELECT * FROM room_members WHERE conversation_id=? AND id!=? AND status!='removed'",
                                        (target_room['id'], target['id'])):
            try:
                canonical_candidate = self.lineage(candidate)
            except DevError:
                continue
            if canonical_candidate['id'] == canonical_source['id']:
                raise DevError('ROOM_CONNECTION_ALREADY_PRESENT', '这个 dot 已有当前房间的接入流程',
                               409, member_id=candidate['id'])
        self.store.execute('''UPDATE room_members SET connection_source_member_id=?,
            connection_source_generation=? WHERE id=?''',
            (source['id'], source['binding_generation'], target['id']))
        return source

    def guard_join(self, member, principal):
        if not member.get('host_connection_id'):
            return self.guard_legacy_join(member, principal)
        room = self.conversation(member)
        saved = member if member['grant_id'] else {**member, 'grant_id': principal.grant_id}
        shadow = (self.c.config.host_connections_mode == 'shadow' and bool(member.get('connection_source_member_id')))
        try:
            host = self.require_host_current(principal, room, saved, expected_epoch=member.get('host_revocation_epoch'))
        except DevError as error:
            if shadow:
                self.observe_shadow(member, principal, host_error=error)
            raise
        if shadow:
            self.observe_shadow(member, principal)
        return host

    def observe_shadow(self, member, principal, *, host_error=None):
        # Advisory only. A broken historical projection or log handler cannot
        # change the decision made by the current authority path above.
        try:
            if host_error is not None:
                record_shadow(None, host_error)
                return
            try:
                self.guard_legacy_join(member, principal)
            except DevError as error:
                record_shadow(error, None)
        except Exception:
            pass

    def guard_legacy_join(self, member, principal):
        if not member.get('connection_source_member_id'):
            return None
        principal = refresh_principal(self.store, principal)
        source = self.lineage(member)
        if (principal.user_id != member['owner_user_id'] or not principal.grant_id
                or principal.grant_id != source['grant_id']):
            raise DevError('ROOM_CONNECTION_IDENTITY_REQUIRED', '请在已选择的原 dot 连接中完成加入', 403)
        return source

    def guard_route(self, member, delivery=None):
        if not delivery:
            return
        if member.get('host_connection_id'):
            self.require_member(self.conversation(member), member, expected_epoch=member.get('host_revocation_epoch'))
            if (not member.get('probe_replied_at') and not member.get('host_control_evidence_id')
                    and not self.store.one('''SELECT 1 FROM room_host_binding_adoptions
                        WHERE member_id=? AND binding_generation=? AND host_connection_id=?''',
                        (member['id'], member['binding_generation'], member['host_connection_id']))):
                raise DevError('ROOM_CONNECTION_CONTROL_REQUIRED', '请先验证原 dot 对连接的控制', 409)
            return
        if not member.get('connection_source_member_id'):
            return
        source = self.lineage(member)
        if not source['endpoint_digest'] or digest(delivery['url']) != source['endpoint_digest']:
            raise DevError('ROOM_CONNECTION_HOST_CHANGED', '请由已选择的原 dot 创建此房间订阅，不能替换为另一宿主', 409)

    def allows_shared_route(self, member, other_member):
        if (member['id'] == other_member['id']
                or member['conversation_id'] == other_member['conversation_id']
                or member['owner_user_id'] != other_member['owner_user_id']
                or not member.get('grant_id') or member['grant_id'] != other_member.get('grant_id')):
            return False
        host_id = member.get('host_connection_id')
        if host_id and other_member.get('host_connection_id'):
            return host_id == other_member['host_connection_id'] and bool(
                member.get('probe_replied_at') or member.get('host_control_evidence_id'))
        if not member.get('connection_source_member_id') and not other_member.get('connection_source_member_id'):
            return False
        try:
            left, right = self.lineage(member), self.lineage(other_member)
        except DevError:
            return False
        return left['id'] == right['id'] and left['binding_generation'] == right['binding_generation']
