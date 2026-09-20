"""Read-only, bounded MCP App projection over existing authorized receipts.

An explicit workflow selects task evidence. Nearby operations are NEVER silently
attributed to that task. This module neither executes work nor certifies source.
"""
from __future__ import annotations

import re
import time

from shared.util import DevError

REF = re.compile(r'^[a-f0-9]{32}$')
RESULT_TOOLS = {'show_changes', 'validation_run', 'artifacts_register', 'shell_exec', 'tasks_run'}
RECENT_TOOLS = RESULT_TOOLS | {'apply_patch', 'fs_write', 'fs_edit', 'download_artifact', 'worktrees_create'}
PRIVATE_MEDIA = {'computer_apps', 'computer_session_open', 'computer_observe', 'computer_action',
                 'computer_session_close', 'browser_open', 'browser_snapshot', 'browser_action', 'browser_close'}


def ref(value):
    return isinstance(value, str) and REF.fullmatch(value) is not None


def operation_summary(runtime, identifier, principal, project, workspace_id, workflow=None):
    # Existing operation access still checks the grant/project and any extra scope.
    op = runtime.operation(identifier, principal, {'include_output': False, 'include_result': False})
    if op['tool'] in PRIVATE_MEDIA:
        return None
    if op['project_id'] != project['id'] or op['device_id'] != project['device_id']:
        return None
    if (op.get('args_summary') or {}).get('workspace_id', '') != workspace_id:
        return None
    if workflow and op['created'] < workflow['created']:
        return None
    item = {k: op.get(k) for k in ('tool', 'state', 'created', 'updated', 'pending')}
    item['operation_id'] = op['id']
    # Do not return commands, environment, source text, logs, credentials or media
    # in the dashboard. The user can explicitly open an authorized log afterwards.
    if op['tool'] not in RESULT_TOOLS:
        return item
    detailed = runtime.operation(identifier, principal, {'include_output': False, 'output_limit': 0})
    result = detailed.get('result') or {}
    data = result.get('data') or {}
    if not isinstance(data, dict):
        return item
    for key in ('exit_code', 'timed_out', 'cancelled', 'command_ok'):
        if key in data:
            item[key] = data[key]
    if op['tool'] == 'validation_run' and data.get('validation_id') == identifier:
        item['validation'] = {'validation_id': identifier, 'label': data.get('label', '验证'),
                              'historical_state': data.get('state', 'unverified'),
                              'freshness': 'not_checked'}
    if op['state'] != 'succeeded' or result.get('ok') is not True:
        return item
    if op['tool'] == 'show_changes' and ref(data.get('review_ref')):
        item['review'] = {'review_ref': data['review_ref'], 'summary': data.get('summary', {}),
                          'coverage': data.get('coverage', {}), 'expires_at': data.get('expires_at'),
                          'immutable': True}
    if op['tool'] == 'artifacts_register' and data.get('artifact_id') == identifier:
        try:
            item['artifact'] = runtime.artifacts.get({'artifact_id': identifier}, principal)
        except DevError as exc:
            item['artifact'] = {'artifact_id': identifier, 'unavailable': True, 'reason': exc.code}
    return item


def workspace_status(runtime, args, principal):
    project = runtime.project(args['project'], principal)
    workspace_id = args.get('workspace_id', '')
    limit = args['limit']
    listing = runtime.workflows.list({'project': project['id'], 'state': '',
                                      'cursor': args['workflow_cursor'], 'limit': limit}, principal)
    # Workflow records are project-level. workspace_id only filters receipts; it
    # is not proof of filesystem ownership and is never used to grant access.
    workflow = None
    evidence = []
    evidence_ids = []
    unavailable = 0
    history_truncated = False
    if args['workflow_id']:
        loaded = runtime.workflows.get({'workflow_id': args['workflow_id'],
                                        'before_event_id': None, 'event_limit': 100}, principal)
        if loaded['project_id'] != project['id']:
            raise DevError('WORKFLOW_NOT_FOUND', '所选任务不属于当前项目', 404)
        workflow = {k: loaded[k] for k in ('workflow_id', 'title', 'goal', 'state', 'version',
                    'created', 'updated', 'steps', 'summary', 'progress', 'next_step', 'mapping_changed')}
        history_truncated = loaded.get('next_before_event_id') is not None
        if not workflow['mapping_changed']:
            # Newest checkpoints first, plus all currently attached step evidence.
            references = [e for event in loaded['events'] for e in event.get('evidence', [])]
            references += [e for step in reversed(loaded['steps']) for e in step.get('evidence', [])]
            evidence_ids = list(dict.fromkeys(e['operation_id'] for e in references
                            if isinstance(e, dict) and ref(e.get('operation_id'))))
            for identifier in evidence_ids[args['evidence_offset']:args['evidence_offset'] + limit]:
                try:
                    item = operation_summary(runtime, identifier, principal, project, workspace_id, workflow)
                except DevError:
                    item = None
                if item is None:
                    unavailable += 1
                else:
                    evidence.append(item)
    # Recent project operations are a separate, explicitly unassigned section.
    recent = runtime.list_operations({'project': project['id'], 'state': '', 'tool': '',
                    'idempotency_key': '', 'limit': 50, 'before_created': None}, principal)
    recent_items = []
    for row in recent['operations']:
        if row['tool'] not in RECENT_TOOLS or row['id'] in evidence_ids:
            continue
        try:
            item = operation_summary(runtime, row['id'], principal, project, workspace_id)
        except DevError:
            item = None
        if item is not None:
            recent_items.append(item)
        if len(recent_items) >= limit:
            break
    end = args['evidence_offset'] + limit
    return {'project': project['alias'], 'project_id': project['id'],
            'workspace_id': workspace_id, 'root': project['root'] if not workspace_id else None,
            'observed_at': time.time(), 'device_online': runtime.online(project['device_id']),
            'workflows': listing['workflows'], 'next_workflow_cursor': listing['next_cursor'],
            'workflow': workflow, 'evidence': evidence,
            'next_evidence_offset': end if end < len(evidence_ids) else None,
            'evidence_total': len(evidence_ids), 'unavailable_evidence': unavailable,
            'history_truncated': history_truncated, 'recent_operations': recent_items,
            'recent_window_limited': recent['next_before_created'] is not None or len(recent_items) >= limit,
            'execution_started': False,
            'scope_note': '任务步骤是项目级保存记录；操作按当前目录编号筛选。近期操作未自动归属任务；历史通过未核对源码新鲜度；任务完成、验证通过与部署成功是不同状态。'}
