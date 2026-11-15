#!/usr/bin/env python3
"""Build deterministic public integration assets from explicit source files."""

from __future__ import annotations
import argparse
import hashlib
import io
import json
from pathlib import Path
import stat
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXTENSION_FILES = (
    "manifest.json",
    "background.js",
    "workspace.js",
    "page.js",
    "popup.html",
    "popup.js",
    "popup.css",
    "idle.html",
)


def render_assets():
    source = ROOT / "web/browser-extension"
    buffer = io.BytesIO()
    entries = {}
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in EXTENSION_FILES:
            path = source / name
            if path.is_symlink() or not path.is_file():
                raise ValueError("Missing regular extension source: " + name)
            raw = path.read_bytes()
            entries[name] = hashlib.sha256(raw).hexdigest()
            info = zipfile.ZipInfo(name, (2026, 9, 17, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, raw)
    manifest = json.loads((source / "manifest.json").read_text())
    if manifest.get("host_permissions"):
        raise ValueError("Public extension must require explicit site grants")
    guide = (ROOT / "docs/INTEGRATIONS-20260917.md").read_bytes()
    outputs = {"browser-extension.zip": buffer.getvalue(), "integration-guide.md": guide}
    info = {}
    for name, raw in outputs.items():
        info[name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    result = {"outputs": info, "extension_sources": entries, "contains_credentials": False}
    outputs["integration-assets.json"] = (json.dumps(result, indent=2) + "\n").encode()
    return outputs, result


def build(*, check: bool = False):
    """Render once; check mode never writes, including when outputs are missing."""
    outputs, result = render_assets()
    drift = []
    for name, raw in outputs.items():
        path = ROOT / "web" / name
        if check:
            if not path.is_file() or path.read_bytes() != raw:
                drift.append(name)
        else:
            path.write_bytes(raw)
    if check:
        result = {**result, "check": True, "drift": drift, "matches": not drift}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Report generated drift without writing"
    )
    options = parser.parse_args()
    result = build(check=options.check)
    print(json.dumps(result, indent=2))
    raise SystemExit(1 if options.check and not result["matches"] else 0)
