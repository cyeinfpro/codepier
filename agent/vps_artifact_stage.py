"""Stage one immutable artifact via SFTP without invoking a remote shell."""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import stat
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from shared.util import DevError

STAGE_ROOT = "/tmp"
CHUNK_LIMIT = 256 * 1024
MAX_STAGE_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class StageArtifact:
    artifact_id: str
    sha256: str
    size: int

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{32}", self.artifact_id):
            raise ValueError("Invalid artifact identifier")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("Invalid artifact digest")
        if type(self.size) is not int or not 0 < self.size <= MAX_STAGE_BYTES:
            raise ValueError("Invalid artifact size")


def _require_mode(attrs, directory, mode):
    bits = attrs.permissions
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not isinstance(bits, int) or not kind(bits) or stat.S_IMODE(bits) != mode:
        raise DevError("VPS_STAGE_UNSAFE_MODE", "远端文件类型或权限无法验证，未继续传包", 409)


@dataclass
class StageSession:
    sftp: object
    artifact: StageArtifact
    ensure_authorized: Callable[[], None]
    attrs_factory: Callable
    directory: str
    directory_created: bool = False
    file_created: bool = False
    commit_attempted: bool = False

    @property
    def incomplete(self):
        return self.directory + "/artifact.part"

    @property
    def complete(self):
        return self.directory + "/artifact.bin"

    async def write(self, chunks):
        self.ensure_authorized()
        parent = await self.sftp.lstat(STAGE_ROOT)
        bits = parent.permissions
        if (
            parent.uid != 0
            or not isinstance(bits, int)
            or not stat.S_ISDIR(bits)
            or not bits & stat.S_ISVTX
        ):
            raise DevError("VPS_STAGE_UNSAFE_ROOT", "VPS 缺少安全的临时目录，未开始传包", 409)
        self.ensure_authorized()
        await self.sftp.mkdir(self.directory, self.attrs_factory(permissions=0o700))
        self.directory_created = True
        _require_mode(await self.sftp.lstat(self.directory), True, 0o700)
        self.ensure_authorized()
        async with self.sftp.open(
            self.incomplete, "xb", self.attrs_factory(permissions=0o600)
        ) as remote:
            self.file_created = True
            _require_mode(await self.sftp.lstat(self.incomplete), False, 0o600)
            await self._copy(chunks, remote)

    async def _copy(self, chunks, remote):
        digest = hashlib.sha256()
        count = 0
        async for chunk in chunks:
            self.ensure_authorized()
            if not isinstance(chunk, bytes) or not 0 < len(chunk) <= CHUNK_LIMIT:
                raise DevError("VPS_STAGE_BAD_CHUNK", "构建产物返回了无效数据块", 409)
            count += len(chunk)
            if count > self.artifact.size:
                raise _digest_failure()
            digest.update(chunk)
            await remote.write(chunk)
        self._verify_digest(count, digest)

    async def verify(self):
        self.ensure_authorized()
        attrs = await self.sftp.lstat(self.incomplete)
        _require_mode(attrs, False, 0o600)
        if attrs.size != self.artifact.size:
            raise _digest_failure()
        count, digest = 0, hashlib.sha256()
        async with self.sftp.open(self.incomplete, "rb") as remote:
            while True:
                self.ensure_authorized()
                block = await remote.read(CHUNK_LIMIT)
                if not block:
                    break
                count += len(block)
                if count > self.artifact.size:
                    raise _digest_failure()
                digest.update(block)
        self._verify_digest(count, digest)

    def _verify_digest(self, count, digest):
        if count != self.artifact.size or digest.hexdigest() != self.artifact.sha256:
            raise _digest_failure()

    async def remove_incomplete(self):
        if self.commit_attempted or not self.directory_created:
            return
        if self.file_created:
            await self.sftp.remove(self.incomplete)
        await self.sftp.rmdir(self.directory)


def _digest_failure():
    return DevError(
        "VPS_STAGE_DIGEST_MISMATCH", "远端或本地产物大小／SHA-256 不匹配，未确认传包", 409
    )


async def stage_artifact(
    sftp,
    artifact: StageArtifact,
    chunks: AsyncIterator[bytes],
    ensure_authorized: Callable[[], None],
    *,
    budget_seconds: float,
    attrs_factory=None,
    record=lambda _stage: None,
):
    """Return a usable path only after remote readback and exclusive rename.

    No arbitrary path, remote command, shell fallback, or automatic replay. An
    uncertain final rename receives a distinct outcome and is never cleaned up
    speculatively. The caller owns the durable operation journal.
    """
    if attrs_factory is None:
        from asyncssh import SFTPAttrs

        attrs_factory = SFTPAttrs
    session = StageSession(
        sftp,
        artifact,
        ensure_authorized,
        attrs_factory,
        f"{STAGE_ROOT}/.codepier-stage-{secrets.token_hex(16)}",
    )
    try:
        return await asyncio.wait_for(_stage(session, chunks, record), timeout=budget_seconds)
    except BaseException as error:
        if session.commit_attempted:
            raise DevError(
                "VPS_STAGE_COMMIT_UNKNOWN",
                "VPS 最终提交结果未知；查询原操作，不要重新上传或安装",
                409,
            ) from error
        try:
            await asyncio.wait_for(session.remove_incomplete(), timeout=1.0)
        except Exception:  # Cleanup is best effort; retain the first failure.
            pass
        raise


