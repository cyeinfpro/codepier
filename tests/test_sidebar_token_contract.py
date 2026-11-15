"""Sidebar fields consume the shared control geometry, not page-local copies."""

import pytest
from playwright.sync_api import expect
from tests.test_ui_unification import _login, _set_scheme


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_sidebar_shared_control_radius(stack, chat_browser_pool, scheme):
    context = chat_browser_pool("chromium").new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    page.set_default_timeout(3000)
    try:
        _login(page, stack)
        _set_scheme(page, scheme)
        fields = page.locator(".sidebar .side-bottom select, .sidebar-space-value")
        assert fields.count() >= 2
        values = []
        for node in fields.all():
            expect(node).to_be_visible()
            values.append(
                node.evaluate("""n => {
                const probe=document.createElement('span');
                probe.style.borderRadius='var(--ui-control-radius)';
                n.parentElement.append(probe);
                const token=getComputedStyle(probe).borderRadius;
                probe.remove();
                return {actual:getComputedStyle(n).borderRadius,token};
            }""")
            )
        assert all(v["actual"] == v["token"] == "8px" for v in values), values
    finally:
        context.close()
