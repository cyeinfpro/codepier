"""Project-map UI uses only explicit read operations and isolated Agent replies."""

import json
from pathlib import Path

import pytest
from playwright.sync_api import expect
from tests.test_ui_unification import _login, _navigate


def map_data(revision="r1", offset=0, next_offset=None):
    return {
        "state": "ready",
        "revision": revision,
        "generated_at": 1700000000,
        "offset": offset,
        "next_offset": next_offset,
        "total_matches": 3,
        "truncated": next_offset is not None,
        "refresh_required": True,
        "entries": [
            {
                "path": "src/" + "long-segment/" * 15 + "main.py",
                "sha256": "a" * 64,
                "state": "current",
                "summary": "Application entry point <img src=x onerror=alert(1)>",
                "symbols": ["main"],
                "imports": ["os"],
                "related_paths": ["README.md"],
            },
            {
                "path": "src/old.py",
                "sha256": "b" * 64,
                "state": "stale",
                "summary": "MUST NOT SHOW OLD SUMMARY",
            },
            {"path": "src/removed.py", "sha256": "c" * 64, "state": "missing"},
        ],
    }


def prepare(page, stack):
    def overview(route):
        response = route.fetch()
        data = response.json()
        for i, p in enumerate(data.get("projects", [])):
            p["root"] = "/workspace/demo/project-" + str(i + 1)
        route.fulfill(response=response, json=data)

    page.route("**/api/overview*", overview)
    page.route("**/api/projects", overview)
    page.route(
        "**/api/call-log?*",
        lambda route: route.fulfill(
            json={
                "operations": [
                    {
                        "id": "map-read-fixture",
                        "tool": "project_knowledge_query",
                        "state": "succeeded",
                        "created": 1700000000,
                        "trigger_source": "request",
                    },
                    {
                        "id": "map-build-fixture",
                        "tool": "project_knowledge_build",
                        "state": "succeeded",
                        "created": 1700000001,
                        "trigger_source": "system_maintenance",
                    },
                ],
                "next_cursor": None,
                "total": 2,
            }
        ),
    )
    _login(page, stack)
    _navigate(page, "project-map")
    expect(page.locator("#map-project")).to_be_visible()


@pytest.mark.parametrize(
    "width,theme",
    [(390, "light"), (390, "dark"), (901, "light"), (901, "dark"), (1440, "light"), (1440, "dark")],
)
def test_project_map_reads_existing_map_and_separates_audit(stack, chat_browser_pool, width, theme):
    context = chat_browser_pool("chromium").new_context(viewport={"width": width, "height": 1800})
    page = context.new_page()
    calls = []
    try:

        def invoke(route):
            body = route.request.post_data_json
            calls.append(body)
            assert body["tool"] == "project_knowledge_query"
            route.fulfill(json=map_data(next_offset=3))

        page.route("**/api/tools/call", invoke)
        prepare(page, stack)
        page.evaluate("theme=>CodePierAppearance.setPreference(theme)", theme)
        assert not calls  # Opening/navigation is never an implicit map query/build.
        expect(page.locator(".map-audit")).to_contain_text("系统维护")
        expect(page.locator(".map-audit")).to_contain_text("请求触发")
        page.locator('[data-map="read"]').click()
        expect(page.locator(".map-results")).to_contain_text("Application entry point")
        expect(page.locator(".map-results")).not_to_contain_text("MUST NOT SHOW OLD SUMMARY")
        expect(page.locator(".map-results")).to_contain_text("文件已变化")
        expect(page.locator(".map-results")).to_contain_text("文件缺失")
        expect(page.locator(".map-results")).to_contain_text("不代表整个仓库")
        assert calls[0]["arguments"]["offset"] == 0
        assert calls[0]["arguments"]["max_chars"] == 16000
        expect(page.locator(".map-results img")).to_have_count(0)
        page.locator(".project-map-entries details > summary").first.click()
        expect(page.locator(".map-entry-facts").first).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth+1")
        folder = Path(".work/project-map-ui")
        folder.mkdir(parents=True, exist_ok=True)
        page.locator("#page").screenshot(
            path=str(folder / f"{theme}-{width}.jpg"), type="jpeg", quality=35
        )
        page.locator('[data-map="next"]').click()
        expect(page.locator(".map-status")).to_have_text("已读取现有地图。")
        assert calls[-1]["arguments"]["offset"] == 3
    finally:
        context.close()


