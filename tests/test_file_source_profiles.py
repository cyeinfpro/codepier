"""Opt-in OpenAI content-host profile; all transfers are synthetic."""
from __future__ import annotations

import hashlib
import io
import json
import socket

import pytest

from agent import incoming_artifacts as incoming
from agent.filesystem import FileEngine
from agent.integration_config import validate_integrations
from agent.journal import Journal
from shared.file_sources import (
    DEFAULT_FILE_HOSTS, file_source_hosts, file_source_policy,
    normalize_file_source_providers, source_host_allowed,
)
from shared.util import DevError

PROVIDERS = ['openai_sediment']
JAPAN = 'sdmntprjapaneast.oaiusercontent.com'
KOREA = 'sdmntprkoreacentral.oaiusercontent.com'


def test_profile_is_never_enabled_implicitly_or_saved_as_default():
    config = validate_integrations({})
    assert 'file_source_providers' not in config
    assert file_source_policy(config)['file_source_providers'] == []
    assert not source_host_allowed(JAPAN, DEFAULT_FILE_HOSTS)
    assert not source_host_allowed(KOREA, file_source_hosts(config))


@pytest.mark.parametrize('host', [
    JAPAN, KOREA, 'sdmntprsoutheastus3.oaiusercontent.com',
    'SDMNTPRJAPANEAST.OAIUSERCONTENT.COM.',
    'sdmntpr' + 'a' * 56 + '.oaiusercontent.com',
])
def test_enabled_profile_matches_only_canonical_single_label(host):
    result = incoming.validate_url('https://' + host + '/object?sig=synthetic',
                                   [], providers=PROVIDERS)
    assert result[1] == host.lower().removesuffix('.')


@pytest.mark.parametrize('url', [
    'https://oaiusercontent.com/file',
    'https://files.oaiusercontent.com/file',
    'https://sdmntpr.oaiusercontent.com/file',
    'https://sdmntpr-japaneast.oaiusercontent.com/file',
    'https://sdmntprjapaneast-.oaiusercontent.com/file',
    'https://sdmntprjapaneast.foo.oaiusercontent.com/file',
    'https://foo.sdmntprjapaneast.oaiusercontent.com/file',
    'https://sdmntprjapaneast.oaiusercontent.com.evil.example/file',
    'https://sdmntprjapaneast.evil.example/file',
    'https://oaisdmntprjapaneast.blob.core.windows.net/file',
    'https://sdmntprjapaneast.blob.core.windows.net/file',
    'https://sdmntprjapaneast.oaiusercontent.com.s3.amazonaws.com/file',
    'https://sdmntpr' + 'a' * 57 + '.oaiusercontent.com/file',
    'https://sdmntpr日本.oaiusercontent.com/file',
    'https://sdmntprxn--jp.oaiusercontent.com/file',
    'https://sdmntprjapaneast%2eoaiusercontent.com/file',
    'https://sdmntprjapaneast%252eoaiusercontent.com/file',
    'https://sdmntprjapaneast.oaiusercontent.com../file',
    'https://sdmntprjapaneast.oaiusercontent.com:444/file',
    'https://user@sdmntprjapaneast.oaiusercontent.com/file',
    'https://@sdmntprjapaneast.oaiusercontent.com/file',
    'https://sdmntprjapaneast.oaiusercontent.com\\@evil.example/file',
    'https://sdmntprjapaneast.oaiusercontent.com/file#',
    'https://sdmntprjapaneast.oaiusercontent.com/file\n',
    'http://sdmntprjapaneast.oaiusercontent.com/file',
    'https://127.0.0.1/file',
    'https://[::1]/file',
])
def test_profile_rejects_confusables_and_other_namespaces_before_network(url):
    with pytest.raises(DevError) as error:
        incoming.validate_url(url, [], providers=PROVIDERS)
    assert error.value.code == 'ARTIFACT_SOURCE_DENIED'
    assert error.value.details['request_sent'] is False


@pytest.mark.parametrize('providers', [
    'openai_sediment', ['*'], ['openai'], ['OPENAI_SEDIMENT'],
    ['*.oaiusercontent.com'], ['openai_sediment', 'unknown'], [True], [None], {},
])
def test_unknown_profiles_fail_closed(providers):
    with pytest.raises(ValueError):
        validate_integrations({'file_source_providers': providers})


