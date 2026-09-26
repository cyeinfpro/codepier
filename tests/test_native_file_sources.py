"""Native-file interoperability without real tickets, accounts or external requests."""
from __future__ import annotations

import hashlib
import io
import json
import socket
import ssl
import uuid
import zipfile
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from agent import incoming_artifacts as incoming
from agent.filesystem import FileEngine
from agent.integration_config import validate_integrations
from agent.journal import Journal
from hub.core_tools import help_result, public_call
from shared.util import DevError, safe_summary
from shared.file_sources import file_source_hosts

AZURE_HOST = 'oaisdmntprkoreacentral.blob.core.windows.net'
TICKET = 'PRIVATE_SYNTHETIC_DOWNLOAD_TICKET'
FILE_ID = 'PRIVATE_SYNTHETIC_FILE_ID'


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / 'project'
    root.mkdir()
    config_path = tmp_path / 'config.json'
    config_path.write_text('{}')
    journal = Journal(tmp_path / 'state')
    config = {'allowed_roots': [{'path': str(root), 'writable': True, 'allow_tasks': True}], 'tasks': {}}
    engine = FileEngine(config, journal, config_path)
    project = {'id': 'p', 'alias': 'P', 'root': str(root), 'mode': 'write', 'allow_tasks': True}
    yield engine, project, root
    journal.db.close()


def file_value(host=AZURE_HOST):
    return {'download_url': f'https://{host}/fixture.zip?sig={TICKET}', 'file_id': FILE_ID}


def import_args(data=b'fixture', host=AZURE_HOST):
    return {'project': 'P', 'idempotency_key': uuid.uuid4().hex,
            'file': {**file_value(host), 'size': len(data)}, 'path': 'assets/fixture.zip',
            'expected_sha256': hashlib.sha256(data).hexdigest()}


def test_verified_native_storage_is_in_default_policy():
    assert incoming.validate_url(file_value()['download_url'], incoming.DEFAULT_HOSTS)[1] == AZURE_HOST
    config = validate_integrations({})
    assert file_source_hosts(config) == incoming.DEFAULT_HOSTS
    assert 'file_hosts' not in config and 'extra_file_hosts' not in config


def test_exact_zip_bytes_are_imported_from_verified_native_storage(workspace):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('图片说明.txt', 'synthetic content')
    data = stream.getvalue()
    engine, project, root = workspace
    result = incoming.import_artifact(engine, project, import_args(data), stream=[data[:20], data[20:]])
    assert (root / result['path']).read_bytes() == data
    assert result['sha256'] == hashlib.sha256(data).hexdigest()
    assert result['created'] and not result['extracted'] and not result['executed']
    assert not (root / 'assets/图片说明.txt').exists()


@pytest.mark.parametrize('hosts', [[], ['files.oaiusercontent.com'], ['only.example.com']])
def test_explicit_restrictive_hosts_are_never_silently_broadened(workspace, hosts):
    engine, project, root = workspace
    engine.config['integrations'] = validate_integrations({'file_hosts': hosts})
    with pytest.raises(DevError) as error:
        incoming.import_artifact(engine, project, import_args(), stream=[b'fixture'])
    assert error.value.code == 'ARTIFACT_SOURCE_DENIED'
    assert not (root / 'assets').exists()


def test_exact_local_extensions_do_not_replace_defaults(workspace):
    engine, project, root = workspace
    config = validate_integrations({'extra_file_hosts': ['UPLOADS.EXAMPLE.COM', 'uploads.example.com.']})
    assert config['extra_file_hosts'] == ['uploads.example.com']
    engine.config['integrations'] = config
    result = incoming.import_artifact(engine, project, import_args(host='uploads.example.com'), stream=[b'fixture'])
    assert (root / result['path']).read_bytes() == b'fixture'
    assert AZURE_HOST in file_source_hosts(config)
    assert 'file_hosts' not in config


def test_explicit_host_names_are_canonicalized():
    config = validate_integrations({'file_hosts': ['FILES.OAIUSERCONTENT.COM.', 'files.oaiusercontent.com']})
    assert config['file_hosts'] == ['files.oaiusercontent.com']


@pytest.mark.parametrize('host', ['*.blob.core.windows.net', 'https://files.oaiusercontent.com',
    'user@files.oaiusercontent.com', 'bad..example.com', '-bad.example.com', 'bad-.example.com',
    'a' * 64 + '.example.com', '127.0.0.1', 'bad_example.com', 'files.oaiusercontent.com:443'])
