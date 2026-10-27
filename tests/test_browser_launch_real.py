"""Synthetic real-browser regression: scheduling and actionability stay real."""
import pytest
from playwright.sync_api import sync_playwright, TimeoutError
from tests.browser_launch import launch_browser, launch_persistent_browser


@pytest.mark.browser
@pytest.mark.parametrize('iteration', range(3))
def test_real_frames_animation_actionability_and_multiple_pages(iteration):
    with sync_playwright() as pw:
        browser = launch_browser(pw.chromium)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.set_content('<style>@keyframes slide {from {transform:translateX(0)} to {transform:translateX(200px)}} button {animation:slide 0.4s linear infinite alternate}</style><button onclick="window.clicks=(window.clicks||0)+1">Go</button>')
            page.evaluate('window.framesSeen=0;function tick(){window.framesSeen++;requestAnimationFrame(tick)};requestAnimationFrame(tick)')
            with pytest.raises(TimeoutError):
                page.get_by_role('button').click(timeout=700)
            assert page.evaluate('window.framesSeen') > 0
            assert page.evaluate('window.clicks||0') == 0
            page.evaluate('document.querySelector("button").style.animationIterationCount="1"')
            page.get_by_role('button').click(timeout=5000)
            assert page.evaluate('window.clicks') == 1
            other = context.new_page()
            other.set_content('<button onclick="window.clicks=(window.clicks||0)+1">Other</button>')
            for _ in range(4):
                other.get_by_role('button').click(timeout=5000)
            assert other.evaluate('window.clicks') == 4
            page.close()
            other.goto('data:text/html,<button onclick="window.ok=true">After</button>')
            other.get_by_role('button').click(timeout=5000)
            assert other.evaluate('window.ok') is True
        finally:
            browser.close()


@pytest.mark.browser
def test_real_persistent_context_uses_isolated_profile(tmp_path):
    with sync_playwright() as pw:
        context = launch_persistent_browser(pw.chromium, str(tmp_path / 'synthetic-profile'))
        try:
            page = context.new_page()
            page.set_content('<button onclick="window.ok=true">Go</button>')
            page.get_by_role('button').click(timeout=5000)
            assert page.evaluate('window.ok') is True
        finally:
            context.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_interrupted_navigation_keeps_other_pages_usable():
    import asyncio
    from playwright.async_api import async_playwright, Error
    async with async_playwright() as pw:
        browser = await launch_browser(pw.chromium)
        try:
            context = await browser.new_context()
            page = await context.new_page()
            survivor = await context.new_page()
            started = asyncio.Event()
            release = asyncio.Event()
            async def hold(route):
                started.set()
                await release.wait()
            await page.route('https://synthetic.invalid/held', hold)
            navigation = asyncio.create_task(page.goto('https://synthetic.invalid/held', timeout=5000))
            await asyncio.wait_for(started.wait(), timeout=5)
            await page.close()
            release.set()
            with pytest.raises(Error):
                await navigation
            await survivor.goto('data:text/html,<button onclick="window.ok=true">Go</button>')
            await survivor.get_by_role('button').click(timeout=5000)
            assert await survivor.evaluate('window.ok') is True
        finally:
            await browser.close()
