"""Durable coordination for authorized, Hub-native VPS readiness.

This queue stores operation identity, never SSH parameters, output, or credentials.
Its executor must check the live grant and target revision immediately before any
network action. There is intentionally no Agent fallback or terminal-create hook.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from uuid import uuid4


_SCHEMA = """
CREATE TABLE IF NOT EXISTS vps_readiness_jobs (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    vps_id TEXT NOT NULL,
    owner_user_id TEXT,
    authorized_user_id TEXT NOT NULL,
    connection_revision INTEGER NOT NULL,
    grant_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','running','installing','ready','failed')),
    reason_code TEXT,
    verify_only INTEGER NOT NULL DEFAULT 0 CHECK(verify_only IN (0,1)),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL,
    updated REAL NOT NULL,
    UNIQUE(space_id,vps_id,connection_revision)
)
"""
_DUE_INDEX = """
CREATE INDEX IF NOT EXISTS vps_readiness_due
ON vps_readiness_jobs(state,next_attempt,updated)
"""
_CURRENT_TARGET = """
EXISTS (SELECT 1 FROM vps_connections AS v WHERE v.id=vps_readiness_jobs.vps_id
        AND v.space_id=vps_readiness_jobs.space_id
        AND v.owner_user_id IS vps_readiness_jobs.owner_user_id
        AND v.connection_revision=vps_readiness_jobs.connection_revision
        AND v.enabled=1)
