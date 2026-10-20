"""The UTF-8 budget must be visible through the real chat layout and CSS."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.browser_support import chat_page as chat_page


ROOT = Path(__file__).resolve().parents[1]


def assert_no_horizontal_overflow(page):
    sizes = page.evaluate("""() => ({
        viewport: innerWidth,
        document: document.documentElement.scrollWidth,
        body: document.body.scrollWidth,
    })""")
    assert max(sizes["document"], sizes["body"]) <= sizes["viewport"] + 1, sizes


@pytest.mark.browser
@pytest.mark.parametrize("chat_page", ["chromium", "webkit"], indirect=True)
@pytest.mark.parametrize("width,height", [(320, 568), (1440, 1000)])
def test_utf8_counter_visible_and_send_recovers_in_real_chat(chat_page, width, height):
    page = chat_page
    page.set_viewport_size({"width": width, "height": height})
    composer = page.locator("#chat-compose")
    hint = page.locator("#chat-text-limit")
    send = page.locator("#chat-send")

    expect(page.locator("#chat-root")).to_be_visible()
    expect(hint).to_be_hidden()
    assert_no_horizontal_overflow(page)

    near_limit = "x" * 63000
    composer.fill(near_limit)
    # to_be_visible inspects ancestor layout, catching a display:none on the
    # composer note even when this span's own hidden property has been cleared.
    expect(hint).to_be_visible()
    expect(hint).to_contain_text("字节")
    expect(send).to_be_enabled()
    expect(composer).to_have_attribute("aria-invalid", "false")
    expect(composer).to_have_value(near_limit)
    assert_no_horizontal_overflow(page)

    over_limit = "你" * 21846
    assert len(over_limit.encode("utf-8")) == 65538
    composer.fill(over_limit)
    expect(hint).to_be_visible()
    expect(hint).to_contain_text("消息过长")
    expect(send).to_be_disabled()
    expect(composer).to_have_attribute("aria-invalid", "true")
    expect(composer).to_have_value(over_limit)
    assert_no_horizontal_overflow(page)

    if width == 320 and page.context.browser.browser_type.name == "chromium":
        evidence = ROOT / "docs/evidence/bugfix-20261020"
        evidence.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(evidence / "chat-counter-mobile.png"), full_page=True)

    shortened = "继续处理这个任务。"
    composer.fill(shortened)
    expect(hint).to_be_hidden()
    expect(send).to_be_enabled()
    expect(composer).to_have_attribute("aria-invalid", "false")
    expect(composer).to_have_value(shortened)
    assert_no_horizontal_overflow(page)
    assert page.evaluate(r"""() => requests.filter(request =>
        /\/(start|chat_prompt|chat_steer)$/.test(request.path)
    ).length""") == 0
