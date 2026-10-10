"""Measured panel query plans and event/worker admission regressions.

All databases, pages and Agent state are disposable fixtures. No production
credentials, database copies, native CLI, or external network are used.
"""
import asyncio
import json
import sqlite3
import statistics
import time
from types import SimpleNamespace

import pytest
from hub import iam
from hub.store import Store
from tests.javascript_support import panel_function
from tests.test_audit_agent import local_agent  # noqa: F401


def test_panel_indexes_cover_counts_and_recent_activity_without_privacy_change(tmp_path):
    store = Store(tmp_path / 'hub')
    try:
        definitions = store.all("SELECT name,sql FROM sqlite_master WHERE name LIKE 'operations_panel_%'")
        assert len(definitions) == 2
        # Use a separate connection and synthetic wide rows for stable planner
        # and VM-work measurements, never timing thresholds on a busy machine.
        db = sqlite3.connect(tmp_path / 'benchmark.sqlite3')
        db.execute("CREATE TABLE operations(id TEXT PRIMARY KEY, space_id TEXT, created REAL, state TEXT, owner_user_id TEXT, visibility TEXT, grant_id TEXT, project_id TEXT, payload TEXT)")
        db.execute("CREATE INDEX operations_space ON operations(space_id)")
        db.execute("CREATE INDEX op_recent ON operations(created DESC)")
        rows = 36470
        db.executemany("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?)", (
            (str(i), 'other' if i % 13 == 0 else 'legacy', float(i),
             'running' if i % 503 == 0 else 'succeeded', 'owner' if i % 3 else 'someone',
             'space' if i % 17 == 0 else 'private', 'grant' if i % 2 else 'another',
             'project', 'x' * 4096) for i in range(rows)))
        db.commit()
        def measure(sql, args):
            samples = []
            for _ in range(3):
                start = time.perf_counter()
                result = db.execute(sql, args).fetchall()
                samples.append((time.perf_counter() - start) * 1000)
            calls = [0]
            def progress():
                calls[0] += 1
                return 0
            db.set_progress_handler(progress, 100)
            result = db.execute(sql, args).fetchall()
            db.set_progress_handler(None, 0)
            plan = [r[3] for r in db.execute("EXPLAIN QUERY PLAN " + sql, args)]
            return {'median_ms': statistics.median(samples), 'vm_steps_approx': calls[0] * 100,
                    'result': result, 'plan': plan}
        queries = {}
        for name, principal in (
            ('instance', SimpleNamespace(space_id='legacy', grant_id=None, instance_admin=True)),
            ('owner', SimpleNamespace(space_id='legacy', grant_id=None, instance_admin=False, user_id='owner')),
            ('grant', SimpleNamespace(space_id='legacy', grant_id='grant', instance_admin=False)),
        ):
            clause, args = iam.private_sql(principal)
            queries[name + '_today'] = ("SELECT state,count(*) FROM operations WHERE " + clause + " AND created>=? GROUP BY state", [*args, rows - 1000])
            queries[name + '_active'] = ("SELECT count(*) FROM operations WHERE " + clause + " AND state IN ('running','queued','reconnecting','cancelling')", args)
        queries['recent'] = ("SELECT id FROM operations WHERE space_id=? ORDER BY created DESC LIMIT 8", ['legacy'])
        before = {name: measure(*query) for name, query in queries.items()}
        for definition in definitions:
            db.execute(definition['sql'])
        db.commit()
        after = {name: measure(*query) for name, query in queries.items()}
        for name in queries:
            assert before[name]['result'] == after[name]['result'], name
            assert any('operations_panel_' in plan for plan in after[name]['plan']), after[name]
            if name != 'recent':
                assert any('COVERING INDEX' in plan for plan in after[name]['plan']), after[name]
            assert after[name]['vm_steps_approx'] < before[name]['vm_steps_approx'] / 5, name
        print("PANEL_DB_BENCHMARK=" + json.dumps({'rows': rows, 'payload_bytes_per_row': 4096, 'before': before, 'after': after}))
        db.close()
        # A second startup reuses additive indexes and preserves existing data.
        store.execute("INSERT INTO meta VALUES ('panel-benchmark-preserve','yes')")
    finally:
        store.close()
    reopened = Store(tmp_path / 'hub')
    try:
        assert reopened.one("SELECT value FROM meta WHERE key='panel-benchmark-preserve'")['value'] == 'yes'
        assert len(reopened.all("SELECT name FROM sqlite_master WHERE name LIKE 'operations_panel_%'")) == 2
    finally:
        reopened.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('tool,args', [
    ('fs_tree', {'path': '.'}),
    ('fs_read', {'path': 'file.txt'}),
    ('fs_read_many', {'paths': ['file.txt']}),
])
async def test_panel_reads_use_existing_io_lane_without_bypassing_resource_locks(local_agent, tool, args):
    agent, root = local_agent
    (root / 'file.txt').write_text('fixture')
    project = {'root': str(root), 'alias': 'fixture', 'mode': 'write'}
    from contextlib import AsyncExitStack
    holders = AsyncExitStack()
    for index in range(agent.scheduler.lane_limit("execution")):
        await holders.enter_async_context(agent.scheduler.slot(f"heavy-{index}", f"project-{index}"))
    entered = asyncio.Event()
    async def read():
        async with agent.execution_slot('fixture-read', tool, project, args, root, False, time.time() + 2):
            entered.set()
    try:
        # Commands own all heavy slots; a non-conflicting bounded read proceeds.
        await asyncio.wait_for(read(), .5)
        assert entered.is_set()
        entered.clear()
        # The same lightweight read still waits behind a conflicting path writer.
        async with agent.project_slot(root, write=True):
            task = asyncio.create_task(read())
            await asyncio.sleep(.03)
            assert not entered.is_set()
        await asyncio.wait_for(task, .5)
        assert entered.is_set()
    finally:
        await holders.aclose()
    assert agent.scheduler.snapshot()["running"] == 0
    assert not agent.resources.active and not agent.resources.waiting


