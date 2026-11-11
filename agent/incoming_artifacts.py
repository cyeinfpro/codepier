"""Native host file import: pinned public TLS connections and anchored publication.

No browser cookies, bearer headers, arbitrary URLs, overwrites or extraction.
The native file object is transport input, never part of a returned receipt.
"""

from __future__ import annotations
import contextlib, hashlib, os, stat, uuid
from pathlib import Path
from agent.filesystem import relative_path
from shared.util import DevError

# Compatibility exports; configuration and runtime share the same registry.
from shared.file_sources import (
    DEFAULT_FILE_HOSTS,
    DEFAULT_MAX_IMPORT_BYTES,
    file_source_hosts,
    file_source_policy,
    normalize_file_host,
    source_metadata,
    safe_import_error_detail,
    normalize_file_source_providers,
    source_host_allowed,
)

DEFAULT_HOSTS = DEFAULT_FILE_HOSTS
MAX_BYTES = DEFAULT_MAX_IMPORT_BYTES


# Compatibility exports; shared transport owns network validation.
from shared.file_download import (
    PublicTLSConnection,
    download_chunks,
    remaining_timeout,
    resolve_addresses,
    validate_url,
)

__all__ = [
    "AnchoredDestination",
    "inspect_file_source",
    "import_artifact",
    "download_chunks",
    "validate_url",
    "PublicTLSConnection",
    "remaining_timeout",
    "resolve_addresses",
    "DEFAULT_FILE_HOSTS",
    "DEFAULT_MAX_IMPORT_BYTES",
    "file_source_hosts",
    "file_source_policy",
    "normalize_file_host",
    "source_metadata",
    "safe_import_error_detail",
    "normalize_file_source_providers",
    "source_host_allowed",
]


