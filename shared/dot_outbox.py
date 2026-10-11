"""Opt-in host adapter for durable Dot publications, never an execution queue.

A host must instantiate this adapter in its own persistent storage. Availability
of this source file does not prove that any external host has installed it.
Encryption is supplied by that host; no MCP credential is stored or created here.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Awaitable, Callable

from shared.collaboration_contracts import DotChatMessage, DotChatMessageV2, DotReplyUpdate
from shared.util import DevError

TERMINAL = frozenset({'committed', 'attention'})
PERMANENT = frozenset({'INVALID_TOKEN', 'FORBIDDEN', 'INSUFFICIENT_SCOPE', 'ROLE_POLICY_DENIED',
    'IDEMPOTENCY_CONFLICT', 'DOT_RECEIPT_SCOPE_CHANGED', 'DOT_CHAT_NOT_APPROVED',
    'DOT_NOT_FOUND', 'ROOM_SCOPE_REVOKED', 'ROOM_SCOPE_CHANGED', 'WORKER_BINDING_INVALID'})


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256((value if isinstance(value, str) else canonical(value)).encode()).hexdigest()


class DotOutbox:
    """SQLite FULL/WAL, encrypted immutable bodies, exact-fence single-flight.

    A callback must honor cancellation and must not implement a hidden resend.
    Every uncertain send transitions to receipt lookup, never to a fresh key.
    Errors/expiry retain the body for explicit review rather than deleting work.
    """
    def __init__(self, path: Path, *, seal: Callable[[str], str], unseal: Callable[[str], str],
                 clock=time.time, max_entries=1024, max_bytes=16 * 1024 * 1024):
        if not callable(seal) or not callable(unseal):
            raise ValueError('The host must supply its own persistent encryption provider')
        if type(max_entries) is not int or not 1 <= max_entries <= 100000 or type(max_bytes) is not int or max_bytes < 1024:
            raise ValueError('Invalid outbox quota')
        path = Path(path).expanduser().absolute()
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('Outbox storage must not be a symbolic link')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.seal, self.unseal, self.clock = seal, unseal, clock
        self.max_entries, self.max_bytes = max_entries, max_bytes
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS dot_outbox_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS dot_outbox_entries(
            id TEXT PRIMARY KEY, lane TEXT NOT NULL, identity_digest TEXT NOT NULL,
            dot_id TEXT NOT NULL, kind TEXT NOT NULL, request_key_hash TEXT NOT NULL,
            request_digest TEXT NOT NULL, body TEXT NOT NULL, byte_count INTEGER NOT NULL,
            state TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,
            claim_owner TEXT,claim_fence INTEGER,claim_until REAL,
            receipt TEXT,error_code TEXT,updated REAL NOT NULL,
            UNIQUE(lane,kind,request_key_hash));
          CREATE TABLE IF NOT EXISTS dot_outbox_lanes(
            id TEXT PRIMARY KEY,owner TEXT NOT NULL,fence INTEGER NOT NULL,until REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS dot_outbox_pending ON dot_outbox_entries(lane,state,next_attempt,created,id);
        ''')
        self.db.execute('INSERT OR IGNORE INTO dot_outbox_meta VALUES (?,?)', ('storage_id', uuid.uuid4().hex))
        os.chmod(path, 0o600)
        self.storage_id = self.db.execute("SELECT value FROM dot_outbox_meta WHERE key='storage_id'").fetchone()[0]

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:
                yield
            except BaseException:
                self.db.execute('ROLLBACK')
                raise
            else:
                self.db.execute('COMMIT')

    def close(self):
        with self.lock:
            self.db.close()

    @staticmethod
    def identity(value):
        required = {'connection_id', 'grant_id', 'space_id', 'owner_user_id', 'conversation_id'}
        if (not isinstance(value, dict) or set(value) != required
                or any(not isinstance(v, str) or not 1 <= len(v) <= 256 for v in value.values())):
            raise ValueError('Persist the exact authenticated connection and conversation identity')
        return dict(value)

    def enqueue(self, identity: dict, kind: str, arguments: dict, *, retention_seconds=30 * 86400):
        identity = self.identity(identity)
        model = {'message': DotChatMessage, 'message_v2': DotChatMessageV2, 'update': DotReplyUpdate}.get(kind)
        if model is None:
            raise ValueError('An outbox only publishes a message or update; it never executes a task')
        args = model.model_validate(arguments).model_dump()
        if type(retention_seconds) is not int or not 60 <= retention_seconds <= 365 * 86400:
            raise ValueError('Invalid retention deadline')
        body = canonical({'schema_version': 1, 'identity': identity, 'kind': kind, 'arguments': args})
        size = len(body.encode())
        if size > 1024 * 1024:
            raise DevError('DOT_OUTBOX_BODY_LIMIT', '完整投递正文超过发件箱单项上限', 413)
        identity_digest = digest(identity)
        lane = digest([identity['connection_id'], identity['grant_id'], args['dot_id']])
        key_hash, request_digest = digest(args['idempotency_key']), digest(args)
        now = self.clock()
        with self.transaction():
            old = self.db.execute('SELECT * FROM dot_outbox_entries WHERE lane=? AND kind=? AND request_key_hash=?',
                                  (lane, kind, key_hash)).fetchone()
            if old:
                if old['request_digest'] != request_digest or old['identity_digest'] != identity_digest:
                    raise DevError('IDEMPOTENCY_CONFLICT', '原投递键已绑定完整正文和身份，不能覆盖', 409)
                return self.public(old)
            count, used = self.db.execute('SELECT COUNT(*),COALESCE(SUM(byte_count),0) FROM dot_outbox_entries').fetchone()
            if count >= self.max_entries or used + size > self.max_bytes:
                raise DevError('DOT_OUTBOX_QUOTA', '持久发件箱已满；先明确整理已确认项，未投递正文不会被自动删除', 409)
            identifier = uuid.uuid4().hex
            self.db.execute('''INSERT INTO dot_outbox_entries
                (id,lane,identity_digest,dot_id,kind,request_key_hash,request_digest,body,byte_count,state,created,expires,updated)
                VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?,?)''',
                (identifier, lane, identity_digest, args['dot_id'], kind, key_hash, request_digest,
                 self.seal(body), size, now, now + retention_seconds, now))
            return self.public(self.db.execute('SELECT * FROM dot_outbox_entries WHERE id=?', (identifier,)).fetchone())

    @staticmethod
    def public(row):
        return {k: row[k] for k in ('id', 'dot_id', 'kind', 'state', 'request_digest', 'created', 'updated',
                                    'expires', 'attempts', 'next_attempt', 'error_code')}

    def list(self, *, after='', limit=50):
        if not isinstance(after, str) or len(after) > 128 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('Invalid outbox page')
        with self.lock:
            rows = self.db.execute('SELECT * FROM dot_outbox_entries WHERE id>? ORDER BY id LIMIT ?', (after, limit + 1)).fetchall()
            return {'items': [self.public(r) for r in rows[:limit]],
                    'next_cursor': rows[limit - 1]['id'] if len(rows) > limit else None,
                    'storage_id': self.storage_id, 'read_only': True}

    def _claim(self, identifier, seconds):
        now, owner = self.clock(), uuid.uuid4().hex
        with self.transaction():
            row = self.db.execute('SELECT * FROM dot_outbox_entries WHERE id=?', (identifier,)).fetchone()
            if row is None or row['state'] in TERMINAL or row['next_attempt'] > now:
                return None
            if row['expires'] <= now:
                self.db.execute("UPDATE dot_outbox_entries SET state='attention',error_code='RETENTION_EXPIRED',updated=? WHERE id=?", (now, identifier))
                return None
            active = self.db.execute('SELECT * FROM dot_outbox_lanes WHERE id=?', (row['lane'],)).fetchone()
            if active is not None and active['until'] > now:
                return None
            fence = (active['fence'] if active else 0) + 1
            self.db.execute('INSERT INTO dot_outbox_lanes VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET owner=excluded.owner,fence=excluded.fence,until=excluded.until',
                            (row['lane'], owner, fence, now + seconds))
            self.db.execute("UPDATE dot_outbox_entries SET state='checking_receipt',claim_owner=?,claim_fence=?,claim_until=?,updated=? WHERE id=?",
                            (owner, fence, now + seconds, now, identifier))
            return dict(row) | {'claim_owner': owner, 'claim_fence': fence, 'claim_until': now + seconds}

    def _current(self, claim):
        row = self.db.execute('SELECT * FROM dot_outbox_lanes WHERE id=?', (claim['lane'],)).fetchone()
        return bool(row and row['owner'] == claim['claim_owner'] and row['fence'] == claim['claim_fence'] and row['until'] > self.clock())

    def _state(self, claim, state, *, error=None, receipt=None, delay=0, sending=False):
        with self.transaction():
            if not self._current(claim):
                raise DevError('DOT_OUTBOX_FENCE_LOST', '原发件箱发送世代已失效；先核对回执，不重放', 409)
            self.db.execute('''UPDATE dot_outbox_entries SET state=?,error_code=?,receipt=COALESCE(?,receipt),
                next_attempt=?,updated=?,attempts=attempts+? WHERE id=? AND claim_owner=? AND claim_fence=?''',
                (state, error, self.seal(canonical(receipt)) if receipt is not None else None,
                 self.clock() + delay, self.clock(), int(sending), claim['id'], claim['claim_owner'], claim['claim_fence']))

    @staticmethod
    def _receipt_matches(claim, receipt):
        return (isinstance(receipt, dict) and receipt.get('committed') is True
                and receipt.get('request_digest') == claim['request_digest']
                and receipt.get('dot_id') == claim['dot_id'] and receipt.get('kind') == claim['kind']
                and isinstance(receipt.get('message_id'), str) and bool(receipt['message_id'])
                and type(receipt.get('committed_version')) is int and receipt['committed_version'] >= 1)

    async def deliver(self, identifier: str, *, authorize: Callable[[dict], Awaitable[None]],
                      lookup: Callable[[dict], Awaitable[dict]], send: Callable[[dict], Awaitable[dict]],
                      timeout_seconds=30):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 120:
            raise ValueError('Invalid delivery timeout')
        claim = self._claim(identifier, timeout_seconds + 15)
        if claim is None:
            return {'id': identifier, 'claimed': False}
        try:
            envelope = json.loads(self.unseal(claim['body']))
            if (envelope.get('schema_version') != 1 or digest(envelope['arguments']) != claim['request_digest']
                    or digest(envelope['identity']) != claim['identity_digest']):
                raise DevError('DOT_OUTBOX_CORRUPT', '持久正文摘要或身份不匹配', 409)
            async with asyncio.timeout(timeout_seconds):
                await authorize(envelope)
                receipt = await lookup(envelope)
                if not isinstance(receipt, dict):
                    raise DevError('DOT_OUTBOX_RECEIPT_INVALID', '投递查询没有返回结构化回执，未重发正文', 502)
                if self._receipt_matches(claim, receipt):
                    self._state(claim, 'committed', receipt=receipt)
                elif receipt.get('committed') is True:
                    self._state(claim, 'attention', error='RECEIPT_CONTENT_UNVERIFIED')
                elif (receipt.get('receipt_state') == 'not_committed'
                      and receipt.get('authoritative_lookup') is True
                      and receipt.get('safe_to_resend_same_request') is True):
                    if claim['attempts'] >= 6:
                        self._state(claim, 'attention', error='RETRY_BUDGET_EXHAUSTED')
                        with self.lock:
                            return self.public(self.db.execute('SELECT * FROM dot_outbox_entries WHERE id=?', (identifier,)).fetchone())
                    # Recheck current permissions immediately before the only send.
                    await authorize(envelope)
                    self._state(claim, 'sending', sending=True)
                    await send(envelope)
                    # A transport response is not proof that this exact body was
                    # committed. Read the authoritative, digest-bound receipt.
                    receipt = await lookup(envelope)
                    if self._receipt_matches(claim, receipt):
                        self._state(claim, 'committed', receipt=receipt)
                    else:
                        self._state(claim, 'checking_receipt', error='COMMIT_UNCONFIRMED', delay=5)
                else:
                    self._state(claim, 'checking_receipt', error='RECEIPT_UNCERTAIN', delay=5)
        except asyncio.CancelledError:
            self._state(claim, 'checking_receipt', error='HOST_INTERRUPTED', delay=5)
            raise
        except (TimeoutError, OSError):
            self._state(claim, 'checking_receipt', error='TRANSPORT_UNCERTAIN', delay=min(300, 5 * 2 ** min(claim['attempts'], 6)))
        except DevError as exc:
            self._state(claim, 'attention' if exc.code in PERMANENT or exc.status in {401, 403, 409, 422} else 'checking_receipt',
                        error=exc.code, delay=30)
        except (ValueError, KeyError, TypeError):
            self._state(claim, 'attention', error='INVALID_RECEIPT_OR_STORAGE')
        finally:
            with self.transaction():
                self.db.execute('UPDATE dot_outbox_lanes SET until=0 WHERE id=? AND owner=? AND fence=?',
                                (claim['lane'], claim['claim_owner'], claim['claim_fence']))
        with self.lock:
            return self.public(self.db.execute('SELECT * FROM dot_outbox_entries WHERE id=?', (identifier,)).fetchone())

    def purge_committed(self, identifiers: list[str], *, expected_digests: dict[str, str]):
        """An explicit host/user housekeeping action, never a retention timer."""
        if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 100 or len(set(identifiers)) != len(identifiers):
            raise ValueError('Select 1–100 exact committed records')
        with self.transaction():
            for identifier in identifiers:
                row = self.db.execute('SELECT * FROM dot_outbox_entries WHERE id=?', (identifier,)).fetchone()
                if row is None or row['state'] != 'committed' or expected_digests.get(identifier) != row['request_digest']:
                    raise DevError('DOT_OUTBOX_PURGE_DENIED', '只能明确清理摘要匹配的已确认投递，不能删除未确认正文', 409)
            self.db.executemany('DELETE FROM dot_outbox_entries WHERE id=?', [(i,) for i in identifiers])
        return {'purged': len(identifiers)}
