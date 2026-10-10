"""Durable Agent follow-through for an explicitly confirmed, healthy panel update."""
from __future__ import annotations
import asyncio
import contextlib
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode

from hub import iam
from hub.agent_install import AgentPackage
from hub.principal import Principal, refresh_principal
from shared.util import DevError, VERSION

SCHEMA = """CREATE TABLE IF NOT EXISTS panel_agent_rollouts (
    request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, target_version TEXT NOT NULL,
    from_version TEXT NOT NULL, principal TEXT NOT NULL, state TEXT NOT NULL,
    nodes TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL,
    job_id TEXT NOT NULL DEFAULT '', message TEXT NOT NULL DEFAULT ''
)"""
DONE = {'succeeded', 'failed', 'skipped', 'blocked'}
ACTIVE_OPS = {'queued', 'running', 'reconnecting', 'cancelling', 'unknown'}


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}', value):
        return None
    return tuple(map(int, value.split('.')))


def device_info(device):
    try:
        value = json.loads(device.get('info') or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


class PanelAgentRollouts:
    def __init__(self, runtime, bridge, package=None):
        self.runtime, self.store, self.bridge = runtime, runtime.store, bridge
        self.package = package or AgentPackage(Path(__file__).resolve().parent.parent)
        self.store.execute(SCHEMA)
        self.store.execute("""CREATE TABLE IF NOT EXISTS panel_update_consents (
            request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
            user_id TEXT NOT NULL, space_id TEXT NOT NULL)""")
        self.task = None
        self.lock = asyncio.Lock()

    def confirm(self, body, principal):
        # Store.run serializes work but does not supply a transaction.
        with self.store.transaction():
            return self._confirm(body, principal)

    def _confirm(self, body, principal):
        """Bind consent and the original node/identity snapshot in one commit."""
        principal = refresh_principal(self.store, principal)
        if not principal.instance_admin or principal.grant_id:
            raise DevError('INSTANCE_ADMIN_REQUIRED', '需要原面板实例管理员授权', 403)
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        old = self.store.one('SELECT * FROM panel_update_consents WHERE request_key=?', (body['idempotency_key'],))
        if old and (old['fingerprint'] != fingerprint or old['user_id'] != principal.user_id or old['space_id'] != principal.space_id):
            raise DevError('IDEMPOTENCY_CONFLICT', '原更新请求的版本或 Agent 更新选择不能改变', 409)
        if old and body.get('update_agents') is True and not self.store.one(
                'SELECT request_key FROM panel_agent_rollouts WHERE request_key=?', (body['idempotency_key'],)):
            raise DevError('AGENT_CONSENT_INCOMPLETE', '原更新授权快照不完整；未重新选择节点，请查看原操作', 409)
        if not old:
            if self.store.one('SELECT count(*) AS n FROM panel_update_consents')['n'] >= 1000:
                raise DevError('UPDATE_QUOTA', '更新确认记录已满，请先归档历史记录', 409)
            self.store.execute('INSERT INTO panel_update_consents VALUES (?,?,?,?)',
                               (body['idempotency_key'], fingerprint, principal.user_id, principal.space_id))
        return self.prepare(body, principal) if body.get('update_agents') is True else False

    def prepare(self, body, principal):
        principal = refresh_principal(self.store, principal)
        if not principal.instance_admin or principal.grant_id:
            raise DevError('INSTANCE_ADMIN_REQUIRED', '自动更新需要原面板实例管理员授权', 403)
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        old = self.store.one('SELECT * FROM panel_agent_rollouts WHERE request_key=?', (body['idempotency_key'],))
        if old:
            if old['fingerprint'] != fingerprint:
                raise DevError('IDEMPOTENCY_CONFLICT', '更新编号已用于其他版本或内容', 409)
            saved = json.loads(old['principal'])
            if saved['user_id'] != principal.user_id or saved['space_id'] != principal.space_id:
                raise DevError('IDEMPOTENCY_CONFLICT', '更新编号属于另一个管理员或空间', 409)
            return False
        if self.store.one('SELECT count(*) AS n FROM panel_agent_rollouts')['n'] >= 1000:
            raise DevError('UPDATE_QUOTA', '自动更新记录已满，请先归档历史记录', 409)
        # Durable intent stores identity/epoch, never a credential or a new grant.
        authority = {key: getattr(principal, key) for key in
                     ('actor', 'user_id', 'space_id', 'user_epoch', 'identity_id')}
        nodes = []
        for device in self.store.all('SELECT * FROM devices WHERE space_id=? ORDER BY id', (principal.space_id,)):
            try:
                iam.require_device(self.store, principal, device['id'], manage=True)
            except DevError:
                continue
            info = device_info(device)
            management = info.get('management') or {}
            reason = ''
            if not device['enabled']:
                reason = '节点已停用，本次不自动更新'
            elif not isinstance(management, dict) or not management.get('managed') or not management.get('service'):
                reason = '不是受管 Agent，请使用原安装方式手动更新'
            elif 'agent_update' not in info.get('device_actions', []):
                reason = 'Agent 未声明安全更新能力，请先修复基础组件'
            nodes.append({'device_id': device['id'], 'name': device['name'],
                          'state': 'blocked' if reason else 'waiting_panel',
                          'message': reason or '等待面板健康检查和提交',
                          'version': info.get('version', ''), 'operation_id': '',
                          'idempotency_key': 'panel-agent-' + hashlib.sha256(
                              (body['idempotency_key'] + ':' + device['id']).encode()).hexdigest()})
        now = time.time()
        self.store.execute("""INSERT INTO panel_agent_rollouts
            (request_key,fingerprint,target_version,from_version,principal,state,nodes,created,updated)
            VALUES (?,?,?,?,?,'waiting_panel',?,?,?)""",
            (body['idempotency_key'], fingerprint, body['version'], VERSION,
             json.dumps(authority), json.dumps(nodes), now, now))
        return True

    def rejected(self, key, message):
        self.store.execute("UPDATE panel_agent_rollouts SET state='blocked',message=?,updated=? WHERE request_key=? AND state='waiting_panel'",
                           (message[:500], time.time(), key))

    def save(self, row, nodes, state=None, message=None):
        self.store.execute('UPDATE panel_agent_rollouts SET nodes=?,state=?,message=?,job_id=?,updated=? WHERE request_key=?',
                           (json.dumps(nodes), state or row['state'], message if message is not None else row['message'],
                            row['job_id'], time.time(), row['request_key']))

    def view(self, principal, key=''):
        principal = refresh_principal(self.store, principal)
        if not principal.instance_admin:
            raise DevError('INSTANCE_ADMIN_REQUIRED', '需要实例管理员权限', 403)
        rows = self.store.all('SELECT * FROM panel_agent_rollouts WHERE request_key=?', (key,)) if key else self.store.all(
            'SELECT * FROM panel_agent_rollouts ORDER BY created DESC')
        row = next((item for item in rows if json.loads(item['principal'])['space_id'] == principal.space_id), None)
        if not row:
            return None
        nodes = []
        for node in json.loads(row['nodes']):
            try:
                iam.require_device(self.store, principal, node['device_id'], manage=True)
            except DevError:
                continue
            nodes.append({key: node.get(key, '') for key in
                          ('device_id', 'name', 'state', 'message', 'version', 'operation_id')})
        return {'state': row['state'], 'target_version': row['target_version'], 'message': row['message'],
                'updated': row['updated'], 'nodes': nodes,
                'completed': sum(node['state'] in DONE for node in nodes), 'total': len(nodes)}

    async def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name='panel-agent-rollouts')

    async def stop(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception:
                # Durable intent and original operation IDs survive transport errors.
                pass
            await asyncio.sleep(3)

    async def tick(self):
        async with self.lock:
            rows = await self.store.run(self.store.all,
                "SELECT * FROM panel_agent_rollouts WHERE state IN ('waiting_panel','running') ORDER BY created")
            for row in rows:
                await self.advance(row)

    async def advance(self, row):
        nodes = json.loads(row['nodes'])
        if row['state'] == 'waiting_panel':
            try:
                status = await self.bridge.request('GET', '/status?' + urlencode({'request_key': row['request_key']}))
            except DevError:
                return
            job = status.get('operation') or {}
            if not status.get('request_found') or job.get('request_key') != row['request_key'] or job.get('kind') != 'apply':
                return
            row['job_id'] = job.get('id', '')
            if job.get('state') in {'failed', 'rolled_back', 'recovery_required'}:
                for node in nodes:
                    node.update(state='blocked', message='面板更新未成功，未发起 Agent 更新')
                await self.store.run(self.save, row, nodes, 'blocked', '面板更新未成功，Agent 保持原状态')
                return
            if not (job.get('state') == 'succeeded' and job.get('phase') == 'done' and
                    job.get('commit_decided') is True and job.get('target_version') == row['target_version'] and
                    row['target_version'] == VERSION and status.get('current_version') == VERSION and
                    not status.get('busy') and not status.get('recovery_required')):
                return
            row['state'] = 'running'
            await self.store.run(self.save, row, nodes)
        if row['target_version'] != VERSION:
            for node in nodes:
                if node['state'] not in DONE:
                    node.update(state='blocked', message='Hub 版本已变化；不重放旧更新，已接收操作请查看原回执')
            await self.store.run(self.save, row, nodes, 'blocked', '目标版本已被替代，未重放旧版本更新')
            return
        principal = Principal(**json.loads(row['principal']), scopes=set(), projects=[])
        for node in nodes:
            if node['state'] in DONE:
                continue
            try:
                await self.advance_node(row, node, principal)
            except DevError as exc:
                if exc.code in {'DEVICE_OFFLINE', 'DEVICE_BUSY', 'PANEL_UPDATING'}:
                    node.update(state='offline' if exc.code == 'DEVICE_OFFLINE' else 'waiting_idle', message=exc.message)
                else:
                    node.update(state='blocked', message=f'{exc.code}: {exc.message}'[:600])
            except Exception:
                node.update(message='状态暂时不可用，保留原更新编号等待恢复')
            await self.store.run(self.save, row, nodes)
        if all(node['state'] in DONE for node in nodes):
            failed = any(node['state'] in {'failed', 'blocked'} for node in nodes)
            await self.store.run(self.save, row, nodes, 'partial_failure' if failed else 'succeeded',
                                '部分 Agent 未完成，请查看逐节点原因' if failed else '已核对全部已授权 Agent')

    def inspect_node(self, node, principal):
        principal = refresh_principal(self.store, principal)
        if not principal.instance_admin:
            raise DevError('INSTANCE_ADMIN_REQUIRED', '原管理员权限已撤销，未继续自动更新', 403)
        iam.require_device(self.store, principal, node['device_id'], manage=True)
        device = self.store.one('SELECT * FROM devices WHERE id=?', (node['device_id'],))
        if not device or not device['enabled']:
            raise DevError('DEVICE_DISABLED', '节点已停用或移除，未继续更新', 409)
        info = device_info(device)
        node['version'] = info.get('version', '')
        # Recover acceptance even if Hub stopped before saving the receipt ID.
        op = self.store.one("""SELECT * FROM operations WHERE tool='agent_update'
            AND idem=? AND owner_user_id=? AND space_id=? ORDER BY created LIMIT 1""",
            (node['idempotency_key'], principal.user_id, principal.space_id))
        return principal, device, info, op, self.runtime.online(node['device_id'])

    async def advance_node(self, row, node, principal):
        principal, device, info, op, online = await self.store.run(self.inspect_node, node, principal)
        management = info.get('management') or {}
        if op:
            node['operation_id'] = op['id']
            if op['state'] in ACTIVE_OPS:
                node.update(state='updating', message='正在准备更新；沿用原操作等待结果')
                return
            if op['state'] != 'succeeded':
                node.update(state='failed', message=str(op.get('error') or 'Agent 更新失败；请查看原操作记录')[:600])
                return
            if online and info.get('version') == row['target_version'] and (device.get('last_seen') or 0) > op['created']:
                if management.get('last_error') or management.get('status') in {'error', 'rollback', 'recovery_required'}:
                    node.update(state='failed', message=str(management.get('last_error') or 'Agent 管理状态需检查')[:600])
                else:
                    node.update(state='succeeded', message='已重连并核对目标版本')
                return
            if online and (device.get('last_seen') or 0) > op['updated'] and management.get('last_error'):
                node.update(state='failed', message=str(management['last_error'])[:600])
            elif time.time() - op['updated'] > 900:
                node.update(state='failed', message='更新已交接，15 分钟内未核对目标版本；请检查原回执和节点，不自动重试切换')
            else:
                node.update(state='awaiting_restart', message='更新已交接，等待节点重连并核对目标版本')
            return
        if node['operation_id']:
            node.update(state='blocked', message='原操作记录不可用；停止自动更新，避免重复执行')
            return
        if not online:
            node.update(state='offline', message='离线待更新，重连后重新核验权限与任务状态')
            return
        current, target = version(info.get('version')), version(row['target_version'])
        if current is None or target is None:
            node.update(state='blocked', message='Agent 版本无法安全比较，需要手动检查')
            return
        if current >= target:
            node.update(state='skipped', message='已是目标版本' if current == target else 'Agent 版本更高，不自动降级')
            return
        bundle = await self.store.run(self.package.build)
        receipt = await self.runtime.dispatch_device_action('agent_update',
            {'idempotency_key': node['idempotency_key'], 'package_sha256': bundle.sha256,
             'package_bytes': len(bundle.content), 'target_version': row['target_version']},
            node['device_id'], principal)
        node.update(state='updating', operation_id=receipt.get('operation_id', ''),
                    message='已发起安全更新，等待 Agent 准备及重连核验')
