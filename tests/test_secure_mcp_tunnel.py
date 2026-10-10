"""Offline tests for configuration previews, not live Tunnel certification."""
from __future__ import annotations

import builtins
import copy
import json
import os
from pathlib import Path
import shlex
import socket
import sqlite3
import subprocess

import pytest

from hub.secure_mcp_tunnel import build_preview, CONFIG_URL, GUIDE_URL

TUNNEL_ID = "tunnel_0123456789abcdef0123456789abcdef"


def example(**updates):
    return {"tunnel_id": TUNNEL_ID, "install_dir": "/opt/codepier", **updates}


def codes(result):
    return {item["code"] for item in result["errors"]}


def yaml_preview(result):
    return next(item["content"] for item in result["artifacts"] if item["name"].endswith(".yaml"))


def scalar(text, field):
    """Read our fixed generated quoted scalar; not a general YAML validator."""
    line = next(line for line in text.splitlines() if line.strip().startswith(field + ":"))
    return json.loads(line.strip().split(":", 1)[1].strip())


def test_preview_contract_and_no_claim_of_connection():
    result = build_preview(example())
    assert result["schema_version"] == 1
    assert result["route"] == "tunnel_stdio"
    assert result["mode"] == "preview_only"
    assert result["configuration_valid"] is True
    assert result["verification_status"] == "not_run"
    assert result["connection_state"] == "unknown"
    assert result["auth_mode"] == "existing_pat"
    assert result["effective_scope"] == "unverified"
    assert result["errors"] == []
    assert {item["id"] for item in result["manual_steps"]} == {
        "host", "credentials", "authorization", "save", "validate", "chatgpt"
    }
    assert {item["url"] for item in result["sources"]} >= {GUIDE_URL, CONFIG_URL}


def test_schema_is_documented_and_contains_only_secret_references():
    result = build_preview(example())
    text = yaml_preview(result)
    assert text.startswith("# Preview only.")
    assert "config_version: 1\n" in text
    assert scalar(text, "base_url") == "https://api.openai.com"
    assert scalar(text, "tunnel_id") == TUNNEL_ID
    assert scalar(text, "api_key") == "env:CONTROL_PLANE_API_KEY"
    assert scalar(text, "listen_addr") == "127.0.0.1:0"
    assert "  open_browser: false\n" in text
    assert '  commands:\n    - channel: "main"\n' in text
    assert scalar(text, "command") == "/opt/codepier/deploy/mcp-stdio.sh"
    assert "OPENAI_API_KEY" not in text
    assert "server_url" not in text
    assert "harpoon" not in text
    assert "cloudflared" not in text
    assert "oauth_trusted_origins" not in text
    env = result["artifacts"][0]
    assert env["suggested_path"] == "/opt/codepier/private/bridge.env"
    assert "CODEPIER_TOKEN_FILE=/opt/codepier/private/token.txt\n" in env["content"]
    assert "CODEPIER_FILE_IMPORT_ROOTS" not in env["content"]


def test_commands_are_explicit_manual_steps_not_profile_mutations():
    result = build_preview(example(profile="demo-2"))
    commands = {item["id"]: item for item in result["commands"]}
    expected_path = "/opt/codepier/private/codepier-tunnel-demo-2.yaml"
    assert commands["doctor"]["argv"] == [
        "env", "-u", "OPENAI_API_KEY", "tunnel-client", "doctor", "--config", expected_path, "--explain"
    ]
    assert commands["run"]["argv"] == [
        "env", "-u", "OPENAI_API_KEY", "tunnel-client", "run", "--config", expected_path
    ]
    for item in commands.values():
        assert item["requires_manual_action"] is True
        assert shlex.split(item["shell"]) == item["argv"]
        assert not set(item["argv"]) & {"init", "create", "install", "chmod", "--apply"}
        assert "--profile" not in item["argv"]


