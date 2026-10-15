"""Versioned public project metadata contracts; no authorization decisions here."""
VIEW_VERSION = 1
KINDS = {'file_read': ('read', 'fs_read'), 'file_write': ('write', 'fs_write'),
         'command_exec': ('execute', 'exec')}
STR = {'type': 'string'}
NULL = {'type': 'null'}
TIME = {'type': ['number', 'null']}

def decision(states):
    return {'type': 'object', 'properties': {
        'state': {'enum': states}, 'source': {'type': ['string', 'null']},
        'observed_at': TIME, 'scope': STR, 'code': STR, 'freshness': STR},
        'required': ['state', 'source', 'observed_at'], 'additionalProperties': False}

ACTION_READINESS = {'type': 'object', 'properties': {
    'view_version': {'const': VIEW_VERSION}, 'operation_id': STR,
    'operation_kind': {'enum': list(KINDS)}, 'project': STR, 'project_id': STR,
    'workspace_id': STR, 'target': {'const': 'agent'},
    'codepier_authorization': decision(['allowed', 'denied', 'unknown']),
    'execution_readiness': decision(['ready', 'unavailable', 'unknown']),
    'caller_platform_decision': {'type': 'object', 'properties': {
        'state': {'const': 'not_observed'}, 'source': NULL, 'observed_at': NULL},
        'required': ['state', 'source', 'observed_at'], 'additionalProperties': False},
    'operation_evidence': NULL, 'checks': {'type': 'array', 'items': {
        'type': 'object', 'properties': {'name': STR, 'state': STR, 'source': STR,
        'observed_at': TIME, 'code': STR}, 'required': ['name', 'state', 'source', 'observed_at'],
        'additionalProperties': False}},
    'unverified': {'type': 'array', 'items': STR}, 'compatibility': STR, 'note': STR},
    'required': ['view_version', 'operation_kind', 'codepier_authorization',
        'execution_readiness', 'caller_platform_decision', 'operation_evidence', 'checks'],
    'additionalProperties': True}

def unknown_decision(code='NOT_OBSERVED', source=None):
    return {'state': 'unknown', 'source': source, 'observed_at': None,
            'scope': 'project_defaults', 'code': code}

def platform_unknown():
    return {'state': 'not_observed', 'source': None, 'observed_at': None}
