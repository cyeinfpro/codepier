"""Keep review filenames readable without changing the exact immutable path."""
import os
from pathlib import Path
import subprocess

import pytest
from playwright.sync_api import expect, sync_playwright

from tests.browser_launch import launch_browser

BASE = Path(__file__).resolve().parents[1]
LEAF = "校对 <note>.py"
PATH = "workspace/" + "long-parent-component/" * 10 + LEAF
LONG_LEAF = "x" * 180 + ".ts"


@pytest.fixture(scope="module")
def review_host(tmp_path_factory):
    output = tmp_path_factory.mktemp("narrow-review") / "host.js"
    command = (
        "const {build}=require(process.argv[1]);"
        "build({entryPoints:[process.argv[2]],outfile:process.argv[3],"
        'bundle:true,platform:"browser",format:"iife"})'
        ".catch(error=>{console.error(error);process.exitCode=1})"
    )
    subprocess.run(
        [
            "node", "-e", command,
            str(BASE / "web/mcp-apps/node_modules/esbuild/lib/main.js"),
            str(BASE / "tests/fixtures/mcp-apps-host.js"),
            str(output),
        ],
        check=True,
        timeout=30,
    )
    return output


@pytest.mark.parametrize("width", [310, 345])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_review_paths_fit_and_keep_original_for_copy(review_host, width, theme):
    with sync_playwright() as pw:
        override = os.environ.get("CODEPIER_DIFF_CHROME_EXECUTABLE")
        options = {"executable_path": override} if override else {}
        browser = launch_browser(pw.chromium, **options)
        p = browser.new_page(viewport={"width": width, "height": 800}, color_scheme=theme)
        p.set_content('<body style="margin:0"><main id="review"></main>')
        p.add_style_tag(path=str(BASE / "web/chat.css"))
        p.add_script_tag(path=str(BASE / "web/chat-panels.js"))
        p.evaluate(
            """({path, longLeaf}) => {
              window.chatReviewRequest = async (_item, {path}) => ({
                diff: path, next_offset: null,
              });
              window.chatCopyText = async text => { window.copiedPath = text; };
              for (const name of [path, longLeaf]) {
                document.getElementById('review').append(chatReviewFile({}, {
                  path: name, status: 'modified', text_diff_available: true,
                  added_lines: 120000000, removed_lines: 950000000,
                }));
              }
            }""",
            {"path": PATH, "longLeaf": LONG_LEAF},
        )
        chat = p.locator(".chat-review-file").first
        assert_path_geometry(chat, "chat-review-path")
        expect(chat.locator("summary")).to_have_attribute("title", PATH)
        chat.locator("summary").click()
        expect(chat.locator("pre")).to_have_text(PATH)
        expect(chat.locator(".chat-review-full-path code")).to_have_text(PATH)
        chat.get_by_role("button", name="复制路径").click()
        assert p.evaluate("window.copiedPath") == PATH
        assert p.locator("note").count() == 0
        assert no_horizontal_overflow(p.locator(".chat-review-file").nth(1).locator("summary"))
        assert p.evaluate("document.documentElement.scrollWidth <= innerWidth")

        p.set_content('<body style="margin:0"></body>')
        p.add_script_tag(path=str(review_host))
        p.evaluate("theme => { window.codepierHostTheme = theme; }", theme)
        requests = []
        ref = "a" * 32

        def read(params):
            arguments = params["arguments"]["options"]
            requests.append(arguments["path"])
            return {"structuredContent": {"review_ref": ref, "diff": arguments["path"]}}

        p.expose_function("codepierHostTool", read)
        p.evaluate("html => window.codepierMount(html)", (BASE / "web/mcp-apps/changes-v1.html").read_text())
        p.wait_for_function("window.codepierHostReady")
        p.evaluate(
            "values => window.codepierDeliver(values)",
            {
                "args": {"project": "Synthetic"},
                "result": {"structuredContent": {
                    "project_alias": "Synthetic", "review_ref": ref,
                    "summary": {"files": 2},
                    "files": [{"path": name, "status": "unverified"} for name in [PATH, LONG_LEAF]],
                }},
            },
        )
        app = p.frame_locator("#app-frame")
        native = app.locator(".files details").first
        expect(native.locator("summary")).to_have_attribute("title", PATH)
        expect(app.locator("html")).to_have_attribute("data-theme", theme)
        assert_path_geometry(native, "file-path")
        native.locator("summary").click()
        expect(native.locator("pre")).to_have_text(PATH)
        expect(native.locator(".file-path-controls code")).to_have_text(PATH)
        native.get_by_role("button", name="复制路径").click()
        # A sandboxed card has no clipboard permission; the fallback selects
        # the exact, original path so the host clipboard is never modified.
        expect(native.get_by_role("status")).to_have_text("已选中路径，请使用系统复制。")
        assert native.evaluate("() => window.getSelection().toString()") == PATH
        native.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {
          value: {writeText: async text => { window.copiedPath = text; }},
        })""")
        native.get_by_role("button", name="复制路径").click()
        expect(native.get_by_role("status")).to_have_text("已复制路径")
        assert native.evaluate("() => window.copiedPath") == PATH
        assert requests == [PATH]
        assert app.locator("note").count() == 0
        assert no_horizontal_overflow(app.locator(".files details").nth(1).locator("summary"))
        assert app.locator("html").evaluate("element => element.scrollWidth <= innerWidth")
        browser.close()


def no_horizontal_overflow(locator):
    return locator.evaluate("node => node.scrollWidth <= node.clientWidth + 1")


def assert_path_geometry(file, prefix):
    basename = file.locator(f".{prefix}-basename")
    folder = file.locator(f".{prefix}-folder")
    expect(basename).to_have_text(LEAF)
    assert no_horizontal_overflow(basename)
    assert folder.evaluate("node => node.scrollWidth > node.clientWidth")
    assert no_horizontal_overflow(file.locator("summary"))
