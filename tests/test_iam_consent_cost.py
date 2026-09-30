"""Batch delegation checks stay linear and still use current human authority."""
from __future__ import annotations

import uuid

import pytest

from hub import roles
from tests.test_iam_integration import team as team, shared_role
from tests.test_iam_review_regressions import projects
from tests.test_access_profiles import start_oauth
from tests.test_roles import must


def submit(app, browsers, kind, selected, suffix):
    client = browsers['alice']
    body = {'label': 'fixed-' + suffix, 'scopes': ['read'], 'projects': selected,
            'idempotency_key': uuid.uuid4().hex}
    if kind == 'profile':
        return client.post('/api/access-profiles', json=body)
    if kind == 'pat':
        return client.post('/api/grants', json={'label': body['label'], 'scopes': body['scopes'], 'projects': selected})
    _, request_id, _ = start_oauth(client, scopes='read')
    must(client.get('/api/oauth/requests/' + request_id))
    return client.post('/api/oauth/requests/' + request_id + '/decide',
                       json={'allow': True, 'scopes': ['read'], 'projects': selected})


@pytest.mark.parametrize('kind', ['profile', 'pat', 'oauth'])
def test_bulk_delegation_evaluates_policy_linearly(team, monkeypatch, record_property, kind):
    app, browsers = team
    store = app.state.store
    shared_role(app, browsers)
    original = roles.project_actions
    evaluations = []
    def counting(*args, **kwargs):
        evaluations.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(roles, 'project_actions', counting)
    def measured(count):
        projects(store, count)
        selected = ['project-team', *[f'load-{n}' for n in range(count)]]
        evaluations.clear()
        response = submit(app, browsers, kind, selected, str(count))
        assert response.status_code in (200, 201), response.text
        return len(evaluations)
    small, large = measured(10), measured(250)
    record_property('policy_evaluations_11_projects', small)
    record_property('policy_evaluations_251_projects', large)
    # A constant number of full maps per real request, NOT one map per ID.
    assert large <= small * 24 + 251
    assert large <= 15 * 251


@pytest.mark.parametrize('kind', ['profile', 'pat', 'oauth'])
def test_delegation_after_role_removal_does_not_reuse_prior_allow(team, kind):
    app, browsers = team
    store = app.state.store
    shared_role(app, browsers)
    selected = ['project-team']
    assert submit(app, browsers, kind, selected, 'before').status_code in (200, 201)
    store.execute("UPDATE role_assignments SET active=0 WHERE user_id='alice'")
    response = submit(app, browsers, kind, selected, 'after')
    assert response.status_code == 403, response.text


@pytest.mark.parametrize('kind', ['profile', 'pat', 'oauth'])
def test_one_unassigned_project_rejects_entire_delegation(team, kind):
    from tests.test_roles import update_role
    app, browsers = team
    current = shared_role(app, browsers)
    projects(app.state.store, 10)
    must(update_role(browsers['owner'], current,
        project_rules=[{'actions': ['read'], 'projects': ['project-team']}]))
    response = submit(app, browsers, kind, ['project-team', 'load-0'], 'mixed')
    assert response.status_code == 403, response.text
    assert not app.state.store.all("SELECT id FROM grants WHERE user_id='alice'")
