"""Private authenticated-panel contracts for device directory grants.

These are Agent control messages, never public MCP tools. A browser session and
device-management authority are required independently of project permissions.
"""

from typing import Literal

from pydantic import Field, model_validator

from shared.runtime_panel_contracts import Strict

PANEL_ROOTS_TOOLS = frozenset({"agent_roots_get", "agent_roots_preview", "agent_roots_set"})


class RootsGet(Strict):
    device_id: str = Field(min_length=1, max_length=100)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)


class RootsChange(Strict):
    action: Literal["upsert", "remove"]
    path: str = Field(min_length=1, max_length=4096)
    writable: bool = False
    allow_tasks: bool = False
    expected_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")

    @model_validator(mode="after")
    def coherent_permissions(self):
        if "\x00" in self.path or not self.path.strip():
            raise ValueError("请输入有效的绝对目录路径")
        if self.allow_tasks and not self.writable:
            raise ValueError("任务执行需要同时授权目录读写权限")
        return self


class RootsCommand(RootsChange):
    device_id: str = Field(min_length=1, max_length=100)


def register(tool_type, tools, outputs):
    tools["agent_roots_get"] = tool_type(
        RootsGet,
        "read",
        "Panel device administrator: inspect directory grants without exposing configuration secrets.",
    )
    tools["agent_roots_preview"] = tool_type(
        RootsCommand,
        "read",
        "Panel device administrator: validate one directory grant change without saving.",
    )
    tools["agent_roots_set"] = tool_type(
        RootsCommand,
        "write",
        "Panel device administrator: CAS-save one explicit directory grant change.",
        True,
    )
    for name in PANEL_ROOTS_TOOLS:
        outputs[name] = {
            "type": "object",
            "properties": {
                "operation_id": {"type": "string"},
                "device_id": {"type": "string"},
                "config_sha256": {"type": "string"},
                "state": {"type": "string"},
                "roots": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["device_id", "config_sha256", "state", "roots"],
            "additionalProperties": True,
        }
