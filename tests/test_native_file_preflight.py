"""Read-only native attachment source preflight with synthetic, offline inputs."""
from __future__ import annotations

import copy
import json
import os
import socket
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from agent import incoming_artifacts as incoming
from agent.filesystem import FileEngine
from agent.integration_config import validate_integrations
from agent.journal import Journal
from hub.core_tools import help_result, invoke, public_call, result as core_result
from shared.contracts import MUTATING, OUTPUT_SCHEMAS, TOOLS, tool_definitions
from shared.core_contracts import CORE_ACTIONS
from shared.file_sources import (
    DEFAULT_FILE_HOSTS,
    DEFAULT_MAX_IMPORT_BYTES,
    FILE_SOURCE_POLICY_VERSION,
    file_source_hosts,
)
from shared.integration_contracts import REMOTE_TOOLS, Mutation
from shared.tool_protocol import negotiate, require_compatible, wire_version
from shared.util import DevError

HOST = 'oaisdmntprkoreacentral.blob.core.windows.net'
PRIVATE_PATH = 'PRIVATE_SYNTHETIC_OBJECT.zip'
PRIVATE_TICKET = 'PRIVATE_SYNTHETIC_SIGNATURE'
PRIVATE_ID = 'PRIVATE_SYNTHETIC_FILE_ID'


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    config_path = tmp_path / 'config.json'
    config_path.write_text('{}')
    journal = Journal(tmp_path / 'state')
    config = {'allowed_roots': [{'path': str(root), 'writable': True}], 'tasks': {}}
    engine = FileEngine(config, journal, config_path)
    project = {'id': 'p', 'alias': 'P', 'root': str(root), 'mode': 'write'}
    yield engine, project, root
    journal.db.close()


def native_file(host=HOST, **metadata):
    return {
        'download_url': f'https://{host}/{PRIVATE_PATH}?sig={PRIVATE_TICKET}',
        'file_id': PRIVATE_ID,
        'file_name': PRIVATE_PATH,
        **metadata,
    }


def inspect_args(file=None):
    return {'project': 'P', 'idempotency_key': uuid.uuid4().hex,
            'file': native_file() if file is None else file}


def check(workspace, file=None):
    engine, project, _ = workspace
    return incoming.inspect_file_source(engine, project, inspect_args(file))


def assert_no_transfer(result):
    assert result['checked'] is True
    assert result['request_sent'] is False
    assert result['created'] is False
    assert result['host_roundtrip'] == 'not_run'
    assert result['policy_scope'] == 'execution_node'
    assert result['source_policy_version'] == FILE_SOURCE_POLICY_VERSION
    serialized = json.dumps(result, ensure_ascii=False)
    for private in (PRIVATE_PATH, PRIVATE_TICKET, PRIVATE_ID):
        assert private not in serialized
    assert not {'download_url', 'file_id', 'file_name', 'name', 'path'} & result.keys()


def test_preflight_default_source_is_an_offline_policy_check(workspace):
    result = check(workspace)
    assert_no_transfer(result)
    assert result['source_allowed'] is True
    assert result['source_host'] == HOST
    assert result['source_scheme'] == 'https'
    assert result['policy_mode'] == 'default'
    assert result['max_bytes'] == DEFAULT_MAX_IMPORT_BYTES
    assert result['declared_size'] is None
    assert result['size_allowed'] is None
    assert not result.get('approval_required')
    assert not result.get('approval_target')
    assert incoming.DEFAULT_HOSTS == DEFAULT_FILE_HOSTS


@pytest.mark.parametrize('host', [
    'unapproved.example.com',
    'attacker.blob.core.windows.net',
    'oaisdmntprattacker.blob.core.windows.net',
    HOST + '.evil.example',
    'files.oaiusercontent.com.evil.example',
    'evil-files.oaiusercontent.com',
])
def test_unapproved_exact_host_returns_bounded_approval_preview(workspace, host):
    result = check(workspace, native_file(host, size=7))
    assert_no_transfer(result)
    assert result['source_allowed'] is False
    assert result['source_host'] == host
    assert result['reason'] == 'host_not_allowed'
    assert result['recovery'] == 'review_local_file_sources'
    assert result['approval_required'] is True
    assert result['approval_target'] == {
        'config_key': 'integrations.extra_file_hosts',
        'hosts': [host],
        'scope': 'execution_node',
        'applies_to': 'all_projects_on_node',
    }
    assert result['size_allowed'] is True
    assert host not in file_source_hosts(workspace[0].config.get('integrations', {}))