def test_project_map_pending_missing_revision_and_failure(stack, chat_browser_pool):
    context = chat_browser_pool("chromium").new_context(viewport={"width": 390, "height": 1000})
    page = context.new_page()
    calls = []
    mode = {"value": "pending"}
    try:

        def invoke(route):
            body = route.request.post_data_json
            calls.append(body)
            if body["tool"] == "operations_wait":
                route.fulfill(
                    json={
                        "state": "succeeded",
                        "pending": False,
                        "result": {"ok": True, "data": map_data(next_offset=3)},
                    }
                )
            elif mode["value"] == "pending":
                route.fulfill(json={"pending": True, "operation_id": "original-map-operation"})
            elif mode["value"] == "revision":
                route.fulfill(json=map_data(revision="changed", offset=3))
            elif mode["value"] == "missing":
                route.fulfill(
                    json={
                        "state": "missing",
                        "revision": "new",
                        "entries": [],
                        "offset": 0,
                        "next_offset": None,
                    }
                )
            else:
                route.fulfill(status=403, json={"detail": "Project access denied"})

        page.route("**/api/tools/call", invoke)
        prepare(page, stack)
        page.locator('[data-map="read"]').click()
        expect(page.locator(".map-results")).to_contain_text("Application entry point")
        assert [c["tool"] for c in calls] == ["project_knowledge_query", "operations_wait"]
        assert calls[1]["arguments"]["operation_id"] == "original-map-operation"
        mode["value"] = "revision"
        page.locator('[data-map="next"]').click()
        expect(page.locator(".map-status")).to_contain_text("地图版本已变化")
        expect(page.locator(".project-map-entries")).to_have_count(0)
        mode["value"] = "missing"
        page.locator('[data-map="read"]').click()
        expect(page.locator(".map-empty")).to_contain_text("该项目暂无地图")
        folder = Path(".work/project-map-ui")
        folder.mkdir(parents=True, exist_ok=True)
        page.locator("#page").screenshot(
            path=str(folder / "mobile-missing.jpg"), type="jpeg", quality=35
        )
        mode["value"] = "error"
        page.locator('[data-map="read"]').click()
        expect(page.locator(".map-status")).to_contain_text("读取未完成")
        page.locator("#page").screenshot(
            path=str(folder / "mobile-error.jpg"), type="jpeg", quality=35
        )
        assert all(c["tool"] != "project_knowledge_build" for c in calls)
    finally:
        context.close()


@pytest.mark.parametrize("change", ["stop", "project", "leave", "identity"])
def test_project_map_late_response_never_crosses_context(stack, chat_browser_pool, change):
    context = chat_browser_pool("chromium").new_context(viewport={"width": 390, "height": 1000})
    page = context.new_page()
    pending = []
    try:
        page.route("**/api/tools/call", lambda route: pending.append(route))
        prepare(page, stack)
        page.locator('[data-map="read"]').click()
        expect(page.locator('[data-map="stop"]')).to_be_visible()
        if change == "stop":
            page.locator('[data-map="stop"]').click()
        elif change == "project":
            options = page.locator("#map-project option").evaluate_all(
                "nodes=>nodes.map(n=>n.value)"
            )
            assert len(options) > 1
            page.locator("#map-project").select_option(options[1])
        elif change == "leave":
            _navigate(page, "projects")
        else:
            page.evaluate(
                "()=>{S.session={...S.session,username:'changed-user'};S.space_id='changed-space';CodePierProjectMap.detach()}"
            )
        assert len(pending) == 1
        pending[0].fulfill(json=map_data())
        page.wait_for_timeout(100)
        expect(page.locator("#page")).not_to_contain_text("Application entry point")
        if change == "stop":
            expect(page.locator(".map-status")).to_contain_text("原操作可能继续")
        if change == "leave":
            _navigate(page, "project-map")
            expect(page.locator(".map-status")).to_contain_text("本页已停止等待")
            expect(page.locator(".map-status")).not_to_contain_text("正在")
    finally:
        context.close()


