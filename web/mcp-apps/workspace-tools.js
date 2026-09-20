import {el, button, notice, badge, disclosure, stateLabel, bytes, timestamp, panelUrl, coverageDetails} from './ui.js';

const CHECK_NAMES = {project_read: '读取项目', project_write: '修改项目', shell: '执行命令',
  native_model_turn: '本地模型调用', host_file_roundtrip: '附件完整传输', host_mcp_apps: '宿主卡片交互', browser: '独立浏览器'};
const SCOPES = {read: '读取', write: '修改文件', execute: '执行命令', computer: '电脑操作'};
const relativeFile = value => !!value && !value.startsWith('/') && !value.includes('\\') &&
  !/^[a-z]:/i.test(value) && !/[\x00-\x1f]/.test(value) && value.split('/').every(part => part && part !== '.' && part !== '..');

export function mountWorkspaceTools(parent, value, ctx) {
  const ws = value.workspace || {};
  const info = disclosure(parent, '项目与权限');
  info.append(el('p', ws.root || '任务卡片未携带目录详情；可通过就绪检查读取当前映射。', 'path muted'));
  const scopes = el('div', undefined, 'facts');
  scopes.append(badge('unknown', ctx.target.workspace_id ? '隔离工作目录' : '原项目目录'));
  for (const scope of ws.granted_scopes || []) scopes.append(badge('unknown', SCOPES[scope] || scope));
  info.append(scopes, el('p', '权限标签不是开关，也不代表能力已经配置或实测；每次请求仍由服务器验证。', 'bottom-note'));
  if (ctx.target.workspace_id) info.append(el('p', ctx.target.workspace_id, 'path muted'));
  const readiness = el('div');
  info.append(button('检查项目就绪状态', async () => {
    readiness.replaceChildren(el('p', '正在检查，不会启动模型或修改设置…', 'muted'));
    try {
      const result = await ctx.read('readiness_get', ctx.target);
      if (!ctx.alive() || !result) return;
      readiness.replaceChildren();
      for (const check of result.checks || []) {
        const row = el('div', undefined, 'readiness-row');
        row.append(el('span', CHECK_NAMES[check.name] || check.name), badge(check.state));
        readiness.append(row);
        if (check.reason) readiness.append(el('p', check.reason, 'bottom-note'));
      }
      if (result.execution?.project_root) readiness.append(el('p', result.execution.project_root, 'path muted'));
      if (result.checks?.some(check => check.state === 'denied')) notice(readiness, '缺少相应授权；请由主理人在管理面板核对，不会自动扩大权限。');
      if (result.checks?.some(check => ['disabled', 'missing', 'not_connected'].includes(check.state))) notice(readiness, '有能力未配置或未连接。文件读写与可选浏览器是独立能力，可在管理面板的开发能力中查看配置说明。');
      if (result.build?.restart_required) notice(readiness, 'Agent 运行代码与磁盘代码不一致；检查并完成受控更新前，不应当作新版已生效。');
      const details = disclosure(readiness, '原始检查项与未实测范围');
      for (const check of result.checks || []) details.append(el('p', check.name + ' · ' + check.state, 'path'));
      details.append(el('p', result.note || '能力可用不等于本次任务验证通过。', 'bottom-note'));
    } catch (error) { if (ctx.alive()) { readiness.replaceChildren(); notice(readiness, '检查未完成：' + error.message, true); } }
  }), readiness);
  coverageDetails(parent, value.baseline_coverage, '修改前基线覆盖');
  if (value.baseline_expires_at) info.append(el('p', '本次基线有效期至 ' + timestamp(value.baseline_expires_at), 'bottom-note'));
  mountUpload(disclosure(parent, '附件导入'), ctx, ws.granted_scopes);
  const advanced = disclosure(parent, '高级管理');
  advanced.append(el('p', '权限修改、验收接受、Agent 升级或卸载仍在需要管理员身份和明确确认的管理面板中进行。', 'muted'));
  const url = panelUrl(ctx.panelUrl);
  if (url) advanced.append(button('打开管理面板', () => ctx.app.openLink({url: url.href})));
  else advanced.append(el('p', '当前结果没有可信的面板地址，请从原管理入口进入。', 'bottom-note'));
}

