"""Exercise the reported HTML sinks with untrusted values in real browsers."""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.browser
@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_chat_project_draft_and_markdown_render_untrusted_text_safely(engine):
    with sync_playwright() as playwright:
        browser = getattr(playwright, engine).launch()
        try:
            page = browser.new_page()
            page.set_content(
                '<div id="buttons"></div><textarea id="chat-compose"></textarea>'
                '<div id="transcript"></div><div id="markdown"></div>'
            )
            page.add_script_tag(path=str(ROOT / "web/core/bundle.js"))
            page.add_script_tag(content="""
                const $ = CP.ui.$, esc = CP.ui.esc;
                const S = {session:{user_id:'fixture'},page:'native',projects:[]};
            """)
            for name in ("chat.js", "chat-chrome.js", "chat-markdown.js"):
                page.add_script_tag(path=str(ROOT / "web" / name))
            result = page.evaluate("""async () => {
                window.payloadExecuted = false;
                const payload = '\"><img src=x onerror="window.payloadExecuted=true">';
                document.querySelector('#buttons').innerHTML = chatProjectButtons(payload);
                chatResizeComposer = () => {};
                chatSyncChrome = () => {};
                await chatReplaceDraft(payload);
                const item = {
                    key:'fixture', kind:'user', text:payload,
                    data:{attachments:[{name:payload,mime:payload}]}, node:null, body:null,
                };
                chatCreateItem(item);
                chatUpdateItem(item);
                document.querySelector('#transcript').append(item.node);
                chatRichMarkdown(document.querySelector('#markdown'),
                    payload + '\\n\\n[bad](javascript:alert(1)) ' +
                    '[data](data:text/html,unsafe) [safe](https://example.invalid/docs)\\n\\n' +
                    '```html"><script>\\n' + payload + '\\n```');
                return {
                    payload,
                    projects:[...document.querySelectorAll('[data-project]')].map(x=>x.dataset.project),
                    draft:document.querySelector('#chat-compose').value,
                    text:item.body.textContent,
                };
            }""")
            assert result["projects"] == [result["payload"]] * 3
            assert result["draft"] == result["payload"]
            assert result["text"] == result["payload"]
            assert page.locator("#buttons img, #transcript img, #markdown img").count() == 0
            assert page.locator("#buttons script, #transcript script, #markdown script").count() == 0
            assert page.locator("#markdown a").count() == 1
            link = page.locator("#markdown a")
            assert link.get_attribute("href") == "https://example.invalid/docs"
            assert link.get_attribute("rel") == "noopener noreferrer"
            assert page.evaluate("window.payloadExecuted") is False
        finally:
            browser.close()
