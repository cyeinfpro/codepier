"""The unified query/act tools must keep every capability and add none.

Reachability is checked against the live registries, not a hand-written list:
every legacy facade operation (hub.tool_discovery.catalogs) and every public
collaboration action (shared.public_collaboration.ACTIONS) has exactly one
unified address, every internal target field stays expressible, and query
never reaches a mutation.
"""
from __future__ import annotations

import asyncio
import base64
import json

import pytest

import shared.public_collaboration as pc
from hub.principal import Principal
from hub.tool_discovery import catalogs
from hub import unified_tools as ut
from shared import unified_contracts as u
from shared.contracts import TOOLS
from shared.util import DevError

ME = Principal(actor='user:u1', user_id='u1', scopes={'read', 'write', 'execute'}, projects=[])
KEY = 'k' * 12


def run(coroutine):
    return asyncio.run(coroutine)


def legacy_pairs():
    pairs = {(tool, op) for tool, ops in catalogs().items() for op in ops}
    return pairs | {(name, None) for name in u.KNOWLEDGE_TOOLS} | {('get_profile', None), ('get_access_context', None), ('workspace', 'help')}


def core_addresses():
    found = {}
    for unified in ('query', 'act'):
        for action, views in u.CORE_ACTIONS[unified].items():
            for view, (tool, op) in views.items():
                found.setdefault((tool, op or u.IMPLICIT_OPERATION.get(tool)), []).append((unified, action, view))
    return found


def internal_target(tool, op):
    if tool in u.STANDALONE_TOOLS:
        return tool
    return catalogs()[tool][op or u.IMPLICIT_OPERATION[tool]]


def test_every_legacy_facade_operation_has_a_unified_address():
    found = core_addresses()
    missing = legacy_pairs() - set(found)
    assert not missing
    assert set(found) <= legacy_pairs()
    # workspace.open is reachable read-only (query project) and with a baseline (act baseline).
    assert {key for key, value in found.items() if len(value) > 1} == {('workspace', 'open')}


def test_every_public_collaboration_action_has_exactly_one_address():
    seen = {}
    for unified in ('query', 'act'):
        for action, views in u.COLLAB_ACTIONS[unified].items():
            for view, (tool, op) in views.items():
                assert view == op
                seen.setdefault((tool, op), []).append((unified, action))
    expected = {(tool, op) for tool, ops in pc.ACTIONS.items() for op in ops}
    assert set(seen) == expected
    assert all(len(addresses) == 1 for addresses in seen.values())
    assert len(expected) == 126
    assert all(tool == u.CQ for views in u.COLLAB_ACTIONS['query'].values() for tool, _ in views.values())
    assert all(tool != u.CQ for views in u.COLLAB_ACTIONS['act'].values() for tool, _ in views.values())


def test_legacy_collaboration_names_route_to_a_unified_address():
    addresses = {(tool, op) for unified in ('query', 'act')
                 for views in u.COLLAB_ACTIONS[unified].values() for tool, op in views.values()}
    routed = 0
    for name in pc.LEGACY_TOOLS:
        try:
            request = pc.request(name, {})
        except ValueError:
            continue  # Not a public action today; still routable by its legacy name.
        routed += 1
        assert (request['tool'], request['arguments']['action']) in addresses, name
    assert routed >= 60


def test_query_never_reaches_a_mutation():
    for action, views in u.CORE_ACTIONS['query'].items():
        if action == 'help':
            continue
        for view, (tool, op) in views.items():
            target = internal_target(tool, op)
            scope = TOOLS[target].scope
            assert scope in {'read', 'devices.read'} or target in u.READ_ONLY_COMPUTER_TARGETS, (action, view, target)


def test_query_rejects_baseline_capture():
    with pytest.raises(DevError):
        u.translate('query', {'action': 'project', 'project': 'P', 'args': {'capture_baseline': True}})
    with pytest.raises(DevError, match='idempotency_key'):
        u.translate('act', {'action': 'baseline', 'project': 'P'})
    plan = u.translate('act', {'action': 'baseline', 'project': 'P', 'idempotency_key': KEY})
    assert (plan.tool, plan.args) == ('workspace', {'operation': 'open', 'project': 'P',
                                                   'idempotency_key': KEY, 'capture_baseline': True})


