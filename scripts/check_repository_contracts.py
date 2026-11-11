#!/usr/bin/env python3
"""Detect drift between runtime defaults, deployment templates and examples."""

from __future__ import annotations
import ast
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]


def check(root=ROOT):
    config = ast.parse((root / "shared/config.py").read_text())
    timezone = next(
        ast.literal_eval(node.value)
        for node in config.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "DEFAULT_TIMEZONE"
            for target in node.targets
        )
    )
    templates = ("Dockerfile", "compose.yml", ".env.example", "deploy/k3s/deployment.yaml")
    for name in templates:
        if timezone not in (root / name).read_text():
            raise ValueError("Default timezone drift in " + name)
    compose = (root / "compose.yml").read_text()
    example = (root / ".env.example").read_text()
    declared = set(re.findall(r"^      ([A-Z][A-Z0-9_]+):", compose, re.M))
    documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", example, re.M))
    expected = {
        "MCP_ALLOWED_ORIGINS",
        "HUB_HOST",
        "CODEPIER_MCP_RESPONSE_BYTES",
        "CODEPIER_RELAY_BATCH_ENABLED",
        "CODEPIER_RELAY_BATCH_SHADOW",
        "CODEPIER_EXPERIENCE_PROJECTION_ENABLED",
        "CODEPIER_KNOWLEDGE_AUTO_ENROLL",
        "CODEPIER_ROOM_HOST_CONNECTIONS_MODE",
        "CODEPIER_WEIXIN_APPROVED_API_BASES",
        "MCP_BLOCK_NATIVE_COMPUTER",
    }
    for name in ("hub/config.py", "hub/oidc.py"):
        tree = ast.parse((root / name).read_text())
        expected.update(
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and re.fullmatch(r"CODEPIER_OIDC_[A-Z_]+", node.value)
        )
    expected.discard("CODEPIER_OIDC_INSECURE_TEST_LOOPBACK")
    for label, actual in (("Compose", declared), ("environment example", documented)):
        missing = expected - actual
        if missing:
            raise ValueError(label + " is missing settings: " + ", ".join(sorted(missing)))
    if "CODEPIER_OIDC_INSECURE_TEST_LOOPBACK" in declared:
        raise ValueError(
            "The test-only insecure OIDC override must not be a production Compose option"
        )
    overlay = (root / "deploy/compose.performance.yml").read_text()
    cpu = re.search(r"CODEPIER_HUB_CPUS:-([^}]+)", overlay).group(1)
    if re.search(r"^CODEPIER_HUB_CPUS=(.+)$", example, re.M).group(1) != cpu:
        raise ValueError("CPU budget example differs from its overlay default")
    return len(expected)


def main():
    try:
        count = check()
    except (ValueError, StopIteration, AttributeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Deployment contracts: {count} settings and timezone/CPU defaults agree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
