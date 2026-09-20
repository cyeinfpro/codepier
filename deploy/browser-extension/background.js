const HOST = 'com.codepier.browser';
const IDLE = chrome.runtime.getURL('idle.html');
const MAX_POOL = 16;
let port = null, enabled = false, profile = '', sites = [], pool = [], seen = [];
let chain = Promise.resolve(), lastError = '', initialized = null;

function id() { return crypto.randomUUID().replaceAll('-', ''); }
function failure(code, message) { const error = new Error(message); error.code = code; throw error; }
function pattern(value) { const url = new URL(value); return url.protocol + '//' + url.hostname + '/*'; }
function validOrigin(value) {
  try {
    const url = new URL(value);
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.pathname !== '/' || url.hash || url.search) throw new Error();
    return url.origin;
  } catch { failure('BROWSER_ORIGIN_DENIED', '网站 origin 无效'); }
}
function stats() { return {pool_size: pool.length, available: pool.filter(p => !p.lease).length, leased: pool.filter(p => p.lease).length}; }
async function save() {
  await chrome.storage.session.set({codepier_pool: pool, codepier_seen: seen.slice(-256)});
  try { port?.postMessage({type: 'state', state: stats()}); } catch {}
}
async function migrateCodePierStore(storage, keys) {
  const names=keys.flatMap(key=>['codepier_'+key,'relay_'+key]);
  const saved=await storage.get(names);
  for (const key of keys) {
    const old='relay_'+key,current='codepier_'+key;
    if (!(old in saved))continue;
    if (!(current in saved))await storage.set({[current]:saved[old]});
    const check=await storage.get(current);
    if (JSON.stringify(check[current])===JSON.stringify(saved[old]) && storage.remove)await storage.remove(old);
  }
}
async function init() {
  if (initialized) return initialized;
  initialized = (async () => {
    await migrateCodePierStore(chrome.storage.local,['profile','enabled','sites']);
    await migrateCodePierStore(chrome.storage.session,['pool','seen']);
    const local = await chrome.storage.local.get(['codepier_profile', 'codepier_enabled', 'codepier_sites']);
    profile = local.codepier_profile || id();
    enabled = local.codepier_enabled === true;
    sites = Array.isArray(local.codepier_sites) ? local.codepier_sites : [];
    await chrome.storage.local.set({codepier_profile: profile});
    const session = await chrome.storage.session.get(['codepier_pool', 'codepier_seen']);
    pool = Array.isArray(session.codepier_pool) ? session.codepier_pool : [];
    seen = Array.isArray(session.codepier_seen) ? session.codepier_seen : [];
    await reconcile();
    await chrome.alarms.create('codepier-maintenance', {periodInMinutes: 0.5});
    if (enabled) connect();
  })();
  return initialized;
}
async function reconcile() {
  const keep = [];
  for (const row of pool) {
    try {
      const tab = await chrome.tabs.get(row.tab);
      if (tab.windowId !== row.window || tab.groupId !== row.group) continue;
      if (!row.lease && tab.url !== IDLE) continue;
      keep.push(row);
    } catch {}
  }
  pool = keep.slice(0, MAX_POOL);
  await save();
}
async function requireTab(row) {
  let tab;
  try { tab = await chrome.tabs.get(row.tab); } catch { failure('BROWSER_LEASE_MISSING', '工作页已关闭'); }
  if (tab.windowId !== row.window || tab.groupId !== row.group) failure('BROWSER_TAB_CHANGED', '工作页已移出原工作组，未操作该标签页');
  return tab;
}
async function allowed(value, origins) {
  let url;
  try { url = new URL(value); } catch { failure('BROWSER_ORIGIN_DENIED', '网址无效'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || !Array.isArray(origins) || !origins.includes(url.origin) || !sites.includes(url.origin)) {
    failure('BROWSER_ORIGIN_DENIED', '此网站未被双方明确授权');
  }
  if (!await chrome.permissions.contains({origins: [pattern(url.origin)]})) failure('BROWSER_PERMISSION_REQUIRED', '浏览器网站权限尚未授予或已撤销');
  return url;
}
async function release(row) {
  const tab = await requireTab(row);
  await chrome.tabs.update(tab.id, {url: IDLE});
  row.lease = null; row.expires = 0; row.document = null;
  await save();
}
async function expire() {
  for (const row of [...pool]) {
    if (row.lease && row.expires <= Date.now() / 1000) {
      try { await release(row); } catch { pool = pool.filter(p => p !== row); }
    }
  }
  await save();
}
async function prepare(size) {
  if (!Number.isInteger(size) || size < 1 || size > MAX_POOL) throw new Error('工作页数量必须为 1–16');
  await reconcile();
  const win = await chrome.windows.getLastFocused();
  if (!win.focused || win.type !== 'normal') throw new Error('请在正常 Chrome 窗口前台打开扩展，再准备工作页');
  let group = pool.find(p => p.window === win.id)?.group;
  while (pool.length < size) {
    const active = await chrome.windows.get(win.id);
    if (!active.focused) break;
    // Creation is reachable only through the owner popup, never remote dispatch.
    const tab = await chrome.tabs.create({windowId: win.id, url: IDLE, active: false});
    try {
      group = await chrome.tabs.group({... (group !== undefined ? {groupId: group} : {}), tabIds: [tab.id]});
      await chrome.tabGroups.update(group, {title: 'CodePier', color: 'grey', collapsed: true});
      pool.push({tab: tab.id, window: win.id, group, lease: null, expires: 0, document: null});
      await save();
    } catch (error) { await chrome.tabs.remove(tab.id); throw error; }
  }
  return stats();
}
function connect() {
  if (port || !enabled) return;
  try {
    const own = chrome.runtime.connectNative(HOST); port = own;
    own.onMessage.addListener(message => {
      if (message?.type === 'request') {
        const task = () => processRequest(message, own);
        chain = chain.then(task, task);
      } else if (message?.type === 'status') lastError = message.message || '';
    });
    own.onDisconnect.addListener(() => {
      lastError = chrome.runtime.lastError?.message || '本机宿主断开';
      if (port === own) port = null;
      if (enabled) void chrome.alarms.create('codepier-reconnect', {delayInMinutes: 0.1});
    });
    own.postMessage({type: 'hello', profile_id: profile, state: stats()});
    lastError = '';
  } catch (error) { lastError = error.message; port = null; }
}
async function tabReady(tabId) {
  const start = Date.now();
  while (Date.now() - start < 6000) {
    const tab = await chrome.tabs.get(tabId);
    if (tab.status === 'complete') return tab;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  failure('BROWSER_DOCUMENT_UNAVAILABLE', '页面仍在加载，请稍后重新观察');
}
async function content(row, message, origins) {
  let tab = await requireTab(row);
  await allowed(tab.url, origins);
  tab = await tabReady(tab.id);
  await allowed(tab.url, origins);
  await chrome.scripting.executeScript({target: {tabId: tab.id}, world: 'ISOLATED', files: ['content.js']});
  let reply;
  try {
    reply = await chrome.tabs.sendMessage(tab.id, {source: 'codepier-native', ...message, allowed_origins: origins, expected_url: tab.url}, {frameId: 0});
  } catch {
    failure(message.operation ? 'BROWSER_ACTION_UNCERTAIN' : 'BROWSER_DOCUMENT_UNAVAILABLE', '页面在操作期间发生导航或没有返回确认；不自动重放');
  }
  if (!reply?.ok) failure(reply?.code || 'BROWSER_DOCUMENT_UNAVAILABLE', reply?.message || '页面未返回有效状态');
  return reply.data;
}
async function dispatch(request) {
  await expire();
  if (!enabled) failure('BROWSER_PERMISSION_REQUIRED', '扩展连接已由本机关闭');
  const action = request.action;
  if (action === 'open') {
    await allowed(request.url, request.origins);
    if (typeof request.lease_id !== 'string' || !/^[a-f0-9]{32}$/.test(request.lease_id) || typeof request.expires !== 'number' || request.expires <= Date.now() / 1000) failure('BROWSER_LEASE_MISSING', '租约无效或已过期');
    await reconcile();
    const row = pool.find(p => !p.lease);
    if (!row) failure('BROWSER_POOL_EMPTY', '没有空闲工作页，请在 Chrome 前台从扩展菜单准备工作页');
    await requireTab(row);
    row.lease = request.lease_id; row.expires = request.expires;
    await save();
    await chrome.tabs.update(row.tab, {url: request.url});
    return {leased: true};
  }
  const row = pool.find(p => p.lease === request.lease_id);
  if (!row) failure('BROWSER_LEASE_MISSING', '此租约没有对应的受管工作页');
  if (action === 'close') { await release(row); return {released: true}; }
  if (row.expires <= Date.now() / 1000) failure('BROWSER_LEASE_MISSING', '租约已过期');
  if (action === 'snapshot') {
    const data = await content(row, {action: 'snapshot'}, request.origins);
    row.document = data.document_id;
    if (typeof request.expires === 'number') row.expires = request.expires;
    await save(); return data;
  }
  if (action === 'action') {
    const op = request.operation;
    if (!op || typeof op !== 'object') failure('BROWSER_ACTION_UNSUPPORTED', '缺少操作参数');
    if (op.action === 'navigate') await allowed(op.value, request.origins);
    await content(row, {action: 'action', operation: op, document_id: request.document_id, observation_token: request.observation_token}, request.origins);
    if (op.action === 'navigate') {
      await requireTab(row); await chrome.tabs.update(row.tab, {url: op.value}); row.document = null;
    }
    if (typeof request.expires === 'number') row.expires = request.expires;
    await save(); return {confirmed: true};
  }
  failure('BROWSER_ACTION_UNSUPPORTED', '不支持的浏览器动作');
}
async function processRequest(request, own) {
  let result;
  try {
    if (!/^[a-f0-9]{32}$/.test(request.request_id || '')) failure('BROWSER_ACTION_UNSUPPORTED', '请求编号无效');
    if (seen.includes(request.request_id)) failure('BROWSER_ACTION_UNCERTAIN', '该请求已接收过；不会重复执行');
    seen.push(request.request_id); seen = seen.slice(-256); await save();
    result = {ok: true, data: await dispatch(request)};
  } catch (error) {
    result = {ok: false, code: error.code || 'BROWSER_DOCUMENT_UNAVAILABLE', message: (error.message || '浏览器操作未确认').slice(0, 500)};
  }
  try { if (port === own) own.postMessage({type: 'result', request_id: request.request_id, result}); } catch {}
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || sender.tab || message?.source !== 'codepier-popup') return false;
  const run = async () => {
    await init(); let data = {};
    switch (message.action) {
      case 'status': data = {profile_id: profile, enabled, connected: !!port, last_error: lastError, sites, ...stats()}; break;
      case 'connect': enabled = true; await chrome.storage.local.set({codepier_enabled: true}); connect(); break;
      case 'disconnect': enabled = false; await chrome.storage.local.set({codepier_enabled: false}); port?.disconnect(); port = null; break;
      case 'prepare': data = await prepare(message.size); break;
      case 'add-site': {const origin = validOrigin(message.origin); if (!sites.includes(origin)) sites.push(origin); await chrome.storage.local.set({codepier_sites: sites}); break;}
      case 'remove-site': sites = sites.filter(site => site !== message.origin); await chrome.storage.local.set({codepier_sites: sites}); break;
      case 'release': for (const row of pool) {if (row.lease) {try {await release(row);} catch {}}} break;
      default: throw new Error('未知扩展菜单操作');
    }
    return data;
  };
  chain = chain.then(run, run);
  chain.then(data => reply({ok: true, data}), error => reply({ok: false, message: error.message}));
  return true;
});
chrome.alarms.onAlarm.addListener(alarm => {
  const run = async () => {await init(); if (alarm.name === 'codepier-maintenance') {await expire(); await reconcile();} if (enabled && !port) connect();};
  chain = chain.then(run, run);
});
chrome.tabs.onRemoved.addListener(tab => {pool = pool.filter(row => row.tab !== tab); void save();});
chrome.runtime.onStartup.addListener(() => {void init();});
chrome.runtime.onInstalled.addListener(() => {void init();});
void init();