@pytest.mark.asyncio
async def test_panel_read_queue_timeout_and_cancel_release_claims(local_agent):
    from shared.util import DevError
    agent, root = local_agent
    project = {'root': str(root), 'alias': 'fixture', 'mode': 'write'}
    from contextlib import AsyncExitStack
    holders = AsyncExitStack()
    for index in range(agent.scheduler.lane_limit("read")):
        await holders.enter_async_context(agent.scheduler.slot(f"io-{index}", f"project-{index}", "read"))
    async def read(deadline):
        async with agent.execution_slot('fixture-read', 'fs_tree', project, {'path': '.'}, root, False, deadline):
            raise AssertionError('No I/O worker should be available')
    try:
        with pytest.raises(DevError, match='首次执行期限'):
            await read(time.time() + .03)
        task = asyncio.create_task(read(time.time() + 10))
        await asyncio.sleep(.03)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        await holders.aclose()
    assert not agent.resources.active and not agent.resources.waiting


@pytest.fixture
def event_page(chat_browser_pool):
    page = chat_browser_pool('chromium').new_page()
    page.set_content('<div id="page"></div>')
    page.evaluate("""() => {
      window.S={session:{user_id:'fixture'},space_id:'legacy',page:'projects',work:{}};
      window.$=s=>document.querySelector(s);
      window.networkState=()=>{};window.toast=()=>{};
      window.CodePierIdentity={refresh:async()=>{}};
      window.renderCalls=0;window.basicCalls=0;window.inflight=0;window.maxInflight=0;
      window.renderPage=async()=>{
        renderCalls++;inflight++;maxInflight=Math.max(maxInflight,inflight);
        await new Promise(r=>setTimeout(r,window.renderDelay||0));inflight--;
      };
      window.loadBasics=async()=>{basicCalls++;};
      window.EventSource=class extends EventTarget {constructor(){super();window.stream=this;}close(){this.closed=true;}};
    }""")
    for name in ('invalidateBasics', 'stopEvents', 'connectEvents'):
        page.add_script_tag(content=panel_function(name))
    page.evaluate('connectEvents()')
    yield page
    page.close()


def test_first_event_connection_does_not_repeat_boot_reads(event_page):
    page = event_page
    page.evaluate('stream.onopen()')
    page.wait_for_timeout(60)
    print("PANEL_FIRST_CONNECT=" + json.dumps(page.evaluate('({renders:renderCalls,basics:basicCalls})')))
    assert page.evaluate('renderCalls') == 0
    assert page.evaluate('basicCalls') == 0
    page.evaluate('stream.onopen()')
    page.wait_for_timeout(60)
    assert page.evaluate('renderCalls') == 1


def test_operation_events_do_not_rerender_unrelated_project_page(event_page):
    page = event_page
    for _ in range(3):
        page.evaluate("stream.onmessage({data:JSON.stringify({type:'operation',data:{id:'fixture'}})})")
        page.wait_for_timeout(550)
    print("PANEL_OPERATION_REFRESHES=" + str(page.evaluate('renderCalls')))
    assert page.evaluate('renderCalls') == 0
    page.evaluate("stream.onmessage({data:JSON.stringify({type:'project',data:{id:'fixture'}})})")
    page.wait_for_function('renderCalls === 1', timeout=5000)
    assert page.evaluate('renderCalls') == 1


