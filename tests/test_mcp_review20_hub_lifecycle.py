"""Recovery stages and observation source regressions, using local fixtures only."""

from types import SimpleNamespace

import pytest

from tests.test_b11_b17_bg_gateway import _seed_hub


@pytest.fixture
def hub(tmp_path):
    store, runtime, *_ = _seed_hub(tmp_path)
    try:
        yield store, runtime.collaboration
    finally:
        store.close()


def record(store, key):
    store.execute("INSERT INTO meta(key,value) VALUES(?, 'marker')", (key,))


def recorded(store, key):
    return store.one("SELECT value FROM meta WHERE key=?", (key,)) is not None


def test_reconcile_failure_never_rolls_back_security_cleanup(hub, monkeypatch):
    store, service = hub

    def corrupt_record():
        record(store, "lifecycle-failed")
        raise RuntimeError("synthetic malformed record")

    monkeypatch.setattr(
        service.room_connections, "drain_revoke_jobs", lambda: record(store, "revoked")
    )
    monkeypatch.setattr(service.rooms.host_proofs, "expire", lambda: record(store, "expired"))
    monkeypatch.setattr(service, "reconcile", corrupt_record)
    with pytest.raises(RuntimeError, match="synthetic"):
        service.loops._run_reconcile(True, True, True, False)
    assert recorded(store, "revoked") and recorded(store, "expired")
    assert not recorded(store, "lifecycle-failed")


def test_failing_event_recovery_does_not_block_dispatch(hub, monkeypatch):
    store, service = hub

    def corrupt_record():
        record(store, "recovery-failed")
        raise RuntimeError("synthetic malformed record")

    def mailbox(*, max_wakes):
        assert max_wakes == 4
        record(store, "mailbox")
        return 1

    def reserve(capacity):
        record(store, "reserved")
        return [capacity]

    monkeypatch.setattr(service.rooms, "recover_paused_work", corrupt_record)
    monkeypatch.setattr(service.dot_relay, "recover", lambda: 0)
    monkeypatch.setattr(service.dot_mailbox, "recover", mailbox)
    monkeypatch.setattr(service.room_consumers, "recover", lambda *, max_wakes: 0)
    monkeypatch.setattr(service.task_liveness, "recover", lambda *, max_wakes: 0)
    monkeypatch.setattr(service.events, "reserve", reserve)
    assert service.events._run_tick_work(2, False, True, True) == [2]
    assert recorded(store, "mailbox") and recorded(store, "reserved")
    assert not recorded(store, "recovery-failed")


def test_idle_hints_do_not_suppress_periodic_canonical_recovery(hub):
    _, service = hub
    service.clock = lambda: 120
    service.loops._full_reconcile_at = 121
    service.events._full_event_recovery_at = 121
    assert not service.loops._probe_reconcile()[2]
    assert not service.events._probe_tick(4, True)[1]
    service.loops._full_reconcile_at = 120
    service.events._full_event_recovery_at = 120
    assert service.loops._probe_reconcile()[2]
    assert service.events._probe_tick(4, True)[1]


def test_liveness_overlay_reads_only_usable_original_device(tmp_path):
    from hub.collaboration.task_liveness import TaskLiveness

    class FixtureStore:
        def all(self, query, args):
            assert args == ("one", "two")
            return [
                {"work_item_id": "one", "device_id": "live"},
                {"work_item_id": "one", "device_id": "older"},
                {"work_item_id": "two", "device_id": "closed"},
            ]

    runtime = SimpleNamespace(connections={
        "live": SimpleNamespace(last_seen=99, unusable=False),
        "older": SimpleNamespace(last_seen=81, unusable=False),
        "closed": SimpleNamespace(last_seen=105, unusable=True),
        "unrelated": SimpleNamespace(last_seen=106, unusable=False),
    })
    liveness = TaskLiveness(SimpleNamespace(store=FixtureStore(), runtime=runtime))
    assert liveness._live_node_contacts(["one", "two"], "?,?") == {"one": 99}
