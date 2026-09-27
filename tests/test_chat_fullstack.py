"""Actual HTTP + encrypted Hub/Agent + independent native pipe + real browsers.
Only the upstream native CLI is a deterministic no-network fixture.
"""
from contextlib import closing
from pathlib import Path
import json, os, sys, time, uuid
import pytest
from playwright.sync_api import sync_playwright,expect
from shared.native_cli import database
from shared.util import atomic_json
from tests.support import running_stack,wait_for
from tests.test_chat_worker import FAKE
ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=ROOT/'docs/evidence/chat-complete-20260915'

@pytest.fixture(scope='module')
def chat_stack(tmp_path_factory):
    with running_stack(tmp_path_factory.mktemp('chat-fullstack')) as s:
        s.stop_agent()
        bindir=s.directory/'fake-bin';bindir.mkdir();home=s.directory/'isolated-home';home.mkdir()
        for cli in ('pi','codex'):
            p=bindir/cli
            p.write_text('#!'+sys.executable+'\nimport sys\nsys.argv=[sys.argv[0],'+repr(cli)+']\n'+FAKE)
            p.chmod(0o700)
        s.config['shell']={'enabled':True,'projects':['*'],'inherit_env':False,'env':{'PATH':str(bindir)+os.pathsep+str(Path(sys.executable).parent)+os.pathsep+os.defpath,'HOME':str(home)}}
        atomic_json(s.config_path,s.config);s.start_agent()
        yield s
        with closing(database(s.directory/'agent-state/native-cli')) as db,db:
            for row in db.execute("SELECT id FROM sessions WHERE status IN ('starting','running')"):
                db.execute('INSERT INTO commands(id,session,kind,payload,created) VALUES (?,?,?,?,?)',(uuid.uuid4().hex,row['id'],'stop','{}',time.time()))
        def done():
            with closing(database(s.directory/'agent-state/native-cli')) as db:
                return not db.execute("SELECT 1 FROM sessions WHERE status IN ('starting','running')").fetchone()
        wait_for(done,10)

@pytest.mark.parametrize('cli',['pi','codex'])
def test_full_panel_direct_chat_actual_sse_history_and_responsive_layout(chat_stack,cli):
    s=chat_stack
    with sync_playwright() as pw:
        browser=pw.chromium.launch()
        context=browser.new_context(viewport={'width':1440,'height':1000})
        context.add_cookies([{'name':c.name,'value':c.value,'url':s.url} for c in s.client.cookies.jar])
        page=context.new_page();errors=[];requests=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.on('request',lambda r:requests.append(r.url))
        try:
            page.goto(s.url+'/#native')
            expect(page.locator('#chat-compose')).to_be_visible(timeout=12000)
            page.select_option('#chat-project',s.project['id'])
            page.select_option('#chat-provider',cli)
            requests.clear()
            page.fill('#chat-compose','检查这个项目的上传队列与重试流程。')
            assert not any('/api/native/start' in r or '/api/native/input' in r for r in requests)
            page.press('#chat-compose','Enter')
            expect(page.locator('.chat-message-assistant')).to_contain_text('hello',timeout=15000)
            expect(page.locator('.chat-process')).to_be_visible()
            page.locator('.chat-process > summary').first.click()
            expect(page.locator('.chat-message-tool')).to_be_visible()
            expect(page.locator('#chat-compose')).to_have_value('')
            from urllib.parse import urlsplit, parse_qs
            event_queries = [parse_qs(urlsplit(r).query) for r in requests if urlsplit(r).path.endswith('/events')]
            assert any(q.get('project') and q.get('space_id') == ['legacy'] for q in event_queries)
            assert not any('/lease' in r or '/output?' in r or '/api/native/input' in r for r in requests)
            assert page.locator('#native-write').count()==0
            expect(page.locator('.chat-review').first).to_be_visible(timeout=15000)
            page.locator('.chat-review > summary').first.click()
            expect(page.locator('.chat-review-file').first).to_be_visible(timeout=15000)
            page.locator('.chat-review-file > summary').first.click()
            expect(page.locator('.chat-review-diff pre').first).to_be_visible(timeout=10000)
            sid=page.evaluate('ChatUI.selected.id')
            page.fill('#chat-compose','hold');page.press('#chat-compose','Enter')
            expect(page.locator('#chat-interrupt')).to_be_visible(timeout=10000)
            expect(page.locator('.chat-message-user').last).to_contain_text('hold')
            page.fill('#chat-compose','这段草稿在切换后保留。')
            page.screenshot(path=str(EVIDENCE/(cli+'-full-panel-desktop.png')))
            page.set_viewport_size({'width':390,'height':844})
            # set_viewport_size can return before visualViewport's resize event.
            # Wait for the application's layout, then assert actual geometry.
            page.wait_for_function('''() => {
                const root = document.querySelector('#chat-root').getBoundingClientRect();
                return root.width <= innerWidth + 1 && root.bottom <= innerHeight + 1;
            }''')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
            composer=page.locator('#chat-compose').bounding_box()
            assert composer and composer['y']+composer['height']<844
            page.screenshot(path=str(EVIDENCE/(cli+'-full-panel-mobile.png')))
            page.click('#chat-interrupt')
            expect(page.locator('#chat-interrupt')).to_be_hidden(timeout=10000)
            page.evaluate('navigate("overview")')
            expect(page.locator('.overview-grid')).to_be_visible()
            page.evaluate('navigate("native")')
            expect(page.locator('#chat-compose')).to_have_value('这段草稿在切换后保留。')
            assert page.evaluate('ChatUI.selected.id')==sid
            page.reload()
            expect(page.locator('#chat-compose')).to_be_visible(timeout=10000)
            page.click('#chat-history-toggle')
            page.locator('.chat-session').filter(has_text='检查这个项目的上传队列与重试流程。').first.click()
            expect(page.locator('.chat-message-assistant').first).to_contain_text('hello',timeout=10000)
            assert page.evaluate('ChatUI.selected.id')==sid
            assert not errors,errors
        finally:browser.close()