def test_review_management_keeps_write_authority_and_does_not_capture_baseline():
    plan = u.translate('query', {'action': 'changes', 'view': 'reviews', 'project': 'P'})
    assert (plan.tool, plan.args) == ('read', {'operation': 'reviews', 'project': 'P'})
    assert TOOLS[internal_target(plan.tool, 'reviews')].scope == 'read'
    for action in ('compact', 'release'):
        options = {'action': action}
        if action == 'release':
            options['baseline_ref'] = 'a' * 32
        plan = u.translate('act', {'action': 'baseline', 'view': 'manage',
                                  'project': 'P', 'idempotency_key': KEY, 'args': options})
        assert (plan.tool, plan.args) == ('edit', {'operation': 'reviews', 'project': 'P',
                                                  'idempotency_key': KEY, 'options': options})
        target = TOOLS[internal_target(plan.tool, 'reviews')]
        assert target.scope == 'write'
        target.model.model_validate({'project': 'P', 'idempotency_key': KEY, **options})


@pytest.mark.parametrize('address', sorted(
    (unified, action, view) for unified in ('query', 'act') for action, views in u.CORE_ACTIONS[unified].items()
    for view in views if action != 'help'))
def test_every_internal_target_field_stays_expressible(address):
    unified, action, view = address
    tool, op = u.CORE_ACTIONS[unified][action][view]
    target = internal_target(tool, op)
    wanted = set(TOOLS[target].model.model_fields) - {'operation'}
    if tool == 'process':
        wanted -= {'operation_id'}  # expressed as ids -> operation_ids
    legacy = u.legacy_call(tool, op, {name: name for name in wanted})
    placed = set(legacy) | set(legacy.get('options', {}))
    assert wanted <= placed


def test_translation_produces_valid_legacy_calls():
    cases = [
        ('act', {'action': 'exec', 'project': 'P', 'idempotency_key': KEY, 'command': 'ls'},
         'exec', {'project': 'P', 'idempotency_key': KEY, 'command': 'ls'}),
        ('act', {'action': 'edit', 'project': 'P', 'idempotency_key': KEY, 'path': 'a.txt', 'content': 'x',
                 'expected_sha256': 'new'},
         'write', {'operation': 'file', 'project': 'P', 'idempotency_key': KEY, 'path': 'a.txt', 'content': 'x',
                   'expected_sha256': 'new'}),
        ('act', {'action': 'edit', 'project': 'P', 'idempotency_key': KEY, 'path': 'a.txt',
                 'edits': [{'old_text': 'a', 'new_text': 'b'}], 'expected_sha256': 'f' * 64},
         'edit', {'operation': 'file', 'project': 'P', 'idempotency_key': KEY, 'path': 'a.txt',
                  'edits': [{'old_text': 'a', 'new_text': 'b'}], 'expected_sha256': 'f' * 64}),
        ('query', {'action': 'job', 'ids': ['a' * 32]}, 'process', {'operation': 'get', 'operation_ids': ['a' * 32]}),
        ('query', {'action': 'job'}, 'process', {'operation': 'list'}),
        ('query', {'action': 'file', 'project': 'P', 'path': 'README.md'},
         'read', {'operation': 'file', 'project': 'P', 'path': 'README.md'}),
        ('query', {'action': 'file', 'project': 'P', 'args': {'items': [{'path': 'a'}]}},
         'read', {'operation': 'batch', 'project': 'P', 'options': {'items': [{'path': 'a'}]}}),
        ('query', {'action': 'project', 'project': 'P'}, 'workspace', {'operation': 'open', 'project': 'P'}),
        ('query', {'action': 'project'}, 'workspace', {'operation': 'list'}),
        ('query', {'action': 'tree', 'project': 'P', 'args': {'depth': 3}},
         'workspace', {'operation': 'tree', 'project': 'P', 'depth': 3}),
        ('query', {'action': 'targets'}, 'vps', {}),
        ('query', {'action': 'whoami'}, 'get_access_context', {}),
        ('act', {'action': 'cancel', 'ids': ['b' * 32], 'idempotency_key': KEY},
         'process', {'operation': 'cancel', 'operation_ids': ['b' * 32], 'idempotency_key': KEY}),
    ]
    for unified, raw, tool, args in cases:
        plan = u.translate(unified, raw)
        assert (plan.tool, plan.args) == (tool, args), raw
        TOOLS[tool].model.model_validate(args)


