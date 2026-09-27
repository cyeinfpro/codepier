"""Interaction regression tests for the 2026-09-15 conversation window.
Browser rendering is real. This component suite mocks only native HTTP/SSE.
Actual HTTP/Hub/Agent workers are exercised by test_chat_fullstack.py.
"""
from pathlib import Path
import json
import re
import pytest
from playwright.sync_api import expect
from tests.browser_support import chat_page, event
from tests.test_chat_complete_browser import assert_composer_controls_fit, send

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/evidence/cli-polish-20260915'
OUT.mkdir(parents=True,exist_ok=True)


def test_production_boot_cannot_load_the_retired_terminal(stack, chat_browser_pool):
    index=(ROOT/'web/index.html').read_text()
    app=(ROOT/'web/app.js').read_text()
    chat=(ROOT/'web/chat.js').read_text()
    for term in ['xterm','addon-fit','addon-search','/native-cli.js','/native-cli.css']:
        assert term not in index
    assert "['terminal','terminal'" not in app
    page=chat_browser_pool('chromium').new_page()
    try:
        page.goto(stack.url+'/#terminal',wait_until='domcontentloaded')
        expect(page.locator('#login-form')).to_be_visible()
        page.fill('#username', 'admin');page.locator('#password').fill(stack.password)
        page.locator('#login-form button[type=submit]').click()
        expect(page.locator('#chat-root')).to_be_visible()
        assert page.evaluate('S.page')=='native'
        assert page.evaluate('location.hash')=='#native'
    finally:
        page.context.close()
    assert '高级终端' not in chat and 'data-nav="terminal"' not in chat
    assert not (ROOT/'web/native-cli.js').exists()
    assert not (ROOT/'web/native-cli.css').exists()
    for name in ['chat.js','chat-panels.js']:
        text=(ROOT/'web'/name).read_text()
        assert not re.search(r'(?<![\w.])(?:prompt|confirm)\(',text)


def test_switch_keeps_window_composer_nodes_and_tool_disclosure(chat_page):
    p=chat_page;r=send(p)['args']['receipt']
    event(p,'chat',{'type':'user','receipt':r,'text':'Review the project'},10)
    event(p,'chat',{'type':'tool','receipt':r,'tool_id':'read1','name':'read','status':'completed','text':'Read src/upload.ts'},20)
    p.locator('.chat-process > summary').click()
    p.locator('.chat-message-tool summary').click()
    p.fill('#chat-compose','Keep selection and draft')
    p.evaluate("window.savedRow=ChatUI.selected;window.windowNode=$('#chat-root');window.editorNode=$('#chat-compose');window.toolNode=$('.chat-message-tool');$('#chat-compose').setSelectionRange(5,14)")
    p.click('#chat-new');p.locator('#chat-project-results button:not(:disabled)').first.click()
    assert p.evaluate("windowNode===$('#chat-root')&&editorNode===$('#chat-compose')")
    p.fill('#chat-compose','A different task')
    p.evaluate('chatSwitch(savedRow)')
    expect(p.locator('#chat-compose')).to_have_value('Keep selection and draft')
    expect(p.locator('.chat-message-tool details')).to_have_attribute('open','')
    assert p.evaluate("toolNode===$('.chat-message-tool')")
    assert p.evaluate("[$('#chat-compose').selectionStart,$('#chat-compose').selectionEnd]")==[5,14]
    assert 'cursor=20' in p.evaluate('streams.at(-1).url')


def test_refresh_history_retains_focused_row_and_scroll(chat_page):
    p=chat_page
    p.evaluate("window.rows=Array.from({length:35},(_,i)=>({id:'s'+i,mode:'chat',provider:'pi',title:'Conversation '+i,status:'running'}));chatList()")
    expect(p.locator('.chat-session')).to_have_count(35)
    p.locator('[data-session-id="s15"]').focus()
    p.evaluate("window.focused=$('[data-session-id=\"s15\"]');window.historyY=$('#chat-history').scrollTop")
    p.evaluate('chatList()')
    assert p.evaluate('document.activeElement===focused')
    assert p.evaluate("$('#chat-history').scrollTop")==p.evaluate('historyY')


