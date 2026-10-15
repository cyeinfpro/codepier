"""Explicit read-only project and operation queries.

These are allowlists over the existing runtime, never an alternate authorization
path. The mixed workspace/process facades keep their conservative annotations.
"""
from __future__ import annotations

from typing import Literal

from pydantic import model_validator
from shared.core_contracts import Workspace, Process

QUERY_TOOLS = frozenset({'project_query', 'task_query'})


class ProjectQuery(Workspace):
    operation: Literal['list', 'open', 'help', 'tree', 'skills', 'skill', 'tasks', 'status', 'readiness',
                       'dashboard', 'workflow_list', 'workflow_get'] = 'list'
    capture_baseline: Literal[False] = False

    @model_validator(mode='after')
    def read_only_options(self):
        if 'capture_baseline' in self.options:
            raise ValueError('Read-only queries cannot capture a baseline')
        return self


class TaskQuery(Process):
    operation: Literal['list', 'get', 'wait', 'trace', 'diagnostics', 'activity'] = 'list'


def register(tool, tools, schemas):
    from shared.core_output_schemas import build_core_output_schemas
    query_schemas = build_core_output_schemas(schemas, include_queries=True)
    specs = {
        'project_query': (ProjectQuery, 'Open a named project directly; list/search only for discovery. Read-only context, trees, skills, help, readiness and workflows. No baseline or execution. Current resource checks and original managed leases apply; platform approval is independent.', False),
        'task_query': (TaskQuery, 'Read/wait for existing authorized receipts, logs and traces. No start, cancel or retry. Original credential/resource checks and managed leases remain; this grants no task authority.', True),
    }
    for name, (model, description, local) in specs.items():
        tools[name] = tool(model, 'read', description, False, local)
        schemas[name] = query_schemas[name]