class AnchoredDestination:
    def __init__(self, engine, root, relative):
        self.engine, self.root = engine, root
        self.parts = relative.split("/")
        self.fds = []
        self.handles = []
        self.parent = root
        self.fd = None
        self.temporary = ".rd-import-" + uuid.uuid4().hex + ".part"
        self.published = False
        self.publication_method = None
        self.publication_atomic = True

    def __enter__(self):
        try:
            if os.name == "nt":
                self._windows_open()
            else:
                flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                fd = os.open(self.root, flags)
                self.fds.append(fd)
                for part in self.parts[:-1]:
                    self.parent /= part
                    self.engine.check_local_write(self.parent)
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                    fd = os.open(part, flags, dir_fd=fd)
                    self.fds.append(fd)
                self.fd = os.open(
                    self.temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=self.fds[-1],
                )
            return self
        except BaseException as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise

    def _windows_open(self):
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel = kernel

        def pin(path):
            # FILE_SHARE_DELETE deliberately absent: all destination ancestors stay pinned.
            handle = kernel.CreateFileW(str(path), 0, 3, None, 3, 0x02000000 | 0x00200000, None)
            if handle == wintypes.HANDLE(-1).value:
                raise OSError(ctypes.get_last_error(), "Cannot pin destination directory")
            self.handles.append(handle)
            if path.is_symlink() or path.is_junction():
                raise DevError("SYMLINK_BLOCKED", "目标父目录是链接或联接", 403)

        current = Path(self.root.anchor)
        pin(current)
        for part in self.root.parts[1:]:
            current /= part
            pin(current)
        for part in self.parts[:-1]:
            self.parent /= part
            self.engine.check_local_write(self.parent)
            self.parent.mkdir(mode=0o700, exist_ok=True)
            pin(self.parent)
        self.fd = os.open(
            self.parent / self.temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_BINARY, 0o600
        )

    def write(self, data):
        view = memoryview(data)
        while view:
            written = os.write(self.fd, view)
            if written <= 0:
                raise OSError("Short write")
            view = view[written:]

    def publish(self):
        os.fsync(self.fd)
        original = os.fstat(self.fd)
        if not stat.S_ISREG(original.st_mode) or original.st_nlink != 1:
            raise DevError("ARTIFACT_DESTINATION_CHANGED", "临时文件身份异常")
        target = self.engine.path(self.root, "/".join(self.parts), False)
        self.engine.check_local_write(target)
        # The on-disk name must still refer to the pinned directory/file.
        if os.name == "nt":
            if self.parent.resolve() != self.parent:
                raise DevError("ARTIFACT_DESTINATION_CHANGED", "目标路径已改变")
            current = os.stat(self.parent / self.temporary, follow_symlinks=False)
        else:
            parent_stat = self.parent.stat()
            pinned = os.fstat(self.fds[-1])
            if (parent_stat.st_dev, parent_stat.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise DevError("ARTIFACT_DESTINATION_CHANGED", "下载期间目标目录发生变化")
            current = os.stat(self.temporary, dir_fd=self.fds[-1], follow_symlinks=False)
        if (current.st_dev, current.st_ino) != (original.st_dev, original.st_ino):
            raise DevError("ARTIFACT_DESTINATION_CHANGED", "临时文件被替换")
        try:
            if os.name == "nt":
                os.close(self.fd)
                self.fd = None
                # os.rename on Windows refuses an existing target.
                os.rename(self.parent / self.temporary, target)
                self.publication_method = "rename_no_replace"
            else:
                publication = self.engine.publish_new_file(
                    self.temporary,
                    self.parts[-1],
                    src_dir_fd=self.fds[-1],
                    dst_dir_fd=self.fds[-1],
                    follow_symlinks=False,
                )
                self.publication_method = publication.method
                self.publication_atomic = publication.atomic
                if not publication.atomic:
                    try:
                        self.engine.path(self.root, "/".join(self.parts), False)
                    except DevError as exc:
                        raise DevError(
                            "WRITE_OUTCOME_UNCONFIRMED",
                            "兼容新建期间目标路径改变，请核查原回执；勿直接重试",
                            500,
                            retryable=False,
                            atomic=False,
                            partial_content_visible=True,
                            mutation_started=True,
                            publication="unconfirmed",
                        ) from exc
                    current_parent, pinned_parent = self.parent.stat(), os.fstat(self.fds[-1])
                    if (current_parent.st_dev, current_parent.st_ino) != (
                        pinned_parent.st_dev,
                        pinned_parent.st_ino,
                    ):
                        raise DevError(
                            "WRITE_OUTCOME_UNCONFIRMED",
                            "兼容新建期间目标目录改变，请核查原回执；勿直接重试",
                            500,
                            retryable=False,
                            atomic=False,
                            partial_content_visible=True,
                            mutation_started=True,
                            publication="unconfirmed",
                        )
                if publication.source_retained:
                    os.unlink(self.temporary, dir_fd=self.fds[-1])
            self.published = True
        except FileExistsError as exc:
            raise DevError("ARTIFACT_DESTINATION_EXISTS", "目标文件已存在，未覆盖", 409) from exc
        except OSError as exc:
            if self.publication_method == "exclusive_create":
                raise DevError(
                    "WRITE_OUTCOME_UNCONFIRMED",
                    "兼容新建后的校验或清理未完成确认，请核查目标和原回执；勿直接重试",
                    500,
                    filesystem_errno=exc.errno,
                    retryable=False,
                    atomic=False,
                    partial_content_visible=True,
                    mutation_started=True,
                    publication="unconfirmed",
                ) from exc
            raise
        self.engine.sync_directory(
            self.parent,
            atomic=self.publication_atomic,
            partial_content_visible=not self.publication_atomic,
            mutation_started=True,
        )

    def __exit__(self, exc_type, exc_value, traceback):
        # Cleanup failures must never strand pinned descriptors/Windows handles.
        # Preserve the primary download error, but do report standalone cleanup
        # failures instead of silently claiming a completely cleaned destination.
        failures = []
        if self.fd is not None:
            fd, self.fd = self.fd, None
            try:
                os.close(fd)
            except OSError as exc:
                failures.append(exc)
        if not self.published:
            try:
                if self.fds:
                    os.unlink(self.temporary, dir_fd=self.fds[-1])
                elif os.name == "nt":
                    (self.parent / self.temporary).unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                failures.append(exc)
        fds, self.fds = self.fds, []
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError as exc:
                failures.append(exc)
        handles, self.handles = self.handles, []
        for handle in reversed(handles):
            try:
                if not self.kernel.CloseHandle(handle):
                    raise OSError("Cannot close pinned destination handle")
            except OSError as exc:
                failures.append(exc)
        if failures:
            if exc_value is not None:
                exc_value.add_note("Destination cleanup also failed: " + type(failures[0]).__name__)
            else:
                raise failures[0]


def inspect_file_source(engine, project, args):
    """Preview policy only: no DNS, network, directory creation or publication.

    Keep this a distinct protocol operation. Older Agents reject an unknown
    operation instead of mistaking an unsupported preview flag for an import.
    """
    engine.root(project)
    config = engine.config.get("integrations", {})
    policy = file_source_policy(config)
    file = args["file"]
    size = file.get("size")
    result = {
        **policy,
        **source_metadata(file["download_url"]),
        "checked": True,
        "source_allowed": False,
        "declared_size": size,
        "size_allowed": None if size is None else size <= policy["max_bytes"],
        "request_sent": False,
        "created": False,
        "host_roundtrip": "not_run",
        "approval_required": False,
        "note": "仅检查当前来源策略和声明大小，未访问来源、验证 DNS/TLS/跳转或文件内容；真正导入时仍会重新校验。",
    }
    try:
        validate_url(
            file["download_url"], policy["allowed_hosts"], providers=policy["file_source_providers"]
        )
    except DevError as exc:
        result.update(safe_import_error_detail(exc.details))
        result["message"] = exc.message
        if exc.details.get("reason") == "host_not_allowed":
            result["approval_required"] = True
            result["approval_target"] = {
                "config_key": "integrations.extra_file_hosts",
                "hosts": [result["source_host"]],
                "scope": "execution_node",
                "applies_to": "all_projects_on_node",
            }
            result["trust"] = (
                "来源主机是待核验输入，原生 file_id 和域名相似性都不构成可信证明；须本人独立核验并明确批准，不能自动更改策略。"
            )
    else:
        result["source_allowed"] = True
    return result


def import_artifact(engine, project, args, *, stream=None):
    root, _ = engine.root(project, True)
    relative = relative_path(args["path"], False)
    destination = engine.path(root, relative, False)
    engine.check_local_write(destination)
    if destination.exists():
        raise DevError("ARTIFACT_DESTINATION_EXISTS", "目标文件已存在；请选择未使用的路径", 409)
    config = engine.config.get("integrations", {})
    limit = config.get("max_import_bytes", MAX_BYTES)
    file = args["file"]
    expected_size = file.get("size")
    if expected_size is not None and expected_size > limit:
        raise DevError("ARTIFACT_TOO_LARGE", "原生文件大小超过本机上限")
    hosts = file_source_hosts(config)
    providers = normalize_file_source_providers(config.get("file_source_providers", []))
    validate_url(file["download_url"], hosts, providers=providers)
    total = 0
    sha = hashlib.sha256()
    # Pin/create the staging destination and publish under the mutation lock,
    # but never hold the global lock across DNS/network/download waits.
    with contextlib.ExitStack() as cleanup:
        with engine.mutation_lock:
            target = cleanup.enter_context(AnchoredDestination(engine, root, relative))
        for block in (
            stream
            if stream is not None
            else download_chunks(file, hosts, limit, providers=providers)
        ):
            if not isinstance(block, bytes):
                raise DevError("ARTIFACT_STREAM", "下载流类型错误")
            total += len(block)
            if total > limit:
                raise DevError("ARTIFACT_TOO_LARGE", "下载超过本机文件大小上限")
            target.write(block)
            sha.update(block)
        if expected_size is not None and total != expected_size:
            raise DevError("ARTIFACT_SIZE", "原生文件大小与下载结果不一致")
        actual = sha.hexdigest()
        if args.get("expected_sha256") and args["expected_sha256"] != actual:
            raise DevError("ARTIFACT_INTEGRITY", "SHA-256 不匹配，未发布文件")
        with engine.mutation_lock:
            engine.root(project, True)
            try:
                target.publish()
            except DevError as exc:
                if exc.code in {"WRITE_OUTCOME_UNCONFIRMED", "DURABILITY_UNCONFIRMED"}:
                    exc.details.update(path=relative, expected_sha256=actual)
                raise
    return {
        "path": relative,
        "bytes": total,
        "sha256": actual,
        "created": True,
        "overwritten": False,
        "extracted": False,
        "executed": False,
        "name": destination.name,
        "publication_method": target.publication_method,
        "atomic": target.publication_atomic,
        "partial_content_visible": not target.publication_atomic,
    }
