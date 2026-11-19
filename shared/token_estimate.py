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

from shared.token_cost import (
    DEFAULT_CACHE_READ_PERCENT,
    DEFAULT_REFERENCE_MODEL,
    combined_total,
    reference_cost,
)

VERSION = "codepier-text-v1"
MAX_CHARACTERS = 262_144
MAX_NODES = 10_000
MAX_DEPTH = 32
NUMBERS = ("estimated_tokens", "low", "high", "characters", "utf8_bytes")
# Whole fields are excluded, including their JSON keys. All URLs are excluded:
# this deliberately avoids trying to distinguish private signed URL formats.
EXCLUDED_KEYS = frozenset(
    {
        "authorization",
        "proxyauthorization",
        "headers",
        "cookie",
        "cookies",
        "setcookie",
        "password",
        "passwd",
        "secret",
        "secrets",
        "token",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "apikey",
        "credentials",
        "credential",
        "privatekey",
        "clientsecret",
        "downloadurl",
        "uploadurl",
        "signedurl",
        "file",
        "base64",
        "contentbase64",
        "chunkbase64",
        "datauri",
        "blob",
    }
)
URL = re.compile(r"(?:https?://|data:)[^\s\"'<>]+", re.I)
ENCODED = re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{128,}={0,2}(?![A-Za-z0-9+/=_-])")
BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.I)


def unavailable():
    return {
        "state": "unavailable",
        **dict.fromkeys(NUMBERS),
        "wire_tokens_est": None,
        "semantic_tokens_est": None,
        "mirror_overhead_ratio": None,
        "excluded_fields": 0,
        "truncated": False,
        "source_truncated": False,
    }


def _excluded(key):
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in EXCLUDED_KEYS or normalized.endswith(
        ("password", "secret", "accesstoken", "refreshtoken", "apikey", "base64")
    )


class Projection:
    def __init__(self, *, output=False):
        self.output = output
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
                if (
                    not isinstance(key, str)
                    or _excluded(key)
                    or (key == "data" and "upload_id" in value and "chunk_sha256" in value)
                ):
                    self.excluded += 1
                    continue
                if (
                    key in {"truncated", "output_truncated", "diff_truncated", "content_truncated"}
                    and item is True
                ):
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

    def _text_and_weight(self):
        text = "".join(self.parts)
        weight = 0.0
        for character in text:
            code = ord(character)
            if code < 128:
                weight += 0.28 if character.isalnum() else 0.10 if character.isspace() else 0.60
            elif unicodedata.east_asian_width(character) in {"W", "F"}:
                weight += 1.30 if code < 0x1F000 else 2.50
            else:
                weight += 0.90
        return text, weight

    def metric(self):
        text, weight = self._text_and_weight()
        estimate = math.ceil(weight)
        return {
            "state": "partial" if self.truncated else "available",
            "estimated_tokens": estimate,
            "low": math.ceil(weight * 0.5),
            "high": math.ceil(weight * 2),
            "characters": len(text),
            "utf8_bytes": len(text.encode("utf-8")),
            "excluded_fields": self.excluded,
            "truncated": self.truncated,
            "source_truncated": self.source_truncated,
        }


def _combine_projections(projections):
    weight = 0.0
    characters = 0
    utf8_bytes = 0
    excluded = 0
    truncated = False
    source_truncated = False
    for proj in projections:
        if proj is None:
            continue
        text, part_weight = proj._text_and_weight()
        weight += part_weight
        characters += len(text)
        utf8_bytes += len(text.encode("utf-8"))
        excluded += proj.excluded
        truncated |= proj.truncated
        source_truncated |= proj.source_truncated
    estimate = math.ceil(weight)
    return {
        "state": "partial" if truncated else "available",
        "estimated_tokens": estimate,
        "low": math.ceil(weight * 0.5),
        "high": math.ceil(weight * 2),
        "characters": characters,
        "utf8_bytes": utf8_bytes,
        "excluded_fields": excluded,
        "truncated": truncated,
        "source_truncated": source_truncated,
    }


def _is_meta_stub(result, structured):
    if not isinstance(structured, dict):
        return False
    if structured.get("content_in") == "text" or structured.get("output_in") == "text":
        return True
    if structured.get("batch") is True and isinstance(structured.get("files"), list):
        return any(
            isinstance(row, dict) and row.get("content_in") == "text" for row in structured["files"]
        )
    if result.get("isError") and set(structured) <= {
        "error_code",
        "recovery_summary",
        "next_call",
        "content_in",
    }:
        return True
    return False


def estimate_input(arguments):
    if not isinstance(arguments, dict):
        return unavailable()
    projection = Projection()
    projection.value(arguments)
    try:
        metric = projection.metric()
        est = metric["estimated_tokens"]
        metric["wire_tokens_est"] = est
        metric["semantic_tokens_est"] = est
        metric["mirror_overhead_ratio"] = round(est / max(1, est), 4)
        return metric
    except UnicodeError:
        return unavailable()


