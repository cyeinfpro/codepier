"""Provider instance persistence and fixed built-in adapter registry."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any
import uuid

from agent.native_adapter_base import AdapterProtocolError, NativeAdapter
from agent.native_adapter_claude import ClaudeAdapter
from agent.native_adapter_codex import CodexAdapter
from agent.native_adapter_pi import PiAdapter
from shared.util import atomic_json

LEGACY_UNRESOLVED_INSTANCE_ID = "legacy_unresolved"
SUPPORTED_PROVIDERS = ("pi", "codex", "claude")


@dataclass(frozen=True)
class ProviderInstanceRecord:
    """Opaque provider instance identity and monotonic revision."""

    provider_instance_id: str
    instance_revision: int
    provider_kind: str


def _digest_parts(*parts: str) -> str:
    payload = "\0".join(parts).encode("utf-8")
    return sha256(payload).hexdigest()


class ProviderInstanceRegistry:
    """Persist opaque 32-hex provider instance IDs in <native_dir>/provider-instances.json."""

    def __init__(self, native_dir: Path | str) -> None:
        self.native_dir = Path(native_dir)
        self.path = self.native_dir / "provider-instances.json"
        self._records: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        if self.path.is_symlink():
            raise ValueError("Provider instance registry cannot be a symlink")
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict) and isinstance(raw.get("instances"), dict):
            self._records = dict(raw["instances"])

    def _save(self) -> None:
        self.native_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.native_dir, 0o700)
        atomic_json(self.path, {"schema_version": 1, "instances": self._records})
        os.chmod(self.path, 0o600)

    @staticmethod
    def compute_config_digest(executable_path: str, config_fingerprint: str = "") -> str:
        """Hash executable and config fingerprint without storing raw paths or secrets."""
        return _digest_parts("cfg", executable_path, config_fingerprint)

    @staticmethod
    def _binding_key(
        execution_account: str, provider_kind: str, executable_and_config_digest: str
    ) -> str:
        if provider_kind not in SUPPORTED_PROVIDERS:
            raise ValueError(f"Unsupported provider_kind: {provider_kind}")
        return _digest_parts(execution_account, provider_kind, executable_and_config_digest)

    def resolve(
        self,
        *,
        execution_account: str,
        provider_kind: str,
        executable_and_config_digest: str,
        existing_instance_id: str | None = None,
    ) -> ProviderInstanceRecord:
        """Return existing or newly registered opaque 32-hex provider_instance_id + revision."""
        key = self._binding_key(execution_account, provider_kind, executable_and_config_digest)
        existing = self._records.get(key)
        if existing is not None:
            return ProviderInstanceRecord(
                provider_instance_id=existing["provider_instance_id"],
                instance_revision=existing["instance_revision"],
                provider_kind=provider_kind,
            )
        if existing_instance_id is not None:
            record = self._bump_existing_instance(key, provider_kind, existing_instance_id)
            self._save()
            return record
        instance_id = uuid.uuid4().hex
        self._records[key] = {
            "provider_instance_id": instance_id,
            "instance_revision": 1,
            "provider_kind": provider_kind,
        }
        self._save()
        return ProviderInstanceRecord(
            provider_instance_id=instance_id,
            instance_revision=1,
            provider_kind=provider_kind,
        )

    def _bump_existing_instance(
        self, new_key: str, provider_kind: str, existing_instance_id: str
    ) -> ProviderInstanceRecord:
        max_rev = 0
        for old_key, row in list(self._records.items()):
            if row.get("provider_instance_id") == existing_instance_id:
                if row.get("provider_kind") != provider_kind:
                    raise ValueError("Cannot reassign provider_instance_id across provider_kind")
                max_rev = max(max_rev, int(row.get("instance_revision", 1)))
                self._records.pop(old_key)
        if max_rev == 0:
            raise ValueError("Unknown existing provider_instance_id")
        new_rev = max_rev + 1
        self._records[new_key] = {
            "provider_instance_id": existing_instance_id,
            "instance_revision": new_rev,
            "provider_kind": provider_kind,
        }
        return ProviderInstanceRecord(
            provider_instance_id=existing_instance_id,
            instance_revision=new_rev,
            provider_kind=provider_kind,
        )

    def resolve_legacy_session(self, session_row: Mapping[str, Any]) -> str:
        """Return persisted instance ID if verifiable, otherwise 'legacy_unresolved'."""
        instance_id = session_row.get("provider_instance_id")
        if isinstance(instance_id, str) and len(instance_id) == 32:
            if any(row.get("provider_instance_id") == instance_id for row in self._records.values()):
                return instance_id
        return LEGACY_UNRESOLVED_INSTANCE_ID


class NativeAdapterRegistry:
    """Fixed registry supporting only pi, codex, and claude (no dynamic remote plugins)."""

    _ADAPTERS: dict[str, type[NativeAdapter]] = {
        "pi": PiAdapter,
        "codex": CodexAdapter,
        "claude": ClaudeAdapter,
    }

    @classmethod
    def supported_providers(cls) -> tuple[str, ...]:
        return SUPPORTED_PROVIDERS

    @classmethod
    def create(
        cls,
        provider_kind: str,
        *,
        provider_instance_id: str,
        instance_revision: int = 1,
        provider_version: str = "unknown",
        platform_supported: bool = True,
        platform_reason: str | None = None,
        **kwargs: Any,
    ) -> NativeAdapter:
        adapter_cls = cls._ADAPTERS.get(provider_kind)
        if adapter_cls is None:
            raise AdapterProtocolError(
                "UNSUPPORTED_PROVIDER",
                f"Provider {provider_kind!r} is not in fixed built-in registry",
            )
        return adapter_cls(
            provider_instance_id=provider_instance_id,
            instance_revision=instance_revision,
            provider_version=provider_version,
            platform_supported=platform_supported,
            platform_reason=platform_reason,
            **kwargs,
        )
