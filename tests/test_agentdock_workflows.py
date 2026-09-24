"""Regression tests for persisted progress, isolation and honest execution evidence."""
import asyncio
import dataclasses
import json
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from jsonschema import Draft202012Validator

from hub.runtime import Principal, Runtime
from hub.store import Store
from shared.contracts import TOOLS, OUTPUT_SCHEMAS
from shared.util import DevError


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "hub")
    store.execute("INSERT INTO devices(id,name,secret,created) VALUES ('d','fixture','fixture',?)", (time.time(),))
    for id in ("p", "p2"):
        store.execute("INSERT INTO projects(id,alias,alias_key,device_id,root,allow_tasks,created) VALUES (?,?,?,'d',?,1,?)",
                      (id, id.upper(), id, str(tmp_path / id), time.time()))
    runtime = Runtime(store)
    principal = Principal("mcp:g:fixture", "u", {"read", "write", "execute"}, ["p", "p2"], "g")
    yield runtime, principal
    store.close()


def call(env, name, **args):
    result = asyncio.run(env[0].invoke(name, args, env[1]))
    Draft202012Validator(OUTPUT_SCHEMAS[name]).validate(result)
    return result


def create(env, **args):
    return call(env, "workflows_create", **{"project": "P", "title": "Current review", "goal": "Validate real results",
        "template": "custom", "steps": [{"title": "Review", "acceptance": "Read actual files"},
        {"title": "Verify", "acceptance": "Check actual result"}], "idempotency_key": uuid.uuid4().hex, **args})


def update(env, receipt, **args):
    return call(env, "workflows_update", **{"workflow_id": receipt["workflow_id"], "expected_version": receipt["version"],
        "action": "checkpoint", "summary": "Observed result", "idempotency_key": uuid.uuid4().hex, **args})


def get(env, receipt, **args):
    return call(env, "workflows_get", workflow_id=receipt["workflow_id"], **args)


def operation(env, *, project="p", grant="g", created=None, exit_code=0, output="passed", finish=True, **data):
    runtime, principal = env
    identifier = uuid.uuid4().hex
    now = time.time() if created is None else created
    runtime.store.execute("INSERT INTO operations(id,device_id,project_id,actor,grant_id,tool,args_summary,fingerprint,state,created,updated) VALUES (?,'d',?,?,?,'tasks_run','{}','fixture','running',?,?)",
                          (identifier, project, principal.actor, grant, now, now))
    if finish:
        runtime.complete({"id": identifier}, {"ok": True, "data": {"exit_code": exit_code, "output": output, **data}})
    return identifier


def test_create_replay_checkpoint_and_store_reopen(env):
    key = uuid.uuid4().hex
    receipt = create(env, idempotency_key=key)
    assert create(env, idempotency_key=key) == {**receipt, "replayed": True}
    assert env[0].store.one("SELECT COUNT(*) n FROM operations")["n"] == 0
    evidence = operation(env)
    receipt = update(env, receipt, step_id="s1", step_state="completed", evidence=[evidence])
    second = Store(env[0].store.directory)
    try:
        restored = call((Runtime(second), env[1]), "workflows_get", workflow_id=receipt["workflow_id"])
        assert restored["steps"][0]["evidence"][0]["operation_id"] == evidence
        assert restored["progress"] == {"completed": 1, "skipped": 0, "total": 2}
        assert restored["next_step"] == "s2" and restored["version"] == 2
    finally:
        second.close()


def test_update_replay_precedes_version_check_but_not_authorization(env):
    receipt = create(env)
    args = {"step_id": "s1", "step_state": "running", "idempotency_key": uuid.uuid4().hex}
    saved = update(env, receipt, **args)
    assert update(env, receipt, **args) == {**saved, "replayed": True}
    assert len(get(env, receipt)["events"]) == 2
    with pytest.raises(DevError, match="幂等键"):
        update(env, receipt, **{**args, "summary": "Different request"})
    with pytest.raises(DevError) as exc:
        update(env, receipt)
    assert exc.value.code == "VERSION_CONFLICT"
    env[0].store.execute("UPDATE projects SET mode='read' WHERE id='p'")
    with pytest.raises(DevError) as exc:
        update(env, receipt, **args)
    assert exc.value.code == "READ_ONLY"


@pytest.mark.parametrize("kind", ["missing", "failed", "pending", "stale", "other_project", "other_grant", "timeout", "cancelled", "boolean_exit"])
def test_completion_requires_current_successful_scoped_evidence(env, kind):
    receipt = create(env)
    opts = {"failed": {"exit_code": 3}, "pending": {"finish": False}, "stale": {"created": 1},
            "other_project": {"project": "p2"}, "other_grant": {"grant": "g2"}, "timeout": {"timed_out": True},
            "cancelled": {"cancelled": True}, "boolean_exit": {"exit_code": False}}
    evidence = [] if kind == "missing" else [operation(env, **opts[kind])]
    with pytest.raises(DevError):
        update(env, receipt, step_id="s1", step_state="completed", evidence=evidence)
    restored = get(env, receipt)
    assert restored["version"] == 1 and restored["steps"][0]["state"] == "pending"
    assert len(restored["events"]) == 1


