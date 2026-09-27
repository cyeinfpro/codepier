"""Additive gateway schema, migrated inside the existing Store transaction.

The IAM schema number is not reused: these tables are optional, independent
feature state. Old binaries must not be used to edit roles containing connector
rules; rollback requires the matching pre-upgrade backup.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS gateway_secrets (id TEXT PRIMARY KEY, secret TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gateway_connectors (
    id TEXT PRIMARY KEY, space_id TEXT NOT NULL REFERENCES spaces(id),
    label TEXT NOT NULL, endpoint TEXT NOT NULL, protocol TEXT NOT NULL,
    networks TEXT NOT NULL, allow_http INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS gateway_connectors_space ON gateway_connectors(space_id,id);
CREATE TABLE IF NOT EXISTS gateway_accounts (
    id TEXT PRIMARY KEY, connector_id TEXT NOT NULL REFERENCES gateway_connectors(id),
    space_id TEXT NOT NULL REFERENCES spaces(id), owner_user_id TEXT NOT NULL REFERENCES users(id),
    label TEXT NOT NULL, sharing TEXT NOT NULL CHECK(sharing IN ('private','space')),
    secret TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
    version INTEGER NOT NULL DEFAULT 1, catalog TEXT NOT NULL DEFAULT '[]',
    catalog_hash TEXT NOT NULL DEFAULT '', created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS gateway_accounts_space ON gateway_accounts(space_id,owner_user_id);
CREATE TABLE IF NOT EXISTS gateway_bindings (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES gateway_accounts(id),
    space_id TEXT NOT NULL REFERENCES spaces(id), alias TEXT NOT NULL,
    tools TEXT NOT NULL, catalog_hash TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, version INTEGER NOT NULL DEFAULT 1,
    created REAL NOT NULL, UNIQUE(space_id,alias)
);
CREATE INDEX IF NOT EXISTS gateway_bindings_account ON gateway_bindings(account_id);
CREATE TABLE IF NOT EXISTS gateway_consents (
    grant_id TEXT PRIMARY KEY REFERENCES grants(id),
    user_id TEXT NOT NULL REFERENCES users(id), space_id TEXT NOT NULL REFERENCES spaces(id),
    profile_id TEXT NOT NULL, role_id TEXT NOT NULL, consent_version INTEGER NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS gateway_calls (
    id TEXT PRIMARY KEY, space_id TEXT NOT NULL, user_id TEXT NOT NULL,
    grant_id TEXT NOT NULL, binding_id TEXT NOT NULL, tool TEXT NOT NULL,
    tool_hash TEXT NOT NULL, state TEXT NOT NULL, error_code TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
    request_key TEXT, fingerprint TEXT NOT NULL,
    UNIQUE(space_id,grant_id,request_key)
);
CREATE INDEX IF NOT EXISTS gateway_calls_owner ON gateway_calls(space_id,user_id,created DESC,id);
"""


def migrate(db):
    row = db.execute("SELECT value FROM meta WHERE key='gateway_schema'").fetchone()
    if row and row[0] not in {'1', '2'}:
        raise RuntimeError('Unsupported gateway database schema')
    for statement in SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)
    db.execute("INSERT OR REPLACE INTO meta VALUES ('gateway_schema','2')")
