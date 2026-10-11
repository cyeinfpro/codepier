"""Synthetic A/B projects: source reads and writes never probe real directories."""
from dataclasses import replace
import json
import time

import pytest

from hub.access_profiles import access_context, refresh_profile_principal
from hub.principal import Principal
from hub.room_scope import descriptor, require_action, require_tool
from shared.util import DevError
from tests.collaboration_support import collab as collab, key


def setup(fixture, *, two_projects=False, execute=False):
    s, owner, *_ = fixture
    scope = fixture[-1]
    owner = replace(owner, scopes={'read', 'write', 'execute'})
    s.store.execute("UPDATE projects SET root='/tmp/dot-scope-fixture/A' WHERE id='proj'")
    s.store.execute("UPDATE projects SET root='/tmp/dot-scope-fixture/B' WHERE id='otherproj'")
    s.store.execute("UPDATE grants SET projects='[\"proj\",\"otherproj\"]',scopes='[\"read\",\"write\",\"execute\"]' WHERE id='broad'")
    actor = Principal('mcp:broad', owner.user_id, {'read', 'write', 'execute'}, ['proj', 'otherproj'],
                      grant_id='broad', space_id=owner.space_id)
    _, room = s.scope(owner, scope)
    if two_projects:
        _, second = s.scope(owner, {**scope, 'project': 'otherproj'}, create=True)
        s.store.execute('INSERT OR IGNORE INTO conversation_projects VALUES (?,?,?)', (room['id'], second['id'], time.time()))
    args = {**scope, 'conversation_id': room['id'], 'grant_id': 'broad', 'expected_version': 0,
            'project_rules': [{'project_id': 'proj', 'capabilities': ['read', 'write'] + (['execute'] if execute else [])}],
            'confirm_connection_wide_ceiling': True, 'idempotency_key': key()}
    if two_projects:
        args['project_rules'].append({'project_id': 'otherproj', 'capabilities': ['read']})
    if execute:
        args.update(execution_mode='account_permissions_acknowledged', acknowledge_unsandboxed_exec=True)
    return s, owner, actor, room, args


def test_legacy_grants_remain_unchanged_until_explicit_owner_narrowing(collab):
    s, owner, actor, room, args = setup(collab)
    original = s.store.one("SELECT * FROM grants WHERE id='broad'")
    assert descriptor(s.store, actor)['credential_bound'] is False
    receipt = s.room_scope.set(args, owner)
    assert s.room_scope.set(args, owner) == receipt
    assert s.store.one("SELECT * FROM grants WHERE id='broad'") == original
    fresh = refresh_profile_principal(s.store, actor)
    assert fresh.projects == ['proj'] and fresh.scopes == {'read', 'write'}
    assert descriptor(s.store, fresh)['credential_bound'] is True
    assert not descriptor(s.store, fresh)['os_sandbox_verified']
    context = access_context(s.store, actor)
    assert [p['id'] for p in context['projects']] == ['proj']
    assert context['room_scope']['grant_id'] == 'broad'
    assert s.room_scope.listing(args, owner)['items'][0]['manifest_digest'] == receipt['manifest_digest']


def test_caller_cannot_opt_out_with_missing_or_fake_room_argument(collab):
    s, owner, actor, room, args = setup(collab)
    s.room_scope.set(args, owner)
    assert s.runtime._invoke('read', {'project': 'proj', 'path': 'README.md'}, actor).project['id'] == 'proj'
    for raw in [{'project': 'otherproj', 'path': 'README.md'}, {'project': 'otherproj', 'path': 'README.md', 'room_id': room['id']}]:
        with pytest.raises(DevError):
            s.runtime._invoke('read', raw, actor)
    with pytest.raises(DevError):
        s.runtime._invoke('write', {'project': 'otherproj', 'path': 'new.txt', 'content': 'fixture',
                                  'expected_sha256': 'new', 'idempotency_key': key()}, actor)
    with pytest.raises(DevError):
        s.runtime.project('otherproj', actor)


def test_per_project_permissions_never_use_a_union_for_writes(collab):
    s, owner, actor, room, args = setup(collab, two_projects=True)
    s.room_scope.set(args, owner)
    fresh = refresh_profile_principal(s.store, actor)
    assert 'write' in fresh.scopes
    require_action(s.store, fresh, 'read', project_id='otherproj')
    require_action(s.store, fresh, 'write', project_id='proj')
    with pytest.raises(DevError, match='能力'):
        require_action(s.store, fresh, 'write', project_id='otherproj')
    with pytest.raises(DevError):
        s.runtime._invoke('write', {'project': 'otherproj', 'path': 'new.txt', 'content': 'fixture',
                                  'expected_sha256': 'new', 'idempotency_key': key()}, actor)


