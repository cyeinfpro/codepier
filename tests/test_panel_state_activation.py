"""Synthetic first-layout activation and durable crash-boundary recovery."""
import json
import sqlite3
import subprocess
import sys
import uuid

import pytest

from scripts import panel_updater as updater
from scripts import panel_update_runtime as runtime_module
from scripts.panel_update_runtime import DockerRuntime, atomic_json, read_json
from scripts.panel_update_source import UpdateError
from shared.util import VERSION
from tests.test_panel_update_runtime import deployment


class Crash(BaseException):
    pass


class LayoutDocker:
    """Deterministic Docker boundary, real SQLite copy; not a Docker rehearsal."""
    def __init__(self, root, spec):
        self.root, self.spec, self.phase, self.commands = root, spec, "old", []
        self.volumes = {"original-data": root / "asset-volume"}
        assets = self.volumes["original-data"]; assets.mkdir()
        (assets / "master.key").write_text("synthetic-key")
        (assets / "attachment.txt").write_text("original attachment")
        (assets / "old-backup.zip").write_bytes(b"old archive")
        with sqlite3.connect(assets / "hub.sqlite3") as db:
            db.execute("CREATE TABLE proof(value)")
            db.execute("INSERT INTO proof VALUES ('original')")
        (assets / "native-cli").mkdir()
        with sqlite3.connect(assets / "native-cli/native.sqlite3") as db:
            db.execute("CREATE TABLE proof(value)")
            db.execute("INSERT INTO proof VALUES ('native')")
        self.old_image = "sha256:" + "b" * 64
        self.new_volume = None
        self.capability = True
        self.target_image = self.old_image

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        if command[:2] == ["docker", "inspect"]:
            mounts = [{"Destination": "/app/data", "Name": "original-data"},
                {"Destination": "/run/codepier-updater", "RW": False,
                 "Source": str(self.root / ".codepier-updater/run")}]
            env = []
            if self.phase == "target":
                mounts.append({"Destination": "/app/state", "Name": self.new_volume})
                env = ["HUB_STATE_DIR=/app/state", "HUB_DATA_DIR=/app/data"]
            return json.dumps([{"Image": self.target_image if self.phase == "target" else self.old_image, "State": {"Running": False},
                "Config": {"Labels": {"com.docker.compose.project": "fixture", "com.docker.compose.service": "hub"}, "Env": env},
                "Mounts": mounts}])
        if command[:3] == ["docker", "image", "inspect"]:
            return self.target_image
        if command[:2] == ["docker", "build"]:
            self.target_image = "sha256:" + "c" * 64
            return "built"
        if command[:2] == ["docker", "exec"]:
            if "/agent/manifest.json" in command[-1]:
                return json.dumps({"version": VERSION, "sha256": "a" * 64, "bytes": 123})
            return json.dumps({"status": "ok", "version": VERSION,
                               "panel_update": {"maintenance": True, "ready": True}})
        if command[:3] == ["docker", "volume", "ls"]:
            return "\n".join(json.dumps(name) for name in self.volumes)
        if command[:3] == ["docker", "volume", "create"]:
            self.new_volume = command[-1]
            path = self.root / self.new_volume; path.mkdir()
            self.volumes[self.new_volume] = path
            return self.new_volume
        if command[:2] == ["docker", "run"]:
            if "/backup" in command[-1]:
                script = command[-1].replace("Path('/backup'),Path('/app/data')",
                    f"Path({str(self.volumes['original-data'])!r}),Path({str(self.volumes[self.new_volume])!r})")
                result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
                if result.returncode:
                    raise RuntimeError("synthetic copy failed: " + result.stderr)
                return result.stdout
            return json.dumps({"version": VERSION, "agent_sha256": "a" * 64, "agent_bytes": 123,
                               "state_layout": "split-state-v1" if self.capability else "legacy"})
        if "ps" in command:
            return "container-id"
        if "up" in command:
            self.phase = "target" if "target.json" in " ".join(command) else "old"
            return "healthy"
        if "stop" in command:
            return "stopped"
        raise AssertionError(command)


def fixture(tmp_path):
    home, config, unused_job, spec = deployment(tmp_path)
    docker = LayoutDocker(tmp_path, spec)
    runtime = DockerRuntime(tmp_path, home, config, execute=docker)
    manager = updater.Manager(tmp_path, runtime=runtime, start_workers=False)
    body = {"idempotency_key": "layout-" + uuid.uuid4().hex, "current_version": VERSION, "actor": "host-admin"}
    accepted = manager.submit("migrate-state", body, host_migration=True)
    job = read_json(home / "jobs" / accepted["operation"]["id"] / "status.json")
    return manager, runtime, docker, job, body


def test_migration_not_available_to_unprivileged_submit(tmp_path):
    manager, runtime, docker, job, body = fixture(tmp_path)
    with pytest.raises(UpdateError):
        manager.submit("migrate-state", body)


