"""Project-independent room and member actions; invitations never grant credentials."""
from __future__ import annotations

from typing import Literal
from pydantic import ConfigDict, Field, field_validator, model_validator
from shared.collaboration_contracts import Model, Identifier, Key, Scope


class RoomCreate(Model):
    title: str = Field(min_length=1, max_length=120)
    projects: list[Scope] = Field(default_factory=list)
    idempotency_key: Key

    @field_validator('title')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('A room name is required')
        return value.strip()


class RoomRead(Model):
    conversation_id: Identifier


class RoomDiagnosticsRead(RoomRead):
    member_id: Identifier | None = None
    delivery_id: Identifier | None = None
    cursor: str = Field(default='', max_length=2048)


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
    execution_targets: list[str] = Field(default_factory=list)

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
    project_scopes: list[MemberProjectScope] = Field(default_factory=list)
    # Existing approvals keep their independent original deadlines unless the
    # owner explicitly chooses a new lifetime; newly added projects default to
    # until_revoked. Member creation still has its own explicit default.
    lifetime: Lifetime | None = None


class RoomProjects(RoomRead):
    projects: list[Scope] = Field(default_factory=list)
    member_approvals: list[MemberApproval] = Field(default_factory=list, max_length=32)
    expected_version: int = Field(ge=1)
    idempotency_key: Key


class RoomProjectImpact(RoomRead):
    removed_partition_ids: list[Identifier] = Field(default_factory=list)
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
    host_connection_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    reuse_binding_generation: int | None = Field(default=None, ge=1)
    keep_connected: bool = Field(default=False, strict=True, exclude_if=lambda value: not value)
    project_scopes: list[MemberProjectScope] = Field(default_factory=list)
    lifetime: Lifetime = Field(default_factory=Lifetime)
    expected_version: int | None = Field(default=None, ge=1)
    idempotency_key: Key

    @model_validator(mode='after')
    def single_connection_reference(self):
        if self.host_connection_id and (self.reuse_member_id or self.reuse_binding_generation is not None):
            raise ValueError('Select one connection reference')
        return self

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
    action: Literal['refresh_invite', 'resume', 'pause', 'remove', 'rebind', 'migrate', 'set_renewal']
    keep_connected: bool | None = Field(default=None, strict=True, exclude_if=lambda value: value is None)
    host_connection_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    expected_version: int = Field(ge=1)
    idempotency_key: Key

    @model_validator(mode='after')
    def host_only_for_rebind(self):
        if self.host_connection_id and self.action != 'rebind':
            raise ValueError('A host connection is only used for rebind')
        return self


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


class HostConnectionRead(Model):
    host_connection_id: Identifier


class HostConnectionList(Model):
    cursor: str = Field(default='', max_length=2048)
    limit: int = Field(default=40, ge=1, le=100)


class HostConnectionImpact(HostConnectionRead, HostConnectionList):
    pass


class HostConnectionRevoke(HostConnectionRead):
    impact_digest: str = Field(min_length=64, max_length=64)
    expected_authority_version: int = Field(ge=1)
    idempotency_key: Key


class HostControl(Model):
    model_config = ConfigDict(json_schema_extra={'oneOf': [
        {'required': ['challenge_proof'], 'properties': {'challenge_proof': {'type': 'string'}},
         'not': {'required': ['control_handle'], 'properties': {'control_handle': {'type': 'string'}}}},
        {'required': ['control_handle'], 'properties': {'control_handle': {'type': 'string'}},
         'not': {'required': ['challenge_proof'], 'properties': {'challenge_proof': {'type': 'string'}}}},
    ]})

    owner_operation_id: Identifier
    challenge_proof: str | None = Field(default=None, min_length=24, max_length=256)
    control_handle: str | None = Field(default=None, min_length=24, max_length=256)
    idempotency_key: Key

    @model_validator(mode='after')
    def single_proof(self):
        if bool(self.challenge_proof) == bool(self.control_handle):
            raise ValueError('Supply exactly one host control proof')
        return self


class HostRoutePrepare(RoomRead):
    member_id: Identifier
    expected_route_generation: int = Field(ge=0)
    callback_url: str = Field(min_length=8, max_length=2048)
    rotate_control_handle: bool = Field(default=False, strict=True)
    idempotency_key: Key


class HostRebindMember(Model):
    member_id: Identifier
    expected_generation: int = Field(ge=1)


class HostRebindBatch(Model):
    members: list[HostRebindMember] = Field(min_length=1, max_length=100)
    host_connection_id: Identifier | None = None
    idempotency_key: Key

    @field_validator('members')
    @classmethod
    def distinct_members(cls, members):
        if len({member.member_id for member in members}) != len(members):
            raise ValueError('Select each member once')
        return members


class HostRebindStatus(HostConnectionList):
    batch_id: Identifier


ROOM_QUERY_ACTIONS = {
    'room_list': RoomList, 'room_view': RoomRead, 'room_connect': ConnectRead,
    'room_changes': RoomChanges, 'room_diagnostics': RoomRead,
    'room_project_impact': RoomProjectImpact,
    'room_connection_list': HostConnectionList, 'room_connection_view': HostConnectionRead,
    'room_connection_impact': HostConnectionImpact, 'room_connection_rebind_status': HostRebindStatus,
}
ROOM_MUTATION_ACTIONS = {
    'room_create': RoomCreate, 'room_update': RoomPatch,
    'room_migrate': RoomMigrate,
    'room_projects': RoomProjects, 'room_member_create': MemberCreate,
    'room_connect_resume': ConnectAction, 'room_connect_join': ConnectJoin,
    'room_probe_read': ConnectProbeRead, 'room_probe_reply': ConnectProbeReply,
    'room_connection_control': HostControl, 'room_connection_revoke': HostConnectionRevoke,
    'room_connection_rebind_batch': HostRebindBatch,
    'room_connection_route_prepare': HostRoutePrepare,
}


class RoomPermissionProject(MemberProjectScope):
    lifetime: Lifetime = Field(default_factory=Lifetime)

    @model_validator(mode='after')
    def targets_require_execute(self):
        if self.execution_targets and 'execute' not in self.capabilities:
            raise ValueError('Execution targets require execute capability')
        return self


class RoomPermissionScope(Model):
    project_ids: list[Identifier] | None = Field(default=None)
    project_scopes: list[RoomPermissionProject] = Field(default_factory=list)

    @model_validator(mode='after')
    def matching_projects(self):
        if self.project_ids is not None and set(self.project_ids) != {s.project_id for s in self.project_scopes}:
            raise ValueError('Project IDs must match the explicit project scopes')
        return self


class RoomChannelPermission(Model):
    mode: Literal['follow_room', 'custom'] = 'follow_room'
    scope: RoomPermissionScope = Field(default_factory=RoomPermissionScope)


class RoomPermissionsUpdate(RoomRead):
    expected_version: int = Field(ge=0)
    scope: RoomPermissionScope
    channels: dict[Literal['weixin', 'telegram'], RoomChannelPermission]
    idempotency_key: Key | None = None

    @field_validator('channels')
    @classmethod
    def both_channels(cls, value):
        if set(value) != {'weixin', 'telegram'}:
            raise ValueError('Both channel policies must be explicit')
        return value
