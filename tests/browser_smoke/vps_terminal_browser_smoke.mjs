// Real xterm browser smoke test with a deterministic local SSH wire fixture.
// Requires Playwright; CODEPIER_BROWSER_ENGINE selects Chromium or WebKit.
// This validates real xterm rendering with a synthetic peer, not an SSH server.
import http from 'node:http';
import { mkdir, readFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
let playwright;
try {
  playwright = require('playwright');
} catch (error) {
  if (!process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES) throw error;
  playwright = require(process.env.CODEX_PRIMARY_RUNTIME_NODE_MODULES + '/playwright');
}
const engine = process.env.CODEPIER_BROWSER_ENGINE || 'chromium';
if (!['chromium', 'webkit'].includes(engine)) throw new Error('Unsupported browser engine');
const evidenceDirectory = process.env.CODEPIER_BROWSER_EVIDENCE_DIR;
if (evidenceDirectory) await mkdir(evidenceDirectory, { recursive: true });
const web = resolve(dirname(fileURLToPath(import.meta.url)), '../../web');
const fixture = `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CodePier terminal QA</title><link rel="stylesheet" href="/tokens.css"><link rel="stylesheet" href="/vps-terminal.css"><style>body{margin:0;padding:20px;background:var(--ui-bg);color:var(--ui-text);font-family:var(--ui-font)}#mount{max-width:1500px;margin:auto}*{box-sizing:border-box}</style><body><main id="mount"></main><script>
window.sockets=[];window.inputs=[];window.sessions=new Map();window.fixtureErrors=[];let serial=0;
function fixtureFailure(message){fixtureErrors.push(message);throw Error(message)}
class MockWebSocket {
 constructor(url){this.url=url;this.readyState=0;this.bufferedAmount=0;this.offset=0n;this.acked=0n;this.nextInput=1n;window.sockets.push(this);setTimeout(()=>{if(this.readyState!==0)return;this.readyState=1;this.onopen?.();},1)}
 send(data){
  if(this.readyState!==1)fixtureFailure('Write to closed fixture socket');
  if(typeof data==='string'){
   const m=JSON.parse(data);
   if(m.type==='authenticate'){
    if(m.protocol_version!==1||!m.renderer_id||!m.ticket.startsWith('ticket-'))fixtureFailure('Invalid fixture authentication');
    this.id=m.ticket.slice(7);
    this.emit({type:'attached',session_id:this.id,runtime_generation:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',attachment_id:this.id,render_epoch:'1',stream_epoch:'1',writer_epoch:'1',start_offset:'0',output_end_offset:'0',next_input_seq:'1',cols:100,rows:30,mode:'tmux',resume_strategy:'live',state:'ready'});
    this.output('\\x1b[32mWelcome to CodePier\\x1b[0m\\r\\nDebian GNU/Linux · kernel 6.1\\r\\n中文输入 · 终端已连接\\r\\n\\x1b[33mroot@hongkong\\x1b[0m:~# ');
   }else if(m.type==='ping')this.emit({type:'pong',nonce:m.nonce});
   else if(m.type==='auth_answer'){window.lastAnswer=m;this.emit({type:'state',state:'ready'});}
   else if(m.type==='output_ack'){
    const offset=BigInt(m.rendered_offset);
    if(m.attachment_id!==this.id||m.render_epoch!=='1'||m.stream_epoch!=='1'||offset<this.acked||offset>this.offset)fixtureFailure('Invalid consumed-output ACK');
    this.acked=offset;
   }else if(m.type==='resize'){
    if(m.attachment_id!==this.id||m.runtime_generation!=='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'||m.stream_epoch!=='1'||m.writer_epoch!=='1'||m.cols<2||m.rows<1)fixtureFailure('Invalid resize binding');
   }else if(!['pong','detach'].includes(m.type))fixtureFailure('Unexpected terminal control: '+m.type);
  }else{
   const v=new DataView(data),hex=bytes=>Array.from(bytes,x=>x.toString(16).padStart(2,'0')).join('');
   if(data.byteLength<62||v.getUint8(0)!==1||v.getUint8(1)!==1||data.byteLength!==62+v.getUint32(58)||v.getUint32(58)>16384||v.getBigUint64(34)!==1n||v.getBigUint64(42)!==1n||v.getBigUint64(50)!==this.nextInput||hex(new Uint8Array(data,2,16))!==this.id||hex(new Uint8Array(data,18,16))!=='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa')fixtureFailure('Invalid input frame or sequence');
   this.nextInput++;
   const value=new TextDecoder().decode(new Uint8Array(data,62));
   window.inputs.push({id:this.id,value,sequence:v.getBigUint64(50).toString()});
   this.emit({type:'input_ack',attachment_id:this.id,render_epoch:'1',stream_epoch:'1',runtime_generation:'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',writer_epoch:'1',input_seq:v.getBigUint64(50).toString(),status:'forwarded'});
   this.output(value);
  }
 }
 emit(data){queueMicrotask(()=>this.onmessage?.({data:JSON.stringify(data)}))}
 output(text){const p=new TextEncoder().encode(text), b=new Uint8Array(62+p.length),v=new DataView(b.buffer);b[0]=1;b[1]=2;for(let i=0;i<16;i++){b[2+i]=parseInt(this.id.slice(i*2,i*2+2),16);b[18+i]=170}v.setBigUint64(34,1n);v.setBigUint64(42,1n);v.setBigUint64(50,this.offset);v.setUint32(58,p.length);b.set(p,62);this.offset+=BigInt(p.length);queueMicrotask(()=>this.onmessage?.({data:b.buffer}))}
 close(code=1000){this.readyState=3;this.onclose?.({code})}
}
window.WebSocket=MockWebSocket;
window.api=async function(path,args){if(path.includes('capabilities'))return {available:true};if(path.endsWith('attach-ticket'))return {ticket:'ticket-'+path.split('/')[4],ws_path:'/ws/vps-terminal'};if(path.endsWith('/diagnostics'))return {session_id:path.split('/')[4],protocol_version:1,collected_at:1805800000,links:{browser:{connected:true,status:'connected'},agent:{connected:true,status:'connected',control_rtt_ms:null},ssh:{connected:null,status:'unconfirmed'}},bytes:{input:'0',output:'1024'},stages_ms:{ssh_connect:42,pty_open:0},metrics:{counters:{reconnect_total:0},gauges:{browser_buffer_bytes:0}},lifecycle:{recover_count:0,version:{hub:'fixture',agent:'fixture',protocol:1}}};if(path.endsWith('/close'))return {state:'exited',output_complete:true};if(path.endsWith('/takeover'))return {state:'ready'};if(path==='/api/vps-terminal/sessions'&&args?.method==='POST'){const body=JSON.parse(args.body),id=(++serial).toString(16).padStart(32,'0'),s={session_id:id,vps_id:body.vps_id,project_id:body.project_id,vps_name:'香港 UC',project_alias:'CodePier',device_name:'Mac Studio',username:'root',mode:'tmux',state:'connecting'};sessions.set(id,s);return s}if(path==='/api/vps-terminal/sessions')return {sessions:[...sessions.values()]};if(path.startsWith('/api/vps-terminal/sessions/'))return sessions.get(path.split('/')[4]);throw Error(path)};
</script><script src="/vps-terminal.js"></script><script>
window.boot=async()=>{CodePierVpsTerminal.configure({api:window.api,getIdentity:()=> 'user-one:space-one',onNavigate:(id)=>{window.lastNavigate=id;if(id)void CodePierVpsTerminal.mount(document.querySelector('#mount'),{sessionId:id})}});await CodePierVpsTerminal.mount(document.querySelector('#mount'));await CodePierVpsTerminal.open({vps:{id:'vps-one',name:'香港 UC',username:'root'},project:{id:'project-one',alias:'CodePier',device_name:'Mac Studio'}})};boot();
</script></body></html>`;
const server = http.createServer(async (req, res) => {
  try {
    const path = new URL(req.url, 'http://local').pathname;
    if (path === '/') {
      res.setHeader('Content-Type', 'text/html; charset=utf-8');
      res.end(fixture);
      return;
    }
    if (!['/vps-terminal.js', '/vps-terminal.css', '/tokens.css'].includes(path)) {
      res.writeHead(404);
      res.end();
      return;
    }
    const file = web + path;
    res.setHeader('Content-Type', path.endsWith('.css') ? 'text/css' : 'text/javascript');
    res.end(await readFile(file));
  } catch (e) {
    res.writeHead(500);
    res.end(e.message);
  }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const url = `http://127.0.0.1:${server.address().port}/`;
let browser, page;
try {
  browser = await playwright[engine].launch({
    headless: true,
    ...(engine === 'chromium' && process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH }
      : engine === 'chromium' && process.platform === 'darwin'
        ? { channel: 'chromium' }
        : {}),
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    ...(engine === 'chromium' ? { permissions: ['clipboard-read', 'clipboard-write'] } : {}),
  });
  page = await context.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(url);
  await page.locator('.vt-state').filter({ hasText: '已连接' }).waitFor();
  await page.locator('.xterm-helper-textarea').focus();
  await page.keyboard.type('echo hello');
  await page.keyboard.press('Control+c');
  await page.keyboard.insertText('中文');
  await page.waitForFunction(() =>
    inputs
      .map((x) => x.value)
      .join('')
      .includes('中文'),
  );
  const typed = await page.evaluate(() => inputs.map((x) => x.value).join(''));
  assert(typed.includes('echo hello'));
  assert(typed.includes('\x03'));
  assert(typed.includes('中文'));
  await page.evaluate(() => (window.originalXterm = document.querySelector('.xterm')));
  await page.getByRole('button', { name: '分屏', exact: true }).click();
  await page
    .locator('.vt-pane')
    .nth(1)
    .locator('.vt-state')
    .filter({ hasText: '已连接' })
    .waitFor();
  assert.equal(await page.locator('.vt-slot .xterm').count(), 2);
  await page.evaluate(async () => {
    CodePierVpsTerminal.hide();
    document.querySelector('#mount').replaceChildren();
    await CodePierVpsTerminal.mount(document.querySelector('#mount'));
  });
  assert.equal(
    await page.evaluate(() => document.querySelector('.xterm') === window.originalXterm),
    true,
  );
  await page.getByRole('button', { name: '查找', exact: true }).click();
  const search = page.getByRole('searchbox', { name: '查找当前终端内容' }).last();
  await search.fill('missing-terminal-fixture-string');
  await search.press('Enter');
  await page.locator('.vt-search-result').filter({ hasText: '没有匹配' }).waitFor();
  await search.fill('kernel');
  await search.press('Enter');
  assert.equal(await page.locator('.vt-search:visible .vt-search-result').textContent(), '');
  await page.getByRole('button', { name: '关闭', exact: true }).last().click();
  const beforePaste = await page.evaluate(() => inputs.length);
  if (engine === 'chromium') {
    await page.evaluate(() => navigator.clipboard.writeText('echo first\necho second'));
    await page.getByRole('button', { name: '粘贴', exact: true }).click();
  } else {
    // WebKit does not expose Chromium's clipboard permission grant. Exercise
    // the actual DOM paste handler with synthetic clipboard data instead.
    await page.locator('.vt-pane-focused .xterm-helper-textarea').evaluate((node) => {
      const data = new DataTransfer();
      data.setData('text/plain', 'echo first\necho second');
      node.dispatchEvent(
        new ClipboardEvent('paste', { bubbles: true, cancelable: true, clipboardData: data }),
      );
    });
  }
  await page.getByRole('textbox', { name: '粘贴前查看和编辑内容' }).waitFor();
  await page.locator('.vt-paste').getByRole('button', { name: '取消', exact: true }).last().click();
  assert.equal(await page.evaluate(() => inputs.length), beforePaste);
  await page.evaluate(() => {
    const socket = sockets.at(-1);
    socket.emit({
      type: 'auth_challenge',
      challenge_id: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      prompts: [{ text: '一次性验证码', echo: false }],
      deadline_seconds: 60,
    });
  });
  await page.getByLabel('一次性验证码').fill('123456');
  await page.getByRole('button', { name: '继续连接' }).click();
  assert.equal(await page.locator('.vt-auth input').count(), 0);
  assert.equal(await page.evaluate(() => lastAnswer.responses[0]), '123456');
  assert.equal(
    await page.evaluate(() => inputs.some((item) => item.value.includes('123456'))),
    false,
  );
  await page.getByRole('button', { name: '诊断', exact: true }).click();
  const diagnostic = page.locator('.vt-diagnostics:visible');
  await diagnostic.locator('.vt-diagnostics-link strong').first().waitFor();
  assert.deepEqual(await diagnostic.locator('.vt-diagnostics-link strong').allTextContents(), [
    '已连接',
    '已连接',
    '未确认',
  ]);
  assert.equal(
    await diagnostic
      .locator('.vt-diagnostics-content > .vt-diagnostics-values dd')
      .last()
      .textContent(),
    '未采集',
  );
  if (evidenceDirectory)
    await page.screenshot({
      path: resolve(evidenceDirectory, `terminal-smoke-${engine}-diagnostics.png`),
    });
  await diagnostic.getByRole('button', { name: '收起', exact: true }).click();
  if (evidenceDirectory)
    await page.screenshot({
      path: resolve(evidenceDirectory, `terminal-smoke-${engine}-desktop.png`),
    });
  await page.setViewportSize({ width: 390, height: 844 });
  await page
    .locator('.vt-toolbar button')
    .filter({ hasText: /^(合并分屏|分屏)$/ })
    .waitFor({ state: 'hidden' });
  assert.equal(await page.locator('.vt-mobile-keys:visible').count(), 1);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
  assert.equal(overflow, false);
  const mobileKeys = page.locator('.vt-mobile-keys:visible');
  const beforeKey = await page.evaluate(() => inputs.length);
  await mobileKeys.getByRole('button', { name: 'Ctrl-C', exact: true }).click();
  await page.waitForFunction((count) => inputs.length > count, beforeKey);
  assert.equal(await page.evaluate(() => inputs.at(-1).value), '\x03');
  if (evidenceDirectory)
    await page.screenshot({
      path: resolve(evidenceDirectory, `terminal-smoke-${engine}-mobile.png`),
    });
  await page.evaluate(() => CodePierVpsTerminal.logout());
  assert.equal(await page.locator('.xterm').count(), 0);
  assert.deepEqual(await page.evaluate(() => fixtureErrors), []);
  assert.deepEqual(errors, []);
  console.log(
    JSON.stringify({
      passed: true,
      engine,
      checks: [
        'real xterm typing/Ctrl-C/Chinese',
        'independent split',
        'stable DOM after hide/mount',
        'search',
        'multiline paste preview/cancel',
        'OTP clears fields',
        'scoped inline diagnostics and unconfirmed status',
        'mobile no overflow/keys',
        'logout cleanup',
      ],
      inputs: typed,
    }),
  );
} catch (error) {
  if (evidenceDirectory && page)
    await page
      .screenshot({
        path: resolve(evidenceDirectory, `terminal-smoke-${engine}-failure.png`),
        timeout: 5000,
      })
      .catch(() => {});
  throw error;
} finally {
  await browser?.close();
  await new Promise((resolve) => server.close(resolve));
}
