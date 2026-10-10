"""Account recovery and Space-scoped role limits, using disposable fixtures."""
from __future__ import annotations

import pytest

from hub import iam
from shared.crypto import digest
from shared.util import DevError
from tests.test_iam_integration import team as team
from tests.test_roles import must, role, update_role


def invitation(browsers):
    return must(browsers['owner'].post('/api/iam/spaces/team/invites',
                json={'level': 'member'}), 201)['invitation']


def unused(store, value):
    return store.one('SELECT used_by FROM space_invites WHERE hash=?', (digest(value),))['used_by'] is None


def test_account_without_any_space_can_accept_invitation_and_audit_target(team):
    app, browsers = team
    value = invitation(browsers)
    store = app.state.store
    store.execute('UPDATE memberships SET active=0 WHERE user_id=?', ('bob',))
    assert browsers['bob'].get('/api/iam/me').json()['spaces'] == []
    # A removed Space left in the tab cannot prevent account-local recovery.
    response = browsers['bob'].post('/api/iam/invites/accept', json={'invitation': value},
                                    headers={'X-CodePier-Space': 'removed-tab-space'})
    assert response.status_code == 200, response.text
    assert response.json() == {'space_id': 'team'}
    assert iam.membership(store, 'bob', 'team')['level'] == 'member'
    audit = store.one("SELECT space_id,owner_user_id FROM audit WHERE action='invitation.accepted'")
    assert audit == {'space_id': 'team', 'owner_user_id': 'bob'}
    assert not iam.is_space_admin(store, 'bob', 'team')
    assert browsers['bob'].get('/api/projects').json()['projects'] == []
    assert browsers['bob'].post('/api/iam/invites/accept',
                                json={'invitation': value}).status_code == 400


@pytest.mark.parametrize('denial', ['csrf', 'session', 'account', 'identity'])
def test_invitation_recovery_never_bypasses_account_checks(team, monkeypatch, denial):
    app, browsers = team
    store = app.state.store
    value = invitation(browsers)
    headers = {}
    if denial == 'csrf':
        headers['X-RD-CSRF'] = 'invalid'
    elif denial == 'session':
        headers['Cookie'] = ''
    elif denial == 'account':
        store.execute("UPDATE iam_users SET active=0 WHERE user_id='bob'")
    else:
        original = iam.check_identity
        def stale(store, identity_id, *, require_fresh=True):
            if require_fresh:
                raise DevError('ENTITLEMENTS_STALE', 'Synthetic stale identity', 403)
            return original(store, identity_id, require_fresh=False)
        monkeypatch.setattr(iam, 'check_identity', stale)
    response = browsers['bob'].post('/api/iam/invites/accept',
                                    json={'invitation': value}, headers=headers)
    assert response.status_code in (401, 403), response.text
    assert unused(store, value)
    assert not store.one("SELECT 1 FROM memberships WHERE user_id='bob' AND source LIKE 'invite:%'")


def test_invitation_cannot_bypass_target_suspension(team):
    app, browsers = team
    value = invitation(browsers)
    store = app.state.store
    store.execute("INSERT INTO membership_blocks VALUES('team','bob',1)")
    response = browsers['bob'].post('/api/iam/invites/accept', json={'invitation': value})
    assert response.status_code == 403, response.text
    assert response.json()['error']['code'] == 'SPACE_FORBIDDEN'
    assert unused(store, value)
    assert store.one("SELECT blocked FROM membership_blocks WHERE space_id='team' AND user_id='bob'")['blocked'] == 1


@pytest.mark.parametrize('denial', ['inviter', 'expiry', 'space'])
def test_invitation_recovery_rechecks_target_authority(team, denial):
    app, browsers = team
    value = invitation(browsers)
    store = app.state.store
    if denial == 'inviter':
        store.execute("UPDATE memberships SET level='member' WHERE user_id='owner' AND space_id='team'")
    elif denial == 'expiry':
        store.execute('UPDATE space_invites SET expires=1 WHERE hash=?', (digest(value),))
    else:
        store.execute("UPDATE spaces SET active=0 WHERE id='team'")
    response = browsers['bob'].post('/api/iam/invites/accept', json={'invitation': value})
    assert response.status_code == 400, response.text
    assert unused(store, value)


def test_role_creation_cap_is_shared_by_space_and_not_by_creator(team, monkeypatch):
    import hub.roles as roles
    app, browsers = team
    monkeypatch.setattr(roles, 'MAX_ROLES', 2)
    app.state.store.execute("UPDATE memberships SET level='admin' WHERE user_id='alice' AND space_id='team'")
    first = role(browsers['owner'], label='A', project_rules=[])
    role(browsers['alice'], label='B', project_rules=[])
    response = browsers['alice'].post('/api/access-roles',
                json={'label': 'C', 'idempotency_key': 'space-cap-third-role'})
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'ROLE_LIMIT'
    # Quota in this Space does not consume the same creator's other Space.
    other = role(browsers['legacy'], label='Other Space', project_rules=[])
    assert {r['id'] for r in browsers['legacy'].get('/api/access-roles').json()['roles']} == {other['id']}
    assert first['id'] not in {r['id'] for r in browsers['legacy'].get('/api/access-roles').json()['roles']}


def test_grandfathered_overquota_roles_remain_visible_and_editable(team, monkeypatch):
    import hub.roles as roles
    app, browsers = team
    app.state.store.execute("UPDATE memberships SET level='admin' WHERE user_id='alice' AND space_id='team'")
    saved = [role(browsers['owner'], label=name, project_rules=[]) for name in ('A', 'B')]
    saved.append(role(browsers['alice'], label='C', project_rules=[]))
    # Represents a historical Space that predates the shared creation limit.
    monkeypatch.setattr(roles, 'MAX_ROLES', 2)
    result = browsers['owner'].get('/api/access-roles').json()
    assert result['limit'] == 2 and result['total'] == 3
    assert {r['id'] for r in result['roles']} == {r['id'] for r in saved}
    changed = must(update_role(browsers['owner'], saved[-1], enabled=False))
    assert not changed['enabled']
    # Ordinary members still see only roles currently assigned to them.
    assert browsers['bob'].get('/api/access-roles').json()['roles'] == []
