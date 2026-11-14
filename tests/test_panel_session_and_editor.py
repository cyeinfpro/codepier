"""F07/F12/F13/F16: real app functions and DOM with disposable HTTP responses.

No live account, model, project file, or browser profile is used by these tests.
"""
from pathlib import Path

import pytest
from playwright.sync_api import expect
from tests.javascript_support import panel_without_boot

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def app_page(chat_browser_pool):
    page = chat_browser_pool('chromium').new_page(viewport={'width':1440,'height':1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('https://flow.fixture/**', lambda route: route.fulfill(
        status=200, content_type='text/html',
        body='<div id="app"></div><div id="modal-root"></div><div id="toasts"></div>'))
    page.goto('https://flow.fixture/')
    for name in ('tokens.css','styles.css','workspace.css','chat.css','agent-roots.css'):
        page.add_style_tag(path=str(ROOT / 'web' / name))
    for name in ('core/bundle.js', 'ui.js'):
        page.add_script_tag(path=str(ROOT / 'web' / name))
    page.add_script_tag(content=panel_without_boot())
    page.add_script_tag(path=str(ROOT / 'web' / 'agent-roots.js'))
    page.add_script_tag(path=str(ROOT / 'web' / 'identity.js'))
    for name in ('chat-markdown.js','chat-panels.js','chat-chrome.js',
                 'chat-history.js','chat-catalog.js','chat.js'):
        page.add_script_tag(path=str(ROOT / 'web' / name))
    page.evaluate('''async () => {
      S.session={user_id:'owner-one',username:'admin',csrf:'fixture'};S.page='native';
      window.nextLogin={...S.session};window.calls=[];
      window.fixtureDevices=[{id:'node-one',name:'Fixture node',online:true}];
      window.fixtureProjects=[{id:'project-one',alias:'Fixture',device_id:'node-one',
        root:'/fixture/one',mode:'write',allow_tasks:true,online:true}];
      window.EventSource=class {addEventListener(){} close(){this.closed=true;}};
      window.fetch=async(path,options={})=>{
        const method=options.method||'GET',body=options.body?JSON.parse(options.body):{};
        calls.push({path,method,body});
        const response=(value,status=200)=>new Response(JSON.stringify(value),{status,
          headers:{'Content-Type':'application/json'}});
        if(path==='/api/fixture-expire')return response({error:{code:'UNAUTHORIZED',message:'Session expired'}},401);
        if(path==='/api/login')return response(nextLogin);
        if(path==='/api/auth/providers')return response({providers:[]});
        if(path==='/api/iam/me')return response({id:S.session.user_id,username:S.session.username,
          instance_admin:true,spaces:[{id:'legacy',label:'Fixture Space',level:'owner',active:true}],
          disabled_spaces:[],identities:[],sessions:[]});
        if(path==='/api/logout')return response({ok:true});
        if(path==='/api/devices')return response({devices:fixtureDevices});
        if(path==='/api/devices/node-one/roots'&&method==='GET')return response({
          device_id:'node-one',state:'applied',config_sha256:'a'.repeat(64),
          roots:[{path:'/fixture/one',writable:false,allow_tasks:false}]});
        if(path==='/api/devices/node-one/roots/preview'&&method==='POST')return response({
          device_id:'node-one',valid:true,config_sha256:body.expected_sha256});
        if(path==='/api/devices/node-one/roots'&&method==='POST')return response({
          device_id:'node-one',state:'applied',config_sha256:'b'.repeat(64),
          roots:[{path:body.path,writable:body.writable,allow_tasks:body.allow_tasks}]});
        if(path.startsWith('/api/projects')&&method!=='GET'){
          if(window.saveHandler)return saveHandler({path,method,body},response);
          return response(fixtureProjects[0]);
        }
        if(path==='/api/projects')return response({projects:fixtureProjects});
        if(path.startsWith('/api/native/access?'))return window.fixtureNativeDenied
          ? response({error:{code:'CLI_FORBIDDEN',message:'Native permission revoked'}},403)
          : response({allowed:true,project_id:new URL(path,location.origin).searchParams.get('project')});
        if(path.startsWith('/api/native/sessions?'))return response({sessions:[]});
        if(path.endsWith('/chat_catalog'))return response({cli:'pi',models:[{
          id:'fixture-model',provider:'fixture',name:'Fixture Model',reasoning:true,
          input:['text','image']}],model:{provider:'fixture',id:'fixture-model'},
          thinking_levels:['off','medium'],thinkingLevel:'medium',commands:[],
          capabilities:{commands:true}});
        if(path.endsWith('/upload_list'))return response({files:[]});
        return response({});
      };
      await bootAuthenticated();
    }''')
    expect(page.locator('#chat-compose')).to_be_visible()
    try:
        yield page
        assert not errors, errors
    finally:
        page.context.close()


def test_async_close_never_closes_a_replacement_modal(app_page):
    result = app_page.evaluate('''async () => {
      modal('Original', '<p>read-only</p>');
      const pending = panelDialogs.requestClose(null, {navigation:true});
      const replacement = modal('Replacement', '<input name="draft" value="keep me">');
      const closed = await pending;
      return {closed, retained:replacement.isConnected,
        title:document.querySelector('#modal-title')?.textContent};
    }''')
    assert result == {'closed': False, 'retained': True, 'title': 'Replacement'}


def test_activity_seed_reconciles_after_running_then_terminal_event(app_page):
    page = app_page
    page.evaluate('''() => {
      window.savedFetch = fetch;
      window.fetch = async (path, options) => path.startsWith('/api/overview')
        ? new Response(JSON.stringify({active_operations:0,recent_operations:[]}),
          {headers:{'Content-Type':'application/json'}}) : savedFetch(path, options);
      navActivitySeed(1);
      navActivityOperation({id:'seeded-operation',state:'running'});
      navActivityOperation({id:'seeded-operation',state:'succeeded'});
    }''')
    page.wait_for_function('S.activeOpsSeed === 0 && S.activeOps.size === 0')


def test_activity_ignores_duplicate_unrelated_terminal_and_old_space_snapshot(app_page):
    page = app_page
    page.evaluate('''() => {
      window.savedFetch = fetch;
      window.fetch = (path, options) => path.startsWith('/api/overview')
        ? new Promise(resolve => {window.releaseOverview = () => resolve(new Response(
          JSON.stringify({active_operations:5,recent_operations:[]}),
          {headers:{'Content-Type':'application/json'}}));}) : savedFetch(path, options);
      navActivitySeed(1);
      navActivityOperation({id:'unrelated',state:'succeeded'});
      navActivityOperation({id:'unrelated',state:'succeeded'});
    }''')
    assert page.evaluate('S.activeOpsSeed') == 1
    page.wait_for_function('typeof releaseOverview === "function"')
    page.evaluate('''() => {clearSpaceSnapshots(); S.space_id='new-space'; releaseOverview();}''')
    page.wait_for_timeout(150)
    assert page.evaluate('[S.activeOpsSeed,S.activeOps.size]') == [0, 0]


def expire(page):
    page.evaluate("api('/api/fixture-expire').catch(error=>error.code)")
    expect(page.locator('#login-form')).to_be_visible()


def login(page):
    page.fill('#username','admin')
    page.fill('#password','disposable-fixture-password')
    page.locator('#login-form button[type=submit]').click()
    expect(page.locator('#page')).to_be_visible()
    if page.evaluate('S.page') != 'native':
        page.evaluate("navigate('native')")
    expect(page.locator('#chat-compose')).to_be_visible()


def test_expired_session_retains_same_account_draft_files_and_unknown_receipt(app_page):
    page = app_page
    page.fill('#chat-compose','Unsent detailed implementation plan')
    page.evaluate('''() => {
      const view=chatView();window.originalView=view;
      view.files.push({file:'a'.repeat(32),name:'unsent.png',mime:'image/png',size:4,
        ready:true,preview:URL.createObjectURL(new Blob(['fixture']))});
      view.pending={id:'b'.repeat(32),receipt:'original-receipt',text:'Original request',
        attachments:[],cli:'pi',cwd:'.'};chatFiles();chatSyncChrome();
    }''')
    expire(page)
    assert page.evaluate('ChatUI.views.size') == 1
    assert page.evaluate("[...ChatUI.views.values()][0]===originalView")
    assert 'Unsent detailed' not in page.evaluate('JSON.stringify({...sessionStorage,...localStorage})')
    login(page)
    expect(page.locator('#chat-compose')).to_have_value('Unsent detailed implementation plan')
    assert page.evaluate('chatView()===originalView')
    assert page.evaluate('chatView().files[0].name') == 'unsent.png'
    assert page.evaluate('chatView().pending.receipt') == 'original-receipt'
    assert not page.evaluate("calls.some(call=>call.path.endsWith('/start')||call.path.endsWith('/chat_prompt'))")


def test_recreated_account_with_same_username_cannot_inherit_old_draft(app_page):
    page = app_page
    page.fill('#chat-compose','private old-account draft')
    page.evaluate("S.work.content='private editor draft';S.work.dirty=true")
    expire(page)
    page.evaluate("nextLogin={user_id:'different-user',username:'admin',csrf:'new-fixture'}")
    login(page)
    expect(page.locator('#chat-compose')).to_have_value('')
    assert not page.evaluate("[...ChatUI.views.values()].some(view=>view.draft.includes('private'))")
    assert page.evaluate('S.work.content') == ''


def test_chat_draft_warns_before_unload_and_explicit_logout_discards(app_page):
    page = app_page
    page.fill('#chat-compose','intentionally discard this draft')
    assert page.evaluate("() => {const event=new Event('beforeunload',{cancelable:true});window.dispatchEvent(event);return event.defaultPrevented;}")
    # The focused CLI layout hides the global sidebar; leave through navigation
    # first, retaining the draft, then use the visible account action.
    page.evaluate("navigate('projects')")
    # uiConfirmAction uses a modal sheet instead of native browser dialogs.
    page.locator('[data-action=logout]').click()
    page.locator('dialog.ui-sheet button[type="submit"]').click()
    expect(page.locator('#login-form')).to_be_visible()
    expect(page.locator('#username')).to_have_value('')
    assert page.evaluate('ChatUI.views.size') == 0
    assert page.evaluate('S.suspendedUser') is None
    login(page)
    expect(page.locator('#chat-compose')).to_have_value('')


def open_project(page):
    page.evaluate("navigate('projects')")
    page.locator('.workspace-project-row .tool-menu > summary').first.click()
    page.locator('[data-action=edit-project]').first.click()
    expect(page.locator('#project-form')).to_be_visible()


def test_old_project_save_cannot_close_or_replace_new_dialog(app_page):
    page = app_page
    page.evaluate('''() => {window.saveHandler=(request,response)=>new Promise(resolve=>{
      window.finishOldSave=()=>resolve(response({id:'project-one'}));
    });}''')
    open_project(page)
    page.fill('#project-form [name=alias]','FirstSave')
    page.click('#save-project')
    page.wait_for_function('!!window.finishOldSave')
    # Explicitly leave an in-flight editor; the late save still must not replace the next view.
    page.locator('.modal-header [data-action=close-modal]').click()
    page.locator('dialog.ui-sheet button[type="submit"]').click()
    page.locator('[data-action=add-project]').first.click()
    page.fill('#project-form [name=alias]','SecondUnsavedMapping')
    page.fill('#project-form [name=root]','/fixture/second')
    page.evaluate('finishOldSave()')
    expect(page.locator('#project-form [name=alias]')).to_have_value('SecondUnsavedMapping')
    expect(page.locator('#project-form [name=root]')).to_have_value('/fixture/second')
    assert page.evaluate("calls.filter(call=>call.method==='PUT').length") == 1


def test_late_project_load_cannot_open_over_a_newer_dialog(app_page):
    page = app_page
    page.evaluate('''() => {
      S.page='projects';let count=0;
      loadBasics=async()=>{if(++count===1)await new Promise(resolve=>window.finishOldLoad=resolve);};
      void projectModal('project-one');
    }''')
    page.wait_for_function('!!window.finishOldLoad')
    page.evaluate("projectModal(null)")
    page.fill('#project-form [name=alias]','NewerDialog')
    page.evaluate('finishOldLoad()')
    expect(page.locator('#project-form [name=alias]')).to_have_value('NewerDialog')


@pytest.mark.parametrize('confirmation', ['automatic', 'manual'])
def test_pending_project_validation_reuses_same_save_receipt(app_page, confirmation):
    page = app_page
    page.evaluate('''() => {
      let attempts=0;
      window.saveHandler=async(request,response)=>{
        if(++attempts===1)return response({error:{code:'VALIDATION_PENDING',
          message:'fixture slow validation',operation_id:'d'.repeat(32)}},409);
        return new Promise(resolve=>{
          window.finishPendingProjectSave=()=>resolve(response({id:'project-one'}));
        });
      };
    }''')
    open_project(page)
    page.evaluate('''confirmation => {
      const button=document.querySelector('#save-project'),handler=button.onclick;
      button.onclick=event=>{
        button.onclick=handler;
        return handler.call(button,event).then(()=>{
          window.initialPendingState={
            text:document.querySelector('#project-form > [role=status]').textContent,
            enabled:!button.disabled};
          // Observe completion in the browser microtask queue. A real explicit
          // click can then run before the scheduled automatic confirmation,
          // without replacing or shortening the production timer.
          if(confirmation==='manual')button.click();
        });
      };
    }''', confirmation)
    page.click('#save-project')
    page.wait_for_function("typeof window.finishPendingProjectSave==='function'")
    assert page.evaluate('initialPendingState') == {
        'text': '正在等待电脑检查目录…',
        'enabled': True,
    }
    expect(page.locator('#project-form')).to_be_visible()
    expect(page.locator('#save-project')).to_be_disabled()
    requests = page.evaluate("calls.filter(call=>call.method==='PUT').map(call=>call.body)")
    assert len(requests) == 2 and requests[0] == requests[1]
    assert requests[0]['idempotency_key'].startswith('project-save-')
    page.evaluate('finishPendingProjectSave()')
    expect(page.locator('#project-form')).to_have_count(0)
    assert page.evaluate("calls.filter(call=>call.method==='PUT').length") == 2


def test_unknown_project_save_does_not_send_changed_configuration(app_page):
    page = app_page
    page.evaluate("() => {window.saveHandler=async()=>{throw new TypeError('fixture lost response')};}")
    open_project(page)
    page.click('#save-project')
    expect(page.locator('#save-project')).to_be_enabled()
    page.fill('#project-form [name=alias]','DoNotReplayDifferentConfig')
    page.click('#save-project')
    expect(page.locator('#project-form > [role=status]')).to_contain_text('上次保存结果待确认')
    assert page.evaluate("calls.filter(call=>call.method==='PUT').length") == 1


def test_definite_project_rejection_allows_a_fresh_explicit_validation(app_page):
    page = app_page
    page.evaluate('''() => {
      let attempts=0;
      window.saveHandler=async(request,response)=>++attempts===1
        ?response({error:{code:'LOCAL_READ_ONLY',message:'fixture permission rejected'}},403)
        :response({id:'project-one'});
    }''')
    open_project(page)
    page.click('#save-project')
    expect(page.locator('#project-form > [role=status]')).to_contain_text('permission rejected')
    page.locator('dialog.ui-sheet button[type="submit"]').click()
    # Explicit approval validates and saves roots, then automatically resumes
    # the rejected mapping with a new receipt. An empty save response would
    # correctly remain uncertain rather than claim that permission was applied.
    expect(page.locator('#project-form')).to_have_count(0)
    roots_requests = page.evaluate("calls.filter(call=>call.path.startsWith('/api/devices/node-one/roots')&&call.method==='POST')")
    assert [request['path'] for request in roots_requests] == [
        '/api/devices/node-one/roots/preview', '/api/devices/node-one/roots']
    assert all(request['body']['path'] == '/fixture/one' for request in roots_requests)
    assert all(request['body']['expected_sha256'] == 'a' * 64 for request in roots_requests)
    assert roots_requests[0]['body']['idempotency_key'].startswith('roots-preview-')
    assert roots_requests[1]['body']['idempotency_key'].startswith('roots-save-')
    requests = page.evaluate("calls.filter(call=>call.method==='PUT').map(call=>call.body)")
    assert len(requests) == 2
    assert requests[0]['idempotency_key'] != requests[1]['idempotency_key']
    assert {k:v for k,v in requests[0].items() if k!='idempotency_key'} == {k:v for k,v in requests[1].items() if k!='idempotency_key'}


def test_crlf_editor_changes_only_the_requested_line(app_page):
    original = ''.join(f'line {index}\r\n' for index in range(2100))
    edited = original.replace('line 1050\r\n','changed 1050\r\n')
    result = app_page.evaluate('([value,original])=>editorLineEndings(value,original)',
                               [edited.replace('\r\n','\n'),original])
    assert result == edited
    assert len(result.splitlines()) == 2100


def test_same_account_relogin_rechecks_project_read_before_restoring_private_views(app_page):
    page=app_page
    page.fill('#chat-compose','REVOKED_PROJECT_PRIVATE_DRAFT')
    page.evaluate("""() => {
      S.work.project='project-one';S.work.path='private.py';
      S.work.content='REVOKED_PROJECT_PRIVATE_SOURCE';S.work.original=S.work.content;
      S.work.console='REVOKED_PROJECT_PRIVATE_OUTPUT';S.work.dirty=true;
      const view=chatView();
      view.pending={receipt:'private-pending',text:'REVOKED_PROJECT_PRIVATE_REQUEST'};
      view.files.push({name:'private.png',preview:URL.createObjectURL(new Blob(['fixture']))});
    }""")
    expire(page)
    page.evaluate("fixtureProjects=[]")
    login(page)
    assert 'REVOKED_PROJECT_PRIVATE' not in page.locator('body').inner_text()
    assert page.evaluate('S.work.content')==''
    assert page.evaluate('S.work.console')==''
    assert not page.evaluate("""[...ChatUI.views.values()].some(v =>
      v.draft.includes('REVOKED_PROJECT_PRIVATE') ||
      v.pending?.text?.includes('REVOKED_PROJECT_PRIVATE') ||
      v.files.some(f=>f.name==='private.png'))""")
    assert not page.evaluate("calls.some(c=>c.path.endsWith('/start')||c.path.endsWith('/chat_prompt'))")


def test_same_account_relogin_drops_native_cache_after_execute_revocation(app_page):
    page=app_page
    page.fill('#chat-compose','PRIVATE_NATIVE_EXECUTE_REVOKED')
    page.evaluate("S.work.project='project-one';S.work.content='readable editor draft'")
    expire(page)
    page.evaluate("fixtureNativeDenied=true")
    login(page)
    assert 'PRIVATE_NATIVE_EXECUTE_REVOKED' not in page.locator('body').inner_text()
    assert not page.evaluate("[...ChatUI.views.values()].some(v=>v.draft.includes('PRIVATE_NATIVE'))")
    assert page.evaluate('S.work.content')=='readable editor draft'
    assert not page.evaluate("calls.some(c=>c.path.endsWith('/start')||c.path.endsWith('/chat_prompt'))")


def test_network_failure_keeps_suspended_draft_hidden_until_authority_recheck(app_page):
    page=app_page
    page.fill('#chat-compose','PRIVATE_NETWORK_SUSPENDED')
    expire(page)
    page.evaluate("""() => {
      window.recheckOffline=true;const original=api;
      api=async(path,options={})=>{
        if(recheckOffline && path.startsWith('/api/native/access?'))
          throw Object.assign(new Error('Fixture offline'),{status:503});
        return original(path,options);
      };
    }""")
    page.fill('#username','admin');page.fill('#password','fixture-password-only')
    page.locator('#login-form button[type=submit]').click()
    expect(page.locator('[data-action=retry-identity]')).to_be_visible()
    assert 'PRIVATE_NETWORK_SUSPENDED' not in page.locator('body').inner_text()
    assert page.evaluate("[...ChatUI.views.values()].some(v=>v.draft==='PRIVATE_NETWORK_SUSPENDED')")
    assert page.evaluate('!!S.suspendedUser')
    page.evaluate('recheckOffline=false')
    page.locator('[data-action=retry-identity]').click()
    expect(page.locator('#chat-compose')).to_have_value('PRIVATE_NETWORK_SUSPENDED')
    assert not page.evaluate("calls.some(c=>c.path.endsWith('/start')||c.path.endsWith('/chat_prompt'))")


def test_editor_breadcrumbs_keep_cumulative_paths_and_unsaved_source(app_page):
    page = app_page
    path = 'src/nested<safe>/file.py'
    source = 'unsaved = "<keep & preserve>"\n'
    page.evaluate('''([path,source]) => {
      S.work.project='project-one'; S.work.path=path;
      S.work.content=source; S.work.original='old source'; S.work.dirty=true;
      S.work.sha='a'.repeat(64); S.work.truncated=false;
      document.querySelector('#page').innerHTML=editorHTML();
    }''', [path, source])
    links = page.locator('.editor-path-directory')
    expect(links).to_have_count(2)
    expect(links.nth(0)).to_have_attribute('data-path', 'src')
    expect(links.nth(1)).to_have_attribute('data-path', 'src/nested<safe>')
    expect(links.nth(1)).to_have_attribute('data-action', 'tree-dir')
    expect(links.nth(1)).to_have_text('nested<safe>')
    expect(page.locator('.editor-path-leaf')).to_have_text('file.py')
    expect(page.locator('.editor-path safe')).to_have_count(0)
    expect(page.locator('#code-editor')).to_have_value(source)
    expect(page.locator('#dirty-state')).to_have_attribute('data-dirty', 'true')
    assert page.evaluate('S.work.content') == source
    assert page.evaluate('S.work.original') == 'old source'
