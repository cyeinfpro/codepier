"""Synthetic source-module regression for floating notices and confirmations."""
from pathlib import Path
import re

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.browser_launch import launch_browser

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=["chromium", "webkit"])
def layer_page(request):
    with sync_playwright() as pw:
        browser = launch_browser(getattr(pw, request.param))
        page = browser.new_page(viewport={"width": 900, "height": 700})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))

        def serve(route):
            name = route.request.url.removeprefix("https://layers.test/")
            if name in {"core/ui.mjs", "core/lifecycle.mjs"}:
                route.fulfill(content_type="text/javascript", body=(ROOT / "web" / name).read_text())
            else:
                route.fulfill(content_type="text/html", body='<div id="app"><button id="origin">Open</button></div><div id="modal-root"></div><div id="toasts"></div>')

        page.route("https://layers.test/**", serve)
        page.goto("https://layers.test/")
        page.evaluate("""async () => {
          window.CP = {ui: await import('/core/ui.mjs')};
          Object.assign(window, { $: CP.ui.$, $$: CP.ui.$$, uid: CP.ui.uid, esc: CP.ui.esc,
            icon: CP.ui.icon, toast: CP.ui.toast,
            S: {session: {}, work: {}, projects: [], page: 'overview'}, nav: [] });
          window.CodePierIdentity = {canNavigate: () => true};
        }""")
        page.add_script_tag(path=str(ROOT / "web/ui.js"))
        page.add_style_tag(content="""
          .modal-backdrop {position:fixed;inset:0;background:#ddd;}
          .modal {margin:50px;padding:20px;background:white;}
          dialog {max-height:80vh;overflow:auto;}
          #computer-approval-inbox {padding:12px;}
        """)
        page.evaluate("""() => {
          window.modalApi = CP.ui.createModal({state:S, labelFields:uiLabelFields,
            trapTab:uiTrapTab, focusable:uiFocusable});
          window.modal = modalApi.modal; window.closeModal = modalApi.closeModal;
          document.querySelector('#origin').focus();
        }""")
        try:
            yield page
            assert not errors
        finally:
            browser.close()


def test_sheet_preserves_editor_notices_escape_and_focus(layer_page):
    page = layer_page
    page.evaluate("""() => {
      window.editor = modal('Editor', '<form id="project-form"><input name="alias" value="original"></form>',
        '<button id="save">Save</button>');
    }""")
    page.locator('[name="alias"]').fill("unsaved")
    page.evaluate("""() => {
      toast('pending error', true);
      const box = document.createElement('aside');box.id='computer-approval-inbox';
      box.innerHTML='<button id="approve">Approve</button>';document.body.append(box);
      window.answer = 'pending';
      CP.ui.confirmSheet({title:'Delete?', danger:true}).then(value => window.answer=value);
    }""")
    expect(page.locator("dialog #toasts")).to_have_count(1)
    expect(page.locator("dialog #approve")).to_be_visible()
    expect(page.locator("dialog .ghost")).to_be_focused()
    page.keyboard.press("Escape")
    page.wait_for_function("window.answer === false")
    expect(page.locator('[name="alias"]')).to_have_value("unsaved")
    assert page.evaluate("document.querySelector('.modal') === editor")
    assert page.evaluate("document.querySelector('.modal-header').nextElementSibling.id === 'toasts'")
    expect(page.locator("#toasts .error")).to_have_count(1)
    page.locator("#save").focus()
    page.keyboard.press("Tab")
    expect(page.locator("#approve")).to_be_focused()
    page.keyboard.press("Shift+Tab")
    expect(page.locator("#save")).to_be_focused()
    # The editor's synchronous unsaved-input guard remains intact.
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.keyboard.press("Escape")
    expect(page.locator(".modal")).to_have_count(1)