def test_slow_event_refresh_is_single_flight_with_one_trailing_refresh(event_page):
    page = event_page
    page.evaluate("S.page='overview';window.renderDelay=1800")
    page.evaluate("stream.onmessage({data:JSON.stringify({type:'operation'})})")
    page.wait_for_function('renderCalls === 1')
    page.evaluate("stream.onmessage({data:JSON.stringify({type:'operation'})})")
    page.wait_for_timeout(800)
    page.evaluate("stream.onmessage({data:JSON.stringify({type:'operation'})})")
    page.wait_for_function('renderCalls >= 2 && inflight === 0', timeout=15000)
    print("PANEL_REFRESH_CONCURRENCY=" + str(page.evaluate('maxInflight')))
    assert page.evaluate('maxInflight') == 1
    assert 2 <= page.evaluate('renderCalls') <= 3



def test_background_refresh_does_not_reopen_page_after_navigation_or_reconnect(event_page):
    page = event_page
    page.evaluate("stream.onmessage({data:JSON.stringify({type:'project'})});S.page='native'")
    page.wait_for_timeout(800)
    assert page.evaluate('renderCalls') == 0
    page.evaluate("S.page='projects';window.oldStream=stream;stream.onmessage({data:JSON.stringify({type:'project'})});connectEvents()")
    page.wait_for_timeout(800)
    assert page.evaluate('oldStream.closed') is True
    assert page.evaluate('renderCalls') == 0


def test_basics_inflight_reads_are_shared_and_never_cross_identity(event_page):
    page = event_page
    page.add_script_tag(content=panel_function('loadBasics'))
    page.evaluate("""() => {
      window.sessionChanged=()=>new Error('SESSION_CHANGED');
      window.pending=[];window.requests=0;
      window.api=path=>{requests++;return new Promise(resolve=>pending.push({path,resolve}));};
      window.first=loadBasics().catch(e=>e.message);
      window.joined=loadBasics().catch(e=>e.message);
    }""")
    assert page.evaluate('requests') == 2
    page.evaluate("""() => {
      S.space_id='other';
      window.newest=loadBasics();
      pending[2].resolve({devices:['new-device']});pending[3].resolve({projects:['new-project']});
    }""")
    page.evaluate('newest')
    assert page.evaluate('requests') == 4
    page.evaluate("pending[0].resolve({devices:['stale']});pending[1].resolve({projects:['stale']})")
    assert page.evaluate('first') == 'SESSION_CHANGED'
    assert page.evaluate('joined') == 'SESSION_CHANGED'
    assert page.evaluate('S.projects') == ['new-project']
    assert page.evaluate('S.devices') == ['new-device']
    assert page.evaluate('S.basicsRequest') is None


def test_basics_response_from_before_mutation_is_revalidated(event_page):
    page = event_page
    page.add_script_tag(content=panel_function('loadBasics'))
    page.evaluate("""() => {
      window.sessionChanged=()=>new Error('SESSION_CHANGED');
      window.pending=[];
      window.api=path=>new Promise(resolve=>pending.push({path,resolve}));
      window.first=loadBasics();
      invalidateBasics();
      window.second=loadBasics();
      pending[0].resolve({devices:['old']});pending[1].resolve({projects:['old']});
    }""")
    page.wait_for_timeout(30)
    assert page.evaluate('pending.length') == 4
    assert page.evaluate('S.projects || []') == []
    page.evaluate("pending[2].resolve({devices:['new']});pending[3].resolve({projects:['new']})")
    page.evaluate('Promise.all([first,second])')
    assert page.evaluate('S.projects') == ['new']
    assert page.evaluate('S.basicsRequest') is None


def test_failed_basics_are_not_cached(event_page):
    page = event_page
    page.add_script_tag(content=panel_function('loadBasics'))
    page.evaluate("""async () => {
      window.api=async()=>{throw new Error('offline');};
      await loadBasics().catch(()=>{});
      window.requests=0;
      window.api=async path=>{requests++;return path.endsWith('devices')?{devices:[]}:{projects:[]};};
      await loadBasics();
    }""")
    assert page.evaluate('requests') == 2


def test_panel_indexes_follow_legacy_iam_migration(tmp_path):
    from tests.legacy_iam_fixture import legacy_store
    directory = tmp_path / 'legacy-hub'
    old = legacy_store(directory)
    try:
        assert 'space_id' not in {row['name'] for row in old.all('PRAGMA table_info(operations)')}
        old.execute("INSERT INTO meta VALUES ('panel-legacy-preserve','yes')")
    finally:
        old.close()
    upgraded = Store(directory)
    try:
        assert upgraded.one("SELECT value FROM meta WHERE key='panel-legacy-preserve'")['value'] == 'yes'
        assert len(upgraded.all("SELECT name FROM sqlite_master WHERE name LIKE 'operations_panel_%'")) == 2
    finally:
        upgraded.close()