@pytest.mark.parametrize('url,reason', [
    ('http://files.oaiusercontent.com/object', 'unsupported_scheme'),
    ('sandbox:/mnt/data/object', 'unsupported_scheme'),
    ('file:///tmp/object', 'unsupported_scheme'),
    ('https://@files.oaiusercontent.com/object', 'invalid_url'),
    ('https://files.oaiusercontent.com:444/object', 'invalid_url'),
    ('https://files.oaiusercontent.com/object#', 'invalid_url'),
    ('https://files.oaiusercontent.com/obj\\ect', 'invalid_url'),
    ('https://files.oaiusercontent.com/obj\tect', 'invalid_url'),
    ('https://files.oaiusercontent.com/object?token=bad\x7f', 'invalid_url'),
    ('\nhttps://files.oaiusercontent.com/object', 'invalid_url'),
    ('https://files.oaiusercontent.com/object\x00', 'invalid_url'),
    ('https://127.0.0.1/object', 'invalid_url'),
    ('https://[::1]/object', 'invalid_url'),
    ('https://bad..example.com/object', 'invalid_url'),
    ('https://fíles.oaiusercontent.com/object', 'invalid_url'),
])
def test_malformed_sources_never_suggest_a_policy_extension(workspace, url, reason):
    result = check(workspace, {**native_file(), 'download_url': url})
    assert_no_transfer(result)
    assert result['source_allowed'] is False
    assert result['reason'] == reason
    assert result['recovery'] == 'provide_native_file'
    assert not result.get('approval_required')
    assert not result.get('approval_target')


@pytest.mark.parametrize('hosts', [[], ['files.oaiusercontent.com'], ['only.example.com']])
def test_preflight_preserves_explicit_restrictive_policy(workspace, hosts):
    engine, _, _ = workspace
    engine.config['integrations'] = validate_integrations({'file_hosts': hosts})
    before = copy.deepcopy(engine.config)
    result = check(workspace)
    assert_no_transfer(result)
    assert result['source_allowed'] is False
    assert result['policy_mode'] == 'explicit'
    assert engine.config == before
    assert file_source_hosts(engine.config['integrations']) == tuple(hosts)


def test_existing_exact_extension_is_honored_without_changing_default_mode(workspace):
    engine, _, _ = workspace
    engine.config['integrations'] = validate_integrations(
        {'extra_file_hosts': ['UPLOADS.EXAMPLE.COM.']})
    before = copy.deepcopy(engine.config)
    result = check(workspace, native_file('uploads.example.com'))
    assert_no_transfer(result)
    assert result['source_allowed'] is True
    assert result['policy_mode'] == 'default'
    assert not result.get('approval_required')
    assert engine.config == before
    assert HOST in file_source_hosts(engine.config['integrations'])


@pytest.mark.parametrize('size,allowed', [(None, None), (0, True), (1, True),
                                        (1024, True), (1025, False)])
def test_declared_size_is_a_separate_bounded_preview(workspace, size, allowed):
    engine, _, _ = workspace
    engine.config['integrations'] = {'max_import_bytes': 1024}
    file = native_file(**({} if size is None else {'size': size}))
    result = check(workspace, file)
    assert_no_transfer(result)
    assert result['source_allowed'] is True
    assert result['max_bytes'] == 1024
    assert result['declared_size'] == size
    assert result['size_allowed'] is allowed


def test_unapproved_and_oversized_file_keeps_both_results(workspace):
    engine, _, _ = workspace
    engine.config['integrations'] = {'max_import_bytes': 1024}
    result = check(workspace, native_file('unapproved.example.com', size=1025))
    assert_no_transfer(result)
    assert result['source_allowed'] is False
    assert result['size_allowed'] is False
    assert result['reason'] == 'host_not_allowed'


