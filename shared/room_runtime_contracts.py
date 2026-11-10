"""Exact room message, work-control and channel-flow contracts."""
from __future__ import annotations

from typing import Literal
from pydantic import Field, field_validator, model_validator
from shared.collaboration_contracts import Model, Identifier, Key
from shared.room_contracts import MemberIdentity, RoomPermissionsUpdate
from shared.team_collaboration_contracts import ArtifactRef


class RoomMessagesRead(Model):
    conversation_id: Identifier
    cursor: str = Field(default='', max_length=2048)
    limit: int = Field(default=50, ge=1, le=100)
    client_message_id: str = Field(default='', max_length=128)
    message_id: str = Field(default='', max_length=128)

    @model_validator(mode='after')
    def one_lookup(self):
        if self.message_id and (self.client_message_id or self.cursor):
            raise ValueError('An exact message lookup cannot mix cursor or client identity')
        return self


class RoomWorksRead(Model):
    conversation_id: Identifier
    cursor: str = Field(default='', max_length=2048)
    limit: int = Field(default=50, ge=1, le=100)


class RoomWorkRead(Model):
    work_id: Identifier


class ChannelRead(Model):
    flow_id: Identifier


class RoomMessage(Model):
    conversation_id: Identifier
    body_text: str = Field(max_length=8000)
    attachment_ids: list[Identifier] = Field(default_factory=list, max_length=16)
    client_message_id: str = Field(min_length=1, max_length=128)
    idempotency_key: Key
    member_id: str = Field(default='', max_length=128)
    reply_to_id: str = Field(default='', max_length=128)
    work_id: str = Field(default='', max_length=128)
    revise_work_intent: bool | None = Field(default=None, strict=True)
    supersedes_context_ids: list[Identifier] = Field(default_factory=list, max_length=20)
    expected_context_version: int | None = Field(default=None, strict=True, ge=0)

    @model_validator(mode='after')
    def nonblank(self):
        if not self.body_text.strip() and not self.attachment_ids:
            raise ValueError('Message must contain text or attachments')
        if len(set(self.attachment_ids)) != len(self.attachment_ids):
            raise ValueError('Attachments must be distinct')
        if (len(set(self.supersedes_context_ids)) != len(self.supersedes_context_ids)
                or bool(self.supersedes_context_ids) != (self.expected_context_version is not None)):
            raise ValueError('Explicit context correction requires distinct fact IDs and the current context version')
        return self


class WorkControl(Model):
    work_id: Identifier
    action: Literal['pause', 'resume', 'cancel', 'supplement', 'takeover', 'restore_binding']
    expected_version: int = Field(ge=1)
    target_member_id: str = Field(default='', max_length=128)
    expected_coordinator_epoch: int | None = Field(default=None, strict=True, ge=1)
    expected_intent_revision: int | None = Field(default=None, strict=True, ge=1)
    body_text: str = Field(default='', max_length=8000)
    attachment_ids: list[Identifier] = Field(default_factory=list,max_length=16)
    idempotency_key: Key

    @model_validator(mode='after')
    def explicit_recovery(self):
        if self.action in {'takeover', 'restore_binding'}:
            if not self.target_member_id or self.expected_coordinator_epoch is None or self.expected_intent_revision is None:
                raise ValueError('Recovery requires the selected original member, current lead epoch and current intent')
        return self


class RoomPermissionContinuationSave(RoomPermissionsUpdate):
    policy_preview_digest: str = Field(default='', max_length=64)
    continuation_preview_id: str = Field(default='', max_length=128)
    continue_selected: bool = Field(default=False, strict=True)
    selected_task_ids: list[Identifier] = Field(default_factory=list, max_length=50)

    @model_validator(mode='after')
    def explicit_selection(self):
        if self.continue_selected:
            if (not self.idempotency_key or not self.continuation_preview_id
                    or len(self.policy_preview_digest) != 64 or not self.selected_task_ids
                    or len(set(self.selected_task_ids)) != len(self.selected_task_ids)):
                raise ValueError('Continuation requires a unique selection, policy preview and request key')
        elif self.selected_task_ids or self.continuation_preview_id or self.policy_preview_digest:
            raise ValueError('Selection and preview require explicit continuation approval')
        return self


class RoomConsumerMutation(MemberIdentity):
    consumer_session_id: Identifier
    consumer_fence: int = Field(ge=1)
    consumer_proof: str = Field(min_length=24, max_length=256)
    expected_inbox_revision: int = Field(ge=0)
    idempotency_key: Key


