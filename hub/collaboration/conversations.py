"""Independent rooms with explicit project partitions and current grant filtering."""
from __future__ import annotations

import json
import uuid

from hub.principal import refresh_principal
from hub.collaboration.common import canonical, digest, read_cursor, sign_cursor, validate
from shared import collaboration_contracts as contracts
from shared.util import DevError


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_rooms (
        id TEXT PRIMARY KEY,space_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,title TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,created REAL NOT NULL)''')
    columns_room = {row[1] for row in db.execute('PRAGMA table_info(conversation_rooms)')}
    if 'history_sequence' not in columns_room:
        db.execute('ALTER TABLE conversation_rooms ADD COLUMN history_sequence INTEGER NOT NULL DEFAULT 0')
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_projects (
        conversation_id TEXT NOT NULL REFERENCES conversation_rooms(id),room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
        created REAL NOT NULL,PRIMARY KEY(conversation_id,room_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_writers (
        conversation_id TEXT NOT NULL REFERENCES conversation_rooms(id),room_id TEXT NOT NULL REFERENCES collaboration_rooms(id),
        grant_id TEXT NOT NULL,enabled INTEGER NOT NULL,expires_at REAL NOT NULL,principal TEXT NOT NULL,
        updated REAL NOT NULL,version INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(conversation_id,room_id,grant_id))''')
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_idempotency (
        id TEXT PRIMARY KEY,digest TEXT NOT NULL,response TEXT NOT NULL,created REAL NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_read_cursors (
        conversation_id TEXT NOT NULL REFERENCES conversation_rooms(id),user_id TEXT NOT NULL,sequence INTEGER NOT NULL,
        updated REAL NOT NULL,PRIMARY KEY(conversation_id,user_id))''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(collaboration_messages)')}
    for name, declaration in [('conversation_id', "TEXT NOT NULL DEFAULT ''"), ('conversation_sequence', 'INTEGER NOT NULL DEFAULT 0')]:
        if name not in columns:
            db.execute(f'ALTER TABLE collaboration_messages ADD COLUMN {name} {declaration}')
    for room in db.execute('SELECT id,space_id,owner_user_id,project_id,environment_id,title,created,chat_history_sequence FROM collaboration_rooms').fetchall():
        db.execute('''INSERT OR IGNORE INTO conversation_rooms (id,space_id,owner_user_id,title,created) VALUES (?,?,?,?,?)''',
                   (room[0], room[1], room[2], room[5] or room[3] + ' · ' + room[4], room[6]))
        db.execute('INSERT OR IGNORE INTO conversation_projects VALUES (?,?,?)', (room[0], room[0], room[6]))
        if 'history_sequence' not in columns_room:
            db.execute('UPDATE conversation_rooms SET history_sequence=? WHERE id=?', (room[7], room[0]))
        db.execute("UPDATE collaboration_messages SET conversation_id=room_id WHERE room_id=? AND conversation_id=''", (room[0],))
        db.execute('''INSERT OR IGNORE INTO conversation_writers
            SELECT room_id,room_id,grant_id,enabled,expires_at,principal,updated,version
            FROM collaboration_message_writers WHERE room_id=?''', (room[0],))
        db.execute('''INSERT OR IGNORE INTO conversation_read_cursors
            SELECT room_id,user_id,MAX(sequence,?),updated FROM collaboration_read_cursors WHERE room_id=?''', (room[7], room[0]))
    db.execute('''UPDATE collaboration_messages SET conversation_sequence=server_sequence
        WHERE conversation_sequence=0 AND conversation_id=room_id''')
    for conversation in db.execute('SELECT id FROM conversation_rooms').fetchall():
        cid = conversation[0]
        maximum = db.execute('SELECT COALESCE(MAX(conversation_sequence),0) FROM collaboration_messages WHERE conversation_id=?', (cid,)).fetchone()[0]
        for message in db.execute('SELECT id FROM collaboration_messages WHERE conversation_id=? AND conversation_sequence=0 ORDER BY server_sequence,created,id', (cid,)).fetchall():
            maximum += 1
            db.execute('UPDATE collaboration_messages SET conversation_sequence=? WHERE id=?', (maximum, message[0]))
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS conversation_message_sequence ON collaboration_messages(conversation_id,conversation_sequence) WHERE conversation_sequence>0')
    db.execute('DROP TRIGGER IF EXISTS collaboration_message_insert')
    db.execute('''CREATE TRIGGER collaboration_message_insert AFTER INSERT ON collaboration_messages BEGIN
        UPDATE collaboration_messages SET
            server_sequence=(SELECT COALESCE(MAX(server_sequence),0)+1 FROM collaboration_messages WHERE room_id=NEW.room_id AND id!=NEW.id),
            conversation_id=CASE WHEN NEW.conversation_id='' THEN NEW.room_id ELSE NEW.conversation_id END,
            conversation_sequence=(SELECT COALESCE(MAX(conversation_sequence),0)+1 FROM collaboration_messages
                WHERE conversation_id=CASE WHEN NEW.conversation_id='' THEN NEW.room_id ELSE NEW.conversation_id END AND id!=NEW.id),
            thread_root_id=CASE WHEN NEW.thread_root_id!='' THEN NEW.thread_root_id ELSE
                COALESCE((SELECT NULLIF(thread_root_id,'') FROM collaboration_messages WHERE room_id=NEW.room_id
                    AND conversation_id=CASE WHEN NEW.conversation_id='' THEN NEW.room_id ELSE NEW.conversation_id END
                    AND id=NEW.thread_id AND id!=NEW.id),NEW.id) END WHERE id=NEW.id;
        INSERT INTO collaboration_room_changes(room_id,kind,object_id,version,created)
            VALUES (NEW.room_id,'message',NEW.id,NEW.version,NEW.created);
        END''')


class ConversationService:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store

    def ensure_default(self, room):
        self.store.execute('''INSERT OR IGNORE INTO conversation_rooms (id,space_id,owner_user_id,title,created)
            VALUES (?,?,?,?,?)''', (room['id'], room['space_id'], room['owner_user_id'],
            room['title'] or room['project_id'] + ' · ' + room['environment_id'], room['created']))
        self.store.execute('INSERT OR IGNORE INTO conversation_projects VALUES (?,?,?)', (room['id'], room['id'], room['created']))

    def object(self, principal, identifier):
        row = self.store.one('SELECT * FROM conversation_rooms WHERE id=? AND space_id=? AND owner_user_id=?',
                             (identifier, principal.space_id, principal.user_id))
        if not row:
            raise DevError('CONVERSATION_NOT_FOUND', '当前授权下没有这个聊天室', 404)
        return row

    def visible(self, principal, conversation):
        rows = self.store.all('''SELECT r.* FROM collaboration_rooms r JOIN conversation_projects p ON p.room_id=r.id
            WHERE p.conversation_id=? ORDER BY p.created,r.id''', (conversation['id'],))
        result = []
        for room in rows:
            try:
                project = self.c.runtime.project(room['project_id'], principal)
            except DevError:
                continue
            result.append({**room, 'project_label': project.get('alias', room['project_id'])})
        return result

    def resolve(self, principal, room, args):
        identifier = args.get('conversation_id') or room['id']
        conversation = self.object(principal, identifier)
        if not self.store.one('SELECT 1 FROM conversation_projects WHERE conversation_id=? AND room_id=?', (identifier, room['id'])):
            raise DevError('CONVERSATION_PROJECT_MISMATCH', '当前项目没有加入这个聊天室', 409)
        return conversation

    def view(self, principal, conversation):
        projects = self.visible(principal, conversation)
        if not projects:
            raise DevError('CONVERSATION_NOT_FOUND', '当前授权下没有这个聊天室', 404)
        return {**conversation, 'kind': 'conversation', 'projects': [
            {key: room[key] for key in ('project_id', 'environment_id', 'project_label', 'state')} | {'room_id': room['id']}
            for room in projects], 'visibility_token': digest([principal.grant_id or principal.user_id, [room['id'] for room in projects]])}

    def listing(self, principal):
        self.c.guard()
        principal = refresh_principal(self.store, principal)
        rows = self.store.all('SELECT * FROM conversation_rooms WHERE space_id=? AND owner_user_id=? ORDER BY created,id LIMIT 128',
                             (principal.space_id, principal.user_id))
        result = []
        for row in rows:
            try:
                result.append(self.view(principal, row))
            except DevError:
                continue
        return {'items': result, 'next_cursor': None}

    def mutation(self, principal, action, args, perform):
        identifier = digest([principal.space_id, principal.user_id, action, args['idempotency_key']])
        fingerprint = digest(args)
        saved = self.store.one('SELECT * FROM conversation_idempotency WHERE id=?', (identifier,))
        if saved:
            if saved['digest'] != fingerprint:
                raise DevError('IDEMPOTENCY_CONFLICT', '同一请求键不能用于不同内容', 409)
            result = json.loads(saved['response'])
            # Receipts identify the completed operation, never retain authority
            # over an old project list, version or visibility token.
            conversation = self.object(principal, result['conversation']['id'])
            return {**result, 'conversation': self.view(principal, conversation)}
        result = perform()
        receipt = {**result, 'conversation': {'id': result['conversation']['id']}}
        self.store.execute('INSERT INTO conversation_idempotency VALUES (?,?,?,?)', (identifier, fingerprint, canonical(receipt), self.c.clock()))
        return result

    def create(self, raw, principal):
        args = validate(contracts.ConversationCreate, raw)
        with self.store.transaction():
            self.c.guard()
            principal = refresh_principal(self.store, principal)
            self.c.owner(principal)
            # Reauthorize every supplied project before replaying an old receipt.
            for project in args['projects']:
                self.c.runtime.project(project['project'], principal)
            def save():
                rooms = []
                for project in args['projects']:
                    _, room = self.c.scope(principal, project, create=True)
                    if room['id'] in rooms:
                        raise DevError('DUPLICATE_PROJECT', '聊天室项目不能重复', 422)
                    rooms.append(room['id'])
                identifier = uuid.uuid4().hex
                self.store.execute('INSERT INTO conversation_rooms (id,space_id,owner_user_id,title,created) VALUES (?,?,?,?,?)',
                                   (identifier, principal.space_id, principal.user_id, args['title'].strip(), self.c.clock()))
                for room in rooms:
                    self.store.execute('INSERT INTO conversation_projects VALUES (?,?,?)', (identifier, room, self.c.clock()))
                return {'conversation': self.view(principal, self.object(principal, identifier))}
            return self.mutation(principal, 'conversation_create', args, save)

    def add_project(self, raw, principal):
        args = validate(contracts.ConversationProject, raw)
        with self.store.transaction():
            self.c.guard()
            principal = refresh_principal(self.store, principal)
            self.c.owner(principal)
            conversation = self.object(principal, args['conversation_id'])
            self.c.runtime.project(args['project'], principal)
            def save():
                if conversation['version'] != args['expected_version']:
                    raise DevError('STALE_VERSION', '聊天室项目列表已改变，请刷新后重新确认', 409)
                _, room = self.c.scope(principal, args, create=True)
                existing = self.store.one('SELECT 1 FROM conversation_projects WHERE conversation_id=? AND room_id=?', (conversation['id'], room['id']))
                if not existing:
                    count = self.store.one('SELECT COUNT(*) AS n FROM conversation_projects WHERE conversation_id=?', (conversation['id'],))['n']
                    if count >= 16:
                        raise DevError('CONVERSATION_PROJECT_LIMIT', '一个聊天室最多加入16个项目环境', 409)
                    self.store.execute('INSERT INTO conversation_projects VALUES (?,?,?)', (conversation['id'], room['id'], self.c.clock()))
                    self.store.execute('UPDATE conversation_rooms SET version=version+1 WHERE id=?', (conversation['id'],))
                return {'conversation': self.view(principal, self.object(principal, conversation['id'])), 'added': not bool(existing)}
            return self.mutation(principal, 'conversation_add_project:' + conversation['id'], args, save)

    def binding(self, principal, conversation, rooms, mode):
        return digest([conversation['id'], principal.grant_id or principal.user_id, sorted(room['id'] for room in rooms), mode])

    def read(self, args, principal, room):
        conversation = self.resolve(principal, room, args)
        rooms = self.visible(principal, conversation) if args.get('conversation_id') else [room]
        ids = [r['id'] for r in rooms]
        if not ids:
            raise DevError('CONVERSATION_NOT_FOUND', '没有可读取的项目', 404)
        mode = args['kind']
        params = [conversation['id'], *ids]
        where = 'conversation_id=? AND room_id IN (' + ','.join('?' for _ in ids) + ')'
        if mode == 'thread':
            root = self.c.object('collaboration_messages', room, args['id'])
            if root['conversation_id'] != conversation['id']:
                raise DevError('THREAD_NOT_FOUND', '话题不属于这个聊天室', 404)
            where += ' AND thread_root_id=?'
            params.append(root['thread_root_id'])
            mode += ':' + root['thread_root_id']
        if mode == 'search':
            query = args['query'].strip()
            if not query:
                raise DevError('INVALID_QUERY', '请输入搜索内容', 422)
            where += " AND body LIKE ? ESCAPE '\\'"
            params.append('%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
            mode += ':' + digest(query)
        binding = self.binding(principal, conversation, rooms, mode)
        latest = self.store.one('SELECT COALESCE(MAX(conversation_sequence),0) AS n FROM collaboration_messages WHERE ' +
                               'conversation_id=? AND room_id IN (' + ','.join('?' for _ in ids) + ')', (conversation['id'], *ids))['n']
        if args['cursor'] and args['after']:
            raise DevError('INVALID_CURSOR', '不能同时向前和向后分页', 400)
        after = bool(args['after'])
        value = read_cursor(self.c.secret, binding, args['after'] or args['cursor']) if args['after'] or args['cursor'] else latest + 1
        if type(value) is not int or value < 0:
            raise DevError('INVALID_CURSOR', '消息游标格式不正确', 400)
        where += ' AND conversation_sequence' + ('>?' if after else '<?')
        params.append(value)
        rows = self.store.all('SELECT * FROM collaboration_messages WHERE ' + where + ' ORDER BY conversation_sequence ' +
                             ('ASC' if after else 'DESC') + ' LIMIT ?', (*params, args['limit'] + 1))
        selected = rows[:args['limit']]
        if not after:
            selected.reverse()
        high = selected[-1]['conversation_sequence'] if selected else (value if after else latest)
        cursor = self.store.one('SELECT sequence FROM conversation_read_cursors WHERE conversation_id=? AND user_id=?',
                                (conversation['id'], principal.user_id))
        baseline = max(conversation['history_sequence'], room['chat_history_sequence'] if conversation['id'] == room['id'] else 0)
        return {'items': self.c.chatroom.views(selected), 'conversation': self.view(principal, conversation),
                'visibility_token': self.view(principal, conversation)['visibility_token'],
                'next_cursor': sign_cursor(self.c.secret, binding, selected[0]['conversation_sequence']) if not after and len(rows) > args['limit'] else None,
                'after_cursor': sign_cursor(self.c.secret, binding, high), 'has_more': len(rows) > args['limit'],
                'latest_sequence': latest, 'read_sequence': max(baseline, cursor['sequence'] if cursor else 0)}

    def changes(self, args, principal, room):
        conversation = self.resolve(principal, room, args)
        rooms = self.visible(principal, conversation) if args.get('conversation_id') else [room]
        ids = [r['id'] for r in rooms]
        binding = self.binding(principal, conversation, rooms, 'changes')
        value = read_cursor(self.c.secret, binding, args['cursor']) if args['cursor'] else 0
        if type(value) is not int or value < 0:
            raise DevError('INVALID_CURSOR', '变更游标格式不正确', 400)
        placeholders = ','.join('?' for _ in ids)
        rows = self.store.all(f'''SELECT c.sequence,c.kind,c.object_id,c.version,c.room_id AS source_room_id FROM collaboration_room_changes c
            WHERE c.room_id IN ({placeholders}) AND c.sequence>? AND (
                (c.kind IN ('message','message_delivery') AND EXISTS
                    (SELECT 1 FROM collaboration_messages m WHERE m.id=c.object_id AND m.conversation_id=?))
                OR (c.kind='job' AND EXISTS (SELECT 1 FROM collaboration_jobs j
                    LEFT JOIN collaboration_goals g ON g.id=j.goal_id
                    JOIN collaboration_messages m ON m.id=COALESCE(json_extract(j.context,'$.origin_message_id'),g.source_message_id)
                    WHERE j.id=c.object_id AND m.conversation_id=?))
                OR (c.kind='goal' AND EXISTS (SELECT 1 FROM collaboration_goals g
                    JOIN collaboration_messages m ON m.id=g.source_message_id WHERE g.id=c.object_id AND m.conversation_id=?)))
            ORDER BY c.sequence LIMIT ?''', (*ids, value, conversation['id'], conversation['id'], conversation['id'], args['limit'] + 1))
        selected = rows[:args['limit']]
        return {'items': selected, 'next_cursor': sign_cursor(self.c.secret, binding, selected[-1]['sequence'] if selected else value),
                'has_more': len(rows) > args['limit'], 'reset_required': False,
                'visibility_token': self.view(principal, conversation)['visibility_token']}