def test_layer_reopen_out_of_order_close_and_removed_host(layer_page):
    page = layer_page
    page.evaluate("""() => {
      window.lower=document.createElement('dialog');lower.innerHTML='<button>Lower</button>';
      window.upper=document.createElement('dialog');upper.innerHTML='<button>Upper</button>';
      document.body.append(lower,upper);CP.ui.showModalLayer(lower);CP.ui.showModalLayer(upper);
      CP.ui.closeModalLayer(lower);
    }""")
    assert page.evaluate("document.querySelector('#toasts').parentElement === upper")
    page.evaluate("""() => {
      CP.ui.closeModalLayer(upper); CP.ui.showModalLayer(upper);
    }""")
    page.wait_for_timeout(50)  # Flush the browser's queued close event from the previous opening.
    assert page.evaluate("CP.ui.layerHost() === upper && upper.contains(document.querySelector('#toasts'))")
    page.evaluate("""() => {
      const box=document.createElement('aside');box.id='computer-approval-inbox';
      box.innerHTML='<button>Approve</button>';CP.ui.layerHost().append(box);
      upper.remove();
    }""")
    page.wait_for_function("document.querySelector('#toasts')?.parentElement === document.body")
    assert page.evaluate("document.querySelector('#computer-approval-inbox').parentElement === document.body")


def test_typed_confirmation_composition_abort_and_repeated_submit(layer_page):
    page = layer_page
    page.evaluate("""() => {
      window.answer='pending';window.count=0;window.controller=new AbortController();
      CP.ui.confirmSheet({title:'Delete project', typed:'Demo', command:'synthetic command',
        signal:controller.signal}).then(value=>{answer=value;count++;});
    }""")
    field = page.locator("dialog input")
    submit = page.locator('dialog button[type="submit"]')
    field.fill("Demo ")
    expect(submit).to_be_disabled()
    field.fill("Demo")
    expect(submit).to_be_enabled()
    field.dispatch_event("compositionstart")
    page.locator("dialog form").evaluate("form=>form.requestSubmit()")
    assert page.evaluate("answer") == "pending"
    field.dispatch_event("compositionend")
    page.locator("dialog form").evaluate("""form => {
      form.requestSubmit();form.requestSubmit();
    }""")
    page.wait_for_function("answer === true && count === 1")
    page.evaluate("""() => {
      window.answer='pending';window.controller=new AbortController();
      CP.ui.confirmSheet({title:'Abort me',signal:controller.signal}).then(value=>answer=value);
      controller.abort();
    }""")
    page.wait_for_function("answer === false")
    expect(page.locator("dialog")).to_have_count(0)


def test_integration_confirmation_replaces_only_sheet_and_detaches(layer_page):
    page = layer_page
    page.add_script_tag(path=str(ROOT / "web/integration-ui.js"))
    page.evaluate("""() => {
      window.editor=modal('Editor','<input id="draft" value="keep">');
      window.first='pending';window.second='pending';
      CodePierIntegrationUI.confirmAction({title:'First',text:'First body'}).then(v=>first=v);
      CodePierIntegrationUI.confirmAction({title:'Second',text:'Second body',typed:'Demo'}).then(v=>second=v);
    }""")
    page.wait_for_function("first === false")
    expect(page.locator("#i-confirm-form")).to_have_count(1)
    expect(page.locator("#draft")).to_have_value("keep")
    page.evaluate("CodePierIntegrationUI.cancelConfirm()")
    page.wait_for_function("second === false")
    assert page.evaluate("editor.isConnected")


def test_native_external_close_resolves_without_confirming(layer_page):
    page = layer_page
    page.evaluate("""() => {
      window.answer='pending';
      CP.ui.confirmSheet({title:'External close'}).then(v=>answer=v);
      document.querySelector('dialog').close();
    }""")
    page.wait_for_function("answer === false")
    expect(page.locator("dialog")).to_have_count(0)
    assert page.evaluate("document.querySelector('#toasts').parentElement === document.body")


