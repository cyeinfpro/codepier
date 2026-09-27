import {el, button, notice, badge, disclosure, stateLabel, bytes, timestamp, panelUrl} from './ui.js';
import {mountReview} from './review.js';

const TOOL_NAMES = {shell_exec: '执行命令', tasks_run: '运行任务', validation_run: '验证命令',
  show_changes: '固定改动', artifacts_register: '登记交付物', apply_patch: '批量修改',
  fs_write: '写入文件', fs_edit: '编辑文件', fs_read: '读取文件', fs_read_many: '读取代码',
  open_workspace: '打开工作区', download_artifact: '导入附件'};
const REFRESH_MS = 6000;
const WATCH_LIMIT_MS = 10 * 60 * 1000;

export function mountDashboard(parent, ctx) {
  let selected = ctx.workflowId || '';
  let snapshot = null;
  let evidence = [];
  let tab = 'operations';
  let revision = 0;
  let detailRevision = 0;
  let timer = 0;
  let detailTimer = 0;
  let resumeDetail = null;
  let started = Date.now();
  let nextWorkflowCursor = null;
  let nextEvidenceOffset = null;
  let closed = false;
  let overviewSignature = '';
  let recentSignature = '';
  const alive = () => !closed && ctx.alive() && parent.isConnected;
  const controls = el('div', undefined, 'dashboard-controls');
  const selectLabel = el('label', '查看任务');
  const select = el('select');
  select.setAttribute('aria-label', '选择任务');
  select.append(new Option('选择任务，不自动猜测本轮归属', ''));
  selectLabel.append(select);
  const refreshButton = button('刷新状态', async () => { started = Date.now(); await refresh(); });
  const autoLabel = el('label', undefined, 'check-label');
  const auto = el('input');
  auto.type = 'checkbox'; auto.checked = true;
  auto.setAttribute('aria-label', '自动刷新任务状态');
  autoLabel.append(auto, document.createTextNode('自动刷新'));
  const moreTasks = button('更早任务', () => refresh({workflowCursor: nextWorkflowCursor || '', appendTasks: true}));
  moreTasks.hidden = true;
  controls.append(selectLabel, refreshButton, autoLabel, moreTasks);
  const stamp = el('p', '正在读取任务和操作记录…', 'bottom-note');
  stamp.setAttribute('role', 'status');
  const overview = el('section', undefined, 'task-hero');
  const tabs = el('nav', undefined, 'tabs');
  tabs.setAttribute('aria-label', '任务结果分类');
  const tabButtons = new Map();
  for (const [key, title] of [['operations', '执行记录'], ['changes', '改动'], ['validations', '验证'], ['artifacts', '交付物']]) {
    const control = button(title, () => { tab = key; closeDetail(); renderRows(); });
    control.dataset.tab = key;
    tabButtons.set(key, control); tabs.append(control);
  }
  const rows = el('div', undefined, 'result-rows');
  const paging = el('div', undefined, 'toolbar');
  const inspector = el('section', undefined, 'inspector');
  inspector.hidden = true;
  const recent = el('div');
  parent.append(controls, stamp, overview, tabs, rows, paging, inspector, recent);

  function closeDetail() {
    detailRevision++;
    resumeDetail = null;
    clearTimeout(detailTimer);
    inspector.replaceChildren(); inspector.hidden = true;
  }
  function detailContext() {
    closeDetail(); inspector.hidden = false;
    const current = detailRevision;
    const valid = () => alive() && current === detailRevision;
    return {...ctx, alive: valid, read: (name, args) => ctx.read(name, args, valid)};
  }
  function schedule() {
    clearTimeout(timer);
    if (!alive() || !auto.checked || document.hidden) return;
    if (Date.now() - started >= WATCH_LIMIT_MS) {
      auto.checked = false;
      stamp.textContent += ' · 自动刷新已达 10 分钟，手动刷新可继续。';
      return;
    }
    const active = snapshot?.workflow?.state === 'active' ||
      [...evidence, ...(snapshot?.recent_operations || [])].some(item => item.pending);
    timer = setTimeout(() => void refresh(), active ? REFRESH_MS : REFRESH_MS * 2);
  }
  async function refresh({workflowCursor = '', appendTasks = false, offset = 0, appendEvidence = false} = {}) {
    if (!alive()) return;
    const current = ++revision;
    const workflowId = selected;
    clearTimeout(timer);
    refreshButton.disabled = true;
    try {
      const data = await ctx.request('workspace_status', {...ctx.target, workflow_id: workflowId,
        workflow_cursor: workflowCursor, evidence_offset: offset, limit: 12});
      if (!alive() || current !== revision || workflowId !== selected) return;
      if (data.workspace_id !== (ctx.target.workspace_id || '') ||
          (workflowId && data.workflow?.workflow_id !== workflowId) ||
          (ctx.projectId && data.project_id !== ctx.projectId)) throw new Error('返回的任务或目录与当前选择不匹配。');
      snapshot = data;
      const loaded = (data.evidence || []).map(item => ({...item, pageOffset: offset}));
      evidence = appendEvidence ? [...new Map([...evidence, ...loaded].map(item => [item.operation_id, item])).values()] : loaded;
      nextWorkflowCursor = data.next_workflow_cursor;
      nextEvidenceOffset = data.next_evidence_offset;
      if (!appendTasks) select.replaceChildren(new Option('选择任务，不自动猜测本轮归属', ''));
      for (const workflow of data.workflows || []) {
        if (![...select.options].some(option => option.value === workflow.workflow_id))
          select.append(new Option(workflow.title + ' · ' + stateLabel(workflow.state), workflow.workflow_id));
      }
      if (selected && ![...select.options].some(option => option.value === selected))
        select.append(new Option(data.workflow.title, selected));
      select.value = selected;
      moreTasks.hidden = !nextWorkflowCursor;
      stamp.textContent = '已读取 ' + timestamp(data.observed_at) + (data.device_online ? ' · 设备在线' : ' · 设备离线，显示已保存回执');
      if (data.workflow?.mapping_changed) closeDetail();
      const nextOverview = JSON.stringify([data.workflow, data.workflow ? null : data.workflows]);
      if (overviewSignature !== nextOverview) { renderOverview(); overviewSignature = nextOverview; }
      renderRows();
      const nextRecent = JSON.stringify(data.recent_operations);
      if (recentSignature !== nextRecent) { renderRecent(); recentSignature = nextRecent; }
    } catch (error) {
      if (!alive() || current !== revision) return;
      auto.checked = false;
      stamp.textContent = '刷新失败，已暂停自动刷新。' + (snapshot ? '保留上次数据，不代表当前状态。' : '') + ' ' + error.message;
      stamp.classList.add('error-text');
      if (!snapshot) {
        overview.replaceChildren(el('h2', '任务记录暂不可用'));
        notice(overview, ['UNKNOWN_TOOL', 'TOOL_OUTSIDE_PROFILE'].includes(error.code) ? '当前 Hub 未提供任务面板接口，需要更新服务。基础项目工具仍可使用。' : '请核对连接、授权与服务版本。读取失败不代表没有任务；基础项目工具仍可使用。');
      }
      return;
    } finally {
      if (alive() && current === revision) refreshButton.disabled = false;
    }
    stamp.classList.remove('error-text');
    schedule();
  }
  select.addEventListener('change', () => {
    selected = select.value; revision++; started = Date.now();
    snapshot = null; evidence = []; overviewSignature = recentSignature = ''; closeDetail();
    overview.replaceChildren(el('p', '正在读取所选任务…')); rows.replaceChildren(); paging.replaceChildren(); recent.replaceChildren();
    void refresh();
  });
  auto.addEventListener('change', () => { started = Date.now(); if (auto.checked) void refresh(); else clearTimeout(timer); });
  function visible() { if (document.hidden) { clearTimeout(timer); clearTimeout(detailTimer); } else if (alive()) { if (auto.checked) void refresh(); resumeDetail?.(); } }
  document.addEventListener('visibilitychange', visible);

  function renderOverview() {
    overview.replaceChildren();
    const workflow = snapshot?.workflow;
    if (!workflow) {
      overview.append(el('span', '任务工作台', 'eyebrow'), el('h2', '把工作过程和结果放在一起'),
        el('p', snapshot?.workflows?.length ? '选择一个已保存任务，查看步骤、执行回执、固定改动和交付物。' : '还没有可见任务。聊天中的多步骤工作需要先建立任务记录，才会在这里显示。', 'muted'),
        el('p', '下面的近期操作可直接查看，但不会自动认定属于本轮聊天。', 'bottom-note'));
      return;
    }
    const heading = el('div', undefined, 'split-row');
    heading.append(el('span', '所选任务', 'eyebrow'), badge(workflow.state));
    overview.append(heading, el('h2', workflow.title));
    if (workflow.summary) overview.append(el('p', workflow.summary.slice(0, 200) + (workflow.summary.length > 200 ? '…' : ''), 'task-summary'));
    const stats = el('div', undefined, 'stats');
    const progress = workflow.progress || {};
    for (const [label, value] of [['已完成步骤', (progress.completed ?? 0) + ' / ' + (progress.total ?? '—')],
      ['源码验证', '需打开核对'], ['部署状态', '未提供部署证明']]) {
      const card = el('div', undefined, 'stat'); card.append(el('small', label), el('strong', value)); stats.append(card);
    }
    overview.append(stats);
    if (workflow.mapping_changed) {
      notice(overview, '项目目录或设备已变化。这是旧任务记录，已停止关联当前目录的结果；请重新建立任务。', true);
      return;
    }
    const steps = disclosure(overview, '步骤与验收条件 · ' + (progress.completed ?? 0) + ' 已完成' + (progress.skipped ? ' · ' + progress.skipped + ' 已跳过' : ''), true);
    const list = el('ol', undefined, 'steps');
    for (const step of workflow.steps || []) {
      const item = el('li', undefined, step.id === workflow.next_step ? 'current-step' : '');
      const line = el('div', undefined, 'split-row');
      line.append(el('span', step.title), badge(step.state)); item.append(line);
      if (step.summary) item.append(el('p', step.summary, 'muted'));
      const details = disclosure(item, '验收条件与记录');
      details.append(el('p', step.acceptance), el('p', '关联回执 ' + (step.evidence?.length || 0) + ' 条。步骤状态由工作流保存，不等于当前命令状态。', 'bottom-note'));
      list.append(item);
    }
    steps.append(list);
    const details = disclosure(overview, '任务目标与完整摘要');
    details.append(el('p', workflow.goal), el('p', workflow.summary || '尚未记录摘要。'),
      el('p', '任务 ' + workflow.workflow_id + ' · 版本 ' + workflow.version, 'path muted'));
  }
  function operationRow(item) {
    const row = el('div', undefined, 'result-row');
    const text = el('div');
    text.append(el('strong', TOOL_NAMES[item.tool] || item.tool),
      el('p', item.operation_id, 'path muted'), el('small', timestamp(item.updated)));
    row.append(text, badge(item.state), button('查看日志与回执', () => openOperation(item)));
    return row;
  }
  function renderRows() {
    for (const [key, control] of tabButtons) control.setAttribute('aria-pressed', String(key === tab));
    rows.replaceChildren(); paging.replaceChildren();
    if (!snapshot?.workflow || snapshot.workflow.mapping_changed) {
      rows.append(el('p', snapshot?.workflow?.mapping_changed ? '旧映射下的任务证据不会在此关联到新目录。' : '选择任务后，这里显示明确关联的记录。', 'empty-state'));
      return;
    }
    const selectedItems = tab === 'operations' ? evidence : evidence.filter(item =>
      tab === 'changes' ? item.review : tab === 'validations' ? item.validation : item.artifact);
    for (const item of selectedItems) {
      if (tab === 'operations') { rows.append(operationRow(item)); continue; }
      const row = el('div', undefined, 'result-row');
      const text = el('div'); row.append(text);
      if (tab === 'changes') {
        text.append(el('strong', (item.review.summary?.files ?? '—') + ' 个文件变化'), el('p', '固定快照 ' + item.review.review_ref, 'path muted'));
        row.append(button('查看固定差异', () => openReview(item.review)));
      } else if (tab === 'validations') {
        text.append(el('strong', item.validation.label), el('p', '历史结果：' + stateLabel(item.validation.historical_state) + ' · 尚未核对当前源码', 'muted'));
        row.append(button('核对源码与报告', () => openValidation(item)));
      } else {
        const artifact = item.artifact;
        text.append(el('strong', artifact.name || '交付物不可用'), el('p', bytes(artifact.bytes), 'muted'));
        if (artifact.sha256) text.append(el('p', 'SHA-256 ' + artifact.sha256, 'path muted'));
        const unavailable = artifact.unavailable || artifact.expired || artifact.expires * 1000 <= Date.now();
        text.append(el('p', unavailable ? '已过期或当前映射下不可用' : '有效期至 ' + timestamp(artifact.expires), 'bottom-note'));
        const download = button('下载交付物', () => downloadArtifact(item)); download.disabled = !!unavailable;
        row.append(download);
      }
      rows.append(row);
    }
    if (!selectedItems.length) rows.append(el('p', '当前已加载的任务证据中没有' + ({operations: '执行记录', changes: '固定改动快照', validations: '验证回执', artifacts: '已登记交付物'}[tab]) + '。不会从摘要文字或附近操作中猜测。', 'empty-state'));
    paging.append(el('span', '已加载 ' + evidence.length + ' 条关联记录 / ' + snapshot.evidence_total + ' 条引用', 'bottom-note'));
    if (nextEvidenceOffset !== null && nextEvidenceOffset !== undefined)
      paging.append(button('更多关联记录', () => { auto.checked = false; clearTimeout(timer); return refresh({offset: nextEvidenceOffset, appendEvidence: true}); }));
    if (snapshot.unavailable_evidence) notice(paging, snapshot.unavailable_evidence + ' 条引用不在当前目录、不可读取或需要其他权限，未合并展示。');
    if (snapshot.history_truncated) notice(paging, '仅检查最近 100 条任务事件及当前步骤引用；更早事件请在管理面板查看。');
  }
  function renderRecent() {
    recent.replaceChildren();
    const body = disclosure(recent, '项目近期操作 · 未自动归属所选任务', !selected);
    if (!snapshot?.recent_operations?.length) body.append(el('p', '最近窗口内没有可展示的开发操作，不代表完整历史为空。', 'muted'));
    for (const item of snapshot?.recent_operations || []) body.append(operationRow(item));
    if (snapshot?.recent_window_limited) body.append(el('p', '这是有上限的近期窗口，不是完整操作历史。', 'bottom-note'));
  }
  async function openReview(value) {
    const child = detailContext();
    try {
      const result = await child.read('show_changes', {...ctx.target, review_ref: value.review_ref, limit: 40});
      if (child.alive() && result) mountReview(inspector, result, child);
    } catch (error) { if (child.alive()) notice(inspector, error.message + '；不会重新冻结当前目录。', true); }
  }
  async function openValidation(item) {
    const child = detailContext();
    inspector.append(el('h3', '正在核对当前源码…'));
    try {
      const result = await child.read('validations_get', {...ctx.target, validation_id: item.validation.validation_id});
      if (!child.alive() || !result) return;
      inspector.replaceChildren(el('h3', result.label || '验证报告'));
      const currentPass = result.state === 'passed' && result.source_current === true && result.current?.complete === true;
      inspector.append(badge(currentPass ? 'passed' : result.state === 'passed' ? 'unverified' : result.state),
        el('p', '核对时间：' + timestamp(result.current?.observed_at) + '。只证明这次核对时的源码状态。', 'muted'),
        el('p', '历史结果：' + stateLabel(result.historical_state) + ' · 退出码 ' + (result.exit_code ?? '未记录')));
      if (!result.current?.complete) notice(inspector, '源码扫描覆盖不完整，不能标为当前验证通过。');
      if (result.current?.skipped?.length) {
        const skipped = disclosure(inspector, '未覆盖的源码');
        for (const entry of result.current.skipped) skipped.append(el('p', entry.path + ' · ' + entry.code, 'path'));
      }
      inspector.append(el('p', '命令通过不代表所有功能已验收，也不代表已部署；此卡片不会替管理员接受验收。', 'bottom-note'),
        button('查看验证输出', () => openOperation(item)));
    } catch (error) { if (child.alive()) { inspector.replaceChildren(); notice(inspector, '验证未完成：' + error.message, true); } }
  }
  async function downloadArtifact(item) {
    const workflowId = selected;
    const fresh = await ctx.request('workspace_status', {...ctx.target, workflow_id: workflowId, evidence_offset: item.pageOffset || 0, limit: 12});
    if (!alive() || selected !== workflowId) return;
    const artifact = [...fresh.evidence, ...fresh.recent_operations].find(entry => entry.artifact?.artifact_id === item.artifact.artifact_id)?.artifact;
    if (!artifact || artifact.unavailable || artifact.expired || artifact.expires * 1000 <= Date.now()) throw new Error('无法重新确认此交付物，请刷新记录；不会重新生成文件。');
    if (!artifact.device_online) throw new Error('交付物保存在离线设备上，恢复连接后再下载原文件。');
    const base = panelUrl(ctx.panelUrl);
    // The server pins the Space in this authenticated link. Construct it from
    // bounded identifiers rather than accepting an arbitrary returned URL.
    if (artifact.space_id != null && !/^[A-Za-z0-9_-]{1,100}$/.test(artifact.space_id)) throw new Error('下载空间标识无效。');
    const path = '/api/artifacts/' + artifact.artifact_id + '/download' +
      (artifact.space_id != null ? '?space_id=' + encodeURIComponent(artifact.space_id) : '');
    if (!base || !/^[a-f0-9]{32}$/.test(artifact.artifact_id) || artifact.download_path !== path) throw new Error('没有可信的下载入口，请在管理面板按交付物编号下载。');
    const url = new URL(path, base);
    if (url.origin !== base.origin) throw new Error('下载地址不属于当前面板。');
    await ctx.app.openLink({url: url.href});
    if (alive()) notice(paging, '已请求打开原文件下载，尚未确认下载完成；外部浏览器需要登录同一管理面板。');
  }
  async function openOperation(item) {
    const child = detailContext();
    const id = item.operation_id;
    const status = el('p', '正在读取原操作…');
    const pre = el('pre', '等待输出…');
    const watchLabel = el('label', undefined, 'check-label');
    const watch = el('input'); watch.type = 'checkbox'; watch.checked = true;
    watchLabel.append(watch, document.createTextNode('跟踪此操作的输出'));
    inspector.append(el('h3', TOOL_NAMES[item.tool] || item.tool), el('p', '原操作 ' + id, 'path'), status, pre, watchLabel,
      el('p', '仅显示最多 8,000 字符的日志尾部。刷新只读取同一回执，不会重新执行命令。', 'bottom-note'));
    const command = disclosure(inspector, '命令与工作目录');
    let sequence;
    let busy = false;
    let pending = false;
    const since = Date.now();
    async function read() {
      if (!child.alive() || busy) return;
      busy = true;
      clearTimeout(detailTimer);
      try {
        const op = await ctx.request('operations_get', {operation_id: id, output_limit: 8000,
          ...(sequence === undefined ? {} : {after_output_seq: sequence})});
        if (!child.alive()) return;
        if ((op.operation_id || op.id) !== id || op.project_id !== snapshot?.project_id ||
            (op.args_summary?.workspace_id || '') !== (ctx.target.workspace_id || '')) throw new Error('回执不属于当前选择的项目或目录。');
        const data = op.result?.data || {};
        command.replaceChildren();
        if (op.args_summary?.command) command.append(el('pre', String(op.args_summary.command).slice(0, 8000)));
        if (op.args_summary?.cwd) command.append(el('p', op.args_summary.cwd, 'path'));
        if (!command.childElementCount) command.append(el('p', '此回执未记录命令；不会从日志中猜测。', 'muted'));
        status.textContent = stateLabel(op.state) + (data.exit_code !== undefined ? ' · 退出码 ' + data.exit_code : '') +
          (data.timed_out ? ' · 命令超时' : '') + (op.error ? ' · ' + (typeof op.error === 'string' ? op.error : op.error.message) : '');
        if (typeof op.output === 'string') pre.textContent = op.output || (op.pending ? '等待输出…' : '未捕获文本输出。');
        else if (typeof data.output === 'string') pre.textContent = data.output;
        else if (!op.output_unchanged && !op.pending) pre.textContent = '此操作没有捕获文本输出。';
        sequence = op.output_seq;
        pending = op.pending === true;
        if (pending && watch.checked && !document.hidden && Date.now() - since < WATCH_LIMIT_MS)
          detailTimer = setTimeout(() => void read(), 2000);
        else if (pending && Date.now() - since >= WATCH_LIMIT_MS) { watch.checked = false; status.textContent += ' · 自动跟踪已暂停，请手动读取原操作。'; }
      } catch (error) {
        if (child.alive()) { status.textContent = '读取失败，保留上次日志：' + error.message; watch.checked = false; }
      } finally { busy = false; }
    }
    resumeDetail = () => { if (child.alive() && watch.checked && pending && Date.now() - since < WATCH_LIMIT_MS) void read(); };
    watch.addEventListener('change', () => { if (watch.checked && pending) void read(); else clearTimeout(detailTimer); });
    inspector.append(button('刷新原操作', read));
    await read();
  }
  void refresh();
  return () => { closed = true; revision++; closeDetail(); clearTimeout(timer); document.removeEventListener('visibilitychange', visible); };
}
