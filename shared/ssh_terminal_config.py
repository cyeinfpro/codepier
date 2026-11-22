"""Bounded configuration shared by the Web SSH gateway and Agent.

Defaults are initial operating limits, not performance claims.  Environment
configuration is opt-in and never changes authority or enables the feature.
"""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import ClassVar


@dataclass(frozen=True)
class TerminalConfig:
    frame_bytes: int = 16 * 1024
    input_frame_bytes: int = 16 * 1024
    control_bytes: int = 8 * 1024
    ring_bytes: int = 2 * 1024 * 1024
    output_high_water_bytes: int = 256 * 1024
    output_low_water_bytes: int = 64 * 1024
    hub_relay_bytes: int = 512 * 1024
    ssh_window_bytes: int = 256 * 1024
    pending_input_bytes: int = 64 * 1024
    paste_bytes: int = 256 * 1024
    resize_coalesce_ms: int = 60
    heartbeat_seconds: float = 15.0
    ssh_keepalive_interval: float = 15.0
    ssh_keepalive_count: int = 3
    ticket_ttl_seconds: float = 30.0
    auth_timeout_seconds: float = 5.0
    lease_seconds: float = 30.0
    lease_renew_seconds: float = 10.0
    direct_detach_seconds: float = 600.0
    attachment_grace_seconds: float = 30.0
    connect_timeout: float = 10.0
    login_timeout: float = 15.0
    write_timeout: float = 5.0
    shutdown_timeout: float = 5.0
    max_sessions_per_user: int = 8
    max_sessions_per_agent: int = 16
    max_sessions_per_hub: int = 64
    agent_buffer_bytes: int = 128 * 1024 * 1024
    hub_buffer_bytes: int = 64 * 1024 * 1024
    min_cols: int = 2
    max_cols: int = 1000
    min_rows: int = 1
    max_rows: int = 500
    input_receipts: int = 256
    scrollback_lines: int = 10000
    tmux_history_lines: int = 10000

    ENV_PREFIX: ClassVar[str] = "CODEPIER_SSH_TERMINAL_"
    _TIME_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "heartbeat_seconds", "ssh_keepalive_interval", "ticket_ttl_seconds",
        "auth_timeout_seconds", "lease_seconds", "lease_renew_seconds",
        "direct_detach_seconds", "attachment_grace_seconds", "connect_timeout",
        "login_timeout", "write_timeout", "shutdown_timeout",
    })
    _RANGES: ClassVar[dict[str, tuple[float, float]]] = {
        "frame_bytes": (256, 65536), "input_frame_bytes": (256, 65536),
        "control_bytes": (1024, 65536), "ring_bytes": (65536, 64 * 1024 * 1024),
        "output_high_water_bytes": (4096, 4 * 1024 * 1024),
        "output_low_water_bytes": (0, 4 * 1024 * 1024),
        "hub_relay_bytes": (8192, 16 * 1024 * 1024),
        "ssh_window_bytes": (4096, 4 * 1024 * 1024),
        "pending_input_bytes": (4096, 1024 * 1024),
        "paste_bytes": (4096, 4 * 1024 * 1024),
        "resize_coalesce_ms": (0, 1000), "heartbeat_seconds": (1, 120),
        "ssh_keepalive_interval": (1, 120), "ssh_keepalive_count": (1, 10),
        "ticket_ttl_seconds": (5, 120), "auth_timeout_seconds": (1, 30),
        "lease_seconds": (5, 120), "lease_renew_seconds": (1, 60),
        "direct_detach_seconds": (0, 3600), "attachment_grace_seconds": (0, 120),
        "connect_timeout": (1, 120), "login_timeout": (1, 120),
        "write_timeout": (0.1, 30), "shutdown_timeout": (0.1, 30),
        "max_sessions_per_user": (1, 128), "max_sessions_per_agent": (1, 256),
        "max_sessions_per_hub": (1, 1024),
        "agent_buffer_bytes": (1024 * 1024, 2 * 1024 * 1024 * 1024),
        "hub_buffer_bytes": (1024 * 1024, 2 * 1024 * 1024 * 1024),
        "min_cols": (1, 1000), "max_cols": (1, 1000),
        "min_rows": (1, 500), "max_rows": (1, 500),
        "input_receipts": (1, 8192), "scrollback_lines": (0, 100000),
        "tmux_history_lines": (0, 100000),
    }

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            expected = (int, float) if field.name in self._TIME_FIELDS else (int,)
            if isinstance(value, bool) or not isinstance(value, expected):
                raise TypeError(f"{field.name} has an invalid numeric type")
            low, high = self._RANGES[field.name]
            if not low <= value <= high or not math.isfinite(value):
                raise ValueError(f"{field.name} must be between {low:g} and {high:g}")
        if not self.output_low_water_bytes < self.output_high_water_bytes:
            raise ValueError("output low water must be below high water")
        if self.frame_bytes > self.output_high_water_bytes:
            raise ValueError("an output frame must fit the output credit window")
        if self.ring_bytes < self.output_high_water_bytes + self.ssh_window_bytes:
            raise ValueError("ring must cover output credit and the granted SSH receive window")
        if self.hub_relay_bytes < self.output_high_water_bytes + self.frame_bytes:
            raise ValueError("Hub relay must cover output credit plus one frame")
        if self.pending_input_bytes < self.input_frame_bytes:
            raise ValueError("pending input budget must fit an input frame")
        if self.paste_bytes < self.input_frame_bytes:
            raise ValueError("paste limit must fit an input frame")
        if self.lease_renew_seconds >= self.lease_seconds:
            raise ValueError("authority lease must outlive its renewal interval")
        if self.auth_timeout_seconds > self.ticket_ttl_seconds:
            raise ValueError("authentication deadline must not outlive ticket TTL")
        if self.min_cols > self.max_cols or self.min_rows > self.max_rows:
            raise ValueError("terminal dimension bounds are inverted")
        minimum_agent = self.ring_bytes + self.ssh_window_bytes + self.pending_input_bytes
        if self.agent_buffer_bytes < minimum_agent:
            raise ValueError("Agent budget cannot accommodate even one session")
        if self.hub_buffer_bytes < self.hub_relay_bytes:
            raise ValueError("Hub budget cannot accommodate even one session")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TerminalConfig:
        """Read known numeric overrides; ignore unrelated feature-switch names."""
        source = os.environ if environ is None else environ
        values: dict[str, int | float] = {}
        for field in fields(cls):
            key = cls.ENV_PREFIX + field.name.upper()
            if key not in source:
                continue
            raw = source[key]
            if not isinstance(raw, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", raw):
                raise ValueError(f"{key} must be an unsigned finite number")
            if field.name in cls._TIME_FIELDS:
                values[field.name] = float(raw)
            else:
                if "." in raw:
                    raise ValueError(f"{key} must be an integer")
                values[field.name] = int(raw)
        return cls(**values)

    def public_limits(self) -> dict[str, int | float]:
        """Non-secret browser limits, using the same snake_case wire names."""
        names = (
            "frame_bytes", "input_frame_bytes", "control_bytes",
            "output_high_water_bytes", "output_low_water_bytes", "pending_input_bytes",
            "paste_bytes", "resize_coalesce_ms", "heartbeat_seconds",
            "auth_timeout_seconds", "min_cols", "max_cols", "min_rows", "max_rows",
            "scrollback_lines",
        )
        result = {name: getattr(self, name) for name in names}
        result.update({
            "output_pending_bytes": self.output_high_water_bytes + self.frame_bytes,
            "heartbeat_timeout_seconds": self.heartbeat_seconds * 3,
            "reconnect_max_ms": 8000,
            "max_user_sessions": self.max_sessions_per_user,
        })
        return result


DEFAULT_CONFIG = TerminalConfig()
