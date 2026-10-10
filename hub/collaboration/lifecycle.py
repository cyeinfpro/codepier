"""Hub-owned, bounded lifecycle; no model or native CLI is a process supervisor."""
import asyncio
import logging
from threading import Lock
from contextlib import suppress

logger = logging.getLogger(__name__)


class CollaborationLoops:
    def __init__(self, service):
        self.service = service
        self.tasks = []
        self.started = False
        self._wake_lock = Lock()
        self._event_loop = self._event_wake = self._wake_scheduled = None

    async def start(self):
        if self.started or not self.service.config.enabled:
            return
        self.started = True
        with self._wake_lock:
            self._event_loop = asyncio.get_running_loop()
            self._event_wake = asyncio.Event() if self.service.config.events_enabled else None
            self._wake_scheduled = None
        operations = [('reconcile', self.reconcile, 5)]
        if self.service.config.events_enabled:
            operations.append(('events', self.service.events.tick, 2))
        if self.service.config.collector_enabled:
            operations.append(('collector', self.service.monitor.tick, 2))
        for name, operation, interval in operations:
            self.tasks.append(asyncio.create_task(self.loop(name, operation, interval), name='collaboration:' + name))

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
                wake.set()

    async def reconcile(self):
        await self.service.store.run(self.service.reconcile)

    async def loop(self, name, operation, interval):
        while True:
            wake = self._event_wake if name == 'events' else None
            # Clear BEFORE the DB scan. A commit during the scan/network await
            # stays set and triggers one more serialized pass, never a lost wake.
            if wake is not None:
                wake.clear()
            try:
                await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not log exception bodies: HTTP exceptions can embed callback
                # paths, authentication material or untrusted response content.
                code = type(exc).__name__
                self.service.health[name] = {'status': 'degraded', 'reason_code': code,
                                             'observed_at': self.service.clock()}
                logger.warning('Collaboration component %s degraded (%s)', name, code)
            else:
                self.service.health[name] = {'status': 'running', 'observed_at': self.service.clock()}
            if wake is None:
                await asyncio.sleep(interval)
            else:
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(wake.wait(), interval)

    async def stop(self):
        with self._wake_lock:
            self.started = False
            self._event_loop = self._event_wake = self._wake_scheduled = None
        tasks, self.tasks = self.tasks, []
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        self.started = False
        for name in self.service.health:
            self.service.health[name] = {'status': 'stopped', 'observed_at': self.service.clock()}


def install(runtime):
    from hub.collaboration.service import CollaborationService
    from hub.collaboration.events import EventService
    from hub.collaboration.monitor import MonitorService
    service = CollaborationService(runtime)
    service.events = EventService(service)
    service.monitor = MonitorService(service)
    service.health = {name: {'status': 'not_started' if enabled else 'disabled'} for name, enabled in (
        ('reconcile', service.config.enabled), ('events', service.config.events_enabled),
        ('collector', service.config.collector_enabled))}
    service.loops = CollaborationLoops(service)
    runtime.collaboration = service
    runtime.store.collaboration = service
