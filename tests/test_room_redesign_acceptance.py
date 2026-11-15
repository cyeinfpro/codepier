"""Current room UI, real isolated Hub; managed-work projection is synthetic.

Font enlargement doubles actual computed text sizes, not a reduced viewport.
This does not claim native browser zoom, physical keyboard or screen-reader QA.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect
from tests.collaboration_support import collaboration_stack  # noqa: F401
from tests.test_room_center_browser import sign_in, create_room

pytestmark = [pytest.mark.browser, pytest.mark.integration]


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
@pytest.mark.parametrize('width,height', [(320, 740), (390, 844), (768, 420), (1440, 1000)])
def test_current_room_detail_source_recovery_keyboard_and_large_text(collaboration_stack, chat_browser_pool, engine, width, height):
    stack = collaboration_stack
    context = chat_browser_pool(engine).new_context(viewport={'width': width, 'height': height})
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    source_reads = []
    source_available = False
    writes = []
    work = {'id': 'synthetic-review-work', 'version': 1, 'state': 'blocked',
            'title': '示例工作：核对页面与保留原回执',
            'acceptance': '核对移动端、键盘返回与原消息读取后记录结果。',
            'display': {'title': '等待核对', 'reason': '原操作结果需要核对，不会重复执行。'},
            'allowed_actions': [], 'items': [], 'operations': []}
    try:
        sign_in(page, stack)
        if width <= 720:
            page.locator('[data-cr-action="rooms"]').click()
        create_room(page, '体验验收 · 示例协作房间')
        page.locator('#cr-message').fill('请核对房间界面。保留原消息与草稿，不重复发起工作。')
        page.locator('.cr-send').click()
        origin = page.locator('.cr-message').filter(has_text='请核对房间界面')
        expect(origin).to_be_visible()
        source_id = origin.get_attribute('data-message-id')
        work['original_source_message_id'] = source_id
        page.locator('#cr-message').fill('验收范围：清晰的工作进度、完整的原消息、可靠的草稿恢复。')
        page.locator('.cr-send').click()
        expect(page.locator('.cr-message')).to_have_count(2)

        def messages(route):
            if route.request.method != 'GET':
                writes.append(route.request.method)
                route.continue_()
                return
            query = parse_qs(urlsplit(route.request.url).query)
            if query.get('message_id') == [source_id]:
                source_reads.append(source_id)
                if not source_available:
                    route.fulfill(status=503, json={'error': {'message': '示例：原消息暂时无法读取'}})
                else:
                    route.continue_()
                return
            response = route.fetch()
            value = response.json()
            value['works'] = [deepcopy(work)]
            # Deliberately omit the source from this bounded window. It still
            # exists in the real Hub and is read through the original ID.
            value['messages'] = [m for m in value.get('messages', []) if m['id'] != source_id]
            route.fulfill(status=response.status, json=value)

        page.route('**/api/collaboration/rooms/*/messages?*', messages)
        page.route('**/api/collaboration/work/synthetic-review-work',
                   lambda route: route.fulfill(status=200, json={'work': deepcopy(work), 'revision': 1, 'read_only': True}))
        # A refresh merges live messages; reload creates the bounded window.
        page.reload()
        expect(page.locator('.cr-message').filter(has_text='请核对房间界面')).to_have_count(0)
        # Drafts are ephemeral: scope is panel/recovery navigation, not reload.
        page.locator('#cr-message').fill('未发送的示例草稿：下一步核对键盘返回。')
        expect(page.locator('[data-cr-work]')).to_contain_text('原操作结果需要核对')
        # Shared conversation rail: message window, work summary and composer.
        rail = page.locator('.cr-composer').bounding_box()
        bar = page.locator('[data-cr-work]').bounding_box()
        feed = page.locator('.cr-messages').bounding_box()
        assert abs(rail['x'] - bar['x']) <= 1
        assert abs(rail['width'] - bar['width']) <= 1
        assert abs(rail['x'] - feed['x']) <= 1
        assert abs(rail['width'] - feed['width']) <= 1
        assert rail['y'] + rail['height'] <= height
        assert abs((rail['x']+rail['width'])-(feed['x']+feed['width'])) <= 1
        if width <= 720:
            main = page.locator('.cr-main').bounding_box()
            assert abs(rail['x'] - main['x'] - 16) <= 1
            summary = page.locator('.cr-work-bar-summary').bounding_box()
            actions = page.locator('[data-cr-work] > span:last-child').bounding_box()
            assert actions['y'] >= summary['y'] + summary['height']
        opener = page.locator('[data-cr-work] [data-cr-action="work-detail"]')
        opener.focus()
        page.keyboard.press('Enter')
        dialog = page.locator('dialog.cr-panel')
        expect(dialog).to_be_visible()
        expect(dialog).to_have_attribute('data-panel', 'work')
        assert dialog.locator('.cr-flow-state').evaluate('node => getComputedStyle(node).textAlign') == 'left'
        heading = dialog.locator('header h2').bounding_box()
        detail = dialog.locator('.cr-flow-state').bounding_box()
        assert abs(heading['x'] - detail['x']) <= 1
        bounds = dialog.bounding_box()
        assert abs(bounds['x'] + bounds['width'] - width) <= 2, bounds
        if width <= 720:
            assert abs(bounds['y']) <= 2 and abs(bounds['height'] - height) <= 2, bounds
        for _ in range(12):
            page.keyboard.press('Tab')
            assert page.evaluate('document.querySelector("dialog.cr-panel").contains(document.activeElement)')
        page.keyboard.press('Escape')
        expect(dialog).not_to_be_visible()
        expect(opener).to_be_focused()
        expect(page.locator('#cr-message')).to_have_value('未发送的示例草稿：下一步核对键盘返回。')
        opener.click()
        page.locator('[data-cr-action="work-source"]').click()
        expect(page.locator('[data-cr-context-source]')).to_contain_text('未能读取原消息', timeout=25000)
        source_available = True
        page.locator('[data-cr-action="work-source"]').click()
        expect(page.locator('[data-cr-context-source]')).to_contain_text('请核对房间界面')
        assert len(source_reads) >= 2 and writes == []
        dialog.locator('[data-cr-action="work-detail"]').click()
        expect(dialog).to_have_attribute('data-panel', 'work')
        # Real text enlargement: freeze each computed font size then double it.
        factor = page.locator('.cr-panel-content').evaluate('''node => {
          const items=[node,...node.querySelectorAll('*')];
          const sizes=items.map(item=>parseFloat(getComputedStyle(item).fontSize));
          const before=parseFloat(getComputedStyle(node).fontSize);
          items.forEach((item,i)=>item.style.fontSize=(sizes[i]*2)+'px');
          return parseFloat(getComputedStyle(node).fontSize)/before;
        }''')
        assert factor == 2
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert dialog.evaluate('(node)=>node.scrollWidth <= node.clientWidth+1')
        close = dialog.locator('[data-cr-action="close"]')
        expect(close).to_be_visible()
        close.focus()
        box = close.bounding_box()
        assert 0 <= box['y'] < height and box['y'] + box['height'] <= height
        close.click()
        expect(page.locator('#cr-message')).to_have_value('未发送的示例草稿：下一步核对键盘返回。')
        # Deliver fresh current-room composition, not an archived UI fixture.
        evidence = Path(__file__).resolve().parents[1] / 'docs/evidence/ui-redesign-20261115/room-alignment-06'
        evidence.mkdir(parents=True, exist_ok=True)
        root = Path(__file__).resolve().parents[1]
        sources = sorted((root / 'web/collaboration-room').rglob('*'))
        record = {'captured_at': datetime.now(timezone.utc).isoformat(),
                  'fixture': 'isolated current Hub; synthetic work projection; real original message read',
                  'sources_sha256': {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources if p.is_file()}}
        (evidence / f'{engine}-{width}x{height}-sources.json').write_text(json.dumps(record, ensure_ascii=False, indent=2))
        expect(dialog).not_to_be_visible()
        page.screenshot(path=str(evidence / f'{engine}-{width}x{height}-conversation.png'), full_page=False, animations='disabled')
        opener.click()
        expect(dialog).to_be_visible()
        expect(dialog.locator('[data-cr-work-view]')).to_contain_text('原操作结果需要核对')
        dialog.evaluate('async node => { await Promise.all(node.getAnimations({subtree:true}).map(a => a.finished.catch(() => {}))); }')
        page.wait_for_function('''() => {
          const d=document.querySelector('dialog.cr-panel'), r=d.getBoundingClientRect();
          return d.open && r.x >= -1 && r.y >= -1 && r.right <= innerWidth+1 && r.bottom <= innerHeight+1;
        }''')
        page.screenshot(path=str(evidence / f'{engine}-{width}x{height}-work-detail.png'), full_page=False, animations='disabled')
        assert not errors, errors
    finally:
        context.close()
