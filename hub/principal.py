"""Authenticated authority value; no Runtime or service imports."""
from dataclasses import dataclass


@dataclass
class Principal:
    actor: str
    user_id: str
    scopes: set[str]
    projects: list[str]
    grant_id: str | None = None
    admin: bool = False
    session_hash: str = ""
    token_hash: str = ""



def refresh_principal(store, principal):
    """Revalidate browser/token identities after waiting for a database worker.

    Internal principals without an authentication fence remain supported for
    trusted local services and isolated tests; HTTP credentials always carry one.
    """
    import json
    import time
    from dataclasses import replace
    from shared.util import DevError

    if principal.session_hash:
        row = store.one("SELECT s.user_id,u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id_hash=? AND s.expires>?", (principal.session_hash, time.time()))
        if not row or row["user_id"] != principal.user_id:
            raise DevError("LOGIN_REQUIRED", "登录已过期，请重新登录", 401)
        return replace(principal, actor="panel:" + row["username"])
    if principal.token_hash:
        row = store.one("SELECT g.*,t.kind AS token_kind FROM tokens t JOIN grants g ON g.id=t.grant_id WHERE t.hash=? AND t.kind IN ('access','pat') AND t.expires>? AND g.revoked=0", (principal.token_hash, time.time()))
        if not row or row["id"] != principal.grant_id:
            raise DevError("INVALID_TOKEN", "凭据已过期或撤销", 401)
        resource = getattr(store, "oauth_resource", None)
        if row["token_kind"] == "access" and resource and row["resource"] != resource():
            raise DevError("INVALID_TOKEN", "凭据的资源标识已改变", 401)
        return replace(principal, scopes=set(json.loads(row["scopes"])), projects=json.loads(row["projects"]))
    return principal
