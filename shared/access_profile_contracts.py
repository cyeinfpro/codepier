"""Credential-bound identity metadata; separate from MCP catalog profiles."""
# These describe separate authorization domains, not a permission decision.
AUTHORIZATION_CONTEXT_VERSION = 2
AUTHORIZATION_DOMAINS = {
    'access_profile': 'credential_permission_ceiling',
    'interactive_queries': 'current_credential_and_resource_permissions',
    'managed_tasks': 'authenticated_owner_approval_and_current_work_lease',
    'host_approval': 'independent_not_evaluated_here',
}
PROFILE_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'type': 'object',
    'properties': {
        'id': {'type': 'string', 'minLength': 1, 'pattern': r'\S',
               'description': 'Persisted opaque profile ID, unchanged across refresh, reconnect and label changes.'},
        'name': {'type': 'string'},
        'nickname': {'type': 'string'},
    },
    'required': ['id'],
    'additionalProperties': False,
}


def register(Tool, Empty, tools, schemas):
    tools['get_profile'] = Tool(Empty, 'read',
        'Identify this authenticated connection. No arguments. Returns one stable profile, never selects another account.', local=True)
    schemas['get_profile'] = PROFILE_SCHEMA
    tools['get_access_context'] = Tool(Empty, 'read',
        'Authorization context v2: read effective scopes and visible projects. '
        'access_profile_managed (legacy managed) means an Access Profile binding, not a task execution mode. '
        'No account selector. This metadata neither approves tasks nor bypasses host policy.', local=True)
    schemas['get_access_context'] = {
        'type': 'object', 'properties': {
            'profile': PROFILE_SCHEMA,
            'authorization_context_version': {'const': AUTHORIZATION_CONTEXT_VERSION},
            'server_version': {'type': 'string', 'description': 'Responding code version; not the client cached catalog or a Git attestation.'},
            'access_profile_managed': {'type': 'boolean', 'description': 'True only when this credential is bound to an Access Profile. Not a delegation or host consumer mode.'},
            'managed': {'type': 'boolean', 'deprecated': True,
                        'description': 'Deprecated compatibility alias of access_profile_managed; neither true nor false grants or exempts task approval.'},
            'managed_field_meaning': {'const': 'access_profile_binding'},
            'authorization_domains': {'type': 'object', 'additionalProperties': False,
                'properties': {key: {'const': value} for key, value in AUTHORIZATION_DOMAINS.items()},
                'required': list(AUTHORIZATION_DOMAINS)},
            'scopes': {'type': 'array', 'items': {'type': 'string'}},
            'projects': {'type': 'array', 'items': {'type': 'object'}},
            'all_projects': {'type': 'boolean'}, 'isolation': {'const': 'credential'},
            'chat_project_is_security_boundary': {'const': False}, 'note': {'type': 'string'},
        }, 'required': ['profile', 'managed', 'access_profile_managed', 'managed_field_meaning',
                       'authorization_context_version', 'server_version', 'authorization_domains',
                       'scopes', 'projects', 'chat_project_is_security_boundary'],
    }
