"""Real browser/MCP/Hub tests of a passive dot relay, not native-model reasoning."""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from hub.store import Store
from tests.collaboration_support import collaboration_stack as collaboration_stack, key
from tests.test_collaboration_delivery_flow import login, scope
from tests.test_collaboration_dot_chat_flow import refresh
from tests.test_collaboration_dots_flow import join_dot, call, mcp
from tests.test_collaboration_join_browser import fixture_host

pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


def prepare(stack, page):
    login(page, stack)
    dot = stack.must(stack.client.post('/api/collaboration/dot', json={**scope(stack), 'duplex': True,
        'label': '项目 dot', 'capabilities': ['read'], 'confirm_tasks': True, 'idempotency_key': key()}))['dot']
    joined = join_dot(stack, dot)
    fixture_host(stack, joined['slot'], joined['subscription_requests'])
    refresh(page)
    expect(page.locator('.collaboration')).to_have_class(__import__('re').compile('cc-relay-focus'))
    page.locator('#cc-message-input').fill('@项目')
    page.locator('#cc-message-input').press('Enter')
    expect(page.locator('.cc-mention-chip')).to_contain_text('@项目 dot')
    return joined


def send(page, text, settle=True):
    area = page.locator('#cc-message-input')
    area.fill(text)
    page.locator('#cc-command button[type="submit"]').click()
    expect(area).to_have_value('')
    if settle:
        expect(page.locator('#cc-relay-outbox [data-pending-id]')).to_have_count(0, timeout=15000)


def no_work(stack):
    store=Store(stack.hubdir)
    try:
        assert store.one('SELECT COUNT(*) AS n FROM delegation_requests')['n'] == 0
        assert store.one('SELECT COUNT(*) AS n FROM coordination_work')['n'] == 0
    finally:
        store.close()


@pytest.mark.parametrize('engine,width',[('chromium',1440),('webkit',390)])
def test_send_is_nonblocking_lost_ack_does_not_duplicate_and_followups_keep_topic(collaboration_stack,chat_browser_pool,engine,width):
    stack=collaboration_stack
    context=chat_browser_pool(engine).new_context(viewport={'width':width,'height':920})
    page=context.new_page()
    errors=[]
    page.on('pageerror',lambda e: errors.append(str(e)))
    held,posts=[],[]
    try:
        joined=prepare(stack,page)
        expect(page.locator('.cc-tabs')).not_to_be_visible()
        expect(page.locator('.cc-context-rail')).not_to_be_visible()
        def intercept(route):
            posts.append(route.request.post_data_json)
            if not held:
                response=route.fetch()
                held.append((route,response))
            else:
                route.continue_()
        page.route('**/api/collaboration/message',intercept)
        send(page,'第一条，先讨论，不用执行。',settle=False)
        expect(page.locator('#cc-message-input')).to_be_enabled()
        expect(page.locator('#cc-command button[type="submit"]')).to_be_enabled()
        expect(page.locator('.collaboration')).not_to_have_attribute('aria-busy','true')
        send(page,'补充一句，手机端优先。',settle=False)
        assert held
        held[0][0].abort('failed')  # server committed, browser did not receive the POST response
        expect(page.locator('#cc-relay-outbox [data-pending-id]')).to_have_count(0,timeout=15000)
        messages=call(stack,joined['inbox_request'])['items']
        assert len(messages)==2
        assert len(posts)==2 and posts[0]['client_message_id']!=posts[1]['client_message_id']
        assert messages[1]['message']['thread_root_id']==messages[0]['message_id']
        assert all(not p.get('delegation') and not p.get('dispatch_mode') for p in posts)
        expect(page.locator('.cc-message.is-owner')).to_have_count(2)
        expect(page.locator('.cc-message.is-owner .cc-rich-text').first).to_have_text('第一条，先讨论，不用执行。')
        page.locator('[data-cc-action="relay-new-topic"]').click()
        send(page,'这是一个新的话题。')
        messages=call(stack,joined['inbox_request'])['items']
        assert len(messages)==3 and messages[2]['message']['thread_root_id']==messages[2]['message_id']
        no_work(stack)
        assert not errors,errors
    finally:
        context.close()