def test_final_review_gate_closed_state_and_cancel_is_not_process_cancel(env):
    receipt = create(env)
    with pytest.raises(DevError):
        update(env, receipt, action="complete")
    receipt = update(env, receipt, step_id="s1", step_state="completed", evidence=[operation(env)])
    receipt = update(env, receipt, step_id="s2", step_state="skipped", summary="Not applicable: no platform build requested")
    receipt = update(env, receipt, action="complete", summary="Read verified; second step explicitly not applicable")
    assert get(env, receipt)["state"] == "completed"
    with pytest.raises(DevError):
        update(env, receipt, action="resume")
    cancelled = create(env)
    op = operation(env, finish=False)
    update(env, cancelled, action="cancel", summary="Owner changed scope")
    assert env[0].operation(op, env[1])["state"] == "running"
    assert env[0].operation(op, env[1])["cancel_requested"] == 0


def test_all_skipped_is_not_a_completed_task(env):
    receipt = create(env)
    for step in ("s1", "s2"):
        receipt = update(env, receipt, step_id=step, step_state="skipped", summary="Not performed")
    with pytest.raises(DevError) as exc:
        update(env, receipt, action="complete")
    assert exc.value.code == "INCOMPLETE_STEPS"


def test_block_resume_single_current_step_and_permission_isolation(env):
    receipt = update(env, create(env), step_id="s1", step_state="running")
    with pytest.raises(DevError):
        update(env, receipt, step_id="s2", step_state="running")
    receipt = update(env, receipt, action="block", summary="Need owner decision")
    with pytest.raises(DevError):
        update(env, receipt, step_id="s1", step_state="pending")
    receipt = update(env, receipt, action="resume", summary="Owner decision recorded")
    for principal in (dataclasses.replace(env[1], grant_id="other"), dataclasses.replace(env[1], projects=[]),
                      dataclasses.replace(env[1], grant_id=None)):
        with pytest.raises(DevError):
            get((env[0], principal), receipt)
        assert not call((env[0], principal), "workflows_list")["workflows"]
    with pytest.raises(DevError) as exc:
        create((env[0], dataclasses.replace(env[1], scopes={"read"})))
    assert exc.value.code == "INSUFFICIENT_SCOPE"


def test_mapping_change_warns_and_fences_mutations(env):
    receipt = create(env)
    env[0].store.execute("UPDATE projects SET root='/different/project' WHERE id='p'")
    assert get(env, receipt)["mapping_changed"]
    with pytest.raises(DevError) as exc:
        update(env, receipt)
    assert exc.value.code == "WORKFLOW_MAPPING_CHANGED"


def test_write_event_and_replay_receipt_are_atomic(env):
    store = env[0].store
    store.execute("CREATE TRIGGER fail_checkpoint BEFORE INSERT ON workflow_events BEGIN SELECT RAISE(ABORT,'injected'); END")
    key = uuid.uuid4().hex
    with pytest.raises(sqlite3.IntegrityError):
        create(env, idempotency_key=key)
    for table in ("workflows", "workflow_events", "workflow_replays"):
        assert store.one(f"SELECT COUNT(*) n FROM {table}")["n"] == 0
    store.execute("DROP TRIGGER fail_checkpoint")
    assert create(env, idempotency_key=key)["version"] == 1


def test_concurrent_compare_and_swap_has_one_winner(env):
    receipt = create(env)
    def writer(i):
        try:
            return update(env, receipt, summary=f"writer {i}")["version"]
        except DevError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(writer, range(2)))
    assert sorted(map(str, results)) == ["2", "VERSION_CONFLICT"]
    assert len(get(env, receipt)["events"]) == 2


def test_stable_cursor_handles_tied_timestamps_and_event_paging(env):
    receipts = [create(env) for _ in range(5)]
    env[0].store.execute("UPDATE workflows SET created=1234")
    first = call(env, "workflows_list", limit=2)
    second = call(env, "workflows_list", limit=2, cursor=first["next_cursor"])
    third = call(env, "workflows_list", limit=2, cursor=second["next_cursor"])
    ids = [r["workflow_id"] for page in (first, second, third) for r in page["workflows"]]
    assert len(set(ids)) == 5 and third["next_cursor"] is None
    receipt = receipts[0]
    for _ in range(3):
        receipt = update(env, receipt)
    recent = get(env, receipt, event_limit=2)
    older = get(env, receipt, event_limit=2, before_event_id=recent["next_before_event_id"])
    assert len({e["id"] for e in recent["events"] + older["events"]}) == 4
    assert older["next_before_event_id"] is None
    with pytest.raises(DevError):
        call(env, "workflows_list", cursor="bad cursor")


