"""B6/B7: delayed workbench responses cannot cross modal or workspace lifetimes.

Runs real panel functions and DOM in disposable browsers with fake transport only.
"""
from pathlib import Path

import pytest
from playwright.sync_api import expect
from tests.javascript_support import panel_without_boot

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(params=['chromium', 'webkit'])
def scope_page(request, chat_browser_pool):
    page = chat_browser_pool(request.param).new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('https://scope.fixture/**', lambda route: route.fulfill(
        status=200, content_type='text/html',
        body='<div id="app"></div><div id="modal-root"></div><div id="toasts"></div>'))
    page.goto('https://scope.fixture/')
    for name in ('core/bundle.js', 'ui.js'):
        page.add_script_tag(path=str(ROOT / 'web' / name))
    page.add_script_tag(content=panel_without_boot())
    page.evaluate('''() => {
      S.session={user_id:'fixture-owner'}; S.page='workbench';
      resetWork('project-one', 'workspace-A'); S.work.path='first.txt';
      window.requests=[]; window.jobs=[]; window.failures=[];
      window.fetch=(path,options={})=>{
        const body=JSON.parse(options.body||'{}');
        return new Promise(resolve=>requests.push({path,...body,finish:(data,status=200)=>
          resolve(new Response(JSON.stringify(data),{status,
            headers:{'Content-Type':'application/json'}}))}));
      };
      window.launch=(kind,query='same')=>{
        const job=(kind==='search'?searchFiles(query):historyModal()).catch(e=>failures.push(e.message));
        jobs.push(job);
      };
      window.result=(kind,name='result',next=null)=>kind==='search'
        ?{matches:[{path:name+'.txt',line:1,text:name}],scanned_files:1,next_offset:next}
        :{backups:[{path:name+'.txt',id:'backup-'+name,at:1}]};
      window.finish=async(index,data)=>{requests[index].finish(data);await jobs[index];};
    }''')
    try:
        yield page
        assert not errors, errors
    finally:
        page.context.close()


@pytest.mark.parametrize('kind', ['search', 'history'])
def test_latest_request_wins_even_for_a_b_a(scope_page, kind):
    page = scope_page
    page.evaluate('kind=>{launch(kind,"A");S.work.path="B.txt";launch(kind,"B");'
                  'S.work.path="A.txt";launch(kind,"A");}', kind)
    page.evaluate('async kind=>{await finish(2,result(kind,"new-A"));'
                  'await finish(1,result(kind,"old-B"));await finish(0,result(kind,"old-A"));}', kind)
    expect(page.locator('.modal-body')).to_contain_text('new-A.txt')
    expect(page.locator('.modal-body')).not_to_contain_text('old-')
    assert page.evaluate('requests.every(r=>r.arguments.workspace_id==="workspace-A")')


@pytest.mark.parametrize('kind', ['search', 'history'])
@pytest.mark.parametrize('invalidate', [
    "resetWork('project-one','workspace-B')",
    "resetWork('project-one','workspace-B');resetWork('project-one','workspace-A')",
    "resetWork('project-two','workspace-A')",
    "closeModal()",
    "S.page='projects';S.renderSeq++;S.page='workbench';S.renderSeq++",
    "S.session=null;S.session={user_id:'fixture-owner'}",
])
def test_late_response_cannot_reopen_after_scope_change(scope_page, kind, invalidate):
    page = scope_page
    page.evaluate('kind=>launch(kind)', kind)
    page.evaluate(invalidate)
    page.evaluate('kind=>finish(0,result(kind))', kind)
    expect(page.locator('.modal')).to_have_count(0)


def test_new_dialog_blocks_old_search_and_retry_remains_possible(scope_page):
    page = scope_page
    page.evaluate('launch("search","old");modal("New dialog","Keep me")')
    page.evaluate('finish(0,result("search"))')
    expect(page.locator('#modal-title')).to_have_text('New dialog')
    page.evaluate('closeModal();launch("search","fail")')
    page.evaluate('async()=>{requests[1].finish({error:{message:"fixture rejected"}},400);await jobs[1];}')
    expect(page.locator('#toasts')).to_contain_text('fixture rejected')
    page.evaluate('launch("search","retry")')
    page.evaluate('finish(2,result("search","retry"))')
    expect(page.locator('.modal-body')).to_contain_text('retry.txt')


def test_search_pagination_keeps_identity_and_close_invalidates_pending_page(scope_page):
    page = scope_page
    page.evaluate('launch("search","needle")')
    page.evaluate('finish(0,result("search","first",100))')
    page.evaluate('window.oldMore=document.querySelector("#search-more");oldMore.click()')
    assert page.evaluate('requests[1].arguments.query') == 'needle'
    assert page.evaluate('requests[1].arguments.offset') == 100
    assert page.evaluate('requests[1].arguments.workspace_id') == 'workspace-A'
    page.evaluate('closeModal();requests[1].finish(result("search","late"))')
    page.wait_for_function('document.querySelector(".modal")===null')
    page.evaluate('oldMore.click()')
    assert page.evaluate('requests.length') == 2


