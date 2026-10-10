"""Joining codes locate room slots; they never grant access or prove chat identity."""
from __future__ import annotations
import json
import secrets
import uuid
from hub.principal import refresh_principal
from hub.collaboration.common import (
    TASK_EVENT, RESULT_EVENT, INCIDENT_EVENT, STATUS_EVENT, canonical, digest, redact, validate,
)
from shared import collaboration_contracts as contracts
from shared.util import DevError


def new_code():
    value = secrets.token_hex(8).upper()
    return 'CPJ-' + '-'.join(value[i:i + 4] for i in range(0, len(value), 4))


class JoiningService:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def slot(self, room, identifier):
        row = self.store.one('SELECT * FROM collaboration_join_slots WHERE id=? AND room_id=?', (identifier, room['id']))
        if row is None:
            raise DevError('JOIN_SLOT_NOT_FOUND', '当前协作室中没有这个智能体位置', 404)
        return row

    def active(self, room, slot, *, registered=False):
        self.c.live_room(room)
        if slot['state'] == 'revoked':
            raise DevError('JOIN_SLOT_REVOKED', '这个智能体位置已撤销；请由面板重新创建', 403)
        if slot['expires_at'] <= self.c.clock():
            raise DevError('JOIN_SLOT_EXPIRED', '这个智能体位置已到期；请由面板重新创建', 403)
        if registered and slot['state'] != 'registered':
            raise DevError('JOIN_REQUIRED', '请先在目标聊天用加入码登记当前连接', 409)

    def member(self, room, slot, principal):
        self.active(room, slot, registered=True)
        self.c.notification_reader(principal)
        saved = json.loads(slot['principal'])
        if saved != self.c.principal_snapshot(principal) or slot['grant_id'] != principal.grant_id:
            raise DevError('JOIN_CONNECTION_MISMATCH', '这个位置已由另一授权连接登记；不能用名字或加入码替换身份', 403)
        self.c.grant_reader(room, slot['grant_id'], snapshot=saved)

    def create(self, raw, principal):
        args = validate(contracts.JoinSlotCreate, raw)
        with self.store.transaction():
            principal, room = self.c.scope(principal, args)
            self.c.owner(principal)
            self.c.live_room(room)
            def save():
                count = self.store.one("SELECT COUNT(*) AS n FROM collaboration_join_slots WHERE room_id=? AND state!='revoked' AND expires_at>?",
                                       (room['id'], self.c.clock()))['n']
                if count >= 16:
                    raise DevError('JOIN_SLOT_LIMIT', '这个协作室已有 16 个有效位置；先撤销不再使用的项', 409)
                identifier, now = uuid.uuid4().hex, self.c.clock()
                self.store.execute('''INSERT INTO collaboration_join_slots
                    (id,room_id,label,kind,join_code,code_expires_at,expires_at,created,updated) VALUES (?,?,?,?,?,?,?,?,?)''',
                    (identifier, room['id'], redact(args['label']), args['kind'], new_code(),
                     now + args['code_ttl_minutes'] * 60, now + args['expires_in_days'] * 86400, now, now))
                return {'id': identifier}
            result = self.c.mutation(principal, room, 'join_slot_create', args, save)
            return {'slot': self.view(room, self.slot(room, result['id']), principal),
                    'message': '位置已创建。复制加入指令到对应聊天；尚未建立订阅或派发任务。'}

    def join(self, raw, principal):
        args = validate(contracts.JoinCode, raw)
        if args['code'].startswith('CPD-'):
            return self.c.dots.join(args, principal)
        with self.store.transaction():
            self.c.guard()
            principal = refresh_principal(self.store, principal)
            self.c.notification_reader(principal)
            found = self.store.one('SELECT * FROM collaboration_join_slots WHERE join_code=?', (args['code'],))
            if found is None:
                raise DevError('JOIN_CODE_UNAVAILABLE', '加入码不存在或已更换；请复制面板里的完整最新指令', 404)
            intended = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (found['room_id'],))
            principal, room = self.c.scope(principal, {'project': intended['project_id'], 'environment_id': intended['environment_id']})
            if room['id'] != intended['id']:
                raise DevError('JOIN_CODE_UNAVAILABLE', '当前连接没有这个房间的访问权限；加入码不会授予权限', 403)
            self.active(room, found)
            if found['state'] == 'registered':
                self.member(room, found, principal)
            def register():
                slot = self.slot(room, found['id'])
                if slot['code_expires_at'] <= self.c.clock():
                    raise DevError('JOIN_CODE_EXPIRED', '加入码已过期；已登记连接可读取现有位置，新的兑换需由面板处理', 409)
                if slot['state'] == 'registered':
                    self.member(room, slot, principal)
                else:
                    self.store.execute('''UPDATE collaboration_join_slots SET state='registered',grant_id=?,
                        principal=?,joined_at=?,updated=?,version=version+1 WHERE id=?''',
                        (principal.grant_id, canonical(self.c.principal_snapshot(principal)), self.c.clock(), self.c.clock(), slot['id']))
                return {'id': slot['id']}
            result = self.c.mutation(principal, room, 'join_code', args, register)
            slot = self.slot(room, result['id'])
            return {'slot': self.view(room, slot, principal), 'registered': True,
                    'permissions_changed': False, 'worker_authorized': False, 'chat_identity_verified': False,
                    'subscription_requests': self.subscription_requests(room, slot),
                    'next_step': '继续使用 Work Cloud 或 dot 的原生事件订阅/自动化能力，完成 subscription_requests 中精确的 name 和 arguments；不要把仅在 CodePier 工具列表中找不到 events/subscribe 当成宿主不支持。先查找宿主能力并读取可订阅事件源，使用实际返回的连接器标识，不从插件名称、插件 ID 或旧会话缓存推导 connector_id。delivery 由宿主提供。插件页可见事件但宿主事件源缺失时报告实际查询结果；创建失败时报告原始错误，不能编造回调或签名材料。',
                    'message': '已登记现有连接。还需宿主订阅和目标聊天收件验证；未创建任务或授予新权限。'}

    @staticmethod
    def subscription_requests(room, slot):
        queue = 'work-analysis' if slot['kind'] == 'work_cloud' else 'dot-coordination'
        names = [TASK_EVENT, STATUS_EVENT] + ([RESULT_EVENT, INCIDENT_EVENT] if slot['kind'] == 'dot' else [])
        return [{'name': name, 'arguments': {'project_id': room['project_id'],
                'environment_id': room['environment_id'], 'slot_id': slot['id'],
                **({'queue': queue} if name == TASK_EVENT else {})}} for name in names]

    @staticmethod
    def subscription_instruction(room, slot, requests):
        host = '原 dot 聊天' if slot['kind'] == 'dot' else '已选择 Cloud 的 Work 聊天'
        return (f'@CodePier 我要在{host}持续接收协作通知，请继续完成已登记位置 {slot["id"]} 的事件订阅。'
                '查找宿主的原生事件订阅/自动化能力，并读取可订阅事件源，选择实际提供下列事件的连接器标识；'
                '不要从插件名称、插件 ID 或旧会话缓存推导 connector_id。'
                f'用 collaboration_query(action=join_slots) 读取项目 {room["project_id"]}、环境 {room["environment_id"]} '
                '的 join_slots，核对当前授权、位置状态和仍缺少的订阅。'
                '按宿主原生确认流程创建全部缺失的事件订阅，已存在的订阅保持原有身份，面板暂停的订阅需先由用户恢复。'
                '收到通知后只读取当前状态并用中文简短汇总，不领取或执行业务任务；测试事件仅回复事件编号。'
                '通知为项目共享；允许创建上述原生事件订阅，不新建 CodePier 凭据或扩大授权。'
                '回调与签名材料由宿主提供；若宿主事件源缺失或校验失败，请报告具体错误并停止，'
                '不要反复兑换加入码或声称已接通。待核对的订阅请求：\n'
                + json.dumps(requests, ensure_ascii=False, indent=2))

    def subscription_guard(self, room, principal, filters, delivery=None):
        identifier = filters.get('slot_id')
        if not identifier:
            return
        slot = self.slot(room, identifier)
        self.member(room, slot, principal)
        queue = 'work-analysis' if slot['kind'] == 'work_cloud' else 'dot-coordination'
        if filters.get('queue') and filters['queue'] != queue:
            raise DevError('JOIN_QUEUE_MISMATCH', '订阅队列与这个 dot / Work 位置的用途不一致', 409)
        if delivery:
            endpoint = digest(delivery['url'])
            for route in self.store.all('''SELECT s.secret FROM collaboration_join_routes r
                    JOIN mcp_event_subscriptions s ON s.id=r.subscription_id
                    JOIN collaboration_join_slots j ON j.id=r.slot_id
                    WHERE r.slot_id!=? AND r.endpoint_digest=? AND j.room_id=?
                    AND j.state='registered' AND j.expires_at>? AND s.expires_at>? AND s.state IN ('active','paused')''',
                    (identifier, endpoint, room['id'], self.c.clock(), self.c.clock())):
                saved = json.loads(self.store.decrypt(route['secret']))
                if delivery['secret'] in {saved.get('current'), saved.get('retiring')}:
                    raise DevError('JOIN_ROUTE_SHARED', '这组宿主回调与签名材料已属于另一个位置；请在目标聊天创建独立订阅', 409)

    def attach_subscription(self, room, principal, filters, subscription, *, reset_verification=False):
        identifier = filters.get('slot_id')
        if not identifier:
            return
        self.member(room, self.slot(room, identifier), principal)
        saved = json.loads(self.store.decrypt(subscription['secret']))
        endpoint = digest(saved['url'])
        old = self.store.one('SELECT * FROM collaboration_join_routes WHERE subscription_id=?', (subscription['id'],))
        watermark = self.store.one('SELECT COALESCE(MAX(seq),0) AS n FROM mcp_event_outbox')['n']
        if old:
            if old['slot_id'] != identifier:
                raise DevError('JOIN_ROUTE_CONFLICT', '已有订阅不能重新绑定到另一个智能体位置', 409)
            if not reset_verification and old['endpoint_digest'] == endpoint and old['credential_digest'] == subscription['key_digest']:
                return
            self.store.execute('''UPDATE collaboration_join_routes SET endpoint_digest=?,credential_digest=?,
                confirmed_event_id='',confirmed_at=NULL,confirmed_by='',test_after_seq=? WHERE subscription_id=?''',
                (endpoint, subscription['key_digest'], watermark, subscription['id']))
        else:
            count = self.store.one("""SELECT COUNT(*) AS n FROM collaboration_join_routes r
                JOIN mcp_event_subscriptions s ON s.id=r.subscription_id WHERE r.slot_id=?
                AND s.state IN ('active','paused') AND s.expires_at>?""", (identifier, self.c.clock()))['n']
            if count >= 8:
                raise DevError('JOIN_ROUTE_LIMIT', '一个位置最多同时保留 8 个有效宿主订阅；需要另一个聊天时请创建独立位置', 409)
            self.store.execute('''INSERT INTO collaboration_join_routes
                (subscription_id,slot_id,endpoint_digest,credential_digest,test_after_seq,created) VALUES (?,?,?,?,?,?)''',
                (subscription['id'], identifier, endpoint, subscription['key_digest'], watermark, self.c.clock()))
        self.store.execute('UPDATE collaboration_join_slots SET version=version+1,updated=? WHERE id=?', (self.c.clock(), identifier))

    def latest_test(self, subscription, after_seq):
        events = self.store.all('''SELECT * FROM mcp_event_outbox WHERE room_id=? AND name=?
            AND target_grant_id=? AND seq>? ORDER BY seq DESC LIMIT 100''',
            (subscription['room_id'], subscription['name'], subscription['grant_id'], after_seq))
        for event in events:
            payload = json.loads(event['data'])
            if payload.get('test') and payload.get('test_subscription_id') == subscription['id']:
                delivery = self.store.one('SELECT state,accepted_at FROM mcp_event_deliveries WHERE subscription_id=? AND event_id=?',
                                          (subscription['id'], event['id']))
                return {'event_id': event['id'], 'state': delivery['state'] if delivery else 'queued',
                        'accepted_at': delivery['accepted_at'] if delivery else None}
        return None

    def routes(self, room, slot):
        result = []
        for row in self.store.all('''SELECT s.*,r.confirmed_event_id,r.confirmed_at,r.confirmed_by,r.test_after_seq
                FROM collaboration_join_routes r JOIN mcp_event_subscriptions s ON s.id=r.subscription_id
                WHERE r.slot_id=? ORDER BY s.created,s.id''', (slot['id'],)):
            available = bool(self.c.events and self.c.config.events_enabled and row['state'] == 'active' and row['expires_at'] > self.c.clock())
            if available:
                try:
                    self.c.events.authorize(row, room)
                except DevError:
                    available = False
            test = self.latest_test(row, row['test_after_seq'])
            confirmed = bool(available and test and test['state'] == 'accepted'
                             and row['confirmed_event_id'] == test['event_id'] and row['confirmed_at'])
            result.append({'id': row['id'], 'name': row['name'], 'state': row['state'],
                           'available': available, 'expires_at': row['expires_at'], 'test': test,
                           'chat_receipt_confirmed': confirmed, 'confirmation_source': 'owner_attested' if confirmed else None,
                           'confirmed_at': row['confirmed_at'] if confirmed else None})
        return result

    def view(self, room, slot, principal):
        dot = self.c.dots.find(slot['id'])
        if dot:
            return self.c.dots.slot_view(room, slot, dot, principal)
        state = slot['state']
        if slot['expires_at'] <= self.c.clock() and state != 'revoked':
            state = 'expired'
        authorization = True
        if state == 'registered':
            try:
                self.c.grant_reader(room, slot['grant_id'], snapshot=json.loads(slot['principal']))
            except DevError:
                authorization = False
        routes = self.routes(room, slot) if slot['state'] == 'registered' else []
        active = [row for row in routes if row['available']]
        expected = {item['name'] for item in self.subscription_requests(room, slot)}
        received = {item['name'] for item in active}
        missing = sorted(expected - received)
        status = state
        if state == 'invited' and slot['code_expires_at'] <= self.c.clock():
            status = 'code_expired'
        if state == 'registered':
            status = 'waiting_subscription'
            if not authorization:
                status = 'authorization_unavailable'
            elif room['state'] != 'active':
                status = 'room_paused'
            elif active:
                status = 'partial_subscription' if missing else 'subscription_verified'
                if any(row['test'] and row['test']['state'] == 'accepted' for row in active):
                    status = 'test_delivered'
                if all(row['chat_receipt_confirmed'] for row in active):
                    status = 'partial_confirmed' if missing else 'chat_confirmed'
        owner = principal.admin and not principal.grant_id
        code = slot['join_code'] if owner and status == 'invited' else None
        resumable = (state == 'registered' and authorization and room['state'] == 'active'
                     and self.c.config.events_enabled and missing and not any(row['state'] == 'paused' for row in routes))
        pending = [item for item in self.subscription_requests(room, slot) if item['name'] in missing]
        return {'id': slot['id'], 'label': slot['label'], 'kind': slot['kind'], 'version': slot['version'],
                'state': state, 'status': status, 'expires_at': slot['expires_at'],
                'code_expires_at': slot['code_expires_at'], 'joined_at': slot['joined_at'], 'join_code': code,
                'join_instruction': (f'@CodePier 我要在本聊天持续接收协作通知。请用加入码 {code} 登记当前已授权连接，'
                                     '再通过宿主的原生事件订阅/自动化能力，按返回的 subscription_requests 创建全部事件订阅。'
                                     '收到通知后只读取当前状态并用中文简短汇总，不领取或执行业务任务；测试事件仅回复事件编号。') if code else None,
                'routes': routes, 'subscription_requests': self.subscription_requests(room, slot) if state == 'registered' else [],
                'subscription_instruction': self.subscription_instruction(room, slot, pending) if resumable else None,
                'chat_identity_verified': False, 'worker_authorized': False,
                'worker_authorized_scope': 'legacy_readonly_monitor_worker_only',
                'notification_scope': 'project_shared',
                'expected_events': sorted(expected), 'missing_events': missing,
                'subscription_count': len(expected & received), 'expected_subscription_count': len(expected),
                'connection_complete': status == 'chat_confirmed',
                'confirmation_event_ids': [row['test']['event_id'] for row in active]
                    if active and all(row['test'] and row['test']['state'] == 'accepted' for row in active) else []}

    def list(self, room, principal, conversation_id=''):
        rows = self.store.all(
            "SELECT * FROM collaboration_join_slots WHERE room_id=? ORDER BY (state='revoked' OR expires_at<=?),created DESC,id DESC LIMIT 64", (room['id'], self.c.clock()))
        result = []
        for row in rows:
            dot = self.c.dots.find(row['id'])
            if dot and ((conversation_id and dot['conversation_id'] != conversation_id)
                        or (principal.grant_id and row['grant_id'] != principal.grant_id)):
                continue
            result.append(self.view(room, row, principal))
        return result

    def control(self, raw, principal):
        args = validate(contracts.JoinSlotControl, raw)
        with self.store.transaction():
            principal, room = self.c.scope(principal, args)
            self.c.owner(principal)
            slot = self.slot(room, args['slot_id'])
            def apply():
                current = self.slot(room, slot['id'])
                if current['version'] != args['expected_version']:
                    raise DevError('STALE_VERSION', '这个智能体位置已改变；请刷新状态后重试', 409)
                action = args['action']
                if action == 'revoke':
                    dot = self.c.dots.find(slot['id'])
                    if dot and dot['policy_id']:
                        policy = self.c.delegation.policy(room, dot['policy_id'])
                        self.c.delegation.stop(policy, 'DOT_REVOKED')
                        self.store.execute("UPDATE delegation_policies SET state='paused',version=version+1,updated=? WHERE id=?", (self.c.clock(), policy['id']))
                    self.store.execute("UPDATE collaboration_join_slots SET state='revoked' WHERE id=?", (slot['id'],))
                    self.store.execute("""UPDATE mcp_event_subscriptions SET state='revoked',version=version+1,updated=?
                        WHERE id IN (SELECT subscription_id FROM collaboration_join_routes WHERE slot_id=?)""", (self.c.clock(), slot['id']))
                    self.store.execute("""UPDATE mcp_event_deliveries SET state='abandoned',lease_until=NULL,fence=fence+1,
                        reason_code='join_slot_revoked' WHERE state IN ('pending','retry_wait','leased')
                        AND subscription_id IN (SELECT subscription_id FROM collaboration_join_routes WHERE slot_id=?)""", (slot['id'],))
                else:
                    self.active(room, current)
                    if action == 'refresh_code':
                        if current['state'] != 'invited':
                            raise DevError('JOIN_ALREADY_REGISTERED', '已登记的位置不更换连接；需要新聊天时请创建独立位置', 409)
                        self.store.execute('UPDATE collaboration_join_slots SET join_code=?,code_expires_at=? WHERE id=?',
                                           (new_code().replace('CPJ-', 'CPD-', 1) if self.c.dots.find(slot['id']) else new_code(),
                                            min(self.c.clock() + 1800, current['expires_at']), slot['id']))
                    else:
                        routes = [row for row in self.routes(room, current) if row['available']]
                        if not routes:
                            raise DevError('JOIN_SUBSCRIPTION_REQUIRED', '还没有可投递的独立宿主订阅；请先在对应聊天完成订阅', 409)
                        if action == 'test':
                            for route in routes:
                                sub = self.store.one('SELECT * FROM mcp_event_subscriptions WHERE id=?', (route['id'],))
                                self.c.events.queue_test(room, sub)
                        else:
                            expected = {row['test']['event_id'] for row in routes if row['test'] and row['test']['state'] == 'accepted'}
                            if not args['confirmed_received'] or len(expected) != len(routes) or set(args['test_event_ids']) != expected:
                                raise DevError('JOIN_RECEIPT_REQUIRED', '先发送测试并在目标聊天核对每个事件，再明确确认；HTTP 接收成功不等于聊天收到', 409)
                            for route in routes:
                                self.store.execute('''UPDATE collaboration_join_routes SET confirmed_event_id=?,confirmed_at=?,confirmed_by=?
                                    WHERE subscription_id=?''', (route['test']['event_id'], self.c.clock(), principal.user_id, route['id']))
                self.store.execute('UPDATE collaboration_join_slots SET version=version+1,updated=? WHERE id=?', (self.c.clock(), slot['id']))
                return {'id': slot['id']}
            result = self.c.mutation(principal, room, 'join_slot_control', args, apply)
            return {'slot': self.view(room, self.slot(room, result['id']), principal),
                    'message': {'revoke': '位置已撤销，关联订阅已停止。', 'refresh_code': '已换成新的 30 分钟加入码，旧码不再可用。',
                                'test': '测试已排队。请到对应聊天核对事件；接收回执不代表已读。',
                                'confirm_chat': '已记录你的目标聊天收件确认；这不等于密码学聊天身份验证。'}[args['action']]}