def test_collaboration_translation_keeps_the_public_action():
    plan = u.translate('act', {'action': 'collab.send', 'view': 'room_reply',
                               'args': {'body_text': 'hi', 'handle': 'cph1.x'}})
    assert (plan.tool, plan.args, plan.handle, plan.wait) == ('collaboration', {'action': 'room_reply', 'body_text': 'hi'},
                                                              'cph1.x', 0)
    with pytest.raises(DevError):
        u.translate('act', {'action': 'collab.room', 'view': 'room_update', 'args': {'action': 'pause'}})


def test_translation_rejects_ambiguity_and_wrong_tool():
    with pytest.raises(DevError):
        u.translate('query', {'action': 'exec', 'project': 'P'})
    with pytest.raises(DevError):
        u.translate('act', {'action': 'exec', 'project': 'P', 'args': {'project': 'Q'}})
    with pytest.raises(DevError):
        u.translate('query', {'action': 'tree', 'ids': ['x']})
    with pytest.raises(DevError):
        u.translate('query', {'action': 'job', 'view': 'nope'})
    with pytest.raises(DevError):
        u.translate('query', {'action': 'file', 'unknown_top_level': 1})


def test_hints_point_to_unified_addresses():
    assert u.unified_hint('task_query', {'operation': 'wait', 'operation_ids': ['x'], 'wait_seconds': 10}) == {
        'name': 'query', 'arguments': {'action': 'job', 'view': 'wait', 'ids': ['x'], 'args': {'wait_seconds': 10}}}
    assert u.unified_hint('project_query', {'operation': 'open', 'project': 'P'}) == {
        'name': 'query', 'arguments': {'action': 'project', 'project': 'P'}}
    assert u.unified_hint('workspace', {'operation': 'open', 'project': 'P', 'capture_baseline': True}) == {
        'name': 'act', 'arguments': {'action': 'baseline', 'project': 'P'}}
    assert u.unified_hint('collaboration_query', {'action': 'room_inbox', 'conversation_id': 'c'}) == {
        'name': 'query', 'arguments': {'action': 'collab.inbox', 'view': 'room_inbox', 'args': {'conversation_id': 'c'}}}
    assert u.unified_hint('read', {'operation': 'changes', 'project': 'P', 'options': {'review_ref': 'r'}}) == {
        'name': 'query', 'arguments': {'action': 'changes', 'project': 'P', 'args': {'review_ref': 'r'}}}
    assert u.unified_hint('unknown', {}) is None


def test_presentation_drops_duplicates_only():
    receipt = {'operation_id': 'x', 'tool': 'shell_exec', 'state': 'succeeded', 'pending': False, 'output': 'hello',
               'next_call': None, 'recovery': {'task_state': 'succeeded', 'task_outcome_confirmed': True},
               'result': {'ok': True, 'data': {'exit_code': 0, 'output': 'hello', 'command_ok': True,
                                               'failure_context': {'category': 'succeeded'}, 'continuation': {'a': 1}}}}
    lean = u.present_unified({'operations': [receipt]})['operations'][0]
    assert lean['output'] == 'hello' and lean['result']['data']['output_at'] == 'output'
    assert 'output' not in lean['result']['data'] and 'next_call' not in lean
    assert 'recovery' not in lean
    for key in ('failure_context', 'continuation'):
        assert key not in lean['result']['data']
    full = u.present_unified(receipt, verbosity='full')
    assert full['result']['data']['output'] == 'hello' and 'recovery' in full
    failed = u.present_unified({**receipt, 'state': 'failed', 'error': 'boom'})
    assert 'recovery' in failed
    pending = {'operation_id': 'x', 'pending': True, 'next_call': {'name': 'task_query', 'arguments': {
        'operation': 'wait', 'operation_ids': ['x']}}}
    assert u.present_unified(pending)['next_call'] == {'name': 'query', 'arguments': {
        'action': 'job', 'view': 'wait', 'ids': ['x']}}


