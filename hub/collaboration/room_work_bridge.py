"""Adapt explicit member approvals to the existing managed execution engine.

The stable member is the product identity. Legacy dot/policy IDs are private
execution adapters, not another invitation or another host subscription.
"""
from __future__ import annotations

import json
from hub.principal import Principal, refresh_principal
from hub.collaboration.common import canonical, digest
from shared.util import DevError


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS room_member_work_bridges(
        member_id TEXT NOT NULL REFERENCES room_members(id),
        legacy_dot_id TEXT PRIMARY KEY,scope_revision INTEGER NOT NULL,
        scope_digest TEXT NOT NULL,project_id TEXT NOT NULL,environment_id TEXT NOT NULL,
        policy_id TEXT NOT NULL DEFAULT '',policy_version INTEGER NOT NULL DEFAULT 0,
        policy_digest TEXT NOT NULL DEFAULT '',state TEXT NOT NULL DEFAULT 'active',created REAL NOT NULL,updated REAL NOT NULL,
        binding_generation INTEGER NOT NULL DEFAULT 1)''')
    if 'binding_generation' not in {row[1] for row in db.execute('PRAGMA table_info(room_member_work_bridges)')}:
        db.execute('ALTER TABLE room_member_work_bridges ADD COLUMN binding_generation INTEGER NOT NULL DEFAULT 1')
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS room_work_bridge_current ON room_member_work_bridges(member_id,project_id,environment_id) WHERE state='active'")
    from hub.collaboration.room_goal_scopes import migrate as migrate_native_goals
    migrate_native_goals(db)


class RoomWorkBridge:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store
        from hub.collaboration.room_goal_scopes import RoomGoalScopes
        self.native=RoomGoalScopes(self)

    def scopes(self, member):
        rows = self.store.all("SELECT * FROM room_member_scopes WHERE member_id=? AND state='active' ORDER BY project_id,environment_id", (member['id'],))
        return [row for row in rows if row['lifetime_mode']=='until_revoked' or row['expires_at'] > self.c.clock()]

    @staticmethod
    def fingerprint(scopes):
        return digest([{k:row[k] for k in ('project_id','environment_id','capabilities','execution_targets',
            'lifetime_mode','expires_at','version','state','approved_by','approved_at','owner_principal','resource_snapshot')} for row in scopes])

    def owner(self, member, scope):
        saved = json.loads(scope['owner_principal'])
        if not saved:
            raise DevError('ROOM_SCOPE_APPROVAL_REQUIRED','需要房主原始项目范围批准',403)
        owner = refresh_principal(self.store, Principal(**{**saved,'scopes':set()}))
        owner, conversation = self.c.rooms.owner(owner,member['conversation_id'])
        if owner.user_id != member['owner_user_id']:
            raise DevError('ROOM_SCOPE_OWNER_CHANGED','房间原批准身份已改变',403)
        return owner

    def ensure(self, member, owner):
        if member['status']=='legacy':
            return self.c.dots.find(member['legacy_dot_id']) if member.get('legacy_dot_id') else None
        scopes = self.scopes(member)
        if not scopes:
            return None
        owner, conversation = self.c.rooms.owner(owner,member['conversation_id'])
        actor=self.c.rooms.principal(member) if member['grant_id'] else owner
        chosen = None
        for scope in scopes:
            fingerprint = self.fingerprint([scope])
            capabilities,targets=self.effective(scope,actor)
            record = self.store.one("SELECT * FROM room_member_work_bridges WHERE member_id=? AND project_id=? AND environment_id=? AND state='active'",
                (member['id'],scope['project_id'],scope['environment_id']))
            old_dot=self.c.dots.find(record['legacy_dot_id']) if record else None
            old_setup=json.loads(old_dot['setup']) if old_dot else {}
            same=bool(record and record['scope_digest']==fingerprint and record['binding_generation']==member['binding_generation']
                and old_setup.get('capabilities')==capabilities and old_setup.get('execution_targets',[])==targets)
            if same:
                dot = self.c.dots.find(record['legacy_dot_id'])
            else:
                if record:
                    self.store.execute("UPDATE room_member_work_bridges SET state='superseded',updated=? WHERE legacy_dot_id=?",(self.c.clock(),record['legacy_dot_id']))
                    policy=self.store.one('SELECT * FROM delegation_policies WHERE id=?',(record['policy_id'],))
                    if policy:
                        self.c.delegation.stop(policy,'ROOM_MEMBER_SCOPE_REVISED')
                if 'read' not in capabilities:
                    continue
                result=self.c.dots.create({'project':scope['project_id'],'environment_id':scope['environment_id'],
                    'conversation_id':member['conversation_id'],'label':member['name'],'duplex':True,
                    'capabilities':capabilities,'execution_target':targets[0] if targets else 'project_agent',
                    **({'execution_targets':targets} if targets else {}),
                    'acknowledge_unsandboxed_exec':'execute' in capabilities,'confirm_tasks':True,
                    'idempotency_key':'room-adapter:'+digest([member['id'],member['binding_generation'],fingerprint,capabilities,targets])},owner)
                identifier=result['dot']['id']
                now=self.c.clock()
                self.store.execute("""INSERT INTO room_member_work_bridges
                    (member_id,legacy_dot_id,scope_revision,scope_digest,project_id,environment_id,created,updated,binding_generation)
                    VALUES (?,?,?,?,?,?,?,?,?)""",
                    (member['id'],identifier,scope['version'],fingerprint,scope['project_id'],scope['environment_id'],now,now,member['binding_generation']))
                self.lifetime(member,scope,identifier)
                dot=self.c.dots.find(identifier)
            if chosen is None or dot['id']==member.get('legacy_dot_id'):
                chosen=dot
        if chosen and chosen['id']!=member.get('legacy_dot_id'):
            self.store.execute('UPDATE room_members SET legacy_dot_id=? WHERE id=?',(chosen['id'],member['id']))
        return chosen

    def effective(self,scope,actor):
        """Grant limits can narrow an approval without blocking room chat."""
        try:
            self.c.runtime.project(scope['project_id'],actor)
        except DevError:
            return [],[]
        capabilities=[]
        for capability in json.loads(scope['capabilities']):
            try:
                self.c.coordination.require_capability(actor,capability,scope['project_id'])
                capabilities.append(capability)
            except DevError:
                pass
        return sorted(capabilities),sorted(json.loads(scope['execution_targets'])) if 'execute' in capabilities else []

    def adapter(self,member,project_id,environment_id=None):
        rows=self.store.all("SELECT * FROM room_member_work_bridges WHERE member_id=? AND project_id=? AND state='active'"+
            (' AND environment_id=?' if environment_id else ''),(member['id'],project_id,*([environment_id] if environment_id else [])))
        if len(rows)!=1:
            raise DevError('ROOM_WORK_SCOPE_REQUIRED','请为此工作明确选择已批准的项目与环境',409)
        record=rows[0]
        scopes=[scope for scope in self.scopes(member) if scope['project_id']==project_id and scope['environment_id']==record['environment_id']]
        if len(scopes)!=1 or self.fingerprint(scopes)!=record['scope_digest'] or record['binding_generation']!=member['binding_generation']:
            raise DevError('ROOM_WORK_SCOPE_CHANGED','本次工作的项目批准已改变',409)
        return record,scopes[0]

    def lifetime(self,member,scope,identifier):
        if scope['lifetime_mode']=='until_revoked':
            self.store.execute("UPDATE collaboration_join_slots SET lifetime_mode='until_revoked',lifetime_authorization_id=? WHERE id=?",
                ('room-member:'+member['id'],identifier))
        else:
            self.store.execute("UPDATE collaboration_join_slots SET lifetime_mode='timed',lifetime_authorization_id=?,expires_at=MIN(expires_at,?) WHERE id=?",
                ('room-member:'+member['id'],scope['expires_at'],identifier))

    def bind(self,member,principal):
        if member['status'] in {'legacy','paused','removed'}:
            return None
        scopes=self.scopes(member)
        if not scopes:
            return None
        owner=self.owner(member,scopes[0])
        self.ensure(member,owner)
        member=self.c.rooms.member(member['id'])
        results=[]
        for scope in scopes:
            capabilities,targets=self.effective(scope,principal)
            if 'read' not in capabilities:
                continue
            record,_=self.adapter(member,scope['project_id'],scope['environment_id'])
            dot=self.c.dots.find(record['legacy_dot_id'])
            for actor in (owner,principal):
                self.c.runtime.project(scope['project_id'],actor)
                for capability in capabilities:
                    self.c.coordination.require_capability(actor,capability,scope['project_id'])
            slot=self.store.one('SELECT * FROM collaboration_join_slots WHERE id=?',(dot['id'],))
            if slot['state']=='registered' and slot['grant_id']!=principal.grant_id:
                raise DevError('ROOM_WORK_BINDING_CHANGED','原执行连接属于另一个身份，请明确更换成员绑定',409)
            result=self.c.dots.join({'code':slot['join_code'],
                'idempotency_key':'room-adapter-join:'+digest([dot['id'],member['binding_generation'],principal.grant_id])},principal)
            dot=self.c.dots.find(dot['id'])
            policy=self.store.one('SELECT * FROM delegation_policies WHERE id=?',(dot['policy_id'],))
            self.store.execute("""UPDATE room_member_work_bridges SET policy_id=?,policy_version=?,policy_digest=?,updated=? WHERE legacy_dot_id=?""",
                (policy['id'],policy['version'],policy['digest'],self.c.clock(),dot['id']))
            if scope['lifetime_mode']=='until_revoked':
                self.store.execute("UPDATE delegation_policies SET lifetime_mode='until_revoked',lifetime_authorization_id=? WHERE id=?",
                    ('room-member:'+member['id'],policy['id']))
            else:
                self.store.execute("UPDATE delegation_policies SET lifetime_mode='timed',lifetime_authorization_id=?,expires_at=MIN(expires_at,?) WHERE id=?",
                    ('room-member:'+member['id'],scope['expires_at'],policy['id']))
            results.append(result)
        return results

    def persistent(self,row):
        return row.get('lifetime_mode')=='until_revoked' and self.authorized(row)

    def authorized(self,row):
        identifier=row.get('lifetime_authorization_id','')
        if not identifier.startswith('room-member:'):
            return False
        member_id=identifier.split(':',1)[1]
        member=self.store.one('SELECT * FROM room_members WHERE id=?',(member_id,))
        record=self.store.one("SELECT * FROM room_member_work_bridges WHERE member_id=? AND state='active' AND (legacy_dot_id=? OR policy_id=?)",(member_id,row['id'],row['id']))
        if (not member or not record or member['status'] in {'removed','paused'}
                or record['binding_generation']!=member['binding_generation']):
            return False
        scopes=self.scopes(member)
        scope=next((s for s in scopes if s['project_id']==record['project_id'] and s['environment_id']==record['environment_id']),None)
        if (not scope or scope['lifetime_mode']!=row.get('lifetime_mode') or self.fingerprint([scope])!=record['scope_digest']
                or not self.c.rooms.member_scope_authorized(scope)):
            return False
        slot=self.store.one('SELECT * FROM collaboration_join_slots WHERE id=?',(record['legacy_dot_id'],))
        if (not slot or slot['state'] in {'revoked','paused'}
                or (member['grant_id'] and slot['grant_id'] and slot['grant_id']!=member['grant_id'])):
            return False
        if row['id']==slot['id']:
            if row.get('principal')!=slot['principal'] or row.get('grant_id')!=slot['grant_id']:
                return False
        elif row['id']==record['policy_id']:
            policy=self.store.one('SELECT * FROM delegation_policies WHERE id=?',(record['policy_id'],))
            if (not policy or policy['state']!='active' or row.get('version')!=record['policy_version']
                    or row.get('digest')!=record['policy_digest'] or row.get('grant_id')!=member['grant_id']
                    or policy['digest']!=record['policy_digest'] or policy['version']!=record['policy_version']):
                return False
        else:
            return False
        try:
            owner=self.owner(member,scope)
            if not self.store.one('''SELECT 1 FROM conversation_projects p JOIN collaboration_rooms r ON r.id=p.room_id
                WHERE p.conversation_id=? AND r.project_id=? AND r.environment_id=?''',
                (member['conversation_id'],scope['project_id'],scope['environment_id'])):
                return False
            self.c.runtime.project(scope['project_id'],owner)
            if member['grant_id']:
                actor=self.c.rooms.principal(member)
                self.c.rooms.member_scope(actor,member['conversation_id'],member['id'],member['binding_generation'])
                self.c.runtime.project(scope['project_id'],actor)
                dot=self.c.dots.find(record['legacy_dot_id'])
                setup=json.loads(dot['setup'])
                capabilities=setup['capabilities']
                if not set(capabilities)<=set(json.loads(scope['capabilities'])) or not set(setup.get('execution_targets',[]))<=set(json.loads(scope['execution_targets'])):
                    return False
                if row['id']==record['policy_id'] and json.loads(row['spec'])['capabilities']!=capabilities:
                    return False
                for capability in capabilities:
                    self.c.coordination.require_capability(actor,capability,scope['project_id'])
        except DevError:
            return False
        return True
