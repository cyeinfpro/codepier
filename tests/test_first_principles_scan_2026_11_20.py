"""Regression tests for docs/FIRST_PRINCIPLES_CODE_SCAN_2026-11-20.md fixes."""

from __future__ import annotations

import asyncio
import struct
import zlib
from pathlib import Path

import pytest

from agent.core_files import CoreFiles
from agent.filesystem import FileEngine, MAX_TEXT_EDIT
from agent.journal import Journal
from shared.config import ConfigurationError
from shared.crypto import digest
from shared.util import DevError

ROOT = Path(__file__).resolve().parents[1]


# ISS-01 ------------------------------------------------------------------
def test_unified_wait_budget_bounds_first_round_and_total(monkeypatch):
    from hub import unified_tools as ut
    from hub.runtime import call_wait_budget
    from shared.unified_contracts import Plan

    seen = []

    class FakeRuntime:
        async def invoke(self, tool, args, principal):
            seen.append(call_wait_budget.get())
            return {"pending": True, "operation_id": "op"}

    plan = Plan("act", "exec", "", "exec", {"command": "true"}, wait=0)
    value, tool, _ = asyncio.run(ut.execute(FakeRuntime(), plan, None))
    # wait=0 caps the first dispatch round at zero and never starts a second wait.
    assert seen == [0.0] and value["pending"] and tool == "exec"
    assert call_wait_budget.get() is None


def test_unified_second_wait_deducts_time_already_spent():
    from hub import unified_tools as ut
    from shared.unified_contracts import Plan

    remaining = []

    class FakeRuntime:
        async def wait_for_operation(self, operation_id, seconds, cancel_event=None):
            remaining.append(seconds)

        async def invoke(self, tool, args, principal):
            return {"pending": True, "operation_id": "op"}

    plan = Plan("act", "exec", "", "exec", {}, wait=30)
    import time

    started = time.monotonic() - 12  # 12 s already consumed by the first round
    asyncio.run(ut._wait(FakeRuntime(), plan, {"operation_id": "op"}, None, started))
    assert remaining and remaining[0] <= 18.5


# ISS-02 ------------------------------------------------------------------
def test_bridge_mirrors_unified_wait_float_and_keys():
    from scripts import mcp_stdio_bridge as bridge

    assert bridge.expected_wait("act", {"action": "exec"}, "unified") == 30
    assert bridge.expected_wait("query", {"action": "job"}, "unified") == 0
    assert bridge.expected_wait("act", {"action": "exec", "wait": 15.0}, "unified") == 15.0
    assert bridge.expected_wait("act", {"action": "exec", "wait": True}, "unified") == 0
    prepared = bridge.prepare_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "act", "arguments": {"action": "exec", "command": "true"}},
        }
    )
    assert prepared["params"]["arguments"]["idempotency_key"].startswith("bridge-")
    lsp = bridge.prepare_request(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "act", "arguments": {"action": "lsp"}},
        }
    )
    assert "idempotency_key" not in lsp["params"]["arguments"]
    assert bridge.recovery_hint("unified", {"name": "act"}, {})[0] == 'query(action="job")'


# ISS-03 ------------------------------------------------------------------
def _engine(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    config_path = tmp_path / "private" / "config.json"
    config_path.parent.mkdir()
    config_path.write_text("{}")
    engine = FileEngine(
        {"allowed_roots": [{"path": str(root), "writable": True}]},
        Journal(tmp_path / "state"),
        config_path,
    )
    project = {"root": str(root), "alias": "Fixture", "mode": "write"}
    return CoreFiles(engine), project, root


def _png(size):
    header = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">II5B", 1, 1, 8, 2, 0, 0, 0)
    chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
    data = header + chunk
    return data + b"\0" * (size - len(data))