def test_rich_streaming_preserves_selection_and_existing_blocks(chat_page):
    p=chat_page;r=send(p)['args']['receipt']
    initial='## Upload review\n\nThe **queue** keeps your selection'
    event(p,'chat',{'type':'delta','receipt':r,'text':initial},10)
    expect(p.locator('.chat-message-assistant strong')).to_have_text('queue')
    p.evaluate("""() => {
      window.richP=$('.chat-message-assistant p');window.strongNode=richP.querySelector('strong');
      const selection=window.getSelection(),range=document.createRange();range.selectNodeContents(strongNode);selection.removeAllRanges();selection.addRange(range);
      window.headingNode=$('.chat-message-assistant h3');
    }""")
    event(p,'chat',{'type':'delta','receipt':r,'text':' while new text arrives.'},20)
    expect(p.locator('.chat-message-assistant p')).to_contain_text('arrives')
    assert p.evaluate("richP===$('.chat-message-assistant p')&&strongNode===$('.chat-message-assistant strong')&&headingNode===$('.chat-message-assistant h3')")
    assert p.evaluate('getSelection().toString()')=='queue'
    event(p,'chat',{'type':'delta','receipt':r,'text':'\n\n| File | Status |\n| --- | --- |\n| queue.ts | ready |'},30)
    expect(p.locator('.chat-message-assistant table td').first).to_have_text('queue.ts')
    assert p.evaluate("headingNode===$('.chat-message-assistant h3')")


def test_streaming_does_not_take_reader_back_to_bottom(chat_page):
    p=chat_page;r=send(p)['args']['receipt']
    text='\n\n'.join('Paragraph '+str(i)+' — readable history. '*8 for i in range(40))
    event(p,'chat',{'type':'delta','receipt':r,'text':text},10)
    expect(p.locator('.chat-message-assistant p')).to_have_count(40)
    p.evaluate("$('#chat-scroll').scrollTop=140")
    p.wait_for_timeout(30)
    before=p.evaluate("$('#chat-scroll').scrollTop")
    event(p,'chat',{'type':'delta','receipt':r,'text':' This arrives while you read.'},20)
    expect(p.locator('.chat-message-assistant p').last).to_contain_text('arrives')
    assert abs(p.evaluate("$('#chat-scroll').scrollTop")-before)<2
    expect(p.locator('#chat-latest')).to_be_visible()
    p.click('#chat-latest')
    assert p.evaluate('chatAtBottom()')


def test_slash_enter_completes_without_sending_a_prompt(chat_page):
    p=chat_page;p.fill('#chat-compose','/mod')
    expect(p.locator('#chat-slash')).to_be_visible()
    p.press('#chat-compose','Enter')
    expect(p.locator('#chat-compose')).to_have_value('/model ')
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/start'))")
    p.press('#chat-compose','Enter')
    expect(p.locator('#chat-model-search')).to_be_visible()
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/start'))")


def test_palette_and_model_picker_are_keyboard_operable(chat_page):
    p=chat_page;p.focus('#chat-compose');p.keyboard.press('Control+k')
    expect(p.get_by_role('dialog',name='快捷操作')).to_be_visible()
    p.get_by_role('searchbox',name='搜索操作与会话').fill('选择模型')
    p.keyboard.press('Enter')
    expect(p.locator('#chat-model-search')).to_be_focused()
    p.fill('#chat-model-search','Advanced');p.keyboard.press('Enter')
    expect(p.locator('#chat-model-name')).to_have_text('Advanced Model')
    expect(p.locator('#chat-model-picker')).to_be_focused()
    p.click('#chat-model-picker');p.keyboard.press('Escape')
    expect(p.locator('#chat-popover')).to_be_hidden()
    expect(p.locator('#chat-model-picker')).to_be_focused()
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/start'))")


def test_rename_dialog_does_not_block_stream_and_requires_explicit_save(chat_page):
    p=chat_page;r=send(p)['args']['receipt'];dialogs=[]
    p.on('dialog',lambda d:(dialogs.append(d.type),d.dismiss()))
    p.locator('.chat-overflow summary').click();p.click('#chat-rename')
    expect(p.get_by_role('dialog',name='重命名对话')).to_be_visible()
    event(p,'chat',{'type':'delta','receipt':r,'text':'The stream is still alive.'},10)
    expect(p.locator('.chat-message-assistant')).to_contain_text('still alive')
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/rename'))")
    p.fill('#chat-dialog-input','上传链路 · 审查');p.click('#chat-dialog-confirm')
    expect(p.locator('#chat-title')).to_have_text('上传链路 · 审查')
    assert p.evaluate("requests.find(r=>r.path.endsWith('/rename')).args.title")=='上传链路 · 审查'
    assert not dialogs


