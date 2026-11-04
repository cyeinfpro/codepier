from __future__ import annotations
import difflib
import errno
import fnmatch
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from shared.crypto import digest
from shared.file_publish import AtomicCreateUnsupported, ExclusiveCreateUnconfirmed, publish_new_file
from shared.util import DevError, fsync_directory
from agent.journal import Journal

MAX_FILE = 1024 * 1024
MAX_DIFF = 100000
MAX_DIFF_CHANGED_LINES = 4000
MAX_SCAN_ENTRIES = 100000
IGNORED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".ssh", ".aws", ".azure", ".kube", ".codepier-agent", ".remote-dev-agent", ".remote-dev", ".codepier", ".cache"}
GENERATED_SCAN_DIRS = frozenset({".build", ".next", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".turbo"})
CHECKPOINT_ARTIFACTS = {".build", ".next", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".turbo", "coverage", "playwright-report", "test-results"}
STORAGE_UNAVAILABLE = frozenset(getattr(errno, name) for name in (
    "EIO", "ESTALE", "ENOTCONN", "ETIMEDOUT", "ENODEV", "ENXIO",
    "ENETDOWN", "ENETUNREACH", "EHOSTUNREACH", "ECONNRESET", "ECONNABORTED",
) if hasattr(errno, name))


def protected(relative: str) -> bool:
    parts = tuple(part.lower() for part in PurePosixPath(relative).parts)
    private_runtime = {'.codex': {'sessions', 'archived_sessions'},
                       '.claude': {'projects'}}
    if any(parts[index + 1] in private_runtime.get(part, ())
           for index, part in enumerate(parts[:-1])):
        return True
    for low in parts:
        if low in IGNORED_DIRS or low.startswith((".rd-", ".venv-", ".venv.")):
            return True
        if low in {".env", ".npmrc", ".pypirc", ".netrc", "id_rsa", "id_ed25519",
                   "auth.json", "credentials.json", ".credentials.json", "secrets.json"}:
            return True
        if low.startswith(".env.") and not low.endswith((".example", ".sample", ".template")):
            return True
        if low.endswith((".pem", ".key", ".p12", ".pfx")):
            return True
    return False


def scan_ignored(relative: str) -> bool:
    # Generated work is excluded from enumeration, not an authorization rule.
    # Explicitly authorized projects can themselves live under a .work parent.
    return any((part.casefold() in GENERATED_SCAN_DIRS or part.casefold() == ".work") or part.casefold().startswith(".venv")
               for part in PurePosixPath(relative).parts)


def relative_path(value: str, allow_dot: bool = True) -> str:
    if not isinstance(value, str) or "\x00" in value or "\\" in value or ":" in value:
        raise DevError("INVALID_PATH", "项目路径需使用 / 分隔的相对路径")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DevError("INVALID_PATH", "项目路径必须是有效的 UTF-8 文本") from exc
    p = PurePosixPath(value)
    if p.is_absolute() or ".." in p.parts or (not allow_dot and str(p) == "."):
        raise DevError("INVALID_PATH", "不允许绝对路径、上级路径或空文件路径")
    if protected(str(p)):
        raise DevError("PROTECTED_PATH", "该路径属于凭据或内部目录，不通过文件工具公开", 403)
    return str(p)


def within(path: Path, root: Path):
    return path == root or root in path.parents


