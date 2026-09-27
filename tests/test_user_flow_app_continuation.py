"""F07/F12/F13/F16: real app functions and DOM with disposable HTTP responses.

No live account, model, project file, or browser profile is used by these tests.
"""
from pathlib import Path

import pytest
from playwright.sync_api import expect

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
    for name in ('tokens.css','styles.css','workspace.css','chat.css'):
        page.add_style_tag(path=str(ROOT / 'web' / name))
    source = (ROOT / 'web/app.js').read_text(encoding='utf-8')
    page.add_script_tag(content=source[:source.index('(async()=>{try{const page=location.hash')])
    for name in ('ui.js','identity.js','chat-markdown.js','chat-panels.js','chat-chrome.js',
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
        if(path.startsWith('/api/projects')&&method!=='GET'){
          if(window.saveHandler)return saveHandler({path,method,body},response);
          return response(fixtureProjects[0]);
        }
        if(path==='/api/projects')return response({projects:fixtureProjects});
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
    page.on('dialog',lambda dialog:dialog.accept())
    # The focused CLI layout hides the global sidebar; leave through navigation
    # first, retaining the draft, then use the visible account action.
    page.evaluate("navigate('projects')")
    page.locator('[data-action=logout]').click()
    expect(page.locator('#login-form')).to_be_visible()
    assert page.evaluate('ChatUI.views.size') == 0
    assert page.evaluate('S.suspendedUser') is None
    login(page)
    expect(page.locator('#chat-compose')).to_have_value('')


def open_project(page):
    page.evaluate("navigate('projects')")
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
    page.locator('.modal-header [data-action=close-modal]').click()
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


def test_pending_project_validation_reuses_same_save_receipt(app_page):
    page = app_page
    page.evaluate('''() => {
      let attempts=0;
      window.saveHandler=async(request,response)=>++attempts===1
        ?response({error:{code:'VALIDATION_PENDING',message:'fixture slow validation',operation_id:'d'.repeat(32)}},409)
        :response({id:'project-one'});
    }''')
    open_project(page)
    page.click('#save-project')
    expect(page.locator('#project-form [role=status]')).to_contain_text('不会新建重复任务')
    expect(page.locator('#save-project')).to_be_enabled()
    page.click('#save-project')
    expect(page.locator('#project-form')).to_have_count(0)
    requests = page.evaluate("calls.filter(call=>call.method==='PUT').map(call=>call.body)")
    assert len(requests) == 2 and requests[0] == requests[1]
    assert requests[0]['idempotency_key'].startswith('project-save-')


def test_unknown_project_save_does_not_send_changed_configuration(app_page):
    page = app_page
    page.evaluate("() => {window.saveHandler=async()=>{throw new TypeError('fixture lost response')};}")
    open_project(page)
    page.click('#save-project')
    expect(page.locator('#save-project')).to_be_enabled()
    page.fill('#project-form [name=alias]','DoNotReplayDifferentConfig')
    page.click('#save-project')
    expect(page.locator('#project-form [role=status]')).to_contain_text('原保存结果尚未核实')
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
    expect(page.locator('#project-form [role=status]')).to_contain_text('permission rejected')
    page.click('#save-project')
    expect(page.locator('#project-form')).to_have_count(0)
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
