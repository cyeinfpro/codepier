'use strict';
// Shared project records only. No private chat history, tokens or callback URLs.
window.CodePierCollaboration = (() => {
  const state = {
    session: null,
    space: null,
    project: '',
    conversation: '',
    viewportCleanup: null,
    environment: 'production',
    view: 'discussion',
    snapshot: null,
    grants: [],
    draft: {},
    joinDraft: {},
    expiryTimer: null,
    notes: '',
    error: false,
    timer: null,
    controller: null,
    generation: 0,
    busy: false,
    busyCleanup: null,
    refreshSequence: 0,
    requests: new Map(),
    chats: new Map(),
    rooms: [],
    members: [],
    delegationPolicies: [],
    drawerFocus: null,
    drawerEpoch: 0,
    composing: false,
  };
  const labels = {
    queued: '已排队',
    leased: '已领取',
    running: '正在处理',
    succeeded: '结果已提交',
    blocked: '等待处理',
    retry_wait: '等待重试',
    cancelled: '已取消',
    expired: '已到期',
    dead_letter: '投递或重试失败',
    failed: '失败',
    active: '已启用',
    paused: '已暂停',
    awaiting_approval: '待批准',
    rejected: '未批准',
    awaiting_decision: '待验收',
    verified: '已验收',
    manual_claim_required: '待人工领取',
    no_valid_subscription: '没有匹配的有效订阅',
    event_missing: '派发事件缺失，需要核对',
    event_queued: '等待投递',
    event_accepted: '事件已接收，等待领取',
    awaiting_consumer: '已接收但尚未领取',
    accepted: '事件已接收',
    pending: '待投递',
    unsubscribed: '已退订',
    revoked: '授权已撤销',
    open: '异常未恢复',
    observing: '恢复观察中',
    resolved: '探针已验证恢复',
    breaching: '探针持续越界',
    stale: '采集数据过期',
    insufficient_data: '样本不足',
    healthy: '探针当前正常',
    policy_denied: '探针被策略阻止',
    collector_disabled: '采集开关未启用',
    awaiting_samples: '等待新鲜样本',
    not_started: '尚未启动',
    disabled: '已关闭',
    degraded: '链路异常',
    enabled: '用途绑定有效',
    disabled_or_expired: '绑定停用或到期',
    authorization_unavailable: '授权不可用',
    budget_exhausted: '分析预算已用尽',
    not_requested: '未创建分析',
    cooldown: '复发冷却中',
    action_required: '需要后续处理',
    inconclusive: '证据不足',
    explained: '已给出解释',
    invited: '待在目标聊天加入',
    registered: '已登记现有连接',
    code_expired: '加入码已过期',
    waiting_subscription: '等待宿主订阅',
    subscription_verified: '订阅回调已验证',
    test_delivered: '测试已获接收回执',
    chat_confirmed: '用户已确认聊天收件',
    room_paused: '协作室已暂停',
    partial_subscription: '订阅尚未完整',
    partial_confirmed: '部分订阅已确认收件',
    slot_revoked: '位置已撤销',
  };
  const E = (value) => CP.ui.esc(value == null ? '' : String(value));
  const text = (value) => labels[value] || value || '暂无记录';
  const when = (value) =>
    value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '暂无';
  const scope = () => ({ project: state.project, environment_id: state.environment });
  const chatScope = () => ({
    ...scope(),
    ...(state.conversation ? { conversation_id: state.conversation } : {}),
  });
  const projectLabel = (id) => S.projects.find((p) => p.id === id)?.alias || id;
  const roomProjects = () =>
    state.snapshot?.conversation?.projects || [
      {
        project_id: state.project,
        environment_id: state.environment,
        room_id: state.snapshot?.room?.id,
        project_label: projectLabel(state.project),
      },
    ];
  function conversationTitle(room) {
    const first = room?.projects?.[0];
    if (first && room.title === first.project_id + ' · ' + first.environment_id)
      return first.project_label || projectLabel(first.project_id);
    return (
      room?.title || (first ? first.project_label || projectLabel(first.project_id) : '聊天室')
    );
  }
  function persistSelection() {
    try {
      sessionStorage.setItem(
        'codepier-collaboration-selection',
        JSON.stringify({
          space: S.space_id,
          project: state.project,
          environment: state.environment,
          conversation: state.conversation,
        }),
      );
    } catch {}
  }
  function projectChips() {
    return `<div class="cc-project-strip" aria-label="房间项目">${roomProjects()
      .map(
        (p) =>
          `<button type="button" class="cc-project-chip ${p.project_id === state.project && p.environment_id === state.environment ? 'is-selected' : ''}" data-cc-partition="${E(p.project_id)}" data-environment="${E(p.environment_id)}" aria-pressed="${p.project_id === state.project && p.environment_id === state.environment}">${E(p.project_label || projectLabel(p.project_id))}<small>${E(p.environment_id)}</small></button>`,
      )
      .join(
        '',
      )}${state.snapshot?.can_manage && state.snapshot?.conversation ? button('add-project', '＋ 添加项目') : ''}</div>`;
  }
  function disableComposer() {
    const area = document.querySelector('#cc-message-input');
    const send = document.querySelector('#cc-command button[type="submit"]');
    if (area) area.disabled = true;
    if (send) send.disabled = true;
  }
  async function selectPartition(project, environment) {
    const conversation = state.conversation;
    rememberChat();
    disableComposer();
    chat().mentions = [];
    chat().reply = '';
    resetScope();
    state.project = project;
    state.environment = environment;
    persistSelection();
    await renderPage(false);
    return (
      state.conversation === conversation &&
      state.project === project &&
      state.environment === environment &&
      S.page === 'collaboration'
    );
  }
  function requestId() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
    return [
      hex.slice(0, 8),
      hex.slice(8, 12),
      hex.slice(12, 16),
      hex.slice(16, 20),
      hex.slice(20),
    ].join('-');
  }
  const options = (items, selected, title = '请选择') =>
    `<option value="">${E(title)}</option>${items.map(([id, label]) => `<option value="${E(id)}" ${id === selected ? 'selected' : ''}>${E(label)}</option>`).join('')}`;
  const agentOptions = (kind, selected, title) =>
    options(
      (state.snapshot?.agents || [])
        .filter((a) => !kind || a.kind === kind)
        .map((a) => [
          a.id,
          `${a.label} · ${a.kind === 'dot' ? 'dot' : 'Work'} · ${a.id.slice(-6)}`,
        ]),
      selected,
      title,
    );
  const badge = (value) => `<span class="cc-status">${E(text(value))}</span>`;
  const empty = (message) => `<p class="cc-empty">${E(message)}</p>`;
  const button = (action, label, row = {}, extra = '') =>
    `<button type="button" class="btn" data-cc-action="${E(action)}" data-id="${E(row.id)}" data-version="${E(row.version)}" ${extra}>${E(label)}</button>`;
  const field = (label, control) =>
    `<label class="cc-field"><span>${E(label)}</span>${control}</label>`;
  const json = (value) => `<pre class="cc-json">${E(JSON.stringify(value, null, 2))}</pre>`;

  const coordination = window.CodePierCollaborationGoals.create({
    state,
    E,
    field,
    button,
    empty,
    getRecord,
    mutation,
    showDrawer,
    closeDrawer,
    projectLabel,
    roomProjects,
    when,
  });

  function detach() {
    rememberChat();
    state.busyCleanup?.();
    state.busyCleanup = null;
    state.refreshSequence++;
    // renderPage(false) keeps the old DOM while fetching its replacement.
    // That detached view must not accept actions that a pending render can erase.
    const root = document.querySelector('.collaboration');
    if (root) {
      root.inert = true;
      root.setAttribute('aria-busy', 'true');
      // A modal dialog escapes ancestor inertness unless marked itself.
      const dialog = root.querySelector('#cc-drawer');
      if (dialog) dialog.inert = true;
    }
    state.viewportCleanup?.();
    state.viewportCleanup = null;
    state.composing = false;
    state.generation++;
    state.busy = false;
    clearTimeout(state.timer);
    clearTimeout(state.expiryTimer);
    state.timer = null;
    state.expiryTimer = null;
    state.controller?.abort();
    state.controller = null;
  }
  function resetScope() {
    coordination.clear();
    state.snapshot = null;
    state.delegationPolicies = [];
    state.draft = {};
    state.joinDraft = {};
    state.notes = '';
    state.error = false;
  }
  function clear() {
    detach();
    resetScope();
    state.requests.clear();
    state.chats.clear();
    state.rooms = [];
    state.members = [];
    state.drawerFocus = null;
    state.conversation = '';
    try {
      sessionStorage.removeItem('codepier-collaboration-selection');
      for (const key of Object.keys(sessionStorage))
        if (key.startsWith('codepier-collaboration-request:')) sessionStorage.removeItem(key);
    } catch {}
    state.grants = [];
    state.session = null;
    state.space = null;
    state.busy = false;
  }
  async function getRecord(kind, id, cursor = '', extra = {}) {
    const query = new URLSearchParams({ ...chatScope(), kind, id, cursor, ...extra });
    return api('/api/collaboration?' + query, { signal: state.controller?.signal });
  }
  async function mutation(operation, args) {
    const payload = {
      ...([
        'message-to-task',
        'message-access',
        'read-cursor',
        'message-remind',
        'delegation-policy',
        'delegation-policy-control',
      ].includes(operation)
        ? chatScope()
        : scope()),
      ...args,
    };
    const fingerprint = JSON.stringify([state.space, operation, payload]);
    let id = state.requests.get(fingerprint);
    if (!id) {
      id = requestId();
      state.requests.set(fingerprint, id);
      if (state.requests.size > 100) state.requests.delete(state.requests.keys().next().value);
    }
    const result = await api('/api/collaboration/' + operation, {
      method: 'POST',
      body: JSON.stringify(
        operation === 'plan-validate' ? payload : { ...payload, idempotency_key: id },
      ),
      retrySafe: true,
    });
    // A confirmed response closes this explicit reminder attempt. Unknown
    // outcomes retain the key, while a later click after setup is a new intent.
    if (['message-remind', 'delegation-remind'].includes(operation))
      state.requests.delete(fingerprint);
    return result;
  }
  async function commandIdentity(payload) {
    // Persist only random request IDs, never draft prose or credentials. The
    // digest binds retries to this login and exact command across a page reload.
    const session = S.session,
      space = S.space_id;
    const source = JSON.stringify([session?.csrf, space, payload]);
    const tag = crypto.subtle
      ? Array.from(
          new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(source))),
          (byte) => byte.toString(16).padStart(2, '0'),
        ).join('')
      : window.sha256(source);
    if (S.session !== session || S.space_id !== space)
      throw new Error('登录或项目范围已改变；未发送旧请求。');
    const storageKey = 'codepier-collaboration-request:' + tag;
    let identifier = state.requests.get(storageKey);
    try {
      identifier ||= sessionStorage.getItem(storageKey);
    } catch {}
    if (!identifier || !/^[a-f0-9-]{36}$/.test(identifier)) identifier = requestId();
    state.requests.set(storageKey, identifier);
    try {
      const keys = Object.keys(sessionStorage).filter((key) =>
        key.startsWith('codepier-collaboration-request:'),
      );
      for (const key of keys.slice(0, Math.max(0, keys.length - 99)))
        sessionStorage.removeItem(key);
      sessionStorage.setItem(storageKey, identifier);
    } catch {}
    return { identifier, storageKey };
  }

  // Chat state is memory-only and scoped to this authenticated session and room.
  // Polling patches only the timeline: it never replaces an active composer.
  function chat() {
    const key = JSON.stringify([
      state.space,
      state.conversation || [state.project, state.environment],
    ]);
    if (!state.chats.has(key))
      state.chats.set(key, {
        messages: [],
        after: '',
        older: '',
        draft: '',
        mentions: [],
        delegationPolicy: '',
        delegationVersion: 0,
        delegationAutomatic: false,
        delegationTargets: [],
        delegationCapabilities: [],
        acceptance: '完成请求，并报告实际检查、结果和限制',
        reply: '',
        unread: 0,
        scroll: null,
        selection: null,
        focused: false,
        initialized: false,
      });
    return state.chats.get(key);
  }
  function rememberChat() {
    const root = document.querySelector('.collaboration');
    if (
      !root ||
      root.dataset.project !== state.project ||
      root.dataset.environment !== state.environment
    )
      return;
    const area = root.querySelector('#cc-message-input');
    const feed = root.querySelector('.cc-feed');
    if (area) {
      const c = chat();
      c.draft = area.value;
      c.selection = [area.selectionStart, area.selectionEnd];
      c.focused = document.activeElement === area;
    }
    if (feed) chat().scroll = feed.scrollTop;
  }
  const messageText = (m) =>
    m.body_text ?? m.body?.text ?? m.body?.command?.request ?? m.body?.summary ?? '';
  const eligibleWorkers = () =>
    (state.snapshot?.agents || []).filter((a) => a.binding_status === 'enabled');
  const messageById = (id) => chat().messages.find((m) => m.id === id);
  const notificationSlots = () => (state.snapshot?.join_slots || []).filter(liveJoin);
  const slotMember = (id) => state.members.find((m) => m.slot_id === id || m.id === id);
  const mentionSlots = () =>
    (state.snapshot?.join_slots || []).map((slot) => slotMember(slot.id) || slot);
  function mentionReadiness(slot) {
    const member = slotMember(slot.id);
    if (state.snapshot?.room?.state !== 'active') return '房间已暂停，提醒不可投递';
    if (!liveJoin(slot)) return '位置已到期或撤销，需要重新接通';
    if (slot.state !== 'registered') return '尚未加入，先完成聊天连接';
    if (slot.status === 'authorization_unavailable') return '原连接授权不可用';
    if (!state.snapshot?.capabilities?.message_notifications) return '消息提醒服务未启用';
    if (!member) return '尚未核对房间提醒订阅';
    if (member.message_notification_state !== 'active') return '尚未订阅此房间的 @ 提醒';
    return member.can_speak ? '房间提醒可投递 · 已允许回帖' : '房间提醒可投递 · 尚未允许回帖';
  }
  const deliveryLabels = {
    queued: '等待投递',
    pending: '等待投递',
    delivering: '正在投递',
    accepted: '宿主已受理，尚无已读或处理证明',
    not_subscribed: '未送达：尚未订阅本房间提醒',
    recipient_unavailable: '未送达：连接不可用、到期或已撤销',
    notifications_disabled: '未送达：消息提醒服务未启用',
    rate_limited: '未送达：提醒发送频率受限',
    failed: '提醒投递失败',
    dead_letter: '提醒投递失败',
    suppressed_agent_reply: '助手回复未触发重复提醒',
    unknown: '投递状态尚未核实',
  };
  function messageDeliveries(m) {
    if (m.body?.delegation) return '';
    const mentions = m.mentions || m.body?.mentions || [];
    if (!mentions.length) return '';
    const receipts = m.body?.notifications || [];
    return (
      '<ul class="cc-message-deliveries" aria-label="消息提醒状态">' +
      mentions
        .map((mention) => {
          const receipt = receipts.find((item) => item.slot_id === mention.slot_id);
          const status = receipt?.state || 'unknown';
          const repair = ![
            'queued',
            'pending',
            'delivering',
            'accepted',
            'suppressed_agent_reply',
          ].includes(status);
          return (
            '<li data-delivery-slot="' +
            E(mention.slot_id) +
            '" data-delivery-state="' +
            E(status) +
            '">' +
            '<span>@' +
            E(mention.display_snapshot || mention.label || '通知位置') +
            ' · ' +
            E(deliveryLabels[status] || deliveryLabels.unknown) +
            '</span>' +
            (repair ? button('mention-connect', '核对接通方式', { id: mention.slot_id }) : '') +
            (repair &&
            state.snapshot?.capabilities?.message_remind &&
            ['owner', 'panel_owner'].includes(m.author_kind) &&
            !m.body?.delegation
              ? button(
                  'message-remind',
                  '重试这条提醒',
                  { id: mention.slot_id },
                  'data-message-id="' + E(m.id) + '"',
                )
              : '') +
            '</li>'
          );
        })
        .join('') +
      '</ul>'
    );
  }
  function patchMemberStatus() {
    for (const node of document.querySelectorAll('[data-cc-mention-status]')) {
      const slot = mentionSlots().find((item) => item.id === node.dataset.ccMentionStatus);
      node.textContent = slot ? mentionReadiness(slot) : '位置已不可用，请重新核对';
    }
    for (const node of document.querySelectorAll('[data-cc-speaking-status]')) {
      const member = slotMember(node.dataset.ccSpeakingStatus);
      node.textContent = member?.can_speak
        ? '这个连接已获准在本房间发言'
        : '这个连接尚未获准在本房间发言';
    }
    for (const node of document.querySelectorAll(
      '.cc-speaking [data-cc-action="message-access"]',
    )) {
      const card = node.closest('[data-speaking-slot]');
      const member = slotMember(card.dataset.speakingSlot);
      if (!member) continue;
      node.dataset.enabled = String(!member.can_speak);
      node.dataset.speakingVersion = String(member.speaking_version || 0);
      node.textContent = member.can_speak ? '撤回这个连接的发言许可' : '审阅并允许这个连接发言';
    }
  }
  async function refreshDeliveries(c, current) {
    const pending = c.messages.filter(
      (m) =>
        (m.body?.notifications || []).some((receipt) =>
          ['queued', 'pending', 'delivering'].includes(receipt.state),
        ) || delegation.pendingDelivery(m),
    );
    if (!pending.length) return;
    const start = (c.deliveryOffset || 0) % pending.length;
    const selected = [...pending.slice(start), ...pending.slice(0, start)].slice(0, 8);
    c.deliveryOffset = start + selected.length;
    const records = await Promise.all(
      selected.map((m) =>
        getRecord('message_status', m.id, '', {
          project: m.project_id || state.project,
          environment_id: m.environment_id || state.environment,
          room_id: m.source_room_id || m.room_id,
        }),
      ),
    );
    if (current())
      mergeMessages(records.flatMap((record) => (record.message ? [record.message] : [])));
  }
  function messageMarkup(m) {
    const body = m.body || {};
    const author =
      ['owner', 'panel_owner'].includes(m.author_kind) || m.kind === 'owner_command'
        ? '你'
        : '助手连接';
    const root = m.thread_root_id || m.reply_to_id;
    const job = (state.snapshot?.jobs || []).find(
      (j) => j.id === (m.job_id || body.job_id) || j.context?.origin_message_id === m.id,
    );
    const resultId = m.result_id || body.result_id;
    const isResult =
      ['agent_result', 'result_reference', 'delegation_result'].includes(m.kind) || resultId;
    const mentions = m.mentions || body.mentions || [];
    const source =
      root && root !== m.id
        ? button('source', '↗ 查看来源消息', { id: root }, 'class="cc-link"')
        : '';
    return `<article class="cc-message" data-message-id="${E(m.id)}" data-project="${E(m.project_id || state.project)}" data-environment="${E(m.environment_id || state.environment)}" data-source-room="${E(m.source_room_id || m.room_id)}" data-sequence="${E(m.server_sequence || 0)}"><div class="cc-avatar ${['owner', 'panel_owner'].includes(m.author_kind) || m.kind === 'owner_command' ? 'is-human' : ''}" aria-hidden="true">${E(author.slice(0, 1))}</div><div class="cc-message-content"><div class="cc-message-meta"><strong>${E(author)}</strong><small>${['owner', 'panel_owner'].includes(m.author_kind) || m.kind === 'owner_command' ? '房主' : '通过已授权连接'}</small><time>${E(when(m.created || m.created_at))}</time><span class="cc-message-project">${E(projectLabel(m.project_id || state.project))}</span></div>${source}${mentions.length ? `<div class="cc-message-mentions">${mentions.map((v) => `<span>@${E(v.display_snapshot || v.label || (state.snapshot.join_slots || []).find((s) => s.id === v.slot_id)?.label || '通知位置')}</span>`).join('')}</div>` : ''}<p class="cc-prose">${E(messageText(m))}</p>${messageDeliveries(m)}${delegation.messageCard(m)}${(job || isResult) && m.kind !== 'delegation_result' ? `<section class="cc-linked-card"><div class="cc-row"><strong>${isResult ? '分析结果' : '只读任务'}</strong>${badge(job?.state || m.state)}</div><p>${E(isResult ? body.summary || messageText(m) : job?.kind === 'summarize_result' ? '汇总结论' : job?.kind === 'propose_monitor_plan' ? '编制监控草稿' : '只读异常分析')}</p><small>讨论与任务分别记录 · 结果不代表业务已恢复</small><div class="cc-actions">${button('result', '查看结果与证据', { id: job?.id || m.job_id || body.job_id || resultId || m.id }, `data-record-kind="${job || m.job_id || body.job_id ? 'job' : resultId ? 'result' : 'message'}"`)}${source}</div></section>` : ''}<div class="cc-message-actions">${button('reply', '回复', m)}${state.snapshot.can_manage && !isResult && !body.delegation ? button('convert', '转为任务', m) : ''}${button('thread', '查看话题', { id: root || m.id })}${m.state === 'awaiting_approval' && state.snapshot.can_manage ? button('accept_proposal', '批准只读任务', m) + button('reject_proposal', '不批准', m) : ''}</div></div></article>`;
  }
  const delegation = window.CodePierCollaborationDelegation.create({
    state,
    E,
    field,
    button,
    options,
    when,
    chat,
    projectLabel,
    slotMember,
    mentionSlots,
    liveJoin: (slot) => liveJoin(slot),
    getRecord,
    readDrawer,
    showDrawer,
    closeDrawer,
    mutation,
    refreshChat,
    coordination,
    mergeMessages,
    patchTimeline,
  });
  const {
    selectedPolicy,
    selectedDelegationValid,
    delegationComposer,
    delegationContext,
    patchDelegationComposer,
    policyCard,
  } = delegation;
  function draftContext() {
    const c = chat(),
      reply = messageById(c.reply);
    return `${c.reply ? `<div class="cc-reply-preview">回复：${E(reply ? messageText(reply).slice(0, 100) : '已选择的房间消息')}${button('clear-reply', '取消回复')}</div>` : ''}${c.mentions
      .map((id) => {
        const slot = (state.snapshot.join_slots || []).find((s) => s.id === id);
        return `<button type="button" class="cc-mention-chip" data-cc-action="remove-mention" data-id="${E(id)}">@${E(slot?.label || '位置已不可用')} ×</button>`;
      })
      .join('')}`;
  }
  function discussion() {
    const d = state.snapshot,
      c = chat();
    const enabled = d.capabilities?.plain_messages && d.can_manage && d.room.state === 'active';
    return `<section class="cc-conversation"><div class="cc-timeline-wrap"><div class="cc-feed" tabindex="0" aria-label="共享讨论记录"><div class="cc-history">${c.older ? button('older-messages', '加载更早消息') : ''}</div><div class="cc-message-list" role="log" aria-label="房间消息" aria-live="polite" aria-relevant="additions text">${c.messages.map(messageMarkup).join('') || `<div class="cc-chat-empty"><span class="cc-empty-symbol" aria-hidden="true">↗</span><h3>从一句话开始。</h3><p>说说你的想法，讨论会留在这个房间。</p><p>需要落实时，选择助手并交给它处理。</p>${d.join_slots?.length ? '<small>已登记助手连接；以实际领取与结果确认进度。</small>' : '<button type="button" class="btn" data-cc-action="open-members">添加 dot 或 Work 连接</button>'}</div>`}</div></div><button type="button" class="btn cc-new-messages" data-cc-action="latest" ${c.unread ? '' : 'hidden'}>${c.unread} 条新消息 ↓</button></div><div class="cc-compose-wrap"><form id="cc-command" class="cc-composer"><div id="cc-draft-context">${draftContext()}</div><div id="cc-delegation-context">${delegationContext()}</div><label class="cc-sr-only" for="cc-message-input">房间消息</label><textarea id="cc-message-input" name="request" rows="2" maxlength="4000" required ${enabled ? '' : 'disabled'} placeholder="${enabled ? '说说你的想法，或 @一个助手连接…' : '普通消息暂不可用，请核对房间与服务能力'}">${E(c.draft)}</textarea><div class="cc-compose-controls">${button('mentions', '@ 提醒', {}, enabled && d.capabilities?.message_notifications ? '' : 'disabled')}${coordination.action()}<span id="cc-send-mode">${delegationComposer()}</span><small>来源 · ${E(projectLabel(state.project))}</small><button class="btn primary" type="submit" ${enabled ? '' : 'disabled'}>发送 ↑</button></div></form><div class="cc-compose-help"><span>普通讨论只提醒；委托按已确认范围处理</span><span class="cc-desktop-key">Enter 发送 · Shift + Enter 换行</span><span class="cc-mobile-key">Enter 换行</span></div></div></section>`;
  }
  function memberStrip() {
    const slots = notificationSlots(),
      registered = slots.filter((slot) => slot.state === 'registered').length;
    const label = registered
      ? '你与 ' +
        registered +
        ' 个助手连接' +
        (slots.length > registered ? ' · ' + (slots.length - registered) + ' 个待接入' : '')
      : slots.length
        ? '你 · ' + slots.length + ' 个聊天待接入'
        : '你 · 房间已就绪';
    return `<div class="cc-member-strip"><span class="cc-avatar is-human" aria-hidden="true">你</span>${slots
      .slice(0, 3)
      .map(
        (s) => `<span class="cc-avatar" aria-hidden="true">${s.kind === 'dot' ? '●' : 'W'}</span>`,
      )
      .join(
        '',
      )}<span>${E(label)}</span><button type="button" class="cc-text-button" data-cc-open-joins>助手与连接 ›</button></div>`;
  }
  function contextContents() {
    const d = state.snapshot;
    return `<h2>房间上下文</h2><p class="cc-hint">当前项目：${E(projectLabel(state.project))} · ${E(state.environment)}</p>${coordination.context()}<section><h3>只读任务目标</h3>${d.goals.map((g) => `<div class="cc-goal"><strong>${E(g.request)}</strong><p>${E(g.acceptance)}</p>${badge(g.state)}${d.can_manage && g.state === 'awaiting_decision' ? button('verify_goal', '核对证据后验收', g) : ''}</div>`).join('') || empty('讨论后再确认目标。普通消息不会自动生成任务。')}</section><section><h3>房间任务</h3>${
      d.jobs
        .slice(0, 8)
        .map(
          (j) =>
            `<button type="button" class="cc-context-job" data-cc-action="result" data-id="${E(j.id)}" data-record-kind="job"><strong>${E(j.kind === 'summarize_result' ? '汇总结论' : j.kind === 'propose_monitor_plan' ? '监控计划草稿' : '只读异常分析')}</strong>${badge(j.state)}</button>`,
        )
        .join('') || empty('还没有任务。')
    }</section><p class="cc-hint">仅共享房间记录，不同步私人聊天。通知位置与任务用途分别验证；名称不证明独立原生聊天身份。</p>`;
  }
  function roomRail(selection) {
    const rooms = state.rooms.length
      ? state.rooms
      : state.snapshot.conversation
        ? [state.snapshot.conversation]
        : [];
    return `<aside class="cc-room-rail"><div class="cc-rail-heading"><strong>协作室</strong>${button('new-conversation', '＋', {}, 'aria-label="创建独立聊天室"')}<button class="btn cc-room-close" type="button" data-cc-action="close-rooms" aria-label="关闭房间列表">×</button></div><nav class="cc-room-list" aria-label="房间">${rooms
      .map((r) => {
        const first = r.projects?.find(
          (p) => p.project_id === state.project && p.environment_id === state.environment,
        ) ||
          r.projects?.[0] || { project_id: r.project_id, environment_id: r.environment_id };
        return `<button type="button" data-cc-room="${E(r.id)}" data-project="${E(first.project_id)}" data-environment="${E(first.environment_id)}" class="cc-room ${r.id === state.conversation ? 'is-current' : ''}"><span class="cc-room-icon" aria-hidden="true">${E((conversationTitle(r) || 'C').slice(0, 1))}</span><span><strong>${E(conversationTitle(r))}</strong><small>${r.projects?.length > 1 ? `${r.projects.length} 个项目` : E(first.environment_id)}</small></span></button>`;
      })
      .join(
        '',
      )}</nav><p class="cc-rail-note"><strong>一个房间，共同讨论。</strong><br>项目可中途加入；消息与任务始终保留来源。</p><details class="cc-scope-details"><summary>打开项目默认房间</summary>${selection}</details></aside>`;
  }
  function showDrawer(title, content, trigger) {
    const dialog = document.querySelector('#cc-drawer');
    if (!dialog) return;
    const epoch = ++state.drawerEpoch,
      generation = state.generation;
    if (!dialog.contains(trigger || document.activeElement))
      state.drawerFocus =
        trigger?.closest('.cc-room-menu')?.querySelector('summary') ||
        trigger ||
        document.activeElement;
    dialog.innerHTML = `<header class="cc-drawer-header"><h2>${E(title)}</h2>${button('close-drawer', '关闭', {}, 'aria-label="关闭面板"')}</header><div class="cc-drawer-body">${content}</div>`;
    if (!dialog.open) {
      // Safari pointer clicks need not focus a button. Establish the actual
      // opener before showModal so native dismissal returns to the right node.
      const opener = state.drawerFocus;
      if (opener?.isConnected && !opener.disabled && !opener.closest('[inert]'))
        opener.focus({ preventScroll: true });
      dialog.showModal();
    }
    bindDetails();
    const target = dialog.querySelector('input:not([type="checkbox"]), textarea, select, button'),
      focused = document.activeElement;
    requestAnimationFrame(() => {
      // A later input choice, replacement dialog or navigation owns its focus.
      if (
        epoch === state.drawerEpoch &&
        generation === state.generation &&
        dialog.isConnected &&
        dialog.open &&
        target?.isConnected &&
        document.activeElement === focused
      )
        target.focus();
    });
  }
  function closeDrawer() {
    state.drawerEpoch++;
    const dialog = document.querySelector('#cc-drawer');
    dialog?.close();
  }
  function patchDraftContext() {
    const node = document.querySelector('#cc-draft-context');
    if (node) node.innerHTML = draftContext();
  }
  function mergeMessages(items) {
    const c = chat(),
      map = new Map(c.messages.map((m) => [m.id, m]));
    for (const m of items) map.set(m.id, m);
    c.messages = [...map.values()].sort(
      (a, b) =>
        (a.server_sequence || 0) - (b.server_sequence || 0) ||
        (a.created || 0) - (b.created || 0) ||
        a.id.localeCompare(b.id),
    );
  }
  function patchTimeline(forceBottom = false) {
    const feed = document.querySelector('.cc-feed'),
      list = feed?.querySelector('.cc-message-list');
    if (!list) return;
    const c = chat(),
      nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 70;
    const top = feed.scrollTop,
      oldIds = new Set(
        [...list.querySelectorAll('[data-message-id]')].map((n) => n.dataset.messageId),
      );
    if (c.messages.length) list.querySelector('.cc-chat-empty')?.remove();
    let previous = null;
    for (const m of c.messages) {
      let node = [...list.children].find((n) => n.dataset.messageId === m.id);
      const markup = messageMarkup(m);
      if (!node || node._ccMarkup !== markup) {
        const template = document.createElement('template');
        template.innerHTML = markup;
        const next = template.content.firstElementChild;
        next._ccMarkup = markup;
        if (node) node.replaceWith(next);
        else list.insertBefore(next, previous ? previous.nextSibling : list.firstChild);
        node = next;
      }
      previous = node;
    }
    if (forceBottom || nearBottom) {
      feed.scrollTop = feed.scrollHeight;
      c.unread = 0;
    } else {
      feed.scrollTop = top;
      c.unread += c.messages.filter((m) => !oldIds.has(m.id)).length;
    }
    c.scroll = feed.scrollTop;
    const newer = document.querySelector('.cc-new-messages');
    newer.hidden = !c.unread;
    newer.textContent = `${c.unread} 条新消息 ↓`;
  }
  function markVisibleRead() {
    const c = chat(),
      feed = document.querySelector('.cc-feed');
    if (
      !state.snapshot?.capabilities?.read_cursors ||
      document.hidden ||
      !feed ||
      feed.scrollHeight - feed.scrollTop - feed.clientHeight >= 70
    )
      return;
    const sequence = Math.max(0, ...c.messages.map((m) => Number(m.server_sequence) || 0));
    if (sequence <= (c.read || 0)) return;
    c.read = sequence;
    mutation('read-cursor', {
      room_id: state.snapshot.room.id,
      last_seen_sequence: sequence,
    }).catch(() => {
      c.read = 0;
    });
  }
  function clearVisibility(c, keepDraft = false) {
    coordination.clear();
    c.messages = [];
    c.after = '';
    c.older = '';
    c.reply = '';
    c.mentions = [];
    c.delegationPolicy = '';
    c.delegationVersion = 0;
    c.unread = 0;
    c.visibility = '';
    if (!keepDraft) {
      c.draft = '';
      state.joinDraft = {};
      state.draft = {};
    }
    if (state.view !== 'discussion') {
      const main = document.querySelector('.cc-main');
      if (main) main.innerHTML = empty('访问范围已改变，正在重新核对可见记录。');
    }
    closeDrawer();
    const drawer = document.querySelector('#cc-drawer');
    if (drawer) drawer.innerHTML = '';
    const list = document.querySelector('.cc-message-list');
    if (list) list.innerHTML = empty('访问范围已改变，正在重新核对可见记录。');
    const context = document.querySelector('.cc-context-rail');
    if (context) context.innerHTML = '';
    const area = document.querySelector('#cc-message-input');
    if (area) {
      area.value = c.draft;
      area.disabled = true;
    }
    const submit = document.querySelector('#cc-command button[type="submit"]');
    if (submit) submit.disabled = true;
    patchDraftContext();
  }
  function bindDeniedScope(pending = false) {
    // Replace cleared markup so recovery never reactivates an old listener epoch.
    state.busyCleanup?.();
    state.busyCleanup = null;
    const root = document.querySelector('.collaboration');
    if (!root) return;
    const rail = root.querySelector('.cc-room-rail');
    if (rail) rail.outerHTML = roomRail('');
    const next = root.cloneNode(true);
    for (const node of next.querySelectorAll('button')) {
      const partition = node.dataset.ccPartition,
        room = node.dataset.ccRoom;
      const allowedPartition =
        partition &&
        roomProjects().some(
          (p) => p.project_id === partition && p.environment_id === node.dataset.environment,
        );
      const allowedRoom = room && state.rooms.some((r) => r.id === room);
      const retry = node.dataset.ccAction === 'retry-directory' && !pending;
      node.disabled = !allowedPartition && !allowedRoom && !retry;
    }
    root.replaceWith(next);
    bind();
    if (pending) next.setAttribute('aria-busy', 'true');
  }
  function showAccessRecovery(items, message, error = false, pending = false) {
    state.rooms = items;
    const conversation = items.find((room) => room.id === state.conversation);
    state.snapshot = {
      room: state.snapshot?.room,
      conversation: conversation || { id: state.conversation, projects: [] },
      can_manage: false,
      capabilities: { plain_messages: false, coordination_goals: false },
      agents: [],
      jobs: [],
      goals: [],
      join_slots: [],
      subscriptions: [],
      messages: [],
      probes: [],
      incidents: [],
      plan: null,
      components: {},
    };
    state.members = [];
    state.notes = message;
    state.error = error;
    const strip = document.querySelector('.cc-project-strip');
    if (strip) {
      strip.outerHTML = projectChips();
      document
        .querySelector('.cc-project-strip')
        ?.insertAdjacentHTML(
          'beforeend',
          button('retry-directory', pending ? '正在读取房间目录…' : '重新读取房间目录'),
        );
    }
    const members = document.querySelector('.cc-member-strip');
    if (members) members.outerHTML = memberStrip();
    const feedback = document.querySelector('#cc-feedback');
    if (feedback) {
      feedback.textContent = message;
      feedback.classList.toggle('is-error', error);
    }
    bindDeniedScope(pending);
  }
  async function recoverAccess(allowCurrent = false) {
    clearVisibility(chat());
    const recovery = ++state.generation,
      session = S.session,
      space = S.space_id;
    state.controller?.abort();
    state.controller = new AbortController();
    state.busy = false;
    clearTimeout(state.timer);
    const current = () =>
      recovery === state.generation &&
      session === S.session &&
      space === S.space_id &&
      S.page === 'collaboration';
    showAccessRecovery([], '项目访问范围已改变，旧记录已清除。正在读取最新房间目录…', false, true);
    try {
      const directory = await api('/api/collaboration/conversations', {
        signal: state.controller.signal,
      });
      if (!current()) return;
      const available = (directory.items || []).find((room) => room.id === state.conversation);
      if (
        allowCurrent &&
        available?.projects?.some(
          (p) => p.project_id === state.project && p.environment_id === state.environment,
        )
      ) {
        state.rooms = directory.items || [];
        state.notes = '访问范围已更新。';
        await renderPage(false);
        return;
      }
      showAccessRecovery(
        directory.items || [],
        '项目访问范围已改变，旧记录已清除。请重新选择仍获权的项目。',
      );
    } catch (error) {
      if (!current()) return;
      showAccessRecovery(
        [],
        '旧记录已清除。房间目录读取失败：' + error.message + '。可重试或从项目映射重新进入。',
        true,
      );
    }
  }
  async function refreshChat(forceBottom = false) {
    if (!state.snapshot?.room || !state.snapshot.capabilities?.plain_messages) return;
    const gen = state.generation,
      sequence = ++state.refreshSequence,
      c = chat(),
      room = state.snapshot.room.id;
    const current = () =>
      sequence === state.refreshSequence &&
      gen === state.generation &&
      S.session === state.session &&
      S.space_id === state.space &&
      c === chat();
    let timeline, overview, members, policies;
    try {
      [timeline, overview, members, policies] = await Promise.all([
        getRecord('timeline', '', '', { room_id: room, after: c.after, limit: '100' }),
        getRecord('overview', ''),
        getRecord('members', ''),
        state.snapshot.capabilities?.direct_delegation
          ? getRecord('delegation_policies', '')
          : Promise.resolve({ items: [] }),
      ]);
      if (!current()) return;
      // A known projection change clears private records before unrelated reads.
      if (c.visibility && c.visibility !== timeline.visibility_token) {
        const allowed = overview.conversation?.projects?.some(
          (p) => p.project_id === state.project && p.environment_id === state.environment,
        );
        clearVisibility(c, !!allowed);
        await renderPage(false);
        return;
      }
      await coordination.load(overview, current);
      if (!current()) return;
      await refreshDeliveries(c, current);
    } catch (error) {
      if (!current()) return;
      if (
        [
          'INVALID_CURSOR',
          'FORBIDDEN',
          'PERMISSION_DENIED',
          'AUTHORIZATION_CHANGED',
          'ACCESS_CHANGED',
        ].includes(error.code) ||
        [400, 403, 409].includes(error.status)
      ) {
        await recoverAccess(error.status !== 403);
        return;
      }
      throw error;
    }
    if (!current()) return;
    c.visibility = timeline.visibility_token;
    mergeMessages(timeline.items || []);
    c.after = timeline.after_cursor || c.after;
    state.snapshot = overview;
    state.members = members.items || [];
    delegation.updatePolicies(policies.items || []);
    patchTimeline(forceBottom);
    patchMemberStatus();
    patchDelegationComposer();
    const goals = document.querySelector('.cc-coordination-list');
    if (goals && !goals.contains(document.activeElement)) goals.outerHTML = coordination.list();
    markVisibleRead();
    const context = document.querySelector('.cc-context-rail');
    if (context && !context.contains(document.activeElement)) context.innerHTML = contextContents();
    const strip = document.querySelector('.cc-member-strip');
    if (strip && !strip.contains(document.activeElement)) strip.outerHTML = memberStrip();
    const projects = document.querySelector('.cc-project-strip');
    if (projects && !projects.contains(document.activeElement)) projects.outerHTML = projectChips();
    const area = document.querySelector('#cc-message-input');
    if (area) {
      area.disabled =
        !overview.can_manage ||
        overview.room.state !== 'active' ||
        !overview.capabilities?.plain_messages;
      document.querySelector('#cc-command button[type="submit"]').disabled = area.disabled;
    }
  }
  function jobs() {
    const d = state.snapshot;
    return `<section class="cc-stack">${coordination.list()}<details class="cc-legacy-tasks" open><summary>只读任务记录</summary>${d.jobs.map((job) => `<article class="cc-card"><div class="cc-row"><strong>${E(job.kind === 'summarize_result' ? '结果汇总' : job.kind === 'propose_monitor_plan' ? '监控计划草稿' : '只读异常分析')}</strong>${badge(job.state)}</div><p>${E(text(job.delivery_status))}${job.reason_code ? ` · ${E(job.reason_code)}` : ''}</p><dl class="cc-facts"><div><dt>任务</dt><dd>${E(job.id.slice(-8))}</dd></div><div><dt>领取轮次</dt><dd>${job.attempt} / 3</dd></div><div><dt>期限</dt><dd>${E(when(job.deadline_at))}</dd></div></dl><details data-cc-detail="job" data-id="${E(job.id)}"><summary>上下文、结果与执行记录</summary><div class="cc-detail">展开后读取当前记录</div></details>${d.can_manage && !['succeeded', 'failed', 'cancelled', 'expired', 'dead_letter'].includes(job.state) ? `<div class="cc-actions">${['blocked', 'retry_wait'].includes(job.state) ? button('retry', '在原期限内解除阻塞', job) : ''}${button('cancel', '取消只读任务', job)}</div>` : ''}</article>`).join('') || empty('尚未创建任务。任务是否完成与业务是否恢复会分别显示。')}${d.jobs_next_cursor ? button('more', '加载更多任务', {}, 'data-kind="jobs"') : ''}</details></section>`;
  }
  function monitoring() {
    const d = state.snapshot,
      plan = d.plan;
    const probes = d.probes.map((probe) => [probe.id, `${probe.label} · ${probe.id.slice(-6)}`]);
    const latest = plan?.latest;
    const current = plan
      ? `<article class="cc-card"><div class="cc-row"><h2>监控计划</h2>${badge(plan.status)}</div><p>生效版本 ${plan.active_version || '无'} · 最新草稿 ${plan.latest_version} · 最近采集 ${E(when(plan.last_collected))}</p>${plan.rules.map((rule) => `<div class="cc-row cc-rule"><span>${E(rule.rule_id)}</span>${badge(rule.status)}<small>异常确认 ${rule.opening} · 恢复确认 ${rule.closing}</small></div>`).join('')}<details><summary>审阅最新计划与固定目标</summary>${json(latest.config)}<div class="cc-targets">${d.probes
          .filter((probe) =>
            latest.config.rules.some(
              (rule) => rule.probe_id === probe.id || rule.require_recovery_probe === probe.id,
            ),
          )
          .map(
            (probe) =>
              `<p><strong>${E(probe.label)}</strong><br>${E(probe.target?.url || '固定目标仅向管理者显示')}</p>`,
          )
          .join(
            '',
          )}</div></details>${d.can_manage && (plan.state !== 'active' || plan.active_version !== latest.version) ? `<div class="cc-approval"><label><input id="cc-approval" type="checkbox" data-digest="${E(latest.digest)}"> 已审阅此版本的固定 GET 目标、阈值、期限和只读分析范围</label>${button('activate-plan', '批准并激活此版本', { id: plan.id, version: plan.version }, `data-plan-version="${latest.version}" data-digest="${E(latest.digest)}"`)}</div>` : ''}${d.can_manage && plan.state === 'active' ? `<div class="cc-actions">${button('plan_pause', '暂停此计划', plan)}</div>` : ''}</article>`
      : empty('尚无计划。先登记固定探针，再保存草稿；批准前不会采集。');
    const incidents = d.incidents
      .map(
        (incident) =>
          `<article class="cc-card"><div class="cc-row"><strong>${E(incident.rule_id)} · 第 ${incident.episode} 次发生</strong>${badge(incident.state)}</div><p>${E(incident.severity)} · ${E(text(incident.analysis_state))}${incident.suppressed ? ' · 维护期仅抑制通知' : ''}</p><small>最近证据 ${E(when(incident.updated))} · 计划 v${incident.plan_version}</small><details data-cc-detail="evidence" data-project-diagnostic="true" data-id="${E(incident.evidence_id)}"><summary>查看脱敏探针证据</summary><div class="cc-detail">展开后读取当前授权证据</div></details></article>`,
      )
      .join('');
    return `<section class="cc-stack"><p class="cc-hint">当前项目监控诊断：${E(projectLabel(state.project))} · ${E(state.environment)}。此处查看项目级监控，不扩展房间共享范围。</p>${current}${incidents || empty('暂无已确认异常；这不等于全部业务均健康。')}${d.incidents_next_cursor ? button('more', '加载更多异常', {}, 'data-kind="incidents"') : ''}${d.can_manage ? `<details class="cc-card"><summary>登记固定只读探针</summary><form id="cc-probe" class="cc-form">${field('名称', '<input name="label" maxlength="80" required>')}${field('固定 GET 地址', '<input name="url" type="url" maxlength="2048" placeholder="https://service.example/ready" required>')}<details><summary>受限内部服务配置</summary>${field('允许的私网 CIDR，每行一个', '<textarea name="networks" rows="2" placeholder="仅填写明确允许此探针访问的网段"></textarea>')}<label><input name="allow_http" type="checkbox"> 此固定内部目标允许明文 HTTP</label></details><label><input type="checkbox" required> 确认该 GET 不修改业务数据，地址不含凭据或客户内容</label><button type="submit" class="btn">登记探针，不启动采集</button></form></details><details class="cc-card"><summary>新建或修订监控草稿</summary><form id="cc-plan" class="cc-form"><div class="cc-two">${field('入口探针', `<select name="probe">${options(probes, state.draft.probe)}</select>`)}${field('独立恢复探针', `<select name="recovery">${options(probes, state.draft.recovery, '使用同一个固定探针')}</select>`)}</div>${field('异常分析对象', `<select name="monitor_assignee">${agentOptions('work_cloud', state.draft.monitor_assignee, '不自动创建分析任务')}</select>`)}${button('plan-template', '生成可审阅的初始配置')}${field('计划 JSON', `<textarea name="plan_json" rows="12" spellcheck="false" required placeholder="先生成配置，或粘贴需要审阅的声明式计划。">${E(state.draft.plan_json)}</textarea>`)}<small>初始配置只代表合成探针，不是全部用户请求的成功率。可以调整后先校验，再保存版本。</small><div class="cc-actions">${button('validate-plan', '只校验')}<button type="submit" class="btn">保存新草稿，不激活</button></div></form></details>` : ''}</section>`;
  }

  const joinStatus = (slot) => {
    if (slot.state !== 'revoked' && slot.expires_at * 1000 <= Date.now()) return 'expired';
    if (!['revoked', 'expired'].includes(slot.state) && state.snapshot?.room?.state === 'paused')
      return 'room_paused';
    return slot.state === 'invited' && slot.code_expires_at * 1000 <= Date.now()
      ? 'code_expired'
      : slot.status;
  };
  const liveJoin = (slot) =>
    !['revoked', 'expired'].includes(slot.state) && slot.expires_at * 1000 > Date.now();
  const joinInstruction = (slot) => {
    if (!liveJoin(slot) || state.snapshot?.room?.state !== 'active') return '';
    if (slot.state === 'registered') return slot.subscription_instruction || '';
    return joinStatus(slot) === 'invited' && slot.join_instruction
      ? slot.join_instruction +
          (slot.kind === 'work_cloud'
            ? ' 请使用 ChatGPT 网页的 Work 聊天，或桌面端已选择 Cloud 的 Work 聊天；本地和 SSH Codex 会话不能创建原生事件订阅。'
            : ' 请发送到支持 MCP Events 的原 dot 聊天。') +
          ' 请仅使用当前聊天已有的 CodePier 授权；允许创建上述原生事件订阅，不新建 CodePier 凭据或扩大权限。' +
          ' 请先查找宿主的订阅能力，使用实际事件来源标识；回调与签名材料由宿主提供，并完成原生确认。无法订阅时报告具体缺失能力或实际调用错误。' +
          ' 通知为项目共享，加入不授权领取或执行任务，也不证明聊天身份。'
      : '';
  };
  function joinCard(slot) {
    const d = state.snapshot;
    const status = joinStatus(slot);
    const live = liveJoin(slot);
    const ready = live && d.room.state === 'active';
    const instruction = joinInstruction(slot);
    const routes = slot.routes || [];
    const available = routes.filter((route) => route.available);
    const confirmations = slot.confirmation_event_ids || [];
    const canConfirm =
      ready && confirmations.length > 0 && available.some((route) => !route.chat_receipt_confirmed);
    const hints = {
      invited: '下一步：复制完整指令，发送到这个位置对应的真实 dot / Work 聊天。',
      code_expired: '加入码已失效。刷新后复制新指令；旧码不能加入。',
      waiting_subscription:
        '现有连接已登记。复制下方继续订阅指令，发到原 dot 或 Work Cloud 聊天。插件页已显示事件但仍无法订阅时，请让聊天重新读取可订阅事件源并核对实际连接器标识。',
      subscription_verified: '宿主回调验证通过。发送测试，再到目标聊天逐个核对事件编号。',
      partial_subscription: '仅部分必需事件已订阅。请在原聊天完成缺少的订阅；可先测试现有订阅。',
      partial_confirmed: '你已确认现有订阅的收件，但仍缺少必需事件。这个聊天位置尚未完整接通。',
      test_delivered:
        '已有测试获得 HTTP 接收回执。请在目标聊天核对下方全部事件；回执不代表聊天已收到或用户已读。',
      chat_confirmed:
        '你已确认在目标聊天看到当前测试事件。这是人工收件确认，聊天身份仍未做密码学验证。',
      authorization_unavailable: '原连接授权不可用。请核对原授权；新聊天应创建独立位置。',
      room_paused: '协作室暂停期间不接受加入或测试。恢复后刷新当前状态。',
      revoked: '这个位置已撤销，关联订阅已停止。需要时创建新的聊天位置。',
      expired: '这个位置已到期。需要继续接收时，请创建新位置并重新验证。',
    };
    return (
      '<article class="cc-card cc-join-card" data-cc-slot="' +
      E(slot.id) +
      '">' +
      '<div class="cc-row"><h3>' +
      E(slot.label) +
      '</h3>' +
      badge(status === 'revoked' ? 'slot_revoked' : status) +
      '</div>' +
      '<p class="cc-hint">' +
      E(slot.kind === 'dot' ? 'dot · 通知位置' : 'Work · 通知位置') +
      ' · 位置 ' +
      E(slot.id.slice(-8)) +
      '</p>' +
      '<p class="cc-join-hint">' +
      E(hints[status] || '请刷新并核对当前状态。') +
      '</p>' +
      (instruction
        ? '<div class="cc-join-invitation">' +
          (slot.state === 'invited'
            ? '<div class="cc-row"><strong class="cc-join-code">' +
              E(slot.join_code) +
              '</strong><small>加入码有效至 ' +
              E(when(slot.code_expires_at)) +
              '</small></div>'
            : '') +
          field(
            slot.state === 'registered' ? '发到原聊天的继续订阅指令' : '发到对应聊天的完整加入指令',
            '<textarea class="cc-join-instruction" rows="5" readonly spellcheck="false">' +
              E(instruction) +
              '</textarea>',
          ) +
          '<div class="cc-actions">' +
          button(
            'join-copy',
            slot.state === 'registered' ? '复制继续订阅指令' : '复制加入指令',
            slot,
          ) +
          button('join-select', '选择指令文本', slot) +
          '</div></div>'
        : '') +
      '<small>位置有效至 ' +
      E(when(slot.expires_at)) +
      ' · 项目共享通知 · 无任务领取权限</small>' +
      (slot.state === 'registered'
        ? '<p class="cc-join-coverage">有效订阅 ' +
          E(slot.subscription_count ?? available.length) +
          ' / ' +
          E(slot.expected_subscription_count ?? (slot.kind === 'dot' ? 4 : 2)) +
          (slot.connection_complete ? ' · 必需订阅与人工收件确认齐全' : ' · 连接验证尚未完成') +
          '</p>' +
          ((slot.missing_events || []).length
            ? '<p class="cc-hint">缺少事件：' +
              slot.missing_events.map((name) => E(name.replace('codepier.', ''))).join('、') +
              '</p>'
            : '')
        : '') +
      (routes.length
        ? '<ul class="cc-join-routes" aria-label="订阅与测试证据">' +
          routes
            .map(
              (route) =>
                '<li><div class="cc-row"><strong>' +
                E(route.name.replace('codepier.', '')) +
                '</strong>' +
                badge(route.available ? route.state : 'no_valid_subscription') +
                '</div>' +
                '<small>订阅 ' +
                E(route.id.slice(-8)) +
                ' · 到期 ' +
                E(when(route.expires_at)) +
                '</small>' +
                (route.test
                  ? '<p>测试事件：<span class="cc-event-id">' +
                    E(route.test.event_id) +
                    '</span></p><p class="cc-hint">' +
                    E(text(route.test.state)) +
                    (route.chat_receipt_confirmed
                      ? ' · 用户已核对这个事件'
                      : ' · 尚未确认目标聊天收件') +
                    '</p>'
                  : '<p class="cc-hint">尚未发送测试事件</p>') +
                '</li>',
            )
            .join('') +
          '</ul>'
        : '') +
      (canConfirm && d.can_manage
        ? '<form class="cc-join-confirm cc-approval" data-slot-id="' +
          E(slot.id) +
          '" data-version="' +
          E(slot.version) +
          '" data-events="' +
          E(JSON.stringify(confirmations)) +
          '"><label><input name="confirmed_received" type="checkbox" required> 我已在此位置对应的真实聊天中逐个看到并核对上方全部当前测试事件。</label>' +
          '<button class="btn" type="submit">确认目标聊天已收到</button></form>'
        : '') +
      (d.can_manage && live
        ? '<div class="cc-actions">' +
          (slot.state === 'invited' && ready
            ? button('join-refresh_code', '刷新加入码', slot)
            : '') +
          (slot.state === 'registered' && ready && available.length
            ? button('join-test', '发送无敏感测试事件', slot)
            : '') +
          button('join-revoke', '撤销位置并停止关联订阅', slot) +
          '</div>'
        : '') +
      '</article>'
    );
  }
  function memberAccessCard(slot, expanded = false) {
    const member = state.members.find(
      (m) =>
        m.slot_id === slot.id ||
        m.join_slot_id === slot.id ||
        m.member_ref === slot.id ||
        m.id === slot.id,
    );
    if (!member || !liveJoin(slot) || slot.state !== 'registered') return '';
    const subscription = member.message_subscription_request;
    const instruction = subscription
      ? '@CodePier 请使用当前已有连接，为这个通知位置订阅房间消息提醒事件 ' +
        subscription.name +
        '，参数：' +
        JSON.stringify(subscription.arguments) +
        '。允许创建这一项原生事件订阅，仍需完成宿主确认；不要更改凭据或扩大 CodePier 授权。此订阅只用于明确 @ 提醒，不创建任务，也不保证自动回复。'
      : '';
    return `<section class="cc-card cc-speaking" data-speaking-slot="${E(slot.id)}"><h3>房间讨论能力</h3><p data-cc-speaking-status="${E(slot.id)}">${member.can_speak ? '这个连接已获准在本房间发言' : '这个连接尚未获准在本房间发言'}</p><p class="cc-hint">同一授权下的多个聊天共享这项许可。标签不证明独立聊天身份；通知订阅、房间发言和任务资格分别管理。</p>${member.speaking_expires_at ? `<small>发言许可到期：${E(when(member.speaking_expires_at))}</small>` : ''}${state.snapshot.can_manage && member.grant_id ? `<div class="cc-actions">${button('message-access', member.can_speak ? '撤回这个连接的发言许可' : '审阅并允许这个连接发言', { id: member.grant_id }, `data-enabled="${!member.can_speak}" data-speaking-version="${member.speaking_version || 0}"`)}</div>` : ''}${instruction ? `<details ${expanded ? 'open' : ''}><summary>独立订阅 @ 提醒</summary><p class="cc-hint">已有 CPJ 通知不会自动增加此事件。将以下指令发到对应的原聊天，完成新的宿主确认。</p><label class="cc-field"><span>@ 提醒订阅指令</span><textarea class="cc-mention-instruction" readonly rows="5">${E(instruction)}</textarea></label>${button('mention-copy', '复制 @ 提醒订阅指令')}<p data-cc-mention-status="${E(slot.id)}">${E(mentionReadiness(slot))}</p></details>` : ''}</section>`;
  }
  function joining() {
    const d = state.snapshot;
    const slots = d.join_slots || [];
    const form =
      d.can_manage && d.room.state === 'active'
        ? '<form id="cc-join-slot" class="cc-form"><div class="cc-two">' +
          field(
            '聊天位置名称',
            '<input name="label" maxlength="80" value="' +
              E(state.joinDraft.label) +
              '" placeholder="例如：项目协调 dot" autocomplete="off" required>',
          ) +
          field(
            '聊天类型',
            '<select name="kind"><option value="dot"' +
              (state.joinDraft.kind !== 'work_cloud' ? ' selected' : '') +
              '>dot · 通知位置</option><option value="work_cloud"' +
              (state.joinDraft.kind === 'work_cloud' ? ' selected' : '') +
              '>Work · 通知位置</option></select>',
          ) +
          '</div><div class="cc-row"><small>每个真实聊天单独创建一个位置。位置有效 7 天，加入码有效 30 分钟。</small>' +
          '<button class="btn primary" type="submit">创建聊天位置并获取加入码</button></div></form>'
        : empty(d.can_manage ? '请先恢复协作室，再添加聊天。' : '请由空间管理员添加聊天位置。');
    return (
      '<section class="cc-join cc-stack" aria-labelledby="cc-join-title"><section class="cc-card cc-join-intro">' +
      '<span class="cc-eyebrow">加入项目协作</span><h2 id="cc-join-title">把 dot 和 Work 聊天接入讨论室</h2>' +
      '<p>登记当前聊天后，确认一次处理范围，复制一条接入指令。以后的明确委托直接在房间发送，进度和结果回到原话题。</p>' +
      form +
      '<p class="cc-hint">加入码只定位讨论室，使用已有项目授权，不创建凭据或扩大权限。项目通知共享；@位置不是任务指派，也不能证明原始聊天身份。</p></section>' +
      slots
        .map((slot) =>
          slot.state === 'registered'
            ? policyCard(slot) +
              '<details class="cc-card"><summary>普通讨论提醒与回帖</summary>' +
              memberAccessCard(slot) +
              '</details>' +
              '<details class="cc-card"><summary>高级：项目监控订阅与测试</summary>' +
              joinCard(slot) +
              '</details>'
            : joinCard(slot),
        )
        .join('') +
      '<details class="cc-card"><summary>聊天中看不到新的工具或事件？</summary><p class="cc-hint">如果插件页仍显示旧工具或事件，可重新扫描 MCP，再回到原聊天继续。无需更换现有授权。</p><a href="https://developers.openai.com/plugins/build/mcp-events#test-in-chatgpt" target="_blank" rel="noopener noreferrer">查看官方事件测试说明</a></details>' +
      '</section>'
    );
  }
  function expireInvitations(root) {
    clearTimeout(state.expiryTimer);
    const invited = (state.snapshot?.join_slots || []).filter(
      (slot) => slot.state === 'invited' && slot.join_instruction,
    );
    let next = Infinity;
    for (const slot of invited) {
      const remaining = Math.min(slot.code_expires_at, slot.expires_at) * 1000 - Date.now();
      if (remaining <= 0) {
        const card = Array.from(root.querySelectorAll('[data-cc-slot]')).find(
          (node) => node.dataset.ccSlot === slot.id,
        );
        if (card?.querySelector('.cc-join-invitation')) {
          card.querySelector('.cc-join-invitation').remove();
          card.querySelector('.cc-status').textContent = '加入码已过期';
          card.querySelector('.cc-join-hint').textContent =
            '加入码已失效。刷新后复制新指令；旧码不能加入。';
        }
      } else next = Math.min(next, remaining);
    }
    if (Number.isFinite(next)) {
      const generation = state.generation;
      state.expiryTimer = setTimeout(
        () => {
          if (generation === state.generation && root.isConnected) expireInvitations(root);
        },
        Math.max(1, Math.min(next + 10, 2147483647)),
      );
    }
  }

  function agents() {
    const d = state.snapshot;
    const available = state.grants
      .filter(
        (grant) =>
          !grant.revoked &&
          Array.isArray(grant.scopes) &&
          grant.scopes.includes('read') &&
          grant.scopes.every((scope) => ['read', 'devices.read'].includes(scope)),
      )
      .map((grant) => [
        grant.id || grant.grant_id,
        `${grant.label || '只读连接'} · ${(grant.id || grant.grant_id).slice(-6)}`,
      ]);
    return `<section class="cc-stack">${joining()}<details class="cc-advanced"><summary>高级：任务用途绑定与事件订阅</summary><h2>独立任务资格</h2><p class="cc-hint">登记、订阅有效和最近响应是三件事。同一连接中的聊天不能仅凭名字彼此隔离。</p>${d.agents.map((agent) => `<article class="cc-card"><div class="cc-row"><strong>${E(agent.label)} <small>${agent.kind === 'dot' ? 'dot' : 'Work Cloud'} · ${E(agent.id.slice(-6))}</small></strong>${badge(agent.binding_status)}</div><dl class="cc-facts"><div><dt>有效订阅</dt><dd>${agent.valid_subscriptions}</dd></div><div><dt>最近领取</dt><dd>${E(when(agent.last_claim))}</dd></div><div><dt>最近结果</dt><dd>${E(when(agent.last_result))}</dd></div></dl><small>用途绑定到期：${E(when(agent.expires_at))} · 原始聊天身份未做密码学验证</small>${d.can_manage ? `<div class="cc-actions">${button(agent.enabled ? 'agent_disable' : 'agent_enable', agent.enabled ? '停用此用途' : '启用此用途', agent)}${button('agent_renew', '确认续期 7 天', agent)}</div>` : ''}</article>`).join('') || empty('先在目标 Work 或 dot 聊天连接一个只读 CodePier 连接，再登记其用途。')}${d.can_manage ? `<details class="cc-card"><summary>登记智能体用途</summary><form id="cc-agent" class="cc-form"><div class="cc-two">${field('显示名称', '<input name="label" maxlength="80" required>')}${field('类型', '<select name="kind"><option value="work_cloud">Work Cloud · 只读分析</option><option value="dot">dot · 协调与汇总</option></select>')}</div>${field('现有只读连接', `<select name="grant_id" required>${options(available, '', '选择现有连接')}</select>`)}<small>这里只绑定现有授权，不创建凭据或扩大权限。没有候选连接时，请在 MCP 接入中创建项目限定的只读连接。</small><button class="btn" type="submit">确认登记 7 天</button></form></details>` : ''}<h2>事件订阅</h2>${d.subscriptions.map((sub) => `<article class="cc-card"><div class="cc-row"><strong>${E(sub.name.replace('codepier.', ''))}</strong>${badge(sub.state)}</div><p>${E(sub.arguments.queue || '项目结果与状态')} · ${E(sub.id.slice(-8))}</p><dl class="cc-facts"><div><dt>到期</dt><dd>${E(when(sub.expires_at))}</dd></div><div><dt>最近接收</dt><dd>${E(when(sub.last_accepted))}</dd></div></dl><small>接收回执不代表已领取或用户已读。回调地址与签名材料不在面板显示。</small>${d.can_manage ? `<div class="cc-actions">${button('subscription_test', '发送无敏感测试事件', sub)}${button(sub.state === 'paused' ? 'subscription_resume' : 'subscription_pause', sub.state === 'paused' ? '恢复投递' : '暂停投递', sub)}</div>` : ''}</article>`).join('') || empty('订阅必须由对应的 Work / dot 聊天发起。面板不会代替用户创建或控制聊天。')}${d.subscriptions_next_cursor ? button('more', '加载更多订阅', {}, 'data-kind="subscriptions"') : ''}</details></section>`;
  }
  function sidebar() {
    const d = state.snapshot;
    return `<aside class="cc-sidebar"><section class="cc-card"><h2>当前目标</h2>${d.goals.map((goal) => `<div class="cc-goal"><strong>${E(goal.request)}</strong><p>${E(goal.acceptance)}</p>${goal.state === 'open' ? '<span class="cc-status">进行中</span>' : badge(goal.state)}${d.can_manage && goal.state === 'awaiting_decision' ? button('verify_goal', '核对证据后验收', goal) : ''}</div>`).join('') || empty('明确的用户指令会生成可追踪目标。')}</section><section class="cc-card"><h2>链路状态</h2>${Object.entries(
      d.components || {},
    )
      .map(
        ([name, value]) =>
          `<div class="cc-row cc-rule"><span>${E({ collector: '采集器', events: '事件投递器', reconcile: '恢复扫描' }[name] || name)}</span>${badge(value.status)}</div>`,
      )
      .join(
        '',
      )}<p class="cc-hint">运行中只表示确定性服务在运行，不表示智能体在线。</p></section></aside>`;
  }

  async function html() {
    if (state.session !== S.session || state.space !== S.space_id) {
      state.session = S.session;
      state.space = S.space_id;
      state.requests.clear();
      state.chats.clear();
      state.rooms = [];
      if (!state.project) {
        try {
          const saved = JSON.parse(
            sessionStorage.getItem('codepier-collaboration-selection') || 'null',
          );
          if (saved?.space === S.space_id) {
            state.project = saved.project;
            state.environment = saved.environment;
            state.conversation = saved.conversation || '';
          }
        } catch {}
      }
      resetScope();
    }
    state.controller = new AbortController();
    const gen = state.generation,
      session = S.session,
      space = S.space_id;
    const status = await api('/api/collaboration/status', { signal: state.controller.signal });
    const header =
      '<header class="cc-heading"><div><span class="cc-eyebrow">CodePier / Collaboration</span><h1 tabindex="-1">协作中心</h1><p>让目标、任务和证据在同一处衔接。</p></div></header>';
    if (!status.features.enabled) state.snapshot = null;
    if (!status.features.enabled)
      return `<div class="collaboration">${header}<section class="cc-card cc-intro"><h2>协作中心尚未启用</h2><p>当前没有启动采集、创建订阅或派发分析任务。部署维护者可按实施说明开启只读试点，原有项目功能不受影响。</p><a class="btn" href="https://github.com/cyeinfpro/codepier/blob/main/docs/COLLABORATION.md" target="_blank" rel="noopener noreferrer">查看启用与验收说明</a></section></div>`;
    if (!S.projects.some((project) => project.id === state.project))
      state.project = S.projects[0]?.id || '';
    if (!state.project)
      return `<div class="collaboration">${header}${empty('请先创建一个有权限访问的项目映射。')}</div>`;
    const data = await getRecord('overview', '');
    const grants = data.can_manage
      ? (await api('/api/grants', { signal: state.controller.signal })).grants
      : [];
    if (gen !== state.generation || session !== S.session || space !== S.space_id) return '';
    state.snapshot = data;
    state.conversation = data.conversation?.id || '';
    persistSelection();
    state.grants = grants || [];
    if (data.room && data.capabilities?.plain_messages) {
      const c = chat();
      const [timeline, rooms, members, policies] = await Promise.all([
        getRecord('timeline', '', '', { room_id: data.room.id, limit: '100' }),
        data.conversation
          ? api('/api/collaboration/conversations', { signal: state.controller.signal })
          : getRecord('rooms', ''),
        getRecord('members', ''),
        data.capabilities?.direct_delegation
          ? getRecord('delegation_policies', '')
          : Promise.resolve({ items: [] }),
      ]);
      if (gen !== state.generation || session !== S.session || space !== S.space_id) return '';
      if (c.visibility && c.visibility !== timeline.visibility_token) clearVisibility(c, true);
      c.visibility = timeline.visibility_token;
      mergeMessages(timeline.items || []);
      c.after = timeline.after_cursor || '';
      c.older = timeline.next_cursor || '';
      c.initialized = true;
      state.rooms = rooms.items || [];
      state.members = members.items || [];
      delegation.updatePolicies(policies.items || []);
    }
    await coordination.load(data);
    if (gen !== state.generation || session !== S.session || space !== S.space_id) return '';
    const selection = `<form id="cc-scope" class="cc-scope">${field(
      '项目',
      `<select name="project">${options(
        S.projects.map((project) => [project.id, project.alias || project.id]),
        state.project,
      )}</select>`,
    )}${field('环境', `<input name="environment" value="${E(state.environment)}" pattern="[A-Za-z0-9][A-Za-z0-9_.-]*" maxlength="64" required>`)}<button class="btn" type="submit">切换</button>${data.room ? '' : button('refresh', '刷新状态')}${data.room && data.can_manage ? button(data.room.state === 'paused' ? 'resume' : 'pause', data.room.state === 'paused' ? '恢复协作室' : '暂停协作室', data.room) : ''}</form>`;
    const feedback = `<p id="cc-feedback" class="cc-feedback ${state.error ? 'is-error' : ''}" role="status" aria-live="polite">${E(state.notes)}</p>`;
    if (!data.room)
      return `<div class="collaboration">${header}${selection}${feedback}<section class="cc-card cc-intro"><h2>开启这个项目的共享讨论室</h2><p>创建讨论室只保存协作记录，不代表授权采集、添加订阅或执行代码。</p>${data.can_manage ? button('create-room', '创建项目讨论室') : empty('请由项目所属空间的管理员创建讨论室。')}</section></div>`;
    const tabs = [
      ['discussion', '讨论'],
      ['jobs', '目标与任务'],
      ['monitor', '监控与诊断'],
      ['agents', '助手与连接'],
    ];
    const body = { discussion, jobs, monitor: monitoring, agents }[state.view]();
    const project = S.projects.find((p) => p.id === state.project);
    return `<div class="collaboration cc-chat-shell ${state.view === 'discussion' ? 'is-discussion' : 'is-management'}" data-project="${E(state.project)}" data-environment="${E(state.environment)}" data-conversation="${E(state.conversation)}"><h1 class="cc-sr-only" tabindex="-1">协作中心</h1>${roomRail(selection)}<section class="cc-room-body"><header class="cc-room-header"><div><div class="cc-room-title"><h2>${E(data.conversation ? conversationTitle(data.conversation) : data.room.title || project?.alias || state.project)}</h2><span class="cc-status">${E(state.environment)}</span>${data.room.state === 'paused' ? badge('paused') : ''}</div><p>让讨论、决定和结果，在同一个地方相遇。</p></div><div class="cc-header-tools"><details class="cc-room-menu"><summary data-cc-more aria-label="房间更多选项">···</summary><div>${button('room-settings', '切换房间')}${button('new-conversation', '新建独立聊天室')}${coordination.action('创建协作目标')}${data.capabilities?.message_search ? button('search', '搜索消息') : ''}${button('context', '房间上下文')}${button('refresh', '刷新状态')}</div></details></div></header>${projectChips()}${memberStrip()}<nav class="cc-tabs" aria-label="协作中心视图">${tabs.map(([id, label]) => `<button class="btn ${id === state.view ? 'is-active' : ''}" data-cc-view="${id}" aria-pressed="${id === state.view}">${label}</button>`).join('')}</nav>${feedback}<div class="cc-main">${body}</div></section><aside class="cc-context-rail">${contextContents()}</aside><dialog id="cc-drawer" class="cc-drawer" aria-label="房间详情"></dialog></div>`;
  }

  async function act(action, element) {
    const local = { local: true, message: '' };
    if (action.startsWith('goal-')) return coordination.act(action, element);
    if (
      [
        'result',
        'thread',
        'source',
        'mention-connect',
        'message-remind',
        'delegation-result',
        'delegation-remind',
        'delegation-retry-blocked',
      ].includes(action)
    ) {
      const origin = element.closest('[data-message-id]');
      if (
        origin?.dataset.project &&
        (origin.dataset.project !== state.project ||
          origin.dataset.environment !== state.environment)
      ) {
        if (!(await selectPartition(origin.dataset.project, origin.dataset.environment)))
          return local;
      }
    }
    if (action === 'close-rooms') {
      document.querySelector('.cc-room-rail')?.classList.remove('is-open');
      document.querySelector('[data-cc-more]')?.focus();
      return local;
    }
    if (action === 'new-conversation' || action === 'add-project') {
      const adding = action === 'add-project';
      const existing = roomProjects();
      const choices = S.projects.filter(
        (p) =>
          !adding ||
          !existing.some((v) => v.project_id === p.id && v.environment_id === state.environment),
      );
      showDrawer(
        adding ? '在这个房间添加项目' : '新建独立聊天室',
        `<p>${adding ? '保留当前房间 ID、消息历史、草稿和原任务归属。' : '选择一个或多个项目，在独立的房间里讨论。'}</p><form id="${adding ? 'cc-add-project' : 'cc-new-conversation'}" class="cc-form" data-version="${E(state.snapshot.conversation?.version || 0)}">${adding ? '' : field('房间名称', '<input name="title" maxlength="120" required placeholder="为这个讨论起个名字">')}${
          adding
            ? field(
                '要加入的项目',
                `<select name="project" required>${options(
                  choices.map((p) => [p.id, p.alias || p.id]),
                  '',
                  '请选择一个已授权项目',
                )}</select>`,
              )
            : `<fieldset class="cc-project-choices"><legend>参与项目（至少一个）</legend>${choices.map((p) => `<label><input type="checkbox" name="projects" value="${E(p.id)}">${E(p.alias || p.id)}</label>`).join('')}</fieldset>`
        }${field('项目环境', `<input name="environment" value="${E(state.environment)}" maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9_.-]*" required>`)}<p class="cc-hint">项目加入不扩大任何连接权限。每条消息、回复和任务保留来源项目；助手连接只能读取自己已有权的项目。新房间的发言许可和 @ 订阅需要分别确认。</p><label><input name="confirm" type="checkbox" required> 已核对项目与房间范围</label><button class="btn primary" type="submit" ${choices.length ? '' : 'disabled'}>${adding ? '确认添加项目' : '创建聊天室'}</button></form>`,
        element,
      );
      return local;
    }
    if (action === 'mention-copy') {
      const area = element.closest('.cc-speaking').querySelector('.cc-mention-instruction');
      area.focus();
      area.select();
      area.setSelectionRange(0, area.value.length);
      try {
        if (navigator.clipboard?.writeText) {
          await navigator.clipboard.writeText(area.value);
          return { local: true, message: '已复制 @ 提醒订阅指令。请在对应的原聊天完成宿主确认。' };
        }
      } catch {}
      return { local: true, message: '已选中指令，请使用系统复制。' };
    }
    if (action === 'message-access') {
      const enabled = element.dataset.enabled === 'true';
      showDrawer(
        enabled ? '允许连接参与房间讨论' : '撤回连接的房间发言许可',
        `<p>${enabled ? '这个现有连接将能够在当前房间读取获准记录并发言，有效期 7 天。' : '这个连接将不能再通过房间发言接口发送新消息。'}</p><p>同一授权下的多个聊天共享这项许可，无法仅凭 Dots 或 Work 标签隔离原生聊天身份。不会创建任务用途、凭据或代码执行权限。</p><form id="cc-message-access" class="cc-form" data-grant="${E(element.dataset.id)}" data-version="${E(element.dataset.speakingVersion)}" data-enabled="${enabled}"><label><input type="checkbox" name="confirm" required> 我已核对连接 ${E(element.dataset.id.slice(-8))} 与当前房间范围</label><button class="btn primary" type="submit">${enabled ? '确认允许这个连接发言' : '确认撤回发言许可'}</button></form>`,
        element,
      );
      return local;
    }
    if (action.startsWith('delegation-')) return delegation.act(action, element);
    if (action === 'message-remind') {
      const message = messageById(element.dataset.messageId);
      if (!message) throw new Error('原消息已不可用，请重新读取。');
      const generation = state.generation;
      const result = await mutation('message-remind', {
        room_id: message.source_room_id || message.room_id,
        message_id: message.id,
        expected_message_version: Number(message.version || 1),
        slot_ids: [element.dataset.id],
      });
      if (generation !== state.generation) return local;
      if (result.message) {
        mergeMessages([result.message]);
        patchTimeline();
      }
      return {
        local: true,
        message: '已核对这条原消息的提醒状态；正文没有重发，请以消息旁的投递记录为准。',
      };
    }
    if (action === 'open-members') {
      rememberChat();
      state.view = 'agents';
      await renderPage(false);
      return local;
    }
    if (action === 'close-drawer') {
      closeDrawer();
      return local;
    }
    if (action === 'latest') {
      const feed = document.querySelector('.cc-feed');
      feed.scrollTop = feed.scrollHeight;
      chat().unread = 0;
      document.querySelector('.cc-new-messages').hidden = true;
      return local;
    }
    if (action === 'context') {
      showDrawer('房间上下文', contextContents(), element);
      return local;
    }
    if (action === 'room-settings') {
      const rail = document.querySelector('.cc-room-rail');
      rail.classList.toggle('is-open');
      const details = rail.querySelector('details');
      details.open = true;
      details.querySelector('select')?.focus();
      return local;
    }
    if (action === 'search') {
      showDrawer(
        '搜索房间消息',
        `<form id="cc-search" class="cc-form">${field('关键词', '<input name="query" maxlength="200" required>')}<button type="submit" class="btn">搜索</button></form><div class="cc-search-results"></div>`,
        element,
      );
      return local;
    }
    if (action === 'mentions') {
      const members = await readDrawer('members', '', '选择提醒对象', element);
      if (!members) return local;
      state.members = members.items || [];
      const generation = state.generation,
        epoch = state.drawerEpoch;
      if (state.snapshot?.capabilities?.direct_delegation) {
        const policies = await getRecord('delegation_policies', '');
        if (generation !== state.generation || epoch !== state.drawerEpoch) return local;
        delegation.updatePolicies(policies.items || []);
      }
      const slots = mentionSlots();
      showDrawer(
        '选择提醒对象',
        `<p>选择已确认范围内的委托，或仅发送讨论提醒。正文不会自动决定执行方式。</p><div class="cc-mention-options">${slots.map((s) => `<div class="cc-mention-row">${delegation.pickerAction(s)}<button type="button" class="cc-mention-option" data-cc-action="pick-mention" data-id="${E(s.id)}" aria-pressed="${chat().mentions.includes(s.id) && !chat().delegationPolicy}"><strong>仅讨论提醒 · ${E(s.label)}</strong><span>${s.kind === 'dot' ? 'dot' : 'Work'} · ${E(s.id.slice(-6))}</span><small data-cc-mention-status="${E(s.id)}">${E(mentionReadiness(s))}</small></button>${button('mention-connect', '接通与回帖设置', s)}</div>`).join('') || empty('还没有聊天连接。先添加 dot 或 Work。')}</div>${button('open-members', '添加或管理助手连接')}<p class="cc-hint">请使用此处的结构化选择。纯文本 @ 不会派发；名称不证明独立聊天身份。</p>`,
        element,
      );
      return local;
    }
    if (action === 'mention-connect' || action === 'mention-notifications') {
      const members = await readDrawer('members', '', '核对聊天连接…', element);
      if (!members) return local;
      state.members = members.items || [];
      const generation = state.generation,
        epoch = state.drawerEpoch;
      if (state.snapshot?.capabilities?.direct_delegation) {
        const policies = await getRecord('delegation_policies', '');
        if (generation !== state.generation || epoch !== state.drawerEpoch) return local;
        delegation.updatePolicies(policies.items || []);
      }
      const slot = mentionSlots().find((item) => item.id === element.dataset.id);
      if (!slot) throw new Error('这个聊天位置已不可用，请在助手与连接中核对。');
      if (action === 'mention-notifications') {
        showDrawer(
          '仅接收提醒 · ' + slot.label,
          '<p class="cc-hint">接收普通讨论提醒。在原聊天确认后生效，不改变已有委托范围。</p>' +
            memberAccessCard(slot, true),
          element,
        );
        return local;
      }
      showDrawer(
        '接通 ' + slot.label,
        `<p class="cc-hint">接通后可继续派发原任务，草稿和已保存消息会保留。</p>${slot.state === 'registered' ? policyCard(slot) : joinCard(slot)}<details class="cc-connection-settings"><summary>普通讨论提醒与回帖</summary><p data-cc-mention-status="${E(slot.id)}">${E(mentionReadiness(slot))}</p>${memberAccessCard(slot, true)}</details><details class="cc-connection-settings"><summary>高级：项目监控订阅与测试</summary>${slot.state === 'registered' ? joinCard(slot) : ''}</details>${button('open-members', '管理助手连接')}`,
        element,
      );
      return local;
    }
    if (action === 'pick-delegation') {
      const policy = delegation.policyFor(element.dataset.id);
      if (
        !delegation.policyAvailable(policy) ||
        policy.id !== element.dataset.policyId ||
        policy.version !== Number(element.dataset.policyVersion)
      )
        throw new Error('委托范围已改变，请重新打开 @ 选择器核对；正文已保留。');
      const c = chat();
      c.mentions = [element.dataset.id];
      delegation.selectPolicy(
        policy,
        policy.version,
        policy.automatic_delegation === true && !c.reply,
      );
      patchDraftContext();
      patchDelegationComposer();
      closeDrawer();
      if (!delegation.selectedScopeValid())
        delegation.openScope(document.querySelector('#cc-message-input'));
      else document.querySelector('#cc-message-input')?.focus();
      return local;
    }
    if (action === 'pick-mention' || action === 'remove-mention') {
      const c = chat(),
        id = element.dataset.id;
      c.mentions =
        action === 'pick-mention' && c.delegationPolicy
          ? [id]
          : c.mentions.includes(id)
            ? c.mentions.filter((v) => v !== id)
            : [...c.mentions, id];
      c.delegationPolicy = '';
      c.delegationVersion = 0;
      c.delegationAutomatic = false;
      element.setAttribute('aria-pressed', String(c.mentions.includes(id)));
      patchDraftContext();
      patchDelegationComposer();
      return local;
    }
    if (action === 'reply' || action === 'clear-reply') {
      const source = action === 'reply' ? messageById(element.dataset.id) : null;
      if (
        source?.project_id &&
        (source.project_id !== state.project || source.environment_id !== state.environment)
      ) {
        if (!(await selectPartition(source.project_id, source.environment_id))) return local;
      }
      chat().reply = action === 'reply' ? element.dataset.id : '';
      if (chat().delegationAutomatic) delegation.selectPolicy(null);
      patchDraftContext();
      patchDelegationComposer();
      document.querySelector('#cc-message-input')?.focus();
      return local;
    }
    if (action === 'source') {
      const id = element.dataset.id;
      let node = [...document.querySelectorAll('.cc-feed [data-message-id]')].find(
        (n) => n.dataset.messageId === id,
      );
      if (node) {
        closeDrawer();
        node.scrollIntoView({ block: 'center' });
        node.tabIndex = -1;
        node.focus({ preventScroll: true });
      } else {
        const result = await readDrawer('thread', id, '来源消息', element);
        if (result)
          showDrawer(
            '来源消息',
            (result.items || []).map(messageMarkup).join('') || empty('来源当前不可访问。'),
            element,
          );
      }
      return local;
    }
    if (action === 'thread') {
      const result = await readDrawer('thread', element.dataset.id, '话题与回复', element);
      if (result)
        showDrawer(
          '话题与回复',
          (result.items || []).map(messageMarkup).join('') || empty('暂无更多回复。'),
          element,
        );
      return local;
    }
    if (action === 'convert') {
      const m = messageById(element.dataset.id),
        workers = eligibleWorkers();
      if (!m) throw new Error('来源消息已改变，请刷新。');
      if (
        m.project_id &&
        (m.project_id !== state.project || m.environment_id !== state.environment)
      ) {
        if (!(await selectPartition(m.project_id, m.environment_id))) return local;
        const trigger = [...document.querySelectorAll('.cc-feed [data-cc-action="convert"]')].find(
          (n) => n.dataset.id === m.id,
        );
        if (trigger) return act('convert', trigger);
        return local;
      }
      const allowed = state.snapshot.capabilities?.task_assignment && workers.length;
      showDrawer(
        '把讨论转为任务',
        `<p>确认后才会创建一个只读任务。通知位置不能直接担任执行者。</p><div class="cc-source-quote"><small>来源消息</small><p class="cc-prose">${E(messageText(m))}</p></div>${allowed ? '' : '<p class="cc-task-blocker" role="status">当前没有可用的只读任务执行者，或服务未启用任务指派。先在助手与连接中核对独立任务资格；你的消息已保留。</p>'}<form id="cc-task-review" class="cc-form" data-message-id="${E(m.id)}" data-version="${E(m.version || 1)}">${field('任务所属项目', `<select name="target_project" required>${options([[state.project, `${projectLabel(state.project)} · ${state.environment}`]], '', '明确确认来源项目')}</select>`)}${field(
          '执行者',
          `<select name="assignee" required ${allowed ? '' : 'disabled'}>${options(
            workers.map((a) => [
              a.id,
              `${a.label} · ${a.kind === 'dot' ? 'dot' : 'Work'} · ${a.id.slice(-6)}`,
            ]),
            '',
            '明确选择合格执行者',
          )}</select>`,
        )}${field('只读任务类型', '<select name="kind"><option value="analyze_incident">只读异常分析</option><option value="propose_monitor_plan">编制监控草稿</option><option value="summarize_result">汇总结论</option></select>')}${field('任务范围', `<textarea name="request" maxlength="4000" rows="4" required>${E(messageText(m))}</textarea>`)}${field('验收要求', '<textarea name="acceptance" maxlength="2000" rows="2" required>提交带证据的结论；恢复由独立探针验证。</textarea>')}<p class="cc-hint">共享本条来源消息及上述范围。不授予代码修改、Shell、部署或新的凭据权限。</p><label><input name="confirm" type="checkbox" required ${allowed ? '' : 'disabled'}> 我已核对执行者、来源消息与只读范围</label><button type="submit" class="btn primary" ${allowed ? '' : 'disabled'}>确认创建只读任务</button></form>`,
        element,
      );
      return local;
    }
    if (action === 'result') {
      const kind = element.dataset.recordKind || 'job';
      if (kind === 'message') return act('thread', element);
      const record = await readDrawer(kind, element.dataset.id, '任务、结果与证据', element);
      if (!record) return local;
      const results = record.results || (record.body ? [record] : []);
      const job = record.job || record;
      showDrawer(
        '任务、结果与证据',
        `<p>${badge(job.state)} ${E(text(job.delivery_status))}</p>${job.context?.origin_message_id ? button('source', '查看来源消息', { id: job.context.origin_message_id }) : ''}${results.map((r) => `<section class="cc-result"><h3>${E(text(r.body?.outcome))}</h3><p class="cc-prose">${E(r.body?.summary)}</p>${(r.body?.observations || []).map((o) => `<p>${E(o.claim)}</p>${(o.evidence_refs || []).map((id) => `<details data-cc-detail="evidence" data-result-id="${E(r.id)}" data-id="${E(id)}"><summary>查看授权证据</summary><div class="cc-detail"></div></details>`).join('')}`).join('')}${(r.body?.limitations || []).map((v) => `<p class="cc-hint">${E(v)}</p>`).join('')}</section>`).join('') || empty('尚无提交的结果；已领取与已完成分别记录。')}<details><summary>技术记录与诊断</summary>${json(record)}</details>`,
        element,
      );
      return local;
    }
    if (action === 'older-messages') {
      const generation = state.generation,
        c = chat(),
        feed = document.querySelector('.cc-feed'),
        top = feed.scrollTop,
        height = feed.scrollHeight;
      const page = await getRecord('timeline', '', c.older, {
        room_id: state.snapshot.room.id,
        limit: '100',
      });
      if (generation !== state.generation || c !== chat()) return local;
      mergeMessages(page.items || []);
      c.older = page.next_cursor || '';
      patchTimeline();
      feed.scrollTop = top + feed.scrollHeight - height;
      c.unread = 0;
      feed.querySelector('.cc-history').innerHTML = c.older
        ? button('older-messages', '加载更早消息')
        : '';
      document.querySelector('.cc-new-messages').hidden = true;
      return local;
    }
    if (action === 'retry-directory') {
      await recoverAccess();
      return local;
    }
    if (action === 'refresh') {
      state.notes = '';
      state.error = false;
      if (
        state.view === 'discussion' &&
        state.snapshot?.room &&
        state.snapshot?.capabilities?.plain_messages
      ) {
        await refreshChat();
        return { local: true, message: '状态已刷新。' };
      }
      const identity = [
        S.session,
        S.space_id,
        state.project,
        state.environment,
        state.conversation,
        state.view,
      ];
      const previousRoot = document.querySelector('.collaboration');
      const rendering = renderPage(false);
      const renderSequence = S.renderSeq,
        generation = state.generation;
      await rendering;
      const root = document.querySelector('.collaboration');
      const sameScope = [
        S.session,
        S.space_id,
        state.project,
        state.environment,
        state.conversation,
        state.view,
      ].every((value, index) => value === identity[index]);
      // renderPage can catch a failed read into an error page. Only a newly
      // committed collaboration view owned by this render may announce success.
      const feedback = root?.querySelector('#cc-feedback');
      if (
        sameScope &&
        S.page === 'collaboration' &&
        S.renderSeq === renderSequence &&
        state.generation === generation &&
        root !== previousRoot &&
        !root?.inert &&
        feedback &&
        (!state.snapshot?.room || state.snapshot.capabilities?.plain_messages)
      ) {
        state.notes = '状态已刷新。';
        state.error = false;
        feedback.textContent = state.notes;
        feedback.classList.remove('is-error');
      }
      return local;
    }
    if (action === 'create-room') return mutation('room', {});
    if (action.startsWith('join-')) {
      const slot = (state.snapshot.join_slots || []).find((row) => row.id === element.dataset.id);
      if (!slot) throw new Error('这个聊天位置已改变，请刷新状态。');
      if (['join-copy', 'join-select'].includes(action)) {
        const instruction = joinInstruction(slot);
        if (!instruction) {
          expireInvitations(element.closest('.collaboration'));
          throw new Error('当前指令不可用，请刷新并核对位置、授权与订阅状态。');
        }
        const area = element.closest('[data-cc-slot]').querySelector('.cc-join-instruction');
        area.focus();
        area.select();
        area.setSelectionRange(0, area.value.length);
        if (action === 'join-copy') {
          try {
            if (!navigator.clipboard?.writeText) throw new Error('clipboard unavailable');
            await navigator.clipboard.writeText(instruction);
            return {
              local: true,
              message: '已复制。请粘贴到这个位置对应的真实 dot / Work 聊天并发送。',
            };
          } catch {
            // Selection is already in place. Never request clipboard permission
            // or read the clipboard; the user can copy with the system controls.
          }
        }
        return { local: true, message: '已选中完整指令。请用系统复制，再粘贴到对应聊天并发送。' };
      }
      return mutation('join-slot-control', {
        slot_id: slot.id,
        expected_version: Number(element.dataset.version),
        action: action.slice(5),
      });
    }
    if (action === 'plan-template') {
      const form = document.querySelector('#cc-plan');
      const probe = form.elements.probe.value;
      if (!probe) throw new Error('先选择一个已登记的固定探针。');
      const candidate = {
        schema_version: 1,
        intent: '观察固定只读入口的可达性与恢复情况',
        valid_until: new Date(Date.now() + 7 * 86400000).toISOString(),
        interval_seconds: 60,
        assignee_agent_id: form.elements.monitor_assignee.value,
        rules: [
          {
            rule_id: 'http_availability',
            probe_id: probe,
            metric: 'availability',
            window_seconds: 300,
            min_samples: 3,
            open_when: { operator: 'lt', value: 0.8 },
            close_when: { operator: 'gt', value: 0.99 },
            open_consecutive_windows: 3,
            close_consecutive_windows: 5,
            sample_freshness_seconds: 180,
            severity: 'high',
            require_recovery_probe: form.elements.recovery.value || probe,
          },
        ],
      };
      state.draft.plan_json = JSON.stringify(candidate, null, 2);
      form.elements.plan_json.value = state.draft.plan_json;
      return { local: true, message: '已生成草稿；请审阅目标、阈值和期限后保存。' };
    }
    if (action === 'validate-plan') {
      const candidate = JSON.parse(document.querySelector('#cc-plan').elements.plan_json.value);
      await mutation('plan-validate', { candidate });
      return { local: true, message: '计划校验通过；尚未保存或激活。' };
    }
    if (action === 'activate-plan') {
      const consent = document.querySelector('#cc-approval');
      if (!consent?.checked || consent.dataset.digest !== element.dataset.digest)
        throw new Error('请先审阅并勾选这个不可变版本的批准范围。');
      return mutation('plan-activate', {
        plan_version: Number(element.dataset.planVersion),
        expected_version: Number(element.dataset.version),
        digest: element.dataset.digest,
      });
    }
    if (action === 'more') {
      const kind = element.dataset.kind;
      const generation = state.generation,
        snapshot = state.snapshot;
      const page = await getRecord(kind, '', snapshot[kind + '_next_cursor']);
      if (
        generation !== state.generation ||
        snapshot !== state.snapshot ||
        S.page !== 'collaboration'
      )
        return { local: true, message: '' };
      state.snapshot[kind].push(...page.items);
      state.snapshot[kind + '_next_cursor'] = page.next_cursor;
      const render = { jobs, messages: discussion, incidents: monitoring, subscriptions: agents }[
        kind
      ];
      document.querySelector('.cc-main').innerHTML = render();
      bindDetails();
      return { local: true, message: '已加载更多记录。' };
    }
    return mutation('control', {
      action,
      target_id: element.dataset.id,
      expected_version: Number(element.dataset.version),
      reason: '用户在协作面板明确确认：' + element.textContent.trim(),
    });
  }
  async function submit(form) {
    if (form.id.startsWith('cc-goal-')) return coordination.submit(form);
    const data = Object.fromEntries(new FormData(form));
    if (['cc-new-conversation', 'cc-add-project'].includes(form.id)) {
      if (!form.elements.confirm.checked) throw new Error('请先确认项目与房间范围。');
      const adding = form.id === 'cc-add-project',
        generation = state.generation;
      const selected = new FormData(form).getAll('projects');
      if (!adding && !selected.length) throw new Error('请选择至少一个项目。');
      const payload = adding
        ? {
            conversation_id: state.conversation,
            expected_version: Number(form.dataset.version),
            project: data.project,
            environment_id: data.environment,
          }
        : {
            title: data.title.trim(),
            projects: selected.map((project) => ({ project, environment_id: data.environment })),
          };
      const operation = adding ? 'conversation-project' : 'conversation';
      const identity = await commandIdentity({ operation, ...payload });
      if (generation !== state.generation) return { local: true };
      try {
        const result = await api('/api/collaboration/' + operation, {
          method: 'POST',
          retrySafe: true,
          body: JSON.stringify({ ...payload, idempotency_key: identity.identifier }),
        });
        if (generation !== state.generation) return { local: true };
        try {
          sessionStorage.removeItem(identity.storageKey);
        } catch {}
        state.requests.delete(identity.storageKey);
        const room = result.conversation || result;
        closeDrawer();
        rememberChat();
        if (!adding) {
          state.conversation = room.id;
          state.project = room.projects?.[0]?.project_id || selected[0];
          state.environment = room.projects?.[0]?.environment_id || data.environment;
          state.view = 'discussion';
          resetScope();
        }
        state.notes = adding
          ? '项目已加入当前房间。原消息、草稿与任务归属保持不变。'
          : '独立聊天室已创建。';
        persistSelection();
        await renderPage(false);
        return { local: true, message: state.notes };
      } catch (error) {
        if (generation !== state.generation) return { local: true };
        form.elements.confirm.checked = false;
        if (error.status === 409 || error.code === 'STALE_VERSION') {
          closeDrawer();
          state.notes = '房间项目已改变，请重新审阅后添加。';
          state.error = true;
          await renderPage(false);
          return { local: true, message: state.notes };
        }
        throw error;
      }
    }
    if (form.id === 'cc-scope') {
      if (state.project !== data.project || state.environment !== data.environment) resetScope();
      state.project = data.project;
      state.environment = data.environment;
      state.conversation = '';
      persistSelection();
      await renderPage(false);
      return { local: true, message: '' };
    }
    if (['cc-delegation-policy', 'cc-delegation-scope'].includes(form.id))
      return delegation.submit(form);
    if (form.id === 'cc-command') {
      if (!state.snapshot.capabilities?.plain_messages)
        throw new Error('当前服务尚未支持普通消息。');
      const c = chat(),
        generation = state.generation,
        session = S.session,
        space = S.space_id,
        room = state.snapshot.room.id;
      const payload = {
        ...chatScope(),
        room_id: room,
        body_text: data.request.trim(),
        reply_to_id: c.reply,
        mentions: c.mentions.map((slot_id) => ({ slot_id })),
      };
      if (!payload.body_text) throw new Error('先写一条消息。');
      if (c.delegationPolicy) {
        if (!selectedDelegationValid())
          throw new Error('委托授权、版本或所选助手已改变，请重新核对；正文已保留。');
        if (!delegation.selectedScopeValid()) {
          delegation.openScope(document.querySelector('#cc-message-input'));
          return { local: true, message: '请选择本次使用的目标与能力；正文已保留。' };
        }
        const acceptance = String(c.acceptance).trim();
        if (!acceptance) throw new Error('请填写本次委托的验收要求。');
        if (c.delegationAutomatic) {
          payload.dispatch_mode = 'automatic';
          payload.automatic_policy_version = c.delegationVersion;
        } else
          payload.delegation = {
            policy_id: c.delegationPolicy,
            policy_version: c.delegationVersion,
            execution_targets: [...c.delegationTargets],
            capabilities: [...c.delegationCapabilities],
            acceptance,
          };
      }
      const identity = await commandIdentity({ operation: 'message', ...payload });
      if (
        generation !== state.generation ||
        session !== S.session ||
        space !== S.space_id ||
        c !== chat()
      )
        throw new Error('登录或房间已改变；未发送旧消息。');
      const result = await api('/api/collaboration/message', {
        method: 'POST',
        retrySafe: true,
        body: JSON.stringify({
          ...payload,
          client_message_id: identity.identifier,
          idempotency_key: identity.identifier,
        }),
      });
      if (
        generation !== state.generation ||
        session !== S.session ||
        space !== S.space_id ||
        c !== chat()
      )
        return { local: true };
      try {
        sessionStorage.removeItem(identity.storageKey);
      } catch {}
      state.requests.delete(identity.storageKey);
      // A new draft typed while saving must not be erased by the older response.
      if (form.elements.request.value.trim() === payload.body_text) {
        c.draft = '';
        c.reply = '';
        if (!c.delegationAutomatic) {
          c.mentions = [];
          delegation.selectPolicy(null);
        }
        form.elements.request.value = '';
        patchDraftContext();
        patchDelegationComposer();
      }
      if (result.message?.id) {
        mergeMessages([result.message]);
        patchTimeline(true);
      }
      state.notes = '消息已保存，正在同步后续状态；请勿重发正文。';
      const feedback = document.querySelector('#cc-feedback');
      if (feedback) {
        feedback.textContent = state.notes;
        feedback.classList.remove('is-error');
      }
      let syncWarning = '';
      try {
        await refreshChat(true);
      } catch (error) {
        if (generation !== state.generation || c !== chat()) return { local: true };
        syncWarning = ' 消息已保存，后续状态刷新暂时失败；请勿重发正文。' + error.message;
      }
      const notifications = result.notifications || [];
      const records = Array.isArray(notifications) ? notifications : Object.values(notifications);
      const failed = records.some(
        (v) =>
          v &&
          typeof v === 'object' &&
          !['queued', 'pending', 'delivering', 'accepted'].includes(v.state || v.status),
      );
      return {
        local: true,
        message:
          (payload.delegation || payload.dispatch_mode === 'automatic'
            ? '委托已保存并创建目标；请查看实际领取、进度与结果，排队不表示已执行。'
            : payload.mentions.length
              ? `消息已保存。${failed ? '部分提醒未送达，可在连接中核对；请勿重发正文。' : '已按所选位置处理提醒；接收不代表已读或回应。'}`
              : '消息已保存，未创建任务。') + syncWarning,
      };
    }
    if (form.id === 'cc-task-review') {
      if (data.target_project !== state.project) throw new Error('请明确确认本次任务所属项目。');
      if (!form.elements.confirm.checked) throw new Error('请先核对并确认本次只读任务范围。');
      if (!eligibleWorkers().some((a) => a.id === data.assignee))
        throw new Error('这个执行者当前没有可用任务资格，请核对助手与连接。');
      const generation = state.generation;
      const result = await mutation('message-to-task', {
        room_id: state.snapshot.room.id,
        message_id: form.dataset.messageId,
        expected_message_version: Number(form.dataset.version),
        assignee_agent_id: data.assignee,
        kind: data.kind,
        request: data.request,
        acceptance: data.acceptance,
      });
      if (generation !== state.generation) return { local: true };
      closeDrawer();
      await refreshChat();
      return {
        local: true,
        message: '已按确认范围创建只读任务。' + text(result.delivery_status || result.state),
      };
    }
    if (form.id === 'cc-message-access') {
      if (!form.elements.confirm.checked) throw new Error('请先确认连接和房间范围。');
      const generation = state.generation;
      try {
        const result = await mutation('message-access', {
          room_id: state.snapshot.room.id,
          grant_id: form.dataset.grant,
          expected_version: Number(form.dataset.version),
          enabled: form.dataset.enabled === 'true',
          expires_in_days: 7,
        });
        if (generation !== state.generation) return { local: true };
        closeDrawer();
        return result;
      } catch (error) {
        if (generation !== state.generation) return { local: true };
        form.elements.confirm.checked = false;
        closeDrawer();
        state.notes = error.message + '。请重新审阅当前连接权限后确认。';
        state.error = true;
        await renderPage(false);
        return { local: true, message: state.notes };
      }
    }
    if (form.id === 'cc-search') {
      const generation = state.generation;
      const result = await getRecord('search', '', '', {
        query: data.query,
        room_id: state.snapshot.room.id,
      });
      if (generation === state.generation && form.isConnected)
        form.nextElementSibling.innerHTML =
          (result.items || [])
            .map(
              (m) =>
                `<article class="cc-card"><p class="cc-prose">${E(messageText(m))}</p>${button('source', '查看消息', m)}</article>`,
            )
            .join('') || empty('没有匹配的房间消息。');
      return { local: true, message: '' };
    }
    if (form.id === 'cc-join-slot') {
      const generation = state.generation,
        session = S.session,
        space = S.space_id,
        room = state.snapshot.room.id;
      const payload = {
        ...scope(),
        label: data.label.trim(),
        kind: data.kind,
        code_ttl_minutes: 30,
        expires_in_days: 7,
      };
      const current = () =>
        generation === state.generation &&
        S.session === session &&
        S.space_id === space &&
        state.project === payload.project &&
        state.environment === payload.environment_id &&
        state.snapshot?.room?.id === room;
      const identity = await commandIdentity({ operation: 'join-slot', ...payload });
      if (!current()) throw new Error('登录或项目范围已改变；未创建旧聊天位置。');
      const result = await api('/api/collaboration/join-slot', {
        method: 'POST',
        retrySafe: true,
        body: JSON.stringify({ ...payload, idempotency_key: identity.identifier }),
      });
      // Keep only the random request ID across reload while the outcome is
      // unknown. A result on an older page cannot clear a newer page's intent.
      if (current()) {
        try {
          sessionStorage.removeItem(identity.storageKey);
        } catch {}
        state.requests.delete(identity.storageKey);
        state.joinDraft = {};
      }
      return result;
    }
    if (form.classList.contains('cc-join-confirm')) {
      if (!form.elements.confirmed_received.checked)
        throw new Error('请先在对应聊天核对全部当前测试事件，再勾选确认。');
      return mutation('join-slot-control', {
        slot_id: form.dataset.slotId,
        expected_version: Number(form.dataset.version),
        action: 'confirm_chat',
        confirmed_received: true,
        test_event_ids: JSON.parse(form.dataset.events),
      });
    }
    if (form.id === 'cc-agent') return mutation('agent', data);
    if (form.id === 'cc-probe')
      return mutation('probe', {
        label: data.label,
        url: data.url,
        allow_http: form.elements.allow_http.checked,
        private_networks: data.networks.split(/\s+/).filter(Boolean),
      });
    if (form.id === 'cc-plan')
      return mutation('plan-save', {
        candidate: JSON.parse(data.plan_json),
        expected_version: state.snapshot.plan?.latest_version || 0,
      });
    throw new Error('未知的协作表单。');
  }
  function busyActions() {
    const root = document.querySelector('.collaboration');
    if (!root) return () => {};
    const previous = new Map();
    let released = false;
    const navigation =
      '[data-cc-room], [data-cc-partition], [data-cc-view], [data-cc-open-joins], [data-cc-action="close-drawer"]';
    const mark = () => {
      for (const node of root.querySelectorAll('button, input[type="submit"]')) {
        if (node.matches(navigation) || previous.has(node)) continue;
        previous.set(node, node.getAttribute('aria-disabled'));
        node.setAttribute('aria-disabled', 'true');
        node.setAttribute('data-cc-request-busy', '');
      }
    };
    root.setAttribute('aria-busy', 'true');
    mark();
    const observer = new MutationObserver(mark);
    observer.observe(root, { childList: true, subtree: true });
    return () => {
      if (released) return;
      released = true;
      observer.disconnect();
      for (const [node, value] of previous) {
        if (value === null) node.removeAttribute('aria-disabled');
        else node.setAttribute('aria-disabled', value);
        node.removeAttribute('data-cc-request-busy');
      }
      // A detached render owns the stronger inert state until replacement.
      if (!root.inert) root.removeAttribute('aria-busy');
    };
  }
  async function readDrawer(kind, id, title, trigger) {
    showDrawer(title, empty('正在读取当前授权记录…'), trigger);
    const generation = state.generation,
      dismissed = state.drawerEpoch;
    const current = () => generation === state.generation && dismissed === state.drawerEpoch;
    try {
      const result = await getRecord(kind, id);
      return current() ? result : null;
    } catch (error) {
      if (current()) showDrawer(title, empty(error.message), trigger);
      throw error;
    }
  }
  async function run(operation, trigger) {
    if (state.busy) return;
    const token = {},
      generation = state.generation,
      session = S.session,
      space = S.space_id,
      project = state.project,
      environment = state.environment;
    const current = () =>
      state.busy === token &&
      state.generation === generation &&
      S.session === session &&
      S.space_id === space &&
      state.project === project &&
      state.environment === environment &&
      S.page === 'collaboration';
    state.busy = token;
    state.refreshSequence++;
    const releaseBusy = busyActions();
    state.busyCleanup = releaseBusy;
    try {
      const result = await operation();
      if (!current()) return;
      state.notes =
        typeof result?.message === 'string'
          ? result.message
          : result?.job_id
            ? '指令已保存。' + text(result.delivery_status || result.state) + '。'
            : '操作已保存，请以当前状态为准。';
      state.error = false;
      if (!result?.local) await renderPage(false);
    } catch (error) {
      if (!current()) return;
      state.notes = error.message;
      state.error = true;
    } finally {
      const stillCurrent = current();
      if (state.busy === token) state.busy = false;
      releaseBusy();
      if (state.busyCleanup === releaseBusy) state.busyCleanup = null;
      const feedback = document.querySelector('#cc-feedback');
      if (feedback && stillCurrent) {
        feedback.textContent = state.notes;
        feedback.classList.toggle('is-error', state.error);
      }
    }
  }
  function bindDetails() {
    document.querySelectorAll('[data-cc-detail]').forEach((details) => {
      if (details.dataset.bound) return;
      details.dataset.bound = 'true';
      details.addEventListener('toggle', async () => {
        if (!details.open || details.dataset.loaded) return;
        const generation = state.generation;
        const target = details.querySelector('.cc-detail');
        target.textContent = '正在读取…';
        try {
          const record = await getRecord(details.dataset.ccDetail, details.dataset.id, '', {
            ...(details.dataset.projectDiagnostic === 'true' ? { conversation_id: '' } : {}),
            ...(details.dataset.resultId ? { result_id: details.dataset.resultId } : {}),
            ...(details.dataset.jobId ? { job_id: details.dataset.jobId } : {}),
          });
          if (generation !== state.generation || !details.isConnected) return;
          target.innerHTML =
            (record.results || [])
              .map(
                (result) =>
                  `<section class="cc-result"><strong>${E(text(result.body.outcome))}</strong><p class="cc-prose">${E(result.body.summary)}</p></section>`,
              )
              .join('') + json(record.body || record);
          details.dataset.loaded = 'true';
        } catch (error) {
          if (details.isConnected) target.textContent = error.message;
        }
      });
    });
  }
  function composerChanged(event) {
    delegation.change(event);
    if (event.target.form?.id !== 'cc-command') return;
    const c = chat();
    if (event.target.name === 'request') c.draft = event.target.value;
    if (event.target.name === 'delegation_acceptance') c.acceptance = event.target.value;
    if (event.target.name === 'delegation_policy' && event.type === 'change') {
      const automatic = event.target.value.startsWith('auto:');
      const policyId = automatic ? event.target.value.slice(5) : event.target.value;
      const policy = state.delegationPolicies.find((item) => item.id === policyId);
      delegation.selectPolicy(
        policy,
        Number(event.target.selectedOptions[0]?.dataset.policyVersion) || 0,
        automatic,
      );
      patchDelegationComposer();
      if (selectedDelegationValid() && !delegation.selectedScopeValid())
        delegation.openScope(event.target);
    }
  }
  function bind() {
    const root = document.querySelector('.collaboration');
    if (!root) return;
    root.inert = false;
    root.removeAttribute('aria-busy');
    const boundGeneration = state.generation;
    const live = () => root.isConnected && !root.inert && boundGeneration === state.generation;
    const viewport = window.visualViewport;
    if (viewport) {
      const resize = () => root.style.setProperty('--cc-viewport-height', viewport.height + 'px');
      viewport.addEventListener('resize', resize);
      resize();
      state.viewportCleanup = () => viewport.removeEventListener('resize', resize);
    }
    root.addEventListener('click', (event) => {
      if (!live() || event.target.closest('button')?.disabled) return;
      const partition = event.target.closest('[data-cc-partition]');
      if (partition) {
        selectPartition(partition.dataset.ccPartition, partition.dataset.environment);
        return;
      }
      const room = event.target.closest('[data-cc-room]');
      if (room) {
        rememberChat();
        disableComposer();
        resetScope();
        state.conversation = room.dataset.ccRoom;
        state.project = room.dataset.project;
        state.environment = room.dataset.environment;
        state.view = 'discussion';
        persistSelection();
        renderPage(false);
        return;
      }
      const tab = event.target.closest('[data-cc-view], [data-cc-open-joins]');
      if (tab) {
        const selected = tab.dataset.ccView || 'agents';
        rememberChat();
        state.view = selected;
        const rendering = renderPage(false);
        const renderSequence = S.renderSeq,
          generation = state.generation,
          session = S.session,
          space = S.space_id;
        rendering.then(() => {
          const next = document.querySelector('.collaboration'),
            focused = document.activeElement;
          if (
            S.page === 'collaboration' &&
            state.view === selected &&
            S.renderSeq === renderSequence &&
            state.generation === generation &&
            S.session === session &&
            S.space_id === space &&
            next &&
            next !== root &&
            !next.inert &&
            (focused === document.body || focused === next || focused === next.parentElement)
          )
            next.querySelector('.cc-tabs [aria-pressed="true"]')?.focus({ preventScroll: true });
        });
        return;
      }
      const button = event.target.closest('[data-cc-action]');
      if (button) {
        if (button.dataset.ccAction === 'close-drawer') {
          closeDrawer();
          return;
        }
        const menu = button.closest('.cc-room-menu');
        if (menu) menu.open = false;
        run(() => act(button.dataset.ccAction, button), button);
      }
    });
    root.addEventListener('submit', (event) => {
      event.preventDefault();
      if (!live()) return;
      if (event.target.checkValidity()) run(() => submit(event.target), event.submitter);
    });
    root.addEventListener('input', (event) => {
      if (!live()) return;
      coordination.change(event);
      if (event.target.form?.id === 'cc-join-slot' && event.target.name)
        state.joinDraft[event.target.name] = event.target.value;
      composerChanged(event);
      if (event.target.form?.id === 'cc-plan' && event.target.name)
        state.draft[event.target.name] = event.target.value;
    });
    root.addEventListener('change', (event) => {
      if (!live()) return;
      coordination.change(event);
      if (event.target.form?.id === 'cc-join-slot' && event.target.name)
        state.joinDraft[event.target.name] = event.target.value;
      composerChanged(event);
      if (event.target.form?.id === 'cc-plan' && event.target.name)
        state.draft[event.target.name] = event.target.value;
    });
    root.addEventListener('compositionstart', () => {
      if (!live()) return;
      state.composing = true;
    });
    root.addEventListener('compositionend', () => {
      if (!live()) return;
      state.composing = false;
    });
    root.addEventListener('keydown', (event) => {
      if (!live()) return;
      if (event.target.id !== 'cc-message-input') return;
      if (
        event.key === 'Enter' &&
        !event.shiftKey &&
        !event.isComposing &&
        !state.composing &&
        event.keyCode !== 229 &&
        !matchMedia('(pointer: coarse), (max-width: 720px)').matches
      ) {
        event.preventDefault();
        event.target.form.requestSubmit();
      }
    });
    const dialog = root.querySelector('#cc-drawer');
    dialog?.addEventListener('close', () => {
      if (!live() || dialog.open) return;
      const epoch = ++state.drawerEpoch,
        target = state.drawerFocus,
        focused = document.activeElement;
      // Native close may already have restored focus. Never override a newer
      // user choice made before the close event or its animation frame runs.
      if (
        focused !== document.body &&
        focused !== root &&
        focused !== root.parentElement &&
        focused !== target &&
        !dialog.contains(focused)
      )
        return;
      requestAnimationFrame(() => {
        if (
          live() &&
          epoch === state.drawerEpoch &&
          !dialog.open &&
          target?.isConnected &&
          document.activeElement === focused
        )
          target.focus({ preventScroll: true });
      });
    });
    dialog?.addEventListener('cancel', () => {
      if (!live()) return;
      state.drawerEpoch++;
    });
    const feed = root.querySelector('.cc-feed'),
      area = root.querySelector('#cc-message-input');
    if (feed) {
      const c = chat();
      for (const node of feed.querySelectorAll('[data-message-id]')) {
        const m = messageById(node.dataset.messageId);
        if (m) node._ccMarkup = messageMarkup(m);
      }
      feed.scrollTop = c.scroll ?? feed.scrollHeight;
      feed.addEventListener('scroll', () => {
        c.scroll = feed.scrollTop;
        if (feed.scrollHeight - feed.scrollTop - feed.clientHeight < 70) {
          c.unread = 0;
          root.querySelector('.cc-new-messages').hidden = true;
          markVisibleRead();
        }
      });
      if (c.focused && area && !area.disabled) {
        area.focus({ preventScroll: true });
        if (c.selection) area.setSelectionRange(...c.selection);
      }
    }
    bindDetails();
    expireInvitations(root);
    if (state.snapshot?.room && state.snapshot.capabilities?.plain_messages) {
      const generation = state.generation;
      const poll = async () => {
        if (generation !== state.generation || S.page !== 'collaboration') return;
        try {
          if (!document.hidden && !state.busy) {
            await refreshChat();
          }
        } catch (error) {
          if (generation === state.generation) {
            const feedback = root.querySelector('#cc-feedback');
            if (feedback) {
              feedback.textContent = '消息刷新暂时失败，草稿已保留。' + error.message;
              feedback.classList.add('is-error');
            }
          }
        }
        if (generation === state.generation) state.timer = setTimeout(poll, 5000);
      };
      state.timer = setTimeout(poll, 5000);
    }
  }

  function open(project) {
    rememberChat();
    if (state.project !== project) resetScope();
    state.project = project;
    state.conversation = '';
    persistSelection();
    if (S.page === 'collaboration') return renderPage();
    return navigate('collaboration');
  }
  return { html, bind, detach, clear, open };
})();