@pytest.mark.parametrize('file', [
    native_file(size=1),
    native_file('unapproved.example.com', size=1),
    {**native_file(), 'download_url': 'sandbox:/mnt/data/' + PRIVATE_PATH},
    native_file(size=DEFAULT_MAX_IMPORT_BYTES + 1),
])
def test_preflight_never_resolves_downloads_or_creates_files(workspace, monkeypatch, file):
    engine, project, root = workspace
    before_config = copy.deepcopy(engine.config)
    before_files = sorted(path.relative_to(root).as_posix() for path in root.rglob('*'))

    def forbidden(*args, **kwargs):
        pytest.fail('Source preflight must not perform DNS, network or filesystem writes')

    with monkeypatch.context() as patch:
        patch.setattr(socket, 'getaddrinfo', forbidden)
        patch.setattr(socket, 'socket', forbidden)
        patch.setattr(incoming, 'PublicTLSConnection', forbidden)
        patch.setattr(incoming, 'download_chunks', forbidden)
        patch.setattr(incoming, 'AnchoredDestination', forbidden)
        patch.setattr(Path, 'mkdir', forbidden)
        patch.setattr(Path, 'open', forbidden)
        for name in ('mkdir', 'makedirs', 'open', 'write', 'link', 'replace'):
            patch.setattr(os, name, forbidden)
        result = incoming.inspect_file_source(engine, project, inspect_args(file))

    assert_no_transfer(result)
    assert engine.config == before_config
    assert sorted(path.relative_to(root).as_posix() for path in root.rglob('*')) == before_files
    assert engine.config_path.read_text() == '{}'


def test_preflight_accepts_read_only_projects(workspace):
    engine, project, _ = workspace
    project['mode'] = 'read'
    engine.config['allowed_roots'][0]['writable'] = False
    assert check(workspace)['source_allowed'] is True


def test_preflight_still_requires_an_authorized_project_root(workspace):
    engine, _, root = workspace
    engine.config['allowed_roots'] = []
    with pytest.raises(DevError) as failure:
        check(workspace)
    assert failure.value.code == 'ROOT_NOT_ALLOWED'
    assert not list(root.iterdir())


def test_rejected_preview_and_rendered_receipt_never_echo_native_secrets(workspace):
    file = {**native_file(), 'download_url':
            f'https://{PRIVATE_TICKET}@[malformed]/{PRIVATE_PATH}?sig={PRIVATE_TICKET}'}
    result = check(workspace, file)
    assert_no_transfer(result)
    rendered = core_result('write', {'operation': 'source_check'}, result)
    assert rendered['isError'] is False
    serialized = json.dumps(rendered)
    for private in (PRIVATE_PATH, PRIVATE_TICKET, PRIVATE_ID):
        assert private not in serialized


def test_preflight_backend_is_read_scoped_remote_and_immutable():
    spec = TOOLS['inspect_file_source']
    assert spec.scope == 'read' and spec.local is False
    assert spec.destructive is False
    assert 'inspect_file_source' in REMOTE_TOOLS
    assert 'inspect_file_source' not in MUTATING
    assert issubclass(spec.model, Mutation)
    assert set(spec.model.model_fields) == {'project', 'workspace_id', 'idempotency_key', 'file'}
    assert CORE_ACTIONS['write']['source_check'] == 'inspect_file_source'


@pytest.mark.parametrize('bad_size', [-1, True, '7', 1.5])
def test_preflight_native_file_contract_rejects_invalid_size(bad_size):
    with pytest.raises(ValidationError):
        TOOLS['inspect_file_source'].model.model_validate(
            inspect_args(native_file(size=bad_size)))


