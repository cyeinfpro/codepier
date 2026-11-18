"""Ruff complexity limits for functions in the Hub, Agent and shared runtime."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts/function_complexity_baseline.json"
RULES = "C901,PLR0915"
# Existing large handlers are tracked individually. Smaller functions use
# Ruff's default thresholds directly.
LIMIT = re.compile(r"\((\d+) > \d+\)$")


class Functions(ast.NodeVisitor):
    def __init__(self):
        self.parents = []
        self.lines = {}

    def visit_ClassDef(self, node):
        self.parents.append(node.name)
        self.generic_visit(node)
        self.parents.pop()

    def visit_FunctionDef(self, node):
        self.parents.append(node.name)
        self.lines[node.lineno] = ".".join(self.parents)
        self.generic_visit(node)
        self.parents.pop()

    visit_AsyncFunctionDef = visit_FunctionDef


def measure():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--select",
            RULES,
            "--output-format",
            "json",
            "agent",
            "hub",
            "shared",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr or result.stdout)
    paths, scores = {}, {}
    for report in json.loads(result.stdout):
        path = Path(report["filename"])
        if path not in paths:
            visitor = Functions()
            visitor.visit(ast.parse(path.read_bytes(), filename=str(path)))
            paths[path] = visitor.lines
        name = paths[path][report["location"]["row"]]
        key = f"{path.relative_to(ROOT).as_posix()}::{name}::{report['code']}"
        value = int(LIMIT.search(report["message"])[1])
        scores[key] = max(scores.get(key, 0), value)
    return scores


def check(*, refresh=False):
    scores = measure()
    if not BASELINE.exists():
        if not refresh:
            raise ValueError("Function complexity baseline is missing")
        baseline = scores
    else:
        baseline = json.loads(BASELINE.read_text())
    increased = {
        name: score
        for name, score in scores.items()
        if name not in baseline or score > baseline[name]
    }
    if increased:
        detail = "\n".join(
            f"{name} increased from {baseline.get(name, 'Ruff limit')} to {score}"
            for name, score in sorted(increased.items())
        )
        raise ValueError(f"Function complexity increased:\n{detail}")
    if refresh:
        BASELINE.write_text(json.dumps(scores, indent=2, sort_keys=True) + "\n")
    elif scores != baseline:
        raise ValueError(
            "Function complexity decreased. Refresh with scripts/check_function_complexity.py --refresh"
        )
    return len(scores)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true")
    args = parser.parse_args()
    try:
        tracked = check(refresh=args.refresh)
    except (RuntimeError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1
    print(f"Function complexity: {tracked} legacy violations at the current limits")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
