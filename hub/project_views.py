"""Project discovery and negotiated read-only views; never a new grant authority."""
from __future__ import annotations
import hashlib
import json
import time
import unicodedata
from hub.principal import refresh_principal
from shared.util import DevError


def authorized_projects(runtime, principal):
    principal = refresh_principal(runtime.store, principal)
    rows = runtime.list_projects(principal)
    visible = []
    for row in rows:
        try:
            runtime.authorize(principal, 'read', project_id=row['id'])
        except DevError as exc:
            if exc.status in {403, 404}:
                continue
            raise
        visible.append(row)
    return visible


def list_projects(runtime, args, principal):
    visible = authorized_projects(runtime, principal)
    normalize = lambda value: unicodedata.normalize('NFKC', value).casefold()
    query = normalize(args.get('query', ''))
    matches = [row for row in visible if query in normalize(row['alias'])
               or query in normalize(row.get('description', '')) or query == normalize(row['id'])]
    matches.sort(key=lambda row: (normalize(row['alias']), row['id']))
    offset, limit = args.get('offset', 0), args.get('limit', 30)
    end = min(offset + limit, len(matches))
    now = time.time()
    return {'projects': [{**row, 'connection': {'state': 'online' if row['online'] else 'offline',
            'source': 'hub_connection', 'observed_at': now}} for row in matches[offset:end]],
        'total': len(matches), 'offset': offset, 'next_offset': end if end < len(matches) else None,
        'view_version': 1, 'query': args.get('query', '')}


def prepare_view_packet(runtime, packet, principal, connection):
    # Modify only the outgoing copy, not the persisted request or fingerprint.
    name = packet.get('tool')
    if name not in {'open_workspace', 'readiness_get'}:
        return
    args = dict(packet['args'])
    if getattr(connection, 'project_view_protocol', 0) != 1:
        args.pop('context_detail', None)
        args.pop('operation_kind', None)
        if name == 'open_workspace':
            args['context_id'] = ''  # Legacy context IDs do not bind the new detail/policy revision.
    packet['args'] = args
    packet['integration_context']['authority_revision'] = authority_revision(runtime, principal)


