"""Read-only rendered select audit; no form saves or external invitations."""
import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.browser_launch import launch_browser
from tests.browser_support import login_panel, set_appearance


def inspect_surface(page, root, folder, label):
    # Open nested disclosures using real summary clicks, never force clicks.
    for _ in range(64):
        closed = root.locator("details:not([open]) > summary:visible")
        if not closed.count():
            break
        # Re-resolve after each expansion; indices of :not([open]) change.
        closed.first.click()
    else:
        raise AssertionError("Disclosure expansion did not settle")
    metrics = []
    selects = root.locator("select:visible")
    for index in range(selects.count()):
        node = selects.nth(index)
        node.scroll_into_view_if_needed()
        value = node.input_value()
        m = node.evaluate("""n => {
          const r=n.getBoundingClientRect(),s=getComputedStyle(n);
          const labels=[...n.labels||[]].map(l=>l.textContent.trim());
          return {id:n.id,name:n.name,labels,ariaLabel:n.getAttribute('aria-label'),
            x:r.x,y:r.y,width:r.width,height:r.height,
            font:s.fontSize,lineHeight:s.lineHeight,borderRadius:s.borderRadius,
            padding:s.padding,paddingEnd:parseFloat(s.paddingInlineEnd),
            appearance:s.appearance,backgroundImage:s.backgroundImage,
            backgroundSize:s.backgroundSize,backgroundPositionX:s.backgroundPositionX,
            backgroundRepeat:s.backgroundRepeat,single:!n.multiple&&!n.hasAttribute('size'),
            arrowSize:parseFloat(s.getPropertyValue('--ui-select-arrow-size')),
            arrowInset:parseFloat(s.getPropertyValue('--ui-select-arrow-inset')),
            endSpace:parseFloat(s.getPropertyValue('--ui-select-end-space')),
            disabled:n.disabled,options:[...n.options].map(o=>({text:o.text,disabled:o.disabled}))};
        }""")
        metrics.append(m)
        if m['name'] in ('native_file_hosts_mode', 'native_file_providers_mode'):
            assert m['labels'], 'File import mode must have a real associated visible label'
        if m['single']:
            assert m['appearance'] == 'none', 'Single selectors use one inset shared arrow'
            assert m['backgroundImage'].count('url(') == 1 and 'gradient(' not in m['backgroundImage'], 'Exactly one shared arrow, no overlapping native/custom indicators'
            assert m['backgroundRepeat'] == 'no-repeat'
            assert m['arrowSize'] == 16 and m['arrowInset'] == 14
            # A one-value background-size serializes its intrinsic-ratio height as auto.
            assert m['backgroundSize'] in ('16px auto', '16px 16px')
            assert '14px' in m['backgroundPositionX'], 'Arrow must be inset from the edge'
            assert m['paddingEnd'] >= m['endSpace'] == 44, 'Long option text needs the full arrow reserve'
            assert m['paddingEnd'] >= m['arrowSize'] + m['arrowInset'] + 12
        if m['id'] in ('i-project', 'i-workspace'):
            assert node.get_attribute('title'), 'Full context must remain available when text is clipped'
        page.screenshot(path=str(folder/f"{label}-select-{index}-closed.png"))
        if node.is_enabled():
            node.focus()
            node.press("Space")
            page.screenshot(path=str(folder/f"{label}-select-{index}-open.png"))
            # Tab leaves a native selector without bubbling Escape to the modal.
            page.keyboard.press("Tab")
            assert node.input_value() == value, "Audit must not change a selected value"
    if selects.count():
        # OS high-contrast emulation must restore native controls without a second arrow.
        page.emulate_media(forced_colors="active")
        try:
            fallback = selects.evaluate_all("""nodes => nodes.filter(n => !n.multiple && !n.hasAttribute('size')).map(n => {
              const s=getComputedStyle(n); return {appearance:s.appearance, image:s.backgroundImage};
            })""")
            assert all(row["appearance"] == "auto" and row["image"] == "none" for row in fallback), fallback
            (folder/f"{label}-forced-colors.json").write_text(json.dumps({"fallback":fallback}))
        finally:
            page.emulate_media(forced_colors="none")
    page.screenshot(path=str(folder/f"{label}-full.png"),full_page=True)
    # Deliverable evidence uses real viewports, not a stitched fixed-bar artifact.
    for segment, fraction in [('top', 0), ('mid', .5), ('bottom', 1)]:
        root.evaluate('''(root, fraction) => {
          const scroller = [root, ...root.querySelectorAll('*')].find(n =>
            /auto|scroll/.test(getComputedStyle(n).overflowY) && n.scrollHeight > n.clientHeight + 1);
          if (scroller) scroller.scrollTop = (scroller.scrollHeight-scroller.clientHeight)*fraction;
          else window.scrollTo(0, (document.documentElement.scrollHeight-innerHeight)*fraction);
        }''', fraction)
        page.screenshot(path=str(folder/f"{label}-viewport-{segment}.png"))
    (folder/f"{label}.json").write_text(json.dumps(metrics,ensure_ascii=False,indent=2))


@pytest.mark.parametrize("width", [390,1440])
@pytest.mark.parametrize("scheme", ["light","dark"])
def test_settings_connection_identity_control_surface(stack,width,scheme):
    folder=Path(os.getenv("CODEPIER_CONTROL_AUDIT_DIR",".work/control-surface-audit"))/f"{scheme}-{width}"
    folder.mkdir(parents=True,exist_ok=True)
    with sync_playwright() as pw:
        browser=launch_browser(pw.chromium)
        context=browser.new_context(viewport={"width":width,"height":900},reduced_motion="reduce")
        page=context.new_page()
        page.set_default_timeout(3000)
        try:
            login_panel(page,stack,"settings")
            set_appearance(page,scheme)
            groups=page.locator(".settings-groups button")
            for i in range(groups.count()):
                button=groups.nth(i)
                group=button.get_attribute("data-settings-group") or "common"
                button.scroll_into_view_if_needed()
                button.click()
                inspect_surface(page,page.locator("#page"),folder,"settings-"+group)
            for route in ["connect","identity","members","roles","profiles","identity-admin","devices","vps","mcp-gateway","integrations"]:
                page.evaluate("route=>navigate(route)",route)
                expect(page.locator("#page h1").first).to_be_visible()
                inspect_surface(page,page.locator("#page"),folder,route)
            for route,selector,label in [
                ("identity-admin",'[data-iam="new-provider"]',"provider-form"),
                ("roles","#role-create","role-form"),
                ("profiles","#profile-create","profile-form"),
                ("devices",'[data-action="add-device"]',"agent-install-form"),
                ("vps",'[data-vps-action="add"]',"vps-form"),
                ("connect",'[data-action="new-grant"]',"grant-form"),
            ]:
                page.evaluate("route=>navigate(route)",route)
                # New grant is deliberately inside Other Clients disclosure.
                target=page.locator(selector).first
                expect(target).to_be_attached()
                for ancestor in target.locator("xpath=ancestor::details").all():
                    if ancestor.get_attribute("open") is None:
                        ancestor.locator(":scope > summary").click()
                target.click()
                expect(page.locator(".modal")).to_be_visible()
                inspect_surface(page,page.locator(".modal"),folder,label)
                page.keyboard.press("Escape")
                expect(page.locator(".modal")).to_have_count(0)
        finally:
            context.close()
            browser.close()
