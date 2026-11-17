"""Bounded, non-executing preconditions for owner-configured named tasks.

Checks only the actual prepared environment; never imports packages, reads file
contents, inherits shell settings or provisions dependencies.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil

from shared.util import DevError

FIELDS = frozenset({"env", "executables", "files"})


def validate_requirements(value):
    """Keep task requirements opt-in and reject unbounded or ambiguous probes."""
    if not isinstance(value, dict) or set(value) - FIELDS:
        raise ValueError("task requirements supports env, executables and files only")
    result = {}
    for field, items in value.items():
        if not isinstance(items, list) or len(items) > 32:
            raise ValueError("task requirements lists must contain at most 32 entries")
        for item in items:
            if not isinstance(item, str) or not 1 <= len(item) <= 256:
                raise ValueError("task requirements entries must be bounded names")
            if field == "env" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item):
                raise ValueError("task requirements env entries must be variable names")
            if field == "executables" and not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*", item):
                raise ValueError("task requirements executables must be PATH basenames")
            if field == "files":
                from agent.filesystem import relative_path

                try:
                    relative_path(item, allow_dot=False)
                except DevError as exc:
                    raise ValueError(
                        "task requirements files must be public project-relative files"
                    ) from exc
        result[field] = list(dict.fromkeys(items))
    return result


def inspect_requirements(task, environment, root, *, mode="project_clean_v1"):
    """Observe presence without exposing values, file contents or resolved paths."""
    requirements = validate_requirements(task.get("requirements", {}))
    if not any(requirements.values()):
        return {"state": "not_configured", "checks": []}
    if mode == "container_v1":
        return {"state": "unsupported", "code": "TASK_PREFLIGHT_UNSUPPORTED", "checks": []}
    checks = []
    for name in requirements.get("env", []):
        present = (
            any(
                key.casefold() == name.casefold() and bool(value)
                for key, value in environment.items()
            )
            if os.name == "nt"
            else bool(environment.get(name))
        )
        checks.append({"kind": "env", "name": name, "available": present})
    for name in requirements.get("executables", []):
        found = shutil.which(name, path=environment.get("PATH", ""))
        checks.append({"kind": "executable", "name": name, "available": bool(found)})
    for name in requirements.get("files", []):
        try:
            resolved_root = Path(root).resolve(strict=True)
            actual = (resolved_root / name).resolve()
            # Do not probe outside the authorized project or follow a link to a
            # protected internal/credential file even when the alias is public.
            from agent.filesystem import relative_path

            relative_path(actual.relative_to(resolved_root).as_posix(), allow_dot=False)
            present = actual.is_file() and os.access(actual, os.R_OK)
        except (OSError, RuntimeError, ValueError, DevError):
            present = False
        checks.append({"kind": "file", "name": name, "available": present})
    return {
        "state": "ready" if all(row["available"] for row in checks) else "blocked",
        "checks": checks,
    }


def enforce_requirements(task, environment, root, *, mode="project_clean_v1"):
    result = inspect_requirements(task, environment, root, mode=mode)
    if result["state"] in {"blocked", "unsupported"}:
        missing = [
            f"{row['kind']}:{row['name']}" for row in result["checks"] if not row["available"]
        ]
        summary = ", ".join(missing[:8])
        if len(missing) > 8:
            summary += f" (+{len(missing) - 8} more)"
        reason = summary or "container task requirements are not supported"
        raise DevError(
            result.get("code", "TASK_PREREQUISITES_MISSING"),
            "Task preflight blocked: " + reason + "; no workload was started",
            409,
            readiness=result,
            workload_started=False,
        )
    return result