def test_menu_flips_and_repositions_after_resize(layer_page):
    page = layer_page
    page.add_style_tag(content="""
      .tool-menu {position:fixed;left:10px;top:560px;width:100px;}
      .tool-menu-items {position:absolute;right:0;top:100%;width:260px;height:200px;overflow:auto;}
      .tool-menu-items[data-placement="top"] {top:auto;bottom:calc(100% + 8px);}
      .tool-menu-items[data-align="start"] {right:auto;left:0;}
    """)
    page.evaluate("""() => {
      const menu=document.createElement('details');menu.className='tool-menu';
      menu.innerHTML='<summary>Menu</summary><div class="tool-menu-items"><button>Item</button></div>';
      document.body.append(menu);menu.open=true;
    }""")
    expect(page.locator(".tool-menu-items")).to_have_attribute("data-placement", "top")
    expect(page.locator(".tool-menu-items")).to_have_attribute("data-align", "start")
    bounds = page.locator(".tool-menu-items").bounding_box()
    assert bounds and bounds["x"] >= 8 and bounds["y"] >= 8
    page.evaluate("document.querySelector('.tool-menu').style.top='20px'")
    page.set_viewport_size({"width": 390, "height": 700})
    page.wait_for_function("!document.querySelector('.tool-menu-items').dataset.placement")
    bounds = page.locator(".tool-menu-items").bounding_box()
    assert bounds and bounds["x"] >= 8 and bounds["x"] + bounds["width"] <= 382


@pytest.mark.parametrize("change", ["S.session={}", "S.space_id='other'", "S.page='other'", "S.renderSeq=1", "document.querySelector('#origin').remove()"])
def test_action_confirmation_rejects_changed_context_and_duplicate_owner(layer_page, change):
    page = layer_page
    page.evaluate("""() => {
      const owner=document.querySelector('#origin');
      window.first='pending';window.second='pending';
      uiConfirmAction({title:'Original action'}, {owner}).then(v=>first=v);
      uiConfirmAction({title:'Duplicate action'}, {owner}).then(v=>second=v);
    }""")
    page.wait_for_function("second === false")
    expect(page.locator(".ui-sheet")).to_have_count(1)
    page.evaluate(change)
    page.locator('.ui-sheet button[type="submit"]').click()
    page.wait_for_function("first === false")


def test_production_css_large_editor_and_native_sheet_keep_approvals_actionable(layer_page):
    page = layer_page
    page.set_viewport_size({"width": 1440, "height": 1000})
    # Replace only this test's synthetic styles with the actual production sheets.
    page.locator("style").evaluate_all("nodes => nodes.forEach(node => node.remove())")
    for name in ("tokens.css", "styles.css", "computer.css", "chat.css"):
        page.add_style_tag(path=str(ROOT / "web" / name))
    page.add_script_tag(path=str(ROOT / "web/computer.js"))
    page.evaluate("""() => {
      window.editor=modal('Large editor',
        '<form id="project-form"><label>Draft<input name="alias" value="keep"></label></form>',
        '<button id="save" class="btn primary">保存</button>', true);
      window.saved=0;document.querySelector('#save').onclick=()=>saved++;
      computerApprovals.login=S.session;
      window.approvalPosts=[];
      window.post=(path,body)=>{approvalPosts.push({path,body});return new Promise(()=>{});};
      renderComputerApprovals([{
        request_id:'synthetic-approval',project_id:'fixture-project',app:'Synthetic app',
        owner:'fixture-owner',session_id:'fixture-session',message:'Synthetic approval only',
        expires_at:Date.now()/1000+3600,
      }], S.session);
      toast('Keep this error',true);
      toast('Dismiss this error',true);
    }""")
    toggle = page.locator(".approval-inbox-toggle")
    expect(toggle).to_be_visible()
    expect(toggle).to_have_text("需要确认 · 1")
    expect(page.locator("#computer-approval-inbox > section")).to_be_hidden()
    pill = page.locator("#computer-approval-inbox").bounding_box()
    save = page.locator("#save").bounding_box()
    assert pill and save and pill["x"] < 40 and pill["y"] > 800
    assert pill["x"] + pill["width"] < save["x"]
    page.locator("#save").click()
    assert page.evaluate("saved") == 1
    page.locator("#save").focus()
    page.keyboard.press("Tab")
    expect(toggle).to_be_focused()
    page.evaluate("""() => {
      window.answer='pending';
      CP.ui.confirmSheet({title:'Confirm above editor', danger:true}).then(v=>answer=v);
    }""")
    expect(page.locator("dialog #computer-approval-inbox")).to_have_count(1)
    expect(page.locator("dialog #toasts")).to_have_count(1)
    # Real clicks check hit testing through the native top layer, not just ancestry.
    toggle.click()
    expect(page.get_by_role("button", name="允许应用访问", exact=True)).to_be_visible()
    page.get_by_role("button", name="允许应用访问", exact=True).click()
    assert page.evaluate("approvalPosts.length") == 1
    expect(page.locator("[data-request-id]")).to_have_attribute("data-state", "pending")
    page.locator(".toast").filter(has_text="Dismiss this error").get_by_role("button", name="关闭提示").click()
    expect(page.locator(".toast").filter(has_text="Dismiss this error")).to_have_count(0)
    toggle.click()
    page.locator(".ui-sheet .ghost").click()
    page.wait_for_function("answer === false")
    expect(page.locator("#computer-approval-inbox")).to_have_count(1)
    expect(page.locator(".toast").filter(has_text="Keep this error")).to_be_visible()
    expect(page.locator('[name="alias"]')).to_have_value("keep")
    page.locator("#save").click()
    assert page.evaluate("saved") == 2


