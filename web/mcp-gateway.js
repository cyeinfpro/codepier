'use strict';
// State contains redacted configuration only. Never retain backend tokens in storage.
window.CodePierGateway = (() => {
  let state = null;
  const root = '/api/mcp-gateway';
  const field = (name, label, extra = '') =>
    `<label class="field">${esc(label)}<input name="${name}" ${extra}></label>`;
  const section = (title, body) =>
    `<section class="panel"><div class="panel-head"><h2>${esc(title)}</h2></div><div class="panel-body">${body}</div></section>`;
  const options = (items, key = 'label') =>
    items.map((item) => `<option value="${esc(item.id)}">${esc(item[key])}</option>`).join('');
  const accountLabel = (id) => state?.accounts.find((account) => account.id === id)?.label || id;
  const manageable = (account) =>
    !!account &&
    (account.owner_user_id === state.user_id || (account.sharing === 'space' && state.space_admin));
  function publishedSectionHTML() {
    if (!state.bindings.length) return empty('在帐户的工具发现窗口选择工具并发布命名空间。');
    const totalTools = state.bindings.reduce((sum, b) => sum + (b.tools?.length || 0), 0);
    return (
      `<div class="gw-published-toolbar"><input type="search" id="gw-published-filter" autocapitalize="none" autocorrect="off" spellcheck="false" enterkeyhint="search" placeholder="筛选命名空间或已发布工具" aria-label="筛选已发布工具"><span id="gw-published-count" role="status">共 ${state.bindings.length} 组 · ${totalTools} 项工具</span></div>` +
      state.bindings
        .map((b) => {
          const acc = state.accounts.find((a) => a.id === b.account_id);
          const canEdit = manageable(acc) && Boolean(acc?.catalog_hash);
          return `<div class="grant-row" data-gw-binding-row="${esc(b.id)}"><div><strong>${esc(b.alias)}__…</strong> · ${esc(accountLabel(b.account_id))} · ${b.enabled ? '启用' : '暂停'}<p class="mono gw-wrap">${esc(b.id)}</p><p class="gw-wrap">${b.tools.map(esc).join(' · ')}</p>${canEdit && b.tools.length > 1 ? `<details class="gw-binding-checklist"><summary>勾选管理已发布工具 · ${b.tools.length} 项</summary><div class="gw-binding-checks">${b.tools.map((t) => `<label class="check"><input type="checkbox" data-gw-binding-tool="${esc(b.id)}" value="${esc(t)}" checked><span>${esc(t)}</span></label>`).join('')}</div></details>` : ''}</div><div class="actions">${state.space_admin ? `<button class="btn small" data-gw-role="${esc(b.id)}">配置角色工具权限</button>` : ''}${manageable(acc) ? `<button class="btn small" data-gw-toggle="bindings" data-id="${esc(b.id)}" data-version="${b.version}" data-enabled="${b.enabled ? '1' : '0'}">${b.enabled ? '暂停' : '恢复'}</button>` : ''}</div></div>`;
        })
        .join('') +
      '<p id="gw-published-empty" class="gw-filter-empty" hidden>没有匹配的已发布工具组。</p>'
    );
  }

  function reset() {
    state = null;
  }

  async function html() {
    const session = S.session,
      space = S.space_id,
      sequence = S.renderSeq;
    const value = await api(root);
    if (session !== S.session || space !== S.space_id || sequence !== S.renderSeq) return '';
    state = value;
    if (!state.enabled)
      return (
        heading('MCP 网关', 'EXTERNAL MCP', '') +
        notice(
          '尚未启用。实例管理员需配置 CODEPIER_MCP_GATEWAY=1 并重启 Hub。现有 MCP 与 IAM 不受影响。',
        )
      );
    return `<div id="gateway-page">${heading(
      'MCP 网关',
      '连接与授权',
      '独立帐户 · 明确发布 · 当前权限',
      `${state.instance_admin ? '<button class="btn primary" data-gw="connector">添加 MCP 服务</button>' : ''}<button class="btn" data-gw="account">连接后端帐户</button>`,
    )}
      ${notice('外部工具保留原始 schema，以“命名空间__工具”发布。发现不会自动发布；共享服务帐户可能访问同一份后端资料，工具权限不是后端资源沙箱。')}
      ${section('已批准的服务', state.connectors.map((c) => `<div class="grant-row"><div><strong>${esc(c.label)}</strong> · ${c.enabled ? '启用' : '暂停'}<p class="mono gw-wrap">${esc(c.endpoint)}</p><small>${esc(c.protocol)}${c.allow_http ? ' · 显式内网 HTTP' : ' · HTTPS'}</small></div>${state.instance_admin ? `<button class="btn small" data-gw-toggle="connectors" data-id="${esc(c.id)}" data-version="${c.version}" data-enabled="${c.enabled ? '1' : '0'}">${c.enabled ? '暂停' : '恢复'}</button>` : ''}</div>`).join('') || empty('先由实例管理员添加固定 MCP endpoint。'))}
      ${section('账号与凭据', state.accounts.map((a) => `<div class="grant-row"><div><strong>${esc(a.label)}</strong> · ${a.sharing === 'space' ? '空间共享' : '个人私有'} · ${a.enabled ? '启用' : '暂停'}<p class="mono gw-wrap">${esc(a.id)}</p><small>服务：${esc(state.connectors.find((c) => c.id === a.connector_id)?.label || '未提供')} · ${a.catalog_hash ? '已有发现目录，仍需明确发布' : '尚未发现当前帐户的工具'}</small></div>${manageable(a) ? `<div class="actions"><button class="btn small" data-gw-discover="${esc(a.id)}">发现 / 审核工具</button><button class="btn small" data-gw-account="${esc(a.id)}">更新凭据</button><button class="btn small" data-gw-toggle="accounts" data-id="${esc(a.id)}" data-version="${a.version}" data-enabled="${a.enabled ? '1' : '0'}">${a.enabled ? '暂停' : '恢复'}</button></div>` : ''}</div>`).join('') || empty('凭据加密存放在 Hub，不会回传到页面。'))}
      ${section('已发布工具', publishedSectionHTML())}
      ${section('我的连接委派', `<p class="form-note">既有 Role grant 不会自动获得外部 MCP 权限。下列同意仅影响该 grant；撤销不影响本地 CodePier 项目权限。</p>${state.grants.map((g) => `<div class="grant-row"><div><strong>${esc(g.label)}</strong><p class="mono gw-wrap">${esc(g.id)}</p><small>${g.consent_version ? '已明确同意外部 MCP 委派' : '尚未同意'}</small></div><button class="btn small" data-gw-consent="${esc(g.id)}">${g.consent_version ? '撤销外部委派' : '查看政策并同意'}</button></div>`).join('') || empty('先在访问 Profiles 建立 Role 连接。')}`)}
      ${section('最近调用', `<p class="form-note">最多显示 50 条私有调用摘要，不包含原始参数、凭据或结果。unknown 表示执行结果待确认，请先查看原请求的状态。</p>${state.calls.map((c) => `<div class="grant-row"><div><strong>${esc(c.tool)}</strong> · ${esc(c.state)}<p class="mono gw-wrap">${esc(c.id)}</p><small>${esc(timeText(c.created))}${c.error_code ? ' · ' + esc(c.error_code) : ''}</small></div></div>`).join('') || empty('尚无外部 MCP 调用。')}`)}
      <div id="gw-sticky-save" class="panel gw-sticky-save-bar" hidden role="region" aria-label="未保存的工具发布变更"><span id="gw-sticky-save-text" role="status"></span><button type="button" class="btn primary small" id="gw-sticky-save-btn">保存并发布</button></div></div>`;
  }

  function form(title, body, save) {
    const session = S.session,
      space = S.space_id;
    const dialog = modal(
      title,
      `<form id="gw-form">${body}<p id="gw-status" class="form-note" role="status"></p></form>`,
      '<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" type="submit" form="gw-form">确认保存</button>',
      body.includes('gw-tools') || body.includes('name="networks"'),
    );
    const node = $('#gw-form', dialog);
    node.onsubmit = async (event) => {
      event.preventDefault();
      if (
        node.dataset.submission === 'uncertain' ||
        session !== S.session ||
        space !== S.space_id ||
        !dialog.isConnected
      )
        return;
      const button = $('button[type="submit"]', dialog);
      await busy(button, async () => {
        if (!node.reportValidity()) return;
        $('#gw-status', dialog).textContent = '正在处理…';
        try {
          await save(node);
          if (session !== S.session || space !== S.space_id || !dialog.isConnected) return;
          closeModal(dialog);
          if (title === '审核工具并发布')
            (S.managementTabs || (S.managementTabs = {}))['mcp-gateway'] = 'tools';
          if (S.page === 'mcp-gateway') await renderPage(false);
          toast('MCP 配置已保存');
        } catch (error) {
          if (session === S.session && space === S.space_id && dialog.isConnected) {
            const uncertain =
              error.code === 'NETWORK_UNCERTAIN' ||
              [408, 500, 502, 503, 504].includes(error.status);
            node.dataset.submission = uncertain ? 'uncertain' : 'rejected';
            $('#gw-status', dialog).textContent = uncertain
              ? '保存结果待确认，重复提交已锁定。请关闭并刷新配置查看结果；如需再次保存，请重新填写凭据或明确选择无认证。'
              : error.message;
            $('#gw-status', dialog).classList.add('error-text');
          }
        } finally {
          $$('input[type="password"]', node).forEach((input) => {
            input.value = '';
          });
        }
      });
      if (dialog.isConnected && node.dataset.submission === 'uncertain') button.disabled = true;
    };
    return dialog;
  }
  const post = (path, body) =>
    api(root + path, {
      method: 'POST',
      body: JSON.stringify(body),
      requestTimeout: 50000,
    });

  function newConnector() {
    form(
      '批准一个 MCP 服务',
      `${field('label', '服务名称', 'required maxlength="80"')}${field('endpoint', '固定 MCP endpoint', 'required type="url" inputmode="url" autocapitalize="none" autocorrect="off" spellcheck="false" placeholder="https://mcp.example/mcp"')}<label class="field">协议<select name="protocol"><option value="auto">自动探测（只用发现请求）</option><option value="modern">2026-07-28</option><option value="legacy">旧版 Streamable HTTP</option></select></label><label class="field">额外批准的内网 CIDR（每行一项）<textarea name="networks" rows="3" autocapitalize="none" autocorrect="off" spellcheck="false" placeholder="10.0.100.30/32"></textarea></label><label class="check"><input type="checkbox" name="allow_http">明确允许这些内网地址使用 HTTP</label><p class="form-note">endpoint 不可原地替换；禁止凭据 URL、查询串、重定向和未批准内网。更换服务须建立新连接。</p>`,
      (node) =>
        post('/connectors', {
          label: node.elements.label.value,
          endpoint: node.elements.endpoint.value,
          protocol: node.elements.protocol.value,
          networks: node.elements.networks.value
            .split('\n')
            .map((x) => x.trim())
            .filter(Boolean),
          allow_http: node.elements.allow_http.checked,
        }),
    );
  }

  function newAccount() {
    const available = state.connectors.filter((c) => c.enabled);
    if (!available.length) return toast('需要先添加可用 MCP 服务', true);
    form(
      '连接后端帐户',
      `<label class="field">服务<select name="connector_id">${options(available)}</select></label>${field('label', '帐户名称', 'required maxlength="80"')}${field('token', '后端 Bearer token（无认证服务可留空）', 'type="password" autocomplete="new-password" autocapitalize="none" autocorrect="off" spellcheck="false" maxlength="8192"')}${state.space_admin ? '<label class="check"><input type="checkbox" name="shared">明确作为空间共享服务帐户</label>' : ''}<p class="form-note">不是 CodePier 或 ChatGPT token。当前版本支持预先取得的 Bearer 凭据；后端 OAuth 浏览器授权流程尚未实现。</p>`,
      (node) =>
        post('/accounts', {
          connector_id: node.elements.connector_id.value,
          label: node.elements.label.value,
          token: node.elements.token.value,
          sharing: node.elements.shared?.checked ? 'space' : 'private',
        }),
    );
  }

  function rotateAccount(id) {
    const account = state.accounts.find((item) => item.id === id);
    form(
      '更新后端凭据',
      `${field('token', '新的后端 Bearer token', 'type="password" autocomplete="new-password" autocapitalize="none" autocorrect="off" spellcheck="false" maxlength="8192"')}<label class="check"><input type="checkbox" name="clear_token">明确移除当前凭据，改用无认证连接</label><p class="form-note">更新后暂停目录可用性；重新发现工具确认 schema 后才能使用。</p>`,
      (node) => {
        if (!node.elements.token.value && !node.elements.clear_token.checked)
          throw Object.assign(new Error('请输入新凭据，或明确勾选移除当前凭据。'), { status: 400 });
        return api(root + '/accounts/' + encodeURIComponent(id), {
          method: 'PATCH',
          body: JSON.stringify({
            expected_version: account.version,
            enabled: !!account.enabled,
            token: node.elements.token.value,
          }),
        });
      },
    );
  }

  function bindDiscoverDialog(dialog, out, existing) {
    const node = $('#gw-form', dialog),
      search = $('#gw-tool-search', node),
      onlySelected = $('#gw-selected-only', node),
      discoverBar = $('[data-gw-discover-bar]', node),
      discoverCount = $('[data-gw-discover-count]', node),
      discoverSubmit = $('[data-gw-discover-submit]', node);
    let baselineSig = '';
    const refresh = () => {
      const query = search.value.trim().toLocaleLowerCase();
      const checkedTools = $$('[name="tool"]:checked', node).map((input) => input.value);
      let visible = 0;
      $$('.gw-tool-row', node).forEach((row) => {
        row.hidden =
          !row.textContent.toLocaleLowerCase().includes(query) ||
          (onlySelected.checked && !$('[name="tool"]', row).checked);
        if (!row.hidden) visible++;
      });
      $('#gw-selection-count', node).textContent =
        `已选 ${checkedTools.length} / ${out.tools.length} 项`;
      $('.gw-filter-empty', node).hidden = visible > 0;
      if (!discoverBar) return;
      const dirty = checkedTools.length > 0 && checkedTools.join('\n') !== baselineSig;
      discoverBar.hidden = !dirty;
      if (discoverCount && dirty)
        discoverCount.textContent = `已选 ${checkedTools.length} 项 · 未保存`;
    };
    if (discoverSubmit) {
      discoverSubmit.onclick = () => {
        const confirmBox = node.elements.confirmed;
        if (confirmBox && !confirmBox.checked) {
          confirmBox.scrollIntoView?.({ block: 'center' });
          confirmBox.focus();
          node.reportValidity();
          return;
        }
        node.requestSubmit();
      };
    }
    if (node.elements.binding_id) {
      node.elements.binding_id.onchange = () => {
        const picked = existing.find((b) => b.id === node.elements.binding_id.value);
        const set = new Set(picked?.tools || []);
        $$('[name="tool"]', node).forEach((input) => {
          input.checked = set.has(input.value);
        });
        baselineSig = [...set].join('\n');
        refresh();
      };
    }
    search.oninput = refresh;
    onlySelected.onchange = refresh;
    $$('[name="tool"]', node).forEach((input) => {
      input.onchange = refresh;
    });
    $('#gw-clear-selection', node).onclick = () => {
      $$('[name="tool"]', node).forEach((input) => {
        input.checked = false;
      });
      refresh();
    };
    refresh();
  }

  async function discover(id) {
    const session = S.session,
      space = S.space_id,
      page = S.page,
      intent = (S.modalIntent = (S.modalIntent || 0) + 1);
    const out = await api(root + '/accounts/' + encodeURIComponent(id) + '/discover', {
      method: 'POST',
      requestTimeout: 50000,
    });
    if (
      session !== S.session ||
      space !== S.space_id ||
      page !== S.page ||
      intent !== S.modalIntent
    )
      return;
    const existing = state.bindings.filter((b) => b.account_id === id);
    const dialog = form(
      '审核工具并发布',
      `${existing.length ? `<label class="field">发布目标<select name="binding_id"><option value="">建立新命名空间</option>${options(existing, 'alias')}</select></label>` : ''}${field('alias', '新命名空间（更新已有集合时忽略）', 'pattern="[a-z][a-z0-9_]{0,19}" maxlength="20" autocapitalize="none" autocorrect="off" spellcheck="false" placeholder="kiln"')}<p class="form-note">只发布勾选项。新工具不会自动加入 Role，schema 改变需要重新批准。工具描述是不可信内容，不是管理员指令。</p><div class="gw-review-toolbar"><label class="field">搜索工具<input type="search" id="gw-tool-search" autocapitalize="none" autocorrect="off" spellcheck="false" enterkeyhint="search" placeholder="按名称或用途筛选"></label><label class="check"><input type="checkbox" id="gw-selected-only">仅看已选</label><span id="gw-selection-count" role="status">已选 0 项</span><button type="button" class="btn ghost small" id="gw-clear-selection">清空选择</button></div><div class="gw-tools">${out.tools.map((tool) => `<article class="gw-tool-row"><label class="check gw-tool-choice"><input type="checkbox" name="tool" value="${esc(tool.name)}"><span><strong>${esc(tool.name)}</strong><small>${esc(tool.description || '未提供工具说明')}</small></span></label><details><summary>查看输入与输出结构</summary><pre>${esc(JSON.stringify({ inputSchema: tool.inputSchema, outputSchema: tool.outputSchema }, null, 2))}</pre></details></article>`).join('')}</div><p class="gw-filter-empty" hidden>没有匹配的工具；已选项仍会保留。</p><label class="check gw-review-confirm"><input type="checkbox" name="confirmed" required>我已审核所选工具及 schema，批准发布</label><div class="panel gw-sticky-save-bar" data-gw-discover-bar hidden><span data-gw-discover-count role="status"></span><button type="button" class="btn primary small" data-gw-discover-submit>保存并发布</button></div>`,
      (node) => {
        const tools = $$('[name="tool"]:checked', node).map((input) => input.value);
        if (!tools.length) throw new Error('至少选择一个工具');
        const binding = existing.find((b) => b.id === node.elements.binding_id?.value);
        const body = {
          tools,
          catalog_hash: out.catalog_hash,
          confirmed: node.elements.confirmed.checked,
        };
        return binding
          ? post('/bindings/' + encodeURIComponent(binding.id) + '/publish', {
              ...body,
              expected_version: binding.version,
            })
          : post('/bindings', {
              ...body,
              account_id: id,
              alias: node.elements.alias.value,
            });
      },
    );
    bindDiscoverDialog(dialog, out, existing);
  }

  async function configureRole(id) {
    const session = S.session,
      space = S.space_id,
      page = S.page,
      intent = (S.modalIntent = (S.modalIntent || 0) + 1);
    const binding = state.bindings.find((item) => item.id === id);
    const out = await api('/api/access-roles');
    if (
      session !== S.session ||
      space !== S.space_id ||
      page !== S.page ||
      intent !== S.modalIntent
    )
      return;
    if (!out.roles.length) return toast('请先建立角色', true);
    const dialog = form(
      '配置角色的 MCP 工具规则',
      `<label class="field">角色<select name="role_id">${options(out.roles)}</select></label><p class="form-note">只修改所选角色对 ${esc(binding.alias)} 的规则。其他项目、设备和 MCP 规则保留；不自动替用户同意外部委派。</p>${binding.tools.map((name) => `<label class="check"><input type="checkbox" name="tool" value="${esc(name)}">${esc(name)}</label>`).join('')}<p class="form-note">全部不选代表移除此 binding 的规则。</p>`,
      (node) => {
        const role = out.roles.find((r) => r.id === node.elements.role_id.value);
        if (!role) throw new Error('请选择已有角色');
        const tools = $$('[name="tool"]:checked', node).map((input) => input.value);
        const rules = (role.connector_rules || []).filter((rule) => rule.binding_id !== id);
        if (tools.length) rules.push({ binding_id: id, tools });
        return api('/api/access-roles/' + encodeURIComponent(role.id), {
          method: 'PUT',
          body: JSON.stringify({
            label: role.label,
            enabled: role.enabled,
            project_rules: role.project_rules,
            device_rules: role.device_rules,
            connector_rules: rules,
            expected_version: role.version,
          }),
        });
      },
    );
    const node = $('#gw-form', dialog);
    const select = node.elements.role_id;
    const showCurrent = () => {
      const role = out.roles.find((item) => item.id === select.value);
      const allowed = new Set(
        (role?.connector_rules || [])
          .filter((rule) => rule.binding_id === id)
          .flatMap((rule) => rule.tools),
      );
      $$('[name="tool"]', node).forEach((input) => {
        input.checked = allowed.has(input.value);
      });
    };
    select.onchange = showCurrent;
    showCurrent();
  }

  async function consent(id) {
    const grant = state.grants.find((item) => item.id === id);
    if (grant.consent_version)
      return form(
        '撤销外部 MCP 委派',
        '<p>后续工具调用与结果读取将重新检查并拒绝。已在外部执行的操作无法由撤权回滚。</p>',
        () =>
          api(root + '/grants/' + encodeURIComponent(id) + '/consent', {
            method: 'DELETE',
          }),
      );
    const session = S.session,
      space = S.space_id,
      page = S.page,
      intent = (S.modalIntent = (S.modalIntent || 0) + 1);
    const role = await api('/api/access-roles/' + encodeURIComponent(grant.role_id));
    if (
      session !== S.session ||
      space !== S.space_id ||
      page !== S.page ||
      intent !== S.modalIntent
    )
      return;
    form(
      '明确同意外部 MCP 委派',
      `<p>此连接将按 ${esc(role.label)} 的当前及未来、经批准的 connector_rules 使用外部帐户。后端工具可能产生写入和执行副作用；不是本机项目权限的简单延伸。</p><pre class="gw-wrap">${esc(JSON.stringify(role.connector_rules || [], null, 2))}</pre><label class="check"><input name="confirmed" type="checkbox" required>我理解并同意上述跨 MCP 动态委派</label>`,
      (node) =>
        post('/grants/' + encodeURIComponent(id) + '/consent', {
          confirmed: node.elements.confirmed.checked,
          expected_role_version: role.version,
        }),
    );
  }

  function bindPublishedTools() {
    const filterInput = $('#gw-published-filter'),
      countEl = $('#gw-published-count'),
      emptyEl = $('#gw-published-empty'),
      saveBar = $('#gw-sticky-save'),
      saveText = $('#gw-sticky-save-text'),
      saveBtn = $('#gw-sticky-save-btn');
    if (filterInput && state?.bindings?.length) {
      filterInput.oninput = () => {
        const q = filterInput.value.trim().toLocaleLowerCase();
        let shownGroups = 0,
          shownTools = 0;
        $$('[data-gw-binding-row]').forEach((row) => {
          const b = state.bindings.find((x) => x.id === row.dataset.gwBindingRow);
          const match = !q || row.textContent.toLocaleLowerCase().includes(q);
          row.hidden = !match;
          if (match) {
            shownGroups++;
            shownTools += b?.tools?.length || 0;
          }
        });
        if (countEl)
          countEl.textContent = `显示 ${shownGroups} / ${state.bindings.length} 组 · ${shownTools} 项工具`;
        if (emptyEl) emptyEl.hidden = shownGroups > 0;
      };
    }
    if (!saveBar || !saveBtn || !state?.bindings?.length) return;
    const findDirtyBinding = () => {
      for (const b of state.bindings) {
        const checks = $$(`[data-gw-binding-tool="${CSS.escape(b.id)}"]`);
        if (!checks.length) continue;
        const nextTools = checks.filter((c) => c.checked).map((c) => c.value);
        if (nextTools.join('\n') !== b.tools.join('\n')) return { binding: b, tools: nextTools };
      }
      return null;
    };
    const syncSaveBar = () => {
      const dirty = findDirtyBinding();
      if (!dirty) {
        saveBar.hidden = true;
        return;
      }
      saveBar.hidden = false;
      saveBtn.disabled = dirty.tools.length === 0;
      if (saveText)
        saveText.textContent = dirty.tools.length
          ? `${dirty.binding.alias} · 已选 ${dirty.tools.length} 项未保存`
          : `${dirty.binding.alias} · 至少保留 1 个工具`;
    };
    $$('[data-gw-binding-tool]').forEach((box) => {
      box.onchange = syncSaveBar;
    });
    saveBtn.onclick = () =>
      busy(saveBtn, async () => {
        const dirty = findDirtyBinding();
        if (!dirty || !dirty.tools.length) return;
        const acc = state.accounts.find((a) => a.id === dirty.binding.account_id);
        if (!acc?.catalog_hash) return;
        const ok = await uiConfirmAction(
          {
            title: `更新 ${dirty.binding.alias} 已发布工具？`,
            body: `将发布所选的 ${dirty.tools.length} 个工具，未勾选项从该命名空间撤下。`,
            confirmLabel: '保存并发布',
          },
          { owner: saveBtn },
        );
        if (!ok) return;
        await post('/bindings/' + encodeURIComponent(dirty.binding.id) + '/publish', {
          tools: dirty.tools,
          catalog_hash: acc.catalog_hash,
          confirmed: true,
          expected_version: dirty.binding.version,
        });
        if (S.page === 'mcp-gateway') await renderPage(false);
        toast('MCP 配置已保存');
      });
  }

  function bind() {
    bindPublishedTools();
    $$('[data-gw]').forEach((button) => {
      button.onclick = () => (button.dataset.gw === 'connector' ? newConnector() : newAccount());
    });
    $$('[data-gw-account]').forEach((button) => {
      button.onclick = () => rotateAccount(button.dataset.gwAccount);
    });
    $$('[data-gw-discover]').forEach((button) => {
      button.onclick = () => busy(button, () => discover(button.dataset.gwDiscover));
    });
    $$('[data-gw-role]').forEach((button) => {
      button.onclick = () => busy(button, () => configureRole(button.dataset.gwRole));
    });
    $$('[data-gw-consent]').forEach((button) => {
      button.onclick = () => busy(button, () => consent(button.dataset.gwConsent));
    });
    $$('[data-gw-toggle]').forEach((button) => {
      button.onclick = () =>
        form(
          button.dataset.enabled === '1' ? '暂停连接能力' : '恢复连接能力',
          '<p>此变更影响所有使用该配置的连接；不会自动重发、取消或回滚外部操作。</p>',
          () =>
            api(
              root + '/' + button.dataset.gwToggle + '/' + encodeURIComponent(button.dataset.id),
              {
                method: 'PATCH',
                body: JSON.stringify({
                  enabled: button.dataset.enabled !== '1',
                  expected_version: Number(button.dataset.version),
                }),
              },
            ),
        );
    });
  }
  return { html, bind, reset };
})();
