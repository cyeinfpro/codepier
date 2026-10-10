"""Closed wire contracts for the versioned collaboration events."""
from typing import Literal
from pydantic import Field, model_validator
from shared.collaboration_contracts import Model, Identifier, Queue, JobKind, Severity
from hub.collaboration.common import TASK_EVENT, RESULT_EVENT, INCIDENT_EVENT, STATUS_EVENT, MESSAGE_EVENT, WORK_EVENT, DELEGATION_EVENT, OPERATION_EVENT


class EventFilters(Model):
    slot_id: Identifier | None = None
    project_id: Identifier
    environment_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
    severity_min: Severity | None = None
    rule_ids: list[Identifier] | None = Field(default=None, max_length=20)


class TaskFilters(EventFilters):
    queue: Queue


class MessageFilters(Model):
    conversation_id: Identifier
    project_id: Identifier
    environment_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
    slot_id: Identifier


class WorkFilters(Model):
    conversation_id: Identifier
    goal_id: Identifier
    approval_id: Identifier
    project_id: Identifier
    environment_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')


class DelegationFilters(Model):
    conversation_id: Identifier
    project_id: Identifier
    environment_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
    slot_id: Identifier
    policy_id: Identifier
    policy_version: int = Field(ge=1)


class OperationFilters(Model):
    project_id: Identifier
    environment_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
    operation_ids: list[Identifier] = Field(min_length=1, max_length=16)
    slot_id: Identifier | None = None

    @model_validator(mode='after')
    def distinct_operations(self):
        if len(set(self.operation_ids)) != len(self.operation_ids):
            raise ValueError('operation_ids must be distinct original operation IDs')
        return self


class Delivery(Model):
    mode: Literal['webhook', 'poll', 'push'] = 'webhook'
    url: str = Field(min_length=1, max_length=2048)


class SignedDelivery(Delivery):
    mode: Literal['webhook', 'poll', 'push']
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
    test_subscription_id: str = Field(default='', max_length=128)


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
    outcome: Literal['healthy', 'explained', 'action_required', 'blocked', 'inconclusive']
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


class WorkPayload(BasePayload):
    conversation_id: Identifier
    goal_id: Identifier
    approval_id: Identifier
    work_item_id: Identifier | None = None
    work_item_version: int | None = Field(default=None, ge=1)
    target_project_id: Identifier
    recipient_grant_id: Identifier
    reason: Literal['work_available', 'peer_message']
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
    operation: Literal['get'] = 'get'
    operation_ids: list[Identifier] = Field(min_length=1, max_length=1)
    include_output: Literal[True] = True
    include_result: Literal[True] = True
    output_limit: Literal[8000] = 8000


class OperationQuery(Model):
    name: Literal['task_query'] = 'task_query'
    arguments: OperationQueryArguments


class OperationPayload(BasePayload):
    operation_id: Identifier
    state: Literal['succeeded', 'failed', 'cancelled', 'needs_review', 'interrupted']
    output_seq: int = Field(ge=0)
    recipient_grant_id: Identifier
    workspace_id: str = Field(default='', pattern=r'^(|[a-f0-9]{32})$')
    next_call: OperationQuery | None = None

    @model_validator(mode='after')
    def original_operation_query(self):
        if self.test:
            if self.next_call is not None:
                raise ValueError('test events must not carry a resume request')
        elif self.next_call is None or self.next_call.arguments.operation_ids != [self.operation_id]:
            raise ValueError('resume request must read the original operation ID')
        return self


PAYLOADS = {OPERATION_EVENT: OperationPayload, DELEGATION_EVENT: DelegationPayload, WORK_EVENT: WorkPayload, MESSAGE_EVENT: MessagePayload, TASK_EVENT: TaskPayload, RESULT_EVENT: ResultPayload,
            INCIDENT_EVENT: IncidentPayload, STATUS_EVENT: StatusPayload}
FILTERS = {OPERATION_EVENT: OperationFilters, DELEGATION_EVENT: DelegationFilters, WORK_EVENT: WorkFilters, MESSAGE_EVENT: MessageFilters, TASK_EVENT: TaskFilters, RESULT_EVENT: EventFilters,
           INCIDENT_EVENT: EventFilters, STATUS_EVENT: EventFilters}


def definitions():
    descriptions = {
        OPERATION_EVENT: 'A bounded hint for explicitly subscribed original operation IDs on this exact grant. Read task_query(operation=get, operation_ids=[original ID]) and use your saved output cursor; never execute again. needs_review/interrupted do not prove success, and cancellation requires its durable terminal receipt. environment_id is notification context, not proof of execution environment. Delivery is accepted only, never host consumption or permission to continue a chat. Synthetic test events create no operation.',
        DELEGATION_EVENT: 'Wake-up for this exact owner policy/version and slot. Only at initial enrollment, read collaboration_delegation_connection_read and persist its checkpoint/inbox_request in the host consumer. On future wakes use the saved inbox_request/resume_request to reconcile collaboration_delegation_inbox, even without this payload; never refresh the initial baseline per event. Preserve existing notification_only consumers. Only an explicitly user-approved managed_execution host may fresh-read, claim, heartbeat, work_execute, poll the original operation and work_result. Test events only confirm delivery; a requested mode or notification never grants permission.',
        WORK_EVENT: 'An approved goal has work or a peer message for this exact connection. Requires separate native opt-in. Re-read current approval, work and capabilities; delivery never authorizes execution and a test creates no work.',
        MESSAGE_EVENT: 'An owner explicitly mentioned this exact room slot. Requires separate opt-in; read the bounded room message. This creates no task and a connector reply must not wake peers.',
        TASK_EVENT: 'A bounded read-only task is available in the authorized project queue. Read its current state before claiming. A test payload creates no work.',
        RESULT_EVENT: 'Validated analysis was saved. Read and acknowledge the result; this does not prove business recovery.',
        INCIDENT_EVENT: 'A deterministic monitoring incident changed state. Recovery is established by probes, not model assertions.',
        STATUS_EVENT: 'Collection, delivery or task consumption became degraded or recovered. This event does not create another analysis job.',
    }
    return [{'name': name, 'description': descriptions[name], 'delivery': ['webhook'],
             'inputSchema': FILTERS[name].model_json_schema(), 'payloadSchema': PAYLOADS[name].model_json_schema()}
            for name in PAYLOADS]