def estimate_output(result):
    if not isinstance(result, dict):
        return unavailable()
    content = result.get("content")
    has_structured = "structuredContent" in result
    if not isinstance(content, list) and not has_structured:
        return unavailable()

    c0_proj = None
    rest_proj = None
    media_excluded = 0
    found_text = False
    if isinstance(content, list):
        for block in content:
            target = rest_proj
            if not found_text:
                probe = Projection(output=True)
                target = probe
            else:
                if target is None:
                    rest_proj = Projection(output=True)
                    target = rest_proj
            if target.nodes >= MAX_NODES or target.characters >= MAX_CHARACTERS:
                target.truncated = True
                if not found_text:
                    media_excluded += target.excluded
                break
            target.nodes += 1
            if not isinstance(block, dict):
                if found_text:
                    target.excluded += 1
                else:
                    media_excluded += 1
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                if (
                    len(block["text"]) > MAX_CHARACTERS
                    and block["text"].lstrip().startswith(("{", "["))
                    and has_structured
                    and not _is_meta_stub(result, result["structuredContent"])
                ):
                    target.value(result["structuredContent"])
                else:
                    target.text(block["text"])
                if not found_text:
                    c0_proj = target
                    c0_proj.excluded += media_excluded
                    media_excluded = 0
                    found_text = True
            elif (
                block.get("type") == "resource"
                and isinstance(block.get("resource"), dict)
                and isinstance(block["resource"].get("text"), str)
            ):
                target.text(block["resource"]["text"])
                if not found_text:
                    c0_proj = target
                    c0_proj.excluded += media_excluded
                    media_excluded = 0
                    found_text = True
            else:
                if found_text:
                    target.excluded += 1
                else:
                    media_excluded += 1

    if not found_text and media_excluded and rest_proj is None:
        rest_proj = Projection(output=True)
        rest_proj.excluded = media_excluded

    sc_proj = None
    if has_structured:
        sc_proj = Projection(output=True)
        sc_proj.value(result["structuredContent"])

    meta_proj = None
    if "_meta" in result:
        meta_proj = Projection(output=True)
        meta_proj.value(result["_meta"])

    try:
        wire_metric = _combine_projections((c0_proj, rest_proj, sc_proj, meta_proj))
        if c0_proj is None:
            sem_parts = (rest_proj, sc_proj)
        elif sc_proj is None:
            sem_parts = (c0_proj, rest_proj)
        else:
            c0_metric = c0_proj.metric()
            sc_metric = sc_proj.metric()
            primary = (
                c0_proj
                if _is_meta_stub(result, result["structuredContent"])
                or c0_metric["estimated_tokens"] >= sc_metric["estimated_tokens"]
                else sc_proj
            )
            sem_parts = (primary, rest_proj)
        sem_metric = _combine_projections(sem_parts)
    except UnicodeError:
        return unavailable()

    wire_tokens = wire_metric["estimated_tokens"]
    semantic_tokens = sem_metric["estimated_tokens"]
    wire_metric["source_truncated"] = (
        wire_metric["source_truncated"] or sem_metric["source_truncated"]
    )
    wire_metric["wire_tokens_est"] = wire_tokens
    wire_metric["semantic_tokens_est"] = semantic_tokens
    wire_metric["mirror_overhead_ratio"] = round(wire_tokens / max(1, semantic_tokens), 4)
    return wire_metric


def usage(input_metric=None, output_metric=None):
    return {
        "version": VERSION,
        "kind": "estimate",
        "scope": "project_tool_payload",
        "input": input_metric or unavailable(),
        "output": output_metric or unavailable(),
        "actual_usage": None,
    }


def summarize(
    rows, *, cache_read_percent=DEFAULT_CACHE_READ_PERCENT, reference_model=DEFAULT_REFERENCE_MODEL
):
    directions = {}
    for direction in ("input", "output"):
        metrics = [(row.get("token_usage") or {}).get(direction) or unavailable() for row in rows]
        measured = [metric for metric in metrics if metric["state"] != "unavailable"]
        wire_sum = (
            sum(
                metric["wire_tokens_est"]
                if isinstance(metric.get("wire_tokens_est"), int)
                else metric["estimated_tokens"]
                for metric in measured
            )
            if measured
            else None
        )
        sem_sum = (
            sum(
                metric["semantic_tokens_est"]
                if isinstance(metric.get("semantic_tokens_est"), int)
                else metric["estimated_tokens"]
                for metric in measured
            )
            if measured
            else None
        )
        directions[direction] = {
            **{
                key: sum(metric[key] for metric in measured) if measured else None
                for key in NUMBERS
            },
            "wire_tokens_est": wire_sum,
            "semantic_tokens_est": sem_sum,
            "mirror_overhead_ratio": (
                round(wire_sum / max(1, sem_sum), 4)
                if wire_sum is not None and sem_sum is not None
                else None
            ),
            "measured_attempts": len(measured),
            "unavailable_attempts": len(metrics) - len(measured),
            "partial_attempts": sum(metric["state"] == "partial" for metric in measured),
            "source_truncated_attempts": sum(metric["source_truncated"] for metric in measured),
        }
    result = {
        "version": VERSION,
        "kind": "estimate",
        "scope": "returned_activity_page",
        "wire_attempts": len(rows),
        "measured_attempts": sum(bool(row.get("token_usage")) for row in rows),
        "unavailable_attempts": sum(not row.get("token_usage") for row in rows),
        "distinct_server_operation_ids": len(
            {row["operation_id"] for row in rows if row.get("operation_id")}
        ),
        **directions,
        "actual_usage": None,
    }
    result["total"] = combined_total(result)
    result["reference_cost"] = reference_cost(result, cache_read_percent, reference_model)
    return result