@pytest.mark.parametrize("directory", [
    "/opt/my codepier", "/opt/码头", "/opt/🚢", "/opt/owner's codepier",
    "/opt/$(touch SHOULD_NOT_RUN); echo unsafe",
    '/opt/x"; echo unsafe; #',
])
def test_paths_roundtrip_without_shell_or_yaml_injection(directory):
    result = build_preview(example(install_dir=directory))
    assert result["configuration_valid"] is True
    command = scalar(yaml_preview(result), "command")
    assert shlex.split(command) == [directory + "/deploy/mcp-stdio.sh"]
    env = result["artifacts"][0]["content"]
    token_line = next(line for line in env.splitlines() if line.startswith("CODEPIER_TOKEN_FILE="))
    assert shlex.split(token_line) == ["CODEPIER_TOKEN_FILE=" + directory + "/private/token.txt"]
    for item in result["commands"]:
        assert shlex.split(item["shell"]) == item["argv"]


@pytest.mark.parametrize("payload", [None, [], "", 1, True])
def test_non_objects_are_rejected(payload):
    result = build_preview(payload)
    assert codes(result) == {"OBJECT_REQUIRED"}
    assert result["commands"] == result["artifacts"] == []


@pytest.mark.parametrize(("field", "value", "code"), [
    ("tunnel_id", "", "INVALID_TUNNEL_ID"),
    ("tunnel_id", None, "INVALID_TUNNEL_ID"),
    ("tunnel_id", 5, "INVALID_TUNNEL_ID"),
    ("tunnel_id", "tunnel_" + "A" * 32, "INVALID_TUNNEL_ID"),
    ("tunnel_id", "tunnel_" + "a" * 31, "INVALID_TUNNEL_ID"),
    ("tunnel_id", "tunnel_" + "a" * 33, "INVALID_TUNNEL_ID"),
    ("tunnel_id", TUNNEL_ID + "\n", "INVALID_TUNNEL_ID"),
    ("profile", "../other", "INVALID_PROFILE"),
    ("profile", "--overwrite", "INVALID_PROFILE"),
    ("profile", "x\ny", "INVALID_PROFILE"),
    ("profile", "a" * 65, "INVALID_PROFILE"),
    ("profile", "x;touch", "INVALID_PROFILE"),
    ("profile", None, "INVALID_PROFILE"),
    ("install_dir", "/", "INVALID_INSTALL_DIR"),
    ("install_dir", "//host/share", "INVALID_INSTALL_DIR"),
    ("install_dir", "relative", "INVALID_INSTALL_DIR"),
    ("install_dir", "/opt/../else", "INVALID_INSTALL_DIR"),
    ("install_dir", "/opt/x\u0085y", "INVALID_INSTALL_DIR"),
    ("install_dir", "/opt/x\u2028y", "INVALID_INSTALL_DIR"),
    ("install_dir", "/opt/./here", "INVALID_INSTALL_DIR"),
    ("install_dir", "/opt/x\x00", "INVALID_INSTALL_DIR"),
    ("install_dir", "/opt/x\ny", "INVALID_INSTALL_DIR"),
    ("install_dir", "C:\\CodePier", "INVALID_INSTALL_DIR"),
    ("install_dir", None, "INVALID_INSTALL_DIR"),
    ("install_dir", ["x"], "INVALID_INSTALL_DIR"),
    ("install_dir", "/opt/" + "x" * 1024, "INVALID_INSTALL_DIR"),
    ("hub_url", "localhost:8765", "INVALID_HUB_URL"),
    ("hub_url", "ftp://localhost", "INVALID_HUB_URL"),
    ("hub_url", "https://user:pass@host", "INVALID_HUB_URL"),
    ("hub_url", "https://example.com/mcp", "INVALID_HUB_URL"),
    ("hub_url", "https://example.com/?key=value", "INVALID_HUB_URL"),
    ("hub_url", "https://example.com/#fragment", "INVALID_HUB_URL"),
    ("hub_url", "https://example.com:99999", "INVALID_HUB_URL"),
    ("hub_url", "http://192.168.1.3:8765", "HTTPS_REQUIRED"),
    ("hub_url", "http://public.example", "HTTPS_REQUIRED"),
    ("hub_url", "http://localhost.attacker.example", "HTTPS_REQUIRED"),
    ("hub_url", "http://127.0.0.1.attacker.example", "HTTPS_REQUIRED"),
    ("hub_url", "http://2130706433", "HTTPS_REQUIRED"),
    ("hub_url", "http://[::]", "HTTPS_REQUIRED"),
    ("hub_url", None, "INVALID_HUB_URL"),
    ("hub_url", {}, "INVALID_HUB_URL"),
    ("hub_url", "https://host\r\nother", "INVALID_HUB_URL"),
    ("profile", "\ud800", "INVALID_PROFILE"),
])
def test_invalid_inputs_have_no_artifacts_or_commands(field, value, code):
    result = build_preview(example(**{field: value}))
    assert code in codes(result)
    assert result["configuration_valid"] is False
    assert result["commands"] == result["artifacts"] == result["manual_steps"] == []
    assert result["connection_state"] == "unknown"


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8765", "http://127.5.4.3", "http://localhost:8765",
    "http://[::1]:8765", "https://private.example:8765", "https://例子.example",
])
def test_supported_explicit_urls(url):
    assert build_preview(example(hub_url=url))["configuration_valid"] is True