def test_invalid_local_source_entries_fail_configuration(host):
    with pytest.raises(ValueError):
        validate_integrations({'file_hosts': [host]})


@pytest.mark.parametrize('url', [
    'https://attacker.blob.core.windows.net/file',
    'https://oaisdmntprattacker.blob.core.windows.net/file',
    'https://' + AZURE_HOST + '.evil.example/file',
    'https://files.oaiusercontent.com.evil.example/file',
    'https://@files.oaiusercontent.com/file',
    'https://files.oaiusercontent.com\\@evil.example/file',
    'http://files.oaiusercontent.com/file',
    'https://files.oaiusercontent.com:444/file',
    'https://files.oaiusercontent.com/file#',
    'https://files.oaiusercontent.com/file?ticket=bad\x7f',
    pytest.param('https://files.oaiusercontent.com/' + 'a' * 8192, id='oversized-url'),
    'sandbox:/mnt/data/fixture.zip',
])
def test_untrusted_or_ambiguous_sources_are_denied_before_network(url):
    with pytest.raises(DevError) as error:
        incoming.validate_url(url, incoming.DEFAULT_HOSTS)
    assert error.value.code == 'ARTIFACT_SOURCE_DENIED'
    assert error.value.details['request_sent'] is False
    assert error.value.details['reason'] in {'invalid_url', 'unsupported_scheme', 'host_not_allowed'}


def test_source_denial_explains_host_without_disclosing_ticket(workspace):
    engine, project, root = workspace
    args = import_args(host='unapproved.example.com')
    with pytest.raises(DevError) as error:
        incoming.import_artifact(engine, project, args, stream=[b'fixture'])
    details = error.value.details
    assert details['source_host'] == 'unapproved.example.com'
    assert details['source_scheme'] == 'https'
    assert details['reason'] == 'host_not_allowed'
    assert details['recovery'] == 'review_local_file_sources'
    serialized = json.dumps({'message': error.value.message, **details})
    assert TICKET not in serialized and FILE_ID not in serialized and 'fixture.zip' not in serialized
    assert not (root / 'assets').exists()


def test_native_argument_summary_keeps_only_safe_source_identity():
    summary = safe_summary({'file': file_value()})['file']
    assert summary['source_host'] == AZURE_HOST
    assert summary['source_scheme'] == 'https'
    assert TICKET not in json.dumps(summary) and FILE_ID not in json.dumps(summary)
    assert 'fixture.zip' not in json.dumps(summary)


def test_help_and_generated_calls_use_native_top_level_file():
    help_info = help_result('write', 'import')
    assert help_info['arguments_location'] == 'top-level'
    schema = help_info['inputSchema']
    Draft202012Validator.check_schema(schema)
    assert 'file' in schema['properties'] and 'file' in schema['required']
    assert 'file' not in schema['properties']['options']['properties']
    name, args = public_call('download_artifact', {'project': 'P', 'idempotency_key': 'fixture-import',
                                                 'path': 'asset.zip', 'file': file_value()})
    assert name == 'write' and args['file'] == file_value() and 'file' not in args['options']
    Draft202012Validator(schema).validate(args)


class Response:
    def __init__(self, status=200, body=b'fixture', headers=None):
        self.status = status
        self.body = io.BytesIO(body)
        self.headers = headers or {}

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, size):
        return self.body.read(size)

    def read(self, size):
        return self.body.read(size)


@pytest.fixture
def transport(monkeypatch):
    responses, requests, connections = [], [], []

    class Connection:
        def __init__(self, host, port, **kwargs):
            self.host, self.port = host, port
            self.sock = SimpleNamespace(settimeout=lambda timeout: None)
            self.closed = False
            connections.append(self)

        def request(self, method, path, headers):
            requests.append((self.host, method, path, headers))

        def getresponse(self):
            return responses.pop(0)

        def close(self):
            self.closed = True

    monkeypatch.setattr(incoming, 'PublicTLSConnection', Connection)
    return responses, requests, connections


def test_native_redirect_uses_same_exact_policy_without_credentials(transport):
    responses, requests, connections = transport
    responses.extend([Response(302, headers={'Location': file_value()['download_url']}),
                      Response(headers={'Content-Length': '7'})])
    result = b''.join(incoming.download_chunks(file_value('files.oaiusercontent.com'), incoming.DEFAULT_HOSTS, 20))
    assert result == b'fixture'
    assert [r[0] for r in requests] == ['files.oaiusercontent.com', AZURE_HOST]
    assert all('Authorization' not in r[3] and 'Cookie' not in r[3] for r in requests)
    assert all(c.closed for c in connections)


