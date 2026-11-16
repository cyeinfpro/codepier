"""Slow reads keep layout; cached estimates never cross an authorization boundary."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.context_usage_fixtures import synthetic_dashboard
from tests.test_ui_unification import _login, _navigate


@pytest.mark.parametrize("width", [390, 1440])
def test_loading_geometry_and_background_refresh(stack, chat_browser_pool, width):
    context = chat_browser_pool("chromium").new_context(viewport={"width": width, "height": 1600})
    page = context.new_page()
    pending = []
    try:

        def paths(route):
            response = route.fetch()
            data = response.json()
            for i, item in enumerate(data.get("projects", [])):
                item["root"] = "/workspace/demo/" + str(i)
            route.fulfill(response=response, json=data)

        page.route("**/api/overview*", paths)
        page.route("**/api/token-usage*", lambda route: pending.append(route))
        _login(page, stack)
        _navigate(page, "overview")
        card = page.locator("[data-token-dashboard]")
        expect(card).to_have_attribute("aria-busy", "true")
        initial = page.locator(".overview-summary").bounding_box()
        print(
            "INITIAL CHILDREN",
            width,
            card.locator(":scope > *").evaluate_all(
                "els=>els.map(e=>[e.className,e.getBoundingClientRect().height,getComputedStyle(e).marginTop,getComputedStyle(e).marginBottom])"
            ),
        )
        folder = Path(".work/usage-loading-stable")
        folder.mkdir(parents=True, exist_ok=True)
        page.locator(".overview-summary").screenshot(
            path=str(folder / f"{width}-initial.jpg"), type="jpeg", quality=35
        )
        assert pending
        for route in pending:
            route.fulfill(json=synthetic_dashboard())
        pending.clear()
        expect(card.locator(".is-total strong")).to_have_text("206.49K")
        loaded = page.locator(".overview-summary").bounding_box()
        print("GEOMETRY", width, initial["height"], loaded["height"])
        print(
            "LOADED CHILDREN",
            width,
            card.locator(":scope > *").evaluate_all(
                "els=>els.map(e=>[e.className,e.getBoundingClientRect().height,getComputedStyle(e).marginTop,getComputedStyle(e).marginBottom])"
            ),
        )
        assert abs(initial["height"] - loaded["height"]) <= 1
        page.locator(".overview-summary").screenshot(
            path=str(folder / f"{width}-loaded-closed.jpg"), type="jpeg", quality=35
        )
        card.locator("[data-token-filters]>summary").click()
        field = card.locator("[data-token-filter=period]")
        field.select_option("7d")
        page.wait_for_timeout(30)
        data = synthetic_dashboard()
        data["period"]["key"] = "7d"
        pending.pop().fulfill(json=data)
        expect(card.locator(".token-dashboard-status")).to_have_text("")
        expect(field).to_have_value("7d")
        field.focus()
        before = card.bounding_box()
        page.evaluate("void renderPage(false)")
        page.wait_for_timeout(100)
        expect(card.locator(".is-total strong")).to_have_text("206.49K")
        expect(field).to_be_focused()
        expect(card.locator("[data-token-filters]")).to_have_attribute("open", "")
        assert abs(before["height"] - card.bounding_box()["height"]) <= 1
        pending.pop().fulfill(json=synthetic_dashboard())
        page.wait_for_timeout(50)
        assert len(pending) == 1
        pending.pop().fulfill(json=data)
        expect(field).to_be_focused()
        expect(field).to_have_value("7d")
        assert abs(before["height"] - card.bounding_box()["height"]) <= 1
        page.locator(".overview-summary").screenshot(
            path=str(folder / f"{width}-loaded.jpg"), type="jpeg", quality=35
        )
    finally:
        context.close()


@pytest.mark.parametrize(
    "code,cleared",
    [
        ("FORBIDDEN", True),
        ("STATISTICS_AUTHORITY_CHANGED", True),
        ("STATISTICS_CHANGED", False),
        ("UNAVAILABLE", False),
    ],
)
@pytest.mark.parametrize("background", [False, True])
def test_refresh_error_authority_boundary(stack, chat_browser_pool, code, cleared, background):
    context = chat_browser_pool("chromium").new_context(viewport={"width": 390, "height": 1600})
    page = context.new_page()
    try:

        def respond(route):
            from urllib.parse import urlparse, parse_qs

            data = synthetic_dashboard()
            data["period"]["key"] = parse_qs(urlparse(route.request.url).query).get(
                "period", ["today"]
            )[0]
            route.fulfill(json=data)

        page.route("**/api/token-usage*", respond)
        _login(page, stack)
        _navigate(page, "overview")
        card = page.locator("[data-token-dashboard]")
        expect(card.locator(".is-total strong")).to_have_text("206.49K")
        page.evaluate(
            """({data,background}) => {
          const root=document.querySelector('#page');
          window.testReject=null;
          window.testFetcher=()=>new Promise((resolve,reject)=>{window.testReject=reject});
          if(background) void CodePierTokenUsage.loadDashboard(root,testFetcher,true,()=>true);
          else CodePierTokenUsage.bindDashboard(root,testFetcher,data);
        }""",
            {"data": synthetic_dashboard(), "background": background},
        )
        if not background:
            card.locator("[data-token-filters]>summary").click()
            card.locator("[data-token-filter=period]").select_option("7d")
        page.wait_for_function("() => window.testReject !== null")
        before = card.bounding_box()
        page.evaluate(
            """code => testReject(Object.assign(new Error('test'),{
          code,status:code==='FORBIDDEN'?403:code==='STATISTICS_AUTHORITY_CHANGED'?409:503
        }))""",
            code,
        )
        expect(card.locator("[role=status]")).to_contain_text("清除" if cleared else "上一次结果")
        if cleared:
            expect(card.locator(".is-total strong")).not_to_have_text("206.49K")
            assert page.evaluate("CodePierTokenUsage.cachedDashboard() === null")
            assert page.evaluate(
                "document.querySelector('[data-token-dashboard]').onclick === null"
            )
        else:
            expect(card.locator(".is-total strong")).to_have_text("206.49K")
            assert abs(card.bounding_box()["height"] - before["height"]) <= 1
        if background:
            folder = Path(".work/usage-loading-stable")
            page.locator(".overview-summary").screenshot(
                path=str(folder / f"390-error-{code}.jpg"), type="jpeg", quality=35
            )
    finally:
        context.close()


@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("second", ["error", "missing_authority"])
def test_authority_changes_clear_before_second_read(stack, chat_browser_pool, background, second):
    context = chat_browser_pool("chromium").new_context(viewport={"width": 390, "height": 1600})
    page = context.new_page()
    try:

        def respond(route):
            from urllib.parse import urlparse, parse_qs

            data = synthetic_dashboard()
            data["period"]["key"] = parse_qs(urlparse(route.request.url).query).get(
                "period", ["today"]
            )[0]
            route.fulfill(json=data)

        page.route("**/api/token-usage*", respond)
        _login(page, stack)
        _navigate(page, "overview")
        card = page.locator("[data-token-dashboard]")
        expect(card.locator(".is-total strong")).to_have_text("206.49K")
        card.locator("[data-token-filters]>summary").click()
        card.locator("[data-token-filter=period]").select_option("7d")
        expect(card.locator("[data-token-filter=period]")).to_have_value("7d")
        expect(card.locator(".token-dashboard-status")).to_have_text("")
        page.evaluate(
            """({data,background})=>{
          window.reads=[];
          window.testFetcher=()=>new Promise((resolve,reject)=>reads.push({resolve,reject}));
          const root=document.querySelector('#page');
          if(background) void CodePierTokenUsage.loadDashboard(root,testFetcher,true,()=>true);
          else CodePierTokenUsage.bindDashboard(root,testFetcher,data);
        }""",
            {"data": synthetic_dashboard(), "background": background},
        )
        if background:
            page.wait_for_function("() => reads.length===1")
            page.evaluate("data=>reads.shift().resolve(data)", synthetic_dashboard())
        else:
            card.locator("[data-token-filter=period]").select_option("30d")
        page.wait_for_function("() => reads.length===1")
        changed = synthetic_dashboard()
        changed["authority_key"] = "different-authority"
        page.evaluate("data=>reads.shift().resolve(data)", changed)
        page.wait_for_function("() => reads.length===1")
        expect(card.locator(".is-total strong")).not_to_have_text("206.49K")
        assert page.evaluate("CodePierTokenUsage.cachedDashboard() === null")
        if second == "error":
            page.evaluate(
                "reads.shift().reject(Object.assign(new Error('temporary'),{status:503}))"
            )
        else:
            changed.pop("authority_key")
            page.evaluate("data=>reads.shift().resolve(data)", changed)
        expect(card.locator("[role=status]")).to_contain_text(
            "清除" if second == "missing_authority" or not background else "暂不可用"
        )
        expect(card.locator(".is-total strong")).not_to_have_text("206.49K")
        assert page.evaluate("CodePierTokenUsage.cachedDashboard() === null")
    finally:
        context.close()


@pytest.mark.parametrize("finish", ["resolve", "reject"])
@pytest.mark.parametrize("return_to_a", [False, True])
def test_late_owner_result_cannot_restore_prior_statistics(
    stack, chat_browser_pool, finish, return_to_a
):
    context = chat_browser_pool("chromium").new_context(viewport={"width": 390, "height": 1600})
    page = context.new_page()
    try:
        page.route("**/api/token-usage*", lambda route: route.fulfill(json=synthetic_dashboard()))
        _login(page, stack)
        _navigate(page, "overview")
        card = page.locator("[data-token-dashboard]")
        expect(card.locator(".is-total strong")).to_have_text("206.49K")
        page.evaluate("""() => {
            window.oldRead={};
            void CodePierTokenUsage.loadDashboard(document.querySelector('#page'),
              ()=>new Promise((resolve,reject)=>Object.assign(oldRead,{resolve,reject})),true,()=>true);
        }""")
        page.evaluate(
            """returnToA => {
            const previous=S.session;
            S.session={...previous,user_id:'other-user'};
            clearSpaceSnapshots();
            if(returnToA) S.session={...previous};
            if(document.querySelector('[data-token-dashboard]').textContent.includes('206.49K')) throw new Error('identity clear leaked prior usage');
        }""",
            return_to_a,
        )
        if finish == "resolve":
            page.evaluate("data=>oldRead.resolve(data)", synthetic_dashboard())
        else:
            page.evaluate("oldRead.reject(Object.assign(new Error('old'),{status:403}))")
        page.wait_for_timeout(50)
        expect(card.locator(".is-total strong")).not_to_have_text("206.49K")
        expect(card.locator("[role=status]")).to_have_text("访问范围已变化，正在读取当前统计…")
        assert page.evaluate("CodePierTokenUsage.cachedDashboard() === null")
    finally:
        context.close()


@pytest.mark.parametrize("width", [320, 390, 1440])
@pytest.mark.parametrize("variant", ["missing", "zero", "long", "error"])
def test_first_read_variants_keep_downstream_layout(stack, chat_browser_pool, width, variant):
    from tests.test_token_dashboard_browser import priced_dashboard

    context = chat_browser_pool("chromium").new_context(viewport={"width": width, "height": 1600})
    page = context.new_page()
    pending = []
    try:
        page.route("**/api/token-usage*", lambda route: pending.append(route))
        _login(page, stack)
        _navigate(page, "overview")
        card = page.locator("[data-token-dashboard]")
        expect(card).to_have_attribute("aria-busy", "true")
        before = page.locator(".overview-summary").bounding_box()
        projects = page.locator(".workspace-recent-projects")
        before_y = projects.bounding_box()["y"]
        data = priced_dashboard()
        n = {"missing": None, "zero": 0, "long": 9007199254740991, "error": None}[variant]
        for direction in ("input", "output"):
            for field in ("estimated_tokens", "low", "high"):
                data["summary"][direction][field] = (
                    n if direction == "input" else (None if n is None else 0)
                )
        data["summary"]["reference_cost"]["amount_nano_usd"] = n
        if variant == "error":
            page.route(
                "**/api/token-usage*",
                lambda route: route.fulfill(
                    status=503, json={"error": {"code": "UNAVAILABLE", "message": "temporary"}}
                ),
            )
        for route in pending:
            if variant == "error":
                route.fulfill(
                    status=503, json={"error": {"code": "UNAVAILABLE", "message": "temporary"}}
                )
            else:
                route.fulfill(json=data)
        pending.clear()
        if variant == "error":
            expect(card.locator("[role=status]")).to_contain_text("暂不可用", timeout=15000)
        else:
            expect(card.locator(".is-total strong")).to_have_text(
                {"missing": "未记录", "zero": "0", "long": "9007199.25B"}[variant]
            )
        after = page.locator(".overview-summary").bounding_box()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth+1")
        if variant != "long":
            assert abs(before["height"] - after["height"]) <= 1, (before, after)
            assert abs(before_y - projects.bounding_box()["y"]) <= 1
        else:
            assert after["height"] >= before["height"] - 1
            for number in card.locator(".token-dashboard-metric strong").all():
                assert number.evaluate("e=>e.scrollWidth<=e.clientWidth+1")
        folder = Path(".work/usage-loading-stable")
        folder.mkdir(parents=True, exist_ok=True)
        page.locator(".overview-summary").screenshot(
            path=str(folder / f"{width}-first-{variant}.jpg"), type="jpeg", quality=35
        )
    finally:
        context.close()
