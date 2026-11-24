"""Thin Agent adapter over shared.vps_ssh_connection.connect_vps.

Maps Agent runtime budgets and configuration into the single shared AsyncSSH
connection factory without duplicating connection logic or overriding host
identity verification parameters.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from shared.vps_ssh_connection import (
    VPSSSHConnection,
    VPSSSHConnectionError,
    asyncssh_available,
    classify_vps_ssh_error,
    connect_vps,
    remaining_budget,
)
from shared.vps_transport import (
    VPS_HOST_IDENTITY_VERIFICATION,
    VPS_SSH_PROTOCOL,
    VPS_TRANSPORT_RUNTIME,
    agent_vps_ssh_capability,
)

__all__ = [
    "VPSRuntimeBudget",
    "VPSSSHConnection",
    "VPSSSHConnectionError",
    "VPS_HOST_IDENTITY_VERIFICATION",
    "VPS_SSH_PROTOCOL",
    "VPS_TRANSPORT_RUNTIME",
    "agent_vps_ssh_capabilities",
    "asyncssh_available",
    "classify_vps_ssh_error",
    "connect_agent_vps",
    "remaining_budget",
]


@dataclass(frozen=True)
class VPSRuntimeBudget:
    """Monotonic deadline budget shared across connect, auth, retry, and exec."""

    started: float
    deadline: float
    connect_timeout: float = 10.0
    login_timeout: float = 15.0
    ssh_keepalive_interval: float | None = 15.0
    ssh_keepalive_count: int = 3
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def start(
        cls,
        timeout_seconds: float,
        *,
        connect_timeout: float = 10.0,
        login_timeout: float = 15.0,
        ssh_keepalive_interval: float | None = 15.0,
        ssh_keepalive_count: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> VPSRuntimeBudget:
        now = float(clock())
        return cls(
            started=now,
            deadline=now + max(0.0, float(timeout_seconds)),
            connect_timeout=float(connect_timeout),
            login_timeout=float(login_timeout),
            ssh_keepalive_interval=ssh_keepalive_interval,
            ssh_keepalive_count=int(ssh_keepalive_count),
            clock=clock,
        )

    def remaining(self) -> float:
        return self.deadline - float(self.clock())

    def elapsed_ms(self) -> int:
        return max(0, round((float(self.clock()) - self.started) * 1000))

    def require_remaining_budget(self) -> float:
        rem = self.remaining()
        if rem <= 0:
            raise VPSSSHConnectionError(
                "SSH_CONNECT_TIMEOUT",
                "SSH 剩余超时预算已耗尽",
                failure_stage="connection",
                retryable=False,
            )
        return rem

    def remaining_connect_budget(self) -> float:
        rem = self.require_remaining_budget()
        return min(self.connect_timeout, rem)

    def remaining_login_budget(self) -> float:
        rem = self.require_remaining_budget()
        return min(self.login_timeout, rem)


def agent_vps_ssh_capabilities(
    *,
    exec_supported: bool = True,
    terminal_supported: bool = True,
    probe_supported: bool = True,
    available: bool | None = None,
) -> dict[str, Any]:
    """Report truthful Agent vps_ssh_v2 capabilities backed by AsyncSSH."""
    is_available = asyncssh_available() if available is None else bool(available)
    return agent_vps_ssh_capability(
        available=is_available,
        exec_supported=exec_supported,
        terminal_supported=terminal_supported,
        probe_supported=probe_supported,
    )


async def connect_agent_vps(
    *,
    host: str,
    port: int,
    username: str,
    password: str | None,
    budget: VPSRuntimeBudget | None = None,
    connect_timeout: float = 10.0,
    login_timeout: float = 15.0,
    deadline: float | None = None,
    keepalive_interval: float | None = 15.0,
    keepalive_count_max: int = 3,
    challenge: Callable[[str, str, Any], Awaitable[Any]] | None = None,
    ssh_module: Any = None,
) -> VPSSSHConnection:
    """Connect to a VPS via the single shared vps_ssh_v2 factory.

    Never accepts arbitrary **kwargs and never overrides host trust parameters.
    """
    if budget is not None:
        eff_connect = budget.remaining_connect_budget()
        eff_login = budget.remaining_login_budget()
        eff_deadline = budget.deadline
        eff_keepalive_interval = budget.ssh_keepalive_interval
        eff_keepalive_count = budget.ssh_keepalive_count
    else:
        eff_connect = float(connect_timeout)
        eff_login = float(login_timeout)
        eff_deadline = deadline
        eff_keepalive_interval = keepalive_interval
        eff_keepalive_count = keepalive_count_max

    return await connect_vps(
        host=host,
        port=port,
        username=username,
        password=password,
        connect_timeout=eff_connect,
        login_timeout=eff_login,
        deadline=eff_deadline,
        keepalive_interval=eff_keepalive_interval,
        keepalive_count_max=eff_keepalive_count,
        challenge=challenge,
        ssh_module=ssh_module,
    )
