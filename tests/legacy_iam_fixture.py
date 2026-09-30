"""Construct the actual pre-IAM Store for forward-migration regression fixtures.

Only fixture creation substitutes the final IAM migration. Reopening the database
uses the real current migrations, constraints and encryption-key checks. This
avoids pretending a v8 database with extra triggers is a historical v5/v6 store.
"""
from unittest.mock import patch
from hub.store import Store


def legacy_store(directory):
    def mark_v7(db):
        db.execute("UPDATE meta SET value='7' WHERE key='schema'")
    with patch('hub.iam_schema.migrate', mark_v7):
        return Store(directory)


def attach_session_security(store):
    """Complete explicit test-only session fixtures; never a production fallback."""
    store.execute("INSERT OR IGNORE INTO session_security(session_hash,epoch,authenticated_at) SELECT s.id_hash,u.epoch,0 FROM sessions s JOIN iam_users u ON u.user_id=s.user_id")


def seed_owner(store,user_id='owner',username='owner'):
    """Persist identities formerly implicit in isolated runtime test fixtures."""
    store.execute("INSERT OR IGNORE INTO users VALUES (?,?, '!fixture-only',1)",(user_id,username))
    return user_id


def seed_grant(store,grant_id,user_id='u',scopes=('read','write','execute'),projects=('p','p2')):
    import json
    store.execute("INSERT OR IGNORE INTO grants(id,user_id,label,scopes,projects,created,space_id,owner_user_id) VALUES(?,?,'fixture',?,?,1,'legacy',?)",(grant_id,user_id,json.dumps(scopes),json.dumps(projects),user_id))
