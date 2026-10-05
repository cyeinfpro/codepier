import {el, button, notice, badge} from './ui.js';

// The list is only a choice surface. Even a singleton requires a user click.
export function mountWorkbench(parent, value, ctx) {
  let projects = parseProjects(value);
  let closed = false;
  let revision = 0;
  let busy = false;
  let projectButtons = [];
  const alive = () => !closed && ctx.alive() && parent.isConnected;
  const valid = current => alive() && !document.hidden && current === revision;
  const header = el('header', undefined, 'workspace-header');
  header.append(el('span', 'CodePier / WORKBENCH', 'eyebrow'), el('h1', '选择项目'),
    el('p', '明确选择一个项目，再查看任务与执行记录。', 'muted'));
  const toolbar = el('div', undefined, 'toolbar');
  const status = el('div');
  status.setAttribute('aria-live', 'polite');
  const list = el('div', undefined, 'result-rows');
  list.setAttribute('aria-label', '可访问项目');
  const refresh = button('刷新项目列表', refreshProjects);
  const cancel = button('取消选择', () => {
    if (!alive()) return;
    revision++; busy = false;
    status.replaceChildren();
    notice(status, '已返回项目选择。尚未打开其他项目。');
    controls();
  });
  cancel.hidden = true;
  toolbar.append(refresh, cancel);
  parent.append(header, toolbar, status, list,
    el('p', '仅列出当前授权可见的项目 · 不自动选择或切换项目 · 打开项目不会捕获修改基线', 'bottom-note'));

  function controls() {
    refresh.disabled = busy;
    for (const control of projectButtons) control.disabled = busy;
    cancel.hidden = !busy;
    parent.setAttribute('aria-busy', busy ? 'true' : 'false');
  }
  refresh.reconcileDisabled = controls;
  cancel.reconcileDisabled = controls;

  function renderProjects() {
    list.replaceChildren();
    projectButtons = [];
    if (!projects.length) {
      notice(list, '没有可访问的项目。请在管理面板核对项目映射和当前授权，然后刷新列表。');
      return;
    }
    for (const project of projects) {
      const row = el('section', undefined, 'result-row');
      const details = el('div');
      const name = project.alias || project.project_alias || project.id || project.project_id;
      details.append(el('strong', name));
      if (project.description) details.append(el('p', project.description, 'muted'));
      if (project.root) details.append(el('p', project.root, 'path muted'));
      const facts = el('div', undefined, 'facts');
      if (project.device_name) facts.append(badge('unknown', project.device_name));
      facts.append(badge(project.online === true ? 'ready' : 'unknown',
        project.online === true ? '设备在线' : project.online === false ? '设备离线' : '设备状态未确认'));
      facts.append(badge('unknown', project.mode === 'write' ? '允许写入' : '只读项目'));
      if (project.allow_tasks !== undefined) facts.append(badge('unknown', project.allow_tasks ? '已允许任务' : '未允许任务'));
      details.append(facts);
      const choose = button('打开项目', () => openProject(project));
      choose.setAttribute('aria-label', '打开项目 ' + name);
      choose.reconcileDisabled = controls;
      projectButtons.push(choose);
      row.append(details, choose); list.append(row);
    }
    controls();
  }

  async function refreshProjects() {
    if (!alive() || document.hidden || busy) return;
    const current = ++revision;
    busy = true; controls();
    status.replaceChildren(); notice(status, '正在刷新可访问项目…');
    try {
      const result = await ctx.read('project_query', {operation: 'list'}, () => valid(current));
      if (!valid(current) || !result) return;
      projects = parseProjects(result);
      ctx.onProjects(projects);
      renderProjects(); status.replaceChildren();
      notice(status, '项目列表已刷新。请选择要打开的项目。');
    } catch (error) {
      if (!valid(current)) return;
      status.replaceChildren();
      notice(status, '刷新失败，保留上次列表。' + error.message, true);
    } finally {
      if (alive() && current === revision) { busy = false; controls(); }
    }
  }

  async function openProject(project) {
    if (!alive() || document.hidden || busy) return;
    const current = ++revision;
    busy = true; controls();
    status.replaceChildren(); notice(status, '正在读取所选项目…');
    try {
      const selected = project.id || project.project_id || project.alias || project.project_alias;
      const result = await ctx.read('project_query', {operation: 'open', project: selected}, () => valid(current));
      if (!valid(current) || !result) return;
      const openedId = result.workspace?.project_id || result.project_id;
      const expectedId = project.id || project.project_id;
      const openedAlias = result.workspace?.project || result.project_alias;
      if (expectedId ? openedId !== expectedId : openedAlias !== selected)
        throw new Error('返回的项目与当前选择不匹配，已停止打开。');
      ctx.onSelect(result);
    } catch (error) {
      if (!valid(current)) return;
      status.replaceChildren(); notice(status, '打开项目失败：' + error.message, true);
    } finally {
      if (alive() && current === revision) { busy = false; controls(); }
    }
  }

  function visible() {
    if (!alive() || !document.hidden || !busy) return;
    revision++; busy = false; controls();
    status.replaceChildren();
    notice(status, '页面已隐藏，已暂停本次显示。返回后可重新选择项目或刷新列表。');
  }
  document.addEventListener('visibilitychange', visible);
  renderProjects();
  ctx.onProjects(projects);
  return () => {
    closed = true; revision++;
    document.removeEventListener('visibilitychange', visible);
  };
}

function parseProjects(value) {
  if (!Array.isArray(value?.projects)) throw new Error('项目列表返回不完整，请重新打开工作台。');
  const result = [];
  const seen = new Set();
  for (const project of value.projects) {
    if (!project || typeof project !== 'object' || Array.isArray(project)) continue;
    const id = project.id || project.project_id || project.alias || project.project_alias;
    if (typeof id !== 'string' || !id || seen.has(id)) continue;
    seen.add(id); result.push(project);
  }
  return result;
}