function mountUpload(parent, ctx, grantedScopes) {
  const host = window.openai;
  const supported = typeof host?.uploadFile === 'function' && typeof host?.getFileDownloadUrl === 'function';
  const denied = Array.isArray(grantedScopes) && !grantedScopes.includes('write');
  if (!supported) notice(parent, '当前宿主没有提供卡片内文件上传能力。可在聊天中上传附件并要求导入指定项目，或使用管理面板。');
  if (denied) notice(parent, '当前授权不允许修改文件。');
  const label = el('label', '选择要保存到项目的文件');
  const fileInput = el('input'); fileInput.type = 'file'; fileInput.setAttribute('aria-label', '选择要保存到项目的文件'); label.append(fileInput);
  const pathLabel = el('label', '项目内目标路径');
  const destination = el('input'); destination.placeholder = '例如 public/images/reference.png'; destination.setAttribute('aria-label', '项目内目标路径'); pathLabel.append(destination);
  const chooser = el('div', undefined, 'directory-picker');
  chooser.hidden = true;
  const output = el('div'); output.setAttribute('aria-live', 'polite');
  const recover = el('div', undefined, 'toolbar');
  let directory = '.';
  let browsing = '.';
  let browseRevision = 0;
  let phase = 'draft';
  let attempt = null;
  let operationId = null;
  let browserButton;
  let save;
  const locked = () => !['draft', 'done', 'failed'].includes(phase);
  function controls() {
    const busy = locked();
    fileInput.disabled = destination.disabled = !supported || denied || busy;
    save.disabled = !supported || denied || busy;
    browserButton.disabled = busy;
    if (busy) chooser.hidden = true;
  }
  function suggestedPath() {
    const name = fileInput.files?.[0]?.name;
    if (name && relativeFile(name)) destination.value = (directory === '.' ? '' : directory + '/') + name;
  }
  fileInput.addEventListener('change', () => { if (!locked()) { phase = 'draft'; attempt = null; operationId = null; suggestedPath(); recover.replaceChildren(); } });
  destination.addEventListener('input', () => { if (!locked()) { phase = 'draft'; attempt = null; operationId = null; recover.replaceChildren(); } });
  async function list(path, offset = 0, append = false) {
    if (locked()) return;
    const revision = ++browseRevision;
    const result = await ctx.read('fs_tree', {...ctx.target, path, depth: 1, limit: 100, offset});
    if (!ctx.alive() || revision !== browseRevision || locked() || !result) return;
    browsing = path; chooser.hidden = false;
    if (!append) chooser.replaceChildren(el('p', '当前浏览：' + path, 'path'));
    chooser.querySelector('[data-directory-more]')?.remove();
    if (!append) {
      chooser.append(button('使用此目录', () => { if (!locked()) { directory = path; suggestedPath(); chooser.hidden = true; } }));
      if (path !== '.') chooser.append(button('上级目录', () => list(path.split('/').slice(0, -1).join('/') || '.')));
    }
    for (const entry of result.entries || []) {
      if (entry.type === 'directory' && relativeFile(entry.path)) chooser.append(button(entry.name + '/', () => list(entry.path)));
    }
    if (result.next_offset !== null && result.next_offset !== undefined) {
      const more = button('更多目录', () => list(browsing, result.next_offset, true)); more.dataset.directoryMore = 'true'; chooser.append(more);
    }
    if (result.truncated) notice(chooser, '目录清单尚未完整读取，可继续分页或手动填写已知路径。');
    if (result.scan_error) notice(chooser, '目录扫描未完成：' + String(result.scan_error), true);
  }
  browserButton = button('选择现有目录', () => list(directory));
  async function waitOriginal() {
    if (!operationId || !ctx.alive()) return;
    const op = await ctx.request('operations_wait', {operation_id: operationId, wait_seconds: 1, output_limit: 0});
    if (!ctx.alive()) return;
    if ((op.operation_id || op.id) !== operationId) throw new Error('返回的导入回执编号不一致。');
    if (op.pending) { output.replaceChildren(); notice(output, '原操作仍在导入：' + operationId + '。请继续读取同一回执。'); return; }
    const data = op.result?.data;
    if (op.state !== 'succeeded' || op.result?.ok !== true) {
      phase = ['unknown', 'needs_review', 'interrupted'].includes(op.state) ? 'unknown' : 'failed';
      output.replaceChildren(); notice(output, '导入没有确认成功：' + stateLabel(op.state) + '。原操作：' + operationId, true);
      if (phase === 'failed') recover.replaceChildren();
      controls(); return;
    }
    finish(data);
  }
  function finish(data) {
    if (!data?.created || data.path !== attempt.path || !/^[a-f0-9]{64}$/.test(data.sha256 || '')) {
      phase = 'unknown'; throw new Error('保存结果缺少完整路径或校验回执，请核对原操作。');
    }
    phase = 'done'; output.replaceChildren(); recover.replaceChildren();
    notice(output, '已保存 ' + data.path + ' · ' + bytes(data.bytes));
    output.append(el('p', 'SHA-256 ' + data.sha256, 'path'));
    controls();
  }
  async function commit() {
    if (!ctx.alive() || !attempt) return;
    phase = 'submitting'; controls(); recover.replaceChildren();
    try {
      const result = await ctx.request('download_artifact', attempt);
      if (!ctx.alive()) return;
      if (result.pending) {
        operationId = result.operation_id; phase = 'pending';
        output.replaceChildren(); notice(output, '已提交导入，尚未确认保存。原操作：' + operationId);
        recover.append(button('读取原导入结果', waitOriginal));
      } else finish(result);
    } catch (error) {
      if (!ctx.alive()) return;
      output.replaceChildren();
      if (error.operationId) operationId = error.operationId;
      if (!operationId && ['INVALID_ARGUMENTS', 'INSUFFICIENT_SCOPE', 'READ_ONLY', 'PROJECT_NOT_FOUND', 'TOOL_OUTSIDE_PROFILE', 'UNKNOWN_TOOL', 'AGENT_UPGRADE_REQUIRED', 'TASKS_DISABLED'].includes(error.code)) {
        phase = 'failed';
        notice(output, '请求在执行前被拒绝：' + error.message + '。请修正输入或授权后再提交。', true);
        recover.replaceChildren();
        return;
      }
      phase = 'unknown';
      notice(output, '保存结果待核实：' + error.message + '。不要新建请求重复保存。', true);
      output.append(el('p', '恢复键：' + attempt.idempotency_key, 'path'));
      recover.replaceChildren(button('用原回执恢复导入', () => operationId ? waitOriginal() : commit()));
    } finally { if (ctx.alive()) controls(); }
  }
  save = button('保存文件', async () => {
    if (locked()) return;
    const file = fileInput.files?.[0];
    const path = destination.value.trim();
    if (!file || !relativeFile(path)) throw new Error('请选择文件，并填写不含绝对路径、空目录段或 .. 的项目内文件路径。');
    const target = {...ctx.target};
    phase = 'uploading'; attempt = null; operationId = null; recover.replaceChildren();
    output.replaceChildren(); notice(output, '正在上传附件，尚未写入项目。'); controls();
    try {
      const uploaded = await host.uploadFile(file);
      if (!ctx.alive()) return;
      const fresh = await host.getFileDownloadUrl({fileId: uploaded.fileId});
      if (!ctx.alive()) return;
      if (!uploaded.fileId || !fresh.downloadUrl) throw new Error('宿主未提供完整文件信息。');
      attempt = {...target, path, file: {file_id: uploaded.fileId, download_url: fresh.downloadUrl,
        mime_type: file.type, file_name: file.name, size: file.size}, idempotency_key: 'widget-import-' + Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join('')};
    } catch (error) { if (ctx.alive()) { phase = 'draft'; controls(); } throw error; }
    await commit();
  });
  save.reconcileDisabled = controls;
  browserButton.reconcileDisabled = controls;
  parent.append(label, pathLabel, browserButton, chooser, save, output, recover,
    el('p', '只保存到未使用的项目内路径。目录不会自动创建；不覆盖、不自动解压或执行。未知结果只恢复同一请求。', 'bottom-note'));
  controls();
}
