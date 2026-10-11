'use strict';
// Presentation only: navigation never grants permission, sends messages or starts work.
window.CodePierCollaborationWorkspace = {
  receiptRows(messages, project, environment) {
    const groups = {
      needs_attention: ['attention', '需要关注'],
      waiting_connection: ['attention', '等待连接'],
      working: ['active', '正在处理'],
      replying: ['active', '正在回复'],
      saved: ['waiting', '等待接收'],
      received: ['waiting', '已送达，等待反馈'],
      waiting_dot: ['waiting', '等待继续反馈'],
      replied: ['responded', '已回复'],
      handled: ['responded', '已确认处理'],
      completed: ['responded', '已完成'],
    };
    const priority = ['attention', 'active', 'waiting', 'unknown', 'responded'];
    return (messages || [])
      .filter(
        (m) =>
          ['owner', 'panel_owner'].includes(m.author_kind) &&
          m.project_id === project &&
          m.environment_id === environment &&
          m.relay_receipts?.length,
      )
      .map((message) => {
        const states = message.relay_receipts.map((r) => ({
          state: r.state,
          dot: r.dot_id,
          group: groups[r.state]?.[0] || 'unknown',
          label: groups[r.state]?.[1] || '状态待核对',
        }));
        const primary = [...states].sort(
          (a, b) => priority.indexOf(a.group) - priority.indexOf(b.group),
        )[0];
        return { message, ...primary, receipts: states };
      })
      .sort((a, b) => (b.message.server_sequence || 0) - (a.message.server_sequence || 0));
  },
  create(ctx) {
    const {
      state,
      E,
      button,
      chat,
      mentionSlots,
      projectLabel,
      messageText,
      renderJobs,
      renderAgents,
      renderMonitor,
      bindDetails,
      onOpen = () => {},
    } = ctx;
    const names = { discussion: '对话', jobs: '进展', agents: '连接', monitor: '监控' };
    const symbols = {
      discussion: '<path d="M5 5h14v10H9l-4 4z"/>',
      jobs: '<path d="M5 17V9m7 8V5m7 12v-6"/>',
      agents:
        '<circle cx="9" cy="8" r="3"/><path d="M3 20v-3a6 6 0 0 1 12 0v3m3-13a3 3 0 0 1 0 6m3 7v-3a6 6 0 0 0-3-5"/>',
      monitor: '<path d="M3 12h4l3-7 4 14 3-7h4"/>',
    };
    let filter = 'all',
      railFocus = null,
      composerSelection = null;
    const root = () => document.querySelector('.cc-workspace');
    const rows = () =>
      window.CodePierCollaborationWorkspace.receiptRows(
        chat().messages,
        state.project,
        state.environment,
      );
    const icon = (name) =>
      `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${symbols[name] || symbols.discussion}</svg>`;
    function navigation() {
      return `<nav class="cc-tabs cc-workspace-nav" aria-label="房间工作区">${Object.entries(names)
        .map(
          ([id, label]) =>
            `<button type="button" class="btn ${state.view === id ? 'is-active' : ''}" data-cc-view="${id}" aria-pressed="${state.view === id}" aria-controls="${id === 'discussion' ? 'cc-discussion-host' : 'cc-workspace-panel'}">${icon(id)}<span>${label}</span>${id === 'jobs' ? '<span class="cc-nav-count" data-cc-attention-count hidden></span>' : ''}</button>`,
        )
        .join('')}</nav>`;
    }
    function summary() {
      return '<div class="cc-workspace-pulse" aria-label="当前协作状态"><span class="cc-pulse-label" data-cc-pulse-label></span><button type="button" data-cc-workspace-view="jobs" class="cc-pulse-action">查看进展 <span aria-hidden="true">↗</span></button></div>';
    }
    function welcome() {
      const slots = mentionSlots().filter(
        (s) => s.task_dot && !['revoked', 'expired'].includes(s.state),
      );
      const pending = slots.find((s) => s.state !== 'registered');
      const connected = slots.some((s) => s.state === 'registered');
      const title = connected
        ? '从这里，把事情一起做好。'
        : pending
          ? '再连接一步，就能开始。'
          : '把你的 dot 带进来。';
      const prose = connected
        ? '提问、讨论、交办都在同一段对话。回复和进展会回到这里。'
        : pending
          ? '把加入指令发到目标 dot，按宿主提示完成确认。这里会保留原来的房间。'
          : '添加助手，复制加入指令，然后回到这里直接交流。无需选择聊天或任务模式。';
      const disabled =
        chat().draft || !state.snapshot?.can_manage || state.snapshot?.room?.state !== 'active';
      return `<div class="cc-chat-empty cc-workspace-welcome"><span class="cc-welcome-mark" aria-hidden="true">${icon('discussion')}</span><span class="cc-eyebrow">${E(projectLabel(state.project))} · ${E(state.environment)}</span><h3>${title}</h3><p>${prose}</p>${!state.snapshot?.can_manage ? '<p>当前为只读访问，可以查看已获准的记录。</p>' : connected ? `<div class="cc-prompt-grid" aria-label="消息草稿建议">${['梳理一下当前进展和下一步', '先一起讨论方案，暂时不要修改', '检查目前有哪些问题需要处理'].map((prompt) => `<button type="button" data-cc-prompt="${E(prompt)}" ${disabled ? 'disabled' : ''}>${E(prompt)}<span aria-hidden="true">↗</span></button>`).join('')}</div><small>点击只填入草稿，由你确认发送。</small>` : button('open-members', pending ? '继续连接 dot' : '添加第一个 dot', {}, 'data-cc-onboarding="true"')}</div>`;
    }
    function progress() {
      const all = rows();
      const categories = [
        ['all', '全部'],
        ['attention', '需关注'],
        ['active', '处理中'],
        ['waiting', '待反馈'],
        ['responded', '已反馈'],
      ];
      const cards = all.filter((r) => filter === 'all' || r.group === filter);
      return `<div class="cc-progress-feed"><div class="cc-progress-caption"><h3>对话进展</h3><small>仅当前项目已加载消息的实际回执；已回复不等于执行已验收。</small></div><div class="cc-progress-filters" aria-label="筛选消息进展">${categories.map(([id, name]) => `<button type="button" data-cc-progress-filter="${id}" aria-pressed="${filter === id}">${name}<span>${id === 'all' ? all.length : all.filter((r) => r.group === id).length}</span></button>`).join('')}</div><div class="cc-progress-items">${cards.map((r) => `<article class="cc-progress-card" data-progress-message="${E(r.message.id)}" data-group="${r.group}"><div class="cc-row"><span class="cc-progress-state">${E(r.label)}</span><small>${E(new Date(r.message.created * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }))}</small></div><p>${E(messageText(r.message).slice(0, 180))}</p><div class="cc-actions">${button('source', '回到原消息', r.message)}${r.state === 'waiting_connection' && r.dot ? button('dot-connect', '核对连接', { id: r.dot }) : ''}</div></article>`).join('') || `<div class="cc-progress-empty"><h3>${all.length ? '这个分类暂无消息' : '进展会从对话中自然出现'}</h3><p>${all.length ? '可以查看其他分类，或回到对话继续交流。' : '发出消息后，在这里查看接收、回复和处理状态，不必手工创建任务。'}</p><button type="button" class="btn" data-cc-workspace-view="discussion">回到对话</button></div>`}</div></div>`;
    }
    function content(view) {
      if (view === 'jobs')
        return progress() + '<div class="cc-workspace-records">' + renderJobs() + '</div>';
      if (view === 'agents') return renderAgents();
      if (view === 'monitor') return renderMonitor();
      return '';
    }
    function panel() {
      return `<aside id="cc-workspace-panel" class="cc-workspace-panel" aria-label="房间工作详情" ${state.view === 'discussion' ? 'hidden' : ''}><header class="cc-panel-heading"><div><span class="cc-eyebrow">当前房间</span><h2 id="cc-panel-title" tabindex="-1">${E(names[state.view] || '进展')}</h2></div><button type="button" class="btn" data-cc-workspace-view="discussion" aria-label="关闭详情，返回对话">返回对话 <span aria-hidden="true">×</span></button></header><div class="cc-panel-scroll">${Object.keys(
        names,
      )
        .filter((v) => v !== 'discussion')
        .map(
          (v) =>
            `<section data-cc-panel="${v}" ${state.view === v ? 'data-loaded="true"' : 'hidden inert'}>${state.view === v ? content(v) : ''}</section>`,
        )
        .join('')}</div></aside>`;
    }
    function selection() {
      const area = root()?.querySelector('#cc-message-input');
      if (area) composerSelection = [area.selectionStart, area.selectionEnd];
    }
    function layout() {
      const node = root();
      if (!node) return;
      const host = node.querySelector('#cc-discussion-host');
      const obscured = state.view !== 'discussion' && matchMedia('(max-width: 1100px)').matches;
      if (host) {
        host.inert = obscured;
        host.setAttribute('aria-hidden', String(obscured));
      }
      if (
        !matchMedia('(max-width: 1000px)').matches &&
        node.classList.contains('is-room-list-open')
      )
        rooms(false);
    }
    function open(view, trigger) {
      if (!names[view] || !root() || !state.snapshot?.room) return;
      selection();
      state.view = view;
      const node = root();
      node.dataset.workspaceView = view;
      const holder = node.querySelector('#cc-workspace-panel');
      if (!holder) return;
      holder.hidden = view === 'discussion';
      for (const section of holder.querySelectorAll('[data-cc-panel]')) {
        const visible = section.dataset.ccPanel === view;
        section.hidden = !visible;
        section.inert = !visible;
        if (visible && !section.dataset.loaded) {
          section.innerHTML = content(view);
          section.dataset.loaded = 'true';
        }
      }
      holder.querySelector('#cc-panel-title').textContent = names[view];
      for (const tab of node.querySelectorAll('.cc-workspace-nav [data-cc-view]')) {
        tab.classList.toggle('is-active', tab.dataset.ccView === view);
        tab.setAttribute('aria-pressed', String(tab.dataset.ccView === view));
      }
      layout();
      bindDetails();
      refresh();
      onOpen();
      if (view === 'discussion') {
        const area = node.querySelector('#cc-message-input');
        if (area && !area.disabled) {
          area.focus({ preventScroll: true });
          if (composerSelection) area.setSelectionRange(...composerSelection);
        }
      } else if (
        matchMedia('(max-width: 1100px)').matches ||
        trigger?.closest('#cc-workspace-panel')
      )
        holder.querySelector('#cc-panel-title').focus({ preventScroll: true });
    }
    function safePatch(node, markup) {
      const selected = window.getSelection();
      if (
        !node ||
        node.matches(':active') ||
        node.contains(document.activeElement) ||
        (selected && !selected.isCollapsed && node.contains(selected.anchorNode))
      )
        return;
      if (node.innerHTML !== markup) node.innerHTML = markup;
    }
    function patchRecords(node, markup) {
      // Focus restoration cannot restore a native click whose pressed target
      // was removed. :active also covers WebKit buttons that never take focus.
      // Do not cache skipped markup; the next refresh must still apply it.
      if (!node || node.matches(':active') || node._workspaceMarkup === markup) return;
      const selected = window.getSelection();
      if (selected && !selected.isCollapsed && node.contains(selected.anchorNode)) return;
      const controls = (parent) => [...parent.querySelectorAll('button, summary')];
      const active = document.activeElement;
      const key = (item) =>
        item.dataset.ccAction
          ? `${item.dataset.ccAction}:${item.dataset.id || ''}`
          : item.textContent;
      const focused = node.contains(active) ? key(active) : null;
      const details = new Map(
        [...node.querySelectorAll('details')].map((d) => [
          d.dataset.ccDetail
            ? `${d.dataset.ccDetail}:${d.dataset.id}`
            : d.querySelector('summary')?.textContent,
          d.open,
        ]),
      );
      node.innerHTML = markup;
      node._workspaceMarkup = markup;
      for (const detail of node.querySelectorAll('details')) {
        const id = detail.dataset.ccDetail
          ? `${detail.dataset.ccDetail}:${detail.dataset.id}`
          : detail.querySelector('summary')?.textContent;
        if (details.has(id)) detail.open = details.get(id);
      }
      bindDetails();
      if (focused)
        controls(node)
          .find((item) => key(item) === focused)
          ?.focus({ preventScroll: true });
    }
    function refreshRooms(markup) {
      const node = root(),
        list = node?.querySelector('.cc-room-list');
      if (!list || list.matches(':active')) return;
      const template = document.createElement('template');
      template.innerHTML = markup;
      const fresh = template.content.querySelector('.cc-room-list');
      if (!fresh || list._workspaceMarkup === fresh.innerHTML) return;
      const focusedId = list.contains(document.activeElement)
        ? document.activeElement.dataset.ccRoom
        : null;
      list.innerHTML = fresh.innerHTML;
      list._workspaceMarkup = fresh.innerHTML;
      if (focusedId)
        [...list.querySelectorAll('[data-cc-room]')]
          .find((r) => r.dataset.ccRoom === focusedId)
          ?.focus({ preventScroll: true });
      const search = node.querySelector('#cc-room-search');
      if (search) input({ target: search });
      refresh();
    }
    function draftBadges(node) {
      if (!node) return;
      for (const room of node.querySelectorAll('[data-cc-room]')) {
        const c = state.chats.get(JSON.stringify([state.space, room.dataset.ccRoom]));
        const draft = room.querySelector('.cc-room-draft');
        if (draft) {
          draft.hidden = !c?.draft;
          draft.textContent = c?.draft ? '草稿' : '';
        }
      }
    }
    function refresh() {
      const node = root();
      if (
        !node ||
        !state.snapshot?.room ||
        node.dataset.project !== state.project ||
        node.dataset.environment !== state.environment
      )
        return;
      const all = rows(),
        attention = all.filter((r) => r.group === 'attention').length;
      const active = all.filter((r) => r.group === 'active').length,
        waiting = all.filter((r) => r.group === 'waiting').length;
      const label = node.querySelector('[data-cc-pulse-label]');
      if (label) {
        label.textContent =
          state.snapshot.room.state !== 'active'
            ? '房间已暂停 · 已保存记录仍可查看'
            : attention
              ? `${attention} 条消息需要关注`
              : active
                ? `${active} 条消息正在处理或回复`
                : waiting
                  ? `${waiting} 条消息等待反馈`
                  : '对话、进展与结果，留在同一个房间';
        label.parentElement.dataset.tone = attention ? 'attention' : active ? 'active' : 'quiet';
      }
      const count = node.querySelector('[data-cc-attention-count]');
      if (count) {
        count.textContent = String(attention);
        count.hidden = !attention;
      }
      const progressNode = node.querySelector('.cc-progress-feed');
      if (progressNode) {
        const template = document.createElement('template');
        template.innerHTML = progress();
        patchRecords(
          progressNode.querySelector('.cc-progress-items'),
          template.content.querySelector('.cc-progress-items').innerHTML,
        );
        for (const control of progressNode.querySelectorAll('[data-cc-progress-filter]')) {
          const fresh = template.content.querySelector(
            `[data-cc-progress-filter="${control.dataset.ccProgressFilter}"] span`,
          );
          control.querySelector('span').textContent = fresh.textContent;
        }
      }
      const records = node.querySelector('.cc-workspace-records');
      if (records) patchRecords(records, renderJobs());
      const monitor = node.querySelector('.cc-monitor-status');
      if (monitor) {
        const template = document.createElement('template');
        template.innerHTML = renderMonitor();
        const status = template.content.querySelector('.cc-monitor-status');
        if (status) patchRecords(monitor, status.innerHTML);
      }
      draftBadges(node);
      const welcomeNode = node.querySelector('.cc-workspace-welcome');
      if (welcomeNode) {
        const template = document.createElement('template');
        template.innerHTML = welcome();
        safePatch(welcomeNode, template.content.firstElementChild.innerHTML);
      }
    }
    function rooms(show, trigger) {
      const node = root(),
        rail = node?.querySelector('.cc-room-rail');
      if (!rail) return;
      if (show) railFocus = trigger || document.activeElement;
      node.classList.toggle('is-room-list-open', show);
      rail.classList.toggle('is-open', show);
      node
        .querySelector('[data-cc-workspace="rooms"]')
        ?.setAttribute('aria-expanded', String(show));
      const body = node.querySelector('.cc-room-body');
      if (body) body.inert = show && matchMedia('(max-width: 1000px)').matches;
      if (show) {
        rail.querySelector('#cc-room-search')?.focus();
        onOpen();
      } else if (railFocus?.isConnected) railFocus.focus({ preventScroll: true });
    }
    function handleClick(event) {
      const local = event.target.closest('[data-cc-workspace]');
      if (local) {
        if (local.dataset.ccWorkspace === 'rooms') rooms(true, local);
        else if (local.dataset.ccWorkspace === 'close-rooms') rooms(false);
        return true;
      }
      const filterButton = event.target.closest('[data-cc-progress-filter]');
      if (filterButton) {
        filter = filterButton.dataset.ccProgressFilter;
        const node = root().querySelector('.cc-progress-feed'),
          template = document.createElement('template');
        template.innerHTML = progress();
        node.replaceWith(template.content.firstElementChild);
        root()
          .querySelector(`[data-cc-progress-filter="${filter}"]`)
          ?.focus({ preventScroll: true });
        return true;
      }
      const prompt = event.target.closest('[data-cc-prompt]');
      if (prompt) {
        const area = root().querySelector('#cc-message-input');
        if (area && !area.disabled && !area.value.trim()) {
          area.value = prompt.dataset.ccPrompt;
          area.dispatchEvent(new Event('input', { bubbles: true }));
          area.focus({ preventScroll: true });
        }
        return true;
      }
      return false;
    }
    function input(event) {
      if (event.target.id === 'cc-room-search') {
        const query = event.target.value.trim().toLocaleLowerCase();
        let matched = 0;
        for (const room of root().querySelectorAll('[data-cc-room]')) {
          room.hidden = !room.textContent.toLocaleLowerCase().includes(query);
          if (!room.hidden) matched++;
        }
        const empty = root().querySelector('.cc-room-search-empty');
        if (empty) empty.hidden = !!matched;
      }
      if (event.target.id === 'cc-message-input') {
        const node = root();
        for (const prompt of node.querySelectorAll('[data-cc-prompt]'))
          prompt.disabled =
            !!event.target.value ||
            !state.snapshot?.can_manage ||
            state.snapshot?.room?.state !== 'active';
        // Typing never rebuilds records or forms and never triggers a network read.
        draftBadges(node);
      }
    }
    function keydown(event) {
      if (
        event.isComposing ||
        state.composing ||
        event.keyCode === 229 ||
        root()?.querySelector('#cc-drawer[open]')
      )
        return false;
      if (
        root()?.classList.contains('is-room-list-open') &&
        matchMedia('(max-width: 1000px)').matches
      ) {
        if (event.key === 'Escape') {
          event.preventDefault();
          rooms(false);
          return true;
        }
        if (event.key === 'Tab') {
          const controls = [
            ...root().querySelectorAll(
              '.cc-room-rail button, .cc-room-rail input, .cc-room-rail select, .cc-room-rail summary',
            ),
          ].filter((n) => !n.disabled && n.getClientRects().length);
          const first = controls[0],
            last = controls.at(-1);
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last?.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first?.focus();
          }
          return true;
        }
      }
      if (event.key === 'Escape' && state.view !== 'discussion') {
        event.preventDefault();
        open('discussion');
        return true;
      }
      return false;
    }
    function clear() {
      filter = 'all';
      composerSelection = null;
      railFocus = null;
      const node = root();
      if (!node) return;
      for (const section of node.querySelectorAll('[data-cc-panel]')) {
        section.replaceChildren();
        delete section.dataset.loaded;
      }
      const pulse = node.querySelector('[data-cc-pulse-label]');
      if (pulse) pulse.textContent = '';
      const count = node.querySelector('[data-cc-attention-count]');
      if (count) {
        count.hidden = true;
        count.textContent = '';
      }
    }
    function bind() {
      layout();
      refresh();
    }
    return {
      navigation,
      summary,
      welcome,
      panel,
      open,
      refresh,
      refreshRooms,
      clear,
      bind,
      layout,
      rooms,
      handleClick,
      input,
      keydown,
    };
  },
};
