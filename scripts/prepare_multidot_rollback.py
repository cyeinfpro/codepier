#!/usr/bin/env python3
"""Offline pre-rollback admission freeze; never restore an older database.

Dry-run opens SQLite read-only and prints a reviewable plan. Apply requires the
exact plan hash, unchanged source/database markers and explicit operator
confirmation that every Hub writer has stopped. SQLite locks alone do not prove
that another Hub process is offline. Existing operations, leases, resource locks,
messages, results and protocol ledgers are never cancelled, replayed or deleted.

This checks guards also present in baseline 3f004f54 and freezes native v2
subscriptions that its dispatcher cannot parse. The command does not start an
old binary. Never run the downgraded binary's rekey/key-maintenance commands:
its cipher catalog omits v2 secrets; return to compatible forward code first.

A source ZIP without .git requires a source proof exported from the reviewed Git
checkout plus its externally known SHA256. This verifies a fixed source inventory,
not a signature, trust endorsement or authorization to deploy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time

FORMAT = 'codepier.multidot.rollback-freeze.v1'
SOURCE_PROOF_FORMAT = 'codepier.multidot.rollback-source-proof.v1'
BASELINE = '3f004f54be61d34e93080e9edda07aeb1a1adb08'
V2_EVENT_NAMES = ('codepier.collaboration.inbox_dirty.v2',)
MAX_ROWS = 10000
SOURCE_FILES = (
    'scripts/prepare_multidot_rollback.py', 'hub/store.py',
    'hub/collaboration/schema.py', 'hub/collaboration/joining.py',
    'hub/collaboration/delegation.py', 'hub/collaboration/coordination.py',
    'hub/collaboration/dot_mailbox.py', 'hub/collaboration/team_collaboration.py',
)
REQUIRED_COLUMNS = {
    'meta': {'key', 'value'},
    'collaboration_rooms': {'id', 'space_id', 'owner_user_id'},
    'collaboration_join_slots': {'id', 'room_id', 'grant_id', 'state', 'version', 'updated'},
    'collaboration_dots': {'id', 'room_id', 'conversation_id'},
    'dot_mailbox_bindings': {'dot_id', 'room_id', 'grant_id', 'generation', 'state'},
    'delegation_policies': {'id', 'room_id', 'slot_id', 'version', 'state', 'updated'},
    'delegation_requests': {'id', 'policy_id', 'goal_id'},
    'collaboration_teams': {'id', 'room_id', 'policy', 'policy_digest', 'version'},
    'team_message_bindings': {'message_id', 'team_id', 'source_version'},
    'team_goals': {'goal_id', 'team_id', 'coordinator_epoch', 'intent_revision'},
    'coordination_goals': {'id', 'room_id', 'state', 'version', 'updated'},
    'coordination_operations': {'operation_id', 'goal_id', 'work_item_id'},
    'operations': {'id', 'state', 'output_seq'},
    'mcp_event_subscriptions': {'id', 'room_id', 'grant_id', 'name', 'state', 'version', 'updated', 'expires_at'},
}
CHANGE_TABLES = ('mcp_event_subscriptions', 'collaboration_join_slots', 'delegation_policies', 'coordination_goals')
MANIFEST_TABLE = 'multidot_rollback_manifests'


class FreezeError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def hex_digest(value, size=64):
    return isinstance(value, str) and len(value) == size and all(
        char in '0123456789abcdef' for char in value)


def source_files(root):
    files = {}
    for name in SOURCE_FILES:
        path = root / name
        try:
            if path.resolve(strict=True) != path or not path.is_file():
                raise FreezeError('Source file must be a regular fixed path without symlinks: ' + name)
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise FreezeError('Source file is missing or unreadable: ' + name) from exc
    return files


def source_stamp(root=None):
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    try:
        top = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=root, check=True,
                             capture_output=True, text=True, timeout=5).stdout.strip()
        if Path(top).resolve() != root:
            raise FreezeError('Source root is not the exact Git checkout; an archive requires '
                              '--source-proof and --expected-source-proof-sha256')
        revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, check=True,
                                  capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise FreezeError('Exact Git source identity is unavailable; an archive requires '
                          '--source-proof and --expected-source-proof-sha256') from exc
    if not hex_digest(revision, 40):
        raise FreezeError('Source checkout has no exact revision')
    files = source_files(root)
    return {'revision': revision, 'files': files, 'fingerprint': digest([revision, files])}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise FreezeError('Source proof contains a duplicate JSON key')
        result[key] = value
    return result


def checked_source(root=None, *, source_proof=None, expected_source_proof_sha256=None):
    """Match an externally pinned source inventory; this is not a signature."""
    root = Path(root or Path(__file__).resolve().parents[1]).resolve()
    if source_proof is None and expected_source_proof_sha256 is None:
        return source_stamp(root)
    if not source_proof or not hex_digest(expected_source_proof_sha256):
        raise FreezeError('Source proof requires an externally known exact file SHA256')
    try:
        with Path(source_proof).open('rb') as stream:
            data = stream.read(65537)
    except OSError as exc:
        raise FreezeError('Source proof is missing or unreadable') from exc
    if len(data) > 65536 or hashlib.sha256(data).hexdigest() != expected_source_proof_sha256:
        raise FreezeError('Source proof file digest/size does not match the reviewed proof')
    try:
        proof = json.loads(data, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as exc:
        raise FreezeError('Source proof is not valid unambiguous JSON') from exc
    if (not isinstance(proof, dict) or set(proof) != {'format', 'source'}
            or proof['format'] != SOURCE_PROOF_FORMAT):
        raise FreezeError('Source proof format or fields are unsupported')
    source = proof['source']
    if (not isinstance(source, dict) or set(source) != {'revision', 'files', 'fingerprint'}
            or not hex_digest(source['revision'], 40)
            or not isinstance(source['files'], dict)
            or set(source['files']) != set(SOURCE_FILES)
            or not all(hex_digest(value) for value in source['files'].values())
            or source['fingerprint'] != digest([source['revision'], source['files']])):
        raise FreezeError('Source proof revision, fixed file inventory or fingerprint is invalid')
    if source_files(root) != source['files']:
        raise FreezeError('Source files changed from the reviewed source proof')
    own_sha256 = hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()
    if source['files']['scripts/prepare_multidot_rollback.py'] != own_sha256:
        raise FreezeError('The executing rollback script differs from the reviewed source proof')
    if ((root / '.git').exists() or (root / '.git').is_symlink()) and source_stamp(root) != source:
        raise FreezeError('Source proof differs from the exact Git checkout identity')
    return source


def export_source_proof(path, *, source_root=None):
    proof = {'format': SOURCE_PROOF_FORMAT, 'source': source_stamp(source_root)}
    proof_sha256 = exclusive_export(path, proof)
    return {'state': 'source_proof_exported', 'source_proof': str(Path(path).resolve()),
            'source_proof_sha256': proof_sha256, 'source': proof['source'],
            'limits': 'An externally pinned source inventory, not a signature, trust endorsement '
                      'or deployment authorization. Review the exact source files before export.'}


def connect(database, *, writable=False):
    path = Path(database).resolve(strict=True)
    if not path.is_file():
        raise FreezeError('Database must be an existing SQLite file')
    db = sqlite3.connect(path.as_uri() + ('?mode=rw' if writable else '?mode=ro'),
                         uri=True, timeout=5, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    if not writable:
        db.execute('PRAGMA query_only=ON')
    return db


def rows(db, sql, args=()):
    result = [dict(row) for row in db.execute(sql, args).fetchmany(MAX_ROWS + 1)]
    if len(result) > MAX_ROWS:
        raise FreezeError('Scope exceeds 10000 rows; use a separately reviewed larger migration')
    return result


def selected(db, table, identifiers, *, key='id'):
    if table not in REQUIRED_COLUMNS or key not in REQUIRED_COLUMNS[table]:
        raise FreezeError('Unsupported source table')
    identifiers = sorted(set(identifiers))
    result = rows(db, f'SELECT * FROM {table} WHERE {key} IN (SELECT value FROM json_each(?)) ORDER BY {key}',
                  (canonical(identifiers),))
    if len(result) != len(identifiers):
        raise FreezeError('Scope references a missing source record')
    return result


def marker(db):
    schema = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    if not schema or schema[0] != '10':
        raise FreezeError('Only exact Hub schema 10 with the v2/team tables is supported')
    definitions = {}
    for table, required in REQUIRED_COLUMNS.items():
        columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        if not required <= columns:
            raise FreezeError('Database protocol marker/columns are unsupported: ' + table)
        definitions[table] = db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
    return {'hub_schema': schema[0], 'protocol_schema_sha256': digest(definitions),
            'user_version': db.execute('PRAGMA user_version').fetchone()[0]}


def row_mark(row, key='id'):
    return {key: row[key], 'row_sha256': digest(row)}


def target(row, table):
    state = row['state']
    if table == 'collaboration_join_slots':
        frozen = 'revoked'
    elif table == 'mcp_event_subscriptions':
        frozen = 'revoked' if state == 'active' else state
    else:
        frozen = 'paused' if state in {'active', 'proposed'} else state
    return {'id': row['id'], 'room_id': row['room_id'], 'state': state,
            **({'grant_id': row['grant_id']} if 'grant_id' in row else {}),
            **({'name': row['name'], 'expires_at': row['expires_at']}
               if table == 'mcp_event_subscriptions' else {}),
            'version': row['version'], 'updated': row['updated'],
            'row_sha256': digest(row), 'target_state': frozen}


def plan_on_connection(db, database, source):
    path = Path(database).resolve(strict=True)
    stat = path.stat()
    schema = marker(db)
    bindings = rows(db, "SELECT * FROM dot_mailbox_bindings WHERE state='active' ORDER BY dot_id")
    # The baseline indexes FILTERS[name] before checking the slot. Revoking only
    # slots leaves its dispatcher failing on an unknown v2 event. Select these
    # rows independently of bindings and expiry: orphan or dormant routes must
    # not escape the reviewed scope, and replay must detect later additions.
    subscriptions = rows(db, """SELECT * FROM mcp_event_subscriptions
        WHERE name IN (SELECT value FROM json_each(?)) ORDER BY id""", (canonical(V2_EVENT_NAMES),))
    links = rows(db, 'SELECT * FROM team_goals ORDER BY goal_id')
    sources = rows(db, 'SELECT * FROM team_message_bindings ORDER BY message_id')
    teams = selected(db, 'collaboration_teams',
                     [row['team_id'] for row in links + sources])
    identifiers = {row['dot_id'] for row in bindings}
    for team in teams:
        policy = json.loads(team['policy'])
        members = policy.get('participants')
        if not isinstance(members, list) or not members:
            raise FreezeError('Team participant scope is not verifiable')
        for member in members:
            if not isinstance(member, dict) or not isinstance(member.get('dot_id'), str):
                raise FreezeError('Team participant scope is not verifiable')
            identifiers.add(member['dot_id'])
    slots = selected(db, 'collaboration_join_slots', identifiers)
    dots = selected(db, 'collaboration_dots', identifiers)
    by_slot, by_dot = ({row['id']: row for row in records} for records in (slots, dots))
    for binding in bindings:
        slot = by_slot[binding['dot_id']]
        if slot['room_id'] != binding['room_id'] or slot['grant_id'] != binding['grant_id']:
            raise FreezeError('Active mailbox binding has changed its exact slot scope')
    for team in teams:
        for member in json.loads(team['policy'])['participants']:
            dot, slot = by_dot[member['dot_id']], by_slot[member['dot_id']]
            if dot['room_id'] != team['room_id'] or slot['room_id'] != team['room_id']:
                raise FreezeError('Team participant room is inconsistent')
    policies = rows(db, """SELECT * FROM delegation_policies
        WHERE slot_id IN (SELECT value FROM json_each(?)) ORDER BY id""", (canonical(sorted(identifiers)),))
    requests = rows(db, """SELECT * FROM delegation_requests
        WHERE policy_id IN (SELECT value FROM json_each(?)) ORDER BY id""",
                    (canonical([row['id'] for row in policies]),))
    goals = selected(db, 'coordination_goals',
                     [row['goal_id'] for row in links + requests])
    tracked = rows(db, """SELECT o.id,o.state,o.output_seq,c.goal_id,c.work_item_id
        FROM coordination_operations c JOIN operations o ON o.id=c.operation_id
        WHERE c.goal_id IN (SELECT value FROM json_each(?)) ORDER BY o.id""",
                   (canonical([row['id'] for row in goals]),))
    rooms = selected(db, 'collaboration_rooms',
                     [row['room_id'] for row in slots + goals + subscriptions])
    return {
        'format': FORMAT, 'guard_baseline': BASELINE, 'source': source,
        'database': {'path': str(path), 'device': stat.st_dev, 'inode': stat.st_ino, 'marker': schema},
        'scope': {
            'active_v2_bindings': [row_mark(row, 'dot_id') for row in bindings],
            'team_goal_links': [row_mark(row, 'goal_id') for row in links],
            'selected_team_sources': [row_mark(row, 'message_id') for row in sources],
            'teams': [row_mark(row) for row in teams],
            'dots': [row_mark(row) for row in dots],
            'delegation_requests': [row_mark(row) for row in requests],
            'rooms': [{'id': row['id'], 'space_id': row['space_id'],
                       'owner_user_id': row['owner_user_id'], 'row_sha256': digest(row)} for row in rooms],
        },
        'targets': {table: [target(row, table) for row in records] for table, records in (
            ('mcp_event_subscriptions', subscriptions),
            ('collaboration_join_slots', slots), ('delegation_policies', policies),
            ('coordination_goals', goals))},
        'original_operations_for_read_only_tracking': tracked,
        'recovery': {
            'restore_database_snapshot': False, 'cancel_or_replay_operations': False,
            'preserve_work_fences_locks_results_and_messages': True,
            'preserve_delivery_bodies_tickets_and_receipts': True,
            'downgraded_key_maintenance_allowed': False,
            'resume': 'Restore a compatible forward binary, inspect current original receipts and '
                      'the saved before/after versions, then obtain fresh owner authorization. '
                      'Never automatically reactivate frozen slots/subscriptions or restore a pre-v2 DB snapshot. '
                      'Do not run rekey/key maintenance from the old binary: it does not know the v2 cipher columns.',
            'limits': 'Requires all Hub writers stopped by the operator. This is a tested server-guard '
                      'freeze, not proof that the old binary has been deployed or tested.',
        },
    }


def create_plan(database, *, source_root=None, source_proof=None, expected_source_proof_sha256=None):
    source = checked_source(source_root, source_proof=source_proof,
                            expected_source_proof_sha256=expected_source_proof_sha256)
    db = connect(database)
    try:
        db.execute('BEGIN')
        return plan_on_connection(db, database, source)
    finally:
        db.rollback()
        db.close()


def verify_applied(db, manifest):
    for table, targets in manifest['after'].items():
        for expected in targets:
            current = db.execute(f'SELECT * FROM {table} WHERE id=?', (expected['id'],)).fetchone()
            if not current or digest(dict(current)) != expected['row_sha256']:
                raise FreezeError('An applied freeze changed; do not replay or overwrite it')


def apply_plan(database, plan, *, expected_plan_sha256, hub_stopped, source_root=None,
               source_proof=None, expected_source_proof_sha256=None):
    if hub_stopped is not True:
        raise FreezeError('Explicit confirmation that every Hub writer is stopped is required')
    identifier = digest(plan)
    if identifier != expected_plan_sha256 or plan.get('format') != FORMAT:
        raise FreezeError('Reviewed plan digest/format does not match')
    source = checked_source(source_root, source_proof=source_proof,
                            expected_source_proof_sha256=expected_source_proof_sha256)
    if source != plan['source']:
        raise FreezeError('Source revision or source files changed; regenerate and review the plan')
    db = connect(database, writable=True)
    try:
        db.execute('BEGIN IMMEDIATE')
        path = Path(database).resolve(strict=True)
        stat = path.stat()
        identity = {'path': str(path), 'device': stat.st_dev, 'inode': stat.st_ino, 'marker': marker(db)}
        if identity != plan['database']:
            raise FreezeError('Exact database identity or protocol marker changed')
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                            (MANIFEST_TABLE,)).fetchone()
        if exists:
            old = db.execute(f'SELECT manifest FROM {MANIFEST_TABLE} WHERE plan_sha256=?', (identifier,)).fetchone()
            if old:
                manifest = json.loads(old[0])
                if manifest['plan'] != plan:
                    raise FreezeError('Persisted manifest differs from the reviewed plan')
                verify_applied(db, manifest)
                current = plan_on_connection(db, database, source)
                if (current['scope'] != plan['scope'] or any(
                        {row['id'] for row in current['targets'][table]} !=
                        {row['id'] for row in plan['targets'][table]} for table in CHANGE_TABLES)):
                    raise FreezeError('Frozen scope changed or gained new admissions; regenerate and review a new plan')
                db.rollback()
                return manifest
        current = plan_on_connection(db, database, source)
        if current != plan:
            raise FreezeError('Database marker, binding, source, version or operation receipt changed; regenerate plan')
        applied_at, after = time.time(), {}
        for table in CHANGE_TABLES:
            after[table] = []
            for row in plan['targets'][table]:
                if row['state'] != row['target_state']:
                    changed = db.execute(f"""UPDATE {table} SET state=?,version=version+1,updated=?
                        WHERE id=? AND state=? AND version=? AND updated=?""",
                        (row['target_state'], applied_at, row['id'], row['state'], row['version'], row['updated']))
                    if changed.rowcount != 1:
                        raise FreezeError('Source version changed during atomic freeze')
                saved = dict(db.execute(f'SELECT * FROM {table} WHERE id=?', (row['id'],)).fetchone())
                after[table].append({key: saved[key] for key in ('id', 'state', 'version', 'updated')} |
                                    {'row_sha256': digest(saved)})
        manifest = {'format': FORMAT, 'plan_sha256': identifier, 'applied_at': applied_at,
                    'plan': plan, 'after': after, 'state': 'admission_frozen', 'operations_replayed': 0}
        db.execute(f"""CREATE TABLE IF NOT EXISTS {MANIFEST_TABLE} (
            plan_sha256 TEXT PRIMARY KEY,manifest TEXT NOT NULL,applied_at REAL NOT NULL)""")
        db.execute(f'INSERT INTO {MANIFEST_TABLE} VALUES (?,?,?)',
                   (identifier, canonical(manifest), applied_at))
        db.execute("INSERT INTO meta(key,value) VALUES ('multidot_rollback_freeze',?) "
                   "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (identifier,))
        db.commit()
        return manifest
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def exclusive_export(path, value):
    # Never replace an existing file, especially an administrator's DB/backup.
    payload = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    descriptor = os.open(Path(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return hashlib.sha256(payload).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', help='Existing hub.sqlite3; never a data directory')
    parser.add_argument('--plan-out', help='Dry-run plan export; must not exist')
    parser.add_argument('--apply', metavar='PLAN_JSON', help='Apply an already reviewed exact plan')
    parser.add_argument('--expected-plan-sha256')
    parser.add_argument('--confirm-hub-stopped', action='store_true')
    parser.add_argument('--manifest-out', help='Optional receipt export; must not exist')
    parser.add_argument('--export-source-proof', metavar='PROOF_JSON',
                        help='Export reviewed Git source inventory without opening a DB; must not exist')
    parser.add_argument('--source-proof', metavar='PROOF_JSON',
                        help='Source inventory exported from the reviewed exact Git checkout')
    parser.add_argument('--expected-source-proof-sha256',
                        help='Externally known exact proof-file SHA256; not read from the proof itself')
    args = parser.parse_args(argv)
    if args.export_source_proof:
        if any((args.database, args.plan_out, args.apply, args.expected_plan_sha256,
                args.confirm_hub_stopped, args.manifest_out, args.source_proof,
                args.expected_source_proof_sha256)):
            parser.error('--export-source-proof cannot be combined with database or plan/apply arguments')
        result = export_source_proof(args.export_source_proof)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if not args.database:
        parser.error('--database is required for plan/apply')
    if bool(args.source_proof) != bool(args.expected_source_proof_sha256):
        parser.error('--source-proof and --expected-source-proof-sha256 are required together')
    source_options = {'source_proof': args.source_proof,
                      'expected_source_proof_sha256': args.expected_source_proof_sha256}
    if args.apply:
        if args.plan_out or not args.expected_plan_sha256 or not args.confirm_hub_stopped:
            parser.error('--apply requires --expected-plan-sha256 and --confirm-hub-stopped, without --plan-out')
        if args.manifest_out and Path(args.manifest_out).exists():
            parser.error('--manifest-out must not replace an existing file')
        plan = json.loads(Path(args.apply).read_text(encoding='utf-8'))
        result = apply_plan(args.database, plan, expected_plan_sha256=args.expected_plan_sha256,
                            hub_stopped=args.confirm_hub_stopped, **source_options)
        if args.manifest_out:
            exclusive_export(args.manifest_out, result)
    else:
        if args.expected_plan_sha256 or args.confirm_hub_stopped or args.manifest_out:
            parser.error('Apply-only arguments require --apply')
        plan = create_plan(args.database, **source_options)
        result = {'state': 'dry_run', 'plan_sha256': digest(plan), 'plan': plan}
        if args.plan_out:
            exclusive_export(args.plan_out, plan)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
