"""Durable, independently owned knowledge maintenance with live authorization.

Never called by a read to create/register work. Each refresh is an ordinary
bounded Agent write operation, with the existing delivery authorization fences.
"""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict
import json
import logging
import time
import uuid

from cryptography.fernet import InvalidToken

from hub.principal import Principal, refresh_principal
from shared.crypto import digest
from shared.util import DevError

INTERVAL = 120
MAX_RECORDS = 128
MAX_DEVICE_RECORDS = 32
BATCH = 4
ACTIVE = {'queued', 'running', 'reconnecting', 'cancelling', 'unknown'}
LOGGER = logging.getLogger(__name__)


def key_for(project, args):
    return digest(json.dumps([project['id'], project['device_id'], project['root'],
                              args.get('workspace_id', '')], sort_keys=True))


class KnowledgeMaintenance:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store
        self.task = None

    def exists(self):
        return bool(self.store.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_maintenance'"))

    def migrate(self):
        self.store.execute('''CREATE TABLE IF NOT EXISTS knowledge_maintenance (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, device_id TEXT NOT NULL,
            root TEXT NOT NULL, workspace TEXT NOT NULL, principal TEXT NOT NULL,
            generation TEXT NOT NULL, enabled INTEGER NOT NULL, state TEXT NOT NULL,
            max_files INTEGER NOT NULL, operation_id TEXT NOT NULL, pending_key TEXT NOT NULL,
            next_at REAL NOT NULL, failures INTEGER NOT NULL, last_error TEXT NOT NULL,
            last_success REAL, updated REAL NOT NULL)''')
        if 'build_options' not in {r['name'] for r in self.store.all('PRAGMA table_info(knowledge_maintenance)')}:
            self.store.execute("ALTER TABLE knowledge_maintenance ADD COLUMN build_options TEXT NOT NULL DEFAULT '{}'")
        if 'cursor' not in {r['name'] for r in self.store.all('PRAGMA table_info(knowledge_maintenance)')}:
            self.store.execute("ALTER TABLE knowledge_maintenance ADD COLUMN cursor TEXT NOT NULL DEFAULT ''")

    def guard(self, maintenance):
        if maintenance is None:
            return
        identifier, generation, pending_key = maintenance
        row = self.store.one('SELECT * FROM knowledge_maintenance WHERE id=?', (identifier,))
        if (not row or not row['enabled'] or row['generation'] != generation
                or row['pending_key'] != pending_key):
            raise DevError('KNOWLEDGE_MAINTENANCE_CHANGED', '自动维护配置已变化；本轮未提交', 409)

    def admitted(self, name, args, project, principal, operation_id, maintenance=None):
        if name != 'project_knowledge_build':
            return
        if maintenance is not None:
            self.guard(maintenance)
            self.store.execute('UPDATE knowledge_maintenance SET operation_id=?,state=?,updated=? WHERE id=?',
                               (operation_id, 'refreshing', time.time(), maintenance[0]))
            return
        enabled = args.get('auto_refresh')
        if enabled is None:
            return
        if enabled and not principal.grant_id:
            # Legacy panel/admin snapshots lack a durably refreshable grant.
            # Preserve manual build compatibility, but never retain authority.
            return
        self.migrate()
        identifier = key_for(project, args)
        old = self.store.one('SELECT id FROM knowledge_maintenance WHERE id=?', (identifier,))
        if not old and not enabled:
            return
        if not old and self.store.one('SELECT count(*) AS n FROM knowledge_maintenance')['n'] >= MAX_RECORDS:
            raise DevError('KNOWLEDGE_MAINTENANCE_LIMIT', '自动维护登记已达上限；本次未提交', 409)
        if enabled and self.store.one(
                'SELECT count(*) AS n FROM knowledge_maintenance WHERE device_id=? AND enabled=1 AND id<>?',
                (project['device_id'], identifier))['n'] >= MAX_DEVICE_RECORDS:
            raise DevError('KNOWLEDGE_DEVICE_MAINTENANCE_LIMIT', '单设备自动维护已达 32 张地图上限；本次未提交', 409)
        # This is a standing grant-bound task, not an OAuth token refresh. Keep
        # the originating identity/epoch and grant; never select another grant.
        value = asdict(principal)
        value['scopes'] = sorted(value['scopes'])
        value['token_hash'] = value['session_hash'] = ''
        now = time.time()
        build_options = {name: args[name] for name in ('paths', 'exclude_paths', 'batch_size', 'time_budget_seconds') if name in args}
        self.store.execute('''INSERT OR REPLACE INTO knowledge_maintenance
            (id,project_id,device_id,root,workspace,principal,generation,enabled,state,max_files,
             operation_id,pending_key,next_at,failures,last_error,last_success,updated,build_options,cursor)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (identifier, project['id'], project['device_id'], project['root'], args.get('workspace_id', ''),
             self.store.encrypt(json.dumps(value)), uuid.uuid4().hex, int(enabled),
             'initializing' if enabled else 'disabled', args.get('max_files', 0), operation_id, '',
             now + INTERVAL, 0, '', None, now, json.dumps(build_options), args.get('cursor', '')))

    def status(self, project, args):
        if not self.exists():
            return {'state': 'disabled', 'enabled': False, 'interval_seconds': INTERVAL}
        row = self.store.one('SELECT * FROM knowledge_maintenance WHERE id=?', (key_for(project, args),))
        if not row:
            return {'state': 'disabled', 'enabled': False, 'interval_seconds': INTERVAL}
        return {'state': row['state'], 'enabled': bool(row['enabled']),
                'interval_seconds': INTERVAL, 'next_check_at': row['next_at'],
                'last_success_at': row['last_success'], 'last_error': row['last_error'],
                'operation_id': row['operation_id'],
                'freshness': 'Bounded polling; queued work and backoff may delay refresh.'}

    async def start(self):
        # Lifecycle, not a query, owns schema and the independent task.
        await self.store.run(self.migrate)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run(), name='knowledge-maintenance')

    async def stop(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None

    def due(self):
        return self.store.all('''SELECT id FROM knowledge_maintenance
            WHERE enabled=1 AND next_at<=? ORDER BY next_at,id LIMIT ?''', (time.time(), BATCH))

    def update(self, row, state, *, error='', delay=INTERVAL, success=False, disable=False, success_at=None):
        now = time.time()
        self.store.execute('''UPDATE knowledge_maintenance SET state=?,last_error=?,next_at=?,
            failures=?,last_success=?,enabled=?,updated=? WHERE id=? AND generation=?''',
            (state, error, now + delay, 0 if success else row['failures'] + bool(error),
             (now if success_at is None else success_at) if success else row['last_success'], 0 if disable else row['enabled'], now,
             row['id'], row['generation']))

    def prepare(self, identifier):
        row = self.store.one('SELECT * FROM knowledge_maintenance WHERE id=?', (identifier,))
        if not row or not row['enabled'] or row['next_at'] > time.time():
            return None
        try:
            data = json.loads(self.store.decrypt(row['principal']))
            data['scopes'] = set(data['scopes'])
            if not data.get('grant_id'):
                raise DevError('KNOWLEDGE_GRANT_REQUIRED', '自动维护需要可持续复核的原授权 grant', 403)
            if not self.store.one('SELECT id FROM users WHERE id=?', (data['user_id'],)):
                raise DevError('KNOWLEDGE_SUBJECT_REMOVED', '自动维护的原用户已不存在', 403)
            # A persisted panel/admin bit must never bypass live grant refresh,
            # including legacy installations without IAM migrations enabled.
            data['admin'] = data['instance_admin'] = False
            principal = refresh_principal(self.store, Principal(**data))
            args = {**json.loads(row['build_options']), 'project': row['project_id'], 'max_files': row['max_files'],
                    'auto_refresh': None, 'incremental': True,
                    'cursor': row['cursor'],
                    'idempotency_key': row['pending_key'] or 'knowledge-auto-' + uuid.uuid4().hex}
            if row['workspace']:
                args['workspace_id'] = row['workspace']
            # Reuse the complete public validation/room/role/project pathway.
            prepared = self.runtime._invoke('project_knowledge_build', args, principal)
            project = prepared.project
            if project['root'] != row['root'] or project['device_id'] != row['device_id']:
                raise DevError('KNOWLEDGE_MAPPING_CHANGED', '项目映射已变化，请显式重新生成', 409)
            if row['operation_id']:
                operation = self.store.one('SELECT state,result,updated FROM operations WHERE id=?', (row['operation_id'],))
                if not operation or operation['state'] in {'needs_review', 'interrupted'}:
                    self.update(row, 'needs_review', error='KNOWLEDGE_OUTCOME_UNCONFIRMED', disable=True)
                    return None
                if operation['state'] in ACTIVE:
                    self.update(row, row['state'])
                    return None
                if operation['state'] != 'succeeded':
                    if row['state'] == 'initializing':
                        self.update(row, 'initial_build_failed', error='KNOWLEDGE_INITIAL_BUILD_FAILED', disable=True)
                        return None
                    self.store.execute("UPDATE knowledge_maintenance SET operation_id='',pending_key='' WHERE id=? AND generation=?", (row['id'], row['generation']))
                    self.update(row, 'backoff', error='KNOWLEDGE_REFRESH_FAILED',
                                delay=min(3600, INTERVAL * 2 ** min(row['failures'] + 1, 5)))
                    return None
                self.store.execute("UPDATE knowledge_maintenance SET operation_id='',pending_key='' WHERE id=? AND generation=?", (row['id'], row['generation']))
                outcome = json.loads(operation['result'] or '{}').get('data', {})
                if outcome.get('state') == 'building':
                    args['cursor'] = outcome['cursor']
                    continuation = outcome.get('continuation', {})
                    if continuation.get('entries'):
                        args['entries'] = continuation['entries']
                    build_options = json.loads(row['build_options'])
                    build_options['entries'] = args.get('entries', [])
                    self.store.execute('UPDATE knowledge_maintenance SET cursor=?,build_options=? WHERE id=? AND generation=?',
                        (args['cursor'], json.dumps(build_options), row['id'], row['generation']))
                    self.update(row, 'building', delay=0)
                else:
                    args['cursor'] = ''
                    args.pop('entries', None)
                    build_options = json.loads(row['build_options'])
                    build_options.pop('entries', None)
                    self.store.execute("UPDATE knowledge_maintenance SET cursor='',build_options=? WHERE id=? AND generation=?",
                        (json.dumps(build_options), row['id'], row['generation']))
                    self.update(row, 'watching', success=True, success_at=operation['updated'])
                row = {**row, 'operation_id': '', 'pending_key': '', 'failures': 0,
                       'last_success': row['last_success'] if outcome.get('state') == 'building' else operation['updated']}
                args['idempotency_key'] = 'knowledge-auto-' + uuid.uuid4().hex
                prepared = self.runtime._invoke('project_knowledge_build', args, principal)
            if not self.runtime.online(project['device_id']):
                self.update(row, 'offline')
                return None
            # Do not add maintenance behind active user work on this device.
            if self.store.one("SELECT 1 FROM operations WHERE device_id=? AND state IN ('queued','running','reconnecting','cancelling') LIMIT 1", (project['device_id'],)):
                self.update(row, 'deferred_busy')
                return None
            self.store.execute('UPDATE knowledge_maintenance SET pending_key=?,state=?,next_at=? WHERE id=? AND generation=?',
                               (args['idempotency_key'], 'submitting', time.time() + INTERVAL, row['id'], row['generation']))
            return prepared.arguments, project, principal, (row['id'], row['generation'], args['idempotency_key'])
        except (InvalidToken, ValueError, TypeError, KeyError):
            # One corrupt registration must not starve the bounded due batch.
            # Database errors still propagate to lifecycle retry, not row repair.
            self.update(row, 'paused', error='KNOWLEDGE_REGISTRATION_INVALID', disable=True)
            return None
        except DevError as exc:
            # Authorization/mapping errors require an explicit new build to resume.
            self.update(row, 'paused', error=exc.code, disable=True)
            return None

    async def tick(self):
        for row in await self.store.run(self.due):
            prepared = await self.store.run(self.prepare, row['id'])
            if prepared:
                args, project, principal, guard = prepared
                try:
                    await self.runtime.dispatch('project_knowledge_build', args, project, principal,
                                                background=True, maintenance=guard)
                except DevError as exc:
                    def failed(code=exc.code):
                        current = self.store.one('SELECT * FROM knowledge_maintenance WHERE id=?', (guard[0],))
                        if current and current['generation'] == guard[1]:
                            self.update(current, 'backoff', error=code, delay=INTERVAL * 2)
                    await self.store.run(failed)

    async def run(self):
        while True:
            try:
                await self.tick()
            except Exception as exc:
                # Do not disclose source paths/credentials in diagnostics, or
                # turn an unknown dispatch outcome into a new operation key.
                LOGGER.warning('Knowledge maintenance cycle failed (%s)', type(exc).__name__)
            await asyncio.sleep(15)