def test_project_map_audit_pagination_is_transactional(stack, chat_browser_pool):
    context = chat_browser_pool("chromium").new_context(viewport={"width": 390, "height": 1000})
    page = context.new_page()
    pending = []
    urls = []
    try:
        prepare(page, stack)
        page.unroute("**/api/call-log?*")

        def audit(route):
            urls.append(route.request.url)
            pending.append(route)

        page.route("**/api/call-log?*", audit)
        page.locator('[data-map="audit-refresh"]').click()
        page.wait_for_timeout(30)
        pending.pop(0).fulfill(json={"operations": [], "total": 50, "next_cursor": "cursor-page2"})
        expect(page.locator('[data-map="audit-next"]')).to_be_enabled()
        page.locator('[data-map="audit-next"]').evaluate("b=>{b.click();b.click()}")
        expect(page.locator('[data-map="audit-next"]')).to_be_disabled()
        expect(page.locator('[data-map="audit-prev"]')).to_be_disabled()
        page.wait_for_timeout(30)
        assert len(pending) == 1
        pending.pop(0).fulfill(status=400, json={"detail": "test cursor read failure"})
        expect(page.locator(".map-audit [role=alert]")).to_contain_text("页码未改变")
        expect(page.locator('[data-map="audit-prev"]')).to_be_disabled()
        page.locator('[data-map="audit-next"]').click()
        page.wait_for_timeout(30)
        pending.pop(0).fulfill(json={"operations": [], "total": 50, "next_cursor": "cursor-page3"})
        expect(page.locator('[data-map="audit-prev"]')).to_be_enabled()
        page.locator('[data-map="audit-prev"]').click()
        page.wait_for_timeout(30)
        from urllib.parse import urlparse, parse_qs

        assert parse_qs(urlparse(urls[-1]).query).get("cursor", [""]) == [""]
        pending.pop(0).fulfill(json={"operations": [], "total": 50, "next_cursor": "cursor-page2"})
        expect(page.locator('[data-map="audit-prev"]')).to_be_disabled()
        assert all("category=project_map" in u for u in urls)
    finally:
        context.close()


def test_project_map_real_agent_read_does_not_build(stack, chat_browser_pool):
    context = chat_browser_pool("chromium").new_context(viewport={"width": 390, "height": 1000})
    page = context.new_page()
    posts = []
    try:
        page.on(
            "request",
            lambda request: (
                posts.append(request.post_data_json)
                if request.url.endswith("/api/tools/call")
                else None
            ),
        )
        prepare(page, stack)
        page.locator('[data-map="read"]').click()
        expect(page.locator(".map-empty")).to_contain_text("该项目暂无地图", timeout=30000)
        assert any(p["tool"] == "project_knowledge_query" for p in posts)
        assert all(p["tool"] != "project_knowledge_build" for p in posts)
    finally:
        context.close()


