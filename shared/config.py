"""Validated process settings. Errors name the setting, never echo its value."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "Asia/Taipei"
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
        raise ConfigurationError(
            f"{name} must be an integer between {minimum} and {maximum}"
        ) from None
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    try:
        value = default if raw is None else float(raw.strip())
    except ValueError:
        raise ConfigurationError(
            f"{name} must be a finite number between {minimum} and {maximum}"
        ) from None
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be a finite number between {minimum} and {maximum}")
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def env_csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(part.strip() for part in os.getenv(name, default).split(",") if part.strip())
    )


def env_timezone(name: str = "TZ", default: str = DEFAULT_TIMEZONE) -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv(name, default).strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigurationError(
            f"{name} must name an installed IANA timezone (for example UTC or Asia/Taipei)"
        ) from None


@dataclass(frozen=True)
class RuntimeConfig:
    queue_seconds: float
    wait_seconds: float
    retry_seconds: float
    # Bounds on prepared and sending requests during delivery.
    delivery_slots: int = 8
    delivery_prefetch: int = 2
    # Only enrolled canary devices use this chunk size and refill.
    delivery_device_chunk: int = 16
    delivery_rolling_devices: tuple[str, ...] = ()

    def rolling_for(self, device_id: str | None) -> bool:
        """Only explicitly enabled device tasks may refill from the durable queue."""
        return bool(
            device_id
            and (
                self.delivery_rolling_devices == ("*",)
                or device_id in self.delivery_rolling_devices
            )
        )

    @classmethod
    def from_env(cls) -> RuntimeConfig:
        devices = env_csv("HUB_DELIVERY_ROLLING_DEVICES")
        if devices != ("*",) and any(len(device) > 100 or "*" in device for device in devices):
            raise ConfigurationError(
                "HUB_DELIVERY_ROLLING_DEVICES must be empty, a comma-separated list of "
                "exact device IDs (up to 100 characters each), or * alone"
            )
        return cls(
            queue_seconds=env_float("HUB_QUEUE_TTL_SECONDS", 1800, 5, 86400),
            wait_seconds=env_float("HUB_CALL_WAIT_SECONDS", 10, 0, OPERATION_WAIT_SECONDS),
            retry_seconds=env_float("HUB_DELIVERY_RETRY_SECONDS", 5, 0.2, 30),
            delivery_slots=env_int("HUB_DELIVERY_SLOTS", 8, 1, 64),
            delivery_prefetch=env_int("HUB_DELIVERY_PREFETCH", 2, 1, 8),
            delivery_device_chunk=env_int("HUB_DELIVERY_DEVICE_CHUNK", 16, 1, 128),
            delivery_rolling_devices=devices,
        )
