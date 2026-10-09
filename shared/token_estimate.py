"""Bounded, model-agnostic estimates of included MCP payload text.

This is not a tokenizer or billing meter. Only integer measurements leave this
module. JSON is normalized, excluded fields/URLs/media are not counted, and a
bounded prefix is never extrapolated to an unseen complete payload.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata

VERSION = "codepier-text-v1"
MAX_CHARACTERS = 262_144
MAX_NODES = 10_000
MAX_DEPTH = 32
NUMBERS = ("estimated_tokens", "low", "high", "characters", "utf8_bytes")
# Whole fields are excluded, including their JSON keys. All URLs are excluded:
# this deliberately avoids trying to distinguish private signed URL formats.
EXCLUDED_KEYS = frozenset({
    "_meta", "authorization", "proxyauthorization", "headers", "cookie",
    "cookies", "setcookie", "password", "passwd", "secret", "secrets",
    "token", "accesstoken", "refreshtoken", "idtoken", "apikey",
    "credentials", "credential", "privatekey", "clientsecret",
    "downloadurl", "uploadurl", "signedurl", "file", "files",
    "base64", "contentbase64", "chunkbase64", "datauri", "blob",
    "inputschema", "outputschema", "schema",
})
URL = re.compile(r"(?:https?://|data:)[^\s\"'<>]+", re.I)
ENCODED = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{128,}={0,2}(?![A-Za-z0-9+/=_-])")
BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.I)


def unavailable():
    return {"state": "unavailable", **dict.fromkeys(NUMBERS), "excluded_fields": 0,
            "truncated": False, "source_truncated": False}


def _excluded(key):
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return key == "_meta" or normalized in EXCLUDED_KEYS or normalized.endswith(
        ("password", "secret", "accesstoken", "refreshtoken", "apikey", "base64"))


class Projection:
    def __init__(self):
        self.parts = []
        self.characters = 0
        self.scanned_characters = 0
        self.nodes = 0
        self.excluded = 0
        self.truncated = False
        self.source_truncated = False

    def append(self, text):
        remaining = MAX_CHARACTERS - self.characters
        if len(text) > remaining:
            text = text[:remaining]
            self.truncated = True
        if text:
            self.parts.append(text)
            self.characters += len(text)

    def clean_text(self, text):
        # Never scan an unbounded string. Exclusions can only reduce this prefix.
        remaining = max(0, MAX_CHARACTERS - self.scanned_characters)
        if len(text) > remaining:
            self.truncated = True
            text = text[:remaining]
        self.scanned_characters += len(text)
        for pattern in (URL, ENCODED, BEARER):
            text, count = pattern.subn("", text)
            self.excluded += count
        return text

    def text(self, text):
        # JSON text blocks mirror structuredContent in CodePier. Normalize once
        # so credentials/media nested inside their string encoding are excluded.
        if text.lstrip().startswith(("{", "[")) and len(text) > MAX_CHARACTERS:
            # Do not treat an incomplete JSON prefix as opaque prose: it could
            # contain a credential field whose boundary lies outside the cap.
            self.truncated = True
            return
        if len(text) <= MAX_CHARACTERS and text.lstrip().startswith(("{", "[")):
            try:
                value = json.loads(text)
            except (ValueError, RecursionError):
                pass
            else:
                self.value(value)
                return
        self.append(self.clean_text(text))

    def value(self, value, depth=0):
        self.nodes += 1
        if self.nodes > MAX_NODES or depth > MAX_DEPTH or self.characters >= MAX_CHARACTERS:
            self.truncated = True
            return
        if isinstance(value, dict):
            if value.get("type") in ("image", "audio") or "blob" in value:
                self.excluded += 1
                return
            self.append("{")
            first = True
            for key, item in value.items():
                self.nodes += 1
                if self.nodes >= MAX_NODES or self.characters >= MAX_CHARACTERS:
                    self.truncated = True
                    break
                if not isinstance(key, str) or _excluded(key) or (key == "data" and "upload_id" in value and "chunk_sha256" in value):
                    self.excluded += 1
                    continue
                if key in {"truncated", "output_truncated", "diff_truncated", "content_truncated"} and item is True:
                    self.source_truncated = True
                if not first:
                    self.append(",")
                first = False
                self.append(json.dumps(self.clean_text(key), ensure_ascii=False) + ":")
                self.value(item, depth + 1)
            self.append("}")
        elif isinstance(value, (list, tuple)):
            self.append("[")
            for index, item in enumerate(value):
                if self.nodes >= MAX_NODES or self.characters >= MAX_CHARACTERS:
                    self.truncated = True
                    break
                if index:
                    self.append(",")
                self.value(item, depth + 1)
            self.append("]")
        elif isinstance(value, str):
            self.append(json.dumps(self.clean_text(value), ensure_ascii=False))
        elif value is None or isinstance(value, (bool, int, float)):
            self.append(json.dumps(value, ensure_ascii=False, allow_nan=False))
        else:
            self.excluded += 1

    def metric(self):
        text = "".join(self.parts)
        weight = 0.0
        for character in text:
            code = ord(character)
            if code < 128:
                weight += .28 if character.isalnum() else .10 if character.isspace() else .60
            elif unicodedata.east_asian_width(character) in {"W", "F"}:
                weight += 1.30 if code < 0x1F000 else 2.50
            else:
                weight += .90
        estimate = math.ceil(weight)
        return {"state": "partial" if self.truncated else "available",
                "estimated_tokens": estimate, "low": math.ceil(weight * .5),
                "high": math.ceil(weight * 2), "characters": len(text),
                "utf8_bytes": len(text.encode("utf-8")),
                "excluded_fields": self.excluded, "truncated": self.truncated,
                "source_truncated": self.source_truncated}


def estimate_input(arguments):
    if not isinstance(arguments, dict):
        return unavailable()
    projection = Projection()
    projection.value(arguments)
    try:
        return projection.metric()
    except UnicodeError:
        return unavailable()


def estimate_output(result):
    if not isinstance(result, dict):
        return unavailable()
    projection = Projection()
    content = result.get("content")
    found_text = False
    if isinstance(content, list):
        for block in content:
            if projection.nodes >= MAX_NODES or projection.characters >= MAX_CHARACTERS:
                projection.truncated = True
                break
            projection.nodes += 1
            if not isinstance(block, dict):
                projection.excluded += 1
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                if len(block["text"]) > MAX_CHARACTERS and block["text"].lstrip().startswith(("{", "[")) and "structuredContent" in result:
                    # Use the already materialized object without reparsing an
                    # oversized JSON mirror or scanning a raw sensitive prefix.
                    projection.value(result["structuredContent"])
                else:
                    projection.text(block["text"])
                found_text = True
            elif block.get("type") == "resource" and isinstance(block.get("resource"), dict) and isinstance(block["resource"].get("text"), str):
                projection.text(block["resource"]["text"])
                found_text = True
            else:
                projection.excluded += 1
        # Empty or media-only content is observed zero text, not missing data.
        if not found_text and "structuredContent" in result:
            projection.value(result["structuredContent"])
    elif "structuredContent" in result:
        projection.value(result["structuredContent"])
    else:
        return unavailable()
    # Do not count a mirrored structuredContent again. Distinct text blocks,
    # including repeated blocks, all remain part of this materialized response.
    try:
        return projection.metric()
    except UnicodeError:
        return unavailable()


def usage(input_metric=None, output_metric=None):
    return {"version": VERSION, "kind": "estimate", "scope": "project_tool_payload",
            "input": input_metric or unavailable(), "output": output_metric or unavailable(),
            "actual_usage": None}


def summarize(rows):
    directions = {}
    for direction in ("input", "output"):
        metrics = [(row.get("token_usage") or {}).get(direction) or unavailable() for row in rows]
        measured = [metric for metric in metrics if metric["state"] != "unavailable"]
        directions[direction] = {
            **{key: sum(metric[key] for metric in measured) if measured else None for key in NUMBERS},
            "measured_attempts": len(measured), "unavailable_attempts": len(metrics) - len(measured),
            "partial_attempts": sum(metric["state"] == "partial" for metric in measured),
            "source_truncated_attempts": sum(metric["source_truncated"] for metric in measured),
        }
    return {"version": VERSION, "kind": "estimate", "scope": "returned_activity_page",
            "wire_attempts": len(rows),
            "measured_attempts": sum(bool(row.get("token_usage")) for row in rows),
            "unavailable_attempts": sum(not row.get("token_usage") for row in rows),
            "distinct_server_operation_ids": len({row["operation_id"] for row in rows if row.get("operation_id")}),
            **directions, "actual_usage": None}
