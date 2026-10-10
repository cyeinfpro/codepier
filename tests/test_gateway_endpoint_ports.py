"""Endpoint ports must be validated before DNS resolution or transport setup."""
import pytest

from hub.gateway.network import endpoint, pin
from shared.util import DevError


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "networks", "allow_http"), [
    ("https://mcp.example:0/mcp", [], False),
    ("https://[2001:4860:4860::8888]:0/mcp", [], False),
    ("http://127.0.0.1:0/mcp", ["127.0.0.1/32"], True),
    ("http://[::1]:0/mcp", ["::1/128"], True),
])
async def test_zero_port_is_rejected_before_dns(url, networks, allow_http):
    lookups = []

    async def resolve(host, port):
        lookups.append((host, port))
        return ["93.184.216.34"]

    with pytest.raises(DevError) as error:
        endpoint(url, networks, allow_http)
    assert error.value.code == "GATEWAY_ENDPOINT_INVALID"
    with pytest.raises(DevError) as error:
        await pin(url, networks, allow_http, resolver=resolve)
    assert error.value.code == "GATEWAY_ENDPOINT_INVALID"
    assert lookups == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("url", "networks", "allow_http", "port", "address"), [
    ("https://mcp.example/mcp", [], False, 443, "93.184.216.34"),
    ("https://mcp.example:8443/mcp", [], False, 8443, "93.184.216.34"),
    ("https://mcp.example:65535/mcp", [], False, 65535, "93.184.216.34"),
    ("http://localhost/mcp", ["127.0.0.1/32"], True, 80, "127.0.0.1"),
    ("http://localhost:1/mcp", ["127.0.0.1/32"], True, 1, "127.0.0.1"),
])
async def test_omitted_and_valid_explicit_ports_are_preserved(url, networks, allow_http, port, address):
    lookups = []

    async def resolve(host, requested_port):
        lookups.append((host, requested_port))
        return [address]

    pinned, host_header, server_name = await pin(url, networks, allow_http, resolver=resolve)
    assert lookups == [(server_name, port)]
    effective_port = pinned.port if pinned.port is not None else (443 if pinned.scheme == "https" else 80)
    assert effective_port == port
    assert pinned.host == address
    assert host_header == url.split("/")[2]
