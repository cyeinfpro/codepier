"""Panel runtime authorization and original-operation receipt recovery."""
from hub import iam
from hub.principal import refresh_principal
from shared.runtime_panel_contracts import PANEL_RUNTIME_TOOLS
from shared.tool_protocol import require_compatible
from shared.util import DevError


def authorize(runtime, principal, project):
    principal = refresh_principal(runtime.store, principal)
    if principal.grant_id or not principal.admin:
        raise DevError('OWNER_REQUIRED', '执行环境设置仅允许面板管理员操作，MCP 不能更改运行边界', 403)
    runtime.authorize(principal, 'read', project_id=project['id'])
    iam.require_device(runtime.store, principal, project['device_id'], manage=True)
    return principal


def availability(runtime, project):
    if not runtime.online(project['device_id']):
        return 'offline'
    connection = runtime.connections.get(project['device_id'])
    if connection is None or not runtime.connection_authorized(project['device_id'], connection):
        return 'offline'
    peer = getattr(connection, 'tool_protocol', None)
    if peer is None or peer.legacy:
        return 'upgrade_required'
    try:
        for name in PANEL_RUNTIME_TOOLS:
            require_compatible(peer, name)
    except DevError:
        return 'upgrade_required'
    return 'supported'


def receipt(runtime, principal, project, key):
    principal = authorize(runtime, principal, project)
    row = runtime.store.one('''SELECT id FROM operations WHERE space_id=? AND actor=?
        AND project_id=? AND grant_id IS NULL AND tool='project_runtime_set' AND idem=?''',
        (principal.space_id, principal.actor, project['id'], key))
    if row is None:
        return {'state': 'not_found', 'pending': False, 'operation_id': None,
            'outcome_confirmed': False, 'replay_recommended': False}
    operation = runtime.operation(row['id'], principal, {'include_output': False})
    return {'state': operation['state'], 'pending': operation.get('pending', False),
        'operation_id': row['id'], 'result': operation.get('result'), 'error': operation.get('error'),
        'outcome_confirmed': operation['state'] in {'succeeded', 'failed', 'cancelled'},
        'replay_recommended': False}
