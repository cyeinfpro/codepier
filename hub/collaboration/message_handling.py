"""Versioned, explicit input decisions. This ledger never owns execution leases.

Only canonical room replies are coverage evidence. External delivery and work
outcomes stay independent, and reads never acknowledge or repair anything.
"""
from __future__ import annotations

import json

from hub.collaboration.common import canonical, digest
from shared import collaboration_contracts as contracts
from shared.util import DevError

STATES = ('awaiting', 'observed', 'waiting_input', 'waiting_authorization',
          'work_linked', 'reply_committed', 'covered', 'no_response_required', 'legacy_handled')
CONTENT_FINAL = {'reply_committed', 'covered', 'no_response_required'}


def migrate(db):
    states = ','.join("'" + state + "'" for state in STATES)
    db.execute(f"""CREATE TABLE IF NOT EXISTS dot_message_handling (
        dot_id TEXT NOT NULL,message_id TEXT NOT NULL,
        source_version INTEGER NOT NULL,source_digest TEXT NOT NULL,scope_digest TEXT NOT NULL,
        handling_state TEXT NOT NULL CHECK(handling_state IN ({states})),
        reason_code TEXT NOT NULL,revision INTEGER NOT NULL CHECK(revision>0),
        reply_id TEXT NOT NULL DEFAULT '',reply_version INTEGER NOT NULL DEFAULT 0,
        work_item_id TEXT NOT NULL DEFAULT '',evidence_source TEXT NOT NULL,actor_ref TEXT NOT NULL,
        observed_at REAL,updated_at REAL NOT NULL,
        PRIMARY KEY(dot_id,message_id),
        FOREIGN KEY(dot_id,message_id) REFERENCES collaboration_dot_messages(dot_id,message_id) ON DELETE CASCADE)""")
    db.execute("""CREATE TABLE IF NOT EXISTS dot_message_handling_history (
        dot_id TEXT NOT NULL,message_id TEXT NOT NULL,revision INTEGER NOT NULL,
        record TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(dot_id,message_id,revision),
        FOREIGN KEY(dot_id,message_id) REFERENCES collaboration_dot_messages(dot_id,message_id) ON DELETE CASCADE)""")
    db.execute("""CREATE TABLE IF NOT EXISTS dot_message_coverage (
        dot_id TEXT NOT NULL,message_id TEXT NOT NULL,source_version INTEGER NOT NULL,
        source_digest TEXT NOT NULL,reply_id TEXT NOT NULL REFERENCES collaboration_messages(id) ON DELETE CASCADE,
        reply_version INTEGER NOT NULL,handling_revision INTEGER NOT NULL,created REAL NOT NULL,
        PRIMARY KEY(dot_id,message_id,source_version,source_digest,reply_id,reply_version),
        FOREIGN KEY(dot_id,message_id) REFERENCES collaboration_dot_messages(dot_id,message_id) ON DELETE CASCADE)""")
    db.execute('CREATE INDEX IF NOT EXISTS dot_coverage_reply ON dot_message_coverage(reply_id,reply_version)')


