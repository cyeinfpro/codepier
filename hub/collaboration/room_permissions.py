"""Single owner-approved room scope, with channel subsets and historical proofs.

Reading legacy rooms never writes or expands approvals. Host grants and project
settings remain resource ceilings; old work retains its original scope snapshot.
"""
from __future__ import annotations

import json

from hub.collaboration.common import canonical, digest, validate
from shared import room_contracts as contracts
from shared.util import DevError


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS room_permission_policies (
        conversation_id TEXT PRIMARY KEY REFERENCES conversation_rooms(id),
        version INTEGER NOT NULL,policy TEXT NOT NULL,approved_by TEXT NOT NULL,
        approved_at REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS room_permission_revisions (
        conversation_id TEXT NOT NULL,version INTEGER NOT NULL,policy TEXT NOT NULL,
        approved_by TEXT NOT NULL,approved_at REAL NOT NULL,
        PRIMARY KEY(conversation_id,version))""")


class RoomPermissions:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def row(self, conversation_id):
        return self.store.one('SELECT * FROM room_permission_policies WHERE conversation_id=?', (conversation_id,))

    def initialize(self, conversation_id, partitions, principal):
        """Persist the explicitly selected read-only scope within room creation."""
        rules = self.normalize([
            {'project_id': part['project_id'], 'environment_id': part['environment_id'],
             'capabilities': ['read'], 'execution_targets': [],
             'lifetime': {'mode': 'until_revoked', 'expires_at': None}}
            for part in partitions
        ], principal)
        policy = {'scope': self.scope(rules), 'channels': {
            name: {'mode': 'follow_room', 'scope': self.scope([])}
            for name in ('weixin', 'telegram')
        }}
        values = (conversation_id, 0, canonical(policy), principal.actor, self.c.clock())
        self.store.execute('INSERT INTO room_permission_policies VALUES (?,?,?,?,?)', values)
        self.store.execute('INSERT INTO room_permission_revisions VALUES (?,?,?,?,?)', values)

    def is_configured(self, conversation_id):
        return self.row(conversation_id) is not None

    @staticmethod
    def scope(rules):
        return {'project_ids': sorted({r['project_id'] for r in rules}), 'project_scopes': rules}

    @staticmethod
    def rule(row):
        return {key: row[key] for key in
                ('project_id', 'environment_id', 'capabilities', 'execution_targets', 'lifetime')}

    def seed(self, conversation_id):
        # Intersection, not the union or grant maximum. A narrower existing
        # member approval cannot silently turn into a room-wide expansion.
        members = self.store.all("""SELECT * FROM room_members WHERE conversation_id=?
            AND status NOT IN ('removed','legacy') ORDER BY created_at,id""", (conversation_id,))
        candidates = None
        for member in members:
            rules = {(r['project_id'], r['environment_id']): self.rule(r)
                     for r in self.c.rooms.scopes(member) if r['capabilities']}
            if candidates is None:
                candidates = rules
                continue
            for key in list(candidates):
                if key not in rules:
                    del candidates[key]
                    continue
                a, b = candidates[key], rules[key]
                a['capabilities'] = sorted(set(a['capabilities']) & set(b['capabilities']))
                a['execution_targets'] = sorted(set(a['execution_targets']) & set(b['execution_targets']))
                if 'execute' in a['capabilities'] and not a['execution_targets']:
                    a['capabilities'].remove('execute')
                deadlines = [r['lifetime']['expires_at'] for r in (a, b)
                             if r['lifetime']['mode'] == 'timed']
                a['lifetime'] = {'mode': 'timed', 'expires_at': min(deadlines)} if deadlines else {
                    'mode': 'until_revoked', 'expires_at': None}
                if 'read' not in a['capabilities']:
                    del candidates[key]
        return list((candidates or {}).values())

    def policy(self, conversation_id):
        row = self.row(conversation_id)
        return json.loads(row['policy']) if row else {
            'scope': self.scope(self.seed(conversation_id)),
            'channels': {name: {'mode': 'follow_room', 'scope': self.scope([])}
                         for name in ('weixin', 'telegram')}}

    def channel_mode(self, conversation_id, channel='weixin'):
        return self.policy(conversation_id)['channels'].get(channel, {}).get('mode', 'follow_room')

    def effective_scopes(self, conversation_id, channel=None):
        policy = self.policy(conversation_id)
        rules = policy['scope']['project_scopes']
        if channel:
            entry = policy['channels'].get(channel, {'mode': 'follow_room'})
            if entry['mode'] == 'custom':
                rules = entry['scope']['project_scopes']
        return [self.rule(r) for r in rules if r['lifetime']['expires_at'] is None
                or r['lifetime']['expires_at'] > self.c.clock()]

    def available(self, principal, conversation_id):
        attached = self.store.all("""SELECT r.project_id,r.environment_id FROM collaboration_rooms r
            JOIN conversation_projects p ON p.room_id=r.id WHERE p.conversation_id=?""", (conversation_id,))
        known = self.policy(conversation_id)['scope']['project_scopes']
        result = []
        for row in self.c.runtime.iter_project_rows(principal):
            try:
                project = self.c.runtime.project(row['id'], principal)
            except DevError:
                continue
            caps = []
            for cap in ('read', 'write', 'execute'):
                if cap == 'write' and project['mode'] != 'write':
                    continue
                if cap == 'execute' and not project['allow_tasks']:
                    continue
                try:
                    self.c.coordination.require_capability(principal, cap, project['id'])
                    caps.append(cap)
                except DevError:
                    continue
            environments = {p['environment_id'] for p in attached + known if p['project_id'] == project['id']} or {'production'}
            target_labels = {'project_agent': '项目 Agent'}
            target_labels.update({'vps:' + row['id']: 'VPS · ' + row['name']
                for row in self.store.all('''SELECT v.id,v.name FROM vps_connections v
                JOIN vps_projects p ON p.vps_id=v.id WHERE p.project_id=? AND v.enabled=1''', (project['id'],))})
            targets = set(target_labels)
            for rule in known:
                if rule['project_id'] == project['id']:
                    targets.update(rule['execution_targets'])
            allowed_targets = []
            for target in sorted(targets):
                try:
                    self.c.delegation.target_snapshot(principal, project, target)
                    allowed_targets.append(target)
                except DevError:
                    continue
            for environment in sorted(environments):
                result.append({'project_id': project['id'], 'environment_id': environment,
                    'name': project.get('alias', project['id']), 'capabilities': caps,
                    'execution_targets': allowed_targets if 'execute' in caps else [],
                    'execution_target_labels': {target: target_labels.get(target, target)
                        for target in allowed_targets} if 'execute' in caps else {}})
        return result

    def read(self, conversation_id, principal):
        with self.store.transaction(immediate=False):
            principal, room = self.c.rooms.owner(principal, conversation_id)
            row, policy = self.row(conversation_id), self.policy(conversation_id)
            available = self.available(principal, conversation_id)
            ceilings = {(r['project_id'], r['environment_id']): r for r in available}
            def effective(channel=None):
                result = []
                for rule in self.effective_scopes(conversation_id, channel):
                    ceiling = ceilings.get((rule['project_id'], rule['environment_id']), {})
                    caps = sorted(set(rule['capabilities']) & set(ceiling.get('capabilities', [])))
                    targets = sorted(set(rule['execution_targets']) & set(ceiling.get('execution_targets', [])))
                    if 'execute' in caps and not targets:
                        caps.remove('execute')
                    if 'read' in caps:
                        result.append({**rule, 'capabilities': caps,
                                       'execution_targets': targets if 'execute' in caps else []})
                return self.scope(result)
            channels = {}
            for name, entry in policy['channels'].items():
                channels[name] = {**entry, 'effective_scope': effective(name),
                                  'transport_supported': name == 'weixin'}
            return {'conversation_id': conversation_id, 'version': row['version'] if row else 0,
                'configured': bool(row), 'scope': policy['scope'],
                'limits': {'project_scopes': None, 'execution_targets': None},
                'effective_scope': effective(),
                'available_projects': available, 'channels': channels,
                'authority': 'room', 'host_constraints_are_hard_limits': True,
                'migration_mode': 'configured' if row else 'preserve_existing_approvals',
                'room_version': room['version']}

    def normalize(self, scopes, principal):
        seen, rules = set(), []
        for scope in scopes:
            key = (scope['project_id'], scope['environment_id'])
            if key in seen:
                raise DevError('DUPLICATE_PROJECT_SCOPE', '项目与环境不能重复', 422)
            seen.add(key)
            project = self.c.runtime.project(scope['project_id'], principal)
            if project['id'] != scope['project_id']:
                raise DevError('ROOM_PERMISSION_PROJECT_ID_REQUIRED', '请选择项目的稳定标识', 422)
            for capability in scope['capabilities']:
                self.c.coordination.require_capability(principal, capability, project['id'])
            if 'write' in scope['capabilities'] and project['mode'] != 'write':
                raise DevError('PROJECT_READ_ONLY', '项目上限仅允许读取', 403)
            if 'execute' in scope['capabilities'] and not project['allow_tasks']:
                raise DevError('PROJECT_EXECUTION_DISABLED', '项目上限不允许执行', 403)
            for target in scope['execution_targets']:
                self.c.delegation.target_snapshot(principal, project, target)
            lifetime = scope['lifetime']
            if lifetime['expires_at'] is not None and lifetime['expires_at'] <= self.c.clock():
                raise DevError('AUTHORIZATION_EXPIRED', '授权期限已过，请明确选择未来期限', 422)
            rules.append({**scope, 'capabilities': sorted(scope['capabilities']),
                          'execution_targets': sorted(set(scope['execution_targets']))})
        return sorted(rules, key=lambda r: (r['project_id'], r['environment_id']))

    @staticmethod
    def subset(child, parent):
        by_key = {(r['project_id'], r['environment_id']): r for r in parent}
        for rule in child:
            ceiling = by_key.get((rule['project_id'], rule['environment_id']))
            if (not ceiling or not set(rule['capabilities']) <= set(ceiling['capabilities'])
                    or not set(rule['execution_targets']) <= set(ceiling['execution_targets'])
                    or (ceiling['lifetime']['expires_at'] is not None and
                        (rule['lifetime']['expires_at'] is None or
                         rule['lifetime']['expires_at'] > ceiling['lifetime']['expires_at']))):
                raise DevError('CHANNEL_SCOPE_EXCEEDS_ROOM', '渠道权限与期限必须是房间权限的子集', 422)

    def sync_member(self, principal, member, scopes):
        detached = self.c.rooms.member(member['id'])['scope_revision'] != member['scope_revision']
        previous_digest = None
        if member['grant_id'] and member['status'] != 'legacy':
            try:
                previous_digest = self.c.room_consumers.scope_digest(
                    self.store.one('SELECT * FROM conversation_rooms WHERE id=?', (member['conversation_id'],)), member)
            except DevError:
                pass
        delta = self.c.rooms.approve_scopes(principal, member['id'], member['conversation_id'], scopes, None)
        bridge = getattr(self.c, 'room_work_bridge', None)
        available = member['status'] not in {'legacy', 'paused', 'removed'}
        actor = None
        if available and member['grant_id']:
            try:
                actor = self.c.rooms.principal(self.c.rooms.member(member['id']))
            except DevError as exc:
                if exc.code not in {'INVALID_TOKEN', 'ROOM_MEMBER_UNBOUND'}:
                    raise
                # A dead host credential cannot veto the owner's room policy.
                # Keep the member degraded; never rebuild or bind its adapters.
                available = False
        if bridge is not None and scopes and available:
            bridge.ensure(self.c.rooms.member(member['id']), principal)
            current = self.c.rooms.member(member['id'])
            if actor is not None:
                bridge.bind(current, actor)
        if (detached or any(delta.values())) and member['grant_id'] and member['status'] != 'legacy':
            self.c.room_consumers.scope_updated(self.c.rooms.member(member['id']), previous_digest,
                additions_only=bool(not detached and delta['added'] and not delta['changed'] and not delta['revoked']))

    def save(self, raw, principal):
        args = validate(contracts.RoomPermissionsUpdate, raw)
        if not args['idempotency_key']:
            args['idempotency_key'] = digest(args)
        with self.store.transaction():
            principal, room = self.c.rooms.owner(principal, args['conversation_id'])
            self.c.rooms.require_native(room)
            def perform():
                current = self.row(room['id'])
                version = current['version'] if current else 0
                previous_channels = self.policy(room['id'])['channels']
                if args['expected_version'] != version:
                    raise DevError('STALE_VERSION', '权限已更新，请重新核对后保存', 409, current_version=version)
                rules = self.normalize(args['scope']['project_scopes'], principal)
                channels = {}
                for name, entry in args['channels'].items():
                    custom = self.normalize(entry['scope']['project_scopes'], principal) if entry['mode'] == 'custom' else []
                    self.subset(custom, rules)
                    channels[name] = {'mode': entry['mode'], 'scope': self.scope(custom)}
                policy = {'scope': self.scope(rules), 'channels': channels}
                # Retain pre-detach revisions so revocations still expire the
                # consumer even when partition removal already changed rows.
                members = self.store.all("""SELECT * FROM room_members WHERE conversation_id=?
                    AND status NOT IN ('removed','legacy')""", (room['id'],))
                # Reuse the audited partition detach path. No file deletion,
                # old work and scope revisions remain historical evidence.
                self.c.rooms.projects({'conversation_id': room['id'], 'expected_version': room['version'],
                    'projects': [{'project': r['project_id'], 'environment_id': r['environment_id']} for r in rules],
                    'idempotency_key': 'permissions:' + digest(args)}, principal, _canonical=True)
                for member in members:
                    self.sync_member(principal, member, rules)
                values = (room['id'], version + 1, canonical(policy), principal.actor, self.c.clock())
                self.store.execute("""INSERT INTO room_permission_policies VALUES (?,?,?,?,?)
                    ON CONFLICT(conversation_id) DO UPDATE SET version=excluded.version,policy=excluded.policy,
                    approved_by=excluded.approved_by,approved_at=excluded.approved_at""", values)
                self.store.execute('INSERT INTO room_permission_revisions VALUES (?,?,?,?,?)', values)
                if previous_channels != channels:
                    for member in members:
                        if member['grant_id']:
                            self.c.room_consumers.enqueue_ref(self.c.rooms.member(member['id']),
                                'control_notice', room['id'], version + 1, {'reason_code': 'channel_scope_changed'})
                self.c.room_work_bridge.native.stop_revoked_inputs(room['id'])
                self.c.rooms.change(room['id'], 'permissions', room['id'], version + 1)
                return {'conversation_id': room['id'], 'permission_version': version + 1}
            self.c.rooms.mutation(principal, 'room_permissions', args, perform)
            return self.read(room['id'], principal)
