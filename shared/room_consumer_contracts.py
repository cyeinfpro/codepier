"""Strict room-member inbox and fenced-consumption requests."""
from typing import Literal

from pydantic import Field

from shared.collaboration_contracts import Identifier, Key
from shared.room_contracts import MemberIdentity, MemberTurnRead
from shared.room_context_contracts import RoomContextRead, RoomContextUpdate


class RoomConsumerRead(MemberTurnRead):
    pass


class RoomInboxRead(MemberTurnRead):
    cursor: str = Field(default='', max_length=2048)
    limit: int = Field(default=40, ge=1, le=100, strict=True)


class RoomMessageRead(MemberTurnRead):
    message_id: Identifier


class RoomAttachmentRead(MemberTurnRead):
    attachment_id: Identifier
    offset: int = Field(default=0, ge=0, strict=True)
    limit: int = Field(default=12000, ge=1, le=24000, strict=True)


class RoomConsumerBegin(MemberIdentity):
    include_inbox: bool = Field(default=False, strict=True)
    wake_ticket: str = Field(min_length=32, max_length=256, repr=False)
    consumer_run_id: Identifier
    lease_seconds: int = Field(default=90, ge=30, le=300, strict=True)
    idempotency_key: Key


class RoomConsumerLease(MemberIdentity):
    consumer_session_id: Identifier
    consumer_fence: int = Field(ge=1, strict=True)
    consumer_proof: str = Field(min_length=32, max_length=256, repr=False)
    lease_seconds: int = Field(default=90, ge=30, le=300, strict=True)
    idempotency_key: Key


class RoomConsumerYield(RoomConsumerLease):
    expected_inbox_revision: int = Field(ge=0, strict=True)
    yield_mode: Literal['if_drained', 'continue_later'] = 'if_drained'


class RoomInboxAck(RoomConsumerLease):
    expected_inbox_revision: int = Field(ge=0, strict=True)
    entry_ids: list[Identifier] = Field(min_length=1, max_length=100)


class RoomInboxRecover(MemberIdentity):
    idempotency_key: Key


ROOM_CONSUMER_QUERIES = {'room_consumer': RoomConsumerRead, 'room_inbox': RoomInboxRead,
                         'room_message_read': RoomMessageRead, 'room_attachment_read': RoomAttachmentRead,
                         'room_context': RoomContextRead}
ROOM_CONSUMER_ACTIONS = {
    'room_consumer_begin': RoomConsumerBegin,
    'room_consumer_heartbeat': RoomConsumerLease,
    'room_consumer_yield': RoomConsumerYield,
    'room_inbox_ack': RoomInboxAck,
    'room_inbox_recover': RoomInboxRecover,
    'room_context_update': RoomContextUpdate,
}