def test_large_project_image_is_asset_metadata_not_screenshot_error(tmp_path):
    files, project, root = _engine(tmp_path)
    (root / "big.png").write_bytes(_png(4 * 1024 * 1024))
    result = files.call(
        "read", project, {"path": "big.png", "offset": 1, "limit": 2000, "expected_sha256": "",
                          "options": {"include_knowledge": False}}
    )
    assert result["content_available"] is False and result["bytes"] == 4 * 1024 * 1024


def test_text_between_one_and_sixteen_mib_is_editable(tmp_path):
    files, project, root = _engine(tmp_path)
    body = ("x" * 1023 + "\n") * 2048 + "needle\n"  # ~2 MiB
    (root / "big.txt").write_text(body)
    sha = digest(body.encode())
    result = files.call(
        "edit", project,
        {"path": "big.txt", "expected_sha256": sha,
         "edits": [{"old_text": "needle", "new_text": "pin"}]},
    )
    assert (root / "big.txt").read_text().endswith("pin\n")
    assert result["sha256"] == digest((root / "big.txt").read_bytes())
    assert MAX_TEXT_EDIT == 16 * 1024 * 1024


# ISS-04 / ISS-05 ---------------------------------------------------------
def test_boolean_settings_share_one_parser(monkeypatch, tmp_path):
    from hub.gateway.service import Gateway
    from hub.store import Store
    from shared.execution_policy import hub_blocks_codex

    store = Store(tmp_path / "hub")
    try:
        monkeypatch.setenv("CODEPIER_MCP_GATEWAY", "true")
        assert Gateway(store).enabled is True
        monkeypatch.setenv("CODEPIER_MCP_GATEWAY", "maybe")
        with pytest.raises(ConfigurationError):
            Gateway(store)
    finally:
        store.close()
    monkeypatch.setenv("MCP_BLOCK_LOCAL_CODEX", "ture")  # misspelt explicit value fails closed
    assert hub_blocks_codex() is True
    monkeypatch.setenv("MCP_BLOCK_LOCAL_CODEX", "false")
    assert hub_blocks_codex() is False


@pytest.mark.parametrize("value", ["-1", "nan", "inf", "abc"])
def test_stream_settings_reject_invalid_numbers(monkeypatch, value):
    from hub.mcp_streams import Streams

    monkeypatch.setenv("CODEPIER_MCP_SSE_KEEPALIVE_INTERVAL", value)
    with pytest.raises(ConfigurationError):
        Streams()
    monkeypatch.setenv("CODEPIER_MCP_SSE_KEEPALIVE_INTERVAL", "")
    assert Streams().keepalive_interval == 5.0


# ISS-06 ------------------------------------------------------------------
def test_retired_access_defaults_card_is_gone_everywhere():
    from shared.settings_catalog import CATALOG

    ids = {item["id"] if isinstance(item, dict) else getattr(item, "id", None) for item in CATALOG}
    assert "access_defaults" not in ids
    app = (ROOT / "web/app.js").read_text()
    assert "access_defaults" not in app


# ISS-07 ------------------------------------------------------------------
@pytest.mark.parametrize(
    "address,public",
    [("8.8.8.8", True), ("::ffff:10.0.0.1", False), ("::ffff:8.8.8.8", True),
     ("240.0.0.1", False), ("fec0::1", False), ("fe80::1%en0", False), ("bogus", False)],
)
def test_one_public_unicast_rule(address, public):
    from shared.net_address import is_public_unicast_ip

    assert is_public_unicast_ip(address) is public


def test_retry_after_is_bounded():
    from hub.collaboration.weixin_protocol import retry_after
    from shared.net_address import parse_retry_after

    assert retry_after("999999999") == 86400
    assert parse_retry_after("999999999") == 86400
    assert parse_retry_after("7", max_seconds=1800) == 7
    assert parse_retry_after("soon", default=60) == 60


