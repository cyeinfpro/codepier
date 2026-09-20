"""Capture the CodePier UI unification matrix with isolated, non-billable data."""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from tests.support import running_stack
from tests.test_ui_unification import exercise_matrix


DEFAULT_OUTPUT = Path("docs/evidence/ui-unify-20260915/screenshots")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Capture every CodePier management page plus native chat in light/dark "
            "at 320/390/768/1440 widths using an isolated Hub/Agent fixture."
        )
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = args.output.resolve()
    with tempfile.TemporaryDirectory(prefix="codepier-ui-unification-") as directory:
        with running_stack(directory) as stack:
            report = exercise_matrix(stack, output)
    report_path = output.parent / "capture-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "cases": len(report["cases"]),
                "screenshots": len(report["screenshots"]),
                "browser_errors": report["browser_errors"],
                "model_submission_requests": sum(
                    len(case["model_submission_requests"]) for case in report["cases"]
                ),
                "output": str(output),
                "report": str(report_path),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