def authority_revision(runtime, principal):
    # Code-defined policy versions, never a new grant or authorization cache.
    revisions = {'grant_id': principal.grant_id, 'user_epoch': principal.user_epoch,
                 'scopes': sorted(principal.scopes), 'projects': sorted(principal.projects),
                 'profile_id': principal.profile_id, 'role_id': principal.role_id,
                 'space_id': getattr(principal, 'space_id', None)}
    for table, identifier, key in (('grants', principal.grant_id, 'grant'),
            ('access_profiles', principal.profile_id, 'profile'), ('access_roles', principal.role_id, 'role')):
        if identifier:
            row = runtime.store.one(f'SELECT * FROM {table} WHERE id=?', (identifier,))
            revisions[key] = {k: v for k, v in (row or {}).items()
                              if k in {'version', 'updated', 'revoked', 'scopes', 'projects', 'policy', 'enabled'}}
    if principal.grant_id:
        from hub.room_scope import current
        room = current(runtime.store, principal.grant_id)
        if room:
            revisions['room'] = {k: room.get(k) for k in ('version', 'manifest_digest', 'expires_at', 'state')}
    return hashlib.sha256(json.dumps(revisions, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validate_context(runtime, args, data, principal, *, operation=None):
    """Reject late/old cached context when its original authorized mapping changes.

    This never mutates the stored result and never turns a failed observation
    into a successful operation. Legacy peers have payload reuse disabled.
    """
    principal = refresh_principal(runtime.store, principal)
    project = runtime.project(args['project'], principal)
    bound = data.get('_project_view_binding')
    if isinstance(bound, dict):
        expected = {'project_id': project['id'], 'root': project['root'],
            'device_id': project['device_id'], 'workspace_id': args.get('workspace_id', ''),
            'mode': project.get('mode'), 'allow_tasks': bool(project.get('allow_tasks'))}
        if any(bound.get(k) != v for k, v in expected.items()) or (
                bound.get('authority_revision') != authority_revision(runtime, principal)):
            raise DevError('PROJECT_CONTEXT_CHANGED', 'Project mapping or authority changed after this context was observed; request a fresh open without context_id.', 409)
    elif operation:
        # The durable encrypted request is the legacy binding, not a user-supplied
        # summary. Read it only for this operation after normal receipt authorization.
        row = runtime.store.one('SELECT payload FROM operations WHERE id=?', (operation,))
        request = json.loads(runtime.store.decrypt(row['payload'])) if row and row.get('payload') else None
        original = request.get('project') if isinstance(request, dict) else None
        if original and any(original.get(k) != project.get(k) for k in ('id', 'root', 'device_id', 'mode', 'allow_tasks')):
            raise DevError('PROJECT_CONTEXT_CHANGED', 'Legacy context belongs to a changed project mapping; request a fresh open.', 409)
    return data


def enrich_readiness(runtime, args, data, principal):
    """Recheck current Hub authority on every direct/polled observation.

    The original node snapshot remains immutable. Historical local observations
    carry their original timestamp; neither this query nor a grant is host approval.
    """
    from copy import deepcopy
    from hub.room_scope import require_tool
    from shared.project_view_contracts import KINDS, unknown_decision, platform_unknown
    kind = args['operation_kind']
    scope, tool = KINDS[kind]
    principal = refresh_principal(runtime.store, principal)
    project = runtime.project(args['project'], principal)
    now = time.time()
    hub = {'state': 'allowed', 'source': 'hub_current_policy', 'observed_at': now,
           'scope': 'project_defaults'}
    target = {'project': project['id'], 'workspace_id': args.get('workspace_id', ''), 'target': 'agent'}
    try:
        if scope not in principal.scopes:
            raise DevError('INSUFFICIENT_SCOPE', 'Current credential lacks the action scope', 403)
        runtime.authorize(principal, scope, project_id=project['id'])
        require_tool(runtime.store, principal, tool, target)
        if kind != 'file_read' and project['mode'] != 'write':
            raise DevError('READ_ONLY', 'Project is read-only', 403)
        if kind == 'command_exec' and not project['allow_tasks']:
            raise DevError('TASKS_DISABLED', 'Project execution is disabled', 403)
        runtime.integrations.guard(tool, target, project, principal)
    except DevError as exc:
        hub.update(state='denied', code=exc.code)
    expected = {'project_id': project['id'], 'root': project['root'],
                'device_id': project['device_id'], 'workspace_id': args.get('workspace_id', '')}
    bound = data.get('_readiness_binding') == expected and data.get('operation_kind') == kind
    local = deepcopy(data.get('local_authorization')) if bound else unknown_decision(
        data.get('compatibility', 'LOCAL_OBSERVATION_NOT_CURRENT'), 'hub_observation')
    if not isinstance(local, dict) or local.get('state') not in {'allowed', 'denied', 'unknown'}:
        local = unknown_decision('LOCAL_OBSERVATION_INVALID')
    authorization = hub if hub['state'] == 'denied' else local
    if hub['state'] == local['state'] == 'allowed':
        authorization = {**local, 'source': 'hub_current_policy_and_agent_snapshot'}
    readiness = deepcopy(data.get('execution_readiness')) if bound else unknown_decision(
        data.get('compatibility', 'LOCAL_OBSERVATION_NOT_CURRENT'), 'hub_observation')
    if not runtime.online(project['device_id']):
        readiness = {'state': 'unavailable', 'source': 'hub_connection', 'observed_at': now,
                     'scope': 'project_defaults', 'code': 'AGENT_OFFLINE'}
    checks = deepcopy(data.get('checks', [])) if bound else []
    checks.append({'name': 'current_hub_policy', 'state': hub['state'],
                   'source': hub['source'], 'observed_at': now,
                   **({'code': hub['code']} if 'code' in hub else {})})
    return {'view_version': 1, 'operation_kind': kind, 'project': project['alias'],
        'project_id': project['id'], 'workspace_id': args.get('workspace_id', ''), 'target': 'agent',
        'codepier_authorization': authorization, 'execution_readiness': readiness,
        'caller_platform_decision': platform_unknown(), 'operation_evidence': None,
        'checks': checks, 'unverified': ['exact_path_and_SHA', 'OS_target_permissions',
            'command_content_and_environment', 'managed_task_lease', 'caller_platform_decision'],
        **({'compatibility': data['compatibility']} if 'compatibility' in data else {}),
        'note': 'Hub authority was rechecked; Agent observations retain their snapshot timestamp. No task was approved or executed. Unknown does not imply denial or require confirmation. Actual operations revalidate their full arguments and any original managed lease.'}
