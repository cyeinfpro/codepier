import {el, button, notice, badge, coverageDetails, timestamp} from './ui.js';

// Every page retains one immutable reference. No fallback to the live directory.
export function mountReview(parent, value, ctx) {
  const reference = value.review_ref;
  const alive = () => ctx.alive() && parent.isConnected;
  if (!/^[a-f0-9]{32}$/.test(reference || '')) {
    notice(parent, '没有固定改动快照；不能用当前目录代替本轮结果。', true);
    return;
  }
  parent.append(el('h2', '本轮改动'));
  const facts = el('div', undefined, 'facts');
  facts.append(badge('unknown', (value.summary?.files ?? '—') + ' 个文件'),
    badge('unknown', '+' + (value.summary?.added_lines ?? '—') + ' / −' + (value.summary?.removed_lines ?? '—')));
  parent.append(facts, el('p', '固定快照 ' + reference, 'path muted'));
  if (value.summary?.line_counts_approximate) parent.append(el('p', '行数为近似值，以逐文件差异为准。', 'muted'));
  if (value.expires_at) parent.append(el('p', '有效期至 ' + timestamp(value.expires_at), 'bottom-note'));
  coverageDetails(parent, value.coverage);
  const files = el('div', undefined, 'files');
  const tools = el('div', undefined, 'toolbar');
  parent.append(files, tools);
  let next = value.next_offset ?? 0;
  async function read(args) {
    const result = await ctx.read('show_changes', {...ctx.target, review_ref: reference, ...args});
    if (!alive() || !result) return null;
    if (result.review_ref !== reference) throw new Error('快照编号发生变化，已停止读取。');
    return result;
  }
  function addFiles(items) {
    for (const item of items) {
      const details = el('details');
      const summary = el('summary');
      const names = {added: '新增', modified: '修改', deleted: '删除', unverified: '未完整捕获'};
      summary.append(el('span', item.path), el('small', names[item.status] || item.status || ''));
      details.append(summary);
      files.append(details);
      let loaded = false;
      details.addEventListener('toggle', () => {
        if (!details.open || loaded || !alive()) return;
        loaded = true;
        const pre = el('pre', '正在读取固定差异…');
        const paging = el('div', undefined, 'toolbar');
        details.append(pre, paging);
        let offset = 0;
        async function page() {
          const result = await read({path: item.path, offset, max_chars: 16000});
          if (!result || !alive()) return;
          if (offset === 0) pre.textContent = '';
          pre.append(document.createTextNode(result.diff || ''));
          offset = result.next_offset;
          paging.replaceChildren();
          if (offset !== null && offset !== undefined) paging.append(button('继续读取差异', page));
          if (result.diff_truncated) notice(paging, '该文件差异未完整捕获；不能当作完整验收依据。');
        }
        void page().catch(error => {
          if (!alive()) return;
          pre.textContent = error.message;
          paging.append(button('重试读取同一快照', page));
        });
      });
    }
  }
  async function more() {
    const result = await read({offset: next, limit: 40});
    if (!result) return;
    addFiles(result.files || []);
    next = result.next_offset;
    tools.replaceChildren();
    if (next !== null && next !== undefined) tools.append(button('更多文件', more));
    if (!result.files?.length && !files.childElementCount) {
      notice(files, result.coverage?.complete ? '捕获范围内没有变化。' : '已捕获范围没有变化，但覆盖不完整。');
    }
  }
  if (Array.isArray(value.files)) {
    addFiles(value.files);
    if (value.next_offset !== null && value.next_offset !== undefined) tools.append(button('更多文件', more));
    if (!value.files.length) notice(files, value.coverage?.complete ? '捕获范围内没有变化。' : '已捕获范围没有变化，但覆盖不完整。');
  } else tools.append(button('读取文件清单', more, 'primary'));
  parent.append(el('p', '共享目录采样期间的变化不证明某个会话独占修改；快照过期或读取失败不会重新采样当前目录。', 'bottom-note'));
}
