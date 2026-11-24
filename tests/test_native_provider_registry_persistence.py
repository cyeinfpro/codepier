"""Regression coverage for provider bindings shared by main and chat workers."""

from concurrent.futures import ProcessPoolExecutor
import json
import multiprocessing
from pathlib import Path

import pytest

from agent.native_adapter_registry import ProviderInstanceRegistry


def _binding(kind, config="fixture"):
    return {
        "execution_account": "local",
        "provider_kind": kind,
        "executable_and_config_digest": config,
    }


def _resolve_process(state, kind, config):
    record = ProviderInstanceRegistry(state).resolve(**_binding(kind, config))
    return record.provider_instance_id


def test_two_live_registries_preserve_each_others_bindings(tmp_path):
    agent = ProviderInstanceRegistry(tmp_path)
    worker = ProviderInstanceRegistry(tmp_path)
    first = agent.resolve(**_binding("pi"))
    second = worker.resolve(**_binding("codex"))
    assert agent.resolve_legacy_session({"provider_instance_id": second.provider_instance_id}) == (
        second.provider_instance_id
    )
    assert worker.resolve(**_binding("pi")) == first
    assert ProviderInstanceRegistry(tmp_path).resolve(**_binding("pi")) == first


def test_processes_resolve_same_binding_and_preserve_distinct_bindings(tmp_path):
    state = str(tmp_path)
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as pool:
        futures = [
            pool.submit(_resolve_process, state, "codex", config)
            for config in ("shared", "a", "shared", "b", "shared", "c", "shared", "d")
        ]
        ids = [future.result(timeout=20) for future in futures]
    assert len(set(ids[::2])) == 1
    assert len(set(ids)) == 5
    registry = ProviderInstanceRegistry(tmp_path)
    for config, instance_id in zip(("shared", "a", "b", "c", "d"), (ids[0], *ids[1::2])):
        assert registry.lookup(**_binding("codex", config)).provider_instance_id == instance_id


def test_lookup_never_creates_state_and_reads_legacy_json(tmp_path):
    path = tmp_path / "absent"
    registry = ProviderInstanceRegistry(path)
    assert registry.lookup(**_binding("pi")) is None
    assert not path.exists()
    path.mkdir()
    key = registry._binding_key("local", "pi", "fixture")
    old = {
        "schema_version": 1,
        "instances": {
            key: {
                "provider_instance_id": "f" * 32,
                "instance_revision": 5,
                "provider_kind": "pi",
            }
        },
    }
    registry.path.write_text(json.dumps(old), encoding="utf-8")
    before = registry.path.read_bytes()
    assert registry.lookup(**_binding("pi")).instance_revision == 5
    assert registry.path.read_bytes() == before
    assert set(path.iterdir()) == {registry.path}


def test_rebind_is_atomic_and_occupied_target_keeps_both_bindings(tmp_path):
    original = ProviderInstanceRegistry(tmp_path)
    old = original.resolve(**_binding("codex", "before"))
    other = original.resolve(**_binding("codex", "occupied"))
    with pytest.raises(ValueError, match="already belongs"):
        original.resolve(
            **_binding("codex", "occupied"), existing_instance_id=old.provider_instance_id
        )
    competing = ProviderInstanceRegistry(tmp_path)
    moved = competing.resolve(
        **_binding("codex", "after"), existing_instance_id=old.provider_instance_id
    )
    assert moved.provider_instance_id == old.provider_instance_id
    assert moved.instance_revision == old.instance_revision + 1
    assert original.lookup(**_binding("codex", "before")) is None
    assert original.lookup(**_binding("codex", "occupied")) == other
    assert original.lookup(**_binding("codex", "after")) == moved


def test_unreadable_or_malformed_state_is_not_erased(tmp_path):
    registry = ProviderInstanceRegistry(tmp_path)
    corrupt = b'{"instances": []}'
    Path(registry.path).write_bytes(corrupt)
    with pytest.raises(ValueError, match="Invalid provider instance registry"):
        registry.resolve(**_binding("pi"))
    assert registry.path.read_bytes() == corrupt
