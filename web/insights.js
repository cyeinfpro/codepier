'use strict';
function deliveryState() {
  return S.delivery || (S.delivery = { project: '', cursor: '', history: [], next: null });
}
const humanBytes = (n) =>
  n >= 1048576
    ? (n / 1048576).toFixed(2) + ' MiB'
    : n >= 1024
      ? (n / 1024).toFixed(1) + ' KiB'
      : n + ' B';
async function diagnosticsHTML(seq) {
  const session = S.session,
    r = await tool('diagnostics_get', {});
  if (seq !== S.renderSeq || session !== S.session) return '';
  const h = r.hub,
    buildCard = (title, b, extra = '') =>
      `<article class="panel insight-card"><div class="card-top"><h2>${esc(title)}</h2>${b?.restart_required ? '<span class="badge blocked">需重启生效</span>' : '<span class="badge completed">运行信息</span>'}</div><dl class="kv"><dt>运行版本</dt><dd>${esc(b?.runtime?.version || '未报告')}</dd><dt>磁盘版本</dt><dd>${esc(b?.disk?.version || '未报告')}</dd><dt>启动时间</dt><dd>${esc(timeText(b?.runtime?.started_at))}</dd><dt>运行源码指纹</dt><dd><code>${esc(b?.runtime?.source_sha256?.slice(0, 20) || '未知')}</code></dd><dt>磁盘源码指纹</dt><dd><code>${esc(b?.disk?.source_sha256?.slice(0, 20) || '未知')}</code></dd></dl>${extra}</article>`;
  return (
    heading(
      '运行诊断',
      'RUNTIME / OBSERVED STATE',
      '',
      `<button class="btn ghost" data-action="refresh">${icon('refresh')}刷新</button>`,
    ) +
    `<div class="insight-summary"><span>${r.tool_count} 个工具</span><span>数据库 schema ${esc(r.schema)}</span><span>客户端工具缓存：${r.client_catalog.matches === null ? '未知，未提供指纹' : r.client_catalog.matches ? '一致' : '不一致'}</span></div>` +
    (r.warnings.length ? notice(r.warnings.map(esc).join('<br>')) : '') +
    `<div class="insight-grid">${buildCard('CodePier Hub', h)}${r.devices.map((d) => buildCard(d.name, d.build, `<div class="actions">${onlineBadge(d.online)}<span class="muted tiny">报告版本 ${esc(d.reported_version || '未知')}</span></div><p class="form-note">${d.missing_tools.length ? '缺少能力：' + esc(d.missing_tools.join('、')) : d.capability_information_available ? '当前远端工具能力已报告完整。' : '设备未报告能力列表，不能推断支持情况。'}</p>`)).join('')}</div>` +
    `<section class="panel insight-card"><h2>操作追踪</h2><form id="trace-form" class="insight-inline"><input name="operation_id" aria-label="操作编号" required maxlength="100" placeholder="输入操作编号"><button class="btn primary">查看执行链路</button></form><p class="form-note">只查看执行链路，不重复执行。</p></section>`
  );
}
function bindDiagnostics() {
  const f = $('#trace-form');
  if (f)
    f.onsubmit = (e) => {
      e.preventDefault();
      insightTrace(f.elements.operation_id.value.trim()).catch((err) => toast(err.message, true));
    };
}
async function insightTrace(id) {
  const session = S.session,
    loading = modal('执行链路', '<p class="muted">读取已保存的阶段记录…</p>');
  try {
    const r = await tool('operations_trace', { operation_id: id, limit: 200 });
    if (session !== S.session || !loading.isConnected) return;
    modal(
      '执行链路 · ' + id.slice(0, 12),
      `<div class="insight-status">${badge(r.current.state)}<strong>${esc(r.current.reason)}</strong></div><p class="muted tiny">总历时 ${(r.current.elapsed_ms / 1000).toFixed(1)} 秒 · 投递 ${r.current.attempts} 次</p><div class="trace-list">${r.events.map((e) => `<article><span class="trace-point"></span><div><h3>${esc(e.label)}</h3><p class="muted tiny">${esc(timeText(e.at))} · ${esc(e.source)}${e.elapsed_ms === null ? '' : ' · 本次处理 ' + (e.elapsed_ms / 1000).toFixed(2) + ' 秒'}</p>${Number.isInteger(e.detail?.native_call_ms) ? `<p class="muted tiny">原生处理 ${(e.detail.native_call_ms / 1000).toFixed(2)} 秒 · 人工等待 ${((e.detail.approval_wait_ms || 0) / 1000).toFixed(2)} 秒</p>` : ''}${e.blocked_by.map((b) => `<button class="btn ghost small" data-insight="trace" data-id="${esc(b.operation_id)}">等待 ${esc(b.tool)} · ${esc(b.operation_id.slice(0, 12))}</button>`).join('')}</div></article>`).join('') || '<p class="muted">此操作没有可用的阶段记录，可能由旧版 Agent 执行。</p>'}</div><p class="form-note">最多保留最近 200 项阶段事件。时间为 Hub 收到事件的时刻；阶段缺失不等于操作未执行。</p>`,
      `<button class="btn" data-insight="trace" data-id="${esc(id)}">${icon('refresh')}刷新链路</button>`,
      true,
    );
  } catch (error) {
    if (session === S.session && loading.isConnected)
      $('.modal-body', loading).innerHTML = notice(esc(error.message));
    throw error;
  }
}
async function artifactsHTML(seq) {
  const session = S.session,
    w = { ...deliveryState() },
    r = await tool('artifacts_list', { project: w.project, cursor: w.cursor, limit: 20 });
  if (seq !== S.renderSeq || session !== S.session) return '';
  deliveryState().next = r.next_cursor;
  return (
    heading(
      '产物交付',
      'ARTIFACTS / VERIFIED DELIVERY',
      '',
      `<button class="btn ghost" data-action="refresh">${icon('refresh')}刷新</button><button class="btn primary" data-insight="register">${icon('plus')}登记产物</button>`,
    ) +
    `<div class="filters"><select id="artifact-project" aria-label="筛选产物项目"><option value="">全部项目</option>${S.projects.map((p) => `<option value="${esc(p.id)}" ${w.project === p.id ? 'selected' : ''}>${esc(p.alias)}</option>`).join('')}</select></div>` +
    `<div class="insight-grid">${r.artifacts.map((a) => `<article class="panel insight-card artifact-card"><div class="card-top"><span class="eyebrow">${esc(S.projects.find((p) => p.id === a.project_id)?.alias || a.project_id)}</span>${a.expired ? '<span class="badge neutral">已过期</span>' : onlineBadge(a.device_online)}</div><h2>${esc(a.name)}</h2><div class="artifact-size">${humanBytes(a.bytes)}</div><p class="muted tiny">到期 ${esc(timeText(a.expires))}</p><div class="artifact-hash"><span>SHA-256</span><code>${esc(a.sha256)}</code></div><div class="actions">${!a.expired ? `<a class="btn primary" href="/api/artifacts/${encodeURIComponent(a.artifact_id)}/download" download>${icon('download')}下载文件</a>` : ''}<button class="btn ghost" data-insight="artifact" data-id="${esc(a.artifact_id)}">详情与续传</button></div></article>`).join('') || empty('还没有交付产物。')}</div>` +
    `<div class="pagination"><span>每页最多 20 项</span><div class="actions"><button class="btn ghost small" data-insight="artifact-prev" ${w.history.length ? '' : 'disabled'}>上一页</button><button class="btn ghost small" data-insight="artifact-next" ${r.next_cursor ? '' : 'disabled'}>下一页</button></div></div>` +
    uiHelp(
      '下载与保留规则',
      '设备在线时可下载。快照保留 7 天，单文件上限 512 MiB，总预算 2 GiB；下载需当前登录或有效授权，不是公开链接。',
    )
  );
}
function bindArtifacts() {
  const s = $('#artifact-project');
  if (s)
    s.onchange = (e) => {
      Object.assign(deliveryState(), { project: e.target.value, cursor: '', history: [] });
      renderPage(false).catch((err) => toast(err.message, true));
    };
}
function registerArtifact() {
  const projects = S.projects.filter((p) => p.mode === 'write');
  if (!projects.length) {
    toast('请先添加可写项目', true);
    return;
  }
  const dialog = modal(
    '登记交付产物',
    `<form id="artifact-form"><div class="field"><label for="artifact-register-project">项目</label><select name="project" id="artifact-register-project">${projects.map((p) => `<option value="${esc(p.id)}" ${p.id === (deliveryState().project || S.work.project) ? 'selected' : ''}>${esc(p.alias)}</option>`).join('')}</select></div><div class="field"><label for="artifact-path">项目内文件路径</label><input id="artifact-path" name="path" required maxlength="1024" placeholder="dist/release.zip"><small>只登记已生成的普通文件；不会自动压缩文件夹或执行构建。</small></div><div class="field"><label for="artifact-name">下载名称（可选）</label><input id="artifact-name" name="name" maxlength="180" placeholder="默认使用原文件名"></div><div class="field"><label for="artifact-source">来源操作编号（可选）</label><input id="artifact-source" name="source_operation_id" maxlength="100"></div><p class="form-note" data-workflow-hint>归档内部是否含凭据需自行检查。文件将复制到独立快照，不会因为原文件后来改变而混用内容。</p></form>`,
    buttons('artifact-save', '生成固定快照'),
  );
  bindWorkflowSubmit(
    dialog,
    $('#artifact-form'),
    $('#artifact-save'),
    'artifacts_register',
    () => Object.fromEntries(new FormData($('#artifact-form'))),
    async (receipt) => {
      const session = S.session;
      const r = await settled(Promise.resolve(receipt));
      if (session !== S.session || !dialog.isConnected) return;
      closeModal(dialog);
      if (S.page === 'artifacts') await renderPage(false);
      await artifactDetail(r.artifact_id);
    },
  );
}
async function artifactDetail(id) {
  const session = S.session,
    loading = modal('产物详情', '<p class="muted">读取文件快照信息…</p>');
  try {
    const a = await tool('artifacts_get', { artifact_id: id });
    if (session !== S.session || !loading.isConnected) return;
    const command = `python -m scripts.download_artifact --hub ${location.origin} --artifact-id ${id} --token-file /path/to/private-token.txt --output /path/to/output-file`;
    modal(
      a.name,
      `<dl class="kv"><dt>大小</dt><dd>${humanBytes(a.bytes)}</dd><dt>状态</dt><dd>${a.expired ? '已过期' : a.device_online ? '设备在线' : '设备离线，联网后续传'}</dd><dt>SHA-256</dt><dd><code>${esc(a.sha256)}</code></dd><dt>到期时间</dt><dd>${esc(timeText(a.expires))}</dd><dt>产物编号</dt><dd><code>${esc(a.artifact_id)}</code></dd></dl><h3>可靠续传</h3><p class="form-note">命令行下载器会保留 .part 文件、使用 Range 续传并验证完整 SHA-256。令牌必须是对此产物有读取权限的授权；面板自己的产物不会自动交给其他授权。</p><div class="code-box"><pre>${esc(command)}</pre></div><p class="form-note">上面的路径需要替换为实际私有令牌和保存位置。浏览器支持普通下载；是否自动续传取决于浏览器。</p>`,
      a.expired
        ? ''
        : `<a class="btn primary" href="/api/artifacts/${encodeURIComponent(id)}/download" download>${icon('download')}下载文件</a>`,
      true,
    );
  } catch (error) {
    if (loading.isConnected && session === S.session)
      $('.modal-body', loading).innerHTML = notice(esc(error.message));
    throw error;
  }
}
function openSearchSession() {
  const { project, workspace_id = '' } = workTarget();
  if (!project) {
    toast('请先选择项目', true);
    return;
  }
  const dialog = modal(
    '项目检索',
    `<form id="session-search-form"><div class="field"><label for="session-query">查找内容</label><input id="session-query" name="query" required maxlength="200"></div><div class="form-row"><div class="field"><label for="session-mode">查找方式</label><select id="session-mode" name="mode"><option value="text">文本</option><option value="symbols">函数、类与方法定义</option><option value="references">引用候选（非类型解析）</option></select></div><div class="field"><label for="session-path">范围</label><input id="session-path" name="path" value="." required></div></div><div class="field"><label for="session-glob">文件模式</label><input id="session-glob" name="file_glob" value="*" required></div><p class="form-note" data-workflow-hint>保存文件清单和扫描位置，按页推进。默认累计扫描预算 60 秒，最多 5000 个结果；会明确标记跳过与截断。</p></form>`,
    buttons('session-search-start', '开始检索'),
  );
  bindWorkflowSubmit(
    dialog,
    $('#session-search-form'),
    $('#session-search-start'),
    'searches_start',
    () => ({
      project,
      workspace_id,
      ...Object.fromEntries(new FormData($('#session-search-form'))),
    }),
    async (receipt) => {
      const session = S.session,
        r = await settled(Promise.resolve(receipt));
      if (session !== S.session || !dialog.isConnected) return;
      closeModal(dialog);
      await searchSessionPage(project, r.search_id, 0, workspace_id);
    },
  );
}
async function searchSessionPage(project, id, cursor, workspace_id = '') {
  const session = S.session,
    loading = modal('检索结果', '<p class="muted">续取已保存的检索结果…</p>');
  try {
    const r = await settled(
      tool('searches_get', { project, workspace_id, search_id: id, cursor, limit: 50 }),
    );
    if (session !== S.session || !loading.isConnected) return;
    modal(
      '检索结果',
      `<div class="insight-status">${badge(r.state)}<span>已扫描 ${r.scanned_files} / ${r.file_count} 个文件 · 跳过 ${r.skipped_files}</span></div><p class="muted tiny">搜索 ${esc(id)} · 当前 ${r.result_count} 条结果${r.truncated ? ' · 已达预算，结果不完整' : ''}</p>${r.error ? notice(esc(r.error)) : ''}<div class="search-results">${r.results.map((x) => `<article><div class="search-result-head"><strong>${esc(x.path)}:${x.line}</strong>${x.stale ? '<span class="badge blocked">文件已变化</span>' : ''}</div>${x.qualified_name ? `<p class="muted tiny">${esc(x.qualified_name)} · ${esc(x.kind)}</p>` : ''}<pre>${esc(x.snippet)}</pre><button class="btn ghost small" data-insight="source" data-project="${esc(project)}" data-workspace="${esc(workspace_id)}" data-path="${esc(x.path)}" data-line="${x.line}">读取当前源码</button></article>`).join('') || '<p class="muted">本页暂无匹配；若扫描尚未完成，可继续下一段。</p>'}</div><p class="form-note">结果不是全仓原子快照。文件变化会标记 stale；结构引用只是语法候选，不保证跨文件类型关系。</p>`,
      `<button class="btn ghost" data-insight="search-page" data-project="${esc(project)}" data-workspace="${esc(workspace_id)}" data-id="${id}" data-cursor="0">回到第一页</button>${r.state === 'running' ? `<button class="btn danger" data-insight="search-cancel" data-project="${esc(project)}" data-workspace="${esc(workspace_id)}" data-id="${id}">停止检索</button>` : ''}${r.has_more ? `<button class="btn primary" data-insight="search-page" data-project="${esc(project)}" data-workspace="${esc(workspace_id)}" data-id="${id}" data-cursor="${r.cursor}">继续下一段</button>` : ''}`,
      true,
    );
  } catch (error) {
    if (session === S.session && loading.isConnected)
      $('.modal-body', loading).innerHTML = notice(esc(error.message));
    throw error;
  }
}
async function currentSymbols() {
  const { project, workspace_id = '' } = workTarget(),
    path = S.work.path;
  if (!project || !path) {
    toast('先在工作台选择源码文件', true);
    return;
  }
  const session = S.session,
    loading = modal('代码结构', '<p class="muted">解析当前文件的函数与类型…</p>');
  try {
    const r = await settled(tool('code_symbols', { project, workspace_id, path, limit: 1000 }));
    if (session !== S.session || !loading.isConnected) return;
    modal(
      '代码结构 · ' + path,
      `<p class="muted tiny">${esc(r.language)} · ${esc(r.backend)} · SHA ${esc(r.sha256.slice(0, 16))}${r.truncated ? ' · 结构结果已截断' : ''}</p><div class="symbol-list">${r.symbols.map((s) => `<button data-insight="source" data-project="${esc(project)}" data-workspace="${esc(workspace_id)}" data-path="${esc(path)}" data-line="${s.line}"><span><strong>${esc(s.qualified_name)}</strong><small>${esc(s.kind)}</small></span><span>${s.line}–${s.end_line}</span></button>`).join('') || '<p class="muted">该文件没有可识别的定义。</p>'}</div><p class="form-note">Python AST / Tree-sitter 语法结构，不是完整语言服务器。文件语法错误或语言不支持时会明确报错。</p>`,
      '',
      true,
    );
  } catch (error) {
    if (session === S.session && loading.isConnected)
      $('.modal-body', loading).innerHTML = notice(esc(error.message));
    throw error;
  }
}
document.addEventListener('click', async (e) => {
  const b = e.target.closest('[data-insight]');
  if (!b || b.disabled || !S.session) return;
  try {
    const w = deliveryState();
    switch (b.dataset.insight) {
      case 'trace':
        await insightTrace(b.dataset.id);
        break;
      case 'register':
        registerArtifact();
        break;
      case 'artifact':
        await artifactDetail(b.dataset.id);
        break;
      case 'artifact-next':
        if (w.next) {
          w.history.push(w.cursor);
          w.cursor = w.next;
          await renderPage(false);
        }
        break;
      case 'artifact-prev':
        if (w.history.length) {
          w.cursor = w.history.pop();
          await renderPage(false);
        }
        break;
      case 'search':
        openSearchSession();
        break;
      case 'symbols':
        await currentSymbols();
        break;
      case 'search-page':
        await searchSessionPage(
          b.dataset.project,
          b.dataset.id,
          Number(b.dataset.cursor),
          b.dataset.workspace || '',
        );
        break;
      case 'search-cancel':
        await settled(
          tool('searches_cancel', {
            project: b.dataset.project,
            workspace_id: b.dataset.workspace || '',
            search_id: b.dataset.id,
          }),
        );
        await searchSessionPage(b.dataset.project, b.dataset.id, 0, b.dataset.workspace || '');
        break;
      case 'source':
        await workflowReadDocument(
          b.dataset.project,
          b.dataset.path,
          Number(b.dataset.line || 1),
          b.dataset.workspace || '',
        );
        break;
    }
  } catch (error) {
    toast(error.message, true);
  }
});
