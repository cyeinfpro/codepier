"""Hub-owned, bounded lifecycle; no model or native CLI is a process supervisor."""
import asyncio
import logging
from contextlib import suppress

logger = logging.getLogger(__name__)


class CollaborationLoops:
    def __init__(self, service):
        self.service = service
        self.tasks = []
        self.started = False

    async def start(self):
        if self.started or not self.service.config.enabled:
            return
        self.started = True
        operations = [('reconcile', self.reconcile, 5)]
        if self.service.config.events_enabled:
            operations.append(('events', self.service.events.tick, 2))
        if self.service.config.collector_enabled:
            operations.append(('collector', self.service.monitor.tick, 2))
        for name, operation, interval in operations:
            self.tasks.append(asyncio.create_task(self.loop(name, operation, interval), name='collaboration:' + name))

    async def reconcile(self):
        await self.service.store.run(self.service.reconcile)

    async def loop(self, name, operation, interval):
        while True:
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
            await asyncio.sleep(interval)

    async def stop(self):
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
