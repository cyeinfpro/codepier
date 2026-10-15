"""Combined project-view and owner-context boundaries using real emitted values."""
from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator

from shared.mcp_presentation import present
from shared.public_collaboration import resolve, tool_definitions
from shared.util import DevError
from tests.collaboration_support import collab as collab, key
from tests.test_collaboration_dot_chat import duplex, inbox, owner_message
from tests.test_collaboration_dots import call
from tests.test_owner_task_context import ask, item


@pytest.mark.parametrize('name', ['collaboration_query', 'collaboration_dot_inbox'])
def test_real_inbox_context_and_protocol_survive_minimal_view(collab, name):
    c = duplex(collab)
    source = owner_message(c, 'Only read the fixture README; keep the original permission boundary.')[0]['message']
    raw = inbox(c)
    before = deepcopy(raw)
    value = present(name, {'action': 'dot_inbox'}, raw)
    assert value['task_context_protocol'] == raw['task_context_protocol']
    current = next(row for row in value['items'] if row['message_id'] == source['id'])
    original = next(row for row in raw['items'] if row['message_id'] == source['id'])
    for field in ('execution_context', 'execution_route', 'response_request_options', 'task_request'):
        assert current[field] == original[field]
    assert current['execution_context']['grants_authority'] is False
    assert current['execution_route']['host_approval']['state'] == 'not_evaluated'
    assert value['task_context_protocol']['host_approval_evaluated'] is False
    assert raw == before


@pytest.mark.parametrize('name', ['collaboration_query', 'collaboration_delegation_read'])
def test_real_delegation_and_confirmation_keep_original_context(collab, name):
    c = duplex(collab)
    source = owner_message(c, 'Read only the independent fixture file.')[0]['message']
    original = call(c, item(c, source['id'])['task_request'])
    detail = call(c, original['read_request'])
    value = present(name, {'action': 'delegation'}, detail)
    for field in ('execution_context', 'execution_route', 'response_request_options', 'delegation'):
        assert value[field] == detail[field]
    assert value['execution_context']['source_message_id'] == source['id']
    question = ask(c, source['id'])
    reply = owner_message(c, '可以')[0]['message']
    page = present('collaboration_query', {'action': 'dot_inbox'}, inbox(c))
    current = next(row for row in page['items'] if row['message_id'] == reply['id'])
    assert current['task_continuation']['host_approval_satisfied'] is False
    assert current['task_continuation']['grants_authority'] is False
    assert call(c, current['task_request'])['delegation_id'] == original['delegation_id']
    assert question['task_continuation']['source_message_id'] == source['id']
    assert not c.s.store.all('SELECT * FROM operations')


def test_projected_historical_context_retains_original_read_permission(collab):
    from tests.test_conversation_project_revision_history import context, cross_reply, revoke_source, SECRET
    c, _, historical = context(collab)
    source = cross_reply(c, historical)
    page = present('collaboration_query', {'action': 'dot_inbox'}, inbox(c))
    current = next(row for row in page['items'] if row['message_id'] == source['id'])
    ref = next(row for row in current['execution_context']['references']
               if row['message_id'] == historical['id'])
    assert ref['read_request']['arguments']['project'] == 'otherproj'
    assert ref['is_execution_authority'] is False
    assert call(c, ref['read_request'])['message']['id'] == historical['id']
    revoke_source(c)
    with pytest.raises(DevError) as error:
        call(c, ref['read_request'])
    assert SECRET not in str(error.value)
    assert not c.s.store.all('SELECT * FROM operations')


def test_refreshed_public_catalog_exposes_exact_optional_question_contract(collab):
    c = duplex(collab)
    source = owner_message(c, 'Discuss the fixture read before proceeding.')[0]['message']
    current = item(c, source['id'])
    args = {'action': 'dot_message', **current['interim_reply_arguments'],
            'idempotency_key': key(), 'body_text': 'Read only README?',
            'response_request': {**current['response_request_options'], 'reason': 'clarification'}}
    definition = next(row for row in tool_definitions() if row['name'] == 'collaboration')
    schema = definition['inputSchema']
    assert 'response_request' not in schema.get('required', [])
    Draft202012Validator(schema).validate(args)
    name, resolved = resolve('collaboration', args)
    assert name == 'collaboration_dot_message'
    assert resolved['response_request'] == args['response_request']
    assert resolved['complete'] is False
    # The public adapter must retain the underlying business validator.
    with pytest.raises(DevError):
        resolve('collaboration', {**args, 'complete': True})
