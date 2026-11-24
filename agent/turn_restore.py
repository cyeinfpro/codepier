"""Joint turn + file restore coordinator and hidden Git ref checkpoint helper."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import subprocess
import tempfile
import time
from typing import Any, Callable, Literal, Mapping, Sequence
import uuid

from agent.resource_queue import Claim
from agent.workspace_ownership import WorkspaceOwnershipStore
from shared.native_cli import append_control_tx, identifier, immediate
from shared.util import DevError, fsync_directory

RESTORE_MODES = frozenset({"native_rewind", "replacement_session", "files_only", "unsupported"})
_GIT_ENV_STRIP = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
)


def _clean_git_env() -> dict[str, str]:
    env = dict(os.environ)
    for key in _GIT_ENV_STRIP:
        env.pop(key, None)
    return env


def _run_git(repo_root: Path, args: Sequence[str], *, env: Mapping[str, str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise DevError("RESTORE_GIT_FAILED", proc.stderr.strip() or "Git checkpoint failed", 409)
    return proc.stdout.strip()


def create_hidden_git_checkpoint(
    repo_root: str | Path,
    *,
    checkpoint_id: str,
    excludes: Sequence[str] = (),
) -> dict[str, Any]:
    """Create a commit under refs/codepier/checkpoints/<id> using an isolated index."""
    root = Path(repo_root)
    if not checkpoint_id or "/" in checkpoint_id or ".." in checkpoint_id:
        raise ValueError("Invalid checkpoint_id")
    ref = f"refs/codepier/checkpoints/{checkpoint_id}"
    base_env = _clean_git_env()
    with tempfile.TemporaryDirectory(prefix="codepier-git-idx-") as tmp:
        index_path = Path(tmp) / "index"
        env = {**base_env, "GIT_INDEX_FILE": str(index_path)}
        head = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(root),
            env=base_env,
            capture_output=True,
            text=True,
            check=False,
        )
        add_args = ["add", "-A", "--", "."]
        for pattern in excludes:
            add_args.append(f":(exclude){pattern}")
        _run_git(root, add_args, env=env)
        tree_oid = _run_git(root, ["write-tree"], env=env)
        parent_args = (
            ["-p", head.stdout.strip()] if head.returncode == 0 and head.stdout.strip() else []
        )
        commit_oid = _run_git(
            root,
            ["commit-tree", tree_oid, *parent_args, "-m", f"codepier checkpoint {checkpoint_id}"],
            env=env,
        )
        _run_git(root, ["update-ref", ref, commit_oid], env=base_env)
        return {"ref": ref, "tree": tree_oid, "commit": commit_oid}


def _safe_target_path(root: Path, rel: str) -> Path:
    posix = PurePosixPath(rel)
    if posix.is_absolute() or ".." in posix.parts or not rel:
        raise DevError("RESTORE_PATH_INVALID", f"非法恢复路径: {rel}", 400)
    resolved_root = root.resolve()
    candidate = root / posix
    if candidate.is_symlink():
        raise DevError("RESTORE_PATH_INVALID", f"拒绝覆盖符号链接: {rel}", 409)
    resolved = candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise DevError("RESTORE_PATH_INVALID", f"恢复路径越界: {rel}", 403)
    return candidate


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise DevError("RESTORE_PATH_INVALID", f"目标路径不是普通文件: {path}", 409)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".codepier-restore-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


class TurnRestoreCoordinator:
    """Staged restore coordinator backed by native_restore_journals and strict restore claims."""

    def _validate_mode(
        self,
        mode: str,
        provider_capabilities: Mapping[str, Any] | None,
    ) -> None:
        if mode not in RESTORE_MODES or mode == "unsupported":
            raise DevError("RESTORE_UNSUPPORTED", "当前会话不支持所选回退模式", 409)
        if provider_capabilities is None:
            return
        if mode == "native_rewind" and not provider_capabilities.get(
            "conversation_rollback", {}
        ).get("available"):
            raise DevError("RESTORE_UNSUPPORTED", "Provider 不支持原生回退", 409)
        if mode == "replacement_session" and not provider_capabilities.get(
            "context_import", {}
        ).get("available"):
            raise DevError("RESTORE_UNSUPPORTED", "Provider 不支持替代会话承接", 409)

    def _load_journal(self, conn: sqlite3.Connection, restore_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM native_restore_journals WHERE restore_id=?", (restore_id,)
        ).fetchone()
        if row is None:
            raise DevError("RESTORE_NOT_FOUND", "恢复计划不存在", 404)
        return row

    def prepare_restore(
        self,
        conn: sqlite3.Connection,
        *,
        restore_id: str | None = None,
        session_id: str,
        receipt: str,
        attempt_id: str,
        checkpoint_ref: str,
        mode: Literal["native_rewind", "replacement_session", "files_only", "unsupported"],
        target_files: Sequence[Mapping[str, Any]],
        expected_workspace_version: int,
        owner_epoch: int,
        claims: WorkspaceOwnershipStore,
        workspace_claims: Sequence[Claim],
        provider_capabilities: Mapping[str, Any] | None = None,
        target_provider_instance_id: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Acquire a strict restore claim and persist the multi-stage restore journal."""
        self._validate_mode(mode, provider_capabilities)
        session_id = identifier(session_id)
        receipt = identifier(receipt)
        if not attempt_id or not checkpoint_ref:
            raise ValueError("attempt_id and checkpoint_ref are required")
        rid = restore_id or uuid.uuid4().hex
        now_ts = time.time() if now is None else now
        plan = {
            "files": [dict(entry) for entry in target_files],
            "owner_epoch": owner_epoch,
        }
        with immediate(conn):
            claims.acquire_claim_tx(
                conn,
                claims=workspace_claims,
                phase="restore",
                receipt=receipt,
                attempt_id=attempt_id,
                owner_epoch=owner_epoch,
                workspace_version=expected_workspace_version,
                now=now_ts,
            )
            conn.execute(
                """INSERT INTO native_restore_journals(
                     restore_id, session_id, receipt, attempt_id, checkpoint_ref,
                     mode, stage, target_provider_instance_id,
                     expected_workspace_version, plan_json, results_json,
                     created_at, updated_at
                   ) VALUES (?,?,?,?,?,?,'prepared',?,?,?,'{}',?,?)""",
                (
                    rid,
                    session_id,
                    receipt,
                    attempt_id,
                    checkpoint_ref,
                    mode,
                    target_provider_instance_id,
                    expected_workspace_version,
                    json.dumps(plan, ensure_ascii=False, separators=(",", ":")),
                    now_ts,
                    now_ts,
                ),
            )
        return {"restore_id": rid, "stage": "prepared", "mode": mode}

    def apply_provider_step(
        self,
        conn: sqlite3.Connection,
        restore_id: str,
        *,
        provider_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        replacement_session_id: str | None = None,
        target_native_thread_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Execute native rewind or prepare replacement thread before modifying files."""
        journal = self._load_journal(conn, restore_id)
        if journal["stage"] != "prepared":
            raise DevError(
                "RESTORE_INVALID_STAGE", f"当前阶段 {journal['stage']} 无法执行会话步骤", 409
            )
        now_ts = time.time() if now is None else now
        mode = journal["mode"]
        results = json.loads(journal["results_json"] or "{}")
        if mode == "files_only":
            return {"restore_id": restore_id, "stage": "prepared", "mode": mode}
        if mode == "replacement_session":
            if not replacement_session_id:
                raise DevError("RESTORE_UNSUPPORTED", "缺少替代会话编号", 409)
            results["provider"] = {"replacement_session_id": replacement_session_id}
            with immediate(conn):
                conn.execute(
                    """UPDATE native_restore_journals
                       SET stage='replacement_thread_prepared',
                           replacement_session_id=?,
                           target_native_thread_id=COALESCE(?, target_native_thread_id),
                           results_json=?,
                           updated_at=?
                       WHERE restore_id=? AND stage='prepared'""",
                    (
                        replacement_session_id,
                        target_native_thread_id,
                        json.dumps(results, ensure_ascii=False, separators=(",", ":")),
                        now_ts,
                        restore_id,
                    ),
                )
            return {"restore_id": restore_id, "stage": "replacement_thread_prepared", "mode": mode}

        if provider_fn is None:
            raise DevError("RESTORE_UNSUPPORTED", "暂未接入 Provider 原生回退", 409)
        return self._apply_native_rewind(
            conn, journal, provider_fn, target_native_thread_id, now_ts
        )

    def _apply_native_rewind(
        self,
        conn: sqlite3.Connection,
        journal: sqlite3.Row,
        provider_fn: Callable[[dict[str, Any]], dict[str, Any]],
        target_native_thread_id: str | None,
        now_ts: float,
    ) -> dict[str, Any]:
        restore_id = journal["restore_id"]
        # Persist ambiguity before crossing the external side-effect boundary.
        # A concurrent caller or a restart must never replay an unconfirmed rewind.
        with immediate(conn):
            changed = conn.execute(
                """UPDATE native_restore_journals
                   SET stage='uncertain', error_code='PROVIDER_REWIND_IN_FLIGHT', updated_at=?
                   WHERE restore_id=? AND stage='prepared'""",
                (now_ts, restore_id),
            )
            if changed.rowcount != 1:
                raise DevError("RESTORE_INVALID_STAGE", "已有回退正在处理或等待对账", 409)

        results = json.loads(journal["results_json"] or "{}")
        try:
            outcome = provider_fn(dict(journal))
            if not isinstance(outcome, Mapping) or outcome.get("ok") is not True:
                raise DevError("PROVIDER_REWIND_UNCONFIRMED", "Provider 未确认会话已回退", 409)
            results["provider"] = dict(outcome)
            serialized = json.dumps(results, ensure_ascii=False, separators=(",", ":"))
        except Exception as exc:
            with immediate(conn):
                conn.execute(
                    """UPDATE native_restore_journals SET error_code=?, updated_at=?
                       WHERE restore_id=? AND stage='uncertain'
                         AND error_code='PROVIDER_REWIND_IN_FLIGHT'""",
                    (getattr(exc, "code", "PROVIDER_REWIND_FAILED"), now_ts, restore_id),
                )
            raise

        with immediate(conn):
            changed = conn.execute(
                """UPDATE native_restore_journals
                   SET stage='provider_rewind_applied', error_code=NULL,
                       target_native_thread_id=COALESCE(?, target_native_thread_id),
                       results_json=?, updated_at=?
                   WHERE restore_id=? AND stage='uncertain'
                     AND error_code='PROVIDER_REWIND_IN_FLIGHT'""",
                (
                    outcome.get("native_thread_id") or target_native_thread_id,
                    serialized,
                    now_ts,
                    restore_id,
                ),
            )
            if changed.rowcount != 1:
                raise DevError(
                    "RESTORE_INVALID_STAGE", "Provider 回退结果需要对账，未恢复文件", 409
                )
        return {
            "restore_id": restore_id,
            "stage": "provider_rewind_applied",
            "mode": journal["mode"],
        }

    def _restore_single_file(
        self,
        root: Path,
        entry: Mapping[str, Any],
        writer: Callable[[Path, bytes], None],
    ) -> dict[str, Any]:
        rel = entry["path"]
        target = _safe_target_path(root, rel)
        current_sha = _file_sha256(target)
        expected_sha = entry.get("expected_current_sha256")
        if current_sha != expected_sha:
            return {
                "path": rel,
                "status": "conflict",
                "error_code": "CONCURRENT_MODIFICATION",
                "expected_sha256": expected_sha,
                "actual_sha256": current_sha,
            }
        content = entry.get("target_content")
        try:
            if content is None:
                if target.exists():
                    target.unlink()
                    fsync_directory(target.parent)
                return {"path": rel, "status": "deleted"}
            raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
            writer(target, raw)
            return {
                "path": rel,
                "status": "restored",
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        except OSError as exc:
            return {
                "path": rel,
                "status": "failed",
                "error_code": "FILE_WRITE_FAILED",
                "detail": str(exc),
            }

    def restore_files_checked(
        self,
        conn: sqlite3.Connection,
        restore_id: str,
        *,
        root: str | Path,
        file_writer: Callable[[Path, bytes], None] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Verify each file's expected current SHA before restoring; fail to partial without dropping barrier."""
        journal = self._load_journal(conn, restore_id)
        expected_stage = {
            "files_only": "prepared",
            "native_rewind": "provider_rewind_applied",
            "replacement_session": "replacement_thread_prepared",
        }.get(journal["mode"])
        if journal["stage"] != expected_stage:
            raise DevError("RESTORE_INVALID_STAGE", f"阶段 {journal['stage']} 不允许恢复文件", 409)

        root_path = Path(root)
        plan = json.loads(journal["plan_json"])
        results = json.loads(journal["results_json"] or "{}")
        writer = file_writer or _atomic_write_bytes
        file_outcomes: list[dict[str, Any]] = []
        failed_item: dict[str, Any] | None = None

        for entry in plan.get("files", []):
            outcome = self._restore_single_file(root_path, entry, writer)
            file_outcomes.append(outcome)
            if outcome["status"] in ("conflict", "failed"):
                failed_item = outcome
                break

        now_ts = time.time() if now is None else now
        results["files"] = file_outcomes
        new_stage = "partial" if failed_item is not None else "files_restored"
        error_code = failed_item["error_code"] if failed_item is not None else None
        with immediate(conn):
            conn.execute(
                """UPDATE native_restore_journals
                   SET stage=?, error_code=?, results_json=?, updated_at=?
                   WHERE restore_id=?""",
                (
                    new_stage,
                    error_code,
                    json.dumps(results, ensure_ascii=False, separators=(",", ":")),
                    now_ts,
                    restore_id,
                ),
            )
        return {
            "restore_id": restore_id,
            "stage": new_stage,
            "error_code": error_code,
            "files": file_outcomes,
            "barrier_held": failed_item is not None,
        }

    def commit_runtime_binding(
        self,
        conn: sqlite3.Connection,
        restore_id: str,
        *,
        claims: WorkspaceOwnershipStore,
        new_workspace_version: int | None = None,
        owner_epoch: int = 1,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Commit updated runtime binding, bump workspace_version, and release restore barrier."""
        journal = self._load_journal(conn, restore_id)
        if journal["stage"] != "files_restored":
            raise DevError(
                "RESTORE_INCOMPLETE",
                f"恢复处于 {journal['stage']} 阶段，禁止释放工作区屏障",
                409,
            )
        now_ts = time.time() if now is None else now
        next_version = (
            new_workspace_version
            if new_workspace_version is not None
            else int(journal["expected_workspace_version"]) + 1
        )
        with immediate(conn):
            conn.execute(
                """UPDATE sessions
                   SET workspace_version=?,
                       native_thread=COALESCE(?, native_thread),
                       provider_instance_id=CASE
                         WHEN ? != '' THEN ? ELSE provider_instance_id
                       END,
                       updated=?
                   WHERE id=?""",
                (
                    next_version,
                    journal["target_native_thread_id"],
                    journal["target_provider_instance_id"],
                    journal["target_provider_instance_id"],
                    now_ts,
                    journal["session_id"],
                ),
            )
            conn.execute(
                """UPDATE native_restore_journals
                   SET stage='completed', new_workspace_version=?, updated_at=?
                   WHERE restore_id=? AND stage='files_restored'""",
                (next_version, now_ts, restore_id),
            )
            claims.release_or_drain_tx(
                conn, (journal["receipt"], journal["attempt_id"]), now=now_ts
            )
            append_control_tx(
                conn,
                event_id=f"turn-restored:{restore_id}",
                session_id=journal["session_id"],
                receipt=journal["receipt"],
                attempt_id=journal["attempt_id"],
                worker_epoch=owner_epoch,
                kind="turn.restored",
                payload={
                    "restore_id": restore_id,
                    "checkpoint_ref": journal["checkpoint_ref"],
                    "mode": journal["mode"],
                    "workspace_version": next_version,
                },
                now=now_ts,
            )
        return {
            "restore_id": restore_id,
            "stage": "completed",
            "workspace_version": next_version,
        }