def test_help_and_public_call_keep_the_native_file_at_top_level():
    help_info = help_result('write', 'source_check')
    assert help_info['arguments_location'] == 'top-level'
    assert help_info['scope'] == 'read'
    assert help_info['requires_project'] and help_info['requires_idempotency_key']
    schema = help_info['inputSchema']
    Draft202012Validator.check_schema(schema)
    assert {'operation', 'project', 'idempotency_key', 'file'} <= set(schema['required'])
    assert 'options' not in schema['required']
    assert 'file' not in schema['properties'].get('options', {}).get('properties', {})

    name, args = public_call('inspect_file_source', inspect_args())
    assert name == 'write'
    assert args['operation'] == 'source_check' and args['file'] == native_file()
    assert 'file' not in args.get('options', {})
    Draft202012Validator(schema).validate(args)
    args.pop('options', None)
    Draft202012Validator(schema).validate(args)
    args['options'] = {}
    Draft202012Validator(schema).validate(args)


@pytest.mark.asyncio
@pytest.mark.parametrize('with_options', [False, True])
async def test_source_check_facade_forwards_native_file_without_a_destination(with_options):
    principal = object()
    runtime = SimpleNamespace(invoke=AsyncMock(return_value={'checked': True}))
    args = {'operation': 'source_check', **inspect_args()}
    if with_options:
        args['options'] = {}
    assert await invoke(runtime, 'write', args, principal) == {'checked': True}
    target, forwarded, actual_principal = runtime.invoke.call_args.args
    assert target == 'inspect_file_source' and actual_principal is principal
    assert forwarded['file']['download_url'] == native_file()['download_url']
    assert forwarded['file']['file_id'] == PRIVATE_ID
    assert forwarded['project'] == 'P'
    assert forwarded['idempotency_key'] == args['idempotency_key']
    assert not {'path', 'options', 'operation'} & forwarded.keys()
    TOOLS[target].model.model_validate(forwarded)


@pytest.mark.asyncio
async def test_source_check_rejects_duplicate_top_level_and_nested_file():
    runtime = SimpleNamespace(invoke=AsyncMock())
    args = {'operation': 'source_check', **inspect_args(), 'options': {'file': native_file()}}
    with pytest.raises(DevError) as failure:
        await invoke(runtime, 'write', args, object())
    assert failure.value.code == 'INVALID_ARGUMENTS'
    runtime.invoke.assert_not_called()


@pytest.mark.parametrize('host', [HOST, 'unapproved.example.com'])
def test_native_catalog_input_and_output_schemas_cover_preview(workspace, host):
    definitions = {item['name']: item for item in tool_definitions()}
    write = definitions['write']
    assert write['_meta']['openai/fileParams'] == ['file']
    raw = {'operation': 'source_check', **inspect_args(native_file(host))}
    Draft202012Validator(write['inputSchema']).validate(raw)
    preview = check(workspace, native_file(host))
    for schema in (OUTPUT_SCHEMAS['inspect_file_source'], write['outputSchema']):
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(preview)


def test_older_agent_can_still_import_but_cannot_receive_new_preflight():
    old_peer = negotiate({
        'version': '1.20.0', 'contract_protocol': 1, 'catalog_sha256': 'a' * 64,
        'tool_contracts': {'download_artifact': wire_version('download_artifact'),
                           'write': wire_version('write')},
    })
    require_compatible(old_peer, 'download_artifact')
    require_compatible(old_peer, 'write')
    with pytest.raises(DevError) as failure:
        require_compatible(old_peer, 'inspect_file_source')
    assert failure.value.code == 'AGENT_UPGRADE_REQUIRED'

    legacy_peer = negotiate({'version': '1.20.0'})
    require_compatible(legacy_peer, 'download_artifact')
    with pytest.raises(DevError) as failure:
        require_compatible(legacy_peer, 'inspect_file_source')
    assert failure.value.code == 'AGENT_UPGRADE_REQUIRED'


