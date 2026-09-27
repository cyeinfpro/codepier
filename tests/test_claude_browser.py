"""Claude browser forms and full HTTP/Agent/worker flow; upstream is a no-network fixture."""
from contextlib import closing
import json
import os
from pathlib import Path
import sys
import time
import uuid

import pytest
from playwright.sync_api import expect, sync_playwright

from shared.native_cli import database
from shared.util import atomic_json
from tests.support import running_stack, wait_for
from tests.claude_fixture import FAKE_CLAUDE
from tests.browser_support import chat_page  # noqa: F401
from tests import test_chat_fullstack as fullstack
from tests.browser_support import event

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def claude_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('claude-fullstack')) as stack:
        stack.stop_agent()
        bindir = stack.directory / 'fake-bin'; bindir.mkdir()
        home = stack.directory / 'isolated-home'; home.mkdir()
        executable = bindir / 'claude'
        executable.write_text('#!' + sys.executable + '\n' + FAKE_CLAUDE)
        executable.chmod(0o700)
        stack.config['shell'] = {'enabled': True, 'projects': ['*'], 'inherit_env': False,
                                 'env': {'PATH': str(bindir) + os.pathsep + str(Path(sys.executable).parent) + os.pathsep + os.defpath, 'HOME': str(home)}}
        atomic_json(stack.config_path, stack.config); stack.start_agent()
        yield stack
        with closing(database(stack.directory / 'agent-state/native-cli')) as db, db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT id FROM sessions WHERE status IN ('starting','running')").fetchall():
                db.execute('INSERT INTO commands(id,session,kind,payload,created) VALUES (?,?,?,?,?)', (uuid.uuid4().hex, row['id'], 'stop', '{}', time.time()))
        def done():
            with closing(database(stack.directory / 'agent-state/native-cli')) as db:
                return not db.execute("SELECT 1 FROM sessions WHERE status IN ('starting','running','stopping')").fetchone()
        wait_for(done, 12)


def test_claude_full_panel_sse_review_mobile_history(claude_stack, tmp_path, monkeypatch):
    monkeypatch.setattr(fullstack, 'EVIDENCE', tmp_path)
    fullstack.test_full_panel_direct_chat_actual_sse_history_and_responsive_layout(claude_stack, 'claude')


@pytest.mark.parametrize('browser_name', ['chromium', 'webkit'])
def test_actual_claude_questions_approvals_models_and_resume(claude_stack, browser_name, tmp_path):
    s = claude_stack
    with sync_playwright() as pw:
        browser = getattr(pw, browser_name).launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 1000})
        context.add_cookies([{'name': c.name, 'value': c.value, 'url': s.url} for c in s.client.cookies.jar])
        page = context.new_page(); errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        try:
            page.goto(s.url + '/#native')
            expect(page.locator('#chat-compose')).to_be_visible(timeout=15000)
            page.select_option('#chat-project', s.project['id'])
            page.select_option('#chat-provider', 'claude')
            page.locator('#chat-model-picker').click()
            expect(page.locator('.chat-model-group')).to_contain_text('Claude', timeout=10000)
            page.locator('.chat-model-option[data-model="sonnet"]').click()
            page.fill('#chat-compose', 'approve');page.press('#chat-compose', 'Enter')
            allow = page.get_by_role('button', name='允许本次', exact=True)
            expect(allow).to_be_visible(timeout=15000); allow.click()
            expect(page.locator('.chat-message-assistant .chat-message-body').last).to_have_text('hello 世界', timeout=10000)
            expect(page.locator('.chat-message-assistant .chat-message-label').last).to_have_text('Claude')
            expect(page.locator('#chat-effort-select')).to_be_disabled()
            expect(page.locator('#chat-send-mode option[value="steer"]')).to_be_disabled()
            page.fill('#chat-compose', 'question');page.press('#chat-compose', 'Enter')
            expect(page.get_by_label('Choose target', exact=True)).to_be_visible(timeout=15000)
            page.get_by_label('Choose target', exact=True).select_option('B')
            page.get_by_label('Choose checks', exact=True).select_option(['Unit', 'Browser'])
            page.get_by_label('Choose target · 其他答案', exact=True).fill('Custom target')
            page.get_by_role('button', name='提交回答', exact=True).click()
            expect(page.locator('.chat-message-assistant')).to_have_count(2, timeout=10000)
            page.wait_for_function('() => !chatView().active')
            original = page.evaluate('ChatUI.selected.id')
            page.evaluate('chatAPI("stop", {id:ChatUI.selected.id,receipt:uid()})')
            page.wait_for_function('() => ["exited", "interrupted"].includes(ChatUI.selected.status)', timeout=15000)
            page.evaluate('chatResume()')
            page.wait_for_function('(old) => ChatUI.selected.id !== old', arg=original, timeout=15000)
            page.fill('#chat-compose', 'after resume');page.press('#chat-compose', 'Enter')
            expect(page.locator('.chat-message-assistant').last).to_contain_text('hello 世界', timeout=15000)
            resumed = page.evaluate('ChatUI.selected.id')
            with closing(database(s.directory / 'agent-state/native-cli')) as db:
                ids = [r[0] for r in db.execute('SELECT native_thread FROM sessions WHERE id IN (?,?)', (original, resumed))]
            assert len(ids) == 2 and ids[0] == ids[1] and ids[0]
            page.screenshot(path=str(tmp_path / (browser_name + '-claude.png')))
            assert not errors, errors
        finally:
            browser.close()


