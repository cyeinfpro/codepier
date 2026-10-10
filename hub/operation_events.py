"""Private, exact-grant completion hints using the existing Events outbox.

No operation is executed here. Request payloads, results and output are never
read or copied. Mapping paths are hashed only for private authorization binding. All writes require the caller's completion/subscription
transaction; dispatch remains the existing fenced EventService transport.
"""
from __future__ import annotations
import json
import re

from hub.collaboration.common import OPERATION_EVENT, digest
from shared.util import DevError

COMPLETED = frozenset({'succeeded', 'failed', 'cancelled', 'needs_review', 'interrupted'})


class OperationEvents:
    def __init__(self, events):
        self.events, self.c, self.store = events, events.c, events.store

    def read(self, identifier, principal, filters):
        # The same live operation reader used by task_query retains tool-specific
        # access checks. Space-visible records still must belong to THIS grant.
        row = self.c.runtime.operation_row(identifier, principal, status_only=True)
        if (row.get('grant_id') != principal.grant_id
                or row.get('owner_user_id') != principal.user_id
                or row.get('space_id') != principal.space_id
                or row.get('project_id') != filters['project_id']):
            raise DevError('OPERATION_NOT_FOUND', '原操作不属于当前订阅授权范围', 404)
        project = self.c.runtime.project(filters['project_id'], principal)
        if row['device_id'] != project['device_id']:
            raise DevError('OPERATION_MAPPING_CHANGED', '原操作的项目映射已经改变', 409)
        summary = self.store.one('SELECT id,state,output_seq,args_summary,created FROM operations WHERE id=?', (identifier,))
        try:
            args = json.loads(summary['args_summary'])
            workspace = args.get('workspace_id', '')
            if not isinstance(workspace, str) or not re.fullmatch(r'(|[a-f0-9]{32})', workspace):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise DevError('OPERATION_CONTEXT_INVALID', '原操作工作目录标识无法验证', 409) from None
        return {'id': identifier, 'state': summary['state'], 'output_seq': summary['output_seq'],
                'workspace_id': workspace, 'created': summary['created'], 'tool': row['tool']}

    def binding(self, principal, filters):
        # Private digest only. Paths and labels never enter the event payload.
        project = self.c.runtime.project(filters['project_id'], principal)
        mapping = {key: project.get(key) for key in
                   ('id', 'device_id', 'root', 'alias', 'mode', 'allow_tasks', 'space_id', 'owner_user_id')}
        operations = {}
        for identifier in filters['operation_ids']:
            row = self.read(identifier, principal, filters)
            operations[identifier] = {key: row[key] for key in ('workspace_id', 'created', 'tool')}
        role = self.store.one('SELECT version,enabled FROM access_roles WHERE id=?', (principal.role_id,)) if principal.role_id else None
        profile = self.store.one('SELECT version,enabled FROM access_profiles WHERE id=?', (principal.profile_id,)) if principal.profile_id else None
        return {'mapping': digest(mapping), 'authorization_mode': principal.authorization_mode,
                'role_id': principal.role_id, 'role': role, 'profile': profile, 'operations': operations}

    def authorize(self, principal, filters, *, snapshot=None, payload=None):
        self.events.guard()
        binding = self.binding(principal, filters)
        if snapshot is not None and snapshot.get('operation_event_binding') != binding:
            raise DevError('OPERATION_SUBSCRIPTION_CHANGED', '操作订阅的授权或项目映射已改变，请重新确认订阅', 403)
        if payload is not None:
            identifier = payload.get('operation_id')
            if (identifier not in filters['operation_ids']
                    or payload.get('recipient_grant_id') != principal.grant_id
                    or payload.get('project_id') != filters['project_id']
                    or payload.get('environment_id') != filters['environment_id']):
                raise DevError('OPERATION_EVENT_OBSOLETE', '操作提示不属于当前订阅', 403)
            row = self.read(identifier, principal, filters)
            if payload.get('workspace_id') != row['workspace_id']:
                raise DevError('OPERATION_EVENT_OBSOLETE', '原操作工作目录已改变', 409)
            # A late output snapshot may advance needs_review's cursor without
            # changing its outcome. The original completion hint remains useful;
            # its read-only next_call retrieves the current original receipt.
            sequence = payload.get('output_seq')
            if not payload.get('test') and (row['state'] not in COMPLETED
                    or payload.get('state') != row['state'] or type(sequence) is not int
                    or not 0 <= sequence <= row['output_seq']):
                raise DevError('OPERATION_EVENT_OBSOLETE', '原操作已有较新状态，请读取当前回执', 409)
        return binding

    @staticmethod
    def object_id(subscription_id, payload):
        # Include the exact subscription and outcome, not wall time or generation:
        # catch-up/renewal/restart deduplicate; needs_review -> success is distinct.
        return digest([subscription_id, payload['operation_id'], payload['state'], payload['output_seq']])

    def queue(self, subscription, room, operation):
        self.store.require_transaction()
        if operation['state'] not in COMPLETED:
            return
        data = {'operation_id': operation['id'], 'state': operation['state'],
                'output_seq': operation['output_seq'], 'workspace_id': operation['workspace_id'],
                'recipient_grant_id': subscription['grant_id'],
                'next_call': {'name': 'task_query', 'arguments': {
                    'operation': 'get', 'operation_ids': [operation['id']],
                    'include_output': True, 'include_result': True, 'output_limit': 8000}}}
        object_id = self.object_id(subscription['id'], data)
        self.c.emit(room, OPERATION_EVENT, object_id, 1, data, grant_id=subscription['grant_id'])
        event = self.store.one('SELECT id,seq FROM mcp_event_outbox WHERE name=? AND object_id=? AND target_grant_id=?',
                               (OPERATION_EVENT, object_id, subscription['grant_id']))
        # Materialize the exact delivery within this same transaction. This also
        # covers completion during callback verification and an older outbox seq.
        # Existing accepted/abandoned receipts are never silently reset.
        delivery = self.store.execute('''INSERT OR IGNORE INTO mcp_event_deliveries
            (id,subscription_id,event_id,event_seq,created) VALUES (?,?,?,?,?)''',
            (digest([subscription['id'], event['id']]), subscription['id'], event['id'],
             event['seq'], self.c.clock()))
        loops = getattr(self.c, 'loops', None)
        if delivery.rowcount and loops is not None:
            self.store.after_commit(loops.wake_events)

    def catch_up(self, subscription, room, principal, filters):
        self.store.require_transaction()
        # This is a bounded current-state reconciliation of the explicitly named
        # IDs, never a replay of operations or an unbounded history scan.
        for identifier in filters['operation_ids']:
            self.queue(subscription, room, self.read(identifier, principal, filters))

    def completed(self, identifier):
        if not self.c.config.enabled or not self.c.config.events_enabled:
            return
        self.store.require_transaction()
        operation = self.store.one('SELECT id,state,grant_id,project_id,space_id,owner_user_id FROM operations WHERE id=?',
                                   (identifier,))
        if not operation or operation['state'] not in COMPLETED or not operation['grant_id']:
            return
        # Existing room/subscription limits bound fan-out. SQL selects only the
        # original ID and exact owner/grant; unrelated subscriptions incur no
        # operation reads and no broad notification is ever created.
        subscriptions = self.store.all('''SELECT s.* FROM mcp_event_subscriptions s
            JOIN collaboration_rooms r ON r.id=s.room_id
            WHERE s.name=? AND s.grant_id=? AND s.state='active' AND s.expires_at>?
              AND r.state='active' AND r.project_id=? AND r.space_id=? AND r.owner_user_id=?
              AND EXISTS (SELECT 1 FROM json_each(s.arguments, '$.operation_ids') WHERE value=?)''',
            (OPERATION_EVENT, operation['grant_id'], self.c.clock(), operation['project_id'],
             operation['space_id'], operation['owner_user_id'], identifier))
        for subscription in subscriptions:
            room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (subscription['room_id'],))
            try:
                filters = self.events.authorize(subscription, room)
                principal = self.c.grant_reader(room, subscription['grant_id'],
                                                snapshot=json.loads(subscription['principal']))
                row = self.read(identifier, principal, filters)
            except DevError:
                # Revocation/pause/changed mapping is a normal no-delivery result.
                # Storage/contract failures must escape and roll back completion.
                continue
            self.queue(subscription, room, row)