def test_native_lower_layer_direct_close_reopen_updates_stack_order(layer_page):
    page = layer_page
    page.evaluate("""() => {
      window.lower=document.createElement('dialog');lower.innerHTML='<button>Lower</button>';
      window.upper=document.createElement('dialog');upper.innerHTML='<button>Upper</button>';
      document.body.append(lower,upper);
      CP.ui.showModalLayer(lower);CP.ui.showModalLayer(upper);
      // Calling show on an already-open lower layer is idempotent.
      CP.ui.showModalLayer(lower);
    }""")
    assert page.evaluate("CP.ui.layerHost() === upper")
    page.evaluate("lower.close(); CP.ui.showModalLayer(lower)")
    assert page.evaluate("CP.ui.layerHost() === lower && lower.contains(document.querySelector('#toasts'))")
    page.wait_for_timeout(50)
    assert page.evaluate("CP.ui.layerHost() === lower")
    page.evaluate("CP.ui.closeModalLayer(lower)")
    assert page.evaluate("CP.ui.layerHost() === upper && upper.contains(document.querySelector('#toasts'))")


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_menu_resize_before_app_helpers_exist(engine):
    with sync_playwright() as pw:
        browser = launch_browser(getattr(pw, engine))
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(error.stack or str(error)))
        page.set_content('<details class="tool-menu" open><summary>Menu</summary><div class="tool-menu-items"><button>Item</button></div></details>')
        page.add_script_tag(path=str(ROOT / "web/ui.js"))
        assert page.evaluate("typeof $$") == "undefined"
        page.evaluate("window.dispatchEvent(new Event('resize'))")
        page.wait_for_function("document.querySelector('.tool-menu-items').style.maxWidth !== ''")
        page.evaluate("""() => {
          window.dispatchEvent(new Event('resize'));
          window.dispatchEvent(new Event('pagehide'));
          window.dispatchEvent(new Event('pageshow'));
        }""")
        page.wait_for_function("uiMenuFrame === 0")
        browser.close()
        assert not errors


