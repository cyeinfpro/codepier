"""Explicit read-only queries and a user-opened MCP Apps workbench.

These are allowlists over the existing runtime, never an alternate authorization
path. The mixed workspace/process facades keep their conservative annotations.
"""
from __future__ import annotations

import copy
from typing import Literal

from pydantic import model_validator
from shared.computer_contracts import ComputerArgs
from shared.core_contracts import Workspace, Process

QUERY_TOOLS = frozenset({'workbench', 'project_query', 'task_query'})


class Workbench(ComputerArgs):
    pass


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
        'workbench': (Workbench, 'Open the CodePier project workbench explicitly. Accepts {} and returns only authorized projects; the user must select a project and task. Never starts execution.', True),
        'project_query': (ProjectQuery, 'Read authorized projects, project context without a baseline, directory trees, skills, tool help, configured tasks, readiness, dashboards and saved workflows. Prefer this for read-only project discovery. No writes, commands or implicit project/task selection.', False),
        'task_query': (TaskQuery, 'Read or wait for existing authorized operation receipts, logs and traces; list diagnostics/activity. Never starts, cancels, or retries execution.', True),
    }
    for name, (model, description, local) in specs.items():
        tools[name] = tool(model, 'read', description, False, local)
        source = 'projects_list' if name == 'workbench' else 'workspace' if name == 'project_query' else 'process'
        schemas[name] = copy.deepcopy(schemas[source]) if name == 'workbench' else query_schemas[name]
