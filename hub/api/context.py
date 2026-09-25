"""Explicit dependencies shared by panel API domains."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from hub.auth import Auth
    from hub.config import HubConfig
    from hub.runtime import Runtime
    from hub.store import Store
    from shared.panel_maintenance import PanelMaintenance


@dataclass(frozen=True)
class HubContext:
    store: Store
    runtime: Runtime
    auth: Auth
    config: HubConfig
    maintenance: PanelMaintenance
    public_url: Callable[[], str]
    base: Path
