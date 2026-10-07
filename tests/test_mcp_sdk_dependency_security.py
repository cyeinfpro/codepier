"""Keep the reviewed MCP client advisory out of the Apps dependency lock.

This verifies distribution inputs, not an OAuth exploit or a real host login.
GHSA-6qxp-vccf-f47h identifies client >=2.0.0,<2.2.0 as affected.
"""
import base64
import json
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]


def test_locked_mcp_client_avoids_reviewed_oauth_advisory():
    lock = json.loads((ROOT / 'web/mcp-apps/package-lock.json').read_text())
    packages = lock['packages']
    client = packages['node_modules/@modelcontextprotocol/client']
    core = packages['node_modules/@modelcontextprotocol/core']
    version = tuple(int(part) for part in client['version'].split('.'))
    assert version >= (2, 2, 0), 'GHSA-6qxp-vccf-f47h requires a patched MCP OAuth client'
    assert client['dependencies']['@modelcontextprotocol/core'] == core['version']
    assert tuple(int(part) for part in core['version'].split('.')) >= (2, 2, 0)
    for name, package in [('client', client), ('core', core)]:
        location = urlsplit(package['resolved'])
        assert location.scheme == 'https' and location.netloc == 'registry.npmjs.org'
        assert location.path == f'/@modelcontextprotocol/{name}/-/{name}-{package["version"]}.tgz'
        algorithm, value = package['integrity'].split('-', 1)
        assert algorithm == 'sha512' and len(base64.b64decode(value, validate=True)) == 64


def test_apps_peer_ranges_and_root_lock_remain_consistent():
    manifest = json.loads((ROOT / 'web/mcp-apps/package.json').read_text())
    lock = json.loads((ROOT / 'web/mcp-apps/package-lock.json').read_text())
    for group in ('dependencies', 'devDependencies'):
        assert lock['packages'][''][group] == manifest[group]
    # Existing Apps SDK peers permit this minor security update. It does not
    # introduce an OAuth credential provider or change the app's direct imports.
    peers = lock['packages']['node_modules/@modelcontextprotocol/ext-apps']['peerDependencies']
    for name in ('client', 'core'):
        selected = lock['packages']['node_modules/@modelcontextprotocol/' + name]['version']
        requirement = peers['@modelcontextprotocol/' + name]
        assert requirement.startswith('^')
        lower = tuple(int(part) for part in requirement[1:].split('.'))
        actual = tuple(int(part) for part in selected.split('.'))
        assert actual[0] == lower[0] and actual >= lower