@pytest.fixture
def vps_delete_page(layer_page):
    page = layer_page
    page.add_script_tag(path=str(ROOT / "web/vps.js"))
    page.evaluate("""async () => {
      window.buttons=CP.ui.buttons;window.busy=CP.ui.busy;
      window.loadBasics=async()=>{};window.renderPage=async()=>{};
      window.deletes=[];
      window.api=async(path,options={})=>{
        if(options.method==='DELETE'){
          deletes.push(path);
          return new Promise(resolve=>window.finishDelete=resolve);
        }
        return {name:'Synthetic VPS',host:'example.invalid',port:22,username:'fixture',
          version:7,project_ids:[],enabled:true};
      };
      S.page='vps';S.space_id='fixture-space';S.renderSeq=1;
      await vpsEdit('fixture-vps');
    }""")
    return page


@pytest.mark.parametrize("change", [
    "S.space_id='other'",
    "S.renderSeq++",
    "vpsIntent()",
    "modal('Replacement', '<input value=\"keep\">')",
])
def test_vps_delete_rejects_changed_context(vps_delete_page, change):
    page = vps_delete_page
    page.locator("#vps-delete").click()
    expect(page.locator(".ui-sheet")).to_have_count(1)
    page.evaluate(change)
    page.locator('.ui-sheet button[type="submit"]').click()
    expect(page.locator(".ui-sheet")).to_have_count(0)
    assert page.evaluate("deletes") == []


def test_vps_delete_cancel_duplicate_and_single_submission(vps_delete_page):
    page = vps_delete_page
    page.evaluate("""() => {
      window.deleteButton=document.querySelector('#vps-delete');
      deleteButton.onclick();deleteButton.onclick();
    }""")
    expect(page.locator(".ui-sheet")).to_have_count(1)
    page.locator(".ui-sheet .ghost").click()
    expect(page.locator(".ui-sheet")).to_have_count(0)
    assert page.evaluate("deletes") == []
    expect(page.locator("#vps-form")).to_be_visible()
    page.locator("#vps-delete").click()
    page.locator('.ui-sheet button[type="submit"]').click()
    page.wait_for_function("deletes.length === 1")
    expect(page.locator("#vps-delete")).to_be_disabled()
    page.evaluate("deleteButton.onclick()")
    expect(page.locator(".ui-sheet")).to_have_count(0)
    assert page.evaluate("deletes") == ["/api/vps/fixture-vps?expected_version=7"]
    page.evaluate("finishDelete({})")
    expect(page.locator("#vps-form")).to_have_count(0)


def test_native_dialog_tab_wrap_scrolls_controls_into_view(layer_page):
    page = layer_page
    page.set_viewport_size({"width": 390, "height": 360})
    page.evaluate("""() => {
      const dialog = document.createElement('dialog');
      dialog.innerHTML = '<button id="first-control">First</button><div style="height:600px"></div>' +
        '<button id="last-control">Last</button><details><summary>More</summary><button>Hidden</button></details>';
      document.body.append(dialog);
      CP.ui.showModalLayer(dialog);
    }""")
    first = page.locator("#first-control")
    summary = page.locator("dialog summary")
    first.focus()
    page.keyboard.press("Shift+Tab")
    expect(summary).to_be_focused()
    expect(summary).to_be_in_viewport()
    page.keyboard.press("Shift+Tab")
    expect(page.locator("#last-control")).to_be_in_viewport()
    page.keyboard.press("Tab")
    page.keyboard.press("Tab")
    expect(first).to_be_focused()
    expect(first).to_be_in_viewport()


def test_confirmation_sheet_fits_reduced_visual_viewport(layer_page):
    page = layer_page
    page.set_viewport_size({"width": 390, "height": 360})
    for name in ("tokens.css", "styles.css"):
        page.add_style_tag(path=str(ROOT / "web" / name))
    page.evaluate("""() => {
      CP.ui.confirmSheet({title:'Confirm', body:'Details\\n'.repeat(20), typed:'Project'});
      const viewport = window.visualViewport;
      Object.defineProperties(viewport, {offsetTop:{value:20}, height:{value:190}});
      viewport.dispatchEvent(new Event('resize'));
    }""")
    bounds = page.locator(".ui-sheet").bounding_box()
    assert bounds and bounds["y"] >= 20 and bounds["y"] + bounds["height"] <= 210
    field = page.locator(".ui-sheet input")
    expect(field).to_be_focused()
    field_bounds = field.bounding_box()
    assert field_bounds and field_bounds["y"] >= bounds["y"]
    assert field_bounds["y"] + field_bounds["height"] <= bounds["y"] + bounds["height"]
    assert page.locator(".ui-sheet").evaluate("el => el.scrollHeight > el.clientHeight")


