"""Bounded long-room windows against real HTTP persistence and browser engines.

All room/message writes and history cursors come from the isolated Hub. Between
short bursts the fixture ages only its persisted message timestamps, modelling
elapsed minutes without weakening the production 60/minute owner limit. This is
a long-history UI test, not wall-clock soak evidence. No message projection is
intercepted, and no dot or external channel is simulated.
"""
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from tests.collaboration_support import collaboration_stack as collaboration_stack
from tests.test_room_center_browser import create_room, sign_in


pytestmark = [pytest.mark.browser, pytest.mark.integration, pytest.mark.slow]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_long_message_window_preserves_reader_and_original_history_cursors(
    collaboration_stack, chat_browser_pool, tmp_path, engine
):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': 1360, 'height': 980})
    page = context.new_page()
    errors, reads, saved = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: reads.append(request.url) if request.method == 'GET' else None)
    try:
        sign_in(page, stack)
        create_room(page, '长会话窗口 A')
        room_id = next(room['id'] for room in stack.must(stack.client.get('/api/collaboration/rooms'))['items']
                       if room['title'] == '长会话窗口 A')
        create_room(page, '草稿切换 B')

        def append_messages(count):
            for index in range(count):
                if index % 30 == 0:
                    # Only the isolated test history's time advances. Bodies,
                    # sequence IDs, revisions and original cursors stay intact.
                    with sqlite3.connect(stack.hubdir / 'hub.sqlite3', timeout=30) as db:
                        db.execute('UPDATE collaboration_messages SET created=created-61 WHERE conversation_id=?', (room_id,))
                number = len(saved) + 1
                saved.append(stack.must(stack.client.post(f'/api/collaboration/rooms/{room_id}/messages', json={
                    'body_text': f'原历史消息 {number:04d}\n这个消息保存在真实房间中。\n阅读时保留原位置。',
                    'client_message_id': uuid4().hex, 'idempotency_key': uuid4().hex,
                }))['message'])

        def wake_at_latest_receipt():
            last_id = saved[-1]['id']
            responses = []

            def collect_response(response):
                if response.status != 200 or response.url.split('?', 1)[0] != stack.url + f'/api/collaboration/rooms/{room_id}/messages':
                    return
                query = parse_qs(urlsplit(response.url).query)
                if not query.get('cursor') and not query.get('message_id'):
                    responses.append(response)

            # A response callback must not synchronously read its body: doing so
            # re-enters Playwright's event loop and can corrupt waiter cleanup.
            page.on('response', collect_response)
            try:
                page.evaluate('document.dispatchEvent(new Event("visibilitychange"))')
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    while responses:
                        response = responses.pop(0)
                        if any(message['id'] == last_id for message in response.json().get('messages', [])):
                            return
                    page.wait_for_timeout(50)
                pytest.fail(f'No latest response contained message {last_id}')
            finally:
                page.remove_listener('response', collect_response)

        append_messages(80)
        page.locator('.cr-room', has_text='长会话窗口 A').click()
        expect(page.locator('.cr-message')).to_have_count(50)
        expect(page.locator('[data-cr-older]')).to_be_visible()

        # Repeated live additions cross several server pages. The page continues
        # following the end until the owner deliberately begins reading history.
        for _ in range(6):
            page.locator('.cr-feed').evaluate('(feed)=>{feed.scrollTop=feed.scrollHeight;}')
            append_messages(25)
            wake_at_latest_receipt()
            expect(page.locator(f'[data-message-id="{saved[-1]["id"]}"]')).to_have_count(1)
            assert page.locator('.cr-message').count() <= 200
        page.locator('#cr-message').fill('这份草稿属于长会话 A，仍未发送。')
        page.evaluate("""async () => {
          const feed=document.querySelector('.cr-feed');
          const target=document.querySelectorAll('[data-message-id]')[20];
          const scrolled=new Promise(resolve=>feed.addEventListener('scroll',resolve,{once:true}));
          feed.scrollTop+=target.getBoundingClientRect().top-feed.getBoundingClientRect().top-12;
          await scrolled;
          const edge=feed.getBoundingClientRect().top;
          window.windowAnchor=[...document.querySelectorAll('[data-message-id]')].find(node=>node.getBoundingClientRect().bottom>edge+8);
          window.windowBody=windowAnchor.querySelector('.cr-message-body');
          window.windowReading={id:windowAnchor.dataset.messageId,offset:windowAnchor.getBoundingClientRect().top-edge};
          const text=document.createTreeWalker(windowBody,NodeFilter.SHOW_TEXT).nextNode();
          const range=document.createRange();range.setStart(text,0);range.setEnd(text,Math.min(8,text.length));
          const selected=getSelection();selected.removeAllRanges();selected.addRange(range);
          window.windowSelectedText=selected.toString();
        }""")
        page.wait_for_function('windowSelectedText.length > 0 && document.querySelector(".cr-feed").scrollTop > 0')

        # New server records cannot replace this visible window, DOM text node,
        # selection or draft even when the latest page becomes wholly disjoint.
        for _ in range(8):
            append_messages(25)
            wake_at_latest_receipt()
            assert page.locator('.cr-message').count() <= 200
            observed = page.evaluate("""() => {
              const node=document.querySelector('[data-message-id="'+windowReading.id+'"]');
              const feed=document.querySelector('.cr-feed');
              return {sameNode:node===windowAnchor,
                sameBody:!!node && node.querySelector('.cr-message-body')===windowBody,
                offset:node ? node.getBoundingClientRect().top-feed.getBoundingClientRect().top-windowReading.offset : null,
                selected:getSelection().toString(), expectedSelection:windowSelectedText};
            }""")
            assert observed['sameNode'], observed
            assert observed['sameBody'], observed
            assert observed['offset'] is not None and abs(observed['offset']) < 3, observed
            assert observed['selected'] == observed['expectedSelection'], observed
        expect(page.locator('[data-cr-latest]')).to_be_visible()
        expect(page.locator('#cr-message')).to_have_value('这份草稿属于长会话 A，仍未发送。')

        # The explicit history action can roll the opposite edge while retaining
        # the current reader. Invoke its actual handler without scrolling the
        # button into view first, so the anchor assertion has a stable baseline.
        first_before = page.locator('.cr-message').first.get_attribute('data-message-id')
        page.locator('[data-cr-action="older"]').evaluate('(button)=>button.click()')
        page.wait_for_function('(old)=>document.querySelector(".cr-message").dataset.messageId!==old', arg=first_before)
        assert page.locator('.cr-message').count() <= 200
        assert page.evaluate('document.querySelector(`[data-message-id="${windowReading.id}"]`)===windowAnchor')
        page.wait_for_function("""() => Math.abs(windowAnchor.getBoundingClientRect().top-
          document.querySelector('.cr-feed').getBoundingClientRect().top-windowReading.offset)<3""")

        # A large gap while another room is open is recovered with the source's
        # exact signed history cursor, not an unbounded client-side page scan.
        page.locator('.cr-room', has_text='草稿切换 B').click()
        expect(page.locator('#cr-message')).to_have_value('')
        append_messages(220)
        page.locator('.cr-room', has_text='长会话窗口 A').click()
        expect(page.locator('#cr-message')).to_have_value('这份草稿属于长会话 A，仍未发送。')
        page.wait_for_function("""() => {
          const anchor=document.querySelector('[data-message-id="'+windowReading.id+'"]');
          return anchor && Math.abs(anchor.getBoundingClientRect().top-
            document.querySelector('.cr-feed').getBoundingClientRect().top-windowReading.offset)<3;
        }""")
        anchor_id = page.evaluate('windowReading.id')
        assert any(parse_qs(urlsplit(url).query).get('message_id') == [anchor_id] for url in reads)
        expect(page.locator('[data-cr-message-window]')).to_contain_text('历史按页读取')
        expect(page.locator('[data-cr-history-end]')).to_be_visible()
        screenshots = Path(os.getenv('CODEPIER_COLLABORATION_SCREENSHOTS', str(tmp_path / 'screenshots')))
        screenshots.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(screenshots / f'{engine}-bounded-history-reading.png'), full_page=True)

        # Returning to latest is explicit. Every evicted original message remains
        # reachable through the server's unmodified pagination chain.
        page.locator('[data-cr-latest]').click()
        expect(page.locator(f'[data-message-id="{saved[-1]["id"]}"]')).to_have_count(1)
        expect(page.locator('[data-cr-history-end]')).not_to_be_visible()
        for _ in range(16):
            if page.locator(f'[data-message-id="{saved[0]["id"]}"]').count():
                break
            expect(page.locator('[data-cr-older]')).to_be_visible()
            first_before = page.locator('.cr-message').first.get_attribute('data-message-id')
            page.locator('.cr-feed').evaluate('(feed)=>{feed.scrollTop=0;}')
            page.locator('[data-cr-action="older"]').click()
            page.wait_for_function('(old)=>document.querySelector(".cr-message").dataset.messageId!==old', arg=first_before)
            assert page.locator('.cr-message').count() <= 200
        expect(page.locator(f'[data-message-id="{saved[0]["id"]}"]')).to_have_count(1)
        expect(page.locator('[data-cr-older]')).not_to_be_visible()
        expect(page.locator('#cr-message')).to_have_value('这份草稿属于长会话 A，仍未发送。')
        assert sum(bool(parse_qs(urlsplit(url).query).get('cursor')) for url in reads if f'/rooms/{room_id}/messages?' in url) >= 10
        assert errors == []
    finally:
        context.close()
