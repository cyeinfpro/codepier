"""Fixed-cardinality process metrics. Durable business facts live in their ledgers."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from enum import Enum
from threading import Lock
import time


class Action(str, Enum):
    BEGIN = 'begin'
    REPLY = 'reply'
    INPUT = 'input'
    WORK_START = 'work_start'
    WORK_CONTROL = 'work_control'
    WORK_HANDOFF = 'work_handoff'
    YIELD = 'yield'
    READ = 'read'
    LEGACY_CONSUMER = 'legacy_consumer'
    WAKE = 'wake'
    WEIXIN_SEND = 'weixin_send'


class Channel(str, Enum):
    ROOM = 'room'
    HOST = 'host'
    WEIXIN = 'weixin'


class Stage(str, Enum):
    ATTEMPTED = 'attempted'
    SUCCEEDED = 'succeeded'
    ERROR = 'error'
    REPLAYED = 'replayed'
    COMMITTED = 'committed'
    ACCEPTED = 'accepted'
    UNKNOWN = 'unknown'
    REJECTED = 'rejected'
    CANCELLED = 'cancelled'
    RETRY = 'retry'
    OWNER_CONFIRMED = 'owner_confirmed'


class Reason(str, Enum):
    NONE = 'none'
    AUTHORIZATION = 'authorization'
    NETWORK = 'network'
    RATE_LIMIT = 'rate_limit'
    POLICY = 'policy'
    EXPIRED = 'expired'
    OTHER = 'other'


TOOL_ACTIONS = {
    'collaboration_room_consumer_begin': Action.BEGIN,
    'collaboration_room_consumer_yield': Action.YIELD,
    'collaboration_room_reply': Action.REPLY,
    'collaboration_room_message': Action.INPUT,
    'collaboration_room_work_start': Action.WORK_START,
    'collaboration_room_work_control': Action.WORK_CONTROL,
    'collaboration_room_work_handoff': Action.WORK_HANDOFF,
    'collaboration_room_consumer': Action.READ,
    'collaboration_room_inbox': Action.READ,
    'collaboration_room_message_read': Action.READ,
    'collaboration_room_messages': Action.READ,
    'collaboration_room_work': Action.READ,
    'collaboration_room_works': Action.READ,
    'collaboration_consumer_begin': Action.LEGACY_CONSUMER,
    'collaboration_consumer_heartbeat': Action.LEGACY_CONSUMER,
    'collaboration_consumer_yield': Action.LEGACY_CONSUMER,
    'collaboration_consumer_read': Action.LEGACY_CONSUMER,
    'collaboration_dot_mailbox_configure': Action.LEGACY_CONSUMER,
    'collaboration_dot_mailbox': Action.LEGACY_CONSUMER,
    'collaboration_dot_mailbox_ack': Action.LEGACY_CONSUMER,
    'collaboration_dot_mailbox_recover': Action.LEGACY_CONSUMER,
}


class CollaborationTelemetry:
    def __init__(self, sink: Callable[[dict], None] | None = None):
        self._sink = sink
        self._lock = Lock()
        self._since = time.time()
        self._counts = Counter()
        self._sink_errors = 0
        self._invalid_events = 0

    def record(self, action: Action, stage: Stage, *, channel: Channel, reason: Reason = Reason.NONE):
        if not (isinstance(action, Action) and isinstance(stage, Stage)
                and isinstance(channel, Channel) and isinstance(reason, Reason)):
            with self._lock:
                self._invalid_events += 1
            return
        labels = action.value, stage.value, channel.value, reason.value
        with self._lock:
            self._counts[labels] += 1
        if self._sink is not None:
            try:
                self._sink(dict(zip(('action', 'stage', 'channel', 'reason'), labels, strict=True)))
            except Exception:
                # A sink can neither change the business result nor conceal its own omissions.
                with self._lock:
                    self._sink_errors += 1

    def snapshot(self):
        with self._lock:
            series = [{'action': action, 'stage': stage, 'channel': channel, 'reason': reason, 'count': count}
                      for (action, stage, channel, reason), count in sorted(self._counts.items())]
            return {'schema_version': 1, 'since': self._since, 'observed_at': time.time(),
                    'scope': 'hub_process', 'series': series,
                    'omitted_events': self._sink_errors + self._invalid_events,
                    'sink_errors': self._sink_errors, 'invalid_events': self._invalid_events}


collaboration_telemetry = CollaborationTelemetry()
