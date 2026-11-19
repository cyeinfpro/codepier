"""Hub-owned, bounded lifecycle; no model or native CLI is a process supervisor."""

import asyncio
import logging
from threading import Lock
from contextlib import suppress
from dataclasses import replace
from functools import partial

logger = logging.getLogger(__name__)
IDLE_BACKOFF_SECONDS = 20
FULL_RECONCILE_INTERVAL_SECONDS = 60


class CollaborationLoops:
    def __init__(self, service):
        self.service = service
        self.tasks = []
        self.started = False
        self.host_backfill_pending = False
        self._wake_lock = Lock()
        self._event_loop = self._event_wake = self._wake_scheduled = None
        self._reconcile_wake = self._monitor_wake = self._renewal_wake = None
        self._reconcile_idle = True
        self._full_reconcile_at = None
        self._renewals_idle = True
        self._channel_wakes = {}

    async def start(self):
        if self.started or not self.service.config.enabled:
            return
        self.started = True
        with self._wake_lock:
            self._event_loop = asyncio.get_running_loop()
            self._event_wake = asyncio.Event() if self.service.config.events_enabled else None
            self._reconcile_wake = asyncio.Event()
            self._renewal_wake = asyncio.Event() if self.service.config.events_enabled else None
            self._monitor_wake = asyncio.Event() if self.service.config.collector_enabled else None
            self._wake_scheduled = None
        operations = [("reconcile", self.reconcile, 5)]
        if self.service.config.events_enabled:
            operations.extend(
                [
                    ("events", partial(self.service.events.tick, wake=self._event_wake), 2),
                    ("room_subscription_renewal", self.renew_room_subscriptions, 5),
                ]
            )
        if self.service.config.collector_enabled:
            operations.append(("collector", self.service.monitor.tick, 2))
        # Owner switches are persisted and can change without a Hub restart.
        # These bounded loops remain idle unless a current owner/account is enabled.
        if self.service.config.enabled:
            operations.extend(
                [
                    ("weixin_poll", self.service.weixin.tick, 1),
                    ("weixin_delivery", self.service.weixin.delivery.tick, 1),
                    ("weixin_media", self.service.weixin.media.tick, 2),
                ]
            )
        if self.service.config.enabled:
            operations.extend(
                [
                    ("telegram_poll", self.service.telegram.tick, 1),
                    ("telegram_media", self.service.channels.media.tick, 2),
                ]
            )
        if self.service.config.enabled:
            operations.append(("channel_mirrors", self.service.channels.tick, 1))
        for name, operation, interval in operations:
            if name.startswith(("weixin_", "telegram_")) or name == "channel_mirrors":
                self._channel_wakes[name] = asyncio.Event()
            self.service.health.setdefault(
                name, {"enabled": True, "running": False, "reason_code": ""}
            )
            self.tasks.append(
                asyncio.create_task(
                    self.loop(name, operation, interval), name="collaboration:" + name
                )
            )

    def wake_events(self):
        """Coalesce a committed outbox hint onto the existing event loop."""
        with self._wake_lock:
            loop, wake = self._event_loop, self._event_wake
            if not self.started or loop is None or wake is None or loop.is_closed():
                return
            if self._wake_scheduled is wake:
                return
            self._wake_scheduled = wake
            try:
                loop.call_soon_threadsafe(self._deliver_event_wake, wake)
            except RuntimeError:
                # Shutdown won the race. Durable outbox survives for the next
                # process; no callback failure may undo a committed result.
                self._wake_scheduled = None

    def _deliver_event_wake(self, wake):
        with self._wake_lock:
            if self._wake_scheduled is wake:
                self._wake_scheduled = None
            if self.started and self._event_wake is wake:
                events = getattr(self.service, "events", None)
                hint = getattr(events, "hint_recovery", None)
                if hint is not None:
                    hint()
                wake.set()

    def _wake_single(self, event):
        with self._wake_lock:
            loop = self._event_loop
            if not self.started or loop is None or event is None or loop.is_closed():
                return
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                return

    def wake_reconcile(self):
        self._wake_single(self._reconcile_wake)

    def wake_monitor(self):
        self._wake_single(self._monitor_wake)

    def wake_renewals(self):
        self._wake_single(self._renewal_wake)

    def wake_channels(self, name=None):
        """An owner/account change interrupts the idle backoff immediately."""
        with self._wake_lock:
            loop = self._event_loop
            if not self.started or loop is None or loop.is_closed():
                return
            wakes = (
                self._channel_wakes.values() if name is None else (self._channel_wakes.get(name),)
            )
            for wake in wakes:
                if wake is None:
                    continue
                try:
                    loop.call_soon_threadsafe(wake.set)
                except RuntimeError:
                    return

    def channel_delay(self, name, interval):
        channels = (
            ("weixin", "telegram")
            if name == "channel_mirrors"
            else ("weixin",)
            if name.startswith("weixin_")
            else ("telegram",)
        )
        for channel in channels:
            enabled = self.service.channel_settings.sql_enabled(channel, "a")
            if self.service.store.one(
                f"SELECT 1 FROM {channel}_accounts a WHERE a.state='active' AND {enabled} LIMIT 1"
            ):
                return interval
        # Still run periodic expiry/reconciliation; no enabled owner/account
        # should consume the one database worker every second.
        return 30

    def channel_probe(self, name, interval):
        delay = self.channel_delay(name, interval)
        if delay == interval:
            return True, delay
        now = self.service.clock()
        if name in {"weixin_media", "telegram_media"}:
            has_media = bool(
                self.service.store.one(
                    "SELECT 1 FROM channel_attachments WHERE state!='expired' AND expires_at<=? LIMIT 1",
                    (now,),
                )
                or (
                    name == "weixin_media"
                    and self.service.store.one(
                        "SELECT 1 FROM channel_attachments WHERE channel='weixin' AND state='pending' AND next_attempt<=? AND COALESCE(lease_until,0)<=? LIMIT 1",
                        (now, now),
                    )
                )
            )
            return has_media, (interval if has_media else delay)
        if name == "channel_mirrors":
            has_mirrors = bool(
                self.service.store.one(
                    "SELECT 1 FROM channel_mirror_parts WHERE state IN ('queued','waiting_context','sending','preparing') LIMIT 1"
                )
            )
            return has_mirrors, (interval if has_mirrors else delay)
        return False, delay

    def _probe_reconcile(self):
        now = self.service.clock()
        store = self.service.store
        has_revoke = bool(
            store.one(
                "SELECT 1 FROM mcp_event_deliveries WHERE state='leased' AND reason_code='host_revoked_in_flight' AND lease_until<=? LIMIT 1",
                (now,),
            )
            or store.one(
                """SELECT 1 FROM room_host_revoke_jobs j JOIN room_host_connections h
                ON h.id=j.host_connection_id AND h.host_revocation_epoch=j.host_revocation_epoch
                WHERE h.state='revoked' AND j.state IN ('pending','running') LIMIT 1""",
            )
        )
        has_proof_expire = bool(
            store.one(
                """SELECT 1 FROM room_host_attach_challenges c JOIN room_host_connections h
                ON h.id=c.host_connection_id WHERE c.state='pending' AND (c.expires_at<=? OR h.state='revoked') LIMIT 1""",
                (now,),
            )
            or store.one(
                """SELECT 1 FROM room_host_control_handles c JOIN room_host_connections h
                ON h.id=c.host_connection_id WHERE c.exchange_secret!='' AND (c.exchange_expires_at<=? OR h.state='revoked') LIMIT 1""",
                (now,),
            )
        )
        # The cheap query is a scheduling hint, not an authority on what the
        # recovery code can repair. Periodically run the canonical scan even
        # when the hint has no matching row.
        has_service_reconcile = bool(
            (self._full_reconcile_at is not None and now >= self._full_reconcile_at)
            or getattr(self.service, "_reconcile_after", "")
            or getattr(getattr(self.service, "coordination", None), "_reconcile_after", "")
            or getattr(getattr(self.service, "dot_mailbox", None), "_settle_after", 0)
            or store.one(
                """SELECT 1 FROM conversation_rooms r
                WHERE (r.archived_at IS NOT NULL AND r.state!='archived')
                   OR (r.state='archived' AND NOT EXISTS (
                       SELECT 1 FROM conversation_archives a WHERE a.conversation_id=r.id))
                LIMIT 1"""
            )
            or store.one(
                """SELECT 1 FROM dot_mailbox_entries e
                JOIN dot_message_handling h ON h.dot_id=e.dot_id AND h.message_id=e.object_id
                JOIN dot_mailbox_bindings b ON b.dot_id=e.dot_id AND b.generation=e.generation
                WHERE e.kind='message' AND e.acknowledged_at IS NULL AND e.superseded_by=''
                  AND h.handling_state IN ('reply_committed','covered','no_response_required','legacy_handled')
                LIMIT 1"""
            )
            or store.one(
                "SELECT 1 FROM coordination_goals WHERE state='active' AND expires_at<=? LIMIT 1",
                (now,),
            )
            or store.one(
                "SELECT 1 FROM coordination_work WHERE state IN ('leased','running') "
                "AND lease_until<=? LIMIT 1",
                (now,),
            )
            or store.one(
                """SELECT 1 FROM collaboration_jobs WHERE state IN ('queued','leased','running','retry_wait')
                AND (deadline_at<=? OR (state IN ('leased','running') AND COALESCE(lease_until,0)<=?)
                     OR (state='retry_wait' AND not_before<=?)) LIMIT 1""",
                (now, now, now),
            )
        )
        return (
            has_revoke,
            has_proof_expire,
            has_service_reconcile,
            bool(self.host_backfill_pending),
        )

    def _run_reconcile(self, do_revoke, do_expire, do_service, do_backfill):
        error = None
        steps = (
            (do_revoke, self.service.room_connections.drain_revoke_jobs),
            (do_expire, self.service.rooms.host_proofs.expire),
            (do_service, self.service.reconcile),
            (do_backfill and self.host_backfill_pending, self.resume_host_backfill),
        )
        for enabled, step in steps:
            if not enabled:
                continue
            try:
                # Separate top-level commits: a malformed job or backfill
                # record must never undo a successful security cleanup.
                if step == self.service.reconcile:
                    # The service publishes each cursor after its own committed unit.
                    step()
                else:
                    with self.service.store.transaction():
                        step()
            except Exception as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error

    async def reconcile(self):
        if self._full_reconcile_at is None:
            self._full_reconcile_at = self.service.clock() + FULL_RECONCILE_INTERVAL_SECONDS
        flags = await self.service.store.run_read(self._probe_reconcile)
        if not any(flags):
            self._reconcile_idle = True
            return
        self._reconcile_idle = False
        await self.service.store.run(self._run_reconcile, *flags)
        if flags[2]:
            self._full_reconcile_at = self.service.clock() + FULL_RECONCILE_INTERVAL_SECONDS

    def resume_host_backfill(self):
        from hub.collaboration.room_host_rollout import advance_auto_backfill

        store = self.service.store
        with store.lock:
            if advance_auto_backfill(store):
                self.service.config = replace(self.service.config, host_connections_mode="on")
                self.host_backfill_pending = False

    def _probe_renewals(self):
        now = self.service.clock()
        return bool(
            self.service.store.one(
                "SELECT 1 FROM room_subscription_renewals WHERE state='approved' AND next_at<=? LIMIT 1",
                (now,),
            )
        )

    async def renew_room_subscriptions(self):
        has_work = await self.service.store.run_read(self._probe_renewals)
        if not has_work:
            self._renewals_idle = True
            return
        self._renewals_idle = False
        await self.service.rooms.renewals.tick(self.service.events)

    def _wake_for(self, name):
        if name == "events":
            return self._event_wake
        if name == "reconcile":
            return self._reconcile_wake
        if name == "collector":
            return self._monitor_wake
        if name == "room_subscription_renewal":
            return self._renewal_wake
        return self._channel_wakes.get(name)

    def _idle_delay_for(self, name, interval, channel_idle_delay):
        if name == "events":
            deadline = getattr(getattr(self.service, "events", None), "next_dispatch_delay", None)
            return deadline(interval) if deadline is not None else IDLE_BACKOFF_SECONDS
        if name == "reconcile":
            return IDLE_BACKOFF_SECONDS if self._reconcile_idle else interval
        if name == "room_subscription_renewal":
            return IDLE_BACKOFF_SECONDS if self._renewals_idle else interval
        if name == "collector":
            next_collect = getattr(
                getattr(self.service, "monitor", None), "next_collect_delay", None
            )
            return (
                next_collect(IDLE_BACKOFF_SECONDS)
                if next_collect is not None
                else IDLE_BACKOFF_SECONDS
            )
        return channel_idle_delay

    async def loop(self, name, operation, interval):
        while True:
            wake = self._wake_for(name)
            # Clear BEFORE the DB scan. A commit during the scan/network await
            # stays set and triggers one more serialized pass, never a lost wake.
            if wake is not None:
                wake.clear()
            idle_delay = interval
            try:
                self.service.health[name] = {
                    "status": "running",
                    "observed_at": self.service.clock(),
                }
                if name in self._channel_wakes:
                    has_work, idle_delay = await self.service.store.run_read(
                        lambda: self.channel_probe(name, interval)
                    )
                    if has_work:
                        await operation()
                else:
                    await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not log exception bodies: HTTP exceptions can embed callback
                # paths, authentication material or untrusted response content.
                code = type(exc).__name__
                self.service.health[name] = {
                    "status": "degraded",
                    "reason_code": code,
                    "observed_at": self.service.clock(),
                }
                logger.warning("Collaboration component %s degraded (%s)", name, code)
            else:
                self.service.health[name] = self._completed_health(name)
            delay = self._idle_delay_for(name, interval, idle_delay)
            if wake is None:
                await asyncio.sleep(delay)
            else:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(wake.wait(), delay)

    def _completed_health(self, name):
        observed = self.service.clock()
        events = getattr(self.service, "events", None)
        failures = getattr(events, "recovery_errors", {}) if name == "events" else {}
        if failures:
            return {
                "status": "degraded",
                "reason_code": next(iter(failures.values())),
                "observed_at": observed,
            }
        return {"status": "running", "observed_at": observed}

    async def stop(self):
        with self._wake_lock:
            self.started = False
            self._event_loop = self._event_wake = self._wake_scheduled = None
            self._reconcile_wake = self._monitor_wake = self._renewal_wake = None
        self._channel_wakes = {}
        tasks, self.tasks = self.tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self.started = False
        for name in self.service.health:
            self.service.health[name] = {"status": "stopped", "observed_at": self.service.clock()}


def install(runtime, *, config=None):
    from hub.collaboration.service import CollaborationService
    from hub.collaboration.events import EventService
    from hub.collaboration.monitor import MonitorService

    service = CollaborationService(runtime, config=config)
    service.events = EventService(service)
    service.monitor = MonitorService(service)
    service.health = {
        name: {"status": "not_started" if enabled else "disabled"}
        for name, enabled in (
            ("reconcile", service.config.enabled),
            ("events", service.config.events_enabled),
            ("collector", service.config.collector_enabled),
        )
    }
    service.loops = CollaborationLoops(service)
    runtime.collaboration = service
    runtime.store.collaboration = service
