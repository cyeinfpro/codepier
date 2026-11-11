"""Closed wire contracts for the versioned collaboration events."""

from typing import Literal
from pydantic import Field, model_validator
from shared.collaboration_contracts import Model, Identifier, Key, Queue, JobKind, Severity
from shared.host_parameter_schema import host_parameter_schema
from shared.schema_cache import schema_for
from hub.collaboration.common import (
    TASK_EVENT,
    RESULT_EVENT,
    INCIDENT_EVENT,
    STATUS_EVENT,
    MESSAGE_EVENT,
    WORK_EVENT,
    DELEGATION_EVENT,
    OPERATION_EVENT,
    MAILBOX_EVENT,
    ROOM_INBOX_EVENT,
)


class EventFilters(Model):
    slot_id: Identifier | None = None
    project_id: Identifier
    environment_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    severity_min: Severity | None = None
    rule_ids: list[Identifier] | None = Field(default=None, max_length=20)


class TaskFilters(EventFilters):
    queue: Queue


class MessageFilters(Model):
    conversation_id: Identifier
    project_id: Identifier
    environment_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    slot_id: Identifier


class MailboxFilters(MessageFilters):
    binding_generation: Identifier


class RoomInboxFilters(Model):
    owner_user_id: Identifier
    conversation_id: Identifier
    member_id: Identifier
    binding_generation: int = Field(ge=1)


class HostControlEventArguments(Model):
    action: Literal["room_connection_control"]
    owner_operation_id: Identifier
    challenge_proof: str = Field(min_length=24, max_length=256)
    idempotency_key: Key


class HostControlEventRequest(Model):
    tool: Literal["collaboration"]
    arguments: HostControlEventArguments


class RoomInboxPayload(RoomInboxFilters):
    schema_version: Literal[1]
    connection_check_required: bool = Field(default=False, exclude_if=lambda required: not required)
    host_control_challenge_id: Identifier | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    host_control_subscription_id: Identifier | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    host_control_request: HostControlEventRequest | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    connection_probe: bool = Field(default=False, exclude_if=lambda probe: not probe)
    route_probe_id: str = Field(
        default="", max_length=128, exclude_if=lambda identifier: not identifier
    )
    dirty_revision: int = Field(ge=0)
    test: bool = False
    test_subscription_id: str = Field(default="", max_length=128)
    wake_ticket: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def synthetic_ticket(self):
        if self.test and self.wake_ticket is not None:
            raise ValueError("Test events cannot carry a consumer ticket")
        if bool(self.host_control_challenge_id) != bool(self.host_control_subscription_id):
            raise ValueError("Host control events must target their original subscription")
        if self.host_control_request and not self.host_control_challenge_id:
            raise ValueError("Host control requests must have an original challenge")
        if self.host_control_challenge_id and (
            self.test or self.wake_ticket is not None or self.route_probe_id
        ):
            raise ValueError("Host control events cannot carry a probe or consumer ticket")
        return self


class WorkFilters(Model):
    conversation_id: Identifier
    goal_id: Identifier
    approval_id: Identifier
    project_id: Identifier
    environment_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )


class DelegationFilters(Model):
    conversation_id: Identifier
    project_id: Identifier
    environment_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    slot_id: Identifier
    policy_id: Identifier
    policy_version: int = Field(ge=1)


class OperationFilters(Model):
    project_id: Identifier
    environment_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$"
    )
    operation_ids: list[Identifier] = Field(min_length=1, max_length=16)
    slot_id: Identifier | None = None

    @model_validator(mode="after")
    def distinct_operations(self):
        if len(set(self.operation_ids)) != len(self.operation_ids):
            raise ValueError("operation_ids must be distinct original operation IDs")
        return self


class Delivery(Model):
    mode: Literal["webhook", "poll", "push"] = "webhook"
    url: str = Field(min_length=1, max_length=2048)


class SignedDelivery(Delivery):
    mode: Literal["webhook", "poll", "push"]
    secret: str = Field(min_length=1, max_length=100)


class Subscribe(Model):
    name: str = Field(min_length=1, max_length=100)
    arguments: dict
    delivery: SignedDelivery
    cursor: str | None = Field(default=None, max_length=2048)
    ttlMs: int | None = 86400000
    maxAgeMs: int | None = Field(default=None, ge=0)


class Unsubscribe(Model):
    name: str = Field(min_length=1, max_length=100)
    arguments: dict
    delivery: Delivery


class BasePayload(Model):
    schema_version: Literal[1]
    project_id: Identifier
    environment_id: str = Field(min_length=1, max_length=64)
    test: bool = False
    test_subscription_id: str = Field(default="", max_length=128)


class TaskPayload(BasePayload):
    job_id: Identifier
    job_version: int = Field(ge=1)
    queue: Queue
    assignee_agent_id: Identifier
    kind: JobKind
    incident_id: str | None = None
    goal_id: str | None = None
    reason_code: str = Field(min_length=1, max_length=100)
    severity: Severity | None = None
    rule_id: str | None = None


