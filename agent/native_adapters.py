"""Normalized native provider adapters and instance registry."""

from agent.native_adapter_base import (
    AdapterProtocolError,
    AdapterTransition,
    LegacyProtocolFacade,
    NativeAdapter,
    SessionFence,
)
from agent.native_adapter_claude import ClaudeAdapter
from agent.native_adapter_codex import CodexAdapter
from agent.native_adapter_pi import PiAdapter
from agent.native_adapter_registry import NativeAdapterRegistry, ProviderInstanceRegistry

__all__ = [
    "AdapterProtocolError",
    "AdapterTransition",
    "ClaudeAdapter",
    "CodexAdapter",
    "LegacyProtocolFacade",
    "NativeAdapter",
    "NativeAdapterRegistry",
    "PiAdapter",
    "ProviderInstanceRegistry",
    "SessionFence",
]
