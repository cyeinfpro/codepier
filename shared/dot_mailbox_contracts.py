"""Explicit stable-inbox negotiation; read operations never consume or renew."""
from typing import Literal

from pydantic import Field

from shared.collaboration_contracts import DotRead, Identifier, Key


class DotMailboxConfigure(DotRead):
    protocol_version: Literal[2] = 2
    confirm_consumer_protocol: Literal[True]
    idempotency_key: Key


class DotMailboxRead(DotRead):
    binding_generation: str = Field(default='', max_length=128)
    cursor: str = Field(default='', max_length=2048)
    limit: int = Field(default=20, ge=1, le=100)


class DotMailboxAck(DotRead):
    binding_generation: Identifier
    entry_ids: list[Identifier] = Field(min_length=1, max_length=20)
    consumer_session_id: str = Field(default='', max_length=128)
    consumer_fence: int = Field(default=0, ge=0)
    consumer_proof: str = Field(default='', max_length=256)
    idempotency_key: Key


class DotMailboxRecover(DotRead):
    binding_generation: Identifier
    idempotency_key: Key


MODEL_MAP = {
    'collaboration_dot_mailbox_configure': DotMailboxConfigure,
    'collaboration_dot_mailbox': DotMailboxRead,
    'collaboration_dot_mailbox_ack': DotMailboxAck,
    'collaboration_dot_mailbox_recover': DotMailboxRecover,
}
