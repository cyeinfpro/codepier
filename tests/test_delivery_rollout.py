"""Canary selection and rollback preserve original durable work on other devices."""

from collections import Counter

import pytest

from shared.config import ConfigurationError, RuntimeConfig
from tests.test_delivery_concurrency import delivery as delivery, insert_queued


@pytest.mark.parametrize(
    "setting,value",
    [
        ("HUB_DELIVERY_SLOTS", "0"),
        ("HUB_DELIVERY_SLOTS", "65"),
        ("HUB_DELIVERY_PREFETCH", "0"),
        ("HUB_DELIVERY_PREFETCH", "9"),
        ("HUB_DELIVERY_DEVICE_CHUNK", "0"),
        ("HUB_DELIVERY_DEVICE_CHUNK", "129"),
        ("HUB_DELIVERY_ROLLING_DEVICES", "hot,*"),
        ("HUB_DELIVERY_ROLLING_DEVICES", "device_*"),
        ("HUB_DELIVERY_ROLLING_DEVICES", "a" * 101),
    ],
)
def test_invalid_rollout_setting_fails_startup(monkeypatch, setting, value):
    monkeypatch.setenv(setting, value)
    with pytest.raises(ConfigurationError, match=setting):
        RuntimeConfig.from_env()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery,canary", [("hot", True), ("", False)], indirect=["delivery"])
async def test_canary_and_rollback_leave_other_device_in_fixed_mode(delivery, canary, monkeypatch):
    e = delivery
    # Keep both devices below the global cap, with more items than the canary chunk.
    for device in ("hot", "cold"):
        insert_queued(e, device, 20)
    rows = await e.store.run_read(e.runtime._delivery_candidates, ())
    assert e.runtime.delivery_config.rolling_for("hot") is canary
    assert e.runtime.delivery_config.rolling_for("cold") is False
    assert Counter(row["device_id"] for row in rows) == {
        "hot": e.runtime.delivery_config.delivery_device_chunk if canary else 20,
        "cold": 20,
    }
    for device in ("hot", "cold"):
        prepared = []

        async def prepare(identifier, *, device_id=None, timing=None):
            assert device_id == device
            if not prepared:
                insert_queued(e, device, 1, start=20)
            prepared.append(identifier)
            e.store.execute("UPDATE operations SET state='succeeded' WHERE id=?", (identifier,))
            return None

        monkeypatch.setattr(e.runtime, "_prepare_for_send", prepare)
        selected = [row for row in rows if row["device_id"] == device]
        await e.runtime.deliver_device(
            [row["id"] for row in selected], device_id=device, selected=selected
        )
        expected_count = 21 if canary and device == "hot" else 20
        assert prepared == [f"{device}{i:03d}" for i in range(expected_count)]
    remaining = await e.store.run_read(e.runtime._delivery_candidates, ())
    assert {row["id"] for row in remaining} == ({"cold020"} if canary else {"cold020", "hot020"})
