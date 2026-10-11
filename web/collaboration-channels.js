'use strict';
// Explicit owner controls. Opening/refreshing this view never sends or replays work.
window.CodePierCollaborationChannels = {
  status(value) {
    return (
      {
        active: '已启用',
        pairing: '等待微信配对',
        awaiting_owner: '等待核对联系人',
        paused: '已暂停',
        revoked: '已撤销',
        pending: '等待获取',
        ready: '附件可用',
        unavailable: '附件获取失败',
        expired: '附件已到保留期限',
        queued: '等待发送',
        preparing: '准备原投递',
        sending: '正在提交',
        accepted: '通道已接受（未证明送达）',
        owner_confirmed: '人工核对已收到',
        unknown: '发送结果未知，禁止自动重发',
        waiting_context: '等待原联系人上下文',
        waiting_media: '等待原附件',
        attention: '需要核对',
        needs_attention: '存在未完成分项',
        rejected: '分项未发送',
        cancelled: '已取消未发送部分',
      }[value] ||
      value ||
      '尚无证据'
    );
  },
  attachments(message, E) {
    const label = window.CodePierCollaborationChannels.status;
    const files = (message.attachments || [])
      .map((item) => {
        const valid = /^[a-f0-9]{32}$/.test(item.id || '');
        const href = valid
          ? '/api/collaboration/attachments/' +
            item.id +
            '/content?space_id=' +
            encodeURIComponent(S.space_id || 'legacy')
          : '';
        return `<div class="cc-channel-file" data-attachment-id="${E(item.id)}"><div><strong>${E(item.name || '附件')}</strong><small>${E(item.kind || 'file')} · ${E(label(item.state))}${Number.isFinite(item.size) ? ' · ' + E(String(item.size)) + ' 字节' : ''}</small>${item.reason_code ? `<small>${E(item.reason_code)}</small>` : ''}</div>${item.state === 'ready' && valid ? `<a class="btn" href="${E(href)}" target="_blank" rel="noopener noreferrer" download>读取原附件</a>` : '<span>原消息与顺序已保留</span>'}</div>`;
      })
      .join('');
    const deliveries = (message.channel_deliveries || [])
      .map(
        (delivery) =>
          `<details class="cc-channel-delivery" ${['needs_attention', 'unknown', 'attention'].includes(delivery.state) ? 'open' : ''}><summary>微信：${E(label(delivery.state))}</summary><p>正文与每件附件分别记录。通道接受不等于用户已读；未知投递不会自动重发。</p>${(delivery.parts || []).map((part) => `<div class="cc-channel-part">${E(String(part.ordinal + 1))}. ${part.kind === 'text' ? '正文' : '附件'} · ${E(label(part.state))}${part.reason_code ? ' · ' + E(part.reason_code) : ''}</div>`).join('')}</details>`,
      )
      .join('');
    return files + deliveries;
  },
  create(ctx) {
    const { state, E, field, button, mentionSlots, showDrawer, mutation } = ctx;
    let loaded = null,
      controller = null;
    const local = { local: true };
    const allowed = () => !!state.snapshot?.can_manage;
    const scope = () => ({
      project: state.project,
      environment_id: state.environment,
      conversation_id:
        state.conversation || state.snapshot?.conversation?.id || state.snapshot?.room?.id || '',
    });
    const capture = () => ({
      generation: state.generation,
      session: S.session,
      space: S.space_id,
      ...scope(),
    });
    const current = (key) =>
      key &&
      key.generation === state.generation &&
      key.session === S.session &&
      key.space === S.space_id &&
      key.project === state.project &&
      key.environment_id === state.environment &&
      key.conversation_id === scope().conversation_id &&
      allowed();
    const select = (name, values) =>
      `<select name="${name}" required><option value="">请选择</option>${values.map(([id, title]) => `<option value="${E(id)}">${E(title)}</option>`).join('')}</select>`;
    const checkbox = (name, label, required = false) =>
      `<label class="cc-channel-check"><input type="checkbox" name="${name}" ${required ? 'required' : ''}>${E(label)}</label>`;
    const listState = (value) => E(window.CodePierCollaborationChannels.status(value));
    const stamp = (value) =>
      Number.isFinite(value)
        ? E(new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }))
        : '尚未记录';
    function card() {
      return allowed() && state.snapshot?.capabilities?.room_scope_v1
        ? `<section class="cc-card cc-channel-settings"><h3>通道与执行边界</h3><p>在此管理当前房间的微信连接、文件外发和凭据范围。不会创建新任务或改变原连接凭据。</p>${button('dot-channel-open', '管理通道与范围')}</section>`
        : '';
    }
    function render(data) {
      const w = data.weixin,
        r = data.scopes;
      const slots = mentionSlots().filter((s) => s.duplex && s.state === 'registered');
      const projects = [
        ...new Map(
          (
            state.snapshot.conversation?.projects || [
              {
                project_id: state.snapshot.room.project_id || state.project,
                project_label: state.project,
                environment_id: state.environment,
              },
            ]
          )
            .filter((p) => p.environment_id === state.environment)
            .map((p) => [p.project_id, p]),
        ).values(),
      ];
      const bindings = (w.bindings || [])
        .map(
          (binding) =>
            `<section class="cc-channel-binding" data-binding-id="${E(binding.id)}"><h4>${listState(binding.state)}</h4><p>原绑定 ${E(binding.id.slice(-8))} · 世代 ${E(binding.generation)} · 有效至 ${stamp(binding.expires_at)}</p>${binding.peer_id ? `<p>原联系人：<code>${E(binding.peer_id)}</code></p>` : ''}${binding.state === 'pairing' ? `<p>在要绑定的微信私聊发送以下配对码，再回到此处核对状态。</p><textarea readonly rows="2" aria-label="微信配对码">${E(binding.pairing_challenge || '原配对码不可用或已过期；撤销后重新绑定')}</textarea><small>配对期限：${stamp(binding.pairing_until)}。配对不会授予代码执行权限。</small>` : ''}${binding.state === 'awaiting_owner' ? `<p>实际返回的联系人编号：<code>${E(binding.pending_peer_id)}</code></p><div data-channel-form="activate">${checkbox('confirm_peer', '我已核对该联系人和当前房间', true)}${button('dot-channel-activate', '确认激活此联系人', binding)}</div>` : ''}<div class="cc-actions">${binding.state === 'active' ? button('dot-channel-pause', '暂停此通道', binding) : binding.state === 'paused' ? button('dot-channel-resume', '恢复原绑定', binding) : ''}${binding.state !== 'revoked' ? button('dot-channel-revoke', '撤销原绑定', binding) : ''}</div></section>`,
        )
        .join('');
      const policies = (r.items || [])
        .map(
          (item) =>
            `<div class="cc-channel-binding"><strong>连接 ${E(item.grant_id.slice(-8))} · ${listState(item.state)}</strong><p>版本 ${E(item.version)} · 到期 ${stamp(item.expires_at)} · ${item.manifest?.execution_mode === 'file_tools_only' ? '仅文件工具' : '账户权限执行（不是 OS 沙箱）'}</p><p>${(item.manifest?.project_rules || []).map((p) => E(p.project_id) + '：' + E(p.capabilities.join(' / '))).join('<br>')}</p>${item.state === 'active' ? button('dot-channel-scope-revoke', '停用此房间连接', { id: item.grant_id }) : ''}</div>`,
        )
        .join('');
      const deliveries = (w.deliveries || [])
        .map(
          (d) =>
            `<details class="cc-channel-delivery" data-delivery-id="${E(d.id)}"><summary>${listState(d.state)} · ${E(d.id.slice(-8))}</summary>${(d.parts || []).map((p) => `<p>${E(String(p.ordinal + 1))}. ${p.kind === 'text' ? '正文' : '附件'}：${listState(p.state)} ${E(p.reason_code || '')}</p>`).join('')}<div data-channel-form="delivery">${(d.parts || []).some((p) => p.state === 'unknown') ? field('人工核对依据（不是平台回执）', '<input name="confirmation_note" maxlength="1000" required autocomplete="off">') + button('dot-channel-confirm-received', '登记人工已收到', d) : ''}${button('dot-channel-cancel-unsent', '取消尚未发送部分', d)}</div></details>`,
        )
        .join('');
      return `<div class="cc-channel-pane"><p class="cc-hint">当前项目：${E(state.project)} · ${E(state.environment)}。刷新仅查询证据，不重发消息、不领取任务。</p>${button('dot-channel-open', '重新核对状态')}
      <section><h3>微信私聊通道</h3><p>${w.enabled ? '功能已开启；真实收发仍以每条回执为准。' : '功能默认关闭。部署负责人开启 CODEPIER_WEIXIN_ENABLED 后，才能登记已有授权机器人。'}</p><p>仅支持机器人私聊，不冒充原生群聊。外部文字不能自动变成房主的执行授权。</p>${bindings || '<p>当前房间尚未绑定微信。</p>'}
      <details><summary>登记已有授权机器人</summary><div data-channel-form="account" class="cc-form">${field('名称', '<input name="label" maxlength="80" required autocomplete="off">')}${field('机器人编号', '<input name="bot_id" maxlength="256" required autocomplete="off">')}${field('已有授权令牌', '<input type="password" name="token" maxlength="16384" required autocomplete="new-password">')}${checkbox('confirm_credentials', '我确认保存此已授权凭据，并已核对服务条款与使用资格', true)}${button('dot-channel-import', '确认登记凭据', {}, w.enabled ? '' : 'disabled')}<small>凭据加密保存；不创建 MCP 凭据，也不会显示到消息中。</small></div></details>
      <details><summary>绑定当前话题与 dot</summary><div data-channel-form="bind" class="cc-form">${field(
        '已登记机器人',
        select(
          'account_id',
          (w.accounts || [])
            .filter((a) => a.state === 'active')
            .map((a) => [a.id, a.label + ' · ' + a.bot_id]),
        ),
      )}${field(
        '此话题的 dot',
        select(
          'dot_id',
          slots.map((s) => [s.id, s.label]),
        ),
      )}${checkbox('allow_replies', '允许将该 dot 对原微信消息的最终回复发送给原联系人')}${checkbox('allow_media', '允许文件外发方向；每份文件仍需单独确认')}${checkbox('confirm_ingress', '我确认将此微信私聊接入当前话题，并会核对实际联系人', true)}${button('dot-channel-bind', '生成配对码', {}, w.enabled ? '' : 'disabled')}</div></details>
      <details><summary>将已有产物附加到原回复</summary><div data-channel-form="files" class="cc-form">${field(
        '最近的原 dot 最终回复',
        select(
          'message_id',
          (w.reply_candidates || []).map((m) => [m.id, m.body_text || m.id]),
        ),
      )}<fieldset><legend>最近的已生成产物（最多16件）</legend>${(w.artifact_candidates || []).map((a) => `<label class="cc-channel-check"><input type="checkbox" name="artifact_ids" value="${E(a.artifact_id)}">${E(a.name)} · ${E(a.bytes)} 字节</label>`).join('') || '<p>当前项目没有可选产物。</p>'}</fieldset>${w.artifact_candidates_more ? '<p>此处仅列最近50件；更早产物请在产物管理中核对编号。</p>' : ''}${checkbox('confirm_files', '我确认仅向这条原微信消息的联系人分享所选文件', true)}${button('dot-channel-files', '确认保存文件外发清单', {}, w.enabled ? '' : 'disabled')}<small>只传输已有产物，不重跑原生成任务；正文不会重复发送。</small></div></details>
      <h4>最近投递分项</h4>${deliveries || '<p>暂无外发记录。</p>'}</section>
      <section><h3>房间凭据范围</h3><p>范围绑定服务器上的原 grant，省略 room_id 也不能绕过。启用或修改会影响所有使用同一凭据的会话；工作目录不是操作系统沙箱。</p>${policies || '<p>尚未启用普通工具的房间限制。</p>'}<details><summary>明确设置连接范围</summary><div data-channel-form="scope" class="cc-form">${field(
        '当前房间已有连接',
        select(
          'grant_id',
          (r.eligible_connections || []).map((g) => [
            g.id,
            g.label +
              ' · ' +
              g.id.slice(-8) +
              (g.scope_version ? '（已有范围，修改会替换原范围）' : ''),
          ]),
        ),
      )}<fieldset><legend>当前环境的项目与逐项目能力</legend>${projects.map((p) => `<div class="cc-channel-project" data-project-id="${E(p.project_id)}"><label><input type="checkbox" name="project_selected" ${p.project_id === (state.snapshot.room.project_id || state.project) ? 'checked disabled' : ''}>${E(p.project_label || p.project_id)}</label>${checkbox('write', '允许项目文件写入')}${checkbox('execute', '允许此项目的 Agent 执行')}</div>`).join('')}</fieldset>${field('有效小时数', '<input type="number" name="hours" value="24" min="1" max="720" step="1" required>')}${checkbox('unsandboxed', '如选择执行，我明确接受 Agent 账户权限；这不是文件系统或网络沙箱')}${checkbox('confirm_scope', '我确认以本次选择替换此凭据的房间范围，且影响它的所有会话', true)}${button('dot-channel-scope-save', '确认设置范围')}<p>严格 OS 隔离尚未验证时不会放行执行。未选择执行时只允许获批文件工具。</p></div></details></section></div>`;
    }
    async function open(element) {
      if (!allowed()) throw new Error('当前身份不能管理房间连接。');
      controller?.abort();
      controller = new AbortController();
      const key = capture();
      showDrawer('通道与执行边界', '<p role="status">正在读取原连接与投递状态…</p>', element);
      const epoch = state.drawerEpoch;
      const query = new URLSearchParams(scope());
      let result;
      try {
        result = await Promise.all([
          api(
            '/api/collaboration/room-scopes?' +
              new URLSearchParams({ project: key.project, environment_id: key.environment_id }),
            { signal: controller.signal },
          ),
          api('/api/collaboration/weixin?' + query, { signal: controller.signal }),
        ]);
      } catch (error) {
        if (!current(key) || epoch !== state.drawerEpoch || error.name === 'AbortError')
          return local;
        throw error;
      }
      if (!current(key) || epoch !== state.drawerEpoch) return local;
      const [scopes, weixin] = result;
      loaded = { key, scopes, weixin };
      showDrawer('通道与执行边界', render(loaded), element);
      loaded.epoch = state.drawerEpoch;
      return local;
    }
    function group(element, validate = true) {
      const pane = document.querySelector('.cc-channel-pane');
      if (
        !loaded ||
        !current(loaded.key) ||
        loaded.epoch !== state.drawerEpoch ||
        !pane?.contains(element)
      )
        throw new Error('房间或登录状态已变化，请重新读取当前连接。');
      const root = element.closest('[data-channel-form]');
      if (
        validate &&
        root &&
        [...root.querySelectorAll('input,select,textarea')].some((input) => !input.reportValidity())
      )
        return null;
      const value = (name) => root?.querySelector('[name="' + name + '"]')?.value || '';
      const checked = (name) => root?.querySelector('[name="' + name + '"]')?.checked === true;
      return { root, value, checked };
    }
    async function act(action, element) {
      if (action === 'dot-channel-open') return open(element);
      const data = group(element, action !== 'dot-channel-cancel-unsent');
      if (!data) return local;
      const { root, value, checked } = data,
        key = loaded.key;
      let operation, args;
      if (action === 'dot-channel-import') {
        operation = 'weixin-account';
        const existing = loaded.weixin.accounts.find((a) => a.bot_id === value('bot_id').trim());
        if (
          existing &&
          !window.confirm('替换这个机器人已有凭据？旧绑定将撤销，未确认投递不会自动迁移。')
        )
          return local;
        args = {
          label: value('label').trim(),
          bot_id: value('bot_id').trim(),
          token: value('token'),
          expected_version: existing?.credential_version || 0,
          confirm_credential_storage: checked('confirm_credentials'),
          confirm_provider_terms: checked('confirm_credentials'),
        };
      } else if (action === 'dot-channel-bind') {
        operation = 'weixin-bind';
        args = {
          account_id: value('account_id'),
          dot_id: value('dot_id'),
          confirm_external_data_ingress: checked('confirm_ingress'),
          allow_replies: checked('allow_replies'),
          allow_media_outbound: checked('allow_media'),
        };
      } else if (
        [
          'dot-channel-activate',
          'dot-channel-pause',
          'dot-channel-resume',
          'dot-channel-revoke',
        ].includes(action)
      ) {
        const row = loaded.weixin.bindings.find((b) => b.id === element.dataset.id);
        if (!row) throw new Error('原绑定已不在当前列表。');
        if (
          action === 'dot-channel-revoke' &&
          !window.confirm('撤销原微信绑定并停止新转发？未知投递仍保留，不会重发到新账号。')
        )
          return local;
        operation = 'weixin-control';
        args = {
          binding_id: row.id,
          expected_version: row.version,
          action: action.slice('dot-channel-'.length),
          confirm_peer_id:
            action === 'dot-channel-activate' && checked('confirm_peer') ? row.pending_peer_id : '',
        };
      } else if (action === 'dot-channel-scope-save') {
        const grant = loaded.scopes.eligible_connections.find((g) => g.id === value('grant_id'));
        if (!grant) throw new Error('请选择当前已有连接。');
        const rules = [...root.querySelectorAll('[data-project-id]')]
          .filter((p) => p.querySelector('[name="project_selected"]').checked)
          .map((p) => ({
            project_id: p.dataset.projectId,
            capabilities: [
              'read',
              ...(p.querySelector('[name="write"]').checked ? ['write'] : []),
              ...(p.querySelector('[name="execute"]').checked ? ['execute'] : []),
            ],
            execution_targets: ['agent'],
          }));
        const execute = rules.some((r) => r.capabilities.includes('execute'));
        if (execute && !checked('unsandboxed'))
          throw new Error('选择执行需要明确接受账户权限边界；未确认时仅使用文件工具。');
        operation = 'room-scope';
        args = {
          grant_id: grant.id,
          expected_version: grant.scope_version || 0,
          project_rules: rules,
          execution_mode: execute ? 'account_permissions_acknowledged' : 'file_tools_only',
          acknowledge_unsandboxed_exec: checked('unsandboxed'),
          expires_seconds: Number(value('hours')) * 3600,
          confirm_connection_wide_ceiling: checked('confirm_scope'),
        };
      } else if (action === 'dot-channel-scope-revoke') {
        const row = loaded.scopes.items.find((r) => r.grant_id === element.dataset.id);
        if (!row || !window.confirm('停用此房间连接？它将被拒绝访问，不会退回更宽的原授权。'))
          return local;
        operation = 'room-scope-control';
        args = { grant_id: row.grant_id, expected_version: row.version, action: 'revoke' };
      } else if (action === 'dot-channel-files') {
        const reply = loaded.weixin.reply_candidates.find((m) => m.id === value('message_id'));
        const ids = [...root.querySelectorAll('[name="artifact_ids"]:checked')].map(
          (input) => input.value,
        );
        if (!reply || !ids.length || ids.length > 16)
          throw new Error('请选择原回复与1至16件实际产物。');
        operation = 'weixin-files';
        args = {
          message_id: reply.id,
          expected_message_version: reply.version,
          artifact_ids: ids,
          confirm_file_egress: checked('confirm_files'),
        };
      } else if (['dot-channel-confirm-received', 'dot-channel-cancel-unsent'].includes(action)) {
        const row = loaded.weixin.deliveries.find((d) => d.id === element.dataset.id);
        if (!row) throw new Error('原投递已不在当前列表。');
        operation = 'weixin-delivery-control';
        args = {
          delivery_id: row.id,
          expected_version: row.version,
          action: action === 'dot-channel-confirm-received' ? 'confirm_received' : 'cancel_unsent',
          confirmation_note: value('confirmation_note'),
        };
      } else return local;
      await mutation(operation, { ...args, ...scope() });
      if (!current(key)) return local;
      if (root?.querySelector('[name="token"]')) root.querySelector('[name="token"]').value = '';
      await open(document.querySelector('[data-cc-action="dot-channel-open"]'));
      return { local: true, message: '原配置或投递清单已保存；状态以新读取的证据为准。' };
    }
    function clear() {
      controller?.abort();
      controller = null;
      loaded = null;
      for (const input of document.querySelectorAll('.cc-channel-pane input[type="password"]'))
        input.value = '';
    }
    return { card, act, clear };
  },
};
