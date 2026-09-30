"""Role-mode protocol constants and Hub-only management tools."""
from typing import Literal
from pydantic import Field

ROLE_SCOPE = 'codepier.role_access'
ROLE_TOOLS = frozenset({'devices_list', 'projects_create'})


def register(Tool, Empty, Model, tools, schemas):
    class ProjectCreate(Model):
        alias: str = Field(min_length=1, max_length=64)
        device_id: str = Field(min_length=1, max_length=100)
        root: str = Field(min_length=1, max_length=2048)
        description: str = Field(default='', max_length=1000)
        mode: Literal['read', 'write'] = 'read'
        allow_tasks: bool = False
        idempotency_key: str = Field(min_length=8, max_length=128, pattern=r'^[a-zA-Z0-9_.:-]+$')

    tools['devices_list'] = Tool(Empty, 'devices.read',
        'List only devices allowed by this authenticated dynamic role. Never returns pairing secrets or local credentials.', local=True)
    tools['projects_create'] = Tool(ProjectCreate, 'projects.create',
        'Create a NEW project mapping on a role-authorized device. Requires explicit role delegation and Agent local root approval. '
        'Does not create a directory, edit an existing project, change a role or expand Agent permissions. '
        'Use the original idempotency key to resume a pending validation; never replay uncertain creation with a new key.',
        True, True)
    schemas['devices_list'] = {'type': 'object', 'properties': {'devices': {'type': 'array', 'items': {'type': 'object'}}}, 'required': ['devices']}
    schemas['projects_create'] = {'type': 'object', 'properties': {
        'id': {'type': 'string'}, 'alias': {'type': 'string'}, 'root': {'type': 'string'}, 'device_id': {'type': 'string'},
        'created_by_role': {'type': ['string', 'null']}, 'role_access': {'type': 'array', 'items': {'type': 'string'}}},
        'required': ['id', 'alias', 'root', 'device_id']}