def test_stale_search_result_cannot_read_in_new_workspace(scope_page):
    page = scope_page
    page.evaluate('launch("search")')
    page.evaluate('finish(0,result("search"))')
    page.evaluate('resetWork("project-one","workspace-B");document.querySelector(".search-hit").click()')
    assert page.evaluate('requests.length') == 1


def test_backup_restore_uses_button_scope_not_mutable_history_globals(scope_page):
    page = scope_page
    page.evaluate('launch("history")')
    page.evaluate('finish(0,result("history","A"))')
    page.evaluate('''() => {
      S.historyProject='wrong-project';S.historyWorkspace='wrong-workspace';
      window.confirm=()=>true;
      const b=document.querySelector('[data-action=restore-backup]');
      window.restoreJob=restoreBackup(b.dataset.id,b.dataset.path,b.workScope).catch(e=>failures.push(e.message));
    }''')
    assert page.evaluate('requests[1].arguments.workspace_id') == 'workspace-A'
    assert page.evaluate('requests[1].arguments.project') == 'project-one'
    page.evaluate('requests[1].finish({sha256:"fixture-sha"})')
    page.wait_for_function('requests.length===3')
    args = page.evaluate('requests[2].arguments')
    assert args['workspace_id'] == 'workspace-A'
    assert args['backup_id'] == 'backup-A'
    assert args['expected_sha256'] == 'fixture-sha'
    # A server-side backup-root rejection is retained; no success or editor refresh.
    page.evaluate('''async()=>{requests[2].finish({error:{code:'BACKUP_ROOT_MISMATCH',message:'Wrong backup root'}},400);await restoreJob;}''')
    assert page.evaluate('failures') == ['Wrong backup root']
    expect(page.locator('.modal')).to_have_count(1)


@pytest.mark.parametrize('invalidate', [
    "resetWork('project-one','workspace-B')",
    "closeModal();modal('Replacement','Keep this dialog')",
])
def test_backup_restore_stops_before_write_when_scope_changes_during_read(scope_page, invalidate):
    page = scope_page
    page.evaluate('launch("history")')
    page.evaluate('finish(0,result("history"))')
    page.evaluate('''() => {window.confirm=()=>true;const b=document.querySelector('[data-action=restore-backup]');
      window.restoreJob=restoreBackup(b.dataset.id,b.dataset.path,b.workScope).catch(e=>failures.push(e.message));}''')
    page.evaluate(invalidate)
    page.evaluate('async()=>{requests[1].finish({sha256:"fixture-sha"});await restoreJob;}')
    assert page.evaluate('requests.length') == 2
    assert page.evaluate('failures.length') == 1


@pytest.mark.parametrize('transition', [
    "S.work.workspace_id='workspace-B'",
    "resetWork('project-one','workspace-B');resetWork('project-one','workspace-A')",
])
def test_completed_restore_does_not_refresh_a_changed_work_scope(scope_page, transition):
    page = scope_page
    page.evaluate('launch("history")')
    page.evaluate('finish(0,result("history","A"))')
    page.evaluate('''() => {
      window.confirm=()=>true;
      const b=document.querySelector('[data-action=restore-backup]');
      window.restoreJob=restoreBackup(b.dataset.id,b.dataset.path,b.workScope).catch(e=>failures.push(e.message));
    }''')
    page.evaluate('requests[1].finish({sha256:"before-restore"})')
    page.wait_for_function('requests.length===3')
    page.evaluate(transition)
    page.evaluate('''() => {
      S.work.content='new workspace draft';S.work.original='new baseline';S.work.path='other.txt';
      closeModal();modal('Replacement','Keep this dialog');
    }''')
    page.evaluate('async()=>{requests[2].finish({ok:true});await restoreJob;}')
    assert page.evaluate('requests.length') == 3
    assert page.evaluate('S.work.content') == 'new workspace draft'
    expect(page.locator('#modal-title')).to_have_text('Replacement')
    assert page.evaluate('failures') == []


def test_normal_restore_reads_restored_file_in_original_workspace(scope_page):
    page = scope_page
    page.evaluate('launch("history")')
    page.evaluate('finish(0,result("history","A"))')
    page.evaluate('''() => {
      window.confirm=()=>true;
      const b=document.querySelector('[data-action=restore-backup]');
      window.restoreJob=restoreBackup(b.dataset.id,b.dataset.path,b.workScope).catch(e=>failures.push(e.message));
    }''')
    page.evaluate('requests[1].finish({sha256:"before-restore"})')
    page.wait_for_function('requests.length===3')
    page.evaluate('requests[2].finish({ok:true})')
    page.wait_for_function('requests.length===4')
    assert page.evaluate('requests[3].arguments.workspace_id') == 'workspace-A'
    assert page.evaluate('requests[3].arguments.path') == 'A.txt'
    page.evaluate('requests[3].finish({path:"A.txt",sha256:"restored-sha",content:"restored",truncated:false})')
    page.wait_for_function('requests.length===5')
    page.evaluate('async()=>{requests[4].finish({entries:[],next_offset:null});await restoreJob;}')
    assert page.evaluate('S.work.content') == 'restored'
    assert page.evaluate('S.work.sha') == 'restored-sha'
    assert page.evaluate('failures') == []
