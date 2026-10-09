'use strict';
// Explicit delegation reuses existing authority; ordinary discussion remains inert.
window.CodePierCollaborationDelegation = {
  subscriptionInstruction(request, mode = 'notification_only', contracts = {}) {
    // The server owns exact filters, checkpoint semantics and host instructions.
    return contracts[mode]?.instructions || '';
  },
  create(ctx) {
    const {
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
      liveJoin,
      getRecord,
      readDrawer,
      showDrawer,
      closeDrawer,
      mutation,
      refreshChat,
      coordination,
      mergeMessages,
      patchTimeline,
    } = ctx;
    const policyFor = (slotId) => state.delegationPolicies.find((p) => p.slot_id === slotId);
    const selectedPolicy = () =>
      state.delegationPolicies.find((p) => p.id === chat().delegationPolicy);
    const policyAvailable = (policy) =>
      !!policy?.effective_active && policy.expires_at * 1000 > Date.now();
    const capabilitiesLabel = (caps) =>
      (caps || [])
        .map((cap) => ({ read: '读取', write: '修改文件', execute: '运行命令' })[cap] || cap)
        .join('、');
    const policyTargets = (policy) =>
      policy?.execution_targets || (policy?.execution_target ? [policy.execution_target] : []);
    const targetLabel = (policy, id) =>
      id === 'project_agent'
        ? '项目 Agent · ' + projectLabel(policy?.project_id || state.project)
        : policy?.execution_target_options?.find((item) => item.id === id)?.label ||
          'VPS · ' + id.slice(4);
    const targetAvailable = (policy, id) =>
      policy?.execution_target_options?.find((item) => item.id === id)?.available !== false;
    function selectPolicy(policy, version = policy?.version || 0) {
      const c = chat();
      c.delegationPolicy = policy?.id || '';
      c.delegationVersion = version;
      c.delegationTargets = policyTargets(policy).length === 1 ? [...policyTargets(policy)] : [];
      c.delegationCapabilities = [...(policy?.capabilities || [])];
    }
    function updatePolicies(items) {
      const previous = state.delegationPolicies || [];
      state.delegationPolicies = items.map((item) => {
        const known = previous.find((row) => row.id === item.id);
        return known && known.version > item.version ? known : item;
      });
    }
    function selectedDelegationValid() {
      const c = chat(),
        policy = selectedPolicy();
      return !!(
        policyAvailable(policy) &&
        policy.version === c.delegationVersion &&
        c.mentions.length === 1 &&
        c.mentions[0] === policy.slot_id
      );
    }
    function selectedScopeValid() {
      const c = chat(),
        policy = selectedPolicy();
      return (
        selectedDelegationValid() &&
        c.delegationTargets?.length > 0 &&
        c.delegationTargets.every(
          (id) => policyTargets(policy).includes(id) && targetAvailable(policy, id),
        ) &&
        c.delegationCapabilities?.includes('read') &&
        c.delegationCapabilities.every((cap) => policy.capabilities.includes(cap)) &&
        (!c.delegationTargets.some((id) => id.startsWith('vps:')) ||
          c.delegationCapabilities.includes('execute')) &&
        (!c.delegationCapabilities.includes('write') ||
          c.delegationTargets.includes('project_agent'))
      );
    }
    function scopeForm() {
      const c = chat(),
        policy = selectedPolicy();
      if (!selectedDelegationValid()) throw new Error('委托范围已改变，请重新选择；正文已保留。');
      return (
        '<form id="cc-delegation-scope" class="cc-form" data-policy-id="' +
        E(policy.id) +
        '" data-policy-version="' +
        E(c.delegationVersion) +
        '"><p>交给 ' +
        E(slotMember(policy.slot_id)?.label || '助手') +
        ' · ' +
        E(policy.purpose) +
        '</p><fieldset class="cc-project-choices"><legend>本次在哪做</legend>' +
        policyTargets(policy)
          .map(
            (id) =>
              '<label><input type="checkbox" name="execution_targets" value="' +
              E(id) +
              '"' +
              (c.delegationTargets?.includes(id) ? ' checked' : '') +
              (targetAvailable(policy, id) ? '' : ' disabled') +
              '>' +
              E(targetLabel(policy, id)) +
              (targetAvailable(policy, id) ? '' : '（当前不可用）') +
              '</label>',
          )
          .join('') +
        '</fieldset><fieldset class="cc-project-choices"><legend>本次可用能力</legend>' +
        policy.capabilities
          .map(
            (cap) =>
              '<label><input type="checkbox" name="capabilities" value="' +
              E(cap) +
              '"' +
              (c.delegationCapabilities?.includes(cap) ? ' checked' : '') +
              (cap === 'read' ? ' disabled' : '') +
              '>' +
              E(capabilitiesLabel([cap])) +
              '</label>',
          )
          .join('') +
        '</fieldset><p class="cc-hint">仅在这些已批准目标和能力内处理。需要其他目标时，助手会报告缺少的范围。</p>' +
        '<details><summary>验收要求</summary>' +
        field(
          '本次验收要求',
          '<textarea name="delegation_acceptance" rows="2" maxlength="2000" required>' +
            E(c.acceptance) +
            '</textarea>',
        ) +
        '</details><div class="cc-actions"><button class="btn primary" type="submit">使用本次范围</button>' +
        button('close-drawer', '取消') +
        '</div></form>'
      );
    }
    function openScope(element) {
      showDrawer('本次处理范围', scopeForm(), element);
    }
    function connectionMode(slot) {
      const policy = policyFor(slot.id);
      const choice = state.delegationConnectionModes?.[slot.id];
      return choice && choice.policyVersion === policy?.version ? choice.mode : '';
    }
    function chooseConnectionMode(slot, mode) {
      state.delegationConnectionModes ||= {};
      state.delegationConnectionModes[slot.id] = {
        mode,
        policyVersion: policyFor(slot.id)?.version,
      };
    }
    function pickerAction(slot) {
      const policy = policyFor(slot.id);
      if (!state.snapshot?.can_manage || !policyAvailable(policy)) return '';
      return (
        '<button type="button" class="btn primary cc-mention-delegate" data-cc-action="pick-delegation" data-id="' +
        E(slot.id) +
        '" data-policy-id="' +
        E(policy.id) +
        '" data-policy-version="' +
        E(policy.version) +
        '" aria-pressed="' +
        String(
          chat().delegationPolicy === policy.id && chat().delegationVersion === policy.version,
        ) +
        '"><strong>交给 ' +
        E(slot.label) +
        ' 处理</strong><small>按已确认范围：' +
        E(policy.purpose) +
        '</small></button>'
      );
    }
    function delegationComposer() {
      if (!state.snapshot?.capabilities?.direct_delegation) return '';
      const c = chat(),
        policy = c.mentions.length === 1 ? policyFor(c.mentions[0]) : null;
      return (
        '<label class="cc-send-mode"><span class="cc-sr-only">发送方式</span><select name="delegation_policy" aria-label="发送方式">' +
        '<option value=""' +
        (!c.delegationPolicy ? ' selected' : '') +
        '>普通讨论</option>' +
        (policy
          ? '<option value="' +
            E(policy.id) +
            '" data-policy-version="' +
            E(policy.version) +
            '"' +
            (c.delegationPolicy === policy.id ? ' selected' : '') +
            (policyAvailable(policy) ? '' : ' disabled') +
            '>交给 ' +
            E(slotMember(policy.slot_id)?.label || '助手') +
            ' 处理' +
            (policyAvailable(policy) ? '' : '（授权不可用）') +
            '</option>'
          : '') +
        (c.delegationPolicy && policy?.id !== c.delegationPolicy
          ? '<option value="' +
            E(c.delegationPolicy) +
            '" selected disabled>原委托授权不可用</option>'
          : '') +
        '</select></label>' +
        (c.mentions.length === 1 && !policyAvailable(policy)
          ? button('delegation-setup', '设置委托范围', { id: c.mentions[0] })
          : '')
      );
    }
    function delegationContext() {
      const c = chat();
      if (!c.delegationPolicy) return '';
      const policy = selectedPolicy(),
        valid = selectedDelegationValid();
      return (
        '<section class="cc-delegation-context" aria-label="本次委托范围"><div class="cc-row"><strong>本次发送将创建委托</strong>' +
        (valid ? button('delegation-scope', '修改本次范围') : '') +
        '</div><p class="cc-delegation-purpose">' +
        E(valid ? policy.purpose : '委托范围已改变、到期或不可用。请重新核对并选择，正文已保留。') +
        '</p>' +
        (valid
          ? '<dl class="cc-send-scope"><div><dt>交给谁</dt><dd>' +
            E(slotMember(policy.slot_id)?.label || '助手') +
            '</dd></div><div><dt>本次在哪做</dt><dd>' +
            E(
              c.delegationTargets?.length
                ? c.delegationTargets.map((id) => targetLabel(policy, id)).join('、')
                : '请选择本次目标',
            ) +
            '</dd></div><div><dt>可用能力</dt><dd>' +
            E(capabilitiesLabel(c.delegationCapabilities)) +
            '</dd></div></dl>' +
            (!selectedScopeValid() ? '<p class="cc-hint">发送前选择本次使用的目标与能力。</p>' : '')
          : '') +
        '</section>'
      );
    }
    function patchDelegationComposer() {
      const mode = document.querySelector('#cc-send-mode');
      if (mode && !mode.contains(document.activeElement)) mode.innerHTML = delegationComposer();
      const context = document.querySelector('#cc-delegation-context');
      if (context && !context.contains(document.activeElement))
        context.innerHTML = delegationContext();
      for (const card of document.querySelectorAll('[data-policy-slot]')) {
        const slot = mentionSlots().find((s) => s.id === card.dataset.policySlot);
        if (!slot) continue;
        const markup = policyCard(slot);
        if (!card.contains(document.activeElement)) card.outerHTML = markup;
        else {
          // Keep the focused copy/edit control and exact viewed scope, while
          // live evidence can still advance without replacing the card.
          const template = document.createElement('template');
          template.innerHTML = markup;
          for (const label of card.querySelectorAll('[data-connection-status]')) {
            const fresh = template.content.querySelector(
              '[data-connection-status="' + label.dataset.connectionStatus + '"]',
            );
            if (fresh && label.textContent !== fresh.textContent)
              label.textContent = fresh.textContent;
          }
        }
      }
    }
    function policyCard(slot) {
      if (!state.snapshot?.capabilities?.direct_delegation) return '';
      const policy = policyFor(slot.id),
        available = policyAvailable(policy),
        mode = connectionMode(slot);
      const instruction = window.CodePierCollaborationDelegation.subscriptionInstruction(
        policy?.subscription_request,
        mode,
        policy?.consumer_contracts,
      );
      const status = policy?.connection_status || {},
        active = status.notification_state === 'active' || policy?.notification_state === 'active';
      const progress =
        status.operation?.unknown_count > 0
          ? '实际操作状态待核对'
          : status.operation?.pending_count > 0
            ? '实际操作已受理，等待完成'
            : status.claim?.active_count > 0
              ? '有任务已领取，等待实际操作记录'
              : status.result?.count > 0
                ? '本范围已有历史结果回到原话题'
                : '尚无任务领取记录';
      return (
        '<section class="cc-card cc-delegation-policy" data-policy-slot="' +
        E(slot.id) +
        '" data-policy-version="' +
        E(policy?.version || 0) +
        '">' +
        '<h3>接通 ' +
        E(slot.label) +
        '</h3><ol class="cc-connection-steps">' +
        '<li><strong>当前连接</strong><span>' +
        E(slot.state === 'registered' ? '已登记，使用现有项目授权' : '请先在原聊天使用加入码登记') +
        '</span></li>' +
        '<li><strong>允许范围</strong><span>' +
        E(available ? '已确认 · ' + policy.purpose : '先确认一次允许的用途、目标和能力') +
        '</span>' +
        (policy
          ? '<small>' +
            E(
              policyTargets(policy)
                .map((id) => targetLabel(policy, id))
                .join('、'),
            ) +
            ' · ' +
            E(capabilitiesLabel(policy.capabilities)) +
            ' · 到期 ' +
            E(when(policy.expires_at)) +
            '</small>'
          : '') +
        '</li>' +
        '<li><strong>原生接入</strong><span data-connection-status="notification">' +
        (active ? '当前范围已订阅' : '等待对应聊天完成原生订阅') +
        '</span><small>尚未核实宿主处理方式；在原聊天确认后生效。</small></li>' +
        '<li><strong>实际处理</strong><span data-connection-status="progress">' +
        E(progress) +
        '</span><small>订阅有效不表示助手在线或正在执行。</small></li></ol>' +
        (policy?.blocked_reason ? '<p class="cc-hint">' + E(policy.blocked_reason) + '</p>' : '') +
        '<div class="cc-actions">' +
        (state.snapshot?.can_manage
          ? button(
              'delegation-connect',
              '接通并处理委托',
              slot,
              'data-mode="managed_execution"',
            ).replace('class="btn"', 'class="btn primary"') +
            (available
              ? button('delegation-connect', '仅接收提醒', slot, 'data-mode="notification_only"')
              : button('mention-notifications', '仅接收提醒', slot))
          : '') +
        '</div>' +
        (instruction
          ? '<section class="cc-connection-instruction" data-consumer-mode="' +
            E(mode) +
            '"><strong>' +
            (mode === 'managed_execution' ? '已选择：处理范围内的明确委托' : '已选择：仅接收提醒') +
            '</strong><p class="cc-hint">把接入说明复制到对应原聊天，在那里确认后生效。已有允许范围和有效订阅会被复用。</p>' +
            button('delegation-copy', '复制接入说明', slot).replace(
              'class="btn"',
              'class="btn primary"',
            ) +
            '<details class="cc-instruction-details"><summary>查看完整接入说明</summary>' +
            field(
              '完整接入说明',
              '<textarea class="cc-delegation-instruction" rows="4" readonly>' +
                E(instruction) +
                '</textarea>',
            ) +
            '</details></section>'
          : '') +
        (available
          ? '<p class="cc-hint">范围内明确选择“交给助手处理”并发送即可。普通 @ 讨论不会创建委托。</p>'
          : '') +
        (policy && state.snapshot?.can_manage
          ? '<details class="cc-connection-settings"><summary>修改允许范围与管理</summary>' +
            button('delegation-setup', '修改允许范围', slot) +
            (available ? button('delegation-pause', '暂停后续委托', policy) : '') +
            '<small>版本 ' +
            E(policy.version) +
            ' · 已用 ' +
            E(policy.usage?.delegations || 0) +
            ' / ' +
            E(policy.usage?.max_delegations || 0) +
            ' 次。只有范围改变时才需重新审阅。</small></details>'
          : '') +
        '</section>'
      );
    }
    function messageCard(message) {
      if (message.kind === 'delegation_result') {
        return (
          '<section class="cc-linked-card cc-delegation-result"><strong>委托结果</strong><p>' +
          (message.body?.execution_verified
            ? '已关联实际操作记录；结果仍待你验收。'
            : '未提供实际执行证明；请核对结果与限制。') +
          '</p>' +
          button('delegation-result', '查看实际操作与结果', {
            id: message.related_goal_id || message.goal_id,
          }) +
          '</section>'
        );
      }
      const d = message.body?.delegation;
      if (!d?.goal_id) return '';
      const names = {
        notifications_disabled: '任务已保存，提醒服务未启用',
        not_subscribed: '任务已保存，尚未接通委托通知',
        dispatch_required: '已接通，可继续派发原任务',
        not_dispatched: '任务已保存，等待派发',
        queued: '等待宿主接收',
        accepted: '宿主已受理，等待领取',
        leased: '助手已领取',
        running: '正在处理',
        succeeded: '全部工作已提交结果，待验收',
        completed: '已完成',
        blocked: '处理受阻，需要核对',
        failed: '处理失败',
        cancelled: '已取消',
        expired: '已到期',
        policy_inactive: '原委托授权已不可用',
      };
      const status = d.delivery_status || 'not_dispatched';
      const retry = ['not_subscribed', 'dispatch_required', 'not_dispatched'].includes(status);
      const next = {
        not_subscribed: '接通对应聊天后，可继续派发这条已保存的委托。',
        dispatch_required: '连接已接通，点击继续派发原任务。',
        not_dispatched: '点击继续派发；不需要重发正文。',
        queued: '等待宿主接收并让助手领取。',
        accepted: '提醒已受理，尚未领取；可在接入页核对处理方式。',
        leased: '助手已领取，正在等待实际操作记录。',
        running: '按已选择范围处理，结果会回到这个话题。',
        blocked: d.retry_blocked_eligible
          ? '补齐阻塞条件后，可重试尚未开始的步骤。'
          : '查看受阻原因与已有操作，再决定下一步。',
        failed: '查看实际操作和失败原因，再决定是否发出新委托。',
        policy_inactive: '原范围已暂停或到期，请核对允许范围。',
        succeeded: '结果已回到原话题，请核对证据与限制。',
      };
      return (
        '<section class="cc-linked-card cc-delegation-message" data-delegation-state="' +
        E(status) +
        '"><strong>' +
        E(names[status] || status) +
        '</strong><p>' +
        E(next[status] || '领取、实际操作与回帖分别记录。') +
        '</p>' +
        (d.progress?.counts
          ? '<small>共 ' +
            E(Object.values(d.progress.counts).reduce((sum, n) => sum + n, 0)) +
            ' 个工作项 · 已提交结果 ' +
            E(d.progress.counts.succeeded || 0) +
            ' 项</small>'
          : '') +
        '<div class="cc-actions">' +
        button('delegation-result', '查看委托进度与结果', { id: d.goal_id }) +
        (status === 'blocked' && d.retry_blocked_eligible === true && state.snapshot?.can_manage
          ? button('delegation-retry-blocked', '重试尚未开始的步骤', { id: d.delegation_id })
          : '') +
        (retry
          ? button('mention-connect', '接通委托通知', {
              id: message.mentions?.[0]?.slot_id || message.body?.mentions?.[0]?.slot_id,
            }) +
            button(
              'delegation-remind',
              '继续派发原任务',
              { id: d.delegation_id },
              'data-message-id="' + E(message.id) + '"',
            )
          : '') +
        '</div></section>'
      );
    }
    function pendingDelivery(message) {
      return [
        'notifications_disabled',
        'policy_inactive',
        'not_subscribed',
        'dispatch_required',
        'not_dispatched',
        'queued',
        'accepted',
        'leased',
        'running',
      ].includes(message.body?.delegation?.delivery_status);
    }
    function delegationForm(slot, targets = [], targetError = '') {
      const policy = policyFor(slot.id),
        caps = policy?.capabilities || ['read'],
        budget = policy?.budget || {};
      const chosenTargets =
        policy?.execution_targets || (policy?.execution_target ? [policy.execution_target] : []);
      const targetRows = targets.map((target) => [
        target.id,
        target.label +
          (target.reason_code === 'VPS_DISABLED'
            ? '（已停用）'
            : target.reason_code === 'DELEGATION_HOST_KEY_REQUIRED'
              ? '（需先核对主机身份）'
              : ''),
        !target.available,
      ]);
      for (const id of chosenTargets) {
        if (!targets.some((target) => target.id === id))
          targetRows.push([id, targetLabel(policy, id) + '（已不属于当前项目或不可用）', true]);
      }
      return (
        '<form id="cc-delegation-policy" class="cc-form" data-slot="' +
        E(slot.id) +
        '" data-version="' +
        E(policy?.version || 0) +
        '"><div class="cc-policy-fields">' +
        '<p>仅允许 ' +
        E(slot.label) +
        ' 的现有连接处理项目 ' +
        E(projectLabel(state.project)) +
        '、当前房间中的明确委托。连接 ' +
        E(String(slot.grant_id || policy?.grant_id || '').slice(-8)) +
        '；位置 ' +
        E(slot.id.slice(-8)) +
        '。</p>' +
        field(
          '允许处理的用途范围',
          '<textarea name="purpose" rows="3" maxlength="1000" required placeholder="例如：检查此项目服务状态并报告问题">' +
            E(policy?.purpose || '') +
            '</textarea>',
        ) +
        '<fieldset class="cc-project-choices"><legend>允许使用已有能力</legend>' +
        [
          ['read', '读取项目文件'],
          ['write', '修改项目文件'],
          ['execute', '运行命令 / Shell'],
        ]
          .map(
            ([id, label]) =>
              '<label><input name="capabilities" type="checkbox" value="' +
              id +
              '"' +
              (caps.includes(id) ? ' checked' : '') +
              '>' +
              label +
              '</label>',
          )
          .join('') +
        '</fieldset>' +
        '<p class="cc-hint">以下目标已绑定当前项目。VPS 不会因项目已绑定而自动获准；请明确勾选。已有策略不会自动扩大。</p>' +
        '<fieldset class="cc-project-choices"><legend>固定执行目标（逐一明确选择）</legend>' +
        targetRows
          .map(
            ([id, label, disabled]) =>
              '<label><input name="execution_targets" type="checkbox" value="' +
              E(id) +
              '"' +
              (chosenTargets.includes(id) && !disabled ? ' checked' : '') +
              (disabled ? ' disabled' : '') +
              '>' +
              E(label) +
              '</label>',
          )
          .join('') +
        '</fieldset>' +
        (targetError ? '<p class="cc-hint">' + E(targetError) + '</p>' : '') +
        '<p class="cc-hint">项目文件读写作用于项目 Agent。选择 VPS 时只允许在这个已保存目标运行命令，不包含远端文件工具。</p>' +
        '<details><summary>更多设置：有效期、次数与步骤预算</summary><div class="cc-two">' +
        field(
          '授权有效期（分钟）',
          '<input name="duration_minutes" type="number" min="1" max="10080" value="10080" required>',
        ) +
        field(
          '每次委托最长（分钟）',
          '<input name="goal_minutes" type="number" min="1" max="60" value="' +
            E((policy?.goal_duration_seconds || 3600) / 60) +
            '" required>',
        ) +
        field(
          '最多接收委托次数',
          '<input name="max_delegations" type="number" min="1" max="100" value="' +
            E(policy?.usage?.max_delegations || 100) +
            '" required>',
        ) +
        '</div>' +
        '<h4>每次委托的步骤与重试预算</h4><div class="cc-two">' +
        [
          ['max_work_items', '工作项上限', 1, 100, 20],
          ['max_steps', '操作步骤上限', 1, 500, 50],
          ['max_messages', '协调消息上限', 0, 200, 40],
          ['max_attempts', '每项尝试上限', 1, 3, 3],
          ['lease_seconds', '领取有效期（秒）', 30, 900, 300],
        ]
          .map(([id, label, min, max, fallback]) =>
            field(
              label,
              '<input name="' +
                id +
                '" type="number" min="' +
                min +
                '" max="' +
                max +
                '" value="' +
                E(budget[id] ?? fallback) +
                '" required>',
            ),
          )
          .join('') +
        '</div></details>' +
        '<section class="cc-goal-boundary cc-exec-boundary"' +
        (caps.includes('execute') ? '' : ' hidden') +
        '><p><strong>Shell 以所选执行目标的现有账号权限运行，不是操作系统级项目沙箱。</strong>检查服务器通常也需要运行命令；不能将此能力称为“只读检查”。</p>' +
        '<label><input name="acknowledge_unsandboxed_exec" type="checkbox"' +
        (caps.includes('execute') ? ' required' : '') +
        '> 如选择运行命令，我理解其账号权限和上述执行边界</label></section>' +
        '<p>用途描述约束助手应做的工作，不是技术沙箱。不会新增连接权限；敏感操作仍须按任务与宿主要求确认。允许在本话题回报进度与结果。</p>' +
        '</div><footer class="cc-policy-submit"><p class="cc-policy-summary">授权最长 7 天（不超过连接有效期） · 每次最长 ' +
        E((policy?.goal_duration_seconds || 3600) / 60) +
        ' 分钟 · 最多 ' +
        E(policy?.usage?.max_delegations || 100) +
        ' 次</p>' +
        '<label><input name="confirm" type="checkbox" required> 我确认上述范围与预算，允许后续明确发送的委托按此范围处理并在本话题回报</label>' +
        '<button class="btn primary" type="submit">确认启用此委托范围</button></footer></form>'
      );
    }

    function change(event) {
      const form = event.target.form;
      if (form?.id !== 'cc-delegation-policy') return;
      const summary = form.querySelector('.cc-policy-summary');
      const minutes = Number(form.elements.duration_minutes.value);
      const duration = minutes && minutes % 1440 === 0 ? minutes / 1440 + ' 天' : minutes + ' 分钟';
      if (summary)
        summary.textContent =
          '授权最长 ' +
          duration +
          '（不超过连接有效期） · 每次最长 ' +
          form.elements.goal_minutes.value +
          ' 分钟 · 最多 ' +
          form.elements.max_delegations.value +
          ' 次';
      const executes = new FormData(form).getAll('capabilities').includes('execute');
      form.querySelector('.cc-exec-boundary').hidden = !executes;
      form.elements.acknowledge_unsandboxed_exec.required = executes;
      if (event.target.name !== 'confirm') form.elements.confirm.checked = false;
      if (event.target.name === 'capabilities' || event.target.name === 'execution_targets')
        form.elements.acknowledge_unsandboxed_exec.checked = false;
    }
    async function act(action, element) {
      const local = { local: true, message: '' };
      if (action === 'delegation-scope') {
        openScope(element);
        return local;
      }
      if (action === 'delegation-connect') {
        const slot = mentionSlots().find((item) => item.id === element.dataset.id);
        if (!slot || !liveJoin(slot) || slot.state !== 'registered')
          throw new Error('请先在原聊天使用加入码登记这个位置。');
        const mode =
          element.dataset.mode === 'notification_only' ? 'notification_only' : 'managed_execution';
        chooseConnectionMode(slot, mode);
        if (!policyAvailable(policyFor(slot.id))) return act('delegation-setup', element);
        const records = await readDrawer('delegation_policies', '', '接通 ' + slot.label, element);
        if (!records) return local;
        updatePolicies(records.items || []);
        chooseConnectionMode(slot, mode);
        if (!policyAvailable(policyFor(slot.id))) return act('delegation-setup', element);
        showDrawer('接通 ' + slot.label, policyCard(slot), element);
        return local;
      }
      if (action === 'delegation-remind' || action === 'delegation-retry-blocked') {
        const generation = state.generation;
        const retryBlocked = action === 'delegation-retry-blocked';
        const result = await mutation('delegation-remind', {
          delegation_id: element.dataset.id,
          ...(retryBlocked ? { retry_blocked: true } : {}),
        });
        if (generation !== state.generation) return local;
        if (result.message) {
          mergeMessages([result.message]);
          patchTimeline();
        }
        return {
          local: true,
          message: retryBlocked
            ? result.work_items_requeued
              ? '已重新排队尚未开始实际操作的步骤；请查看当前领取与结果。'
              : '未重试：已有实际操作或重试条件不再符合，请查看原操作记录。'
            : '已核对并继续派发原委托，没有重复创建任务；请查看消息旁的当前状态。',
        };
      }
      if (action === 'delegation-result') {
        const generation = state.generation;
        await coordination.load();
        if (generation !== state.generation) return local;
        return coordination.act('goal-detail', element);
      }
      if (action === 'delegation-setup') {
        if (!state.snapshot?.capabilities?.direct_delegation)
          throw new Error('当前服务尚未支持直接委托。');
        const records = await readDrawer('delegation_policies', '', '审阅委托范围', element);
        if (!records) return local;
        updatePolicies(records.items || []);
        const slot = mentionSlots().find((item) => item.id === element.dataset.id);
        if (!slot || !liveJoin(slot) || slot.state !== 'registered')
          throw new Error('请先接通这个仍有效的聊天位置，再设置委托。');
        const generation = state.generation,
          epoch = state.drawerEpoch,
          policyVersion = policyFor(slot.id)?.version || 0;
        const targets = records.execution_target_candidates || [];
        const targetError = targets.length
          ? ''
          : '服务尚未返回此项目的精确执行目标，请升级 Hub 后再设置；已有范围保持不变。';
        if (generation !== state.generation || epoch !== state.drawerEpoch) return local;
        if ((policyFor(slot.id)?.version || 0) !== policyVersion) {
          showDrawer('范围已更新', policyCard(slot), element);
          return { local: true, message: '允许范围已更新，已显示当前版本；正文已保留。' };
        }
        showDrawer('审阅委托范围', delegationForm(slot, targets, targetError), element);
        return local;
      }
      if (action === 'delegation-copy') {
        const card = element.closest('.cc-delegation-policy');
        const slot = mentionSlots().find((item) => item.id === card.dataset.policySlot);
        const policy = slot && policyFor(slot.id);
        if (!policyAvailable(policy) || Number(card.dataset.policyVersion) !== policy.version) {
          if (slot) showDrawer('核对当前允许范围', policyCard(slot), element);
          return {
            local: true,
            message: '允许范围已改变或不可用，请重新选择接入方式；正文已保留。',
          };
        }
        const area = card.querySelector('.cc-delegation-instruction');
        try {
          if (navigator.clipboard?.writeText) {
            await navigator.clipboard.writeText(area.value);
            return {
              local: true,
              message: '已复制接入指令，请在对应原聊天继续；已有范围和订阅会被复用。',
            };
          }
        } catch {}
        const details = area.closest('details');
        if (details) details.open = true;
        area.focus();
        area.select();
        area.setSelectionRange(0, area.value.length);
        return { local: true, message: '已展开并选中接入说明，请使用系统复制。' };
      }
      if (action === 'delegation-pause') {
        const generation = state.generation;
        const result = await mutation('delegation-policy-control', {
          policy_id: element.dataset.id,
          expected_version: Number(element.dataset.version),
          action: 'pause',
        });
        if (generation !== state.generation) return local;
        if (result.policy)
          state.delegationPolicies = state.delegationPolicies.map((p) =>
            p.id === result.policy.id ? result.policy : p,
          );
        closeDrawer();
        patchDelegationComposer();
        await refreshChat();
        return { local: true, message: '已暂停后续委托；已经开始的实际操作请核对目标记录。' };
      }

      return local;
    }
    async function submit(form) {
      const data = Object.fromEntries(new FormData(form));
      if (form.id === 'cc-delegation-scope') {
        const c = chat(),
          policy = selectedPolicy();
        if (
          !selectedDelegationValid() ||
          form.dataset.policyId !== policy.id ||
          Number(form.dataset.policyVersion) !== policy.version
        )
          throw new Error('允许范围已改变，请重新选择本次范围；正文已保留。');
        const formData = new FormData(form),
          targets = formData.getAll('execution_targets'),
          capabilities = [
            'read',
            ...formData.getAll('capabilities').filter((cap) => cap !== 'read'),
          ];
        if (!targets.length) throw new Error('请选择本次在哪些已批准目标处理。');
        if (
          targets.some(
            (id) => !policyTargets(policy).includes(id) || !targetAvailable(policy, id),
          ) ||
          capabilities.some((cap) => !policy.capabilities.includes(cap))
        )
          throw new Error('所选范围已不可用，请重新核对。');
        if (targets.some((id) => id.startsWith('vps:')) && !capabilities.includes('execute'))
          throw new Error('在 VPS 处理需要本次启用运行命令能力。');
        if (capabilities.includes('write') && !targets.includes('project_agent'))
          throw new Error('修改项目文件需要本次包含项目 Agent。');
        c.delegationTargets = targets;
        c.delegationCapabilities = capabilities;
        c.acceptance = String(data.delegation_acceptance || c.acceptance).trim();
        closeDrawer();
        patchDelegationComposer();
        return { local: true, message: '本次范围已选择，可直接发送委托。' };
      }
      if (form.id === 'cc-delegation-policy') {
        if (!form.elements.confirm.checked) throw new Error('请先确认本次委托范围。');
        const formData = new FormData(form);
        const capabilities = formData.getAll('capabilities');
        const targets = formData.getAll('execution_targets');
        if (!capabilities.includes('read')) throw new Error('委托须保留项目读取能力。');
        if (!targets.length) throw new Error('请明确选择至少一个执行目标。');
        const acknowledged = form.elements.acknowledge_unsandboxed_exec.checked;
        if (capabilities.includes('execute') && !acknowledged)
          throw new Error('请明确确认命令执行账号权限与非沙箱边界。');
        if (targets.some((v) => v.startsWith('vps:')) && !capabilities.includes('execute'))
          throw new Error('VPS 委托需要明确选择运行命令能力。');
        if (capabilities.includes('write') && !targets.includes('project_agent'))
          throw new Error('修改项目文件需要明确包含项目 Agent。');
        const generation = state.generation,
          epoch = state.drawerEpoch;
        const result = await mutation('delegation-policy', {
          slot_id: form.dataset.slot,
          expected_version: Number(form.dataset.version),
          purpose: data.purpose.trim(),
          capabilities,
          execution_targets: targets,
          duration_seconds: Number(data.duration_minutes) * 60,
          goal_duration_seconds: Number(data.goal_minutes) * 60,
          max_delegations: Number(data.max_delegations),
          budget: Object.fromEntries(
            ['max_work_items', 'max_steps', 'max_messages', 'max_attempts', 'lease_seconds'].map(
              (name) => [name, Number(data[name])],
            ),
          ),
          acknowledge_unsandboxed_exec: acknowledged,
        });
        if (generation !== state.generation) return { local: true };
        if (result.policy)
          updatePolicies([
            ...state.delegationPolicies.filter((p) => p.slot_id !== result.policy.slot_id),
            result.policy,
          ]);
        patchDelegationComposer();
        const slot = mentionSlots().find((s) => s.id === form.dataset.slot);
        if (epoch === state.drawerEpoch && slot) {
          chooseConnectionMode(
            slot,
            state.delegationConnectionModes?.[slot.id]?.mode || 'managed_execution',
          );
          showDrawer(
            '委托范围已保存',
            policyCard(slot),
            document.querySelector('[data-cc-action="mentions"]'),
          );
        }
        return {
          local: true,
          message: '委托范围已保存。复制接入指令到原聊天继续；以后在此范围内直接选择助手发送。',
        };
      }

      return { local: true };
    }
    return {
      policyFor,
      pickerAction,
      selectedPolicy,
      policyAvailable,
      selectedDelegationValid,
      selectedScopeValid,
      selectPolicy,
      openScope,
      updatePolicies,
      delegationComposer,
      delegationContext,
      patchDelegationComposer,
      policyCard,
      messageCard,
      pendingDelivery,
      act,
      submit,
      change,
    };
  },
};
