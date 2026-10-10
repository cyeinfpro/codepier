"""Navigation/modal intent fences for delayed panel reads."""
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.javascript_support import panel_without_boot
from tests.test_audit_web import HARNESS

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
SCENARIO = r"""
if(name.startsWith('modal_scope:')){
  const [,kind,change]=name.split(':');
  editor();type('draft');run('S.renderSeq=1');
  if(kind==='operation')run("S.page='audit'");
  const opened=[];
  context.makeScopedModal=()=>{const dialog=element();opened.push(dialog);elements.set('.modal',dialog);
    for(const s of ['#confirm-save','#refresh-operation'])elements.set(s,element());return dialog;};
  run('modal=()=>{S.modalIntent=(S.modalIntent||0)+1;return makeScopedModal();}');
  const jobs=[];
  fetchImpl=(path,options)=>{const job=deferred();jobs.push({path,options,...job});return job.promise;};
  const action=kind==='preview'?'previewSave()':kind==='git'?"gitView('git_diff')":kind==='checkpoint'?"checkpoint()":"operationDetail('op')";
  const pending=run(action);await tick();assert.equal(jobs.length,1);
  const payload={id:'op',tool:'exec',state:'succeeded',output:'current',diff:'-base\n+draft'};
  if(change==='error'){
    const checked=assert.rejects(pending,/fixture rejected/);
    jobs[0].resolve(response({error:{message:'fixture rejected'}},400));await checked;
    assert.equal(opened.length,0);return;
  }
  if(change==='navigation'||change==='stale_error')run("S.page='overview';S.renderSeq++");
  if(change==='page_epoch')run('S.renderSeq++');
  if(change==='session')run("S.session={csrf:'different'}");
  if(change==='project')run("S.work.project='other';S.workGeneration=(S.workGeneration||0)+1");
  if(change==='workspace')run("S.work.workspace_id='other'");
  if(change==='file')run("S.work.path='other.txt';S.fileGeneration=(S.fileGeneration||0)+1");
  if(change==='newer_modal')run("modal('newer')");
  if(change==='dismiss')run('S.modalIntent=(S.modalIntent||0)+1');
  if(change==='newer_request'){
    const later=run(action);await tick();assert.equal(jobs.length,2);
    jobs[1].resolve(response(payload));await later;assert.equal(opened.length,1);
  }
  const before=opened.length,existing=elements.get('.modal');
  jobs[0].resolve(change==='stale_error'?response({error:{message:'old error'}},400):response(payload));
  await pending;
  if(change==='success'||change==='stale_confirm'){
    assert.equal(opened.length,1);
    if(kind==='preview')assert.equal(elements.get('#confirm-save').disabled,false);
    if(change==='stale_confirm'){
      run("S.work.path='other.txt';S.fileGeneration=(S.fileGeneration||0)+1");
      await elements.get('#confirm-save').onclick();
      assert.equal(jobs.length,1,'Stale confirmation must not submit fs_write');
    }
  }else{
    assert.equal(opened.length,before,'A stale reply opened or replaced a modal');
    assert.equal(elements.get('.modal'),existing);
  }
}else if(name.startsWith('copy_scope:')){
  const change=name.split(':')[1];editor();run('S.renderSeq=1');
  const opened=[];
  context.makeCopyModal=()=>{const dialog=element();opened.push(dialog);elements.set('.modal',dialog);
    elements.set('#copy-text',{select(){}});return dialog;};
  run('modal=()=>{S.modalIntent=(S.modalIntent||0)+1;return makeCopyModal();}');
  const job=deferred();context.navigator.clipboard={writeText:()=>job.promise};context.window.isSecureContext=true;
  const pending=run("copy('fixture copied text')");
  if(change==='session')run("S.session={csrf:'different'}");
  if(change==='navigation')run("S.page='overview'");
  if(change==='page_epoch')run("S.renderSeq++");
  if(change==='space')run("S.space_id='other'");
  if(change==='newer_modal')run("modal('newer')");
  if(change==='dismiss')run('S.modalIntent=(S.modalIntent||0)+1');
  const before=opened.length,existing=elements.get('.modal');
  if(change==='success')job.resolve();else job.reject(Error('clipboard unavailable'));
  await pending;
  assert.equal(opened.length,change==='fallback'?1:before);
  if(change!=='fallback')assert.equal(elements.get('.modal'),existing);
}else
"""
SCOPE_HARNESS = HARNESS.replace(
    "if(name==='editor_restores_html_stripped_first_newline'){",
    SCENARIO + "if(name==='editor_restores_html_stripped_first_newline'){",
)

CASES = [
    (kind, change)
    for kind in ("preview", "git", "operation", "checkpoint")
    for change in ("navigation", "page_epoch", "session", "project", "workspace",
                   "newer_modal", "dismiss", "success", "error", "stale_error")
] + [("preview", "file"), ("git", "file"), ("git", "newer_request"),
     ("operation", "newer_request"), ("preview", "stale_confirm")]


@pytest.mark.skipif(NODE is None, reason="Node.js is required for modal response tests")
@pytest.mark.parametrize("kind,change", CASES)
def test_late_modal_scope(kind, change, tmp_path):
    fixture = tmp_path / "panel-without-boot.js"
    fixture.write_text(panel_without_boot(), encoding="utf-8")
    result = subprocess.run([NODE, "-e", SCOPE_HARNESS, f"modal_scope:{kind}:{change}", str(fixture)],
                            cwd=ROOT, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SCENARIO_COMPLETED" in result.stdout



@pytest.mark.skipif(NODE is None, reason="Node.js is required for modal response tests")
@pytest.mark.parametrize("change", [
    "session", "navigation", "page_epoch", "space", "newer_modal", "dismiss", "fallback", "success",
])
def test_copy_fallback_scope(change, tmp_path):
    fixture = tmp_path / "panel-without-boot.js"
    fixture.write_text(panel_without_boot(), encoding="utf-8")
    result = subprocess.run([NODE, "-e", SCOPE_HARNESS, f"copy_scope:{change}", str(fixture)],
                            cwd=ROOT, text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SCENARIO_COMPLETED" in result.stdout
