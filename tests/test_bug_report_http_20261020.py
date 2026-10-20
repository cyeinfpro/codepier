"""HTTP security regressions from the 2026-10-20 report; no external service."""
import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from hub.http import BodyLimit, install_http_behaviors


@pytest.fixture
def app():
    value = FastAPI()
    value.add_middleware(BodyLimit)
    install_http_behaviors(value)

    @value.get("/api/ok")
    def ok():
        return {"ok": True}

    @value.get("/api/attachment")
    def attachment():
        return Response(b"private bytes", media_type="application/octet-stream",
                        headers={"Content-Security-Policy": "sandbox; default-src 'none'",
                                 "Cache-Control": "private, no-store",
                                 "Content-Disposition": "attachment"})

    @value.get("/api/failure")
    def failure():
        raise RuntimeError("private-fixture-secret")

    return value


def test_attachment_keeps_stricter_content_security_policy(app):
    with TestClient(app) as client:
        response = client.get("/api/attachment")
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("path,status", [("/api/ok", 200), ("/api/failure", 500)])
def test_https_security_headers_cover_success_and_unhandled_errors(app, path, status):
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as client:
        response = client.get(path)
    assert response.status_code == status
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "script-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["strict-transport-security"] == "max-age=31536000"
    assert response.headers["cache-control"] == "no-store"
    assert "private-fixture-secret" not in response.text


def test_untrusted_forwarded_scheme_cannot_enable_hsts(app):
    with TestClient(app) as client:
        response = client.get("/api/ok", headers={"X-Forwarded-Proto": "https"})
    assert "strict-transport-security" not in response.headers


def test_cross_origin_requests_never_receive_credentialed_cors_permission(app):
    with TestClient(app) as client:
        for method, headers in [
            ("GET", {"Origin": "https://untrusted.example"}),
            ("OPTIONS", {"Origin": "https://untrusted.example",
                         "Access-Control-Request-Method": "POST",
                         "Access-Control-Request-Headers": "X-CSRF-Token"})]:
            response = client.request(method, "/api/ok", headers=headers)
            assert "access-control-allow-origin" not in response.headers
            assert "access-control-allow-credentials" not in response.headers


def test_early_body_limit_response_keeps_security_headers(app):
    with TestClient(app) as client:
        response = client.post("/api/ok", content=b"x",
                               headers={"Content-Length": str(6 * 1024 * 1024 + 1)})
    assert response.status_code == 413
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