class MessageHandling:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    @staticmethod
    def capability():
        return {'schema_version': 1, 'action': 'dot_handle', 'max_inputs': 20,
                'states': list(STATES), 'explicit_coverage_only': True,
                'content_and_delivery_independent': True, 'creates_work': False,
                'legacy_ack_supported': True, 'coverage_requires_canonical_final_reply': True}

    @staticmethod
    def source_digest(message):
        return digest(json.loads(message['body']).get('body_text', ''))

    def scope_digest(self, room, dot):
        slot = self.c.joining.slot(room, dot['id'])
        # Only actual authority/binding fields belong here. The general slot
        # version also changes for a test notification or receipt confirmation;
        # those observations must never reopen already completed input.
        return digest(['dot-handling-v1', dot['id'], dot['setup_digest'],
                       slot['grant_id'], slot['principal'],
                       self.c.dots.scope_manifest(room, dot)])

    def current(self, dot_id, message_id):
        return self.store.one('SELECT * FROM dot_message_handling WHERE dot_id=? AND message_id=?',
                              (dot_id, message_id))

    def work(self, dot_id, message_id):
        linked = self.store.one('''SELECT w.id,w.state,w.version,w.goal_id FROM dot_task_links l
            JOIN delegation_requests d ON d.id=l.delegation_id
            JOIN delegation_policies p ON p.id=d.policy_id
            JOIN coordination_work w ON w.id=d.work_item_id
            WHERE l.dot_id=? AND l.message_id=? AND p.slot_id=l.dot_id''', (dot_id, message_id))
        if linked:
            return linked
        return self.store.one("""SELECT w.id,w.state,w.version,w.goal_id FROM delegation_requests d
            JOIN delegation_policies p ON p.id=d.policy_id
            JOIN coordination_work w ON w.id=d.work_item_id
            WHERE d.message_id=? AND p.slot_id=?""", (message_id, dot_id))

    def _save(self, dot, message, principal, scope_digest, state, reason, *,
              reply=None, work=None, evidence='consumer_declaration'):
        old = self.current(dot['id'], message['id'])
        now = self.c.clock()
        record = {'dot_id': dot['id'], 'message_id': message['id'],
            'source_version': message['version'], 'source_digest': self.source_digest(message),
            'scope_digest': scope_digest, 'handling_state': state, 'reason_code': reason,
            'revision': (old['revision'] if old else 0) + 1,
            'reply_id': reply['id'] if reply else '', 'reply_version': reply['version'] if reply else 0,
            'work_item_id': work['id'] if work else '', 'evidence_source': evidence,
            'actor_ref': principal.grant_id or principal.user_id,
            'observed_at': old['observed_at'] if old and old['observed_at'] is not None else now,
            'updated_at': now}
        columns = ','.join(record)
        self.store.execute(f"""INSERT INTO dot_message_handling ({columns})
            VALUES ({','.join('?' for _ in record)})
            ON CONFLICT(dot_id,message_id) DO UPDATE SET
            {','.join(k + '=excluded.' + k for k in record if k not in {'dot_id', 'message_id'})}""",
            tuple(record.values()))
        self.store.execute('INSERT INTO dot_message_handling_history VALUES (?,?,?,?,?)',
                           (dot['id'], message['id'], record['revision'], canonical(record), now))
        if state == 'covered':
            self.store.execute('INSERT OR IGNORE INTO dot_message_coverage VALUES (?,?,?,?,?,?,?,?)',
                (dot['id'], message['id'], message['version'], record['source_digest'],
                 reply['id'], reply['version'], record['revision'], now))
        return record

    def record_reply(self, room, dot, principal, reply):
        """Record a new canonical reply fact inside the publication transaction.

        This hook never creates coverage or asserts that linked work succeeded.
        Historical calls are not backfilled from text or timestamp similarity.
        """
        if not reply['reply_to_id']:
            return
        receipt = self.store.one('SELECT * FROM collaboration_dot_messages WHERE dot_id=? AND message_id=?',
                                 (dot['id'], reply['reply_to_id']))
        if not receipt:
            return
        source = self.c.object('collaboration_messages', room, receipt['message_id'])
        if (source['version'] != receipt['message_version']
                or self.source_digest(source) != receipt['message_digest']):
            return
        old = self.current(dot['id'], source['id'])
        if old and old['handling_state'] in CONTENT_FINAL:
            return
        final = json.loads(reply['body']).get('dot_reply_complete') is not False
        self._save(dot, source, principal, self.scope_digest(room, dot),
                   'reply_committed' if final else 'observed', 'final_reply' if final else 'received',
                   reply=reply if final else None, evidence='canonical_publication')

    def record_work(self, room, dot, principal, message):
        work = self.work(dot['id'], message['id'])
        if not work:
            return
        old = self.current(dot['id'], message['id'])
        if old and old['handling_state'] == 'work_linked' and old['work_item_id'] == work['id']:
            return
        self._save(dot, message, principal, self.scope_digest(room, dot), 'work_linked',
                   'existing_work', work=work, evidence='existing_work_handoff')

    def record_ack(self, room, dot, principal, message, disposition):
        # Old handled is deliberately not upgraded to a specific completion fact.
        old = self.current(dot['id'], message['id'])
        if old and not (old['evidence_source'] == 'legacy_ack' and old['handling_state'] == 'observed'
                        and disposition == 'handled'):
            return
        receipt = self.store.one('SELECT * FROM collaboration_dot_messages WHERE dot_id=? AND message_id=?',
                                 (dot['id'], message['id']))
        if (message['version'] != receipt['message_version']
                or self.source_digest(message) != receipt['message_digest']):
            return
        self._save(dot, message, principal, self.scope_digest(room, dot),
                   'legacy_handled' if disposition == 'handled' else 'observed',
                   'legacy_handled' if disposition == 'handled' else 'received',
                   evidence='legacy_ack')

    def submit(self, raw, principal):
        with self.store.transaction():
            args, principal, room, _, dot = self.c.dot_chat.scope(
                raw, principal, contracts.DotHandling, connector=True)
            self.c.live_room(room)
            scope_digest = self.scope_digest(room, dot)
            if args['scope_digest'] != scope_digest:
                raise DevError('DOT_HANDLING_SCOPE_CHANGED', '当前连接或可见范围已改变，请重新读取原消息', 409)
            selected = []
            for entry in args['inputs']:
                receipt = self.store.one('SELECT * FROM collaboration_dot_messages WHERE dot_id=? AND message_id=?',
                                         (dot['id'], entry['message_id']))
                if not receipt:
                    raise DevError('DOT_MESSAGE_NOT_ADDRESSED', '每条输入必须明确属于这个 dot', 404)
                message = self.c.object('collaboration_messages', room, entry['message_id'])
                self.c.chatroom.require_message_visible(principal, message)
                if message['conversation_id'] != dot['conversation_id']:
                    raise DevError('DOT_HANDLING_THREAD_MISMATCH', '处理记录不能跨房间或话题', 409)
                if (message['version'] != entry['source_version']
                        or self.source_digest(message) != entry['source_digest']):
                    raise DevError('DOT_MESSAGE_CHANGED', '原输入已改变；旧处理不能满足新版本', 409)
                if args['handling_state'] == 'work_linked':
                    linked = self.work(dot['id'], message['id'])
                    if linked:
                        goal = self.c.coordination.object(room, linked['goal_id'])
                        self.c.coordination.authorize(principal, room, goal)
                selected.append((entry, message))
            roots = {message['thread_root_id'] or message['id'] for _, message in selected}
            if len(roots) != 1:
                raise DevError('DOT_HANDLING_THREAD_MISMATCH', '合并输入必须属于同一个原话题', 409)
            reply = None
            if args['reply_id']:
                reply = self.c.object('collaboration_messages', room, args['reply_id'])
                self.c.chatroom.require_message_visible(principal, reply)
                body = json.loads(reply['body'])
                if (reply['version'] != args['reply_version'] or reply['origin'] != 'bound_connector'
                        or reply['author'] != principal.grant_id or body.get('sender_dot_id') != dot['id']
                        or reply['conversation_id'] != dot['conversation_id']
                        or (reply['thread_root_id'] or reply['id']) not in roots
                        or not reply['reply_to_id'] or reply['kind'] != 'reply' or reply['state'] != 'saved'):
                    raise DevError('DOT_HANDLING_REPLY_INVALID', '必须引用此 dot 在原话题真实保存的直接回复版本', 409)
                final = body.get('dot_reply_complete') is not False
                if final != (args['handling_state'] in {'covered', 'reply_committed'}):
                    raise DevError('DOT_HANDLING_REPLY_INVALID', '等待说明必须是阶段回复；覆盖必须是最终回复', 409)
                # Replies cannot themselves be addressed input records. No graph
                # traversal, inherited coverage, self edges or coverage chains.
                if self.store.one('SELECT 1 FROM collaboration_dot_messages WHERE message_id=?', (reply['id'],)):
                    raise DevError('DOT_HANDLING_COVERAGE_CHAIN', '覆盖只能直接指向规范回复，不能链式覆盖输入', 409)
                if any(reply['conversation_sequence'] <= message['conversation_sequence'] for _, message in selected):
                    raise DevError('DOT_HANDLING_REPLY_INVALID', '回复不能覆盖它保存之后才出现的新输入', 409)
                if args['handling_state'] == 'reply_committed' and any(
                        reply['reply_to_id'] != message['id'] for _, message in selected):
                    raise DevError('DOT_HANDLING_REPLY_INVALID', 'reply_committed 仅指直接回复的原输入；合并关系使用 covered', 409)

            def save():
                # Only exact idempotent replays may bypass a stale revision.
                for entry, message in selected:
                    current = self.current(dot['id'], message['id'])
                    if (current['revision'] if current else 0) != entry['expected_revision']:
                        raise DevError('DOT_HANDLING_REVISION_CONFLICT', '输入处理已更新，请读取当前 revision', 409)
                    work = self.work(dot['id'], message['id'])
                    if args['handling_state'] in CONTENT_FINAL and work and work['state'] != 'succeeded':
                        raise DevError('DOT_HANDLING_WORK_UNFINISHED', '原工作尚未成功；普通文字不能隐藏未完成工作或失败', 409)
                    if args['handling_state'] == 'work_linked' and (not work or work['id'] != args['work_item_id']):
                        raise DevError('DOT_HANDLING_WORK_INVALID', '只能关联原输入已有的同一工作', 409)
                    if args['handling_state'] == 'no_response_required' and self.c.chatroom.is_delegation_source(message):
                        raise DevError('DOT_HANDLING_WORK_UNFINISHED', '执行请求不能声明无需回应', 409)
                result = []
                for _, message in selected:
                    work = self.work(dot['id'], message['id'])
                    record = self._save(dot, message, principal, scope_digest,
                        args['handling_state'], args['reason_code'], reply=reply,
                        work=work if args['handling_state'] == 'work_linked' else None)
                    handled = args['handling_state'] in CONTENT_FINAL | {'work_linked'}
                    self.store.execute("""UPDATE collaboration_dot_messages SET
                        state=?,received_at=COALESCE(received_at,?),
                        handled_at=CASE WHEN ? THEN COALESCE(handled_at,?) ELSE NULL END
                        WHERE dot_id=? AND message_id=?""",
                        ('handled' if handled else 'read', self.c.clock(), handled, self.c.clock(),
                         dot['id'], message['id']))
                    result.append(record)
                return {'schema_version': 1, 'dot_id': dot['id'], 'records': result,
                        'content_only': True, 'execution_changed': False, 'delivery_verified': False}
            return self.c.mutation(principal, room, 'dot_handle:' + dot['id'], args, save)

    def summaries(self, message_ids, principal=None):
        """Read evidence for an already-authorized visible message set (max 100).

        Callers must filter source visibility first, as for delivery receipts.
        Related reply/work references still require this caller's current access.
        An absent principal never implicitly authorizes related references.
        """
        ids = list(dict.fromkeys(message_ids))
        if len(ids) > 100:
            raise ValueError('Handling summaries are bounded to 100 visible messages')
        if not ids:
            return {}
        rows = self.store.all(f"""SELECT d.dot_id,d.message_id,d.state AS legacy_state,
            d.message_version,d.message_digest,d.received_at,d.handled_at,
            m.version AS current_version,m.body AS source_body,m.room_id AS source_room_id,
            m.conversation_id AS source_conversation_id,m.thread_root_id AS source_root,
            h.source_version,h.source_digest,h.scope_digest,h.handling_state,h.reason_code,h.revision,
            h.reply_id,h.reply_version,h.work_item_id,h.evidence_source,h.actor_ref,h.observed_at,h.updated_at,
            r.version AS current_reply_version,r.body AS reply_body,r.room_id AS reply_room_id,
            r.conversation_id AS reply_conversation_id,r.thread_root_id AS reply_root,
            r.origin AS reply_origin,r.kind AS reply_kind,
            w.id AS linked_work_id,w.state AS work_state,w.goal_id AS work_goal_id
            FROM collaboration_dot_messages d JOIN collaboration_messages m ON m.id=d.message_id
            LEFT JOIN dot_message_handling h ON h.dot_id=d.dot_id AND h.message_id=d.message_id
            LEFT JOIN collaboration_messages r ON r.id=h.reply_id
            LEFT JOIN dot_task_links tl ON tl.dot_id=d.dot_id AND tl.message_id=d.message_id
            LEFT JOIN delegation_requests dr ON dr.id=COALESCE(tl.delegation_id,
                (SELECT id FROM delegation_requests WHERE message_id=d.message_id))
            LEFT JOIN delegation_policies p ON p.id=dr.policy_id AND p.slot_id=d.dot_id
            LEFT JOIN coordination_work w ON w.id=dr.work_item_id AND p.id IS NOT NULL
            WHERE d.message_id IN ({','.join('?' for _ in ids)})""", tuple(ids))
        scopes, work_access = {}, {}
        result = {}
        for row in rows:
            source_version = row['source_version'] if row['revision'] else row['message_version']
            source_digest = row['source_digest'] if row['revision'] else row['message_digest']
            current = (source_version == row['current_version']
                       and source_digest == digest(json.loads(row['source_body']).get('body_text', '')))
            state = row['handling_state'] or {'pending': 'awaiting', 'read': 'observed',
                                             'handled': 'legacy_handled'}[row['legacy_state']]
            evidence_valid = current
            if row['revision']:
                if row['dot_id'] not in scopes:
                    dot = self.c.dots.find(row['dot_id'])
                    room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (dot['room_id'],))
                    scopes[row['dot_id']] = self.scope_digest(room, dot)
                evidence_valid &= row['scope_digest'] == scopes[row['dot_id']]
            reply_id, reply_version = row['reply_id'] or '', row['reply_version'] or 0
            if reply_id:
                reply_body = json.loads(row['reply_body']) if row['reply_body'] else {}
                reply_visible = bool(principal is not None and row['reply_body']
                    and row['reply_room_id'] == row['source_room_id']
                    and row['reply_conversation_id'] == row['source_conversation_id']
                    and row['reply_root'] == row['source_root']
                    and row['reply_origin'] == 'bound_connector' and row['reply_kind'] == 'reply'
                    and reply_body.get('sender_dot_id') == row['dot_id']
                    and self.c.chatroom.message_visible(principal, {'body': row['reply_body']}))
                evidence_valid &= reply_visible and reply_version == row['current_reply_version']
                if state in {'covered', 'reply_committed'}:
                    evidence_valid &= reply_body.get('dot_reply_complete') is not False
                if not reply_visible:
                    reply_id, reply_version = '', 0
            work_id, work_state = row['linked_work_id'] or row['work_item_id'] or '', row['work_state']
            if work_id:
                if work_id not in work_access:
                    allowed = False
                    if principal is not None and row['work_goal_id']:
                        try:
                            room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (row['source_room_id'],))
                            goal = self.c.coordination.object(room, row['work_goal_id'])
                            self.c.coordination.authorize(principal, room, goal)
                            allowed = True
                        except DevError:
                            pass
                    work_access[work_id] = allowed
                if not work_access[work_id]:
                    evidence_valid = False
                    work_id, work_state = '', None
            needs_work = bool(row['linked_work_id'] and row['work_state'] != 'succeeded')
            result[(row['dot_id'], row['message_id'])] = {
                'state': state if evidence_valid else 'awaiting', 'historical_state': state,
                'revision': row['revision'] or 0, 'source_version': source_version,
                'source_digest': source_digest, 'source_current': bool(current), 'evidence_current': bool(evidence_valid),
                'reply_id': reply_id, 'reply_version': reply_version,
                'work_item_id': work_id,
                'work_state': work_state, 'reason_code': row['reason_code'] or '',
                'evidence_source': row['evidence_source'] or 'legacy_receipt',
                'actor_ref': (row['actor_ref'] or '') if principal is not None else '',
                'observed_at': row['observed_at'] if row['revision'] else row['received_at'],
                'updated_at': row['updated_at'],
                'content_completed': bool(evidence_valid and state in CONTENT_FINAL and not needs_work),
                'requires_attention': bool(not evidence_valid or needs_work),
                'delivery_verified': False}
        return result

    def arguments(self, room, dot, message, summary, scope_digest):
        return {'project': room['project_id'], 'environment_id': room['environment_id'],
            'dot_id': dot['id'], 'handling_schema_version': 1, 'scope_digest': scope_digest,
            'inputs': [{'message_id': message['id'], 'source_version': message['version'],
                'source_digest': self.source_digest(message), 'expected_revision': summary['revision']}],
            'idempotency_key': 'dot-handle:' + digest([dot['id'], message['id'],
                message['version'], summary['revision'], scope_digest])}
