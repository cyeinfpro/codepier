'use strict';
// The panel relays user messages. It never chooses a model or executes dot work.
window.CodePierCollaborationRelay = {
  create(ctx) {
    const {
      state,
      E,
      button,
      chat,
      chatScope,
      mentionSlots,
      messageById,
      messageText,
      requestId,
      dots,
      delegation,
      patchTimeline,
      patchDraftContext,
      patchDelegationComposer,
      mergeMessages,
      recoverAccess,
    } = ctx;
    let cleanup = null,
      retryTimer = null;
    const capable = () => !!state.snapshot?.capabilities?.native_dot_relay;
    const active = () =>
      capable() && state.view === 'discussion' && mentionSlots().some((s) => s.duplex);
    const focused = () => active() && !state.relayManagement;
    const belongs = (entry) =>
      entry.epoch === (entry.chat.relayEpoch || 0) &&
      entry.session === S.session &&
      entry.space === S.space_id &&
      [...state.chats.values()].includes(entry.chat);
    const visible = (entry) =>
      belongs(entry) &&
      entry.chat === chat() &&
      S.page === 'collaboration' &&
      entry.payload.project === state.project &&
      entry.payload.environment_id === state.environment;
    const pending = (c) => (c.relayOutbox || []).filter((e) => e.phase !== 'sent');
    const currentPending = (c) =>
      pending(c).filter(
        (e) =>
          e.payload.project === state.project && e.payload.environment_id === state.environment,
      );
    const isRelayMessage = (m) => capable() && dots.isConversationMessage(m);
    const labels = {
      saved: '已发送 · 等待 dot 接收',
      received: '已送达 dot',
      replying: 'dot 正在回复',
      replied: '已回复',
      handled: 'dot 已确认处理',
      working: 'dot 正在处理',
      completed: '已完成',
      waiting_dot: '等待 dot 继续反馈',
      needs_attention: '需要关注 · 查看反馈',
      waiting_connection: '已保存 · 等待 dot 连接',
    };
    function statuses(m) {
      return (m.relay_receipts || [])
        .map(
          (r) =>
            `<span class="cc-dot-receipt" data-relay-state="${E(r.state)}" title="状态来自实际插件回执，不代表模型持续在线">${E(labels[r.state] || '状态待同步')}${r.state === 'waiting_connection' ? button('dot-connect', '连接详情', { id: r.dot_id }) : ''}</span>`,
        )
        .join('');
    }
    function renderMessage(m) {
      if (!isRelayMessage(m)) return null;
      const human = ['owner', 'panel_owner'].includes(m.author_kind),
        name = human ? '你' : m.display_name || 'dot';
      const parent = m.reply_to_id && messageById(m.reply_to_id);
      const source = parent
        ? `<button type="button" class="cc-relay-quote" data-cc-action="source" data-id="${E(parent.id)}"><span>${E(parent.display_name === '房主' ? '你' : parent.display_name || '话题')}</span>${E(messageText(parent).slice(0, 100))}</button>`
        : '';
      const proof = delegation.messageCard(m);
      const partial = m.body?.dot_reply_complete === false;
      return `<article class="cc-message cc-relay-turn ${human ? 'is-owner' : 'is-dot'} ${m.body?.system_receipt ? 'is-receipt' : ''}" data-message-id="${E(m.id)}" data-project="${E(m.project_id || state.project)}" data-environment="${E(m.environment_id || state.environment)}" data-source-room="${E(m.source_room_id || m.room_id)}" data-sequence="${E(m.server_sequence || 0)}" data-version="${E(m.version || 1)}">
        <div class="cc-avatar ${human ? 'is-human' : ''}" aria-hidden="true">${E(name.slice(0, 1))}</div>
        <div class="cc-message-content"><div class="cc-message-meta"><strong>${E(name)}</strong><time title="${E(new Date(m.created * 1000).toLocaleString())}">${E(new Date(m.created * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }))}</time>${partial ? '<small>阶段回复</small>' : ''}</div>
        ${source}<div class="cc-rich-text cc-prose" data-rich-message="${E(m.id)}">${E(messageText(m))}</div>
        <div class="cc-relay-delivery">${statuses(m)}</div>
        <div class="cc-relay-extra">${proof ? `<details class="cc-reply-evidence"><summary>查看执行记录</summary>${proof}</details>` : ''}</div>
        <div class="cc-message-actions">${button('reply', '回复', m)}${button('relay-copy', '复制', m)}${button('thread', '查看话题', { id: m.thread_root_id || m.id })}</div></div></article>`;
    }
    function hydrate(root = document) {
      for (const holder of root.querySelectorAll('[data-rich-message]')) {
        const m = messageById(holder.dataset.richMessage);
        const selection = window.getSelection();
        if (selection && !selection.isCollapsed && holder.contains(selection.anchorNode)) continue;
        if (m && typeof chatRichMarkdown === 'function' && holder._chatSource !== messageText(m)) {
          if (holder._chatSource === undefined) holder.replaceChildren();
          chatRichMarkdown(holder, messageText(m));
        }
      }
    }
    function patchMessage(node, next, m) {
      if (!node.matches('.cc-relay-turn') || !next.matches('.cc-relay-turn')) return false;
      const selection = window.getSelection();
      if (selection && !selection.isCollapsed && node.contains(selection.anchorNode))
        return 'defer';
      const holder = node.querySelector('[data-rich-message]');
      if (
        holder &&
        typeof chatRichMarkdown === 'function' &&
        holder._chatSource !== messageText(m)
      ) {
        if (holder._chatSource === undefined) holder.replaceChildren();
        chatRichMarkdown(holder, messageText(m));
      }
      for (const selector of ['.cc-message-meta', '.cc-relay-delivery', '.cc-relay-extra']) {
        const old = node.querySelector(selector),
          fresh = next.querySelector(selector);
        if (
          !old ||
          !fresh ||
          old.innerHTML === fresh.innerHTML ||
          old.contains(document.activeElement)
        )
          continue;
        const opened = old.querySelector('details')?.open;
        old.innerHTML = fresh.innerHTML;
        if (opened && old.querySelector('details')) old.querySelector('details').open = true;
      }
      node.dataset.version = String(m.version || 1);
      return true;
    }
    function toolbar() {
      return capable()
        ? `${button('relay-management', state.relayManagement ? '专注对话' : '管理视图', {}, 'class="cc-relay-manage-toggle"')}`
        : '';
    }
    function chrome() {
      const root = document.querySelector('.collaboration');
      if (!root) return;
      root.classList.toggle('cc-relay-focus', focused());
      root.dataset.relayMulti = String((state.snapshot?.conversation?.projects?.length || 1) > 1);
      const control = root.querySelector('[data-cc-action="relay-management"]');
      if (control) {
        control.textContent = state.relayManagement ? '专注对话' : '管理视图';
        control.hidden = !active();
      }
      if (active()) {
        const c = chat();
        c.relayUsed = true;
        const candidates = mentionSlots().filter(
          (s) =>
            s.duplex &&
            s.state === 'registered' &&
            !['authorization_unavailable', 'revoked', 'expired', 'room_paused'].includes(s.status),
        );
        if (!c.mentions.length && !c.reply && !c.relayRecipientCleared && candidates.length === 1) {
          c.mentions = [candidates[0].id];
          delegation.selectPolicy(null);
          patchDraftContext();
          patchDelegationComposer();
        }
        const empty = root.querySelector('.cc-chat-empty');
        if (empty)
          empty.innerHTML =
            '<h3>和 dot 直接聊</h3><p>提问、讨论、交办都在这里。<br>回复和进展会留在同一段对话。</p>';
      }
      autosize();
    }
    function autosize() {
      const area = document.querySelector('#cc-message-input');
      if (!area || !active()) return;
      const feed = document.querySelector('.cc-feed');
      const bottom = feed && feed.scrollHeight - feed.clientHeight - feed.scrollTop < 70;
      area.style.height = 'auto';
      area.style.height = Math.min(180, Math.max(52, area.scrollHeight)) + 'px';
      area.style.overflowY = area.scrollHeight > 180 ? 'auto' : 'hidden';
      if (bottom) feed.scrollTop = feed.scrollHeight;
    }
    function topicHint() {
      return active() && chat().relayAnchor
        ? `<div class="cc-relay-topic">同一话题，直接继续说${button('relay-new-topic', '新话题')}</div>`
        : '';
    }
    function canSend() {
      const c = chat();
      return (
        capable() &&
        !c.delegationPolicy &&
        c.mentions.length === 1 &&
        mentionSlots().some(
          (s) =>
            s.id === c.mentions[0] &&
            s.duplex &&
            s.state === 'registered' &&
            !['revoked', 'expired'].includes(s.status),
        )
      );
    }
    function network(message = '', error = false) {
      const node = document.querySelector('#cc-relay-network');
      if (node) {
        node.hidden = !message;
        node.textContent = message;
        node.classList.toggle('is-error', error);
      }
    }
    function outboxMarkup(c) {
      return currentPending(c)
        .map(
          (e) =>
            `<article class="cc-relay-pending" data-pending-id="${E(e.id)}"><p>${E(e.payload.body_text)}</p><div role="status">${E({ queued: '等待发送', sending: '正在发送…', checking: '正在核对收件回执…', retry: '连接中断，正在恢复', error: '消息尚未确认送达' }[e.phase] || '等待发送')}${e.phase === 'error' ? button('relay-retry', '重试此消息', { id: e.id }) + `<span>${E(e.error || '')}</span>` : ''}</div></article>`,
        )
        .join('');
    }
    function paintOutbox(c = chat(), scroll = false) {
      if (c !== chat() || S.page !== 'collaboration') return;
      const node = document.querySelector('#cc-relay-outbox'),
        feed = document.querySelector('.cc-feed');
      const bottom = feed && feed.scrollHeight - feed.clientHeight - feed.scrollTop < 70;
      if (node) node.innerHTML = outboxMarkup(c);
      if (feed && (bottom || scroll)) feed.scrollTop = feed.scrollHeight;
    }
    function send(form) {
      const c = chat(),
        text = form.elements.request.value.trim();
      if (!text) return;
      if (pending(c).length >= 20) {
        network('有较多消息等待发送，请先处理未送达的消息。', true);
        return;
      }
      const payload = {
        ...chatScope(),
        room_id: state.snapshot.room.id,
        body_text: text,
        reply_to_id: c.reply,
        mentions: c.mentions.map((slot_id) => ({ slot_id })),
      };
      const previous = c.relayAnchor;
      const anchor =
        !c.reply &&
        previous &&
        previous.dotId === c.mentions[0] &&
        previous.payload.project === payload.project &&
        previous.payload.environment_id === payload.environment_id
          ? previous
          : null;
      const entry = {
        id: requestId(),
        chat: c,
        epoch: c.relayEpoch || 0,
        session: S.session,
        space: S.space_id,
        payload,
        dotId: c.mentions[0],
        anchor,
        phase: 'queued',
        attempts: 0,
        due: 0,
        created: Date.now(),
      };
      c.relayOutbox ||= [];
      c.relayOutbox.push(entry);
      c.relayAnchor = entry;
      c.relayRecent = Date.now();
      c.draft = '';
      c.reply = '';
      form.elements.request.value = '';
      patchDraftContext();
      patchDelegationComposer();
      autosize();
      paintOutbox(c, true);
      form.elements.request.focus({ preventScroll: true });
      void drain(c);
    }
    function adopt(entry, result) {
      if (!belongs(entry) || !result.message?.id) return;
      entry.phase = 'sent';
      entry.message = result.message;
      entry.anchor = null;
      const c = entry.chat,
        existing = c.messages.find((m) => m.id === result.message.id);
      if (!existing) c.messages.push(result.message);
      else if ((existing.version || 1) <= (result.message.version || 1))
        Object.assign(existing, result.message);
      c.messages.sort((a, b) => (a.server_sequence || 0) - (b.server_sequence || 0));
      c.relayOutbox = c.relayOutbox.filter((e) => e !== entry);
      if (visible(entry)) {
        patchTimeline();
        paintOutbox(c);
        patchDraftContext();
        hydrate();
      }
    }
    async function drain(c) {
      if (c.relayFlight || c !== chat() || S.page !== 'collaboration') return;
      c.relayFlight = true;
      try {
        while (true) {
          // Preserve order within a topic. A failed unrelated topic must not
          // block the whole conversation; only unresolved children wait for it.
          const e = currentPending(c).find(
            (row) =>
              row.phase !== 'error' && row.due <= Date.now() && (!row.anchor || row.anchor.message),
          );
          if (!e || !visible(e)) break;
          if (e.anchor)
            e.payload.reply_to_id = e.anchor.message.thread_root_id || e.anchor.message.id;
          if (navigator.onLine === false) {
            e.phase = 'retry';
            e.due = Date.now() + 3000;
            network('当前离线，消息和草稿保留在本页。恢复连接后继续发送。', true);
            break;
          }
          e.phase = 'sending';
          e.attempts++;
          paintOutbox(c);
          try {
            const result = await api('/api/collaboration/message', {
              method: 'POST',
              retrySafe: true,
              retryDelays: [],
              requestTimeout: 10000,
              body: JSON.stringify({
                ...e.payload,
                client_message_id: e.id,
                idempotency_key: e.id,
              }),
            });
            if (!belongs(e)) break;
            if (!result.message?.id)
              throw Object.assign(new Error('消息回执格式不完整'), { code: 'NETWORK_UNCERTAIN' });
            adopt(e, result);
          } catch (error) {
            if (!belongs(e)) break;
            // An auth/validation failure is not a connectivity retry.
            if (error.status && ![408, 429, 500, 502, 503, 504].includes(error.status)) {
              e.phase = 'error';
              e.error = error.message;
              continue;
            }
            e.phase = 'checking';
            paintOutbox(c);
            try {
              const q = new URLSearchParams({
                project: e.payload.project,
                environment_id: e.payload.environment_id,
                conversation_id: e.payload.conversation_id || '',
                kind: 'message_status',
                client_message_id: e.id,
              });
              const found = await api('/api/collaboration?' + q, {
                retryDelays: [],
                requestTimeout: 8000,
              });
              if (!belongs(e)) break;
              adopt(e, found);
              if (e.phase === 'sent') continue;
            } catch (checkError) {
              if (!belongs(e)) break;
              if (checkError.status && [401, 403, 409].includes(checkError.status)) {
                e.phase = 'error';
                e.error = checkError.message;
                break;
              }
            }
            e.phase = e.attempts < 3 ? 'retry' : 'error';
            e.error = '请重试核对原消息，不必重新输入。';
            e.due = Date.now() + Math.min(10000, 1000 * 2 ** e.attempts);
            break;
          }
        }
      } finally {
        c.relayFlight = false;
        paintOutbox(c);
        const e = currentPending(c)
          .filter((row) => row.phase === 'retry' && (!row.anchor || row.anchor.message))
          .sort((a, b) => a.due - b.due)[0];
        clearTimeout(retryTimer);
        if (e && visible(e) && e.phase === 'retry')
          retryTimer = setTimeout(() => void drain(c), Math.max(500, e.due - Date.now()));
      }
    }
    async function sync() {
      const c = chat(),
        gen = state.generation;
      if (c.relaySync) return;
      const sequence = ++state.refreshSequence;
      c.relaySync = true;
      const current = () =>
        sequence === state.refreshSequence &&
        gen === state.generation &&
        c === chat() &&
        S.session === state.session &&
        S.space_id === state.space;
      try {
        const candidates = [
          ...new Map(
            [
              ...c.messages.filter(
                (m) =>
                  m.body?.dot_reply_complete === false ||
                  (m.relay_receipts || []).some(
                    (r) =>
                      !['replied', 'completed', 'handled', 'needs_attention'].includes(r.state),
                  ),
              ),
              ...c.messages.filter((m) => m.relay_receipts?.length).slice(-8),
            ].map((m) => [m.id, m]),
          ).values(),
        ];
        const offset = (c.relayTrackOffset || 0) % Math.max(candidates.length, 1);
        const tracked = [...candidates.slice(offset), ...candidates.slice(0, offset)].slice(0, 24);
        c.relayTrackOffset = offset + tracked.length;
        const result = await api(
          '/api/collaboration/relay-sync?' +
            new URLSearchParams({
              ...chatScope(),
              after: c.after,
              tracked: tracked.map((m) => m.id).join(','),
            }),
          { signal: state.controller?.signal, retryDelays: [], requestTimeout: 8000 },
        );
        if (!current()) return;
        if (c.visibility && result.visibility_token !== c.visibility) {
          await recoverAccess(true);
          return;
        }
        c.visibility = result.visibility_token;
        c.after = result.after_cursor || c.after;
        mergeMessages([...(result.updates || []), ...(result.items || [])]);
        if (result.join_slots)
          state.snapshot.join_slots = result.join_slots.map((slot) => ({
            ...state.snapshot.join_slots?.find((s) => s.id === slot.id),
            ...slot,
          }));
        state.members = state.members.map((m) => ({
          ...m,
          ...result.join_slots?.find((s) => s.id === (m.slot_id || m.id)),
        }));
        state.snapshot.room.state = result.room_state;
        state.snapshot.can_manage = result.can_manage;
        c.relayFailures = 0;
        network();
        for (const entry of [...pending(c)]) {
          const m = c.messages.find((m) => m.client_message_id === entry.id);
          if (m) adopt(entry, { message: m });
        }
        patchTimeline();
        hydrate();
        patchDelegationComposer();
        dots.refreshCards();
        const area = document.querySelector('#cc-message-input');
        if (area) {
          area.disabled = result.room_state !== 'active' || !state.snapshot.can_manage;
          area.form.querySelector('[type="submit"]').disabled = area.disabled;
        }
        c.relayMore = result.has_more;
      } catch (error) {
        if (!current()) return;
        if ([401, 403].includes(error.status) || error.code === 'INVALID_CURSOR') {
          await recoverAccess(error.status !== 403);
          return;
        }
        c.relayFailures = (c.relayFailures || 0) + 1;
        network('连接暂时中断，已发送的消息保留在服务端；正在恢复反馈。', true);
      } finally {
        c.relaySync = false;
        if (c === chat() && S.page === 'collaboration') void drain(c);
      }
    }
    function delay() {
      const c = chat();
      if (document.hidden) return 15000;
      if (c.relayFailures) return Math.min(30000, 2000 * 2 ** Math.min(c.relayFailures, 4));
      if (c.relayMore) return 250;
      if (
        pending(c).length ||
        Date.now() - (c.relayRecent || 0) < 60000 ||
        c.messages.some(
          (m) =>
            m.body?.dot_reply_complete === false ||
            (m.relay_receipts || []).some((r) =>
              ['working', 'replying', 'received', 'saved'].includes(r.state),
            ),
        )
      )
        return 1500;
      return 5000;
    }
    function detach() {
      cleanup?.();
      cleanup = null;
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    function purge(c) {
      c.relayEpoch = (c.relayEpoch || 0) + 1;
      c.relayOutbox = [];
      c.relayAnchor = null;
      c.relayRecent = 0;
    }
    function bind(root) {
      detach();
      chrome();
      hydrate(root);
      paintOutbox();
      const wake = () => {
        if (root.isConnected && S.page === 'collaboration' && !document.hidden) {
          const c = chat();
          for (const e of pending(c)) if (e.phase === 'retry') e.due = 0;
          void drain(c);
          if (active()) void sync();
        }
      };

      const select = () => {
        if (active() && !state.busy && root.isConnected && window.getSelection()?.isCollapsed)
          patchTimeline();
      };
      window.addEventListener('online', wake);
      document.addEventListener('visibilitychange', wake);
      document.addEventListener('selectionchange', select);
      root.addEventListener('input', (e) => {
        if (e.target.id === 'cc-message-input') autosize();
      });
      const dialog = root.querySelector('#cc-drawer');
      dialog?.addEventListener('click', (e) => {
        if (e.target === dialog) {
          const r = dialog.getBoundingClientRect();
          if (
            e.clientX < r.left ||
            e.clientX > r.right ||
            e.clientY < r.top ||
            e.clientY > r.bottom
          )
            dialog.close();
        }
      });
      cleanup = () => {
        window.removeEventListener('online', wake);
        document.removeEventListener('visibilitychange', wake);
        document.removeEventListener('selectionchange', select);
      };
      void drain(chat());
    }
    function action(name, node) {
      if (!name.startsWith('relay-')) return false;
      if (name === 'relay-management') {
        state.relayManagement = !state.relayManagement;
        chrome();
      }
      if (name === 'relay-new-topic') {
        chat().relayAnchor = null;
        chat().reply = '';
        patchDraftContext();
        document.querySelector('#cc-message-input')?.focus();
      }
      if (name === 'relay-retry') {
        const e = pending(chat()).find((e) => e.id === node.dataset.id);
        if (e && visible(e)) {
          e.phase = 'retry';
          e.due = 0;
          e.attempts = 0;
          void drain(chat());
        }
      }
      if (name === 'relay-copy') {
        const text = messageById(node.dataset.id);
        if (text)
          void navigator.clipboard
            .writeText(messageText(text))
            .then(() => {
              if (node.isConnected) node.textContent = '已复制';
            })
            .catch(() => network('复制未成功，请选中文字复制。', true));
      }
      return true;
    }
    window.addEventListener('beforeunload', (event) => {
      if ([...state.chats.values()].some((c) => c.relayUsed && (c.draft || pending(c).length))) {
        event.preventDefault();
        event.returnValue = '';
      }
    });
    return {
      active,
      focused,
      capable,
      isRelayMessage,
      renderMessage,
      hydrate,
      patchMessage,
      toolbar,
      chrome,
      autosize,
      topicHint,
      canSend,
      send,
      sync,
      delay,
      detach,
      purge,
      bind,
      action,
      outboxMarkup,
      paintOutbox,
      network,
    };
  },
};
