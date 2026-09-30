"""Loop-side continuations produced by synchronous, worker-owned validation."""
from dataclasses import dataclass


@dataclass(frozen=True)
class DeferredCall:
    action: str
    arguments: dict
    project: dict | None = None
