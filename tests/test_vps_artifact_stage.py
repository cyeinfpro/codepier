from __future__ import annotations

import asyncio
import hashlib
import stat
from types import SimpleNamespace

import pytest

from agent.vps_artifact_stage import CHUNK_LIMIT, StageArtifact, stage_artifact
from shared.util import DevError


class File:
    def __init__(self, peer, path):
        self.peer, self.path = peer, path
        self.position = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass

    async def write(self, chunk):
        self.peer.data[self.path] += chunk

    async def read(self, size):
        data = self.peer.data[self.path][self.position : self.position + size]
        self.position += len(data)
        return data


class Sftp:
    def __init__(self):
        self.directories = {"/tmp": stat.S_IFDIR | 0o1777}
        self.modes = {}
        self.data = {}
        self.rename_count = 0
        self.corrupt = False
        self.lost_ack = False

    async def mkdir(self, path, attrs):
        assert path not in self.directories
        self.directories[path] = stat.S_IFDIR | attrs.permissions

    async def lstat(self, path):
        if path in self.directories:
            return SimpleNamespace(permissions=self.directories[path], uid=0)
        return SimpleNamespace(permissions=self.modes[path], size=len(self.data[path]))

    def open(self, path, mode, attrs=None):
        if mode == "xb":
            assert path not in self.data
            self.data[path] = b""
            self.modes[path] = stat.S_IFREG | attrs.permissions
        elif self.corrupt:
            self.data[path] = self.data[path].swapcase()
        return File(self, path)

    async def rename(self, old, new):
        self.rename_count += 1
        assert new not in self.data
        self.data[new] = self.data.pop(old)
        self.modes[new] = self.modes.pop(old)
        if self.lost_ack:
            raise ConnectionError("reply lost")

    async def remove(self, path):
        del self.data[path], self.modes[path]

    async def rmdir(self, path):
        del self.directories[path]


def manifest(raw):
    return StageArtifact("0" * 32, hashlib.sha256(raw).hexdigest(), len(raw))


async def chunks(raw):
    for offset in range(0, len(raw), CHUNK_LIMIT):
        yield raw[offset : offset + CHUNK_LIMIT]


async def stage(sftp, expected, raw, authorize=lambda: None):
    return await stage_artifact(
        sftp,
        manifest(expected),
        chunks(raw),
        authorize,
        budget_seconds=2,
        attrs_factory=SimpleNamespace,
    )


@pytest.mark.asyncio
async def test_multichunk_roundtrip_only_publishes_complete_private_file():
    peer = Sftp()
    raw = (b"\x00\xffPACK" * 50000) + b"tail"
    receipt = await stage(peer, raw, raw)
    path = receipt["remote_path"]
    assert receipt["stage_state"] == "verified"
    assert receipt["install_state"] == "not_started"
    assert peer.data == {path: raw}
    assert stat.S_IMODE(peer.modes[path]) == 0o600
    assert stat.S_IMODE(peer.directories[path.rsplit("/", 1)[0]]) == 0o700
    assert peer.rename_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_local", [True, False])
async def test_digest_failure_cleans_only_owned_partial_data(bad_local):
    peer = Sftp()
    peer.corrupt = not bad_local
    with pytest.raises(DevError) as e:
        await stage(peer, b"ABC", b"AXC" if bad_local else b"ABC")
    assert e.value.code == "VPS_STAGE_DIGEST_MISMATCH"
    assert not peer.data and peer.rename_count == 0 and len(peer.directories) == 1


@pytest.mark.asyncio
async def test_rename_ack_loss_is_unknown_and_does_not_delete_published_bytes():
    peer = Sftp()
    peer.lost_ack = True
    with pytest.raises(DevError) as e:
        await stage(peer, b"ABC", b"ABC")
    assert e.value.code == "VPS_STAGE_COMMIT_UNKNOWN"
    assert peer.rename_count == 1 and list(peer.data.values()) == [b"ABC"]


