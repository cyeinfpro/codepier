"""Ephemeral human decisions, fenced to a live desktop session and operation."""
import asyncio
import contextlib
import time
import uuid

class AgentApprovals:
    def __init__(self, send):
        self.send = send
        self.pending = {}

    async def request(self, context, message, valid, timeout):
        if not valid() or len(self.pending) >= 8:
            return {'action': 'cancel'}
        identifier = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = (context['session_id'], future, valid)
        try:
            async with asyncio.timeout(timeout):
                sent = await self.send({'type': 'computer_approval', 'request_id': identifier,
                    **context, 'message': message, 'expires_at': time.time() + timeout})
                if not sent:
                    return {'action': 'cancel'}
                while not future.done():
                    if not valid():
                        return {'action': 'cancel'}
                    await asyncio.wait({future}, timeout=.25)
                action = future.result()
                if not valid():
                    action = 'cancel'
                return {'action': action, **({'content': {}} if action == 'accept' else {})}
        except TimeoutError:
            return {'action': 'cancel'}
        finally:
            self.pending.pop(identifier, None)
            if not future.done():
                future.cancel()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.send({'type': 'computer_approval_closed', 'request_id': identifier}),
                                       timeout=min(1, timeout))

    def decide(self, data):
        pending = self.pending.get(data.get('request_id'))
        if pending and data.get('session_id') == pending[0] and data.get('action') in {'accept','decline','cancel'}:
            _, future, valid = pending
            if not future.done():
                future.set_result(data['action'] if valid() else 'cancel')

    def cancel(self, session_id=None):
        for session, future, _ in list(self.pending.values()):
            if (session_id is None or session == session_id) and not future.done():
                future.set_result('cancel')
