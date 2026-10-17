"""Runtime requirements augment existing owner/grant checks, never replace them."""
from copy import deepcopy
from types import SimpleNamespace
import json

import pytest
from pydantic import ValidationError

from hub.project_runtimes import (parse_advertisement, configured_requirement,
    attach_requirement, current_requirement)
from hub.room_scope import current, require_action, require_tool
from shared.contracts import TOOLS
from shared.util import DevError
from tests.test_dot_room_scope import setup
from tests.collaboration_support import collab, key  # noqa: F401


REV = 'a' * 64


def configured(fixture, monkeypatch):
    s, owner, actor, room, args = setup(fixture, execute=True)
    project = s.runtime.project('proj', owner)
    connection = SimpleNamespace(project_runtime_protocol=1, project_runtime_profiles=[{
        'project': 'proj', 'mode': 'container_v1', 'driver': 'docker_restricted_v1', 'revision': REV}],
        tool_protocol=None)
    monkeypatch.setattr(s.runtime, 'online', lambda _: True)
    monkeypatch.setattr(s.runtime, 'connection_authorized', lambda *_: True)
    s.runtime.connections[project['device_id']] = connection
    args = {**args, 'execution_mode': 'require_os_isolation', 'acknowledge_unsandboxed_exec': False}
    return s, owner, actor, room, args, project, connection


def test_validated_handshake_is_capability_not_execution_proof():
    assert parse_advertisement({}) == (0, [])
    assert parse_advertisement({'project_runtime_protocol': True}) == (0, [])
    hello = {'project_runtime_protocol': 1, 'project_runtime_profiles': [{
        'project': 'P', 'mode': 'container_v1', 'driver': 'docker_restricted_v1', 'revision': REV}]}
    protocol, rows = parse_advertisement(hello)
    assert protocol == 1 and rows == hello['project_runtime_profiles']
    assert 'isolation_verified' not in rows[0]
    with pytest.raises(ValueError):
        parse_advertisement({**hello, 'project_runtime_profiles': [{**rows[0], 'approved': True}]})
    with pytest.raises(ValueError):
        parse_advertisement({**hello, 'project_runtime_profiles': [rows[0], rows[0]]})


def test_strict_owner_selection_binds_requirement_without_claiming_verification(collab, monkeypatch):
    s, owner, actor, _, args, project, _ = configured(collab, monkeypatch)
    original_grant = s.store.one("SELECT * FROM grants WHERE id='broad'")
    receipt = s.room_scope.set(args, owner)
    assert receipt['os_sandbox_verified'] is False
    record = current(s.store, actor.grant_id)
    requirement = record['manifest']['project_rules'][0]['runtime_requirement']
    assert requirement == configured_requirement(s.runtime, project)
    assert s.store.one("SELECT * FROM grants WHERE id='broad'") == original_grant
    require_action(s.store, actor, 'execute', project_id='proj')
    require_tool(s.store, actor, 'exec', {'project': 'proj', 'command': 'true', 'target': 'agent'})
    with pytest.raises(DevError):
        require_tool(s.store, actor, 'exec', {'project': 'proj', 'command': 'true', 'target': 'vps:' + 'f' * 32})


def test_legacy_or_replaced_connection_cannot_satisfy_strict_requirement(collab, monkeypatch):
    s, owner, _, _, args, project, connection = configured(collab, monkeypatch)
    connection.project_runtime_protocol = 0
    with pytest.raises(DevError) as error:
        s.room_scope.set(args, owner)
    assert error.value.code == 'ROOM_SCOPE_ISOLATION_UNAVAILABLE'
    assert not s.store.all('SELECT * FROM room_scope_manifests')
    connection.project_runtime_protocol = 1
    monkeypatch.setattr(s.runtime, 'connection_authorized', lambda *_: False)
    with pytest.raises(DevError):
        configured_requirement(s.runtime, project)


def test_original_requirement_is_not_downgraded_after_owner_changes_current_mode(collab, monkeypatch):
    s, owner, actor, _, args, project, connection = configured(collab, monkeypatch)
    s.room_scope.set(args, owner)
    request = {'tool': 'exec', 'args': {'project': 'proj', 'command': 'true', 'target': 'agent'}}
    saved = attach_requirement(s.runtime, request, actor, project, admission=True)
    assert saved == request['project_runtime_requirement']
    s.room_scope.set({**args, 'expected_version': 1, 'execution_mode': 'account_permissions_acknowledged',
                     'acknowledge_unsandboxed_exec': True, 'idempotency_key': key()}, owner)
    assert current_requirement(s.store, actor, 'proj', 'exec', request['args']) is None
    assert attach_requirement(s.runtime, request, actor, project, connection) == saved
    connection.project_runtime_profiles[0]['revision'] = 'b' * 64
    with pytest.raises(DevError) as error:
        attach_requirement(s.runtime, request, actor, project, connection)
    assert error.value.code == 'RUNTIME_CONFIGURATION_CHANGED'
    assert request['project_runtime_requirement'] == saved


def test_older_operation_cannot_be_reinterpreted_when_scope_tightens(collab, monkeypatch):
    s, owner, actor, _, args, project, connection = configured(collab, monkeypatch)
    request = {'tool': 'exec', 'args': {'project': 'proj', 'command': 'true', 'target': 'agent'}}
    original = deepcopy(request)
    s.room_scope.set(args, owner)
    with pytest.raises(DevError) as error:
        attach_requirement(s.runtime, request, actor, project, connection)
    assert error.value.code == 'RUNTIME_CONFIGURATION_CHANGED'
    assert request == original


def test_execution_request_cannot_supply_its_own_requirement_or_approval():
    with pytest.raises(ValidationError):
        TOOLS['exec'].model.model_validate({'project': 'P', 'command': 'true', 'idempotency_key': key(),
            'project_runtime_requirement': {'approved': True}})


def test_runtime_binding_is_persisted_with_new_operation_not_in_mutable_instructions(collab, monkeypatch):
    s, owner, actor, _, args, project, _ = configured(collab, monkeypatch)
    s.room_scope.set(args, owner)
    request = TOOLS['exec'].model.model_validate({'project': 'proj', 'command': 'printf checked',
                'idempotency_key': key()}).model_dump()
    operation, result = s.runtime._admit_operation('exec', request, project, actor, background=True)
    assert result is None
    row = s.store.one('SELECT * FROM operations WHERE id=?', (operation,))
    saved = json.loads(s.store.decrypt(row['payload']))
    assert saved['project_runtime_requirement']['revision'] == REV
    assert saved['project_runtime_requirement']['project_id'] == 'proj'
    assert 'project_runtime_requirement' not in saved['args']
    again, _ = s.runtime._admit_operation('exec', request, project, actor, background=True)
    assert again == operation
