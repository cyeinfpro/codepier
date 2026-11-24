#!/usr/bin/env python3
"""Runner for the P5.4 synthetic CodePier tool-use evaluation suite."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.evals.codepier_tool_use import LiveEndpointError, run_eval  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--live-endpoint", default=None)
    parser.add_argument("--model-id", default="offline-baseline-policy-v1")
    parser.add_argument("--task", action="append", dest="tasks", default=None)
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = run_eval(
            mode=args.mode,
            task_ids=args.tasks,
            live_endpoint=args.live_endpoint,
            model_id=args.model_id,
        )
    except (LiveEndpointError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2

    payload = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)
    return 0 if not report["failed_samples"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
