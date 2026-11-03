"""Project-independent room and member actions; invitations never grant credentials."""
from __future__ import annotations

from typing import Literal
from pydantic import Field, field_validator, model_validator
from shared.collaboration_contracts import Model, Identifier, Key, Scope


class RoomCreate(Model):
    title: str = Field(min_length=1, max_length=120)
    projects: list[Scope] = Field(default_factory=list, max_length=16)
    idempotency_key: Key

    @field_validator('title')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('A room name is required')
        return value.strip()


class RoomRead(Model):
    conversation_id: Identifier


class RoomList(Model):
    cursor: str = Field(default='', max_length=2048)
    limit: int = Field(default=40, ge=1, le=100)
    include_archived: bool = False


class RoomPatch(RoomRead):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    state: Literal['active', 'paused', 'archived'] | None = None
    lead_member_id: str | None = Field(default=None, max_length=128)
    expected_version: int = Field(ge=1)
    idempotency_key: Key

    @field_validator('title')
    @classmethod
    def nonblank(cls, value):
        if value is not None and not value.strip():
            raise ValueError('A room name is required')
        return value.strip() if value is not None else None


class RoomMigrate(RoomRead):
    action: Literal['activate']
    expected_version: int = Field(ge=1)
    idempotency_key: Key


class MemberProjectScope(Model):
    project_id: Identifier
    environment_id: str = Field(default='production', min_length=1, max_length=64)
    capabilities: list[Literal['read', 'write', 'execute']] = Field(default_factory=lambda: ['read'], min_length=1, max_length=3)
    execution_targets: list[str] = Field(default_factory=list, max_length=16)

    @model_validator(mode='after')
    def bounded_scope(self):
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError('Capabilities must be distinct')
        if 'read' not in self.capabilities:
            raise ValueError('Member project access requires read')
        if 'execute' in self.capabilities and not self.execution_targets:
            raise ValueError('Execution requires an explicit target')
        return self


class Lifetime(Model):
    mode: Literal['until_revoked', 'timed'] = 'until_revoked'
    expires_at: float | int | None = None

    @model_validator(mode='after')
    def lifetime(self):
        if (self.mode == 'timed') != (self.expires_at is not None):
            raise ValueError('Only timed authorization has an expiry')
        return self


class MemberApproval(Model):
    member_id: Identifier
    project_scopes: list[MemberProjectScope] = Field(default_factory=list, max_length=16)
    # Existing approvals keep their independent original deadlines unless the
    # owner explicitly chooses a new lifetime; newly added projects default to
    # until_revoked. Member creation still has its own explicit default.
    lifetime: Lifetime | None = None


class RoomProjects(RoomRead):
    projects: list[Scope] = Field(default_factory=list, max_length=16)
    member_approvals: list[MemberApproval] = Field(default_factory=list, max_length=32)
    expected_version: int = Field(ge=1)
    idempotency_key: Key


class RoomProjectImpact(RoomRead):
    removed_partition_ids: list[Identifier] = Field(default_factory=list, max_length=16)
    expected_version: int = Field(ge=1)

    @field_validator('removed_partition_ids')
    @classmethod
    def distinct_partitions(cls, value):
        if len(set(value)) != len(value):
            raise ValueError('Removed partitions must be distinct')
        return value


class MemberCreate(RoomRead):
    name: str = Field(min_length=1, max_length=80)
    reuse_member_id: Identifier | None = None
    reuse_binding_generation: int | None = Field(default=None, ge=1)
    project_scopes: list[MemberProjectScope] = Field(default_factory=list, max_length=16)
    lifetime: Lifetime = Field(default_factory=Lifetime)
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: Key

    @field_validator('name')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('A member name is required')
        return value.strip()


class ConnectRead(Model):
    flow_id: Identifier
    conversation_id: Identifier | None = None


class ConnectAction(ConnectRead):
    action: Literal['refresh_invite', 'resume', 'pause', 'remove', 'rebind', 'migrate']
    expected_version: int = Field(ge=1)
    idempotency_key: Key


class ConnectJoin(Model):
    invite_code: str = Field(min_length=24, max_length=256)
    idempotency_key: Key


class MemberIdentity(RoomRead):
    member_id: Identifier
    binding_generation: int = Field(ge=1)
    connection_proof: str = Field(min_length=24, max_length=256)


class ConnectProbeRead(MemberIdentity):
    wake_ticket: str = Field(min_length=32, max_length=256)
    idempotency_key: Key


class ConnectProbeReply(MemberIdentity):
    probe_nonce: str = Field(min_length=24, max_length=256)
    idempotency_key: Key


class RoomChanges(RoomRead):
    cursor: str = Field(default='', max_length=2048)
    limit: int = Field(default=40, ge=1, le=100)


ROOM_QUERY_ACTIONS = {
    'room_list': RoomList, 'room_view': RoomRead, 'room_connect': ConnectRead,
    'room_changes': RoomChanges, 'room_diagnostics': RoomRead,
    'room_project_impact': RoomProjectImpact,
}
ROOM_MUTATION_ACTIONS = {
    'room_create': RoomCreate, 'room_update': RoomPatch,
    'room_migrate': RoomMigrate,
    'room_projects': RoomProjects, 'room_member_create': MemberCreate,
    'room_connect_resume': ConnectAction, 'room_connect_join': ConnectJoin,
    'room_probe_read': ConnectProbeRead, 'room_probe_reply': ConnectProbeReply,
}
