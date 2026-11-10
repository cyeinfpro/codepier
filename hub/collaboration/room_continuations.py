"""Owner-reviewed continuation batches over the existing room work journal."""
from dataclasses import asdict
import json
import uuid

from hub.collaboration.common import canonical, digest, validate
from hub.principal import Principal
from shared.room_contracts import RoomPermissionsUpdate
from shared.room_runtime_contracts import RoomPermissionContinuationSave
from shared.util import DevError


MAX_CANDIDATES = 50
PREVIEW_TTL = 600
TERMINAL_OPERATIONS = {'succeeded', 'failed', 'cancelled'}


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS room_continuation_previews(
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        policy_digest TEXT NOT NULL, candidates TEXT NOT NULL, expires_at REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_continuation_batches(
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, owner_id TEXT NOT NULL,
        preview_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL, policy_version INTEGER NOT NULL,
        owner_principal TEXT NOT NULL, created REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_continuation_items(
        batch_id TEXT NOT NULL REFERENCES room_continuation_batches(id), task_id TEXT NOT NULL,
        candidate TEXT NOT NULL, state TEXT NOT NULL, reason_code TEXT NOT NULL DEFAULT '',
        retry_at REAL NOT NULL DEFAULT 0, PRIMARY KEY(batch_id,task_id))''')
    db.execute('''CREATE INDEX IF NOT EXISTS room_continuation_pending ON room_continuation_items(retry_at,batch_id)
        WHERE state IN ('pending','waiting_operations')''')
    db.execute('''CREATE TABLE IF NOT EXISTS room_continuation_proofs(
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL, task_id TEXT NOT NULL,
        message_id TEXT NOT NULL, member_id TEXT NOT NULL, goal_id TEXT NOT NULL,
        epoch INTEGER NOT NULL, evidence TEXT NOT NULL, digest TEXT NOT NULL,
        UNIQUE(batch_id,task_id))''')
    db.execute('''CREATE INDEX IF NOT EXISTS room_continuation_source_proof
        ON room_continuation_proofs(message_id,member_id,sequence DESC) WHERE goal_id='' ''')
    db.execute('''CREATE INDEX IF NOT EXISTS room_continuation_work_proof
        ON room_continuation_proofs(goal_id,epoch) WHERE goal_id!='' ''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS room_continuation_proof_immutable
        BEFORE UPDATE ON room_continuation_proofs
        BEGIN SELECT RAISE(ABORT,'room continuation evidence is immutable'); END''')


class RoomContinuations:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    @property
    def native(self):
        return self.c.room_work_bridge.native

    def _policy(self, args, owner, room):
        permissions = self.c.room_permissions
        policy, _, paused = permissions.plan(args, owner, room)
        for entry in [policy, *policy['channels'].values()]:
            entry['scope'] = permissions.scope([rule for rule in entry['scope']['project_scopes']
                if (rule['project_id'], rule['environment_id']) not in paused])
        return policy

    @staticmethod
    def _scope(policy, source):
        weixin = json.loads(source['body']).get('external_origin', {}).get('native_room')
        channel = policy['channels']['weixin']
        scope = channel['scope'] if weixin and channel['mode'] == 'custom' else policy['scope']
        return {(rule['project_id'], rule['environment_id']): rule for rule in scope['project_scopes']}

    def _source_member(self, source, room, owner):
        body = json.loads(source['body'])
        if source['state'] != 'saved' or source['conversation_id'] != room['id']:
            raise DevError('ROOM_SOURCE_CHANGED', '原任务消息已修改或撤回', 409)
        self.c.chatroom.require_message_visible(owner, source)
        if body.get('external_origin', {}).get('native_room'):
            self.native.input_authority.channel_identity(source)
        elif source['origin'] != 'panel_owner' or source['author'] != owner.user_id:
            raise DevError('ROOM_OWNER_SOURCE_REQUIRED', '原任务需要可核验的房主来源', 403)
        waiting = self.store.one('SELECT follow_lead FROM room_waiting_inputs WHERE message_id=?', (source['id'],))
        recipient_id = '' if waiting and waiting['follow_lead'] else body.get('room_member_id', '')
        return self.c.room_runtime.recipient(room, recipient_id)

    def _source_blocker(self, source):
        if source['origin'] != 'external_channel':
            return ''
        binding, _, _ = self.native.input_authority.channel_identity(source)
        try:
            self.native.input_authority.channel_authorization(binding)
        except DevError as exc:
            return exc.code
        return ''

    def _goal_source(self, goal, room, owner):
        if goal['state'] in {'cancelled', 'completed', 'succeeded', 'failed'} or not goal['approval_id']:
            raise DevError('ROOM_CONTINUATION_UNAVAILABLE', '已结束的工作不能续办', 409)
        reference = self.native.reference(goal)
        if not reference:
            raise DevError('ROOM_CONTINUATION_UNAVAILABLE', '此工作缺少原房间批准', 409)
        source = self.native.source(goal, owner)
        self._source_member(source, room, owner)
        intent = self.c.room_runtime.intent_revision(goal['id'])
        current = self.store.one('SELECT * FROM room_work_intent_sources WHERE goal_id=? AND intent_revision=?',
                                 (goal['id'], intent))
        if current:
            message = self.store.one('SELECT * FROM collaboration_messages WHERE id=?', (current['source_message_id'],))
            if (not message or message['state'] != 'saved' or message['version'] != current['source_version']
                    or digest(json.loads(message['body']).get('body_text', '')) != current['source_digest']):
                raise DevError('ROOM_INTENT_CHANGED', '原工作最新意图已改变', 409)
        if self.store.one('SELECT 1 FROM room_goal_publications WHERE goal_id=? AND intent_revision=?',
                          (goal['id'], intent)):
            raise DevError('ROOM_CONTINUATION_UNAVAILABLE', '原工作已提交本轮最终结果', 409)
        return source, reference, intent, current

    def _plan_digest(self, goal, intent):
        # A worker can add or reassign a work item without changing the Goal
        # version. Bind the reviewed plan independently of live status changes.
        work = self.store.all('''SELECT w.id,w.project_id,w.objective,w.acceptance,w.assignee_grant_id,
            w.dependencies,w.required_capabilities,a.member_id FROM coordination_work w
            JOIN room_native_work_assignments a ON a.work_id=w.id
            WHERE w.goal_id=? AND w.approval_id=? AND a.intent_revision=? ORDER BY w.id''',
            (goal['id'], goal['approval_id'], intent))
        operations = [op['operation_id'] for op in self.c.coordination.operations(goal)]
        return digest([work, operations])

    def _candidate(self, goal, source, room, owner, policy):
        if goal:
            source, reference, intent, current = self._goal_source(goal, room, owner)
            roles = self.c.room_goal_recovery.roles(reference, self.native.original(reference))
            if goal['state'] == 'active':
                try:
                    partition = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (goal['room_id'],))
                    self.c.coordination.authorize(owner, partition, goal, active=True)
                    for role in roles.values():
                        actor = self.c.rooms.principal(self.c.rooms.member(role['member_id'], room['id']))
                        for rule in role['projects']:
                            self.native.check_work(actor, goal, {'project_id': rule['project_id'],
                                'required_capabilities': canonical(rule['capabilities'])}, role['member_id'])
                except DevError:
                    pass
                else:
                    raise DevError('ROOM_CONTINUATION_NOT_NEEDED', '此工作已具备有效批准', 409)
            ceiling = self._scope(policy, source)
            for saved in roles.values():
                for rule in saved['projects']:
                    draft = ceiling.get((rule['project_id'], rule['environment_id']), {})
                    if (not set(rule['capabilities']) <= set(draft.get('capabilities', []))
                            or not set(rule['execution_targets']) <= set(draft.get('execution_targets', []))):
                        raise DevError('ROOM_CONTINUATION_SCOPE_DENIED', '新权限未完整覆盖原工作的能力和执行位置', 403)
            remaining = self.store.all('''SELECT w.id,w.objective,w.state,w.reason_code FROM coordination_work w
                JOIN room_native_work_assignments a ON a.work_id=w.id
                WHERE w.goal_id=? AND w.approval_id=? AND a.intent_revision=? AND w.state!='succeeded'
                ORDER BY w.created,w.id''', (goal['id'], goal['approval_id'], intent))
            if remaining and all(item['reason_code'] == 'GOAL_PAUSE' for item in remaining):
                raise DevError('ROOM_CONTINUATION_MANUALLY_PAUSED', '房主暂停的工作须在原工作详情中继续', 409)
            value = {'task_id': goal['id'], 'kind': 'work', 'label': json.loads(goal['spec'])['objective'][:200],
                     'remaining_operations': [{'label': item['objective'][:200], 'state': item['state']} for item in remaining[:20]],
                     'selected_by_default': True, 'goal_version': goal['version'], 'goal_state': goal['state'], 'intent_revision': intent,
                     'intent_source': current['source_message_id'] if current else '', 'approval_id': goal['approval_id'],
                     'coordinator_epoch': self.c.room_goal_recovery.epoch(goal['id']),
                     'plan_digest': self._plan_digest(goal, intent)}
        else:
            self._source_member(source, room, owner)
            if not self._scope(policy, source):
                raise DevError('ROOM_CONTINUATION_SCOPE_DENIED', '新权限没有可开工的项目', 403)
            value = {'task_id': source['id'], 'kind': 'source', 'label': json.loads(source['body']).get('body_text', '')[:200],
                     'remaining_operations': [], 'selected_by_default': False}
        blocker = self._source_blocker(source)
        return {**value, 'selected_by_default': value['selected_by_default'] and not blocker,
                'eligible': not blocker, 'reason_code': blocker,
                'source_id': source['id'], 'source_version': source['version'],
                'source_digest': digest(json.loads(source['body']).get('body_text', ''))}

    def preview(self, raw, owner):
        args = validate(RoomPermissionsUpdate, raw)
        with self.store.transaction():
            owner, room = self.c.rooms.owner(owner, args['conversation_id'])
            self.c.rooms.require_native(room)
            current = self.c.room_permissions.row(room['id'])
            version = current['version'] if current else 0
            if version != args['expected_version']:
                raise DevError('STALE_VERSION', '权限已更新，请重新核对续办清单', 409, current_version=version)
            policy = self._policy(args, owner, room)
            fingerprint = digest([room['id'], owner.user_id, version, policy])
            candidates = []
            goals = self.store.all('''SELECT * FROM coordination_goals WHERE conversation_id=?
                AND state IN ('active','paused','expired') ORDER BY updated DESC,id LIMIT ?''', (room['id'], 100))
            sources = self.store.all('''SELECT m.* FROM collaboration_messages m WHERE m.conversation_id=?
                AND m.state='saved' AND m.goal_id IS NULL AND m.origin IN ('panel_owner','external_channel')
                AND COALESCE(json_extract(m.body,'$.work_id'),'')=''
                AND (json_extract(m.body,'$.room_routing.state')='waiting'
                     OR COALESCE(json_extract(m.body,'$.owner_task_blocker'),'')!=''
                     OR EXISTS(SELECT 1 FROM room_input_authority a WHERE a.message_id=m.id)
                     OR EXISTS(SELECT 1 FROM dot_mailbox_entries e WHERE e.object_id=m.id AND e.kind='message'
                         AND e.acknowledged_at IS NULL AND e.superseded_by=''))
                AND NOT EXISTS(SELECT 1 FROM room_work_sources w WHERE w.message_id=m.id)
                ORDER BY m.conversation_sequence DESC LIMIT ?''', (room['id'], 100))
            for goal, source in [(goal, None) for goal in goals] + [(None, source) for source in sources]:
                try:
                    if not goal:
                        body = json.loads(source['body'])
                        member = self._source_member(source, room, owner)
                        if (body.get('room_routing', {}).get('state') != 'waiting'
                                and not body.get('owner_task_blocker')
                                and self.native.input_authority.rules(source, member)):
                            continue
                except DevError:
                    pass  # An unstarted input may need a new reviewed grant.
                try:
                    candidates.append(self._candidate(goal, source, room, owner, policy))
                except DevError:
                    continue
                if len(candidates) == MAX_CANDIDATES:
                    break
            identifier, expires_at = uuid.uuid4().hex, self.c.clock() + PREVIEW_TTL
            self.store.execute('DELETE FROM room_continuation_previews WHERE expires_at<=?', (self.c.clock(),))
            self.store.execute('INSERT INTO room_continuation_previews VALUES (?,?,?,?,?,?)',
                               (identifier, room['id'], owner.user_id, fingerprint, canonical(candidates), expires_at))
            return {'policy_preview_digest': fingerprint, 'continuation_preview_id': identifier, 'expires_at': expires_at,
                    'candidates': [{key: candidate[key] for key in ('task_id', 'kind', 'label', 'remaining_operations',
                        'selected_by_default', 'eligible', 'reason_code')} for candidate in candidates]}

    def _check_candidate(self, candidate, room, owner, *, check_version=True):
        source = self.store.one('SELECT * FROM collaboration_messages WHERE id=? AND conversation_id=?',
                                (candidate['source_id'], room['id']))
        if (not source or source['version'] != candidate['source_version']
                or digest(json.loads(source['body']).get('body_text', '')) != candidate['source_digest']):
            raise DevError('ROOM_SOURCE_CHANGED', '原任务消息已修改或撤回', 409)
        if candidate['kind'] == 'source':
            if (source.get('goal_id') or self.store.one('SELECT 1 FROM room_work_sources WHERE message_id=?', (source['id'],))):
                raise DevError('ROOM_CONTINUATION_ALREADY_STARTED', '此消息已经开工，请核对原工作', 409)
            return source, self._source_member(source, room, owner), None
        goal = self.c.room_runtime.goal(candidate['task_id'])
        if goal['conversation_id'] != room['id']:
            raise DevError('ROOM_CONTINUATION_UNAVAILABLE', '原工作不属于此房间', 409)
        _, reference, intent, current = self._goal_source(goal, room, owner)
        expired_while_waiting = (candidate['goal_state'] == 'active' and goal['state'] == 'expired'
                                 and (goal['expires_at'] or 0) <= self.c.clock())
        if ((check_version and goal['version'] != candidate['goal_version'] and not expired_while_waiting)
                or goal['approval_id'] != candidate['approval_id']
                or intent != candidate['intent_revision'] or (current['source_message_id'] if current else '') != candidate['intent_source']
                or self.c.room_goal_recovery.epoch(goal['id']) != candidate['coordinator_epoch']):
            raise DevError('ROOM_INTENT_CHANGED', '工作在预览后已更新', 409)
        if self._plan_digest(goal, intent) != candidate['plan_digest']:
            raise DevError('ROOM_CONTINUATION_PLAN_CHANGED', '剩余工作计划在预览后已更新', 409)
        member = self.c.rooms.member(self.native.record(reference)['coordinator_member_id'], room['id'])
        return source, member, goal

    def save(self, raw, owner):
        args = validate(RoomPermissionContinuationSave, raw)
        fields = ('continue_selected', 'selected_task_ids', 'continuation_preview_id', 'policy_preview_digest')
        permissions = {key: value for key, value in args.items() if key not in fields}
        if not args['continue_selected']:
            return self.c.room_permissions.save(permissions, owner)
        with self.store.transaction():
            owner, room = self.c.rooms.owner(owner, args['conversation_id'])
            self.c.rooms.require_native(room)
            identifier = digest(['room-continuation', room['id'], owner.user_id, args['idempotency_key']])
            fingerprint = digest(args)
            existing = self.store.one('SELECT * FROM room_continuation_batches WHERE id=?', (identifier,))
            if existing:
                if existing['fingerprint'] != fingerprint:
                    raise DevError('IDEMPOTENCY_CONFLICT', '续办请求键已经用于不同选择', 409)
                from hub.collaboration.history_retention import RETIRED_RESPONSE
                receipt_id = digest([owner.space_id, owner.user_id, owner.grant_id,
                                     'room_permissions', permissions['idempotency_key']])
                receipt = self.store.one('SELECT result FROM room_mutations WHERE id=?', (receipt_id,))
                result = json.loads(receipt['result']) if receipt and receipt['result'] != RETIRED_RESPONSE else {}
            else:
                preview = self.store.one('''SELECT * FROM room_continuation_previews WHERE id=?
                    AND conversation_id=? AND owner_id=?''', (args['continuation_preview_id'], room['id'], owner.user_id))
                if (not preview or preview['expires_at'] <= self.c.clock()
                        or self.store.one('SELECT 1 FROM room_continuation_batches WHERE preview_id=?', (preview['id'],))):
                    raise DevError('ROOM_CONTINUATION_PREVIEW_EXPIRED', '续办清单已过期，请重新核对', 409)
                proposed = self._policy(permissions, owner, room)
                policy_digest = digest([room['id'], owner.user_id, args['expected_version'], proposed])
                if preview['policy_digest'] != policy_digest or args['policy_preview_digest'] != policy_digest:
                    raise DevError('ROOM_CONTINUATION_PREVIEW_CHANGED', '权限或续办预览已变化，请重新核对', 409)
                candidates = {item['task_id']: item for item in json.loads(preview['candidates'])}
                if not set(args['selected_task_ids']) <= candidates.keys():
                    raise DevError('ROOM_CONTINUATION_SELECTION_CHANGED', '续办选择超出已核对的清单', 409)
                selected = []
                for task_id in args['selected_task_ids']:
                    candidate = candidates[task_id]
                    try:
                        self._check_candidate(candidate, room, owner)
                        selected.append((candidate, 'pending', ''))
                    except DevError as exc:
                        selected.append((candidate, 'skipped', exc.code))
                result = self.c.room_permissions.save(permissions, owner)
                snapshot = {**asdict(owner), 'scopes': [], 'token_hash': '', 'session_hash': ''}
                self.store.execute('INSERT INTO room_continuation_batches VALUES (?,?,?,?,?,?,?,?)',
                    (identifier, room['id'], owner.user_id, preview['id'], fingerprint, result['version'],
                     canonical(snapshot), self.c.clock()))
                for candidate, state, reason in selected:
                    candidate = {key: value for key, value in candidate.items()
                                 if key not in {'label', 'remaining_operations', 'selected_by_default'}}
                    if state == 'pending':
                        try:
                            source, member, goal = self._check_candidate(candidate, room, owner, check_version=False)
                            if not member:
                                raise DevError('ROOM_RECOVERY_MEMBER_UNAVAILABLE', '续办消息当前没有主责成员', 409)
                            proof = self.native.input_authority.continuation_evidence(source, owner, member)
                            if not proof or not proof['room_rules']:
                                raise DevError('ROOM_CONTINUATION_SCOPE_DENIED', '原来源当前没有获准的续办权限', 403)
                            candidate.update(proof=proof, approved_member_id=member['id'])
                            if goal:
                                candidate.update(goal_version=goal['version'], goal_state=goal['state'])
                        except DevError as exc:
                            state, reason = 'skipped', exc.code
                    self.store.execute('INSERT INTO room_continuation_items VALUES (?,?,?,?,?,?)',
                        (identifier, candidate['task_id'], canonical(candidate), state, reason, 0))
        self.recover(limit=8, batch_id=identifier)
        return {**self.c.room_permissions.read(room['id'], owner),
                'blocked_projects': result.get('blocked_projects', []),
                'continuation': self.status(room['id'], identifier, owner)}

    def status(self, conversation_id, identifier, owner):
        owner, room = self.c.rooms.owner(owner, conversation_id)
        batch = self.store.one('''SELECT * FROM room_continuation_batches WHERE id=? AND conversation_id=? AND owner_id=?''',
                               (identifier, room['id'], owner.user_id))
        if not batch:
            raise DevError('ROOM_CONTINUATION_NOT_FOUND', '未找到此房间的续办回执', 404)
        rows = self.store.all('SELECT task_id,state,reason_code FROM room_continuation_items WHERE batch_id=? ORDER BY rowid',
                              (identifier,))
        counts = {state: sum(item['state'] == state for item in rows) for state in
                  ('continued', 'waiting_operations', 'skipped', 'pending')}
        return {'batch_id': identifier, 'policy_version': batch['policy_version'],
                'state': 'pending' if counts['pending'] or counts['waiting_operations'] else 'complete',
                'results': rows, 'counts': counts}

    def _save_proof(self, batch, candidate, source, member, goal, epoch, evidence):
        return self.store.execute('''INSERT INTO room_continuation_proofs
            (batch_id,task_id,message_id,member_id,goal_id,epoch,evidence,digest) VALUES (?,?,?,?,?,?,?,?)''',
            (batch['id'], candidate['task_id'], source['id'], member['id'], goal['id'] if goal else '', epoch,
             canonical(evidence), digest(evidence))).lastrowid

    @staticmethod
    def _proof(row):
        if not row:
            return None
        evidence = json.loads(row['evidence'])
        if digest(evidence) != row['digest']:
            raise DevError('ROOM_CONTINUATION_EVIDENCE_CHANGED', '续办许可记录已改变', 403)
        return evidence

    def source_proof(self, source, member):
        if member is None:
            identifier = json.loads(source['body']).get('room_member_id')
            if not identifier:
                return None
            member = {'id': identifier}
        row = self.store.one('''SELECT * FROM room_continuation_proofs WHERE message_id=? AND member_id=? AND goal_id=''
            ORDER BY sequence DESC LIMIT 1''', (source['id'], member['id']))
        return self._proof(row)

    def work_proof(self, goal, source):
        row = self.store.one('''SELECT * FROM room_continuation_proofs WHERE goal_id=? AND message_id=?
            AND epoch<=? ORDER BY epoch DESC,sequence DESC LIMIT 1''',
            (goal['id'], source['id'], self.c.room_goal_recovery.epoch(goal['id'])))
        if row:
            return self._proof(row)
        # A selected waiting source may create its first Goal afterward. The
        # original member recorded by that Goal owns the source continuation.
        reference = self.native.reference(goal)
        if reference:
            original = self.native.original(reference)
            return self.source_proof(source, {'id': original['coordinator_member_id']})
        return None

    def _approve_source(self, batch, candidate, room, owner, source, member, evidence):
        if member is None:
            raise DevError('ROOM_RECOVERY_MEMBER_UNAVAILABLE', '续办消息当前没有主责成员', 409)
        body = json.loads(source['body'])
        waiting = self.store.one('SELECT 1 FROM room_waiting_inputs WHERE message_id=?', (source['id'],))
        if body.get('room_member_id') != member['id']:
            body['room_member_id'] = member['id']
            self.store.execute('UPDATE collaboration_messages SET body=? WHERE id=?', (canonical(body), source['id']))
        sequence = self._save_proof(batch, candidate, source, member, None, 0, evidence)
        if waiting:
            # Let the regular dispatcher preserve ordering and pause behavior.
            # This explicit approval is bound to the member reviewed here.
            self.store.execute('UPDATE room_waiting_inputs SET member_id=?,follow_lead=0 WHERE message_id=?',
                               (member['id'], source['id']))
        else:
            self.c.room_consumers.ack_message(member, source['id'])
            self.c.room_consumers.enqueue_ref(member, 'continuation_source', source['id'], sequence)
        self.c.rooms.change(room['id'], 'message', source['id'], source['version'])

    def _process(self, batch, candidate, owner):
        owner, room = self.c.rooms.owner(owner, batch['conversation_id'])
        self.c.rooms.require_native(room)
        policy = self.c.room_permissions.row(room['id'])
        if not policy or policy['version'] != batch['policy_version']:
            raise DevError('ROOM_CONTINUATION_POLICY_CHANGED', '此次续办批准后房间权限已改变', 409)
        source, member, goal = self._check_candidate(candidate, room, owner)
        if not member or member['id'] != candidate['approved_member_id'] or member['status'] == 'removed':
            raise DevError('ROOM_CONTINUATION_RECIPIENT_CHANGED', '续办批准的成员已改变，请重新核对', 409)
        evidence = candidate['proof']
        if not self.native.input_authority.rules(source, member, proof=evidence):
            raise DevError('ROOM_CONTINUATION_SCOPE_DENIED', '原来源当前没有获准的续办权限', 403)
        if goal and any(op['state'] not in TERMINAL_OPERATIONS for op in self.c.coordination.operations(goal)):
            return 'waiting_operations', 'WORK_OPERATION_PENDING'
        if goal:
            self._save_proof(batch, candidate, source, member, goal, candidate['coordinator_epoch'] + 1, evidence)
            self.c.room_goal_recovery.continue_approved(goal, source, owner, room, batch['id'], evidence)
        else:
            self._approve_source(batch, candidate, room, owner, source, member, evidence)
        return 'continued', ''

    def recover(self, *, limit=8, batch_id=''):
        items = self.store.all('''SELECT i.*,b.conversation_id,b.policy_version,b.owner_principal,b.id FROM room_continuation_items i
            JOIN room_continuation_batches b ON b.id=i.batch_id WHERE i.state IN ('pending','waiting_operations')
            AND i.retry_at<=?''' + (' AND i.batch_id=?' if batch_id else '') + ' ORDER BY i.retry_at,b.created,i.rowid LIMIT ?',
            (self.c.clock(), *((batch_id,) if batch_id else ()), limit))
        for item in items:
            identifiers = (item['batch_id'], item['task_id'])
            try:
                with self.store.transaction():
                    current = self.store.one('SELECT state,retry_at FROM room_continuation_items WHERE batch_id=? AND task_id=?', identifiers)
                    if current['state'] != item['state'] or current['retry_at'] != item['retry_at']:
                        continue
                    owner = Principal(**{**json.loads(item['owner_principal']), 'scopes': set()})
                    state, reason = self._process(item, json.loads(item['candidate']), owner)
                    self.store.execute('UPDATE room_continuation_items SET state=?,reason_code=?,retry_at=? WHERE batch_id=? AND task_id=?',
                        (state, reason, self.c.clock() + 30 if state == 'waiting_operations' else 0, *identifiers))
            except DevError as exc:
                with self.store.transaction():
                    transient = exc.code == 'ROOM_RECOVERY_MEMBER_UNAVAILABLE'
                    self.store.execute('''UPDATE room_continuation_items SET state=?,reason_code=?,retry_at=?
                        WHERE batch_id=? AND task_id=? AND state=? AND retry_at=?''',
                        ('pending' if transient else 'skipped', exc.code, self.c.clock() + 30 if transient else 0,
                         *identifiers, item['state'], item['retry_at']))
        return len(items)
