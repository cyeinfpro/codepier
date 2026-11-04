"""The reload HTTP evidence must preserve request provenance and duplicates."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx

from tests.lifecycle_http import held_legacy_reload


KINDS = ['delegation_policies', 'members', 'overview', 'timeline']
DOCUMENT = b'''<script src="/static/collaboration-room.js"></script>
<script src="/static/collaboration-room-store.js"></script>
<link href="/static/collaboration-room.css">
<script src="/static/app.js"></script>'''


def test_reload_records_only_matching_requests_without_hiding_duplicates():
    background_started, background_release = Event(), Event()

    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parse_qs(parsed.query).get('background'):
                background_started.set()
                assert background_release.wait(10)
            body = DOCUMENT if parsed.path == '/' else b'{}'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def get(url, marker=None):
        headers = {'X-CodePier-Test-Lifecycle': marker} if marker else {}
        return httpx.get(url, headers=headers, timeout=10, trust_env=False)

    with ThreadingHTTPServer(('127.0.0.1', 0), Upstream) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            stack = SimpleNamespace(url=f'http://127.0.0.1:{server.server_port}')
            with held_legacy_reload(stack) as proxy, ThreadPoolExecutor(max_workers=2) as pool:
                try:
                    # Begin this read before arming, but finish it while the
                    # navigation is held: the previous time-only filter counted it.
                    background = pool.submit(get, proxy.url + '/api/collaboration?kind=timeline&background=1')
                    assert background_started.wait(10)
                    proxy.reload_marker = 'synthetic-original-document'
                    proxy.reload_armed.set()
                    navigation = pool.submit(get, proxy.url + '/')
                    assert proxy.reload_arrived.wait(10)
                    background_release.set()
                    assert background.result().status_code == 200
                    for marker in (None, 'synthetic-other-document'):
                        for kind in KINDS:
                            assert get(proxy.url + '/api/collaboration?kind=' + kind, marker).status_code == 200
                    assert proxy.reload_records == [{'event': 'navigation-response-held', 'status': 200}]
                    for kind in KINDS:
                        assert get(proxy.url + '/api/collaboration?kind=' + kind, proxy.reload_marker).status_code == 200
                    reads = lambda: [r['kind'] for r in proxy.reload_records if r['event'] == 'read-response']
                    assert sorted(reads()) == KINDS
                    assert get(proxy.url + '/api/collaboration?kind=timeline', proxy.reload_marker).status_code == 200
                    assert reads().count('timeline') == 2
                    assert sorted(reads()) != KINDS  # Never deduplicate genuine repeated probe requests.
                    proxy.reload_release.set()
                    assert navigation.result().status_code == 200
                finally:
                    background_release.set()
                    proxy.reload_release.set()
        finally:
            server.shutdown()
            worker.join()