@pytest.mark.parametrize("name", list(TOOLS))
def test_output_schemas_cover_errors_and_remote_pending_receipts(name):
    schema = OUTPUT_SCHEMAS[name]
    Draft202012Validator.check_schema(schema)
    error = {"error": {"code": "FAILURE", "message": "Expected tool error"}}
    if name == 'get_profile':
        # Identity discovery uses the standard strict profile schema. Its errors
        # are text/isError only; they must never masquerade as an identity.
        assert schema['additionalProperties'] is False
        assert not Draft202012Validator(schema).is_valid(error)
        Draft202012Validator(schema).validate({'id': 'opaque-profile'})
    else:
        Draft202012Validator(schema).validate(error)
    assert not Draft202012Validator(schema).is_valid({})
    if not TOOLS[name].local:
        Draft202012Validator(schema).validate({"operation_id": "receipt", "pending": True, "state": "queued"})
        assert not Draft202012Validator(schema).is_valid({"operation_id": "receipt", "pending": False, "state": "queued"})


@pytest.mark.parametrize("include_result", [True, False])
def test_compact_polling_omits_nested_output_without_losing_durable_data(env, include_result):
    identifier = operation(env, output="你好" * 30000)
    compact = call(env, "operations_wait", operation_id=identifier, wait_seconds=0, include_output=False, include_result=include_result)
    assert "output" not in compact
    if include_result:
        assert "output" not in compact["result"]["data"]
    else:
        assert "result" not in compact
    full = call(env, "operations_get", operation_id=identifier)
    assert full["result"]["data"]["output"] == "你好" * 30000
    assert len(json.dumps(compact)) < len(json.dumps(full)) / 100
    tail = call(env, "operations_get", operation_id=identifier, output_limit=5)
    assert len(tail["output"]) == 5 and len(tail["result"]["data"]["output"]) == 5
    zero = call(env, "operations_get", operation_id=identifier, output_limit=0)
    assert zero["output"] == zero["result"]["data"]["output"] == "" and zero["output_truncated"]


def test_final_output_advances_cursor_and_command_ok_is_not_transport_ok(env):
    identifier = operation(env, finish=False)
    before = call(env, "operations_get", operation_id=identifier)
    env[0].complete({"id": identifier}, {"ok": True, "data": {"exit_code": 3, "output": "test assertion failed"}})
    after = call(env, "operations_get", operation_id=identifier, after_output_seq=before["output_seq"])
    assert after["output_seq"] > before["output_seq"] and after["output"] == "test assertion failed"
    assert after["state"] == "failed" and after["result"]["ok"] and not after["result"]["data"]["command_ok"]
    unchanged = call(env, "operations_get", operation_id=identifier, after_output_seq=after["output_seq"])
    assert unchanged["output_unchanged"] and "output" not in unchanged["result"]["data"]
    assert call(env, "projects_resolve", project="P")["allow_tasks"] is True


def test_owner_can_explicitly_handoff_to_scoped_mcp_grant(env):
    runtime, principal = env
    runtime.store.execute("INSERT INTO grants(id,user_id,label,scopes,projects,created) VALUES ('g','u','Fixture','[\"read\",\"write\"]','[\"p\"]',?)", (time.time(),))
    runtime.store.execute("INSERT INTO tokens(id,hash,grant_id,kind,expires,created) VALUES ('t','test-only','g','pat',?,?)", (time.time()+3600,time.time()))
    admin = dataclasses.replace(principal, actor='panel:u', grant_id=None, admin=True)
    receipt = create((runtime, admin), assignee_grant_id='g')
    assert get(env, receipt)['assigned_to_mcp']
    assert call(env, 'workflows_list')['workflows'][0]['workflow_id'] == receipt['workflow_id']
    owner_only = create((runtime, admin))
    with pytest.raises(DevError):
        get(env, owner_only)
    with pytest.raises(DevError) as exc:
        create(env, assignee_grant_id='other')
    assert exc.value.code == 'ASSIGNEE_FORBIDDEN'
    with pytest.raises(DevError):
        create((runtime, admin), project='P2', assignee_grant_id='g')
    runtime.store.execute("UPDATE grants SET revoked=1 WHERE id='g'")
    with pytest.raises(DevError):
        create((runtime, admin), assignee_grant_id='g')


@pytest.mark.parametrize('code,extra,expected', [(0,{},'succeeded'),(128,{},'failed'),(False,{},'failed'),(0,{'timed_out':True},'failed')])
def test_git_process_outcomes_follow_actual_exit_status(env, code, extra, expected):
    identifier=operation(env,finish=False)
    env[0].store.execute("UPDATE operations SET tool='git_status' WHERE id=?",(identifier,))
    env[0].complete({'id':identifier},{'ok':True,'data':{'exit_code':code,'output':'git result',**extra}})
    op=call(env,'operations_get',operation_id=identifier)
    assert op['state']==expected
    assert op['result']['data']['command_ok']==(expected=='succeeded')
