"""Production routes exclude old UI while its exact test snapshot stays runnable.

This proves a static delivery boundary, not retirement of external wire clients.
"""
import hashlib
from html import unescape
import json
from pathlib import Path
import re

from scripts.build_source_bundle import REQUIRED_FILES, include
from shared.util import VERSION
from tests.legacy_collaboration_ui import (
    LEGACY_ASSET_NAMES, LEGACY_DIRECTORY, LEGACY_PREFIX, legacy_asset, legacy_document,
)


ROOT = Path(__file__).resolve().parents[1]
ROOM_ASSETS = ('collaboration-room.js', 'collaboration-room-store.js', 'collaboration-room.css')


def test_legacy_ui_snapshot_keeps_original_bytes_and_public_test_material():
    manifest = json.loads((LEGACY_DIRECTORY / 'manifest.json').read_text())
    assert manifest['source_commit'] == 'a60d43c36486821273343453f5b6f5b7780ac833'
    assert set(manifest['assets']) == set(LEGACY_ASSET_NAMES)
    for name in LEGACY_ASSET_NAMES:
        path = legacy_asset(name)
        raw = path.read_bytes()
        assert manifest['assets'][name] == {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        assert not (ROOT / 'web' / name).exists()
        assert 'web/' + name not in REQUIRED_FILES
        assert include(path.relative_to(ROOT), public=True)
    assert {'web/' + name for name in ROOM_ASSETS} <= REQUIRED_FILES
    historical = legacy_document((ROOT / 'web/index.html').read_text())
    for name in LEGACY_ASSET_NAMES:
        assert LEGACY_PREFIX + name in historical
        assert '/static/' + name not in historical
    assert 'legacy-collaboration-wire-compatibility' in historical


def test_production_http_does_not_expose_legacy_ui_or_test_fixture_routes(stack):
    # Exercise the actual Hub routing and ReleaseAssets mount, not a scan of
    # HTML text or a test-only route serving the removed files back to clients.
    manifest = json.loads((LEGACY_DIRECTORY / 'manifest.json').read_text())
    for name in LEGACY_ASSET_NAMES:
        for path in (
            '/static/' + name,
            '/static/' + name + '?v=codepier-' + VERSION + '&h=' + manifest['assets'][name]['sha256'],
            LEGACY_PREFIX + name,
            '/static/../tests/fixtures/legacy_collaboration_ui/' + name,
        ):
            response = stack.client.get(path)
            assert response.status_code == 404, (path, response.status_code)
    index = stack.client.get('/')
    assert index.status_code == 200
    assert LEGACY_PREFIX not in index.text
    for name in ROOM_ASSETS:
        links = re.findall(r'(?:src|href)="(/static/' + re.escape(name) + r'\?[^" ]+)"', index.text)
        assert len(links) == 1
        response = stack.client.get(unescape(links[0]))
        assert response.status_code == 200
        assert response.content == (ROOT / 'web' / name).read_bytes()
        assert 'immutable' in response.headers['cache-control']
