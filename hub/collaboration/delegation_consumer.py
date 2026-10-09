"""Read-only consumer contract and reconciliation. No enrollment grants authority."""
from __future__ import annotations

import json

from hub.collaboration.common import DELEGATION_EVENT, digest, read_cursor, sign_cursor, validate
from shared import collaboration_contracts as contracts
from shared.public_collaboration import request as public_request
from shared.util import DevError

PENDING = {'queued', 'running', 'reconnecting', 'cancelling', 'unknown'}
MODES = ('managed_execution', 'notification_only')
READ_TOOLS = ['collaboration_query']
MANAGED_TOOLS = READ_TOOLS + ['collaboration_work', 'task_query']
LEGACY_READ_TOOLS = ['collaboration_read', 'collaboration_delegation_read']
LEGACY_MANAGED_TOOLS = LEGACY_READ_TOOLS + [
    'collaboration_work_claim', 'collaboration_work_heartbeat',
    'collaboration_work_execute', 'collaboration_work_result', 'task_query']
LEGACY_PREFIX = 'delegation-consumer-v1'


def legacy_request(action, arguments):
    """Explicit read-only bridge within the already-discovered v1 Read schema.

    query carries a versioned selector, id is the exact policy, after is the
    immutable signed baseline, and cursor is only the temporary page cursor.
    No generic RPC, permission change or execution can be routed here.
    """
    return {'tool': 'collaboration_read', 'arguments': {
        'project': arguments['project'], 'environment_id': arguments['environment_id'],
        'kind': 'delegation_policies', 'id': arguments['policy_id'],
        'query': ':'.join((LEGACY_PREFIX, action, arguments['mode'], str(arguments['policy_version']))),
        **({'after': arguments['checkpoint'], 'limit': arguments.get('limit', 40),
            **({'cursor': arguments['cursor']} if arguments.get('cursor') else {})}
           if action == 'inbox' else {})}}


def legacy_step(name, arguments):
    return {'tool': name, 'arguments': arguments}


