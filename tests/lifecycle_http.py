"""Real loopback HTTP transport for a held legacy-document reload response."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread
from urllib.parse import parse_qs, urlsplit

import httpx

from tests.legacy_collaboration_ui import LEGACY_PREFIX, legacy_asset, legacy_document


@contextmanager
def held_legacy_reload(stack):
    """Forward unchanged Hub APIs; gate only the next actual document response."""
    armed, arrived, release, stopped = Event(), Event(), Event(), Event()
    records, clients = [], set()
    clients_lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            self.forward()

        def do_POST(self):
            self.forward()

        def send_headers(self, status, headers, length=None):
            self.send_response(status)
            for key, value in headers:
                if key.lower() not in {'content-length', 'transfer-encoding', 'connection', 'content-encoding'}:
                    self.send_header(key, value)
            if length is not None:
                self.send_header('Content-Length', str(length))
            self.end_headers()

        def forward(self):
            try:
                self.forward_response()
            except (BrokenPipeError, ConnectionResetError):
                # A navigating/closed browser can stop consuming its old stream.
                pass
            except httpx.HTTPError:
                if not stopped.is_set():
                    raise

        def forward_response(self):
            parsed = urlsplit(self.path)
            if parsed.path.startswith(LEGACY_PREFIX):
                resource = legacy_asset(parsed.path.removeprefix(LEGACY_PREFIX))
                body = resource.read_bytes()
                status = 200
                headers = [('content-type', 'text/css' if resource.suffix == '.css' else 'text/javascript')]
            else:
                # Keep Host, Origin, cookies and CSRF: the real Hub still checks
                # same-origin/auth. Never retain or publish these headers.
                headers = dict(self.headers)
                headers['accept-encoding'] = 'identity'
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                with httpx.Client(trust_env=False, timeout=35) as client:
                    with clients_lock:
                        if stopped.is_set():
                            return
                        clients.add(client)
                    try:
                        with client.stream(self.command, stack.url + self.path,
                                           headers=headers, content=body) as response:
                            status, headers = response.status_code, response.headers.multi_items()
                            if response.headers.get('content-type', '').startswith('text/event-stream'):
                                self.send_headers(status, headers)
                                for chunk in response.iter_bytes():
                                    if stopped.is_set():
                                        break
                                    self.wfile.write(chunk)
                                    self.wfile.flush()
                                return
                            body = response.read()
                            if parsed.path == '/' and status == 200:
                                body = legacy_document(response.text).encode()
                    finally:
                        with clients_lock:
                            clients.discard(client)
            if parsed.path == '/' and armed.is_set():
                armed.clear()
                records.append({'event': 'navigation-response-held', 'status': status})
                arrived.set()
                if not release.wait(30):
                    self.send_error(504, 'Test did not release the navigation response')
                    return
            # Completion during the gate does not imply this read started in
            # the armed probe: older/background reads must still pass through.
            marker = self.headers.get('X-CodePier-Test-Lifecycle')
            if (parsed.path == '/api/collaboration' and marker and marker == proxy.reload_marker
                    and arrived.is_set() and not release.is_set()):
                records.append({'event': 'read-response', 'kind': parse_qs(parsed.query).get('kind', [''])[0],
                                'status': status})
            self.send_headers(status, headers, len(body))
            self.wfile.write(body)

    class Proxy:
        native_legacy_document = True

        def __getattr__(self, name):
            return getattr(stack, name)

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        proxy = Proxy()
        proxy.url = f'http://127.0.0.1:{server.server_port}'
        proxy.reload_armed, proxy.reload_arrived, proxy.reload_release = armed, arrived, release
        proxy.reload_records = records
        proxy.reload_marker = None
        try:
            yield proxy
        finally:
            release.set()
            with clients_lock:
                stopped.set()
                pending = list(clients)
            for client in pending:
                client.close()
            server.shutdown()
            worker.join()
