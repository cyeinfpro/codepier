"""Owner-approved goals and flexible work, never authority derived from prose.

Managed calls intersect the immutable human approval with live human/grant/project
rights and reuse Runtime admission and Agent policy. Interactive grants remain
independent; a goal pause is not revocation of their unrelated access.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict

from hub.principal import Principal, refresh_principal
from hub.collaboration.common import canonical, digest, redact, validate, read_cursor, sign_cursor
from shared import collaboration_contracts as contracts
from shared.util import DevError

WORK_EVENT = 'codepier.collaboration.work_available.v1'
TERMINAL = {'succeeded', 'failed', 'blocked', 'cancelled', 'expired'}
BOUNDARY = {'shell': 'execution_account_not_project_sandbox',
            'scope': 'managed_goal_calls_only', 'chat_identity_verified': False,
            'responsibility_labels_are_iam_roles': False,
            'participants_require_all_project_read_access': True}


class CoordinationService:
    def __init__(self, collaboration):
        self.c, self.store, self.runtime = collaboration, collaboration.store, collaboration.runtime

    def object(self, room, goal_id):
        row = self.store.one('SELECT * FROM coordination_goals WHERE id=? AND room_id=?', (goal_id, room['id']))
        if not row:
            raise DevError('GOAL_NOT_FOUND', '当前项目中没有这个协作目标', 404)
        return row

    def scope(self, principal, args, *, active=False):
        principal, room = self.c.scope(principal, args)
        goal = self.object(room, args['goal_id'])
        self.authorize(principal, room, goal, active=active)
        return principal, room, goal

    def authorize(self, principal, room, goal, *, active=False):
        principal = refresh_principal(self.store, principal)
        if principal.user_id != room['owner_user_id'] or principal.space_id != room['space_id']:
            raise DevError('GOAL_OWNER_MISMATCH', '目标只属于当前所有者自己的连接', 403)
        spec = json.loads(goal['spec'])
        self.c.conversations.object(principal, goal['conversation_id'])
        # A narrower new proposal cannot relabel old multi-project evidence.
        source_projects = set(spec['project_ids'])
        for history in self.store.all('SELECT spec FROM coordination_approvals WHERE goal_id=?', (goal['id'],)):
            source_projects.update(json.loads(history['spec'])['project_ids'])
        for project_id in source_projects:
            self.runtime.project(project_id, principal)
        if principal.grant_id:
            self.c.notification_reader(principal)
            if principal.grant_id not in spec['participant_grant_ids']:
                raise DevError('GOAL_PARTICIPANT_REQUIRED', '当前连接不在这个目标的明确参与范围内', 403)
        else:
            self.c.owner(principal)
        if active:
            self.c.delegation.authorize_goal(principal, room, goal)
            self.c.live_room(room)
            if goal['state'] != 'active' or not goal['approval_id'] or (goal['expires_at'] or 0) <= self.c.clock():
                raise DevError('GOAL_INACTIVE', '目标未批准、已暂停或已到期；不能开始新步骤', 409)
            approval = self.store.one('SELECT * FROM coordination_approvals WHERE id=? AND goal_id=?',
                                      (goal['approval_id'], goal['id']))
            if not approval or approval['digest'] != goal['digest'] or approval['expires_at'] <= self.c.clock():
                raise DevError('GOAL_APPROVAL_CHANGED', '原目标批准已失效', 409)
            owner = refresh_principal(self.store, Principal(**{**json.loads(approval['principal']), 'scopes': set()}))
            self.c.owner(owner)
            for snapshot in spec['projects']:
                current = self.runtime.project(snapshot['project_id'], owner)
                if self.project_snapshot(current) != snapshot:
                    raise DevError('GOAL_PROJECT_CHANGED', '项目执行位置或设置已改变，需要重新审阅目标', 409)
                for capability in spec['capabilities']:
                    self.require_capability(owner, capability, current['id'])
        return principal

    @staticmethod
    def project_snapshot(project):
        return {'project_id': project['id'], **{key: project[key] for key in ('device_id', 'root', 'mode', 'allow_tasks')}}

    def require_capability(self, principal, capability, project_id):
        principal = refresh_principal(self.store, principal)
        if capability not in principal.scopes:
            raise DevError('INSUFFICIENT_SCOPE', '连接当前缺少工作项所需能力', 403, required_scope=capability)
        self.runtime.authorize(principal, capability, project_id=project_id)

    def work_capabilities(self, principal, goal, item):
        capabilities = json.loads(item['required_capabilities'])
        if not set(capabilities) <= set(json.loads(goal['spec'])['capabilities']):
            raise DevError('GOAL_CAPABILITY_DENIED', '工作项能力超过目标批准上限', 403)
        for capability in capabilities:
            self.require_capability(principal, capability, item['project_id'])

    def candidate(self, args, principal, room):
        conversation = self.c.conversations.resolve(principal, room, args)
        projects = []
        for project_id in sorted(args['project_ids']):
            project = self.runtime.project(project_id, principal)
            if project['id'] != project_id:
                raise DevError('PROJECT_ID_REQUIRED', '目标必须绑定明确的项目 ID', 422)
            if not self.store.one('''SELECT 1 FROM conversation_projects p JOIN collaboration_rooms r ON r.id=p.room_id
                WHERE p.conversation_id=? AND r.project_id=? AND r.environment_id=?''',
                (conversation['id'], project_id, room['environment_id'])):
                raise DevError('GOAL_PROJECT_NOT_IN_ROOM', '目标项目必须已明确加入当前聊天室和环境', 409)
            projects.append(self.project_snapshot(project))
        if room['project_id'] not in args['project_ids']:
            raise DevError('GOAL_ANCHOR_REQUIRED', '目标范围需要包含当前项目', 422)
        for grant_id in args['participant_grant_ids']:
            participant = self.c.grant_reader(room, grant_id)
            for project_id in args['project_ids']:
                self.runtime.project(project_id, participant)
        if principal.grant_id and principal.grant_id not in args['participant_grant_ids']:
            raise DevError('GOAL_PARTICIPANT_REQUIRED', '提议连接必须在目标参与者集合中', 403)
        coordinator = args['coordinator_grant_id'] or args['participant_grant_ids'][0]
        if coordinator not in args['participant_grant_ids']:
            raise DevError('GOAL_PARTICIPANT_REQUIRED', '初始协调连接必须在目标参与者中', 403)
        capabilities = sorted(args['capabilities'])
        if 'read' not in capabilities:
            raise DevError('GOAL_READ_REQUIRED', '协作目标必须包含读取能力', 422)
        return {key: redact(args[key]) for key in ('objective', 'acceptance', 'duration_seconds', 'budget')} | {
            'project_ids': sorted(args['project_ids']), 'participant_grant_ids': sorted(args['participant_grant_ids']),
            'capabilities': capabilities, 'projects': projects, 'coordinator_grant_id': coordinator, 'execution_boundary': BOUNDARY}

    def view(self, goal, principal):
        spec = json.loads(goal['spec'])
        anchor = self.store.one('SELECT project_id,environment_id FROM collaboration_rooms WHERE id=?', (goal['room_id'],))
        count = self.store.one('SELECT COUNT(*) AS n FROM coordination_work WHERE goal_id=?', (goal['id'],))['n']
        delegation = self.c.delegation.goal_metadata(goal)
        return {key: goal[key] for key in ('id', 'room_id', 'conversation_id', 'digest', 'version',
                'state', 'approval_id', 'expires_at', 'created', 'updated')} | spec | {
                'can_approve': bool(principal.admin and not principal.grant_id and not delegation),
                'delegation': delegation,
                'anchor_project_id': anchor['project_id'], 'environment_id': anchor['environment_id'],
                'usage': {'work_items': count, 'steps': goal['steps'], 'messages': goal['messages']}}

    def mutation(self, principal, room, action, args, perform):
        result = self.c.mutation(principal, room, action, args, perform)
        # Stored idempotency receipts are evidence, never stale UI authority.
        if 'goal' in result:
            goal = self.object(room, result['goal']['id'])
            self.authorize(principal, room, goal)
            result = {**result, 'goal': self.view(goal, principal)}
            if 'operations' in result:
                result['operations'] = self.operations(goal)
        if 'work_item' in result:
            goal = self.object(room, result['work_item']['goal_id'])
            self.authorize(principal, room, goal)
            result = {**result, 'work_item': self.work_view(self.work(goal, result['work_item']['id']))}
        return result

    def create(self, raw, principal):
        args = validate(contracts.GoalCreate, raw)
        with self.store.transaction():
            principal, room = self.c.scope(principal, args)
            spec = self.candidate(args, principal, room)
            def save():
                identifier, now = uuid.uuid4().hex, self.c.clock()
                self.store.execute('''INSERT INTO coordination_goals
                    (id,room_id,conversation_id,spec,digest,created,updated) VALUES (?,?,?,?,?,?,?)''',
                    (identifier, room['id'], args['conversation_id'], canonical(spec), digest(spec), now, now))
                return {'goal': self.view(self.object(room, identifier), principal)}
            return self.mutation(principal, room, 'goal_create', args, save)

    def update(self, raw, principal):
        args = validate(contracts.GoalUpdate, raw)
        with self.store.transaction():
            principal, room, goal = self.scope(principal, args)
            if self.c.delegation.goal_link(goal):
                raise DevError('DELEGATION_IMMUTABLE', '原消息委托不能改写目标；请在明确设置范围内发送新委托', 409)
            if args['conversation_id'] != goal['conversation_id']:
                raise DevError('GOAL_CONVERSATION_CHANGED', '不能移动既有目标的聊天室', 409)
            spec = self.candidate(args, principal, room)
            def save():
                self.version(goal, args)
                self.stop_work(goal, 'GOAL_REVISED')
                self.store.execute("""UPDATE coordination_goals SET spec=?,digest=?,state='proposed',
                    version=version+1,approval_id=NULL,expires_at=NULL,updated=? WHERE id=?""",
                    (canonical(spec), digest(spec), self.c.clock(), goal['id']))
                return {'goal': self.view(self.object(room, goal['id']), principal)}
            return self.mutation(principal, room, 'goal_update', args, save)

    @staticmethod
    def version(row, args):
        if row['version'] != args['expected_version']:
            raise DevError('STALE_VERSION', '内容已改变，请重新读取后确认', 409)

    def approve(self, raw, principal):
        args = validate(contracts.GoalApprove, raw)
        with self.store.transaction():
            principal, room, goal = self.scope(principal, args)
            self.c.owner(principal)
            link = self.c.delegation.goal_link(goal)
            if link and link['approval_id']:
                raise DevError('DELEGATION_IMMUTABLE', '原消息委托不能重新批准；请核对已有操作后发送新委托', 409)
            spec = json.loads(goal['spec'])
            # Current owner and every connection must already have every approved
            # capability. This approval creates no IAM grant or notification route.
            for snapshot in spec['projects']:
                project = self.runtime.project(snapshot['project_id'], principal)
                if self.project_snapshot(project) != snapshot:
                    raise DevError('GOAL_PROJECT_CHANGED', '项目设置已改变，请先更新目标提议', 409)
                for capability in spec['capabilities']:
                    self.require_capability(principal, capability, project['id'])
                    if capability in {'write', 'execute'} and project['mode'] != 'write':
                        raise DevError('READ_ONLY', '目标包含只读项目', 403)
                    if capability == 'execute' and not project['allow_tasks']:
                        raise DevError('TASKS_DISABLED', '目标项目没有允许执行任务', 403)
                for grant_id in spec['participant_grant_ids']:
                    participant = self.c.grant_reader(room, grant_id)
                    self.authorize(participant, room, goal)
                    self.runtime.project(project['id'], participant)
            def save():
                self.version(goal, args)
                self.c.live_room(room)
                if goal['digest'] != args['digest'] or goal['state'] == 'cancelled':
                    raise DevError('GOAL_APPROVAL_CHANGED', '目标摘要已改变或目标已取消', 409)
                count = self.store.one('SELECT COUNT(*) AS n FROM coordination_work WHERE goal_id=?', (goal['id'],))['n']
                if count >= spec['budget']['max_work_items']:
                    raise DevError('GOAL_BUDGET_EXCEEDED', '重新批准需要初始协调工作预算；请先更新目标预算', 409)
                self.stop_work(goal, 'GOAL_REAPPROVED')
                identifier, now = uuid.uuid4().hex, self.c.clock()
                expires = now + spec['duration_seconds']
                snapshot = asdict(principal)
                snapshot.update(scopes=[], session_hash='', token_hash='')
                self.store.execute('''INSERT INTO coordination_approvals VALUES (?,?,?,?,?,?,?,?)''',
                    (identifier, goal['id'], goal['version'], goal['digest'], goal['spec'], canonical(snapshot), now, expires))
                self.store.execute("""UPDATE coordination_goals SET state='active',version=version+1,
                    approval_id=?,expires_at=?,updated=? WHERE id=?""", (identifier, expires, now, goal['id']))
                kickoff = uuid.uuid4().hex
                self.store.execute('''INSERT INTO coordination_work
                    (id,goal_id,approval_id,project_id,objective,acceptance,responsibility,assignee_grant_id,created,updated)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''', (kickoff, goal['id'], identifier, room['project_id'],
                    spec['objective'], spec['acceptance'], 'coordinator', spec['coordinator_grant_id'], now, now))
                current = self.object(room, goal['id'])
                self.emit_work(room, current, self.work(current, kickoff))
                return {'goal': self.view(self.object(room, goal['id']), principal)}
            return self.mutation(principal, room, 'goal_approve', args, save)

    def operations(self, goal):
        return self.store.all('''SELECT o.id,o.id AS operation_id,o.state,o.tool,o.project_id,o.cancel_requested,
            c.work_item_id,c.attempt,c.cancel_note FROM coordination_operations c JOIN operations o ON o.id=c.operation_id
            WHERE c.goal_id=? ORDER BY c.created,o.id''', (goal['id'],))

    def stop_work(self, goal, reason):
        now = self.c.clock()
        self.store.execute("""UPDATE coordination_work SET state='blocked',reason_code=?,lease_until=NULL,
            fencing_token=fencing_token+1,version=version+1,updated=?
            WHERE goal_id=? AND state IN ('queued','leased','running')""", (reason, now, goal['id']))
        for op in self.operations(goal):
            if op['state'] not in {'queued', 'running', 'reconnecting', 'cancelling', 'unknown'}:
                continue
            full = self.store.one('SELECT * FROM operations WHERE id=?', (op['id'],))
            if not full['attempts']:
                self.store.execute('UPDATE operations SET cancel_requested=1 WHERE id=?', (op['id'],))
                self.runtime.complete(full, {'ok': False, 'error': {'code': 'CANCELLED', 'message': '目标已停止，操作尚未投递'}})
                note = 'cancelled_before_delivery'
            elif op['tool'] in {'exec', 'read'}:
                self.store.execute("UPDATE operations SET cancel_requested=1,state='cancelling',next_attempt=0 WHERE id=?", (op['id'],))
                note = 'cancel_requested_in_flight_not_proven_stopped'
            else:
                note = 'in_flight_file_operation_tracked_not_cancellable'
            self.store.execute('UPDATE coordination_operations SET cancel_note=? WHERE operation_id=?', (note, op['id']))
        self.runtime.wake_delivery()

    def control(self, raw, principal):
        args = validate(contracts.GoalControl, raw)
        with self.store.transaction():
            principal, room, goal = self.scope(principal, args)
            self.c.owner(principal)
            def save():
                self.version(goal, args)
                if goal['state'] == ('paused' if args['action'] == 'pause' else 'cancelled'):
                    return {'goal': self.view(goal, principal), 'operations': self.operations(goal)}
                self.stop_work(goal, 'GOAL_' + args['action'].upper())
                self.store.execute('UPDATE coordination_goals SET state=?,version=version+1,updated=? WHERE id=?',
                    ('paused' if args['action'] == 'pause' else 'cancelled', self.c.clock(), goal['id']))
                return {'goal': self.view(self.object(room, goal['id']), principal), 'operations': self.operations(goal)}
            return self.mutation(principal, room, 'goal_control', args, save)

    def options(self, principal, room, args):
        conversation = self.c.conversations.resolve(principal, room, args)
        rows = self.store.all('SELECT id,label FROM grants WHERE user_id=? AND space_id=? AND revoked=0 ORDER BY id LIMIT 129',
                              (principal.user_id, principal.space_id))
        participants = []
        for row in rows[:128]:
            try:
                actor = self.c.grant_reader(room, row['id'])
            except DevError:
                continue
            projects = []
            for candidate in self.c.conversations.visible(principal, conversation):
                try:
                    self.runtime.project(candidate['project_id'], actor)
                    projects.append(candidate['project_id'])
                except DevError:
                    continue
            project_capabilities = {}
            for project_id in projects:
                available = []
                for capability in ('read', 'write', 'execute'):
                    try:
                        self.require_capability(actor, capability, project_id)
                        available.append(capability)
                    except DevError:
                        continue
                project_capabilities[project_id] = available
            participants.append({'grant_id': row['id'], 'label': row['label'] or row['id'],
                                 'scopes': sorted(actor.scopes), 'project_ids': projects,
                                 'project_capabilities': project_capabilities,
                                 'chat_identity_verified': False, 'online': 'unknown'})
        return {'participants': participants, 'truncated': len(rows) > 128, 'scan_limit': 128,
                'capabilities': ['read', 'write', 'execute'],
                'can_approve': bool(principal.admin and not principal.grant_id), 'execution_boundary': BOUNDARY,
                'budget_defaults': contracts.CoordinationBudget().model_dump(),
                'work_event': WORK_EVENT, 'subscription_required': True}

    def read(self, raw, principal):
        args = validate(contracts.GoalRead, raw)
        with self.store.transaction(immediate=False):
            principal, room, goal = self.scope(principal, args)
            return self.detail(goal, principal)

    def detail(self, goal, principal):
        rows = self.store.all('SELECT * FROM coordination_work WHERE goal_id=? ORDER BY created,id', (goal['id'],))
        messages = self.store.all('SELECT * FROM coordination_messages WHERE goal_id=? ORDER BY created,id', (goal['id'],))
        for message in messages:
            for key in ('mention_grant_ids', 'provenance_project_ids'):
                message[key] = json.loads(message[key])
            message['body_text'] = message.pop('body')
        return {'goal': self.view(goal, principal), 'can_approve': bool(principal.admin and not principal.grant_id and not self.c.delegation.goal_link(goal)),
                'work_items': [self.work_view(row) for row in rows], 'messages': messages, 'operations': self.operations(goal)}

    def listing(self, args, principal, room):
        if args['kind'] == 'coordination_options':
            return self.options(principal, room, args)
        if args['kind'] == 'coordination_goal':
            goal = self.object(room, args['id'])
            self.authorize(principal, room, goal)
            if args.get('conversation_id') and args['conversation_id'] != goal['conversation_id']:
                raise DevError('GOAL_NOT_FOUND', '目标不属于当前聊天室', 404)
            return self.detail(goal, principal)
        conversation = self.c.conversations.resolve(principal, room, args)
        binding = digest(['coordination_goals', conversation['id'], principal.grant_id or principal.user_id])
        after = read_cursor(self.c.secret, binding, args['cursor']) if args['cursor'] else ''
        if not isinstance(after, str):
            raise DevError('INVALID_CURSOR', '目标分页游标无效', 400)
        rows = self.store.all('SELECT * FROM coordination_goals WHERE conversation_id=? AND id>? ORDER BY id LIMIT ?',
                              (conversation['id'], after, args['limit'] + 1))
        selected, items = rows[:args['limit']], []
        for row in selected:
            try:
                anchor = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (row['room_id'],))
                self.authorize(principal, anchor, row)
            except DevError:
                continue
            items.append(self.view(row, principal))
        return {'items': items, 'next_cursor': sign_cursor(self.c.secret, binding, selected[-1]['id'])
                if len(rows) > args['limit'] else None}

    def work(self, goal, identifier):
        row = self.store.one('SELECT * FROM coordination_work WHERE id=? AND goal_id=?', (identifier, goal['id']))
        if not row:
            raise DevError('WORK_NOT_FOUND', '此工作项不属于当前目标', 404)
        return row

    def work_view(self, row):
        return {**row, 'dependencies': json.loads(row['dependencies']),
                'required_capabilities': json.loads(row['required_capabilities']),
                'result': json.loads(row['result']) if row['result'] else None,
                'provenance_project_ids': json.loads(row['result']).get('provenance_project_ids', [row['project_id']]) if row['result'] else [row['project_id']]}

    def participant(self, room, goal, grant_id):
        if grant_id not in json.loads(goal['spec'])['participant_grant_ids']:
            raise DevError('GOAL_PARTICIPANT_REQUIRED', '目标未批准此参与连接', 403)
        actor = self.c.grant_reader(room, grant_id)
        return self.authorize(actor, room, goal, active=True)

    def work_create(self, raw, principal):
        args = validate(contracts.WorkCreate, raw)
        with self.store.transaction():
            principal, room, goal = self.scope(principal, args, active=True)
            spec = json.loads(goal['spec'])
            if args['target_project_id'] not in spec['project_ids']:
                raise DevError('GOAL_PROJECT_DENIED', '工作项项目不在原目标批准范围内', 403)
            assignee = self.participant(room, goal, args['assignee_grant_id'])
            self.work_capabilities(assignee, goal, {'required_capabilities': canonical(args['required_capabilities']),
                                                   'project_id': args['target_project_id']})
            dependencies = list(dict.fromkeys(args['dependencies']))
            for identifier in dependencies:
                dependency = self.work(goal, identifier)
                if dependency['approval_id'] != goal['approval_id']:
                    raise DevError('WORK_APPROVAL_CHANGED', '依赖不属于当前目标批准', 409)
            def save():
                count = self.store.one('SELECT COUNT(*) AS n FROM coordination_work WHERE goal_id=?', (goal['id'],))['n']
                if count >= spec['budget']['max_work_items']:
                    raise DevError('GOAL_BUDGET_EXCEEDED', '目标工作项预算已用尽', 409)
                identifier, now = uuid.uuid4().hex, self.c.clock()
                self.store.execute('''INSERT INTO coordination_work
                    (id,goal_id,approval_id,project_id,objective,acceptance,responsibility,assignee_grant_id,dependencies,required_capabilities,created,updated)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (identifier, goal['id'], goal['approval_id'], args['target_project_id'], redact(args['objective']),
                     redact(args['acceptance']), redact(args['responsibility']), args['assignee_grant_id'], canonical(dependencies), canonical(args['required_capabilities']), now, now))
                item = self.work(goal, identifier)
                self.emit_work(room, goal, item)
                return {'work_item': self.work_view(item)}
            return self.mutation(principal, room, 'work_create', args, save)

    def work_assign(self, raw, principal):
        args = validate(contracts.WorkAssign, raw)
        with self.store.transaction():
            principal, room, goal = self.scope(principal, args, active=True)
            item = self.work(goal, args['work_item_id'])
            assignee = self.participant(room, goal, args['assignee_grant_id'])
            self.work_capabilities(assignee, goal, item)
            def save():
                self.version(item, args)
                if item['state'] != 'queued' or item['approval_id'] != goal['approval_id']:
                    raise DevError('WORK_NOT_ASSIGNABLE', '只有当前批准的未领取工作可以交接', 409)
                self.store.execute('UPDATE coordination_work SET assignee_grant_id=?,version=version+1,updated=? WHERE id=?',
                                    (args['assignee_grant_id'], self.c.clock(), item['id']))
                current = self.work(goal, item['id'])
                self.emit_work(room, goal, current)
                return {'work_item': self.work_view(current)}
            return self.mutation(principal, room, 'work_assign', args, save)

    def work_claim(self, raw, principal):
        args = validate(contracts.WorkClaim, raw)
        with self.store.transaction():
            principal, room, goal = self.scope(principal, args, active=True)
            item = self.work(goal, args['work_item_id'])
            if not principal.grant_id or item['assignee_grant_id'] != principal.grant_id:
                raise DevError('WORK_ASSIGNEE_REQUIRED', '工作项没有分配给当前连接', 403)
            def take():
                self.version(item, args)
                self.work_capabilities(principal, goal, item)
                spec, now = json.loads(goal['spec']), self.c.clock()
                if item['approval_id'] != goal['approval_id'] or item['state'] != 'queued':
                    raise DevError('WORK_NOT_CLAIMABLE', '工作项当前不可领取', 409)
                if item['attempt'] >= spec['budget']['max_attempts']:
                    raise DevError('WORK_ATTEMPTS_EXHAUSTED', '原工作项领取次数已用尽', 409)
                for identifier in json.loads(item['dependencies']):
                    if self.work(goal, identifier)['state'] != 'succeeded':
                        raise DevError('WORK_DEPENDENCY_PENDING', '先等待依赖工作项真实完成', 409)
                # An ambiguous old operation must settle before another attempt.
                if self.pending_operations(item):
                    raise DevError('WORK_OPERATION_PENDING', '原尝试还有未完成操作，不能重复执行', 409)
                attempt, fence = item['attempt'] + 1, item['fencing_token'] + 1
                until = min(goal['expires_at'], now + spec['budget']['lease_seconds'])
                self.store.execute("""UPDATE coordination_work SET state='leased',attempt=?,fencing_token=?,
                    lease_until=?,version=version+1,reason_code='',updated=? WHERE id=?""",
                    (attempt, fence, until, now, item['id']))
                self.store.execute('INSERT INTO coordination_attempts VALUES (?,?,?,?,?)',
                    (item['id'], attempt, fence, principal.grant_id, now))
                self.c.delegation.project_progress(room, goal, self.work(goal, item['id']),
                    '已领取任务，开始处理。', principal, 'delegation-received:' + item['id'] + ':' + str(attempt),
                    system_receipt=True)
                return {'work_item': self.work_view(self.work(goal, item['id']))}
            return self.mutation(principal, room, 'work_claim', args, take)

    def pending_operations(self, item):
        return self.store.one("""SELECT 1 FROM coordination_operations c JOIN operations o ON o.id=c.operation_id
            WHERE c.work_item_id=? AND o.state IN ('queued','running','reconnecting','cancelling','unknown') LIMIT 1""", (item['id'],))

    def lease(self, principal, goal, args):
        item = self.work(goal, args['work_item_id'])
        if not principal.grant_id or item['assignee_grant_id'] != principal.grant_id:
            raise DevError('WORK_ASSIGNEE_REQUIRED', '原工作租约不属于当前连接', 403)
        if (item['approval_id'] != goal['approval_id'] or item['state'] not in {'leased', 'running'}
                or item['attempt'] != args['attempt'] or item['fencing_token'] != args['fencing_token']
                or (item['lease_until'] or 0) <= self.c.clock()):
            raise DevError('WORK_LEASE_EXPIRED', '工作租约或执行世代已经失效', 409)
        self.work_capabilities(principal, goal, item)
        return item

    def heartbeat(self, raw, principal):
        args = validate(contracts.WorkLease, raw)
        with self.store.transaction():
            principal, room, goal = self.scope(principal, args, active=True)
            def extend():
                item = self.lease(principal, goal, args)
                until = min(goal['expires_at'], self.c.clock() + json.loads(goal['spec'])['budget']['lease_seconds'])
                self.store.execute('UPDATE coordination_work SET lease_until=?,updated=? WHERE id=?', (until, self.c.clock(), item['id']))
                return {'work_item': self.work_view(self.work(goal, item['id']))}
            return self.mutation(principal, room, 'work_heartbeat', args, extend)

    def progress(self, raw, principal):
        args = validate(contracts.WorkProgress, raw)
        with self.store.transaction():
            principal, room, goal = self.scope(principal, args, active=True)
            def save():
                item = self.lease(principal, goal, args)
                budget = json.loads(goal['spec'])['budget']
                if goal['messages'] >= budget['max_messages']:
                    raise DevError('GOAL_BUDGET_EXCEEDED', '目标通信预算已用尽，仍可提交真实最终结果', 409)
                if not self.c.delegation.goal_link(goal):
                    raise DevError('DELEGATION_REQUIRED', '原话题进展需要关联的房主任务；普通目标使用 goal_message', 409)
                identifier = 'delegation-progress:' + digest([goal['id'], item['id'], item['attempt'], args['idempotency_key']])
                self.c.delegation.project_progress(room, goal, item, args['summary'], principal, identifier)
                until = min(goal['expires_at'], self.c.clock() + budget['lease_seconds'])
                self.store.execute('UPDATE coordination_work SET lease_until=?,updated=? WHERE id=?', (until, self.c.clock(), item['id']))
                self.store.execute('UPDATE coordination_goals SET messages=messages+1 WHERE id=?', (goal['id'],))
                return {'message_id': identifier, 'work_item': self.work_view(self.work(goal, item['id']))}
            return self.mutation(principal, room, 'work_progress', args, save)

    def admit(self, raw, principal):
        args = validate(contracts.WorkExecute, raw)
        with self.store.transaction():
            principal, room, goal = self.scope(principal, args, active=True)
            spec = json.loads(goal['spec'])
            item = self.work(goal, args['work_item_id'])
            # Replays identify genuine admission, but never bypass current scope.
            if item['assignee_grant_id'] != principal.grant_id:
                raise DevError('WORK_ASSIGNEE_REQUIRED', '工作项不属于当前连接', 403)
            def execute():
                item = self.lease(principal, goal, args)
                from shared.contracts import TOOLS
                from hub.dispatch_plan import DeferredCall
                capability = TOOLS[args['tool']].scope
                if capability not in spec['capabilities'] or capability not in json.loads(item['required_capabilities']):
                    raise DevError('GOAL_CAPABILITY_DENIED', '目标没有批准此执行能力', 403)
                if goal['steps'] >= spec['budget']['max_steps']:
                    raise DevError('GOAL_BUDGET_EXCEEDED', '目标执行步骤预算已用尽', 409)
                supplied = args['arguments']
                if supplied.get('project', item['project_id']) != item['project_id']:
                    raise DevError('WORK_PROJECT_MISMATCH', '执行参数必须使用工作项的唯一项目', 403)
                target = self.c.delegation.target(principal, room, goal, args['tool'], supplied)
                if supplied.get('operation', 'file') != 'file':
                    raise DevError('GOAL_OPERATION_NOT_SUPPORTED', '此入口只支持基本文件操作和 exec', 422)
                if args['tool'] == 'exec' and supplied.get('task'):
                    raise DevError('GOAL_TASK_NOT_SUPPORTED', '此入口需要明确的受校验命令', 422)
                tool_args = {key: value for key, value in supplied.items() if key != 'operation'}
                tool_args.update(project=item['project_id'], idempotency_key='goal:' + digest([
                    goal['id'], item['id'], item['attempt'], args['idempotency_key']]))
                if args['tool'] == 'exec':
                    tool_args['target'] = target
                prepared = self.runtime._invoke(args['tool'], tool_args, principal)
                if not isinstance(prepared, DeferredCall) or prepared.action != 'dispatch':
                    raise DevError('GOAL_OPERATION_NOT_SUPPORTED', '请求不能通过已验证的项目执行路径', 422)
                if args['tool'] == 'exec':
                    remaining = max(1, int(min(item['lease_until'], goal['expires_at']) - self.c.clock()))
                    prepared.arguments['timeout_seconds'] = min(prepared.arguments['timeout_seconds'], remaining)
                    prepared.arguments['yield_seconds'] = 0
                if self.store.one('SELECT id FROM operations WHERE space_id=? AND actor=? AND idem=?',
                                  (principal.space_id, principal.actor, prepared.arguments['idempotency_key'])):
                    raise DevError('WORK_OPERATION_PROVENANCE', '不能把已存在的独立操作追认为目标执行', 409)
                operation_id, _ = self.runtime._admit_operation(args['tool'], prepared.arguments, prepared.project, principal, True)
                operation = self.store.one('SELECT * FROM operations WHERE id=?', (operation_id,))
                if (operation['grant_id'] != principal.grant_id or operation['project_id'] != item['project_id']
                        or operation['owner_user_id'] != principal.user_id):
                    raise DevError('WORK_OPERATION_MISMATCH', '持久操作身份不匹配', 409)
                self.store.execute('''INSERT INTO coordination_operations
                    (operation_id,goal_id,work_item_id,approval_id,attempt,fencing_token,project_id,grant_id,request_digest,created)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''', (operation_id, goal['id'], item['id'], goal['approval_id'],
                    item['attempt'], item['fencing_token'], item['project_id'], principal.grant_id, digest(args), self.c.clock()))
                self.store.execute('UPDATE operations SET deadline=MIN(deadline,?) WHERE id=?', (min(item['lease_until'], goal['expires_at']), operation_id))
                self.store.execute('UPDATE coordination_goals SET steps=steps+1 WHERE id=?', (goal['id'],))
                self.store.execute("UPDATE coordination_work SET state='running',updated=? WHERE id=?", (self.c.clock(), item['id']))
                return {'operation_id': operation_id, 'work_item_id': item['id'], 'attempt': item['attempt'],
                        'project_id': item['project_id']}
            return self.mutation(principal, room, 'work_execute', args, execute)

    async def execute(self, raw, principal):
        async with self.runtime.dispatch_lock:
            receipt = await self.store.run(self.admit, raw, principal)
        self.runtime.wake_delivery()
        try:
            operation = await self.store.run(self.runtime.operation, receipt['operation_id'], principal,
                                             {'include_output': False, 'include_result': False})
        except DevError as exc:
            # Admission already committed. Preserve its ID if authorization changes
            # across the await; never encourage another submission under a new key.
            raise DevError(exc.code, exc.message, exc.status, operation_id=receipt['operation_id']) from exc
        return {**receipt, 'state': operation['state'], 'pending': operation['pending'], 'operation': operation}

    def provenance(self, goal, identifiers, initial=()):
        projects = set(initial)
        for identifier in identifiers:
            item = self.work(goal, identifier)
            if item['state'] != 'succeeded' or not item['result']:
                raise DevError('WORK_INPUT_NOT_READY', '汇总来源必须是同目标的已完成工作项', 409)
            projects.update(json.loads(item['result'])['provenance_project_ids'])
        return sorted(projects)

    def result(self, raw, principal):
        args = validate(contracts.WorkResult, raw)
        with self.store.transaction():
            principal, room, goal = self.scope(principal, args, active=True)
            def save():
                item = self.lease(principal, goal, args)
                rows = self.store.all('''SELECT o.id,o.state FROM coordination_operations c JOIN operations o ON o.id=c.operation_id
                    WHERE c.work_item_id=? AND c.attempt=? AND c.fencing_token=? AND c.grant_id=? AND c.project_id=?''',
                    (item['id'], item['attempt'], item['fencing_token'], principal.grant_id, item['project_id']))
                actual = {row['id']: row['state'] for row in rows}
                if set(args['operation_ids']) != set(actual):
                    raise DevError('WORK_OPERATION_MISMATCH', '结果必须列出本尝试的全部真实操作，不能伪造或遗漏', 409)
                if self.pending_operations(item):
                    raise DevError('WORK_OPERATION_PENDING', '持久操作尚未完成；不能宣称工作结束', 409)
                if (args['outcome'] == 'succeeded' and self.c.delegation.goal_link(goal)
                        and set(json.loads(item['required_capabilities'])) & {'write', 'execute'} and not actual):
                    raise DevError('WORK_OPERATION_REQUIRED', '执行委托必须有真实操作回执，不能仅凭文字宣称完成', 409)
                if args['outcome'] == 'succeeded' and any(state != 'succeeded' for state in actual.values()):
                    raise DevError('WORK_OPERATION_FAILED', '失败或取消的操作不能作为成功执行结果', 409)
                inputs = list(dict.fromkeys([*json.loads(item['dependencies']), *args['input_work_item_ids']]))
                provenance = self.provenance(goal, inputs, [item['project_id']])
                body = redact({key: args[key] for key in ('outcome', 'summary', 'operation_ids', 'limitations')}) | {
                    'input_work_item_ids': inputs, 'provenance_project_ids': provenance,
                    'execution_verified': bool(actual), 'acceptance_verified_by_owner': False}
                self.store.execute('''UPDATE coordination_work SET state=?,result=?,lease_until=NULL,
                    version=version+1,updated=? WHERE id=?''',
                    (args['outcome'], canonical(body), self.c.clock(), item['id']))
                self.c.delegation.project_result(room, goal, item, body, principal)
                if args['outcome'] == 'succeeded':
                    for dependent in self.store.all("SELECT * FROM coordination_work WHERE goal_id=? AND approval_id=? AND state='queued'",
                                                    (goal['id'], goal['approval_id'])):
                        if item['id'] in json.loads(dependent['dependencies']):
                            self.emit_work(room, goal, dependent)
                return {'work_item': self.work_view(self.work(goal, item['id']))}
            return self.mutation(principal, room, 'work_result', args, save)

    def message(self, raw, principal):
        args = validate(contracts.GoalMessage, raw)
        with self.store.transaction():
            principal, room, goal = self.scope(principal, args, active=True)
            for grant_id in args['mention_grant_ids']:
                self.participant(room, goal, grant_id)
            def save():
                if not args['body_text'].strip():
                    raise DevError('INVALID_ARGUMENTS', '目标消息不能为空', 422)
                if goal['messages'] >= json.loads(goal['spec'])['budget']['max_messages']:
                    raise DevError('GOAL_BUDGET_EXCEEDED', '目标通信预算已用尽', 409)
                identifier, now = uuid.uuid4().hex, self.c.clock()
                mentions = list(dict.fromkeys(args['mention_grant_ids']))
                provenance = self.provenance(goal, args['input_work_item_ids'], json.loads(goal['spec'])['project_ids'])
                self.store.execute('INSERT INTO coordination_messages VALUES (?,?,?,?,?,?,?,?)',
                    (identifier, goal['id'], goal['approval_id'], principal.grant_id or 'owner',
                     redact(args['body_text']), canonical(mentions), canonical(provenance), now))
                self.store.execute('UPDATE coordination_goals SET messages=messages+1 WHERE id=?', (goal['id'],))
                for grant_id in mentions:
                    self.c.emit(room, WORK_EVENT, identifier, 1,
                        {'conversation_id': goal['conversation_id'], 'goal_id': goal['id'], 'approval_id': goal['approval_id'],
                         'work_item_id': None, 'work_item_version': None, 'target_project_id': room['project_id'],
                         'recipient_grant_id': grant_id, 'reason': 'peer_message', 'message_id': identifier}, grant_id=grant_id)
                return {'message_id': identifier, 'provenance_project_ids': provenance, 'mentions_queued': len(mentions)}
            return self.mutation(principal, room, 'goal_message', args, save)

    def emit_work(self, room, goal, item):
        if any(self.work(goal, identifier)['state'] != 'succeeded' for identifier in json.loads(item['dependencies'])):
            return
        if self.c.delegation.emit_work(room, goal, item):
            return
        self.c.emit(room, WORK_EVENT, item['id'], item['version'],
            {'conversation_id': goal['conversation_id'], 'goal_id': goal['id'], 'approval_id': goal['approval_id'],
             'work_item_id': item['id'], 'work_item_version': item['version'], 'target_project_id': item['project_id'],
             'recipient_grant_id': item['assignee_grant_id'], 'reason': 'work_available', 'message_id': None},
            grant_id=item['assignee_grant_id'])

    def authorize_event(self, principal, filters, payload=None):
        principal, room, goal = self.scope(principal, {'project': filters['project_id'],
            'environment_id': filters['environment_id'], 'goal_id': filters['goal_id']}, active=True)
        if (filters['conversation_id'] != goal['conversation_id'] or not principal.grant_id
                or filters.get('approval_id') != goal['approval_id']):
            raise DevError('GOAL_EVENT_DENIED', '目标通知过滤器不匹配', 403)
        if payload and not payload.get('test'):
            if (payload['goal_id'] != goal['id'] or payload['approval_id'] != goal['approval_id']
                    or payload['recipient_grant_id'] != principal.grant_id or payload['conversation_id'] != goal['conversation_id']):
                raise DevError('GOAL_EVENT_OBSOLETE', '原目标通知已过期', 409)
            if payload['reason'] == 'work_available':
                item = self.work(goal, payload['work_item_id'])
                self.work_capabilities(principal, goal, item)
                if any(self.work(goal, identifier)['state'] != 'succeeded' for identifier in json.loads(item['dependencies'])):
                    raise DevError('WORK_DEPENDENCY_PENDING', '依赖尚未就绪，不能投递工作通知', 409)
                if (item['state'] != 'queued' or item['version'] != payload['work_item_version']
                        or item['approval_id'] != goal['approval_id'] or item['assignee_grant_id'] != principal.grant_id
                        or item['project_id'] != payload['target_project_id']):
                    raise DevError('GOAL_EVENT_OBSOLETE', '原工作项通知已过期', 409)
            else:
                message = self.store.one('SELECT * FROM coordination_messages WHERE id=? AND goal_id=? AND approval_id=?',
                    (payload['message_id'], goal['id'], goal['approval_id']))
                if not message or principal.grant_id not in json.loads(message['mention_grant_ids']):
                    raise DevError('GOAL_EVENT_DENIED', '此消息没有提及当前目标连接', 403)
        return goal

    def operation_denial(self, operation):
        link = self.store.one('SELECT * FROM coordination_operations WHERE operation_id=?', (operation['id'],))
        if not link:
            return None
        try:
            goal = self.store.one('SELECT * FROM coordination_goals WHERE id=?', (link['goal_id'],))
            room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (goal['room_id'],))
            actor = self.participant(room, goal, link['grant_id'])
            item = self.lease(actor, goal, link)
            if item['project_id'] != operation['project_id'] or operation['grant_id'] != link['grant_id']:
                raise DevError('WORK_OPERATION_MISMATCH', '执行操作归属改变', 403)
        except DevError as exc:
            return exc.message
        return None

    def reconcile(self):
        now = self.c.clock()
        with self.store.transaction():
            after = getattr(self, '_reconcile_after', '')
            rows = self.store.all("SELECT * FROM coordination_goals WHERE state='active' AND id>? ORDER BY id LIMIT 100", (after,))
            self._reconcile_after = rows[-1]['id'] if len(rows) == 100 else ''
            for goal in rows:
                room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (goal['room_id'],))
                spec = json.loads(goal['spec'])
                try:
                    for grant_id in spec['participant_grant_ids']:
                        self.participant(room, goal, grant_id)
                except DevError as exc:
                    self.stop_work(goal, exc.code)
                    self.store.execute("UPDATE coordination_goals SET state='paused',version=version+1,updated=? WHERE id=?", (now, goal['id']))
                    continue
                for item in self.store.all("SELECT * FROM coordination_work WHERE goal_id=? AND state IN ('leased','running') AND lease_until<=?", (goal['id'], now)):
                    pending = self.pending_operations(item)
                    admitted = self.store.one('SELECT 1 FROM coordination_operations WHERE work_item_id=? LIMIT 1', (item['id'],))
                    state = 'blocked' if pending or admitted or item['attempt'] >= spec['budget']['max_attempts'] else 'queued'
                    self.store.execute("""UPDATE coordination_work SET state=?,reason_code='LEASE_EXPIRED',lease_until=NULL,
                        fencing_token=fencing_token+1,version=version+1,updated=? WHERE id=?""", (state, now, item['id']))
                    if state == 'queued':
                        self.emit_work(room, goal, self.work(goal, item['id']))

    def invoke(self, name, raw, principal):
        handlers = {'collaboration_goal_create': self.create, 'collaboration_goal_update': self.update,
            'collaboration_goal_read': self.read, 'collaboration_work_create': self.work_create,
            'collaboration_work_assign': self.work_assign, 'collaboration_work_claim': self.work_claim,
            'collaboration_work_progress': self.progress, 'collaboration_work_heartbeat': self.heartbeat, 'collaboration_work_result': self.result,
            'collaboration_goal_message': self.message}
        return handlers[name](raw, principal)