def test_switch_cancels_pending_confirmation_without_wrong_project_write(chat_page):
    p=chat_page;send(p)
    p.evaluate('void chatStop()')
    expect(p.get_by_role('dialog',name='停止会话？')).to_be_visible()
    p.evaluate("chatSwitch(null,'p2')")
    expect(p.locator('.chat-sheet')).to_have_count(0)
    assert p.evaluate('ChatUI.project')=='p2'
    assert not p.evaluate("requests.some(r=>r.path.endsWith('/stop'))")
    expect(p.locator('#chat-compose')).to_be_enabled()


@pytest.mark.parametrize('width,height',[(1440,1000),(1024,768),(390,844),(320,568),(667,375)])
def test_layers_follow_actual_composer_and_viewport(chat_page,width,height):
    p=chat_page;p.set_viewport_size({'width':width,'height':height})
    p.fill('#chat-compose','A multiline draft\nsecond line\nthird line\nfourth line')
    p.click('#chat-model-picker')
    expect(p.locator('#chat-model-search')).to_be_visible()
    p.wait_for_timeout(180)
    popup=p.locator('#chat-popover').bounding_box();anchor=p.locator('#chat-model-picker').bounding_box()
    assert popup['x']>=0 and popup['y']>=0
    assert popup['x']+popup['width']<=width+1 and popup['y']+popup['height']<=height+1
    assert popup['y']+popup['height']<=anchor['y']+2 or popup['y']>=anchor['y']+anchor['height']-2
    p.screenshot(path=str(OUT/f'anchored-model-{width}.png'))
    p.press('#chat-model-search','Escape')
    assert_composer_controls_fit(p,width,height)
    assert p.evaluate('document.documentElement.scrollWidth<=innerWidth+1')


@pytest.mark.parametrize('appearance',['light','dark'])
def test_neutral_appearance_and_reduced_motion(chat_page,appearance):
    p=chat_page;p.select_option('#chat-appearance',appearance)
    expect(p.locator('#chat-root')).to_have_attribute('data-appearance',appearance)
    bg=p.evaluate("getComputedStyle($('#chat-root')).backgroundColor")
    assert bg==('rgb(255, 255, 255)' if appearance=='light' else 'rgb(34, 34, 34)')
    p.emulate_media(reduced_motion='reduce')
    p.click('#chat-model-picker')
    assert p.evaluate("getComputedStyle($('#chat-popover')).animationName")=='none'
    p.press('#chat-model-search','Escape')
    p.screenshot(path=str(OUT/f'window-{appearance}-desktop.png'))
    p.set_viewport_size({'width':390,'height':844})
    p.screenshot(path=str(OUT/f'window-{appearance}-mobile.png'))


def test_session_settings_confirmation_recovers_after_switch(chat_page):
    p=chat_page;r=send(p)['args']['receipt']
    event(p,'chat',{'type':'user','receipt':r,'text':'Review'},10)
    event(p,'chat',{'type':'done','receipt':r,'status':'completed'},20)
    p.evaluate("window.receiptState='queued';window.savedRow=ChatUI.selected")
    p.select_option('#chat-effort-select','high')
    expect(p.locator('#chat-setting-state')).to_contain_text('切换')
    p.click('#chat-new');p.locator('#chat-project-results button:not(:disabled)').first.click();p.evaluate("window.receiptState='completed';chatSwitch(savedRow)")
    expect(p.locator('#chat-settings-retry')).to_be_hidden()
    p.wait_for_function('chatView().settingOp===null',timeout=4000)
    expect(p.locator('#chat-effort-select')).to_be_enabled()


def test_old_errors_do_not_overwrite_new_view_and_new_errors_are_visible(chat_page):
    p=chat_page;p.select_option('#chat-project','p2')
    p.fill('#chat-compose','Keep the new project draft')
    p.evaluate('window.catalogFailure=true')
    p.click('#chat-model-picker');p.click('#chat-model-refresh')
    expect(p.locator('#chat-catalog-notice')).to_contain_text('offline')
    expect(p.locator('#chat-compose')).to_have_value('Keep the new project draft')
    assert p.evaluate('ChatUI.project')=='p2'


