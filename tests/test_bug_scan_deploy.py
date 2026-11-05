"""Regression coverage for the 2026-11-05 upgrade audit."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import stat
import sys

import pytest

from scripts import migrate_hub, migrate_hub_data


@pytest.mark.parametrize("admin", [True, False])
def test_read_only_wal_admin_probe_does_not_touch_source(tmp_path, admin):
    source = tmp_path / "source"
    source.mkdir()
    database = source / "hub.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE users (id TEXT)")
    if admin:
        connection.execute("INSERT INTO users VALUES ('fixture')")
    connection.commit()
    connection.close()
    assert not Path(str(database) + "-wal").exists()
    before = database.read_bytes()
    database.chmod(0o400)
    source.chmod(0o500)
    try:
        assert migrate_hub_data.probe_admin(source) == (0 if admin else 3)
        assert database.read_bytes() == before
        assert sorted(p.name for p in source.iterdir()) == ["hub.sqlite3"]
    finally:
        source.chmod(0o700)
        database.chmod(0o600)


def test_admin_probe_preserves_uncheckpointed_wal(tmp_path):
    database = tmp_path / "hub.sqlite3"
    connection = sqlite3.connect(database)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE users (id TEXT)")
        connection.execute("INSERT INTO users VALUES ('wal-admin')")
        connection.commit()
        assert Path(str(database) + "-wal").exists()
        assert migrate_hub_data.probe_admin(tmp_path) == 0
    finally:
        connection.close()


def test_admin_probe_failure_is_not_no_admin(tmp_path):
    (tmp_path / "hub.sqlite3").write_bytes(b"invalid database")
    assert migrate_hub_data.probe_admin(tmp_path) == 4


def test_journal_replace_syncs_parent_directory(tmp_path, monkeypatch):
    calls = []
    original = os.fsync
    def record(fd):
        calls.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        original(fd)
    monkeypatch.setattr(os, "fsync", record)
    journal = tmp_path / "journal.json"
    migrate_hub.write_state(journal, {"stage": "starting", "write_boundary": True})
    assert calls == ["file", "directory"]
    assert json.loads(journal.read_text())["write_boundary"]


@pytest.mark.parametrize("action", ["commit", "write-boundary", "rollback"])
def test_missing_migration_journal_fails_closed(tmp_path, monkeypatch, action):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["migrate_hub", action, "--root", str(tmp_path)])
    assert migrate_hub.main() == 1


@pytest.mark.parametrize("name", ["hub.sqlite3-wal", "native.sqlite-shm", "cache.db-journal"])
def test_evidence_inventory_rejects_database_sidecars(tmp_path, name):
    import time
    from scripts import archive_evidence
    source = tmp_path / "evidence"
    source.mkdir()
    (source / name).write_bytes(b"private database pages")
    with pytest.raises(ValueError, match="dedicated backup"):
        archive_evidence.inventory(source, time.time() + 10)


@pytest.mark.parametrize("code,stderr", [(125, "daemon unavailable"),
    (1, "RuntimeError: source changed"), (0, "RuntimeError: Destination/backup is not empty")])
def test_docker_probe_rejects_unrelated_failures(code, stderr):
    from types import SimpleNamespace
    from scripts.check_codepier_migration_docker import confirm_occupied_refusal
    with pytest.raises(AssertionError):
        confirm_occupied_refusal(SimpleNamespace(returncode=code, stderr=stderr))


def test_docker_probe_accepts_specific_occupied_guard():
    from types import SimpleNamespace
    from scripts.check_codepier_migration_docker import confirm_occupied_refusal
    confirm_occupied_refusal(SimpleNamespace(returncode=1,
        stderr="Traceback...\nRuntimeError: Destination/backup is not empty\n"))


def test_https_overlay_requires_real_configuration_and_secures_panel():
    root = Path(__file__).resolve().parents[1]
    example = (root / ".env.example").read_text()
    assert "MCP_DOMAIN=\n" in example and "TLS_EMAIL=\n" in example
    overlay = (root / "deploy/compose.https.yml").read_text()
    assert 'ports: !override' in overlay
    assert '127.0.0.1:${HUB_PORT:-8765}:8765' in overlay
    assert 'HUB_PUBLIC_URL: "https://${MCP_DOMAIN:?' in overlay
    assert 'caddy:2@sha256:' in overlay
    assert 'no-new-privileges:true' in overlay and 'cap_drop:' in overlay


def test_oidc_fixture_dependencies_are_digest_pinned():
    import re
    root = Path(__file__).resolve().parents[1]
    images = re.findall(r"image:\s*(\S+)", (root / "tests/fixtures/oidc-authentik/compose.yml").read_text())
    assert len(images) == 3
    assert all(re.fullmatch(r".+@sha256:[a-f0-9]{64}", image) for image in images)


def test_k3s_render_binds_both_images_to_verified_digest():
    from scripts.render_k3s import render
    root = Path(__file__).resolve().parents[1]
    rendered, metadata = render(root, "ghcr.io/cyeinfpro/codepier", "sha256:" + "a" * 64, "b" * 40)
    assert rendered.count("@sha256:" + "a" * 64) == 2
    assert "RELEASE_DIGEST" not in rendered
    assert metadata["source_commit"] == "b" * 40


@pytest.mark.parametrize("digest", ["latest", "sha256:short", "sha256:" + "A" * 64])
def test_k3s_renderer_rejects_mutable_or_invalid_digest(digest):
    from scripts.render_k3s import render
    with pytest.raises(ValueError):
        render(Path(__file__).resolve().parents[1], "ghcr.io/cyeinfpro/codepier", digest, "b" * 40)


def test_admin_probe_dangling_symlink_is_not_empty_database(tmp_path):
    (tmp_path / "hub.sqlite3").symlink_to(tmp_path / "missing.sqlite3")
    assert migrate_hub_data.probe_admin(tmp_path) == 4