class DelegationConsumer:
    def __init__(self, delegation):
        self.d, self.c, self.store = delegation, delegation.c, delegation.store

    def scope(self, raw, principal, model):
        args = validate(model, raw)
        principal, room = self.c.scope(principal, args)
        policy = self.d.policy(room, args['policy_id'])
        self.d.authorize_policy(principal, room, policy, active=False)
        if policy['version'] != args['policy_version']:
            raise DevError('DELEGATION_POLICY_CHANGED', '接入引用的策略版本已改变，请读取当前设置', 409)
        return args, principal, room, policy

    def arguments(self, policy, room, mode):
        return {'project': room['project_id'], 'environment_id': room['environment_id'],
                'policy_id': policy['id'], 'policy_version': policy['version'], 'mode': mode}

    def contract(self, policy, room, mode):
        request = public_request('collaboration_delegation_connection_read', self.arguments(policy, room, mode))
        fallback = legacy_request('connection', self.arguments(policy, room, mode))
        consent = '先在宿主取得用户对 managed_execution 及此房主范围的明确同意，再开始本次接入。' if mode == 'managed_execution' else ''
        preface = (consent + '仅在本次接入时通过 CodePier ' + request['tool'] + ' 读取接入契约，参数为 '
                   + json.dumps(request['arguments'], ensure_ascii=False, separators=(',', ':')) + '。'
                   '若当前插件尚未发现该工具，使用同一连接的旧版只读入口 ' + json.dumps(fallback, ensure_ascii=False, separators=(',', ':')) + '；两者只选一个。'
                   '请重新扫描插件以发现新工具；不能用 exec、直接RPC或新凭据代替缺失工具。'
                   '服务会返回精确 subscription_request 和初始 checkpoint；请让宿主原生订阅使用原样参数，'
                   '把返回的 checkpoint 和 inbox_request 原样保存到宿主持久消费者配置，仅接入时创建一次基线；'
                   '将返回的 consumer_configuration.wake_instructions 原样保存在持久消费者提示中，不能只保存静态接入说明。'
                   '未来唤醒只用保存的 inbox_request 或 resume_request（旧工具使用 legacy_inbox_request/legacy_resume_request），不能重复调用 connection_read 重置基线。'
                   '从仅通知升级处理模式时，需宿主用户明确同意并重新读取一次新执行基线，不能复用旧只读checkpoint。'
                   '不要手填过滤器或创建凭据。测试事件只确认收件，不领取任务。'
                   '订阅成功、模式声明和此读取都不是用户授权，也不证明模型在线。')
        if mode == 'notification_only':
            instructions = (preface + '本次仅接收提醒并读取、报告待办；禁止 claim、heartbeat、execute 或提交执行结果。'
                '已有只读自动化继续保持只读，不能因升级接入文案改变模式。'
                '使用保存的 checkpoint 分页读 inbox，历史待办只报告；结束前从首屏重新协调一次。')
        else:
            instructions = (preface + '按宿主用户已明确同意的 managed_execution 模式处理此基线之后的将来明确委托；'
                '若未同意则只报告，不领取。保存首次 checkpoint，初次看到的历史待办只报告，不自动回放；'
                '房主继续派发的新事件可恢复该旧任务。每次唤醒即使没有payload，也按精确policy/version读取inbox。'
                '遍历全部分页，逐条fresh-read委托，核对可信房主、原消息和本次目标/能力子集；范围不符或不清楚则报告阻塞。'
                '只领取claimable项，按服务返回的claim_request调用collaboration_work(action=claim)，冲突继续下一项；维持短租约并用原attempt/fence调用collaboration_work(action=heartbeat)续约。'
                '每个步骤只用collaboration_work(action=execute)，旧工具使用collaboration_work_execute；claim/heartbeat/result同样使用对应旧工具名，业务租约和幂等键不变。'
                '遇pending或unknown只用task_query查询原operation_id直到明确结果，绝不重投。'
                '租约失效不能重领已有操作；报告待核，不绕过平台拒绝。'
                '完成后用collaboration_work(action=result)提交真实operation回执，结果自动回复原话题。'
                '继续协调本次唤醒的下一条待办，结束前清空分页cursor并从首屏重读；若事件序号变化则再协调，'
                '不要把最大任务/事件序号当永久游标。通知缺失也不能推断执行成功。')
        return {'schema_version': 1, 'mode': mode, 'mode_evidence': 'requested_only',
                'grants_authority': False, 'required_tools': MANAGED_TOOLS if mode == 'managed_execution' else READ_TOOLS,
                'required_tool_sets': [MANAGED_TOOLS, LEGACY_MANAGED_TOOLS] if mode == 'managed_execution' else [READ_TOOLS, LEGACY_READ_TOOLS],
                'missing_primary_tools_action': 'rescan_plugin_or_use_exact_legacy_requests', 'legacy_read_request': fallback,
                'instructions': instructions, 'read_request': request,
                'scope_summary': self.summary(policy, room)}

    def recovery(self, args, principal, room=None, policies=None):
        """Diagnose only the caller's visible scope, without enrolling or claiming."""
        project = self.c.runtime.project(args['project'], principal)
        scope = {'project': project['id'], 'environment_id': args['environment_id']}
        if room is None:
            code, action = 'COLLABORATION_ROOM_REQUIRED', 'owner_setup_or_check_connection'
            instructions = ('当前连接可见范围内没有此项目和环境的协作房间；核对面板账号、项目、环境和 dot 连接。'
                '由房主在面板建立或关联房间后再派发，不要编造 delegation_id 或把项目执行权限当作委托。')
            read = public_request('collaboration_read', {**scope, 'kind': 'rooms'})
        elif not policies:
            code, action = 'DELEGATION_POLICY_REQUIRED', 'owner_bind_current_connection'
            instructions = ('当前连接在此聊天室没有可见委托规则；这不证明其他连接没有规则。'
                '请房主核对接收位置绑定的连接和允许范围，建立真实委托后再接入；重复允许不会创建委托。')
            read = public_request('collaboration_read', {**scope, 'kind': 'delegation_policies',
                'conversation_id': args.get('conversation_id') or room['id']})
        else:
            code, action = 'SAVED_INBOX_REQUIRED', 'resume_saved_inbox'
            instructions = ('缺少 delegation_id 时，先使用宿主已保存的 inbox_request 或 legacy_inbox_request 查找待办，'
                '再按每项的 read_request 或 legacy_read_request 核验真实委托，不要求用户重复派发。'
                '基线丢失时不要猜测或静默重置；仅重新接入建立报告基线，历史任务由房主明确重新派发。'
                '发现规则不代表已同意执行，保持原消费者模式和权限。')
            read = None
        return {'reason_code': code, 'next_action': action, 'instructions': instructions,
                'project_id': project['id'], 'environment_id': args['environment_id'],
                'visibility': 'current_connection_only', 'read_request': read,
                'permissions_changed': False, 'grants_authority': False}

    def contracts(self, policy, room):
        return {mode: self.contract(policy, room, mode) for mode in MODES}

    @staticmethod
    def summary(policy, room):
        spec = json.loads(policy['spec'])
        return {'project_id': room['project_id'], 'environment_id': room['environment_id'],
                'conversation_id': policy['conversation_id'], 'slot_id': policy['slot_id'],
                'grant_id': policy['grant_id'], 'policy_id': policy['id'], 'policy_version': policy['version'],
                'purpose': spec['purpose'], 'execution_targets': spec['execution_targets'],
                'capabilities': spec['capabilities'], 'expires_at': policy['expires_at']}

    def status(self, policy, room):
        items = self.store.all("""SELECT w.*,g.state AS goal_state,g.approval_id AS goal_approval,g.expires_at AS goal_expires
            FROM coordination_work w JOIN delegation_requests d ON d.goal_id=w.goal_id
            JOIN coordination_goals g ON g.id=w.goal_id WHERE d.policy_id=? AND d.policy_version=?""", (policy['id'], policy['version']))
        active = [w for w in items if w['state'] in {'leased', 'running'} and (w['lease_until'] or 0) > self.c.clock()
                  and w['goal_state'] == 'active' and w['goal_approval'] == w['approval_id']
                  and (w['goal_expires'] or 0) > self.c.clock()]
        operations = self.store.all("""SELECT o.state FROM coordination_operations c
            JOIN delegation_requests d ON d.goal_id=c.goal_id LEFT JOIN operations o ON o.id=c.operation_id
            WHERE d.policy_id=? AND d.policy_version=?""", (policy['id'], policy['version']))
        pending = sum(op['state'] in PENDING or op['state'] is None for op in operations)
        unknown = sum(op['state'] in {None, 'unknown'} for op in operations)
        results = sum(bool(w['result']) for w in items)
        deliveries = self.store.all("""SELECT d.state,d.accepted_at FROM mcp_event_deliveries d
            JOIN mcp_event_outbox e ON e.id=d.event_id WHERE e.room_id=? AND e.name=?
            AND json_extract(e.data,'$.policy_id')=? AND json_extract(e.data,'$.policy_version')=?""",
            (room['id'], DELEGATION_EVENT, policy['id'], policy['version']))
        accepted = [d['accepted_at'] for d in deliveries if d['state'] == 'accepted' and d['accepted_at']]
        return {**self.d.notification_status(policy, room), 'model_online': 'unknown',
                'consumer_mode': 'unknown', 'mode_evidence': 'not_observed',
                'notification': {'delivery_state': 'accepted' if accepted else 'not_observed',
                    'accepted_count': len(accepted), 'last_accepted_at': max(accepted) if accepted else None,
                    'pending_count': sum(d['state'] in {'pending', 'retry_wait', 'leased'} for d in deliveries),
                    'read_verified': False},
                'claim': {'state': 'leased' if active else 'not_observed', 'active_count': len(active),
                          'observed_count': sum(w['attempt'] > 0 for w in items)},
                'operation': {'state': 'unknown' if unknown else 'pending' if pending else 'settled' if operations else 'not_observed',
                              'pending_count': pending, 'unknown_count': unknown, 'count': len(operations)},
                'result': {'state': 'reported' if results else 'not_observed', 'count': results}}

    @staticmethod
    def next_action(status, reason):
        if reason:
            return {'code': 'review_policy', 'label': '查看范围阻塞原因', 'reason': reason}
        if status['operation']['pending_count']:
            return {'code': 'poll_original_operations', 'label': '查询原操作进度'}
        if status['claim']['active_count']:
            return {'code': 'review_leased_work', 'label': '查看已领取工作与宿主处理约定'}
        if status['notification_state'] != 'active':
            return {'code': 'connect_host', 'label': '接通原生提醒'}
        return {'code': 'reconcile_inbox', 'label': '读取当前待办；消费模式尚未核实'}

    def presentation(self, policy, room, reason=''):
        spec, options = json.loads(policy['spec']), []
        for target in spec['execution_targets']:
            label = '项目 Agent · ' + room['project_id']
            if target.startswith('vps:'):
                row = self.store.one('SELECT name FROM vps_connections WHERE id=?', (target[4:],))
                label = 'VPS · ' + (row['name'] if row else target[4:])
            options.append({'id': target, 'label': label, 'available': not bool(reason)})
        status = self.status(policy, room)
        if reason:
            status['claim'].update(state='blocked', active_count=0)
        return {'consumer_contracts': self.contracts(policy, room), 'execution_target_options': options,
                'connection_status': status, 'next_action': self.next_action(status, reason)}

    def binding(self, policy, room, mode):
        return digest(['delegation-consumer-checkpoint-v1', room['id'], policy['id'],
                       policy['version'], policy['grant_id'], mode])

    def high_water(self, policy, room):
        return self.store.one("""SELECT MAX(seq) AS n FROM mcp_event_outbox WHERE room_id=? AND name=?
            AND json_extract(data,'$.policy_id')=? AND json_extract(data,'$.policy_version')=?""",
            (room['id'], DELEGATION_EVENT, policy['id'], policy['version']))['n'] or 0

    def checkpoint(self, policy, room, mode):
        return sign_cursor(self.c.secret, self.binding(policy, room, mode), self.high_water(policy, room))

    def legacy_read(self, args, principal, room):
        parts = args['query'].split(':')
        if (len(parts) != 4 or parts[0] != LEGACY_PREFIX or parts[1] not in {'connection', 'inbox'}
                or parts[2] not in MODES or not parts[3].isascii() or not parts[3].isdigit()
                or not args['id']):
            raise DevError('INVALID_CONSUMER_REQUEST', '旧版消费者读取参数无效，请使用服务返回的精确请求', 422)
        if any(args.get(key) for key in ('client_message_id', 'job_id', 'result_id', 'attempt', 'fencing_token')):
            raise DevError('INVALID_CONSUMER_REQUEST', '消费者读取不能混入其他记录或执行参数', 422)
        policy = self.d.policy(room, args['id'])
        self.d.authorize_policy(principal, room, policy, active=False)
        if ((args.get('conversation_id') and args['conversation_id'] != policy['conversation_id'])
                or (args.get('room_id') and args['room_id'] != policy['conversation_id'])):
            raise DevError('DELEGATION_POLICY_CHANGED', '消费者读取不能切换原聊天室', 409)
        raw = {'project': args['project'], 'environment_id': args['environment_id'],
               'policy_id': args['id'], 'policy_version': int(parts[3]), 'mode': parts[2]}
        if parts[1] == 'connection':
            if args['after'] or args['cursor']:
                raise DevError('INVALID_CONSUMER_REQUEST', '接入读取不能接收旧基线或分页游标', 422)
            return self.connection(raw, principal)
        if not args['after']:
            raise DevError('CONSUMER_CHECKPOINT_REQUIRED', '请使用首次接入保存的基线；缺失时仅重新接入并报告历史任务', 422)
        return self.inbox({**raw, 'checkpoint': args['after'], 'cursor': args['cursor'], 'limit': args['limit']}, principal)

    def configuration(self, policy, room, mode, checkpoint):
        arguments = {**self.arguments(policy, room, mode), 'checkpoint': checkpoint}
        primary = public_request('collaboration_delegation_inbox', arguments)
        fallback = legacy_request('inbox', arguments)
        instructions = ('这是已保存的消费者范围，模式为 ' + mode + '。保持原模式和初始基线。'
            '每次唤醒，无论是否收到事件payload，先通过当前已发现的插件工具执行以下只读inbox请求之一：'
            + json.dumps([primary, fallback], ensure_ascii=False, separators=(',', ':')) + '。'
            '不重新接入、不重置checkpoint，不用exec或直接RPC代替插件。'
            '遍历next_page_request或legacy_next_page_request，结束前用resume_request或legacy_resume_request从首屏重读。'
            '仅通知模式只读和报告。处理模式还必须已有宿主用户明确同意；逐条fresh-read可信房主、原消息、精确项目目标与能力。'
            '历史report_only和测试事件不领取；仅claimable可按返回请求领取，所有步骤用受管work工具及原attempt/fence。'
            'pending/unknown只查询原operation_id，不重投；保持租约，实际终态后提交work_result，自动回复原话题。'
            '目标、权限或工具不匹配时报告明确阻塞，不换凭据、不扩大范围。')
        return {'schema_version': 1, 'scope': self.summary(policy, room), 'mode': mode,
                'mode_evidence': 'requested_only', 'checkpoint': checkpoint,
                'inbox_request': primary, 'legacy_inbox_request': fallback,
                'wake_instructions': instructions, 'must_persist_before_subscription': True,
                'grants_authority': False, 'historical_work': 'report_only'}

    def connection(self, raw, principal):
        with self.store.transaction(immediate=False):
            args, principal, room, policy = self.scope(raw, principal, contracts.DelegationConnectionRead)
            view = self.d.view(policy, principal, room)
            checkpoint = self.checkpoint(policy, room, args['mode'])
            configuration = self.configuration(policy, room, args['mode'], checkpoint)
            return {'policy': view, 'consumer_contract': view['consumer_contracts'][args['mode']],
                    'subscription_request': view['subscription_request'], 'checkpoint': checkpoint,
                    'checkpoint_kind': 'initial_report_only_baseline', 'checkpoint_grants_authority': False,
                    'inbox_request': public_request('collaboration_delegation_inbox',
                        {**self.arguments(policy, room, args['mode']), 'checkpoint': checkpoint}),
                    'legacy_inbox_request': configuration['legacy_inbox_request'],
                    'consumer_configuration': configuration,
                    'status': view['connection_status'], 'next_action': view['next_action'],
                    'permissions_changed': False, 'subscription_created': False}

    def inbox(self, raw, principal):
        with self.store.transaction(immediate=False):
            args, principal, room, policy = self.scope(raw, principal, contracts.DelegationInbox)
            binding = self.binding(policy, room, args['mode'])
            checkpoint = args['checkpoint'] or self.checkpoint(policy, room, args['mode'])
            baseline = read_cursor(self.c.secret, binding, checkpoint)
            if isinstance(baseline, bool) or not isinstance(baseline, int) or baseline < 0:
                raise DevError('INVALID_CURSOR', '接入checkpoint无效', 400)
            current = self.high_water(policy, room)
            page_binding = digest([binding, checkpoint, 'page', args['mode']])
            position = read_cursor(self.c.secret, page_binding, args['cursor']) if args['cursor'] else [current, 0, '']
            if (not isinstance(position, list) or len(position) != 3
                    or any(isinstance(n, bool) or not isinstance(n, int) or n < 0 for n in position[:2])
                    or not isinstance(position[2], str) or position[0] > current):
                raise DevError('INVALID_CURSOR', '待办分页游标无效', 400)
            ceiling, after_seq, after_id = position
            # Snapshot pagination is temporary. Every wake rescans current work;
            # requeued/reordered old tasks get a new event and remain discoverable.
            rows = self.store.all("""SELECT d.id AS delegation_id,d.message_id,d.goal_id,w.id AS work_item_id,
                COALESCE((SELECT MAX(e.seq) FROM mcp_event_outbox e WHERE e.name=? AND e.object_id=w.id
                    AND e.seq<=? AND json_extract(e.data,'$.policy_id')=d.policy_id
                    AND json_extract(e.data,'$.policy_version')=d.policy_version),0) AS event_seq
                FROM delegation_requests d JOIN coordination_work w ON w.goal_id=d.goal_id
                WHERE d.policy_id=? AND d.policy_version=? ORDER BY event_seq,w.id""",
                (DELEGATION_EVENT, ceiling, policy['id'], policy['version']))
            rows = [row for row in rows if (row['event_seq'], row['work_item_id']) > (after_seq, after_id)]
            selected, items = rows[:args['limit']], []
            for row in selected:
                goal = self.c.coordination.object(room, row['goal_id'])
                self.c.coordination.authorize(principal, room, goal)
                item = self.c.coordination.work(goal, row['work_item_id'])
                operations = self.store.all("""SELECT c.operation_id,o.state,c.attempt FROM coordination_operations c
                    LEFT JOIN operations o ON o.id=c.operation_id WHERE c.work_item_id=? ORDER BY c.created,c.operation_id""", (item['id'],))
                reason = ''
                try:
                    self.c.coordination.authorize(principal, room, goal, active=True)
                    actor = self.c.grant_reader(room, policy['grant_id'])
                    self.c.coordination.work_capabilities(actor, goal, item)
                    if item['assignee_grant_id'] != policy['grant_id'] or item['approval_id'] != goal['approval_id']:
                        raise DevError('WORK_APPROVAL_CHANGED', '工作项不再属于本次明确批准', 409)
                except DevError as exc:
                    reason = exc.code
                own = bool(principal.grant_id and item['assignee_grant_id'] == principal.grant_id)
                pending = any(op['state'] in PENDING or op['state'] is None for op in operations)
                historical = row['event_seq'] <= baseline
                dependencies = any(self.c.coordination.work(goal, dep)['state'] != 'succeeded'
                                   for dep in json.loads(item['dependencies']))
                if item['state'] in {'succeeded', 'failed', 'cancelled', 'expired'}:
                    category = 'terminal'
                elif pending:
                    category = 'operation_review'
                elif reason or item['state'] == 'blocked':
                    category = 'blocked'
                elif historical:
                    category, reason = 'report_only', 'initial_history'
                elif item['state'] in {'leased', 'running'}:
                    category = 'own_running' if own and (item['lease_until'] or 0) > self.c.clock() else 'blocked'
                    if category == 'blocked':
                        reason = 'WORK_LEASE_EXPIRED'
                elif operations:
                    category, reason = 'operation_review', 'operation_admitted_requires_review'
                elif item['state'] == 'queued' and not dependencies and not historical:
                    category = 'claimable'
                else:
                    category = 'report_only' if historical else 'blocked'
                    reason = reason or ('initial_history' if historical else 'WORK_DEPENDENCY_PENDING')
                # A requested mode is not proof of the host user's consent.
                action = ('poll_original_operations' if pending else
                          'renew_lease_and_continue' if category == 'own_running' else
                          'fresh_read_then_claim_if_host_authorized' if category == 'claimable' else 'report')
                if args['mode'] == 'notification_only':
                    action = 'report'
                items.append({**row, 'category': category, 'reason_code': reason,
                    'historical_report_only': historical, 'work_item': self.c.coordination.work_view(item),
                    'operations': operations, 'next_action': action,
                    'read_request': public_request('collaboration_delegation_read',
                        {'project': room['project_id'], 'environment_id': room['environment_id'],
                         'delegation_id': row['delegation_id']}),
                    'claim_request': public_request('collaboration_work_claim',
                        {'project': room['project_id'], 'environment_id': room['environment_id'],
                         'goal_id': item['goal_id'], 'work_item_id': item['id'],
                         'expected_version': item['version'],
                         'idempotency_key': 'consumer-claim:' + digest([policy['id'], policy['version'],
                             item['id'], item['version'], checkpoint])})
                        if category == 'claimable' and args['mode'] == 'managed_execution' else None})
            for item in items:
                item['legacy_read_request'] = legacy_step('collaboration_delegation_read',
                    {key: value for key, value in item['read_request']['arguments'].items() if key != 'action'})
                claim = item['claim_request']
                item['legacy_claim_request'] = legacy_step('collaboration_work_claim',
                    {key: value for key, value in claim['arguments'].items() if key != 'action'}) if claim else None
            more = len(rows) > args['limit']
            next_cursor = sign_cursor(self.c.secret, page_binding,
                [ceiling, selected[-1]['event_seq'], selected[-1]['work_item_id']]) if more else None
            resume = {**self.arguments(policy, room, args['mode']), 'checkpoint': checkpoint, 'limit': args['limit']}
            page = {**resume, 'cursor': next_cursor} if next_cursor else None
            return {'items': items, 'next_cursor': next_cursor, 'checkpoint': checkpoint,
                    'legacy_resume_request': legacy_request('inbox', resume),
                    'next_page_request': public_request('collaboration_delegation_inbox', page) if page else None,
                    'legacy_next_page_request': legacy_request('inbox', page) if page else None,
                    'snapshot_event_seq': ceiling, 'current_event_seq': current,
                    'reconcile_required': current != ceiling, 'restart_from_first_page_before_sleep': True,
                    'mode': args['mode'], 'mode_evidence': 'requested_only',
                    'permissions_changed': False, 'claimed': False,
                    'resume_request': public_request('collaboration_delegation_inbox',
                        {**self.arguments(policy, room, args['mode']), 'checkpoint': checkpoint,
                         'limit': args['limit']})}
