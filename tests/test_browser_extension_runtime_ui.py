"""Real Chromium extension UI in a fresh profile, never the owner's browser.

Only the copied fixture's native host name differs: a unique nonexistent name
prevents contacting an installed real bridge. This is not native-host E2E.
"""
import json
import shutil
import uuid
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from tests.browser_launch import launch_persistent_browser

BASE = Path(__file__).resolve().parents[1]


@pytest.mark.browser
def test_real_extension_popup_feedback_and_connection_evidence(tmp_path):
    source = BASE / 'web/browser-extension'
    extension = tmp_path / 'extension'
    shutil.copytree(source, extension)
    background = extension / 'background.js'
    original = background.read_text()
    assert original.count("'com.codepier.browser'") == 1
    fixture_host = 'com.codepier.fixture_' + uuid.uuid4().hex
    background.write_text(original.replace("'com.codepier.browser'", repr(fixture_host)))
    evidence = BASE / '.work' / ('extension-ui-' + uuid.uuid4().hex)
    evidence.mkdir(parents=True)
    with sync_playwright() as playwright:
        context = launch_persistent_browser(
            playwright.chromium, str(tmp_path / 'profile'), headless=True,
            args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'],
            viewport={'width': 390, 'height': 940},
        )
        try:
            # An empty isolated profile has no user accounts, permissions or tabs.
            context.route('http://**/*', lambda route: route.abort())
            context.route('https://**/*', lambda route: route.abort())
            worker = context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
            extension_id = worker.url.split('/')[2]
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'chrome-extension://{extension_id}/popup.html')
            expect(page.locator('#connection')).to_contain_text('未连接', timeout=15000)
            expect(page.locator('#connection-observed')).to_contain_text('最近观察')
            assert page.locator('#feedback').inner_text() == ''
            assert page.locator('#origins li').count() == 0
            page.locator('#size').fill('2')
            page.locator('#prepare').click()
            expect(page.locator('#pool')).to_contain_text('2 个标签页')
            expect(page.locator('#feedback')).to_contain_text('操作已完成')
            expect(page.locator('#connection')).to_contain_text('未连接')
            assert page.locator('#origins li').count() == 0
            for theme in ('light', 'dark'):
                page.emulate_media(color_scheme=theme)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(evidence / f'extension-popup-{theme}.png'), full_page=True)
            assert not errors, errors
            (evidence / 'source.json').write_text(json.dumps({
                'surface': 'real Chromium extension runtime / isolated profile',
                'native_host': 'nonexistent fixture; real bridge not tested',
                'credentials': 'none', 'external_network': 'blocked',
                'source': 'web/browser-extension', 'only_fixture_change': 'native host name',
                'assertions': ['connection evidence separate from feedback', 'prepare isolated tabs', 'no origins granted', '390px overflow', 'no page errors'],
            }, ensure_ascii=False, indent=2))
            print('EXTENSION_UI_EVIDENCE=' + str(evidence.relative_to(BASE)))
        finally:
            context.close()
