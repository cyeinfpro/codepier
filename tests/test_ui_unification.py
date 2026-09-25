"""CodePier UI unification acceptance tests using an isolated Hub and real browser.

The native conversation is opened but never submitted, so this suite cannot
start a CLI process or call a model.
"""
from __future__ import annotations

import os
import re
import sys
import pytest
from pathlib import Path
from scripts.check_release import check_web_assets
from shared.util import VERSION

from playwright.sync_api import Page, expect, sync_playwright

from shared.util import atomic_json
from tests.test_chat_worker import FAKE


ROOT = Path(__file__).resolve().parents[1]
SCREENSHOTS = Path(
    os.getenv(
        "CODEPIER_UI_UNIFICATION_SCREENSHOTS",
        "docs/evidence/ui-unify-20260915/screenshots",
    )
)
VIEWPORTS = (
    (320, 568),
    (390, 844),
    (768, 1024),
    (1440, 1000),
)
SCHEMES = ("light", "dark")
MANAGEMENT_ROUTES = (
    "overview",
    "devices",
    "projects",
    "workbench",
    "workflows",
    "audit",
    "connect",
    "diagnostics",
    "artifacts",
    "settings",
)
TOKEN_VALUES = {'light': {'--ui-bg': '#f4f4f4', '--ui-panel': '#ffffff', '--ui-text': '#272727', '--ui-accent': '#746018'}, 'dark': {'--ui-bg': '#191919', '--ui-panel': '#222222', '--ui-text': '#e5e5e5', '--ui-accent': '#d1bd75'}}


def _asset_positions(html: str) -> dict[str, int]:
    assets = ("tokens.css", "appearance.js", "styles.css", "computer.css", "chat.css")
    positions = {asset: html.find(asset) for asset in assets}
    assert all(value >= 0 for value in positions.values()), positions
    return positions


def _login(page: Page, stack, route: str = "overview") -> None:
    page.goto(f"{stack.url}/#{route}", wait_until="domcontentloaded")
    expect(page.locator("#login-form")).to_be_visible()
    page.fill('#username', 'admin');page.locator("#password").fill(stack.password)
    page.locator("#login-form button[type=submit]").click()
    expect(page.locator(".shell")).to_be_visible()
    expect(page.locator(".skeleton")).to_have_count(0, timeout=15_000)


def _prepare_native_fixture(stack) -> None:
    """Force native catalog traffic through local deterministic CLI substitutes."""
    if getattr(stack, "_ui_unification_native_fixture", False):
        return
    bindir = stack.directory / "ui-unification-fake-bin"
    home = stack.directory / "ui-unification-home"
    bindir.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    for cli in ("pi", "codex"):
        executable = bindir / cli
        executable.write_text(
            "#!"
            + sys.executable
            + "\nimport sys\nsys.argv=[sys.argv[0],"
            + repr(cli)
            + "]\n"
            + FAKE,
            encoding="utf-8",
        )
        executable.chmod(0o700)
    stack.stop_agent()
    stack.config["shell"] = {
        "enabled": True,
        "projects": ["*"],
        "inherit_env": False,
        "env": {
            "PATH": str(bindir)
            + os.pathsep
            + str(Path(sys.executable).parent)
            + os.pathsep
            + os.defpath,
            "HOME": str(home),
        },
    }
    atomic_json(stack.config_path, stack.config)
    stack.start_agent()
    stack._ui_unification_native_fixture = True


def _set_scheme(page: Page, scheme: str) -> None:
    result = page.evaluate(
        """scheme => {
          if (!window.CodePierAppearance) return {missing: true};
          window.CodePierAppearance.setPreference(scheme);
          return {
            preference: window.CodePierAppearance.getPreference(),
            scheme: window.CodePierAppearance.getScheme(),
            dataset: document.documentElement.dataset.appearance,
          };
        }""",
        scheme,
    )
    assert result == {"preference": scheme, "scheme": scheme, "dataset": scheme}
    page.wait_for_function(
        "scheme => document.documentElement.dataset.appearance === scheme", arg=scheme
    )
    controls = page.locator("[data-ui-appearance]")
    expect(controls).not_to_have_count(0)
    for index in range(controls.count()):
        expect(controls.nth(index)).to_have_value(scheme)


def _navigate(page: Page, route: str) -> None:
    page.evaluate("route => navigate(route)", route)
    expect(page.locator(".skeleton")).to_have_count(0, timeout=15_000)
    if route == "native":
        expect(page.locator("#chat-root")).to_be_visible()
    else:
        expect(page.locator(f'.nav [data-nav="{route}"]')).to_have_attribute(
            "aria-current", "page"
        )


