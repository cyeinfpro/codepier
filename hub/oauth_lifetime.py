"""Grant-bound OAuth credentials and durable, one-way legacy migration.

New OAuth grants issue only access tokens. A migration alias is created ONLY for
a refresh token that was live at upgrade; consumed/revoked/deleted credentials
never become aliases. Legacy aliases return one encrypted, durable credential,
without rotating or extending the user's authorization deadline.
"""

from __future__ import annotations

import json
import time

from hub import grant_expiry
from hub.access_profiles import effective_grant
from shared.crypto import digest, token
from shared.hub_data_contract import CONTRACT, CONTRACT_KEY, version_tuple
from shared.role_contracts import ROLE_SCOPE
from shared.util import DevError

MIN_VERSION = "1.33.23"


def migrate_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS oauth_lifetime_grants (
        grant_id TEXT PRIMARY KEY REFERENCES grants(id) ON DELETE CASCADE,
        created REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS oauth_refresh_migrations (
        token_hash TEXT PRIMARY KEY REFERENCES tokens(hash) ON DELETE CASCADE,
        grant_id TEXT NOT NULL REFERENCES grants(id) ON DELETE CASCADE,
        created REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS oauth_lifetime_credentials (
        grant_id TEXT PRIMARY KEY REFERENCES grants(id) ON DELETE CASCADE,
        access_hash TEXT NOT NULL, secret TEXT NOT NULL, created REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS oauth_lifetime_access (
        token_hash TEXT PRIMARY KEY REFERENCES tokens(hash) ON DELETE CASCADE,
        grant_id TEXT NOT NULL REFERENCES grants(id) ON DELETE CASCADE)""")
    db.execute(
        "CREATE INDEX IF NOT EXISTS oauth_migration_grant ON oauth_refresh_migrations(grant_id)"
    )


def require_compatible_writer(store):
    # The existing preflight is understood by older releases. A separate new
    # marker alone would not stop an old writer from rotating migration aliases.
    row = store.one("SELECT value FROM meta WHERE key=?", (CONTRACT_KEY,))
    contract = json.loads(row["value"]) if row else dict(CONTRACT)
    for key in ("min_reader_version", "min_writer_version"):
        if version_tuple(contract[key]) < version_tuple(MIN_VERSION):
            contract[key] = MIN_VERSION
    store.db.execute(
        "INSERT INTO meta(key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (CONTRACT_KEY, json.dumps(contract, sort_keys=True)),
    )


def migrate_existing(store):
    """Run atomically before serving OAuth/MCP; safe to repeat after a restart."""
    now = time.time()
    migrated = 0
    with store.transaction():
        require_compatible_writer(store)
        rows = store.all(
            """SELECT g.id, g.client_id FROM grants g
            WHERE g.client_id IS NOT NULL AND g.revoked=0
            AND NOT EXISTS (SELECT 1 FROM oauth_lifetime_grants l WHERE l.grant_id=g.id)
            AND EXISTS (SELECT 1 FROM tokens t WHERE t.grant_id=g.id
                        AND t.kind IN ('access','refresh') AND t.expires>?)""",
            (now,),
        )
        for grant in rows:
            gid = grant["id"]
            policy = grant_expiry.policy(store, gid)
            if policy and policy["expires"] is not None and policy["expires"] <= now:
                continue
            if not policy:
                # Old releases had no explicit lifetime record. Preserve their
                # remaining absolute refresh deadline, never start a new 30 days.
                row = store.one(
                    """SELECT
                    max(CASE WHEN kind='refresh' THEN expires END) refresh_end,
                    max(CASE WHEN kind='access' THEN expires END) access_end
                    FROM tokens WHERE grant_id=? AND expires>?""",
                    (gid, now),
                )
                end = row["refresh_end"] or row["access_end"]
                grant_expiry.save_new(store, gid, end)
            else:
                end = policy["expires"]
            token_end = grant_expiry.UNLIMITED_TOKEN_EXPIRY if end is None else end
            store.db.execute("INSERT INTO oauth_lifetime_grants VALUES (?,?)", (gid, now))
            store.db.execute(
                """INSERT INTO oauth_refresh_migrations
                SELECT hash,grant_id,? FROM tokens
                WHERE grant_id=? AND kind='refresh' AND expires>?""",
                (now, gid, now),
            )
            store.db.execute(
                """INSERT INTO oauth_lifetime_access
                SELECT hash,grant_id FROM tokens
                WHERE grant_id=? AND kind='access' AND expires>?""",
                (gid, now),
            )
            # Only still-live access tokens are carried forward. Never resurrect
            # expired access tokens, even if their old rows have not been swept.
            store.db.execute(
                """UPDATE tokens SET expires=? WHERE grant_id=?
                AND kind IN ('access','refresh') AND expires>?""",
                (token_end, gid, now),
            )
            store.audit(
                "oauth:" + grant["client_id"],
                "oauth.lifetime.migrated",
                gid,
                detail={"unlimited": end is None},
                target_kind="grant",
                commit=False,
            )
            migrated += 1
    return migrated


def issue(store, grant_id):
    """Return the same credential; caller owns a BEGIN IMMEDIATE transaction."""
    store.require_transaction()
    grant = store.one("SELECT * FROM grants WHERE id=?", (grant_id,))
    if not grant or grant["revoked"]:
        raise DevError("INVALID_TOKEN", "授权已撤销或不存在", 401)
    scopes, _, _ = effective_grant(store, grant)
    if grant.get("authorization_mode") == "role":
        scopes = {ROLE_SCOPE}
    policy = grant_expiry.check(store, grant_id)
    if not policy:
        raise DevError("INVALID_TOKEN", "授权期限记录缺失", 401)
    now = time.time()
    end = policy["expires"]
    saved = store.one("SELECT * FROM oauth_lifetime_credentials WHERE grant_id=?", (grant_id,))
    if saved:
        row = store.one(
            "SELECT * FROM tokens WHERE hash=? AND grant_id=? AND kind='access'",
            (saved["access_hash"], grant_id),
        )
        if not row or row["expires"] <= now:
            raise DevError("INVALID_TOKEN", "凭据已删除或过期", 401)
        secret = store.decrypt(saved["secret"])
        if digest(secret) != saved["access_hash"]:
            raise DevError("INVALID_TOKEN", "凭据完整性校验失败", 401)
    else:
        secret = "rda_" + token()
        token_end = grant_expiry.UNLIMITED_TOKEN_EXPIRY if end is None else end
        store.db.execute(
            "INSERT INTO tokens VALUES (?,?,?,'access',?,?)",
            (token(16), digest(secret), grant_id, token_end, now),
        )
        store.db.execute(
            "INSERT INTO oauth_lifetime_access VALUES (?,?)", (digest(secret), grant_id)
        )
        store.db.execute(
            "INSERT INTO oauth_lifetime_credentials VALUES (?,?,?,?)",
            (grant_id, digest(secret), store.encrypt(secret), now),
        )
        store.db.execute(
            "INSERT OR IGNORE INTO oauth_lifetime_grants VALUES (?,?)", (grant_id, now)
        )
    result = {"access_token": secret, "token_type": "Bearer", "scope": " ".join(sorted(scopes))}
    # Omitted means no automatic expiration, documented in OAUTH_LIFETIME.md.
    # Do not send 0 (interpreted inconsistently), null or an overflowing sentinel.
    if end is not None:
        result["expires_in"] = max(0, int(end - now))
    return result
