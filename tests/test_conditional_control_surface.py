"""Conditional controls in real panel DOM, with browser-local, nonpersistent fixtures.
No gateway/SSH/install/browser actions reach a service. No permission form is saved.
"""
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.browser_launch import launch_browser
from tests.browser_support import login_panel, set_appearance
from tests.test_control_surface_audit import inspect_surface
from tests.test_integrations_browser import capture_integration_renders


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_conditional_select_surfaces(stack, width, scheme):
    folder = Path(os.getenv("CODEPIER_CONTROL_AUDIT_DIR", ".work/control-surface-02")) / f"conditional-{scheme}-{width}"
    folder.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = launch_browser(pw.chromium)
        context = browser.new_context(viewport={"width": width, "height": 900}, reduced_motion="reduce")
        page = context.new_page()
        page.set_default_timeout(3000)
        blocked = []
        try:
            login_panel(page, stack, "devices")
            set_appearance(page, scheme)
            device = page.evaluate("S.devices[0]")
            projects = page.evaluate("S.projects.slice(0,2)")
            assert len(projects) == 2
            # Roots uses the same snapshot contract as test_agent_roots_browser.
            def roots(route):
                if route.request.method != "GET":
                    blocked.append(route.request.url)
                    route.abort()
                    return
                route.fulfill(json={"device_id": device["id"], "state": "applied",
                    "config_sha256": "1"*64,
                    "roots": [{"path": "/fixture/projects", "writable": True, "allow_tasks": True}]})
            page.route("**/api/devices/*/roots", roots)
            page.locator('[data-action="device-detail"]').first.click()
            expect(page.locator('[data-roots="add"]')).to_be_enabled()
            page.locator('[data-roots="add"]').click()
            expect(page.locator('[data-root-field="mode"]')).to_be_visible()
            inspect_surface(page, page.locator(".modal"), folder, "roots-editor")
            page.locator('[data-root-field="mode"]').select_option("read")
            expect(page.locator("[data-roots-scope]")).to_contain_text("只读")
            page.locator('[data-root-field="mode"]').select_option("write")
            page.keyboard.press("Escape")

            # Real device-detail command entry; do not generate or copy commands.
            page.locator('[data-action="device-detail"]').first.click()
            command = page.locator('[data-action="agent-commands"]').first
            for ancestor in command.locator("xpath=ancestor::details").all():
                if ancestor.get_attribute("open") is None:
                    ancestor.locator(":scope > summary").click()
            command.click()
            expect(page.locator("#agent-command-form")).to_be_visible()
            inspect_surface(page, page.locator(".modal"), folder, "maintenance")
            original_platform = page.locator('#agent-command-form [name="platform"]').input_value()
            page.locator('#agent-command-form [name="platform"]').select_option("windows")
            expect(page.locator("#agent-command-archive-wrapper")).to_be_visible()
            inspect_surface(page, page.locator(".modal"), folder, "maintenance-windows")
            page.locator('#agent-command-form [name="platform"]').select_option(original_platform)
            page.keyboard.press("Escape")
            # Local unsaved platform exploration correctly invokes dirty-form guard.
            page.get_by_role("button", name="继续离开", exact=True).click()
            expect(page.locator(".modal")).to_have_count(0)

            # Browser-local VPS metadata, no credentials or real saved record.
            vps = {"id": "fixture-vps", "name": "隔离审阅 VPS", "host": "vps.example.invalid",
                   "port": 22, "username": "fixture", "enabled": True, "host_key_policy": "strict",
                   "provider": "", "region": "", "system": "Linux", "notes": "",
                   "project_ids": [p["id"] for p in projects],
                   "projects": [{**p, "mode": "write", "allow_tasks": True} for p in projects]}
            def vps_route(route):
                if route.request.method != "GET":
                    blocked.append(route.request.url); route.abort(); return
                route.fulfill(json={"vps": [vps], "next_offset": None} if urlsplit(route.request.url).path == "/api/vps" else vps)
            page.route("**/api/vps**", vps_route)
            page.evaluate("navigate('vps')")
            page.locator('[data-vps-action="check"]').first.click()
            expect(page.locator("#vps-check-project")).to_be_visible()
            inspect_surface(page, page.locator(".modal"), folder, "vps-existing-check")
            page.keyboard.press("Escape")

            gateway = {"enabled": True, "instance_admin": True, "space_admin": True, "user_id": "fixture",
                "connectors": [{"id":"c1","label":"隔离服务","enabled":True,"endpoint":"https://mcp.example.invalid/mcp","protocol":"auto","version":1}],
                "accounts": [{"id":"a1","label":"无凭据测试帐户","connector_id":"c1","enabled":True,"sharing":"space","owner_user_id":"fixture","catalog_hash":"fixture","version":1}],
                "bindings": [{"id":"b1","account_id":"a1","alias":"fixture","tools":["echo"],"enabled":True,"version":1}],
                "grants": [], "calls": []}
            def gateway_route(route):
                path = urlsplit(route.request.url).path
                if path.endswith("/discover") and route.request.method == "POST":
                    route.fulfill(json={"catalog_hash":"fixture","tools":[{"name":"echo","description":"本地假工具","inputSchema":{"type":"object"}}]})
                elif path == "/api/mcp-gateway" and route.request.method == "GET":
                    route.fulfill(json=gateway)
                else:
                    blocked.append(route.request.url); route.abort()
            page.route("**/api/mcp-gateway**", gateway_route)
            page.route("**/api/access-roles", lambda route: route.fulfill(json={"roles":[
                {"id":"r1","label":"隔离角色","enabled":True,"version":1,"connector_rules":[],"project_rules":[],"device_rules":[]}]}))
            page.evaluate("navigate('mcp-gateway')")
            for selector, name, field in [
                ('[data-gw="connector"]', "gateway-protocol", "protocol"),
                ('[data-gw="account"]', "gateway-account", "connector_id"),
                ('[data-gw-discover]', "gateway-publish-target", "binding_id"),
                ('[data-gw-role]', "gateway-role", "role_id"),
            ]:
                if selector == '[data-gw-role]':
                    page.get_by_role('tab', name='已发布工具', exact=True).click()
                page.locator(selector).first.click()
                expect(page.locator(f'#gw-form [name="{field}"]')).to_be_visible()
                inspect_surface(page, page.locator(".modal"), folder, name)
                page.keyboard.press("Escape")

            # Exercise all real tabs. Only browser status/snapshot are deterministic
            # fixture responses; no browser_open/action or other mutation is sent.
            def tools_route(route):
                data = route.request.post_data_json
                tool = data.get("tool")
                if tool == "browser_status":
                    route.fulfill(json={"active_leases":[{"lease_id":"fixture-lease","state":"active","expires_at":1999999999}]})
                elif tool == "browser_snapshot":
                    route.fulfill(json={"lease_id":"fixture-lease","observation_id":"fixture-observation",
                        "title":"隔离页面观察","url":"https://fixture.invalid","text":"本地静态测试",
                        "elements":[{"id":"select1","tag":"select","label":"完整的测试下拉框名称",
                                     "options":[{"label":"选项甲"},{"label":"选项乙"}]}]})
                elif tool in ("browser_open", "browser_action", "browser_close", "agent_setup_integrations"):
                    blocked.append(tool); route.abort()
                else:
                    route.continue_()
            page.route("**/api/tools/call", tools_route)
            page.evaluate("navigate('integrations')")
            capture_integration_renders(page)
            for tab in ["overview","validation","handoff","navigation","worktrees","browser","status","setup"]:
                page.locator(f'[data-i-tab="{tab}"]').click()
                page.evaluate("async () => { await Promise.all(window.integrationRenders); }")
                expect(page.locator("#i-body")).to_have_attribute("aria-labelledby", "i-tab-"+tab)
                inspect_surface(page, page.locator("#page"), folder, "integrations-"+tab)
                if tab == "browser":
                    page.locator('[data-i-action="snapshot"]').first.click()
                    form = page.locator('[data-i-form="browser-action"]')
                    expect(form).to_be_visible()
                    form.locator('[name="action"]').select_option("select")
                    form.locator('[name="element_id"]').select_option("select1")
                    expect(form.locator('[name="option_value"]')).to_be_visible()
                    inspect_surface(page, page.locator("#page"), folder, "integrations-browser-options")
            assert not blocked, f"Unexpected mutation attempted: {blocked}"
        except Exception:
            page.screenshot(path=str(folder / "failure-viewport.png"))
            (folder / "failure.html").write_text(page.content())
            raise
        finally:
            page.unroute_all(behavior="wait")
            context.close()
            browser.close()
