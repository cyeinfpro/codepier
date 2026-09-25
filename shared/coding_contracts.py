"""Small, typed coding surface; a profile is never an authorization grant."""
from __future__ import annotations
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from shared.util import valid_json_value


class CodingArgs(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    @model_validator(mode='before')
    @classmethod
    def valid_json(cls, value):
        if not valid_json_value(value):
            raise ValueError('Arguments must be finite, valid Unicode JSON')
        return value


class OpenWorkspace(CodingArgs):
    workspace_id: str = Field(default='', pattern=r'^(|[a-f0-9]{32})$')
    project: str = Field(min_length=1, max_length=100)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)
    context_id: str = Field(default='', pattern=r'^(|[a-f0-9]{64})$')
    capture_baseline: bool = Field(default=False, description='Capture before editing, not after. Bounded source snapshot, not a Git commit or authorship proof.')
    max_chars: int = Field(default=6000, ge=1000, le=32000)
    max_files: int = Field(default=8, ge=1, le=32)
    include_skills: bool = True


class ShowChanges(CodingArgs):
    workspace_id: str = Field(default='', pattern=r'^(|[a-f0-9]{32})$')
    project: str = Field(min_length=1, max_length=100)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=128)
    baseline_ref: str = Field(default='', pattern=r'^(|[a-f0-9]{32})$')
    review_ref: str = Field(default='', pattern=r'^(|[a-f0-9]{32})$')
    path: str = Field(default='', max_length=1024)
    offset: int = Field(default=0, ge=0, le=1000000)
    limit: int = Field(default=40, ge=1, le=100)
    max_chars: int = Field(default=16000, ge=256, le=32000)

    @model_validator(mode='after')
    def exactly_one_reference(self):
        if bool(self.baseline_ref) == bool(self.review_ref):
            raise ValueError('Supply exactly one of baseline_ref (freeze a review) or review_ref (read the same snapshot)')
        if self.baseline_ref and (self.path or self.offset):
            raise ValueError('Freeze first, then read paths/pages using the returned review_ref')
        return self


class PatchChange(CodingArgs):
    action: Literal['write', 'delete', 'move'] = 'write'
    path: str = Field(min_length=1, max_length=1024)
    expected_sha256: str = Field(pattern=r'^(?:[a-f0-9]{64}|new)$')
    content: str | None = Field(default=None, max_length=1048576)
    destination: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode='after')
    def action_fields(self):
        if (self.action == 'write') != (self.content is not None):
            raise ValueError('Only write requires content (empty text is allowed)')
        if (self.action == 'move') != (self.destination is not None):
            raise ValueError('Only move requires destination; destination must be absent on disk')
        if self.action != 'write' and self.expected_sha256 == 'new':
            raise ValueError('Move/delete requires an existing-file SHA')
        return self


class ApplyPatch(CodingArgs):
    workspace_id: str = Field(default='', pattern=r'^(|[a-f0-9]{32})$')
    project: str = Field(min_length=1, max_length=100)
    idempotency_key: str = Field(min_length=8, max_length=128)
    changes: list[PatchChange] = Field(min_length=1, max_length=32)
    dry_run: bool = Field(default=False, description='Preflight every path/SHA and preview changes without writing. Use a different key for the intentional apply.')