def test_long_markdown_stream_mutation_profile(chat_page):
    p=chat_page;r=send(p)['args']['receipt']
    baseline='\n\n'.join('## Section '+str(i)+'\n\nVerified **content** and `code`.' for i in range(80))
    event(p,'chat',{'type':'delta','receipt':r,'text':baseline},10)
    expect(p.locator('.chat-message-assistant h3')).to_have_count(80)
    metrics=p.evaluate("""() => {
      const holder=$('.chat-message-assistant .chat-message-body'),first=holder.firstChild;
      let source=chatView().items.get(chatView().order.at(-1)).text,times=[];
      for(let i=0;i<120;i++){source+=' incremental';const t=performance.now();chatRichMarkdown(holder,source);times.push(performance.now()-t);}
      times.sort((a,b)=>a-b);
      return {samples:times.length,characters:source.length,p50_ms:times[59],p95_ms:times[113],max_ms:times[119],prefix_node_preserved:first===holder.firstChild};
    }""")
    assert metrics['prefix_node_preserved']
    (OUT/'stream-render-profile.json').write_text(json.dumps(metrics,indent=2))


def test_resize_preserves_history_preference_and_focus(chat_page):
    p=chat_page
    p.focus('#chat-compose')
    p.set_viewport_size({'width':390,'height':844})
    p.wait_for_timeout(50)
    p.set_viewport_size({'width':1440,'height':1000})
    p.wait_for_timeout(50)
    expect(p.locator('.chat-sidebar')).to_be_visible()
    expect(p.locator('#chat-compose')).to_be_focused()
    assert not p.evaluate("$('.chat-main').inert")
    p.click('#chat-history-toggle')
    expect(p.locator('.chat-sidebar')).to_be_hidden()
    p.focus('#chat-compose')
    p.set_viewport_size({'width':390,'height':844})
    p.wait_for_timeout(50)
    p.set_viewport_size({'width':1440,'height':1000})
    p.wait_for_timeout(50)
    expect(p.locator('.chat-sidebar')).to_be_hidden()
    expect(p.locator('#chat-compose')).to_be_focused()
    assert p.evaluate('ChatUI.historyCollapsed') is True


def test_theme_switch_is_atomic_for_composer_and_window(chat_page):
    p=chat_page
    result=p.evaluate("""() => {
      chatApplyAppearance('dark');
      const dark=[getComputedStyle($('#chat-root')).backgroundColor,getComputedStyle($('.chat-composer')).backgroundColor];
      chatApplyAppearance('light');
      const light=[getComputedStyle($('#chat-root')).backgroundColor,getComputedStyle($('.chat-composer')).backgroundColor];
      return {dark,light};
    }""")
    assert result['dark']==['rgb(34, 34, 34)','rgb(43, 43, 43)']
    assert result['light']==['rgb(255, 255, 255)']*2


@pytest.mark.parametrize('chat_page',['chromium','webkit'],indirect=True)
@pytest.mark.parametrize('width,height',[(320,568),(390,844),(667,375)])
def test_resize_does_not_turn_focus_scroll_into_extra_window_height(chat_page,width,height):
    p=chat_page;p.set_viewport_size({'width':width,'height':height})
    p.fill('#chat-compose','First line\nSecond line\nThird line\nFourth line')
    # Present the actual negative DOM coordinate produced by focus scrolling.
    # WebKit can decline scrollTo on an overflow-hidden document or service a
    # pending resize between test calls. A temporary relative offset exercises
    # the same measured input synchronously without depending on either race.
    before=p.evaluate("""() => {
        const root=document.querySelector('#chat-root');
        root.style.height=(innerHeight+300)+'px';root.style.top='-300px';
        const before=root.getBoundingClientRect().top;
        chatViewport();root.style.top='';window.scrollTo(0,0);
        return before;
    }""")
    assert before<0
    assert p.locator('#chat-root').bounding_box()['height']<=height+1
    p.click('#chat-model-picker');p.press('#chat-model-search','Escape')
    assert_composer_controls_fit(p,width,height)
