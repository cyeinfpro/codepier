"""Validated process settings. Errors name the setting, never echo its value."""
from __future__ import annotations

import math
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_HUB_PORT = 8765
DEFAULT_PUBLIC_URL = f"http://127.0.0.1:{DEFAULT_HUB_PORT}"
OPERATION_WAIT_SECONDS = 10


class ConfigurationError(ValueError):
    """An invalid operator setting; suitable for a concise startup error."""


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = default if raw is None else int(raw.strip())
    except ValueError:
        raise ConfigurationError(f"{name} must be an integer between {minimum} and {maximum}") from None
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(raw.strip())
    except ValueError:
        raise ConfigurationError(f"{name} must be a finite number between {minimum} and {maximum}") from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be a finite number between {minimum} and {maximum}")
    return value


def env_csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in os.getenv(name, default).split(",") if part.strip()))


def env_timezone(name: str = "TZ", default: str = "Asia/Taipei") -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv(name, default).strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigurationError(f"{name} must name an installed IANA timezone (for example UTC or Asia/Taipei)") from None


@dataclass(frozen=True)
class RuntimeConfig:
    queue_seconds: float
    wait_seconds: float
    retry_seconds: float

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        return cls(
            env_float("HUB_QUEUE_TTL_SECONDS", 1800, 5, 86400),
            env_float("HUB_CALL_WAIT_SECONDS", 8, 0, OPERATION_WAIT_SECONDS),
            env_float("HUB_DELIVERY_RETRY_SECONDS", 5, .2, 30),
        )
