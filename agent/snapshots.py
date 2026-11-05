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
import subprocess
import sys
import time

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
            raise DevError("SNAPSHOT_UNSUPPORTED", "APFS clonefile is unavailable for this filesystem")
    elif sys.platform.startswith("linux"):
        try:
            result = subprocess.run(
                ["cp", "--reflink=always", "--preserve=mode,timestamps", "--", str(source), str(destination)],
                capture_output=True, timeout=60, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DevError("SNAPSHOT_UNSUPPORTED", "Required reflink clone command is unavailable") from exc
        if result.returncode:
            raise DevError("SNAPSHOT_UNSUPPORTED", "Filesystem does not support required reflink cloning")
    else:
        raise DevError("SNAPSHOT_UNSUPPORTED", "Snapshot execution requires APFS or Linux reflink")
    return str(destination)


def _validate_tree(root: Path, *, artifacts: bool = False) -> None:
    for directory, directories, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        if parent == root and not artifacts:
            directories[:] = [name for name in directories if name not in {".git", ".codepier"}]
            files = [name for name in files if name not in {".git", ".codepier"}]
        for name in directories + files:
            path = parent / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                target = os.readlink(path)
                if artifacts or os.path.isabs(target):
                    raise DevError("SNAPSHOT_UNSAFE", "Snapshot contains an unsafe symbolic link")
                try:
                    resolved = path.resolve(strict=True)
                    resolved.relative_to(root)
                except (ValueError, OSError, RuntimeError) as exc:
                    raise DevError("SNAPSHOT_UNSAFE", "Snapshot symbolic link escapes its source") from exc
                relative = resolved.relative_to(root)
                if relative.parts and relative.parts[0] in {".git", ".codepier"}:
                    raise DevError("SNAPSHOT_UNSAFE", "Snapshot symbolic link targets excluded metadata")
            elif not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise DevError("SNAPSHOT_UNSAFE", "Snapshot contains a special file")
            elif stat.S_ISREG(mode) and not artifacts:
                _validate_editable(path, root)


def _validate_editable(path: Path, root: Path) -> None:
    if path.stat().st_mode & 0o111:
        with path.open("rb") as stream:
            shebang = stream.read(4096).split(b"\n", 1)[0]
        if shebang.startswith(b"#!") and os.fsencode(root) in shebang:
            raise DevError("SNAPSHOT_UNSAFE", "Script interpreter points to the original project")
    if path.suffix == ".egg-link" or path.name.startswith("__editable__"):
        raise DevError("SNAPSHOT_UNSAFE", "Editable Python installs are not supported in snapshots")
    if path.name == "direct_url.json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise DevError("SNAPSHOT_UNSAFE", "Cannot validate Python installation metadata") from exc
        if (not isinstance(data, dict) or not isinstance(data.get("dir_info", {}), dict)
                or data.get("dir_info", {}).get("editable")):
            raise DevError("SNAPSHOT_UNSAFE", "Editable Python installs are not supported in snapshots")
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
                raise DevError("SNAPSHOT_UNSAFE", "Python path configuration escapes the snapshot") from exc


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
        if relative.parts and relative.parts[0] in {".git", ".codepier"}:
            raise DevError("SNAPSHOT_UNSAFE", "Cannot execute inside excluded metadata")
        if self.parent == root or self.parent.is_relative_to(root):
            raise DevError("SNAPSHOT_UNSAFE", "Snapshot storage must be outside the project")
        _validate_tree(root)
        container = self.parent / name
        try:
            container.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise DevError("SNAPSHOT_EXISTS", "Snapshot already exists", 409) from exc
        destination = container / "tree"
        try:
            shutil.copytree(
                root, destination, symlinks=True, copy_function=_clone_file,
                ignore=lambda directory, names: [name for name in names if Path(directory) == root and name in {".git", ".codepier"}],
            )
            _validate_tree(destination)
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

    def extract_artifacts(self, snapshot: Snapshot, relative_paths: list[str], run_dir: Path) -> list[str]:
        self._owned(snapshot)
        run_dir = safe_directory(run_dir)
        extracted = []
        for value in relative_paths:
            relative = _relative(value)
            if relative == Path(".") or relative.parts[0] in {".git", ".codepier", ".codepier-run.json"}:
                raise DevError("SNAPSHOT_UNSAFE", "Invalid snapshot artifact path")
            source = snapshot.root / relative
            # Check every component: resolve containment alone permits internal symlinks.
            for component in [source, *source.parents]:
                if component == snapshot.root:
                    break
                if component.is_symlink():
                    raise DevError("SNAPSHOT_UNSAFE", "Artifact symlinks are not supported")
            if not source.exists():
                raise DevError("SNAPSHOT_ARTIFACT_MISSING", "Declared snapshot artifact was not produced")
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
