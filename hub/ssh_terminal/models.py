"""Bounded public request schemas. The browser cannot submit an SSH target."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.ssh_terminal_protocol import canonical_uuid, parse_counter


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateSession(StrictBody):
    vps_id: str = Field(min_length=1, max_length=100)
    project_id: str | None = Field(default=None, max_length=100)
    request_id: str
    mode: Literal["auto", "direct", "tmux"] = "auto"
    cols: int = Field(default=100, ge=2, le=1000)
    rows: int = Field(default=30, ge=1, le=500)

    @field_validator("project_id")
    @classmethod
    def normalize_project_id(cls, value):
        if value is None:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("project_id 不能包含控制字符")
        return value.strip() or None

    @field_validator("request_id")
    @classmethod
    def uuid(cls, value):
        return canonical_uuid(value)


class AttachTicket(StrictBody):
    renderer_id: str
    runtime_generation: str | None = None
    known_epoch: str | None = None
    rendered_offset: str | None = None

    @field_validator("renderer_id", "runtime_generation")
    @classmethod
    def uuid(cls, value):
        return canonical_uuid(value) if value is not None else value

    @field_validator("known_epoch", "rendered_offset")
    @classmethod
    def counter(cls, value):
        if value is not None:
            parse_counter(value)
        return value


class SessionAction(StrictBody):
    request_id: str
    runtime_generation: str | None = None
    expected_generation: str | None = None
    expected_writer_epoch: str | None = None
    reason: str | None = Field(default=None, max_length=64)

    @field_validator("request_id", "runtime_generation", "expected_generation")
    @classmethod
    def uuid(cls, value):
        return canonical_uuid(value) if value is not None else value

    @field_validator("expected_writer_epoch")
    @classmethod
    def counter(cls, value):
        if value is not None:
            parse_counter(value)
        return value
