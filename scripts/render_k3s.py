#!/usr/bin/env python3
"""Render a deployable immutable manifest from a source-only k3s template."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

try:
    from .check_release import version
except ImportError:
    from check_release import version


def render(root, image, digest, commit):
    root = Path(root)
    current = version(root)
    if not re.fullmatch(r"ghcr\.io/[a-z0-9][a-z0-9._-]*/codepier", image):
        raise ValueError("Use the verified CodePier GHCR image")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError("An immutable OCI index digest is required")
    if not re.fullmatch(r"[a-f0-9]{40}", commit):
        raise ValueError("The exact source commit is required")
    source = (root / "deploy/k3s/deployment.yaml").read_text()
    pattern = re.compile(r"(image:\s*)ghcr\.io/[a-z0-9][a-z0-9._-]*/codepier:"
                         + re.escape(current) + r"@sha256:RELEASE_DIGEST")
    pinned = f"{image}:{current}@{digest}"
    result, count = pattern.subn(lambda match: match[1] + pinned, source)
    images = re.findall(r"^\s+(?:-\s+)?image:\s*([^\n]+)$", result, re.M)
    if count != 2 or len(images) != 2 or any(value.strip() != pinned for value in images):
        raise ValueError("Template image set is incomplete or contains an unpinned image")
    result = result.replace("# TEMPLATE ONLY: render with scripts/render_k3s.py and the verified image digest before applying.",
                            f"# Verified source {commit}; immutable OCI index {digest}.", 1)
    metadata = {"schema_version": 1, "source_commit": commit, "version": current,
                "image": pinned, "digest": digest,
                "manifest_sha256": hashlib.sha256(result.encode()).hexdigest()}
    return result, metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--image", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    args = parser.parse_args()
    rendered, metadata = render(args.root, args.image, args.digest, args.commit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.metadata.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered)
    args.metadata.write_text(json.dumps(metadata, indent=2) + "\n")


if __name__ == "__main__":
    main()
