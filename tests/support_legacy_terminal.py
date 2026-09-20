"""Test-only renderer for retained terminal transport/security assertions.
No production asset, route, or CSP policy is changed by this helper.
"""
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def mount_archived_terminal(page, provider='codex'):
    # The project button starts asynchronous chat navigation. Wait for its real
    # mount before replacing it so a pending hashchange cannot reclaim the page.
    page.wait_for_function("() => S.page==='native' && document.querySelector('#chat-compose')")
    origin=page.evaluate('location.origin')
    def serve_asset(body, kind):
        # Playwright may pass (route, request); keep captured assets out of the
        # callback signature so Request cannot replace the response body.
        def respond(route):
            route.fulfill(body=body, content_type=kind)
        return respond

    for name,kind in [('native-cli.css','text/css'),('native-cli.js','application/javascript')]:
        body=(ROOT/'tests/fixtures/legacy-terminal'/name).read_text()
        page.route('**/static/__legacy_terminal_test__/'+name,
                   serve_asset(body, kind))
    # Same-origin external scripts satisfy the existing strict production CSP.
    page.add_style_tag(url=origin+'/static/vendor/xterm-5.5.0/xterm.css')
    page.add_style_tag(url=origin+'/static/__legacy_terminal_test__/native-cli.css')
    for asset in ['vendor/xterm-5.5.0/xterm.js','vendor/addon-fit-0.10.0/addon-fit.js','vendor/addon-search-0.15.0/addon-search.js','__legacy_terminal_test__/native-cli.js']:
        page.add_script_tag(url=origin+'/static/'+asset)
    page.evaluate("provider => {stopEvents();chatDetach();S.renderSeq++;S.page='terminal';document.querySelector('#page').replaceChildren();NativeCLIUI.project=ChatUI.project;NativeCLIUI.provider=provider;return nativePage();}", provider)
