from shared.util import VERSION
from shared.contracts import tool_definitions
import hashlib
import os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect


def test_browser_diagnostics_search_symbols_and_download(stack,tmp_path):
    directory=Path(os.getenv('CODEPIER_V140_SCREENSHOTS',str(tmp_path/'screenshots')));directory.mkdir(parents=True,exist_ok=True)
    filename='browser-deliverable.txt';data='真实浏览器下载验证\n'.encode();(stack.imago/filename).write_bytes(data)
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch()
        page=browser.new_page(viewport={'width':1440,'height':1000},accept_downloads=True)
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        try:
            page.goto(stack.url+'/#diagnostics');page.fill('#username', 'admin');page.fill('#password',stack.password);page.click('#login-form button')
            expect(page.locator('#page h1')).to_have_text('运行诊断')
            expect(page.locator('#page')).to_contain_text(VERSION)
            expect(page.locator('#page')).to_contain_text(str(len(tool_definitions()))+' 个工具')
            page.screenshot(path=str(directory/'diagnostics-desktop.png'),full_page=True)
            page.click('[data-nav="artifacts"]');expect(page.locator('#page h1')).to_have_text('产物交付')
            page.click('[data-insight="register"]');page.fill('#artifact-path',filename);page.fill('#artifact-name','浏览器产物.txt')
            page.click('#artifact-save');expect(page.locator('#modal-title')).to_have_text('浏览器产物.txt')
            with page.expect_download() as pending:
                page.locator('.modal-footer a[download]').click()
            downloaded=pending.value
            assert Path(downloaded.path()).read_bytes()==data
            assert downloaded.suggested_filename=='浏览器产物.txt'
            page.locator('.modal [data-action="close-modal"]').first.click()
            expect(page.locator('#page')).to_contain_text('浏览器产物.txt')
            page.screenshot(path=str(directory/'artifacts-desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(directory/'artifacts-mobile.png'),full_page=True)
            page.set_viewport_size({'width':1440,'height':1000})
            page.click('[data-nav="workbench"]');expect(page.locator('#page h1')).to_have_text('远程工作台')
            page.locator('[data-action="read-file"][data-path="src/main.py"]').click()
            expect(page.locator('#code-editor')).to_be_visible()
            page.locator('.tool-menu summary').click()
            page.click('[data-insight="symbols"]');expect(page.locator('#modal-title')).to_have_text('代码结构 · src/main.py')
            expect(page.locator('.symbol-list')).to_contain_text('greeting')
            page.screenshot(path=str(directory/'symbols-desktop.png'),full_page=True)
            page.locator('.modal [data-action="close-modal"]').first.click()
            page.click('[data-insight="search"]');page.fill('#session-query','greeting');page.select_option('#session-mode','symbols');page.fill('#session-glob','*.py')
            page.click('#session-search-start');expect(page.locator('#modal-title')).to_have_text('检索结果')
            expect(page.locator('.search-results')).to_contain_text('greeting')
            page.screenshot(path=str(directory/'search-desktop.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            page.screenshot(path=str(directory/'search-mobile.png'),full_page=True)
            page.locator('.modal [data-action="close-modal"]').first.click()
            page.set_viewport_size({'width':1440,'height':1000})
            operations=stack.call('operations_list',{'project':'Imago','tool':'code_symbols'})['operations']
            identifier=operations[0]['id']
            page.click('[data-nav="diagnostics"]');page.fill('#trace-form input',identifier);page.click('#trace-form button')
            expect(page.locator('#modal-title')).to_contain_text('执行链路')
            expect(page.locator('.trace-list')).to_contain_text('本机正在执行')
            page.screenshot(path=str(directory/'trace-desktop.png'),full_page=True)
            assert not errors,errors
        finally:browser.close()