class FileEngine:
    def __init__(self, config: dict, journal: Journal, config_path: Path):
        self.config = config
        self.journal = journal
        self.config_path = config_path.resolve()
        # Nested project mappings can address the same file under different roots.
        self.mutation_lock = threading.RLock()

    def root(self, project: dict, write: bool = False) -> tuple[Path, dict]:
        raw = Path(project["root"]).expanduser()
        if not raw.is_absolute():
            raise DevError("INVALID_ROOT", "项目根目录必须是本机绝对路径")
        try:
            actual = raw.resolve(strict=True)
            root_stat = actual.stat()
        except FileNotFoundError as exc:
            raise DevError("ROOT_MISSING", "执行节点上的项目目录不存在，或共享磁盘尚未挂载；请检查路径与挂载状态", 404) from exc
        except OSError as exc:
            if exc.errno in STORAGE_UNAVAILABLE:
                raise DevError("FILESYSTEM_UNAVAILABLE", "项目存储暂时不可访问；请检查磁盘或网络共享连接，增加路径授权不能修复连接故障", 503,
                               filesystem_errno=exc.errno, mutation_started=False) from exc
            raise
        if not stat.S_ISDIR(root_stat.st_mode):
            raise DevError("INVALID_ROOT", "项目根路径不是目录")
        if protected(actual.as_posix()):
            raise DevError("PROTECTED_ROOT", "不能把凭据、依赖或内部目录直接映射成项目", 403)
        matches = []
        for spec in self.config.get("allowed_roots", []):
            allowed = Path(spec["path"]).expanduser().resolve()
            if within(actual, allowed):
                matches.append((len(allowed.parts), spec))
        if not matches:
            raise DevError("ROOT_NOT_ALLOWED", "这个路径未在执行节点 Agent 的 allowed_roots 中授权", 403)
        spec = max(matches, key=lambda x: x[0])[1]
        if write and (project.get("mode") != "write" or not spec.get("writable", True)):
            raise DevError("READ_ONLY", "项目或本机授权目录是只读的", 403)
        if within(actual, self.journal.directory) or actual == self.config_path.parent:
            raise DevError("PROTECTED_ROOT", "不能把 Agent 配置/状态目录映射为项目", 403)
        return actual, spec

    def agent_private(self, path: Path) -> bool:
        # Conservatively protect aliases on case-insensitive APFS/NTFS as well
        # as legacy adjacent backups and temporary configuration writes.
        key = lambda value: os.path.normcase(str(value)).casefold()
        config, candidate, state = key(self.config_path), key(path), key(self.journal.directory)
        return (candidate == config or candidate == state or candidate.startswith(state + os.sep)
                or key(path.parent) == key(self.config_path.parent)
                and key(path.name).startswith(key(self.config_path.name) + "."))

    def path(self, root: Path, value: str, allow_dot: bool = True) -> Path:
        rel = relative_path(value, allow_dot)
        current = root
        for part in PurePosixPath(rel).parts:
            current = current / part
            if current.is_symlink():
                raise DevError("SYMLINK_BLOCKED", "文件工具不跟随符号链接或目录联接", 403)
            # is_junction is available on Python 3.12+; resolve containment also covers junctions.
            if hasattr(current, "is_junction") and current.is_junction():
                raise DevError("SYMLINK_BLOCKED", "不跟随 Windows 目录联接", 403)
        resolved = current.resolve(strict=False)
        if not within(resolved, root):
            raise DevError("OUTSIDE_PROJECT", "路径超出项目范围", 403)
        if self.agent_private(resolved):
            raise DevError("PROTECTED_PATH", "不能访问 Agent 配置、配置备份或状态目录", 403)
        return current

    def check_local_write(self, path: Path):
        actual = path.resolve(strict=False)
        matches = [(len(allowed.parts), spec) for spec in self.config.get("allowed_roots", [])
                   if within(actual, allowed := Path(spec["path"]).expanduser().resolve())]
        if not matches or not max(matches, key=lambda x: x[0])[1].get("writable", True):
            raise DevError("READ_ONLY", "目标文件属于本机只读授权目录", 403)

    @staticmethod
    def sync_directory(directory: Path, **recovery):
        try:
            fsync_directory(directory)
        except OSError as exc:
            unsupported = exc.errno in {errno.EINVAL, errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
            message = ("文件系统已改变，但此存储不支持目录落盘确认；请核查当前文件和备份，勿盲目重试" if unsupported else
                       "文件系统已改变，但目录落盘失败；请先核查当前文件和备份，勿盲目重试")
            raise DevError("DURABILITY_UNCONFIRMED", message, 500,
                           filesystem_errno=exc.errno, durability="unconfirmed", retryable=False, **recovery) from exc

    @staticmethod
    def publish_new_file(source, destination, **kwargs):
        """Share safe publication across text writes, moves and native imports."""
        try:
            return publish_new_file(source, destination, allow_exclusive_copy=True, **kwargs)
        except ExclusiveCreateUnconfirmed as exc:
            raise DevError("WRITE_OUTCOME_UNCONFIRMED",
                           "兼容新建未完成确认，目标可能包含部分内容；未覆盖同名文件，请核查原回执和目标后再继续，勿直接重试",
                           500, filesystem_errno=exc.errno, retryable=False,
                           publication="unconfirmed", mutation_started=True,
                           atomic=False, partial_content_visible=True, destination_overwritten=False) from exc
        except AtomicCreateUnsupported as exc:
            raise DevError("ATOMIC_CREATE_UNSUPPORTED",
                           "当前文件系统不支持原子发布或受保护的排他新建，无法安全创建文件；未覆盖同名目标。请使用兼容存储或在共享服务端运行 Agent", 409,
                           retryable=False, destination_overwritten=False) from exc
        except OSError as exc:
            if exc.errno in STORAGE_UNAVAILABLE or exc.errno == errno.EINTR:
                raise DevError("WRITE_OUTCOME_UNCONFIRMED",
                               "发布文件时存储连接或 I/O 中断，最终状态尚未确认；请检查目标文件和原操作回执后再决定，勿直接重复写入", 500,
                               filesystem_errno=exc.errno, retryable=False,
                               publication="unconfirmed") from exc
            raise

    @staticmethod
    def cleanup_staging(staging_path, **recovery):
        # A rename fallback has already consumed this name.  Missing is normal;
        # a disconnected share is not proof that cleanup succeeded.
        original = sys.exception()
        try:
            os.unlink(staging_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if original is not None:
                original.add_note("Staging cleanup also failed; inspect the operation before replaying it")
                if isinstance(original, DevError):
                    original.details["staging_cleanup"] = "unconfirmed"
            else:
                raise DevError("STAGING_CLEANUP_UNCONFIRMED",
                               "临时文件清理未确认；目标可能已发布，请核查原操作和文件状态后再继续", 500,
                               filesystem_errno=exc.errno, retryable=False,
                               staging_cleanup="unconfirmed", **recovery) from exc

    def read_bytes(self, path: Path, missing: bool = False, *, max_bytes: int = MAX_FILE, verify_path: bool = False) -> bytes | None:
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(path, flags)
        except FileNotFoundError:
            if missing:
                return None
            raise DevError("NOT_FOUND", "文件不存在", 404)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise DevError("SYMLINK_BLOCKED", "文件工具不跟随符号链接", 403) from exc
            if exc.errno in STORAGE_UNAVAILABLE:
                raise DevError("FILESYSTEM_UNAVAILABLE", "文件存储暂时不可访问；请检查磁盘或网络共享连接", 503,
                               filesystem_errno=exc.errno) from exc
            raise
        try:
            s = os.fstat(fd)
            if not stat.S_ISREG(s.st_mode):
                raise DevError("NOT_FILE", "只能读取普通文件")
            if s.st_nlink > 1:
                raise DevError("HARDLINK_BLOCKED", "不读写有多个硬链接的文件", 403)
            if s.st_size > max_bytes:
                raise DevError("FILE_TOO_LARGE", f"单个文件上限为 {max_bytes} bytes；请用 exec 拆分读取")
            chunks, size = [], 0
            while size <= max_bytes:
                block = os.read(fd, min(65536, max_bytes + 1 - size))
                if not block:
                    break
                chunks.append(block)
                size += len(block)
            data = b"".join(chunks)
            if len(data) > max_bytes:
                raise DevError("FILE_TOO_LARGE", f"文件超过 {max_bytes} bytes")
            latest = os.fstat(fd)
            if (s.st_size, s.st_mtime_ns, s.st_ctime_ns) != (latest.st_size, latest.st_mtime_ns, latest.st_ctime_ns):
                raise DevError("FILE_CHANGED", "文件在读取期间改变，请重新读取", 409)
            if verify_path:
                current = path.lstat()
                if (not stat.S_ISREG(current.st_mode) or
                        (current.st_dev, current.st_ino) != (latest.st_dev, latest.st_ino)):
                    raise DevError("FILE_CHANGED", "文件名在读取核验期间指向了不同文件", 409)
            return data
        except OSError as exc:
            if exc.errno in STORAGE_UNAVAILABLE:
                raise DevError("FILESYSTEM_UNAVAILABLE", "读取期间存储连接或 I/O 中断；未返回不完整内容", 503,
                               filesystem_errno=exc.errno) from exc
            raise
        finally:
            os.close(fd)

    @staticmethod
    def text(data: bytes) -> str:
        if b"\x00" in data:
            raise DevError("BINARY_FILE", "该文件是二进制文件，不作为源码文本读取")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise DevError("ENCODING", "当前只编辑 UTF-8 文件，未改动文件编码") from exc

    @staticmethod
    def encode_content(content: str) -> bytes:
        try:
            data = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise DevError("INVALID_CONTENT", "内容必须是有效的 UTF-8 文本") from exc
        if len(data) > MAX_FILE:
            raise DevError("FILE_TOO_LARGE", "UTF-8 编码后的内容超过 1 MiB")
        if b"\x00" in data:
            raise DevError("INVALID_CONTENT", "源码文本不能包含 NUL 字符")
        return data

    @staticmethod
    def diff(path: str, before: bytes | None, after: bytes | None):
        old = before.decode("utf-8", errors="replace") if before is not None else ""
        new = after.decode("utf-8", errors="replace") if after is not None else ""
        if old == new:
            return {"diff": "", "diff_truncated": False, "added_lines": 0, "removed_lines": 0}
        old_lines, new_lines = old.splitlines(keepends=True), new.splitlines(keepends=True)
        # Bound SequenceMatcher's quadratic work on reordered lines, even when
        # both source files are small. Trim unchanged ends so a small edit in a
        # large file still gets an exact diff with the usual three context lines.
        prefix = 0
        while prefix < min(len(old_lines), len(new_lines)) and old_lines[prefix] == new_lines[prefix]:
            prefix += 1
        old_end, new_end = len(old_lines), len(new_lines)
        while old_end > prefix and new_end > prefix and old_lines[old_end - 1] == new_lines[new_end - 1]:
            old_end -= 1
            new_end -= 1
        if old_end + new_end - 2 * prefix > MAX_DIFF_CHANGED_LINES:
            return {"diff": "Diff omitted: changed region exceeds the comparison budget; inspect locally before editing.",
                    "diff_truncated": True, "added_lines": new_end - prefix, "removed_lines": old_end - prefix,
                    "line_counts_approximate": True}
        line_offset = max(0, prefix - 3)
        old_lines = old_lines[line_offset:old_end + 3]
        new_lines = new_lines[line_offset:new_end + 3]
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=True)
        ops = matcher.get_opcodes()
        added = sum(j2-j1 for tag, i1, i2, j1, j2 in ops if tag in {"insert", "replace"})
        removed = sum(i2-i1 for tag, i1, i2, j1, j2 in ops if tag in {"delete", "replace"})
        parts, size, truncated = [], 0, False
        def emit(text):
            nonlocal size, truncated
            left = MAX_DIFF-size
            if left <= 0:
                truncated = True
                return False
            parts.append(text[:left]); size += min(left, len(text))
            if len(text) > left:
                truncated = True
                return False
            return True
        groups = matcher.get_grouped_opcodes(3)
        for group in groups:
            if not parts:
                emit(f"--- a/{path}\n+++ b/{path}\n")
            first, last = group[0], group[-1]
            old_count, new_count = last[2] - first[1], last[4] - first[3]
            old_start = first[1] + line_offset + (1 if old_count else 0)
            new_start = first[3] + line_offset + (1 if new_count else 0)
            if not emit(f"@@ -{old_start},{old_count} +{new_start},{new_count} @@\n"):
                break
            for tag, i1, i2, j1, j2 in group:
                segments = [(" ", old_lines[i1:i2])] if tag == "equal" else []
                if tag in {"replace", "delete"}: segments.append(("-", old_lines[i1:i2]))
                if tag in {"replace", "insert"}: segments.append(("+", new_lines[j1:j2]))
                for prefix, lines in segments:
                    for line in lines:
                        suffix = "" if line.endswith("\n") else "\n\\ No newline at end of file\n"
                        if not emit(prefix + line + suffix): break
                    if truncated: break
                if truncated: break
            if truncated: break
        return {"diff": "".join(parts), "diff_truncated": truncated, "added_lines": added, "removed_lines": removed}

    def walk(self, root: Path, start: Path, depth: int | None = None, *, exclude=None, deadline=None, files_first=False):
        # Bound directory enumeration as well as matching files. A tree containing
        # only directories or ignored names must not bypass search/checkpoint limits.
        scanned = 0
        def check_budget():
            if scanned > MAX_SCAN_ENTRIES or deadline is not None and time.monotonic() > deadline:
                raise DevError("SCAN_LIMIT", "目录扫描达到数量或时间上限；请缩小路径范围")

        def entries(directory):
            nonlocal scanned
            names = []
            try:
                self.path(root, directory.relative_to(root).as_posix())
                with os.scandir(directory) as iterator:
                    for entry in iterator:
                        scanned += 1
                        check_budget()
                        p = Path(entry.path)
                        rel = p.relative_to(root).as_posix()
                        # Filter names before metadata I/O. SMB stat calls are
                        # expensive; excluded build trees must not consume them.
                        # Count every entry above so exclusions cannot evade budgets.
                        if protected(rel) or (exclude is not None and exclude(rel)) or scan_ignored(rel):
                            continue
                        st = entry.stat(follow_symlinks=False)
                        names.append((p, st))
            except OSError as exc:
                raise DevError("SCAN_FAILED", "目录在扫描期间改变或无法读取；未完成扫描") from exc
            preferred = stat.S_ISREG if files_first else stat.S_ISDIR
            return iter(sorted(names, key=lambda item: (not preferred(item[1].st_mode), item[0].name.casefold(), item[0].name)))

        # An explicit stack avoids silently omitting paths below 100 levels and
        # avoids Python recursion limits on deep, otherwise valid source trees.
        stack = [(entries(start), 1)]
        while stack:
            check_budget()
            iterator, level = stack[-1]
            try:
                p, s = next(iterator)
            except StopIteration:
                stack.pop()
                continue
            rel = p.relative_to(root).as_posix()
            if protected(rel) or scan_ignored(rel) or stat.S_ISLNK(s.st_mode) or (hasattr(p, "is_junction") and p.is_junction()):
                continue
            if not (stat.S_ISDIR(s.st_mode) or stat.S_ISREG(s.st_mode)) or s.st_nlink > 1 and stat.S_ISREG(s.st_mode):
                continue
            try:
                self.path(root, rel)
            except DevError as exc:
                if exc.code in {"PROTECTED_PATH", "INVALID_PATH"}:
                    continue
                raise
            if exclude is not None and exclude(rel):
                continue
            yield p, rel, s
            if stat.S_ISDIR(s.st_mode) and (depth is None or level < depth):
                stack.append((entries(p), level + 1))

    def tree(self, project, args):
        root, _ = self.root(project)
        start = self.path(root, args["path"])
        if not start.is_dir():
            raise DevError("NOT_DIRECTORY", "目录不存在", 404)
        rows, more = [], False
        scan_error = None
        try:
            for i, (p, rel, st) in enumerate(self.walk(root, start, args["depth"], deadline=time.monotonic() + 8)):
                if i < args["offset"]:
                    continue
                if len(rows) >= args["limit"]:
                    more = True
                    break
                is_file = stat.S_ISREG(st.st_mode)
                rows.append({"path": rel, "name": p.name, "type": "file" if is_file else "directory", "size": st.st_size if is_file else None})
        except DevError as exc:
            if exc.code not in {"SCAN_LIMIT", "SCAN_FAILED"}:
                raise
            more, scan_error = True, exc.code
        return {"entries": rows, "truncated": more, "next_offset": args["offset"] + len(rows) if more and rows else None,
                "scan_error": scan_error,
                "exclusions": "Credentials, .git, dependencies, build caches (including .build), Agent state, links and unsupported non-UTF-8 or non-portable filenames are excluded."}

    def read(self, project, args):
        root, _ = self.root(project)
        p = self.path(root, args["path"], False)
        data = self.read_bytes(p)
        text = self.text(data)
        lines = text.splitlines(keepends=True)
        start, end = args.get("start_line", 1) - 1, args.get("start_line", 1) - 1 + args.get("max_lines", 400)
        content = "".join(lines[start:end])
        return {"path": p.relative_to(root).as_posix(), "content": content, "sha256": digest(data), "bytes": len(data),
                "total_lines": len(lines), "start_line": start + 1, "end_line": min(end, len(lines)),
                "truncated": start > 0 or end < len(lines), "next_start_line": end + 1 if end < len(lines) else None}

    def git_candidates(self, root, start, *, max_files=20000):
        """Bounded Git enumeration; content still passes every filesystem guard."""
        if not (root / ".git").exists():
            return None
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment["GIT_TERMINAL_PROMPT"] = "0"
        deadline = time.monotonic() + 5
        try:
            # Spool instead of capturing unbounded output from a large index.
            with tempfile.TemporaryFile() as spool:
                result = subprocess.run(
                    ["git", "--no-optional-locks", "--literal-pathspecs", "-c", "core.fsmonitor=false",
                     "-c", "core.hooksPath=" + str(self.journal.directory / "no-hooks"),
                     "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
                     start.relative_to(root).as_posix()],
                    cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=spool,
                    stderr=subprocess.DEVNULL, timeout=3, check=False)
                if result.returncode:
                    return None
                spool.seek(0)
                raw = spool.read(8 * 1024 * 1024 + 1)
        except (OSError, subprocess.SubprocessError):
            return None
        capped = len(raw) > 8 * 1024 * 1024
        if capped:
            raw = raw[:8 * 1024 * 1024].rsplit(b"\0", 1)[0] + b"\0"
        rows, seen = [], set()
        for name in raw.split(b"\0"):
            if time.monotonic() > deadline:
                capped = True
                break
            if not name:
                continue
            try:
                rel = name.decode("utf-8")
                if scan_ignored(rel):
                    continue
                path = self.path(root, rel, False)
                if rel in seen:
                    continue
                seen.add(rel)
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    continue
            except (UnicodeError, DevError, OSError):
                continue
            if len(rows) >= max_files:
                capped = True
                break
            rows.append((path, rel, info))
        return rows, capped

    def search(self, project, args):
        root, _ = self.root(project)
        start = self.path(root, args["path"])
        budget = time.monotonic()
        scan_scope, candidates_capped = "filesystem_walk", False
        if start.is_file():
            entries = [(start, start.relative_to(root).as_posix(), start.stat())]
            scan_scope = "explicit_file"
        elif start.is_dir():
            candidates = self.git_candidates(root, start)
            if candidates is None:
                entries = self.walk(root, start, deadline=budget + 8, files_first=True)
            else:
                entries, candidates_capped = candidates
                scan_scope = "git_index"
        else:
            raise DevError("NOT_FOUND", "搜索路径不存在或不是普通文件/目录", 404)
        needle = args["query"] if args["case_sensitive"] else args["query"].casefold()
        results, seen, read_total, matches, skipped = [], 0, 0, 0, 0
        truncated, scan_error = False, None
        iterator = iter(entries)
        while True:
            try:
                p, rel, st = next(iterator)
            except StopIteration:
                break
            except DevError as exc:
                if exc.code not in {"SCAN_LIMIT", "SCAN_FAILED"}:
                    raise
                truncated, scan_error = True, exc.code
                break
            if not stat.S_ISREG(st.st_mode) or not fnmatch.fnmatchcase(rel, args["file_glob"]):
                continue
            seen += 1
            if seen > 20000 or read_total >= 32 * MAX_FILE or time.monotonic() - budget > 8:
                truncated = True
                break
            try:
                self.path(root, rel, False)
                data = self.read_bytes(p)
                text = self.text(data)
            except (DevError, OSError):
                skipped += 1
                continue
            read_total += len(data)
            if read_total > 32 * MAX_FILE:
                truncated = True
                break
            for n, line in enumerate(text.splitlines(), 1):
                if needle not in (line if args["case_sensitive"] else line.casefold()):
                    continue
                matches += 1
                if matches <= args["offset"]:
                    continue
                if len(results) >= args["limit"]:
                    truncated = True
                    break
                results.append({"path": rel, "line": n, "text": line[:1600], "line_truncated": len(line) > 1600})
            if truncated:
                break
        truncated = truncated or candidates_capped
        return {"matches": results, "scanned_files": min(seen, 20000), "skipped_files": skipped, "truncated": truncated,
                "scan_scope": scan_scope,
                "hint": "结果被截断；请缩小 path 或 file_glob，0 条命中不代表不存在" if truncated else None,
                "next_offset": args["offset"] + len(results) if truncated and results else None,
                "scan_error": scan_error,
                "limits": "20,000 files / 100,000 directory entries / 32 MiB / 8 seconds per call; narrow the path if the scan limit is reached. Protected paths, generated build caches (including .build) and unsupported filenames are excluded."}

    def preview(self, project, args):
        root, _ = self.root(project)
        p = self.path(root, args["path"], False)
        before = self.read_bytes(p, True)
        if before is not None:
            self.text(before)
        after = self.encode_content(args["content"])
        return {"path": args["path"], "current_sha256": digest(before) if before is not None else "new", **self.diff(args["path"], before, after)}

    def mutate(self, project, path: str, expected: str, after: bytes | None, *, restore_mode: int | None = None):
        with self.mutation_lock:
            return self._mutate(project, path, expected, after, restore_mode=restore_mode)

    def _mutate(self, project, path: str, expected: str, after: bytes | None, *, restore_mode: int | None = None):
        root, _ = self.root(project, True)
        p = self.path(root, path, False)
        self.check_local_write(p)
        before = self.read_bytes(p, True)
        if before is not None:
            self.text(before)
        actual = digest(before) if before is not None else "new"
        if actual != expected:
            raise DevError("SHA_CONFLICT", f"文件已改变：当前 SHA 为 {actual}。请重新读取后再修改。", 409)
        before_mode = stat.S_IMODE(p.stat().st_mode) if before is not None else None
        if after is not None and (len(after) > MAX_FILE or b"\x00" in after):
            raise DevError("INVALID_CONTENT", "内容必须是不超过 1 MiB 的 UTF-8 文本")
        if after is not None:
            self.text(after)
        if before == after and (after is None or restore_mode is None or before_mode == restore_mode):
            return {"path": path, "sha256": actual, "unchanged": True, "backup_id": None, **self.diff(path, before, after)}
        if not p.parent.is_dir():
            raise DevError("PARENT_MISSING", "父目录不存在，请先调用 fs_mkdir")
        backup_id = uuid.uuid4().hex
        after_sha = digest(after) if after is not None else "new"
        self.journal.add_backup(backup_id, str(root), p.relative_to(root).as_posix(), before, after_sha, before_mode=before_mode)
        # Re-check immediately before replacement. This is not an OS sandbox against hostile local processes.
        self.path(root, path, False)
        again = self.read_bytes(p, True)
        if (digest(again) if again is not None else "new") != expected:
            raise DevError("SHA_CONFLICT", "文件在保存期间变化，未覆盖", 409)
        publication_atomic = True
        if after is None:
            publication_method = "delete"
            if before is not None:
                p.unlink()
        else:
            mode = restore_mode if restore_mode is not None else before_mode if before_mode is not None else 0o644
            fd, tmp = tempfile.mkstemp(prefix=".rd-", dir=p.parent)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(after)
                    f.flush()
                    os.fsync(f.fileno())
                os.chmod(tmp, mode)
                self.path(root, path, False)
                # Recheck after the temporary file has been flushed, not before:
                # a local editor may save while a slow disk is writing the temp file.
                latest = self.read_bytes(p, True)
                if (digest(latest) if latest is not None else "new") != expected:
                    raise DevError("SHA_CONFLICT", "写入临时文件期间本机文件已改变，未覆盖", 409)
                if before is None:
                    try:
                        # Prefer atomic publication; compatibility creation reports weaker visibility.
                        publication = self.publish_new_file(tmp, p)
                        publication_method = publication.method
                        publication_atomic = publication.atomic
                    except FileExistsError as exc:
                        raise DevError("SHA_CONFLICT", "本机刚创建了同名文件，未覆盖", 409) from exc
                    except DevError as exc:
                        if exc.code == "WRITE_OUTCOME_UNCONFIRMED":
                            exc.details.update(path=path, backup_id=backup_id, expected_sha256=after_sha)
                        raise
                else:
                    publication_method = "replace"
                    try:
                        os.replace(tmp, p)
                    except OSError as exc:
                        if exc.errno in STORAGE_UNAVAILABLE or exc.errno == errno.EINTR:
                            raise DevError("WRITE_OUTCOME_UNCONFIRMED",
                                           "替换文件时存储连接或 I/O 中断，最终状态尚未确认；请核查当前文件和备份，勿直接重复写入", 500,
                                           filesystem_errno=exc.errno, retryable=False,
                                           path=path, backup_id=backup_id, expected_sha256=after_sha) from exc
                        raise
            finally:
                self.cleanup_staging(tmp, path=path, backup_id=backup_id,
                                     expected_sha256=after_sha, atomic=publication_atomic,
                                     partial_content_visible=not publication_atomic, mutation_started=True)
        self.sync_directory(p.parent, path=path, backup_id=backup_id,
                            atomic=publication_atomic, partial_content_visible=not publication_atomic,
                            mutation_started=True, expected_sha256=after_sha)
        # A successful rename/fsync is not proof that a network client can read
        # the intended bytes back. Do not replay on transient ENOENT or mismatch:
        # the mutation has happened and must remain reviewable with its backup.
        try:
            self.path(root, path, False)
            verified = self.read_bytes(p, missing=after is None, verify_path=True)
            if verified != after:
                raise DevError("FILE_CHANGED", "保存后文件内容与预期不一致", 409)
        except (OSError, DevError) as exc:
            raise DevError("WRITE_OUTCOME_UNCONFIRMED",
                           "文件变更已执行，但保存后核验未确认；请核对当前文件、备份和原回执，勿直接重试", 500,
                           retryable=False, mutation_started=True, publication="unconfirmed",
                           path=path, backup_id=backup_id, expected_sha256=after_sha,
                           atomic=publication_atomic, partial_content_visible=not publication_atomic) from exc
        return {"path": p.relative_to(root).as_posix(), "sha256": after_sha, "backup_id": backup_id,
                "publication_method": publication_method, "atomic": publication_atomic,
                "atomicity_scope": "filesystem_primitive", "continuous_visibility_guaranteed": False,
                "content_verified": True,
                "partial_content_visible": not publication_atomic, **self.diff(path, before, after)}

    def edit(self, project, args):
        root, _ = self.root(project, True)
        before = self.read_bytes(self.path(root, args["path"], False))
        if digest(before) != args["expected_sha256"]:
            raise DevError("SHA_CONFLICT", "源文件已改变，请重新读取", 409)
        content = self.text(before)
        for edit in args["edits"]:
            count = content.count(edit["old_text"])
            if count == 0 or count > 1 and not edit["replace_all"]:
                raise DevError("EDIT_MATCH", f"替换目标出现 {count} 次，要求唯一匹配；未修改文件")
            old_bytes = self.encode_content(edit["old_text"])
            new_bytes = self.encode_content(edit["new_text"])
            replacements = count if edit["replace_all"] else 1
            resulting_size = len(content.encode("utf-8")) + replacements * (len(new_bytes) - len(old_bytes))
            if resulting_size > MAX_FILE:
                raise DevError("FILE_TOO_LARGE", "替换后的 UTF-8 内容超过 1 MiB，未修改文件")
            content = content.replace(edit["old_text"], edit["new_text"], -1 if edit["replace_all"] else 1)
        return self.mutate(project, args["path"], args["expected_sha256"], self.encode_content(content))

    def move(self, project, args):
        with self.mutation_lock:
            return self._move(project, args)

    def _move(self, project, args):
        root, _ = self.root(project, True)
        src = self.path(root, args["path"], False)
        dest = self.path(root, args["destination"], False)
        self.check_local_write(src)
        self.check_local_write(dest)
        if dest.exists() or dest.is_symlink():
            raise DevError("DESTINATION_EXISTS", "目标文件已存在，不会覆盖", 409)
        if not dest.parent.is_dir():
            raise DevError("PARENT_MISSING", "目标父目录不存在")
        before = self.read_bytes(src)
        self.text(before)
        if digest(before) != args["expected_sha256"]:
            raise DevError("SHA_CONFLICT", "源文件已改变", 409)
        backup_id = uuid.uuid4().hex
        mode = stat.S_IMODE(src.stat().st_mode)
        self.journal.add_backup(backup_id, str(root), src.relative_to(root).as_posix(), before, "new", before_mode=mode)
        # Stage complete content before attempting atomic publication. On storage
        # without atomic primitives, exclusive creation reports partial visibility.
        recovery = dict(path=args["path"], destination=args["destination"],
                        backup_id=backup_id, expected_sha256=args["expected_sha256"],
                        atomic=True, partial_content_visible=False, mutation_started=True)
        publication = None
        fd, tmp = tempfile.mkstemp(prefix=".rd-", dir=dest.parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(before)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            staged_stat = os.stat(tmp, follow_symlinks=False)
            self.path(root, args["path"], False)
            self.path(root, args["destination"], False)
            if digest(self.read_bytes(src)) != args["expected_sha256"]:
                raise DevError("SHA_CONFLICT", "移动期间源文件改变，未删除源文件", 409)
            try:
                publication = self.publish_new_file(tmp, dest)
            except FileExistsError as exc:
                raise DevError("DESTINATION_EXISTS", "本机刚创建了目标文件，未覆盖", 409) from exc
            except DevError as exc:
                if exc.code == "WRITE_OUTCOME_UNCONFIRMED":
                    exc.details.update(path=args["path"], destination=args["destination"], backup_id=backup_id)
                raise
            recovery.update(atomic=publication.atomic, partial_content_visible=not publication.atomic)
            published_identity = publication.identity or (staged_stat.st_dev, staged_stat.st_ino)
            # Persist the destination name before removing the source, including
            # when a move crosses two directories on the same volume.
            self.sync_directory(dest.parent, **recovery)
            def destination_unchanged():
                try:
                    destination_stat = dest.lstat()
                    if (not stat.S_ISREG(destination_stat.st_mode) or
                            published_identity !=
                            (destination_stat.st_dev, destination_stat.st_ino)):
                        return False
                    # Exclusive rename consumes tmp. Read the published inode,
                    # with no-follow and an fd identity check, on either path.
                    fd = os.open(dest, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                    with os.fdopen(fd, "rb") as published:
                        opened = os.fstat(published.fileno())
                        if (opened.st_dev, opened.st_ino) != published_identity:
                            return False
                        content = published.read(MAX_FILE + 1)
                        latest = os.fstat(published.fileno())
                        current = dest.lstat()
                        return (content == before and
                                (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) ==
                                (latest.st_size, latest.st_mtime_ns, latest.st_ctime_ns) and
                                (current.st_dev, current.st_ino) == published_identity)
                except FileNotFoundError:
                    return False
                except OSError as exc:
                    if exc.errno == errno.ELOOP:
                        return False
                    raise
            # A local editor can save either file while the copy is being staged.
            # Never remove a destination another process has replaced or edited.
            self.path(root, args["path"], False)
            if digest(self.read_bytes(src)) != args["expected_sha256"]:
                raise DevError("WRITE_OUTCOME_UNCONFIRMED",
                               "移动期间源文件改变，已保留源和已发布目标；请核查后继续", 409,
                               retryable=False, publication="unconfirmed", source_retained=True, **recovery)
            if not destination_unchanged():
                raise DevError("WRITE_OUTCOME_UNCONFIRMED",
                               "移动期间目标文件改变，已保留源文件；请核查后继续", 409,
                               retryable=False, publication="unconfirmed", source_retained=True, **recovery)
            if not publication.atomic:
                # There is no portable compare-inode-and-unlink primitive.
                # Never delete a source that another process may have replaced
                # during a slow SMB copy. This is a copy awaiting review, not a
                # completed move; do not replay it with a fresh operation key.
                raise DevError("WRITE_OUTCOME_UNCONFIRMED",
                               "兼容模式已复制并校验目标，但无法安全条件删除源；源文件已保留，移动尚未完成，请核查后处理",
                               409, retryable=False, publication="unconfirmed", source_retained=True, **recovery)
            try:
                # Legacy atomic-publication moves retain best-effort source
                # removal. The checks above are not an OS transaction against
                # external editors; see the documented concurrency boundary.
                src.unlink()
            except OSError as exc:
                # Never check-then-unlink the destination as rollback: a
                # concurrent replacement could be removed between those calls.
                raise DevError("WRITE_OUTCOME_UNCONFIRMED",
                               "移动目标已发布，但源文件删除未完成确认；已保留目标和备份，请核查后继续",
                               500, filesystem_errno=exc.errno, retryable=False,
                               publication="unconfirmed", **recovery) from exc
            self.sync_directory(src.parent, **recovery)
        except (DevError, OSError) as exc:
            if publication is None:
                raise
            if isinstance(exc, DevError) and exc.code in {
                    "WRITE_OUTCOME_UNCONFIRMED", "DURABILITY_UNCONFIRMED",
                    "STAGING_CLEANUP_UNCONFIRMED"}:
                for key, value in recovery.items():
                    exc.details.setdefault(key, value)
                raise
            raise DevError("WRITE_OUTCOME_UNCONFIRMED",
                           "移动目标已发布，但后续校验未完成；已保留目标，请核查原回执后继续",
                           500, retryable=False, publication="unconfirmed", **recovery) from exc
        finally:
            self.cleanup_staging(tmp, **recovery)
        self.sync_directory(dest.parent, **recovery)
        return {"path": args["path"], "destination": args["destination"], "sha256": digest(before), "backup_id": backup_id,
                "publication_method": publication.method, "atomic": publication.atomic,
                "partial_content_visible": not publication.atomic,
                "note": "Restoring the source backup does not remove the moved destination."}

    def checkpoint(self, project, args):
        root, _ = self.root(project)
        max_mib = self.config.get("checkpoint_max_mib", 100)
        if type(max_mib) is not int or not 1 <= max_mib <= 4096:
            raise DevError("CHECKPOINT_CONFIG", "checkpoint_max_mib 必须是 1–4096 的整数")
        max_bytes = max_mib * 1024 * 1024
        id = uuid.uuid4().hex
        dest = self.journal.checkpoints / f"{id}.zip"
        manifest, skipped, total, files = [], [], 0, 0
        temp = dest.with_suffix(".part")
        started = time.monotonic()
        def exclude(rel):
            name = PurePosixPath(rel).name.lower()
            if name in CHECKPOINT_ARTIFACTS or name.startswith(".next-") or name.endswith(".tsbuildinfo"):
                skipped.append({"path": rel, "reason": "GENERATED_ARTIFACT"})
                return True
            return False
        try:
            with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as z:
                for p, rel, st in self.walk(root, root, exclude=exclude, deadline=started + 60):
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    files += 1
                    if files > 20000 or time.monotonic() - started > 60:
                        raise DevError("CHECKPOINT_LIMIT", "项目超过检查点数量/时间上限，未生成不完整检查点")
                    try:
                        self.path(root, rel, False)
                        data = self.read_bytes(p)
                    except DevError as exc:
                        skipped.append({"path": rel, "reason": exc.code})
                        continue
                    total += len(data)
                    if total > max_bytes:
                        raise DevError("CHECKPOINT_LIMIT", f"源码检查点超过 {max_mib} MiB 上限；可在本机配置 checkpoint_max_mib 调整")
                    member = zipfile.ZipInfo(f"files/{rel}")
                    member.create_system = 3
                    member.compress_type = zipfile.ZIP_DEFLATED
                    member.external_attr = (st.st_mode & 0xFFFF) << 16
                    z.writestr(member, data)
                    manifest.append({"path": rel, "sha256": digest(data), "bytes": len(data), "mode": stat.S_IMODE(st.st_mode)})
                z.writestr("manifest.json", json.dumps({"label": args["label"], "root": str(root), "files": manifest, "skipped": skipped, "at": time.time()}, ensure_ascii=False, indent=2))
            os.chmod(temp, 0o600)
            with temp.open("rb") as archive:
                os.fsync(archive.fileno())
            os.replace(temp, dest)
            self.sync_directory(dest.parent)
        except DevError as exc:
            if exc.code == "SCAN_LIMIT":
                raise DevError("CHECKPOINT_LIMIT", "项目超过检查点目录数量/时间上限，未生成不完整检查点") from exc
            raise
        finally:
            if temp.exists():
                temp.unlink()
        return {"checkpoint_id": id, "local_archive": str(dest), "files": len(manifest), "bytes": total, "skipped": skipped[:100],
                "skipped_count": len(skipped), "max_bytes": max_bytes,
                "exclusions": "Protected paths, dependencies, links, unsupported filenames, Next.js builds, caches and test reports are excluded. Not an atomic filesystem snapshot. Restore manually on the home machine."}

    def call(self, tool: str, project: dict, args: dict):
        if tool == "apply_patch":
            from agent.batch_patch import apply_patch
            return apply_patch(self, project, args)
        if tool in {"open_workspace", "show_changes"}:
            from agent.coding_reviews import open_workspace, show_changes
            return (open_workspace if tool == "open_workspace" else show_changes)(self, project, args)
        if tool in {"skills_list", "skills_read"}:
            from agent.skills import Skills
            manager = Skills(self)
            return manager.list(project, args) if tool == "skills_list" else manager.read(project, args)
        if tool == "code_symbols":
            from agent.symbols import code_symbols
            return code_symbols(self, project, args)
        if tool == "project_context":
            from agent.context import project_context
            return project_context(self, project, args)
        if tool == "fs_tree": return self.tree(project, args)
        if tool == "fs_read": return self.read(project, args)
        if tool == "fs_read_many":
            items, used = [], 0
            for index, path in enumerate(args["paths"]):
                try:
                    item = {"ok": True, **self.read(project, {"path": path, "max_lines": args["max_lines"]})}
                except (DevError, OSError) as exc:
                    item = {"ok": False, "path": path, "error": str(exc)}
                size = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
                if size > 2 * MAX_FILE - 4096:
                    item = {"ok": False, "path": path, "error": "BATCH_ITEM_TOO_LARGE",
                            "message": "该文件的 JSON 编码超出批量预算；请使用 fs_read 单独读取。"}
                    size = len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
                if items and used + size > 2 * MAX_FILE - 4096:
                    return {"files": items, "truncated": True, "remaining_paths": args["paths"][index:],
                            "note": "批量返回达到 2 MiB 预算，请继续读取 remaining_paths；未读取的文件不算已扫描。"}
                items.append(item)
                used += size
            return {"files": items, "truncated": False, "remaining_paths": []}
        if tool == "fs_search": return self.search(project, args)
        if tool == "fs_preview": return self.preview(project, args)
        if tool == "fs_write": return self.mutate(project, args["path"], args["expected_sha256"], self.encode_content(args["content"]))
        if tool == "fs_edit": return self.edit(project, args)
        if tool == "fs_delete": return self.mutate(project, args["path"], args["expected_sha256"], None)
        if tool == "fs_move": return self.move(project, args)
        if tool == "fs_mkdir":
            with self.mutation_lock:
                root, _ = self.root(project, True)
                p = self.path(root, args["path"], False)
                self.check_local_write(p)
                missing, current = [], p
                while not current.exists():
                    self.check_local_write(current)
                    missing.append(current)
                    current = current.parent
                for directory in reversed(missing):
                    self.path(root, directory.relative_to(root).as_posix(), False)
                    directory.mkdir(exist_ok=True)
                    self.sync_directory(directory.parent)
                if not p.is_dir():
                    raise DevError("NOT_DIRECTORY", "路径已存在且不是目录", 409)
                return {"path": p.relative_to(root).as_posix(), "created": bool(missing)}
        if tool == "history_list":
            root, _ = self.root(project)
            path = relative_path(args["path"]) if args["path"] else ""
            return {"backups": [row for row in self.journal.history(str(root), path, args["limit"])
                                if not protected(row["path"])]}
        if tool == "history_restore":
            root, _ = self.root(project, True)
            row, data = self.journal.backup(str(root), args["backup_id"])
            return self.mutate(project, row["path"], args["expected_sha256"], data, restore_mode=row.get("before_mode"))
        if tool == "project_checkpoint": return self.checkpoint(project, args)
        raise DevError("UNKNOWN_TOOL", "未知文件操作")
