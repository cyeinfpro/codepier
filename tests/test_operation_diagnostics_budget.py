"""Real local HTTP trickle and byte budget coverage for diagnostic reads."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import time
import socket

import httpx
import pytest

from tests.operation_diagnostics import bounded_json


class Chunks(httpx.SyncByteStream):
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        yield from self.chunks

    def close(self):
        self.closed = True


def test_json_and_byte_budget_close_original_stream():
    small = Chunks([b'{"ok":', b"true}"])
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=small))
    ) as client:
        assert bounded_json(client, "GET", "http://fixture/") == {"ok": True}
    assert small.closed
    large = Chunks([b"x" * 65537])
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=large))
    ) as client:
        with pytest.raises(ValueError, match="byte budget"):
            bounded_json(client, "GET", "http://fixture/")
    assert large.closed


def test_elapsed_budget_stops_real_continuous_small_response():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "10000")
            self.end_headers()
            try:
                for _ in range(100):
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(trust_env=False) as client:
            start = time.monotonic()
            with pytest.raises((TimeoutError, httpx.ReadTimeout)):
                bounded_json(
                    client, "GET", f"http://127.0.0.1:{server.server_port}/", total_timeout=0.5
                )
            assert time.monotonic() - start < 1.5
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_trickle_transport_hits_total_deadline_before_byte_limit():
    def trickle():
        for _ in range(100):
            time.sleep(0.02)
            yield b" "

    stream = Chunks(trickle())
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=stream))
    ) as client:
        with pytest.raises(TimeoutError, match="elapsed-time budget"):
            bounded_json(client, "GET", "http://fixture/", total_timeout=0.1)
    assert stream.closed


def test_nonpositive_budget_has_no_request():
    calls = []
    with httpx.Client(transport=httpx.MockTransport(lambda r: calls.append(r))) as client:
        with pytest.raises(ValueError, match="positive"):
            bounded_json(client, "GET", "http://fixture/", total_timeout=0)
    assert calls == []
