"""Validate Hub process settings before acquiring or migrating persistent state."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from shared.config import DEFAULT_HUB_PORT, DEFAULT_PUBLIC_URL, ConfigurationError, RuntimeConfig, env_int, env_timezone
from shared.util import normalize_url


@dataclass(frozen=True)
class HubConfig:
    port: int
    timezone: ZoneInfo
    public_url: str
    runtime: RuntimeConfig

    @classmethod
    def from_env(cls) -> HubConfig:
        port = env_int("HUB_PORT", DEFAULT_HUB_PORT, 1, 65535)
        timezone = env_timezone()
        setting = "MCP_PUBLIC_URL" if os.getenv("MCP_PUBLIC_URL") else "HUB_PUBLIC_URL"
        try:
            public_url = normalize_url(os.getenv(setting) or DEFAULT_PUBLIC_URL)
        except ValueError:
            raise ConfigurationError(f"{setting} must be an HTTP(S) base URL without credentials, a query or a fragment") from None
        return cls(port, timezone, public_url, RuntimeConfig.from_env())
