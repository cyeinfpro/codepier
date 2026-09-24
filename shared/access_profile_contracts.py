"""Credential-bound identity metadata; separate from MCP catalog profiles."""
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
        'Read this connection’s effective scopes and visible project names. No account selector. '
        'ChatGPT projects and conversation names do not constrain credentials.', local=True)
    schemas['get_access_context'] = {
        'type': 'object', 'properties': {
            'profile': PROFILE_SCHEMA, 'managed': {'type': 'boolean'},
            'scopes': {'type': 'array', 'items': {'type': 'string'}},
            'projects': {'type': 'array', 'items': {'type': 'object'}},
            'all_projects': {'type': 'boolean'}, 'isolation': {'const': 'credential'},
            'chat_project_is_security_boundary': {'const': False}, 'note': {'type': 'string'},
        }, 'required': ['profile', 'managed', 'scopes', 'projects', 'chat_project_is_security_boundary'],
    }