def _layout(page: Page, label: str) -> dict:
    result = page.evaluate(
        """() => {
          const selectors = [
            '#page > *', '.page-head', '.panel', '.notice', '.modal',
            '#chat-root', '.chat-header', '.chat-composer-wrap'
          ];
          const errors = [...document.querySelectorAll(selectors.join(','))]
            .flatMap(el => {
              if (!el.getClientRects().length) return [];
              const style = getComputedStyle(el);
              if (style.visibility === 'hidden' || style.display === 'none') return [];
              const r = el.getBoundingClientRect();
              return r.left < -1 || r.right > innerWidth + 1
                ? [{selector: el.id || el.className || el.tagName,
                    left: Math.round(r.left), right: Math.round(r.right)}]
                : [];
            });
          return {
            viewport: innerWidth,
            documentWidth: document.documentElement.scrollWidth,
            bodyWidth: document.body.scrollWidth,
            errors,
          };
        }"""
    )
    assert result["documentWidth"] <= result["viewport"] + 1, (label, result)
    assert result["bodyWidth"] <= result["viewport"] + 1, (label, result)
    assert not result["errors"], (label, result)
    return result


def _assert_tokens(page: Page, scheme: str) -> dict[str, str]:
    values = page.evaluate(
        """names => Object.fromEntries(names.map(name => [
          name, getComputedStyle(document.documentElement).getPropertyValue(name).trim()
        ]))""",
        list(TOKEN_VALUES[scheme]),
    )
    assert {key: value.lower() for key, value in values.items()} == TOKEN_VALUES[scheme]
    return values


def _font(page: Page, selector: str) -> str:
    return page.locator(selector).evaluate("el => getComputedStyle(el).fontFamily")


def _assert_control_is_uncovered(page: Page, selector: str) -> None:
    # Wait for native actionability, including drawer transforms, without
    # actually opening a select or changing the tested preference.
    page.locator(selector).click(trial=True)
    result = page.locator(selector).evaluate(
        """el => {
          const r = el.getBoundingClientRect();
          const x = Math.max(0, Math.min(innerWidth - 1, r.left + r.width / 2));
          const y = Math.max(0, Math.min(innerHeight - 1, r.top + r.height / 2));
          const top = document.elementFromPoint(x, y);
          return {
            visible: !!el.getClientRects().length,
            hit: !!top && (top === el || el.contains(top)),
            top: top && (top.id || top.className || top.tagName),
          };
        }"""
    )
    assert result["visible"] and result["hit"], (selector, result)


def _native_management_round_trip(page: Page, draft: str) -> None:
    page.locator("#chat-compose").fill(draft)
    expect(page.locator("#chat-compose")).to_have_value(draft)
    if page.viewport_size["width"] <= 760:
        page.locator("#chat-history-toggle").click()
        expect(page.locator("#chat-root")).to_have_class(re.compile(r"\bdrawer-open\b"))
    page.locator('#chat-back').click()
    expect(page.locator(".overview-grid")).to_be_visible()
    if page.viewport_size["width"] <= 900:
        page.locator(".mobile-menu").click()
    page.locator('.nav [data-nav="native"]').click()
    expect(page.locator("#chat-compose")).to_have_value(draft)