def test_same_version_migration_preserves_original_and_supports_idempotent_status(tmp_path):
    manager, runtime, docker, job, body = fixture(tmp_path)
    before = (tmp_path / ".env").read_bytes()
    manager.perform(job)
    assert job["state"] == "succeeded" and job["commit_decided"]
    assert job["from_version"] == job["target_version"] == VERSION
    current = read_json(manager.home / "current.json")
    assert current["compose"]["volumes"]["hub-data"]["name"] == "original-data"
    assert current["compose"]["volumes"]["hub-state"]["name"] == job["new_volume"]
    assert "COMPOSE_FILE=" + job["layout_config"] in (tmp_path / ".env").read_text()
    assert (manager.home / "jobs" / job["id"] / "env.before").read_bytes() == before
    assert (docker.volumes["original-data"] / "attachment.txt").read_text() == "original attachment"
    assert not (docker.volumes[job["new_volume"]] / "attachment.txt").exists()
    assert not (docker.volumes[job["new_volume"]] / "old-backup.zip").exists()
    assert (docker.volumes["original-data"] / "old-backup.zip").read_bytes() == b"old archive"
    assert not any(command[:2] == ["docker", "build"] or "prune" in command for command in docker.commands)
    count = len(docker.commands)
    # Repeated same intent returns original receipt; caller must not perform again.
    assert manager.submit("migrate-state", body, host_migration=True)["replayed"]
    assert len(docker.commands) == count


@pytest.mark.parametrize("phase", ["quiescing", "stopping", "copying", "starting", "verifying", "committing", "committed"])
def test_crash_after_phase_record_recovers_correct_side(tmp_path, monkeypatch, phase):
    manager, runtime, docker, job, body = fixture(tmp_path)
    before = (tmp_path / ".env").read_bytes()
    save = manager.save
    def crash_save(record, **values):
        save(record, **values)
        if values.get("phase") == phase:
            raise Crash()
    monkeypatch.setattr(manager, "save", crash_save)
    with pytest.raises(Crash):
        manager.perform(job)
    recovered = updater.Manager(tmp_path, runtime=runtime, start_workers=False)
    recovered.recover(manual=True)
    final = read_json(manager.home / "jobs" / job["id"] / "status.json")
    if phase == "committed":
        assert final["state"] == "succeeded"
        assert docker.phase == "target"
        assert final["commit_decided"]
    else:
        assert final["state"] in {"failed", "rolled_back"}
        assert docker.phase == "old"
        assert (tmp_path / ".env").read_bytes() == before
        assert "hub-state" not in read_json(manager.home / "current.json")["compose"]["volumes"]
    assert (docker.volumes["original-data"] / "attachment.txt").read_text() == "original attachment"


@pytest.mark.parametrize("boundary", ["layout-file", "env", "current-json", "before-decision"])
def test_partial_commit_never_opens_admission_and_restores_legacy(tmp_path, monkeypatch, boundary):
    manager, runtime, docker, job, body = fixture(tmp_path)
    before = (tmp_path / ".env").read_bytes()
    original_bytes = runtime_module.atomic_bytes
    original_save = manager.save
    def interrupted_bytes(path, content, mode=0o600):
        original_bytes(path, content, mode)
        path = str(path)
        trigger = ((boundary == "env" and path == str(tmp_path / ".env")) or
                   (boundary == "current-json" and path == str(manager.home / "current.json")) or
                   (boundary == "layout-file" and "/layouts/" in path and path.endswith(".json")))
        if trigger:
            raise Crash()
    def interrupted_save(record, **values):
        if boundary == "before-decision" and values.get("phase") == "committed":
            raise Crash()
        return original_save(record, **values)
    with monkeypatch.context() as scoped:
        scoped.setattr(runtime_module, "atomic_bytes", interrupted_bytes)
        scoped.setattr(manager, "save", interrupted_save)
        with pytest.raises(Crash):
            manager.perform(job)
    assert runtime.gate.exists()
    updater.Manager(tmp_path, runtime=runtime, start_workers=False).recover(manual=True)
    final = read_json(manager.home / "jobs" / job["id"] / "status.json")
    assert final["state"] == "rolled_back"
    assert (tmp_path / ".env").read_bytes() == before
    assert docker.phase == "old"
    assert not final.get("commit_decided")


def test_crash_after_admission_preserves_new_writes(tmp_path, monkeypatch):
    manager, runtime, docker, job, body = fixture(tmp_path)
    original = runtime.clear_gate
    def interrupted_clear():
        original()
        raise Crash()
    with monkeypatch.context() as scoped:
        scoped.setattr(runtime, "clear_gate", interrupted_clear)
        with pytest.raises(Crash):
            manager.perform(job)
    with sqlite3.connect(docker.volumes[job["new_volume"]] / "hub.sqlite3") as db:
        db.execute("INSERT INTO proof VALUES ('new acknowledged write')")
    updater.Manager(tmp_path, runtime=runtime, start_workers=False).recover(manual=True)
    assert docker.phase == "target"
    with sqlite3.connect(docker.volumes[job["new_volume"]] / "hub.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM proof").fetchone()[0] == 2
    with sqlite3.connect(docker.volumes["original-data"] / "hub.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM proof").fetchone()[0] == 1