@pytest.mark.asyncio
async def test_revocation_stops_publication_and_cleans_partial_data():
    peer = Sftp()

    def authorize():
        if b"ABC" in peer.data.values():
            raise DevError("STALE_GRANT", "permission revoked", 403)

    with pytest.raises(DevError) as e:
        await stage(peer, b"ABC", b"ABC", authorize)
    assert e.value.code == "STALE_GRANT"
    assert peer.rename_count == 0 and not peer.data


@pytest.mark.asyncio
async def test_refuses_unsafe_staging_root_before_mutation():
    peer = Sftp()
    peer.directories["/tmp"] = stat.S_IFDIR | 0o777
    with pytest.raises(DevError) as e:
        await stage(peer, b"ABC", b"ABC")
    assert e.value.code == "VPS_STAGE_UNSAFE_ROOT"
    assert not peer.data and len(peer.directories) == 1


@pytest.mark.asyncio
async def test_times_out_before_publication():
    peer = Sftp()

    async def delayed():
        await asyncio.sleep(1)
        yield b"ABC"

    with pytest.raises(TimeoutError):
        await stage_artifact(
            peer,
            manifest(b"ABC"),
            delayed(),
            lambda: None,
            budget_seconds=0.03,
            attrs_factory=SimpleNamespace,
        )
    assert peer.rename_count == 0 and not peer.data


@pytest.mark.asyncio
async def test_real_sftp_writes_only_inside_disposable_loopback_root(tmp_path):
    ssh = pytest.importorskip("asyncssh")
    (tmp_path / "tmp").mkdir()
    (tmp_path / "tmp").chmod(0o1777)

    class Server(ssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return (username, password) == ("fixture", "fixture-secret")

    class IsolatedSftp(ssh.SFTPServer):
        def __init__(self, channel):
            super().__init__(channel, chroot=str(tmp_path))

        def lstat(self, path):
            attrs = super().lstat(path)
            if path == b"/tmp":
                attrs = ssh.SFTPAttrs.from_local(attrs)
                attrs.uid = 0
            return attrs

    listener = await ssh.create_server(
        Server,
        "127.0.0.1",
        0,
        server_host_keys=[ssh.generate_private_key("ssh-ed25519")],
        sftp_factory=IsolatedSftp,
    )
    raw = b"fixture package\x00" * 32768
    from agent.vps_ssh import connect_agent_vps

    try:
        conn = await connect_agent_vps(
            host="127.0.0.1",
            port=listener.get_port(),
            username="fixture",
            password="fixture-secret",
        )
        try:
            async with conn.connection.start_sftp_client(sftp_version=3) as peer:
                receipt = await stage_artifact(
                    peer,
                    manifest(raw),
                    chunks(raw),
                    lambda: None,
                    budget_seconds=5,
                )
        finally:
            await conn.close()
    finally:
        listener.close()
        await listener.wait_closed()
    result = tmp_path / receipt["remote_path"].lstrip("/")
    assert result.read_bytes() == raw and stat.S_IMODE(result.stat().st_mode) == 0o600
    assert not list(result.parent.glob("*.part"))


def test_public_contract_keeps_old_exec_shape_and_requires_exact_stage_intent():
    from pydantic import ValidationError
    from shared.core_contracts import Exec

    legacy = Exec(project="fixture", command="true", idempotency_key="legacy-123").model_dump()
    assert "stage_artifact" not in legacy
    base = {
        "project": "fixture",
        "target": "vps:" + "1" * 32,
        "timeout_seconds": 120,
        "idempotency_key": "fixture-123",
        "stage_artifact": {"artifact_id": "0" * 32, "sha256": "2" * 64},
    }
    assert Exec(**base).model_dump()["stage_artifact"] == base["stage_artifact"]
    bad_options = (
        {"target": "agent"},
        {"command": "true"},
        {"workspace_id": "3" * 32},
        {"options": {"read_only": True}},
        {"env": {"TMPDIR": "/home"}},
        {"stage_artifact": {**base["stage_artifact"], "destination": "/etc"}},
    )
    for bad in bad_options:
        with pytest.raises(ValidationError):
            Exec(**(base | bad))