@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
def test_claude_provider_label_and_capability_switch_no_stale_steer(chat_page):
    page = chat_page
    page.select_option('#chat-provider', 'claude')
    expect(page.locator('#chat-empty p')).to_contain_text('Claude')
    page.evaluate('''() => {
      const v=chatView();v.mode='steer';
      v.catalog={models:[],capabilities:{steer:false,effort_runtime:false}};
      chatSettings({});
    }''')
    assert page.evaluate('chatView().mode') == 'followup'
    expect(page.locator('#chat-send-mode option[value="steer"]')).to_be_disabled()
    page.select_option('#chat-provider', 'codex')
    expect(page.locator('#chat-empty p')).to_contain_text('Codex')
    page.wait_for_function('() => !chatView().catalogLoading')
    expect(page.locator('#chat-send-mode option[value="steer"]')).to_be_enabled()


@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
def test_legacy_claude_history_reconciles_only_matching_message_blocks(chat_page):
    page=chat_page
    page.select_option('#chat-provider','claude')
    page.fill('#chat-compose','history');page.click('#chat-send')
    receipt=page.evaluate("requests.find(r=>r.path.endsWith('/chat_prompt')).args.receipt")
    for cursor, (kind, item_id) in enumerate([
        ('delta','msg-a:1'),('message','msg-a:0'),
        ('delta','msg-a:2'),('message','msg-a:0'),
        ('delta','msg-b:1'),('message','msg-b:0'),
    ],1):
        event(page,'chat',{'type':kind,'item_id':item_id,'receipt':receipt,'text':'same legitimate text'},cursor)
    expect(page.locator('.chat-message-assistant')).to_have_count(3)
    assert page.locator('.chat-message-assistant .chat-message-body').all_text_contents()==['same legitimate text']*3


@pytest.mark.parametrize('chat_page', ['chromium', 'webkit'], indirect=True)
@pytest.mark.parametrize('width,height',[(1440,1000),(900,700),(390,844),(320,568)])
def test_cli_composer_and_interrupt_fit_with_panel_styles(chat_page,width,height,tmp_path):
    page=chat_page
    # Reproduce the real panel cascade, including global select width:100%.
    for name in ('tokens.css','styles.css','chat.css'):
        page.add_style_tag(path=str(ROOT/'web'/name))
    page.select_option('#chat-provider','claude')
    page.set_viewport_size({'width':width,'height':height})
    page.fill('#chat-compose','hold');page.click('#chat-send')
    receipt=page.evaluate("requests.find(r=>r.path.endsWith('/chat_prompt')).args.receipt")
    event(page,'chat',{'type':'user','receipt':receipt,'text':'hold'},10)
    event(page,'chat',{'type':'tool','receipt':receipt,'tool_id':'t','name':'Bash','status':'start','text':'fixture'},20)
    page.fill('#chat-compose','第一行内容不会被圆角遮挡。\n第二行仍然可以编辑。')
    page.wait_for_function('() => document.querySelector("#chat-root").getBoundingClientRect().bottom <= innerHeight+1')
    button=page.locator('#chat-interrupt')
    expect(button).to_be_visible()
    bounds=button.bounding_box()
    assert bounds and bounds['height']<=48,bounds
    for selector in ('#chat-compose','#chat-interrupt','#chat-send'):
        box=page.locator(selector).bounding_box()
        assert box and box['x']>=0 and box['y']>=0 and box['x']+box['width']<=width+1 and box['y']+box['height']<=height+1,(selector,box)
    expect(page.locator('.chat-composer')).to_have_css('border-top-left-radius','12px')
    expect(page.locator('#chat-compose')).to_have_css('border-top-left-radius','0px')
    if width>760:
        expect(button.locator('span')).to_have_css('white-space','nowrap')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.screenshot(path=str(tmp_path/f'composer-{width}.png'))
