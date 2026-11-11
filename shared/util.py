from __future__ import annotations
import json
import math
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from shared.audit_redaction import redact_command, redact_text

VERSION = "1.33.15"


class DevError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, **details):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.details = details


def normalize_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("地址必须是 HTTP(S) 根地址文字")
    value = value.strip()
    if (
        not value
        or not valid_json_value(value)
        or any(c.isspace() or ord(c) < 32 or ord(c) == 127 or c == "\\" for c in value)
    ):
        raise ValueError("地址不可包含空白、控制字符或反斜杠")
    if "://" not in value:
        value = "http://" + value
    u = urlsplit(value)
    if u.scheme not in {"http", "https"} or not u.hostname or u.username or u.password:
        raise ValueError("地址必须是 http(s)://主机:端口，不可包含账号密码")
    if u.query or u.fragment or u.path not in {"", "/"}:
        raise ValueError("请填写面板根地址，不要添加路径、查询参数或 /mcp")
    try:
        port = u.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid port")
    except ValueError as exc:
        raise ValueError("端口应在 1–65535 之间") from exc
    # HTTP headers (including the OAuth challenge) cannot carry Unicode hosts.
    # Canonicalize IDNs once so both Agent URLs and advertised resource IDs agree.
    try:
        hostname = u.hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("无效的主机名称") from exc
    authority = f"[{hostname}]" if ":" in hostname else hostname
    if port is not None:
        authority += f":{port}"
    return urlunsplit((u.scheme, authority, "", "", ""))


def valid_json_value(value) -> bool:
    """Validate JSON at trust boundaries before UTF-8, SQLite or URL handling.

    Python's JSON decoder accepts lone surrogates and non-finite floats. Neither
    can safely round-trip through our database and HTTP responses. Iterate so
    hostile nesting cannot overflow the validator's own Python call stack.
    """
    # Keep only one iterator per nesting level; a wide 6 MiB request must not
    # allocate millions of additional (child, depth) tuples during validation.
    pending = [(iter((value,)), 0)]
    while pending:
        iterator, depth = pending[-1]
        try:
            item = next(iterator)
        except StopIteration:
            pending.pop()
            continue
        if depth > 100:
            return False
        if item is None or type(item) in (bool, int):
            continue
        if isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeError:
                return False
        elif type(item) is float:
            if not math.isfinite(item):
                return False
        elif isinstance(item, list):
            pending.append((iter(item), depth + 1))
        elif isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                return False
            pending.append(((child for pair in item.items() for child in pair), depth + 1))
        else:
            return False
    return True


def fsync_directory(directory: Path) -> None:
    """Persist directory entries after publication on POSIX filesystems."""
    if os.name == "nt":
        return  # Windows does not expose directory fsync through os.open.
    fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".codepier-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        fsync_directory(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def safe_summary(value, limit: int = 500):
    """Do not copy file contents, credentials or command output into audit inputs."""
    if isinstance(value, dict):
        if "download_url" in value and "file_id" in value:
            from shared.file_sources import source_metadata

            return {
                "native_file": "<private file reference>",
                "size": safe_summary(value.get("size"), limit),
                **source_metadata(value["download_url"]),
            }
        private_chunk = "upload_id" in value
        action = value.get("action")
        private_input = isinstance(action, str) and action in {"fill", "key", "navigate", "select"}
        native_type = value.get("type")
        native_input = isinstance(native_type, str) and native_type in {
            "type_text",
            "set_value",
            "select_text",
            "press_key",
            "perform_secondary_action",
            "click",
            "drag",
            "scroll",
        }
        result = {}
        for key, item in value.items():
            # Every request shape goes through the same credential filter.
            if any(
                part in str(key).lower()
                for part in (
                    "content",
                    "secret",
                    "token",
                    "password",
                    "old_text",
                    "new_text",
                    "verifier",
                    "passwd",
                    "passphrase",
                    "api_key",
                    "apikey",
                    "api-key",
                    "authorization",
                    "cookie",
                    "credential",
                    "private_key",
                    "headers",
                )
            ):
                result[key] = f"<{len(str(item))} chars>"
            elif key == "env" and isinstance(item, dict):
                result[key] = {name: "<redacted>" for name in item}
            elif private_chunk and key in {"data", "data_base64"}:
                result[key] = "<private binary chunk>"
            elif private_input and key in {"value", "url"}:
                result[key] = "<private input>"
            elif native_input and key in {"text", "value", "prefix", "suffix", "key"}:
                result[key] = f"<{len(str(item))} chars>"
            else:
                if key == "command" and isinstance(item, str):
                    item = redact_command(item)
                result[key] = safe_summary(item, limit)
        return result
    if isinstance(value, list):
        return [safe_summary(v, limit) for v in value[:30]]
    if isinstance(value, str):
        value = redact_text(value)
        return value[:limit] + ("…" if len(value) > limit else "")
    return value
