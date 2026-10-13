"""Recoverable conversation removal with explicit, stale-safe owner confirmation.

Snapshots preserve history/membership, never credentials or executable authority.
Shared project partitions and account credentials are not owned by a conversation.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import Field
from hub.collaboration.common import MESSAGE_EVENT, canonical, digest, validate
from hub.principal import refresh_principal
from shared import collaboration_contracts as contracts
from shared.util import DevError


class ArchivePreview(contracts.Model):
    conversation_id: contracts.Identifier


class Archive(ArchivePreview):
    expected_version: int = Field(ge=1)
    impact_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirm_archive: Literal[True]
    idempotency_key: contracts.Key


class Restore(ArchivePreview):
    expected_version: int = Field(ge=1)
    confirm_restore: Literal[True]
    idempotency_key: contracts.Key


WARNINGS = [
    '删除后停止此房间的新任务、连接、订阅和外部消息投递，并撤销此房间的执行授权。',
    '已发出的消息无法收回；已开始的外部操作会请求停止，但不能保证进程或文件操作已经停止。',
    '可恢复房间历史和项目关联；执行权限、旧连接、订阅、任务和监控不会自动恢复。继续执行建议新建房间并明确接入助手。',
]
RETAINED = ['源项目及源码不删除；其他房间和共享账号凭据不改变。',
            '消息、结果和必要审计记录保留供恢复及追溯。']


def migrate(db):
    columns = {r[1] for r in db.execute('PRAGMA table_info(conversation_rooms)')}
    for name, declaration in [('archived_at', 'REAL'), ('archived_by', "TEXT NOT NULL DEFAULT ''")]:
        if name not in columns:
            db.execute(f'ALTER TABLE conversation_rooms ADD COLUMN {name} {declaration}')
    db.execute("""CREATE TABLE IF NOT EXISTS conversation_archives(
        conversation_id TEXT NOT NULL,version INTEGER NOT NULL,snapshot TEXT NOT NULL,
        actor TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(conversation_id,version))""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS conversation_archive_immutable
        BEFORE UPDATE ON conversation_archives
        BEGIN SELECT RAISE(ABORT,'conversation archive evidence is immutable'); END""")


class ConversationArchive:
    def __init__(self, conversations):
        self.conversations = conversations
        self.c, self.store = conversations.c, conversations.store

    def owner(self, principal, identifier):
        self.c.guard()
        principal = refresh_principal(self.store, principal)
        self.c.owner(principal)
        return principal, self.conversations.object(principal, identifier, include_archived=True)

    @staticmethod
    def ids(rows):
        return [r['id'] for r in rows]

    def inventory(self, conversation):
        cid = conversation['id']
        projects = self.store.all("""SELECT r.id,r.project_id,r.environment_id,r.state,r.version,p.created,
            COALESCE(pr.alias,r.project_id) AS project_label,
            NOT EXISTS(SELECT 1 FROM conversation_projects other
                JOIN conversation_rooms c ON c.id=other.conversation_id
                WHERE other.room_id=r.id AND other.conversation_id!=? AND c.archived_at IS NULL) AS exclusive
            FROM conversation_projects p JOIN collaboration_rooms r ON r.id=p.room_id
            LEFT JOIN projects pr ON pr.id=r.project_id
            WHERE p.conversation_id=? ORDER BY r.id""", (cid, cid))
        exclusive = [r['id'] for r in projects if r['exclusive']]
        slots = self.store.all("""SELECT s.id,s.version,s.state FROM collaboration_join_slots s
            WHERE s.id IN (SELECT id FROM collaboration_dots WHERE conversation_id=?)
            OR s.id IN (SELECT slot_id FROM delegation_policies WHERE conversation_id=?)
            ORDER BY s.id""", (cid, cid))
        slots += [s for s in self.store.all('SELECT id,room_id,version,state FROM collaboration_join_slots ORDER BY id')
                  if s['room_id'] in exclusive and s['id'] not in self.ids(slots)]
        # A legacy slot can serve more than one conversation. Revoke only this
        # relationship's policy/routes; never revoke the shared slot itself.
        shared_slot_ids = {r['slot_id'] for r in self.store.all(
            "SELECT slot_id FROM delegation_policies WHERE conversation_id!=? AND state='active'", (cid,))}
        slots = [s for s in slots if s['id'] not in shared_slot_ids]
        slot_ids = set(self.ids(slots))
        policy_ids = {r['id'] for r in self.store.all('SELECT id FROM delegation_policies WHERE conversation_id=?', (cid,))}
        goals = self.store.all('SELECT id,state,version FROM coordination_goals WHERE conversation_id=? ORDER BY id', (cid,))
        goal_ids = self.ids(goals)
        subscriptions = []
        routes = self.store.all('SELECT subscription_id,slot_id FROM collaboration_join_routes')
        route_ids = {r['subscription_id'] for r in routes if r['slot_id'] in slot_ids}
        for sub in self.store.all('SELECT id,room_id,version,state,name,arguments FROM mcp_event_subscriptions ORDER BY id'):
            filters = json.loads(sub['arguments'])
            if (sub['room_id'] in exclusive or filters.get('conversation_id') == cid or sub['id'] in route_ids
                    or filters.get('goal_id') in goal_ids or filters.get('policy_id') in policy_ids
                    or sub['name'] == MESSAGE_EVENT and sub['room_id'] == cid and not filters.get('conversation_id')):
                subscriptions.append({k: sub[k] for k in ('id', 'version', 'state')})
        jobs = self.store.all("""SELECT j.id,j.room_id,j.version,j.state FROM collaboration_jobs j
            LEFT JOIN collaboration_goals g ON g.id=j.goal_id
            LEFT JOIN collaboration_messages m
                ON m.id=COALESCE(json_extract(j.context,'$.origin_message_id'),g.source_message_id)
            WHERE m.conversation_id=? ORDER BY j.id""", (cid,))
        jobs += [j for j in self.store.all('SELECT id,room_id,version,state FROM collaboration_jobs ORDER BY id')
                 if j['room_id'] in exclusive and j['id'] not in self.ids(jobs)]
        bindings = {channel: self.store.all('SELECT id,version,state FROM ' + channel +
                    '_bindings WHERE conversation_id=? ORDER BY id', (cid,)) for channel in ('weixin', 'telegram')}
        scopes = self.store.all('SELECT id,version,state FROM conversation_project_scopes WHERE conversation_id=? ORDER BY id', (cid,))
        policies = self.store.all('SELECT id,version,state FROM delegation_policies WHERE conversation_id=? ORDER BY id', (cid,))
        writers = self.store.all('SELECT room_id,grant_id,version,enabled FROM conversation_writers WHERE conversation_id=? ORDER BY room_id,grant_id', (cid,))
        legacy_goals = self.store.all("""SELECT g.id,g.state,g.version FROM collaboration_goals g
            JOIN collaboration_messages m ON m.id=g.source_message_id WHERE m.conversation_id=? ORDER BY g.id""", (cid,))
        proposals = self.store.all("""SELECT id,state,version FROM collaboration_messages
            WHERE conversation_id=? AND state='awaiting_approval' ORDER BY id""", (cid,))
        work = self.store.all("""SELECT w.id,w.state,w.version FROM coordination_work w
            JOIN coordination_goals g ON g.id=w.goal_id WHERE g.conversation_id=? ORDER BY w.id""", (cid,))
        operations = self.store.all("""SELECT o.id,o.state,o.attempts,o.tool FROM coordination_operations c
            JOIN operations o ON o.id=c.operation_id JOIN coordination_goals g ON g.id=c.goal_id
            WHERE g.conversation_id=? AND o.state IN ('queued','running','reconnecting','cancelling','unknown')
            ORDER BY o.id""", (cid,))
        monitors = [r for r in self.store.all('SELECT id,room_id,state,version FROM monitor_plan_bindings ORDER BY id')
                    if r['room_id'] in exclusive]
        agents = [r for r in self.store.all('SELECT id,room_id,enabled,version FROM collaboration_agents ORDER BY id')
                  if r['room_id'] in exclusive]
        messages = self.store.one('SELECT COUNT(*) AS n FROM collaboration_messages WHERE conversation_id=?', (cid,))['n']
        working = self.conversations.working_scopes.current(cid)
        return dict(projects=projects, slots=slots, subscriptions=subscriptions, jobs=jobs, goals=goals,
                    bindings=bindings, scopes=scopes, policies=policies, writers=writers,
                    work=work, operations=operations, messages=messages, monitors=monitors, agents=agents,
                    legacy_goals=legacy_goals, proposals=proposals,
                    working_scope=self.conversations.working_scopes.reference(working) if working else None)

    def dependent_ceilings(self, cid):
        return self.store.all("""SELECT r.grant_id,COALESCE(g.label,r.grant_id) AS label,r.version
            FROM room_scope_manifests r LEFT JOIN grants g ON g.id=r.grant_id
            WHERE r.conversation_id=? AND r.state='active' ORDER BY r.grant_id""", (cid,))

    def preview_result(self, conversation, inventory):
        def active(rows):
            return sum(r['state'] not in {'revoked', 'cancelled', 'succeeded', 'failed', 'expired',
                                         'dead_letter', 'verified', 'completed', 'unsubscribed'} for r in rows)
        dependencies = self.dependent_ceilings(conversation['id'])
        return {'ready': not bool(dependencies), 'dependent_connections': dependencies,
                **({'reason_code': 'CONVERSATION_ARCHIVE_CEILING_DEPENDENCY',
                'message': '此房间仍被整条连接的权限边界依赖。请在消息连接的高级设置中打开“整条连接限制”，按下列连接核对后选择“停用整条凭据访问”；此操作影响所有使用同一凭据的会话。普通凭据撤销不能替代此限制的停用。这里不会自动处理共享连接。'} if dependencies else {}),
                'conversation': {k: conversation[k] for k in ('id', 'title', 'version', 'archived_at')},
                'impact': {'projects': len(inventory['projects']),
                    'project_labels': [r['project_label'] for r in inventory['projects']],
                    'exclusive_projects': sum(r['exclusive'] for r in inventory['projects']),
                    'shared_projects': sum(not r['exclusive'] for r in inventory['projects']),
                    'connections': active(inventory['slots']) + sum(active(rows) for rows in inventory['bindings'].values()),
                    'subscriptions': active(inventory['subscriptions']),
                    'active_tasks': active(inventory['work']) + active(inventory['jobs']),
                    'project_authorizations': active(inventory['scopes']),
                    'messages': inventory['messages'], 'in_flight_operations': len(inventory['operations']),
                    'monitoring_plans': active(inventory['monitors']),
                    'legacy_agents': sum(bool(r['enabled']) for r in inventory['agents']),
                    'pending_proposals': len(inventory['proposals'])},
                'impact_digest': digest([conversation['id'], conversation['version'], inventory]),
                'reversible': True, 'warnings': list(WARNINGS), 'retained': list(RETAINED)}

    def preview(self, raw, principal):
        args = validate(ArchivePreview, raw)
        with self.store.transaction(immediate=False):
            _, conversation = self.owner(principal, args['conversation_id'])
            if conversation['archived_at'] is not None:
                raise DevError('CONVERSATION_ARCHIVED', '此房间已删除，可从已删除列表恢复', 409)
            return self.preview_result(conversation, self.inventory(conversation))

    def archived_view(self, principal, conversation):
        row = self.store.one('SELECT snapshot FROM conversation_archives WHERE conversation_id=? ORDER BY version DESC LIMIT 1',
                             (conversation['id'],))
        snapshot = json.loads(row['snapshot']) if row else {'projects': []}
        projects = []
        for item in snapshot['projects']:
            try:
                project = self.c.runtime.project(item['project_id'], principal)
            except DevError:
                continue
            projects.append({'room_id': item['id'], 'project_id': item['project_id'],
                'project_label': project.get('alias') or item['project_id'], 'environment_id': item['environment_id'],
                'state': 'archived'})
        return {**conversation, 'kind': 'conversation', 'title_is_generated': self.conversations.title_is_generated(conversation), 'projects': projects,
                'project_authorizations': [], 'can_restore': True, 'requires_reauthorization': True}

    def receipt(self, principal, action, args, perform):
        identifier = digest([principal.space_id, principal.user_id, action, args['idempotency_key']])
        fingerprint = digest(args)
        saved = self.store.one('SELECT * FROM conversation_idempotency WHERE id=?', (identifier,))
        if saved:
            if saved['digest'] != fingerprint:
                raise DevError('IDEMPOTENCY_CONFLICT', '同一请求键不能用于不同内容', 409)
            result = json.loads(saved['response'])
            row = self.conversations.object(principal, args['conversation_id'], include_archived=True)
            # A retry never re-applies the historical mutation or re-enables authority.
            result.update({key: row['archived_at'] is not None if key == 'archived' else row['archived_at'] is None
                           for key in ('archived', 'restored') if key in result})
            return {**result, 'conversation': self.archived_view(principal, row) if row['archived_at'] is not None
                    else self.conversations.view(principal, row), 'replayed': True}
        result = perform()
        receipt = {**result, 'conversation': {'id': args['conversation_id']}}
        self.store.execute('INSERT INTO conversation_idempotency VALUES (?,?,?,?)',
                           (identifier, fingerprint, canonical(receipt), self.c.clock()))
        return result

    def archive(self, raw, principal):
        args = validate(Archive, raw)
        with self.store.transaction():
            principal, conversation = self.owner(principal, args['conversation_id'])
            def save():
                if conversation['version'] != args['expected_version']:
                    raise DevError('STALE_VERSION', '房间已改变，请重新核对删除影响', 409)
                if conversation['archived_at'] is not None:
                    raise DevError('CONVERSATION_ARCHIVED', '此房间已经删除', 409)
                if self.dependent_ceilings(conversation['id']):
                    raise DevError('CONVERSATION_ARCHIVE_CEILING_DEPENDENCY',
                        '此房间仍被整条连接的权限边界依赖，请先在连接管理中处理原绑定。', 409)
                inventory = self.inventory(conversation)
                if self.preview_result(conversation, inventory)['impact_digest'] != args['impact_digest']:
                    raise DevError('CONVERSATION_ARCHIVE_PREVIEW_CHANGED', '房间关联或活跃工作已改变，请重新核对删除影响', 409)
                now, cid = self.c.clock(), conversation['id']
                self.store.execute('INSERT INTO conversation_archives VALUES (?,?,?,?,?)',
                    (cid, conversation['version'] + 1, canonical({'projects': inventory['projects']}),
                     principal.actor, now))
                self.store.execute('UPDATE conversation_rooms SET archived_at=?,archived_by=?,version=version+1 WHERE id=?',
                                   (now, principal.actor, cid))
                operations = self.stop(conversation, inventory, now)
                self.store.execute('DELETE FROM conversation_projects WHERE conversation_id=?', (cid,))
                for item in inventory['projects']:
                    room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=?', (item['id'],))
                    self.c.audit(room, principal.actor, 'conversation.archived', cid,
                        {'conversation_version': conversation['version'] + 1, 'recoverable': True,
                         'shared_partition': not bool(item['exclusive']), 'authority_restored_on_undo': False})
                row = self.conversations.object(principal, cid, include_archived=True)
                return {'conversation': self.archived_view(principal, row), 'archived': True, 'can_restore': True,
                        'operations': operations, 'warnings': list(WARNINGS), 'retained': list(RETAINED)}
            return self.receipt(principal, 'conversation_archive', args, save)

    def stop(self, conversation, inventory, now):
        cid = conversation['id']
        operations = []
        self.store.execute("""UPDATE collaboration_messages SET state='rejected',version=version+1
            WHERE conversation_id=? AND state='awaiting_approval'""", (cid,))
        self.store.execute("""UPDATE collaboration_goals SET state='cancelled',version=version+1,updated=?
            WHERE source_message_id IN (SELECT id FROM collaboration_messages WHERE conversation_id=?)
            AND state NOT IN ('verified','cancelled')""", (now, cid))
        for item in inventory['goals']:
            goal = self.store.one('SELECT * FROM coordination_goals WHERE id=?', (item['id'],))
            self.c.coordination.stop_work(goal, 'CONVERSATION_ARCHIVED')
            self.store.execute("""UPDATE coordination_goals SET state='cancelled',version=version+1,updated=?
                WHERE id=? AND state NOT IN ('succeeded','failed','cancelled','completed')""", (now, goal['id']))
            operations.extend(self.c.coordination.operations(goal))
        self.store.execute("""UPDATE coordination_work SET state='cancelled',lease_until=NULL,fencing_token=fencing_token+1,
            version=version+1,reason_code='CONVERSATION_ARCHIVED',updated=?
            WHERE goal_id IN (SELECT id FROM coordination_goals WHERE conversation_id=?)
            AND state NOT IN ('succeeded','failed','cancelled')""", (now, cid))
        for item in inventory['jobs']:
            self.store.execute("""UPDATE collaboration_jobs SET state='cancelled',fencing_token=fencing_token+1,
                lease_until=NULL,version=version+1,updated=?,reason_code='CONVERSATION_ARCHIVED'
                WHERE id=? AND state NOT IN ('succeeded','failed','cancelled','expired','dead_letter')""", (now, item['id']))
        self.store.execute("""UPDATE delegation_policies SET state='revoked',version=version+1,updated=?
            WHERE conversation_id=? AND state!='revoked'""", (now, cid))
        self.store.execute("""UPDATE conversation_project_scopes SET state='revoked',revoked_at=?
            WHERE conversation_id=? AND state!='revoked'""", (now, cid))
        # Credential-wide ceilings are global grant restrictions, not room-owned
        # authority. Preserve them even when their provenance names this room.
        self.store.execute("""UPDATE conversation_writers SET enabled=0,version=version+1,updated=?
            WHERE conversation_id=?""", (now, cid))
        working = self.conversations.working_scopes.current(cid)
        if working:
            spec = {**working['spec'], 'projects': [], 'version': working['version'] + 1,
                    'archived_scope': True, 'archive_version': conversation['version'] + 1}
            self.store.execute('INSERT INTO conversation_working_scopes VALUES (?,?,?,?,?)',
                               (cid, spec['version'], canonical(spec), digest(spec), now))
            for item in working['spec']['projects']:
                self.store.execute('INSERT OR IGNORE INTO conversation_project_revocations VALUES (?,?,?,?)',
                                   (cid, item['project_id'], spec['version'], now))
        for item in inventory['slots']:
            self.store.execute("""UPDATE collaboration_join_slots SET state='revoked',version=version+1,
                code_expires_at=0,updated=? WHERE id=? AND state!='revoked'""", (now, item['id']))
        for item in inventory['subscriptions']:
            self.store.execute("UPDATE mcp_event_subscriptions SET state='revoked',version=version+1,updated=? WHERE id=?",
                               (now, item['id']))
            self.store.execute("""UPDATE mcp_event_deliveries SET state='abandoned',lease_until=NULL,fence=fence+1,
                reason_code='conversation_archived' WHERE subscription_id=? AND state IN ('pending','retry_wait','leased')""", (item['id'],))
        for channel, bindings in inventory['bindings'].items():
            for binding in bindings:
                self.store.execute('UPDATE ' + channel + """_bindings SET state='revoked',version=version+1,
                    pairing_hash='',pairing_secret='',updated=? WHERE id=?""", (now, binding['id']))
                self.c.channel_owner.revoke(binding['id'], 'conversation_archived')
                self.c.channel_enrollment.invalidate(binding['id'], 'conversation_archived')
                if channel == 'weixin':
                    self.c.weixin.delivery.quarantine_binding(binding['id'], 'conversation_archived')
                else:
                    self.c.channels.quarantine(channel, binding['id'], 'conversation_archived')
                    # Keep uncertain external sends uncertain, fence their stale workers.
                    self.store.execute("""UPDATE channel_mirror_parts SET
                        state=CASE WHEN state='sending' THEN 'unknown' ELSE state END,
                        lease_fence=lease_fence+1,lease_until=0,reason_code='conversation_archived',updated=?
                        WHERE delivery_id IN (SELECT id FROM channel_mirrors WHERE channel='telegram' AND binding_id=?)
                        AND state NOT IN ('accepted','owner_confirmed','cancelled','rejected','superseded')""", (now, binding['id']))
        self.store.execute("""UPDATE weixin_enrollments SET state='cancelled',version=version+1,updated=?
            WHERE conversation_id=? AND state NOT IN ('cancelled','completed')""", (now, cid))
        self.store.execute("""UPDATE channel_attachments SET state='quarantined',lease_fence=lease_fence+1,
            lease_until=0,reason_code='CONVERSATION_ARCHIVED',updated=? WHERE conversation_id=? AND state='pending'""", (now, cid))
        for item in inventory['projects']:
            if not item['exclusive']:
                continue
            rid = item['id']
            self.store.execute("UPDATE collaboration_rooms SET state='paused',version=version+1 WHERE id=?", (rid,))
            self.store.execute('UPDATE collaboration_agents SET enabled=0,version=version+1 WHERE room_id=?', (rid,))
            self.store.execute('UPDATE collaboration_message_writers SET enabled=0,version=version+1,updated=? WHERE room_id=?', (now, rid))
            self.store.execute("""UPDATE monitor_plan_bindings SET state='paused',active_version=NULL,
                collection_fence=collection_fence+1,collection_lease_until=NULL,version=version+1 WHERE room_id=?""", (rid,))
        return operations

    def restore(self, raw, principal):
        args = validate(Restore, raw)
        with self.store.transaction():
            principal, conversation = self.owner(principal, args['conversation_id'])
            def save():
                if conversation['version'] != args['expected_version']:
                    raise DevError('STALE_VERSION', '房间已改变，请刷新后再恢复', 409)
                if conversation['archived_at'] is None:
                    raise DevError('CONVERSATION_NOT_ARCHIVED', '此房间未删除', 409)
                row = self.store.one('SELECT snapshot FROM conversation_archives WHERE conversation_id=? ORDER BY version DESC LIMIT 1',
                                     (conversation['id'],))
                snapshot = json.loads(row['snapshot'])
                restored, unavailable = [], []
                for item in snapshot['projects']:
                    try:
                        self.c.runtime.project(item['project_id'], principal)
                    except DevError:
                        unavailable.append(item['project_id'])
                        continue
                    room = self.store.one('SELECT * FROM collaboration_rooms WHERE id=? AND space_id=? AND owner_user_id=?',
                                          (item['id'], principal.space_id, principal.user_id))
                    if not room:
                        unavailable.append(item['project_id'])
                        continue
                    self.store.execute('INSERT OR IGNORE INTO conversation_projects VALUES (?,?,?)',
                                       (conversation['id'], room['id'], item['created']))
                    restored.append(room)
                if not restored:
                    raise DevError('CONVERSATION_RESTORE_PROJECT_UNAVAILABLE', '原项目当前不可访问，无法恢复房间；请先恢复项目访问', 409)
                self.store.execute("UPDATE conversation_rooms SET archived_at=NULL,archived_by='',version=version+1 WHERE id=?",
                                   (conversation['id'],))
                for room in restored:
                    self.c.audit(room, principal.actor, 'conversation.restored', conversation['id'],
                                 {'authority_restored': False, 'unavailable_projects': unavailable})
                current = self.conversations.object(principal, conversation['id'])
                return {'conversation': self.conversations.view(principal, current), 'restored': True,
                        'requires_reauthorization': True, 'restored_from_archive': True, 'unavailable_projects': unavailable,
                        'warnings': [WARNINGS[2]], 'retained': list(RETAINED)}
            return self.receipt(principal, 'conversation_restore', args, save)
