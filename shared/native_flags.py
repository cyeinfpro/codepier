"""Feature flags for CodePier T3 core adoption rollout.

All new execution, snapshot read, viewport, adapter, restore, and Windows
structured-chat paths remain disabled by default until explicitly enabled.
Only read-only shadow projection comparison is enabled by default.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from shared.config import env_bool


@dataclass(frozen=True)
class NativeFeatureFlags:
    """Explicit rollout switches for native session reliability & view v2."""

    native_capture_v2: bool = False
    native_projection_shadow: bool = True
    native_snapshot_v2_read: bool = False
    native_log_on_demand: bool = False
    native_viewport_v2: bool = False
    native_provider_adapter_v2: bool = False
    native_workspace_restore_v2: bool = False
    native_windows_structured_chat: bool = False

    @classmethod
    def from_env(cls) -> NativeFeatureFlags:
        return cls(
            native_capture_v2=env_bool("CODEPIER_NATIVE_CAPTURE_V2", False),
            native_projection_shadow=env_bool("CODEPIER_NATIVE_PROJECTION_SHADOW", True),
            native_snapshot_v2_read=env_bool("CODEPIER_NATIVE_SNAPSHOT_V2_READ", False),
            native_log_on_demand=env_bool("CODEPIER_NATIVE_LOG_ON_DEMAND", False),
            native_viewport_v2=env_bool("CODEPIER_NATIVE_VIEWPORT_V2", False),
            native_provider_adapter_v2=env_bool("CODEPIER_NATIVE_PROVIDER_ADAPTER_V2", False),
            native_workspace_restore_v2=env_bool("CODEPIER_NATIVE_WORKSPACE_RESTORE_V2", False),
            native_windows_structured_chat=env_bool(
                "CODEPIER_NATIVE_WINDOWS_STRUCTURED_CHAT", False
            ),
        )

    def with_overrides(self, overrides: Mapping[str, Any] | None = None) -> NativeFeatureFlags:
        """Return a copy with explicit boolean overrides applied."""
        if not overrides:
            return self
        known = set(self.__dataclass_fields__)
        clean: dict[str, bool] = {}
        for key, value in overrides.items():
            if key not in known:
                raise ValueError(f"Unknown native feature flag: {key}")
            if type(value) is not bool:
                raise TypeError(f"Native feature flag {key} must be a strict bool")
            clean[key] = value
        return replace(self, **clean)

    def as_dict(self) -> dict[str, bool]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


def resolve_native_flags(
    flags: NativeFeatureFlags | Mapping[str, Any] | None = None,
) -> NativeFeatureFlags:
    """Resolve feature flags from explicit instance, mapping overrides, or env."""
    if isinstance(flags, NativeFeatureFlags):
        return flags
    base = NativeFeatureFlags.from_env()
    if flags is None:
        return base
    return base.with_overrides(flags)
