"""Synthetic temporary-DB coverage of saved native policy; no production changes."""
import asyncio
import json
from types import SimpleNamespace
import pytest
from hub.file_import_settings import META_KEY


# These bridge tests use only temporary databases and synthetic source bytes.
from tests.test_native_file_ingress import relay as relay, request as native_request, inspect_request, expect_error
from hub import native_file_ingress as native
from hub.runtime import Runtime


def persist_fixture_policy(store, **values):
    store.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (META_KEY, json.dumps({"version": 1, "revision": 1, "values": values})))


@pytest.mark.parametrize("tool", ["download_artifact", "inspect_file_source"])
@pytest.mark.parametrize("gate", ["streaming_enabled", "native_relay_enabled"])
def test_saved_disable_has_no_source_request_or_legacy_dispatch(relay, tool, gate):
    persist_fixture_policy(relay.store, **{gate: False})
    calls = []
    def legacy(*args):
        calls.append(args)
        raise AssertionError("Disabled native file entry must not dispatch to Agent")
    runtime = SimpleNamespace(_loop=None, store=relay.store, _invoke=legacy)
    expect_error("FILE_IMPORT_DISABLED", Runtime._invoke_async(runtime, tool, {}, relay.principal))
    assert calls == [] and relay.downloads == []
    assert not relay.store.all("SELECT * FROM incoming_file_imports")


def test_saved_source_preview_and_actual_download_agree(relay):
    persist_fixture_policy(relay.store, native_file_hosts=[], native_file_providers=["openai_sediment"])
    file = {"file_id": "synthetic-private-id", "download_url":
            "https://sdmntprwest.oaiusercontent.com/secret-path?sig=private-secret", "size": 3}
    preview = asyncio.run(native.inspect_native_file(relay.runtime, inspect_request(file=file), relay.principal))
    assert preview["source_allowed"] is True and preview["allowed_hosts"] == []
    result = asyncio.run(native.import_native_file(relay.runtime, native_request(file=file), relay.principal))
    assert result["created"] is True
    assert relay.downloads[0][1] == tuple(preview["allowed_hosts"])
    assert relay.downloads[0][3] == tuple(preview["file_source_providers"])
    public = json.dumps({"preview": preview, "result": result})
    assert all(secret not in public for secret in ("synthetic-private-id", "secret-path", "private-secret"))


def test_saved_empty_sources_block_preview_and_download(relay):
    persist_fixture_policy(relay.store, native_file_hosts=[], native_file_providers=[])
    preview = asyncio.run(native.inspect_native_file(relay.runtime, inspect_request(), relay.principal))
    assert preview["source_allowed"] is False
    expect_error("ARTIFACT_SOURCE_DENIED",
                 native.import_native_file(relay.runtime, native_request(), relay.principal))
    assert relay.downloads == [] and relay.runtime.calls == []


def test_saved_stream_disable_during_fetch_blocks_before_agent_publication(relay, monkeypatch):
    def download(*args, **kwargs):
        persist_fixture_policy(relay.store, streaming_enabled=False)
        yield b"abc"
    monkeypatch.setattr(native, "download_chunks", download)
    expect_error("FILE_IMPORT_DISABLED",
                 native.import_native_file(relay.runtime, native_request(), relay.principal))
    assert relay.runtime.calls == []
    assert not (relay.root / native_request()["path"]).exists()
