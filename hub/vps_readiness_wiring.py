"""Authorization and Hub lifecycle wiring for the VPS readiness queue."""

from __future__ import annotations

import os
import time
from uuid import uuid4

from hub import iam
from shared.util import DevError
from shared.vps_transport import (
    VPS_HOST_IDENTITY_VERIFICATION,
    VPS_SSH_PROTOCOL,
    hub_native_vps_ssh_capability,
)
from hub.vps_readiness import (
    HOST_VERIFICATION_SKIP,
    ReadinessTarget,
    VPSReadiness,
    require_host_verification_mode,
)

_GRANTS = """CREATE TABLE IF NOT EXISTS vps_readiness_grants (
    id TEXT PRIMARY KEY,
    space_id TEXT NOT NULL,
    authorized_user_id TEXT NOT NULL,
    user_epoch INTEGER NOT NULL,
    identity_id TEXT,
    vps_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    intent TEXT NOT NULL DEFAULT 'scan' CHECK(intent IN ('scan','install')),
    host_verification TEXT NOT NULL DEFAULT 'required'
        CHECK(host_verification IN ('required','skip')),
    created REAL NOT NULL
)"""
_ACTOR_GRANT_INDEX = """CREATE UNIQUE INDEX IF NOT EXISTS vps_readiness_one_actor_mode_grant
ON vps_readiness_grants(
    space_id,ifnull(vps_id,''),authorized_user_id,ifnull(identity_id,''),intent,host_verification
)
WHERE active=1"""
_LIVE_SPACE_SCAN_GRANT = """
SELECT * FROM vps_readiness_grants WHERE space_id=? AND authorized_user_id=?
AND identity_id IS ? AND vps_id IS NULL AND active=1 AND intent='scan'
ORDER BY CASE WHEN host_verification='skip' THEN 0 ELSE 1 END, created DESC
"""
_CURRENT_JOB = """
SELECT q.* FROM vps_readiness_jobs q JOIN vps_connections v ON v.id=q.vps_id
WHERE q.space_id=? AND q.vps_id=? AND v.space_id=q.space_id
  AND v.owner_user_id IS q.owner_user_id
  AND v.connection_revision=q.connection_revision AND v.enabled=1
"""


