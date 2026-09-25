"""Content identity and cache boundaries exercised through real ASGI responses."""
from pathlib import Path
import hashlib
import json
from urllib.parse import urlencode
import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient
from hub.static_assets import ReleaseAssets
from scripts.build_web_assets import generate
from shared.util import VERSION


@pytest.fixture
def assets(tmp_path):
    raw = b'globalThis.fixture="original";'
    (tmp_path / 'app.js').write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    manifest = {'version': VERSION, 'algorithm': 'sha256', 'assets': {'app.js': {'sha256': digest, 'bytes': len(raw)}}}
    (tmp_path / 'assets-manifest.json').write_text(json.dumps(manifest))
    return tmp_path, raw, digest, manifest


def client_for(path):
    return TestClient(Starlette(routes=[Mount('/static', app=ReleaseAssets(directory=path))]))


def test_only_correct_generated_identity_is_immutable_and_frozen(assets):
    directory, raw, digest, _ = assets
    url = '/static/app.js?' + urlencode({'v': 'codepier-' + VERSION, 'h': digest})
    with client_for(directory) as client:
        first = client.get(url)
        assert first.content == raw and 'immutable' in first.headers['cache-control']
        (directory / 'app.js').write_bytes(b'changed on disk')
        assert client.get(url).content == raw
        legacy = client.get('/static/app.js?v=codepier-' + VERSION)
        assert legacy.content == b'changed on disk' and legacy.headers['cache-control'] == 'no-cache'
        head = client.head(url)
        assert head.status_code == 200 and head.content == b'' and int(head.headers['content-length']) == len(raw)
        cached = client.get(url, headers={'If-None-Match': 'W/' + first.headers['etag']})
        assert cached.status_code == 304 and not cached.content


@pytest.mark.parametrize('query', ['h=bad', 'v=old&h={digest}', 'v=codepier-{version}&h={digest}&h={digest}', 'v=codepier-{version}&v=codepier-{version}&h={digest}', 'v=codepier-{version}&h='])
def test_unrecognized_or_ambiguous_hash_urls_fail_without_cache(assets, query):
    directory, _, digest, _ = assets
    with client_for(directory) as client:
        response = client.get('/static/app.js?' + query.format(digest=digest, version=VERSION))
        assert response.status_code == 404 and response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('problem', ['wrong_version', 'wrong_hash', 'missing', 'shape', 'traversal'])
def test_invalid_release_inventory_fails_before_serving_any_bytes(assets, problem):
    directory, _, _, manifest = assets
    if problem == 'wrong_version': manifest['version'] = '0.0.0'
    elif problem == 'wrong_hash': manifest['assets']['app.js']['sha256'] = '0' * 64
    elif problem == 'missing': (directory / 'app.js').unlink()
    elif problem == 'shape': manifest = []
    elif problem == 'traversal': manifest['assets']['../app.js'] = manifest['assets'].pop('app.js')
    (directory / 'assets-manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ValueError): ReleaseAssets(directory=directory)


def test_broken_manifest_symlink_is_not_a_legacy_install(assets):
    directory, *_ = assets
    path = directory / 'assets-manifest.json'
    path.unlink();path.symlink_to(directory / 'missing-private-file')
    with pytest.raises(ValueError): ReleaseAssets(directory=directory)


def test_generator_uses_literal_version_and_hashes_without_importing_application(tmp_path):
    (tmp_path / 'shared').mkdir();(tmp_path / 'web/core').mkdir(parents=True)
    (tmp_path / 'shared/util.py').write_text('VERSION="9.8.7"\nraise AssertionError("must never import")\n')
    raw = b'window.CP={};'
    (tmp_path / 'web/core/bundle.js').write_bytes(raw)
    (tmp_path / 'web/index.source.html').write_text('<script src="/static/core/bundle.js" defer></script>')
    generated = generate(tmp_path)
    manifest = json.loads(generated['web/assets-manifest.json'])
    assert manifest['version'] == '9.8.7'
    assert manifest['assets']['core/bundle.js'] == {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
    assert b'?v=codepier-9.8.7&amp;h=' in generated['web/index.html']
    assert generated == generate(tmp_path)