@pytest.mark.parametrize('engine,width',[('chromium',1440),('webkit',390)])
def test_incremental_reply_rich_text_copy_focus_and_plain_followup(collaboration_stack,chat_browser_pool,engine,width):
    stack=collaboration_stack
    context=chat_browser_pool(engine).new_context(viewport={'width':width,'height':920})
    page=context.new_page()
    errors=[]
    page.on('pageerror',lambda e: errors.append(str(e)))
    try:
        joined=prepare(stack,page)
        send(page,'请说明方案，用代码块和表格，先不要改文件。')
        inbound=call(stack,joined['inbox_request'])['items'][0]
        reply=mcp(stack,'collaboration',{'action':'dot_message',**inbound['interim_reply_arguments'],
            'body_text':'## 正在整理\n\n先核对方案。'})
        card=page.locator(f'[data-message-id="{reply["message"]["id"]}"]')
        expect(card.locator('h3')).to_have_text('正在整理',timeout=15000)
        page.locator('#cc-message-input').fill('这是仍在输入的补充草稿')
        focused=page.locator('#cc-message-input')
        focused.evaluate('(e)=>e.setSelectionRange(3,6)')
        body='## 检查建议\n\n**先保持原样**，再核对。\n\n```python\nprint("safe <script>")\n```\n\n| 项目 | 建议 |\n| --- | --- |\n| 手机端 | 优先 |\n\n<script>window.relayXss=1</script>\n\n[危险](javascript:alert(1))\n![远程图片](https://example.invalid/private.png)'
        latest=mcp(stack,'collaboration',{'action':'dot_update',**reply['update_arguments'],
            'body_text':body,'complete':False})
        expect(card.locator('h3')).to_have_text('检查建议',timeout=15000)
        expect(card.locator('pre code')).to_have_text('print("safe <script>")')
        expect(card.locator('table')).to_have_count(1)
        assert page.evaluate('window.relayXss') is None
        expect(card.locator('script,img,a[href^="javascript:"]')).to_have_count(0)
        expect(focused).to_have_value('这是仍在输入的补充草稿')
        expect(focused).to_be_focused()
        assert focused.evaluate('(e)=>[e.selectionStart,e.selectionEnd]')==[3,6]
        # Unchanged status polling must not replace the rich DOM or open controls.
        rich=card.locator('[data-rich-message]').element_handle()
        finalized=mcp(stack,'collaboration',{'action':'dot_update',**latest['update_arguments'],
            'body_text':body+'\n\n整理完成，可继续讨论。','complete':True})
        expect(card).to_contain_text('整理完成，可继续讨论。',timeout=15000)
        assert rich.evaluate('(e)=>e.isConnected')
        expect(page.locator(f'[data-message-id="{inbound["message_id"]}"] .cc-dot-receipt')).to_have_text('已回复',timeout=15000)
        expect(page.locator(f'[data-message-id="{inbound["message_id"]}"] .cc-message-deliveries')).to_have_count(0)
        page.evaluate("""()=>Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{window.relayCopied=text;}}})""")
        card.locator('[data-cc-action="relay-copy"]').click()
        expect(card.locator('[data-cc-action="relay-copy"]')).to_have_text('已复制')
        assert page.evaluate('window.relayCopied')==finalized['message']['body_text']
        send(page,'继续解释一下即可。')
        followup=call(stack,joined['inbox_request'])['items'][0]
        assert followup['message']['thread_root_id']==inbound['message_id']
        no_work(stack)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        directory=Path('.work/dot-refactor/screenshots');directory.mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(directory/f'relay-rich-{engine}-{width}.png'),full_page=True)
        page.locator('[data-cc-action="relay-management"]').click()
        expect(page.locator('.cc-tabs')).to_be_visible()
        page.locator('[data-cc-action="relay-management"]').click()
        expect(page.locator('.cc-tabs')).not_to_be_visible()
        assert not errors,errors
    finally:
        context.close()


def test_offline_queue_recovers_without_persisting_prose_and_keeps_next_draft(collaboration_stack,chat_browser_pool):
    stack=collaboration_stack
    context=chat_browser_pool('chromium').new_context(viewport={'width':1440,'height':920})
    page=context.new_page()
    try:
        joined=prepare(stack,page)
        context.set_offline(True)
        send(page,'私有草稿内容，恢复后发送一次。',settle=False)
        expect(page.locator('#cc-relay-outbox')).to_contain_text('私有草稿内容')
        page.locator('#cc-message-input').fill('第二段还没有发送')
        assert not page.evaluate("""()=>[...Object.values(localStorage),...Object.values(sessionStorage)].some(s=>s.includes('私有草稿内容')||s.includes('第二段还没有发送'))""")
        context.set_offline(False)
        page.evaluate("window.dispatchEvent(new Event('online'))")
        expect(page.locator('#cc-relay-outbox [data-pending-id]')).to_have_count(0,timeout=15000)
        expect(page.locator('#cc-message-input')).to_have_value('第二段还没有发送')
        assert len(call(stack,joined['inbox_request'])['items'])==1
        no_work(stack)
    finally:
        context.close()


