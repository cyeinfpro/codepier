"""Root diagnostics inspect mapping metadata without widening a credential."""
import json

import pytest

from shared.util import DevError
from tests.collaboration_support import collab as collab
from tests.test_dot_room_scope import setup
from tests.test_collaboration_delegation_bridge import bridge as bridge


@pytest.mark.parametrize("other_root", [
    "/tmp/dot-scope-fixture/A", "/tmp/dot-scope-fixture/A/private-B",
])
def test_root_preflight_is_read_only_and_redacts_outside_projects(collab, other_root):
    s, owner, actor, room, args = setup(collab)
    s.store.execute("UPDATE projects SET root=? WHERE id='otherproj'", (other_root,))
    before = s.store.one("SELECT * FROM grants WHERE id='broad'")
    result = s.room_scope.listing(args, owner)
    assert result["project_root_checks"] == [{
        "project_id": "proj", "overlap_with_project_ids": [], "has_outside_projects": True,
    }]
    assert "otherproj" not in json.dumps(result["project_root_checks"])
    assert other_root not in json.dumps(result)
    assert not s.store.all("SELECT * FROM room_scope_manifests")
    assert s.store.one("SELECT * FROM grants WHERE id='broad'") == before
    with pytest.raises(DevError) as error:
        s.room_scope.set(args, owner)
    assert error.value.code == "ROOM_SCOPE_ROOT_OVERLAP"
    assert error.value.details["project_id"] == "proj"
    assert "项目管理" in error.value.message
    assert "本次未更改连接权限" in error.value.message
    assert "otherproj" not in json.dumps(error.value.details)
    assert not s.store.all("SELECT * FROM room_scope_manifests")


@pytest.mark.parametrize("other_root", [
    "/tmp/dot-scope-fixture/A", "/tmp/dot-scope-fixture/A/nested",
])
def test_explicitly_selected_same_root_and_child_are_not_false_conflicts(collab, other_root):
    s, owner, actor, room, args = setup(collab, two_projects=True)
    s.store.execute("UPDATE projects SET root=? WHERE id='otherproj'", (other_root,))
    checks = {row["project_id"]: row for row in s.room_scope.listing(args, owner)["project_root_checks"]}
    assert checks["proj"]["overlap_with_project_ids"] == ["otherproj"]
    assert not checks["proj"]["has_outside_projects"]
    assert s.room_scope.set(args, owner)["credential_bound"]
    from hub.room_scope import descriptor
    assert descriptor(s.store, actor)["credential_bound"]


def test_cross_device_same_root_is_not_a_conflict(collab):
    s, owner, actor, room, args = setup(collab)
    s.store.execute("INSERT INTO devices(id,name,secret,created) SELECT 'other-node','Other fixture',secret,created FROM devices WHERE id='dev'")
    s.store.execute("UPDATE projects SET root='/tmp/dot-scope-fixture/A',device_id='other-node' WHERE id='otherproj'")
    assert s.room_scope.listing(args, owner)["project_root_checks"] == [{
        "project_id": "proj", "overlap_with_project_ids": [], "has_outside_projects": False,
    }]
    assert s.room_scope.set(args, owner)["credential_bound"]


def test_windows_parent_root_stays_blocked_without_exposing_outside_id(collab):
    s, owner, actor, room, args = setup(collab)
    s.store.execute("UPDATE projects SET root=? WHERE id='proj'", (r"C:\Work\Root",))
    s.store.execute("UPDATE projects SET root=? WHERE id='otherproj'", (r"c:\work\root\Private",))
    assert s.room_scope.listing(args, owner)["project_root_checks"][0]["has_outside_projects"]
    with pytest.raises(DevError) as error:
        s.room_scope.set(args, owner)
    assert error.value.code == "ROOM_SCOPE_ROOT_OVERLAP"


def test_root_preflight_honors_requested_conversation_and_owner(bridge):
    b = bridge
    response = b.client.get("/api/collaboration/room-scopes", params={
        **b.scope, "conversation_id": b.room["id"],
    })
    assert response.is_success, response.text
    assert response.json()["read_only"]
    assert response.json()["project_root_checks"][0]["project_id"] == "project"
    rejected = b.client.get("/api/collaboration/room-scopes", params={
        **b.scope, "conversation_id": "not-an-owned-conversation",
    })
    assert rejected.status_code == 404
    assert not b.app.state.store.all("SELECT * FROM room_scope_manifests")


def test_new_overlap_still_fails_live_authorization_after_preflight(collab):
    s, owner, actor, room, args = setup(collab)
    assert not s.room_scope.listing(args, owner)["project_root_checks"][0]["has_outside_projects"]
    s.room_scope.set(args, owner)
    s.store.execute("UPDATE projects SET root='/tmp/dot-scope-fixture/A/new-child' WHERE id='otherproj'")
    from hub.access_profiles import refresh_profile_principal
    with pytest.raises(DevError) as error:
        refresh_profile_principal(s.store, actor)
    assert error.value.code == "ROOM_SCOPE_ROOT_OVERLAP"