class ResultPayload(BasePayload):
    result_id: Identifier
    job_id: Identifier
    job_version: int = Field(ge=1)
    incident_id: str | None
    incident_version: int | None
    outcome: Literal["healthy", "explained", "action_required", "blocked", "inconclusive"]
    requires_decision: bool
    evidence_refs: list[Identifier] = Field(max_length=256)
    severity: Severity | None = None
    rule_id: str | None = None


class IncidentPayload(BasePayload):
    incident_id: Identifier
    transition: str = Field(min_length=1, max_length=64)
    severity: Severity
    state: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    episode: int = Field(ge=1)
    rule_id: Identifier
    evidence_ref: Identifier


class StatusPayload(BasePayload):
    component: str = Field(min_length=1, max_length=64)
    status: str = Field(min_length=1, max_length=64)
    reason_code: str = Field(min_length=1, max_length=100)
    observed_at: str = Field(min_length=1, max_length=64)
    recovery_state: str = Field(min_length=1, max_length=64)


class MessagePayload(BasePayload):
    conversation_id: Identifier
    room_id: Identifier
    message_id: Identifier
    message_version: int = Field(ge=1)
    recipient_slot_id: Identifier
    thread_root_id: Identifier


class MailboxPayload(Model):
    schema_version: Literal[2]
    room_id: Identifier
    project_id: Identifier
    environment_id: str = Field(min_length=1, max_length=64)
    test: bool = False
    test_subscription_id: str = Field(default="", max_length=128)
    conversation_id: Identifier
    recipient_slot_id: Identifier
    recipient_grant_id: Identifier
    binding_generation: Identifier
    dirty_revision: int = Field(ge=0)
    wake_ticket: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def synthetic_ticket(self):
        if self.test and self.wake_ticket is not None:
            raise ValueError("Test events cannot carry a consumer ticket")
        return self


class WorkPayload(BasePayload):
    conversation_id: Identifier
    goal_id: Identifier
    approval_id: Identifier
    work_item_id: Identifier | None = None
    work_item_version: int | None = Field(default=None, ge=1)
    target_project_id: Identifier
    recipient_grant_id: Identifier
    reason: Literal["work_available", "peer_message"]
    message_id: Identifier | None = None


class DelegationPayload(BasePayload):
    conversation_id: Identifier
    policy_id: Identifier
    policy_version: int = Field(ge=1)
    delegation_id: Identifier
    message_id: Identifier
    message_version: int = Field(ge=1)
    goal_id: Identifier
    approval_id: Identifier
    work_item_id: Identifier
    work_item_version: int = Field(ge=1)
    recipient_slot_id: Identifier
    recipient_grant_id: Identifier


class OperationQueryArguments(Model):
    operation: Literal["get"] = "get"
    operation_ids: list[Identifier] = Field(min_length=1, max_length=1)
    include_output: Literal[True] = True
    include_result: Literal[True] = True
    output_limit: Literal[8000] = 8000


class OperationQuery(Model):
    name: Literal["task_query"] = "task_query"
    arguments: OperationQueryArguments


class UnifiedOperationQueryArguments(Model):
    action: Literal["job"]
    view: Literal["get"]
    project: Identifier
    workspace_id: str = Field(default="", pattern=r"^(|[a-f0-9]{32})$")
    ids: list[Identifier] = Field(min_length=1, max_length=1)


class UnifiedOperationQuery(Model):
    name: Literal["query"]
    arguments: UnifiedOperationQueryArguments


class OperationPayload(BasePayload):
    operation_id: Identifier
    state: Literal["succeeded", "failed", "cancelled", "needs_review", "interrupted"]
    output_seq: int = Field(ge=0)
    recipient_grant_id: Identifier
    workspace_id: str = Field(default="", pattern=r"^(|[a-f0-9]{32})$")
    next_call: OperationQuery | None = None

    @model_validator(mode="after")
    def original_operation_query(self):
        if self.test:
            if self.next_call is not None:
                raise ValueError("test events must not carry a resume request")
        elif self.next_call is None:
            raise ValueError("resume request must read the original operation ID")
        elif isinstance(self.next_call, UnifiedOperationQuery):
            args = self.next_call.arguments
            if (
                args.ids != [self.operation_id]
                or args.project != self.project_id
                or args.workspace_id != self.workspace_id
            ):
                raise ValueError("resume request must use the original operation and workspace")
        elif self.next_call.arguments.operation_ids != [self.operation_id]:
            raise ValueError("resume request must read the original operation ID")
        return self


class UnifiedOperationPayload(OperationPayload):
    # Existing subscriptions and frozen deliveries still use the legacy call.
    next_call: UnifiedOperationQuery | OperationQuery | None = None


PAYLOADS = {
    ROOM_INBOX_EVENT: RoomInboxPayload,
    MAILBOX_EVENT: MailboxPayload,
    OPERATION_EVENT: OperationPayload,
    DELEGATION_EVENT: DelegationPayload,
    WORK_EVENT: WorkPayload,
    MESSAGE_EVENT: MessagePayload,
    TASK_EVENT: TaskPayload,
    RESULT_EVENT: ResultPayload,
    INCIDENT_EVENT: IncidentPayload,
    STATUS_EVENT: StatusPayload,
}
FILTERS = {
    ROOM_INBOX_EVENT: RoomInboxFilters,
    MAILBOX_EVENT: MailboxFilters,
    OPERATION_EVENT: OperationFilters,
    DELEGATION_EVENT: DelegationFilters,
    WORK_EVENT: WorkFilters,
    MESSAGE_EVENT: MessageFilters,
    TASK_EVENT: TaskFilters,
    RESULT_EVENT: EventFilters,
    INCIDENT_EVENT: EventFilters,
    STATUS_EVENT: EventFilters,
}


