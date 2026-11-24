#!/usr/bin/env python3
"""Refresh the reviewed public-source inventory from Git-owned source paths.

Run after intentional additions, moves or deletions. --check is read-only.
Without a checked-out commit, use the packager's curated filesystem selection.
In a committed checkout, ignored local evidence stays outside the candidate set.
"""

from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_source_bundle import BASE_REQUIRED, include


def _has_git_baseline(root):
    if not (root / ".git").exists():
        return False
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
        cwd=root,
        check=False,
        stdout=subprocess.DEVNULL,
    )
    return result.returncode == 0


def inventory(root=ROOT):
    if not _has_git_baseline(root):
        from scripts.check_release import public_files

        return {"schema_version": 1, "files": sorted(set(BASE_REQUIRED) | set(public_files(root)))}

    raw = subprocess.check_output(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
    )
    names = set(BASE_REQUIRED)
    for raw_name in raw.split(b"\0"):
        if not raw_name:
            continue
        name = raw_name.decode("utf-8")
        relative = Path(name)
        path = root / relative
        if not include(relative, public=True):
            continue
        if path.is_symlink():
            raise ValueError("Public source must not be a symlink: " + name)
        if path.is_file():
            names.add(relative.as_posix())
    return {"schema_version": 1, "files": sorted(names)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = ROOT / "shared/source_inventory.json"
    raw = (json.dumps(inventory(), ensure_ascii=False, indent=2) + "\n").encode()
    changed = not path.is_file() or path.read_bytes() != raw
    if changed and not args.check:
        path.write_bytes(raw)
    print("Public source inventory: " + ("outdated" if changed and args.check else "current"))
    return int(args.check and changed)


if __name__ == "__main__":
    raise SystemExit(main())
