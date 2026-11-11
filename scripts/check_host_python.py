#!/usr/bin/env python3
"""Check the Python 3.9 host bootstrap contract without starting services.

CI also executes --smoke with a real Python 3.9 interpreter; the syntax/API
check runs in ordinary Python 3.13 development environments.
"""

from __future__ import annotations
import argparse
import ast
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = (
    "migrate_hub*.py",
    "rename_checkout.py",
    "panel_update*.py",
    "install_agent.py",
    "agent_lifecycle.py",
)


def selected(root=ROOT):
    return sorted({path for pattern in PATTERNS for path in (root / "scripts").glob(pattern)})


def check(root=ROOT, *, smoke=False):
    files = selected(root)
    for path in files:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path), feature_version=(3, 9))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "zip"
                and any(arg.arg == "strict" for arg in node.keywords)
            ):
                raise ValueError(f"{path.name}:{node.lineno}: zip(strict=) requires Python 3.10")
        if smoke:
            runpy.run_path(str(path), run_name="codepier_host_compat_check")
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "scripts"))
    sys.path.insert(0, str(ROOT))
    print(f"Host Python 3.9 contract: {check(smoke=args.smoke)} scripts checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
