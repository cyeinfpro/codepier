"""Durable coordination for authorized, Hub-native VPS readiness.

This queue stores operation identity, never SSH parameters, output, or credentials.
A default scan is read-only. Only explicit installation of a current, positively
missing target can enter the install path. Its executor rechecks live authority
and revision immediately before any network action.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from uuid import uuid4


_LOGGER = logging.getLogger(__name__)
_INSTALL_ELIGIBILITY_SECONDS = 30 * 60
_PROBE_FAILURE_CODES = {"offline": "VPS_OFFLINE", "probe_interrupted": "VPS_PROBE_INTERRUPTED"}
HOST_VERIFICATION_REQUIRED = "required"
HOST_VERIFICATION_SKIP = "skip"
_HOST_VERIFICATION_MODES = (HOST_VERIFICATION_REQUIRED, HOST_VERIFICATION_SKIP)


def require_host_verification_mode(value):
    if value not in _HOST_VERIFICATION_MODES:
        raise ValueError("Unknown VPS readiness host verification mode")
    return value


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
    intent TEXT NOT NULL DEFAULT 'scan' CHECK(intent IN ('scan','install')),
    host_verification TEXT NOT NULL DEFAULT 'required'
        CHECK(host_verification IN ('required','skip')),
    reason_code TEXT,
    verify_only INTEGER NOT NULL DEFAULT 0 CHECK(verify_only IN (0,1)),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL NOT NULL,
    updated REAL NOT NULL,
    lease_token TEXT,
    lease_until REAL,
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
    host_verification: str = HOST_VERIFICATION_REQUIRED
    lease_token: str | None = None

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
                    "host_verification",
                    "lease_token",
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
        # Five bounded checks plus the bounded installer; add room for the
        # storage round trips. A persisted install fence survives expiry.
        self.lease_seconds = max(30, 5 * check_timeout + install_timeout + 15)
        self._wake = asyncio.Event()
        self._workers = []
        self._stopping = False

    def _initialize(self):
        self.store.execute(_SCHEMA)
        with self.store.transaction() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(vps_readiness_jobs)")}
            if "intent" not in columns:
                # Existing work carries no explicit install authorization. Map
                # it to scan, preserving verify_only and all historical results.
                db.execute(
                    "ALTER TABLE vps_readiness_jobs ADD COLUMN intent TEXT NOT NULL "
                    "DEFAULT 'scan' CHECK(intent IN ('scan','install'))"
                )
            if "host_verification" not in columns:
                db.execute(
                    "ALTER TABLE vps_readiness_jobs ADD COLUMN host_verification TEXT NOT NULL "
                    "DEFAULT 'required' CHECK(host_verification IN ('required','skip'))"
                )
            if "lease_token" not in columns:
                db.execute("ALTER TABLE vps_readiness_jobs ADD COLUMN lease_token TEXT")
            if "lease_until" not in columns:
                db.execute("ALTER TABLE vps_readiness_jobs ADD COLUMN lease_until REAL")
        self.store.execute(_DUE_INDEX)
        # Earlier versions set the fence even on independently verified
        # ready results. Resolve those historical rows without changing age.
        self.store.execute(
            "UPDATE vps_readiness_jobs SET verify_only=0 "
            "WHERE state='ready' AND reason_code IS NULL AND verify_only=1"
        )
        # Hub instance lock prevents another live Hub sharing this database.
        # An interrupted installation is never replayed, only re-inspected.
        self.store.execute(
            """UPDATE vps_readiness_jobs SET state='queued',next_attempt=?,updated=?,
                 lease_token=NULL,lease_until=NULL
               WHERE state IN ('running','installing')""",
            (self.clock(), self.clock()),
        )

        now = self.clock()
        # Historical rows stopped solely by retired host-key verification errors
        # become re-probeable under vps_ssh_v2 without replaying unknown installs
        # (verify_only is preserved as-is).
        self.store.execute(
            """UPDATE vps_readiness_jobs
               SET state='queued',intent='scan',reason_code=NULL,attempts=0,
                   next_attempt=?,updated=?,host_verification=?
               WHERE state='failed'
                 AND reason_code IN ('HOST_KEY_CHANGED','HOST_KEY_UNKNOWN','HOST_KEY_REVOKED','HUB_SSH_TRUST_MIGRATION_REQUIRED')""",
            (now, now, HOST_VERIFICATION_SKIP),
        )

    def _spawn_worker(self, index):
        worker = asyncio.create_task(self._worker(), name=f"vps-readiness-{index}")

        def supervise(done):
            if done.cancelled():
                return
            error = done.exception()
            if not self._stopping and index < len(self._workers) and self._workers[index] is done:
                _LOGGER.warning("VPS readiness worker exited (%s)", type(error).__name__)
                self._workers[index] = self._spawn_worker(index)

        worker.add_done_callback(supervise)
        return worker

    async def start(self):
        if self._workers:
            return
        await self.store.run(self._initialize)
        self._stopping = False
        self._workers = [self._spawn_worker(i) for i in range(self.concurrency)]

    async def stop(self):
        self._stopping = True
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
    def _public(row, *, now=None):
        if not row:
            return None
        public = {
            key: row[key]
            for key in (
                "id",
                "state",
                "reason_code",
                "connection_revision",
                "updated",
                "intent",
                "host_verification",
            )
        }
        # Expose missing independently of failed, without rebuilding the
        # existing SQLite CHECK-constrained table on upgraded servers.
        if public["state"] == "failed" and public["reason_code"] == "TMUX_MISSING":
            public["state"] = "missing"
        public["verification_only"] = bool(row["verify_only"])
        if now is None:
            now = time.time()
        public["install_eligible"] = (
            public["state"] == "missing"
            and public["intent"] == "scan"
            and not row["verify_only"]
            and 0 <= now - public["updated"] <= _INSTALL_ELIGIBILITY_SECONDS
        )
        return public

    def _enqueue(
        self,
        space_id,
        vps_id,
        grant_id,
        authorized_user_id,
        expected_revision,
        refresh,
        host_verification,
    ):
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
                   (id,space_id,vps_id,owner_user_id,authorized_user_id,connection_revision,
                    grant_id,state,next_attempt,updated,host_verification)
                   VALUES (?,?,?,?,?,?,?,'queued',?,?,?)""",
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
                    host_verification,
                ),
            )
            if refresh:
                # Rebind a finished job only after a new server-authorized manual
                # request. Never transfer an active job or clear verify_only.
                db.execute(
                    """UPDATE vps_readiness_jobs
                       SET state='queued',intent='scan',reason_code=NULL,attempts=0,next_attempt=?,updated=?,
                           authorized_user_id=?,grant_id=?,host_verification=?
                       WHERE space_id=? AND vps_id=? AND connection_revision=?
                         AND owner_user_id IS ?
                         AND state IN ('ready','failed')
                         AND (verify_only=0 OR host_verification=?)""",
                    (
                        now,
                        now,
                        authorized_user_id,
                        grant_id,
                        host_verification,
                        space_id,
                        vps_id,
                        expected_revision,
                        target["owner_user_id"],
                        host_verification,
                    ),
                )
            row = db.execute(
                """SELECT * FROM vps_readiness_jobs WHERE space_id=? AND vps_id=?
                   AND connection_revision=? AND owner_user_id IS ?""",
                (space_id, vps_id, expected_revision, target["owner_user_id"]),
            ).fetchone()
            return self._public(row, now=self.clock())

    async def enqueue(
        self,
        space_id,
        vps_id,
        grant_id,
        *,
        authorized_user_id,
        expected_revision,
        refresh=False,
        host_verification=HOST_VERIFICATION_REQUIRED,
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
            require_host_verification_mode(host_verification),
        )
        if row:
            self._wake.set()
        return row

    def _backfill(self, space_id, grant_id, authorized_user_id, refresh, host_verification):
        now = self.clock()
        with self.store.transaction() as db:
            if refresh:
                db.execute(
                    f"""UPDATE vps_readiness_jobs
                        SET state='queued',intent='scan',reason_code=NULL,attempts=0,
                            next_attempt=?,updated=?,authorized_user_id=?,grant_id=?,host_verification=?
                        WHERE space_id=? AND {_CURRENT_TARGET}
                          AND (
                            (state IN ('ready','failed') AND (verify_only=0 OR host_verification=?))
                            OR (?='skip' AND host_verification='required' AND state='queued'
                                AND intent='scan' AND attempts=0 AND verify_only=0
                                AND authorized_user_id=?)
                          )""",
                    (
                        now,
                        now,
                        authorized_user_id,
                        grant_id,
                        host_verification,
                        space_id,
                        host_verification,
                        host_verification,
                        authorized_user_id,
                    ),
                )
                refreshed = db.execute("SELECT changes()").fetchone()[0]
            else:
                refreshed = 0
            return (
                refreshed
                + db.execute(
                    """INSERT OR IGNORE INTO vps_readiness_jobs
                   (id,space_id,vps_id,owner_user_id,authorized_user_id,connection_revision,
                    grant_id,state,next_attempt,updated,host_verification)
                   SELECT lower(hex(randomblob(16))),space_id,id,owner_user_id,?,connection_revision,
                          ?,'queued',?,?,?
                   FROM vps_connections WHERE space_id=? AND enabled=1""",
                    (authorized_user_id, grant_id, now, now, host_verification, space_id),
                ).rowcount
            )

    async def backfill(
        self,
        space_id,
        grant_id,
        *,
        authorized_user_id,
        refresh=False,
        host_verification=HOST_VERIFICATION_REQUIRED,
    ):
        """Queue read-only scans only in an already-authorized Space."""
        self._grant(grant_id, authorized_user_id)
        if not space_id:
            raise ValueError("An explicitly authorized Space is required")
        count = await self.store.run(
            self._backfill,
            space_id,
            grant_id,
            authorized_user_id,
            refresh,
            require_host_verification_mode(host_verification),
        )
        if count:
            self._wake.set()
        return count

    def _install_missing(
        self, space_id, grant_id, authorized_user_id, vps_id, expected_revision, host_verification
    ):
        now = self.clock()
        with self.store.transaction() as db:
            # Avoid a SELECT/UPDATE gap: rebind only an exact, latest positive
            # scan result. Active and uncertain jobs are never overwritten.
            return db.execute(
                f"""UPDATE vps_readiness_jobs
                    SET state='queued',intent='install',reason_code=NULL,attempts=0,
                        next_attempt=?,updated=?,authorized_user_id=?,grant_id=?
                    WHERE space_id=? AND state='failed' AND intent='scan'
                      AND reason_code='TMUX_MISSING' AND verify_only=0
                      AND host_verification=? AND updated BETWEEN ? AND ?
                      AND (? IS NULL OR vps_id=?) AND (? IS NULL OR connection_revision=?)
                      AND {_CURRENT_TARGET}""",
                (
                    now,
                    now,
                    authorized_user_id,
                    grant_id,
                    space_id,
                    host_verification,
                    now - _INSTALL_ELIGIBILITY_SECONDS,
                    now,
                    vps_id,
                    vps_id,
                    expected_revision,
                    expected_revision,
                ),
            ).rowcount

    async def install_missing(
        self,
        space_id,
        grant_id,
        *,
        authorized_user_id,
        vps_id=None,
        expected_revision=None,
        host_verification=HOST_VERIFICATION_REQUIRED,
    ):
        """Promote only current, conclusively missing rows after explicit authorization."""
        self._grant(grant_id, authorized_user_id)
        if not space_id:
            raise ValueError("An explicitly authorized Space is required")
        if vps_id is not None and (
            not isinstance(vps_id, str)
            or not vps_id
            or type(expected_revision) is not int
            or expected_revision < 1
        ):
            raise ValueError("A single target install requires its exact revision")
        if vps_id is None and expected_revision is not None:
            raise ValueError("A revision requires a single target")
        count = await self.store.run(
            self._install_missing,
            space_id,
            grant_id,
            authorized_user_id,
            vps_id,
            expected_revision,
            require_host_verification_mode(host_verification),
        )
        if count:
            self._wake.set()
        return count

    async def status(self, space_id, vps_id):
        row = await self.store.run(
            self.store.one,
            f"""SELECT * FROM vps_readiness_jobs WHERE space_id=? AND vps_id=? AND {_CURRENT_TARGET}""",
            (space_id, vps_id),
        )
        return self._public(row, now=self.clock())

    def _claim(self):
        now = self.clock()
        with self.store.transaction() as db:
            db.execute(
                """UPDATE vps_readiness_jobs SET state='queued',next_attempt=?,updated=?,
                       lease_token=NULL,lease_until=NULL
                   WHERE state IN ('running','installing') AND lease_until<=?""",
                (now, now, now),
            )
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
            lease_token = uuid4().hex
            db.execute(
                """UPDATE vps_readiness_jobs SET state='running',attempts=attempts+1,
                       updated=?,lease_token=?,lease_until=? WHERE id=?""",
                (now, lease_token, now + self.lease_seconds, row["id"]),
            )
            return dict(row) | {"attempts": row["attempts"] + 1, "lease_token": lease_token}

    def _current(self, identifier, lease_token):
        return bool(
            self.store.one(
                f"""SELECT id FROM vps_readiness_jobs
                      WHERE id=? AND lease_token=? AND lease_until>? AND {_CURRENT_TARGET}""",
                (identifier, lease_token, self.clock()),
            )
        )

    def _begin_install(self, identifier, lease_token):
        return (
            self.store.execute(
                f"""UPDATE vps_readiness_jobs SET state='installing',verify_only=1,attempts=1,updated=?
                WHERE id=? AND state='running' AND intent='install'
                  AND verify_only=0 AND lease_token=? AND lease_until>? AND {_CURRENT_TARGET}""",
                (self.clock(), identifier, lease_token, self.clock()),
            ).rowcount
            == 1
        )

    def _update(
        self, identifier, lease_token, state, reason=None, delay=0, clear_install_fence=False
    ):
        now = self.clock()
        self.store.execute(
            """UPDATE vps_readiness_jobs SET state=?,reason_code=?,next_attempt=?,updated=?,
                   verify_only=CASE WHEN ? THEN 0 ELSE verify_only END,
                   lease_token=NULL, lease_until=NULL
               WHERE id=? AND state IN ('running','installing') AND lease_token=? AND lease_until>?""",
            (state, reason, now + delay, now, clear_install_fence, identifier, lease_token, now),
        )

    async def _dispatch_allowed(self, target, action):
        try:
            allowed = await asyncio.wait_for(self.authorize(target, action), self.check_timeout)
        except Exception:
            return False
        return allowed is True and await self.store.run(
            self._current, target.id, target.lease_token
        )

    async def _finish(self, target, state, reason=None, delay=0, *, clear_install_fence=False):
        await self.store.run(
            self._update,
            target.id,
            target.lease_token,
            state,
            reason,
            delay,
            clear_install_fence,
        )
        if reason and reason != "TMUX_MISSING":
            _LOGGER.info("VPS readiness job=%s state=%s reason=%s", target.id, state, reason)
        if not delay:
            self._wake.set()

    async def _probe(self, target):
        try:
            value = await asyncio.wait_for(self.inspect(target), self.check_timeout)
            return value if isinstance(value, str) else "failed"
        except TimeoutError:
            return "probe_interrupted"
        except Exception:
            return "failed"

    async def _resolve_probe(self, target, result, attempts, *, verify_only):
        if not await self.store.run(self._current, target.id, target.lease_token):
            return await self._finish(target, "failed", "TARGET_CHANGED")
        if result == "ready":
            # A successful authorized probe resolves an earlier interrupted or
            # completed install. Retain the fence only while the outcome is unknown.
            return await self._finish(target, "ready", clear_install_fence=verify_only)
        retry_reason = _PROBE_FAILURE_CODES.get(result)
        if result == "missing" and verify_only:
            retry_reason = "INSTALL_OUTCOME_UNCONFIRMED"
        if retry_reason:
            if attempts <= len(self.retry_delays):
                return await self._finish(
                    target, "queued", retry_reason, self.retry_delays[attempts - 1]
                )
            return await self._finish(target, "failed", retry_reason)
        codes = {
            "missing": "TMUX_MISSING",
            "unsupported": "VPS_ENV_UNSUPPORTED",
            "location_unsupported": "TMUX_LOCATION_UNSUPPORTED",
            "version_unsupported": "TMUX_VERSION_UNSUPPORTED",
            "install_failed": "TMUX_INSTALL_FAILED",
            "denied": "VPS_PRIVILEGE_REQUIRED",
            "failed": "ENV_CHECK_FAILED",
            "trust_missing": "HUB_SSH_TRUST_MIGRATION_REQUIRED",
            "host_key_unknown": "HOST_KEY_UNKNOWN",
            "host_key_changed": "HOST_KEY_CHANGED",
            "host_key_revoked": "HOST_KEY_REVOKED",
            "ssh_auth_failed": "SSH_AUTH_FAILED",
            "authorization_changed": "TARGET_OR_GRANT_CHANGED",
        }
        return await self._finish(target, "failed", codes.get(result, "ENV_CHECK_FAILED"))

    async def _perform(self, row):
        target = ReadinessTarget.from_row(row)
        if not await self._dispatch_allowed(target, "inspect"):
            return await self._finish(target, "failed", "TARGET_OR_GRANT_CHANGED")
        result = await self._probe(target)
        if result != "missing" or row["verify_only"] or row["intent"] != "install":
            return await self._resolve_probe(
                target, result, row["attempts"], verify_only=bool(row["verify_only"])
            )
        await self._install_after_probe(target, row["attempts"])

    def _install_fenced(self, identifier):
        row = self.store.one("SELECT verify_only FROM vps_readiness_jobs WHERE id=?", (identifier,))
        return bool(row and row["verify_only"])

    async def _install_after_probe(self, target, attempts):
        if not await self._dispatch_allowed(target, "install"):
            return await self._finish(target, "failed", "TARGET_OR_GRANT_CHANGED")

        async def before_install():
            # Called by the native executor only after its SSH preflight and
            # immediately before sending a package command. Restart can never
            # replay a write once this checkpoint has been committed.
            return await self.store.run(self._begin_install, target.id, target.lease_token)

        try:
            outcome = await asyncio.wait_for(
                self.provision(target, before_install=before_install), self.install_timeout
            )
        except TimeoutError:
            outcome = "probe_interrupted"
        except Exception:
            outcome = "failed"
        # Read the committed flag even if the coroutine timed out while
        # waiting for the checkpoint write; an in-memory flag could be stale.
        fenced = await self.store.run(self._install_fenced, target.id)
        no_install_reason = (
            {
                "install_not_started_denied": "VPS_PRIVILEGE_REQUIRED",
                "install_not_started_unsupported": "VPS_ENV_UNSUPPORTED",
                "install_not_started_authorization_changed": "TARGET_OR_GRANT_CHANGED",
            }.get(outcome)
            if isinstance(outcome, str)
            else None
        )
        if no_install_reason:
            return await self._finish(
                target, "failed", no_install_reason, clear_install_fence=fenced
            )
        if not await self._dispatch_allowed(target, "inspect"):
            return await self._finish(target, "failed", "TARGET_OR_GRANT_CHANGED")
        if outcome == "ok":
            # The native executor has already checked tmux on the live SSH
            # connection. Do not discard this proof if a new connection fails.
            return await self._resolve_probe(target, "ready", attempts, verify_only=fenced)
        if not fenced:
            return await self._resolve_probe(target, outcome, attempts, verify_only=False)
        verified = await self._probe(target)
        if verified == "missing" and outcome == "install_failed":
            verified = "install_failed"
        # A write may still complete after a lost receipt. The separate retry
        # budget starts at dispatch, and every retry is inspection only.
        return await self._resolve_probe(target, verified, 1, verify_only=True)

    async def run_one(self):
        row = await self.store.run(self._claim)
        if not row:
            return False
        await self._perform(row)
        return True

    async def _worker(self):
        failures = 0
        while True:
            self._wake.clear()
            try:
                worked = await self.run_one()
            except Exception as exc:
                failures = min(failures + 1, 6)
                _LOGGER.warning("VPS readiness worker deferred (%s)", type(exc).__name__)
                await asyncio.sleep(min(10, 0.25 * 2 ** (failures - 1)))
                continue
            failures = 0
            if worked:
                continue
            try:
                await asyncio.wait_for(self._wake.wait(), self.poll_seconds)
            except TimeoutError:
                pass