@pytest.mark.asyncio
async def test_import_lost_success_receipt_recovers_from_durable_outbox_once(
        tmp_path, monkeypatch):
    """Drop only the result transport, restart, then recover the same operation."""
    from agent.runner import Agent
    from shared.crypto import digest, token
    from shared.util import atomic_json

    root = tmp_path / 'project'
    root.mkdir()
    config_path = tmp_path / 'config.json'
    atomic_json(config_path, {
        'hub_url': 'http://127.0.0.1:9', 'device_id': 'fixture', 'secret': token(),
        'state_dir': str(tmp_path / 'state'),
        'allowed_roots': [{'path': str(root), 'writable': True, 'allow_tasks': True}],
        'tasks': {},
    })
    data = b'\x00\xffone exact synthetic attachment'
    downloads = []

    def download_once(file, hosts, max_bytes, *, providers=()):
        assert not providers
        downloads.append(file['file_id'])
        assert HOST in hosts and len(data) <= max_bytes
        yield data[:5]
        yield data[5:]

    monkeypatch.setattr(incoming, 'download_chunks', download_once)

    class Socket:
        def __init__(self, fail_result=False):
            self.fail_result = fail_result
            self.sent = []
            self.closed = False

        async def send(self, packet):
            if self.fail_result and packet['type'] == 'result':
                raise ConnectionError('Synthetic transport lost after local publication')
            self.sent.append(copy.deepcopy(packet))

        async def close(self):
            self.closed = True

    project = {'id': 'p', 'alias': 'P', 'root': str(root), 'mode': 'write',
               'allow_tasks': True}
    args = TOOLS['download_artifact'].model.model_validate({
        **inspect_args(native_file(size=len(data))),
        'path': 'assets/fixture.bin', 'expected_sha256': digest(data),
    }).model_dump()
    request = {'id': uuid.uuid4().hex, 'tool': 'download_artifact',
               'tool_contract_version': wire_version('download_artifact'),
               'project': project, 'args': args}
    instance = Agent(config_path)
    try:
        failed_socket = Socket(fail_result=True)
        instance.socket = failed_socket
        instance.channel = SimpleNamespace(pack=lambda packet: packet)
        await instance.handle(copy.deepcopy(request))
        assert failed_socket.closed and instance.socket is None
        assert not any(packet['type'] == 'result' for packet in failed_socket.sent)
        durable = instance.journal.status(request['id'])
        assert durable['status'] == 'done'
        assert durable['result']['ok'] is True, durable['result'].get('error')
        saved_result = durable['result']
        assert saved_result['data']['sha256'] == digest(data)
        assert saved_result['data']['created'] is True
        assert [item['id'] for item in instance.journal.outbox()] == [request['id']]
        target = root / args['path']
        assert target.read_bytes() == data and downloads == [PRIVATE_ID]
        before = target.stat()
        journal_id = instance.journal.journal_id

        # Reopening the actual SQLite journal rules out an in-memory-only cache.
        instance.journal.db.close()
        instance.instance_lock.close()
        instance = Agent(config_path)
        assert instance.journal.journal_id == journal_id
        recovered_socket = Socket()
        instance.socket = recovered_socket
        instance.channel = SimpleNamespace(pack=lambda packet: packet)
        await instance.report_outbox()
        assert recovered_socket.sent[-1] == {
            'type': 'result', 'id': request['id'], 'result': saved_result,
        }

        # Status recovery and identical redelivery both use the saved receipt.
        await instance.report_status(request['id'])
        await instance.handle(copy.deepcopy(request))
        assert recovered_socket.sent[-1]['result'] == saved_result
        assert downloads == [PRIVATE_ID]
        assert target.read_bytes() == data
        assert (target.stat().st_ino, target.stat().st_mtime_ns) == (
            before.st_ino, before.st_mtime_ns)
        instance.journal.ack(request['id'])
        assert instance.journal.outbox() == []

        # A new operation is not a retry and cannot overwrite the prior import.
        fresh = copy.deepcopy(request)
        fresh['id'] = uuid.uuid4().hex
        fresh['args']['idempotency_key'] = uuid.uuid4().hex
        await instance.handle(fresh)
        failure = instance.journal.status(fresh['id'])['result']
        assert failure['ok'] is False
        assert failure['error']['code'] == 'ARTIFACT_DESTINATION_EXISTS'
        assert downloads == [PRIVATE_ID]
        assert target.read_bytes() == data and not list(root.rglob('*.part'))
    finally:
        instance.journal.db.close()
        instance.instance_lock.close()
