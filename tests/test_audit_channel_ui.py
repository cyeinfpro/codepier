"""Run shipped UI helpers using escaped synthetic status data."""
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_channel_health_and_upload_guidance_are_safe_and_actionable():
    node = shutil.which('node') or '/opt/homebrew/bin/node'
    script = r"""
const fs=require('node:fs'), vm=require('node:vm'), assert=require('node:assert/strict');
const esc=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
const context={
  window:{CodePierRoomStore:{create:()=>({state:{}})},addEventListener:()=>{}},
  CP:{ui:{esc}},Map,Set,Date,Math,JSON,Number,String,
};
vm.runInNewContext(fs.readFileSync('web/collaboration-room.js','utf8'),context);
const p=context.window.CodePierCollaboration.presentation;
assert.equal(p.inboundHealthMarkup([]),'');
const html=p.inboundHealthMarkup([{label:'<img src=x>',inbound_health:[
  {state:'deferred',reason_code:'BACKLOG',count:2},
  {state:'quarantined',reason_code:'<script>alert(1)</script>',count:1},
  {state:'committed',reason_code:'HIDDEN',count:7},
]}]);
assert(html.includes('等待积压消化') && html.includes('已隔离'));
assert(html.includes('2 条') && html.includes('1 条'));
assert(!html.includes('<img') && !html.includes('<script>') && !html.includes('HIDDEN'));
assert(html.includes('隔离消息不会执行任务'));
assert.equal(p.uploadEstimateMarkup(null),'');
assert.equal(p.uploadEstimateMarkup({estimated_seconds:'invalid'}),'');
const estimate=p.uploadEstimateMarkup({estimated_seconds:158,reference_bytes:33554432,note:'结果未知不要重复发送 <img>'});
assert(estimate.includes('3 分钟'));
assert(estimate.includes('32'));
assert(estimate.includes('外发文件仍需单独批准'));
assert(estimate.includes('结果未知不要重复发送 &lt;img&gt;'));
"""
    result = subprocess.run([node, '-e', script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
