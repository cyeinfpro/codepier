"""Bounded, redacted /mcp ingress diagnostics; correlation is never authority.

No bodies, raw headers, URLs, arguments, commands, identities or exception messages
are logged. The allowlisted Mcp-Method hint is unverified until body validation.
Host-side rejections that never reach ASGI cannot
produce an ID here. Transport interruption never cancels durable execution.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
import json
import logging
import re
import threading
import time
import uuid

LOGGER = logging.getLogger('uvicorn.error')
_CURRENT = ContextVar('codepier_mcp_request', default=None)
HEADER = b'x-codepier-request-id'
MAX_EVENTS = 12
METHODS = frozenset({'initialize', 'ping', 'server/discover', 'tools/list', 'tools/call',
    'resources/list', 'resources/read', 'resources/templates/list', 'prompts/list',
    'prompts/get', 'tasks/get', 'tasks/update', 'tasks/cancel',
    'events/list', 'events/subscribe', 'events/unsubscribe'})
STAGES = frozenset({'received', 'authenticate', 'authenticated', 'parsed',
    'protocol_validated', 'route_resolved', 'invoke_started', 'tool_returned',
    'rejected', 'tool_error', 'task_cancel_acknowledged', 'event_catalog_returned', 'response_started',
    'transport_disconnected', 'unhandled_exception', 'event_callback_failed', 'finished'})
ERRORS = frozenset({'INVALID_ARGUMENTS', 'INSUFFICIENT_SCOPE', 'INVALID_TOKEN',
    'UNKNOWN_TOOL', 'TOOL_REMOVED', 'UNKNOWN_OPERATION', 'PROJECT_NOT_FOUND',
    'DEVICE_OFFLINE', 'DEVICE_DISABLED', 'DEVICE_BUSY', 'IDEMPOTENCY_CONFLICT',
    'EXECUTION_POLICY_LIMIT', 'EXECUTION_POLICY_BLOCKED', 'TASK_NOT_FOUND',
    'TASK_BINDING_INVALID', 'BODY_TOO_LARGE', 'OTHER',
    # Fixed non-secret business codes; never log arbitrary strings or request text.
    'OWNER_REQUIRED', 'MESSAGE_OWNER_REQUIRED', 'LEASE_EXPIRED', 'WORK_LEASE_EXPIRED',
    'GOAL_APPROVAL_CHANGED', 'GOAL_CAPABILITY_DENIED', 'WORK_APPROVAL_CHANGED',
    'DELEGATION_APPROVAL_CHANGED', 'DELEGATION_BUDGET_EXCEEDED',
    'DELEGATION_EVENT_DENIED', 'DELEGATION_EVENT_OBSOLETE', 'DELEGATION_GRANT_REQUIRED',
    'DELEGATION_HOST_KEY_REQUIRED', 'DELEGATION_IMMUTABLE', 'DELEGATION_MESSAGE_CHANGED',
    'DELEGATION_NOT_FOUND', 'DELEGATION_POLICY_CHANGED', 'DELEGATION_POLICY_INACTIVE',
    'DELEGATION_POLICY_NOT_FOUND', 'DELEGATION_PROJECT_CHANGED', 'DELEGATION_SCOPE_CHANGED',
    'DELEGATION_SCOPE_DENIED', 'DELEGATION_SLOT_CHANGED', 'DELEGATION_TARGET_CHANGED',
    'DELEGATION_TARGET_REQUIRED', 'AUTOMATIC_DELEGATION_NOT_CONFIGURED',
    'AUTOMATIC_DELEGATION_SCOPE_REQUIRED'})
OUTCOMES = frozenset({'complete', 'tool_error', 'protocol_error', 'http_error',
    'internal_error', 'transport_interrupted', 'response_incomplete'})
CALLBACK_REASONS = frozenset({'connection_refused', 'timeout', 'tls_error',
    'http_4xx', 'http_5xx', 'challenge_failed'})
ID = re.compile(r'^[a-f0-9]{32}$')
LABEL = re.compile(r'^[A-Za-z0-9_.:/-]{1,100}$')


def write_event(event):
    try:
        LOGGER.info('codepier_mcp_request %s', json.dumps(event, ensure_ascii=True, separators=(',', ':')))
    except Exception:
        # Diagnostic sink failures must not change execution or authorization.
        pass


class RateGate:
    """One process-local token bucket; no unbounded per-caller identity map."""
    def __init__(self, rate=5.0, burst=50, clock=time.monotonic):
        self.rate, self.burst, self.clock = rate, burst, clock
        self.tokens = float(burst)
        self.updated = self.reported = clock()
        self.suppressed = 0
        self.lock = threading.Lock()

    def admit(self):
        with self.lock:
            now = self.clock()
            self.tokens = min(self.burst, self.tokens + max(0, now - self.updated) * self.rate)
            self.updated = now
            summary = 0
            if now - self.reported >= 60:
                summary, self.suppressed, self.reported = self.suppressed, 0, now
            allowed = self.tokens >= 1
            if allowed: self.tokens -= 1
            else: self.suppressed += 1
            return allowed, summary


class Trace:
    def __init__(self, sampled, http_method, method_hint=None):
        self.identifier = uuid.uuid4().hex
        self.sampled = sampled
        self.http_method = http_method if isinstance(http_method, str) and http_method in {'POST', 'GET', 'DELETE', 'OPTIONS'} else 'OTHER'
        self.started = time.monotonic()
        self.method = 'unknown'
        self.method_hint = method_hint if isinstance(method_hint, str) and method_hint in METHODS else None
        self.tool = None
        self.operation_id = None
        self.protocol = 'unknown'
        self.last_stage = None
        self.outcome = 'complete'
        self.http_status = None
        self.events = self.omitted = 0
        self.closed = False

    def record(self, stage, *, method=None, tool=None, operation_id=None,
               protocol=None, error_code=None, outcome=None, http_status=None,
               callback_reason=None, callback_http_status=None, event_count=None):
        if self.closed or not isinstance(stage, str) or stage not in STAGES: return
        if method is not None: self.method = method if isinstance(method, str) and method in METHODS else 'unknown'
        if tool is not None: self.tool = tool if isinstance(tool, str) and LABEL.fullmatch(tool) else 'unknown'
        if isinstance(operation_id, str) and ID.fullmatch(operation_id): self.operation_id = operation_id
        if isinstance(protocol, str) and protocol in {'legacy', 'modern'}: self.protocol = protocol
        if isinstance(outcome, str) and outcome in OUTCOMES: self.outcome = outcome
        if type(http_status) is int and 100 <= http_status <= 599: self.http_status = http_status
        previous, self.last_stage = self.last_stage, stage
        if not self.sampled: return
        if self.events >= MAX_EVENTS - (stage != 'finished'):
            self.omitted += 1
            return
        event = {'request_id': self.identifier, 'stage': stage, 'previous_stage': previous,
            'http_method': self.http_method, 'rpc_method': self.method, 'protocol': self.protocol,
            'outcome': self.outcome, 'elapsed_ms': max(0, round((time.monotonic() - self.started) * 1000))}
        if self.method_hint: event['rpc_method_hint'] = self.method_hint
        if self.tool: event['tool'] = self.tool
        if self.operation_id: event['operation_id'] = self.operation_id
        if self.http_status is not None: event['http_status'] = self.http_status
        if type(error_code) is int and -32768 <= error_code < 0:
            event['rpc_error_code'] = error_code
        elif error_code is not None:
            event['error_code'] = error_code if isinstance(error_code, str) and error_code in ERRORS else 'OTHER'
        if stage == 'event_callback_failed':
            if isinstance(callback_reason, str) and callback_reason in CALLBACK_REASONS:
                event['callback_reason'] = callback_reason
            if type(callback_http_status) is int and 100 <= callback_http_status <= 599:
                event['callback_http_status'] = callback_http_status
        if stage == 'event_catalog_returned' and type(event_count) is int and 0 <= event_count <= 10000:
            event['event_count'] = event_count
        if stage == 'finished': event['omitted_events'] = self.omitted
        self.events += 1
        write_event(event)

    def finish(self, outcome=None):
        if self.closed: return
        if outcome is None and self.http_status is not None and self.http_status >= 400 and self.outcome == 'complete':
            outcome = 'http_error'
        self.record('finished', outcome=outcome)
        self.closed = True


def request_id():
    trace = _CURRENT.get()
    return trace.identifier if trace else None


def mark(stage, **safe_fields):
    trace = _CURRENT.get()
    if trace: trace.record(stage, **safe_fields)


def error_headers(scope):
    value = scope.get('state', {}).get('codepier_request_id')
    return {'X-CodePier-Request-ID': value} if isinstance(value, str) and ID.fullmatch(value) else {}


class MCPRequestAuditMiddleware:
    def __init__(self, app, gate=None):
        self.app = app
        self.gate = gate or RateGate()

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') != '/mcp':
            return await self.app(scope, receive, send)
        sampled, suppressed = self.gate.admit()
        if suppressed: write_event({'stage': 'sampling_summary', 'suppressed_requests': suppressed})
        hints = [value for name, value in scope.get('headers', []) if name.lower() == b'mcp-method']
        hint = hints[0].decode('latin-1') if len(hints) == 1 else None
        trace = Trace(sampled, scope.get('method'), hint)
        scope.setdefault('state', {})['codepier_request_id'] = trace.identifier
        context = _CURRENT.set(trace)
        disconnected = False
        trace.record('received')

        async def observed_receive():
            nonlocal disconnected
            message = await receive()
            if message['type'] == 'http.disconnect' and not disconnected:
                disconnected = True
                trace.record('transport_disconnected', outcome='transport_interrupted')
            return message

        async def observed_send(message):
            if message['type'] == 'http.response.start':
                headers = [(k, v) for k, v in message.get('headers', []) if k.lower() != HEADER]
                headers.append((HEADER, trace.identifier.encode('ascii')))
                exposed = next((v.decode('latin-1') for k, v in headers if k.lower() == b'access-control-expose-headers'), '')
                headers = [(k, v) for k, v in headers if k.lower() != b'access-control-expose-headers']
                names = [name.strip() for name in exposed.split(',') if name.strip()]
                if 'x-codepier-request-id' not in {name.lower() for name in names}:
                    names.append('X-CodePier-Request-ID')
                headers.append((b'access-control-expose-headers', ', '.join(names).encode('latin-1')))
                message = {**message, 'headers': headers}
                await send(message)
                trace.record('response_started', http_status=message['status'])
                return
            await send(message)
            if message['type'] == 'http.response.body' and not message.get('more_body', False):
                trace.finish()

        try:
            await self.app(scope, observed_receive, observed_send)
        except asyncio.CancelledError:
            trace.finish('transport_interrupted')
            raise
        except (BrokenPipeError, ConnectionResetError):
            trace.finish('transport_interrupted')
            raise
        except Exception:
            trace.record('unhandled_exception', outcome='internal_error')
            trace.finish('internal_error')
            raise
        finally:
            if not trace.closed:
                trace.finish('transport_interrupted' if disconnected else 'response_incomplete')
            _CURRENT.reset(context)
