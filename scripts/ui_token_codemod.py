#!/usr/bin/env python3
"""Rewrite literal font-size / font-weight / border-radius values to design tokens.

python3 scripts/ui_token_codemod.py web/styles.css            # dry run, unified diff
python3 scripts/ui_token_codemod.py web/*.css --apply          # write back
"""

from __future__ import annotations

import argparse
import difflib
import re
from pathlib import Path

FONT = {
    "8px": "var(--ui-text-xs)",
    "9px": "var(--ui-text-xs)",
    "10px": "var(--ui-text-xs)",
    "11px": "var(--ui-text-xs)",
    "12px": "var(--ui-text-xs)",
    "13px": "var(--ui-text-sm)",
    "14px": "var(--ui-text-base)",
    "15px": "var(--ui-text-md)",
    "16px": "var(--ui-text-input)",
    "17px": "var(--ui-text-title)",
    "18px": "var(--ui-text-title)",
    "19px": "var(--ui-text-title)",
    "20px": "var(--ui-text-h2)",
    "21px": "var(--ui-text-h2)",
    "22px": "var(--ui-text-h2)",
    "23px": "var(--ui-text-h2)",
    "24px": "var(--ui-text-h2)",
}
WEIGHT = {
    "550": "var(--ui-weight-medium)",
    "500": "var(--ui-weight-medium)",
    "600": "var(--ui-weight-semibold)",
    "620": "var(--ui-weight-semibold)",
    "650": "var(--ui-weight-semibold)",
    "700": "var(--ui-weight-bold)",
    "720": "var(--ui-weight-bold)",
    "750": "var(--ui-weight-bold)",
    "400": "var(--ui-weight-regular)",
    "800": "var(--ui-weight-bold)",
}
RADIUS = {
    "6px": "var(--ui-radius-xs)",
    "7px": "var(--ui-radius-xs)",
    "8px": "var(--ui-radius-small)",
    "9px": "var(--ui-radius-small)",
    "10px": "var(--ui-radius-small)",
    "11px": "var(--ui-radius-md)",
    "12px": "var(--ui-radius-md)",
    "13px": "var(--ui-radius-md)",
    "14px": "var(--ui-radius-md)",
    "15px": "var(--ui-radius-lg)",
    "16px": "var(--ui-radius-lg)",
    "17px": "var(--ui-radius-lg)",
    "18px": "var(--ui-radius-lg)",
    "19px": "var(--ui-radius-xl)",
    "20px": "var(--ui-radius-xl)",
    "21px": "var(--ui-radius-xl)",
    "22px": "var(--ui-radius-xl)",
    "23px": "var(--ui-radius-xl)",
    "24px": "var(--ui-radius-xl)",
    "25px": "var(--ui-radius-xl)",
    "26px": "var(--ui-radius-xl)",
    "27px": "var(--ui-radius-xl)",
    "99px": "var(--ui-radius-pill)",
    "999px": "var(--ui-radius-pill)",
}
FONT.update({f"{n}px": "var(--ui-text-display)" for n in range(29, 100)})
FONT.update(
    {
        "25px": "var(--ui-page-title)",
        "26px": "var(--ui-page-title)",
        "27px": "var(--ui-page-title)",
        "28px": "var(--ui-page-title)",
    }
)
RADIUS.update({f"{n}px": "var(--ui-radius-xs)" for n in range(1, 6)})
RADIUS.update({f"{n}px": "var(--ui-radius-xl)" for n in range(28, 49)})
CANONICAL = {
    "text-xs": "12px",
    "text-sm": "13px",
    "text-base": "14px",
    "text-md": "15px",
    "text-input": "16px",
    "text-title": "18px",
    "text-h2": "22px",
    "text-display": "36px",
    "page-title": "28px",
    "radius-xs": "6px",
    "radius-small": "8px",
    "radius-md": "12px",
    "radius-lg": "16px",
    "radius-xl": "20px",
    "radius-pill": "999px",
    "weight-regular": "400",
    "weight-medium": "500",
    "weight-semibold": "600",
    "weight-bold": "700",
}


def with_fallback(value: str, keep: bool) -> str:
    return value[:-1] + ", " + CANONICAL[value[9:-1]] + ")" if keep else value


# Files whose declarations must keep a standalone fallback (see tests/test_ui_conversation_tokens.py).
NEEDS_FALLBACK = {"chat.css", "computer.css"}
RULES = [
    (re.compile(r"(font-size:\s*)(\d+px)(\s*(?:!important)?\s*[;}])"), FONT),
    (re.compile(r"(font-weight:\s*)(\d{3})(\s*(?:!important)?\s*[;}])"), WEIGHT),
    (re.compile(r"(border-radius:\s*)(\d+px)(\s*(?:!important)?\s*[;}])"), RADIUS),
]


def rewrite(text: str, keep_fallback: bool) -> str:
    def safe_sub(pattern, callback, source):
        protected = list(
            re.finditer(
                r"""("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(/\*.*?\*/)""", source, flags=re.S
            )
        )
        return re.sub(
            pattern,
            lambda match: (
                match[0]
                if any(span.start() <= match.start() < span.end() for span in protected)
                else callback(match)
            ),
            source,
        )

    for pattern, table in RULES:

        def swap(match: re.Match, *, table=table) -> str:
            token = table.get(match[2])
            if not token:
                return match[0]
            return match[1] + with_fallback(token, keep_fallback) + match[3]

        text = safe_sub(pattern, swap, text)

    def shorthand(match: re.Match) -> str:
        value = match[2]
        value = re.sub(
            r"(?<![\w.-])(\d+)px(?=\s|/)",
            lambda m: with_fallback(FONT[m[0]], keep_fallback) if m[0] in FONT else m[0],
            value,
            count=1,
        )
        value = re.sub(r"^(\s*)(\d{3})(\s+)", lambda m: m[1] + WEIGHT.get(m[2], m[2]) + m[3], value)
        return match[1] + value + match[3]

    return safe_sub(r"(?<![-\w])(font:\s*)([^;{}]+)([;}])", shorthand, text)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    for name in args.files:
        path = Path(name)
        if path.name == "tokens.css":
            continue
        before = path.read_text(encoding="utf-8")
        after = rewrite(before, path.name in NEEDS_FALLBACK)
        if before == after:
            continue
        if args.apply:
            path.write_text(after, encoding="utf-8")
            print("rewrote", path)
        else:
            print(
                "".join(
                    difflib.unified_diff(
                        before.splitlines(True),
                        after.splitlines(True),
                        str(path),
                        str(path) + " (tokens)",
                    )
                )
            )


if __name__ == "__main__":
    main()