def test_catalog_is_small_and_annotated():
    definitions = ut.tool_definitions(None, collaboration=True)
    assert [item['name'] for item in definitions] == ['query', 'act']
    from shared.contracts import tool_definitions as legacy_definitions
    identity = [item for item in legacy_definitions() if item['name'] == 'get_profile']
    size = len(json.dumps(definitions + identity, ensure_ascii=False))
    assert size <= 10_000, size
    query, act = definitions
    assert query['annotations']['readOnlyHint'] is True and act['annotations']['readOnlyHint'] is False
    assert len(query['inputSchema']['properties']['action']['enum']) == 29
    assert len(act['inputSchema']['properties']['action']['enum']) == 27
    assert 'outputSchema' not in query


def test_discovery_follows_scopes():
    assert ut.visible_actions('act', {'read'}) == []
    assert [item['name'] for item in ut.tool_definitions({'read'})] == ['query']
    readable = ut.visible_actions('query', {'read'})
    assert 'devices' not in readable and 'file' in readable and 'collab.inbox' not in readable
    assert 'devices' in ut.visible_actions('query', {'read', 'devices.read'})
    assert 'collab.inbox' in ut.visible_actions('query', {'read'}, collaboration=True)
    writable = ut.visible_actions('act', {'read', 'write'})
    assert 'edit' in writable and 'baseline' in writable and 'exec' not in writable
    assert 'exec' in ut.visible_actions('act', {'read', 'execute'})
    assert 'gui' in ut.visible_actions('act', {'read', 'computer'})


class FakeStore:
    def encrypt(self, value):
        return base64.urlsafe_b64encode(value[::-1].encode()).decode()

    def decrypt(self, value):
        return base64.urlsafe_b64decode(value.encode()).decode()[::-1]

    async def run(self, function, *args):
        return function(*args)


class FakeRuntime:
    def __init__(self, responses):
        self.responses, self.calls, self.store = list(responses), [], FakeStore()

    async def invoke(self, name, arguments, principal):
        self.calls.append((name, arguments))
        return self.responses.pop(0)


def test_handle_is_bound_to_identity_and_expires(monkeypatch):
    store = FakeStore()
    token = ut.mint_handle(store, ME, {'consumer_proof': 'p', 'consumer_fence': 3, 'ignored': 'x'})
    assert ut.open_handle(store, ME, token) == {'consumer_proof': 'p', 'consumer_fence': 3}
    other = Principal(actor='user:u2', user_id='u2', scopes={'read'}, projects=[])
    with pytest.raises(DevError):
        ut.open_handle(store, other, token)
    with pytest.raises(DevError):
        ut.open_handle(store, ME, 'cph1.not-a-token')
    monkeypatch.setattr(ut.time, 'time', lambda: 10 ** 12)
    with pytest.raises(DevError):
        ut.open_handle(store, ME, token)