def test_project_map_workspace_identity_and_unavailable_selection(stack, chat_browser_pool):
    context = chat_browser_pool("chromium").new_context(viewport={"width": 390, "height": 1600})
    page = context.new_page()
    calls = []
    pending = []
    mode = {"query": "hold", "list": "ready"}
    try:

        def invoke(route):
            body = route.request.post_data_json
            calls.append(body)
            if body["tool"] == "worktrees_list":
                rows = (
                    []
                    if mode["list"] == "removed"
                    else [
                        {
                            "workspace_id": "ws-one",
                            "label": "First workspace",
                            "state": "ready",
                            "path": "/workspace/demo/one",
                        },
                        {
                            "workspace_id": "ws-two",
                            "label": "Second workspace",
                            "state": "ready",
                            "path": "/workspace/demo/two",
                        },
                        {
                            "workspace_id": "ws-broken",
                            "label": "Broken",
                            "state": "needs_review",
                            "path": "/workspace/demo/broken",
                        },
                    ]
                )
                route.fulfill(json={"workspaces": rows, "record_errors": 1, "limit": 100})
            elif mode["query"] == "hold":
                pending.append(route)
            elif mode["query"] == "denied":
                route.fulfill(status=403, json={"detail": "Workspace access denied"})
            else:
                route.fulfill(
                    json={
                        "state": "missing",
                        "revision": "new",
                        "entries": [],
                        "offset": 0,
                        "next_offset": None,
                    }
                )

        page.route("**/api/tools/call", invoke)
        prepare(page, stack)
        assert not calls
        page.locator('[data-map="workspaces"]').click()
        expect(page.locator("#map-workspace option")).to_have_count(3)
        expect(page.locator(".map-workspace-status")).to_contain_text("列表可能不完整")
        expect(page.locator(".map-audit")).to_contain_text("当前项目全部工作目录")
        page.locator("#map-workspace").select_option("ws-one")
        page.locator('[data-map="read"]').click()
        expect(page.locator('[data-map="stop"]')).to_be_visible()
        assert calls[-1]["arguments"]["workspace_id"] == "ws-one"
        page.locator("#map-workspace").select_option("ws-two")
        pending.pop().fulfill(json=map_data())
        page.wait_for_timeout(50)
        expect(page.locator(".project-map-entries")).to_have_count(0)
        mode["query"] = "denied"
        page.locator('[data-map="read"]').click()
        expect(page.locator(".map-status")).to_contain_text("读取未完成")
        expect(page.locator("#map-workspace")).to_have_value("ws-two")
        mode["query"] = "missing"
        page.locator('[data-map="read"]').click()
        expect(page.locator(".map-empty")).to_be_visible()
        assert calls[-1]["arguments"]["workspace_id"] == "ws-two"
        mode["list"] = "removed"
        page.locator('[data-map="workspaces"]').click()
        expect(page.locator("#map-workspace")).to_have_value("ws-two")
        expect(page.locator('#map-workspace option[value="ws-two"]')).to_have_attribute(
            "disabled", ""
        )
        assert page.locator('#map-workspace option[value="ws-two"]').evaluate("e=>e.disabled")
        expect(page.locator('[data-map="read"]')).to_be_disabled()
        expect(page.locator(".map-status")).to_contain_text("保留原选择")
        page.locator("#map-workspace").select_option("")
        expect(page.locator('[data-map="read"]')).to_be_enabled()
    finally:
        context.close()


def test_project_map_late_workspace_list_does_not_cross_project(stack, chat_browser_pool):
    context = chat_browser_pool("chromium").new_context(viewport={"width": 390, "height": 1000})
    page = context.new_page()
    pending = []
    try:
        page.route("**/api/tools/call", lambda route: pending.append(route))
        prepare(page, stack)
        page.locator('[data-map="workspaces"]').click()
        expect(page.locator(".map-workspace-status")).to_contain_text("正在读取")
        options = page.locator("#map-project option").evaluate_all("nodes=>nodes.map(n=>n.value)")
        page.locator("#map-project").select_option(options[1])
        assert len(pending) == 1
        pending[0].fulfill(
            json={
                "workspaces": [
                    {"workspace_id": "old-target", "label": "Old target", "state": "ready"}
                ],
                "record_errors": 0,
                "limit": 100,
            }
        )
        page.wait_for_timeout(50)
        expect(page.locator("#map-workspace option")).to_have_count(1)
        expect(page.locator(".map-workspace-status")).to_contain_text("尚未读取")
    finally:
        context.close()


def test_project_map_workspace_pending_can_stop_without_cancelling_operation(
    stack, chat_browser_pool
):
    context = chat_browser_pool("chromium").new_context(viewport={"width": 390, "height": 1000})
    page = context.new_page()
    pending = []
    calls = []
    try:

        def invoke(route):
            body = route.request.post_data_json
            calls.append(body)
            if body["tool"] == "worktrees_list":
                route.fulfill(
                    json={"pending": True, "operation_id": "directory-original-operation"}
                )
            else:
                pending.append(route)

        page.route("**/api/tools/call", invoke)
        prepare(page, stack)
        page.locator('[data-map="workspaces"]').click()
        expect(page.locator(".map-workspace-status")).to_contain_text(
            "directory-original-operation"
        )
        page.locator('[data-map="stop"]').click()
        expect(page.locator(".map-workspace-status")).to_contain_text("已停止目录列表等待")
        expect(page.locator(".map-workspace-status")).to_contain_text(
            "directory-original-operation"
        )
        assert all(c["tool"] != "operations_cancel" for c in calls)
        for route in pending:
            route.fulfill(
                json={
                    "state": "succeeded",
                    "pending": False,
                    "result": {
                        "ok": True,
                        "data": {"workspaces": [{"workspace_id": "late", "state": "ready"}]},
                    },
                }
            )
        expect(page.locator("#map-workspace option")).to_have_count(1)
    finally:
        context.close()
