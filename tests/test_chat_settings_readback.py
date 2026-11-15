"""Native setting failures are not atomic; reconcile the original receipt."""
import pytest
from tests.test_chat_complete_backend import protocol, reply

@pytest.mark.parametrize("actual,success,outcome", [
    ({"model": {"provider": "p", "id": "new"}, "thinkingLevel": "low"}, True, "partial"),
    ({"model": {"provider": "p", "id": "old"}, "thinkingLevel": "low"}, True, "rejected"),
    ({"model": {"provider": "p", "id": "new"}, "thinkingLevel": "high"}, True, "applied"),
    ({}, False, "unknown"),
    ({"model": {"provider": 3, "id": "new"}, "thinkingLevel": "high"}, True, "unknown"),
    ({"model": {}, "thinkingLevel": "high"}, True, "unknown"),
    ({"model": None, "thinkingLevel": "high"}, True, "unknown"),
    ({"model": {"provider": "p", "id": "new"}, "thinkingLevel": []}, True, "unknown"),
    ({"model": {"provider": "p", "id": "new"}}, True, "unknown"),
])
def test_failed_sequence_reconciles_original_receipt(actual, success, outcome):
    p, wire, events, finished = protocol()
    p.ready = True
    p.settings.update(model={"provider": "p", "id": "old"}, thinkingLevel="low")
    p.change_settings("original", {"model": "p/new", "effort": "high"})
    reply(p, wire[-1], {"model": {"provider": "p", "id": "new"}})
    reply(p, wire[-1], {"levels": ["low"]})
    assert finished[-1] == ("original", "error")
    assert wire[-1]["type"] == "get_state"
    reply(p, wire[-1], actual, success=success)
    result = next(value for kind, value in reversed(events) if kind == "settings_result")
    assert result["receipt"] == "original"
    assert result["outcome"] == outcome
    assert not p.settings_readbacks
    assert not p.settings_busy

def test_old_readback_does_not_publish_over_new_request():
    p, wire, events, finished = protocol()
    p.ready = True
    p.change_settings("old", {"model": "p/old"})
    reply(p, wire[-1], success=False)
    old_read = wire[-1]
    p.change_settings("new", {"model": "p/new"})
    count = len(events)
    reply(p, old_read, {"model": {"provider": "p", "id": "old"}, "thinkingLevel": "low"})
    assert len(events) == count
    assert p.settings_receipt == "new"
    assert p.settings_busy

def test_missing_readback_expires_as_unknown():
    p, wire, events, finished = protocol()
    p.ready = True
    p.change_settings("r", {"model": "p/new"})
    reply(p, wire[-1], success=False)
    late = wire[-1]
    for tag in p.settings_readback_deadlines:
        p.settings_readback_deadlines[tag] = 0
    p.expire_settings_readbacks()
    assert events[-1] == ("settings_result", {"receipt": "r", "outcome": "unknown"})
    assert not p.settings_readbacks
    count = len(events)
    reply(p, late, {"model": {"provider": "p", "id": "new"}})
    assert len(events) == count
