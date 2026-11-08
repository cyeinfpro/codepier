"""Scope-aware discovery and execution for the unified ``query``/``act`` tools.

``prepare`` turns a unified call into one legacy public call (see
``shared.unified_contracts``). ``execute`` runs it through ``runtime.invoke``
unchanged, then adds three presentation conveniences that never widen
authority: a collaboration credential handle, bounded response waiting on the
same operation ID, and help that answers in unified terms.
"""
from __future__ import annotations

import json
import time
import typing

from shared import unified_contracts as u
from shared.util import DevError

HANDLE_PREFIX = 'cph1.'
HANDLE_TTL_SECONDS = 7 * 24 * 3600
# Result fields that mean the server just issued or advanced a credential.
_ISSUED = frozenset({'consumer_session_id', 'consumer_fence', 'consumer_proof', 'connection_proof',
                     'binding_generation'})


def visible_actions(tool, scopes=None, *, collaboration=False):
    """Unified actions the caller may discover. Discovery is never authorization."""
    from hub.tool_discovery import catalogs, scope_allows
    visible = catalogs(scopes)
    readable = scopes is None or 'read' in scopes
    result = []
    for action, spec in u.ACTIONS[tool].items():
        if action.startswith('collab.'):
            if collaboration and readable:
                result.append(action)
            continue
        if action == 'help':
            if readable:
                result.append(action)
            continue
        if action == 'baseline' and scopes is not None and 'write' not in scopes:
            continue
        for legacy_tool, operation in spec.values():
            if legacy_tool in {'get_profile', 'get_access_context'}:
                allowed = scope_allows(legacy_tool, {}, scopes) is not False
            else:
                allowed = (operation or u.IMPLICIT_OPERATION[legacy_tool]) in visible.get(legacy_tool, {})
            if allowed:
                result.append(action)
                break
    return result


def tool_definitions(scopes=None, *, collaboration=False, authorization='fixed'):
    result = []
    for tool in ('query', 'act'):
        actions = visible_actions(tool, scopes, collaboration=collaboration)
        if not actions:
            continue
        item = {'name': tool, 'description': u.DESCRIPTIONS[tool],
                'inputSchema': u.input_schema(tool, actions),
                'annotations': dict(u.ANNOTATIONS[tool]),
                '_meta': {'securitySchemes': [{'type': 'oauth2', 'scopes': ['read']}],
                          'codepier/inputSchemaVersion': 'unified-v1'}}
        if authorization == 'role':
            from shared.role_contracts import ROLE_SCOPE
            schemes = [{'type': 'oauth2', 'scopes': [ROLE_SCOPE]}]
            item['securitySchemes'] = schemes
            item['_meta']['securitySchemes'] = schemes
            item['_meta']['codepier/authorizationMode'] = 'role'
        result.append(item)
    return result


def catalog(scopes=None, *, collaboration=False):
    return {'tools': {tool: {action: list(u.ACTIONS[tool][action])
                             for action in visible_actions(tool, scopes, collaboration=collaboration)}
                      for tool in ('query', 'act')},
            'top_level': {tool: list(fields) for tool, fields in u.TOP_LEVEL.items()},
            'note': "Values are views; the first is the default. Put every other field in args. "
                    "query(action='help', args={'action': '<action>', 'view': '<view>'}) returns the exact schema. "
                    "Discovery is not authorization."}


# ---- Collaboration handles ------------------------------------------------

def mint_handle(store, principal, fields):
    body = {'v': 1, 'u': principal.user_id, 's': principal.space_id,
            'exp': int(time.time()) + HANDLE_TTL_SECONDS,
            'f': {key: fields[key] for key in u.HANDLE_FIELDS if fields.get(key) not in (None, '')}}
    return HANDLE_PREFIX + store.encrypt(json.dumps(body, separators=(',', ':'), sort_keys=True))


def open_handle(store, principal, token):
    if not isinstance(token, str) or not token.startswith(HANDLE_PREFIX):
        raise DevError('INVALID_HANDLE', 'handle 格式无效', 422)
    try:
        body = json.loads(store.decrypt(token[len(HANDLE_PREFIX):]))
    except Exception:
        raise DevError('INVALID_HANDLE', 'handle 无法验证', 422) from None
    if not isinstance(body, dict) or body.get('v') != 1:
        raise DevError('INVALID_HANDLE', 'handle 版本不受支持', 422)
    if body.get('u') != principal.user_id or body.get('s') != principal.space_id:
        raise DevError('INVALID_HANDLE', 'handle 不属于当前身份', 403)
    if int(body.get('exp', 0)) < time.time():
        raise DevError('HANDLE_EXPIRED', 'handle 已过期；请重新 begin 或 resume', 422)
    fields = body.get('f')
    return {key: fields[key] for key in u.HANDLE_FIELDS if key in fields} if isinstance(fields, dict) else {}


