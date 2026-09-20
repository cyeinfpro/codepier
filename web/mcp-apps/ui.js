// All project text is data. Never interpret filenames, logs or summaries as HTML.
export function el(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = String(text);
  if (className) node.className = className;
  return node;
}
export function notice(parent, text, error = false) {
  const node = el('p', text, 'notice' + (error ? ' error' : ''));
  node.setAttribute('role', error ? 'alert' : 'status');
  parent.append(node);
  return node;
}
export function button(text, action, className = '') {
  const node = el('button', text, className);
  node.type = 'button';
  node.addEventListener('click', async () => {
    node.disabled = true;
    try { await action(); }
    catch (error) { if (node.isConnected) notice(node.parentElement, error.message || '读取未完成，请重试', true); }
    finally { node.disabled = false; node.reconcileDisabled?.(); }
  });
  return node;
}
export function disclosure(parent, title, open = false) {
  const node = el('details');
  node.open = open;
  node.append(el('summary', title));
  const body = el('div', undefined, 'disclosure-body');
  node.append(body);
  parent.append(node);
  return body;
}
export const stateLabel = (state) => ({
  active: '进行中', running: '执行中', queued: '已排队', reconnecting: '等待重连',
  cancelling: '正在取消', succeeded: '操作成功', failed: '失败', cancelled: '已取消',
  needs_review: '需核实', interrupted: '已中断', unknown: '状态未确认',
  pending: '待处理', completed: '已完成', skipped: '已跳过', blocked: '受阻',
  passed: '通过', stale: '源码已变化', unverified: '未验证', executing: '执行中',
  ready: '可用', disabled: '未启用', denied: '未获授权', missing: '程序缺失',
  not_connected: '未连接', not_run: '未实测', paused: '已暂停'
}[state] || '未确认');
export function badge(state, text) {
  return el('span', text || stateLabel(state), 'chip state-' + (/^[a-z_]+$/.test(state || '') ? state : 'unknown'));
}
export function bytes(value) {
  if (!Number.isFinite(value)) return '大小未知';
  if (value < 1024) return value + ' B';
  if (value < 1024 * 1024) return (value / 1024).toFixed(1) + ' KiB';
  return (value / 1024 / 1024).toFixed(1) + ' MiB';
}
export function timestamp(seconds) {
  return Number.isFinite(seconds) ? new Date(seconds * 1000).toLocaleString('zh-CN', {hour12: false}) : '时间未记录';
}
export function panelUrl(value) {
  try {
    const url = new URL(value);
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url : null;
  } catch { return null; }
}
export function coverageDetails(parent, coverage, title = '快照覆盖范围') {
  if (!coverage) return;
  const body = disclosure(parent, title + (coverage.complete === true ? ' · 范围内完整' : ' · 不完整'));
  const entries = coverage.before || coverage.after ? [['修改前', coverage.before], ['修改后', coverage.after]] : [['已捕获', coverage]];
  for (const [label, value] of entries) {
    if (!value) continue;
    body.append(el('p', label + '：' + (value.captured_files ?? '未知') + ' 个文件 · ' + bytes(value.captured_bytes)));
    if (value.max_files || value.max_bytes) body.append(el('p', '上限：' + (value.max_files ?? '—') + ' 个文件 / ' + bytes(value.max_bytes), 'muted'));
    if (value.truncated) body.append(el('p', '扫描达到预算或时限；未遍历的文件没有完整清单，不能据此判断未修改。', 'muted'));
    if (value.skipped?.length) {
      const list = el('ul', undefined, 'compact-list');
      for (const item of value.skipped) list.append(el('li', item.path + ' · ' + item.code));
      body.append(list);
    }
    if (value.exclusions) body.append(el('p', value.exclusions, 'bottom-note'));
  }
  body.append(el('p', '这是允许范围内的逐文件采样，不是全盘备份；现在重新拍摄不能补回修改前遗漏的内容。', 'bottom-note'));
}
