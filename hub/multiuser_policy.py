"""Draft multi-user authorization core; NOT connected to production routes yet.

This is a policy evaluator, not authentication or token verification. Its store
must load a consistent, current view from trusted server records on every call.
Never construct a view from MCP arguments, unverified JWTs or cached login claims.
No migration, OIDC route, enrollment or replacement of Auth.admin is enabled here.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol
from urllib.parse import urlsplit


# These are proposed internal capabilities, not OAuth scopes. The existing
# read/write/execute/computer project vocabulary is retained.
ACTIONS = {
    "project": frozenset({"read", "write", "execute", "computer", "projects.update", "projects.delete"}),
    "device": frozenset({"devices.read", "devices.manage", "projects.create"}),
    "vps": frozenset({"vps.read", "vps.execute", "vps.manage"}),
    "operation": frozenset({"operations.read", "operations.cancel"}),
    "workflow": frozenset({"workflows.read", "workflows.write"}),
    "artifact": frozenset({"artifacts.read", "artifacts.share"}),
    "native_session": frozenset({"native.read", "native.control"}),
    "browser_session": frozenset({"browser.read", "browser.control"}),
}


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not value or value == "*" or len(value) > 256:
        raise ValueError("An explicit nonempty identifier is required")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Control characters are not permitted in identifiers")


@dataclass(frozen=True)
class ExternalIdentityKey:
    """Exact OIDC identity key, usable only AFTER successful ID-token validation.

    Email/username do not participate. Do not normalize issuer or subject:
    OIDC issuer comparison and subject identity are case-sensitive.
    """
    issuer: str
    subject: str

    def __post_init__(self):
        if not isinstance(self.issuer, str) or len(self.issuer) > 2048:
            raise ValueError("Invalid issuer")
        if any(c.isspace() or ord(c) < 32 for c in self.issuer):
            raise ValueError("Invalid issuer")
        try:
            uri = urlsplit(self.issuer)
            if (uri.scheme != "https" or not uri.hostname or uri.username is not None
                    or uri.password is not None or "?" in self.issuer or "#" in self.issuer
                    or "\\" in self.issuer or uri.port is not None and not 1 <= uri.port <= 65535):
                raise ValueError("Invalid issuer")
        except ValueError as exc:
            raise ValueError("Issuer must be an exact HTTPS issuer without query/fragment") from exc
        if (not isinstance(self.subject, str) or not 1 <= len(self.subject) <= 255
                or not self.subject.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in self.subject)):
            raise ValueError("Subject must contain 1-255 non-control ASCII characters")


@dataclass(frozen=True)
class Rule:
    resource_kind: str
    actions: frozenset[str]
    resource_ids: frozenset[str] = field(default_factory=frozenset)
    all_in_space: bool = False
    created_by_role: bool = False
    excluded_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self):
        if self.resource_kind not in ACTIONS:
            raise ValueError("Unknown resource kind")
        if not isinstance(self.actions, frozenset) or not self.actions or not self.actions <= ACTIONS[self.resource_kind]:
            raise ValueError("Rule contains unknown or mismatched actions")
        if type(self.all_in_space) is not bool or type(self.created_by_role) is not bool:
            raise ValueError("Selectors must be booleans")
        if not isinstance(self.resource_ids, frozenset) or not isinstance(self.excluded_ids, frozenset):
            raise ValueError("Resource selectors must be immutable sets")
        for value in self.resource_ids | self.excluded_ids:
            _identifier(value)
        if not (self.resource_ids or self.all_in_space or self.created_by_role):
            raise ValueError("Rule must have an explicit resource selector")

    def permits(self, action: str, resource: Resource, role_id: str) -> bool:
        # Exclusions are local to this rule. They are not a global deny list.
        return (
            resource.kind == self.resource_kind
            and action in self.actions
            and resource.id not in self.excluded_ids
            and (self.all_in_space or resource.id in self.resource_ids
                 or self.created_by_role and resource.created_by_role_id == role_id)
        )


@dataclass(frozen=True)
class User:
    id: str
    active: bool = True


@dataclass(frozen=True)
class Space:
    id: str
    active: bool = True


@dataclass(frozen=True)
class Membership:
    user_id: str
    space_id: str
    active: bool = True


@dataclass(frozen=True)
class RoleAssignment:
    user_id: str
    space_id: str
    role_id: str
    active: bool = True
    may_delegate: bool = False


@dataclass(frozen=True)
class Profile:
    id: str
    user_id: str
    space_id: str
    role_id: str
    enabled: bool = True


@dataclass(frozen=True)
class Grant:
    id: str
    user_id: str
    space_id: str
    profile_id: str
    role_id: str
    audience: str
    expires_at: float
    revoked: bool = False


@dataclass(frozen=True)
class Role:
    id: str
    space_id: str
    version: int
    rules: tuple[Rule, ...]
    enabled: bool = True

    def __post_init__(self):
        if type(self.version) is not int or self.version < 1:
            raise ValueError("Role version must be a positive integer")
        if not isinstance(self.rules, tuple) or any(not isinstance(rule, Rule) for rule in self.rules):
            raise ValueError("Role rules must be an immutable tuple")


@dataclass(frozen=True)
class Resource:
    id: str
    space_id: str
    kind: str
    owner_user_id: str | None = None
    owner_grant_id: str | None = None
    visibility: str = "user"
    created_by_role_id: str | None = None
    active: bool = True
    # Server-derived intersection of project/device/local restrictions, not a
    # caller-supplied override. The executing Agent must still recheck locally.
    allowed_actions: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self):
        if self.kind not in ACTIONS or self.visibility not in {"user", "grant", "space"}:
            raise ValueError("Unknown resource kind or visibility")
        if not isinstance(self.allowed_actions, frozenset) or not self.allowed_actions <= ACTIONS[self.kind]:
            raise ValueError("Invalid resource action ceiling")


@dataclass(frozen=True)
class AccessView:
    """One consistent DB snapshot. Missing records must produce None, not defaults."""
    user: User
    space: Space
    membership: Membership
    assignment: RoleAssignment
    profile: Profile
    grant: Grant
    role: Role
    resource: Resource


class AccessStore(Protocol):
    def read_access(self, grant_id: str, resource_kind: str, resource_id: str) -> AccessView | None:
        """Resolve ALL records with one read transaction from current server state."""
        ...


@dataclass(frozen=True)
class Authorization:
    user_id: str
    space_id: str
    profile_id: str
    grant_id: str
    role_id: str
    role_version: int
    resource_kind: str
    resource_id: str
    action: str


class AccessDenied(PermissionError):
    """Generic client-facing message; reason is for protected internal audit only."""
    def __init__(self, reason: str):
        super().__init__("ACCESS_DENIED")
        self.reason = reason


class AccessController:
    def __init__(self, store: AccessStore, audience: str, clock: Callable[[], float] = time.time):
        if not isinstance(audience, str) or not audience:
            raise ValueError("Expected audience is required")
        self.store, self.audience, self.clock = store, audience, clock

    def require(self, grant_id: str, action: str, resource_kind: str, resource_id: str) -> Authorization:
        """Read current authority on every call, including post-wait/result reads.

        Does NOT verify a bearer token or authorize a mutation transaction. The
        future adapter must validate credentials before this method and recheck
        inside dispatch/mutation transactions. Do not cache this return value as
        continuing permission for a stream, queued task, or subsequent request.
        """
        try:
            _identifier(grant_id)
            _identifier(resource_id)
        except ValueError as exc:
            raise AccessDenied("invalid_identifier") from exc
        if resource_kind not in ACTIONS or action not in ACTIONS[resource_kind]:
            raise AccessDenied("unknown_action")
        view = self.store.read_access(grant_id, resource_kind, resource_id)
        if view is None:
            raise AccessDenied("missing_binding")
        u, s, m, a, p, g, role, r = (
            view.user, view.space, view.membership, view.assignment,
            view.profile, view.grant, view.role, view.resource,
        )
        if (g.id != grant_id or r.id != resource_id or r.kind != resource_kind
                or not (u.id == m.user_id == a.user_id == p.user_id == g.user_id)
                or not (s.id == m.space_id == a.space_id == p.space_id == g.space_id == role.space_id == r.space_id)
                or not (g.profile_id == p.id)
                or not (g.role_id == p.role_id == a.role_id == role.id)):
            raise AccessDenied("binding_mismatch")
        if not all(value is True for value in (u.active, s.active, m.active, a.active, p.enabled, r.active)):
            raise AccessDenied("inactive_binding")
        if g.revoked is not False or g.audience != self.audience:
            raise AccessDenied("invalid_grant")
        if (type(g.expires_at) not in (int, float) or not math.isfinite(g.expires_at)
                or g.expires_at <= self.clock()):
            raise AccessDenied("expired_grant")
        if a.may_delegate is not True:
            raise AccessDenied("delegation_not_allowed")
        if role.enabled is not True:
            raise AccessDenied("role_paused")
        if r.visibility == "user" and r.owner_user_id != u.id:
            raise AccessDenied("private_resource")
        if r.visibility == "grant" and (r.owner_grant_id != g.id or r.owner_user_id != u.id):
            raise AccessDenied("private_resource")
        if action not in r.allowed_actions:
            raise AccessDenied("resource_ceiling")
        if not any(rule.permits(action, r, role.id) for rule in role.rules):
            raise AccessDenied("role_policy")
        return Authorization(u.id, s.id, p.id, g.id, role.id, role.version, r.kind, r.id, action)
