"""Split storage keeps file originals stable while versioning both databases."""
import json
import sqlite3
import subprocess
import sys

import pytest

from hub.store import Store
from scripts.panel_update_runtime import COPY_STATE, storage_layout
from scripts.panel_update_source import UpdateError


def marker(path):
    (path / ".codepier-state.json").write_text(json.dumps({"schema_version": 1, "layout": "split-state-v1"}))


def test_split_store_routes_database_keys_without_touching_assets(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    state = tmp_path / "state"
    old = Store(state)
    old.execute("INSERT INTO meta(key,value) VALUES (?,?)", ("split-proof", "state-only"))
    old.close()
    marker(state)
    assets.mkdir()
    (assets / "attachment.txt").write_text("original")
    (assets / "hub.sqlite3").write_text("stale decoy - never open")
    (assets / "master.key").write_text("stale decoy - never read")
    monkeypatch.setenv("HUB_STATE_DIR", str(state))
    store = Store(assets)
    try:
        assert store.directory == assets
        assert store.state_directory == state
        assert store.one("SELECT value FROM meta WHERE key='split-proof'")["value"] == "state-only"
        assert store.decrypt(store.encrypt("test")) == "test"
    finally:
        store.close()
    assert (assets / "attachment.txt").read_text() == "original"
    assert (assets / "hub.sqlite3").read_text() == "stale decoy - never open"


def test_split_missing_state_fails_closed(tmp_path, monkeypatch):
    state = tmp_path / "state"; state.mkdir()
    marker(state)
    monkeypatch.setenv("HUB_STATE_DIR", str(state))
    with pytest.raises(RuntimeError, match="identity is incomplete"):
        Store(tmp_path / "assets")
    assert not (state / "hub.sqlite3").exists()


def copy_state(source, target):
    script = COPY_STATE.replace("Path('/backup'),Path('/app/data')", f"Path({str(source)!r}),Path({str(target)!r})")
    return subprocess.run([sys.executable, "-c", script], text=True, capture_output=True, timeout=20)


def test_snapshot_copies_wal_and_native_database_not_archives(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    target = tmp_path / "target"; target.mkdir()
    marker(source)
    (source / "master.key").write_text("matching-key")
    db = sqlite3.connect(source / "hub.sqlite3")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE proof(value)")
    db.execute("INSERT INTO proof VALUES ('committed-in-wal')"); db.commit()
    (source / "native-cli").mkdir()
    with sqlite3.connect(source / "native-cli/native.sqlite3") as native:
        native.execute("PRAGMA journal_mode=WAL")
        native.execute("CREATE TABLE proof(value)")
        native.execute("INSERT INTO proof VALUES ('native-state')")
    try:
        result = copy_state(source, target)
        assert result.returncode == 0, result.stderr
        with sqlite3.connect(target / "hub.sqlite3") as copied:
            assert copied.execute("SELECT value FROM proof").fetchone()[0] == "committed-in-wal"
        with sqlite3.connect(target / "native-cli/native.sqlite3") as copied:
            assert copied.execute("SELECT value FROM proof").fetchone()[0] == "native-state"
        assert not (target / "hub.sqlite3-wal").exists()
        assert (target / "master.key").read_bytes() == (source / "master.key").read_bytes()
        second = tmp_path / "second"; second.mkdir()
        result = copy_state(target, second)
        assert result.returncode == 0, result.stderr
    finally:
        native.close()
        db.close()


@pytest.mark.parametrize("name", ["unexpected.zip", "rekey.json", ".rekey"])
def test_snapshot_unknown_or_recovery_material_never_silently_omitted(tmp_path, name):
    source = tmp_path / "source"; source.mkdir()
    target = tmp_path / "target"; target.mkdir()
    (source / name).write_text("retain")
    result = copy_state(source, target)
    assert result.returncode != 0
    assert (source / name).read_text() == "retain"
    assert list(target.iterdir()) == []


def spec():
    return {"services": {"hub": {"environment": {"HUB_STATE_DIR": "/app/state", "HUB_DATA_DIR": "/app/data"},
            "volumes": [{"type": "volume", "source": "assets", "target": "/app/data"},
                        {"type": "volume", "source": "state", "target": "/app/state"}]}},
            "volumes": {"assets": {"external": True, "name": "stable-assets"},
                        "state": {"external": True, "name": "state-v1"}}}


@pytest.mark.parametrize("alias", [False, True])
def test_state_and_asset_volume_must_be_physically_distinct(alias):
    value = spec()
    if alias:
        value["volumes"]["state"]["name"] = "stable-assets"
    else:
        value["services"]["hub"]["volumes"][1]["source"] = "assets"
    with pytest.raises(UpdateError):
        storage_layout(value)


def test_split_rekey_keeps_store_asset_identity(tmp_path, monkeypatch):
    from hub.keyring import rotate_key
    assets = tmp_path / "assets"; assets.mkdir()
    state = tmp_path / "state"
    store = Store(state); store.close(); marker(state)
    monkeypatch.setenv("HUB_STATE_DIR", str(state))
    result = rotate_key(assets)
    assert result["status"] == "completed"
    assert (state / "rekey.json").is_file()
    assert not (assets / "hub.sqlite3").exists()

def test_offline_preparation_keeps_attachments_archives_and_original_db(tmp_path):
    from scripts.panel_state_migration import plan, prepare
    assets = tmp_path / "assets"
    state = tmp_path / "state"; state.mkdir()
    store = Store(assets)
    store.execute("INSERT INTO meta(key,value) VALUES ('migration-proof','kept')")
    store.close()
    (assets / "backups").mkdir()
    (assets / "backups/old.zip").write_bytes(b"archive")
    (assets / "attachment.bin").write_bytes(b"original")
    before = (assets / "hub.sqlite3").read_bytes()
    assert plan(assets, state)["delete"] == []
    assert list(state.iterdir()) == []
    result = prepare(assets, state, confirm_offline=True)
    assert result["prepared"] and not result["activate"]
    assert (assets / "hub.sqlite3").read_bytes() == before
    assert (assets / "attachment.bin").read_bytes() == b"original"
    assert (assets / "backups/old.zip").read_bytes() == b"archive"
    assert not (state / "backups").exists()
    assert not (state / "attachment.bin").exists()
    with sqlite3.connect(state / "hub.sqlite3") as copied:
        assert copied.execute("SELECT value FROM meta WHERE key='migration-proof'").fetchone()[0] == "kept"


def test_preparation_rejects_running_hub(tmp_path):
    from scripts.panel_state_migration import prepare
    from shared.instance_lock import InstanceLock
    assets = tmp_path / "assets"
    state = tmp_path / "state"; state.mkdir()
    store = Store(assets); store.close()
    lock = InstanceLock(assets / ".hub.lock")
    try:
        with pytest.raises(Exception):
            prepare(assets, state, confirm_offline=True)
    finally:
        lock.close()
    assert list(state.iterdir()) == []


def test_split_candidate_rejects_old_image_before_stop(tmp_path):
    from tests.test_panel_update_runtime import deployment, DockerBoundary
    from scripts.panel_update_runtime import DockerRuntime
    home, config, job, original = deployment(tmp_path)
    docker = DockerBoundary(tmp_path, original)
    runtime = DockerRuntime(tmp_path, home, config, execute=docker)
    source = home / "releases" / job["id"] / "source"; source.mkdir(parents=True)
    job.update(runtime.prepare(job, source))
    job["state_layout"] = "split-state-v1"
    with pytest.raises(UpdateError, match="候选版本"):
        runtime.build(job, source)
    assert not any("stop" in command for command in docker.commands)

def test_split_update_and_rollback_preserve_asset_mount_and_env(tmp_path):
    from tests.test_panel_update_runtime import deployment, DockerBoundary
    from scripts.panel_update_runtime import DockerRuntime, atomic_json, read_json
    home, config, job, original = deployment(tmp_path)
    original["services"]["hub"]["environment"].update(HUB_STATE_DIR="/app/state", HUB_DATA_DIR="/app/data")
    original["services"]["hub"]["volumes"][0]["target"] = "/app/state"
    original["services"]["hub"]["volumes"].append({"type": "volume", "source": "assets", "target": "/app/data"})
    original["volumes"]["assets"] = {"external": True, "name": "stable-original-assets"}
    current = read_json(home / "current.json");current["compose"] = original
    atomic_json(home / "current.json", current)
    atomic_json(home / "jobs" / job["id"] / "current.before.json", current)
    boundary = DockerBoundary(tmp_path, original)
    def docker(command, **kwargs):
        result = boundary(command, **kwargs)
        if command[:2] == ["docker", "inspect"]:
            info = json.loads(result)
            info[0]["Mounts"][0]["Destination"] = "/app/state"
            info[0]["Mounts"].append({"Destination": "/app/data", "Name": "stable-original-assets"})
            return json.dumps(info)
        if command[:2] == ["docker", "run"] and "/backup" not in command[-1]:
            result = json.loads(result);result["state_layout"] = "split-state-v1"
            return json.dumps(result)
        return result
    runtime = DockerRuntime(tmp_path, home, config, execute=docker)
    source = home / "releases" / job["id"] / "source";source.mkdir(parents=True)
    job.update(runtime.prepare(job, source))
    target = read_json(home / "jobs" / job["id"] / "target.spec.json")
    assert target["volumes"]["assets"] == original["volumes"]["assets"]
    assert target["volumes"]["hub-data"]["name"] == job["new_volume"]
    job["package"] = runtime.build(job, source)
    runtime.quiesce(job);runtime.stop(job);runtime.copy_data(job)
    runtime.start(job, "target");runtime.verify(job);runtime.commit(job)
    text = (tmp_path / ".env").read_text()
    assert "CODEPIER_HUB_STATE_VOLUME=" + job["new_volume"] in text
    assert "CODEPIER_HUB_DATA_VOLUME=" not in text
    runtime.rollback(job)
    restored = read_json(home / "current.json")
    assert restored["compose"]["volumes"]["assets"]["name"] == "stable-original-assets"
    assert restored["compose"]["volumes"]["hub-data"]["name"] == "original-data"
    assert not any("prune" in command or "rm" in command for command in boundary.commands)

def test_split_cli_backup_uses_matching_state_keys(tmp_path, monkeypatch):
    import zipfile
    from hub.__main__ import write_backup
    state = tmp_path / "state"
    store = Store(state); store.close(); marker(state)
    assets = tmp_path / "assets"; assets.mkdir()
    (assets / "master.key").write_text("decoy")
    monkeypatch.setenv("HUB_STATE_DIR", str(state))
    store = Store(assets)
    try:
        output = tmp_path / "backup.zip"
        write_backup(store, output)
        with zipfile.ZipFile(output) as archive:
            assert archive.read("master.key") == (state / "master.key").read_bytes()
            assert archive.read("master.key") != b"decoy"
    finally:
        store.close()


def test_incomplete_native_copy_has_no_initialized_receipt(tmp_path):
    source = tmp_path / "source"; source.mkdir()
    target = tmp_path / "target"; target.mkdir()
    marker(source); (source / "master.key").write_text("fixture-key")
    with sqlite3.connect(source / "hub.sqlite3") as db:
        db.execute("CREATE TABLE proof(value)")
    (source / "native-cli").mkdir()
    (source / "native-cli/native.sqlite3").write_bytes(b"not a SQLite database")
    result = copy_state(source, target)
    assert result.returncode != 0
    assert not (target / ".codepier-state.json").exists()
    assert (source / "native-cli/native.sqlite3").read_bytes() == b"not a SQLite database"
