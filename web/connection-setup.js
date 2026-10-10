'use strict';
window.CodePierConnectionSetup = (() => {
  const lifetime = CP.createPanel({ owner: () => String(S.session) + ':' + S.space_id });
  const labels = {
    observed: '已有服务端证据',
    allowed: '当前授权有效',
    blocked: '当前阻止',
    stale: '历史证据，需重新验收',
    unknown: '未知',
    online: '在线',
    offline: '离线',
  };
  function html() {
    return `<section class="panel" id="connection-setup"><div class="panel-head"><h2>ChatGPT 接入与分层自检</h2></div><div class="panel-body">
      <ol class="integration-steps">
        <li><strong>核对身份与入口</strong><p>ChatGPT 登录、CodePier OAuth/PAT 授权和模型 API 凭据互相独立。MCP 工具本身不要求模型 Key。先核对本页 Hub 地址、当前 Space 和连接所有者。</p></li>
        <li><strong>在 ChatGPT 添加并启用连接</strong><p>公开 HTTPS 继续使用 OAuth；私有部署可在下方预览官方 Tunnel 配置。客户端入口与工作区权限以 <a href="https://developers.openai.com/plugins/deploy/connect-chatgpt" target="_blank" rel="noopener noreferrer">官方接入指南</a> 为准。</p></li>
        <li><strong>刷新后做最小只读验收</strong><p>在客户端刷新工具元数据，再在新对话启用 CodePier。先列出项目、读取一个已授权测试文件，核对原操作结果。这里的检查不调用模型、不代你创建授权，也不会运行测试命令。</p></li>
      </ol>
      <form id="connection-check-form"><div class="form-row">
        <label class="field">CodePier 连接<select name="grant_id" required><option value="">请选择连接</option>${S.grants.map((g) => `<option value="${esc(g.id)}">${esc(g.label)} · ${g.client_id ? 'OAuth' : 'PAT'}</option>`).join('')}</select></label>
        <label class="field">验收项目<select name="project_id"><option value="">仅检查连接</option>${S.projects.map((p) => `<option value="${esc(p.id)}">${esc(p.alias)}</option>`).join('')}</select></label>
      </div><label class="field">客户端主动报告的目录 SHA（可选）<input name="catalog" maxlength="64" pattern="[a-f0-9]{64}" autocomplete="off"><small>仅用于比对，不证明 ChatGPT 缓存已刷新。</small></label>
      <button type="submit" class="btn primary">读取连接证据</button></form>
      <div id="connection-check-result" role="status" aria-live="polite"></div>
      <details><summary>私有部署：官方 Secure MCP Tunnel 配置预览</summary>
        <p>仅生成本地配置文本，不安装、保存凭据、创建 Tunnel 或启动连接。运行位置是能访问 Hub 的主机，并非上方项目的 Agent。</p>
        <form id="connection-tunnel-form"><div class="form-row">
          <label class="field">已有 Tunnel ID<input name="tunnel_id" required autocomplete="off" placeholder="tunnel_…"></label>
          <label class="field">本地 Profile 名<input name="profile" required value="codepier" autocomplete="off"></label>
          <label class="field">Hub 安装绝对路径<input name="install_dir" required autocomplete="off" placeholder="/opt/codepier"></label>
          <label class="field">该主机可访问的 Hub URL<input name="hub_url" required value="http://127.0.0.1:8765" autocomplete="off"></label>
        </div><p>不要填写任何 PAT、API Key 或密码。Tunnel+stdio 使用既有 PAT 的固定身份，必须核对该 PAT 范围及 Tunnel 关联受众。</p>
        <button class="btn" type="submit">预览并校验</button></form>
        <div id="connection-tunnel-result" role="status" aria-live="polite"></div>
      </details></div></section>`;
  }
  function renderStatus(result) {
    const last = result.last_actual_call;
    return `<dl class="kv"><dt>Hub MCP</dt><dd>${esc(result.hub_mcp_url || '未知')}</dd>
      <dt>当前 Space / 连接所有者</dt><dd>${esc(result.space.label)} / ${esc(result.owner.label)}</dd>
      <dt>当前服务版本 / 工具合约</dt><dd>${esc(result.server_info.version)} / ${esc(result.contract_protocol)}</dd>
      <dt>当前有效工具目录 SHA</dt><dd><code>${esc(result.catalog.current_effective_sha256 || '未知')}</code></dd>
      <dt>最近返回目录 SHA</dt><dd><code>${esc(result.catalog.last_served_sha256 || '未知')}</code></dd>
      <dt>最近实际调用</dt><dd>${last ? esc(last.tool) + ' · 服务 ' + esc(last.server_info.version) + ' · 工具合约 ' + esc(last.tool_contract_version) + ' · ' + esc(timeText(last.observed_at)) : '未知'}</dd>
      <dt>ChatGPT 工具扫描 / 缓存</dt><dd>未知 / 未观测；不会自动刷新</dd>
      <dt>报告 SHA 比对</dt><dd>${result.catalog.client_report_matches_last_served == null ? '未知' : result.catalog.client_report_matches_last_served ? '与最近返回目录相同（客户端自报）' : '与最近返回目录不同，请刷新后只读验收'}</dd></dl>
      <ol class="integration-steps">${result.layers
        .map(
          (
            layer,
          ) => `<li data-connection-layer="${esc(layer.id)}"><strong>${esc(layer.label)}：${esc(labels[layer.status] || layer.status)}</strong>
        <p>${esc(layer.evidence?.observed_at ? '观察时间：' + timeText(layer.evidence.observed_at) : layer.observed_at ? '最近心跳：' + timeText(layer.observed_at) : layer.checked_at ? '检查时间：' + timeText(layer.checked_at) : '尚无服务端证据')}
        ${layer.read_capability ? ' · 读取能力：' + esc(layer.read_capability) : ''}</p>${layer.note ? '<p>' + esc(layer.note) + '</p>' : ''}</li>`,
        )
        .join('')}</ol>
      <p>${esc(result.catalog.note)}</p><p>记录按原连接、项目与权限范围隔离。旧授权或旧映射证据不能证明现在可用。</p>
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
      output.textContent = '正在读取当前授权与历史证据…';
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
    const form = $('#connection-tunnel-form', root),
      preview = $('#connection-tunnel-result', root);
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
  return { html, bind, detach: lifetime.detach, renderStatus, renderPreview };
})();
