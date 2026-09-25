"""Real HTTP Hub, outbound Agent, MCP and Chromium acceptance checks."""
import json
import os
import time
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator
from playwright.sync_api import expect, sync_playwright

from shared.contracts import OUTPUT_SCHEMAS
from tests.support import wait_for


def mcp(stack, name, args):
    from hub.core_tools import public_call
    public_name, public_args = public_call(name, args)
    result = stack.mcp(public_name, public_args)
    data = result["structuredContent"]
    if public_name == 'process' and 'operations' in data:
        data = data['operations'][0]
    assert not isinstance(data.get('error'), dict), result
    Draft202012Validator(OUTPUT_SCHEMAS[name]).validate(data)
    return data


def wf_args(title):
    return {"project": "Imago", "title": title, "goal": "Read and verify the actual fixture", "template": "custom",
            "steps": [{"title": "Check fixture", "acceptance": "Read the current README"}], "idempotency_key": uuid.uuid4().hex}


def test_mcp_context_workflow_evidence_and_complete(stack):
    args = wf_args("MCP round trip")
    created = mcp(stack, "workflows_create", args)
    assert mcp(stack, "workflows_create", args)["replayed"]
    context = mcp(stack, "project_context", {"project": "Imago", "max_chars": 1000})
    if context.get("pending"):
        op = stack.poll(context["operation_id"])
        context = {"operation_id": op["id"], **op["result"]["data"]}
    assert context["documents"][0]["path"] == "README.md"
    checkpoint = mcp(stack, "workflows_update", {"workflow_id": created["workflow_id"], "expected_version": 1,
        "action": "checkpoint", "step_id": "s1", "step_state": "completed", "summary": "README content inspected",
        "evidence": [context["operation_id"]], "idempotency_key": uuid.uuid4().hex})
    completed = mcp(stack, "workflows_update", {"workflow_id": created["workflow_id"], "expected_version": checkpoint["version"],
        "action": "complete", "summary": "Expected fixture content confirmed", "idempotency_key": uuid.uuid4().hex})
    assert completed["state"] == "completed"
    assert mcp(stack, "workflows_get", {"workflow_id": created["workflow_id"]})["next_step"] is None


def test_real_command_failure_is_evidence_not_false_success(stack):
    receipt = mcp(stack, "workflows_create", wf_args("Failure evidence"))
    submitted = mcp(stack, "tasks_run", {"project": "Imago", "task": "failure", "idempotency_key": uuid.uuid4().hex})
    op = stack.poll(submitted["operation_id"])
    assert op["state"] == "failed" and op["result"]["data"]["exit_code"] == 3
    assert not op["result"]["data"]["command_ok"]
    compact = mcp(stack, "operations_get", {"operation_id": op["id"], "include_result": False, "include_output": False})
    assert "output" not in compact and "result" not in compact
    error = stack.mcp('workspace', {'idempotency_key': uuid.uuid4().hex, 'operation': 'workflow_update', 'options': {'workflow_id': receipt['workflow_id'], 'expected_version': 1, 'action': 'checkpoint', 'step_id': 's1', 'step_state': 'completed', 'summary': 'Should be rejected', 'evidence': [op['id']]}})
    assert error["isError"] and error["structuredContent"]["error"]["code"] == "EVIDENCE_NOT_SUCCESSFUL"
    Draft202012Validator(OUTPUT_SCHEMAS["workflows_update"]).validate(error["structuredContent"])


def test_saved_workflow_survives_actual_hub_restart(stack):
    args = wf_args("Restart recovery")
    receipt = mcp(stack, "workflows_create", args)
    mcp(stack, "workflows_update", {"workflow_id": receipt["workflow_id"], "expected_version": 1,
        "action": "block", "summary": "Waiting for a decision", "idempotency_key": uuid.uuid4().hex})
    stack.hub.terminate()
    stack.hub.wait(timeout=12)
    stack.start_hub()
    restored = mcp(stack, "workflows_get", {"workflow_id": receipt["workflow_id"]})
    assert restored["version"] == 2 and restored["state"] == "blocked" and len(restored["events"]) == 2
    assert mcp(stack, "workflows_create", args)["replayed"]
    resumed = mcp(stack, "workflows_update", {"workflow_id": receipt["workflow_id"], "expected_version": 2,
        "action": "resume", "summary": "Decision recorded", "idempotency_key": uuid.uuid4().hex})
    assert resumed["state"] == "active"
    wait_for(lambda: any(d["id"] == stack.device and d["online"] for d in stack.client.get('/api/devices').json()['devices']), timeout=20)