def test_late_saved_response_cannot_restore_messages_after_access_recovery(collaboration_stack,chat_browser_pool):
    stack=collaboration_stack
    context=chat_browser_pool('chromium').new_context(viewport={'width':1440,'height':920})
    page=context.new_page()
    held=[]
    try:
        prepare(stack,page)
        def hold(route):
            response=route.fetch()
            held.append((route,response))
        page.route('**/api/collaboration/message',hold)
        page.route('**/api/collaboration/conversations',lambda r:r.fulfill(status=200,json={'items':[]}))
        send(page,'AUTH-FENCE-PRIVATE-MESSAGE',settle=False)
        expect(page.locator('#cc-relay-outbox')).to_contain_text('AUTH-FENCE-PRIVATE-MESSAGE')
        page.route('**/api/collaboration/relay-sync?*',lambda r:r.fulfill(status=403,json={'error':{'code':'FORBIDDEN','message':'test access revoked'}}))
        expect(page.locator('#cc-feedback')).to_contain_text('旧记录已清除',timeout=15000)
        assert held
        held[0][0].fulfill(response=held[0][1])
        # Pump browser until the original response settles, then check the real DOM.
        page.wait_for_function("() => !document.querySelector('#cc-relay-outbox [data-pending-id]')")
        expect(page.locator('.cc-message-list')).not_to_contain_text('AUTH-FENCE-PRIVATE-MESSAGE')
        expect(page.locator('#cc-message-input')).to_be_disabled()
    finally:
        context.close()


def test_empty_success_receipt_is_reconciled_by_original_client_id(collaboration_stack,chat_browser_pool):
    stack=collaboration_stack
    context=chat_browser_pool('chromium').new_context(viewport={'width':1440,'height':920})
    page=context.new_page()
    posts=[]
    try:
        joined=prepare(stack,page)
        def lose_body(route):
            posts.append(route.request.post_data_json)
            route.fetch()
            route.fulfill(status=200,json={})
        page.route('**/api/collaboration/message',lose_body)
        send(page,'Malformed acknowledgement still means one message.')
        assert len(posts)==1
        messages=call(stack,joined['inbox_request'])['items']
        assert len(messages)==1 and messages[0]['message']['client_message_id']==posts[0]['client_message_id']
        expect(page.locator('.cc-message.is-owner .cc-rich-text')).to_have_text('Malformed acknowledgement still means one message.')
        no_work(stack)
    finally:
        context.close()


def test_single_dot_is_visible_default_but_explicit_clear_is_respected(collaboration_stack,chat_browser_pool):
    stack=collaboration_stack
    context=chat_browser_pool('chromium').new_context(viewport={'width':1440,'height':920})
    page=context.new_page()
    try:
        login(page,stack)
        dot=stack.must(stack.client.post('/api/collaboration/dot',json={**scope(stack),'duplex':True,
            'label':'唯一 dot','capabilities':['read'],'confirm_tasks':True,'idempotency_key':key()}))['dot']
        joined=join_dot(stack,dot)
        fixture_host(stack,joined['slot'],joined['subscription_requests'])
        refresh(page)
        expect(page.locator('.cc-mention-chip')).to_have_text('@唯一 dot ×')
        send(page,'不用先打 @，直接开始讨论。')
        assert len(call(stack,joined['inbox_request'])['items'])==1
        page.locator('[data-cc-action="dot-clear"]').click()
        refresh(page)
        expect(page.locator('.cc-mention-chip')).to_have_count(0)
        page.locator('#cc-message-input').fill('这是一条不发给 dot 的独立记录。')
        page.locator('#cc-command button[type="submit"]').click()
        expect(page.locator('#cc-message-input')).to_have_value('')
        assert len(call(stack,joined['inbox_request'])['items'])==1
        no_work(stack)
    finally:
        context.close()


def test_failed_message_does_not_block_an_independent_new_topic(collaboration_stack,chat_browser_pool):
    stack=collaboration_stack
    context=chat_browser_pool('chromium').new_context(viewport={'width':1440,'height':920})
    page=context.new_page()
    try:
        joined=prepare(stack,page)
        def reject_one(route):
            if route.request.post_data_json['body_text']=='Only this message is rejected.':
                route.fulfill(status=422,json={'error':{'code':'INVALID_ARGUMENTS','message':'test single message rejected'}})
            else:
                route.continue_()
        page.route('**/api/collaboration/message',reject_one)
        send(page,'Only this message is rejected.',settle=False)
        expect(page.locator('[data-cc-action="relay-retry"]')).to_be_visible()
        page.locator('[data-cc-action="relay-new-topic"]').click()
        send(page,'A separate valid discussion.',settle=False)
        expect(page.locator('.cc-message.is-owner .cc-rich-text')).to_have_text('A separate valid discussion.',timeout=10000)
        expect(page.locator('#cc-relay-outbox [data-pending-id]')).to_have_count(1)
        expect(page.locator('#cc-relay-outbox')).to_contain_text('Only this message is rejected.')
        items=call(stack,joined['inbox_request'])['items']
        assert len(items)==1 and items[0]['message']['thread_root_id']==items[0]['message_id']
        no_work(stack)
    finally:
        context.close()
