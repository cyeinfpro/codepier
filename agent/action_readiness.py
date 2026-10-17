"""Action-specific observations using the same local gates as real operations.

No shell process, file mutation, browser/model probe or platform approval. A
ready project default cannot prove a particular path, SHA or command will work.
"""
from __future__ import annotations
import time
from agent.shell import shell_denial
from agent.project_runtime import runtime_readiness
from shared.project_view_contracts import KINDS, platform_unknown, unknown_decision
from shared.util import DevError


def action_readiness(service, project, args):
    engine = service.agent.engine
    kind = args['operation_kind']
    scope, target_tool = KINDS[kind]
    now = time.time()
    local = {'state': 'allowed', 'source': 'agent_local_policy', 'observed_at': now,
             'scope': 'project_defaults', 'freshness': 'snapshot'}
    ready = {'state': 'ready', 'source': 'agent_local_observation', 'observed_at': now,
             'scope': 'project_defaults', 'freshness': 'snapshot'}
    checks = []
    scopes = project.get('_coding_scopes')
    if not isinstance(scopes, list) or not all(isinstance(scope, str) for scope in scopes):
        local = unknown_decision('DELIVERY_SCOPE_NOT_OBSERVED', 'agent_local_policy')
    elif scope not in scopes:
        local.update(state='denied', code='INSUFFICIENT_SCOPE')
    checks.append({'name': 'authenticated_delivery_scope', 'state': local['state'],
                   'source': 'agent_authenticated_delivery', 'observed_at': now,
                   **({'code': local['code']} if 'code' in local else {})})
    try:
        root, spec = engine.root(project, write=kind != 'file_read')
        service.control.guard(target_tool, project)
        if kind == 'command_exec':
            denial = shell_denial(engine.config, project, spec)
            if denial:
                raise DevError(*denial, 403)
            observed = runtime_readiness(engine.config, project, root)
            ready['state'] = observed['state']
            if observed.get('code'):
                ready['code'] = observed['code']
        checks.append({'name': 'local_project_policy', 'state': 'allowed',
                       'source': 'agent_local_policy', 'observed_at': now})
    except (DevError, OSError) as exc:
        code = getattr(exc, 'code', 'FILESYSTEM_UNAVAILABLE')
        if code in {'READ_ONLY', 'ROOT_NOT_ALLOWED', 'PROTECTED_ROOT', 'TASKS_DISABLED',
                    'SHELL_DISABLED', 'SHELL_NOT_ALLOWED', 'REMOTE_PAUSED'}:
            local.update(state='denied', code=code, observed_at=now)
        elif local['state'] != 'denied':
            local = unknown_decision(code, 'agent_local_policy')
        ready.update(state='unavailable', code=code)
        checks.append({'name': 'local_project_policy', 'state': local['state'],
                       'source': 'agent_local_policy', 'observed_at': now, 'code': code})
    return {'view_version': 1, 'operation_kind': kind, 'project': project.get('alias', ''),
        'project_id': project.get('id', ''), 'workspace_id': args.get('workspace_id', ''),
        'target': 'agent', 'codepier_authorization': unknown_decision('HUB_POLICY_NOT_OBSERVED'),
        'local_authorization': local, 'execution_readiness': ready,
        '_readiness_binding': {'project_id': project.get('id'),
            'root': project.get('_original_root', project.get('root')),
            'device_id': project.get('_coding_device', project.get('device_id')),
            'workspace_id': args.get('workspace_id', '')},
        'caller_platform_decision': platform_unknown(), 'operation_evidence': None,
        'checks': checks, 'unverified': ['exact_path_and_SHA', 'OS_target_permissions',
            'command_content_and_environment', 'managed_task_lease', 'caller_platform_decision'],
        'note': 'Point-in-time project-default checks, not permission to execute a task. Actual calls recheck current authorization, path/SHA, command policy and any managed lease.'}