@pytest.mark.parametrize("field", ["api_key", "pat", "token", "authorization", "env", "command", "route", "unknown"])
def test_unknown_or_secret_fields_are_never_echoed(field):
    marker = "private-marker-do-not-echo"
    result = build_preview(example(**{field: marker}))
    assert "UNSUPPORTED_FIELDS" in codes(result)
    assert marker not in json.dumps(result)
    assert result["artifacts"] == result["commands"] == []


@pytest.mark.parametrize("secret", ["sk-" + "x" * 24, "rd_" + "A" * 40, "Bearer private-marker"])
def test_obvious_credentials_in_allowed_fields_are_rejected(secret):
    result = build_preview(example(install_dir="/opt/" + secret))
    assert codes(result) == {"SECRET_NOT_ACCEPTED"}
    assert secret not in json.dumps(result)
    assert result["artifacts"] == result["commands"] == []


def test_required_inputs_and_independent_result_instances():
    result = build_preview({})
    assert {"INVALID_TUNNEL_ID", "INVALID_INSTALL_DIR"} <= codes(result)
    first = build_preview(example())
    first["warnings"].clear()
    first["sources"][0]["url"] = "changed"
    second = build_preview(example())
    assert second["warnings"]
    assert second["sources"][0]["url"] == GUIDE_URL


def test_preview_does_not_read_environment_or_mutate_input(monkeypatch):
    payload = example()
    before = copy.deepcopy(payload)
    normal = build_preview(payload)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "Z" * 30)
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "sk-" + "Y" * 30)
    monkeypatch.setenv("CODEPIER_TOKEN_FILE", "/do-not-read")
    assert build_preview(payload) == normal
    assert payload == before


def test_preview_performs_no_io_or_process_activity(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Preview attempted an external side effect")
    for target, name in [
        (builtins, "open"), (os, "open"), (socket, "create_connection"),
        (socket, "getaddrinfo"), (subprocess, "Popen"), (sqlite3, "connect"),
        (Path, "read_text"), (Path, "write_text"), (Path, "exists"),
        (Path, "resolve"), (Path, "mkdir"),
    ]:
        monkeypatch.setattr(target, name, forbidden)
    assert build_preview(example())["configuration_valid"] is True
    assert build_preview(example(hub_url="https://unknown.invalid"))["configuration_valid"] is True
    assert build_preview(example(hub_url="http://unknown.invalid"))["configuration_valid"] is False


def test_safety_boundaries_are_present_even_when_input_is_invalid():
    required = {
        "PREVIEW_ONLY", "PAT_SERVICE_IDENTITY", "PERMISSIONS_SEPARATE",
        "PRIVATE_DISTRIBUTION", "STDIO_SINGLE_INSTANCE", "OAUTH_REACHABILITY",
        "RUNTIME_CREDENTIALS", "HOST_BOUNDARY", "CONFIG_PRECEDENCE",
    }
    assert {item["code"] for item in build_preview({})["warnings"]} == required