"""


@dataclass(frozen=True)
class ReadinessTarget:
    id: str
    space_id: str
    vps_id: str
    owner_user_id: str | None
    authorized_user_id: str
    connection_revision: int
    grant_id: str

    @classmethod
    def from_row(cls, row):
        return cls(
            *(
                row[key]
                for key in (
                    "id",
                    "space_id",
                    "vps_id",
                    "owner_user_id",
                    "authorized_user_id",
                    "connection_revision",
                    "grant_id",
                )
            )
        )


class VPSReadiness:
    def __init__(
        self,
        store,
        *,
        authorize,
        inspect,
        provision,
        concurrency=2,
        retry_delays=(15, 60, 180),
        poll_seconds=2,
        check_timeout=30,
        install_timeout=300,
        clock=time.time,
    ):
        if type(concurrency) is not int or not 1 <= concurrency <= 8:
            raise ValueError("VPS readiness concurrency must be between 1 and 8")
        if len(retry_delays) > 5 or any(not 0 < delay <= 3600 for delay in retry_delays):
            raise ValueError("Invalid VPS readiness retry delays")
        self.store = store
        self.authorize, self.inspect, self.provision = authorize, inspect, provision
        self.concurrency, self.retry_delays = concurrency, tuple(retry_delays)
        self.poll_seconds, self.check_timeout, self.install_timeout = (
            poll_seconds,
            check_timeout,
            install_timeout,
        )
        self.clock = clock
        self._wake = asyncio.Event()
        self._workers = []

    def _initialize(self):
        self.store.execute(_SCHEMA)
        self.store.execute(_DUE_INDEX)
        # Hub instance lock prevents another live Hub sharing this database.
        # An interrupted installation is never replayed, only re-inspected.
        self.store.execute(
            """UPDATE vps_readiness_jobs SET state='queued',next_attempt=?,updated=?
               WHERE state IN ('running','installing')""",
            (self.clock(), self.clock()),
        )

    async def start(self):
        if self._workers:
            return
        await self.store.run(self._initialize)
        self._workers = [
            asyncio.create_task(self._worker(), name=f"vps-readiness-{i}")
            for i in range(self.concurrency)
        ]

    async def stop(self):
        workers, self._workers = self._workers, []
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

    @staticmethod
    def _grant(grant_id, authorized_user_id):
        if not isinstance(grant_id, str) or not grant_id or len(grant_id) > 128:
            raise ValueError("An opaque, authorized VPS readiness grant is required")
        if (
            not isinstance(authorized_user_id, str)
            or not authorized_user_id
            or len(authorized_user_id) > 128
        ):
            raise ValueError("The authorizing Space administrator is required")

    @staticmethod
    def _public(row):
        return (
            {
                key: row[key]
                for key in ("id", "state", "reason_code", "connection_revision", "updated")
            }
            if row
            else None
        )

    def _enqueue(self, space_id, vps_id, grant_id, authorized_user_id, expected_revision, refresh):
        with self.store.transaction() as db:
            target = db.execute(
                """SELECT owner_user_id,connection_revision FROM vps_connections
                   WHERE id=? AND space_id=? AND enabled=1""",
                (vps_id, space_id),
            ).fetchone()
            if not target or target["connection_revision"] != expected_revision:
                return None
            now = self.clock()
            db.execute(
                """INSERT OR IGNORE INTO vps_readiness_jobs
                   (id,space_id,vps_id,owner_user_id,authorized_user_id,connection_revision,grant_id,state,next_attempt,updated)
                   VALUES (?,?,?,?,?,?,?,'queued',?,?)""",
                (
                    uuid4().hex,
                    space_id,
                    vps_id,
                    target["owner_user_id"],
                    authorized_user_id,
                    expected_revision,
                    grant_id,
                    now,
                    now,
                ),
            )
            if refresh:
                # Rebind a finished job only after a new server-authorized manual
                # request. Never transfer an active job or clear verify_only.
                db.execute(
                    """UPDATE vps_readiness_jobs
                       SET state='queued',reason_code=NULL,attempts=0,next_attempt=?,updated=?,
                           authorized_user_id=?,grant_id=?
                       WHERE space_id=? AND vps_id=? AND connection_revision=?
                         AND owner_user_id IS ?
                         AND state IN ('ready','failed')""",
                    (
                        now,
                        now,
                        authorized_user_id,
                        grant_id,
                        space_id,
                        vps_id,
                        expected_revision,
                        target["owner_user_id"],
                    ),
                )
            row = db.execute(
                """SELECT * FROM vps_readiness_jobs WHERE space_id=? AND vps_id=?
                   AND connection_revision=? AND owner_user_id IS ?""",
                (space_id, vps_id, expected_revision, target["owner_user_id"]),
            ).fetchone()
            return self._public(row)

    async def enqueue(
        self, space_id, vps_id, grant_id, *, authorized_user_id, expected_revision, refresh=False
    ):
        """The route must pre-authorize this grant/Space before enqueueing."""
        self._grant(grant_id, authorized_user_id)
        row = await self.store.run(
            self._enqueue,
            space_id,
            vps_id,
            grant_id,
            authorized_user_id,
            expected_revision,
            refresh,
        )
        if row:
            self._wake.set()
        return row

    def _backfill(self, space_id, grant_id, authorized_user_id):
        now = self.clock()
        with self.store.transaction() as db:
            return db.execute(
                """INSERT OR IGNORE INTO vps_readiness_jobs
                   (id,space_id,vps_id,owner_user_id,authorized_user_id,connection_revision,grant_id,state,next_attempt,updated)
                   SELECT lower(hex(randomblob(16))),space_id,id,owner_user_id,?,connection_revision,
                          ?,'queued',?,?
                   FROM vps_connections WHERE space_id=? AND enabled=1""",
                (authorized_user_id, grant_id, now, now, space_id),
            ).rowcount

    async def backfill(self, space_id, grant_id, *, authorized_user_id):
        """Enumerate target identifiers only in an already-authorized Space."""
        self._grant(grant_id, authorized_user_id)
        if not space_id:
            raise ValueError("An explicitly authorized Space is required")
        count = await self.store.run(self._backfill, space_id, grant_id, authorized_user_id)
        if count:
            self._wake.set()
        return count

    async def status(self, space_id, vps_id):
        row = await self.store.run(
            self.store.one,
            f"""SELECT * FROM vps_readiness_jobs WHERE space_id=? AND vps_id=? AND {_CURRENT_TARGET}""",
            (space_id, vps_id),
        )
        return self._public(row)

    def _claim(self):
        now = self.clock()
        with self.store.transaction() as db:
            row = db.execute(
                """SELECT q.* FROM vps_readiness_jobs AS q
                   WHERE q.state='queued' AND q.next_attempt<=?
                     AND NOT EXISTS (SELECT 1 FROM vps_readiness_jobs AS a
                         WHERE a.space_id=q.space_id AND a.vps_id=q.vps_id
                           AND a.state IN ('running','installing'))
                   ORDER BY q.next_attempt,q.updated,q.id LIMIT 1""",
                (now,),
            ).fetchone()
            if not row:
                return None
            db.execute(
                "UPDATE vps_readiness_jobs SET state='running',attempts=attempts+1,updated=? WHERE id=?",
                (now, row["id"]),
            )
            return dict(row) | {"attempts": row["attempts"] + 1}

    def _current(self, identifier):
        return bool(
            self.store.one(
                f"SELECT id FROM vps_readiness_jobs WHERE id=? AND {_CURRENT_TARGET}", (identifier,)
            )
        )

    def _begin_install(self, identifier):
        return (
            self.store.execute(
                f"""UPDATE vps_readiness_jobs SET state='installing',verify_only=1,updated=?
                WHERE id=? AND state='running' AND verify_only=0 AND {_CURRENT_TARGET}""",
                (self.clock(), identifier),
            ).rowcount
            == 1
        )

    def _update(self, identifier, state, reason=None, delay=0, clear_install_fence=False):
        now = self.clock()
        self.store.execute(
            """UPDATE vps_readiness_jobs SET state=?,reason_code=?,next_attempt=?,updated=?,
                   verify_only=CASE WHEN ? THEN 0 ELSE verify_only END
               WHERE id=? AND state IN ('running','installing')""",
            (state, reason, now + delay, now, clear_install_fence, identifier),
        )

    async def _dispatch_allowed(self, target, action):
        try:
            allowed = await asyncio.wait_for(self.authorize(target, action), self.check_timeout)
        except Exception:
            return False
        return allowed is True and await self.store.run(self._current, target.id)

    async def _finish(self, target, state, reason=None, delay=0, *, clear_install_fence=False):
        await self.store.run(self._update, target.id, state, reason, delay, clear_install_fence)
        if not delay:
            self._wake.set()

    async def _probe(self, target):
        try:
            value = await asyncio.wait_for(self.inspect(target), self.check_timeout)
            return value if isinstance(value, str) else "failed"
        except TimeoutError:
            return "offline"
        except Exception:
            return "failed"

    async def _resolve_probe(self, target, result, attempts, *, verify_only):
        if not await self.store.run(self._current, target.id):
            return await self._finish(target, "failed", "TARGET_CHANGED")
        if result == "ready":
            return await self._finish(target, "ready")
        if result == "offline" and attempts <= len(self.retry_delays):
            return await self._finish(
                target, "queued", "VPS_OFFLINE", self.retry_delays[attempts - 1]
            )
        if result == "missing" and verify_only:
            return await self._finish(target, "failed", "INSTALL_OUTCOME_UNCONFIRMED")
        codes = {
            "offline": "VPS_OFFLINE",
            "unsupported": "VPS_ENV_UNSUPPORTED",
            "denied": "VPS_PRIVILEGE_REQUIRED",
            "failed": "ENV_CHECK_FAILED",
            "trust_missing": "HUB_SSH_TRUST_MIGRATION_REQUIRED",
            "authorization_changed": "TARGET_OR_GRANT_CHANGED",
        }
        return await self._finish(target, "failed", codes.get(result, "ENV_CHECK_FAILED"))

    async def _perform(self, row):
        target = ReadinessTarget.from_row(row)
        if not await self._dispatch_allowed(target, "inspect"):
            return await self._finish(target, "failed", "TARGET_OR_GRANT_CHANGED")
        result = await self._probe(target)
        if result != "missing" or row["verify_only"]:
            return await self._resolve_probe(
                target, result, row["attempts"], verify_only=bool(row["verify_only"])
            )
        if not await self._dispatch_allowed(target, "install"):
            return await self._finish(target, "failed", "TARGET_OR_GRANT_CHANGED")
        if not await self.store.run(self._begin_install, target.id):
            return await self._finish(target, "failed", "TARGET_CHANGED")
        try:
            outcome = await asyncio.wait_for(self.provision(target), self.install_timeout)
        except Exception:
            outcome = "uncertain"
        no_install_reason = (
            {
                "install_not_started_denied": "VPS_PRIVILEGE_REQUIRED",
                "install_not_started_unsupported": "VPS_ENV_UNSUPPORTED",
            }.get(outcome)
            if isinstance(outcome, str)
            else None
        )
        if not await self._dispatch_allowed(target, "inspect"):
            return await self._finish(
                target,
                "failed",
                "TARGET_OR_GRANT_CHANGED",
                clear_install_fence=bool(no_install_reason),
            )
        return await self._verify_install(target, outcome, no_install_reason, row["attempts"])

    async def _verify_install(self, target, outcome, no_install_reason, attempts):
        verified = await self._probe(target)
        if no_install_reason:
            # Only explicit no-write receipts may clear the fence. Finish rather
            # than queueing an automatic retry if the follow-up probe went offline.
            if not await self.store.run(self._current, target.id):
                return await self._finish(target, "failed", "TARGET_CHANGED")
            if verified == "ready":
                return await self._finish(target, "ready", clear_install_fence=True)
            return await self._finish(target, "failed", no_install_reason, clear_install_fence=True)
        if verified == "missing" and outcome in ("denied", "unsupported", "failed"):
            verified = outcome
        return await self._resolve_probe(target, verified, attempts, verify_only=True)

    async def run_one(self):
        row = await self.store.run(self._claim)
        if not row:
            return False
        await self._perform(row)
        return True

    async def _worker(self):
        while True:
            self._wake.clear()
            if await self.run_one():
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_seconds)
            except TimeoutError:
                pass