def _models(model):
    if hasattr(model, 'model_fields'):
        return [model]
    found = []
    for arg in typing.get_args(model):
        found.extend(_models(arg))
    return found


def public_fields(tool, action):
    """Exact top-level fields the public collaboration action accepts."""
    import shared.public_collaboration as pc
    models = {u.CQ: pc._QUERY_MODELS, u.CD: pc._DISCUSSION_MODELS, u.CW: pc._WORK_MODELS}[tool]
    fields = set()
    for candidate in models:
        for model in _models(candidate):
            literal = model.model_fields.get('action')
            if literal is not None and action in typing.get_args(literal.annotation):
                fields |= set(model.model_fields)
    return fields


async def _apply_handle(runtime, plan, principal):
    fields = await runtime.store.run(open_handle, runtime.store, principal, plan.handle)
    allowed = public_fields(plan.tool, plan.args['action'])
    for key, value in fields.items():
        if key in allowed and key not in plan.args:
            plan.args[key] = value
    plan.notes = [fields]


async def _mint(runtime, plan, value, principal):
    issued = {key: value[key] for key in _ISSUED if value.get(key) not in (None, '')}
    if not issued:
        return value
    credentials = dict(plan.notes[0]) if plan.notes else {}
    credentials.update({key: plan.args[key] for key in u.HANDLE_FIELDS if plan.args.get(key) not in (None, '')})
    credentials.update({key: value[key] for key in u.HANDLE_FIELDS if value.get(key) not in (None, '')})
    if not ({'consumer_proof', 'connection_proof'} & credentials.keys()):
        return value
    handle = await runtime.store.run(mint_handle, runtime.store, principal, credentials)
    return {**value, 'handle': handle}


# ---- Help, execution and waiting -----------------------------------------

async def _prepare_help(runtime, plan, principal):
    from hub.principal import refresh_principal
    user = await runtime.store.run(refresh_principal, runtime.store, principal)
    if 'read' not in user.scopes:
        raise DevError('INSUFFICIENT_SCOPE', '缺少 read 权限', 403, required_scope='read')
    request = plan.help_for
    plan.args = {'operation': 'help', 'tool': '', 'action': ''}
    if not request.get('action'):
        collaboration = getattr(runtime, 'collaboration', None)
        enabled = bool(collaboration is not None and collaboration.config.enabled)
        plan.local = catalog(user.scopes, collaboration=enabled)
        return
    unified, action, view, legacy_tool, operation = u.resolve_help(
        request['action'], request.get('view', ''), request.get('tool', ''))
    note = u.unified_call_note(unified, action, view)
    if legacy_tool in {'get_profile', 'get_access_context'}:
        plan.local = {'tool': unified, 'action': action, 'view': view,
                      'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
                      'unified_call': note}
        return
    plan.args = {'operation': 'help', 'tool': legacy_tool, 'action': operation}
    request['note'] = note


async def prepare(runtime, name, raw, principal):
    plan = u.translate(name, raw)
    if plan.help_for is not None:
        await _prepare_help(runtime, plan, principal)
    if plan.handle:
        await _apply_handle(runtime, plan, principal)
    return plan


async def _wait(runtime, plan, value, principal):
    """Wait on the SAME operation; never restarts or replays work."""
    call = {'operation': 'wait', 'operation_ids': [value['operation_id']]}
    deadline = time.monotonic() + plan.wait
    latest = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining < 1:
            break
        latest = await runtime.invoke('process', {**call, 'wait_seconds': max(1, min(10, int(remaining)))}, principal)
        rows = latest.get('operations') if isinstance(latest, dict) else None
        row = rows[0] if isinstance(rows, list) and rows else {}
        if not isinstance(row, dict) or row.get('error') or not row.get('pending'):
            break
    if latest is None:
        return value, plan.tool, plan.args
    return latest, 'process', call


async def execute(runtime, plan, principal):
    """Returns (value, legacy tool name, legacy arguments) for rendering."""
    if plan.local is not None:
        return plan.local, plan.tool, plan.args
    value = await runtime.invoke(plan.tool, plan.args, principal)
    if plan.help_for is not None and plan.help_for.get('note') and isinstance(value, dict):
        value = {**value, 'unified_call': plan.help_for['note']}
    if plan.collab and isinstance(value, dict):
        value = await _mint(runtime, plan, value, principal)
    if plan.wait and isinstance(value, dict) and value.get('pending') and isinstance(value.get('operation_id'), str):
        return await _wait(runtime, plan, value, principal)
    return value, plan.tool, plan.args
