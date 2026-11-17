"""Synthetic pre-upgrade OAuth issuer; never reads real credentials."""
import time
from contextlib import contextmanager
from hub import grant_expiry
from hub.access_profiles import effective_grant
from hub.oauth import OAuth
from shared.crypto import digest, token
from shared.role_contracts import ROLE_SCOPE


def legacy_new_tokens(self, grant_id):
    self.store.require_transaction()
    grant = self.store.one("SELECT * FROM grants WHERE id=?", (grant_id,))
    scopes, _, _ = effective_grant(self.store, grant)
    if grant.get("authorization_mode") == "role":
        scopes = {ROLE_SCOPE}
    lifetime = grant_expiry.check(self.store, grant_id)
    now = time.time()
    end = ((grant_expiry.UNLIMITED_TOKEN_EXPIRY if lifetime["expires"] is None
            else lifetime["expires"]) if lifetime else now + 30 * 86400)
    access, refresh = "rda_" + token(), "rdr_" + token()
    for kind, value, expires in [("access", access, min(now + 3600, end)),
                                  ("refresh", refresh, end)]:
        self.store.db.execute("INSERT INTO tokens VALUES (?,?,?,?,?,?)",
                              (token(16), digest(value), grant_id, kind, expires, now))
    return {"access_token": access, "refresh_token": refresh, "token_type": "Bearer",
            "expires_in": int(min(now + 3600, end) - now), "scope": " ".join(sorted(scopes))}


@contextmanager
def legacy_issuer(monkeypatch):
    with monkeypatch.context() as patch:
        patch.setattr(OAuth, "new_tokens", legacy_new_tokens)
        yield


def migrate(app):
    from hub.oauth_lifetime import migrate_existing
    return migrate_existing(app.state.store)


def legacy_call(client, factory, *args, **kwargs):
    """Keep historical refresh tests explicit; new issuance uses the real issuer."""
    from unittest.mock import patch
    with patch.object(OAuth, "new_tokens", legacy_new_tokens):
        result = factory(*args, **kwargs)
    migrate(client.app)
    return result
