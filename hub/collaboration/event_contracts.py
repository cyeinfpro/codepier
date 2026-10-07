"""Closed wire contracts for the four versioned collaboration events."""
from typing import Literal
from pydantic import Field
from shared.collaboration_contracts import Model, Identifier, Queue, JobKind, Severity
from hub.collaboration.common import TASK_EVENT, RESULT_EVENT, INCIDENT_EVENT, STATUS_EVENT


class EventFilters(Model):
    project_id: Identifier
    environment_id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]*$')
    severity_min: Severity | None = None
    rule_ids: list[Identifier] | None = Field(default=None, max_length=20)


class TaskFilters(EventFilters):
    queue: Queue


class Delivery(Model):
    mode: Literal['webhook']
    url: str = Field(min_length=1, max_length=2048)


class SignedDelivery(Delivery):
    secret: str = Field(min_length=1, max_length=100)


class Subscribe(Model):
    name: str = Field(min_length=1, max_length=100)
    arguments: dict
    delivery: SignedDelivery
    cursor: str | None = Field(default=None, max_length=2048)
    ttlMs: int | None = Field(default=86400000, ge=1000)


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


PAYLOADS = {TASK_EVENT: TaskPayload, RESULT_EVENT: ResultPayload,
            INCIDENT_EVENT: IncidentPayload, STATUS_EVENT: StatusPayload}
FILTERS = {TASK_EVENT: TaskFilters, RESULT_EVENT: EventFilters,
           INCIDENT_EVENT: EventFilters, STATUS_EVENT: EventFilters}


def definitions():
    descriptions = {
        TASK_EVENT: 'A bounded read-only task is available in the authorized project queue. Read its current state before claiming. A test payload creates no work.',
        RESULT_EVENT: 'Validated analysis was saved. Read and acknowledge the result; this does not prove business recovery.',
        INCIDENT_EVENT: 'A deterministic monitoring incident changed state. Recovery is established by probes, not model assertions.',
        STATUS_EVENT: 'Collection, delivery or task consumption became degraded or recovered. This event does not create another analysis job.',
    }
    return [{'name': name, 'description': descriptions[name], 'delivery': ['webhook'],
             'inputSchema': FILTERS[name].model_json_schema(), 'payloadSchema': PAYLOADS[name].model_json_schema()}
            for name in PAYLOADS]