def exercise_matrix(stack, output: Path, *, screenshots: bool = True, schemes=SCHEMES, viewports=VIEWPORTS) -> dict:
    """Run the complete visual matrix and return machine-readable evidence."""
    _prepare_native_fixture(stack)
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "scope": (
            "isolated Hub/Agent with deterministic local pi/codex substitutes; "
            "native conversation opened without submission"
        ),
        "screenshots": [],
        "cases": [],
        "browser_errors": [],
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            for scheme in schemes:
                for width, height in viewports:
                    context = browser.new_context(
                        viewport={"width": width, "height": height},
                        color_scheme=scheme,
                        reduced_motion="no-preference",
                    )
                    page = context.new_page()
                    errors: list[str] = []
                    submitted: list[str] = []
                    page.on("pageerror", lambda error, rows=errors: rows.append(str(error)))

                    def observe_request(request, rows=submitted):
                        if request.method != "POST" or "/api/native/" not in request.url:
                            return
                        action = request.url.rsplit("/", 1)[-1]
                        if action in {"start", "chat_prompt"}:
                            rows.append(action)

                    page.on("request", observe_request)
                    _login(page, stack)
                    _set_scheme(page, scheme)
                    tokens = _assert_tokens(page, scheme)
                    management_font = _font(page, ".main")
                    if width <= 900:
                        page.locator('.mobile-menu').click()
                    page.locator('[data-ui-appearance]').scroll_into_view_if_needed()
                    _assert_control_is_uncovered(page, "[data-ui-appearance]")
                    if width <= 900:
                        page.keyboard.press('Escape')
                    expect(page.locator(".ambient")).to_have_attribute("aria-hidden", "true")
                    assert page.locator(".ambient").evaluate(
                        "el => getComputedStyle(el).pointerEvents"
                    ) == "none"
                    management_layouts = {}
                    for route in MANAGEMENT_ROUTES:
                        if route != "overview":
                            _navigate(page, route)
                        assert page.evaluate("CodePierAppearance.getScheme()") == scheme
                        expect(page.locator("[data-ui-appearance]").first).to_have_value(
                            scheme
                        )
                        assert _font(page, ".main") == management_font
                        management_layouts[route] = _layout(
                            page, f"{scheme}-{width}-{route}"
                        )
                        name = f"{scheme}-{width}-{route}.png"
                        if screenshots:
                            page.screenshot(
                                path=str(output / name),
                                full_page=True,
                                animations="disabled",
                            )
                            report["screenshots"].append(name)

                    _navigate(page, "native")
                    expect(page.locator("#chat-appearance")).to_have_value(scheme)
                    assert _font(page, "#chat-root") == management_font
                    _assert_control_is_uncovered(page, "#chat-compose")
                    native_layout = _layout(page, f"{scheme}-{width}-native")
                    draft = f"UI fixture draft {scheme} {width}"
                    _native_management_round_trip(page, draft)
                    assert page.evaluate("CodePierAppearance.getScheme()") == scheme
                    assert not submitted, submitted
                    native_name = f"{scheme}-{width}-native.png"
                    if screenshots:
                        page.screenshot(
                            path=str(output / native_name),
                            full_page=True,
                            animations="disabled",
                        )
                        report["screenshots"].append(native_name)

                    report["cases"].append(
                        {
                            "scheme": scheme,
                            "viewport": {"width": width, "height": height},
                            "tokens": tokens,
                            "font": management_font,
                            "management_layouts": management_layouts,
                            "native_layout": native_layout,
                            "model_submission_requests": submitted,
                        }
                    )
                    report["browser_errors"].extend(errors)
                    assert not errors, errors
                    context.close()
        finally:
            browser.close()
    assert not report["browser_errors"], report["browser_errors"]
    atomic_json(output.parent / 'capture-report.json', report)
    return report


def test_contract_assets_load_in_the_required_order():
    html = (ROOT / "web/index.html").read_text(encoding="utf-8")
    positions = _asset_positions(html)
    assert positions["tokens.css"] < positions["styles.css"]
    assert positions["tokens.css"] < positions["computer.css"]
    assert positions["tokens.css"] < positions["chat.css"]
    assert positions["appearance.js"] < positions["styles.css"]
    appearance_tag = re.search(r"<script\b[^>]*appearance\.js[^>]*>", html)
    assert appearance_tag
    assert "defer" not in appearance_tag.group(0)
    changed_assets = re.findall(
        r'(?:tokens\.css|appearance\.js|styles\.css|computer\.css|chat\.css'
        r'|ui\.js|app\.js|chat-chrome\.js)[^"\']*',
        html,
    )
    assert changed_assets
    current_assets = check_web_assets(ROOT, VERSION)
    for asset in ('chat-history.js','chat-catalog.js','chat.js','chat-panels.js','chat-chrome.js','chat.css','app.js'):
        assert asset in current_assets, asset
    assert html.index('chat-history.js') < html.index('chat.js?')
    assert html.index('chat-catalog.js') < html.index('chat.js?')
    assert all(asset.split("?", 1)[0] in current_assets for asset in changed_assets), changed_assets


@pytest.mark.isolated_case
@pytest.mark.parametrize('scheme', SCHEMES)
@pytest.mark.parametrize('viewport', VIEWPORTS, ids=lambda value: f'{value[0]}x{value[1]}')
def test_theme_viewport_conversation_matrix(stack, tmp_path, scheme, viewport):
    output = (
        SCREENSHOTS
        if os.getenv("CODEPIER_UI_UNIFICATION_SCREENSHOTS")
        else tmp_path / "ui-unification"
    )
    # Each case owns both its screenshots and capture-report.json when sharded.
    output = output / f'{scheme}-{viewport[0]}x{viewport[1]}' / 'screenshots'
    report = exercise_matrix(stack, output, schemes=(scheme,), viewports=(viewport,))
    assert len(report["cases"]) == 1
    assert len(report["screenshots"]) == len(report["cases"]) * (
        len(MANAGEMENT_ROUTES) + 1
    )


