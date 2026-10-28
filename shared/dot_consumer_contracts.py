"""Explicit Dot consumer ownership, separate from work/operation authority."""
from pydantic import Field
from shared.collaboration_contracts import Scope, Identifier, Key


class ConsumerRead(Scope):
    dot_id: Identifier


class ConsumerBegin(ConsumerRead):
    binding_generation: str = Field(min_length=1, max_length=128)
    wake_ticket: str = Field(min_length=32, max_length=256, repr=False)
    consumer_run_id: Identifier
    lease_seconds: int = Field(default=90, ge=30, le=300, strict=True)
    idempotency_key: Key


class ConsumerLease(ConsumerRead):
    consumer_session_id: Identifier
    consumer_fence: int = Field(ge=1, strict=True)
    consumer_proof: str = Field(min_length=32, max_length=256, repr=False)
    lease_seconds: int = Field(default=90, ge=30, le=300, strict=True)
    idempotency_key: Key


class ConsumerYield(ConsumerLease):
    expected_inbox_revision: int = Field(ge=0, strict=True)