def test_handle_replaces_repeated_consumer_credentials():
    runtime = FakeRuntime([{'consumer_session_id': 's1', 'consumer_fence': 2, 'consumer_proof': 's' * 40},
                           {'ok': True}])
    begin = run(ut.prepare(runtime, 'act', {'action': 'collab.consume', 'view': 'consumer_begin', 'args': {
        'project': 'P', 'dot_id': 'd1', 'binding_generation': 1, 'wake_ticket': 'w' * 32,
        'consumer_run_id': 'r1', 'idempotency_key': KEY}}, ME))
    value, name, args = run(ut.execute(runtime, begin, ME))
    assert name == 'collaboration' and args['action'] == 'consumer_begin'
    handle = value['handle']
    beat = run(ut.prepare(runtime, 'act', {'action': 'collab.consume', 'view': 'consumer_heartbeat', 'args': {
        'project': 'P', 'handle': handle, 'idempotency_key': KEY + 'x'}}, ME))
    assert beat.args['consumer_session_id'] == 's1' and beat.args['consumer_fence'] == 2
    assert beat.args['consumer_proof'] == 's' * 40 and beat.args['dot_id'] == 'd1'
    pc.ADAPTERS['collaboration'].validate_python(beat.args)
    explicit = run(ut.prepare(runtime, 'act', {'action': 'collab.consume', 'view': 'consumer_heartbeat', 'args': {
        'project': 'P', 'handle': handle, 'consumer_fence': 5, 'idempotency_key': KEY + 'y'}}, ME))
    assert explicit.args['consumer_fence'] == 5


def test_inline_wait_polls_the_same_operation():
    runtime = FakeRuntime([{'operation_id': 'op1', 'pending': True, 'state': 'queued'},
                           {'operations': [{'operation_id': 'op1', 'pending': True, 'state': 'running'}]},
                           {'operations': [{'operation_id': 'op1', 'pending': False, 'state': 'succeeded'}]}])
    plan = run(ut.prepare(runtime, 'act', {'action': 'exec', 'project': 'P', 'idempotency_key': KEY,
                                           'command': 'ls'}, ME))
    value, name, args = run(ut.execute(runtime, plan, ME))
    assert (name, args) == ('process', {'operation': 'wait', 'operation_ids': ['op1']})
    assert value['operations'][0]['state'] == 'succeeded'
    assert [call[0] for call in runtime.calls] == ['exec', 'process', 'process']
    assert all(call[1]['operation_ids'] == ['op1'] for call in runtime.calls[1:])


def test_wait_zero_returns_the_receipt():
    runtime = FakeRuntime([{'operation_id': 'op1', 'pending': True}])
    plan = run(ut.prepare(runtime, 'act', {'action': 'exec', 'project': 'P', 'idempotency_key': KEY,
                                           'command': 'ls', 'wait': 0}, ME))
    value, name, _ = run(ut.execute(runtime, plan, ME))
    assert name == 'exec' and value['pending'] is True and len(runtime.calls) == 1


def test_help_resolves_to_the_legacy_schema():
    unified, action, view, tool, operation = u.resolve_help('job', 'wait')
    assert (unified, action, view, tool, operation) == ('query', 'job', 'wait', 'process', 'wait')
    assert u.resolve_help('exec')[3:] == ('exec', 'run')
    assert u.resolve_help('collab.send', 'room_reply')[3:] == ('collaboration', 'room_reply')
    assert u.resolve_help('workflow', 'update')[:3] == ('act', 'workflow', 'update')


@pytest.mark.parametrize('tool,action', [('query', 'source_check'), ('act', 'import')])
def test_native_attachment_contract_and_legacy_translation(tool, action):
    from jsonschema import Draft202012Validator
    from hub.core_tools import public_call
    file = {'file_id': 'synthetic-id', 'download_url': 'https://files.oaiusercontent.com/fixture'}
    raw = {'action': action, 'project': 'P', 'file': file, 'args': {'idempotency_key': KEY}}
    if action == 'import':
        raw['args'].update(path='fixture.txt', expected_sha256='a' * 64)
    definition = next(item for item in ut.tool_definitions() if item['name'] == tool)
    assert definition['_meta']['openai/fileParams'] == ['file']
    assert definition['annotations']['openWorldHint'] is True
    Draft202012Validator(definition['inputSchema']).validate(raw)
    plan = u.translate(tool, raw)
    expected = public_call('inspect_file_source' if action == 'source_check' else 'download_artifact',
                           {'project': 'P', 'file': file, **raw['args']})
    assert plan.tool == 'write'
    assert TOOLS['write'].model.model_validate(plan.args) == TOOLS['write'].model.model_validate(expected[1])
    assert plan.args['file'] == file and 'file' not in plan.args.get('options', {})
    TOOLS['write'].model.model_validate(plan.args)
    nested = {**raw, 'args': {**raw['args'], 'file': file}}
    with pytest.raises(DevError):
        u.translate(tool, nested)
    legacy_nested = {k: v for k, v in nested.items() if k != 'file'}
    assert u.translate(tool, legacy_nested).args == plan.args


