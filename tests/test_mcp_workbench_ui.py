"""Real SDK sandbox coverage for explicit, read-only workbench navigation.

Responses are deterministic host fixtures; this module verifies browser state,
request contracts and lifecycle races, not live Hub authorization or deployment.
"""
import shutil
import subprocess

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.support import BASE
from tests.test_mcp_apps_host import host_bundle


PROJECT_A = {
    "id": "a" * 32, "alias": "Project A", "root": "/fixture/project-a",
    "description": "<img src=x onerror='window.INJECTED=true'>",
    "device_name": "Fixture device", "online": True, "mode": "read", "allow_tasks": False,
}
PROJECT_B = {
    "id": "b" * 32, "alias": "Project B", "root": "/fixture/project-b",
    "device_name": "Other device", "online": False, "mode": "write", "allow_tasks": True,
}


@pytest.fixture(scope="module")
def workbench_html(tmp_path_factory):
    """Build current source into a temporary file, leaving shipped outputs alone."""
    directory = tmp_path_factory.mktemp("workbench-source")
    path = directory / "app.js"
    code = ("const {build}=require(process.argv[1]);"
            "build({entryPoints:[process.argv[2]],outfile:process.argv[3],"
            "bundle:true,platform:'browser',format:'iife'})"
            ".catch(e=>{console.error(e);process.exit(1)})")
    result = subprocess.run(
        [shutil.which("node"), "-e", code,
         str(BASE / "web/mcp-apps/node_modules/esbuild/lib/main.js"),
         str(BASE / "web/mcp-apps/app.js"), str(path)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    script = path.read_text().replace("</script", "<\\/script")
    css = (BASE / "web/mcp-apps/app.css").read_text()
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<style>{css}</style><body data-kind="workspace">'
            f'<main id="app"></main><script>{script}</script></body></html>')


def response(value):
    return {"structuredContent": value, "content": [], "isError": False}


def card(projects):
    return {**response({"projects": projects}),
            "_meta": {"com.codepier/binding": {"kind": "workbench"}}}


def opened(project):
    return {"workspace": {"project": project["alias"], "project_id": project["id"],
                          "root": project["root"], "granted_scopes": ["read"]}}


def mount(page, host_bundle, html, projects):
    page.set_content("<!doctype html><html><body></body></html>")
    page.add_script_tag(path=str(host_bundle))
    page.evaluate("""() => {
      window.workbenchCalls = [];
      window.workbenchPending = [];
      window.codepierHostTool = params => {
        window.workbenchCalls.push(params);
        if (params.name === 'project_query' && params.arguments.operation === 'dashboard')
          return Promise.resolve({content: [], isError: false, structuredContent: {
            project_id: params.arguments.project, workspace_id: '', observed_at: 1,
            device_online: true, workflow: null, workflows: [], evidence: [],
            recent_operations: [], evidence_total: 0, unavailable_evidence: 0,
            next_workflow_cursor: null, next_evidence_offset: null
          }});
        return new Promise(resolve => window.workbenchPending.push({params, resolve, done: false}));
      };
    }""")
    page.evaluate("html=>window.codepierMount(html)", html)
    page.wait_for_function("window.codepierHostReady")
    page.evaluate("result=>window.codepierDeliver({args:{},result})", card(projects))
    app = page.frame_locator("#app-frame")
    expect(app.get_by_role("heading", name="选择项目", exact=True)).to_be_visible()
    return app


def wait_pending(page, operation, count=1):
    page.wait_for_function(
        """([operation,count])=>window.workbenchPending
          .filter(p=>p.params.arguments.operation===operation).length>=count""",
        arg=[operation, count],
    )


def deliver(page, operation, value, index=0, *, raw=False):
    wait_pending(page, operation, index + 1)
    page.evaluate("""([operation,index,result])=>{
      const pending=window.workbenchPending.filter(p=>p.params.arguments.operation===operation)[index];
      pending.done=true; pending.resolve(result);
    }""", [operation, index, value if raw else response(value)])


def expect_clean(page, errors):
    assert not errors
    assert not page.evaluate("window.codepierHostErrors")
    calls = page.evaluate("window.workbenchCalls")
    assert all(call["name"] in {"project_query", "task_query"} for call in calls)
    assert all("capture_baseline" not in call["arguments"] for call in calls)


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_projected_roots_keep_distinct_project_selection(host_bundle, workbench_html, engine):
    from shared.mcp_presentation import present
    raw = [{**PROJECT_A, 'device_id': 'node-a'}, {**PROJECT_B, 'device_id': 'node-b'}]
    projects = present('workbench', {}, {'projects': raw})['projects']
    assert projects[0]['root'] == projects[1]['root'] == '.'
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        app = mount(page, host_bundle, workbench_html, projects)
        expect(app.locator('#app')).not_to_contain_text(PROJECT_A['root'])
        expect(app.locator('#app')).not_to_contain_text(PROJECT_A['device_name'])
        app.get_by_role('button', name='打开项目 Project A', exact=True).click()
        deliver(page, 'open', opened(projects[0]))
        expect(app.get_by_role('heading', name='Project A', exact=True)).to_be_visible()
        calls = page.evaluate('window.workbenchCalls')
        selected = [call['arguments']['project'] for call in calls if call['arguments'].get('operation') == 'open']
        assert selected == [PROJECT_A['id']]
        expect_clean(page, errors)
        browser.close()


@pytest.mark.parametrize("engine,width", [("chromium", 1100), ("webkit", 390)])
def test_workbench_requires_explicit_singleton_selection_back_and_refresh(host_bundle, workbench_html, engine, width):
    with sync_playwright() as pw:
        browser = getattr(pw, engine).launch(headless=True)
        page = browser.new_page(viewport={"width": width, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        app = mount(page, host_bundle, workbench_html, [PROJECT_A])
        assert page.evaluate("window.workbenchCalls") == []
        expect(app.locator("#app")).to_contain_text(PROJECT_A["root"])
        expect(app.locator("#app")).to_contain_text("Fixture device")
        assert app.locator("img").count() == 0
        # Two synchronous clicks may start only one read.
        app.get_by_role("button", name="打开项目 Project A", exact=True).evaluate(
            "node=>{node.click();node.click();}")
        wait_pending(page, "open")
        first_call = page.evaluate("window.workbenchCalls[0]")
        assert {key: first_call[key] for key in ("name", "arguments")} == {
            "name": "project_query", "arguments": {"operation": "open", "project": PROJECT_A["id"]},
        }
        expect(app.get_by_role("button", name="刷新项目列表")).to_be_disabled()
        deliver(page, "open", opened(PROJECT_A))
        expect(app.get_by_role("heading", name="Project A", exact=True)).to_be_visible()
        app.get_by_label("自动刷新任务状态").uncheck()
        app.get_by_role("button", name="返回项目选择").click()
        expect(app.get_by_role("heading", name="选择项目", exact=True)).to_be_visible()
        app.get_by_role("button", name="刷新项目列表").click()
        deliver(page, "list", {"projects": [PROJECT_B]})
        expect(app.get_by_role("button", name="打开项目 Project B", exact=True)).to_be_visible()
        assert app.get_by_role("button", name="打开项目 Project A", exact=True).count() == 0
        assert page.evaluate("window.workbenchPending.filter(p=>p.params.arguments.operation==='open').length") == 1
        app.get_by_role("button", name="刷新项目列表").click()
        deliver(page, "list", {"projects": []}, index=1)
        expect(app.locator("#app")).to_contain_text("没有可访问的项目")
        assert app.locator("html").evaluate("node=>node.scrollWidth <= node.clientWidth + 1")
        expect_clean(page, errors)
        browser.close()


def test_workbench_cancel_hidden_and_new_result_discard_delayed_selection(host_bundle, workbench_html):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        app = mount(page, host_bundle, workbench_html, [PROJECT_A, PROJECT_B])
        app.get_by_role("button", name="打开项目 Project A", exact=True).click()
        wait_pending(page, "open")
        app.get_by_role("button", name="取消选择").click()
        app.get_by_role("button", name="打开项目 Project B", exact=True).click()
        wait_pending(page, "open", 2)
        deliver(page, "open", opened(PROJECT_A), index=0)
        expect(app.get_by_role("heading", name="选择项目", exact=True)).to_be_visible()
        deliver(page, "open", opened(PROJECT_B), index=1)
        expect(app.get_by_role("heading", name="Project B", exact=True)).to_be_visible()
        app.get_by_label("自动刷新任务状态").uncheck()
        app.get_by_role("button", name="返回项目选择").click()
        app.get_by_role("button", name="打开项目 Project A", exact=True).click()
        wait_pending(page, "open", 3)
        app.locator("html").evaluate("""()=>{
          Object.defineProperty(document,'hidden',{configurable:true,value:true});
          document.dispatchEvent(new Event('visibilitychange'));
        }""")
        deliver(page, "open", opened(PROJECT_A), index=2)
        expect(app.locator("#app")).to_contain_text("页面已隐藏")
        expect(app.get_by_role("heading", name="选择项目", exact=True)).to_be_visible()
        app.get_by_role("button", name="打开项目 Project A", exact=True).evaluate("node=>node.click()")
        assert page.evaluate("window.workbenchPending.filter(p=>p.params.arguments.operation==='open').length") == 3
        app.locator("html").evaluate("""()=>{
          Object.defineProperty(document,'hidden',{configurable:true,value:false});
          document.dispatchEvent(new Event('visibilitychange'));
        }""")
        app.get_by_role("button", name="打开项目 Project B", exact=True).click()
        wait_pending(page, "open", 4)
        page.evaluate("result=>window.codepierDeliver({args:{},result})", card([PROJECT_A]))
        expect(app.get_by_role("button", name="打开项目 Project A", exact=True)).to_be_visible()
        deliver(page, "open", opened(PROJECT_B), index=3)
        expect(app.get_by_role("heading", name="选择项目", exact=True)).to_be_visible()
        assert app.get_by_role("button", name="打开项目 Project B", exact=True).count() == 0
        # A cancelled list refresh must not replace a newer list.
        app.get_by_role("button", name="刷新项目列表").click()
        wait_pending(page, "list")
        app.get_by_role("button", name="取消选择").click()
        app.get_by_role("button", name="刷新项目列表").click()
        wait_pending(page, "list", 2)
        deliver(page, "list", {"projects": [PROJECT_B]}, index=1)
        expect(app.get_by_role("button", name="打开项目 Project B", exact=True)).to_be_visible()
        deliver(page, "list", {"projects": [PROJECT_A]}, index=0)
        expect(app.get_by_role("button", name="打开项目 Project B", exact=True)).to_be_visible()
        assert app.get_by_role("button", name="打开项目 Project A", exact=True).count() == 0
        expect_clean(page, errors)
        browser.close()


def test_workbench_mismatch_refresh_error_and_pending_read_receipt(host_bundle, workbench_html):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        app = mount(page, host_bundle, workbench_html, [PROJECT_A])
        app.get_by_role("button", name="打开项目 Project A", exact=True).click()
        deliver(page, "open", opened(PROJECT_B))
        expect(app.locator("#app")).to_contain_text("返回的项目与当前选择不匹配")
        expect(app.get_by_role("button", name="打开项目 Project A", exact=True)).to_be_enabled()
        app.get_by_role("button", name="刷新项目列表").click()
        deliver(page, "list", {"error": {"code": "INSUFFICIENT_SCOPE", "message": "fixture denied"}})
        expect(app.locator("#app")).to_contain_text("刷新失败，保留上次列表")
        expect(app.get_by_role("button", name="打开项目 Project A", exact=True)).to_be_visible()
        app.get_by_role("button", name="打开项目 Project A", exact=True).click()
        operation_id = "c" * 32
        deliver(page, "open", {"pending": True, "operation_id": operation_id}, index=1)
        wait_pending(page, "wait")
        assert page.evaluate("window.workbenchCalls.at(-1).name") == "task_query"
        deliver(page, "wait", {"operations": [{
            "id": operation_id, "operation_id": operation_id, "tool": "open_workspace",
            "state": "succeeded", "pending": False, "result": {"ok": True, "data": opened(PROJECT_A)},
        }]})
        expect(app.get_by_role("heading", name="Project A", exact=True)).to_be_visible()
        app.get_by_label("自动刷新任务状态").uncheck()
        expect_clean(page, errors)
        browser.close()


@pytest.mark.parametrize("lifecycle", ["cancel", "teardown"])
def test_workbench_lifecycle_stops_late_selection(host_bundle, workbench_html, lifecycle):
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        app = mount(page, host_bundle, workbench_html, [PROJECT_A])
        app.get_by_role("button", name="打开项目 Project A", exact=True).click()
        wait_pending(page, "open")
        if lifecycle == "cancel":
            page.evaluate("()=>window.codepierBridge.sendToolCancelled({reason:'fixture cancel'})")
            expect(app.locator("#app")).to_contain_text("卡片显示已结束")
        else:
            page.evaluate("()=>window.codepierBridge.teardownResource({})")
        deliver(page, "open", opened(PROJECT_A))
        # Cross-frame notifications settle before inspecting the retained/dismissed UI.
        page.evaluate("()=>new Promise(resolve=>setTimeout(resolve,100))")
        assert app.get_by_role("heading", name="Project A", exact=True).count() == 0
        assert not page.evaluate("window.workbenchCalls.some(p=>p.arguments.operation==='dashboard')")
        expect_clean(page, errors)
        browser.close()
