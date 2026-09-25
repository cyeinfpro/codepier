import {App} from '@modelcontextprotocol/ext-apps';
import {el, button, notice} from './ui.js';
import {mountDashboard} from './dashboard.js';
import {mountReview} from './review.js';
import {mountWorkspaceTools} from './workspace-tools.js';

const app = new App({name: 'CodePier Task Workspace', version: '1.1.0'});
const root = document.getElementById('app');
let generation = 0;
let input = {};
let binding = {};
let cleanup = () => {};

function valueOf(result, name = '') {
  const value = name === 'process' && result?.structuredContent?.operations ? result.structuredContent.operations[0] : result?.structuredContent;
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('工具未返回结构化结果。');
  // A failed original operation is a valid read result: keep its status and logs.
  const receipt = name === 'process' &&
    typeof value.tool === 'string' && typeof value.state === 'string' && (value.id || value.operation_id);
  if (!receipt && (result.isError || value.error)) {
    const error = new Error(value.error?.message || (typeof value.error === 'string' ? value.error : '请求未完成，请检查原操作。'));
    error.code = value.error?.code;
    error.operationId = /^[a-f0-9]{32}$/.test(value.operation_id || '') ? value.operation_id : null;
    throw error;
  }
  return value;
}
async function request(name, args) {
  return valueOf(await app.callServerTool({name, arguments: args}), name);
}
function stop() {
  generation++;
  cleanup();
  cleanup = () => {};
}
function theme(context) {
  document.documentElement.dataset.theme = context?.theme === 'dark' ? 'dark' : 'light';
}
function decode(op) {
  if (op.pending) return null;
  if (op.state !== 'succeeded' || op.result?.ok !== true) {
    throw new Error(op.result?.error?.message || (typeof op.error === 'string' ? op.error : '') || '原操作未成功；不能当作空结果。');
  }
  return {...op.result.data, operation_id: op.operation_id || op.id};
}
async function settle(value, alive) {
  const deadline = Date.now() + 120000;
  while (value?.pending) {
    if (!alive()) return null;
    if (Date.now() > deadline) throw new Error('读取仍在进行。请继续查询原操作 ' + value.operation_id + '，不要重新执行。');
    const id = value.operation_id;
    const op = await request('process', {operation: 'wait', operation_ids: [id], wait_seconds: 1, output_limit: 2000});
    if (!alive()) return null;
    if ((op.operation_id || op.id) !== id) throw new Error('原操作编号不匹配，已停止读取。');
    const completed = decode(op);
    if (completed) return completed;
    await new Promise(resolve => setTimeout(resolve, 400));
  }
  return alive() ? value : null;
}
function render(value, g) {
  const alive = () => generation === g;
  if (!alive()) return;
  root.replaceChildren();
  root.setAttribute('aria-busy', 'false');
  if (value.pending) {
    notice(root, '请求已保存，正在读取原操作的结果…');
    const id = value.operation_id;
    async function recover() {
      try {
        const completed = await settle({pending: true, operation_id: id}, alive);
        if (alive() && completed) render(completed, g);
      } catch (error) {
        if (!alive()) return;
        root.replaceChildren(); notice(root, error.message, true);
        root.append(el('p', '原操作 ' + id, 'path'), button('重新读取原操作', recover));
      }
    }
    void recover(); return;
  }
  const project = value.workspace?.project || value.project_alias || binding.project || input.project;
  const projectId = value.workspace?.project_id || value.project_id || null;
  const workspaceId = binding.workspace_id || value.workspace?.workspace_id || input.workspace_id || '';
  const ctx = {
    app, alive, request,
    target: {project: projectId || project, ...(workspaceId ? {workspace_id: workspaceId} : {})},
    projectId,
    workflowId: binding.workflow_id || value.workflow_id || input.workflow_id || '',
    panelUrl: binding.panel_url || '',
    async read(name, args, additionalAlive = () => true) {
      const valid = () => alive() && additionalAlive();
      if (!valid()) return null;
      const response = await request(name, args);
      return valid() ? settle(response, valid) : null;
    }
  };
  if (!project) { notice(root, '没有已核实的项目绑定，请重新打开项目或任务。', true); return; }
  if ((binding.kind || document.body.dataset.kind) === 'changes') {
    root.append(el('span', 'CodePier / CHANGES', 'eyebrow'));
    mountReview(root, {...value, review_ref: value.review_ref || binding.review_ref || input.review_ref}, ctx);
  } else {
    const header = el('header', undefined, 'workspace-header');
    header.append(el('span', 'CodePier / TASK WORKSPACE', 'eyebrow'), el('h1', project),
      el('p', '任务进度 · 执行证据 · 结果交付', 'muted'));
    root.append(header);
    const dashboard = el('div'); root.append(dashboard);
    cleanup = mountDashboard(dashboard, ctx);
    const secondary = el('section', undefined, 'secondary-tools'); root.append(secondary);
    mountWorkspaceTools(secondary, value, ctx);
  }
  root.append(el('p', '只读刷新不启动模型或命令 · 证据不等于完整验收 · 管理操作保持原有权限', 'bottom-note'));
}
app.ontoolinput = params => {
  stop(); input = params.arguments || {};
  root.replaceChildren(el('p', '正在读取所选项目或任务…', 'muted'));
  root.setAttribute('aria-busy', 'true');
};
app.ontoolresult = result => {
  stop(); binding = {...(result._meta?.['com.codepier/binding'] || result._meta?.['me.infpro.relay/binding'])};
  try { render(valueOf(result), generation); }
  catch (error) { root.replaceChildren(); notice(root, error.message, true); root.setAttribute('aria-busy', 'false'); }
};
app.onhostcontextchanged = theme;
app.ontoolcancelled = () => { stop(); root.replaceChildren(); notice(root, '卡片显示已结束，后台操作没有因此自动取消。请按原操作编号核实。'); };
app.onteardown = async () => { stop(); return {}; };
void (async () => {
  try { await app.connect(); theme(app.getHostContext()); }
  catch {
    stop(); root.replaceChildren();
    notice(root, '当前页面不支持 MCP Apps，或组件连接失败。工具的文字结果仍可使用。', true);
    root.setAttribute('aria-busy', 'false');
  }
})();
