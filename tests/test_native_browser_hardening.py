from tests.evidence import evidence_path
"""Archived terminal compatibility/security coverage.
The production panel no longer mounts this application. The old renderer is
explicitly injected ONLY into this test to guard retained transport behavior.
"""
from pathlib import Path
import json
import time
import uuid

from playwright.sync_api import sync_playwright,expect
from tests.test_native_cli_integration import cli_stack,read,call
from tests.support import wait_for
from scripts.check_native_cli_real import png


from tests.support_legacy_terminal import mount_archived_terminal


def test_http_upload_reload_binding_input_batch_queries_and_export(cli_stack,tmp_path):
    s=cli_stack
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch()
        page=browser.new_page(viewport={'width':1440,'height':1000})
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        # localhost normally grants a secure context. Explicitly remove WebCrypto
        # to exercise the actual ordinary-HTTP fallback in this real browser.
        page.add_init_script("Object.defineProperty(window.crypto,'subtle',{get:()=>undefined,configurable:true})")
        try:
            page.goto(s.url+'/#projects');page.fill('#password',s.password);page.click('#login-form button')
            page.locator('[data-native-launch="codex"][data-project="'+s.project['id']+'"]').click()
            mount_archived_terminal(page)
            expect(page.locator('#native-root')).to_be_visible()
            assert page.evaluate('typeof crypto.subtle')=='undefined'
            page.set_input_files('#native-files',[
                {'name':'hardening image.png','mimeType':'image/png','buffer':png()},
                {'name':'hardening notes.txt','mimeType':'text/plain','buffer':b'Native browser file fixture'}])
            expect(page.locator('.native-file-card')).to_have_count(2,timeout=15000)
            expect(page.locator('#native-status')).to_contain_text('已校验')
            page.reload()
            expect(page.locator("#chat-compose")).to_be_visible()
            mount_archived_terminal(page)
            expect(page.locator('.native-file-card')).to_have_count(2,timeout=15000)
            assert all(x['ready'] for x in page.evaluate('NativeCLIUI.files'))
            page.fill('#native-cwd','does-not-exist');page.click('#native-start')
            expect(page.locator('#native-status')).to_contain_text('不存在',timeout=10000)
            assert page.evaluate('NativeCLIUI.pendingStart') is None
            page.fill('#native-cwd','src');page.click('#native-start')
            expect(page.locator('#native-status')).to_contain_text('running',timeout=15000)
            sid=page.evaluate('NativeCLIUI.selected.id')
            expect(page.locator('[data-native-command="/thinking"]')).to_be_hidden()
            page.locator('#native-launch-details > summary').click()
            page.click('#native-detect');expect(page.locator('#native-detection')).to_contain_text('fixture',timeout=15000)
            page.click('#native-write');expect(page.locator('#native-status')).to_contain_text('可输入',timeout=10000)
            posts=[]
            page.on('request',lambda request:posts.append(request.post_data_json) if request.url.endswith('/api/native/input') else None)
            page.evaluate("Promise.all([...('BATCHED-INPUT\\n')].map(character=>nativeInput(character)))")
            wait_for(lambda:b'ECHO:BATCHED-INPUT' in read(s,sid),10)
            assert len(posts)==1,posts
            before=len(posts)
            page.evaluate("new Promise(resolve=>NativeCLIUI.term.write('\\x1b[6n\\x1b[c\\x1b[?u\\x1b]11;?\\x07',resolve))")
            page.wait_for_timeout(250)
            assert len(posts)==before, 'Replay generated a second terminal query response'
            card=page.locator('.native-file-card').filter(has_text='hardening notes.txt')
            card.get_by_role('button',name='插入路径').click()
            page.click('#native-insert')
            expect(page.locator('#native-compose')).to_have_value('')
            file=next(x for x in page.evaluate('NativeCLIUI.files') if x['name']=='hardening notes.txt')
            denied=call(s,'upload_delete',{'file':file['file'],'confirm':file['file']})
            assert denied.status_code==409 and denied.json()['error']['code']=='CLI_BUSY'
            with page.expect_download() as download_info:page.click('#native-export')
            download=download_info.value;destination=tmp_path/'native.ansi';download.save_as(destination)
            assert b'ECHO:BATCHED-INPUT' in destination.read_bytes()
            page.screenshot(path=evidence_path('docs/evidence/chat-ui-20260915/desktop-terminal.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            page.wait_for_timeout(300)
            page.screenshot(path=evidence_path('docs/evidence/chat-ui-20260915/mobile-native-hardened.png'),full_page=True)
            overflow=page.evaluate("[...document.querySelectorAll('body *')].map(e=>({tag:e.tagName,id:e.id,class:e.className,x:e.getBoundingClientRect().x,w:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right})).filter(x=>x.right>innerWidth+2&&x.w>0).slice(0,30)")
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2'),overflow
            page.on('dialog',lambda dialog:dialog.accept())
            page.click('#native-stop')
            wait_for(lambda:any(x['id']==sid and x['status']=='exited' for x in s.must(s.client.get('/api/native/sessions'))['sessions']),12)
            expect(page.locator('#native-status')).to_contain_text('exited',timeout=10000)
            s.must(call(s,'upload_delete',{'file':file['file'],'confirm':file['file']}))
            page.click('#native-clear');expect(page.locator('#native-terminal .xterm')).to_have_count(0)
            assert read(s,sid)==b''
            assert errors==[],errors
        finally:browser.close()
