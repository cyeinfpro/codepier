"""Fenced Dot consumer sessions; a session never grants work authority.

The unified mailbox owns the binding and dirty revision. This module owns only
short-lived delivery tickets, consumer ownership, and idempotent lease actions.
No read returns a ticket/proof, acknowledges input, or renews a lease.
"""
from __future__ import annotations


from hub.collaboration.consumer_engine import FencedConsumerEngine
from shared import dot_consumer_contracts as contracts
from shared.util import DevError


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS dot_consumer_fences (
        dot_id TEXT PRIMARY KEY REFERENCES collaboration_dots(id),
        fence INTEGER NOT NULL DEFAULT 0 CHECK(fence>=0))""")
    db.execute("""CREATE TABLE IF NOT EXISTS dot_consumer_sessions (
        id TEXT PRIMARY KEY, dot_id TEXT NOT NULL REFERENCES collaboration_dots(id),
        generation TEXT NOT NULL, grant_id TEXT NOT NULL, subscription_id TEXT NOT NULL,
        run_id TEXT NOT NULL, request_key TEXT NOT NULL, request_digest TEXT NOT NULL,
        proof_hash TEXT NOT NULL, proof_secret TEXT NOT NULL, scope_digest TEXT NOT NULL,
        fence INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','yielded','expired')),
        lease_until REAL NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
        UNIQUE(dot_id,generation,request_key), UNIQUE(dot_id,generation,run_id),
        UNIQUE(dot_id,fence))""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS dot_consumer_single_owner
        ON dot_consumer_sessions(dot_id) WHERE state='active'""")
    db.execute("""CREATE INDEX IF NOT EXISTS dot_consumer_session_scope
        ON dot_consumer_sessions(dot_id,generation,state,lease_until)""")
    db.execute("""CREATE TABLE IF NOT EXISTS dot_wake_tickets (
        ticket_hash TEXT PRIMARY KEY, dot_id TEXT NOT NULL REFERENCES collaboration_dots(id),
        generation TEXT NOT NULL, grant_id TEXT NOT NULL, subscription_id TEXT NOT NULL,
        delivery_key TEXT NOT NULL, ticket_secret TEXT NOT NULL, expires_at REAL NOT NULL,
        redeemed_session_id TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
        UNIQUE(dot_id,generation,subscription_id,delivery_key))""")
    db.execute("""CREATE INDEX IF NOT EXISTS dot_wake_ticket_expiry
        ON dot_wake_tickets(expires_at)""")
    db.execute("""CREATE TABLE IF NOT EXISTS dot_consumer_mutations (
        session_id TEXT NOT NULL REFERENCES dot_consumer_sessions(id),
        action TEXT NOT NULL, request_key TEXT NOT NULL, request_digest TEXT NOT NULL,
        result TEXT NOT NULL, created REAL NOT NULL,
        PRIMARY KEY(session_id,action,request_key))""")


class DotConsumers(FencedConsumerEngine):
    begin_model = contracts.ConsumerBegin
    lease_model = contracts.ConsumerLease
    yield_model = contracts.ConsumerYield

    def scope_digest(self, room, dot):
        return self.c.message_handling.scope_digest(room, dot)

    def consumer_audit(self, room, actor, action, target, details=None):
        self.c.audit(room, actor, action, target, details)

    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def mailbox(self):
        mailbox = getattr(self.c, 'dot_mailbox', None)
        if mailbox is None:
            raise DevError('DOT_CONSUMER_UNAVAILABLE', '当前服务未启用统一收件箱', 409)
        return mailbox

    def active_binding(self, dot_id):
        mailbox = getattr(self.c, 'dot_mailbox', None)
        row = mailbox.binding(dot_id) if mailbox is not None else None
        # Expiration/revocation must not silently restore an unfenced legacy
        # write path. The mailbox keeps activation as a durable protocol choice.
        return row if row and row['state'] != 'prepared' else None

    def binding(self, room, dot, principal=None, generation=None):
        row = self.active_binding(dot['id'])
        if not row or row['state'] != 'active' or (generation is not None and row['generation'] != generation):
            raise DevError('DOT_CONSUMER_BINDING_CHANGED', '消费者绑定未启用或已改变，请恢复原连接', 409)
        if (row['room_id'] != room['id'] or row['conversation_id'] != dot['conversation_id']
                or (principal is not None and row['grant_id'] != principal.grant_id)):
            raise DevError('DOT_CONSUMER_BINDING_DENIED', '消费者不属于当前认证连接与房间', 403)
        sub = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (row['subscription_id'],))
        if (not sub or sub['state'] != 'active' or sub['expires_at'] <= self.c.clock()
                or sub['grant_id'] != row['grant_id'] or sub['room_id'] != room['id']):
            raise DevError('DOT_CONSUMER_SUBSCRIPTION_INACTIVE', '原订阅未就绪或已失效，请恢复原订阅', 409)
        self.c.events.authorize(sub, room)
        return row, sub



    def scope(self, raw, principal, model):
        args, principal, room, slot, dot = self.c.dot_chat.scope(raw, principal, model, connector=True)
        if model is not contracts.ConsumerYield:
            self.c.rooms.migration.legacy_write(dot['conversation_id'])
        self.c.live_room(room)
        binding, sub = self.binding(room, dot, principal, args.get('binding_generation'))
        return args, principal, room, slot, dot, binding, sub





    def guard(self, raw, principal, room, dot, *, require_revision=False):
        """Call inside the same transaction as the protected mutation."""
        member = self.room_member(dot['id'])
        if member:
            conversation = self.c.conversations.object(principal, member['conversation_id'])
            return self.c.room_consumers.guard_session(raw, principal, conversation, member,
                                                      require_revision=require_revision)
        if not self.active_binding(dot['id']):
            return None
        self.c.dot_chat.authorize(principal, room, dot, connector=True)
        self.c.live_room(room)
        binding, _ = self.binding(room, dot, principal)
        row = self._credential(raw, principal, room, dot, binding)
        expected = raw.get('expected_inbox_revision')
        if require_revision and (not isinstance(expected, int) or isinstance(expected, bool)
                or expected != self._revision(dot, binding)):
            raise DevError('DOT_INBOX_CHANGED', '有新的输入或工作状态，请读取当前收件箱后继续原工作', 409,
                           inbox_revision=self._revision(dot, binding))
        return row




    def read(self, raw, principal):
        """Passive status only; no original or other consumer bearer material."""
        with self.store.transaction(immediate=False):
            args, principal, room, _, dot = self.c.dot_chat.scope(raw, principal, contracts.ConsumerRead, connector=True)
            binding = self.active_binding(dot['id'])
            rows = self.store.all("""SELECT * FROM dot_consumer_sessions
                WHERE dot_id=? ORDER BY fence DESC LIMIT 1""", (dot['id'],))
            return {'dot_id': dot['id'], 'consumer_protocol': 2 if binding else 1,
                    'binding_generation': binding['generation'] if binding else None,
                    'inbox_revision': self._revision(dot, binding) if binding else None,
                    'consumer': self._reply(rows[0]) if rows else None,
                    'read_only': True, 'grants_authority': False}

    def guard_work(self, raw, principal, goal, item=None, *, require_revision=True):
        """Locate the responsible Dot from server facts, never a display name."""
        native = self.c.room_work_bridge.native
        if native.reference(goal):
            return native.guard(raw, principal, goal, item, require_revision=require_revision)
        room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (goal['room_id'],))
        dot_id = ''
        delegation_slot = False
        if item:
            if self.store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='team_work_assignments'"):
                assignment = self.store.one('SELECT dot_id FROM team_work_assignments WHERE work_id=?', (item['id'],))
                dot_id = assignment['dot_id'] if assignment else ''
            if not dot_id:
                source = self.store.one("""SELECT p.slot_id FROM delegation_requests d
                    JOIN delegation_policies p ON p.id=d.policy_id
                    WHERE d.work_item_id=? AND d.goal_id=?""", (item['id'], goal['id']))
                dot_id = source['slot_id'] if source else ''
                delegation_slot = bool(dot_id)
        if not dot_id:
            source = self.store.one("""SELECT p.slot_id FROM delegation_requests d
                JOIN delegation_policies p ON p.id=d.policy_id WHERE d.goal_id=?""", (goal['id'],))
            dot_id = source['slot_id'] if source else ''
            delegation_slot = bool(dot_id)
        session = self.store.one('SELECT dot_id,member_id FROM dot_consumer_sessions WHERE id=?',
                                 (raw.get('consumer_session_id', ''),)) if raw.get('consumer_session_id') else None
        if session and session['member_id']:
            member = self.c.rooms.member(session['member_id'], goal['conversation_id'])
            adapter = self.store.one('''SELECT legacy_dot_id FROM room_member_work_bridges
                WHERE member_id=? AND legacy_dot_id=? AND state='active' ''', (member['id'], dot_id)) if dot_id else None
            if (not adapter or (raw.get('dot_id') and raw['dot_id'] != dot_id)):
                raise DevError('DOT_CONSUMER_WORK_MISMATCH', '此消费回合不属于原工作的成员', 403)
            conversation = self.c.conversations.object(principal, goal['conversation_id'])
            return self.c.room_consumers.guard_session(raw, principal, conversation, member,
                                                      require_revision=require_revision)
        if session and raw.get('dot_id') and raw['dot_id'] != session['dot_id']:
            raise DevError('DOT_CONSUMER_WORK_MISMATCH', '显式 Dot 与消费回合不一致', 403)
        if dot_id:
            if ((session and session['dot_id'] != dot_id)
                    or (raw.get('dot_id') and raw['dot_id'] != dot_id)):
                raise DevError('DOT_CONSUMER_WORK_MISMATCH', '工作不属于这个消费回合的 Dot', 403)
        elif session:
            dot_id = session['dot_id']
        else:
            mailbox = getattr(self.c, 'dot_mailbox', None)
            bindings = mailbox.for_grant(principal.grant_id, goal['conversation_id']) if mailbox else []
            if bindings:
                raise DevError('DOT_CONSUMER_PROOF_REQUIRED', '当前连接已启用消费凭证，不能走旧工作入口', 403)
            if not raw.get('dot_id'):
                return None
            dot_id = raw['dot_id']
        dot = self.c.dots.find(dot_id)
        if not dot and delegation_slot and not self.active_binding(dot_id):
            # CPJ delegation slots predate task/duplex Dot records. Their
            # original policy, goal approval and work lease still authorize
            # effects; a different v2 slot sharing the grant does not upgrade
            # this exact legacy route. Never infer this exception from raw IDs.
            legacy = self.store.one("""SELECT 1 FROM collaboration_join_slots s
                JOIN delegation_policies p ON p.slot_id=s.id AND p.room_id=s.room_id
                JOIN delegation_requests d ON d.policy_id=p.id
                WHERE s.id=? AND s.room_id=? AND p.conversation_id=? AND d.goal_id=?""",
                (dot_id, goal['room_id'], goal['conversation_id'], goal['id']))
            if legacy:
                return None
        if not dot or dot['conversation_id'] != goal['conversation_id']:
            raise DevError('DOT_CONSUMER_WORK_MISMATCH', '消费者不属于原工作会话', 403)
        return self.guard(raw, principal, room, dot, require_revision=require_revision)

    def room_member(self, dot_id):
        # Once explicitly migrated, expiration/removal cannot reopen an older
        # unfenced API. Historical legacy records retain their original path.
        return self.store.one('''SELECT m.* FROM room_members m
            WHERE m.status!='legacy' AND (m.legacy_dot_id=? OR EXISTS (
                SELECT 1 FROM room_member_work_bridges b WHERE b.member_id=m.id AND b.legacy_dot_id=?))
            LIMIT 1''', (dot_id, dot_id))