def test_tool_menu_respects_fixed_navigation_and_scroll_pane(layer_page):
    page = layer_page
    page.set_viewport_size({"width": 390, "height": 360})
    page.add_style_tag(content="""
      .topbar {position:fixed;inset:0 0 auto;height:60px;}
      .mobile-dock {position:fixed;inset:auto 0 0;height:60px;}
      #page {position:fixed;inset:60px 12px 60px 34px;overflow:auto;}
      .tool-menu {position:relative;margin-top:80px;width:66px;height:40px;}
      .tool-menu-items {position:absolute;top:calc(100% + 8px);right:0;width:210px;height:240px;overflow:auto;}
      .tool-menu-items button {display:block;height:40px;}
      .tool-menu-items[data-placement="top"] {top:auto;bottom:calc(100% + 8px);}
      .tool-menu-items[data-align="start"] {right:auto;left:0;}
    """)
    page.evaluate("""() => {
      document.querySelector('#app').innerHTML = '<main class="main"><header class="topbar"></header>' +
        '<div id="page">' + uiMenu('Tools', '<button>Item</button>'.repeat(14)) +
        '</div><nav class="mobile-dock"></nav></main>';
      document.querySelector('.tool-menu').open = true;
    }""")
    menu = page.locator(".tool-menu-items")
    expect(menu).to_have_attribute("style", re.compile("max-height:"))
    bounds = menu.bounding_box()
    assert bounds and bounds["x"] >= 42
    assert bounds["y"] >= 60 and bounds["y"] + bounds["height"] <= 300
    bottom = menu.evaluate("el => { el.scrollTop = el.scrollHeight; return el.scrollTop; }")
    assert bottom > 0
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    assert menu.evaluate("el => el.scrollTop") == bottom


@pytest.mark.parametrize("native", [False, True])
def test_escape_closes_tool_menu_before_its_dialog(layer_page, native):
    page = layer_page
    page.evaluate("""native => {
      const menu = uiMenu('Tools', '<button>Item</button>');
      if (native) {
        const dialog = document.createElement('dialog');
        dialog.innerHTML = menu;
        document.body.append(dialog);
        CP.ui.showModalLayer(dialog);
      } else {
        modal('Editor', menu);
      }
      document.querySelector('.tool-menu').open = true;
    }""", native)
    page.get_by_role("button", name="Item").focus()
    page.keyboard.press("Escape")
    expect(page.locator(".tool-menu")).not_to_have_attribute("open", "")
    expect(page.get_by_text("Tools")).to_be_focused()
    expect(page.get_by_role("dialog")).to_be_visible()


def test_long_editor_numbers_and_project_label_fit_mobile_layout(layer_page):
    page = layer_page
    page.set_viewport_size({"width": 320, "height": 568})
    for name in ("tokens.css", "styles.css"):
        page.add_style_tag(path=str(ROOT / "web" / name))
    page.evaluate("""() => {
      document.querySelector('#app').innerHTML = '<div class="editor"><div class="editor-lines">14001</div>' +
        '<textarea>Code</textarea></div><div class="editor-bottom"><span>' +
        'Project_' + 'A'.repeat(80) + '</span><span>SHA abcdef</span></div>';
    }""")
    assert page.locator(".editor-lines").evaluate("""el => {
      const range = document.createRange();
      range.selectNodeContents(el);
      const text = range.getBoundingClientRect(), box = el.getBoundingClientRect();
      return text.left >= box.left && text.right <= box.right;
    }""")
    assert page.locator(".editor-bottom").evaluate("el => el.scrollWidth <= el.clientWidth")


