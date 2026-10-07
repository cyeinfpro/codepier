'use strict';
// Shared project records only. No private chat history, tokens or callback URLs.
window.CodePierCollaboration = (() => {
  const state = {
    session: null,
    space: null,
    project: '',
    environment: 'production',
    view: 'discussion',
    snapshot: null,
    grants: [],
    draft: {},
    notes: '',
    error: false,
    timer: null,
    controller: null,
    generation: 0,
    busy: false,
    requests: new Map(),
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
  };
  const E = (value) => CP.ui.esc(value == null ? '' : String(value));
  const text = (value) => labels[value] || value || '暂无记录';
  const when = (value) =>
    value ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }) : '暂无';
  const scope = () => ({ project: state.project, environment_id: state.environment });
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

  function detach() {
    state.generation++;
    clearTimeout(state.timer);
    state.timer = null;
    state.controller?.abort();
    state.controller = null;
  }
  function resetScope() {
    state.snapshot = null;
    state.draft = {};
    state.notes = '';
  }
  async function getRecord(kind, id, cursor = '') {
    const query = new URLSearchParams({ ...scope(), kind, id, cursor });
    return api('/api/collaboration?' + query, { signal: state.controller?.signal });
  }
  async function mutation(operation, args) {
    const payload = { ...scope(), ...args };
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
    return result;
  }
  async function commandIdentity(payload) {
    // Persist only random request IDs, never draft prose or credentials. The
    // digest binds retries to this login and exact command across a page reload.
    const source = JSON.stringify([S.session?.csrf, S.space_id, payload]);
    const tag = crypto.subtle
      ? Array.from(
          new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(source))),
          (byte) => byte.toString(16).padStart(2, '0'),
        ).join('')
      : window.sha256(source);
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

  function discussion() {
    const d = state.snapshot;
    const feed = d.messages
      .map((message) => {
        const command = message.body.command;
        const summary = command?.request || message.body.summary || '';
        const author =
          message.kind === 'owner_command'
            ? '用户指令'
            : message.kind === 'agent_proposal'
              ? '智能体建议'
              : '分析结果';
        return `<article class="cc-message"><div class="cc-row"><strong>${author}</strong>${badge(message.state)}</div><p class="cc-prose">${E(summary)}</p><small>${E(when(message.created))}</small>${message.state === 'awaiting_approval' && d.can_manage ? `<div class="cc-actions">${button('accept_proposal', '批准只读任务', message)}${button('reject_proposal', '不批准', message)}</div>` : ''}</article>`;
      })
      .join('');
    return `<section class="cc-conversation"><div class="cc-feed" aria-label="共享讨论记录">${feed || empty('在这里给已登记的智能体下达一条明确指令。这里只共享项目记录，不同步私人聊天。')}${d.messages_next_cursor ? button('more', '加载更早记录', {}, 'data-kind="messages"') : ''}</div>${
      d.can_manage
        ? `<form id="cc-command" class="cc-composer">
      <div class="cc-two">${field('@ 收件智能体', `<select name="assignee" required>${agentOptions(null, state.draft.assignee, '选择真实用途绑定')}</select>`)}${field('任务类型', `<select name="kind"><option value="analyze_incident" ${state.draft.kind === 'analyze_incident' ? 'selected' : ''}>只读异常分析</option><option value="propose_monitor_plan" ${state.draft.kind === 'propose_monitor_plan' ? 'selected' : ''}>编制监控草稿</option><option value="summarize_result" ${state.draft.kind === 'summarize_result' ? 'selected' : ''}>汇总结论</option></select>`)}</div>
      ${field('指令', `<textarea name="request" rows="4" maxlength="4000" required placeholder="例如：检查本次超时异常，列出证据和需要进一步确认的问题。">${E(state.draft.request)}</textarea>`)}
      <div class="cc-two">${field('明确的下一步', `<select name="next_assignee">${agentOptions('dot', state.draft.next_assignee, '不自动交接')}</select>`)}${field('验收要求', `<input name="acceptance" maxlength="2000" value="${E(state.draft.acceptance || '提交带证据的结论；恢复由独立探针验证。')}">`)}</div>
      <div class="cc-row"><small>文本里的 @ 不派发任务。发送不授予代码修改、Shell 或部署权限。</small><button class="btn primary" type="submit">保存并派发</button></div></form>`
        : empty('当前账号可以查看获准记录，管理指令需要空间管理员确认。')
    }</section>`;
  }
  function jobs() {
    const d = state.snapshot;
    return `<section class="cc-stack">${d.jobs.map((job) => `<article class="cc-card"><div class="cc-row"><strong>${E(job.kind === 'summarize_result' ? '结果汇总' : job.kind === 'propose_monitor_plan' ? '监控计划草稿' : '只读异常分析')}</strong>${badge(job.state)}</div><p>${E(text(job.delivery_status))}${job.reason_code ? ` · ${E(job.reason_code)}` : ''}</p><dl class="cc-facts"><div><dt>任务</dt><dd>${E(job.id.slice(-8))}</dd></div><div><dt>领取轮次</dt><dd>${job.attempt} / 3</dd></div><div><dt>期限</dt><dd>${E(when(job.deadline_at))}</dd></div></dl><details data-cc-detail="job" data-id="${E(job.id)}"><summary>上下文、结果与执行记录</summary><div class="cc-detail">展开后读取当前记录</div></details>${d.can_manage && !['succeeded', 'failed', 'cancelled', 'expired', 'dead_letter'].includes(job.state) ? `<div class="cc-actions">${['blocked', 'retry_wait'].includes(job.state) ? button('retry', '在原期限内解除阻塞', job) : ''}${button('cancel', '取消只读任务', job)}</div>` : ''}</article>`).join('') || empty('尚未创建任务。任务是否完成与业务是否恢复会分别显示。')}${d.jobs_next_cursor ? button('more', '加载更多任务', {}, 'data-kind="jobs"') : ''}</section>`;
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
          `<article class="cc-card"><div class="cc-row"><strong>${E(incident.rule_id)} · 第 ${incident.episode} 次发生</strong>${badge(incident.state)}</div><p>${E(incident.severity)} · ${E(text(incident.analysis_state))}${incident.suppressed ? ' · 维护期仅抑制通知' : ''}</p><small>最近证据 ${E(when(incident.updated))} · 计划 v${incident.plan_version}</small><details data-cc-detail="evidence" data-id="${E(incident.evidence_id)}"><summary>查看脱敏探针证据</summary><div class="cc-detail">展开后读取当前授权证据</div></details></article>`,
      )
      .join('');
    return `<section class="cc-stack">${current}${incidents || empty('暂无已确认异常；这不等于全部业务均健康。')}${d.incidents_next_cursor ? button('more', '加载更多异常', {}, 'data-kind="incidents"') : ''}${d.can_manage ? `<details class="cc-card"><summary>登记固定只读探针</summary><form id="cc-probe" class="cc-form">${field('名称', '<input name="label" maxlength="80" required>')}${field('固定 GET 地址', '<input name="url" type="url" maxlength="2048" placeholder="https://service.example/ready" required>')}<details><summary>受限内部服务配置</summary>${field('允许的私网 CIDR，每行一个', '<textarea name="networks" rows="2" placeholder="仅填写明确允许此探针访问的网段"></textarea>')}<label><input name="allow_http" type="checkbox"> 此固定内部目标允许明文 HTTP</label></details><label><input type="checkbox" required> 确认该 GET 不修改业务数据，地址不含凭据或客户内容</label><button type="submit" class="btn">登记探针，不启动采集</button></form></details><details class="cc-card"><summary>新建或修订监控草稿</summary><form id="cc-plan" class="cc-form"><div class="cc-two">${field('入口探针', `<select name="probe">${options(probes, state.draft.probe)}</select>`)}${field('独立恢复探针', `<select name="recovery">${options(probes, state.draft.recovery, '使用同一个固定探针')}</select>`)}</div>${field('异常分析对象', `<select name="monitor_assignee">${agentOptions('work_cloud', state.draft.monitor_assignee, '不自动创建分析任务')}</select>`)}${button('plan-template', '生成可审阅的初始配置')}${field('计划 JSON', `<textarea name="plan_json" rows="12" spellcheck="false" required placeholder="先生成配置，或粘贴需要审阅的声明式计划。">${E(state.draft.plan_json)}</textarea>`)}<small>初始配置只代表合成探针，不是全部用户请求的成功率。可以调整后先校验，再保存版本。</small><div class="cc-actions">${button('validate-plan', '只校验')}<button type="submit" class="btn">保存新草稿，不激活</button></div></form></details>` : ''}</section>`;
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
    return `<section class="cc-stack"><p class="cc-hint">登记、订阅有效和最近响应是三件事。同一连接中的聊天不能仅凭名字彼此隔离。</p>${d.agents.map((agent) => `<article class="cc-card"><div class="cc-row"><strong>${E(agent.label)} <small>${agent.kind === 'dot' ? 'dot' : 'Work Cloud'} · ${E(agent.id.slice(-6))}</small></strong>${badge(agent.binding_status)}</div><dl class="cc-facts"><div><dt>有效订阅</dt><dd>${agent.valid_subscriptions}</dd></div><div><dt>最近领取</dt><dd>${E(when(agent.last_claim))}</dd></div><div><dt>最近结果</dt><dd>${E(when(agent.last_result))}</dd></div></dl><small>用途绑定到期：${E(when(agent.expires_at))} · 原始聊天身份未做密码学验证</small>${d.can_manage ? `<div class="cc-actions">${button(agent.enabled ? 'agent_disable' : 'agent_enable', agent.enabled ? '停用此用途' : '启用此用途', agent)}${button('agent_renew', '确认续期 7 天', agent)}</div>` : ''}</article>`).join('') || empty('先在目标 Work 或 dot 聊天连接一个只读 CodePier 连接，再登记其用途。')}${d.can_manage ? `<details class="cc-card"><summary>登记智能体用途</summary><form id="cc-agent" class="cc-form"><div class="cc-two">${field('显示名称', '<input name="label" maxlength="80" required>')}${field('类型', '<select name="kind"><option value="work_cloud">Work Cloud · 只读分析</option><option value="dot">dot · 协调与汇总</option></select>')}</div>${field('现有只读连接', `<select name="grant_id" required>${options(available, '', '选择现有连接')}</select>`)}<small>这里只绑定现有授权，不创建凭据或扩大权限。没有候选连接时，请在 MCP 接入中创建项目限定的只读连接。</small><button class="btn" type="submit">确认登记 7 天</button></form></details>` : ''}<h2>事件订阅</h2>${d.subscriptions.map((sub) => `<article class="cc-card"><div class="cc-row"><strong>${E(sub.name.replace('codepier.', ''))}</strong>${badge(sub.state)}</div><p>${E(sub.arguments.queue || '项目结果与状态')} · ${E(sub.id.slice(-8))}</p><dl class="cc-facts"><div><dt>到期</dt><dd>${E(when(sub.expires_at))}</dd></div><div><dt>最近接收</dt><dd>${E(when(sub.last_accepted))}</dd></div></dl><small>接收回执不代表已领取或用户已读。回调地址与签名材料不在面板显示。</small>${d.can_manage ? `<div class="cc-actions">${button('subscription_test', '发送无敏感测试事件', sub)}${button(sub.state === 'paused' ? 'subscription_resume' : 'subscription_pause', sub.state === 'paused' ? '恢复投递' : '暂停投递', sub)}</div>` : ''}</article>`).join('') || empty('订阅必须由对应的 Work / dot 聊天发起。面板不会代替用户创建或控制聊天。')}${d.subscriptions_next_cursor ? button('more', '加载更多订阅', {}, 'data-kind="subscriptions"') : ''}</section>`;
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
      resetScope();
    }
    state.controller = new AbortController();
    const gen = state.generation;
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
    if (gen !== state.generation) return '';
    state.snapshot = data;
    state.grants = grants || [];
    const selection = `<form id="cc-scope" class="cc-scope">${field(
      '项目',
      `<select name="project">${options(
        S.projects.map((project) => [project.id, project.alias || project.id]),
        state.project,
      )}</select>`,
    )}${field('环境', `<input name="environment" value="${E(state.environment)}" pattern="[A-Za-z0-9][A-Za-z0-9_.-]*" maxlength="64" required>`)}<button class="btn" type="submit">切换</button>${button('refresh', '刷新状态')}${data.room && data.can_manage ? button(data.room.state === 'paused' ? 'resume' : 'pause', data.room.state === 'paused' ? '恢复协作室' : '暂停协作室', data.room) : ''}</form>`;
    const feedback = `<p id="cc-feedback" class="cc-feedback ${state.error ? 'is-error' : ''}" role="status" aria-live="polite">${E(state.notes)}</p>`;
    if (!data.room)
      return `<div class="collaboration">${header}${selection}${feedback}<section class="cc-card cc-intro"><h2>开启这个项目的共享讨论室</h2><p>创建讨论室只保存协作记录，不代表授权采集、添加订阅或执行代码。</p>${data.can_manage ? button('create-room', '创建项目讨论室') : empty('请由项目所属空间的管理员创建讨论室。')}</section></div>`;
    const tabs = [
      ['discussion', '讨论'],
      ['jobs', '任务'],
      ['monitor', '监控'],
      ['agents', '智能体与订阅'],
    ];
    const body = { discussion, jobs, monitor: monitoring, agents }[state.view]();
    return `<div class="collaboration">${header}${selection}${feedback}<div class="cc-summary"><span>${data.counts.open_jobs} 个未结束任务</span><span>${data.counts.open_incidents} 个未恢复异常</span><span>${data.counts.pending_proposals} 条待批准建议</span>${badge(data.room.state)}</div><nav class="cc-tabs" aria-label="协作中心视图">${tabs.map(([id, label]) => `<button class="btn ${id === state.view ? 'is-active' : ''}" data-cc-view="${id}" aria-pressed="${id === state.view}">${label}</button>`).join('')}</nav><div class="cc-layout"><div class="cc-main">${body}</div>${sidebar()}</div></div>`;
  }

  async function act(action, element) {
    if (action === 'refresh') {
      await renderPage(false);
      return { local: true, message: '状态已刷新。' };
    }
    if (action === 'create-room') return mutation('room', {});
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
    const data = Object.fromEntries(new FormData(form));
    if (form.id === 'cc-scope') {
      if (state.project !== data.project || state.environment !== data.environment) resetScope();
      state.project = data.project;
      state.environment = data.environment;
      await renderPage(false);
      return { local: true, message: '' };
    }
    if (form.id === 'cc-command') {
      const session = S.session,
        space = S.space_id,
        room = state.snapshot.room.id;
      const payload = {
        ...scope(),
        room_id: state.snapshot.room.id,
        structured_mentions: [{ agent_id: data.assignee }],
        kind: data.kind,
        request: data.request,
        acceptance: data.acceptance,
        next_assignee_agent_id: data.next_assignee,
      };
      const identity = await commandIdentity(payload);
      if (S.session !== session || S.space_id !== space || state.snapshot?.room?.id !== room)
        throw new Error('登录或项目范围已改变；未发送旧指令。');
      const result = await api('/api/collaboration/command', {
        method: 'POST',
        retrySafe: true,
        body: JSON.stringify({
          ...payload,
          source_message_id: identity.identifier,
          idempotency_key: identity.identifier,
        }),
      });
      try {
        sessionStorage.removeItem(identity.storageKey);
      } catch {}
      state.requests.delete(identity.storageKey);
      if (S.session === session && S.space_id === space && state.snapshot?.room?.id === room)
        state.draft.request = '';
      return result;
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
  async function run(operation, trigger) {
    if (state.busy) return;
    const session = S.session,
      space = S.space_id,
      project = state.project,
      environment = state.environment;
    const current = () =>
      S.session === session &&
      S.space_id === space &&
      state.project === project &&
      state.environment === environment &&
      S.page === 'collaboration';
    state.busy = true;
    trigger?.setAttribute('disabled', '');
    try {
      const result = await operation();
      if (!current()) return;
      state.notes =
        result?.message ||
        (result?.job_id
          ? '指令已保存。' + text(result.delivery_status || result.state) + '。'
          : '操作已保存，请以当前状态为准。');
      state.error = false;
      if (!result?.local) await renderPage(false);
    } catch (error) {
      if (!current()) return;
      state.notes = error.message;
      state.error = true;
    } finally {
      state.busy = false;
      trigger?.removeAttribute('disabled');
      const feedback = document.querySelector('#cc-feedback');
      if (feedback && current()) {
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
          const record = await getRecord(details.dataset.ccDetail, details.dataset.id);
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
  function bind() {
    const root = document.querySelector('.collaboration');
    if (!root) return;
    root.addEventListener('click', (event) => {
      const tab = event.target.closest('[data-cc-view]');
      if (tab) {
        if (state.busy) return;
        const selected = tab.dataset.ccView;
        state.view = selected;
        renderPage(false).then(() => {
          if (S.page === 'collaboration' && state.view === selected)
            document
              .querySelector('.cc-tabs [aria-pressed="true"]')
              ?.focus({ preventScroll: true });
        });
        return;
      }
      const button = event.target.closest('[data-cc-action]');
      if (button) run(() => act(button.dataset.ccAction, button), button);
    });
    root.addEventListener('submit', (event) => {
      event.preventDefault();
      if (event.target.checkValidity()) run(() => submit(event.target), event.submitter);
    });
    root.addEventListener('input', (event) => {
      if (['cc-command', 'cc-plan'].includes(event.target.form?.id) && event.target.name)
        state.draft[event.target.name] = event.target.value;
    });
    root.addEventListener('change', (event) => {
      if (['cc-command', 'cc-plan'].includes(event.target.form?.id) && event.target.name)
        state.draft[event.target.name] = event.target.value;
    });
    bindDetails();
    if (state.snapshot?.room) {
      const generation = state.generation;
      const poll = async () => {
        if (generation !== state.generation || S.page !== 'collaboration') return;
        // Never replace an edited form, an expanded evidence packet or a focused
        // control while refreshing. Explicit refresh is always available.
        const active =
          root.contains(document.activeElement) ||
          root.querySelector('details[open]') ||
          state.busy;
        if (!document.hidden && !active) await renderPage(false);
        else state.timer = setTimeout(poll, 10000);
      };
      state.timer = setTimeout(poll, 10000);
    }
  }
  function open(project) {
    if (state.project !== project) resetScope();
    state.project = project;
    if (S.page === 'collaboration') return renderPage();
    return navigate('collaboration');
  }
  return { html, bind, detach, open };
})();
