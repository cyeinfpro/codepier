"""Probe behavior while the SQLite writer is occupied."""

from threading import Event

from fastapi.testclient import TestClient

from hub.app import create_app


def test_liveness_stays_ok_with_unready_writer(tmp_path, monkeypatch):
    app = create_app(str(tmp_path / "hub"))
    store = app.state.store
    held, release = Event(), Event()

    def hold():
        with store.transaction():
            held.set()
            assert release.wait(10)

    with TestClient(app) as client:
        assert client.get("/readyz").json() == {"status": "ok"}
        phase = client.portal.start_task_soon(store.run, hold)
        try:
            assert held.wait(3)
            monkeypatch.setattr(store.worker_metrics, "running_since", 0)
            assert client.get("/livez").json() == {"status": "ok"}
            result = client.get("/readyz")
            assert result.status_code == 503
            assert result.json() == {"status": "unavailable"}
        finally:
            release.set()
            phase.result(timeout=3)
        assert client.get("/readyz").json() == {"status": "ok"}
        assert client.get("/api/system/health").status_code == 401