def test_reduced_motion_disables_visible_css_motion(stack):
    _prepare_native_fixture(stack)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1440, "height": 1000},
            reduced_motion="reduce",
        )
        try:
            _login(page, stack)
            _set_scheme(page, "dark")
            _navigate(page, "native")
            offenders = page.evaluate(
                """() => [...document.querySelectorAll('body *')].flatMap(el => {
                  if (!el.getClientRects().length) return [];
                  return [null, '::before', '::after'].flatMap(pseudo => {
                    const s = getComputedStyle(el, pseudo);
                    const animations = s.animationName.split(',').map((name, i) => ({
                      name: name.trim(),
                      duration: parseFloat((s.animationDuration.split(',')[i] || '0s')),
                      iterations: (s.animationIterationCount.split(',')[i] || '1').trim(),
                    })).filter(a => a.name !== 'none' && a.duration > .01);
                    const properties = s.transitionProperty.split(',');
                    const transitions = s.transitionDuration.split(',').flatMap((value, i) => {
                      const property = (properties[i] || properties.at(-1) || '').trim();
                      const duration = parseFloat(value);
                      return duration > .01 && (
                        property === 'all' ||
                        ['transform', 'translate', 'scale', 'rotate', 'opacity',
                         'height', 'width', 'left', 'right', 'top', 'bottom']
                          .includes(property)
                      ) ? [{property, duration}] : [];
                    });
                    return animations.length || transitions.length
                      ? [{element: el.id || el.className || el.tagName,
                          pseudo, animations, transitions}]
                      : [];
                  });
                }).slice(0, 20)"""
            )
            assert not offenders, offenders
        finally:
            browser.close()


def test_native_and_management_share_theme_without_transcript_scans(stack):
    """Exercise the integrated theme service, not chat's standalone fallback."""
    _prepare_native_fixture(stack)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, color_scheme="dark")
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(stack.url, wait_until="domcontentloaded")
            expect(page.locator('#login-form')).to_be_visible()
            page.evaluate("localStorage.setItem('codepier-chat-appearance','dark');localStorage.removeItem('codepier-appearance')")
            page.reload(wait_until="domcontentloaded")
            expect(page.locator('[data-ui-appearance]')).to_have_value('dark')
            assert page.evaluate("localStorage.getItem('codepier-appearance')") == 'dark'
            _login(page, stack)
            expect(page.locator('[data-ui-appearance]')).to_have_value('dark')
            _navigate(page, 'native')
            composer = page.locator('#chat-compose')
            composer.fill('Theme changes must keep this draft and selection.')
            page.evaluate("window.themeComposer=document.querySelector('#chat-compose');themeComposer.setSelectionRange(2,7)")
            scans = page.evaluate("""async () => {
              let scans=0;const original=document.querySelectorAll;
              document.querySelectorAll=function(selector){
                if(selector==='[data-ui-appearance],#chat-appearance')scans++;
                return original.call(this,selector);
              };
              try {
                const container=document.createElement('div');
                document.querySelector('.chat-scroll').append(container);
                for(let i=0;i<50;i++){const p=document.createElement('p');p.textContent='UI fixture '+i;container.append(p);}
                await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
                container.remove();return scans;
              } finally {document.querySelectorAll=original;}
            }""")
            assert scans == 0
            page.locator('#chat-appearance').select_option('light')
            expect(page.locator('html')).to_have_attribute('data-appearance','light')
            assert page.evaluate("themeComposer===document.querySelector('#chat-compose')")
            assert composer.evaluate('el=>[el.selectionStart,el.selectionEnd]') == [2,7]
            expect(composer).to_have_value('Theme changes must keep this draft and selection.')
            assert page.locator('#chat-root').evaluate('el=>getComputedStyle(el).backgroundColor') == 'rgb(255, 255, 255)'
            _navigate(page,'overview')
            expect(page.locator('[data-ui-appearance]')).to_have_value('light')
            peer=context.new_page();peer.goto(stack.url,wait_until='domcontentloaded')
            expect(peer.locator('.shell')).to_be_visible()
            peer.evaluate("localStorage.setItem('codepier-appearance','dark')")
            expect(page.locator('html')).to_have_attribute('data-appearance','dark')
            peer.evaluate('localStorage.clear()')
            expect(page.locator('[data-ui-appearance]')).to_have_value('auto')
            page.emulate_media(color_scheme='light')
            expect(page.locator('html')).to_have_attribute('data-appearance','light')
            _navigate(page,'native')
            expect(page.locator('#chat-appearance')).to_have_value('auto')
            expect(page.locator('#chat-compose')).to_have_value('Theme changes must keep this draft and selection.')
            assert not errors, errors
        finally:
            context.close();browser.close()
