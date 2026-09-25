"""Executable contract for the draft core, NOT HTTP/OIDC/Agent acceptance."""
from dataclasses import replace

import pytest

from hub.multiuser_policy import (
    ACTIONS, AccessController, AccessDenied, AccessView, ExternalIdentityKey,
    Grant, Membership, Profile, Resource, Role, RoleAssignment, Rule, Space, User,
)

AUDIENCE = "https://codepier.example.test/mcp"


class MemoryStore:
    def __init__(self, view):
        self.view = view
        self.reads = 0

    def read_access(self, grant_id, resource_kind, resource_id):
        self.reads += 1
        return self.view


@pytest.fixture
def access():
    rule = Rule("project", frozenset({"read"}), frozenset({"project-a"}))
    view = AccessView(
        User("alice"), Space("team"), Membership("alice", "team"),
        RoleAssignment("alice", "team", "secretary", may_delegate=True),
        Profile("alice-secretary", "alice", "team", "secretary"),
        Grant("connection-a", "alice", "team", "alice-secretary", "secretary", AUDIENCE, 200),
        Role("secretary", "team", 1, (rule,)),
        Resource("project-a", "team", "project", visibility="space", allowed_actions=ACTIONS["project"]),
    )
    store = MemoryStore(view)
    return store, AccessController(store, AUDIENCE, lambda: 100)


def request(controller, action="read", kind="project", identifier="project-a", grant="connection-a"):
    return controller.require(grant, action, kind, identifier)


def change(store, record, **values):
    store.view = replace(store.view, **{record: replace(getattr(store.view, record), **values)})


def test_uses_current_role_for_new_project_and_capability_without_new_grant(access):
    store, controller = access
    grant = store.view.grant
    assert request(controller).role_version == 1
    change(store, "resource", id="project-b")
    with pytest.raises(AccessDenied):
        request(controller, identifier="project-b")
    change(store, "role", version=2, rules=(Rule("project", frozenset({"read", "execute"}), all_in_space=True),))
    result = request(controller, "execute", identifier="project-b")
    assert result.grant_id == grant.id and result.role_version == 2
    assert store.view.grant is grant
    assert store.reads == 3


def test_actions_never_cross_product_with_unrelated_resource_rules(access):
    store, controller = access
    change(store, "role", rules=(
        Rule("project", frozenset({"read"}), all_in_space=True),
        Rule("project", frozenset({"execute"}), frozenset({"project-a"})),
    ))
    assert request(controller, "execute")
    change(store, "resource", id="project-b")
    assert request(controller, identifier="project-b")
    with pytest.raises(AccessDenied):
        request(controller, "execute", identifier="project-b")


@pytest.mark.parametrize("record", ["membership", "assignment", "profile", "grant", "role", "resource"])
def test_all_projects_never_crosses_space_boundary(access, record):
    store, controller = access
    change(store, "role", rules=(Rule("project", frozenset({"read"}), all_in_space=True),))
    change(store, record, space_id="bob-personal")
    with pytest.raises(AccessDenied) as caught:
        request(controller)
    assert caught.value.reason == "binding_mismatch"
    assert str(caught.value) == "ACCESS_DENIED"


@pytest.mark.parametrize("record,field,value", [
    ("user", "id", "bob"), ("membership", "user_id", "bob"),
    ("assignment", "user_id", "bob"), ("profile", "user_id", "bob"),
    ("grant", "user_id", "bob"), ("profile", "id", "other-profile"),
    ("profile", "role_id", "admin"), ("assignment", "role_id", "admin"),
    ("role", "id", "admin"), ("grant", "role_id", "admin"),
    ("resource", "id", "project-b"), ("grant", "id", "connection-b"),
])
def test_mismatched_identity_cannot_be_swapped(access, record, field, value):
    store, controller = access
    change(store, record, **{field: value})
    with pytest.raises(AccessDenied):
        request(controller)


@pytest.mark.parametrize("record,field", [
    ("user", "active"), ("space", "active"), ("membership", "active"),
    ("assignment", "active"), ("profile", "enabled"), ("resource", "active"),
    ("role", "enabled"), ("assignment", "may_delegate"),
])
def test_live_suspension_and_restoration_without_reconnecting(access, record, field):
    store, controller = access
    grant = store.view.grant
    assert request(controller)
    change(store, record, **{field: False})
    with pytest.raises(AccessDenied):
        request(controller)
    change(store, record, **{field: True})
    assert request(controller).grant_id == grant.id
    assert store.view.grant is grant


def test_revoked_grant_does_not_revive_when_role_is_reenabled(access):
    store, controller = access
    change(store, "grant", revoked=True)
    change(store, "role", enabled=False)
    change(store, "role", enabled=True)
    with pytest.raises(AccessDenied) as caught:
        request(controller)
    assert caught.value.reason == "invalid_grant"


@pytest.mark.parametrize("expiry", [0, 99, 100, True, float("nan"), float("inf"), "200"])
def test_invalid_or_expired_grant_denied(access, expiry):
    store, controller = access
    change(store, "grant", expires_at=expiry)
    with pytest.raises(AccessDenied):
        request(controller)


def test_wrong_token_audience_is_denied(access):
    store, controller = access
    change(store, "grant", audience="https://authentik.example.test/")
    with pytest.raises(AccessDenied):
        request(controller)


def test_removal_applies_to_next_check_including_result_reads(access):
    store, controller = access
    assert request(controller)
    change(store, "role", version=2, rules=())
    with pytest.raises(AccessDenied) as caught:
        request(controller)
    assert caught.value.reason == "role_policy"
    assert store.reads == 2


