"""Panel evidence HTTP routing through real auth, service and isolated SQLite."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hub.auth import Auth
from hub.collaboration.api import make_router
from hub.collaboration.common import canonical, digest
from hub.http import install_http_behaviors
from tests.collaboration_support import collab, key  # noqa: F401


@pytest.fixture
def evidence_http(collab):
    service, owner, worker, _, room, agents, _, scope = collab
    conversation = service.conversations.create({
        'title': 'Evidence source', 'projects': [{'project': 'proj'}],
        'idempotency_key': key(),
    }, owner)['conversation']
    unrelated = service.conversations.create({
        'title': 'Unrelated conversation', 'projects': [{'project': 'proj'}],
        'idempotency_key': key(),
    }, owner)['conversation']
    other_room = service.room_create({
        'project': 'otherproj', 'idempotency_key': key(),
    }, owner)['room']
    selected = {**scope, 'room_id': room['id'], 'conversation_id': conversation['id']}
    message = service.chatroom.create({
        **selected, 'body_text': 'Inspect this synthetic evidence',
        'client_message_id': key(), 'idempotency_key': key(),
    }, owner)['message']
    task = service.chatroom.to_task({
        **selected, 'message_id': message['id'], 'expected_message_version': 1,
        'assignee_agent_id': agents[0]['id'], 'kind': 'analyze_incident',
        'request': 'Inspect this project', 'acceptance': 'Bounded evidence',
        'idempotency_key': key(),
    }, owner)
    for evidence_id in ('http_evidence', 'unrelated_evidence'):
        body = canonical({'observations': ['Synthetic ' + evidence_id]})
        service.store.execute(
            'INSERT INTO monitor_evidence (id,room_id,body,digest,created,expires_at) VALUES (?,?,?,?,?,?)',
            (evidence_id, room['id'], body, digest(body), service.clock(), service.clock() + 3600),
        )
    context = json.loads(service.store.one(
        'SELECT context FROM collaboration_jobs WHERE id=?', (task['job_id'],),
    )['context'])
    context['evidence_refs'] = ['http_evidence']
    service.store.execute(
        'UPDATE collaboration_jobs SET context=? WHERE id=?',
        (canonical(context), task['job_id']),
    )
    lease = service.claim({
        **scope, 'job_id': task['job_id'], 'expected_version': 1,
        'idempotency_key': key(),
    }, worker)
    submitted = service.submit({
        **scope, 'job_id': task['job_id'], 'attempt': lease['attempt'],
        'fencing_token': lease['fencing_token'], 'idempotency_key': key(),
        'result': {'outcome': 'explained', 'summary': 'Synthetic explanation',
                   'observations': [{'claim': 'Fixture', 'evidence_refs': ['http_evidence']}]},
    }, worker)

    # Exercise FastAPI's actual query parsing and panel authentication without
    # loading unrelated static bundles or starting background monitor loops.
    auth = Auth(service.store)
    with service.store.transaction():
        session = auth.new_session(owner.user_id)
    service.runtime.collaboration = service
    service.health = {}  # The isolated fixture does not start lifecycle components.
    app = FastAPI()
    install_http_behaviors(app)
    app.include_router(make_router(auth, service.runtime))
    with TestClient(app) as client:
        client.cookies.set('rd_session', session['cookie'])
        yield client, {
            'selected': selected, 'job_id': task['job_id'], 'result_id': submitted['result_id'],
            'unrelated_conversation_id': unrelated['id'], 'other_room': other_room,
        }


@pytest.mark.parametrize('context_field', ['result_id', 'job_id'])
def test_panel_evidence_http_forwards_authorized_context(evidence_http, context_field):
    client, fixture = evidence_http
    response = client.get('/api/collaboration', params={
        **fixture['selected'], 'kind': 'evidence', 'id': 'http_evidence',
        context_field: fixture[context_field],
    })
    assert response.status_code == 200, response.text
    assert response.json()['id'] == 'http_evidence'
    assert 'components' in response.json()


@pytest.mark.parametrize('context_field', ['result_id', 'job_id'])
def test_panel_evidence_http_rejects_unbound_and_foreign_context(evidence_http, context_field):
    client, fixture = evidence_http
    params = {**fixture['selected'], 'kind': 'evidence', 'id': 'http_evidence'}
    missing = client.get('/api/collaboration', params=params)
    assert missing.status_code == 422, missing.text
    assert missing.json()['error']['code'] == 'EVIDENCE_CONTEXT_REQUIRED'

    params[context_field] = fixture[context_field]
    mismatch = client.get('/api/collaboration', params={**params, 'id': 'unrelated_evidence'})
    assert mismatch.status_code == 403, mismatch.text
    assert mismatch.json()['error']['code'] == 'EVIDENCE_CONTEXT_MISMATCH'

    foreign_conversation = client.get('/api/collaboration', params={
        **params, 'conversation_id': fixture['unrelated_conversation_id'],
    })
    assert foreign_conversation.status_code == 404, foreign_conversation.text
    assert foreign_conversation.json()['error']['code'] == 'NOT_FOUND'

    other = fixture['other_room']
    foreign_room = client.get('/api/collaboration', params={
        **params, 'project': other['project_id'], 'environment_id': other['environment_id'],
        'room_id': other['id'], 'conversation_id': other['id'],
    })
    assert foreign_room.status_code == 404, foreign_room.text
    assert foreign_room.json()['error']['code'] == 'NOT_FOUND'


def test_panel_evidence_http_requires_panel_session(evidence_http):
    client, fixture = evidence_http
    client.cookies.clear()
    response = client.get('/api/collaboration', params={
        **fixture['selected'], 'kind': 'evidence', 'id': 'http_evidence',
        'result_id': fixture['result_id'],
    })
    assert response.status_code == 401, response.text
    assert response.json()['error']['code'] == 'LOGIN_REQUIRED'
