const $ = id => document.getElementById(id);
let profile = '';
async function call(action, extra = {}) {
  const result = await chrome.runtime.sendMessage({source: 'codepier-popup', action, ...extra});
  if (!result?.ok) throw new Error(result?.message || '扩展操作未确认');
  return result.data;
}
function message(text, error = false) { $(error ? 'error' : 'status').textContent = text; }
function site(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.pathname !== '/' || url.search || url.hash) throw new Error('请输入完整网站 origin，例如 https://example.com，不包含路径');
  return url.origin;
}
function pattern(value) { const url = new URL(value); return url.protocol + '//' + url.hostname + '/*'; }
async function refresh() {
  const state = await call('status'); profile = state.profile_id;
  $('extension').value = chrome.runtime.id; $('profile').value = profile;
  $('status').textContent = state.connected ? '已连接本机宿主 · ' + state.pool_size + ' 个工作页' : state.enabled ? '等待本机 Agent 或首次绑定' : '连接未启用';
  if (state.last_error) $('error').textContent = state.last_error;
  $('sites').replaceChildren();
  for (const origin of state.sites) {
    const row = document.createElement('div'); row.className = 'site';
    const text = document.createElement('span'); text.textContent = origin;
    const button = document.createElement('button'); button.textContent = '撤销';
    button.onclick = async () => {
      try { await chrome.permissions.remove({origins: [pattern(origin)]}); await call('remove-site', {origin}); await refresh(); }
      catch (error) { message(error.message, true); }
    };
    row.append(text, button); $('sites').append(row);
  }
}
function bind(id, fn) {
  $(id).addEventListener('click', async () => {
    $('error').textContent = ''; $(id).disabled = true;
    try { await fn(); await refresh(); } catch (error) { message(error.message, true); }
    finally { $(id).disabled = false; }
  });
}
bind('copy', () => navigator.clipboard.writeText('扩展编号：' + chrome.runtime.id + '\n档案编号：' + profile));
bind('connect', () => call('connect'));
bind('disconnect', () => call('disconnect'));
bind('prepare', () => call('prepare', {size: Number($('size').value)}));
bind('release', () => call('release'));
// Permission request originates only from this explicit owner gesture.
bind('grant', async () => {
  const origin = site($('origin').value.trim());
  if (!await chrome.permissions.request({origins: [pattern(origin)]})) throw new Error('未授予网站权限');
  await call('add-site', {origin}); $('origin').value = '';
});
refresh().catch(error => message(error.message, true));