def test_capability_help_hints_round_trip():
    from shared.runtime_results import capability_index
    for hint in capability_index('P').values():
        rewritten = u.unified_hint(hint['name'], hint['arguments'])
        plan = u.translate(rewritten['name'], rewritten['arguments'])
        request = plan.help_for
        _, _, _, tool, operation = u.resolve_help(request['action'], request['view'], request['tool'])
        assert (tool, operation) == (hint['arguments']['tool'], hint['arguments']['action'])
    unknown = {'operation': 'help', 'tool': 'unknown', 'action': 'unknown'}
    assert u.unified_hint('project_query', unknown) is None


@pytest.mark.parametrize('tool', ['query', 'act'])
def test_native_attachment_rejects_malformed_identity(tool):
    with pytest.raises(DevError):
        u.translate(tool, {'action': 'source_check' if tool == 'query' else 'import',
                           'file': {'download_url': 'https://files.oaiusercontent.com/fixture'}})


@pytest.mark.parametrize('legacy_tool,operation', [('process', 'search_start'), ('process', 'validate'),
                                                   ('workspace', 'tasks'), ('read', 'changes'),
                                                   ('read', 'artifacts')])
def test_rewritten_help_identifies_unified_target(legacy_tool, operation):
    hint = u.unified_hint('project_query', {'operation': 'help', 'tool': legacy_tool, 'action': operation})
    request = hint['arguments']['args']
    assert request['tool'] in {'query', 'act'}
    assert u.resolve_help(request['action'], request['view'], request['tool'])[3:] == (legacy_tool, operation)


def test_unified_help_projects_actual_inputs_and_visible_views():
    from jsonschema import Draft202012Validator
    from hub.unified_help import _EXAMPLES, action_help

    visible = ut.directory(ME.scopes, collaboration=True)
    for tool, action, view in _EXAMPLES:
        address = u.resolve_help(action, view, tool, visible=visible)
        brief = action_help(address, visible[tool][action], project='P')
        detailed = action_help(address, visible[tool][action], project='P', detail='schema')
        assert 'inputSchema' not in brief and 'outputSchema' not in brief
        example = detailed['example']
        Draft202012Validator(detailed['inputSchema']).validate(example)
        plan = u.translate(tool, example)
        TOOLS[plan.tool].model.model_validate(plan.args)
    open_help = action_help(u.resolve_help('project', 'open', 'query'), ['open', 'list'], detail='schema')
    assert {'capture_baseline', 'baseline_paths'}.isdisjoint(open_help['inputSchema']['properties']['args']['properties'])
    assert 'idempotency_key' in action_help(u.resolve_help('baseline'), ['open'])['required']
    assert 'max_results' in action_help(u.resolve_help('search', 'start'), ['sync', 'start'])['args']
    assert 'max_results' not in action_help(u.resolve_help('search', 'sync'), ['sync', 'start'])['args']
    app_status = u.resolve_help('gui', 'app_status', 'query', visible=ut.directory({'read'}))
    gui_help = action_help(app_status, ['status', 'app_status'], detail='schema', scopes={'read'})
    assert gui_help['inputSchema']['properties']['args']['properties']['probe']['const'] is False
    with pytest.raises(DevError, match='outside the current tool directory'):
        u.resolve_help('exec', tool='act', visible=ut.directory({'read'}))
    with pytest.raises(DevError, match='Unknown action'):
        u.resolve_help('made_up', tool='query')


