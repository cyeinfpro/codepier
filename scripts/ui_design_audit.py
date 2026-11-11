#!/usr/bin/env python3
"""Report raw design drift and the bounded standalone compatibility exceptions.

Color fallback exceptions are exact declarations and bounded counts, not file
or var() exemptions. Raw totals remain visible. Semantic dimension totals
collapse aliases and fallback spellings of the same token; raw totals are also
reported. No CSS or baseline is changed by this command.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
from pathlib import Path

try:
    from scripts.css_override_report import clean, declarations
except ModuleNotFoundError:
    from css_override_report import clean, declarations

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
HEX = re.compile(r"#[0-9a-fA-F]{3,8}\b")
RAW_Z = re.compile(r"^-?\d+$")
CONFIRM = re.compile(r"(?<![\w.])(?:window\.)?confirm\(")
ALIASES = {
    "--chat-control-radius": "--ui-radius-small",
    "--ui-control-radius": "--ui-radius-small",
    "--chat-window-radius": "--ui-radius-xl",
    "--ui-window-radius": "--ui-radius-xl",
    "--chat-panel-radius": "--ui-radius-xl",
    "--ui-panel-radius": "--ui-radius-xl",
    "--ui-panel-radius-compact": "--ui-radius-lg",
}
FALLBACK_PATH = ROOT / "scripts/ui_design_fallbacks.json"


def key(path: str, context, selector: str, prop: str, value: str) -> tuple:
    return path, tuple(context), selector, prop, value


def compatibility_limits(path: Path = FALLBACK_PATH) -> collections.Counter:
    if not path.exists():
        return collections.Counter()
    data = json.loads(path.read_text(encoding="utf-8"))
    return collections.Counter(
        {
            key(row["path"], row["context"], row["selector"], row["property"], row["value"]): row[
                "count"
            ]
            for row in data
        }
    )


def eligible_fallback(path: str, value: str) -> bool:
    if path not in {"chat.css", "computer.css"}:
        return False
    remainder = re.sub(r"var\(\s*--ui-[\w-]+\s*,\s*#[0-9a-fA-F]{3,8}\s*\)", "", value)
    return bool(HEX.search(value)) and not HEX.search(remainder)


def semantic(value: str) -> str:
    value = value.replace("!important", "").strip()
    value = re.sub(r"var\((--[\w-]+),\s*[^()]+\)", r"var(\1)", value)
    value = re.sub(r"--[\w-]+", lambda m: ALIASES.get(m[0], m[0]), value)
    return value


def metrics(web: Path = WEB, limits=None) -> dict:
    allowed = compatibility_limits() if limits is None else collections.Counter(limits)
    used = collections.Counter()
    sizes, raw_sizes, radii, raw_radii, weights = set(), set(), set(), set(), set()
    layers, per_file = set(), {}
    for path in sorted(web.glob("*.css")):
        css = clean(path.read_text(encoding="utf-8"))
        row = {
            "hex": 0,
            "compatibility_hex": 0,
            "important": 0,
            "raw_z_index": 0,
            "font_px_below_11": 0,
            "font_px_below_12": 0,
        }
        for context, selector, prop, value in declarations(css):
            hexes = len(HEX.findall(value)) if path.name != "tokens.css" else 0
            row["hex"] += hexes
            identity = key(path.name, context, selector, prop, value)
            if eligible_fallback(path.name, value) and used[identity] < allowed[identity]:
                used[identity] += 1
                row["compatibility_hex"] += hexes
            row["important"] += value.count("!important")
            plain = value.replace("!important", "").strip()
            if prop == "z-index" and RAW_Z.fullmatch(plain):
                row["raw_z_index"] += 1
                layers.add(int(plain))
            if prop == "font-size":
                sizes.add(semantic(value))
                raw_sizes.add(plain)
            if prop == "border-radius":
                radii.add(semantic(value))
                raw_radii.add(plain)
            if prop == "font-weight":
                weights.add(semantic(value))
            if prop in {"font", "font-size"}:
                numbers = [float(n) for n in re.findall(r"(?<![\w.-])(\d+(?:\.\d+)?)px", value)]
                row["font_px_below_11"] += sum(n < 11 for n in numbers)
                row["font_px_below_12"] += sum(n < 12 for n in numbers)
        per_file[path.name] = row
    total = lambda field: sum(row[field] for row in per_file.values())
    confirms, references, show_modal = 0, 0, 0
    for path in list(web.glob("*.js")) + list((web / "core").glob("*.mjs")):
        code = path.read_text(encoding="utf-8")
        count = len(CONFIRM.findall(code))
        references += count
        # This module's scoped async confirm is the existing branded adapter,
        # not window.confirm. Do not count its definition/calls as native UI.
        if not (path.name == "integrations.js" and "async function confirm(" in code):
            confirms += count
        show_modal += len(re.findall(r"\.showModal\(\)", code))
    return {
        "files": per_file,
        "hex_outside_tokens": total("hex"),
        "compatibility_hex": total("compatibility_hex"),
        "unapproved_hex": total("hex") - total("compatibility_hex"),
        "important": total("important"),
        "raw_z_index": total("raw_z_index"),
        "raw_z_values": sorted(layers),
        "font_px_below_11": total("font_px_below_11"),
        "font_px_below_12": total("font_px_below_12"),
        "font_size_distinct": len(sizes),
        "font_size_distinct_raw": len(raw_sizes),
        "radius_distinct": len(radii),
        "radius_distinct_raw": len(raw_radii),
        "weight_distinct": len(weights),
        "weights": sorted(weights),
        "native_confirm_calls": confirms,
        "confirm_references": references,
        "native_show_modal_calls": show_modal,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = metrics()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        for key_, value in result.items():
            if key_ != "files":
                print(f"{key_}: {value}")
        for name, row in result["files"].items():
            print(name, row)


if __name__ == "__main__":
    main()
