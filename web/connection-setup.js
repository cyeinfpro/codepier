'use strict';
window.CodePierConnectionSetup = (() => {
  const lifetime = CP.createPanel({ owner: () => String(S.session) + ':' + S.space_id });
  const labels = {
    observed: '已连通',
    allowed: '当前授权有效',
    blocked: '当前阻止',
    stale: '需要重新测试',
    unknown: '未知',
    online: '在线',
    offline: '离线',
  };
  function html() {
    const active = S.grants.filter(
      (g) => !g.revoked && (g.status === 'active' || (!g.status && g.expires > Date.now() / 1000)),
    );
    const latest = active.find((g) => g.client_id) || active[0];
    return `<section class="panel connection-check-card" id="connection-setup"><div class="panel-head"><h2>连接检查</h2><small class="muted">在 ChatGPT 新建对话并启用 CodePier 后测试</small></div><div class="panel-body">
      <form id="connection-check-form" class="connection-check-inline">
        <label class="field">要测试的连接<select name="grant_id" required><option value="">请选择连接</option>${S.grants.map((g) => `<option value="${esc(g.id)}" ${g.id === latest?.id ? 'selected' : ''}>${esc(g.label)}${g.client_id ? '' : ' · 访问令牌'}</option>`).join('')}</select></label>
        <details class="form-advanced"><summary>排查选项</summary>
          <label class="field">项目<select name="project_id"><option value="">仅检查连接</option>${S.projects.map((p) => `<option value="${esc(p.id)}">${esc(p.alias)}</option>`).join('')}</select></label>
          <label class="field">工具目录版本<input name="catalog" maxlength="64" pattern="[a-f0-9]{64}" autocomplete="off"></label>
        </details><button type="submit" class="btn primary">测试连接</button></form>
      <div id="connection-check-result" role="status" aria-live="polite"></div></div></section>`;
  }
  function tunnelHTML() {
    return `      <details><summary>Secure MCP Tunnel 配置预览</summary>
        <p>在能访问面板的电脑上运行生成的命令。</p>
        <form id="connection-tunnel-form"><div class="form-row">
          <label class="field">已有 Tunnel ID<input name="tunnel_id" required autocomplete="off" placeholder="tunnel_…"></label>
          <label class="field">本地 Profile 名<input name="profile" required value="codepier" autocomplete="off"></label>
          <label class="field">Hub 安装绝对路径<input name="install_dir" required autocomplete="off" placeholder="/opt/codepier"></label>
          <label class="field">该主机可访问的 Hub URL<input name="hub_url" required value="http://127.0.0.1:8765" autocomplete="off"></label>
        </div><p>预览只需连接参数，访问令牌请留在本机。</p>
        <button class="btn" type="submit">预览并校验</button></form>
        <div id="connection-tunnel-result" role="status" aria-live="polite"></div>
      </details>`;
  }
  function renderStatus(result) {
    const last = result.last_actual_call;
    return `<dl class="kv"><dt>Hub MCP</dt><dd>${esc(result.hub_mcp_url || '未知')}</dd>
      <dt>当前空间 / 持有人</dt><dd>${esc(result.space.label)} / ${esc(result.owner.label)}</dd>
      <dt>当前服务版本 / 工具协议</dt><dd>${esc(result.server_info.version)} / ${esc(result.contract_protocol)}</dd>
      <dt>当前工具目录版本</dt><dd><code>${esc(result.catalog.current_effective_sha256 || '未知')}</code></dd>
      <dt>最近发送的目录版本</dt><dd><code>${esc(result.catalog.last_served_sha256 || '未知')}</code></dd>
      <dt>最近实际调用</dt><dd>${last ? esc(last.tool) + ' · 服务 ' + esc(last.server_info.version) + ' · 工具协议 ' + esc(last.tool_contract_version) + ' · ' + esc(timeText(last.observed_at)) : '未知'}</dd>
      <dt>ChatGPT 工具扫描 / 缓存</dt><dd>尚未确认</dd>
      <dt>客户端目录版本</dt><dd>${result.catalog.client_report_matches_last_served == null ? '未知' : result.catalog.client_report_matches_last_served ? '与最近发送的版本相同' : '版本不同，请刷新 ChatGPT 连接后再测试'}</dd></dl>
      <ol class="integration-steps">${result.layers
        .map(
          (
            layer,
          ) => `<li data-connection-layer="${esc(layer.id)}"><strong>${esc(layer.label)}：${esc(labels[layer.status] || layer.status)}</strong>
        <p>${esc(layer.evidence?.observed_at ? '观察时间：' + timeText(layer.evidence.observed_at) : layer.observed_at ? '最近心跳：' + timeText(layer.observed_at) : layer.checked_at ? '检查时间：' + timeText(layer.checked_at) : '尚无检查结果')}
        ${layer.read_capability ? ' · 读取能力：' + esc(layer.read_capability) : ''}</p>${layer.note ? '<p>' + esc(layer.note) + '</p>' : ''}</li>`,
        )
        .join('')}</ol>
      <p>${esc(result.catalog.note)}</p>
      <ul>${result.next_actions.map((item) => '<li>' + esc(item) + '</li>').join('')}</ul>`;
  }
  function renderPreview(value) {
    return `<h3>${value.configuration_valid ? '配置文本校验通过，尚未运行' : '配置需修正'}</h3>
      <p>连接：未知 · 真实 Tunnel 验收：未运行 · PAT 实际范围：未验证</p>
      <ul>${[...value.errors, ...value.warnings].map((item) => '<li>' + esc(item.message) + '</li>').join('')}</ul>
      ${value.artifacts.map((item) => '<h4>' + esc(item.name) + '</h4><pre class="integration-page-text">' + esc(item.content) + '</pre>').join('')}
      ${value.commands.map((item) => '<h4>' + esc(item.title) + '</h4><pre class="integration-page-text">' + esc(item.shell) + '</pre>').join('')}
      <ol>${value.manual_steps.map((item) => '<li><strong>' + esc(item.title) + '</strong><p>' + esc(item.detail) + '</p></li>').join('')}</ol>`;
  }
  function bind() {
    const root = $('#connection-setup'),
      scope = lifetime.mount(root);
    if (!scope) return;
    let checkEpoch = 0,
      previewEpoch = 0;
    const current = () => scope.current() && S.page === 'connect' && root.isConnected;
    const check = $('#connection-check-form', root),
      output = $('#connection-check-result', root);
    const invalidate = () => {
      checkEpoch++;
      output.replaceChildren();
    };
    scope.listen(check, 'change', invalidate);
    scope.listen(check, 'submit', async (event) => {
      event.preventDefault();
      if (!check.reportValidity() || !current()) return;
      const epoch = ++checkEpoch,
        button = $('button', check);
      button.disabled = true;
      output.textContent = '正在测试…';
      try {
        const query = new URLSearchParams({
          project_id: check.elements.project_id.value,
          client_catalog_sha256: check.elements.catalog.value,
        });
        const value = await api(
          '/api/grants/' +
            encodeURIComponent(check.elements.grant_id.value) +
            '/connection-status?' +
            query,
          { signal: scope.signal, retryDelays: [], retrySafe: false },
        );
        if (current() && epoch === checkEpoch) output.innerHTML = renderStatus(value);
      } catch (error) {
        if (current() && epoch === checkEpoch) output.textContent = error.message;
      } finally {
        if (current()) button.disabled = false;
      }
    });
    if (check.elements.grant_id.value) check.requestSubmit();
    const form = $('#connection-tunnel-form'),
      preview = $('#connection-tunnel-result');
    if (!form || !preview) return;
    scope.listen(form, 'input', () => {
      previewEpoch++;
      preview.replaceChildren();
    });
    scope.listen(form, 'submit', async (event) => {
      event.preventDefault();
      if (!form.reportValidity() || !current()) return;
      const epoch = ++previewEpoch,
        button = $('button', form);
      button.disabled = true;
      preview.textContent = '正在校验配置文本…';
      const body = Object.fromEntries(new FormData(form));
      try {
        const value = await api('/api/connection/tunnel-preview', {
          method: 'POST',
          body: JSON.stringify(body),
          signal: scope.signal,
          retryDelays: [],
          retrySafe: false,
        });
        if (current() && epoch === previewEpoch) preview.innerHTML = renderPreview(value);
      } catch (error) {
        if (current() && epoch === previewEpoch) preview.textContent = error.message;
      } finally {
        if (current()) button.disabled = false;
      }
    });
  }
  return { html, tunnelHTML, bind, detach: lifetime.detach, renderStatus, renderPreview };
})();