# The host repeats these descriptions in its generated create/update tools.
# Keep the always-visible directory short and put exact consumption here.
DESCRIPTIONS = {
    ROOM_INBOX_EVENT: "Room member inbox changed.",
    MAILBOX_EVENT: "Authorized Dot mailbox changed. Delivery grants no execution rights.",
    OPERATION_EVENT: "Original operation updated. Reconcile its receipt without replay.",
    DELEGATION_EVENT: "An approved delegation has an inbox update.",
    WORK_EVENT: "An approved goal has work or a peer message.",
    MESSAGE_EVENT: "An authorized member was addressed.",
    TASK_EVENT: "An authorized read-only analysis task is available.",
    RESULT_EVENT: "Validated analysis was saved.",
    INCIDENT_EVENT: "A monitoring incident changed state.",
    STATUS_EVENT: "Monitoring component status changed.",
}
_GUIDES = {
    ROOM_INBOX_EVENT: "For a host_control_request, execute the restricted request to continue the approved connection. It creates no work. Otherwise use the server-issued room inbox request for the exact member and binding generation. Probe replies prove connectivity only.",
    MAILBOX_EVENT: "Read the current mailbox and original ledgers. V2 writes require live consumer proof and fence. Synthetic tests issue no ticket. Preserve notification-only bindings.",
    OPERATION_EVENT: "Use query action=job, view=get with ids=[operation_id]. Supply your last-read args.after_output_seq, never the event’s new output_seq as a read checkpoint. Preserve project and workspace. needs_review and interrupted require reconciliation. A cancellation needs its durable terminal receipt. Delivery is not host consumption or execution proof.",
    DELEGATION_EVENT: "For CPD, use the saved query collab.inbox view=dot_inbox and recover the setup through query collab.get view=dot_connection. On every wake, paginate and reconcile original operations. Legacy notification_only consumers keep their saved baseline and exact inbox/resume requests. Only explicitly user-approved managed_execution may claim, heartbeat, execute or record work results.",
    WORK_EVENT: "Read the latest approval, work and capabilities on the original connection before acting. Native opt-in and execution approval are separate.",
    MESSAGE_EVENT: "Read the saved inbox with query collab.inbox and reply within the existing authorization. Chat creates no task and connector replies must not wake peers.",
    TASK_EVENT: "Read current task state before claiming; the project task scope is read-only.",
    RESULT_EVENT: "Read and acknowledge the original analysis result. A saved analysis does not establish business recovery.",
    INCIDENT_EVENT: "Confirm recovery from probe evidence.",
    STATUS_EVENT: "Read the affected component status. The event does not create another analysis job.",
}


def definition(name, *, unified=False, exact=False):
    filter_schema = schema_for(FILTERS[name])
    payload = UnifiedOperationPayload if unified and name == OPERATION_EVENT else PAYLOADS[name]
    # Some importers cannot translate refs and nullable anyOf branches. Each
    # event still has a typed object envelope; validation uses FILTERS[name].
    description = DESCRIPTIONS[name]
    if unified and name == ROOM_INBOX_EVENT:
        description += " Use query help with args.event for filters and recovery."
    return {
        "name": name,
        "description": description,
        "delivery": ["webhook"],
        "inputSchema": host_parameter_schema(filter_schema)
        if unified and not exact
        else filter_schema,
        "payloadSchema": schema_for(payload),
    }


def definitions(*, names=None, unified=False):
    return [definition(name, unified=unified) for name in (PAYLOADS if names is None else names)]


def event_help(name, *, detail="brief"):
    schema = schema_for(FILTERS[name])
    result = {
        "event": name,
        "description": DESCRIPTIONS[name],
        "filters": list(schema["properties"]),
        "required_filters": schema.get("required", []),
        "consume": _GUIDES[name],
        "subscription": (
            "Subscribe separately for this binding. Delivery and synthetic tests grant no execution rights. "
            "When updating triggers, supply the complete desired list."
        ),
    }
    if name == ROOM_INBOX_EVENT:
        result["renewal"] = {
            "host": "Persist the original subscription ID, callback identity, signing key, and last signed cursor. Renew the same subscription before refreshBefore.",
            "managed": "A room owner can enable connection health checks. Only a real host subscribe call renews a finite lifetime. Unsubscribe stops maintenance.",
            "restore": "Use the original signed cursor when available. Observe truncated and resume pending room inbox work with a fresh ticket.",
        }
    if detail == "schema":
        definition_for_help = definition(name, unified=True, exact=True)
        result.update(
            inputSchema=definition_for_help["inputSchema"],
            payloadSchema=definition_for_help["payloadSchema"],
        )
    return result
