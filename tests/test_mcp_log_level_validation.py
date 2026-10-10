"""Malformed JSON metadata must produce INVALID_PARAMS, never a TypeError/500."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from hub.mcp import make_router
from shared.mcp_protocol import MODERN, PREFIX, ProtocolError, validate_modern
from tests.test_audit_runtime import runtime  # noqa: F401


INVALID_LEVELS = [[], {}, None, True, False, 0, 1.5, "", "verbose", ["info"], {"value": "info"}]
VALID_LEVELS = ["debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"]


def request(level, *, include=True):
    meta = {PREFIX + "protocolVersion": MODERN, PREFIX + "clientCapabilities": {}}
    if include:
        meta[PREFIX + "logLevel"] = level
    body = {"jsonrpc": "2.0", "id": "log-level-check", "method": "server/discover", "params": {"_meta": meta}}
    headers = {"mcp-protocol-version": MODERN, "mcp-method": body["method"]}
    return body, headers


@pytest.mark.parametrize("level", INVALID_LEVELS)
def test_invalid_log_levels_raise_protocol_error(level):
    body, headers = request(level)
    with pytest.raises(ProtocolError) as caught:
        validate_modern(body, headers)
    assert caught.value.code == -32602
    assert caught.value.message == "Invalid logLevel"


@pytest.mark.parametrize("level", VALID_LEVELS)
def test_standard_log_levels_are_accepted(level):
    body, headers = request(level)
    assert validate_modern(body, headers)[PREFIX + "logLevel"] == level


def test_absent_optional_log_level_remains_accepted():
    body, headers = request(None, include=False)
    assert PREFIX + "logLevel" not in validate_modern(body, headers)


@pytest.mark.asyncio
@pytest.mark.parametrize("level", INVALID_LEVELS)
async def test_http_route_returns_invalid_params_for_malformed_log_level(runtime, level):
    state, principal, _ = runtime
    app = FastAPI()
    # Validation rejects before either a gateway or a business handler is used.
    router_state = SimpleNamespace(store=state.store, gateway=None)
    app.include_router(make_router(SimpleNamespace(bearer=lambda _: principal), router_state, lambda: "http://test"))
    body, headers = request(level)
    headers["accept"] = "application/json, text/event-stream"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/mcp", json=body, headers=headers)
    assert response.status_code == 400
    assert response.json() == {
        "jsonrpc": "2.0",
        "id": "log-level-check",
        "error": {"code": -32602, "message": "Invalid logLevel"},
    }
