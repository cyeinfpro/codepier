"""Bounded, best-effort secret scrubbing for human-readable audit displays.

This is a display boundary, not a credential detector: never log raw request
bodies or decrypt operation payloads to make a richer audit record.
"""
from __future__ import annotations

import re
from itertools import islice

REDACTED = "<redacted>"
_SECRET = r"(?:password|passwd|passphrase|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|client[_-]?secret|authorization|proxy[_-]?authorization|cookie|set[_-]?cookie|credential|sshp[a-z]*|verifier)"
_SECRET_KEY = re.compile(_SECRET, re.I)
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)")
_AUTH = re.compile(r"\b(Bearer|Basic)\s+[^\s\"'<>;,]+", re.I)
_URL_AUTH = re.compile(r"(\b[a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]*@", re.I)
_VALUE = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;&\r\n]+)'''
_ASSIGN = re.compile(rf'''(?<![\w.-])((?:[\w.-]{{0,96}}{_SECRET}[\w.-]{{0,96}})["']?\s*[:=]\s*){_VALUE}''', re.I)
_FLAG = re.compile(rf'''((?:--?[\w-]{{0,96}}{_SECRET}[\w-]{{0,96}})\s+){_VALUE}''', re.I)
_HEADER = re.compile(r"(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\s*:\s*)[^\r\n]+")
_ENV_ASSIGN = re.compile(rf'''(?<![\w/])([A-Z][A-Z0-9_]*=){_VALUE}''')
_CONTROL = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\Z))|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def redact_text(text: str) -> str:
    """Scrub recognizable credentials before shortening or HTML escaping."""
    text = _CONTROL.sub("", text)
    text = _PEM.sub(REDACTED, text)
    text = _URL_AUTH.sub(lambda m: m[1] + REDACTED + "@", text)
    text = _HEADER.sub(lambda m: m[1] + REDACTED, text)
    text = _AUTH.sub(lambda m: m[1] + " " + REDACTED, text)
    text = _ASSIGN.sub(lambda m: m[1] + REDACTED, text)
    text = _FLAG.sub(lambda m: m[1] + REDACTED, text)
    return text


def redact_command(text: str) -> str:
    """Additionally hide inline environment assignments in command previews."""
    return _ENV_ASSIGN.sub(lambda m: m[1] + REDACTED, redact_text(text))


def display_value(value, *, text_limit: int = 32768, budget: int = 65536):
    """Return a JSON-safe display copy, a truncation flag and redaction flag."""
    remaining = max(0, budget)
    nodes = 0
    truncated = False
    redacted = False

    def visit(item, key="", depth=0):
        nonlocal remaining, nodes, truncated, redacted
        nodes += 1
        if depth > 16 or nodes > 1000 or remaining <= 0:
            truncated = True
            return "<truncated>"
        if _SECRET_KEY.search(key) or key.lower() in {"env", "headers", "request_headers"}:
            if item is not None:
                redacted = True
                remaining -= len(REDACTED)
                return REDACTED
        if isinstance(item, str):
            clean = redact_command(item) if key == "command" else redact_text(item)
            redacted |= clean != item
            cap = max(0, min(text_limit, remaining))
            remaining -= min(len(clean), cap)
            if len(clean) > cap:
                truncated = True
                clean = clean[:cap] + "\n<… truncated>"
            return clean
        if isinstance(item, dict):
            result = {}
            for k, v in islice(item.items(), 101):
                if len(result) >= 100 or remaining <= 0 or nodes >= 1000:
                    result["_display_truncated"] = True
                    truncated = True
                    break
                k = str(k)
                safe_key = redact_text(k)[:200]
                remaining -= len(safe_key)
                result[safe_key] = visit(v, k, depth + 1)
            return result
        if isinstance(item, list):
            result = []
            for child in islice(item, 101):
                if len(result) >= 100 or remaining <= 0 or nodes >= 1000:
                    result.append("<… truncated>")
                    truncated = True
                    break
                result.append(visit(child, depth=depth + 1))
            return result
        if item is None or isinstance(item, (bool, int, float)):
            remaining -= 16
            return item
        return visit(str(item), key, depth + 1)

    return visit(value), truncated, redacted
