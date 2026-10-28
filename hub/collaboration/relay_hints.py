"""Authorized, compact UI invalidation. Hints never carry collaboration content."""
from __future__ import annotations

import asyncio
from threading import Lock

from hub.collaboration.common import canonical, digest
from shared.util import DevError


def migrate(db):
    db.execute('''CREATE TABLE IF NOT EXISTS collaboration_relay_revisions (
        room_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0)''')
    direct = ('collaboration_messages', 'collaboration_rooms', 'collaboration_join_slots',
              'mcp_event_subscriptions', 'coordination_goals', 'collaboration_audit',
              'collaboration_teams', 'dot_mailbox_bindings')
    indirect = {
        'dot_consumer_sessions': "SELECT room_id AS room_id FROM collaboration_join_slots WHERE id={row}.dot_id",
        'collaboration_dot_messages': "SELECT room_id AS room_id FROM collaboration_join_slots WHERE id={row}.dot_id",
        'mcp_event_deliveries': "SELECT room_id AS room_id FROM mcp_event_subscriptions WHERE id={row}.subscription_id",
        'coordination_work': "SELECT room_id AS room_id FROM coordination_goals WHERE id={row}.goal_id",
        'operations': "SELECT id AS room_id FROM collaboration_rooms WHERE project_id={row}.project_id",
    }
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in (*direct, *indirect):
        if table not in tables:
            continue
        for event in ('INSERT', 'UPDATE', 'DELETE'):
            row = 'OLD' if event == 'DELETE' else 'NEW'
            if table in indirect:
                selection = indirect[table].format(row=row)
            else:
                selection = 'SELECT ' + row + ('.id' if table == 'collaboration_rooms' else '.room_id') + ' AS room_id'
            # The WHERE also disambiguates SQLite's UPSERT after SELECT.
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS relay_hint_{table}_{event.lower()}
                AFTER {event} ON {table} BEGIN
                INSERT INTO collaboration_relay_revisions(room_id,revision)
                SELECT room_id,1 FROM ({selection}) AS rooms WHERE room_id IS NOT NULL
                ON CONFLICT(room_id) DO UPDATE SET revision=revision+1;
                END''')


class RelayHints:
    def __init__(self, collaboration):
        self.c, self.store = collaboration, collaboration.store
        self._lock = Lock()
        self._subscribers = {}

    def snapshot(self, raw, principal):
        with self.store.transaction(immediate=False):
            principal, room = self.c.scope(principal, raw)
            if principal.grant_id:
                raise DevError('PANEL_SESSION_REQUIRED', '实时提示只面向当前面板会话', 403)
            conversation = self.c.conversations.resolve(principal, room, raw)
            visible = self.c.conversations.visible(principal, conversation)
            identifiers = sorted(item['id'] for item in visible)
            placeholders = ','.join('?' for _ in identifiers)
            revisions = self.store.all(
                f'SELECT room_id,revision FROM collaboration_relay_revisions WHERE room_id IN ({placeholders}) ORDER BY room_id',
                identifiers) if identifiers else []
            access = digest([self.c.principal_snapshot(principal),
                             {'admin': principal.admin, 'scopes': sorted(principal.scopes),
                              'projects': sorted(principal.projects), 'role_id': principal.role_id,
                              'authorization_mode': principal.authorization_mode}, conversation['id'],
                             conversation['version'], [(item['id'], item['project_id'], item['state'], item['version'])
                                                       for item in sorted(visible, key=lambda item: item['id'])]])
            return {'revision': digest([access, revisions]), 'access': access,
                    'owner': (principal.space_id, principal.user_id)}

    def subscribe(self, owner):
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=1)
        with self._lock:
            if len(self._subscribers) >= 64 or sum(item[1] == owner for item in self._subscribers.values()) >= 4:
                raise DevError('TOO_MANY_STREAMS', '实时连接已达上限，请关闭重复页面后重试', 429)
            self._subscribers[queue] = (loop, owner)
        # Native operation transitions also wake the compact projection.
        self.c.runtime.watchers.add(queue)
        return queue

    def unsubscribe(self, queue):
        with self._lock:
            self._subscribers.pop(queue, None)
        self.c.runtime.watchers.discard(queue)

    @staticmethod
    def _put(queue):
        if queue.empty():
            queue.put_nowait(None)

    def wake(self):
        with self._lock:
            subscribers = tuple(self._subscribers.items())
        for queue, (loop, _) in subscribers:
            try:
                loop.call_soon_threadsafe(self._put, queue)
            except RuntimeError:
                pass  # Closing event loop; persistent revisions remain authoritative.

    async def stream(self, request, raw, auth, queue, initial, cursor=''):
        access, previous = initial['access'], cursor
        try:
            while not self.c.runtime.stopping:
                if await request.is_disconnected():
                    return
                try:
                    current = await self.store.run(lambda: self.snapshot(raw, auth.panel(request)))
                except DevError:
                    yield 'event: access_changed\ndata: {}\n\n'
                    return
                if current['access'] != access:
                    yield 'event: access_changed\ndata: {}\n\n'
                    return
                if current['revision'] != previous:
                    previous = current['revision']
                    yield 'id: ' + previous + '\nevent: change\ndata: ' + canonical({'revision': previous}) + '\n\n'
                try:
                    await asyncio.wait_for(queue.get(), 5)
                except asyncio.TimeoutError:
                    yield ': heartbeat\n\n'
        finally:
            self.unsubscribe(queue)
