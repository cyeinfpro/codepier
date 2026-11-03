"""Retained history must not turn bounded room operations into full scans.

The fixtures contain synthetic records only. VM instruction counts measure
query work without depending on host speed, wall-clock latency, or index names.
"""
import ast
import inspect
import re
import textwrap

from hub.collaboration.events import EventService, migrate_dispatch
from hub.collaboration.room_message_migration import migrate as migrate_messages
from hub.collaboration.room_runtime import RoomRuntime
from tests.test_room_event_migration import legacy_database
from tests.test_room_message_migration import populated_messages


def sql_literal(function, prefix):
    """Exercise the exact business query, including its original scope."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    matches = [node.value for node in ast.walk(tree)
               if isinstance(node, ast.Constant) and isinstance(node.value, str)
               and node.value.startswith(prefix)]
    assert len(matches) == 1, (function.__name__, prefix)
    return matches[0]


def query_work(db, sql, arguments):
    instructions = [0]
    def progress():
        instructions[0] += 1
        return 0
    db.set_progress_handler(progress, 1)
    try:
        rows = db.execute(sql, arguments).fetchall()
    finally:
        db.set_progress_handler(None, 0)
    return rows, instructions[0]


def assert_bounded(small, large):
    for name, (rows, steps) in small.items():
        assert large[name][0] == rows, name
        # Ten times as much retained history may change B-tree depth, but
        # must not multiply VM work for an unchanged lookup result.
        assert large[name][1] < steps * 2 + 100, (name, steps, large[name][1])


def test_room_message_queries_keep_scope_and_bounded_work_as_history_grows():
    db = populated_messages()
    try:
        with db:
            migrate_messages(db)
            db.execute("INSERT INTO conversation_rooms VALUES ('noise-room')")
            for identifier, partition in [('recent-native', None), ('recent-legacy', 'partition')]:
                db.execute('''INSERT INTO collaboration_messages
                    (id,room_id,conversation_id,author,origin,source_id,kind,body,state,created)
                    VALUES (?,?,'legacy-room','owner','panel_owner',?,'text','{}','saved',1000)''',
                    (identifier, partition, identifier))
            db.execute("UPDATE collaboration_messages SET conversation_sequence=1000000000 WHERE id='recent-native'")
        trigger = db.execute("SELECT sql FROM sqlite_master WHERE name='collaboration_message_insert'").fetchone()[0]
        maximum = re.search(r'conversation_sequence=\((SELECT.*?id!=NEW\.id)\),', trigger, re.S)
        assert maximum
        maximum = maximum[1].replace('NEW.conversation_id', ':conversation_id').replace(
            'NEW.room_id', ':room_id').replace('NEW.id', ':new_id')
        identity = sql_literal(RoomRuntime.send, 'SELECT * FROM collaboration_messages WHERE conversation_id=?')
        rate = sql_literal(RoomRuntime.send, 'SELECT COUNT(*) AS n FROM collaboration_messages')
        page = sql_literal(RoomRuntime.read, 'SELECT * FROM collaboration_messages WHERE conversation_id=? AND conversation_sequence<?')
        queries = {
            'next_conversation_sequence': (maximum, {'conversation_id': 'legacy-room', 'room_id': None, 'new_id': 'next-unsaved'}),
            'new_message_identity': (identity, ('legacy-room', 'owner', 'next-client-message')),
            'native_message_identity': (identity, ('legacy-room', 'owner', 'recent-native')),
            'legacy_message_identity': (identity, ('legacy-room', 'owner', 'recent-legacy')),
            'owner_recent_message_count': (rate, ('legacy-room', 'owner', 900)),
            'latest_room_page': (page, ('legacy-room', 9223372036854775807, 1)),
        }
        def seed(start, end):
            # Populate already assigned historical sequences directly. The
            # migration reinstalls the real trigger before every measurement;
            # trigger assignment semantics have their own migration tests.
            with db:
                db.execute('DROP TRIGGER collaboration_message_insert')
                db.executemany('''INSERT INTO collaboration_messages
                    (id,room_id,conversation_id,author,origin,source_id,kind,body,state,created,
                     server_sequence,conversation_sequence)
                    VALUES (?,?,?,'owner','panel_owner',?,'text','{}','saved',1,?,?)''',
                    ((f'history-{n}', 'partition' if n % 3 else None,
                      'legacy-room' if n % 2 else 'noise-room', f'history-{n}', n + 1000, n + 1000)
                     for n in range(start, end)))
                migrate_messages(db)
        def measure():
            return {name: query_work(db, *query) for name, query in queries.items()}
        seed(0, 1000)
        small = measure()
        assert small['next_conversation_sequence'][0] == [(1000000001,)]
        assert small['new_message_identity'][0] == []
        assert [row[0] for row in small['native_message_identity'][0]] == ['recent-native']
        assert [row[0] for row in small['legacy_message_identity'][0]] == ['recent-legacy']
        assert small['owner_recent_message_count'][0] == [(2,)]
        assert [row[0] for row in small['latest_room_page'][0]] == ['recent-native']
        seed(1000, 10000)
        large = measure()
        assert_bounded(small, large)
        with db:
            migrate_messages(db)
            migrate_messages(db)
        assert {name: query_work(db, *query)[0] for name, query in queries.items()} == {
            name: result[0] for name, result in large.items()}
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        db.close()


def test_global_lease_query_ignores_terminal_and_expired_history_cost():
    db = legacy_database()
    try:
        with db:
            migrate_dispatch(db)
            db.execute("UPDATE mcp_event_deliveries SET state='leased',lease_until=1000 WHERE id='delivery'")
        query = sql_literal(EventService.reserve, 'SELECT s.* FROM mcp_event_subscriptions s')
        def seed(start, end):
            with db:
                db.executemany('''INSERT INTO mcp_event_outbox
                    (seq,id,room_id,name,object_id,object_version,data,created)
                    VALUES (?,?,'partition','legacy',?,1,'{}',1)''',
                    ((n + 1000, f'history-event-{n}', f'history-object-{n}') for n in range(start, end)))
                db.executemany('''INSERT INTO mcp_event_deliveries
                    (id,subscription_id,event_id,event_seq,state,lease_until,created)
                    VALUES (?,'subscription',?,?,?,?,1)''',
                    ((f'history-delivery-{n}', f'history-event-{n}', n + 1000,
                      'leased' if n % 100 == 0 else 'accepted' if n % 2 else 'abandoned',
                      1 if n % 100 == 0 else None) for n in range(start, end)))
        seed(0, 1000)
        small = {'global_leases': query_work(db, query, (100,))}
        assert len(small['global_leases'][0]) == 1
        assert small['global_leases'][0][0][0] == 'subscription'
        seed(1000, 10000)
        large = {'global_leases': query_work(db, query, (100,))}
        assert_bounded(small, large)
        with db:
            migrate_dispatch(db)
            migrate_dispatch(db)
        assert query_work(db, query, (100,))[0] == large['global_leases'][0]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        db.close()
