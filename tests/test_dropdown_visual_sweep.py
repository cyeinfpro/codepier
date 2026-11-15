"""Read-only dropdown sweep on isolated test fixtures; never submit model/task forms.
Screenshots of a native select after click cannot certify OS popup rendering.
The JSON inventory explicitly retains hidden/disabled/unreachable controls.
"""
import json
import os
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from tests.browser_launch import launch_browser
from tests.test_ui_unification import _login, _navigate, _prepare_native_fixture, _set_scheme

@pytest.mark.parametrize("width,height,scheme", [(1440,1000,"light"),(1440,1000,"dark"),(390,844,"light"),(320,568,"dark")])
def test_dropdown_visual_sweep(stack, width, height, scheme):
    _prepare_native_fixture(stack)
    output = Path(os.environ.get("CODEPIER_DROPDOWN_DIR", ".work/dropdown-sweep-"+uuid.uuid4().hex))
    output = output / f"{scheme}-{width}x{height}"
    output.mkdir(parents=True, exist_ok=False)
    report = {"cases": [], "gaps": [], "errors": [], "scope":"isolated fixture, no form submits; native OS popup visual not certified"}
    with sync_playwright() as pw:
        browser = launch_browser(pw.chromium)
        page = browser.new_page(viewport={"width":width,"height":height},reduced_motion="reduce",color_scheme=scheme)
        page.on("pageerror",lambda e:report["errors"].append(str(e)))
        page.set_default_timeout(3000)
        page.set_default_navigation_timeout(5000)
        expect.set_options(timeout=5000)
        stage = os.environ.get("CODEPIER_SWEEP_STAGE", "filters")
        assert stage in {"filters", "dialogs", "native"}
        serial = 0
        def persist():
            (output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
        def shot(label):
            nonlocal serial
            serial += 1
            name=f"{serial:03d}-{label}.png"
            page.screenshot(path=str(output/name),animations="disabled",timeout=5000)
            persist()
            return name
        def attempt(label, fn):
            if ((label in {"overview","workflows","audit","artifacts","workbench","audit-events"} and stage != "filters")
                or (label in {"workflow-create","artifact-register","local-skills","search-dialog","runtime-shell"} and stage != "dialogs")
                or (label == "native" and stage != "native")):
                return
            print("STAGE START", label, flush=True)
            try:
                fn()
            except Exception as e:
                report["gaps"].append({"case":label,"reason":str(e)[:900],"screenshot":shot("gap")})
            finally:
                persist()
                print("STAGE END", label, "records", len(report["cases"]), "gaps", len(report["gaps"]), flush=True)
        def scan(label, scope="#page"):
            root=page.locator(scope)
            expect(root).to_be_visible()
            # Open disclosures one by one; no buttons within menus are activated.
            summaries=root.locator("details > summary")
            for i in range(summaries.count()):
                summary=summaries.nth(i)
                if not summary.is_visible():continue
                parent=summary.locator("..")
                was_open=parent.get_attribute("open") is not None
                if not was_open:
                    summary.scroll_into_view_if_needed()
                    summary.click()
                report["cases"].append({"case":label,"disclosure":summary.inner_text()[:100],"screenshot":shot(label+"-disclosure")})
                # Keep ordinary disclosure open so contained selects can be measured.
                if parent.evaluate("n=>n.matches('.ui-menu,.menu,.call-more-filters')") and not was_open:
                    summary.click()
            selects=root.locator("select")
            for i in range(selects.count()):
                control=selects.nth(i)
                record={"case":label,"id":control.get_attribute("id"),"name":control.get_attribute("name"),"label":control.get_attribute("aria-label")}
                if not control.is_visible():
                    record["hidden"]=True
                    report["cases"].append(record)
                    continue
                control.scroll_into_view_if_needed()
                record.update(control.evaluate("""n=>{
                  const r=n.getBoundingClientRect(),s=getComputedStyle(n);
                  const labels=[...(n.labels||[])].map(l=>l.innerText);
                  return {labels,disabled:n.disabled,value:n.value,options:[...n.options].map(o=>o.text),
                    rect:{x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom},
                    css:{font:s.fontSize,padding:s.padding,borderRadius:s.borderRadius,appearance:s.appearance},
                    horizontalOverflow:r.left < -1 || r.right > innerWidth+1};
                }"""))
                record["screenshot"]=shot(label+"-select")
                if not control.is_disabled():
                    before=control.input_value()
                    control.click(timeout=3000)
                    record["afterClickScreenshot"]=shot(label+"-opened")
                    # Escape can bubble to the host modal in headless native selects.
                    # Tab dismisses/focuses away without dismissing the containing dialog.
                    page.keyboard.press("Tab")
                    record["valueUnchanged"]=control.input_value()==before
                report["cases"].append(record)
            # Scroll every actually scrollable container, not only window.
            for i in range(root.locator("*").count()):
                n=root.locator("*").nth(i)
                if n.evaluate("n=>n.scrollHeight>n.clientHeight+2 && /auto|scroll/.test(getComputedStyle(n).overflowY)"):
                    n.evaluate("n=>n.scrollTop=n.scrollHeight")
                    report["cases"].append({"case":label,"scrollContainer":n.get_attribute("id") or n.get_attribute("class"),"screenshot":shot(label+"-scroll-end")})
            root.evaluate("""n=>{for(let p=n;p;p=p.parentElement){if(p.scrollHeight>p.clientHeight)p.scrollTop=p.scrollHeight;}window.scrollTo(0,document.documentElement.scrollHeight);}""")
            report["cases"].append({"case":label,"scroll":"page-end","screenshot":shot(label+"-bottom")})
        def close_dialog():
            page.keyboard.press("Escape")
            for selector in ["[data-action='close-modal']", ".modal-close"]:
                b=page.locator(selector+":visible")
                if b.count():b.first.click();break
        def dialog(label, selector, scope=".modal"):
            page.locator(selector).first.click()
            scan(label,scope)
            close_dialog()
        try:
            _login(page,stack)
            _set_scheme(page,scheme)
            for route in ("overview","workflows","audit","artifacts","workbench"):
                def inspect(route=route):
                    _navigate(page,route)
                    if route=="workbench":
                        expect(page.locator(".file-node[data-path]").first).to_be_visible(timeout=15000)
                    scan(route)
                attempt(route,inspect)
            attempt("workflow-create",lambda:(_navigate(page,"workflows"),dialog("workflow-create","[data-wf-action='create']")))
            def audit_events():
                _navigate(page,"audit")
                with page.expect_response(lambda r: "/api/" in r.url and "audit" in r.url and r.request.method == "GET"):
                    page.locator("[data-action='audit-mode'][data-mode='events']").click()
                page.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
                scan("audit-events")
            attempt("audit-events",audit_events)
            attempt("artifact-register",lambda:(_navigate(page,"artifacts"),dialog("artifact-register","[data-insight='register']")))
            attempt("local-skills",lambda:(_navigate(page,"workbench"),page.evaluate("localSkills()"),scan("local-skills",".modal"),close_dialog()))
            attempt("search-dialog",lambda:(_navigate(page,"workbench"),page.locator("summary").filter(has_text="检索与 Git").click(),dialog("search-dialog","[data-insight='search']")))
            attempt("runtime-shell",lambda:(_navigate(page,"projects"),page.evaluate("CodePierProjectRuntime.open(S.projects[0].id)"),scan("runtime-shell",".modal"),close_dialog()))
            # Chat settings and history filters are explicitly opened via their UI.
            def native():
                _navigate(page,"native")
                page.locator("#chat-options-toggle").click()
                expect(page.locator("#chat-options")).to_be_visible()
                scan("native-settings","#chat-options")
                attempt("native-model-popup",lambda:(page.locator("#chat-model-picker").click(),scan("native-model-popup","#chat-popover"),page.keyboard.press("Escape")))
                page.locator("#chat-options-close").click()
                if width<=760:page.locator("#chat-history-toggle").click()
                scan("native-history",".chat-sidebar")
            attempt("native",native)
        finally:
            (output/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
            page.close()
            browser.close()
    assert report["cases"],report

@pytest.mark.parametrize("width,height,scheme", [(1440,1000,"light"),(1440,1000,"dark"),(390,844,"light"),(320,568,"dark")])
def test_peer_menus(stack, width, height, scheme):
    output=Path(os.environ.get("CODEPIER_DROPDOWN_DIR",".work/menu-peers"))/f"{scheme}-{width}x{height}"
    output.mkdir(parents=True,exist_ok=False)
    with sync_playwright() as pw:
        browser=launch_browser(pw.chromium)
        page=browser.new_page(viewport={"width":width,"height":height},color_scheme=scheme,reduced_motion="reduce")
        page.set_default_timeout(3000)
        _login(page,stack)
        _set_scheme(page,scheme)
        for route,group,outside in [("workbench",".work-controls .actions","h1"),("native",".chat-history-chips","#chat-history-search")]:
            _navigate(page,route)
            if route=="native" and width<=760:page.locator("#chat-history-toggle").click()
            menus=page.locator(group+" > details")
            expect(menus.first).to_be_visible()
            # Rapid same-turn summary activation must retain the newest menu after queued toggles.
            menus.evaluate_all("(ms)=>{ms[0].querySelector('summary').click();ms[1].querySelector('summary').click();ms[0].querySelector('summary').click();}")
            expect(page.locator(group+" > details[open]")).to_have_count(1)
            expect(menus.first).to_have_attribute("open","")
            page.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
            expect(menus.first).to_have_attribute("open","")
            for i in range(menus.count()):
                summary=menus.nth(i).locator("summary")
                if menus.nth(i).get_attribute("open") is None:summary.click()
                expect(page.locator(group+" > details[open]")).to_have_count(1)
                page.screenshot(path=str(output/f"{route}-{i}.png"),animations="disabled",timeout=5000)
                summary.press("Escape")
                expect(page.locator(group+" > details[open]")).to_have_count(0)
                expect(summary).to_be_focused()
                summary.press("Enter")
                expect(menus.nth(i)).to_have_attribute("open","")
                if route=="workbench":
                    # A flipped popup may overlay h1; click actual unobscured topbar chrome.
                    page.locator(".topbar").click(position={"x":80,"y":8})
                else:
                    page.locator(outside).first.click()
                expect(page.locator(group+" > details[open]")).to_have_count(0)
                summary.press("Space")
                expect(menus.nth(i)).to_have_attribute("open","")
                summary.press("Escape")
                expect(page.locator(group+" > details[open]")).to_have_count(0)
        browser.close()