def test_unsupported_current_image_never_stops_legacy_hub(tmp_path):
    manager, runtime, docker, job, body = fixture(tmp_path)
    docker.capability = False
    manager.perform(job)
    assert job["state"] == "failed"
    assert not any("stop" in command for command in docker.commands)
    assert list(docker.volumes) == ["original-data"]

def test_next_update_publishes_fresh_authoritative_compose_and_rollback(tmp_path):
    from scripts.panel_update_runtime import load_compose_config
    manager, runtime, docker, first, body = fixture(tmp_path)
    manager.perform(first)
    original_env = (tmp_path / ".env").read_bytes()
    original_current = read_json(manager.home / "current.json")
    second = {"id": uuid.uuid4().hex, "kind": "apply", "from_version": VERSION, "target_version": "9.9.9"}
    folder = manager.home / "jobs" / second["id"]; folder.mkdir()
    atomic_json(folder / "current.before.json", original_current)
    second.update(runtime.prepare(second, tmp_path))
    runtime.commit(second)
    current = read_json(manager.home / "current.json")
    selected_path = second["layout_config"]
    assert selected_path != first["layout_config"]
    assert "COMPOSE_FILE=" + selected_path in (tmp_path / ".env").read_text()
    resolved = load_compose_config((tmp_path / selected_path).read_text())
    assert resolved["volumes"]["hub-state"]["name"] == second["new_volume"]
    assert resolved["services"]["hub"]["image"] == second["image"]
    assert resolved["volumes"]["hub-data"]["name"] == "original-data"
    assert set(current["files"]) == {selected_path, ".env"}
    runtime.rollback(second)
    assert (tmp_path / ".env").read_bytes() == original_env
    assert read_json(manager.home / "current.json") == original_current


@pytest.mark.parametrize("mismatch", ["state-env", "data-env", "state-mount"])
def test_live_source_path_mismatch_rejected_before_stop(tmp_path, mismatch):
    manager, runtime, docker, job, body = fixture(tmp_path)
    original = docker.__call__
    def boundary(command, **kwargs):
        response = original(command, **kwargs)
        if command[:2] == ["docker", "inspect"]:
            info = json.loads(response)
            if mismatch == "state-env":
                info[0]["Config"]["Env"] = ["HUB_STATE_DIR=/unexpected"]
            elif mismatch == "data-env":
                info[0]["Config"]["Env"] = ["HUB_DATA_DIR=/unexpected"]
            else:
                info[0]["Mounts"].append({"Destination": "/app/state", "Name": "other-state"})
            return json.dumps(info)
        return response
    runtime.execute = boundary
    manager.perform(job)
    assert job["state"] == "failed"
    assert not any("stop" in command for command in docker.commands)


def test_unknown_maintenance_marker_blocks_new_migration(tmp_path):
    manager, runtime, docker, job, body = fixture(tmp_path)
    manager.save(job, state="failed")
    atomic_json(runtime.gate, {"operation_id": "another-operator"})
    body["idempotency_key"] = "other-" + uuid.uuid4().hex
    with pytest.raises(UpdateError, match="维护"):
        manager.submit("migrate-state", body, host_migration=True)
    assert read_json(runtime.gate)["operation_id"] == "another-operator"


def test_regular_apply_defaults_to_split_without_same_version_shortcut(tmp_path):
    manager, runtime, docker, job, body = fixture(tmp_path)
    job['kind'] = 'apply'
    prepared = runtime.prepare(job, tmp_path)
    job.update(prepared)
    assert job['layout_migration'] is True
    assert job['image'] != docker.old_image
    assert job['asset_volume'] == 'original-data'
    target = read_json(runtime.home / 'jobs' / job['id'] / 'target.spec.json')
    assert target['services']['hub']['environment']['HUB_STATE_DIR'] == '/app/state'
    assert target['volumes']['hub-data']['name'] == 'original-data'
    assert target['volumes']['hub-state']['name'] == job['new_volume']
    calls = []
    original = runtime.execute
    def execute(command, **kwargs):
        calls.append(command)
        return original(command, **kwargs)
    runtime.execute = execute
    job["package"] = runtime.build(job, tmp_path)
    assert any(command[:2] == ['docker', 'build'] for command in calls)
    docker.capability = False
    with pytest.raises(UpdateError, match='候选版本不支持'):
        runtime.build(job, tmp_path)
    assert not any('stop' in command for command in calls)


def test_default_apply_split_verifies_new_image_and_commits_new_source(tmp_path):
    manager, runtime, docker, job, body = fixture(tmp_path)
    job['kind'] = 'apply'
    source = manager.home / 'releases' / job['id'] / 'source'
    source.mkdir(parents=True)
    job.update(runtime.prepare(job, source))
    package = runtime.build(job, source)
    manager.cutover(job, package)
    assert job['state'] == 'succeeded'
    assert package['image_id'] == docker.target_image != docker.old_image
    env = (tmp_path / '.env').read_text()
    assert 'CODEPIER_HUB_SOURCE=' + job['source'] in env
    assert 'CODEPIER_HUB_STATE_VOLUME=' + job['new_volume'] in env
    assert (docker.volumes['original-data'] / 'attachment.txt').read_text() == 'original attachment'
