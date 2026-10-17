"""Private panel runtime settings contracts. Not MCP tools or grants."""
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from shared.util import valid_json_value

PANEL_RUNTIME_TOOLS = frozenset({'project_runtime_get', 'project_runtime_set'})
DEFAULT_MODE = 'project_clean_v1'
PathText = Annotated[str, Field(min_length=1, max_length=4096)]
EnvName = Annotated[str, Field(pattern=r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')]

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @model_validator(mode='before')
    @classmethod
    def json_safe(cls, value):
        if not valid_json_value(value):
            raise ValueError('Expected bounded finite JSON')
        return value

class ContainerSettings(Strict):
    engine: PathText
    endpoint: PathText
    image: str = Field(pattern=r'^(?:sha256:|[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:)[a-f0-9]{64}$')
    uid: int = Field(default=1000, ge=1, le=2147483647)
    gid: int = Field(default=1000, ge=1, le=2147483647)
    memory_mib: int = Field(default=1024, ge=64, le=65536)
    cpus: float = Field(default=1.0, ge=0.1, le=32)
    pids_limit: int = Field(default=128, ge=16, le=4096)
    tmp_mib: int = Field(default=256, ge=16, le=4096)
    home_mib: int = Field(default=256, ge=16, le=4096)

class EditableRuntime(Strict):
    mode: Literal['native_compat', 'project_clean_v1', 'container_v1'] = DEFAULT_MODE
    shell: Literal['sh', 'bash', 'powershell'] | None = None
    path: list[PathText] | None = Field(default=None, max_length=32)
    pass_env: list[EnvName] | None = Field(default=None, max_length=64)
    container: ContainerSettings | None = None

    @model_validator(mode='after')
    def mode_fields(self):
        if self.mode == 'native_compat' and any(getattr(self, key) is not None for key in ('shell', 'path', 'pass_env', 'container')):
            raise ValueError('Compatibility mode uses existing shell settings')
        if self.mode == 'container_v1':
            if self.container is None:
                raise ValueError('Container mode requires an installed immutable image and engine settings')
            if self.shell == 'powershell' or self.pass_env:
                raise ValueError('Linux containers cannot inherit the host environment or use PowerShell')
        elif self.container is not None:
            raise ValueError('Container settings require container mode')
        return self

class RuntimeGet(Strict):
    project: str = Field(min_length=1, max_length=100)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)

class RuntimeSave(Strict):
    expected_revision: str = Field(pattern=r'^[a-f0-9]{64}$')
    profile: EditableRuntime
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r'^[A-Za-z0-9_.:-]+$')

class RuntimeSet(RuntimeSave):
    project: str = Field(min_length=1, max_length=100)


def register(tool_type, tools, outputs):
    tools['project_runtime_get'] = tool_type(RuntimeGet, 'read', 'Panel administrator: inspect one project execution profile without exposing environment values.')
    tools['project_runtime_set'] = tool_type(RuntimeSet, 'write', 'Panel administrator: CAS-save one project execution profile; never changes grants or execution opt-in.', True)
    for name in PANEL_RUNTIME_TOOLS:
        outputs[name] = {'type': 'object', 'properties': {
            'operation_id': {'type': 'string'}, 'project_id': {'type': 'string'},
            'revision': {'type': 'string'}, 'state': {'type': 'string'},
            'profile': {'type': 'object'}, 'default_mode': {'const': DEFAULT_MODE}},
            'required': ['project_id', 'revision', 'state', 'profile', 'default_mode'],
            'additionalProperties': True}
