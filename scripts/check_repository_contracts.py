#!/usr/bin/env python3
"""Detect drift between runtime defaults, deployment templates and examples."""

from __future__ import annotations
import argparse
import ast
from hashlib import sha256
import json
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
        "HUB_DELIVERY_SLOTS",
        "HUB_DELIVERY_PREFETCH",
        "HUB_DELIVERY_DEVICE_CHUNK",
        "HUB_DELIVERY_ROLLING_DEVICES",
        "CODEPIER_MCP_RESPONSE_BYTES",
        "CODEPIER_RELAY_BATCH_ENABLED",
        "CODEPIER_RELAY_BATCH_SHADOW",
        "CODEPIER_EXPERIENCE_PROJECTION_ENABLED",
        "CODEPIER_KNOWLEDGE_AUTO_ENROLL",
        "CODEPIER_ROOM_HOST_CONNECTIONS_MODE",
        "CODEPIER_WEIXIN_APPROVED_API_BASES",
        "MCP_BLOCK_NATIVE_COMPUTER",
        "CODEPIER_MCP_DEFAULT_VIEW",
        "CODEPIER_MCP_STREAMS_PER_PRINCIPAL",
        "CODEPIER_MCP_MAX_STREAMS",
        "CODEPIER_MCP_SSE_KEEPALIVE_INTERVAL",
        "CODEPIER_MCP_SSE_GRACE_SECONDS",
        "CODEPIER_SQLITE_SYNCHRONOUS",
        "CODEPIER_EVIDENCE_DEDUP_SECONDS",
    }
    runtime_tuning = {
        "CODEPIER_MCP_RESPONSE_BYTES",
        "CODEPIER_MCP_DEFAULT_VIEW",
        "CODEPIER_MCP_STREAMS_PER_PRINCIPAL",
        "CODEPIER_MCP_MAX_STREAMS",
        "CODEPIER_MCP_SSE_KEEPALIVE_INTERVAL",
        "CODEPIER_MCP_SSE_GRACE_SECONDS",
        "CODEPIER_SQLITE_SYNCHRONOUS",
        "CODEPIER_EVIDENCE_DEDUP_SECONDS",
    }
    k3s = (root / "deploy/k3s/deployment.yaml").read_text()
    configmap = set(re.findall(r"^  ([A-Z][A-Z0-9_]+):", k3s, re.M))
    if runtime_tuning - configmap:
        raise ValueError(
            "k3s ConfigMap is missing settings: " + ", ".join(sorted(runtime_tuning - configmap))
        )
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from shared.mcp_protocol import DEFAULT_OAUTH_REDIRECT_HOSTS

    for name, text in (("compose.yml", compose), (".env.example", example), ("k3s", k3s)):
        if DEFAULT_OAUTH_REDIRECT_HOSTS not in text:
            raise ValueError("OAUTH_REDIRECT_HOSTS default drift in " + name)
    for setting in ("CODEPIER_LOG_MAX_SIZE", "CODEPIER_LOG_MAX_FILES"):
        if "${" + setting + ":-" not in compose:
            raise ValueError("Compose log rotation must honor " + setting)
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


DOC_SYMBOL = re.compile(
    r"`((?:agent|hub|scripts|shared|tests)/[\w./-]+\.py)::"
    r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)`"
)


def source_symbols(nodes, prefix=""):
    for node in nodes:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            name = prefix + node.name
            yield name
            yield from source_symbols(node.body, name + ".")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    yield prefix + target.id


def check_doc_symbols(root=ROOT):
    symbols = {}
    errors = []
    count = 0
    for document in sorted((root / "docs").rglob("*.md")):
        if "archive" in document.relative_to(root / "docs").parts:
            continue
        for line_number, line in enumerate(document.read_text(encoding="utf-8").splitlines(), 1):
            for path, name in DOC_SYMBOL.findall(line):
                count += 1
                if path not in symbols:
                    target = root / path
                    symbols[path] = (
                        set(source_symbols(ast.parse(target.read_bytes(), filename=path).body))
                        if target.is_file()
                        else set()
                    )
                if name not in symbols[path]:
                    errors.append(f"{document.relative_to(root)}:{line_number}: {path}::{name}")
    if errors:
        raise ValueError("Broken documentation anchors:\n" + "\n".join(errors))
    return count


def check_core_schemas(*, write=False):
    sys.path.insert(0, str(ROOT))
    from shared.contracts import _CORE_SCHEMA_INPUTS
    from shared.core_output_schemas import build_core_output_schemas
    from shared.frozen_core_output_schemas import CORE_OUTPUT_SCHEMA_JSON

    schemas = build_core_output_schemas(_CORE_SCHEMA_INPUTS, include_queries=True)
    names = dict.fromkeys(["vps", *schemas])
    data = json.dumps(
        {name: schemas[name] for name in names if name in schemas},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    if write:
        lines = [
            '"""Generated by scripts/check_repository_contracts.py --write-core-schemas."""',
            "",
            "CORE_OUTPUT_SCHEMA_JSON = (",
        ]
        lines.extend(f"    {data[offset : offset + 96]!r}" for offset in range(0, len(data), 96))
        (ROOT / "shared/frozen_core_output_schemas.py").write_text("\n".join([*lines, ")", ""]))
    elif data != CORE_OUTPUT_SCHEMA_JSON:
        raise ValueError(
            "Frozen output schemas drifted. Run scripts/check_repository_contracts.py --write-core-schemas"
        )
    return sha256(data.encode()).hexdigest()


def check_native_contracts(*, write=False):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts.build_native_contracts import check as check_contracts

    changed, digest = check_contracts(write=write)
    if changed:
        raise ValueError("Native contracts drifted. Run scripts/build_native_contracts.py")
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-core-schemas", action="store_true")
    args = parser.parse_args()
    try:
        count = check()
        anchors = check_doc_symbols()
        core_hash = check_core_schemas(write=args.write_core_schemas)
        native_hash = check_native_contracts(write=False)
    except (ValueError, StopIteration, AttributeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"Deployment contracts: {count} settings and timezone/CPU defaults agree")
    print(f"Documentation: {anchors} source symbols verified")
    print(f"Core output schema SHA256: {core_hash}")
    print(f"Native contracts SHA256: {native_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
