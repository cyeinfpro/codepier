"""Two public tools over the existing facades: ``query`` (read-only) and ``act``.

Every unified call is translated into exactly one existing public facade call
(workspace, read, write, edit, exec, process, vps, browser, computer,
get_profile, get_access_context or one public collaboration tool). Validation,
authorization, idempotency, recovery and rendering then run through that
unchanged path. A unified call therefore reaches nothing the legacy call could
not reach, and every legacy facade operation keeps exactly one unified address.

This module is pure data and translation. Scope-aware discovery, credential
handles and response waiting live in ``hub.unified_tools``.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from shared.util import DevError
from shared.integration_contracts import NativeFile
from shared.knowledge_contracts import KNOWLEDGE_TOOLS

UNIFIED_TOOLS = frozenset({'query', 'act'})
DEFAULT_WAIT_SECONDS = 30
MAX_WAIT_SECONDS = 60

CQ, CD, CW = 'collaboration_query', 'collaboration', 'collaboration_work'


def _same(tool, *ops):
    return {op: (tool, op) for op in ops}


# action -> {view: (legacy tool, legacy operation)}. The first view is the default.
CORE_ACTIONS = {
    'query': {
        'knowledge': {'query': ('project_knowledge_query', None)},
        'file': {'file': ('read', 'file'), 'batch': ('read', 'batch')},
        'tree': {'tree': ('workspace', 'tree')},
        'search': {'sync': ('workspace', 'search'), 'start': ('process', 'search_start'),
                   'get': ('process', 'search_get'), 'cancel': ('process', 'search_cancel')},
        'changes': {'changes': ('read', 'changes'), 'reviews': ('read', 'reviews')},
        'history': {'history': ('read', 'history')},
        'symbols': {'symbols': ('read', 'symbols')},
        'artifacts': {'list': ('read', 'artifacts'), 'get': ('read', 'artifact')},
        'source_check': {'source_check': ('write', 'source_check')},
        'job': {'get': ('process', 'get'), 'wait': ('process', 'wait'), 'list': ('process', 'list'),
                'trace': ('process', 'trace'), 'diagnostics': ('process', 'diagnostics'),
                'activity': ('process', 'activity'), 'agent': ('process', 'agent'),
                'validation_get': ('process', 'validation_get'),
                'validation_list': ('process', 'validation_list')},
        'whoami': {'access': ('get_access_context', None), 'profile': ('get_profile', None)},
        'project': {'open': ('workspace', 'open'), 'list': ('workspace', 'list'),
                    'resolve': ('workspace', 'resolve'), 'context': ('workspace', 'context'),
                    'dashboard': ('workspace', 'dashboard'), 'status': ('workspace', 'status'),
                    'readiness': ('workspace', 'readiness'), 'lsp_status': ('workspace', 'lsp_status')},
        'skills': {'list': ('workspace', 'skills'), 'read': ('workspace', 'skill')},
        'tasks': {'list': ('workspace', 'tasks')},
        'workflow': {'list': ('workspace', 'workflow_list'), 'get': ('workspace', 'workflow_get'),
                     'handoff': ('workspace', 'handoff')},
        'worktrees': {'list': ('workspace', 'worktree_list')},
        'targets': {'list': ('vps', None)},
        'devices': {'list': ('workspace', 'devices')},
        'help': {'help': ('workspace', 'help')},
        'gui': {'status': ('browser', 'status'), 'app_status': ('computer', 'status'),
                'apps': ('computer', 'apps'), 'snapshot': ('browser', 'snapshot'),
                'observe': ('computer', 'observe')},
    },
    'act': {
        'knowledge': {'build': ('project_knowledge_build', None)},
        'exec': {'run': ('exec', None)},
        'cancel': {'cancel': ('process', 'cancel')},
        'validate': {'run': ('process', 'validate')},
        'lsp': {'query': ('read', 'lsp')},
        'edit': {'edit': ('edit', 'file'), 'write': ('write', 'file')},
        'restore': {'restore': ('edit', 'restore')},
        'checkpoint': {'checkpoint': ('edit', 'checkpoint')},
        'import': {'import': ('write', 'import')},
        'artifact': {'register': ('write', 'artifact')},
        'baseline': {'open': ('workspace', 'open'), 'manage': ('edit', 'reviews')},
        'project_create': {'create': ('workspace', 'project_create')},
        'worktree': {'create': ('workspace', 'worktree_create'), 'remove': ('workspace', 'worktree_remove')},
        'workflow': {'create': ('workspace', 'workflow_create'), 'update': ('workspace', 'workflow_update')},
        'gui': {'open': ('browser', 'open'), 'action': ('browser', 'action'), 'close': ('browser', 'close'),
                'app_open': ('computer', 'open'), 'app_action': ('computer', 'action'),
                'app_close': ('computer', 'close')},
    },
}

# Collaboration: 115 public actions grouped by noun. A view is the original
# public action name, so every business contract, idempotency record and
# receipt is reused verbatim.
COLLAB_ACTIONS = {
    'query': {
        'collab.inbox': _same(CQ, 'inbox', 'dot_inbox', 'dot_mailbox', 'room_inbox', 'dot_consumer', 'room_consumer'),
        'collab.get': _same(CQ, 'job', 'result', 'plan', 'thread', 'goal', 'coordination_goal', 'delegation', 'team',
                            'message_by_id', 'message_status', 'room_view', 'room_work', 'room_message_read',
                            'room_attachment_read', 'room_work_handoff_read', 'room_channel', 'room_connect',
                            'connection', 'dot_connection', 'dot_recovery', 'dot_receipt'),
        'collab.list': _same(CQ, 'overview', 'rooms', 'room_list', 'jobs', 'messages', 'room_messages', 'goals',
                             'coordination_goals', 'coordination_options', 'incidents', 'agents', 'subscriptions',
                             'join_slots', 'members', 'delegations', 'delegation_policies', 'teams', 'room_works',
                             'timeline'),
        'collab.changes': _same(CQ, 'changes', 'room_changes', 'room_project_impact'),
        'collab.context': _same(CQ, 'room_context'),
        'collab.search': _same(CQ, 'search'),
        'collab.evidence': _same(CQ, 'result_evidence', 'work_evidence'),
        'collab.diagnostics': _same(CQ, 'room_diagnostics'),
        'collab.validate': _same(CQ, 'plan_validate'),
    },
    'act': {
        'collab.connect': _same(CD, 'join', 'room_connect_join', 'room_connect_resume', 'room_probe_read',
                                'room_probe_reply', 'room_channel_connect', 'room_channel_action',
                                'dot_mailbox_configure'),
        'collab.consume': _same(CD, 'consumer_begin', 'consumer_heartbeat', 'consumer_yield', 'room_consumer_begin',
                                'room_consumer_heartbeat', 'room_consumer_yield'),
        'collab.ack': {**_same(CD, 'dot_ack', 'dot_mailbox_ack', 'room_inbox_ack'), **_same(CW, 'ack')},
        'collab.recover': _same(CD, 'dot_mailbox_recover', 'room_inbox_recover'),
        'collab.send': _same(CD, 'room_reply', 'room_message', 'message', 'dot_message', 'dot_message_v2',
                             'dot_update', 'goal_message', 'command'),
        'collab.handle': _same(CD, 'dot_handle'),
        'collab.goal': _same(CD, 'goal_create', 'goal_update'),
        'collab.plan': _same(CD, 'plan_save'),
        'collab.work': {**_same(CW, 'claim', 'heartbeat', 'progress', 'create', 'assign', 'from_message',
                                'team_from_message', 'execute', 'job_evidence', 'analysis_claim',
                                'analysis_heartbeat'),
                        **_same(CD, 'room_work_start')},
        'collab.finish': {**_same(CW, 'result', 'result_v2', 'archive_result', 'analysis_result', 'analysis_block'),
                          **_same(CD, 'team_publish')},
        'collab.handoff': _same(CD, 'room_work_handoff', 'room_work_control', 'team_handoff', 'team_takeover'),
        'collab.room': _same(CD, 'room_create', 'room_update', 'room_migrate', 'room_projects', 'room_member_create',
                             'room_context_update'),
    },
}

ACTIONS = {tool: {**CORE_ACTIONS[tool], **COLLAB_ACTIONS[tool]} for tool in ('query', 'act')}
COLLAB_TOOLS = frozenset({CQ, CD, CW})
STANDALONE_TOOLS = KNOWLEDGE_TOOLS | {'get_profile', 'get_access_context'}
IMPLICIT_OPERATION = {'exec': 'run', 'vps': 'list'}
# Read-only discovery of a computer-scoped surface is still free of side effects.
READ_ONLY_COMPUTER_TARGETS = frozenset({'browser_snapshot', 'computer_observe', 'computer_apps'})

TOP_LEVEL = {
    'query': ('project', 'workspace_id', 'path', 'offset', 'limit', 'ids', 'file'),
    'act': ('project', 'workspace_id', 'idempotency_key', 'command', 'cwd', 'target', 'timeout_seconds',
            'path', 'edits', 'content', 'expected_sha256', 'ids', 'file'),
}
CONTROL_FIELDS = frozenset({'action', 'view', 'args', 'wait'})
NO_WAIT_ACTIONS = frozenset({'job', 'cancel', 'help', 'whoami', 'targets'})

# Fields a collaboration handle may carry. They are the existing credential and
# binding fields; the handle only stops the model from re-sending them.
HANDLE_FIELDS = ('consumer_session_id', 'consumer_fence', 'consumer_proof', 'connection_proof',
                 'binding_generation', 'member_id', 'conversation_id', 'dot_id')


class _Envelope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: str = Field(min_length=1, max_length=64)
    view: str = Field(default='', max_length=64)
    project: str = Field(default='', max_length=100)
    workspace_id: str = Field(default='', pattern=r'^(|[a-f0-9]{32})$')
    path: str = Field(default='', max_length=1024)
    offset: int | None = Field(default=None, ge=0, le=1048576)
    limit: int | None = Field(default=None, ge=1, le=100000)
    ids: list[str] = Field(default_factory=list, max_length=16)
    wait: int | None = Field(default=None, ge=0, le=MAX_WAIT_SECONDS)
    args: dict[str, Any] = Field(default_factory=dict)
    file: NativeFile | None = None


class QueryCall(_Envelope):
    pass


class ActCall(_Envelope):
    idempotency_key: str = Field(default='', max_length=128)
    command: str = Field(default='', max_length=65536)
    cwd: str = Field(default='', max_length=4096)
    target: str = Field(default='', max_length=132)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)
    edits: list[dict[str, Any]] | None = Field(default=None, max_length=20)
    content: str | None = Field(default=None, max_length=1048576)
    expected_sha256: str = Field(default='', max_length=64)


MODELS = {'query': QueryCall, 'act': ActCall}


@dataclass
class Plan:
    """One translated call. ``tool``/``args`` are the exact legacy public call."""
    unified: str
    action: str
    view: str
    tool: str
    args: dict
    wait: int = 0
    collab: bool = False
    handle: str = ''
    help_for: dict | None = None
    local: Any = None
    notes: list = field(default_factory=list)
    project: str = ''
    workspace_id: str = ''
    after_output_seq: int | None = None


def _fields(tool):
    from shared.contracts import TOOLS
    return TOOLS[tool].model.model_fields


def legacy_call(tool, operation, flat):
    """Shape flat arguments exactly as the legacy facade expects them."""
    from shared.core_contracts import CORE_ACTIONS as LEGACY_ADVANCED
    rest = dict(flat)
    if tool in COLLAB_TOOLS:
        if 'action' in rest:
            raise DevError('INVALID_ARGUMENTS', '协作资源子动作请使用 flow_action；action 由 view 决定', 422)
        return {'action': operation, **rest}
    fields = _fields(tool)
    legacy = {}
    if operation is not None and 'operation' in fields:
        legacy['operation'] = operation
    advanced = operation in LEGACY_ADVANCED.get(tool, {}) or (tool == 'read' and operation == 'batch')
    if advanced:
        outer = {'project', 'workspace_id', 'idempotency_key'}
        if tool == 'write' and operation in {'import', 'source_check'}:
            outer.add('file')
        for key in sorted(outer & rest.keys()):
            legacy[key] = rest.pop(key)
        if rest:
            legacy['options'] = rest
        return legacy
    for key in list(rest):
        if key in fields and key not in {'operation', 'options'}:
            legacy[key] = rest.pop(key)
    if rest:
        if 'options' not in fields:
            raise DevError('INVALID_ARGUMENTS', f'{tool}.{operation or "call"} 不接受参数：' + ', '.join(sorted(rest)), 422)
        legacy['options'] = rest
    return legacy


def _auto_view(action, flat):
    if action == 'file' and 'items' in flat:
        return 'batch'
    if action == 'edit' and flat.get('content') is not None and not ({'edits', 'changes'} & flat.keys()):
        return 'write'
    if action == 'job':
        return 'get' if flat.get('ids') else 'list'
    if action == 'project':
        return 'open' if flat.get('project') else 'list'
    return ''


def parse(tool, raw):
    if tool not in MODELS:
        raise DevError('UNKNOWN_TOOL', '未知的统一工具', 404)
    if not isinstance(raw, dict):
        raise DevError('INVALID_ARGUMENTS', '参数必须是对象', 422)
    try:
        return MODELS[tool].model_validate(raw)
    except ValidationError as exc:
        issues = [{'field': '.'.join(map(str, e['loc'])), 'message': e['msg']} for e in exc.errors()]
        raise DevError('INVALID_ARGUMENTS', '参数不符合统一工具契约', 422, issues=issues) from None


def translate(tool, raw):
    """Unified call -> Plan with the exact legacy public tool call."""
    call = parse(tool, raw)
    spec = ACTIONS[tool].get(call.action)
    if spec is None:
        other = 'act' if tool == 'query' else 'query'
        hint = f'；此 action 属于 {other}' if call.action in ACTIONS[other] else ''
        raise DevError('UNKNOWN_OPERATION', f'{tool} 没有 action {call.action}{hint}', 422,
                       actions=sorted(ACTIONS[tool]))
    flat = {key: getattr(call, key) for key in call.model_fields_set if key not in CONTROL_FIELDS}
    if isinstance(flat.get('file'), NativeFile):
        flat['file'] = flat['file'].model_dump(exclude_unset=True)
    overlap = flat.keys() & call.args.keys()
    if overlap:
        raise DevError('INVALID_ARGUMENTS', '同一参数不能同时出现在顶层和 args：' + ', '.join(sorted(overlap)), 422)
    flat.update(copy.deepcopy(call.args))
    handle = ''
    if call.action.startswith('collab.'):
        handle = flat.pop('handle', '') or ''
        if not isinstance(handle, str):
            raise DevError('INVALID_ARGUMENTS', 'handle 必须是字符串', 422)
    view = call.view or _auto_view(call.action, flat) or next(iter(spec))
    if view not in spec:
        raise DevError('INVALID_ARGUMENTS', f'{tool}.{call.action} 没有 view {view}', 422, views=list(spec))
    legacy_tool, operation = spec[view]
    if 'ids' in flat:
        ids = flat.pop('ids')
        if legacy_tool != 'process':
            raise DevError('INVALID_ARGUMENTS', 'ids 只用于 job、cancel 与 search', 422)
        flat['operation_ids'] = ids
    if call.action == 'help':
        allowed = {'project', 'workspace_id', 'action', 'view', 'tool', 'detail', 'event'}
        if flat.keys() - allowed or any(not isinstance(value, str) for value in flat.values()):
            raise DevError('INVALID_ARGUMENTS', 'help accepts project, workspace_id and args.action/view/tool/detail/event', 422)
        if flat.get('detail', 'brief') not in {'brief', 'schema'}:
            raise DevError('INVALID_ARGUMENTS', 'help detail must be brief or schema', 422)
        return Plan(tool, call.action, view, 'workspace', {'operation': 'help'},
                    help_for=flat, project=call.project, workspace_id=call.workspace_id)
    if tool == 'query' and call.action == 'project' and (flat.get('capture_baseline') or 'baseline_paths' in flat):
        raise DevError('INVALID_ARGUMENTS', 'Use act(action="baseline") to capture source paths.', 422)
    if tool == 'act' and call.action == 'baseline' and view == 'open':
        if not isinstance(flat.get('idempotency_key'), str) or len(flat['idempotency_key']) < 8:
            raise DevError('INVALID_ARGUMENTS', 'baseline requires idempotency_key', 422)
        flat['capture_baseline'] = True
    args = legacy_call(legacy_tool, operation, flat)
    if call.wait is not None:
        wait = call.wait
    else:
        wait = 0 if (call.action in NO_WAIT_ACTIONS or call.action.startswith('collab.')) else DEFAULT_WAIT_SECONDS
    return Plan(tool, call.action, view, legacy_tool, args, wait=wait,
                collab=legacy_tool in COLLAB_TOOLS, handle=handle,
                project=args.get('project', ''), workspace_id=args.get('workspace_id', ''),
                after_output_seq=args.get('after_output_seq'))


def resolve_help(target, view='', tool='', *, visible=None):
    """Map a unified (tool, action, view) to the current legacy help address."""
    directory = ACTIONS if visible is None else visible
    if tool and tool not in ACTIONS:
        raise DevError('INVALID_ARGUMENTS', 'help tool must be query or act', 422)
    tools = (tool,) if tool else ACTIONS
    known = [t for t in tools if target in ACTIONS[t]]
    candidates = [t for t in known if target in directory.get(t, {})]
    if not candidates:
        if known:
            raise DevError('INSUFFICIENT_SCOPE', 'This action is outside the current tool directory.', 403)
        raise DevError('UNKNOWN_OPERATION', f'Unknown action {target}', 422)
    unified = next((t for t in candidates if view in directory[t][target]), candidates[0])
    views = directory[unified][target]
    view = view or next(iter(views))
    if view not in views:
        if any(view in ACTIONS[t][target] for t in known):
            raise DevError('INSUFFICIENT_SCOPE', 'This view is outside the current tool directory.', 403)
        raise DevError('INVALID_ARGUMENTS', f'{unified}.{target} has no view {view}', 422, views=list(views))
    legacy_tool, operation = ACTIONS[unified][target][view]
    return unified, target, view, legacy_tool, operation or IMPLICIT_OPERATION.get(legacy_tool)


# ---- Presentation ---------------------------------------------------------

def _reverse():
    table = {}
    for unified in ('query', 'act'):
        for action, views in ACTIONS[unified].items():
            for view, (legacy_tool, operation) in views.items():
                key = (legacy_tool, operation or IMPLICIT_OPERATION.get(legacy_tool))
                table.setdefault(key, (unified, action, view))
    return table


REVERSE = _reverse()
_DEFAULT_OPERATION = {'workspace': 'list', 'process': 'list', 'browser': 'status', 'computer': 'status',
                      'read': 'file', 'write': 'file', 'edit': 'file', 'exec': 'run', 'vps': 'list'}


def unified_hint(name, arguments):
    """Rewrite one legacy next-call hint to its unified address, or None."""
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return None
    if name in UNIFIED_TOOLS:
        return {'name': name, 'arguments': copy.deepcopy(arguments)}
    name = {'project_query': 'workspace', 'task_query': 'process'}.get(name, name)
    args = copy.deepcopy(arguments)
    if name in COLLAB_TOOLS:
        operation = args.pop('action', None)
    elif name in STANDALONE_TOOLS:
        operation = None
    else:
        operation = args.pop('operation', _DEFAULT_OPERATION.get(name))
    key = (name, operation)
    if key == ('workspace', 'help') and args.get('action'):
        help_tool = {'project_query': 'workspace', 'task_query': 'process'}.get(args.get('tool'), args.get('tool'))
        address = REVERSE.get((help_tool, args['action']))
        if address is None:
            return None  # Keep an unknown legacy help pointer intact.
        args['tool'], args['action'], args['view'] = address
    options = args.pop('options', None)
    if isinstance(options, dict):
        for k, v in options.items():
            args.setdefault(k, v)
    if key == ('workspace', 'open') and args.get('capture_baseline'):
        unified, action, view = 'act', 'baseline', 'open'
        args.pop('capture_baseline')
    elif key in REVERSE:
        unified, action, view = REVERSE[key]
    else:
        return None
    if 'operation_ids' in args:
        args['ids'] = args.pop('operation_ids')
    out = {'action': action}
    if view != next(iter(ACTIONS[unified][action])):
        out['view'] = view
    for k in TOP_LEVEL[unified]:
        if k in args:
            out[k] = args.pop(k)
    if args:
        out['args'] = args
    return {'name': unified, 'arguments': out}


def hint_is_visible(hint, visible):
    if visible is None:
        visible = ACTIONS
    try:
        plan = translate(hint['name'], hint['arguments'])
    except DevError:
        return False
    if plan.view not in visible.get(plan.unified, {}).get(plan.action, ()):
        return False
    if plan.help_for and plan.help_for.get('action'):
        request = plan.help_for
        try:
            resolve_help(request['action'], request.get('view', ''), request.get('tool', ''), visible=visible)
        except DevError:
            return False
    return True


def recovery_call(identifier, *, project='', workspace_id='', after_output_seq=None, view='get'):
    arguments = {'action': 'job', 'view': view, 'ids': [identifier]}
    if project:
        arguments['project'] = project
    if workspace_id:
        arguments['workspace_id'] = workspace_id
    if after_output_seq is not None:
        arguments['args'] = {'after_output_seq': after_output_seq}
    return {'name': 'query', 'arguments': arguments}


def _help_call(tool, action, view=''):
    args = {'tool': tool, 'action': action}
    if view:
        args['view'] = view
    return {'name': 'query', 'arguments': {'action': 'help', 'args': args}}


_STRING_HINTS = {
    'project_query(help, tool, action)': {'name': 'query', 'arguments': {'action': 'help'}},
    'project_query(tree, path=docs)': {'name': 'query', 'arguments': {'action': 'tree', 'path': 'docs'}},
    'project_query.skills': _help_call('query', 'skills'),
    'project_query': _help_call('query', 'project'),
    'project_knowledge_query': _help_call('query', 'knowledge'),
    'project_knowledge_build': _help_call('act', 'knowledge'),
    'skills_read': _help_call('query', 'skills', 'read'),
    'show_changes': _help_call('query', 'changes'),
    'fs_read': _help_call('query', 'file'),
}
_JOB_HINTS = {'task_query', 'operations_wait', 'operations_get'}
_NEXT_KEYS = {'next', 'next_call', 'documents_next', 'read_hint', 'help'}


def _rewrite_hints(value, *, visible, project, workspace_id, convert):
    if isinstance(value, list):
        for item in value:
            _rewrite_hints(item, visible=visible, project=project, workspace_id=workspace_id, convert=convert)
        return
    if not isinstance(value, dict):
        return

    def project_hint(hint):
        if isinstance(hint, dict):
            name = hint.get('name', hint.get('tool'))
            args = hint.get('arguments')
            if isinstance(name, str) and isinstance(args, dict):
                new = unified_hint(name, args)
                if new is None and convert:
                    new = convert(name, args)
                if new is None:
                    # A protocol request (e.g. events/subscribe) is not a tool.
                    # Do not advertise incompatible legacy tool fallbacks.
                    return None
            else:
                return hint
        elif isinstance(hint, str) and hint in _JOB_HINTS and value.get('operation_id'):
            new = recovery_call(value['operation_id'], view='wait' if value.get('pending') else 'get',
                                after_output_seq=(value.get('recovery') or {}).get('after_output_seq'))
        elif isinstance(hint, str) and hint == 'searches_get' and value.get('search_id'):
            new = {'name': 'query', 'arguments': {'action': 'search', 'view': 'get',
                   'args': {'search_id': value['search_id'], 'cursor': value.get('cursor', 0)}}}
        elif hint == 'read' and isinstance(value.get('path'), str):
            # A path is already next to this hint in the index. Do not repeat
            # the full call for every document in an initial project open.
            return 'query file' if visible is None or 'file' in visible.get('query', {}) else None
        elif isinstance(hint, str) and hint in _STRING_HINTS:
            new = copy.deepcopy(_STRING_HINTS[hint])
        elif isinstance(hint, str) and hint.startswith('fs_read / skills_read'):
            new = _help_call('query', 'file')
        else:
            return hint
        args = new['arguments']
        if project and not str(args.get('action', '')).startswith('collab.'):
            args.setdefault('project', project)
            if workspace_id:
                args.setdefault('workspace_id', workspace_id)
        if not hint_is_visible(new, visible):
            return None
        if isinstance(hint, dict):
            return {**{k: v for k, v in hint.items() if k not in {'name', 'tool', 'arguments'}}, **new}
        return new

    for key, hint in list(value.items()):
        if key in _NEXT_KEYS or (key.endswith('_request') and key != 'subscription_request'):
            value[key] = project_hint(hint)
    index = value.get('capability_index')
    if isinstance(index, dict):
        if 'search' in index:
            index['search'] = _help_call('query', 'search', 'sync')
        value['capability_index'] = {key: projected for key, hint in index.items()
                                     if (projected := project_hint(hint)) is not None}
    # These are server result envelopes, not file contents or arbitrary tool
    # arguments. Collaboration records can nest resume and acknowledgement calls.
    for key in ('result', 'data', 'operations', 'execution', 'context', 'knowledge', 'messages',
                'items', 'work', 'work_items', 'entries', 'configuration', 'page', 'setup',
                'skills_catalog', 'files', 'documents', 'instruction_index'):
        if isinstance(value.get(key), (dict, list)):
            _rewrite_hints(value[key], visible=visible, project=project, workspace_id=workspace_id, convert=convert)


def _slim(row):
    """Drop presentation duplicates only; the stored receipt is unchanged."""
    if isinstance(row, list):
        for item in row:
            _slim(item)
        return
    if not isinstance(row, dict):
        return
    nested = row.get('result')
    data = nested.get('data') if isinstance(nested, dict) else None
    if isinstance(data, dict) and isinstance(row.get('output'), str) and data.get('output') == row['output']:
        data.pop('output')
        data['output_at'] = 'output'
    terminal = not row.get('pending') and not row.get('error')
    if terminal and isinstance(row.get('recovery'), dict) and row['recovery'].get('task_outcome_confirmed') \
            and row['recovery'].get('task_state') == 'succeeded':
        row.pop('recovery')
    if terminal and 'next_call' in row and row['next_call'] is None:
        row.pop('next_call')
    if isinstance(data, dict) and data.get('command_ok') is True:
        context = data.get('failure_context')
        if isinstance(context, dict) and context.get('category') == 'succeeded':
            data.pop('failure_context')
        data.pop('continuation', None)
    _slim(row.get('operations'))


_SCHEDULING_FIELDS = {'created', 'updated', 'accepted_at', 'attempts', 'next_attempt', 'deadline',
                      'last_operation_verified_at', 'last_output_at', 'transport_error', 'cancel_requested'}


def _exec_data(row):
    result = row.get('result')
    data = result.get('data') if isinstance(result, dict) else row
    return data if isinstance(data, dict) else {}


def _command_error(row, *, project, workspace_id, visible):
    from shared.runtime_results import failure_context
    data = _exec_data(row)
    if data.get('command_ok') is not False and (
            type(data.get('exit_code')) is not int or data['exit_code'] == 0):
        return
    context = failure_context(data={**data, 'output': row.get('output', data.get('output'))},
                              operation_id=row.get('operation_id'))
    saved = data.get('failure_context') or {}
    if context['category'] != 'command_missing' and saved.get('category'):
        context = saved
    code = context.get('code') or {
        'command_failed': 'COMMAND_FAILED', 'command_timeout': 'COMMAND_TIMEOUT',
        'cancelled': 'CANCELLED', 'outcome_unconfirmed': 'OPERATION_OUTCOME_UNCONFIRMED',
    }.get(context['category'], 'EXECUTION_FAILED')
    row['error_code'] = code
    if not isinstance(row.get('error'), dict):
        message = row.get('error') or f"Execution ended with exit code {data.get('exit_code')}."
        row['error'] = {'code': code, 'message': message}
    if code == 'COMMAND_MISSING' and project and not str(data.get('target', '')).startswith('vps'):
        from shared.runtime_results import missing_shell_command
        command = missing_shell_command(row.get('output', data.get('output')))
        hint = _help_call('query', 'search', 'sync')
        if command in {'rg', 'grep', 'ag', 'ack'} and hint_is_visible(hint, visible):
            hint['arguments']['project'] = project
            if workspace_id:
                hint['arguments']['workspace_id'] = workspace_id
            row['search_help'] = hint


def _present_exec(row, *, project, workspace_id, visible, lean):
    data = _exec_data(row)
    _command_error(row, project=project, workspace_id=workspace_id, visible=visible)
    if row.get('pending'):
        for key in _SCHEDULING_FIELDS - {'transport_error', 'cancel_requested'} if lean else ():
            row.pop(key, None)
        return row
    if not lean:
        return row
    # A synchronous dispatch and an inline wait both return one execution. Keep
    # the same public keys regardless of which path delivered the terminal row.
    if type(data.get('exit_code')) is not int:
        return row
    result = {**data, **row}
    if data.get('output_truncated'):
        result['output_truncated'] = True
    result.pop('result', None)
    result.pop('output_at', None)
    identifier = result.get('operation_id') or result.get('id')
    if identifier:
        result['operation_id'] = identifier
        result.pop('id', None)
    result.setdefault('state', 'failed' if result.get('error') else 'succeeded')
    if result['state'] == 'succeeded':
        for key in _SCHEDULING_FIELDS | {'args_summary', 'project_id', 'tool', 'output_chars',
                'next', 'next_call', 'retry_after_seconds', 'elapsed_seconds', 'log_ref',
                'resource_coordination', 'continuation'}:
            result.pop(key, None)
        for key in ('cancelled', 'timed_out'):
            if result.get(key) is False:
                result.pop(key)
        if (result.get('failure_context') or {}).get('category') == 'succeeded':
            result.pop('failure_context')
        runtime = result.get('runtime')
        if isinstance(runtime, dict) and runtime.get('runtime_ref'):
            result['runtime'] = {key: runtime[key] for key in ('runtime_ref', 'revision', 'mode') if key in runtime}
    return result


def _project_list(value):
    if not isinstance(value.get('projects'), list):
        return
    common = None
    rows = value['projects']
    for row in rows:
        connection = row.get('connection') or {}
        if common is None:
            common = {key: connection[key] for key in ('source', 'observed_at') if key in connection}
        else:
            common = {key: val for key, val in common.items() if connection.get(key) == val}
    for row in rows:
        if row.get('root') == '.':
            row.pop('root')
        if not row.get('description'):
            row.pop('description', None)
        connection = row.get('connection') or {}
        for key in common or {}:
            connection.pop(key, None)
        if connection.get('state') == ('online' if row.get('online') else 'offline'):
            connection.pop('state')
        if not connection:
            row.pop('connection', None)
    if common:
        value['connection_observation'] = common


def present_unified(value, *, verbosity='lean', plan=None, visible=None, convert=None):
    """Presentation-only projection for unified callers."""
    result = copy.deepcopy(value)
    lean = verbosity != 'full'
    project, workspace_id = (plan.project, plan.workspace_id) if plan else ('', '')
    if plan and plan.action == 'exec':
        rows = result.get('operations')
        if isinstance(rows, list) and len(rows) == 1:
            result = rows[0]
        result = _present_exec(result, project=project, workspace_id=workspace_id, visible=visible, lean=lean)
    elif plan and plan.action == 'job':
        for row in result.get('operations', []):
            _command_error(row, project=project, workspace_id=workspace_id, visible=visible)
            if lean and row.get('state') == 'succeeded' and not row.get('execution') and not row.get('events'):
                for key in _SCHEDULING_FIELDS:
                    row.pop(key, None)
    if lean:
        _slim(result)
        if plan and (plan.action, plan.view) == ('project', 'list'):
            _project_list(result)
        if 'context_id' in result:
            result.pop('help', None)
            if result.get('context_unchanged') and result.get('context') is None:
                result.pop('capability_index', None)
    _rewrite_hints(result, visible=visible, project=project, workspace_id=workspace_id, convert=convert)
    return result


# ---- Discovery ------------------------------------------------------------

_GUIDANCE = {
    'query': {
        'project': 'Open by project name. Reuse args.context_id only when you have the prior context.',
        'file': 'Read by path or batch up to 8 args.items. Follow next_offset and use the returned SHA for edits.',
        'tree': 'Tree is compact by default. Use args.format=structured for structured entries.',
        'search': 'Search defaults to sync. Use view=start for paginated text/symbol/reference search.',
        'job': 'Read original ids. Pass args.after_output_seq for incremental output; use view=diagnostics or trace for details.',
    },
    'act': {
        'exec': 'Exec takes command or args.task, with project and idempotency_key. wait is response time (default 30s).',
        'edit': 'Edit uses unique replacements, whole content or args.changes with current SHA (new for creation).',
        'baseline': 'Capture a baseline before edits. args.baseline_paths accepts exact relative paths.',
        'collab.handle': 'Collaboration handles reduce repeated credentials without changing authority.',
    },
}


def description(tool, actions):
    lead = ('Read authorized project data and original receipts.' if tool == 'query'
            else 'Change authorized project data or run an operation. Use a unique idempotency_key for each mutation.')
    parts = [lead, *[hint for action, hint in _GUIDANCE[tool].items() if action in actions]]
    if any(action.startswith('collab.') for action in actions):
        parts.append('Collaboration views use the resource action name.')
    if tool == 'act':
        parts.append('Recover pending or uncertain work through query job on its original ID.')
    parts.append("Put action-specific fields in args. Use query(action='help', args={tool,action,view}) for a ready-to-use example and parameters.")
    return ' '.join(parts)


ANNOTATIONS = {
    'query': {'readOnlyHint': True, 'destructiveHint': False, 'idempotentHint': True, 'openWorldHint': False},
    'act': {'readOnlyHint': False, 'destructiveHint': True, 'idempotentHint': True, 'openWorldHint': True},
}

_STR = {'type': 'string'}
_PROPERTIES = {
    'file': NativeFile.model_json_schema(),
    'view': {'type': 'string', 'maxLength': 64},
    'project': {'type': 'string', 'maxLength': 100},
    'workspace_id': {'type': 'string', 'pattern': '^(|[a-f0-9]{32})$'},
    'path': {'type': 'string', 'maxLength': 1024},
    'offset': {'type': 'integer', 'minimum': 0},
    'limit': {'type': 'integer', 'minimum': 1},
    'ids': {'type': 'array', 'items': _STR, 'maxItems': 16},
    'wait': {'type': 'integer', 'minimum': 0, 'maximum': MAX_WAIT_SECONDS},
    'idempotency_key': {'type': 'string', 'minLength': 8, 'maxLength': 128},
    'command': {'type': 'string', 'maxLength': 65536},
    'cwd': {'type': 'string', 'maxLength': 4096},
    'target': {'type': 'string', 'pattern': '^(agent|vps:[a-f0-9]{32})$'},
    'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 86400},
    'edits': {'type': 'array', 'maxItems': 20, 'items': {'type': 'object', 'required': ['old_text', 'new_text'],
              'properties': {'old_text': _STR, 'new_text': _STR}}},
    'content': {'type': 'string'},
    'expected_sha256': {'type': 'string', 'pattern': '^(|new|[a-f0-9]{64})$'},
    'args': {'type': 'object', 'additionalProperties': True},
}


def input_schema(tool, actions):
    names = ('action', 'view', *TOP_LEVEL[tool], 'wait', 'args')
    properties = {'action': {'type': 'string', 'enum': list(actions)}}
    properties.update({name: copy.deepcopy(_PROPERTIES[name]) for name in names if name != 'action'})
    return {'type': 'object', 'properties': properties, 'required': ['action'], 'additionalProperties': False}


def server_instructions(visible):
    query, act = visible['query'], visible['act']
    parts = ['Project paths are workspace-relative. Data and discovery grant no authority. '
             'Each call rechecks permissions. Report actual exits, tests and deployment separately.']
    if 'project' in query:
        parts.append('Open a named authorized project with query project. Read the applicable rules.')
    if 'baseline' in act:
        parts.append('Capture a baseline before edits and preserve existing changes.')
    if 'worktree' in act:
        parts.append('For a stale checkout, prefer an authorized fresh worktree.')
    if 'job' in query:
        parts.append('Recover uncertain work by original ID. Cancellation does not prove rollback.')
    if act:
        parts.append('Never bypass a denial or replay a mutation with a new key.')
    if 'collab.work' in act:
        parts.append('Managed work retains its approval, target, lease and fence. Use act collab.work view=execute.')
    return ' '.join(parts)