class VPSReadinessWiring:
    def __init__(self, store, *, executor_factory=None):
        self.store = store
        self.executor = executor_factory(self) if executor_factory else None
        self.queue = (
            VPSReadiness(
                store,
                authorize=self.authorize,
                inspect=self._inspect,
                provision=self.executor.provision,
            )
            if self.executor is not None
            else None
        )
        self.started = False

    @property
    def available(self):
        return self.queue is not None and self.started

    def _initialize(self):
        self.store.execute(_GRANTS)
        with self.store.transaction() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(vps_readiness_grants)")}
            if "intent" not in columns:
                db.execute(
                    "ALTER TABLE vps_readiness_grants ADD COLUMN intent TEXT NOT NULL "
                    "DEFAULT 'scan' CHECK(intent IN ('scan','install'))"
                )
            if "host_verification" not in columns:
                db.execute(
                    "ALTER TABLE vps_readiness_grants ADD COLUMN host_verification TEXT NOT NULL "
                    "DEFAULT 'required' CHECK(host_verification IN ('required','skip'))"
                )
            db.execute("DROP INDEX IF EXISTS vps_readiness_one_space_grant")
            db.execute("DROP INDEX IF EXISTS vps_readiness_one_actor_grant")
            db.execute(_ACTOR_GRANT_INDEX)

    async def start(self):
        if self.queue is None or self.started:
            return
        await self.store.run(self._initialize)
        await self.queue.start()
        self.started = True

    async def stop(self):
        self.started = False
        if self.queue is not None:
            await self.queue.stop()

    def _admin(self, principal):
        principal = iam.live_principal(self.store, principal)
        if (
            not principal.user_id
            or not principal.session_hash
            or principal.grant_id
            or not principal.admin
        ):
            raise DevError("VPS_READINESS_ADMIN_REQUIRED", "仅当前空间管理员可授权检查环境", 403)
        return principal

    def _valid_grant(self, row):
        if not row or not row["active"]:
            return False
        try:
            user = iam.user_security(self.store, row["authorized_user_id"])
            if user["epoch"] != row["user_epoch"]:
                return False
            if not iam.is_space_admin(self.store, row["authorized_user_id"], row["space_id"]):
                return False
            identity = iam.check_identity(self.store, row["identity_id"], require_fresh=True)
            return identity is None or identity["user_id"] == row["authorized_user_id"]
        except DevError:
            return False

    def _current_vps(self, space_id, vps_id):
        return self.store.one(
            """SELECT id,space_id,owner_user_id,connection_revision,enabled,host,port,
                      username,host_key_policy,host_key_fingerprint
               FROM vps_connections WHERE id=? AND space_id=?""",
            (vps_id, space_id),
        )

    def _bound_target(self, target, action="inspect"):
        if action not in ("inspect", "install"):
            return None
        grant = self.store.one(
            "SELECT * FROM vps_readiness_grants WHERE id=? AND space_id=?",
            (target.grant_id, target.space_id),
        )
        if not self._matches_grant(grant, target, action):
            return None
        row = self._current_vps(target.space_id, target.vps_id)
        if (
            not row
            or not row["enabled"]
            or row["owner_user_id"] != target.owner_user_id
            or row["connection_revision"] != target.connection_revision
        ):
            return None
        return row

    def _matches_grant(self, grant, target, action):
        return bool(
            grant
            and grant["authorized_user_id"] == target.authorized_user_id
            and grant["vps_id"] in (None, target.vps_id)
            and (action != "install" or grant["intent"] == "install")
            and self._valid_grant(grant)
        )

    @staticmethod
    def _host_parameters(row, target):
        # Unified vps_ssh_v2 contract: legacy host_key_policy / host_key_fingerprint
        # and historical required/skip grant modes never gate readiness execution.
        return HOST_VERIFICATION_SKIP, "", "managed_vps"

    async def authorize(self, target, action):
        return bool(await self.store.run(self._bound_target, target, action))

    async def _inspect(self, target):
        row = await self.store.run(self._bound_target, target)
        if not row:
            return "authorization_changed"
        return await self.executor.inspect(target)

    async def resolve_target(self, target, action):
        # Transport imports are unnecessary when no native executor is mounted.
        from hub.vps_readiness_native import NativeSSHTarget

        row = await self.store.run(self._bound_target, target, action)
        if not row:
            return None
        mode, pin, purpose = self._host_parameters(row, target)
        return NativeSSHTarget(
            job_id=target.id,
            space_id=target.space_id,
            vps_id=target.vps_id,
            owner_user_id=target.owner_user_id,
            authorized_user_id=target.authorized_user_id,
            connection_revision=target.connection_revision,
            grant_id=target.grant_id,
            host=row["host"],
            port=row["port"],
            username=row["username"],
            host_fingerprint=pin,
            host_verification=mode,
            purpose=purpose,
        )

    def _secret(self, native):
        # The native connector invokes this only after the selected SSH
        # handshake. Rebind the exact active job and saved target before reading.
        with self.store.transaction(immediate=False) as db:
            job = db.execute(
                """SELECT * FROM vps_readiness_jobs WHERE id=? AND grant_id=?
                      AND space_id=? AND vps_id=? AND connection_revision=?
                      AND owner_user_id IS ? AND authorized_user_id=?
                      AND state IN ('running','installing')""",
                (
                    native.job_id,
                    native.grant_id,
                    native.space_id,
                    native.vps_id,
                    native.connection_revision,
                    native.owner_user_id,
                    native.authorized_user_id,
                ),
            ).fetchone()
            if not job or not self._native_target_matches(job, native):
                return None
            row = db.execute(
                "SELECT secret FROM vps_connections WHERE id=? AND space_id=? AND connection_revision=?",
                (native.vps_id, native.space_id, native.connection_revision),
            ).fetchone()
            return self.store.decrypt(row["secret"]) if row else None

    def _native_target_matches(self, job, native):
        target = ReadinessTarget.from_row(job)
        row = self._bound_target(target, "install" if job["state"] == "installing" else "inspect")
        if not row:
            return False
        mode, pin, purpose = self._host_parameters(row, target)
        return (
            row["host"],
            row["port"],
            row["username"],
            pin,
            mode,
            purpose,
        ) == (
            native.host,
            native.port,
            native.username,
            native.host_fingerprint,
            native.host_verification,
            native.purpose,
        )

    async def secret(self, native):
        return await self.store.run(self._secret, native)

    def _grant_for(
        self,
        principal,
        vps_id=None,
        intent="scan",
        host_verification=HOST_VERIFICATION_SKIP,
    ):
        """Reuse any valid active grant for the exact actor, identity, scope and intent.

        Compatible with dual active historical grants (required + skip) without
        violating the unique index, duplicating grants, or reviving revoked grants.
        """
        if intent not in ("scan", "install"):
            raise ValueError("Unknown readiness grant intent")
        require_host_verification_mode(host_verification)
        with self.store.transaction() as db:
            principal = self._admin(principal)
            identity_id = getattr(principal, "identity_id", None)
            rows = self.store.all(
                """SELECT * FROM vps_readiness_grants
                   WHERE space_id=? AND vps_id IS ? AND authorized_user_id=?
                     AND identity_id IS ? AND intent=? AND active=1
                   ORDER BY CASE WHEN host_verification='skip' THEN 0 ELSE 1 END, created DESC""",
                (
                    principal.space_id,
                    vps_id,
                    principal.user_id,
                    identity_id,
                    intent,
                ),
            )
            for row in rows:
                if self._valid_grant(row):
                    return row["id"]
            for row in rows:
                db.execute("UPDATE vps_readiness_grants SET active=0 WHERE id=?", (row["id"],))
            grant = uuid4().hex
            db.execute(
                """INSERT INTO vps_readiness_grants
                   (id,space_id,authorized_user_id,user_epoch,identity_id,vps_id,intent,
                    host_verification,created)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    grant,
                    principal.space_id,
                    principal.user_id,
                    principal.user_epoch,
                    identity_id,
                    vps_id,
                    intent,
                    HOST_VERIFICATION_SKIP,
                    time.time(),
                ),
            )
            return grant

    def _public_job(self, space_id, vps_id):
        return VPSReadiness._public(self.store.one(_CURRENT_JOB, (space_id, vps_id)))

    def fleet_summary(self, principal):
        available = self.available and bool(
            principal.admin and principal.session_hash and not principal.grant_id
        )
        grants = (
            self.store.all(
                _LIVE_SPACE_SCAN_GRANT,
                (principal.space_id, principal.user_id, getattr(principal, "identity_id", None)),
            )
            if available
            else []
        )
        return {
            "readiness_backfill_available": available,
            "readiness_backfill_enabled": any(self._valid_grant(g) for g in grants),
            "readiness_manual_actions": available,
            "readiness_connection_contract": VPS_SSH_PROTOCOL if available else None,
            "readiness_host_identity_verification": (
                VPS_HOST_IDENTITY_VERIFICATION if available else None
            ),
            "hub_native_vps_ssh": hub_native_vps_ssh_capability(available=self.available),
        }

    def summary(self, principal, vps_id, enabled):
        visible = self.available and bool(
            enabled
            and principal is not None
            and principal.admin
            and principal.session_hash
            and not principal.grant_id
        )
        return {
            "readiness_available": visible,
            "readiness": self._public_job(principal.space_id, vps_id) if visible else None,
        }

    def _target_for_admin(self, principal, vps_id):
        principal = self._admin(principal)
        row = self._current_vps(principal.space_id, vps_id)
        if not row:
            raise DevError("VPS_NOT_FOUND", "未找到 VPS 连接", 404)
        return principal, row

    async def get(self, principal, vps_id):
        principal, row = await self.store.run(self._target_for_admin, principal, vps_id)
        meta = await self.store.run(self.summary, principal, vps_id, row["enabled"])
        return {"available": meta["readiness_available"], "job": meta["readiness"]}

    async def request(self, principal, vps_id):
        principal, row = await self.store.run(self._target_for_admin, principal, vps_id)
        self._require_available()
        if not row["enabled"]:
            raise DevError("VPS_DISABLED", "请先启用 VPS", 409)
        grant = await self.store.run(
            self._grant_for, principal, vps_id, "scan", HOST_VERIFICATION_SKIP
        )
        job = await self.queue.enqueue(
            principal.space_id,
            vps_id,
            grant,
            authorized_user_id=principal.user_id,
            expected_revision=row["connection_revision"],
            refresh=True,
            host_verification=HOST_VERIFICATION_SKIP,
        )
        if not job:
            raise DevError("VPS_CONFIGURATION_CHANGED", "VPS 配置已变更，请刷新后重试", 409)
        return {"available": True, "job": job}

    async def activate_space(self, principal, host_verification=HOST_VERIFICATION_SKIP):
        principal = await self.store.run(self._admin, principal)
        self._require_available()
        require_host_verification_mode(host_verification)
        grant = await self.store.run(
            self._grant_for, principal, None, "scan", HOST_VERIFICATION_SKIP
        )
        count = await self.queue.backfill(
            principal.space_id,
            grant,
            authorized_user_id=principal.user_id,
            refresh=True,
            host_verification=HOST_VERIFICATION_SKIP,
        )
        return {"enabled": True, "queued_count": count}

    async def install_space(self, principal, host_verification=HOST_VERIFICATION_SKIP):
        principal = await self.store.run(self._admin, principal)
        self._require_available()
        require_host_verification_mode(host_verification)
        grant = await self.store.run(
            self._grant_for, principal, None, "install", HOST_VERIFICATION_SKIP
        )
        count = await self.queue.install_missing(
            principal.space_id,
            grant,
            authorized_user_id=principal.user_id,
            host_verification=HOST_VERIFICATION_SKIP,
        )
        return {"queued_count": count}

    def _require_available(self):
        if not self.available:
            raise DevError("VPS_READINESS_UNAVAILABLE", "Hub 直连检查环境未配置", 503)


def native_readiness_from_env(wiring):
    """Use an installed native connector by default; allow explicit disable."""
    if os.environ.get("CODEPIER_VPS_READINESS_NATIVE", "1") != "1":
        return None
    try:
        import asyncssh  # noqa: F401 -- this needs to exist on the Hub, not the Agent
        from agent.ssh_terminal_tmux import provision_tmux
        from hub.vps_readiness_native import NativeVPSReadiness
        from hub.vps_readiness_ssh import connect_readiness
    except ImportError:
        return None
    return NativeVPSReadiness(
        resolve_target=wiring.resolve_target,
        secret=wiring.secret,
        connector=connect_readiness,
        provisioner=provision_tmux,
    )
