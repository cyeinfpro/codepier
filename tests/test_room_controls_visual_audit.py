"""Read-only expanded control inspection against an isolated current Hub."""
from pathlib import Path
import os
import json
import pytest
from playwright.sync_api import expect
from tests.collaboration_support import collaboration_stack
from tests.test_room_center_browser import sign_in, create_room

@pytest.mark.parametrize('width,height', [(320,740),(390,844),(768,420),(1440,1000)])
def test_room_controls_visual_audit(collaboration_stack,chat_browser_pool,width,height):
    context=chat_browser_pool('chromium').new_context(viewport={'width':width,'height':height})
    page=context.new_page()
    evidence=Path(os.getenv('CODEPIER_ROOM_CONTROLS_DIR', 'docs/evidence/ui-redesign-20261115/room-controls-after-01'))
    evidence.mkdir(parents=True,exist_ok=True)
    try:
        sign_in(page,collaboration_stack)
        if width<721: page.locator('[data-cr-action="rooms"]').click()
        create_room(page,'控件审美核验 · 隔离示例')
        page.locator('.cr-recipient select').focus()
        page.screenshot(path=str(evidence/f'{width}-recipient.png'))
        records=[]
        for action in ['settings','context','diagnostics','members','add-member','permissions','weixin']:
            if action in ['context','diagnostics']:
                page.locator('.cr-topbar [data-cr-action="settings"]').click()
                page.locator(f'dialog [data-cr-action="{action}"]').click()
            elif action == 'add-member':
                page.locator('.cr-topbar [data-cr-action="members"]').click()
                page.locator('dialog [data-cr-action="add-member"]').click()
            else:
                page.locator(f'.cr-topbar [data-cr-action="{action}"]').click()
            dialog=page.locator('dialog.cr-panel')
            expect(dialog).to_be_visible()
            dialog.evaluate('async n=>await Promise.all(n.getAnimations({subtree:true}).map(a=>a.finished.catch(()=>{})))')
            if action == 'permissions':
                levels=dialog.locator('[data-permission-level]:visible:enabled')
                if levels.count(): levels.first.select_option(label='只读')
            for _ in range(80):
                closed=dialog.locator('details:not([open]) > summary:visible')
                if not closed.count(): break
                closed.first.click()
            if action == 'permissions':
                for select in dialog.locator('[data-permission-lifetime-mode]:visible:enabled').all():
                    select.select_option('timed')
            if action == 'settings':
                buttons=dialog.locator('.cr-setting-actions > button')
                boxes=[n.bounding_box() for n in buttons.all()]
                assert abs(boxes[0]['width']-boxes[1]['width']) < 1
                assert abs(boxes[0]['x']-boxes[2]['x']) < 1
            if action == 'add-member':
                radios=dialog.locator('.cr-connection-mode input[type=radio]')
                for radio in radios.all():
                    box=radio.bounding_box()
                    assert 12 <= box['width'] <= 24 and 12 <= box['height'] <= 24
                labels=[n.bounding_box() for n in dialog.locator('.cr-connection-mode label').all()]
                assert abs(labels[0]['width']-labels[1]['width']) < 1
            if action == 'permissions':
                bulk=dialog.locator('[data-permission-bulk-level]').first.bounding_box()
                project=dialog.locator('[data-permission-level]').first.bounding_box()
                assert abs(bulk['x']-project['x']) < 1
                assert abs(bulk['width']-project['width']) < 1
                expiry=dialog.locator('.cr-permission-expiry:visible').first
                assert abs(expiry.bounding_box()['x']-dialog.locator('[data-permission-row]').first.bounding_box()['x']) < 1
                for node in dialog.locator('[data-cr-form=permissions] [hidden]').all():
                    assert not node.is_visible()
                assert dialog.locator('[data-permission-lifetime-until]:visible').count() >= 1
            page.screenshot(path=str(evidence/f'{width}-{action}-top.png'),animations='disabled')
            content=dialog.locator('.cr-panel-content')
            records.append({'action':action,'text':dialog.inner_text(),'selects':dialog.locator('select').evaluate_all('(ns)=>ns.map(n=>({name:n.name,options:[...n.options].map(o=>o.text),rect:n.getBoundingClientRect().toJSON()}))')})
            steps = content.evaluate('(n)=>Math.ceil(n.scrollHeight/Math.max(1,n.clientHeight))')
            for step in range(1,min(steps,20)):
                content.evaluate('(n,s)=>n.scrollTop=s*n.clientHeight',step)
                page.screenshot(path=str(evidence/f'{width}-{action}-scroll-{step}.png'),animations='disabled')
            content.evaluate('(n)=>n.scrollTop=n.scrollHeight')
            dialog.evaluate('(n)=>n.scrollTop=n.scrollHeight')
            page.screenshot(path=str(evidence/f'{width}-{action}-bottom.png'),animations='disabled')
            if action == 'add-member':
                dialog.locator('input[name=connection_mode][value=existing]').check()
                page.screenshot(path=str(evidence/f'{width}-existing-member.png'),animations='disabled')
            dialog.locator('[data-cr-action="close"]').click()
        (evidence/f'{width}-controls.json').write_text(json.dumps(records,ensure_ascii=False,indent=2))
    finally: context.close()