def test_long_room_panel_title_keeps_close_button_in_bounds(layer_page):
    page = layer_page
    page.set_viewport_size({"width": 320, "height": 568})
    for name in ("tokens.css", "styles.css", "collaboration-room.css"):
        page.add_style_tag(path=str(ROOT / "web" / name))
    page.evaluate("""() => {
      const dialog = document.createElement('dialog');
      dialog.className = 'cr-panel';
      dialog.innerHTML = '<header><h2>' + 'Collaborator'.repeat(16) +
        '</h2><button class="cr-button">Close</button></header><div class="cr-panel-content">Room</div>';
      document.body.append(dialog);
      CP.ui.showModalLayer(dialog);
    }""")
    assert page.locator(".cr-panel").evaluate("""el => {
      const panel = el.getBoundingClientRect(), button = el.querySelector('button').getBoundingClientRect();
      return el.scrollWidth <= el.clientWidth && button.right <= panel.right;
    }""")


def test_panel_skips_hidden_fields_and_restores_collapsed_menu_origin(layer_page):
    page = layer_page
    page.evaluate("""() => {
      const root = document.querySelector('#app');
      root.innerHTML = '<details class="tool-menu" open><summary id="trigger">Actions</summary>' +
        '<div class="tool-menu-items"><button id="edit">Edit</button></div></details>';
      document.querySelector('#edit').focus();
      modal('Edit', '<input type="hidden" name="id"><details><summary>Advanced</summary>' +
        '<input name="advanced"></details><input name="label">');
      root.querySelector('details').open = false;
    }""")
    expect(page.locator('[name="label"]')).to_be_focused()
    page.keyboard.press('Escape')
    expect(page.locator('#trigger')).to_be_focused()


def test_tool_menu_can_shrink_inside_narrow_editor_pane(layer_page):
    page = layer_page
    for name in ('tokens.css', 'styles.css'):
        page.add_style_tag(path=str(ROOT / 'web' / name))
    page.evaluate("""() => {
      document.querySelector('#app').innerHTML = '<div id="clip" style="width:170px;height:350px;' +
        'margin:50px;overflow:auto;padding:12px">' + uiMenu('Actions', '<button class="btn">Action</button>') + '</div>';
      document.querySelector('.tool-menu').open = true;
    }""")
    menu = page.locator('.tool-menu-items')
    expect(menu).to_have_attribute('style', re.compile('max-width:'))
    assert menu.evaluate("""el => {
      const box = el.getBoundingClientRect(), pane = el.closest('#clip').getBoundingClientRect();
      return box.width > 0 && box.left >= pane.left && box.right <= pane.right;
    }""")


def test_tablet_room_panel_fits_the_keyboard_viewport(layer_page):
    page = layer_page
    page.set_viewport_size({"width": 768, "height": 700})
    for name in ('tokens.css', 'styles.css', 'collaboration-room.css'):
        page.add_style_tag(path=str(ROOT / 'web' / name))
    page.evaluate("""() => {
      const dialog = document.createElement('dialog');
      dialog.className = 'cr-panel';
      dialog.innerHTML = '<header><h2>Permissions</h2></header><div class="cr-panel-content">' +
        '<div style="height:550px"></div><input name="label"></div>';
      document.body.append(dialog);
      CP.ui.showModalLayer(dialog);
      const viewport = window.visualViewport;
      Object.defineProperties(viewport, {offsetTop:{value:30}, height:{value:360}});
      viewport.dispatchEvent(new Event('resize'));
    }""")
    bounds = page.locator('.cr-panel').bounding_box()
    assert bounds and bounds['y'] >= 30 and bounds['y'] + bounds['height'] <= 390
    field = page.locator('.cr-panel input')
    expect(field).to_be_focused()
    field_bounds = field.bounding_box()
    assert field_bounds and field_bounds['y'] >= bounds['y']
    assert field_bounds['y'] + field_bounds['height'] <= bounds['y'] + bounds['height']
