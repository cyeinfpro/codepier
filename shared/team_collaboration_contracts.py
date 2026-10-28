"""Explicit owner-approved multi-Dot teams layered on coordination goals."""
from typing import Literal
from pydantic import Field, model_validator
from shared.collaboration_contracts import Scope, Model, Identifier, Key, GoalRead, CoordinationBudget, GoalCapability


class TeamCreate(Scope):
    conversation_id: Identifier
    label: str = Field(min_length=1, max_length=80)
    dot_ids: list[Identifier] = Field(min_length=2, max_length=16)
    coordinator_dot_id: Identifier
    project_ids: list[Identifier] = Field(min_length=1, max_length=16)
    capabilities: list[GoalCapability] = Field(default_factory=lambda: ['read'], min_length=1, max_length=3)
    duration_seconds: int = Field(strict=True, default=3600, ge=60, le=86400)
    policy_duration_seconds: int = Field(strict=True, default=86400, ge=60, le=2592000)
    max_goals: int = Field(strict=True, default=20, ge=1, le=100)
    max_total_steps: int = Field(strict=True, default=500, ge=1, le=10000)
    max_total_messages: int = Field(strict=True, default=400, ge=1, le=10000)
    budget: CoordinationBudget = Field(default_factory=CoordinationBudget)
    confirm_tasks: Literal[True]
    idempotency_key: Key

    @model_validator(mode='after')
    def explicit_members(self):
        if not self.label.strip() or len(set(self.dot_ids)) != len(self.dot_ids):
            raise ValueError('Named distinct members are required')
        if self.coordinator_dot_id not in self.dot_ids or 'read' not in self.capabilities:
            raise ValueError('The coordinator must be a member and read must be approved')
        if len(set(self.project_ids)) != len(self.project_ids):
            raise ValueError('Duplicate projects are not allowed')
        return self


class TeamList(Scope):
    conversation_id: Identifier


class TeamRead(Scope):
    team_id: Identifier


class TeamActor(TeamRead):
    dot_id: str = Field(default='', max_length=128)
    consumer_session_id: str = Field(default='', max_length=128)
    consumer_fence: int = Field(strict=True, default=0, ge=0)
    consumer_proof: str = Field(default='', max_length=2048)
    expected_inbox_revision: int = Field(strict=True, default=0, ge=0)


class TeamFromMessage(TeamActor):
    message_id: Identifier
    source_version: int = Field(strict=True, ge=1)
    source_digest: str = Field(min_length=64, max_length=64)
    acceptance: str = Field(default='依据原始请求交付可核对的结果，并如实报告未完成项。', min_length=1, max_length=2000)
    idempotency_key: Key


class TeamControl(TeamRead):
    expected_version: int = Field(strict=True, ge=1)
    action: Literal['pause', 'revoke', 'resume']
    idempotency_key: Key


class TeamGoal(GoalRead):
    coordinator_epoch: int = Field(strict=True, ge=1)
    expected_intent_revision: int = Field(strict=True, ge=1)
    idempotency_key: Key


class TeamTakeover(TeamGoal):
    coordinator_dot_id: Identifier


class TeamReviseIntent(TeamGoal):
    message_id: Identifier
    source_version: int = Field(strict=True, ge=1)
    source_digest: str = Field(min_length=64, max_length=64)


class ArtifactRef(Model):
    work_item_id: Identifier
    version: int = Field(strict=True, ge=1)
    digest: str = Field(min_length=64, max_length=64)


class TeamHandoff(TeamGoal):
    recipient_dot_id: Identifier
    work_item_id: str = Field(default='', max_length=128)
    purpose: str = Field(min_length=1, max_length=1000)
    decision_needed: str = Field(default='', max_length=1000)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list, max_length=20)
    causation_id: str = Field(default='', max_length=128)
    hop_count: int = Field(strict=True, default=0, ge=0, le=4)


class TeamPublish(TeamGoal):
    summary: str = Field(min_length=1, max_length=12000)
    outcome: Literal['succeeded', 'partial', 'blocked']
    artifact_refs: list[ArtifactRef] = Field(min_length=1, max_length=100)


TOOL_MODELS = {
    'collaboration_team_create': TeamCreate,
    'collaboration_team_list': TeamList,
    'collaboration_team_read': TeamRead,
    'collaboration_team_from_message': TeamFromMessage,
    'collaboration_team_control': TeamControl,
    'collaboration_team_takeover': TeamTakeover,
    'collaboration_team_revise_intent': TeamReviseIntent,
    'collaboration_team_handoff': TeamHandoff,
    'collaboration_team_publish': TeamPublish,
}
