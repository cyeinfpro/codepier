"""One panel approval and one host subscription for a task dot.

CPD codes locate an immutable owner-approved setup; they are not credentials.
The existing current-grant, goal, operation and lease checks remain authoritative.
Legacy CPJ notification slots are deliberately untouched.
"""
from __future__ import annotations

import json
from dataclasses import asdict

from hub.principal import Principal, refresh_principal
from hub.collaboration.common import DELEGATION_EVENT, canonical, digest, validate
from shared import collaboration_contracts as contracts
from shared.public_collaboration import request as public_request
from shared.util import DevError


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS collaboration_dots (
        id TEXT PRIMARY KEY REFERENCES collaboration_join_slots(id),
        room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
        conversation_id TEXT NOT NULL REFERENCES conversation_rooms(id),
        setup TEXT NOT NULL,owner_principal TEXT NOT NULL,setup_digest TEXT NOT NULL,
        policy_id TEXT NOT NULL DEFAULT '',policy_version INTEGER NOT NULL DEFAULT 0,
        checkpoint TEXT NOT NULL DEFAULT '',created REAL NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS collaboration_dots_conversation ON collaboration_dots(conversation_id,room_id)')
    db.execute('''CREATE TRIGGER IF NOT EXISTS collaboration_dot_setup_immutable BEFORE UPDATE ON collaboration_dots
        WHEN NEW.id!=OLD.id OR NEW.room_id!=OLD.room_id OR NEW.conversation_id!=OLD.conversation_id
          OR NEW.setup!=OLD.setup OR NEW.owner_principal!=OLD.owner_principal OR NEW.setup_digest!=OLD.setup_digest
        BEGIN SELECT RAISE(ABORT,'dot owner approval is immutable'); END''')
    db.execute('''CREATE TRIGGER IF NOT EXISTS collaboration_dot_baseline_immutable BEFORE UPDATE ON collaboration_dots
        WHEN OLD.policy_id!='' AND (NEW.policy_id!=OLD.policy_id OR NEW.policy_version!=OLD.policy_version
          OR NEW.checkpoint!=OLD.checkpoint)
        BEGIN SELECT RAISE(ABORT,'dot enrollment baseline is immutable'); END''')


class DotService:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def find(self, identifier):
        return self.store.one('SELECT * FROM collaboration_dots WHERE id=?', (identifier,))

    def scope(self, raw, principal, model=contracts.DotRead):
        args = validate(model, raw)
        principal, room = self.c.scope(principal, args)
        dot = self.find(args['dot_id'])
        if not dot or dot['room_id'] != room['id']:
            raise DevError('DOT_NOT_FOUND', '当前项目中没有这个 dot', 404)
        self.c.conversations.object(principal, dot['conversation_id'])
        slot = self.c.joining.slot(room, dot['id'])
        if principal.grant_id:
            self.c.joining.member(room, slot, principal)
        else:
            self.c.owner(principal)
        return args, principal, room, slot, dot

    def create(self, raw, principal):
        args = validate(contracts.DotCreate, raw)
        if not args['duplex']:
            args.pop('duplex')  # Preserve old task-only creation fingerprints.
        with self.store.transaction():
            principal, room = self.c.scope(principal, args, create=True)
            self.c.owner(principal)
            self.c.live_room(room)
            conversation = self.c.conversations.resolve(principal, room, args)
            project = self.c.runtime.project(room['project_id'], principal)
            for capability in args['capabilities']:
                self.c.coordination.require_capability(principal, capability, project['id'])
            target = self.c.delegation.target_snapshot(principal, project, args['execution_target'])
            snapshot = asdict(principal)
            snapshot.update(scopes=[], session_hash='', token_hash='')
            setup = {key: args[key] for key in ('capabilities', 'execution_target', 'acknowledge_unsandboxed_exec')}
            if args.get('duplex'):
                setup['duplex'] = True
            setup.update(project_snapshot=self.c.coordination.project_snapshot(project), target_snapshot=target,
                         purpose='处理房主在此房间明确 @ 此 dot 交办的任务，并回复实际进展、结果和未完成项。')
            def save():
                slot = self.c.joining.create({'project': room['project_id'], 'environment_id': room['environment_id'],
                    'label': args['label'], 'kind': 'dot', 'idempotency_key': 'dot-slot:' + digest(args)}, principal)['slot']
                self.store.execute('UPDATE collaboration_join_slots SET join_code=? WHERE id=?',
                                   (slot['join_code'].replace('CPJ-', 'CPD-', 1), slot['id']))
                self.store.execute('''INSERT INTO collaboration_dots
                    (id,room_id,conversation_id,setup,owner_principal,setup_digest,created) VALUES (?,?,?,?,?,?,?)''',
                    (slot['id'], room['id'], conversation['id'], canonical(setup), canonical(snapshot),
                     digest([setup, snapshot, conversation['id']]), self.c.clock()))
                self.c.audit(room, principal.actor, 'dot.owner_approved', slot['id'],
                             {'conversation_id': conversation['id'], 'capabilities': setup['capabilities'],
                              'execution_target': setup['execution_target'], 'creates_grant': False})
                return {'id': slot['id']}
            result = self.c.mutation(principal, room, 'dot_create:' + conversation['id'], args, save)
            slot = self.c.joining.slot(room, result['id'])
            return {'dot': self.slot_view(room, slot, self.find(slot['id']), principal),
                    'message': '已添加 dot。将加入指令发到目标 dot；绑定后只需一条任务订阅，无需另建委托规则。'}

    def approved_owner(self, room, dot, principal):
        setup, saved = json.loads(dot['setup']), json.loads(dot['owner_principal'])
        if digest([setup, saved, dot['conversation_id']]) != dot['setup_digest']:
            raise DevError('DOT_APPROVAL_CHANGED', '原面板确认内容已改变，不能接入', 409)
        owner = refresh_principal(self.store, Principal(**{**saved, 'scopes': set()}))
        self.c.owner(owner)
        if owner.user_id != room['owner_user_id'] or owner.space_id != room['space_id']:
            raise DevError('DOT_OWNER_CHANGED', '原面板房主身份已改变', 403)
        self.c.conversations.object(owner, dot['conversation_id'])
        for subject in (owner, principal):
            project = self.c.runtime.project(room['project_id'], subject)
            if self.c.coordination.project_snapshot(project) != setup['project_snapshot']:
                raise DevError('DOT_PROJECT_CHANGED', '项目已改变，请在面板重新添加并确认 dot', 409)
            for capability in setup['capabilities']:
                self.c.coordination.require_capability(subject, capability, project['id'])
        if self.c.delegation.target_snapshot(owner, project, setup['execution_target']) != setup['target_snapshot']:
            raise DevError('DOT_TARGET_CHANGED', '已确认执行目标发生变化，请重新确认', 409)
        return owner, setup

    def join(self, raw, principal):
        args = validate(contracts.JoinCode, raw)
        if not args['code'].startswith('CPD-'):
            raise DevError('DOT_CODE_REQUIRED', '任务 dot 必须使用面板生成的 CPD 加入码', 422)
        with self.store.transaction():
            self.c.guard()
            principal = refresh_principal(self.store, principal)
            self.c.notification_reader(principal)
            slot = self.store.one('SELECT * FROM collaboration_join_slots WHERE join_code=?', (args['code'],))
            dot = self.find(slot['id']) if slot else None
            if not dot:
                raise DevError('JOIN_CODE_UNAVAILABLE', '加入码不存在或已更换', 404)
            intended = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (dot['room_id'],))
            principal, room = self.c.scope(principal, {'project': intended['project_id'], 'environment_id': intended['environment_id']})
            if room['id'] != intended['id']:
                raise DevError('JOIN_CODE_UNAVAILABLE', '加入码不属于当前连接账号和项目', 403)
            self.c.conversations.object(principal, dot['conversation_id'])
            self.c.joining.active(room, slot)
            if slot['state'] == 'registered':
                self.c.joining.member(room, slot, principal)
            owner, setup = self.approved_owner(room, dot, principal)
            def enroll():
                current = self.c.joining.slot(room, slot['id'])
                if current['code_expires_at'] <= self.c.clock():
                    raise DevError('JOIN_CODE_EXPIRED', '加入码已过期；已加入的 dot 使用 dot_connection 恢复，不要重新兑换', 409)
                if current['state'] == 'registered':
                    return {'id': current['id']}
                self.store.execute('''UPDATE collaboration_join_slots SET state='registered',grant_id=?,principal=?,
                    joined_at=?,updated=?,version=version+1 WHERE id=?''',
                    (principal.grant_id, canonical(self.c.principal_snapshot(principal)), self.c.clock(), self.c.clock(), slot['id']))
                # This is the exact previously persisted PANEL approval, revalidated above,
                # not authority derived from the joining code or an assistant-authored proposal.
                policy = self.c.delegation.set_policy({
                    'project': room['project_id'], 'environment_id': room['environment_id'],
                    'conversation_id': dot['conversation_id'], 'slot_id': dot['id'], 'expected_version': 0,
                    'purpose': setup['purpose'], 'capabilities': setup['capabilities'],
                    'execution_target': setup['execution_target'],
                    'acknowledge_unsandboxed_exec': setup['acknowledge_unsandboxed_exec'],
                    'automatic_delegation': True, 'idempotency_key': 'dot-policy:' + dot['id']}, owner)['policy']
                policy_row = self.c.delegation.policy(room, policy['id'])
                baseline = self.c.delegation.consumer.checkpoint(policy_row, room, 'managed_execution')
                self.store.execute('UPDATE collaboration_dots SET policy_id=?,policy_version=?,checkpoint=? WHERE id=?',
                                   (policy['id'], policy['version'], baseline, dot['id']))
                return {'id': dot['id']}
            receipt = self.c.mutation(principal, room, 'dot_join', args, enroll)
            result = self.connection({'project': room['project_id'], 'environment_id': room['environment_id'],
                                      'dot_id': receipt['id']}, principal)
            return {**result, 'registered': True, 'permissions_changed': False, 'chat_identity_verified': False,
                    'managed_scope_activated': True, 'approval_source': 'authenticated_panel_owner_setup',
                    'next_step': '请宿主按用户确认创建返回的唯一订阅，并保存 consumer_configuration.wake_instructions。'
                                 '用实际事件源的 connector_id；回调和签名材料由宿主生成。订阅确认后立即调用 inbox_request 读取一次，避免接入期间已排队任务遗漏。成功须以实际 dot 读取和回帖为证据。',
                    'message': ('已加入双向消息房间。请宿主确认原生消息订阅；普通回复与主动发言不需要任务编号。'
                        if self.c.dot_chat.enabled(self.find(receipt['id'])) else
                        '已绑定原授权并接好面板批准的任务范围。还需本 dot 宿主确认任务处理并完成原生事件订阅。')}

    def policy(self, room, dot):
        if not dot['policy_id'] or not dot['checkpoint']:
            raise DevError('DOT_JOIN_REQUIRED', '请先在目标 dot 使用加入码', 409)
        policy = self.c.delegation.policy(room, dot['policy_id'])
        if policy['version'] != dot['policy_version']:
            raise DevError('DOT_SETUP_CHANGED', '此 dot 的原任务范围已变更或暂停；请在面板核对，不自动接受新范围', 409)
        return policy

    @staticmethod
    def subscription(room, dot, policy):
        if json.loads(dot['setup']).get('duplex'):
            from hub.collaboration.dot_chat import DotChatService
            return DotChatService.subscription(room, dot)
        return {'name': DELEGATION_EVENT, 'arguments': {
            'project_id': room['project_id'], 'environment_id': room['environment_id'],
            'conversation_id': dot['conversation_id'], 'slot_id': dot['id'],
            'policy_id': policy['id'], 'policy_version': policy['version']}}

    @staticmethod
    def request(room, dot, action='dot_inbox', **extra):
        return {'tool': 'collaboration_query', 'arguments': {
            'action': action, 'project': room['project_id'], 'environment_id': room['environment_id'],
            'dot_id': dot['id'], **extra}}

    def connection(self, raw, principal):
        with self.store.transaction(immediate=False):
            _, principal, room, slot, dot = self.scope(raw, principal)
            if self.c.dot_chat.enabled(dot):
                self.c.dot_chat.authorize(principal, room, dot)
                configuration = self.c.dot_chat.configuration(room, dot, principal)
                subscription = self.c.dot_chat.subscription(room, dot)
                return {'dot_id': dot['id'], 'slot': self.slot_view(room, slot, dot, principal),
                    'subscription_request': subscription, 'subscription_requests': [subscription],
                    'subscription_created': False, 'permissions_changed': False,
                    'inbox_request': configuration['inbox_request'],
                    'work_inbox_request': configuration['work_inbox_request'],
                    'consumer_configuration': configuration}
            policy = self.policy(room, dot)
            self.c.delegation.authorize_policy(principal, room, policy)
            inbox = self.request(room, dot)
            fallback = public_request('collaboration_delegation_inbox', {
                **self.c.delegation.consumer.arguments(policy, room, 'managed_execution'), 'checkpoint': dot['checkpoint']})
            instructions = ('此 dot 只处理本人已在面板确认的项目任务；宿主用户已明确同意 managed_execution 后才执行。'
                '首次订阅成功、恢复接入，以及每次任务事件唤醒（即使没有事件正文）均先调用 ' + canonical(inbox) + '。'
                '当前目录缺少 dot_inbox 时可用同一连接兼容请求 ' + canonical(fallback) + '；不要重置接入基线。'
                'test=true 只确认测试编号，不领取任务。遍历 next_page_request；每项先用 read_request 核对可信房主原消息与已批准范围，'
                '只按 claim_request 领取 claimable 项；历史 report_only 不执行。用 collaboration_work(action=progress) 回报进展并续租，'
                '每一步使用 collaboration_work(action=execute)，保留原 attempt/fencing_token 和幂等键。'
                'pending/unknown 用 task_query 查询原 operation_id，绝不重投；等待长任务期间每30秒用 heartbeat 续租。实际终态后用 collaboration_work(action=result) '
                '提交真实回执，结果自动回原话题。拒绝或缺权限应回报 blocked，不换凭据、不绕过宿主审批。'
                '结束前用 resume_request 从首屏再次检查所有待办；下一次唤醒复用此配置，不重新加入。')
            subscription = self.subscription(room, dot, policy)
            return {'dot_id': dot['id'], 'slot': self.slot_view(room, slot, dot, principal),
                    'subscription_request': subscription, 'subscription_requests': [subscription],
                    'subscription_created': False, 'permissions_changed': False,
                    'inbox_request': inbox, 'compatible_inbox_request': fallback,
                    'consumer_configuration': {'schema_version': 1, 'mode': 'managed_execution',
                        'mode_evidence': 'requested_only', 'checkpoint_storage': 'server_persisted_once',
                        'scope': self.c.delegation.consumer.summary(policy, room), 'inbox_request': inbox,
                        'wake_instructions': instructions, 'must_persist_before_subscription': True,
                        'historical_work': 'report_only', 'grants_authority': False}}

    def inbox(self, raw, principal):
        with self.store.transaction(immediate=False):
            args, principal, room, _, dot = self.scope(raw, principal, contracts.DotInbox)
            if self.c.dot_chat.enabled(dot):
                return self.c.dot_chat.inbox(raw, principal)
            policy = self.policy(room, dot)
            page = self.c.delegation.consumer.inbox({
                **self.c.delegation.consumer.arguments(policy, room, 'managed_execution'),
                'checkpoint': dot['checkpoint'], 'cursor': args['cursor'], 'limit': args['limit']}, principal)
            page['resume_request'] = self.request(room, dot, limit=args['limit'])
            page['next_page_request'] = self.request(room, dot, cursor=page['next_cursor'], limit=args['limit']) if page['next_cursor'] else None
            return {**page, 'dot_id': dot['id'], 'checkpoint_storage': 'server_persisted_once'}

    def slot_view(self, room, slot, dot, principal):
        setup = json.loads(dot['setup'])
        state = 'expired' if slot['expires_at'] <= self.c.clock() and slot['state'] != 'revoked' else slot['state']
        status, reason, policy, request, progress = state, '', None, None, None
        routes = self.c.joining.routes(room, slot) if state == 'registered' else []
        if state == 'invited' and slot['code_expires_at'] <= self.c.clock():
            status = 'code_expired'
        if state == 'registered':
            try:
                policy = self.policy(room, dot)
                self.c.delegation.authorize_policy(principal, room, policy)
                request = self.subscription(room, dot, policy)
                progress = self.c.delegation.consumer.status(policy, room)
                status = 'subscription_verified' if progress['notification_state'] == 'active' else 'waiting_subscription'
            except DevError as exc:
                reason, status = exc.code, 'authorization_unavailable'
        chat_status, task_reason = None, reason
        duplex = setup.get('duplex') is True
        if duplex and state == 'registered':
            try:
                self.c.dot_chat.authorize(principal, room, dot)
                chat_status = self.c.dot_chat.status(room, dot)
                request = self.c.dot_chat.subscription(room, dot)
                progress = {**(progress or {}), 'notification_state': chat_status['notification_state']}
                reason = ''
                status = 'subscription_verified' if chat_status['notification_state'] == 'active' else 'waiting_subscription'
            except DevError as exc:
                reason, status = exc.code, 'authorization_unavailable'
        from hub.collaboration.common import MESSAGE_EVENT
        expected_event = MESSAGE_EVENT if duplex else DELEGATION_EVENT
        routes = [r for r in routes if r['name'] == expected_event]
        active = [r for r in routes if r['available']]
        owner = bool(principal.admin and not principal.grant_id)
        code = slot['join_code'] if owner and status == 'invited' else None
        instruction = (f'@CodePier 请用加入码 {code} 接入这个任务 dot。'
            '我同意持续接收并处理本人从面板 @ 此 dot 发送的任务，只限面板已确认且当前连接已有的项目权限。'
            '请调用 collaboration(action=join)，然后让宿主按返回的唯一 subscription_request 完成原生事件订阅，'
            '把 consumer_configuration.wake_instructions 保存为持续处理说明。'
            '任务通过插件执行，进度和结果回复原话题；测试事件只确认编号。不要新建凭据或扩大授权。') if code else None
        resume = ('@CodePier 请恢复这个任务 dot 的原订阅和处理配置，不重新兑换加入码或重置基线。调用 '
                  + canonical(self.request(room, dot, 'dot_connection')) + '，按返回配置补齐原生订阅；'
                  '仅在本聊天已经同意任务处理的范围内继续，未同意时先确认。') if state == 'registered' else None
        if duplex:
            instruction = (f'@CodePier 请用加入码 {code} 以双向聊天成员身份加入面板房间。'
                '我同意你持续接收发给你的消息，并通过插件回复、主动发言或提问。普通对话不需要建立任务。'
                '只有本人明确要求实际工作时，才在面板已确认且当前连接已有的范围内处理。'
                '调用 collaboration(action=join)，保存 consumer_configuration.wake_instructions，'
                '由宿主按唯一 subscription_request 完成原生消息订阅，之后立即读取待办。'
                '任务通过插件执行，消息、进度和结果回复原话题；不新建凭据，不同步私人聊天历史。') if code else None
            resume = ('@CodePier 恢复这个已确认双向聊天 dot 的原消息订阅；不重新加入，不重置记录。调用 '
                + canonical(self.request(room, dot, 'dot_connection')) + '，保存返回说明并立即读取待办。'
                '继续按原同意范围交流与处理明确任务；仍需宿主原生确认。') if state == 'registered' else None
        return {'id': slot['id'], 'dot_id': slot['id'], 'task_dot': True,
                'duplex': duplex, 'chat_status': chat_status, 'task_blocked_reason': task_reason if duplex else '',
 'label': slot['label'], 'kind': 'dot',
                'version': slot['version'], 'state': state, 'status': status, 'blocked_reason': reason,
                'conversation_id': dot['conversation_id'], 'expires_at': slot['expires_at'],
                'code_expires_at': slot['code_expires_at'], 'joined_at': slot['joined_at'], 'join_code': code,
                'join_instruction': instruction, 'subscription_instruction': resume,
                'capabilities': setup['capabilities'], 'execution_target': setup['execution_target'],
                'policy_id': dot['policy_id'], 'policy_version': dot['policy_version'],
                'subscription_requests': [request] if request else [], 'routes': routes,
                'expected_events': [expected_event], 'expected_subscription_count': 1,
                'subscription_count': int(bool(active)), 'missing_events': [] if active else [expected_event],
                'notification_scope': 'exact_dot_conversation' if duplex else 'exact_dot_tasks', 'chat_identity_verified': False,
                'connection_complete': bool(active and all(r['chat_receipt_confirmed'] for r in active)),
                'confirmation_event_ids': [r['test']['event_id'] for r in active]
                    if active and all(r['test'] and r['test']['state'] == 'accepted' for r in active) else [],
                'task_status': progress, 'default_dispatch': 'discussion' if duplex else 'automatic', 'approval_source': 'authenticated_panel_owner_setup'}
