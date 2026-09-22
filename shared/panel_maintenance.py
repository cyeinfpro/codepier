"""Host-owned maintenance gate. The Hub cannot remove its read-only marker."""
from __future__ import annotations

import os
from pathlib import Path
from starlette.responses import JSONResponse
from shared.util import DevError


class PanelMaintenance:
    def __init__(self, runtime, socket_path=''):
        self.runtime = runtime
        self.marker = Path(socket_path).parent / 'maintenance.json' if socket_path else None
        self.inflight = 0

    def active(self):
        if self.marker is None:
            return False
        try:
            os.lstat(self.marker)
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return True  # A configured but unreadable gate must fail closed.

    def guard(self):
        if self.active():
            raise DevError('PANEL_UPDATING', '面板正在更新维护，暂不接受新操作；已有任务没有被取消', 503)

    def status(self):
        active = self.active()
        operations = self.runtime.store.one("SELECT count(*) AS n FROM operations WHERE state IN ('queued','running','reconnecting','cancelling')")['n'] if active else 0
        native = len(getattr(getattr(self.runtime, 'native', None), 'pending', {}))
        return {'maintenance': active, 'ready': active and not (self.inflight or operations or native)}


class PanelMaintenanceMiddleware:
    def __init__(self, app, gate):
        self.app, self.gate = app, gate

    async def __call__(self, scope, receive, send):
        if scope['type'] not in {'http', 'websocket'}:
            return await self.app(scope, receive, send)
        path = scope.get('path', '')
        safe = scope['type'] == 'http' and scope.get('method') in {'GET', 'HEAD'} and (
            path in {'/', '/healthz', '/api/session', '/api/settings', '/api/panel-update/status', '/agent/manifest.json'} or path.startswith('/static/'))
        if not safe and self.gate.active():
            if scope['type'] == 'websocket':
                return await send({'type': 'websocket.close', 'code': 1013})
            return await JSONResponse({'error': {'code': 'PANEL_UPDATING',
                'message': '面板正在更新维护；请保留当前页面，连接恢复后可继续查看进度'}},
                status_code=503, headers={'Retry-After': '3', 'Cache-Control': 'no-store'})(scope, receive, send)
        counted = scope['type'] == 'http' and not safe
        if counted:
            self.gate.inflight += 1
        async def observed_send(message):
            nonlocal counted
            if counted and scope.get('method') in {'GET', 'HEAD'} and message['type'] == 'http.response.start':
                headers = dict(message.get('headers', []))
                if headers.get(b'content-type', b'').startswith(b'text/event-stream'):
                    # Passive streams stay connected; control dispatches recheck guard.
                    self.gate.inflight -= 1
                    counted = False
            await send(message)
        try:
            await self.app(scope, receive, observed_send)
        finally:
            if counted:
                self.gate.inflight -= 1
