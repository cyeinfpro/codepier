"""Run the exact Windows bootstrap downloader against loopback fault fixtures."""
from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import ssl
import threading
import time
from types import SimpleNamespace
import urllib.error

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = b"synthetic-agent-package-" * 8000
SHA = hashlib.sha256(PAYLOAD).hexdigest()


@pytest.fixture
def downloader():
    text = (ROOT / "deploy/install-from-hub.ps1").read_text()
    code = text.split("$CodePierDownload = @'\n", 1)[1].split("\n'@", 1)[0]
    namespace = {"__name__": "bootstrap_download"}
    exec(compile(code, "bootstrap-download", "exec"), namespace)
    delays = []
    namespace["time"] = SimpleNamespace(monotonic=time.monotonic, sleep=delays.append)
    return SimpleNamespace(download=namespace["download"], namespace=namespace, delays=delays, code=code)


@pytest.fixture
def server():
    state = SimpleNamespace(modes=[], requests=[])

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            index = len(state.requests)
            state.requests.append({"path": self.path, "headers": dict(self.headers)})
            mode = state.modes[min(index, len(state.modes) - 1)] if state.modes else "ok"
            if mode == "slow":
                time.sleep(0.15)
            status = int(mode) if mode.isdigit() else (302 if mode == "redirect" else 200)
            self.send_response(status)
            if mode == "redirect":
                self.send_header("Location", "/must-not-follow")
            etag = "0" * 64 if mode == "etag" else SHA
            self.send_header("ETag", '"' + etag + '"')
            body = b"<html>gateway error</html>" if mode == "html" else PAYLOAD
            self.send_header("Content-Length", str(8388609 if mode == "oversize" else len(body)))
            self.end_headers()
            if mode == "truncate":
                body = body[:70000]
            if mode == "corrupt":
                body = b"x" + body[1:]
            try:
                if mode == "slow-body":
                    self.wfile.write(body[:1000])
                    self.wfile.flush()
                    time.sleep(0.15)
                    self.wfile.write(body[1000:])
                elif mode != "oversize":
                    self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{httpd.server_port}"
    try:
        yield state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("first", ["truncate", "503", "429", "slow", "slow-body"])
def test_transient_download_restarts_without_appending(downloader, server, tmp_path, first, capsys):
    server.modes = [first, "ok"]
    output = tmp_path / "Agent package [test].zip"
    downloader.download(server.url, SHA, output, timeout=0.05 if first.startswith("slow") else 2)
    assert output.read_bytes() == PAYLOAD
    assert not output.with_name(output.name + ".part").exists()
    assert len(server.requests) == 2 and downloader.delays == [1]
    assert {r["path"] for r in server.requests} == {"/agent/agent.zip?sha256=" + SHA}
    assert all("Range" not in r["headers"] and "Authorization" not in r["headers"] for r in server.requests)
    log = capsys.readouterr().out
    assert "attempt 1/4" in log and "attempt 2/4" in log
    assert "retrying from the beginning" in log and "SHA-256 matched" in log


@pytest.mark.parametrize("mode,message", [
    ("409", "HTTP 409"), ("401", "HTTP 401"), ("403", "HTTP 403"), ("redirect", "HTTP 302"),
    ("etag", "identity changed"), ("oversize", "response size"),
    ("html", "checksum mismatch"), ("corrupt", "checksum mismatch"),
])
def test_untrusted_or_permanent_failure_never_retries_or_publishes(downloader, server, tmp_path, mode, message):
    server.modes = [mode, "ok"]
    output = tmp_path / "agent.zip"
    with pytest.raises(ValueError, match=message):
        downloader.download(server.url, SHA, output, timeout=2)
    assert len(server.requests) == 1 and downloader.delays == []
    assert not output.exists() and not output.with_name("agent.zip.part").exists()


def test_retries_are_bounded_and_partial_is_removed(downloader, server, tmp_path):
    server.modes = ["truncate"]
    output = tmp_path / "agent.zip"
    with pytest.raises(ValueError, match="after 4 attempts"):
        downloader.download(server.url, SHA, output, timeout=2)
    assert len(server.requests) == 4 and downloader.delays == [1, 2, 4]
    assert not output.exists() and not output.with_name("agent.zip.part").exists()


@pytest.mark.parametrize("name", ["agent.zip", "agent.zip.part"])
def test_existing_bytes_are_never_reused_or_overwritten(downloader, server, tmp_path, name):
    output = tmp_path / "agent.zip"
    existing = tmp_path / name
    existing.write_bytes(b"preserve")
    with pytest.raises(ValueError, match="already exists"):
        downloader.download(server.url, SHA, output)
    assert existing.read_bytes() == b"preserve" and not server.requests


def test_tls_verification_failure_is_not_retried(downloader, monkeypatch, tmp_path):
    class Opener:
        def open(self, *args, **kwargs):
            raise urllib.error.URLError(ssl.SSLCertVerificationError("fixture"))
    monkeypatch.setattr(downloader.namespace["urllib"].request, "build_opener", lambda *_: Opener())
    with pytest.raises(ValueError, match="certificate verification"):
        downloader.download("https://panel.invalid", SHA, tmp_path / "agent.zip")
    assert downloader.delays == []


def test_bootstrap_writes_payload_before_call_for_powershell_51_quoting():
    text = (ROOT / "deploy/install-from-hub.ps1").read_text()
    assert "Set-Content -LiteralPath $DownloadHelper -Value $CodePierDownload -Encoding UTF8" in text
    assert "& $CodePierPython $DownloadHelper $Hub $Sha256 $Archive $DownloadTimeoutSec $DownloadAttempts" in text
    assert text.index("& $CodePierPython $DownloadHelper") < text.index("$env:CODEPIER_INSTALL_TOKEN = $Token")
    assert "[ValidateRange(1,5)][int]$DownloadAttempts = 4" in text
