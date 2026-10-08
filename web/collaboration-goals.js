'use strict';
// Goal proposals do not create credentials, subscriptions or execution authority.
window.CodePierCollaborationGoals = {
  create(ctx) {
    const {
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
    } = ctx;
    let items = [],
      options = null,
      detail = null,
      revision = 0,
      loading = 0,
      projection = '';
    const names = {
      proposed: '讨论与规划 · 尚未启用执行',
      active: '执行范围已批准',
      paused: '已暂停',
      cancelled: '已取消',
      expired: '已到期',
      blocked: '等待处理',
      queued: '等待领取',
      leased: '已领取',
      running: '正在处理',
      succeeded: '已提交结果',
      completed: '已完成',
      failed: '失败',
      pending: '等待依赖',
      awaiting_review: '等待复核',
    };
    const capNames = { read: '读取项目', write: '修改项目文件', execute: '运行命令 / Shell' };
    const local = (message = '') => ({ local: true, message });
    const status = (value) => names[value] || value || '暂无记录';
    const badge = (value) => '<span class="cc-status">' + E(status(value)) + '</span>';
    const canManage = () => !!state.snapshot?.can_manage;
    const canApprove = () => canManage() && options?.can_approve === true;
    const enabled = () => !!state.snapshot?.capabilities?.coordination_goals;
    const connection = (id) => options?.participants?.find((p) => p.grant_id === id);
    const connectionName = (id) =>
      (connection(id)?.label || '助手连接') + ' · ' + String(id).slice(-8);
    const goalById = (id) =>
      detail?.goal?.id === id ? detail.goal : items.find((g) => g.id === id);
    const goalScope = (id) => {
      const g = goalById(id);
      if (!g) throw new Error('目标记录已改变，请重新读取。');
      return {
        project: g.anchor_project_id || state.project,
        environment_id: g.environment_id || state.environment,
      };
    };
    const mutateGoal = (operation, args) =>
      mutation(operation, {
        ...(args.goal_id ? goalScope(args.goal_id) : {}),
        ...args,
      });
    const token = () => [
      state.generation,
      revision,
      state.project,
      state.conversation,
      state.session,
      state.space,
    ];
    const current = (t) => t.every((v, i) => v === token()[i]) && S.page === 'collaboration';
    function clear() {
      revision++;
      loading++;
      items = [];
      options = null;
      detail = null;
      projection = '';
    }
    function privateClear() {
      clear();
      closeDrawer();
      const drawer = document.querySelector('#cc-drawer');
      if (drawer) drawer.innerHTML = '';
      for (const node of document.querySelectorAll(
        '.cc-coordination-list, .cc-coordination-context',
      ))
        node.innerHTML = empty('连接或项目范围已改变，请重新读取。');
    }
    async function load(snapshot = state.snapshot, sourceCurrent = () => true) {
      if (!snapshot?.room || !snapshot.capabilities?.coordination_goals) {
        clear();
        return;
      }
      const t = token(),
        request = ++loading;
      const fresh = () => current(t) && request === loading && sourceCurrent();
      try {
        // Read the authority projection first. Never reuse an earlier goals
        // response after observing revocation or a changed project projection.
        const nextOptions = await getRecord('coordination_options', '');
        if (!fresh()) return;
        const nextProjection = JSON.stringify({
          participants: nextOptions.participants || [],
          can_approve: nextOptions.can_approve,
        });
        if (projection && projection !== nextProjection) {
          privateClear();
          return load(snapshot, sourceCurrent);
        }
        options = nextOptions;
        projection = nextProjection;
        const collected = [],
          cursors = new Set();
        let cursor = '';
        do {
          const page = await getRecord('coordination_goals', '', cursor, { limit: '30' });
          if (!fresh()) return;
          collected.push(...(page.items || []));
          cursor = page.next_cursor || '';
          if (cursor && cursors.has(cursor)) throw new Error('目标分页游标重复，请刷新后重试。');
          if (cursor) cursors.add(cursor);
        } while (cursor);
        if (!fresh()) return;
        items = [...new Map(collected.map((g) => [g.id, g])).values()];
        if (detail) {
          const latest = items.find((g) => g.id === detail.goal?.id);
          if (!latest || latest.version !== detail.goal.version) {
            detail = null;
            closeDrawer();
            const drawer = document.querySelector('#cc-drawer');
            if (drawer) drawer.innerHTML = '';
          }
        }
      } catch (error) {
        if (!fresh()) return;
        if (
          [401, 403, 409].includes(error.status) ||
          ['FORBIDDEN', 'PERMISSION_DENIED', 'AUTHORIZATION_CHANGED', 'ACCESS_CHANGED'].includes(
            error.code,
          )
        )
          privateClear();
        throw error;
      }
    }
    function action(label = '＋ 目标') {
      return enabled() && canManage() ? button('goal-new', label) : '';
    }
    function summary(g) {
      return (
        '<article class="cc-goal-summary" data-goal-id="' +
        E(g.id) +
        '">' +
        '<div class="cc-row"><strong>' +
        E(g.objective) +
        '</strong>' +
        badge(g.state) +
        '</div>' +
        '<p>' +
        E(g.acceptance) +
        '</p><small>' +
        E((g.project_ids || []).map(projectLabel).join(' · ')) +
        ' · ' +
        (g.participant_grant_ids || []).length +
        ' 个助手连接 · v' +
        E(g.version) +
        '</small><div class="cc-actions">' +
        button('goal-detail', '查看步骤与结果', g) +
        (canApprove() &&
        !g.delegation &&
        g.can_approve !== false &&
        ['proposed', 'paused'].includes(g.state)
          ? button('goal-review', '审阅执行范围', g)
          : '') +
        '</div></article>'
      );
    }
    function context() {
      if (!enabled()) return '';
      return (
        '<section class="cc-coordination-context"><div class="cc-row"><h3>协作目标</h3>' +
        action('新目标') +
        '</div>' +
        (items.slice(0, 3).map(summary).join('') ||
          empty('给出想完成的结果。先规划，再由房主确认执行范围。')) +
        '</section>'
      );
    }
    function list() {
      if (!enabled()) return '';
      return (
        '<section class="cc-coordination-list"><div class="cc-row"><h2>目标与工作项</h2>' +
        action('创建协作目标') +
        '</div><p class="cc-hint">助手可围绕目标拆解、交接与复核。保存目标仅建立规划；执行需房主另外确认已有连接与能力。</p>' +
        (items.map(summary).join('') || empty('还没有协作目标。')) +
        '</section>'
      );
    }
    function choices(name, rows, selected) {
      return rows
        .map(
          ([id, label]) =>
            '<label><input type="checkbox" name="' +
            name +
            '" value="' +
            E(id) +
            '" ' +
            (selected.includes(id) ? 'checked' : '') +
            '>' +
            E(label) +
            '</label>',
        )
        .join('');
    }
    function draftForm(goal = null) {
      const projects = roomProjects().filter((p) => p.environment_id === state.environment);
      const participants = options?.participants || [],
        caps = options?.capabilities || [];
      const budget = goal?.budget || options?.budget_defaults || {};
      return (
        '<p>写清希望达到的结果与验收条件。保存后仍需房主单独批准，才会接受执行。</p>' +
        '<form id="cc-goal-draft" class="cc-form" data-id="' +
        E(goal?.id || '') +
        '" data-version="' +
        E(goal?.version || '') +
        '">' +
        field(
          '想完成什么',
          '<textarea name="objective" rows="3" maxlength="4000" required placeholder="例如：找出失败原因，修复并提供测试证据">' +
            E(goal?.objective) +
            '</textarea>',
        ) +
        field(
          '怎样算完成',
          '<textarea name="acceptance" rows="2" maxlength="2000" required placeholder="写出可核对的结果或证据">' +
            E(goal?.acceptance) +
            '</textarea>',
        ) +
        '<fieldset class="cc-project-choices"><legend>本目标涉及的项目</legend>' +
        choices(
          'project_ids',
          projects.map((p) => [p.project_id, p.project_label || projectLabel(p.project_id)]),
          goal?.project_ids || [state.project],
        ) +
        '</fieldset><p class="cc-hint">之后加入房间的项目不会自动加入这个目标。</p>' +
        '<fieldset class="cc-project-choices"><legend>参与的助手连接</legend>' +
        choices(
          'participant_grant_ids',
          participants.map((p) => [p.grant_id, connectionName(p.grant_id)]),
          goal?.participant_grant_ids || [],
        ) +
        (participants.length
          ? ''
          : empty('没有可用的助手连接。请先在助手与连接核对已有项目授权。')) +
        '</fieldset>' +
        (options?.truncated
          ? '<p class="cc-hint">当前仅显示前 ' +
            E(options.scan_limit || 128) +
            ' 个可扫描连接，候选可能不完整。</p>'
          : '') +
        field(
          '首位协调助手',
          '<select name="coordinator_grant_id" required>' +
            '<option value="">先选择参与连接</option>' +
            participants
              .filter((p) => (goal?.participant_grant_ids || []).includes(p.grant_id))
              .map(
                (p) =>
                  '<option value="' +
                  E(p.grant_id) +
                  '" ' +
                  (p.grant_id === goal?.coordinator_grant_id ? 'selected' : '') +
                  '>' +
                  E(connectionName(p.grant_id)) +
                  '</option>',
              )
              .join('') +
            '</select>',
        ) +
        '<p class="cc-hint">批准后，先交给这个连接一个只读协调工作项，由它拆解、交接和复核。职责名称不授予权限。</p>' +
        '<fieldset class="cc-project-choices"><legend>拟使用的已有能力</legend>' +
        choices(
          'capabilities',
          caps.map((c) => [c, capNames[c] || c]),
          goal?.capabilities || ['read'],
        ) +
        '</fieldset><p class="cc-hint">所有参与连接须已有全部目标项目的读取权限。能力是目标上限；每项工作按所需能力选择连接，只读助手可参与复核。</p>' +
        '<div class="cc-two">' +
        field(
          '执行有效期（分钟）',
          '<input name="duration_minutes" type="number" min="1" max="1440" step="1" value="' +
            E((goal?.duration_seconds || 3600) / 60) +
            '" required>',
        ) +
        field(
          '工作项上限',
          '<input name="max_work_items" type="number" min="1" max="100" value="' +
            E(budget.max_work_items || 20) +
            '" required>',
        ) +
        '</div><details><summary>步骤、交接与重试预算</summary><div class="cc-two">' +
        field(
          '操作步骤上限',
          '<input name="max_steps" type="number" min="1" max="500" value="' +
            E(budget.max_steps || 50) +
            '" required>',
        ) +
        field(
          '协调消息上限',
          '<input name="max_messages" type="number" min="0" max="200" value="' +
            E(budget.max_messages ?? 40) +
            '" required>',
        ) +
        field(
          '每项尝试上限',
          '<input name="max_attempts" type="number" min="1" max="3" value="' +
            E(budget.max_attempts || 3) +
            '" required>',
        ) +
        field(
          '领取有效期（秒）',
          '<input name="lease_seconds" type="number" min="30" max="900" value="' +
            E(budget.lease_seconds || 300) +
            '" required>',
        ) +
        '</div></details><p class="cc-goal-gate" role="status"></p><div class="cc-actions">' +
        '<button class="btn primary" type="submit">保存规划，不启用执行</button>' +
        button('close-drawer', '取消') +
        '</div></form>'
      );
    }
    function draftPayload(form) {
      const f = new FormData(form);
      return {
        conversation_id: state.conversation,
        objective: String(f.get('objective')).trim(),
        acceptance: String(f.get('acceptance')).trim(),
        project_ids: f.getAll('project_ids'),
        participant_grant_ids: f.getAll('participant_grant_ids'),
        coordinator_grant_id: f.get('coordinator_grant_id'),
        capabilities: f.getAll('capabilities'),
        duration_seconds: Number(f.get('duration_minutes')) * 60,
        budget: Object.fromEntries(
          ['max_work_items', 'max_steps', 'max_messages', 'max_attempts', 'lease_seconds'].map(
            (k) => [k, Number(f.get(k))],
          ),
        ),
      };
    }
    function validateDraft(form, payload) {
      const issues = [];
      if (!payload.project_ids.length) issues.push('请至少选择一个项目。');
      if (!payload.participant_grant_ids.length) issues.push('请至少选择一个助手连接。');
      if (!payload.capabilities.includes('read')) issues.push('目标须保留读取能力。');
      if (!payload.participant_grant_ids.includes(payload.coordinator_grant_id))
        issues.push('请选择本目标中的首位协调助手。');
      for (const id of payload.participant_grant_ids) {
        const p = connection(id);
        if (!p) {
          issues.push('所选连接已不可用。');
          continue;
        }
        const visible = p.project_ids || [];
        if (!visible.includes('*') && payload.project_ids.some((id) => !visible.includes(id)))
          issues.push(connectionName(id) + ' 缺少所选项目权限。');
        if (!(p.scopes || []).includes('read'))
          issues.push(connectionName(id) + ' 缺少项目读取能力。');
      }
      for (const project of payload.project_ids)
        for (const cap of payload.capabilities)
          if (
            !payload.participant_grant_ids.some((id) => {
              const p = connection(id);
              return (p?.project_capabilities?.[project] || p?.scopes || []).includes(cap);
            })
          )
            issues.push(
              projectLabel(project) + ' 的所选助手连接缺少能力：' + (capNames[cap] || cap) + '。',
            );
      const node = form.querySelector('.cc-goal-gate');
      if (node) node.textContent = issues.join(' ');
      return issues;
    }
    function review(g) {
      const budget = g.budget || {},
        projects = g.projects || (g.project_ids || []).map((project_id) => ({ project_id }));
      return (
        '<div class="cc-goal-review-scroll"><p class="cc-prose">' +
        E(g.objective) +
        '</p><p><strong>验收：</strong>' +
        E(g.acceptance) +
        '</p><dl class="cc-facts"><div><dt>确认版本</dt><dd>v' +
        E(g.version) +
        '</dd></div><div><dt>有效期</dt><dd>' +
        E(g.duration_seconds / 60) +
        ' 分钟</dd></div>' +
        '<div><dt>能力</dt><dd>' +
        E((g.capabilities || []).map((c) => capNames[c] || c).join('、')) +
        '</dd></div><div><dt>首位协调助手</dt><dd>' +
        E(connectionName(g.coordinator_grant_id)) +
        '</dd></div></dl><p>批准后将为上述协调连接创建一个只读协调工作项，计入工作项预算。后续工作分别核对所需能力。</p><h3>固定项目范围</h3><ul>' +
        projects
          .map(
            (p) =>
              '<li><strong>' +
              E(projectLabel(p.project_id)) +
              '</strong>' +
              (p.root ? '<br><span class="cc-goal-path">' + E(p.root) + '</span>' : '') +
              (p.mode ? '<br><small>映射模式：' + E(p.mode) + '</small>' : '') +
              '</li>',
          )
          .join('') +
        '</ul><h3>现有助手连接</h3><ul>' +
        (g.participant_grant_ids || [])
          .map((id) => '<li>' + E(connectionName(id)) + '</li>')
          .join('') +
        '</ul><p>最多 ' +
        E(budget.max_work_items) +
        ' 个工作项、' +
        E(budget.max_steps) +
        ' 个操作步骤、' +
        E(budget.max_messages) +
        ' 条协调消息；每项最多尝试 ' +
        E(budget.max_attempts) +
        ' 次，领取有效期 ' +
        E(budget.lease_seconds) +
        ' 秒。</p>' +
        '<section class="cc-goal-boundary"><h3>执行边界</h3><p>此确认允许上述现有连接在这个目标内使用已有能力。不会扩大连接权限；关键操作仍按具体任务与平台要求审批。</p>' +
        ((g.capabilities || []).includes('execute')
          ? '<p><strong>Shell 以执行节点账户权限运行，不是操作系统级的项目沙箱。</strong>项目路径限制不能代表命令的实际文件访问边界。</p>'
          : '') +
        '<p>自动交接需要独立的目标工作事件原生订阅。普通消息提醒、旧只读任务订阅和收件回执都不会启用目标执行，也不证明助手在线或独立聊天身份。</p></section>' +
        '</div>' +
        (canApprove() && g.can_approve !== false
          ? '<form id="cc-goal-approve" class="cc-form" data-id="' +
            E(g.id) +
            '" data-version="' +
            E(g.version) +
            '" data-digest="' +
            E(g.digest) +
            '"><label><input type="checkbox" name="confirm" required> 我以房主身份确认此版本、项目、连接、能力、有效期与预算</label>' +
            '<div class="cc-actions"><button type="submit" class="btn primary">确认启用此目标执行</button>' +
            button('close-drawer', '关闭审阅') +
            '<p class="cc-hint">提交确认后，关闭窗口不会撤销已提交的请求。</p>' +
            '</div></form>'
          : empty('仅房主可确认启用执行。'))
      );
    }
    function safeLink(url, label) {
      if (typeof url !== 'string' || !url) return '';
      try {
        const parsed = new URL(url, location.origin);
        if (!['https:', 'http:'].includes(parsed.protocol) || parsed.username || parsed.password)
          return '';
        return (
          '<a class="cc-result-link" href="' +
          E(parsed.href) +
          '" target="_blank" rel="noopener noreferrer">' +
          E(label || '查看记录') +
          '</a>'
        );
      } catch {
        return '';
      }
    }
    function workCard(w, all, operations) {
      const related = operations.filter(
        (o) =>
          o.work_item_id === w.id ||
          o.work_id === w.id ||
          (w.operation_ids || w.result?.operation_ids || []).includes(o.id || o.operation_id),
      );
      const results = w.result || {},
        refs = results.artifacts || results.artifact_refs || [];
      return (
        '<article class="cc-work-card" data-work-id="' +
        E(w.id) +
        '"><div class="cc-row"><h3>' +
        E(w.objective) +
        '</h3>' +
        badge(w.state) +
        '</div><p>' +
        E(w.acceptance) +
        '</p><small>' +
        E(projectLabel(w.project_id)) +
        ' · ' +
        E(connectionName(w.assignee_grant_id)) +
        ' · 所需能力：' +
        E((w.required_capabilities || ['read']).map((c) => capNames[c] || c).join('、')) +
        ' · 第 ' +
        E(w.attempt || 0) +
        ' 次领取</small><p>依赖：' +
        ((w.dependencies || [])
          .map((id) => E(all.find((v) => v.id === id)?.objective || id))
          .join('、') || '无') +
        '</p>' +
        (w.lease_until ? '<p>当前领取有效至 ' + E(when(w.lease_until)) + '</p>' : '') +
        '<section class="cc-work-operations"><h4>实际操作</h4>' +
        (related
          .map(
            (op) =>
              '<details><summary>' +
              E(op.tool || '操作') +
              ' · ' +
              E(status(op.state)) +
              ' · ' +
              E(String(op.operation_id || op.id).slice(-8)) +
              '</summary><p>操作 ID：' +
              E(op.operation_id || op.id) +
              '</p>' +
              (op.summary ? '<p>' + E(op.summary) + '</p>' : '') +
              button('goal-operation', '查看实际操作记录', { id: op.operation_id || op.id }) +
              safeLink(op.url || op.operation_url, '打开记录链接') +
              (op.exit_code !== undefined ? '<p>退出码：' + E(op.exit_code) + '</p>' : '') +
              '</details>',
          )
          .join('') || empty('暂无实际操作记录。规划与领取状态不等于执行完成。')) +
        '</section>' +
        (results.summary
          ? '<section class="cc-work-result"><h4>提交结果</h4><p class="cc-prose">' +
            E(results.summary) +
            '</p>' +
            refs
              .map((r) =>
                typeof r === 'string'
                  ? safeLink(r, '查看结果附件')
                  : safeLink(r.url, r.label || r.name || '查看结果附件'),
              )
              .join('') +
            '</section>'
          : '') +
        (results.limitations || []).map((v) => '<p class="cc-hint">' + E(v) + '</p>').join('') +
        '</article>'
      );
    }
    function subscription(g) {
      if (g.delegation) {
        const request = g.delegation.subscription_request;
        if (!request)
          return empty('原委托范围已不可用。请核对已有操作，再重新审阅范围并发送新委托。');
        const instruction = window.CodePierCollaborationDelegation.subscriptionInstruction(request);
        return (
          '<details class="cc-goal-subscription"><summary>连接本委托范围的通知</summary>' +
          field(
            '委托范围订阅指令',
            '<textarea rows="5" readonly class="cc-goal-subscription-text">' +
              E(instruction) +
              '</textarea>',
          ) +
          button('goal-copy-subscription', '复制委托订阅指令') +
          '</details>'
        );
      }
      if (g.state !== 'active' || !options?.work_event) return '';
      const filters = {
        project_id: g.anchor_project_id || state.project,
        environment_id: g.environment_id || state.environment,
        conversation_id: g.conversation_id,
        goal_id: g.id,
        approval_id: g.approval_id,
      };
      const instruction =
        '请使用当前聊天已有的 CodePier 助手连接，只为此已批准目标创建原生事件订阅。事件：' +
        options.work_event +
        '；参数：' +
        JSON.stringify(filters) +
        '。须使用当前宿主实际提供的订阅能力、回调与签名材料并完成其原生确认，不新建凭据或扩大权限。' +
        '普通消息与旧只读任务订阅不能替代此订阅。若宿主不支持，请说明具体缺失能力，不声称已经接通。' +
        '订阅后先读取目标并核对现有待领取工作，批准前发出的通知可能早于订阅。收到事件后重新读取目标与工作项，按当前版本、所需能力和领取租约执行；关键操作仍遵守任务和平台审批。' +
        '此订阅只绑定上述 approval_id，修订重新批准后需要对应新批准的原生订阅。';
      return (
        '<details class="cc-goal-subscription"><summary>连接原生目标工作通知</summary>' +
        '<p class="cc-hint">分别发送到所选助手连接对应的原聊天。页面不会自动订阅，订阅有效也不代表助手在线。</p>' +
        field(
          '目标工作订阅指令',
          '<textarea rows="5" readonly class="cc-goal-subscription-text">' +
            E(instruction) +
            '</textarea>',
        ) +
        button('goal-copy-subscription', '复制订阅指令') +
        '</details>'
      );
    }
    function detailMarkup(record) {
      const g = record.goal,
        work = record.work_items || [],
        operations = record.operations || [];
      return (
        '<div class="cc-goal-detail" data-goal-id="' +
        E(g.id) +
        '"><div class="cc-row">' +
        badge(g.state) +
        '<small>v' +
        E(g.version) +
        '</small></div><h3>' +
        E(g.objective) +
        '</h3><p>' +
        E(g.acceptance) +
        '</p><p class="cc-hint">' +
        E((g.project_ids || []).map(projectLabel).join(' · ')) +
        '。新加入房间的项目不属于此目标。</p><div class="cc-actions">' +
        (canApprove() &&
        !g.delegation &&
        g.can_approve !== false &&
        ['proposed', 'paused'].includes(g.state)
          ? button('goal-review', '审阅执行范围', g)
          : '') +
        (canManage() && !g.delegation && !['cancelled', 'expired'].includes(g.state)
          ? button('goal-edit', '修订规划', g)
          : '') +
        (canApprove() && g.state === 'active' ? button('goal-pause', '暂停目标', g) : '') +
        (canApprove() && !['cancelled', 'expired'].includes(g.state)
          ? button('goal-cancel-review', '取消目标…', g)
          : '') +
        '</div><p class="cc-hint">' +
        (g.state === 'active'
          ? '已批准范围不保证助手在线。实际领取、操作与结果分别记录。'
          : '尚不接受新的目标执行；普通讨论和规划可以继续。') +
        '</p>' +
        (g.delegation
          ? '<section class="cc-delegation-origin" data-message-id="' +
            E(g.delegation.source_message_id) +
            '" data-project="' +
            E(g.anchor_project_id || state.project) +
            '" data-environment="' +
            E(g.environment_id || state.environment) +
            '">' +
            button('source', '查看原委托消息', { id: g.delegation.source_message_id }) +
            button('mention-connect', '查看委托范围与接通设置', { id: g.delegation.slot_id }) +
            '<p class="cc-hint">此目标绑定原委托，不能修订或重新批准。暂停后若需继续，请核对已有操作，再发送新委托；已运行的实际操作不保证停止。</p></section>'
          : '') +
        subscription(g) +
        '<div class="cc-row"><h3>步骤与结果</h3>' +
        button('goal-detail', '刷新步骤与结果', g) +
        '</div>' +
        (work.map((w) => workCard(w, work, operations)).join('') ||
          empty('还没有工作项。助手提出的拆解、依赖与交接会显示在这里。')) +
        '<h3>目标内协调记录</h3>' +
        ((record.messages || [])
          .map(
            (m) =>
              '<article class="cc-goal-message">' +
              '<small>' +
              E(
                m.author_kind === 'owner'
                  ? '房主'
                  : connectionName(m.author_grant_id || m.grant_id),
              ) +
              ' · ' +
              E(when(m.created || m.created_at)) +
              '</small><p class="cc-prose">' +
              E(m.body_text || m.text || m.body?.text || '') +
              '</p></article>',
          )
          .join('') || empty('暂无目标内交接或复核消息。普通房间消息不会递归触发执行。')) +
        (canApprove() && !g.delegation && g.state === 'active'
          ? '<form id="cc-goal-message" class="cc-form" data-id="' +
            E(g.id) +
            '">' +
            field(
              '目标内补充说明',
              '<textarea name="body_text" rows="2" maxlength="4000" required></textarea>',
            ) +
            '<fieldset class="cc-project-choices"><legend>@一个助手连接（可选）</legend>' +
            choices(
              'mention_grant_ids',
              (g.participant_grant_ids || []).map((id) => [id, connectionName(id)]),
              [],
            ) +
            '</fieldset><p class="cc-hint">仅在此已批准目标内交接；需要单独工作事件订阅，不复用普通提醒。</p>' +
            '<button type="submit" class="btn">发送目标内消息</button></form>'
          : '') +
        '</div>'
      );
    }
    async function readDetail(id) {
      const dismissed = state.drawerEpoch;
      const t = token(),
        record = await getRecord('coordination_goal', id, '', goalScope(id));
      if (!current(t) || dismissed !== state.drawerEpoch) return null;
      detail = record;
      return record;
    }
    async function act(actionName, element) {
      if (!actionName.startsWith('goal-')) return null;
      if (!enabled()) throw new Error('当前服务未启用目标协作能力。');
      if (actionName === 'goal-copy-subscription') {
        const area = element.closest('.cc-goal-subscription').querySelector('textarea');
        area.focus();
        area.select();
        area.setSelectionRange(0, area.value.length);
        try {
          if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(area.value);
            return local('已复制目标工作订阅指令，请在对应原聊天完成原生确认。');
          }
        } catch {}
        return local('已选中目标工作订阅指令，请使用系统复制。');
      }
      if (actionName === 'goal-operation') {
        const dismissed = state.drawerEpoch;
        const t = token(),
          record = detail,
          id = element.dataset.id;
        if (!record?.operations?.some((op) => (op.operation_id || op.id) === id))
          throw new Error('此操作不属于当前目标记录，请重新读取。');
        try {
          const operation = await api('/api/operations/' + encodeURIComponent(id), {
            signal: state.controller?.signal,
          });
          if (!current(t) || dismissed !== state.drawerEpoch) return local();
          showDrawer(
            '实际操作记录',
            '<p>' +
              badge(operation.state) +
              '</p><p>操作 ID：' +
              E(operation.id) +
              '</p><p>工具：' +
              E(operation.tool) +
              '</p><p>所属项目：' +
              E(projectLabel(operation.project_id)) +
              '</p>' +
              (operation.error ? '<p class="cc-goal-gate">' + E(operation.error) + '</p>' : '') +
              '<h3>实际输出</h3><pre class="cc-json">' +
              E(operation.output || '暂无输出') +
              '</pre>' +
              '<details><summary>实际返回结果</summary><pre class="cc-json">' +
              E(JSON.stringify(operation.result, null, 2)) +
              '</pre></details>' +
              button('goal-detail', '返回目标步骤', record.goal),
            element,
          );
          return local();
        } catch (error) {
          if (current(t) && [401, 403].includes(error.status)) privateClear();
          throw error;
        }
      }
      if (actionName === 'goal-new') {
        showDrawer('创建协作目标', empty('正在核对已有助手连接…'), element);
        const t = token(),
          dismissed = state.drawerEpoch;
        await load();
        if (!current(t) || dismissed !== state.drawerEpoch) return local();
        if (!canManage()) throw new Error('当前没有目标管理权限。');
        showDrawer('创建协作目标', draftForm(), element);
        return local();
      }
      if (['goal-detail', 'goal-review', 'goal-edit', 'goal-cancel-review'].includes(actionName)) {
        showDrawer('读取目标…', empty('正在核对当前目标…'), element);
        const record = await readDetail(element.dataset.id);
        if (!record) return local();
        const g = record.goal;
        if (actionName === 'goal-review') showDrawer('房主确认执行范围', review(g), element);
        else if (actionName === 'goal-edit')
          showDrawer(
            '修订目标规划',
            '<p class="cc-goal-boundary">保存修订会停止旧版本接受新执行，必须重新批准。</p>' +
              draftForm(g),
            element,
          );
        else if (actionName === 'goal-cancel-review')
          showDrawer(
            '取消这个协作目标',
            '<p>停止本目标接受新工作。已发出的实际操作可能仍在运行，需以其状态为准。</p><form id="cc-goal-cancel" class="cc-form" data-id="' +
              E(g.id) +
              '" data-version="' +
              E(g.version) +
              '"><label><input type="checkbox" name="confirm" required> 确认取消“' +
              E(g.objective) +
              '”</label><button type="submit" class="btn">确认取消目标</button>' +
              button('close-drawer', '返回') +
              '</form>',
            element,
          );
        else showDrawer('目标、步骤与结果', detailMarkup(record), element);
        return local();
      }
      if (actionName === 'goal-pause') {
        const t = token();
        await mutateGoal('goal-control', {
          goal_id: element.dataset.id,
          expected_version: Number(element.dataset.version),
          action: 'pause',
        });
        if (!current(t)) return local();
        detail = null;
        state.notes = '目标已暂停。现有实际操作是否停止，请核对操作记录。';
        await renderPage(false);
        return local(state.notes);
      }
      return local();
    }
    async function submit(form) {
      if (!form.id.startsWith('cc-goal-')) return null;
      const t = token();
      try {
        if (form.id === 'cc-goal-draft') {
          const payload = draftPayload(form);
          if (validateDraft(form, payload).length) throw new Error('请核对所选连接的项目与能力。');
          await mutateGoal(form.dataset.id ? 'goal-update' : 'goal-create', {
            ...payload,
            ...(form.dataset.id
              ? { goal_id: form.dataset.id, expected_version: Number(form.dataset.version) }
              : {}),
          });
        } else if (form.id === 'cc-goal-message') {
          const f = new FormData(form);
          await mutateGoal('goal-message', {
            goal_id: form.dataset.id,
            body_text: f.get('body_text'),
            mention_grant_ids: f.getAll('mention_grant_ids'),
            input_work_item_ids: [],
          });
        } else {
          if (!form.elements.confirm?.checked) throw new Error('请先明确确认此版本的范围。');
          if (!canApprove()) throw new Error('仅房主可确认执行或取消目标。');
          await mutateGoal(form.id === 'cc-goal-approve' ? 'goal-approve' : 'goal-control', {
            goal_id: form.dataset.id,
            expected_version: Number(form.dataset.version),
            ...(form.id === 'cc-goal-approve'
              ? { digest: form.dataset.digest }
              : { action: 'cancel' }),
          });
        }
        if (!current(t)) return local();
        // Keep the review visible until the single replacement render commits.
        // Closing it before a second fetch exposes actionable stale controls.
        detail = null;
        state.notes =
          form.id === 'cc-goal-draft'
            ? '目标规划已保存，执行尚未启用。'
            : form.id === 'cc-goal-approve'
              ? '此目标版本已批准，首个只读协调工作项已排队。请连接本批准版本的原生工作通知；排队不代表执行。'
              : form.id === 'cc-goal-message'
                ? '目标内消息已保存，不代表助手已读取或执行。'
                : '目标已取消。';
        state.error = false;
        await renderPage(false);
        return local(state.notes);
      } catch (error) {
        if (!current(t)) return local();
        if (form.elements.confirm) form.elements.confirm.checked = false;
        if ([401, 403].includes(error.status)) privateClear();
        if (error.status === 409 || ['STALE_VERSION', 'VERSION_CONFLICT'].includes(error.code)) {
          detail = null;
          state.notes = '目标或连接权限已改变。请重新审阅当前版本；未按旧版本启用。';
          state.error = true;
          await renderPage(false);
          return local(state.notes);
        }
        throw error;
      }
    }
    function change(event) {
      const form = event.target.form;
      if (form?.id === 'cc-goal-draft') {
        if (event.target.name === 'participant_grant_ids') {
          const selected = new FormData(form).getAll('participant_grant_ids');
          const picker = form.elements.coordinator_grant_id;
          const chosen = selected.includes(picker.value) ? picker.value : selected[0];
          picker.innerHTML =
            '<option value="">先选择参与连接</option>' +
            selected
              .map(
                (id) =>
                  '<option value="' +
                  E(id) +
                  '"' +
                  (id === chosen ? ' selected' : '') +
                  '>' +
                  E(connectionName(id)) +
                  '</option>',
              )
              .join('');
        }
        validateDraft(form, draftPayload(form));
      }
    }
    return { clear, load, action, context, list, act, submit, change, enabled };
  },
};
