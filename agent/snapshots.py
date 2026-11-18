"""Fail-closed copy-on-write execution trees (not a security sandbox).

Git metadata and prior CodePier output are deliberately excluded. Commands that
require Git metadata must use a registered worktree instead of snapshot mode.
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import time

from agent.filesystem import snapshot_ignored
from agent.run_directories import operation_name, safe_directory
from shared.util import DevError


@dataclass(frozen=True)
class Snapshot:
    root: Path
    cwd: Path


def _relative(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise DevError("SNAPSHOT_UNSAFE", "Snapshot paths must be relative and contained")
    return path


def _clone_directory(source: Path, destination: Path) -> bool:
    if sys.platform != "darwin":
        return False
    import errno

    clone = getattr(ctypes.CDLL(None, use_errno=True), "clonefile", None)
    if clone is None:
        return False
    clone.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
    clone.restype = ctypes.c_int
    if clone(os.fsencode(source), os.fsencode(destination), 1) == 0:  # CLONE_NOFOLLOW
        return True
    if not destination.exists() and ctypes.get_errno() in (
        errno.ENOTSUP,
        errno.EINVAL,
        errno.EISDIR,
    ):
        return False
    raise DevError("SNAPSHOT_UNSUPPORTED", "Cannot clone snapshot directory on this filesystem")


def _clone_file(source: str, destination: str) -> str:
    """Clone data blocks; never fall back to a full copy."""
    if sys.platform == "darwin":
        library = ctypes.CDLL(None, use_errno=True)
        try:
            clonefile = library.clonefile
        except AttributeError as exc:
            raise DevError("SNAPSHOT_UNSUPPORTED", "clonefile is not available") from exc
        clonefile.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
        clonefile.restype = ctypes.c_int
        if clonefile(os.fsencode(source), os.fsencode(destination), 0):
            raise DevError(
                "SNAPSHOT_UNSUPPORTED", "APFS clonefile is unavailable for this filesystem"
            )
    elif sys.platform.startswith("linux"):
        import fcntl

        try:
            with open(source, "rb") as src, open(destination, "xb") as dst:
                # Linux FICLONE is _IOW(0x94, 9, int).
                fcntl.ioctl(dst.fileno(), getattr(fcntl, "FICLONE", 0x40049409), src.fileno())
            shutil.copystat(source, destination, follow_symlinks=False)
        except OSError as exc:
            raise DevError(
                "SNAPSHOT_UNSUPPORTED", "Filesystem does not support required reflink cloning"
            ) from exc
    else:
        raise DevError("SNAPSHOT_UNSUPPORTED", "Snapshot execution requires APFS or Linux reflink")
    return str(destination)


def _snapshot_exclusions(root, parent, names):
    return {
        name for name in names if snapshot_ignored((parent / name).relative_to(root).as_posix())
    }


def _validate_link(path, root, artifacts):
    target = os.readlink(path)
    if artifacts or os.path.isabs(target):
        raise DevError("SNAPSHOT_UNSAFE", "Snapshot contains an unsafe symbolic link")
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (ValueError, OSError, RuntimeError) as exc:
        raise DevError("SNAPSHOT_UNSAFE", "Snapshot symbolic link escapes its source") from exc
    relative = resolved.relative_to(root)
    if snapshot_ignored(relative.as_posix()):
        raise DevError("SNAPSHOT_UNSAFE", "Snapshot symbolic link targets excluded metadata")


def _validate_tree(root: Path, *, artifacts: bool = False, original: Path | None = None) -> set:
    partial_clone = set()
    for directory, directories, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        if not artifacts:
            excluded = _snapshot_exclusions(root, parent, directories + files)
            if excluded and original is not None:
                raise DevError("SNAPSHOT_UNSAFE", "Excluded path appeared during snapshot clone")
            if excluded and parent != root:
                partial_clone.add(parent.relative_to(root).parts[0])
            directories[:] = [name for name in directories if name not in excluded]
            files = [name for name in files if name not in excluded]
        for name in directories + files:
            path = parent / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                _validate_link(path, root, artifacts)
            elif not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise DevError("SNAPSHOT_UNSAFE", "Snapshot contains a special file")
            elif stat.S_ISREG(mode) and not artifacts:
                _validate_editable(path, root, original)
    return partial_clone


def _validate_editable(path: Path, root: Path, original: Path | None = None) -> None:
    if path.stat().st_mode & 0o111:
        with path.open("rb") as stream:
            shebang = stream.readline(4096)
        source = original or root
        if shebang.startswith(b"#!") and os.fsencode(source) in shebang:
            prefix = b"#!" + os.fsencode(source) + b"/"
            command = shebang.rstrip(b"\r\n").split(maxsplit=1)[0]
            relative = os.fsdecode(command[len(prefix) :]) if command.startswith(prefix) else ""
            import re

            if (
                not re.fullmatch(r"\.venv/bin/python(?:[0-9]+(?:\.[0-9]+)?)?", relative)
                or os.fsencode(source) in shebang[len(command) :]
                or path.relative_to(root).parts[:2] != (".venv", "bin")
                or not (root / relative).is_file()
                or not (root / relative).resolve().is_relative_to(root)
            ):
                raise DevError(
                    "SNAPSHOT_UNSAFE", "Script interpreter points to the original project"
                )
            if original is not None:
                rewritten = b"#!" + os.fsencode(root) + b"/" + shebang[len(prefix) :]
                if len(rewritten) > 255 or any(
                    c in os.fsencode(root) for c in (b" ", b"\t", b"\n")
                ):
                    raise DevError(
                        "SNAPSHOT_UNSAFE", "Relocated interpreter exceeds kernel shebang limits"
                    )
                import tempfile

                with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as out:
                    temporary = Path(out.name)
                    with path.open("rb") as source_file:
                        if source_file.readline(4096) != shebang:
                            raise DevError(
                                "SNAPSHOT_UNSAFE", "Snapshot script changed during relocation"
                            )
                        out.write(rewritten)
                        shutil.copyfileobj(source_file, out)
                    os.fchmod(out.fileno(), stat.S_IMODE(path.stat().st_mode))
                os.replace(temporary, path)
    if path.suffix == ".egg-link" or path.name.startswith("__editable__"):
        raise DevError("SNAPSHOT_UNSAFE", "Editable Python installs are not supported in snapshots")
    if path.name == "direct_url.json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise DevError(
                "SNAPSHOT_UNSAFE", "Cannot validate Python installation metadata"
            ) from exc
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("dir_info", {}), dict)
            or data.get("dir_info", {}).get("editable")
        ):
            raise DevError(
                "SNAPSHOT_UNSAFE", "Editable Python installs are not supported in snapshots"
            )
    if path.suffix == ".pth":
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeError as exc:
            raise DevError("SNAPSHOT_UNSAFE", "Cannot validate Python path configuration") from exc
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Executable pth content can import arbitrary external source.
            if line.startswith(("import ", "import\t")) or Path(line).is_absolute():
                raise DevError("SNAPSHOT_UNSAFE", "Python path configuration escapes the snapshot")
            try:
                (path.parent / line).resolve().relative_to(root)
            except ValueError as exc:
                raise DevError(
                    "SNAPSHOT_UNSAFE", "Python path configuration escapes the snapshot"
                ) from exc


class SnapshotManager:
    def __init__(self, state_dir: Path):
        self.parent = safe_directory(Path(state_dir) / "snapshots", create=True)

    def create(self, root: Path, operation_id: str, cwd: str = ".") -> Snapshot:
        root = safe_directory(root)
        name = operation_name(operation_id)
        relative = _relative(cwd)
        source_cwd = root / relative
        safe_directory(source_cwd)
        source_cwd.relative_to(root)
        if snapshot_ignored(relative.as_posix()):
            raise DevError("SNAPSHOT_UNSAFE", "Cannot execute inside excluded metadata")
        if self.parent == root or self.parent.is_relative_to(root):
            raise DevError("SNAPSHOT_UNSAFE", "Snapshot storage must be outside the project")
        partial_clone = _validate_tree(root)
        container = self.parent / name
        try:
            container.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise DevError("SNAPSHOT_EXISTS", "Snapshot already exists", 409) from exc
        destination = container / "tree"
        try:
            destination.mkdir(mode=0o700)
            # Clone top-level subtrees in one APFS syscall each. Excluded
            # roots are never copied, even transiently, into a command workspace.
            for source in root.iterdir():
                if snapshot_ignored(source.name):
                    continue
                target = destination / source.name
                mode = source.lstat().st_mode
                if stat.S_ISLNK(mode):
                    target.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
                elif stat.S_ISDIR(mode):
                    if source.name in partial_clone or not _clone_directory(source, target):
                        shutil.copytree(
                            source,
                            target,
                            symlinks=True,
                            copy_function=_clone_file,
                            ignore=lambda directory, names: _snapshot_exclusions(
                                root, Path(directory), names
                            ),
                        )
                elif stat.S_ISREG(mode):
                    _clone_file(str(source), str(target))
                else:
                    raise DevError("SNAPSHOT_UNSAFE", "Snapshot contains a special file")
            shutil.copystat(root, destination, follow_symlinks=False)
            _validate_tree(destination, original=root)
            # Empty projects still require a working clone facility.
            if not any(destination.iterdir()):
                probe = container / "probe"
                probe.write_bytes(b"snapshot")
                _clone_file(str(probe), str(container / "probe-clone"))
                probe.unlink()
                (container / "probe-clone").unlink()
            return Snapshot(destination, destination / relative)
        except BaseException:
            shutil.rmtree(container)
            raise

    def extract_artifacts(
        self, snapshot: Snapshot, relative_paths: list[str], run_dir: Path
    ) -> list[str]:
        self._owned(snapshot)
        run_dir = safe_directory(run_dir)
        extracted = []
        for value in relative_paths:
            relative = _relative(value)
            if relative == Path(".") or relative.parts[0] in {
                ".git",
                ".codepier",
                ".codepier-run.json",
            }:
                raise DevError("SNAPSHOT_UNSAFE", "Invalid snapshot artifact path")
            source = snapshot.root / relative
            # Check every component: resolve containment alone permits internal symlinks.
            for component in [source, *source.parents]:
                if component == snapshot.root:
                    break
                if component.is_symlink():
                    raise DevError("SNAPSHOT_UNSAFE", "Artifact symlinks are not supported")
            if not source.exists():
                raise DevError(
                    "SNAPSHOT_ARTIFACT_MISSING", "Declared snapshot artifact was not produced"
                )
            destination = run_dir / relative
            safe_directory(destination.parent, create=True)
            if destination.exists() or destination.is_symlink():
                raise DevError("SNAPSHOT_ARTIFACT_EXISTS", "Artifact destination already exists")
            if source.is_dir():
                _validate_tree(source, artifacts=True)
                shutil.copytree(source, destination, symlinks=False)
            elif source.is_file():
                # Exclusive creation prevents overwriting an existing artifact.
                with source.open("rb") as reader, destination.open("xb") as writer:
                    shutil.copyfileobj(reader, writer)
            else:
                raise DevError("SNAPSHOT_UNSAFE", "Artifact must be a regular file or directory")
            extracted.append(relative.as_posix())
        return extracted

    def _owned(self, snapshot: Snapshot) -> Path:
        safe_directory(self.parent)
        container = snapshot.root.parent
        if container.parent != self.parent or snapshot.root.name != "tree":
            raise DevError("SNAPSHOT_UNSAFE", "Snapshot is not owned by this manager")
        safe_directory(snapshot.root)
        return container

    def finish(self, snapshot: Snapshot, *, success: bool) -> None:
        container = self._owned(snapshot)
        if success:
            shutil.rmtree(container)
        else:
            marker = container / "failed.json"
            with marker.open("x", encoding="utf-8") as stream:
                json.dump({"finished": time.time()}, stream)

    def cleanup(self, ttl_seconds: float = 3600, *, now: float | None = None) -> list[str]:
        if ttl_seconds < 0:
            raise ValueError("TTL must be nonnegative")
        safe_directory(self.parent)
        cutoff = (time.time() if now is None else now) - ttl_seconds
        removed = []
        for container in self.parent.iterdir():
            if container.is_symlink() or not container.is_dir():
                continue
            marker = container / "failed.json"
            if marker.is_symlink():
                continue
            try:
                finished = json.loads(marker.read_text(encoding="utf-8"))["finished"]
                if not isinstance(finished, (int, float)) or finished > cutoff:
                    continue
            except (OSError, ValueError, KeyError, TypeError):
                continue
            try:
                shutil.rmtree(container)
            except FileNotFoundError:
                # Another cleanup may have removed this expired entry. Only
                # suppress proven absence, not child I/O failures or replacement
                # symlinks (lstat deliberately does not follow them).
                try:
                    container.lstat()
                except FileNotFoundError:
                    continue
                raise
            removed.append(container.name)
        return removed
