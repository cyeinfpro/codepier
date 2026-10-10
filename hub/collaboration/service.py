"""One bounded transaction service shared by panel, MCP and monitoring.

Legacy monitor tasks stay read-only. Separately approved goal work reuses the
Runtime execution policy. Grants own leases; model proposals cannot approve them.
"""
from __future__ import annotations
import json
import secrets
import time
import uuid
from dataclasses import asdict
from hub.principal import Principal, refresh_principal
from shared import collaboration_contracts as contracts
from shared.util import DevError
from hub.collaboration.config import CollaborationConfig
from hub.collaboration.common import (
    TERMINAL, TASK_EVENT, RESULT_EVENT, STATUS_EVENT, canonical, digest,
    read_cursor, redact, sign_cursor, timestamp, validate,
)


def new_id():
    return uuid.uuid4().hex


class CollaborationService:
    def __init__(self, runtime, config=None, clock=time.time):
        self.runtime, self.store = runtime, runtime.store
        self.config = config or CollaborationConfig.from_env()
        self.clock = clock
        from hub.collaboration.joining import JoiningService
        self.joining = JoiningService(self)
        from hub.collaboration.chatroom import ChatroomService
        self.chatroom = ChatroomService(self)
        from hub.collaboration.conversations import ConversationService
        self.conversations = ConversationService(self)
        from hub.collaboration.coordination import CoordinationService
        self.coordination = CoordinationService(self)
        from hub.collaboration.delegation import DelegationService
        self.delegation = DelegationService(self)
        from hub.collaboration.dots import DotService
        self.dots = DotService(self)
        self.events = None
        self.monitor = None
        self.secret = b''
        if self.config.enabled:
            with self.store.transaction():
                row = self.store.one("SELECT secret FROM collaboration_secrets WHERE id='cursor'")
                if row is None:
                    self.store.execute('INSERT INTO collaboration_secrets(id,secret) VALUES (?,?)',
                                       ('cursor', self.store.encrypt(secrets.token_hex(32))))
                    row = self.store.one("SELECT secret FROM collaboration_secrets WHERE id='cursor'")
                self.secret = bytes.fromhex(self.store.decrypt(row['secret']))

    def guard(self):
        if not self.config.enabled:
            raise DevError('COLLABORATION_DISABLED', '协作功能未启用；未创建任务或启动监控', 409)

    @staticmethod
    def owner(principal):
        if not principal.admin or principal.grant_id is not None:
            raise DevError('OWNER_REQUIRED', '此操作需要已登录面板的空间管理员确认', 403)

    def scope(self, principal, args, *, create=False, optional=False, notification=False):
        self.guard()
        principal = refresh_principal(self.store, principal)
        project = self.runtime.project(args['project'], principal)
        room = self.store.one('''SELECT * FROM collaboration_rooms WHERE space_id=? AND
            owner_user_id=? AND project_id=? AND environment_id=?''',
            (principal.space_id, principal.user_id, project['id'], args['environment_id']))
        if room is None and create:
            if notification:
                # A subscription needs only a passive, tenant-scoped record container.
                # It never creates agents, tasks, monitor plans or execution authority.
                self.notification_reader(principal)
                count = self.store.one('SELECT COUNT(*) AS n FROM collaboration_rooms WHERE space_id=? AND owner_user_id=? AND project_id=?',
                                       (principal.space_id, principal.user_id, project['id']))['n']
                if count >= 64:
                    raise DevError('ROOM_LIMIT', '项目环境记录已达上限，请先在面板核查', 409)
            else:
                self.owner(principal)
            identifier = new_id()
            self.store.execute('''INSERT INTO collaboration_rooms
                (id,space_id,owner_user_id,project_id,environment_id,created) VALUES (?,?,?,?,?,?)''',
                (identifier, principal.space_id, principal.user_id, project['id'], args['environment_id'], self.clock()))
            room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (identifier,))
        if room is not None and create:
            self.conversations.ensure_default(room)
        if room is None and not optional:
            raise DevError('ROOM_NOT_FOUND', '请先在面板开启这个项目环境的协作室', 404)
        return principal, room

    def live_room(self, room):
        current = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (room['id'],))
        if current is None or current['state'] != 'active':
            raise DevError('ROOM_PAUSED', '协作室已暂停；不接受新任务或领取', 409)
        return current

    def grant_reader(self, room, grant_id, *, snapshot=None):
        grant = self.store.one('SELECT * FROM grants WHERE id=?', (grant_id,))
        if not grant or grant['user_id'] != room['owner_user_id'] or grant.get('space_id', 'legacy') != room['space_id']:
            raise DevError('WORKER_BINDING_INVALID', '没有可用于此协作室的授权', 403)
        principal = Principal(
            actor='mcp:' + grant_id + ':collaboration', user_id=grant['user_id'], scopes=set(), projects=[],
            grant_id=grant_id, profile_id=grant.get('profile_id'), space_id=room['space_id'],
            user_epoch=grant.get('user_epoch'), identity_id=grant.get('identity_id'),
            authorization_mode=grant.get('authorization_mode', 'fixed'), role_id=grant.get('role_id'))
        if snapshot is not None:
            for key in ('user_id', 'space_id', 'grant_id', 'profile_id', 'user_epoch', 'identity_id'):
                if snapshot.get(key) != getattr(principal, key):
                    raise DevError('WORKER_BINDING_INVALID', '原订阅的身份绑定已改变', 403)
        principal = refresh_principal(self.store, principal)
        self.runtime.project(room['project_id'], principal)
        self.notification_reader(principal)
        return principal

    def grant_principal(self, room, grant_id, *, snapshot=None):
        principal = self.grant_reader(room, grant_id, snapshot=snapshot)
        self.readonly_worker(principal)
        return principal

    @staticmethod
    def notification_reader(principal):
        if not principal.grant_id or 'read' not in principal.scopes:
            raise DevError('SUBSCRIPTION_READ_REQUIRED', '订阅需要当前项目的 MCP 读取授权', 403)

    @staticmethod
    def principal_snapshot(principal):
        return {key: getattr(principal, key) for key in
                ('user_id', 'space_id', 'grant_id', 'profile_id', 'user_epoch', 'identity_id')}

    @staticmethod
    def readonly_worker(principal):
        if not principal.grant_id or 'read' not in principal.scopes:
            raise DevError('WORKER_BINDING_REQUIRED', '需要明确绑定的只读 MCP 连接', 403)
        if not principal.scopes <= {'read', 'devices.read'}:
            raise DevError('WORKER_SCOPE_TOO_BROAD', '只读试点不接受具有额外写入、执行或管理权限的连接', 403)

    def agent(self, room, identifier, *, active=True):
        row = self.store.one('SELECT * FROM collaboration_agents WHERE id=? AND room_id=?', (identifier, room['id']))
        if row is None:
            raise DevError('AGENT_NOT_FOUND', '未找到当前协作室中登记的智能体', 404)
        if active and (not row['enabled'] or row['expires_at'] <= self.clock()):
            raise DevError('WORKER_BINDING_EXPIRED', '智能体绑定已停用或到期，需要所有者处理', 403)
        if active:
            self.grant_principal(room, row['grant_id'])
        return row

    def worker(self, principal, room, agent_id):
        self.readonly_worker(principal)
        agent = self.agent(room, agent_id)
        if agent['grant_id'] != principal.grant_id:
            raise DevError('FORBIDDEN', '任务没有分配给当前认证连接', 403)
        return agent

    def object(self, table, room, identifier):
        if table not in {'collaboration_jobs', 'collaboration_results', 'collaboration_messages',
                         'collaboration_goals', 'collaboration_agents', 'mcp_event_subscriptions',
                         'monitor_incidents', 'monitor_evidence'}:
            raise RuntimeError('Unknown collaboration object kind')
        row = self.store.one(f'SELECT * FROM {table} WHERE id=? AND room_id=?', (identifier, room['id']))
        if row is None:
            raise DevError('NOT_FOUND', '未找到当前授权范围内的记录', 404)
        return row

    def mutation(self, principal, room, action, args, perform):
        # Reauthorize before replay, but replay before checking a now-terminal lease.
        key = digest([room['space_id'], room['owner_user_id'], principal.grant_id or 'panel',
                      action, room['id'], args['idempotency_key']])
        fingerprint = digest(args)
        saved = self.store.one('SELECT * FROM collaboration_idempotency WHERE id=?', (key,))
        if saved:
            if saved['digest'] != fingerprint:
                raise DevError('IDEMPOTENCY_CONFLICT', '同一请求键不能用于不同内容', 409)
            return json.loads(saved['response'])
        result = perform()
        self.store.execute('INSERT INTO collaboration_idempotency VALUES (?,?,?,?,?)',
                           (key, room['id'], fingerprint, canonical(result), self.clock()))
        self.audit(room, principal.actor, action, result.get('id', result.get('job_id', room['id'])),
                   {'request_digest': fingerprint})
        return result

    def audit(self, room, actor, action, target, detail=None):
        self.store.execute('''INSERT INTO collaboration_audit(room_id,actor,action,target,detail,created)
            VALUES (?,?,?,?,?,?)''', (room['id'], actor, action, target, canonical(redact(detail or {})), self.clock()))

    def emit(self, room, name, object_id, version, data, *, grant_id='', queue=''):
        body = redact({'schema_version': 1, 'project_id': room['project_id'],
                       'environment_id': room['environment_id'], **data})
        from hub.collaboration.event_contracts import PAYLOADS
        if name not in PAYLOADS:
            raise DevError('UNKNOWN_EVENT', '未登记的事件类型', 422)
        body = validate(PAYLOADS[name], body)
        if len(canonical(body).encode()) > 12 * 1024:
            raise DevError('EVENT_TOO_LARGE', '事件摘要超过上限，请只保存受控引用', 413)
        self.store.execute('''INSERT OR IGNORE INTO mcp_event_outbox
            (id,room_id,name,object_id,object_version,target_grant_id,queue,data,created)
            VALUES (?,?,?,?,?,?,?,?,?)''',
            (new_id(), room['id'], name, object_id, version, grant_id, queue, canonical(body), self.clock()))

    def budget(self, room, budget):
        now = self.clock()
        rooms = '''SELECT id FROM collaboration_rooms WHERE space_id=? AND owner_user_id=? AND project_id=?'''
        boundary = (room['space_id'], room['owner_user_id'], room['project_id'])
        count = self.store.one(f'''SELECT COUNT(*) AS n FROM collaboration_jobs WHERE room_id IN ({rooms})
            AND state IN ('queued','leased','running','retry_wait','blocked')''', boundary)['n']
        hourly = self.store.one(f'SELECT COUNT(*) AS n FROM collaboration_jobs WHERE room_id IN ({rooms}) AND created>?', (*boundary, now - 3600))['n']
        daily = self.store.one(f'SELECT COUNT(*) AS n FROM collaboration_jobs WHERE room_id IN ({rooms}) AND created>?', (*boundary, now - 86400))['n']
        if (count >= budget['max_open_analysis_jobs'] or hourly >= budget['max_new_analysis_jobs_per_hour']
                or daily >= budget['max_new_analysis_jobs_per_day']):
            raise DevError('BUDGET_EXCEEDED', '项目分析预算已用尽；采集可以继续，不能拆分任务绕过限额', 409)

    def make_job(self, room, *, agent_id, kind, business_key, context, goal_id=None,
                 incident_id=None, next_agent='', parent=None, plan_version=None):
        old = self.store.one('SELECT * FROM collaboration_jobs WHERE business_key=? AND room_id=?', (business_key, room['id']))
        if old:
            return old
        self.live_room(room)
        agent = self.agent(room, agent_id)
        if kind == 'summarize_result' and agent['kind'] != 'dot':
            raise DevError('ASSIGNEE_KIND_MISMATCH', '汇总任务需要明确登记的 dot', 409)
        if kind == 'analyze_incident' and agent['kind'] != 'work_cloud':
            raise DevError('ASSIGNEE_KIND_MISMATCH', '异常分析任务需要明确登记的 Work Cloud', 409)
        budget = context.get('budget') or contracts.Budget().model_dump()
        self.budget(room, budget)
        depth, hops = (parent['depth'] + 1, parent['hops'] + 1) if parent else (0, 0)
        if depth > 4 or hops > 8:
            raise DevError('HANDOFF_LIMIT', '交接层数已达上限，需要所有者决定', 409)
        identifier, now = new_id(), self.clock()
        context = redact({**context, 'budget': budget})
        self.store.execute('''INSERT INTO collaboration_jobs
            (id,room_id,goal_id,incident_id,kind,assignee_agent_id,target_grant_id,queue,
             next_assignee_agent_id,parent_job_id,depth,hops,business_key,deadline_at,max_attempts,
             context,plan_version,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (identifier, room['id'], goal_id, incident_id, kind, agent['id'], agent['grant_id'], agent['queue'],
             next_agent, parent['id'] if parent else None, depth, hops, business_key,
             now + budget['analysis_deadline_seconds'], budget['max_attempts'], canonical(context), plan_version, now, now))
        job = self.object('collaboration_jobs', room, identifier)
        self.task_event(room, job)
        return job

    def task_event(self, room, job):
        incident = self.object('monitor_incidents', room, job['incident_id']) if job['incident_id'] else None
        self.emit(room, TASK_EVENT, job['id'], job['version'],
                  {'job_id': job['id'], 'job_version': job['version'], 'queue': job['queue'],
                   'assignee_agent_id': job['assignee_agent_id'], 'kind': job['kind'],
                   'incident_id': job['incident_id'], 'goal_id': job['goal_id'],
                   'reason_code': 'task_available', 'severity': incident['severity'] if incident else None,
                   'rule_id': incident['rule_id'] if incident else None}, grant_id=job['target_grant_id'], queue=job['queue'])

    def check_plan(self, room, job):
        if job['plan_version'] is not None:
            binding = self.store.one('SELECT * FROM monitor_plan_bindings WHERE room_id=?', (room['id'],))
            if not binding or binding['state'] != 'active' or binding['active_version'] != job['plan_version']:
                raise DevError('PLAN_INACTIVE', '原任务计划已暂停或被替代；不会继承新版本权限', 409)
            approval = self.store.one('SELECT expires_at FROM monitor_approvals WHERE id=?', (binding['approval_id'],))
            if not approval or approval['expires_at'] <= self.clock():
                raise DevError('PLAN_INACTIVE', '监控计划批准已到期', 409)

    def claim(self, raw, principal):
        args = validate(contracts.Claim, raw)
        with self.store.transaction():
            principal, room = self.scope(principal, args)
            job = self.object('collaboration_jobs', room, args['job_id'])
            self.worker(principal, room, job['assignee_agent_id'])
            if job['target_grant_id'] != principal.grant_id:
                raise DevError('FORBIDDEN', '原任务绑定不属于此连接', 403)
            def take():
                self.live_room(room)
                self.check_plan(room, job)
                source_id = json.loads(job['context']).get('origin_message_id')
                if source_id:
                    self.object('collaboration_messages', room, source_id)
                if job['version'] != args['expected_version']:
                    raise DevError('STALE_VERSION', '任务已改变，请重新读取任务版本', 409)
                if job['state'] != 'queued':
                    raise DevError('ALREADY_CLAIMED', '任务不是可领取状态', 409)
                now = self.clock()
                if now >= job['deadline_at'] or job['attempt'] >= job['max_attempts']:
                    raise DevError('JOB_EXPIRED', '任务已超过期限或领取次数上限', 409)
                rooms = 'SELECT id FROM collaboration_rooms WHERE space_id=? AND owner_user_id=? AND project_id=?'
                boundary = (room['space_id'], room['owner_user_id'], room['project_id'])
                occupied = self.store.one(f'''SELECT COUNT(*) AS n FROM collaboration_jobs WHERE room_id IN ({rooms})
                    AND queue=? AND state IN ('leased','running') AND lease_until>?''', (*boundary, job['queue'], now))['n']
                total = self.store.one(f'''SELECT COUNT(*) AS n FROM collaboration_jobs WHERE room_id IN ({rooms})
                    AND state IN ('leased','running') AND lease_until>?''', (*boundary, now))['n']
                if occupied >= 1 or total >= 2:
                    raise DevError('QUEUE_BUSY', '队列已有有效租约，请等待原任务，不要重复创建', 409)
                budget = json.loads(job['context'])['budget']
                if job['tool_calls'] >= budget['max_tool_calls_per_job']:
                    raise DevError('BUDGET_EXCEEDED', '原任务调用预算已经用尽；重领不能重置预算', 409)
                attempt, fence = job['attempt'] + 1, job['fencing_token'] + 1
                until = min(now + 900, job['deadline_at'])
                self.store.execute('''UPDATE collaboration_jobs SET state='leased',version=version+1,
                    attempt=?,fencing_token=?,lease_grant_id=?,lease_until=?,tool_calls=tool_calls+1,updated=? WHERE id=?''',
                    (attempt, fence, principal.grant_id, until, now, job['id']))
                self.store.execute('INSERT INTO collaboration_attempts VALUES (?,?,?,?,?)', (job['id'], attempt, fence, principal.grant_id, now))
                return self.job_view(self.object('collaboration_jobs', room, job['id']), full=True)
            return self.mutation(principal, room, 'claim', args, take)

    def lease(self, principal, room, args, *, spend=True):
        job = self.object('collaboration_jobs', room, args['job_id'])
        self.worker(principal, room, job['assignee_agent_id'])
        if job['target_grant_id'] != principal.grant_id or job['lease_grant_id'] != principal.grant_id:
            raise DevError('FORBIDDEN', '租约不属于当前认证连接', 403)
        self.live_room(room)
        self.check_plan(room, job)
        if (job['state'] not in {'leased', 'running'} or job['attempt'] != args['attempt']
                or job['fencing_token'] != args['fencing_token'] or not job['lease_until']
                or self.clock() >= min(job['lease_until'], job['deadline_at'])):
            raise DevError('LEASE_EXPIRED', '租约或执行世代已经失效，请读取原任务；不能重放旧结果', 409)
        budget = json.loads(job['context'])['budget']
        if spend and job['tool_calls'] >= budget['max_tool_calls_per_job']:
            raise DevError('BUDGET_EXCEEDED', '任务工具调用预算已用尽，请提交阻塞说明', 409)
        if spend:
            self.store.execute('UPDATE collaboration_jobs SET tool_calls=tool_calls+1 WHERE id=?', (job['id'],))
        return job

    def heartbeat(self, raw, principal):
        args = validate(contracts.Lease, raw)
        with self.store.transaction():
            principal, room = self.scope(principal, args)
            job = self.object('collaboration_jobs', room, args['job_id'])
            self.worker(principal, room, job['assignee_agent_id'])
            def extend():
                job = self.lease(principal, room, args)
                until = min(self.clock() + 900, job['deadline_at'])
                self.store.execute("UPDATE collaboration_jobs SET state='running',lease_until=?,version=version+1,updated=? WHERE id=?", (until, self.clock(), job['id']))
                return {'job_id': job['id'], 'version': job['version'] + 1, 'attempt': job['attempt'],
                        'fencing_token': job['fencing_token'], 'lease_until': until, 'state': 'running'}
            return self.mutation(principal, room, 'heartbeat', args, extend)

    def room_create(self, raw, principal):
        args = validate(contracts.RoomCreate, raw)
        with self.store.transaction():
            principal, room = self.scope(principal, args, create=True)
            self.owner(principal)
            return self.mutation(principal, room, 'room_create', args,
                                 lambda: {'id': room['id'], 'room': room, 'state': room['state']})

    def register_agent(self, raw, principal):
        args = validate(contracts.AgentRegister, raw)
        with self.store.transaction():
            principal, room = self.scope(principal, args)
            self.owner(principal)
            def register():
                self.live_room(room)
                self.grant_principal(room, args['grant_id'])
                count = self.store.one('SELECT COUNT(*) AS n FROM collaboration_agents WHERE room_id=?', (room['id'],))['n']
                if count >= 16:
                    raise DevError('AGENT_LIMIT', '试点协作室最多登记 16 个用途绑定', 409)
                queue = 'work-analysis' if args['kind'] == 'work_cloud' else 'dot-coordination'
                existing = self.store.one('SELECT id FROM collaboration_agents WHERE room_id=? AND grant_id=? AND queue=?',
                                          (room['id'], args['grant_id'], queue))
                if existing:
                    raise DevError('AGENT_EXISTS', '此连接已经登记了相同队列用途，请读取现有绑定', 409)
                identifier = new_id()
                self.store.execute('''INSERT INTO collaboration_agents
                    (id,room_id,label,kind,queue,grant_id,expires_at,created) VALUES (?,?,?,?,?,?,?,?)''',
                    (identifier, room['id'], redact(args['label']), args['kind'], queue, args['grant_id'],
                     self.clock() + args['expires_in_days'] * 86400, self.clock()))
                return self.agent_view(room, self.agent(room, identifier))
            return self.mutation(principal, room, 'agent_register', args, register)

    def bound_worker(self, principal, room, *, kind=None):
        self.readonly_worker(principal)
        rows = self.store.all('''SELECT * FROM collaboration_agents WHERE room_id=? AND grant_id=?
            AND enabled=1 AND expires_at>?''', (room['id'], principal.grant_id, self.clock()))
        if not any(kind is None or row['kind'] == kind for row in rows):
            raise DevError('WORKER_BINDING_REQUIRED', '需要所有者明确登记且尚未到期的用途绑定', 403)
        return rows

    def command(self, raw, principal, *, from_panel=False):
        args = validate(contracts.Command, raw)
        with self.store.transaction():
            principal, room = self.scope(principal, args)
            if args['room_id'] != room['id']:
                raise DevError('ROOM_MISMATCH', '指令与当前协作室不一致', 409)
            self.owner(principal) if from_panel else self.bound_worker(principal, room)
            def save():
                self.live_room(room)
                target = self.agent(room, args['structured_mentions'][0]['agent_id'])
                if args['next_assignee_agent_id']:
                    following = self.agent(room, args['next_assignee_agent_id'])
                    if following['kind'] != 'dot' or following['id'] == target['id']:
                        raise DevError('HANDOFF_INVALID', '只能明确交给另一位 dot 做一次结果汇总', 409)
                if args['thread_id']:
                    thread = self.store.one('''SELECT id FROM collaboration_messages WHERE room_id=?
                        AND (thread_id=? OR id=?) LIMIT 1''', (room['id'], args['thread_id'], args['thread_id']))
                    if not thread:
                        raise DevError('THREAD_NOT_FOUND', '讨论串不属于当前协作室', 404)
                origin = 'panel' if from_panel else 'mcp'
                author = principal.user_id if from_panel else principal.grant_id
                content_digest = digest({key: value for key, value in args.items() if key != 'idempotency_key'})
                old = self.store.one('''SELECT * FROM collaboration_messages WHERE room_id=?
                    AND origin=? AND author=? AND source_id=?''', (room['id'], origin, author, args['source_message_id']))
                if old:
                    if json.loads(old['body']).get('request_digest') != content_digest:
                        raise DevError('SOURCE_MESSAGE_CONFLICT', '同一来源消息不能替换已经保存的指令', 409)
                    return {'id': old['id'], 'message_id': old['id'], 'goal_id': old['goal_id'],
                            'state': old['state'], 'version': old['version'], 'replayed': True}
                pending = self.store.one("SELECT COUNT(*) AS n FROM collaboration_messages WHERE room_id=? AND state='awaiting_approval'", (room['id'],))['n']
                if not from_panel and pending >= 20:
                    raise DevError('PROPOSAL_LIMIT', '待审批建议已达上限，请等待所有者处理', 409)
                identifier = new_id()
                body = {'command': redact(args), 'request_digest': content_digest}
                self.store.execute('''INSERT INTO collaboration_messages
                    (id,room_id,thread_id,author,origin,source_id,kind,body,state,created)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''',
                    (identifier, room['id'], args['thread_id'] or identifier, author, origin, args['source_message_id'],
                     'owner_command' if from_panel else 'agent_proposal', canonical(body),
                     'saved' if from_panel else 'awaiting_approval', self.clock()))
                if from_panel:
                    return self.schedule_message(room, self.object('collaboration_messages', room, identifier), principal)
                return {'id': identifier, 'message_id': identifier, 'state': 'awaiting_approval', 'version': 1, 'scheduled': False}
            return self.mutation(principal, room, 'command_create', args, save)

    def schedule_message(self, room, message, principal):
        args = json.loads(message['body'])['command']
        self.live_room(room)
        # All writes below join the caller's transaction, including the job/outbox.
        if args['goal_id']:
            goal = self.object('collaboration_goals', room, args['goal_id'])
            if goal['version'] != args['expected_version'] or goal['state'] not in {'open', 'awaiting_decision'}:
                raise DevError('STALE_VERSION', '目标已改变或已终结，请重新读取', 409)
            goal_id = goal['id']
            self.store.execute('UPDATE collaboration_goals SET version=version+1,updated=? WHERE id=?', (self.clock(), goal_id))
        else:
            goal_id = new_id()
            self.store.execute('''INSERT INTO collaboration_goals
                (id,room_id,source_message_id,request,acceptance,created,updated) VALUES (?,?,?,?,?,?,?)''',
                (goal_id, room['id'], message['id'], args['request'], args['acceptance'], self.clock(), self.clock()))
        job = self.make_job(room, agent_id=args['structured_mentions'][0]['agent_id'], kind=args['kind'],
                            business_key='command:' + message['id'], goal_id=goal_id, next_agent=args['next_assignee_agent_id'],
                            context={'request': args['request'], 'acceptance': args['acceptance'],
                                     'origin_message_id': message['id'], 'evidence_refs': []})
        self.store.execute("UPDATE collaboration_messages SET state='queued',goal_id=?,version=version+1 WHERE id=?", (goal_id, message['id']))
        self.audit(room, principal.actor, 'command.authorized', message['id'], {'goal_id': goal_id})
        return {'id': message['id'], 'message_id': message['id'], 'goal_id': goal_id, 'job_id': job['id'],
                'state': 'queued', 'version': message['version'] + 1, 'scheduled': True,
                'delivery_status': self.delivery_status(job)}

    def evidence(self, room, identifier):
        row = self.object('monitor_evidence', room, identifier)
        if row['expires_at'] <= self.clock() or row['body'] == 'null':
            raise DevError('EVIDENCE_EXPIRED', '证据已超过保留期限，仅保留摘要与引用墓碑', 410)
        return {**row, 'body': redact(json.loads(row['body']))}

    @staticmethod
    def result_refs(result):
        return {ref for observation in result['observations'] for ref in observation['evidence_refs']} | set(result['artifacts'])

    def submit(self, raw, principal):
        args = validate(contracts.Submit, raw)
        with self.store.transaction():
            principal, room = self.scope(principal, args)
            job = self.object('collaboration_jobs', room, args['job_id'])
            self.worker(principal, room, job['assignee_agent_id'])
            if job['target_grant_id'] != principal.grant_id:
                raise DevError('FORBIDDEN', '原任务不属于当前认证连接', 403)
            def finish():
                result = redact(args['result'])
                body, fingerprint = canonical(result), digest(result)
                if len(body.encode()) > 131072:
                    raise DevError('RESULT_TOO_LARGE', '结果超过 128 KiB', 413)
                existing = self.store.one('SELECT * FROM collaboration_results WHERE job_id=? AND attempt=?', (job['id'], args['attempt']))
                if existing:
                    if existing['digest'] != fingerprint:
                        raise DevError('RESULT_CONFLICT', '同一执行轮次已经保存了另一份结果，不能覆盖', 409)
                    attempt = self.store.one('SELECT fencing_token,grant_id FROM collaboration_attempts WHERE job_id=? AND attempt=?', (job['id'], args['attempt']))
                    if not attempt or attempt['fencing_token'] != args['fencing_token'] or attempt['grant_id'] != principal.grant_id:
                        raise DevError('FORBIDDEN', '原结果不属于此执行世代', 403)
                    return {'job_id': job['id'], 'result_id': existing['id'], 'accepted': True,
                            'state': job['state'], 'version': job['version'], 'replayed': True,
                            'incident_recovery_verified': False}
                refs = self.result_refs(result)
                evidence_bytes = sum(len(canonical(self.evidence(room, identifier)['body']).encode()) for identifier in refs)
                if evidence_bytes > json.loads(job['context'])['budget']['max_evidence_bytes_per_job']:
                    raise DevError('EVIDENCE_BUDGET_EXCEEDED', '结果引用的证据超过任务上限', 409)
                try:
                    current = self.lease(principal, room, args, spend=False)
                except DevError as exc:
                    known = self.store.one('''SELECT * FROM collaboration_attempts WHERE job_id=?
                        AND attempt=? AND fencing_token=? AND grant_id=?''',
                        (job['id'], args['attempt'], args['fencing_token'], principal.grant_id))
                    if exc.code != 'LEASE_EXPIRED' or not known:
                        raise
                    self.store.execute('''INSERT OR IGNORE INTO collaboration_late_results
                        (id,job_id,attempt,fencing_token,digest,body,created) VALUES (?,?,?,?,?,?,?)''',
                        (new_id(), job['id'], args['attempt'], args['fencing_token'], fingerprint, body, self.clock()))
                    late = self.store.one('''SELECT id FROM collaboration_late_results WHERE job_id=?
                        AND attempt=? AND fencing_token=? AND digest=?''',
                        (job['id'], args['attempt'], args['fencing_token'], fingerprint))
                    return {'job_id': job['id'], 'accepted': False, 'late_result_id': late['id'],
                            'error': {'code': 'LEASE_EXPIRED', 'message': '迟到结果仅存证，不改变任务或触发后续动作'}}
                identifier, now = new_id(), self.clock()
                self.store.execute('INSERT INTO collaboration_results VALUES (?,?,?,?,?,?,?)',
                                   (identifier, job['id'], room['id'], current['attempt'], body, fingerprint, now))
                state = 'blocked' if result['outcome'] == 'blocked' else 'succeeded'
                self.store.execute('''UPDATE collaboration_jobs SET state=?,lease_until=NULL,
                    version=version+1,updated=?,reason_code=? WHERE id=?''',
                    (state, now, 'result_blocked' if state == 'blocked' else '', job['id']))
                incident = self.object('monitor_incidents', room, job['incident_id']) if job['incident_id'] else None
                self.emit(room, RESULT_EVENT, identifier, 1,
                          {'result_id': identifier, 'job_id': job['id'], 'job_version': current['version'] + 1,
                           'incident_id': job['incident_id'], 'incident_version': incident['version'] if incident else None,
                           'outcome': result['outcome'], 'requires_decision': result['outcome'] in {'action_required', 'blocked', 'inconclusive'},
                           'evidence_refs': sorted(refs), 'severity': incident['severity'] if incident else None,
                           'rule_id': incident['rule_id'] if incident else None})
                origin_message = json.loads(job['context']).get('origin_message_id')
                if not origin_message and job['goal_id']:
                    origin_message = self.object('collaboration_goals', room, job['goal_id'])['source_message_id']
                result_conversation = self.object('collaboration_messages', room, origin_message)['conversation_id'] if origin_message else room['id']
                thread_root = (self.object('collaboration_messages', room, origin_message)['thread_root_id']
                               if origin_message else new_id())
                self.store.execute('''INSERT INTO collaboration_messages
                    (id,room_id,conversation_id,thread_id,author,origin,source_id,kind,body,state,goal_id,created)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (new_id(), room['id'], result_conversation, thread_root, principal.grant_id, 'mcp', 'result:' + identifier,
                     'agent_result', canonical({'result_id': identifier, 'summary': result['summary'],
                     'outcome': result['outcome'], 'job_id': job['id']}), 'submitted', job['goal_id'], now))
                # The result transaction never changes incident recovery state.
                handoff = None
                if state == 'succeeded' and job['next_assignee_agent_id']:
                    try:
                        handoff = self.make_job(room, agent_id=job['next_assignee_agent_id'], kind='summarize_result',
                            business_key='handoff:' + job['id'] + ':' + str(current['attempt']),
                            context={'source_result_id': identifier, 'request': '汇总已授权工作流的分析结果',
                                     'evidence_refs': sorted(refs), 'budget': json.loads(job['context'])['budget']},
                            goal_id=job['goal_id'], incident_id=job['incident_id'], parent=job, plan_version=job['plan_version'])
                    except DevError as exc:
                        # Checks in make_job precede its first write. Unexpected SQL
                        # errors must abort this transaction, never be swallowed.
                        self.audit(room, principal.actor, 'handoff.blocked', job['id'], {'reason_code': exc.code})
                        self.emit(room, STATUS_EVENT, job['id'] + ':handoff', current['version'] + 1,
                                  {'component': 'handoff', 'status': 'blocked', 'reason_code': exc.code,
                                   'observed_at': timestamp(now), 'recovery_state': 'owner_action_required'})
                if job['goal_id']:
                    pending = self.store.one("SELECT COUNT(*) AS n FROM collaboration_jobs WHERE goal_id=? AND state IN ('queued','leased','running','retry_wait','blocked')", (job['goal_id'],))['n']
                    self.store.execute('UPDATE collaboration_goals SET state=?,version=version+1,updated=? WHERE id=?',
                                       ('open' if pending else 'awaiting_decision', now, job['goal_id']))
                return {'job_id': job['id'], 'result_id': identifier, 'state': state, 'version': current['version'] + 1,
                        'accepted': True, 'incident_recovery_verified': False, 'handoff_job_id': handoff['id'] if handoff else None}
            return self.mutation(principal, room, 'result', args, finish)

    def block(self, raw, principal):
        args = validate(contracts.Block, raw)
        with self.store.transaction():
            principal, room = self.scope(principal, args)
            job = self.object('collaboration_jobs', room, args['job_id'])
            self.worker(principal, room, job['assignee_agent_id'])
            def stop():
                current = self.lease(principal, room, args, spend=False)
                self.store.execute("UPDATE collaboration_jobs SET state='blocked',reason_code=?,lease_until=NULL,version=version+1,updated=? WHERE id=?", (args['reason_code'], self.clock(), job['id']))
                self.audit(room, principal.actor, 'job.blocked', job['id'], {'reason_code': args['reason_code'], 'summary': redact(args['summary'])})
                self.emit(room, STATUS_EVENT, job['id'], current['version'] + 1,
                          {'component': 'analysis', 'status': 'blocked', 'reason_code': args['reason_code'],
                           'observed_at': timestamp(self.clock()), 'recovery_state': 'owner_action_required'})
                return {'job_id': job['id'], 'state': 'blocked', 'version': current['version'] + 1, 'reason_code': args['reason_code']}
            return self.mutation(principal, room, 'block', args, stop)

    def ack(self, raw, principal):
        args = validate(contracts.Ack, raw)
        with self.store.transaction():
            principal, room = self.scope(principal, args)
            self.object('collaboration_results', room, args['result_id'])
            self.bound_worker(principal, room, kind='dot') if principal.grant_id else self.owner(principal)
            def record():
                self.store.execute('INSERT OR IGNORE INTO collaboration_consumptions VALUES (?,?,?,?)', (room['id'], principal.grant_id or 'panel', args['result_id'], self.clock()))
                return {'result_id': args['result_id'], 'consumed': True, 'user_read_verified': False}
            return self.mutation(principal, room, 'ack', args, record)

    def control(self, raw, principal):
        args = validate(contracts.Control, raw)
        with self.store.transaction():
            principal, room = self.scope(principal, args)
            self.owner(principal)
            def apply():
                action, identifier, now = args['action'], args['target_id'], self.clock()
                table = {'cancel': 'collaboration_jobs', 'retry': 'collaboration_jobs',
                         'verify_goal': 'collaboration_goals', 'accept_proposal': 'collaboration_messages',
                         'reject_proposal': 'collaboration_messages', 'agent_disable': 'collaboration_agents',
                         'agent_enable': 'collaboration_agents', 'agent_renew': 'collaboration_agents',
                         'subscription_pause': 'mcp_event_subscriptions', 'subscription_resume': 'mcp_event_subscriptions',
                         'subscription_test': 'mcp_event_subscriptions'}.get(action)
                if action in {'pause', 'resume'}:
                    target = room
                elif action == 'plan_pause':
                    target = self.store.one('SELECT * FROM monitor_plan_bindings WHERE id=? AND room_id=?', (identifier, room['id']))
                else:
                    target = self.object(table, room, identifier)
                if not target or target['id'] != identifier:
                    raise DevError('NOT_FOUND', '未找到当前范围内的记录', 404)
                if target['version'] != args['expected_version']:
                    raise DevError('STALE_VERSION', '记录已改变，请先刷新再操作', 409)
                result = {'id': identifier, 'version': target['version'] + 1}
                if action in {'pause', 'resume'}:
                    state = 'paused' if action == 'pause' else 'active'
                    self.store.execute('UPDATE collaboration_rooms SET state=?,version=version+1 WHERE id=?', (state, identifier))
                    if action == 'pause':
                        self.store.execute('''UPDATE collaboration_jobs SET state='blocked',reason_code='owner_pause',
                            fencing_token=fencing_token+1,lease_until=NULL,version=version+1,updated=?
                            WHERE room_id=? AND state IN ('leased','running','queued','retry_wait')''', (now, room['id']))
                    result.update(state=state, jobs_require_explicit_retry=True)
                elif action == 'cancel':
                    if target['state'] in TERMINAL:
                        raise DevError('JOB_TERMINAL', '终态任务不能再次取消', 409)
                    self.store.execute('''UPDATE collaboration_jobs SET state='cancelled',fencing_token=fencing_token+1,
                        lease_until=NULL,version=version+1,updated=?,reason_code='owner_cancel' WHERE id=?''', (now, identifier))
                    result.update(job_id=identifier, state='cancelled', external_process_cancelled=False)
                elif action == 'retry':
                    self.live_room(room)
                    self.check_plan(room, target)
                    self.agent(room, target['assignee_agent_id'])
                    if target['state'] not in {'blocked', 'retry_wait'} or target['deadline_at'] <= now or target['attempt'] >= target['max_attempts']:
                        raise DevError('RETRY_DENIED', '只能在原期限和次数内明确解除只读阻塞，不能重置预算', 409)
                    self.store.execute("UPDATE collaboration_jobs SET state='queued',reason_code='',not_before=0,version=version+1,updated=? WHERE id=?", (now, identifier))
                    self.task_event(room, self.object('collaboration_jobs', room, identifier))
                    result.update(job_id=identifier, state='queued')
                elif action == 'verify_goal':
                    jobs = self.store.all('SELECT * FROM collaboration_jobs WHERE room_id=? AND goal_id=?', (room['id'], identifier))
                    if not jobs or any(job['state'] != 'succeeded' for job in jobs):
                        raise DevError('GOAL_UNVERIFIED', '目标仍有未成功提交结果的任务', 409)
                    for job in jobs:
                        if job['incident_id'] and self.object('monitor_incidents', room, job['incident_id'])['state'] != 'resolved':
                            raise DevError('RECOVERY_UNVERIFIED', '关联异常尚未通过独立探针的恢复验证', 409)
                    self.store.execute("UPDATE collaboration_goals SET state='verified',version=version+1,updated=? WHERE id=?", (now, identifier))
                    result.update(state='verified')
                elif action in {'accept_proposal', 'reject_proposal'}:
                    if target['state'] != 'awaiting_approval' or target['kind'] != 'agent_proposal':
                        raise DevError('PROPOSAL_NOT_PENDING', '这条消息不是待审批建议', 409)
                    if action == 'accept_proposal':
                        result = self.schedule_message(room, target, principal)
                    else:
                        self.store.execute("UPDATE collaboration_messages SET state='rejected',version=version+1 WHERE id=?", (identifier,))
                        result.update(state='rejected')
                elif action.startswith('agent_'):
                    if action != 'agent_disable':
                        self.grant_principal(room, target['grant_id'])
                        if action == 'agent_enable' and target['expires_at'] <= now:
                            raise DevError('WORKER_BINDING_EXPIRED', '绑定已到期，需要明确续期而非仅启用', 409)
                    expiry = now + 7 * 86400 if action == 'agent_renew' else target['expires_at']
                    enabled = action != 'agent_disable'
                    self.store.execute('UPDATE collaboration_agents SET enabled=?,expires_at=?,version=version+1 WHERE id=?', (int(enabled), expiry, identifier))
                    if not enabled:
                        self.store.execute('''UPDATE collaboration_jobs SET state='blocked',reason_code='agent_disabled',
                            fencing_token=fencing_token+1,lease_until=NULL,version=version+1,updated=?
                            WHERE assignee_agent_id=? AND state IN ('queued','leased','running','retry_wait')''', (now, identifier))
                    result.update(enabled=enabled, expires_at=expiry)
                elif action in {'subscription_pause', 'subscription_resume'}:
                    if action == 'subscription_resume':
                        if not self.events or target['expires_at'] <= now:
                            raise DevError('SUBSCRIPTION_EXPIRED', '订阅已到期，需要由原聊天明确重新订阅', 409)
                        self.events.authorize(target, room)
                    state = 'paused' if action == 'subscription_pause' else 'active'
                    self.store.execute('UPDATE mcp_event_subscriptions SET state=?,version=version+1,updated=? WHERE id=?', (state, now, identifier))
                    result.update(state=state)
                elif action == 'subscription_test':
                    if self.events is None:
                        raise DevError('EVENTS_DISABLED', '事件适配器未启用', 409)
                    result = self.events.queue_test(room, target)
                elif action == 'plan_pause':
                    self.store.execute("UPDATE monitor_plan_bindings SET state='paused',collection_fence=collection_fence+1,collection_lease_until=NULL,version=version+1 WHERE id=?", (identifier,))
                    result.update(state='paused')
                self.audit(room, principal.actor, action, identifier, {'reason': redact(args['reason'])})
                return result
            return self.mutation(principal, room, 'control', args, apply)

    def delivery_status(self, job):
        if job['state'] != 'queued':
            return job['state']
        if not self.config.events_enabled or not self.config.analysis_dispatch_enabled:
            return 'manual_claim_required'
        if self.events is None:
            return 'no_valid_subscription'
        event = self.store.one('''SELECT * FROM mcp_event_outbox WHERE room_id=? AND object_id=?
            AND object_version=? AND name=? AND target_grant_id=?''',
            (job['room_id'], job['id'], job['version'], TASK_EVENT, job['target_grant_id']))
        if not event:
            return 'event_missing'
        room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (job['room_id'],))
        subscriptions = self.store.all('''SELECT * FROM mcp_event_subscriptions WHERE room_id=? AND grant_id=?
            AND name=? AND state='active' AND expires_at>? ORDER BY id LIMIT 32''',
            (job['room_id'], job['target_grant_id'], TASK_EVENT, self.clock()))
        states = []
        for sub in subscriptions:
            try:
                filters = self.events.authorize(sub, room)
            except DevError:
                continue
            if not self.events.matches(sub, filters, event):
                continue
            delivery = self.store.one('SELECT state,accepted_at FROM mcp_event_deliveries WHERE event_id=? AND subscription_id=?',
                                      (event['id'], sub['id']))
            if delivery and delivery['state'] == 'accepted':
                return 'awaiting_consumer' if self.clock() - delivery['accepted_at'] >= 300 else 'event_accepted'
            if not delivery and event['seq'] <= sub['scan_seq']:
                continue  # Before enrollment/replay floor, not awaiting delivery.
            states.append(delivery['state'] if delivery else 'event_queued')
        for status in ('leased', 'retry_wait', 'pending', 'event_queued', 'dead_letter', 'abandoned'):
            if status in states:
                return status
        return 'no_valid_subscription'

    def job_view(self, job, *, full=False):
        public = {key: job[key] for key in ('id', 'room_id', 'goal_id', 'incident_id', 'kind',
            'assignee_agent_id', 'queue', 'state', 'version', 'attempt', 'fencing_token', 'lease_until',
            'deadline_at', 'not_before', 'reason_code', 'created', 'updated', 'tool_calls', 'plan_version')}
        public.update(job_id=job['id'], delivery_status=self.delivery_status(job),
                      allowed_actions=['collaboration_read', 'collaboration_heartbeat', 'collaboration_result', 'collaboration_block'])
        if job['kind'] == 'propose_monitor_plan':
            public['allowed_actions'].append('monitor_plan_save')
        # Keep the legacy receipt field stable for cached consumers; current hosts
        # use the exact tool/action hints rather than an unrestricted work tool.
        public['canonical_actions'] = [
            {'tool': 'collaboration_query', 'action': 'job'},
            {'tool': 'collaboration_work', 'action': 'job_evidence'},
            {'tool': 'collaboration_work', 'action': 'analysis_heartbeat'},
            {'tool': 'collaboration_work', 'action': 'analysis_result'},
            {'tool': 'collaboration_work', 'action': 'analysis_block'},
        ]
        if job['kind'] == 'propose_monitor_plan':
            public['canonical_actions'].append({'tool': 'collaboration', 'action': 'plan_save'})
        if full:
            public['context'] = redact(json.loads(job['context']))
            public['results'] = [self.result_view(row) for row in self.store.all('SELECT * FROM collaboration_results WHERE job_id=? ORDER BY attempt', (job['id'],))]
        return public

    @staticmethod
    def result_view(row):
        return {**row, 'body': redact(json.loads(row['body']))}

    def agent_view(self, room, row):
        result = {key: row[key] for key in ('id', 'label', 'kind', 'queue', 'enabled', 'version', 'expires_at', 'grant_id')}
        result['binding_status'] = 'enabled' if row['enabled'] and row['expires_at'] > self.clock() else 'disabled_or_expired'
        try:
            self.grant_principal(room, row['grant_id'])
        except DevError:
            result['binding_status'] = 'authorization_unavailable'

        sub = self.store.one('''SELECT COUNT(*) AS n,MAX(last_accepted) AS last_accepted FROM mcp_event_subscriptions
            WHERE room_id=? AND grant_id=? AND state='active' AND expires_at>?''',
            (room['id'], row['grant_id'], self.clock()))
        result.update(valid_subscriptions=sub['n'], last_event_accepted=sub['last_accepted'], chat_identity_verified=False)
        result['last_claim'] = self.store.one('''SELECT MAX(a.created) AS at FROM collaboration_attempts a
            JOIN collaboration_jobs j ON j.id=a.job_id WHERE j.assignee_agent_id=?''', (row['id'],))['at']
        result['last_result'] = self.store.one('''SELECT MAX(r.created) AS at FROM collaboration_results r
            JOIN collaboration_jobs j ON j.id=r.job_id WHERE j.assignee_agent_id=?''', (row['id'],))['at']
        return result

    @staticmethod
    def subscription_view(row):
        return {key: row[key] for key in ('id', 'name', 'grant_id', 'state', 'expires_at', 'verified_until',
            'version', 'last_accepted', 'created', 'updated', 'scan_seq', 'ack_seq')} | {'arguments': json.loads(row['arguments'])}

    def listing(self, principal, room, kind, limit, cursor=''):
        tables = {'messages': ('collaboration_messages', 'created'), 'jobs': ('collaboration_jobs', 'created'),
                  'goals': ('collaboration_goals', 'created'), 'incidents': ('monitor_incidents', 'opened_at'),
                  'agents': ('collaboration_agents', 'created'), 'subscriptions': ('mcp_event_subscriptions', 'created')}
        table, column = tables[kind]
        where, parameters = (" AND conversation_id=?", [room['id'], room['id']]) if kind == 'messages' else ('', [room['id']])
        binding = digest([room['id'], principal.grant_id or 'panel', kind])
        if cursor:
            position = read_cursor(self.secret, binding, cursor)
            if (not isinstance(position, list) or len(position) != 2 or isinstance(position[0], bool)
                    or not isinstance(position[0], (float, int)) or not isinstance(position[1], str)):
                raise DevError('INVALID_CURSOR', '分页游标格式不正确', 400)
            where += f' AND ({column}<? OR ({column}=? AND id<?))'
            parameters.extend([position[0], position[0], position[1]])
        rows = self.store.all(f'SELECT * FROM {table} WHERE room_id=?{where} ORDER BY {column} DESC,id DESC LIMIT ?', (*parameters, limit + 1))
        selected = rows[:limit]
        next_cursor = sign_cursor(self.secret, binding, [selected[-1][column], selected[-1]['id']]) if len(rows) > limit else None
        if kind == 'jobs':
            selected = [self.job_view(row) for row in selected]
        elif kind == 'messages':
            selected = self.chatroom.views(selected, principal=principal)
        elif kind == 'agents':
            selected = [self.agent_view(room, row) for row in selected]
        elif kind == 'subscriptions':
            selected = [self.subscription_view(row) for row in selected]
        return {'items': selected, 'next_cursor': next_cursor}

    def read(self, raw, principal):
        args = validate(contracts.Read, raw)
        with self.store.transaction(immediate=False):
            principal, room = self.scope(principal, args, optional=True)
            if args['kind'] == 'rooms':
                project = self.runtime.project(args['project'], principal)
                rows = self.store.all('''SELECT id,project_id,environment_id,state,version,title,topic FROM collaboration_rooms
                    WHERE space_id=? AND owner_user_id=? AND project_id=? ORDER BY created,id LIMIT 64''',
                    (principal.space_id, principal.user_id, project['id']))
                return {'items': rows, 'next_cursor': None}
            if not room:
                return {'room': None, 'features': asdict(self.config), 'setup_required': True,
                        'delegation': self.delegation.authority_descriptor(),
                        'delegation_recovery': self.delegation.consumer.recovery(args, principal),
                        'schema_version': 3, 'capabilities': self.chatroom.capabilities(),
                        'can_manage': bool(principal.admin and not principal.grant_id)}
            kind = args['kind']
            if kind == 'delegation_policies':
                if args['query'].startswith('delegation-consumer-v1:'):
                    return self.delegation.consumer.legacy_read(args, principal, room)
                return self.delegation.listing(args, principal, room)
            if kind == 'delegations':
                return self.delegation.requests(args, principal, room)
            if kind in {'coordination_goals', 'coordination_goal', 'coordination_options'}:
                return self.coordination.listing(args, principal, room)
            self.chatroom.same_room(room, args)
            if kind in {'timeline', 'thread', 'search', 'members', 'changes', 'message_status'}:
                return self.chatroom.read(args, principal, room)
            if args['conversation_id']:
                conversation = self.conversations.resolve(principal, room, args)
                if kind == 'messages':
                    return self.conversations.read({**args, 'kind': 'timeline'}, principal, room)
                if kind in {'job', 'result'}:
                    self.chatroom.check_object_conversation(room, conversation['id'], kind, args['id'])
                if kind in {'jobs', 'goals'}:
                    return self.chatroom.conversation_listing(principal, room, conversation['id'], kind, args['limit'], args['cursor'])
                if kind == 'evidence':
                    if args['result_id']:
                        self.chatroom.check_object_conversation(room, conversation['id'], 'result', args['result_id'])
                        result = self.result_view(self.object('collaboration_results', room, args['result_id']))
                        refs = self.result_refs(result['body'])
                    elif args['job_id']:
                        self.chatroom.check_object_conversation(room, conversation['id'], 'job', args['job_id'])
                        job = self.object('collaboration_jobs', room, args['job_id'])
                        refs = set(json.loads(job['context']).get('evidence_refs', []))
                        if job['incident_id']:
                            refs.add(self.object('monitor_incidents', room, job['incident_id'])['evidence_id'])
                    else:
                        raise DevError('EVIDENCE_CONTEXT_REQUIRED', '聊天室证据读取需要关联任务或结果', 422)
                    if args['id'] not in refs:
                        raise DevError('EVIDENCE_CONTEXT_MISMATCH', '此证据不属于指定任务或结果', 403)
            if kind == 'join_slots':
                return {'items': self.joining.list(room, principal, args['conversation_id']), 'next_cursor': None}
            if kind in {'messages', 'jobs', 'goals', 'incidents', 'agents', 'subscriptions'}:
                return self.listing(principal, room, kind, args['limit'], args['cursor'])
            if kind == 'job':
                return self.job_view(self.object('collaboration_jobs', room, args['id']), full=True)
            if kind == 'result':
                return self.result_view(self.object('collaboration_results', room, args['id']))
            if kind == 'evidence':
                if principal.grant_id:
                    if args['result_id']:
                        self.bound_worker(principal, room, kind='dot')
                        result = self.result_view(self.object('collaboration_results', room, args['result_id']))
                        if args['id'] not in self.result_refs(result['body']):
                            raise DevError('EVIDENCE_CONTEXT_MISMATCH', '证据不在指定结果的引用中', 403)
                    elif args['job_id']:
                        job = self.lease(principal, room, args)
                        permitted = set(json.loads(job['context']).get('evidence_refs', []))
                        if job['incident_id']:
                            incident = self.object('monitor_incidents', room, job['incident_id'])
                            permitted.add(incident['evidence_id'])
                        if args['id'] not in permitted:
                            raise DevError('EVIDENCE_CONTEXT_MISMATCH', '证据不在指定任务的上下文中', 403)
                    else:
                        raise DevError('TASK_CONTEXT_REQUIRED', '证据读取需要有效任务租约或 dot 的结果上下文', 403)
                return self.evidence(room, args['id'])
            if kind == 'plan':
                return (self.monitor.plan_view(room) or {'state': 'not_configured'}) if self.monitor else {'state': 'unsupported'}
            result = {'room': room, 'features': asdict(self.config), 'setup_required': False,
                      'can_manage': bool(principal.admin and not principal.grant_id),
                      'chat_identity_verified': False, 'production_actions_enabled': False,
                      'schema_version': 3, 'capabilities': self.chatroom.capabilities()}
            for item in ('messages', 'jobs', 'goals', 'incidents', 'agents', 'subscriptions'):
                page = self.listing(principal, room, item, args['limit'])
                result[item], result[item + '_next_cursor'] = page['items'], page['next_cursor']
            conversation = self.conversations.resolve(principal, room, args)
            result['conversation'] = self.conversations.view(principal, conversation)
            result['delegation'] = self.delegation.authority_descriptor()
            result['selected_partition'] = {'project_id': room['project_id'], 'environment_id': room['environment_id'], 'room_id': room['id']}
            if args['conversation_id']:
                page = self.conversations.read({**args, 'kind': 'timeline'}, principal, room)
                result['messages'], result['messages_next_cursor'] = page['items'], page['next_cursor']
                result['visibility_token'] = page['visibility_token']
                for item in ('jobs', 'goals'):
                    page = self.chatroom.conversation_listing(principal, room, conversation['id'], item, args['limit'])
                    result[item], result[item + '_next_cursor'] = page['items'], page['next_cursor']
            result['join_slots'] = self.joining.list(room, principal, conversation['id'])
            result['plan'] = self.monitor.plan_view(room) if self.monitor else None
            result['probes'] = self.monitor.probes_view(room, include_targets=bool(principal.admin and not principal.grant_id)) if self.monitor else []
            result['counts'] = {
                'open_jobs': self.store.one("SELECT COUNT(*) AS n FROM collaboration_jobs WHERE room_id=? AND state IN ('queued','leased','running','retry_wait','blocked')", (room['id'],))['n'],
                'open_incidents': self.store.one("SELECT COUNT(*) AS n FROM monitor_incidents WHERE room_id=? AND state!='resolved'", (room['id'],))['n'],
                'pending_proposals': self.store.one("SELECT COUNT(*) AS n FROM collaboration_messages WHERE room_id=? AND state='awaiting_approval'", (room['id'],))['n']}
            if args['conversation_id']:
                result['counts']['open_jobs'] = self.store.one('''SELECT COUNT(*) AS n FROM collaboration_jobs j
                    LEFT JOIN collaboration_goals g ON g.id=j.goal_id JOIN collaboration_messages m
                    ON m.id=COALESCE(json_extract(j.context,'$.origin_message_id'),g.source_message_id)
                    WHERE j.room_id=? AND m.conversation_id=? AND j.state IN ('queued','leased','running','retry_wait','blocked')''',
                    (room['id'], conversation['id']))['n']
                result['counts']['pending_proposals'] = self.store.one("SELECT COUNT(*) AS n FROM collaboration_messages WHERE room_id=? AND conversation_id=? AND state='awaiting_approval'", (room['id'], conversation['id']))['n']
            return result

    def reconcile(self):
        """Rotate a bounded scan; a full early batch cannot starve later rooms."""
        self.coordination.reconcile()
        now = self.clock()
        after = getattr(self, '_reconcile_after', '')
        with self.store.transaction():
            jobs = self.store.all('''SELECT * FROM collaboration_jobs WHERE id>?
                AND state IN ('queued','leased','running','retry_wait') ORDER BY id LIMIT 200''', (after,))
            self._reconcile_after = jobs[-1]['id'] if len(jobs) == 200 else ''
            for job in jobs:
                room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (job['room_id'],))
                try:
                    self.live_room(room)
                    self.agent(room, job['assignee_agent_id'])
                    self.check_plan(room, job)
                except DevError as exc:
                    state, reason, not_before = 'blocked', exc.code, 0
                else:
                    expired_lease = job['state'] in {'leased', 'running'} and (job['lease_until'] or 0) <= now
                    if job['deadline_at'] <= now:
                        state, reason, not_before = 'expired', 'deadline_exceeded', 0
                    elif expired_lease:
                        state = 'dead_letter' if job['attempt'] >= job['max_attempts'] else 'retry_wait'
                        reason, not_before = 'analysis_timeout', now + (30, 120, 600)[max(0, min(job['attempt'] - 1, 2))]
                    elif job['state'] == 'retry_wait' and job['not_before'] <= now:
                        state, reason, not_before = 'queued', '', 0
                    else:
                        continue
                self.store.execute('''UPDATE collaboration_jobs SET state=?,reason_code=?,not_before=?,lease_until=NULL,
                    fencing_token=fencing_token+1,version=version+1,updated=? WHERE id=?''', (state, reason, not_before, now, job['id']))
                current = self.object('collaboration_jobs', room, job['id'])
                if state == 'queued':
                    self.task_event(room, current)
                else:
                    self.emit(room, STATUS_EVENT, job['id'], current['version'],
                              {'component': 'analysis', 'status': state, 'reason_code': reason,
                               'observed_at': timestamp(now), 'recovery_state': 'pending' if state == 'retry_wait' else 'owner_action_required'})

    def invoke(self, name, raw, principal):
        self.guard()
        from shared.public_collaboration import resolve
        name, raw = resolve(name, raw)
        if name in contracts.COORDINATION_TOOL_MODELS:
            return self.coordination.invoke(name, raw, principal)
        handlers = {'collaboration_dot_connection': self.dots.connection,
                    'collaboration_dot_inbox': self.dots.inbox,
                    'collaboration_delegation_connection_read': self.delegation.consumer.connection,
                    'collaboration_delegation_inbox': self.delegation.consumer.inbox,
                    'collaboration_delegation_read': self.delegation.read, 'collaboration_message_create': self.chatroom.create, 'collaboration_join': self.joining.join, 'collaboration_read': self.read, 'collaboration_command_create': self.command,
                    'collaboration_claim': self.claim, 'collaboration_heartbeat': self.heartbeat,
                    'collaboration_result': self.submit, 'collaboration_block': self.block, 'collaboration_ack': self.ack}
        if name in handlers:
            return handlers[name](raw, principal)
        if name in {'monitor_plan_validate', 'monitor_plan_save'} and self.monitor:
            handler = self.monitor.validate_plan if name == 'monitor_plan_validate' else self.monitor.save_plan
            return handler(raw, principal)
        raise DevError('UNKNOWN_TOOL', '未实现的协作工具', 404)
