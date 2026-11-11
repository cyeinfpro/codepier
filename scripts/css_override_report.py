#!/usr/bin/env python3
"""Conservative CSS declaration comparison, respecting functions and conditions.

Reports candidates, not an automatic deletion plan. A grouped rule can only
lose a declaration when every selector is covered by the later stylesheet.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path


def split_top(value: str, delimiter: str = ",") -> list[str]:
    result, start, depth, quote, escaped = [], 0, 0, "", False
    for i, char in enumerate(value):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote:
            if char == quote:
                quote = ""
        elif char in ('"', "'"):
            quote = char
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == delimiter and depth == 0:
            result.append(value[start:i].strip())
            start = i + 1
    result.append(value[start:].strip())
    return result


def clean(css: str) -> str:
    # Keep line positions, and do not treat comment markers inside strings as comments.
    pattern = r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(/\*.*?\*/)"""
    return re.sub(pattern, lambda m: m[1] if m[1] else "\n" * m[0].count("\n"), css, flags=re.S)


def declarations(css: str, context: tuple[str, ...] = ()):
    css = clean(css)
    start, level, quote, escaped, opened = 0, 0, "", False, None
    for i, char in enumerate(css):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in ('"', "'"):
            quote = char
            continue
        if char == "{":
            if level == 0:
                prelude = css[start:i].strip()
                if ";" in prelude:
                    prelude = split_top(prelude, ";")[-1]
                opened = i + 1
            level += 1
        elif char == "}":
            level -= 1
            if level < 0:
                raise ValueError("Unbalanced CSS closing brace")
            if level:
                continue
            body = css[opened:i]
            name = " ".join(prelude.split())
            if name.startswith(("@media", "@supports", "@layer", "@container")):
                yield from declarations(body, (*context, name))
            elif not name.startswith("@"):
                for item in split_top(body, ";"):
                    if ":" not in item:
                        continue
                    prop, value = item.split(":", 1)
                    yield context, name, prop.strip(), " ".join(value.split())
            start = i + 1
    if level or quote:
        raise ValueError("Unclosed CSS block or string")


def overridden(earlier: str, later: str):
    winners = {}
    for context, selector, prop, value in declarations(later):
        for item in split_top(selector):
            winners[(context, item, prop)] = value
    for context, selector, prop, value in declarations(earlier):
        if all(
            (context, item, prop) in winners
            and ("!important" not in value or "!important" in winners[(context, item, prop)])
            for item in split_top(selector)
        ):
            yield context, selector, prop


def main(earlier: str, later: str) -> int:
    dead = defaultdict(list)
    for context, selector, prop in overridden(Path(earlier).read_text(), Path(later).read_text()):
        dead[(context, selector)].append(prop)
    for (context, selector), props in sorted(dead.items()):
        print(f"{earlier}: {' '.join(context)} {selector} -> {', '.join(props)}")
    print(f"{sum(map(len, dead.values()))} overridden declaration candidates")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(*sys.argv[1:]))
