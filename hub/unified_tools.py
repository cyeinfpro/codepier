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


def directory(scopes=None, *, collaboration=False):
    """Project one action/view directory for the caller's current scopes."""
    from hub.tool_discovery import catalogs, scope_allows

    visible = {'query': {}, 'act': {}}
    if scopes is not None and 'read' not in scopes:
        return visible
    native = catalogs(scopes)
    for tool, actions in u.ACTIONS.items():
        for action, routes in actions.items():
            if action.startswith('collab.'):
                views = list(routes) if collaboration else []
            elif action == 'help':
                views = list(routes)
            elif action == 'baseline' and scopes is not None and 'write' not in scopes:
                continue
            else:
                views = [view for view, (legacy, operation) in routes.items()
                         if (scope_allows(legacy, {}, scopes) if legacy in u.STANDALONE_TOOLS
                             else (operation or u.IMPLICIT_OPERATION[legacy]) in native.get(legacy, {}))]
            if views:
                visible[tool][action] = views
    return visible


def visible_actions(tool, scopes=None, *, collaboration=False):
    return list(directory(scopes, collaboration=collaboration)[tool])


def runtime_directory(runtime, scopes):
    collaboration = getattr(runtime, 'collaboration', None)
    return directory(scopes, collaboration=bool(collaboration and collaboration.config.enabled))


def tool_definitions(scopes=None, *, collaboration=False, authorization='fixed'):
    result = []
    for tool, actions in directory(scopes, collaboration=collaboration).items():
        if not actions:
            continue
        item = {'name': tool, 'description': u.description(tool, actions),
                'inputSchema': u.input_schema(tool, actions),
                'annotations': dict(u.ANNOTATIONS[tool]),
                '_meta': {'securitySchemes': [{'type': 'oauth2', 'scopes': ['read']}],
                          'codepier/inputSchemaVersion': 'unified-v1'}}
        if any(action in actions for action in ('source_check', 'import')):
            item['_meta']['openai/fileParams'] = ['file']
            item['annotations']['openWorldHint'] = True
        if authorization == 'role':
            from shared.role_contracts import ROLE_SCOPE
            schemes = [{'type': 'oauth2', 'scopes': [ROLE_SCOPE]}]
            item['securitySchemes'] = schemes
            item['_meta']['securitySchemes'] = schemes
            item['_meta']['codepier/authorizationMode'] = 'role'
        result.append(item)
    return result


def catalog(scopes=None, *, collaboration=False):
    return {'tools': directory(scopes, collaboration=collaboration),
            'note': "Choose a view or use the default. query help accepts args.tool/action/view; "
                    "args.detail=schema returns machine schemas. Action-specific fields go in args."}


def convert_hint(name, arguments):
    from shared.public_collaboration import LEGACY_TOOLS, request
    from hub.core_tools import public_call
    try:
        if name in LEGACY_TOOLS:
            public = request(name, arguments)
            return u.unified_hint(public['tool'], public['arguments'])
        return u.unified_hint(*public_call(name, arguments))
    except (KeyError, ValueError):
        return None


def error_followup(recovery, name, raw, plan, visible):
    project = plan.project if plan else raw.get('project')
    project = project if isinstance(project, str) and 0 < len(project) <= 100 else ''
    workspace = plan.workspace_id if plan else ''
    if recovery['next_action'] == 'query_original_operation':
        hint = u.recovery_call(recovery['operation_id'], project=project, workspace_id=workspace,
                               after_output_seq=plan.after_output_seq if plan else None)
    elif recovery['category'] == 'argument_rejected' and 'help' in visible.get('query', {}):
        action = plan.action if plan else raw.get('action')
        arguments = {'action': 'help'}
        if project:
            arguments['project'] = project
        if isinstance(action, str) and action in visible.get(name, {}) and action != 'help':
            arguments['args'] = {'tool': name, 'action': action}
            view = plan.view if plan else raw.get('view')
            if view in visible[name][action]:
                arguments['args']['view'] = view
            options = raw.get('args', {})
            if name == 'query' and action == 'project' and isinstance(options, dict) and (
                    options.get('capture_baseline') or 'baseline_paths' in options):
                if 'baseline' in visible.get('act', {}):
                    arguments['args'] = {'tool': 'act', 'action': 'baseline'}
        elif isinstance(action, str) and name == 'query' and action in visible.get('act', {}):
            arguments['args'] = {'tool': 'act', 'action': action}
        hint = {'name': 'query', 'arguments': arguments}
    else:
        return None
    return hint if u.hint_is_visible(hint, visible) else None


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
    collaboration = getattr(runtime, 'collaboration', None)
    if 'event' in request:
        if set(request) & {'action', 'view', 'tool'}:
            raise DevError('INVALID_ARGUMENTS', 'Choose either an action or an event for help.', 422)
        if collaboration is None or not collaboration.config.events_enabled:
            raise DevError('EVENTS_DISABLED', 'Events are unavailable on this connection.', 403)
        plan.local = await runtime.store.run(collaboration.events.help, request['event'], user,
                                             request.get('detail', 'brief'))
        return
    if not request.get('action'):
        plan.local = catalog(user.scopes, collaboration=bool(collaboration and collaboration.config.enabled))
        return
    visible = runtime_directory(runtime, user.scopes)
    address = u.resolve_help(request['action'], request.get('view', ''), request.get('tool', ''), visible=visible)
    tool, action = address[:2]
    from hub.unified_help import action_help
    plan.local = action_help(address, visible[tool][action], project=request.get('project', ''),
                             workspace_id=request.get('workspace_id', ''), detail=request.get('detail', 'brief'),
                             scopes=user.scopes)


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
    if plan.collab and isinstance(value, dict):
        value = await _mint(runtime, plan, value, principal)
    if plan.wait and isinstance(value, dict) and value.get('pending') and isinstance(value.get('operation_id'), str):
        return await _wait(runtime, plan, value, principal)
    return value, plan.tool, plan.args
