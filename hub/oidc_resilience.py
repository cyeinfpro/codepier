"""Bounded, durable OIDC maintenance. Attempt clocks never renew authority.

Only successful verified UserInfo reconciliation changes fresh_until. Scheduling
and coalesced operational audit state survive Hub restarts and are not Tokens.
"""
from __future__ import annotations

import asyncio
import time

from shared.crypto import digest
from shared.util import DevError

SYNC_INTERVAL = 60
SYNC_BATCH = 32
SYNC_CONCURRENCY = 4
SYNC_LEASE = 120
MAX_BACKOFF = 1800
AUDIT_INTERVAL = 3600


class SyncScheduler:
    def __init__(self, service):
        self.service = service
        self.store = service.store

    def due(self):
        now = time.time()
        # Rank inside each provider BEFORE limiting the global batch. One broken
        # provider cannot occupy all slots while another has eligible identities.
        return self.store.all('''WITH eligible AS (
            SELECT i.*,p.version AS provider_version,u.epoch AS user_epoch,
                   COALESCE(s.attempted_at,0) AS attempted_at,
                   ROW_NUMBER() OVER (PARTITION BY i.provider_id ORDER BY
                       COALESCE(s.attempted_at,0),i.checked_at,i.id) AS provider_rank
            FROM external_identities i
            JOIN oidc_providers p ON p.id=i.provider_id
            JOIN iam_users u ON u.user_id=i.user_id
            LEFT JOIN oidc_sync_state s ON s.identity_id=i.id
            WHERE i.enabled=1 AND p.enabled=1 AND u.active=1
              AND i.upstream_tokens IS NOT NULL AND i.checked_at<?
              AND (s.identity_id IS NULL OR s.next_attempt<=?
                   OR s.provider_version<>p.version
                   OR s.observed_checked_at<>i.checked_at))
            SELECT * FROM eligible ORDER BY provider_rank,attempted_at,checked_at,id LIMIT ?''',
            (now - SYNC_INTERVAL, now, SYNC_BATCH))

    def claim(self, snapshot):
        now = time.time()
        with self.store.transaction():
            current = self.store.one('SELECT * FROM external_identities WHERE id=?', (snapshot['id'],))
            if (not current or not current['enabled']
                    or current['upstream_tokens'] != snapshot['upstream_tokens']
                    or current['checked_at'] != snapshot['checked_at']):
                return None
            previous = self.store.one('SELECT * FROM oidc_sync_state WHERE identity_id=?', (snapshot['id'],))
            generation = digest(snapshot['upstream_tokens'])
            same = bool(previous and previous['credential_hash'] == generation
                        and previous['provider_version'] == snapshot['provider_version']
                        and previous['observed_checked_at'] == snapshot['checked_at'])
            if same and previous['next_attempt'] > now:
                return None
            failures = previous['failures'] if same else 0
            self.store.db.execute('''INSERT INTO oidc_sync_state
                (identity_id,attempted_at,next_attempt,failures,code,credential_hash,provider_version,observed_checked_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(identity_id) DO UPDATE SET
                attempted_at=excluded.attempted_at,next_attempt=excluded.next_attempt,
                failures=excluded.failures,credential_hash=excluded.credential_hash,
                provider_version=excluded.provider_version,observed_checked_at=excluded.observed_checked_at''',
                (snapshot['id'], now, now + SYNC_LEASE, failures, '', generation,
                 snapshot['provider_version'], snapshot['checked_at']))
            return now

    def audit_outcome(self, provider_id, code, *, recovered=False):
        """One summary per provider/error/hour, not one row per user/attempt."""
        now = time.time()
        row = self.store.one('SELECT * FROM oidc_sync_audit WHERE provider_id=? AND code=?', (provider_id, code))
        if row and now - row['emitted_at'] < AUDIT_INTERVAL:
            self.store.db.execute('UPDATE oidc_sync_audit SET suppressed=suppressed+1 WHERE provider_id=? AND code=?', (provider_id, code))
            return
        suppressed = row['suppressed'] if row else 0
        self.store.db.execute('''INSERT INTO oidc_sync_audit VALUES(?,?,?,0)
            ON CONFLICT(provider_id,code) DO UPDATE SET emitted_at=excluded.emitted_at,suppressed=0''',
            (provider_id, code, now))
        # This is instance operational audit, not a user-selected resource scope.
        self.store.audit('oidc-worker', 'oidc.reconcile', provider_id,
                         'ok' if recovered else 'error',
                         {'code': code, 'suppressed': suppressed, 'provider_id': provider_id}, commit=False)

    def finish(self, snapshot, attempt, code):
        now = time.time()
        with self.store.transaction():
            current = self.store.one('SELECT * FROM external_identities WHERE id=?', (snapshot['id'],))
            configured = self.store.one('SELECT version,enabled FROM oidc_providers WHERE id=?', (snapshot['provider_id'],))
            account = self.store.one('SELECT epoch,active FROM iam_users WHERE user_id=?', (snapshot['user_id'],))
            state = self.store.one('SELECT * FROM oidc_sync_state WHERE identity_id=?', (snapshot['id'],))
            if (not current or current['upstream_tokens'] != snapshot['upstream_tokens']
                    or not configured or not configured['enabled'] or configured['version'] != snapshot['provider_version']
                    or not account or not account['active'] or account['epoch'] != snapshot['user_epoch']
                    or not state or state['attempted_at'] != attempt):
                # A new login, token rotation or configuration now owns this
                # identity. Do not overwrite its scheduling/audit generation.
                return
            previous_failures = state['failures']
            failures = min(previous_failures + 1, 31) if code else 0
            delay = min(MAX_BACKOFF, SYNC_INTERVAL * 2 ** min(failures - 1, 5)) if code else SYNC_INTERVAL
            self.store.db.execute('''UPDATE oidc_sync_state SET next_attempt=?,failures=?,code=?,observed_checked_at=?
                WHERE identity_id=? AND attempted_at=?''',
                (now + delay, failures, code, current['checked_at'], snapshot['id'], attempt))
            if code:
                self.audit_outcome(snapshot['provider_id'], code)
            elif previous_failures:
                self.audit_outcome(snapshot['provider_id'], 'OIDC_SYNC_RECOVERED', recovered=True)

    async def run(self):
        semaphore = asyncio.Semaphore(SYNC_CONCURRENCY)
        async def one(snapshot):
            async with semaphore:
                attempt = self.claim(snapshot)
                if attempt is None:
                    return
                code = ''
                try:
                    outcome = await self.service.sync_identity(snapshot['id'])
                    # Non-syncable identities back off rather than monopolizing
                    # the oldest verified timestamp indefinitely.
                    code = outcome or ''
                except DevError as exc:
                    code = exc.code
                except (ValueError, TypeError, KeyError):
                    code = 'OIDC_RECORD_INVALID'
                # Cancellation deliberately keeps the finite lease. It neither
                # reports success nor renews a verified entitlement.
                self.finish(snapshot, attempt, code)
        await asyncio.gather(*(one(row) for row in self.due()))