def test_explicit_profiles_are_additive_and_roundtrip_without_mutating_exact_hosts():
    before = {'file_hosts': [], 'extra_file_hosts': ['owned.example.com'],
              'file_source_providers': ['openai_sediment', 'openai_sediment']}
    config = validate_integrations(before)
    assert before['file_hosts'] == []
    assert config['file_source_providers'] == PROVIDERS
    assert file_source_hosts(config) == ('owned.example.com',)
    assert not source_host_allowed('files.oaiusercontent.com', file_source_hosts(config), PROVIDERS)
    assert source_host_allowed(JAPAN, file_source_hosts(config), PROVIDERS)
    assert source_host_allowed('owned.example.com', file_source_hosts(config), PROVIDERS)
    assert normalize_file_source_providers(json.loads(json.dumps(PROVIDERS))) == PROVIDERS
    assert not source_host_allowed(JAPAN, file_source_hosts(config), [])


class Response(io.BytesIO):
    def __init__(self, status=200, body=b'fixture', **headers):
        super().__init__(body)
        self.status = status
        self.headers = headers

    def getheader(self, key, default=None):
        return self.headers.get(key, default)


@pytest.fixture
def transport(monkeypatch):
    responses, connected = [], []

    class Connection:
        def __init__(self, host, *args, **kwargs):
            self.host = host
            self.sock = None
            connected.append(host)

        def request(self, *args, **kwargs):
            assert kwargs['headers']['Accept-Encoding'] == 'identity'

        def getresponse(self):
            return responses.pop(0)

        def close(self):
            pass

    monkeypatch.setattr(incoming, 'PublicTLSConnection', Connection)
    return responses, connected


def test_regions_can_redirect_within_the_explicit_profile(transport):
    responses, connected = transport
    responses.extend([Response(302, Location='https://' + KOREA + '/next'), Response()])
    result = b''.join(incoming.download_chunks(
        {'download_url': 'https://' + JAPAN + '/file'}, [], 100, providers=PROVIDERS))
    assert result == b'fixture'
    assert connected == [JAPAN, KOREA]


def test_outside_redirect_is_never_requested(transport):
    responses, connected = transport
    responses.append(Response(302, Location='https://attacker.blob.core.windows.net/file'))
    with pytest.raises(DevError) as error:
        list(incoming.download_chunks(
            {'download_url': 'https://' + JAPAN + '/file'}, [], 100, providers=PROVIDERS))
    assert error.value.details['stage'] == 'redirect_validation'
    assert error.value.details['request_sent'] is False
    assert connected == [JAPAN]


@pytest.mark.parametrize('addresses', [
    ['127.0.0.1'], ['10.0.0.1'], ['169.254.169.254'],
    ['1.1.1.1', '10.0.0.1'], ['224.0.0.1'],
])
def test_profile_still_rejects_all_nonpublic_dns_answers(monkeypatch, addresses):
    monkeypatch.setattr(socket, 'getaddrinfo', lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in addresses])
    monkeypatch.setattr(socket, 'socket', lambda *a, **kw: pytest.fail('No socket on unsafe DNS'))
    with pytest.raises(DevError) as error:
        incoming.PublicTLSConnection(JAPAN, 443).connect()
    assert error.value.details['reason'] == 'non_public_address'


@pytest.mark.parametrize('data,name', [(b'', '空文件.bin'), (b'\x00\xff\x80fixture', '普通文件.weird')])
def test_import_checks_full_bytes_without_type_restrictions(tmp_path, data, name):
    root = tmp_path / 'project'
    root.mkdir()
    journal = Journal(tmp_path / 'state')
    config = {'allowed_roots': [{'path': str(root), 'writable': True}],
              'integrations': validate_integrations({'file_source_providers': PROVIDERS})}
    engine = FileEngine(config, journal, tmp_path / 'config.json')
    project = {'root': str(root), 'mode': 'write'}
    args = {'path': name, 'file': {'download_url': 'https://' + KOREA + '/opaque',
            'file_id': 'synthetic', 'size': len(data)},
            'expected_sha256': hashlib.sha256(data).hexdigest()}
    try:
        result = incoming.import_artifact(engine, project, args, stream=[data])
        assert (root / name).read_bytes() == data
        assert result['bytes'] == len(data) and result['sha256'] == args['expected_sha256']
        assert not result['executed'] and not result['extracted'] and not result['overwritten']
        assert (root / name).stat().st_mode & 0o111 == 0
    finally:
        journal.db.close()