@pytest.mark.parametrize("kind", ["dns", "connect"])
def test_telegram_not_sent_is_retryable(kind):
    import httpx

    from hub.collaboration.telegram_protocol import TelegramAdapter
    from shared.net_address import TransportNotSent

    async def transport(*args, **kwargs):
        raise TransportNotSent("dns") if kind == "dns" else httpx.ConnectError("refused")

    adapter = TelegramAdapter("123456:" + "A" * 30, transport=transport)
    with pytest.raises(DevError) as caught:
        asyncio.run(adapter.call("sendMessage", {"chat_id": 1, "text": "x"}))
    assert caught.value.code == "TELEGRAM_TRANSPORT_NOT_SENT"
    assert caught.value.details["request_sent"] is False


# ISS-08 / ISS-11 ---------------------------------------------------------
def test_shutdown_flushes_queued_control_frames_before_close():
    from hub.runtime import _flush_control

    calls = []

    class Connection:
        async def flush_control(self, wait_seconds):
            calls.append(wait_seconds)

    asyncio.run(_flush_control(Connection()))
    asyncio.run(_flush_control(object()))  # connections without the method are ignored
    assert calls == [1.0]


def test_coalescing_queue_replaces_pending_output_in_place():
    from hub.runtime import CoalescingQueue, Runtime

    queue = CoalescingQueue(maxsize=4)
    Runtime._enqueue(queue, {"type": "operation", "data": {"id": "a"}})
    for at in range(5):
        Runtime._enqueue(queue, {"type": "output", "at": at, "data": {"id": "a"}})
    assert queue.qsize() == 2
    assert queue.get_nowait()["type"] == "operation"
    assert queue.get_nowait()["at"] == 4


# ISS-10 ------------------------------------------------------------------
def test_origin_and_oauth_defaults_derive_from_one_host_list():
    from shared.mcp_protocol import DEFAULT_OAUTH_REDIRECT_HOSTS, TRUSTED_CLIENT_HOSTS

    redirect = DEFAULT_OAUTH_REDIRECT_HOSTS.split(",")
    assert set(TRUSTED_CLIENT_HOSTS) <= set(redirect)
    assert {"platform.openai.com", "gemini.google.com", "antigravity.google.com"} <= set(
        TRUSTED_CLIENT_HOSTS
    )


# ISS-13 ------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    ["agent/relay_browser_host.py", "agent/relay_control.py",
     "scripts/relay_browser_host.py", "scripts/relay_control.py"],
)
def test_legacy_shims_propagate_exit_status(path):
    assert "raise SystemExit(main())" in (ROOT / path).read_text()


# ISS-16 ------------------------------------------------------------------
def test_lsp_pool_follows_config_and_validates_it():
    from agent.integration_config import validate_integrations
    from agent.lsp_navigation import LspPool, apply_pool_config

    config = {"integrations": validate_integrations(
        {"lsp_pool": {"max_instances": 7, "idle_ttl_seconds": 42}})}
    pool = LspPool(idle_ttl=1, max_instances=1)
    apply_pool_config(pool, config)
    assert (pool.max_instances, pool.idle_ttl) == (7, 42.0)
    with pytest.raises(ValueError):
        validate_integrations({"lsp_pool": {"max_instances": 0}})
    assert "--keys-output" in (ROOT / "deploy/k3s/deployment.yaml").read_text()


def test_dockerfile_ships_migration_and_import_helpers():
    recipe = (ROOT / "Dockerfile").read_text()
    assert "scripts/migrate_hub_data.py" in recipe and "scripts/file_import_client.py" in recipe


def test_core_file_inline_image_bound_matches_desktop_media_bound():
    from agent.core_files import MAX_INLINE_IMAGE_BYTES
    from shared.computer_media import MAX_IMAGE_BYTES

    assert MAX_INLINE_IMAGE_BYTES == MAX_IMAGE_BYTES


def test_host_updater_default_matches_shared_default():
    from scripts.panel_update_runtime import OAUTH_CURRENT_DEFAULT
    from shared.mcp_protocol import DEFAULT_OAUTH_REDIRECT_HOSTS

    assert OAUTH_CURRENT_DEFAULT == DEFAULT_OAUTH_REDIRECT_HOSTS
