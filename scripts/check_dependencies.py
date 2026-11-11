#!/usr/bin/env python3
"""Check installed versions against the platform's hash-locked requirements."""

from __future__ import annotations

import argparse
from importlib.metadata import distributions
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def expected_versions(lock: str) -> dict[str, str]:
    expected = {}
    for line in lock.splitlines():
        text = line.strip().removesuffix("\\").strip()
        if not text or text.startswith(("#", "--")):
            continue
        requirement = Requirement(text)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        pins = list(requirement.specifier)
        if len(pins) != 1 or pins[0].operator != "==" or "*" in pins[0].version:
            raise ValueError("Lock contains a non-exact requirement: " + requirement.name)
        name = canonicalize_name(requirement.name)
        if name in expected and expected[name] != pins[0].version:
            raise ValueError("Conflicting active lock versions: " + name)
        expected[name] = pins[0].version
    if not expected:
        raise ValueError("Lock contains no active requirements")
    return expected


def differences(expected: dict[str, str], installed: dict[str, str]) -> list[str]:
    installed = {canonicalize_name(name): version for name, version in installed.items()}
    return [
        f"{name}: expected {version}, installed {installed.get(name, 'missing')}"
        for name, version in sorted(expected.items())
        if installed.get(name) != version
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=ROOT / "requirements-dev.txt")
    args = parser.parse_args()
    expected = expected_versions(args.lock.read_text(encoding="utf-8"))
    installed = {d.metadata["Name"]: d.version for d in distributions()}
    drift = differences(expected, installed)
    print("\n".join(drift) if drift else f"Dependency lock verified: {len(expected)} active pins")
    return int(bool(drift))


if __name__ == "__main__":
    raise SystemExit(main())
