"""Offline, non-destructive preparation of a split-state volume.

Never changes Compose, live data, updater state, or removes any source file.
The default command prints a plan. Preparing requires an explicit offline flag
and exclusive Hub/key locks; activating the result is a separate deployment.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

try:
    from .panel_update_runtime import COPY_STATE
except ImportError:
    from panel_update_runtime import COPY_STATE

def plan(source, destination):
    source, destination = Path(source).absolute(), Path(destination).absolute()
    if source.is_symlink() or destination.is_symlink():
        raise ValueError("Storage roots cannot be symlinks")
    source, destination = source.resolve(), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("State and asset roots must be separate")
    if not source.is_dir() or not destination.is_dir() or any(destination.iterdir()):
        raise ValueError("Source must exist and destination must be an empty prepared mount")
    for name in ("hub.sqlite3", "master.key"):
        path = source / name
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            raise ValueError("Source identity is incomplete")
    if (source / "rekey.json").exists() or (source / ".rekey").exists():
        raise ValueError("Key recovery material needs explicit review before migration")
    names = ["hub.sqlite3", "master.key", "master.keys.json", "native-cli/native.sqlite3"]
    files = [{"path": name, "bytes": (source / name).stat().st_size}
             for name in names if (source / name).exists()]
    return {"schema_version": 1, "operation": "prepare-split-state", "layout": "split-state-v1",
            "source": str(source), "destination": str(destination), "files": files,
            "source_preserved": True, "activate": False, "delete": [],
            "stable_assets_must_remain_mounted": "/app/data",
            "required": ["Hub stopped and maintenance admission closed", "exclusive Hub and key locks",
                         "separate activation approval", "preserve original Compose for rollback"]}

def prepare(source, destination, *, confirm_offline=False):
    if not confirm_offline:
        raise ValueError("Explicit offline preparation confirmation required")
    proposed = plan(source, destination)
    # Import from the trusted source tree, never from the source data volume.
    from shared.instance_lock import InstanceLock
    source, destination = Path(proposed["source"]), Path(proposed["destination"])
    hub_lock = InstanceLock(source / ".hub.lock")
    key_lock = None
    try:
        key_lock = InstanceLock(source / ".keys.lock")
        proposed = plan(source, destination)
        required = sum(item["bytes"] for item in proposed["files"]) + 512 * 1024 * 1024
        if shutil.disk_usage(destination).free < required:
            raise ValueError("Insufficient destination space for state plus safety margin")
        script = COPY_STATE.replace("Path('/backup'),Path('/app/data')",
                                   f"Path({str(source)!r}),Path({str(destination)!r})")
        # Only preparation accepts a legacy data root containing unrelated assets.
        # They remain in their original volume, never deleted, moved or copied.
        script = script.replace(
            "if any(p.name not in allowed or p.is_symlink() for p in src.iterdir()): raise RuntimeError('Unknown state files; inspect before snapshot')",
            "if any((src/n).is_symlink() for n in allowed): raise RuntimeError('Symlink state identity')")
        script = script.replace("marker=json.loads((src/'.codepier-state.json').read_text())",
                                "marker={'schema_version':1,'layout':'split-state-v1'}")
        completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=1800)
        if completed.returncode:
            raise RuntimeError("State preparation incomplete; destination retained for inspection: " + completed.stderr[-2000:])
        return {**proposed, "prepared": True, "copy": json.loads(completed.stdout)}
    finally:
        if key_lock is not None:
            key_lock.close()
        hub_lock.close()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source")
    parser.add_argument("destination")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--confirm-offline", action="store_true")
    args = parser.parse_args()
    result = prepare(args.source, args.destination, confirm_offline=args.confirm_offline) if args.prepare else plan(args.source, args.destination)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))

if __name__ == "__main__":
    main()
