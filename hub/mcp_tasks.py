"""MCP 2026 Tasks are authorized views over native exec receipts, not runners."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
import time

from hub import iam
from hub.principal import refresh_principal
from shared.mcp_protocol import PREFIX, ProtocolError
from shared.mcp_presentation import error_view
from shared.util import DevError

EXTENSION = 'io.modelcontextprotocol/tasks'
METHODS = frozenset({'tasks/get', 'tasks/update', 'tasks/cancel'})
TERMINAL = frozenset({'succeeded', 'failed', 'cancelled', 'needs_review', 'interrupted'})


@dataclass(frozen=True)
class CreatedTask:
    """Only the router may turn this trusted value into resultType=task."""
    body: dict


def supported(metadata):
    return EXTENSION in metadata.get(PREFIX + 'clientCapabilities', {}).get('extensions', {})


def arguments(method, params):
    allowed = {'_meta', 'taskId'} | ({'inputResponses'} if method == 'tasks/update' else set())
    if set(params) - allowed or not isinstance(params.get('taskId'), str) or not re.fullmatch(r'[a-f0-9]{32}', params['taskId']):
        raise ProtocolError(-32602, 'Expected a taskId and supported task parameters')
    if method == 'tasks/update' and not isinstance(params.get('inputResponses'), dict):
        raise ProtocolError(-32602, 'inputResponses must be an object')
    return params['taskId']


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def render_exec_v1(snapshot):
    from hub.core_tools import result as core_result
    from hub.runtime import Runtime
    try:
        saved = json.loads(snapshot['result']) if snapshot.get('result') else None
    except (ValueError, TypeError) as exc:
        raise ProtocolError(-32603, 'Stored task result is not valid JSON') from exc
    if saved is not None and not isinstance(saved, dict):
        raise ProtocolError(-32603, 'Stored task result has an invalid protocol shape')
    identifier = snapshot['operation_id']
    if saved is None:
        saved = {'ok': False, 'error': {'code': 'OPERATION_UNVERIFIED',
            'message': snapshot.get('error') or 'Original operation ended without a verified result.'}}
    # TaskService._binding performs the same operation_row authorization as
    # Runtime.authorized_result, plus exact task ownership/current scope. It
    # remains inside the same IAM/SQLite decision with no intervening await.
    # Reuse the actual synchronous unwrap/redaction implementation, on a fresh
    # decoded copy only; never mutate the immutable stored terminal snapshot.
    try:
        value = Runtime.unwrap(identifier, saved)
    except DevError as exc:
        value = {'error': error_view({'code': exc.code, 'message': exc.message, **exc.details})}
        return {'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}],
                'structuredContent': value, 'isError': True}
    rendered = core_result('exec', {}, value)
    rendered['isError'] = rendered['isError'] or snapshot['state'] != 'succeeded'
    return rendered

class TaskService:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store

    def _binding(self, identifier, principal):
        principal = refresh_principal(self.store, principal)
        row = self.store.one('SELECT * FROM mcp_tasks WHERE task_id=?', (identifier,))
        if not row or (row['space_id'], row['owner_user_id'], row['grant_id']) != (
                principal.space_id, principal.user_id, principal.grant_id):
            raise DevError('TASK_NOT_FOUND', 'Task not found for this connection', 404)
        if not {'read', 'execute'} <= principal.scopes:
            raise DevError('INSUFFICIENT_SCOPE', 'Task access requires current read and execute scope', 403, required_scope='execute', operation_id=identifier)
        # Ownership alone is insufficient after any dynamic permission change.
        operation = self.runtime.operation_row(identifier, principal)
        if (operation['space_id'], operation['owner_user_id'], operation['grant_id'], operation['tool']) != (
                row['space_id'], row['owner_user_id'], row['grant_id'], 'exec'):
            raise DevError('TASK_NOT_FOUND', 'Task binding no longer matches its operation', 404)
        return row, operation

    def _freeze(self, row, operation):
        if row['terminal_operation'] is None and operation['state'] in TERMINAL:
            snapshot = {key: operation.get(key) for key in ('state', 'result', 'error', 'updated')}
            snapshot['operation_id'] = operation['id']
            self.store.execute('UPDATE mcp_tasks SET terminal_operation=? WHERE task_id=? AND terminal_operation IS NULL',
                (json.dumps(snapshot, ensure_ascii=False), row['task_id']))
            row = self.store.one('SELECT * FROM mcp_tasks WHERE task_id=?', (row['task_id'],))
        return row

    @iam.read_decision
    def create(self, value, args, principal):
        if not principal.grant_id or value.get('pending') is not True:
            return None
        identifier = value.get('operation_id')
        if not isinstance(identifier, str):
            return None
        with self.store.transaction():
            principal = refresh_principal(self.store, principal)
            operation = self.runtime.operation_row(identifier, principal)
            if (operation['tool'], operation.get('grant_id'), operation.get('owner_user_id'), operation['space_id'],
                    operation.get('idem')) != ('exec', principal.grant_id, principal.user_id, principal.space_id,
                                               args.get('idempotency_key')):
                raise DevError('TASK_BINDING_INVALID', 'Pending execution does not match its original request', 409)
            self.store.execute("""INSERT OR IGNORE INTO mcp_tasks(task_id,space_id,owner_user_id,grant_id,created)
                VALUES(?,?,?,?,?)""", (identifier, principal.space_id, principal.user_id, principal.grant_id, time.time()))
            row, operation = self._binding(identifier, principal)
            self._freeze(row, operation)
            body = self.get(identifier, principal)
            return CreatedTask(body)

    @iam.read_decision
    def get(self, identifier, principal):
        with self.store.transaction():
            row, operation = self._binding(identifier, principal)
            row = self._freeze(row, operation)
            snapshot = json.loads(row['terminal_operation']) if row['terminal_operation'] else None
            updated = snapshot['updated'] if snapshot else operation['updated']
            body = {'taskId': identifier, 'status': 'working', 'createdAt': iso(row['created']),
                    'lastUpdatedAt': iso(max(row['created'], updated)), 'ttlMs': None, 'pollIntervalMs': 2000}
            if snapshot:
                if snapshot['state'] == 'cancelled':
                    body['status'] = 'cancelled'
                else:
                    try:
                        body['result'] = render_exec_v1(snapshot)
                        body['status'] = 'completed'
                    except ProtocolError as exc:
                        body['status'] = 'failed'
                        body['error'] = {'code': exc.code, 'message': exc.message}
                        if exc.data is not None: body['error']['data'] = exc.data
            elif operation.get('cancel_requested'):
                body['statusMessage'] = 'Cancellation requested; execution has not confirmed a terminal state.'
            else:
                body['statusMessage'] = 'Original execution is ' + operation['state'] + '.'
            return body

    async def call(self, method, identifier, principal):
        before = await self.store.run(self.get, identifier, principal)
        if method == 'tasks/cancel' and before['status'] == 'working':
            # Reuse existing execute checks and honest cooperative cancellation.
            await self.runtime.cancel(identifier, principal)
        after = await self.store.run(self.get, identifier, principal)
        # No input requests are ever issued in this exec-only version. Unknown
        # inputResponses are ignored as required; they grant no new authority.
        return after if method == 'tasks/get' else {}