def test_untrusted_redirect_is_not_requested_and_never_publishes(workspace, transport):
    engine, project, root = workspace
    responses, requests, connections = transport
    responses.append(Response(302, headers={'Location': 'https://attacker.blob.core.windows.net/file?sig=' + TICKET}))
    with pytest.raises(DevError) as error:
        incoming.import_artifact(engine, project, import_args(host='files.oaiusercontent.com'))
    assert error.value.details['source_host'] == 'attacker.blob.core.windows.net'
    assert error.value.details['stage'] == 'redirect_validation'
    assert len(requests) == 1 and all(c.closed for c in connections)
    assert not (root / 'assets/fixture.zip').exists() and not list(root.rglob('*.part'))


@pytest.mark.parametrize('status', [401, 403, 404, 410, 429, 500, 503])
def test_http_failure_keeps_status_and_correct_recovery(workspace, transport, status):
    engine, project, root = workspace
    responses, _, connections = transport
    responses.append(Response(status))
    with pytest.raises(DevError) as error:
        incoming.import_artifact(engine, project, import_args(host='files.oaiusercontent.com'))
    assert error.value.code == 'ARTIFACT_DOWNLOAD_FAILED'
    assert error.value.details['http_status'] == status
    assert error.value.details['source_host'] == 'files.oaiusercontent.com'
    expected = 'refresh_native_file' if status in {401, 403, 404, 410} else 'retry_later'
    assert error.value.details['recovery'] == expected
    assert all(c.closed for c in connections)
    assert not (root / 'assets/fixture.zip').exists() and not list(root.rglob('*.part'))


def test_dns_failure_is_distinguishable_without_returning_raw_error(monkeypatch):
    def resolve(*args, **kwargs):
        raise socket.gaierror('private diagnostic ' + TICKET)
    monkeypatch.setattr(socket, 'getaddrinfo', resolve)
    with pytest.raises(DevError) as error:
        incoming.PublicTLSConnection('files.oaiusercontent.com', 443).connect()
    assert error.value.code == 'ARTIFACT_NETWORK'
    assert error.value.details['reason'] == 'dns_failed'
    assert TICKET not in json.dumps(error.value.details) + error.value.message


@pytest.mark.parametrize('ip', ['127.0.0.1', '10.0.0.1', '169.254.169.254', '224.0.0.1',
    '::1', 'ff0e::1', 'fec0::1', 'feff:ffff:ffff:ffff:ffff:ffff:ffff:ffff'])
def test_private_or_multicast_dns_never_opens_socket(monkeypatch, ip):
    family = socket.AF_INET6 if ':' in ip else socket.AF_INET
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [(family, socket.SOCK_STREAM, 6, '', (ip, 443))])
    monkeypatch.setattr(socket, 'socket', lambda *args, **kwargs: pytest.fail('No private/multicast socket'))
    with pytest.raises(DevError) as error:
        incoming.PublicTLSConnection('files.oaiusercontent.com', 443).connect()
    assert error.value.code == 'ARTIFACT_SOURCE_DENIED'
    assert error.value.details['reason'] == 'non_public_address'


def test_tls_certificate_failure_does_not_become_generic_network_error(monkeypatch):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443))])
    closed = []
    fake_socket = SimpleNamespace(settimeout=lambda value: None, connect=lambda value: None, close=lambda: closed.append(True))
    monkeypatch.setattr(socket, 'socket', lambda *args, **kwargs: fake_socket)
    connection = incoming.PublicTLSConnection('files.oaiusercontent.com', 443)
    def wrap(*args, **kwargs):
        raise ssl.SSLCertVerificationError('private ' + TICKET)
    connection._context = SimpleNamespace(wrap_socket=wrap)
    with pytest.raises(DevError) as error:
        connection.connect()
    assert error.value.code == 'ARTIFACT_NETWORK' and error.value.details['reason'] == 'tls_failed'
    assert closed and TICKET not in error.value.message


def test_short_content_length_stream_never_publishes(workspace, transport):
    engine, project, root = workspace
    responses, _, _ = transport
    responses.append(Response(body=b'fix', headers={'Content-Length': '7'}))
    with pytest.raises(DevError) as error:
        incoming.import_artifact(engine, project, import_args(host='files.oaiusercontent.com'))
    assert error.value.code == 'ARTIFACT_SIZE'
    assert not (root / 'assets/fixture.zip').exists() and not list(root.rglob('*.part'))