async def _stage(session, chunks, record):
    record("vps_artifact_write")
    await session.write(chunks)
    record("vps_artifact_verify")
    await session.verify()
    session.ensure_authorized()
    record("vps_artifact_commit")
    session.commit_attempted = True
    await session.sftp.rename(session.incomplete, session.complete)
    return {
        "artifact_id": session.artifact.artifact_id,
        "sha256": session.artifact.sha256,
        "bytes": session.artifact.size,
        "remote_path": session.complete,
        "stage_state": "verified",
        "install_state": "not_started",
    }


async def _snapshot_chunks(agent, project, artifact):
    import base64

    from agent.artifacts import CHUNK

    for offset in range(0, artifact.size, CHUNK):
        part = await agent.disk_call(agent.artifacts.chunk, project, artifact.artifact_id, offset)
        if part["sha256"] != artifact.sha256 or part["offset"] != offset:
            raise _digest_failure()
        yield base64.b64decode(part["data"], validate=True)


def _authorized_stage(agent, operation_id, not_after=None):
    import time

    if (
        operation_id in agent.cancelled
        or agent.stop_event.is_set()
        or agent.journal.is_cancelled(operation_id)
    ):
        raise DevError("CANCELLED", "传包已取消，未启动安装", 409)
    if not_after is not None and time.time() > not_after:
        raise DevError("QUEUE_EXPIRED", "传包还未开始就超过执行期限", 409)


def _prepared_stage(agent, project, args):
    envelope = project.get("_core_ssh")
    request = args["stage_artifact"]
    record = envelope.get("artifact") if isinstance(envelope, dict) else None
    if not isinstance(record, dict) or any(
        record.get(key) != request.get(key) for key in ("artifact_id", "sha256")
    ):
        raise DevError("VPS_TRANSPORT_MISSING", "未收到 Hub 核验的产物和 VPS 身份", 409)
    if project.get("_runtime_requirement"):
        raise DevError(
            "RUNTIME_REMOTE_TARGET_UNSUPPORTED", "隔离容器项目不能使用本地 VPS 传输", 403
        )
    _, spec = agent.engine.root(project, True)
    if not project.get("allow_tasks") or not spec.get("allow_tasks"):
        raise DevError("TASKS_DISABLED", "当前项目不允许 VPS 传输", 403)
    artifact = StageArtifact(record["artifact_id"], record["sha256"], record["bytes"])
    return envelope, artifact


async def stage_for_operation(agent, operation_id, project, args, not_after):
    from asyncssh import Error as SSHError

    from agent.vps_ssh import VPSRuntimeBudget, VPSSSHConnectionError, connect_agent_vps

    conn = None
    envelope, artifact = await agent.disk_call(_prepared_stage, agent, project, args)
    _authorized_stage(agent, operation_id, not_after)
    budget = VPSRuntimeBudget.start(agent.bounded_timeout(project, args["timeout_seconds"]))
    agent.phase(operation_id, "vps_artifact_connect")
    try:
        conn = await connect_agent_vps(
            host=envelope["host"],
            port=envelope["port"],
            username=envelope["username"],
            password=envelope["password"],
            budget=budget,
        )
        _authorized_stage(agent, operation_id, not_after)
        try:
            sftp = await asyncio.wait_for(
                conn.connection.start_sftp_client(sftp_version=3),
                timeout=budget.require_remaining_budget(),
            )
        except SSHError as exc:
            raise DevError(
                "VPS_SFTP_UNAVAILABLE", "目标没有可用 SFTP 子系统，未写入文件", 409
            ) from exc
        _authorized_stage(agent, operation_id, not_after)
        receipt = await stage_artifact(
            sftp,
            artifact,
            _snapshot_chunks(agent, project, artifact),
            lambda: _authorized_stage(agent, operation_id),
            budget_seconds=budget.require_remaining_budget(),
            record=lambda stage: agent.phase(operation_id, stage),
        )
        return {**receipt, "operation_id": operation_id, "target": args["target"]}
    except VPSSSHConnectionError as exc:
        raise DevError("VPS_STAGE_SSH_FAILED", "VPS SSH 未连接，未确认传包", 409) from exc
    except asyncio.TimeoutError as exc:
        raise DevError("VPS_STAGE_TIMEOUT", "VPS 传包超时，未确认可供安装的文件", 504) from exc
    except (SSHError, OSError) as exc:
        raise DevError(
            "VPS_STAGE_IO_FAILED", "VPS 传包通道写入或回读失败，未确认可供安装的文件", 502
        ) from exc
    finally:
        if conn is not None:
            await conn.close(timeout=2)
