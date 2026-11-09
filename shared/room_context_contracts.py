"""Sourced room memory; no context action can create or approve work."""
from typing import Literal

from pydantic import ConfigDict, Field, model_validator
from shared.collaboration_contracts import Model, Identifier
from shared.room_contracts import MemberIdentity
from shared.room_runtime_contracts import RoomConsumerMutation
from shared.team_collaboration_contracts import ArtifactRef


class ContextSource(Model):
    message_id: Identifier
    version: int = Field(strict=True, ge=1)
    digest: str = Field(pattern=r'^[a-f0-9]{64}$')


class ContextFact(Model):
    kind: Literal['objective', 'decision', 'constraint', 'question', 'artifact']
    key: str = Field(pattern=r'^[A-Za-z0-9_.:-]{1,80}$')
    text: str = Field(min_length=1, max_length=1000)
    sources: list[ContextSource] = Field(min_length=1, max_length=4)
    project_ids: list[Identifier] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list, max_length=8)

    @model_validator(mode='after')
    def meaningful(self):
        if not self.text.strip() or len({s.message_id for s in self.sources}) != len(self.sources):
            raise ValueError('Context requires distinct original messages and nonblank text')
        if (self.kind == 'artifact') != bool(self.artifact_refs):
            raise ValueError('Completed artifacts require exact result references')
        return self


class RoomContextRead(MemberIdentity):
    cursor: str = Field(default='', max_length=2048)
    limit: int = Field(default=20, strict=True, ge=1, le=50)
    byte_budget: int = Field(default=16000, strict=True, ge=4096, le=24000)
    input_message_id: str = Field(default='', max_length=128)


class RoomContextUpdate(RoomConsumerMutation):
    model_config = ConfigDict(json_schema_extra={'anyOf': [
        {'required': ['facts'], 'properties': {'facts': {'minItems': 1}}},
        {'required': ['reviewed_sources'], 'properties': {'reviewed_sources': {'minItems': 1}}},
    ]})
    expected_context_version: int = Field(strict=True, ge=0)
    facts: list[ContextFact] = Field(default_factory=list, max_length=10)
    supersedes: list[Identifier] = Field(default_factory=list, max_length=20)
    reviewed_sources: list[ContextSource] = Field(default_factory=list, max_length=32)

    @model_validator(mode='after')
    def meaningful(self):
        if not self.facts and not self.supersedes and not self.reviewed_sources:
            raise ValueError('Context update requires a substantive change or reviewed source')
        if len({(f.kind, f.key) for f in self.facts}) != len(self.facts):
            raise ValueError('Only one current fact may use the same kind and key')
        if len(set(self.supersedes)) != len(self.supersedes):
            raise ValueError('Replacement facts must be distinct')
        if self.supersedes and not self.facts and not self.reviewed_sources:
            raise ValueError('Withdrawing a fact requires a source for the correction')
        return self