def test_host_metadata_does_not_leak_malformed_authority():
    value = {'file_id': FILE_ID, 'download_url': 'https://' + TICKET + '@[malformed]/file?sig=' + TICKET}
    summary = safe_summary(value)
    assert TICKET not in json.dumps(summary) and FILE_ID not in json.dumps(summary)


@pytest.mark.parametrize('raw,expected', [
    (b'HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n', b''),
    (b'HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\nfixture', b'fixture'),
    (b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nfix\r\n4\r\nture\r\n0\r\n\r\n', b'fixture'),
    (b'HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nfixture', b'fixture'),
], ids=['empty','content-length','chunked','connection-close'])
def test_real_http_response_framing_is_preserved(transport, raw, expected):
    import http.client
    response = http.client.HTTPResponse(SimpleNamespace(makefile=lambda *args: io.BytesIO(raw)))
    response.begin()
    transport[0].append(response)
    assert b''.join(incoming.download_chunks(file_value(), incoming.DEFAULT_HOSTS, 20)) == expected
    assert all(connection.closed for connection in transport[2])


def test_slow_stream_checks_deadline_between_raw_reads(workspace, transport, monkeypatch):
    engine, project, root = workspace
    clock = [0.0]
    monkeypatch.setattr(incoming.time, 'monotonic', lambda: clock[0])
    class Slow(Response):
        def read(self, size):
            pytest.fail('Do not hide slow reads inside an unbounded buffered read')
        def read1(self, size):
            clock[0] += 100
            return b'x'
    transport[0].append(Slow())
    args = import_args(host='files.oaiusercontent.com')
    args['file'].pop('size')
    with pytest.raises(DevError) as error:
        incoming.import_artifact(engine, project, args)
    assert error.value.code == 'ARTIFACT_TIMEOUT'
    assert not (root / args['path']).exists() and not list(root.rglob('*.part'))
    assert all(connection.closed for connection in transport[2])


@pytest.mark.parametrize('location', ['\nhttps://files.oaiusercontent.com/valid', '/file\tname', '//evil.example.com/\\file'])
def test_raw_redirect_controls_are_rejected_before_urljoin(transport, location):
    transport[0].append(Response(302, headers={'Location': location}))
    with pytest.raises(DevError) as error:
        list(incoming.download_chunks(file_value(), incoming.DEFAULT_HOSTS, 20))
    assert error.value.code == 'ARTIFACT_SOURCE_DENIED'
    assert len(transport[1]) == 1 and all(connection.closed for connection in transport[2])


@pytest.mark.parametrize('ip', ['127.0.0.1', 'fec0::1'])
def test_mixed_dns_answers_are_all_validated_before_connect(monkeypatch, ip):
    family = socket.AF_INET6 if ':' in ip else socket.AF_INET
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('1.1.1.1', 443)),
        (family, socket.SOCK_STREAM, 6, '', (ip, 443))])
    monkeypatch.setattr(socket, 'socket', lambda *args, **kwargs: pytest.fail('Do not connect even to the public answer'))
    with pytest.raises(DevError) as error:
        incoming.PublicTLSConnection('files.oaiusercontent.com', 443).connect()
    assert error.value.details['reason'] == 'non_public_address'



def test_public_ipv6_is_pinned_without_changing_tls_identity(monkeypatch):
    address = ('2606:4700:4700::1111', 443, 0, 0)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *args, **kwargs: [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, '', address)])
    connections, tls_names = [], []
    sock = SimpleNamespace(settimeout=lambda value: None, connect=connections.append, close=lambda: None)
    monkeypatch.setattr(socket, 'socket', lambda *args, **kwargs: sock)
    connection = incoming.PublicTLSConnection('files.oaiusercontent.com', 443)
    def wrap(raw, *, server_hostname):
        assert raw is sock
        tls_names.append(server_hostname)
        return sock
    connection._context = SimpleNamespace(wrap_socket=wrap)
    connection.connect()
    assert connections == [address] and tls_names == ['files.oaiusercontent.com']
    connection.close()


def test_saved_default_configuration_follows_future_registry(monkeypatch):
    from shared import file_sources
    saved = json.loads(json.dumps(validate_integrations({})))
    assert 'file_hosts' not in saved
    future = (*file_sources.DEFAULT_FILE_HOSTS, 'newly-reviewed.example.com')
    monkeypatch.setattr(file_sources, 'DEFAULT_FILE_HOSTS', future)
    assert file_source_hosts(validate_integrations(saved)) == future
    assert file_source_hosts(validate_integrations({'file_hosts': []})) == ()
    assert file_source_hosts(validate_integrations({'file_hosts': ['files.oaiusercontent.com']})) == ('files.oaiusercontent.com',)