def test_hints_filter_scope_preserve_cursor_and_project_nested_requests():
    from shared.runtime_results import capability_index
    visible = ut.directory({'read'}, collaboration=True)
    value = {'capability_index': capability_index('P'),
             'configuration': {'inbox_request': {'tool': 'collaboration_query', 'arguments': {
                 'action': 'room_inbox', 'conversation_id': 'room1'}}},
             'next_call': {'name': 'task_query', 'arguments': {'operation': 'get',
                 'operation_ids': ['original'], 'after_output_seq': 2}}}
    plan = u.translate('query', {'action': 'project', 'project': 'P'})
    public = u.present_unified(value, visible=visible, plan=plan)
    assert 'validation' not in public['capability_index']
    assert public['capability_index']['search']['arguments']['args']['view'] == 'sync'
    assert public['next_call']['arguments']['ids'] == ['original']
    assert public['next_call']['arguments']['args']['after_output_seq'] == 2
    assert public['configuration']['inbox_request']['arguments'] == {
        'action': 'collab.inbox', 'view': 'room_inbox', 'args': {'conversation_id': 'room1'}}
    assert 'tool' in value['configuration']['inbox_request']


def test_successful_exec_projection_matches_inline_wait_and_retains_failure_evidence():
    plan = u.translate('act', {'action': 'exec', 'project': 'P', 'command': 'ls', 'idempotency_key': KEY})
    data = {'exit_code': 0, 'command_ok': True, 'output': 'output', 'output_truncated': False,
            'runtime': {'runtime_ref': 'ref', 'revision': 'rev', 'mode': 'clean', 'schema_version': 1}}
    terminal = {'operation_id': 'original', 'id': 'original', 'state': 'succeeded', 'pending': False,
                'output': 'output', 'output_seq': 2, 'created': 12, 'next_attempt': 13,
                'result': {'ok': True, 'data': data}}
    lean = u.present_unified(terminal, plan=plan)
    assert lean == u.present_unified({'operations': [terminal]}, plan=plan)
    assert lean['output_seq'] == 2 and lean['exit_code'] == 0 and lean['output'] == 'output'
    assert lean['runtime']['runtime_ref'] == 'ref' and 'next_attempt' not in lean
    assert u.present_unified(terminal, plan=plan, verbosity='full')['created'] == 12
    assert terminal['result']['data']['runtime']['schema_version'] == 1

    failed = {'operation_id': 'original', 'state': 'failed', 'output_seq': 2,
              'output': '/bin/sh: 1: rg: not found\n', 'result': {'ok': True,
                  'data': {'exit_code': 127, 'command_ok': False}}}
    shown = u.present_unified(failed, plan=plan, visible=ut.directory({'read', 'execute'}))
    assert shown['error']['code'] == shown['error_code'] == 'COMMAND_MISSING'
    assert shown['state'] == 'failed' and shown['exit_code'] == 127 and shown['output_seq'] == 2
    assert shown['search_help']['arguments']['args']['view'] == 'sync'


def test_recovery_separates_argument_rejection_from_transport_and_unknown_outcomes():
    from shared.operation_recovery import error_recovery, operation_recovery

    rejected = error_recovery('INVALID_ARGUMENTS')
    assert rejected['execution_started'] is False and rejected['next_action'] == 'correct_arguments'
    assert error_recovery('INVALID_ARGUMENTS', 'original')['next_action'] == 'query_original_operation'
    disconnected = operation_recovery({'state': 'reconnecting', 'id': 'original', 'pending': True},
                                      online=False, after_output_seq=2)
    assert disconnected['category'] == 'transport_interrupted'
    assert disconnected['operation_id'] == 'original' and disconnected['after_output_seq'] == 2
    uncertain = operation_recovery({'state': 'needs_review', 'id': 'original'}, online=True)
    assert uncertain['category'] == 'outcome_unconfirmed' and not uncertain['task_outcome_confirmed']
    assert uncertain['next_action'] == 'query_original_operation' and not uncertain['automatic_replay']
