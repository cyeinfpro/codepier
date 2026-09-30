"""Security regressions found during final multi-user/OIDC integration review."""
import pytest

from tests.test_iam_integration import team as team, Browser
from tests.test_oidc_integration import oidc as oidc, start, callback
from tests.test_roles import must


def test_space_admin_cannot_restore_suspended_owner(team):
    app, browsers = team
    store = app.state.store
    store.execute("UPDATE memberships SET level='owner' WHERE user_id='alice' AND space_id='team'")
    store.execute("UPDATE memberships SET level='admin' WHERE user_id='bob' AND space_id='team'")
    path = '/api/iam/spaces/team/members/alice/suspension'
    must(browsers['owner'].put(path, json={'blocked': True}))
    denied = browsers['bob'].put(path, json={'blocked': False})
    assert denied.status_code == 403
    assert denied.json()['error']['code'] == 'OWNER_REQUIRED'
    assert store.one("SELECT blocked FROM membership_blocks WHERE space_id='team' AND user_id='alice'")['blocked'] == 1
    must(browsers['owner'].put(path, json={'blocked': False}))
    assert browsers['alice'].get('/api/projects').status_code == 200


def local_browser(app, template, user_id):
    with app.state.store.transaction():
        session = app.state.auth.new_session(user_id)
    return Browser(template.client, session['cookie'], session['csrf'], template.space)


@pytest.mark.parametrize("when", ["pending", "inflight"])
def test_unlink_cancels_pending_link_even_in_another_local_session(oidc, when):
    app, browsers, fake, provider, _ = oidc
    state, response = start(oidc, browsers['alice'], link=True)
    assert callback(oidc, state, response, browser=browsers['alice']).status_code == 303
    identity = app.state.store.one('SELECT * FROM external_identities')
    first = local_browser(app, browsers['alice'], 'alice')
    second = local_browser(app, browsers['alice'], 'alice')
    state, response = start(oidc, first, link=True)
    def unlink():
        fake.on_userinfo = None
        if when == 'pending':
            must(second.delete('/api/iam/identities/' + identity['id']))
        else:
            # The callback is awaiting UserInfo here. Apply the same unlink
            # mutation as the concurrent HTTP route, without re-entering the
            # blocking TestClient from its event-loop thread.
            with app.state.store.transaction():
                app.state.oidc.disable_identity(identity, 'unlinked', revoke=True)
    if when == 'pending':
        unlink()
    else:
        fake.on_userinfo = unlink
    denied = callback(oidc, state, response, browser=first)
    assert denied.status_code in (400, 401, 403)
    row = app.state.store.one('SELECT * FROM external_identities WHERE id=?', (identity['id'],))
    assert row['enabled'] == 0 and row['disabled_reason'] == 'unlinked'
    # A genuinely new explicit link remains possible for the same local owner.
    state, response = start(oidc, first, link=True)
    assert callback(oidc, state, response, browser=first).status_code == 303
