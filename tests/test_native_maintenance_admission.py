"""Start admission must be visible before PTY creation; maintenance fences it."""
import threading
import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import uuid

import pytest
from tests.test_native_cli import native
from shared.util import DevError


def test_native_start_rejects_maintenance_and_handoff(native):
    obj,p=native
    state={'locked':True,'handoff':False}
    lifecycle=SimpleNamespace(lock=SimpleNamespace(locked=lambda:state['locked']),handoff_pending=False)
    obj.agent.lifecycle=lifecycle
    for locked,handoff in [(True,True),(False,True)]:
        state['locked']=locked;lifecycle.handoff_pending=handoff
        with pytest.raises(DevError,match='维护'):
            obj.action('start',p,{'id':uuid.uuid4().hex,'cli':'codex'})
    assert not obj.children and not obj.pending_starts


def test_pending_process_launch_is_already_busy_to_lifecycle(native,monkeypatch):
    obj,p=native;entered=threading.Event();finish=threading.Event();sid=uuid.uuid4().hex
    def pending(action,project,args):
        entered.set();assert finish.wait(5)
        return {'id':args['id']}
    monkeypatch.setattr(obj,'_action',pending)
    with ThreadPoolExecutor(max_workers=1) as pool:
        operation=pool.submit(obj.action,'start',p,{'id':sid,'cli':'codex'})
        try:
            assert entered.wait(3)
            assert any(row['id']==sid for row in obj.live())
        finally:finish.set()
        assert operation.result()['id']==sid
    assert not obj.pending_starts


@pytest.mark.asyncio
async def test_native_transport_waits_for_lifecycle_lock_without_rejecting_own_lock(native,monkeypatch):
    obj,p=native;socket=object();replies=[];calls=[]
    lifecycle=SimpleNamespace(lock=asyncio.Lock(),plan_dir=obj.directory/'fixture-plans',handoff_pending=False)
    lifecycle.plan_dir.mkdir();obj.agent.lifecycle=lifecycle;obj.agent.socket=socket
    async def send(message):replies.append(message)
    obj.agent.send=send
    def launch(action,project,args):
        assert lifecycle.lock.locked()  # This is the admitted request's OWN lock.
        calls.append(action);return {'id':args['id']}
    monkeypatch.setattr(obj,'_action',launch)
    await lifecycle.lock.acquire()
    task=asyncio.create_task(obj.handle({'request_id':uuid.uuid4().hex,'action':'start','project':p,'args':{'id':uuid.uuid4().hex,'cli':'codex'}},socket))
    try:
        await asyncio.sleep(.03)
        assert not calls and not task.done()
    finally:lifecycle.lock.release()
    await asyncio.wait_for(task,3)
    assert calls==['start'] and replies[0]['result']['ok']
