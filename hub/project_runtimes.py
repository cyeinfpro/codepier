"""Authenticated project runtime negotiation; never an independent grant system.

An owner may require the configured driver, but only the Agent's pre-start
inspection can establish a particular operation's isolation evidence. A saved
requirement cannot be relaxed on delivery or silently sent to a legacy Agent.
"""
from __future__ import annotations

from copy import deepcopy
import re

from shared.runtime_requirements import valid_requirement
from shared.util import DevError

PROTOCOL = 1
DRIVER = 'docker_restricted_v1'
EXECUTING = frozenset({'exec', 'shell_exec', 'tasks_run', 'validation_run'})


def parse_advertisement(hello):
    if type(hello.get('project_runtime_protocol')) is not int or hello['project_runtime_protocol'] != PROTOCOL:
        return 0, []
    rows = hello.get('project_runtime_profiles', [])
    if not isinstance(rows, list) or len(rows) > 128:
        raise ValueError('Invalid project runtime capability advertisement')
    profiles, seen = [], set()
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'project', 'mode', 'revision', 'driver'}
                or not isinstance(row['project'], str) or not 1 <= len(row['project']) <= 100
                or row['project'] in seen or '*' in row['project']
                or row['mode'] != 'container_v1' or row['driver'] != DRIVER
                or not isinstance(row['revision'], str) or not re.fullmatch(r'[a-f0-9]{64}', row['revision'])):
            raise ValueError('Invalid project runtime capability advertisement')
        profiles.append(dict(row))
        seen.add(row['project'])
    return PROTOCOL, profiles


def configured_requirement(runtime, project):
    connection = runtime.connections.get(project['device_id'])
    if (not connection or not runtime.online(project['device_id'])
            or not runtime.connection_authorized(project['device_id'], connection)
            or getattr(connection, 'project_runtime_protocol', 0) != PROTOCOL):
        raise DevError('ROOM_SCOPE_ISOLATION_UNAVAILABLE', '此项目没有已连接且支持强制 OS 隔离检查的运行时；不会回退为账号级执行', 409)
    rows = getattr(connection, 'project_runtime_profiles', [])
    matches = [row for row in rows if row['project'] in {project['id'], project['alias']}]
    if not matches or len({row['revision'] for row in matches}) != 1:
        raise DevError('ROOM_SCOPE_ISOLATION_UNAVAILABLE', '此项目没有唯一的 OS 隔离运行时配置；不能用工作目录限制代替', 409)
    return {'protocol': PROTOCOL, 'driver': DRIVER, 'revision': matches[0]['revision'], 'project_id': project['id']}


def current_requirement(store, principal, project_id, name, args):
    from hub.room_scope import current
    record = current(store, principal.grant_id)
    if record is None or record['manifest']['execution_mode'] != 'require_os_isolation':
        return None
    from shared.contracts import TOOLS, required_scope
    if name not in TOOLS or required_scope(name, args) != 'execute':
        return None
    if name not in EXECUTING or args.get('target', 'agent') != 'agent':
        raise DevError('ROOM_SCOPE_ISOLATION_TARGET', '严格隔离仅允许已批准项目运行时，不支持其他执行位置或管理入口', 403)
    rule = next((row for row in record['manifest']['project_rules'] if row['project_id'] == project_id), {})
    requirement = rule.get('runtime_requirement')
    if not valid_requirement(requirement, project_id):
        raise DevError('ROOM_SCOPE_ISOLATION_UNAVAILABLE', '原范围缺少有效 OS 隔离要求；不能降级执行', 403)
    return deepcopy(requirement)


def attach_requirement(runtime, request, principal, project, connection=None, *, admission=False):
    """Called at durable admission and again immediately before delivery.

    The immutable saved requirement wins over a later less restrictive policy.
    New stricter policy may fence an old unsent operation but cannot rewrite its
    identity or permission. Current grant/room checks happen independently.
    """
    saved = request.get('project_runtime_requirement')
    current = current_requirement(runtime.store, principal, project['id'], request['tool'], request['args'])
    if saved is not None and not valid_requirement(saved, project['id']):
        raise DevError('RUNTIME_REQUIREMENT_INVALID', 'Original runtime requirement is invalid', 409)
    if current and saved and current != saved:
        raise DevError('RUNTIME_CONFIGURATION_CHANGED', 'The original operation requires a different runtime revision; do not replay', 409)
    if current and saved is None and not admission:
        raise DevError("RUNTIME_CONFIGURATION_CHANGED", "The original operation predates the current isolation requirement; it must not be reinterpreted", 409)
    required = saved or current
    if required is None:
        return None
    if request['tool'] not in EXECUTING or request['args'].get('target', 'agent') != 'agent':
        raise DevError('ROOM_SCOPE_ISOLATION_TARGET', 'Original runtime requirement does not permit this execution target', 403)
    configured = configured_requirement(runtime, project)
    if configured != required:
        raise DevError('RUNTIME_CONFIGURATION_CHANGED', 'Configured runtime no longer matches the originally approved revision', 409)
    if connection is not None and runtime.connections.get(project['device_id']) is not connection:
        raise DevError('RUNTIME_CONNECTION_CHANGED', 'Runtime connection changed before delivery', 409)
    request['project_runtime_requirement'] = deepcopy(required)
    return deepcopy(required)
