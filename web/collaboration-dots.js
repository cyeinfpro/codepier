'use strict';
// Task-dot UX. Authority stays in the server's immutable owner setup and live leases.
window.CodePierCollaborationDots = {
  create(ctx) {
    const { state, E, field, button, when, projectLabel, chat, mentionSlots, delegation,
      showDrawer, closeDrawer, mutation, patchDraftContext, patchDelegationComposer,
      liveJoin, joinInstruction, joinStatus } = ctx;
    let token = null, activeOption = 0;
    const slots = () => mentionSlots().filter((slot) => slot.task_dot);
    const selected = () => slots().find((slot) => slot.id === chat().mentions[0] && (slot.duplex || chat().delegationPolicy));
    const capsLabel = (caps) => (caps || []).map((cap) => ({read: '读取', write: '修改文件', execute: '运行命令'})[cap]).join('、');
    const ready = (slot) => liveJoin(slot) && (slot.duplex ? slot.state === 'registered' && !['authorization_unavailable', 'room_paused'].includes(joinStatus(slot)) : delegation.policyAvailable(delegation.policyFor(slot.id)));
    const transport = (slot) => slot.task_status?.notification_state === 'active';
    function statusText(slot) {
      const status = joinStatus(slot);
      if (status === 'invited') return '等待在 dot 加入';
      if (status === 'code_expired') return '加入码已过期';
      if (status === 'revoked') return '已移除';
      if (status === 'expired') return '已到期';
      if (status === 'authorization_unavailable') return '需要核对原授权';
      if (status === 'room_paused') return '房间已暂停';
      if (!transport(slot)) return slot.duplex ? '已绑定 · 等待消息订阅' : '已绑定 · 等待任务订阅';
      if (slot.task_status?.claim?.active_count) return '正在处理任务';
      return slot.duplex ? '双向消息已连接' : '任务订阅已连接';
    }
    function card(slot) {
      const code = joinInstruction(slot), status = joinStatus(slot);
      const alive = liveJoin(slot), policy = delegation.policyFor(slot.id);
      const task = slot.task_status;
      const explain = status === 'invited' ? '复制加入指令，发到你的 dot。完成一次宿主确认后，就能在这里持续交流和交办。'
        : status === 'code_expired' ? '刷新加入码后，将新指令发到目标 dot。'
        : status === 'authorization_unavailable' ? '原范围或连接已不可用：' + (slot.blocked_reason || '请核对项目授权')
        : transport(slot) ? (slot.duplex ? '像房间成员一样双向交流：你发消息，它回复或主动提问；需要做事时再执行。' : '面板 @ 它发送任务；领取、进度和结果会回复原消息。连接有效不等于模型已处理。')
        : slot.state === 'registered' ? '绑定已完成。将恢复指令发到原 dot，补齐原来的订阅，不必重新加入。'
        : '此 dot 不再接收新任务。已有操作保留原回执，不会重投。';
      return `<article class="cc-card cc-dot-card cc-join-card" data-cc-slot="${E(slot.id)}" data-dot-card="${E(slot.id)}">
        <div class="cc-row"><div class="cc-dot-title"><span class="cc-avatar" aria-hidden="true">●</span><h3>${E(slot.label)}</h3></div><span class="cc-status">${E(statusText(slot))}</span></div>
        <p class="cc-join-hint">${E(explain)}</p><p class="cc-hint">${E(projectLabel(state.project))} · ${E(capsLabel(slot.capabilities))}${slot.execution_target !== 'project_agent' ? ' · 已选 VPS' : ''}</p>
        ${code ? `<div class="cc-join-invitation">${slot.state === 'invited' ? `<div class="cc-row"><strong class="cc-join-code">${E(slot.join_code)}</strong><small>有效至 ${E(when(slot.code_expires_at))}</small></div>` : ''}
          <details class="cc-dot-instruction" ${slot.state === 'invited' ? 'open' : ''}><summary>${slot.state === 'invited' ? '加入指令' : '恢复原 dot 接入'}</summary><textarea class="cc-join-instruction" aria-label="dot 加入或恢复指令" rows="4" readonly>${E(code)}</textarea></details>
          <div class="cc-actions">${button('join-copy', slot.state === 'invited' ? '复制加入指令' : '复制恢复指令', slot)}${slot.state === 'invited' ? '' : button('dot-select', '@' + slot.label + ' 发消息', slot, ready(slot) ? '' : 'disabled')}</div></div>` : ''}
        ${task ? `<div class="cc-dot-evidence" aria-label="实际协作状态"><span>${slot.duplex ? '消息订阅' : '任务订阅'} ${transport(slot) ? '1 / 1' : '0 / 1'}</span><span>已领取 ${E(task.claim?.observed_count || 0)}</span><span>已回传 ${E(task.result?.count || 0)}</span></div>` : ''}
        <small>连接范围有效至 ${E(when(policy?.expires_at || slot.expires_at))}；不新增账号权限。</small>
        ${alive && state.snapshot.can_manage ? `<div class="cc-actions">${slot.state === 'invited' ? button('join-refresh_code', '刷新加入码', slot) : ''}${transport(slot) ? button('join-test', '测试通知链路', slot) : ''}${button('join-revoke', '移除 dot 并停止新任务', slot)}</div>` : ''}
        ${slot.routes?.some((route) => route.test) ? `<details><summary>通知测试回执</summary>${slot.routes.filter((route) => route.test).map((route) => `<p>事件 <code>${E(route.test.event_id)}</code> · ${E(route.test.state)}</p>`).join('')}<small>HTTP 接收不代表 dot 已读取；以原话题实际回复为准。</small></details>` : ''}
      </article>`;
    }
    function simpleComposer() {
      const c = chat();
      return !!state.snapshot?.capabilities?.duplex_dots && slots().some((slot) => slot.duplex) && !c.delegationPolicy &&
        (!c.mentions.length || c.mentions.every((id) => slots().some((slot) => slot.id === id && slot.duplex)));
    }
    function isConversationMessage(message) {
      if (message.sender_dot_id || message.body?.sender_dot_id || message.body?.duplex_recipients?.length) return true;
      const slotId = message.slot_id || message.body?.delegation?.slot_id;
      return !!slots().find((slot) => slot.id === slotId && slot.duplex);
    }
    function render() {
      const d = state.snapshot, draft = state.dotDraft || {}, target = draft.execution_target || 'project_agent';
      const defaults = state.dotDefaults?.project_id === state.project ? state.dotDefaults.capabilities : ['read'];
      const candidates = state.dotTargets?.length ? state.dotTargets : [{id: 'project_agent', label: '当前项目 Agent', available: true}];
      const form = d.can_manage && d.room.state === 'active' ? `<form id="cc-dot-create" class="cc-form" data-default-capabilities="${E(JSON.stringify(defaults))}">
        ${field('dot 名称', `<input name="label" maxlength="80" value="${E(draft.label || '')}" placeholder="例如：项目助手" autocomplete="off" required>`)}
        <p class="cc-hint">直接交流，让 dot 根据上下文决定回答、追问或做事。</p>
        <p class="cc-dot-default-scope">沿用当前项目已有范围：${E(capsLabel(defaults))}。</p>
        <details class="cc-dot-advanced"><summary>项目访问与执行位置（可选）</summary>
          ${field('使用范围', `<select name="access"><option value="work" ${draft.access !== 'read' ? 'selected' : ''}>沿用当前项目已有范围</option><option value="read" ${draft.access === 'read' ? 'selected' : ''}>仅使用读取权限</option></select>`)}
          ${field('执行位置', `<select name="execution_target">${candidates.map((item) => `<option value="${E(item.id)}" ${target === item.id ? 'selected' : ''} ${item.available ? '' : 'disabled'}>${E(item.label)}${item.available ? '' : '（不可用）'}</option>`).join('')}</select>`)}
          <small>加入码 30 分钟有效，原确认范围最多 7 天；可随时移除。这里调整的是访问范围，不是聊天/任务模式。</small>
        </details>
        <p class="cc-dot-consent">点击添加，即允许此 dot 在本房间收发消息，并根据你的要求使用上述已有范围。运行命令使用执行账号权限，并非项目沙箱。</p>
        <button class="btn primary" type="submit">添加 dot，生成加入码</button></form>` : '<p class="cc-hint">由房间管理员在启用的房间添加 dot。</p>';
      return `<section class="cc-stack cc-dots" aria-label="dot 成员"><section class="cc-card"><span class="cc-eyebrow">你的项目助手</span><h2>添加 dot，然后直接 @ 它</h2>
        <div class="cc-dot-steps"><span><b>1</b> 添加 dot</span><span><b>2</b> 发加入码</span><span><b>3</b> 直接交流</span></div>${d.capabilities?.message_notifications ? '' : '<p class="cc-hint">事件服务尚未开启，暂不能自动唤醒 dot。已保存的消息仍会保留；管理员需开启 MCP Events。</p>'}${form}</section>${slots().map(card).join('')}</section>`;
    }
    function picker() {
      const dots = slots().filter(liveJoin);
      return `<p>选择 dot 后直接发消息，可以讨论、追问或交办；后续消息保留接收对象。</p><div class="cc-dot-picker">${dots.map((slot) => ready(slot)
        ? `<button type="button" class="btn cc-dot-option" data-cc-action="dot-select" data-id="${E(slot.id)}"><strong>@${E(slot.label)}</strong><small>${E(statusText(slot))} · ${E(capsLabel(slot.capabilities))}</small></button>`
        : `<div class="cc-row"><span>${E(slot.label)} · ${E(statusText(slot))}</span>${button('dot-connect', '继续接入', slot)}</div>`).join('') || '<p>还没有可用的 dot。</p>'}</div>${button('open-members', '添加或管理 dot')}`;
    }
    function hideSuggestions() {
      const list = document.querySelector('#cc-dot-suggestions'), area = document.querySelector('#cc-message-input');
      if (list) { list.hidden = true; list.innerHTML = ''; }
      area?.setAttribute('aria-expanded', 'false');
      area?.removeAttribute('aria-activedescendant');
      token = null;
    }
    function choose(slot) {
      const policy = delegation.policyFor(slot.id);
      if (!ready(slot) || (!slot.duplex && policy.version !== slot.policy_version)) throw new Error('dot 范围已改变，请重新选择；正文已保留。');
      const c = chat(), area = document.querySelector('#cc-message-input');
      if (token && area && area.value === token.value && area.selectionStart === token.end) {
        area.setRangeText('', token.start, token.end, 'end');
        c.draft = area.value;
      }
      hideSuggestions();
      c.mentions = [slot.id];
      if (slot.duplex) delegation.selectPolicy(null);
      else { c.acceptance = policy.automatic_acceptance; delegation.selectPolicy(policy, policy.version, true); }
      patchDraftContext(); patchDelegationComposer(); closeDrawer(); area?.focus();
    }
    async function act(action, element) {
      if (action === 'dot-clear') {
        chat().mentions = []; delegation.selectPolicy(null); hideSuggestions(); patchDraftContext(); patchDelegationComposer();
      } else {
        const slot = slots().find((item) => item.id === element.dataset.id);
        if (!slot) throw new Error('当前房间已没有这个 dot。');
        if (action === 'dot-select') {
          const navigate = state.view !== 'discussion';
          choose(slot);
          if (navigate) { state.view = 'discussion'; return {local: false}; }
        }
        else if (action === 'dot-connect') showDrawer('接入 ' + slot.label, card(slot), element);
      }
      return {local: true};
    }
    async function submit(form) {
      const data = Object.fromEntries(new FormData(form));
      const target = data.execution_target;
      const inherited = JSON.parse(form.dataset.defaultCapabilities || '["read"]');
      const capabilities = (data.access === 'read' ? ['read'] : inherited).filter((cap) => target === 'project_agent' || cap !== 'write');
      if (!capabilities.includes('read') || (target !== 'project_agent' && !capabilities.includes('execute'))) throw new Error('当前项目范围不允许这个执行位置；可以保留默认项目位置。');
      const generation = state.generation;
      const result = await mutation('dot', {conversation_id: state.conversation || state.snapshot.conversation?.id || state.snapshot.room.id,
        label: data.label.trim(), capabilities,
        execution_target: target, acknowledge_unsandboxed_exec: capabilities.includes('execute'), confirm_tasks: true, duplex: true});
      if (generation !== state.generation) return {local: true};
      state.dotDraft = {};
      state.snapshot.join_slots = [result.dot, ...(state.snapshot.join_slots || []).filter((slot) => slot.id !== result.dot.id)];
      showDrawer('把加入指令发到你的 dot', card(result.dot), document.querySelector('[data-cc-open-joins]'));
      return {local: true, message: result.message};
    }
    function change(event) {
      if (event.target.form?.id === 'cc-dot-create' && event.target.name) {
        state.dotDraft ||= {};
        state.dotDraft[event.target.name] = event.target.value;
      }
      if (event.target.id !== 'cc-message-input' || event.isComposing || state.composing) return;
      const area = event.target, list = document.querySelector('#cc-dot-suggestions');
      if (!list || area.selectionStart !== area.selectionEnd) return hideSuggestions();
      const prefix = area.value.slice(0, area.selectionStart), match = /(?:^|\s)@([^\s@]{0,80})$/u.exec(prefix);
      if (!match) return hideSuggestions();
      const choices = slots().filter((slot) => ready(slot) && slot.label.toLocaleLowerCase().includes(match[1].toLocaleLowerCase()));
      if (!choices.length) return hideSuggestions();
      token = {value: area.value, start: prefix.lastIndexOf('@'), end: area.selectionStart}; activeOption = 0;
      list.innerHTML = choices.map((slot, i) => `<button type="button" role="option" id="cc-dot-option-${i}" aria-selected="${i === 0}" data-cc-action="dot-select" data-id="${E(slot.id)}"><strong>@${E(slot.label)}</strong><small>${E(statusText(slot))}</small></button>`).join('');
      list.hidden = false;
      area.setAttribute('aria-expanded', 'true'); area.setAttribute('aria-controls', list.id); area.setAttribute('aria-activedescendant', 'cc-dot-option-0');
    }
    function keydown(event) {
      const list = document.querySelector('#cc-dot-suggestions');
      if (!list || list.hidden || !token || event.target.id !== 'cc-message-input' || event.isComposing || state.composing) return false;
      const choices = [...list.querySelectorAll('[role="option"]')];
      if (!['ArrowUp', 'ArrowDown', 'Enter', 'Escape'].includes(event.key)) return false;
      event.preventDefault(); event.stopPropagation();
      if (event.key === 'Escape') hideSuggestions();
      else if (event.key === 'Enter') choices[activeOption]?.click();
      else {
        activeOption = (activeOption + (event.key === 'ArrowDown' ? 1 : -1) + choices.length) % choices.length;
        choices.forEach((choice, i) => choice.setAttribute('aria-selected', String(i === activeOption)));
        event.target.setAttribute('aria-activedescendant', choices[activeOption].id);
      }
      return true;
    }
    function refreshCards() {
      for (const node of document.querySelectorAll('[data-dot-card]')) {
        const slot = slots().find((item) => item.id === node.dataset.dotCard);
        if (!slot || node.contains(document.activeElement)) continue;
        const signature = JSON.stringify(slot);
        if (node.dataset.signature === signature) continue;
        const wrapper = document.createElement('div'); wrapper.innerHTML = card(slot);
        const next = wrapper.firstElementChild; next.dataset.signature = signature; node.replaceWith(next);
      }
    }
    return {card, render, picker, selected, simpleComposer, isConversationMessage, act, submit, change, keydown, hideSuggestions, refreshCards};
  },
};