@pytest.mark.parametrize("ceiling", [frozenset(), frozenset({"write"})])
def test_local_resource_ceiling_remains_an_intersection(access, ceiling):
    store, controller = access
    change(store, "resource", allowed_actions=ceiling)
    with pytest.raises(AccessDenied):
        request(controller)


def test_user_private_records_not_shared_by_shared_role(access):
    store, controller = access
    change(store, "role", rules=(Rule("artifact", frozenset({"artifacts.read"}), all_in_space=True),))
    store.view = replace(store.view, resource=Resource(
        "artifact-a", "team", "artifact", owner_user_id="bob", allowed_actions=ACTIONS["artifact"]))
    with pytest.raises(AccessDenied):
        request(controller, "artifacts.read", "artifact", "artifact-a")
    change(store, "resource", owner_user_id="alice")
    assert request(controller, "artifacts.read", "artifact", "artifact-a")


@pytest.mark.parametrize("owner_user,owner_grant", [
    ("bob", "connection-b"), ("alice", "connection-b"), ("bob", "connection-a"), (None, None),
])
def test_grant_private_sessions_need_both_user_and_connection(access, owner_user, owner_grant):
    store, controller = access
    change(store, "role", rules=(Rule("native_session", frozenset({"native.read"}), all_in_space=True),))
    store.view = replace(store.view, resource=Resource(
        "session-a", "team", "native_session", owner_user_id=owner_user,
        owner_grant_id=owner_grant, visibility="grant", allowed_actions=ACTIONS["native_session"]))
    with pytest.raises(AccessDenied):
        request(controller, "native.read", "native_session", "session-a")
    change(store, "resource", owner_user_id="alice", owner_grant_id="connection-a")
    assert request(controller, "native.read", "native_session", "session-a")


def test_created_project_selector_requires_server_record_of_creating_role(access):
    store, controller = access
    change(store, "role", rules=(Rule("project", frozenset({"read"}), created_by_role=True),))
    with pytest.raises(AccessDenied):
        request(controller)
    change(store, "resource", created_by_role_id="secretary")
    assert request(controller)
    change(store, "resource", created_by_role_id="other-role")
    with pytest.raises(AccessDenied):
        request(controller)


def test_project_creation_does_not_allow_role_editing_or_data_access(access):
    store, controller = access
    change(store, "role", rules=(Rule("device", frozenset({"projects.create"}), frozenset({"dev-a"})),))
    store.view = replace(store.view, resource=Resource(
        "dev-a", "team", "device", visibility="space", allowed_actions=ACTIONS["device"]))
    assert request(controller, "projects.create", "device", "dev-a")
    with pytest.raises(AccessDenied):
        request(controller, "roles.update", "device", "dev-a")
    store.view = replace(store.view, resource=Resource(
        "project-a", "team", "project", visibility="space", allowed_actions=ACTIONS["project"]))
    with pytest.raises(AccessDenied):
        request(controller)


def test_rule_exclusion_is_local_not_global(access):
    store, controller = access
    excluded = Rule("project", frozenset({"read"}), all_in_space=True, excluded_ids=frozenset({"project-a"}))
    change(store, "role", rules=(excluded,))
    with pytest.raises(AccessDenied):
        request(controller)
    change(store, "role", rules=(excluded, Rule("project", frozenset({"read"}), frozenset({"project-a"}))))
    assert request(controller)


@pytest.mark.parametrize("kind,action", [("project", "roles.update"), ("device", "execute"), ("*", "read")])
def test_unknown_or_mismatched_actions_fail_before_store_read(access, kind, action):
    store, controller = access
    with pytest.raises(AccessDenied):
        request(controller, action, kind)
    assert store.reads == 0


def test_missing_view_denied(access):
    store, controller = access
    store.view = None
    with pytest.raises(AccessDenied):
        request(controller)


@pytest.mark.parametrize("kwargs", [
    {"actions": frozenset({"*"})}, {"actions": frozenset({"devices.manage"})},
    {"actions": frozenset()}, {"actions": {"read"}}, {"all_in_space": "true"},
    {"resource_ids": frozenset({"*"})}, {"resource_ids": frozenset({"a\n"})},
    {"resource_kind": "unknown"}, {"resource_ids": frozenset()},
])
def test_invalid_rule_configuration_is_rejected(kwargs):
    values = {"resource_kind": "project", "actions": frozenset({"read"}), "resource_ids": frozenset({"a"})}
    values.update(kwargs)
    with pytest.raises(ValueError):
        Rule(**values)


def test_external_identity_is_exact_issuer_subject_not_email_or_username():
    key = ExternalIdentityKey("https://idp.example.test/application/o/codepier/", "subject-a")
    assert key == ExternalIdentityKey(key.issuer, key.subject)
    assert key != ExternalIdentityKey("https://other-idp.example.test/", key.subject)
    assert key != ExternalIdentityKey(key.issuer, "Subject-a")
    assert key != ExternalIdentityKey(key.issuer.rstrip("/"), key.subject)


@pytest.mark.parametrize("issuer,subject", [
    ("http://idp.example.test/", "a"), ("https://idp.example.test/?query=x", "a"),
    ("https://idp.example.test/#part", "a"), ("https://user:pass@idp.example.test/", "a"),
    ("https://idp.example.test/\n", "a"), ("https://idp.example.test:70000/", "a"),
    ("https://idp.example.test/", ""), ("https://idp.example.test/", "a" * 256),
    ("https://idp.example.test/", "a\n"), ("https://idp.example.test/", "非ASCII"),
])
def test_invalid_external_identity_key_rejected(issuer, subject):
    with pytest.raises(ValueError):
        ExternalIdentityKey(issuer, subject)