def test_browser_task_creation_evidence_gate_progress_context_and_responsiveness(stack, tmp_path):
    screenshots = Path(os.getenv("AGENTDOCK_SCREENSHOTS_DIR", str(tmp_path / "screenshots")))
    screenshots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            page.goto(stack.url + '/#workflows')
            page.fill('#username', 'admin');page.fill('#password', stack.password)
            page.click('#login-form button')
            expect(page.locator('#page h1')).to_have_text('开发任务')
            page.click('[data-wf-action="create"]')
            page.fill('#wf-title', '浏览器验收')
            page.fill('#wf-goal', '验证断点、执行证据与最终验收。<img src=x onerror=window.unexpectedXss=1>')
            page.click('#workflow-create-save')
            expect(page.locator('#modal-title')).to_have_text('浏览器验收')
            assert page.locator('.modal img').count() == 0
            assert page.evaluate('window.unexpectedXss') is None
            rows = stack.call('workflows_list', {'project': 'Imago'})['workflows']
            workflow = next(w for w in rows if w['title'] == '浏览器验收')
            identifier = workflow['workflow_id']
            page.click('#workflow-edit')
            page.select_option('#wf-step', 's1')
            page.select_option('#wf-step-state', 'completed')
            page.fill('#wf-summary', '已核对入口文档与实际返回值。')
            page.click('#workflow-update-save')
            expect(page.locator('[data-workflow-hint]')).to_contain_text('成功操作')
            evidence = stack.fs('fs_read', path='README.md')
            if evidence.get('pending'):
                stack.poll(evidence['operation_id'])
            page.fill('#wf-evidence', evidence['operation_id'])
            page.click('#workflow-update-save')
            expect(page.locator('#workflow-edit')).to_be_visible()
            for step in ('s2', 's3'):
                page.click('#workflow-edit')
                page.select_option('#wf-step', step)
                page.select_option('#wf-step-state', 'skipped')
                page.fill('#wf-summary', '本任务仅验证管理界面，未要求修改或发布业务代码。')
                page.click('#workflow-update-save')
                expect(page.locator('#workflow-edit')).to_be_visible()
            page.click('#workflow-edit')
            page.select_option('#wf-action', 'complete')
            page.fill('#wf-summary', '已核对真实读取结果和步骤记录；其余步骤已说明不适用。')
            page.click('#workflow-update-save')
            expect(page.locator('.workflow-detail-meta')).to_contain_text('已验收')
            page.screenshot(path=str(screenshots / 'workflow-desktop-detail.png'), full_page=True)
            page.locator('.modal [data-action="close-modal"]').first.click()
            page.screenshot(path=str(screenshots / 'workflow-desktop-list.png'), full_page=True)
            page.set_viewport_size({'width': 390, 'height': 844})
            page.reload()
            expect(page.locator('#page h1')).to_have_text('开发任务')
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            page.screenshot(path=str(screenshots / 'workflow-mobile-list.png'), full_page=True)
            page.locator(f'#page [data-wf-action="detail"][data-id="{identifier}"]').click()
            expect(page.locator('#modal-title')).to_have_text('浏览器验收')
            assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
            page.screenshot(path=str(screenshots / 'workflow-mobile-detail.png'), full_page=True)
            page.locator('.modal [data-action="close-modal"]').first.click()
            page.set_viewport_size({'width': 1440, 'height': 1000})
            page.click('[data-nav="workbench"]')
            page.locator('.tool-menu summary').click()
            page.click('[data-wf-action="context"]')
            expect(page.locator('#modal-title')).to_contain_text('项目上下文 ·')
            expect(page.locator('.context-document summary').first).to_contain_text('README.md')
            page.locator('.context-document summary').first.click()
            page.screenshot(path=str(screenshots / 'context-desktop.png'), full_page=True)
            page.locator('.modal [data-action="close-modal"]').first.click()
            page.click('[data-nav="workflows"]')
            expect(page.locator('#page h1')).to_have_text('开发任务')
            page.evaluate('''() => {
              const original=window.fetch;
              window.fetch=async (...args)=>{
                const body=args[1]?.body;
                if(typeof body==='string' && JSON.parse(body).tool==='workflows_get'){
                  await new Promise(resolve=>window.releaseWorkflowRead=resolve);
                  const response=await original(...args);window.workflowReadFinished=true;return response;
                }
                return original(...args);
              };
            }''')
            page.locator(f'#page [data-wf-action="detail"][data-id="{identifier}"]').click()
            expect(page.locator('#modal-title')).to_have_text('读取开发任务')
            page.locator('.modal [data-action="close-modal"]').first.click()
            page.wait_for_function('() => typeof window.releaseWorkflowRead==="function"')
            page.evaluate('window.releaseWorkflowRead()')
            page.wait_for_function('() => window.workflowReadFinished===true')
            page.wait_for_timeout(150)
            assert page.locator('.modal').count() == 0
            assert not errors, errors
        finally:
            browser.close()


def test_owner_panel_handoff_is_visible_to_selected_mcp_and_rejects_other_grants(stack):
    receipt = stack.call('workflows_create', {**wf_args('Owner to MCP handoff'), 'assignee_grant_id': stack.grant})
    rows = mcp(stack, 'workflows_list', {'project': 'Imago'})['workflows']
    assert any(w['workflow_id'] == receipt['workflow_id'] for w in rows)
    assert mcp(stack, 'workflows_get', {'workflow_id': receipt['workflow_id']})['assigned_to_mcp']
    denied = stack.mcp('workspace', {**{k:v for k,v in ({**wf_args('Denied handoff'), 'assignee_grant_id': 'not-this-grant'}).items() if k in {'project','workspace_id','idempotency_key'}}, 'operation': 'workflow_create', 'options': {k:v for k,v in ({**wf_args('Denied handoff'), 'assignee_grant_id': 'not-this-grant'}).items() if k not in {'project','workspace_id','idempotency_key'}}})
    assert denied['isError'] and denied['structuredContent']['error']['code'] == 'ASSIGNEE_FORBIDDEN'
