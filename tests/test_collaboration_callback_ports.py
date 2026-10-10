"""Explicit callback ports must be valid before any DNS or HTTP work."""
import pytest

from hub.collaboration import network
from shared.util import DevError


@pytest.mark.asyncio
@pytest.mark.parametrize('authority', [
    'callback.example.invalid:0',
    'callback.example.invalid:000',
    '[2606:4700:4700::1111]:0',
    'callback.example.invalid:65536',
    'callback.example.invalid:-1',
])
async def test_invalid_callback_port_never_resolves(monkeypatch, authority):
    async def unexpected_resolve(*args):
        pytest.fail('invalid callback port reached DNS')
    monkeypatch.setattr(network, 'resolve', unexpected_resolve)
    with pytest.raises(DevError) as caught:
        await network.callback_destination('https://' + authority + '/hook')
    assert caught.value.code == 'CALLBACK_URL_REJECTED'


@pytest.mark.asyncio
@pytest.mark.parametrize('suffix, expected_port', [
    ('', 443), (':1', 1), (':443', 443), (':8443', 8443), (':65535', 65535),
])
async def test_valid_callback_port_is_preserved(monkeypatch, suffix, expected_port):
    calls = []
    async def fake_resolve(host, port):
        calls.append((host, port))
        return ['8.8.8.8']
    monkeypatch.setattr(network, 'resolve', fake_resolve)
    pinned, host, name = await network.callback_destination(
        'https://callback.example.invalid' + suffix + '/hook')
    assert calls == [('callback.example.invalid', expected_port)]
    assert pinned.host == '8.8.8.8'
    assert (pinned.port or 443) == expected_port
    assert host == 'callback.example.invalid' + suffix
    assert name == 'callback.example.invalid'
