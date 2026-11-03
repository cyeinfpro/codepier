"""Explicit pre-room UI fixture for the retained wire compatibility window.

Only the disposable browser's HTML entry point and test-only asset requests are
intercepted. JavaScript/styles are byte-identical historical fixtures; the HTTP
handlers, subscriptions and operation receipts remain the real retained wire.
Neither the production index nor the static router exposes the old UI. No test
assertion is skipped: historical journeys keep exercising their original wire
semantics while the default room journeys cover the new single write surface.
"""
from pathlib import Path
import re
from urllib.parse import urlsplit


LEGACY_DIRECTORY = Path(__file__).resolve().parent / 'fixtures/legacy_collaboration_ui'
LEGACY_PREFIX = '/__tests__/legacy-collaboration-ui/'
LEGACY_ASSET_NAMES = (
    'collaboration.css', 'collaboration-relay.css', 'collaboration-workspace.css',
    'collaboration-runtime.js', 'collaboration-goals.js', 'collaboration-delegation.js',
    'collaboration-channels.css', 'collaboration-channels.js', 'collaboration-dots.js',
    'collaboration-relay.js', 'collaboration-workspace.js', 'collaboration.js',
)


def legacy_asset(name):
    """Resolve only the twelve pinned, test-only historical resources."""
    if name not in LEGACY_ASSET_NAMES:
        raise ValueError('Not a legacy collaboration fixture asset: ' + str(name))
    return LEGACY_DIRECTORY / name


LEGACY_ASSETS = '''
<link rel="stylesheet" href="/static/collaboration.css">
<link rel="stylesheet" href="/static/collaboration-relay.css">
<link rel="stylesheet" href="/static/collaboration-workspace.css">
<script src="/static/collaboration-runtime.js" defer></script>
<script src="/static/collaboration-goals.js" defer></script>
<script src="/static/collaboration-delegation.js" defer></script>
<link rel="stylesheet" href="/static/collaboration-channels.css">
<script src="/static/collaboration-channels.js" defer></script>
<script src="/static/collaboration-dots.js" defer></script>
<script src="/static/collaboration-relay.js" defer></script>
<script src="/static/collaboration-workspace.js" defer></script>
<script src="/static/collaboration.js" defer></script>
<meta name="codepier-test-surface" content="legacy-collaboration-wire-compatibility">
'''.replace('/static/', LEGACY_PREFIX)


def legacy_document(source):
    """Use the unmodified old bytes; do not synthesize app or API behavior."""
    source, scripts = re.subn(
        r'<script\b[^>]*src="/static/collaboration-room(?:-store)?\.js(?:\?[^" ]*)?"[^>]*>\s*</script>',
        '', source,
    )
    source, styles = re.subn(
        r'<link\b[^>]*href="/static/collaboration-room\.css(?:\?[^" ]*)?"[^>]*>',
        '', source,
    )
    assert scripts == 2 and styles == 1, 'Expected the default room entry assets'
    marker = '<script src="/static/app.js'
    assert marker in source, 'Expected the real app entry point'
    return source.replace(marker, LEGACY_ASSETS + marker, 1)


def use_legacy_collaboration_ui(page, stack):
    """Install before navigation; scoped to this test page and fixture Hub."""
    if getattr(page, '_codepier_legacy_collaboration_fixture', False):
        return
    page._codepier_legacy_collaboration_fixture = True

    def index(route):
        response = route.fetch()
        assert response.status == 200
        route.fulfill(response=response, body=legacy_document(response.text()))

    def fixture_asset(route):
        name = urlsplit(route.request.url).path.removeprefix(LEGACY_PREFIX)
        resource = legacy_asset(name)
        route.fulfill(status=200, body=resource.read_bytes(),
            content_type='text/css' if resource.suffix == '.css' else 'text/javascript',
            headers={'Cache-Control': 'no-store', 'X-CodePier-Test-Surface': 'legacy-ui-fixture'})

    page.route(stack.url + LEGACY_PREFIX + '*', fixture_asset)
    page.route(stack.url + '/', index)
