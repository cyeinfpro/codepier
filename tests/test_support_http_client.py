"""The shared fixture's control client has auth state, but no idle TCP pool."""
import itertools
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from types import SimpleNamespace

import httpx
import pytest

from tests import support


@pytest.fixture
def http_server():
    requests = Queue()
    connection_ids = itertools.count(1)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def setup(self):
            super().setup()
            # setup runs once per accepted TCP connection, not once per request.
            self.connection_id = next(connection_ids)

        def log_message(self, *args):
            pass

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            requests.put({
                'connection': self.connection_id,
                'method': self.command,
                'path': self.path,
                'cookie': self.headers.get('Cookie'),
                'csrf': self.headers.get('X-RD-CSRF'),
                'body': body,
            })
            if self.path == '/fail-after-write':
                # The mutation was received, but no response headers reach the
                # client. Retrying here would duplicate an already accepted write.
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            payload = json.dumps({'csrf': 'fixture-csrf', 'connection': self.connection_id}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Connection', 'keep-alive')
            if self.path == '/api/login':
                self.send_header('Set-Cookie', 'session=fixture-session; Path=/; HttpOnly')
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()

        do_GET = handle_request
        do_POST = handle_request

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(url=f'http://127.0.0.1:{server.server_port}', requests=requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_loopback_server_really_allows_http11_connection_reuse(http_server):
    # A normal pooled client must reuse this server's connection, so the fixture
    # regression cannot pass merely because the server closes every response.
    with httpx.Client(base_url=http_server.url, trust_env=False) as client:
        first = client.get('/first')
        second = client.get('/second')
        assert first.http_version == second.http_version == 'HTTP/1.1'
        assert first.json()['connection'] == second.json()['connection']


def test_fixture_client_uses_fresh_connections_and_preserves_login(http_server, monkeypatch):
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'):
        monkeypatch.setenv(name, 'http://127.0.0.1:1')
    monkeypatch.setenv('NO_PROXY', '')
    with support.fixture_http_client(http_server.url) as client:
        assert client.timeout == httpx.Timeout(35)
        assert client.trust_env is False
        # Exercise Stack's real login path without starting a Hub or Agent.
        stack = support.Stack.__new__(support.Stack)
        stack.client = client
        stack.password = 'fixture-password'
        stack.login()
        first = client.get('/first')
        second = client.post('/second', json={'value': 'accepted'})
        assert first.status_code == second.status_code == 200
        assert first.http_version == second.http_version == 'HTTP/1.1'
        assert client.cookies.get('session') == 'fixture-session'
        assert client.headers['X-RD-CSRF'] == 'fixture-csrf'

        requests = [http_server.requests.get_nowait() for _ in range(3)]
        assert len({request['connection'] for request in requests}) == 3
        assert [(request['method'], request['path']) for request in requests] == [
            ('POST', '/api/login'), ('GET', '/first'), ('POST', '/second')]
        for request in requests[1:]:
            assert request['cookie'] == 'session=fixture-session'
            assert request['csrf'] == 'fixture-csrf'
        assert json.loads(requests[-1]['body']) == {'value': 'accepted'}
        assert http_server.requests.empty()
    assert client.is_closed


def test_fixture_client_surfaces_failed_mutation_without_replay(http_server):
    with support.fixture_http_client(http_server.url) as client:
        with pytest.raises(httpx.TransportError):
            client.post('/fail-after-write', json={'value': 'write-once'})
        # A later independent request works, but the failed mutation is not replayed.
        assert client.get('/after-failure').status_code == 200
        mutation = http_server.requests.get_nowait()
        later = http_server.requests.get_nowait()
        assert (mutation['method'], mutation['path']) == ('POST', '/fail-after-write')
        assert json.loads(mutation['body']) == {'value': 'write-once'}
        assert (later['method'], later['path']) == ('GET', '/after-failure')
        assert mutation['connection'] != later['connection']
        assert http_server.requests.empty()
