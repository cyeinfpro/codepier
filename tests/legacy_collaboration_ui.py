"""Explicit pre-room UI fixture for the retained wire compatibility window.

Only the disposable browser's HTML entry point is changed. All JavaScript,
styles, HTTP handlers, subscriptions and operation receipts are the real retained
implementation. The production index never enables this entry point, and the
new default room journeys live in test_room_center_browser.py. No assertion or
test is skipped: these older journeys continue to exercise their original wire
semantics while new room data uses the new single write surface.
"""
import re


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
'''


def legacy_document(source):
    """Use actual asset files; do not synthesize application or API behavior."""
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

    page.route(stack.url + '/', index)
