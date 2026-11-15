"""Arrow geometry on isolated fixture controls; never submit model or permission changes."""
import json
import os
from pathlib import Path
import pytest
from playwright.sync_api import expect
from tests.collaboration_support import collaboration_stack
from tests.test_room_center_browser import sign_in, create_room
from tests.test_ui_unification import _login, _navigate, _prepare_native_fixture, _set_scheme

CASES=[(390,844,"light"),(1440,1000,"dark")]
def check_select(page,control):
    expect(control).to_be_visible()
    control.scroll_into_view_if_needed()
    def style():
        return control.evaluate("""n=>{const s=getComputedStyle(n);return {appearance:s.appearance,image:s.backgroundImage,position:s.backgroundPosition,size:s.backgroundSize,end:s.paddingInlineEnd};}""")
    before=control.input_value()
    for state in ("normal","focus","hover"):
        if state=="focus":control.focus()
        if state=="hover":control.hover()
        s=style()
        assert s["appearance"]=="none",s
        assert s["image"].count("url(")==1,s
        assert "14px" in s["position"],s
        assert s["size"] in ("16px","16px 16px","16px auto"),s
        assert float(s["end"].replace("px",""))>=44,s
    page.emulate_media(forced_colors="active")
    s=style()
    assert s["appearance"]=="auto" and s["image"]=="none",s
    page.emulate_media(forced_colors="none")
    assert control.input_value()==before
    return style()
def out(width,scheme):
    p=Path(os.environ.get("CODEPIER_ARROW_DIR",".work/arrows-local"))/f"{scheme}-{width}"
    p.mkdir(parents=True,exist_ok=True)
    return p

@pytest.mark.parametrize("width,height,scheme",CASES)
def test_native_arrow_contract(stack,chat_browser_pool,width,height,scheme):
    _prepare_native_fixture(stack)
    context=chat_browser_pool("chromium").new_context(viewport={"width":width,"height":height},color_scheme=scheme)
    page=context.new_page()
    page.set_default_timeout(3000)
    try:
        _login(page,stack);_set_scheme(page,scheme);_navigate(page,"native")
        page.locator("#chat-options-toggle").click()
        records={}
        for sel in ["#chat-project","#chat-provider","#chat-effort-select"]:
            records[sel]=check_select(page,page.locator(sel))
        for sel in ["#chat-model-picker","#chat-cwd-button"]:
            button=page.locator(sel)
            button.scroll_into_view_if_needed()
            values=button.evaluate("""n=>{const icons=[...n.querySelectorAll('svg')].filter(x=>getComputedStyle(x).display!=='none');const r=n.getBoundingClientRect(),i=icons[0].getBoundingClientRect();return {count:icons.length,size:i.width,gap:r.right-i.right};}""")
            assert values["count"]==1 and values["size"]==16 and 14<=values["gap"]<=15,values
        page.screenshot(path=str(out(width,scheme)/"native-settings.png"),animations="disabled")
        (out(width,scheme)/"native.json").write_text(json.dumps(records,indent=2))
    finally:context.close()

@pytest.mark.parametrize("width,height,scheme",CASES)
def test_room_arrow_contract(collaboration_stack,chat_browser_pool,width,height,scheme):
    context=chat_browser_pool("chromium").new_context(viewport={"width":width,"height":height},color_scheme=scheme)
    page=context.new_page();page.set_default_timeout(3000)
    try:
        sign_in(page,collaboration_stack);_set_scheme(page,scheme)
        if width<=720:page.locator('[data-cr-action="rooms"]').click()
        create_room(page,"箭头隔离验收")
        records={"recipient":check_select(page,page.locator(".cr-recipient select"))}
        page.screenshot(path=str(out(width,scheme)/"room-recipient.png"),animations="disabled")
        page.locator('.cr-room-actions [data-cr-action="permissions"]').click()
        control=page.locator('[data-cr-form="permissions"] [data-permission-level]').first
        records["permission"]=check_select(page,control)
        page.screenshot(path=str(out(width,scheme)/"room-permission.png"),animations="disabled")
        (out(width,scheme)/"room.json").write_text(json.dumps(records,indent=2))
    finally:context.close()
