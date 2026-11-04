"""Explicit connection lifetimes; absent policies preserve legacy behavior."""
import math
import time
from shared.util import DevError

UNLIMITED_TOKEN_EXPIRY = 253402300799.0


def migrate(db):
    db.execute("""CREATE TABLE IF NOT EXISTS grant_expirations (
        grant_id TEXT PRIMARY KEY REFERENCES grants(id),
        expires REAL, revision INTEGER NOT NULL DEFAULT 1)""")


def expiration(days, now=None):
    if days is None:
        return None
    if type(days) is not int or not 1 <= days <= 365:
        raise DevError("INVALID_EXPIRATION", "有效天数须为 1 至 365，或明确选择无限期")
    return (time.time() if now is None else now) + days * 86400


def policy(store, grant_id):
    return store.one("SELECT * FROM grant_expirations WHERE grant_id=?", (grant_id,))


def check(store, grant_id):
    row = policy(store, grant_id)
    if row and row["expires"] is not None and row["expires"] <= time.time():
        raise DevError("INVALID_TOKEN", "授权已过期", 401)
    return row


def save_new(store, grant_id, expires):
    store.db.execute("INSERT INTO grant_expirations(grant_id,expires) VALUES (?,?)", (grant_id, expires))


def public_expiry(store, row):
    value = policy(store, row["id"])
    row["expiry_revision"] = value["revision"] if value else 0
    row["unlimited"] = bool(value and value["expires"] is None)
    row["authorization_expires"] = value["expires"] if value else row["expires"]
    if row["unlimited"]:
        row["expires"] = None


def update(store, principal, grant_id, expires, expected_revision, confirm_reactivate=False):
    if expires is not None and (isinstance(expires, bool) or not isinstance(expires, (float, int))
            or not math.isfinite(expires) or not time.time() < expires < UNLIMITED_TOKEN_EXPIRY):
        raise DevError("INVALID_EXPIRATION", "请选择未来的有效到期日期")
    with store.transaction():
        row = store.one("SELECT * FROM grants WHERE id=? AND user_id=? AND space_id=?",
                        (grant_id, principal.user_id, principal.space_id))
        if not row:
            raise DevError("GRANT_NOT_FOUND", "授权不存在", 404)
        if row["revoked"]:
            raise DevError("GRANT_REVOKED", "已撤销授权不能恢复，请重新授权", 409)
        previous = policy(store, grant_id)
        revision = previous["revision"] if previous else 0
        if expected_revision != revision:
            raise DevError("EXPIRATION_CONFLICT", "期限已被修改，请刷新后重试", 409)
        now = time.time()
        token_row = store.one("SELECT max(expires) AS expires FROM tokens WHERE grant_id=? AND kind IN ('pat','refresh')", (grant_id,))
        old_expires = previous["expires"] if previous else token_row["expires"]
        expired = (old_expires is not None and old_expires <= now) or not token_row["expires"] or token_row["expires"] <= now
        if expired and not confirm_reactivate:
            raise DevError("REACTIVATION_CONFIRMATION_REQUIRED", "授权已过期或尚无凭据；修改期限可能恢复现存凭据，请明确确认", 409)
        value = UNLIMITED_TOKEN_EXPIRY if expires is None else expires
        # Never revive consumed refresh tokens, revoked grants, or deleted credentials.
        store.db.execute("UPDATE tokens SET expires=? WHERE grant_id=? AND kind IN ('pat','refresh')", (value, grant_id))
        store.db.execute("UPDATE tokens SET expires=min(expires,?) WHERE grant_id=? AND kind='access'", (value, grant_id))
        store.db.execute("""INSERT INTO grant_expirations(grant_id,expires,revision) VALUES (?,?,1)
            ON CONFLICT(grant_id) DO UPDATE SET expires=excluded.expires,revision=revision+1""", (grant_id, expires))
        store.audit(principal.actor, "grant.expiration.updated", grant_id, detail={
            "previous_expires": old_expires, "expires": expires, "unlimited": expires is None,
            "confirmed_reactivation": confirm_reactivate, "revision": revision + 1})
        return {"ok": True, "expires": expires, "unlimited": expires is None,
                "expiry_revision": revision + 1, "reconnect_required": not bool(token_row["expires"])}