class RoomMemberReply(RoomConsumerMutation):
    source_message_id: Identifier
    source_version: int = Field(ge=1)
    source_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    body_text: str = Field(min_length=1, max_length=8000)
    complete: bool = True
    expected_intent_revision: int | None = Field(default=None,ge=1)
    coordinator_epoch: int | None = Field(default=None,strict=True,ge=1)
    outcome: Literal['succeeded','partial','blocked'] | None = None
    artifact_refs: list[ArtifactRef] = Field(default_factory=list,max_length=100)


class RoomWorkStart(RoomConsumerMutation):
    expected_intent_revision: int | None = Field(default=None, strict=True, ge=1)
    source_message_id: Identifier
    source_version: int = Field(ge=1)
    source_digest: str = Field(pattern=r'^[a-f0-9]{64}$')
    coordinator_epoch: int | None = Field(default=None, strict=True, ge=1)
    target_project_id: str = Field(default='', max_length=128)
    project_ids: list[Identifier] = Field(default_factory=list)
    participant_member_ids: list[Identifier] = Field(default_factory=list,max_length=12)
    environment_id: str = Field(default='', max_length=100)
    acceptance: str = Field(min_length=1, max_length=2000)
    capabilities: list[Literal['read','write','execute']] = Field(default_factory=list,max_length=3)
    execution_targets: list[str] = Field(default_factory=list)
    confirm_task: Literal[True]


class RoomWorkHandoff(RoomConsumerMutation):
    goal_id: Identifier
    recipient_member_id: Identifier
    expected_intent_revision: int = Field(strict=True, ge=1)
    coordinator_epoch: int = Field(strict=True, ge=1)
    work_item_id: str = Field(default='', max_length=128)
    purpose: str = Field(min_length=1, max_length=1000)
    decision_needed: str = Field(default='', max_length=1000)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list, max_length=20)
    causation_id: str = Field(default='', max_length=128)
    hop_count: int = Field(strict=True, default=0, ge=0, le=4)

    @field_validator('purpose')
    @classmethod
    def nonblank_purpose(cls, value):
        if not value.strip():
            raise ValueError('A handoff must have a concrete purpose')
        return value


class RoomWorkHandoffRead(MemberIdentity):
    handoff_id: Identifier


ROOM_RUNTIME_QUERIES = {'room_work_handoff_read': RoomWorkHandoffRead,
                        'room_works': RoomWorksRead, 'room_work': RoomWorkRead}


ROOM_RUNTIME_MUTATION_ACTIONS = {
    'room_reply': RoomMemberReply,
    'room_work_start': RoomWorkStart,
    'room_work_handoff': RoomWorkHandoff,
}


class ChannelConnect(Model):
    conversation_id: Identifier
    member_id: str = Field(default='', max_length=128)
    account_id: str = Field(default='', max_length=128)
    idempotency_key: Key


class ChannelAction(Model):
    flow_id: Identifier
    action: Literal['select_account', 'start_qr', 'poll_qr', 'accept_account', 'bind',
                    'confirm_owner', 'refresh_pairing', 'refresh', 'cancel',
                    'pause', 'resume', 'disconnect', 'migrate', 'approve_tasks', 'revoke_tasks']
    expected_version: int = Field(ge=1)
    idempotency_key: Key
    account_id: str = Field(default='', max_length=128)
    member_id: str = Field(default='', max_length=128)
    label: str = Field(default='微信', min_length=1, max_length=80)
    confirm_scanner_id: str = Field(default='', max_length=256)
    confirm_peer_id: str = Field(default='', max_length=256)
    confirm_pairing_sender: bool = False
    verify_code: str = Field(default='', max_length=32)
    confirm_credential_storage: bool = False
    confirm_provider_terms: bool = False
    confirm_external_data_ingress: bool = False
    allow_replies: bool = False
    confirm_until_revoked: bool = False
    allow_owner_tasks: bool = False
    confirm_owner_identity: bool = False
    confirm_future_tasks: bool = False
    confirm_follow_room: bool = False
    confirm_result_delivery: bool = False
    confirm_pairing_notices: bool = False
    expected_scope_digest: str = Field(default='', max_length=64)
    confirm_migrate_binding_id: str = Field(default='', max_length=128)
    expected_binding_version: int = Field(default=0, ge=0)
    confirm_replace_existing: bool = False