def test_remapping_expiry_and_revocation_fail_closed_without_broad_fallback(collab):
    s, owner, actor, room, args = setup(collab)
    s.room_scope.set(args, owner)
    s.store.execute("UPDATE projects SET root='/tmp/dot-scope-fixture/moved' WHERE id='proj'")
    with pytest.raises(DevError, match='目录'):
        refresh_profile_principal(s.store, actor)
    s.store.execute("UPDATE projects SET root='/tmp/dot-scope-fixture/A' WHERE id='proj'")
    control = {k: args[k] for k in ('project', 'environment_id', 'grant_id')}
    control.update(expected_version=1, action='revoke', idempotency_key=key())
    result = s.room_scope.control(control, owner)
    assert s.room_scope.control(control, owner) == result
    with pytest.raises(DevError, match='不会退回'):
        refresh_profile_principal(s.store, actor)
    assert not s.store.one("SELECT revoked FROM grants WHERE id='broad'")['revoked']


def test_expired_scope_is_not_original_grant_authority(collab):
    s, owner, actor, room, args = setup(collab)
    s.room_scope.set(args, owner)
    s.store.execute("UPDATE room_scope_manifests SET expires_at=? WHERE grant_id='broad'", (time.time() - 1,))
    with pytest.raises(DevError, match='过期'):
        s.runtime._invoke('projects_list', {}, actor)


def test_new_role_or_profile_broadening_does_not_change_room_ceiling(collab):
    s, owner, actor, room, args = setup(collab)
    s.room_scope.set(args, owner)
    s.store.execute("UPDATE grants SET projects='[\"*\"]',scopes='[\"read\",\"write\",\"execute\",\"computer\"]' WHERE id='broad'")
    fresh = refresh_profile_principal(s.store, actor)
    assert fresh.projects == ['proj'] and fresh.scopes == {'read', 'write'}
    s.store.execute("UPDATE grants SET scopes='[\"read\"]' WHERE id='broad'")
    assert refresh_profile_principal(s.store, actor).scopes == {'read'}


def test_strict_shell_is_not_misrepresented_as_a_cwd_sandbox(collab):
    s, owner, actor, room, args = setup(collab)
    with pytest.raises(DevError, match='OS 隔离'):
        s.room_scope.set({**args, 'execution_mode': 'require_os_isolation'}, owner)
    assert not s.store.all('SELECT * FROM room_scope_manifests')
    s.room_scope.set(args, owner)
    with pytest.raises(DevError):
        s.runtime._invoke('exec', {'project': 'proj', 'command': 'printf fixture', 'idempotency_key': key()}, actor)
    for domain in ('computer_screenshot', 'browser_open', 'gateway_call', 'devices_list', 'integrations_list'):
        with pytest.raises(DevError):
            require_tool(s.store, actor, domain, {'project': 'proj'})


def test_explicit_unsandboxed_execution_remains_target_bound(collab):
    s, owner, actor, room, args = setup(collab, execute=True)
    s.room_scope.set(args, owner)
    require_action(s.store, actor, 'execute', project_id='proj')
    with pytest.raises(DevError, match='执行目标'):
        require_tool(s.store, actor, 'exec', {'project': 'proj', 'target': 'vps:' + 'f' * 32,
                                             'command': 'printf fixture'})
    assert descriptor(s.store, actor)['execution_mode'] == 'account_permissions_acknowledged'
    assert descriptor(s.store, actor)['os_sandbox_verified'] is False


def test_cross_conversation_known_ids_and_listing_are_restricted(collab):
    s, owner, actor, room, args = setup(collab)
    other = s.conversations.create({'title': 'Other fixture conversation', 'projects': [
        {'project': 'proj', 'environment_id': args['environment_id']}], 'idempotency_key': key()}, owner)['conversation']
    s.room_scope.set(args, owner)
    with pytest.raises(DevError):
        s.conversations.object(actor, other['id'])
    assert [r['id'] for r in s.conversations.listing(actor)['items']] == [room['id']]
    with pytest.raises(DevError):
        s.scope(actor, {**args, 'conversation_id': other['id']})


def test_ancestor_project_overlap_is_not_a_hidden_read_bypass(collab):
    s, owner, actor, room, args = setup(collab)
    s.store.execute("UPDATE projects SET root='/tmp/dot-scope-fixture/A/private-B' WHERE id='otherproj'")
    with pytest.raises(DevError, match='包含范围外'):
        s.room_scope.set(args, owner)
    assert not s.store.all('SELECT * FROM room_scope_manifests')


def test_connector_cannot_self_approve_or_widen_manifest(collab):
    s, owner, actor, room, args = setup(collab)
    with pytest.raises(DevError, match='管理员'):
        s.room_scope.set(args, actor)
    s.store.execute("UPDATE grants SET scopes='[\"read\"]' WHERE id='broad'")
    with pytest.raises(DevError, match='不能扩大'):
        s.room_scope.set(args, owner)
    assert not s.store.all('SELECT * FROM room_scope_manifests')


def test_manifest_tampering_is_not_an_authority_override(collab):
    s, owner, actor, room, args = setup(collab)
    s.room_scope.set(args, owner)
    row = s.store.one("SELECT * FROM room_scope_manifests WHERE grant_id='broad'")
    payload = json.loads(row['manifest'])
    payload['project_rules'][0]['capabilities'].append('execute')
    s.store.execute("UPDATE room_scope_manifests SET manifest=? WHERE grant_id='broad'", (json.dumps(payload),))
    with pytest.raises(DevError, match='记录无效'):
        refresh_profile_principal(s.store, actor)
