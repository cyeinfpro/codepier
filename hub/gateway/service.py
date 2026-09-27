"""Gateway routing and receipts. External calls never enter Agent's dispatch queue."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
import time
import uuid

from hub.gateway.validation import Validator

from hub.gateway import catalog, policy
from hub.gateway.remote import RemotePool
from shared.util import DevError, valid_json_value


def result_value(raw, tool):
    if not isinstance(raw, dict) or not valid_json_value(raw) or raw.get('resultType', 'complete') != 'complete':
        raise DevError('GATEWAY_UNSUPPORTED_RESULT', '后端未返回完整工具结果；未自动回应批准或输入请求', 502)
    content = raw.get('content')
    if not isinstance(content, list) or len(content) > 128 or type(raw.get('isError', False)) is not bool:
        raise DevError('GATEWAY_RESULT_INVALID', '后端工具结果结构无效', 502)
    safe = []
    for item in content:
        if not isinstance(item, dict):
            raise DevError('GATEWAY_RESULT_INVALID', '后端内容块无效', 502)
        if item.get('type') == 'text' and isinstance(item.get('text'), str):
            safe.append({'type': 'text', 'text': item['text']})
        elif item.get('type') in ('image', 'audio'):
            mime = item.get('mimeType'); data = item.get('data')
            allowed = ('image/png', 'image/jpeg', 'image/webp') if item['type'] == 'image' else ('audio/wav', 'audio/mpeg', 'audio/mp3', 'audio/ogg')
            try:
                if mime not in allowed or not isinstance(data, str) or len(data) > 1048576:
                    raise ValueError()
                base64.b64decode(data, validate=True)
            except (ValueError, TypeError) as exc:
                raise DevError('GATEWAY_RESULT_INVALID', '后端媒体格式或大小不受支持', 502) from exc
            safe.append({'type': item['type'], 'mimeType': mime, 'data': data})
        else:
            # Resource URIs/Apps need authenticated namespaced routing, which is
            # deliberately not advertised by this tools-only backend adapter.
            raise DevError('GATEWAY_UNSUPPORTED_CONTENT', '后端返回需要资源代理的内容；此版本未启用该能力，未重发操作', 502)
    result = {'content': safe, 'isError': raw.get('isError', False)}
    if 'structuredContent' in raw:
        if not isinstance(raw['structuredContent'], dict):
            raise DevError('GATEWAY_RESULT_INVALID', '后端 structuredContent 必须是 object', 502)
        result['structuredContent'] = raw['structuredContent']
    if len(catalog.encoded(result).encode()) > 1048576:
        raise DevError('GATEWAY_RESULT_LIMIT', '工具结果超过存储限制；操作可能已执行', 502)
    # Backend _meta, identity annotations, auth challenges and cache promises are
    # not part of the trusted Hub envelope and are never blindly forwarded.
    return result


class Gateway:
    def __init__(self, store, *, enabled=None, pool=None):
        self.store = store
        self.enabled = os.getenv('CODEPIER_MCP_GATEWAY', '0') == '1' if enabled is None else enabled
        self.pool = pool or RemotePool()
        self.inflight = 0
        self.discovery_inflight = 0
        self.validator = Validator()
        self.secret = hmac.new((store.directory / 'master.key').read_bytes(), b'codepier-gateway-v1', hashlib.sha256).digest()
        store.execute("UPDATE gateway_calls SET state='unknown',error_code='GATEWAY_HUB_RESTARTED',updated=? WHERE state='running'", (time.time(),))

    def require_enabled(self):
        if not self.enabled:
            raise DevError('GATEWAY_DISABLED', '实例尚未启用 CODEPIER_MCP_GATEWAY', 403)

    async def close(self):
        await self.pool.close()

    def tools(self, principal):
        if not self.enabled:
            return []
        result = [catalog.definition(binding['alias'], tool) for binding, tool in policy.visible_tools(self.store, principal)]
        try:
            policy.grant_context(self.store, principal)
        except DevError:
            return result
        return result + [catalog.status_definition()]

    def list_tools(self, principal, native, cursor=None):
        return catalog.page(native + self.tools(principal), cursor,
                            [principal.space_id, principal.user_id, principal.grant_id, principal.profile_id], self.secret)

    def context(self, principal):
        groups = {}
        if self.enabled:
            for binding, tool in policy.visible_tools(self.store, principal):
                item = groups.setdefault(binding['id'], {'binding_id': binding['id'], 'alias': binding['alias'], 'tools': []})
                item['tools'].append(catalog.public_name(binding['alias'], tool['name']))
        return {'enabled': self.enabled, 'bindings': list(groups.values()),
                'authorization': 'role + explicit gateway consent', 'resource_isolation': 'backend account and reviewed tool; no generic project sandbox'}

    def resolve(self, principal, name):
        self.require_enabled()
        policy.grant_context(self.store, principal)
        for binding, tool in policy.visible_tools(self.store, principal):
            if catalog.public_name(binding['alias'], tool['name']) == name:
                return policy.require_tool(self.store, principal, binding['id'], tool['name'])
        raise DevError('GATEWAY_TOOL_NOT_FOUND', '工具不存在或不在此凭据的授权范围', 404)

    def _signature(self, plan):
        _, binding, account, connector, tool = plan
        return (binding['id'], binding['version'], account['id'], account['version'],
                connector['id'], connector['version'], catalog.fingerprint(tool))

    async def discover(self, principal, account_id, reauthenticate):
        from hub.gateway.registry import managed_account
        self.require_enabled()
        with self.store.lock:
            account = managed_account(self.store, principal, account_id)
            connector = self.store.one('SELECT * FROM gateway_connectors WHERE id=? AND space_id=?', (account['connector_id'], principal.space_id))
            if not connector or not connector['enabled'] or not account['enabled']:
                raise DevError('GATEWAY_DISABLED', '帐户或连接已暂停', 409)
            secret = self.store.decrypt(account['secret'])
        key = ('discovery', principal.space_id, principal.user_id, account['id'], account['version'], connector['version'])
        session = None
        if self.discovery_inflight >= 4:
            raise DevError('GATEWAY_BUSY', '工具发现容量已满', 429)
        self.discovery_inflight += 1
        try:
            async with asyncio.timeout(45):
                session = await self.pool.get(key, connector, secret)
                tools = await session.tools()
        except TimeoutError as exc:
            raise DevError('GATEWAY_TIMEOUT', '发现工具超时；未变更已发布的目录', 504) from exc
        finally:
            self.discovery_inflight -= 1
            if session:
                await self.pool.release(key)
        with self.store.transaction():
            fresh = reauthenticate()
            if (fresh.user_id, fresh.space_id) != (principal.user_id, principal.space_id):
                raise DevError('GATEWAY_IDENTITY_CHANGED', '身份已变化，请重新开始', 409)
            current = managed_account(self.store, fresh, account_id)
            latest = self.store.one('SELECT * FROM gateway_connectors WHERE id=?', (connector['id'],))
            if current['version'] != account['version'] or not latest or latest['version'] != connector['version'] or not latest['enabled']:
                raise DevError('GATEWAY_CONFIG_CHANGED', '发现期间帐户或连接已修改；未覆盖目录', 409)
            digest = catalog.fingerprint(tools)
            self.store.db.execute('UPDATE gateway_accounts SET catalog=?,catalog_hash=? WHERE id=?', (catalog.encoded(tools), digest, account_id))
            self.store.audit(fresh.actor, 'gateway.discovered', account_id, detail={'tool_count': len(tools)}, commit=False)
        return {'tools': tools, 'catalog_hash': digest, 'published': False}

    def _with_receipt(self, result, identifier):
        # Metadata is often hidden from models; put the receipt in a separate
        # text block without changing the backend's structured output schema.
        content = result['content'] + [{'type': 'text', 'text': 'CodePier gateway call_id: ' + identifier + '. Recover with gateway_call_get; never replay an uncertain operation.'}]
        return {**result, 'content': content, '_meta': {**result.get('_meta', {}), 'codepier/callId': identifier,
                                   'codepier/recovery': {'name': catalog.STATUS_TOOL, 'arguments': {'call_id': identifier}}}}

    def receipt(self, principal, identifier):
        self.require_enabled()
        row = self.store.one('SELECT * FROM gateway_calls WHERE id=? AND space_id=? AND user_id=? AND grant_id=?',
                             (identifier, principal.space_id, principal.user_id, principal.grant_id))
        if not row:
            raise DevError('GATEWAY_CALL_NOT_FOUND', '找不到此凭据的调用记录', 404)
        plan = policy.require_tool(self.store, principal, row['binding_id'], row['tool'])
        if catalog.fingerprint(plan[-1]) != row['tool_hash']:
            raise DevError('GATEWAY_CATALOG_CHANGED', '工具定义已变化；旧结果未自动跨版本公开', 409)
        value = {k: row[k] for k in ('id', 'state', 'error_code', 'created', 'updated')}
        value['result'] = json.loads(self.store.decrypt(row['result'])) if row['result'] else None
        value['replayed'] = False
        return value

    async def call(self, principal, name, arguments, reauthenticate, request_key=None):
        self.require_enabled()
        if name == catalog.STATUS_TOOL:
            catalog.validate_arguments(catalog.status_definition(), arguments)
            value = self.receipt(reauthenticate(), arguments['call_id'])
            return {'content': [{'type': 'text', 'text': catalog.encoded(value)}], 'structuredContent': value}
        with self.store.lock:
            plan = self.resolve(principal, name)
            principal, binding, account, connector, tool = plan
            signature = self._signature(plan)
            catalog.validate_arguments(tool, arguments, schema_check=False)
            secret = self.store.decrypt(account['secret'])
        await self.validator.validate(tool['inputSchema'], arguments)
        with self.store.lock:
            fresh_plan = policy.require_tool(self.store, reauthenticate(), binding['id'], tool['name'])
            if self._signature(fresh_plan) != signature:
                raise DevError('GATEWAY_CONFIG_CHANGED', '验证期间配置改变；未发送操作', 409)
        if request_key is not None and (not isinstance(request_key, str) or not 8 <= len(request_key) <= 128):
            raise DevError('GATEWAY_IDEMPOTENCY_INVALID', 'codepier/idempotencyKey 必须是 8–128 字符')
        request_hash = hmac.new(self.secret, request_key.encode(), hashlib.sha256).hexdigest() if request_key else None
        fingerprint = hmac.new(self.secret, catalog.encoded([binding['id'], tool, arguments]).encode(), hashlib.sha256).hexdigest()
        with self.store.transaction():
            if request_hash:
                old = self.store.one('SELECT * FROM gateway_calls WHERE space_id=? AND grant_id=? AND request_key=?',
                                     (principal.space_id, principal.grant_id, request_hash))
                if old:
                    if old['fingerprint'] != fingerprint:
                        raise DevError('GATEWAY_IDEMPOTENCY_CONFLICT', '同一幂等键已用于另一请求；未重发', 409)
                    receipt = self.receipt(principal, old['id'])
                    if receipt['result'] is not None:
                        return self._with_receipt(receipt['result'], old['id'])
                    raise DevError('GATEWAY_ORIGINAL_CALL', '原调用仍在执行或结果未知；请读取原 call_id，未重发', 409, call_id=old['id'])
            # Bounded retained receipts; expiry never triggers replay. Callers
            # must not treat an expired idempotency key as a global exactly-once guarantee.
            self.store.db.execute("DELETE FROM gateway_calls WHERE space_id=? AND state!='running' AND updated<?", (principal.space_id, time.time() - 7 * 86400))
            count = self.store.one('SELECT count(*) AS n FROM gateway_calls WHERE space_id=?', (principal.space_id,))['n']
            if count >= 1000 or self.inflight >= 32:
                raise DevError('GATEWAY_BUSY', '调用或回执配额已满；未发送操作', 429)
            identifier = 'gwc_' + uuid.uuid4().hex
            now = time.time()
            self.store.db.execute('''INSERT INTO gateway_calls(id,space_id,user_id,grant_id,binding_id,tool,tool_hash,state,created,updated,request_key,fingerprint)
                VALUES(?,?,?,?,?,?,?,'running',?,?,?,?)''',
                (identifier, principal.space_id, principal.user_id, principal.grant_id, binding['id'], tool['name'], catalog.fingerprint(tool), now, now, request_hash, fingerprint))
        dispatched = False
        session = None
        key = ('call', principal.space_id, principal.user_id, principal.grant_id, binding['id'], account['id'], account['version'], connector['version'])

        def current():
            fresh = reauthenticate()
            if (fresh.space_id, fresh.user_id, fresh.grant_id) != (principal.space_id, principal.user_id, principal.grant_id):
                raise DevError('GATEWAY_IDENTITY_CHANGED', '请求身份已改变', 403)
            with self.store.lock:
                latest = policy.require_tool(self.store, fresh, binding['id'], tool['name'])
                if self._signature(latest) != signature:
                    raise DevError('GATEWAY_CONFIG_CHANGED', '连接/帐户/工具在等待期间改变；请核对原结果', 409)

        def before_send():
            nonlocal dispatched
            current()
            dispatched = True

        self.inflight += 1
        try:
            async with asyncio.timeout(45):
                session = await self.pool.get(key, connector, secret)
                raw = await session.call(tool['name'], arguments, before_send)
                value = result_value(raw, tool)
                if not value.get('isError') and 'outputSchema' in tool:
                    await self.validator.validate(tool['outputSchema'], value.get('structuredContent'), 'GATEWAY_RESULT_INVALID')
            current()
            state = 'tool_error' if value.get('isError') else 'completed'
            self.store.execute('UPDATE gateway_calls SET state=?,result=?,updated=? WHERE id=?',
                               (state, self.store.encrypt(catalog.encoded(value)), time.time(), identifier))
            return self._with_receipt(value, identifier)
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError):
                code = 'GATEWAY_REQUEST_CANCELLED'
            elif isinstance(exc, TimeoutError):
                code = 'GATEWAY_TIMEOUT'
            elif isinstance(exc, DevError):
                code = exc.code
            else:
                code = 'GATEWAY_BACKEND_FAILURE'
            self.store.execute('UPDATE gateway_calls SET state=?,error_code=?,updated=? WHERE id=?',
                               ('unknown' if dispatched else 'rejected', code, time.time(), identifier))
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            message = exc.message if isinstance(exc, DevError) else '调用未得到可验证的结果；未自动重发'
            raise DevError(code, message, 502, call_id=identifier,
                           outcome='unknown' if dispatched else 'not_sent') from exc
        finally:
            self.inflight -= 1
            if session:
                await self.pool.release(key)
