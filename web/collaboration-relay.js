'use strict';
// The panel relays user messages. It never chooses a model or executes dot work.
window.CodePierCollaborationRelay = {
  // Only finite presentation states and navigation actions are accepted. This is
  // a read-only view, never an execution or permission dispatcher.
  states: {
    saved_waiting_dot: ['waiting', '已保存，等待 dot'],
    observed: ['waiting', 'dot 已接收'],
    waiting_dot: ['waiting', '等待 dot 继续反馈'],
    processing: ['active', '正在处理'],
    waiting_input: ['attention', '等待你补充'],
    waiting_authorization: ['attention', '需要授权'],
    original_operation_uncertain: ['attention', '正在核对原操作'],
    awaiting_result_publication: ['waiting', '执行已有结果，等待回复'],
    replied: ['responded', '已回复'],
    completed: ['responded', '已完成'],
    work_completed_acceptance_unverified: ['attention', '工作完成／验收待核验'],
    result_saved_channel_uncertain: ['attention', '结果已保存，部分通道待核对'],
    needs_attention: ['attention', '需要关注'],
    waiting_connection: ['attention', '已保存，等待连接'],
    no_response_required: ['responded', '无需回复（dot 明确决定）'],
    legacy_handled: ['responded', '已处理，旧回执未细分'],
  },
  presentReceipt(receipt) {
    const aliases = {
      saved: 'saved_waiting_dot',
      received: 'observed',
      working: 'processing',
      replying: 'processing',
      handled: 'legacy_handled',
    };
    const projection = receipt.experience?.read_only === true ? receipt.experience : null;
    const state = projection?.primary_state || aliases[receipt.state] || receipt.state;
    const entry = this.states[state] || ['unknown', '状态待核对'];
    return { state, group: entry[0], label: entry[1], dot: receipt.dot_id, projection, receipt };
  },
  primaryReceipt(message) {
    const priority = ['attention', 'active', 'waiting', 'unknown', 'responded'];
    return (message.relay_receipts || [])
      .map((r) => this.presentReceipt(r))
      .sort((a, b) => priority.indexOf(a.group) - priority.indexOf(b.group))[0];
  },
  diagnosticSummary(message) {
    // Explicit allowlist, no object spreading: body, paths, URLs, tokens, join
    // codes and unrelated fields can never become diagnostic output.
    const fingerprint = (value) => {
      let hash = 2166136261;
      for (const char of String(value || '')) hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
      return (hash >>> 0).toString(16).padStart(8, '0');
    };
    const reasons = new Set([
      'result_committed',
      'original_operation_uncertain',
      'awaiting_result_publication',
      'original_operation_pending',
      'awaiting_consumer',
      'waiting_input',
      'waiting_authorization',
      'reply_committed',
      'covered',
      'no_response_required',
      'legacy_handled',
      'subscription_missing',
      'room_paused',
      'authorization_unavailable',
      'channel_delivery_uncertain',
      'acceptance_unverified',
    ]);
    return JSON.stringify(
      {
        schema_version: 1,
        read_only: true,
        object: 'message-' + fingerprint(message.id),
        receipts: (message.relay_receipts || []).map((receipt) => {
          const view = this.presentReceipt(receipt),
            p = view.projection;
          return {
            dot: 'dot-' + fingerprint(receipt.dot_id),
            state: this.states[view.state] ? view.state : 'unknown',
            reason: reasons.has(p?.reason_code) ? p.reason_code : 'unverified',
            observed_at: Number.isFinite(p?.observed_at) ? p.observed_at : null,
            evidence: [
              'durable_receipts',
              'task_liveness',
              'consumer_report',
              'message_handling',
              'connection_readiness',
            ].includes(p?.evidence_source)
              ? p.evidence_source
              : 'unverified',
          };
        }),
        unverified: ['模型持续在线', '外部客户端显示或已读'],
      },
      null,
      2,
    );
  },
  pendingPayload(value, dotId) {
    const id = (value) => typeof value === 'string' && value.length > 0 && value.length <= 256;
    if (
      !value ||
      typeof value !== 'object' ||
      !id(dotId) ||
      !id(value.project) ||
      !id(value.environment_id) ||
      !id(value.room_id) ||
      !id(value.conversation_id || value.room_id) ||
      typeof value.body_text !== 'string' ||
      !value.body_text.trim() ||
      !Array.isArray(value.mentions) ||
      value.mentions.length !== 1 ||
      value.mentions[0]?.slot_id !== dotId ||
      (value.reply_to_id != null && value.reply_to_id !== '' && !id(value.reply_to_id)) ||
      (value.pending_scope_revision != null && !/^[a-f0-9]{64}$/.test(value.pending_scope_revision))
    )
      return null;
    return {
      project: value.project,
      environment_id: value.environment_id,
      room_id: value.room_id,
      conversation_id: value.conversation_id || value.room_id,
      body_text: value.body_text,
      reply_to_id: value.reply_to_id || '',
      mentions: [{ slot_id: dotId }],
      ...(value.pending_scope_revision
        ? { pending_scope_revision: value.pending_scope_revision }
        : {}),
    };
  },
  pendingStore(storage, owner) {
    const key = 'codepier-relay-pending-v1',
      ttl = 24 * 60 * 60 * 1000;
    const empty = () => ({ version: 1, owner, enabled: false, records: [] });
    const read = () => {
      try {
        const raw = storage.getItem(key);
        if (!raw) return empty();
        if (new TextEncoder().encode(raw).length > 262144) {
          storage.removeItem(key);
          return empty();
        }
        const value = JSON.parse(raw);
        if (
          !owner ||
          value.version !== 1 ||
          value.owner !== owner ||
          !Array.isArray(value.records)
        ) {
          storage.removeItem(key);
          return empty();
        }
        value.records = value.records
          .filter(
            (r) =>
              r &&
              typeof r.id === 'string' &&
              r.id.length <= 256 &&
              typeof r.scope === 'string' &&
              r.scope.length <= 65536 &&
              /^[a-f0-9]{64}$/.test(r.authorization_revision || '') &&
              Number.isFinite(r.created) &&
              r.created <= Date.now() &&
              Date.now() - r.created < ttl,
          )
          .slice(0, 20)
          .map((r) => {
            const payload = window.CodePierCollaborationRelay.pendingPayload(r.payload, r.dotId);
            if (
              !payload ||
              (payload.pending_scope_revision &&
                payload.pending_scope_revision !== r.authorization_revision)
            )
              return null;
            return {
              id: r.id,
              scope: r.scope,
              created: r.created,
              dotId: r.dotId,
              payload,
              authorization_revision: r.authorization_revision,
              anchorId:
                typeof r.anchorId === 'string' && r.anchorId.length <= 256 ? r.anchorId : null,
              anchorMessage:
                r.anchorMessage &&
                typeof r.anchorMessage.id === 'string' &&
                typeof r.anchorMessage.thread_root_id === 'string'
                  ? { id: r.anchorMessage.id, thread_root_id: r.anchorMessage.thread_root_id }
                  : null,
            };
          })
          .filter(Boolean);
        storage.setItem(key, JSON.stringify(value));
        return value;
      } catch {
        return empty();
      }
    };
    const write = (value) => {
      const text = JSON.stringify(value);
      if (new TextEncoder().encode(text).length > 262144 || value.records.length > 20)
        throw new Error('本机待发保存已满；此消息仅保留在本页。');
      storage.setItem(key, text);
    };
    return {
      enabled: () => !!owner && read().enabled === true,
      enable: () => {
        if (!owner) return false;
        const value = read();
        value.enabled = true;
        write(value);
        return true;
      },
      clear: () => {
        storage.removeItem(key);
      },
      remove: (id) => {
        const value = read();
        value.records = value.records.filter((r) => r.id !== id);
        write(value);
      },
      save: (record) => {
        const value = read();
        if (!owner || !value.enabled) return false;
        value.records = value.records.filter((r) => r.id !== record.id);
        const payload = window.CodePierCollaborationRelay.pendingPayload(
          record.payload,
          record.dotId,
        );
        if (
          !payload ||
          (payload.pending_scope_revision &&
            payload.pending_scope_revision !== record.authorization_revision)
        )
          throw new Error('原待发消息结构不完整，仍仅保留在本页。');
        value.records.push({
          id: record.id,
          scope: record.scope,
          created: record.created,
          dotId: record.dotId,
          payload,
          authorization_revision: record.authorization_revision,
          anchorId: record.anchorId || null,
          anchorMessage: record.anchorMessage
            ? { id: record.anchorMessage.id, thread_root_id: record.anchorMessage.thread_root_id }
            : null,
        });
        write(value);
        return true;
      },
      recover: (scope) => {
        const value = read();
        if (!owner || !value.enabled) return [];
        return value.records.filter((r) => r.scope === scope);
      },
    };
  },
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
      onWorkspaceChange = () => {},
    } = ctx;
    let cleanup = null,
      retryTimer = null,
      sizeFrame = 0,
      selectionFrame = 0,
      sizeMemo = null;
    let pendingStore = null,
      storeOwner = null;
    function localStore() {
      const owner =
        S.session?.user_id && S.space_id ? JSON.stringify([S.session.user_id, S.space_id]) : '';
      if (owner !== storeOwner) {
        pendingStore?.clear();
        storeOwner = owner;
        try {
          pendingStore = window.CodePierCollaborationRelay.pendingStore(localStorage, owner);
        } catch {
          pendingStore = null;
        }
      }
      return pendingStore;
    }
    function persistenceScope() {
      const c = chat();
      if (
        !S.session?.user_id ||
        !c.visibility ||
        !state.snapshot?.can_manage ||
        !/^[a-f0-9]{64}$/.test(state.snapshot.pending_scope_revision || '')
      )
        return '';
      return JSON.stringify([
        S.session.user_id,
        S.space_id,
        chatScope(),
        c.visibility,
        state.snapshot.pending_scope_revision,
        mentionSlots().map((s) => [s.id, s.version || 0, s.scope_manifest?.scope_digest || null]),
      ]);
    }
    function savePending(entry) {
      if (!entry.persistenceScope) return;
      try {
        localStore()?.save({
          id: entry.id,
          scope: entry.persistenceScope,
          created: entry.created,
          payload: entry.payload,
          dotId: entry.dotId,
          authorization_revision: entry.authorizationRevision,
          anchorId: entry.anchor?.id || null,
          anchorMessage: entry.anchor?.message
            ? { id: entry.anchor.message.id, thread_root_id: entry.anchor.message.thread_root_id }
            : null,
        });
      } catch (error) {
        network(error.message, true);
      }
    }
    function restorePending() {
      const c = chat(),
        scope = persistenceScope();
      if (!scope || c.relayRestoredScope === scope) return;
      c.relayRestoredScope = scope;
      const records = localStore()?.recover(scope) || [];
      c.relayOutbox ||= [];
      const entries = new Map(c.relayOutbox.map((e) => [e.id, e]));
      for (const record of records) {
        if (
          entries.has(record.id) ||
          typeof record.id !== 'string' ||
          typeof record.payload?.body_text !== 'string' ||
          record.payload.project !== state.project ||
          record.payload.environment_id !== state.environment ||
          !mentionSlots().some((s) => s.id === record.dotId)
        )
          continue;
        const entry = {
          ...record,
          persistenceScope: scope,
          authorizationRevision: record.authorization_revision,
          chat: c,
          epoch: c.relayEpoch || 0,
          session: S.session,
          space: S.space_id,
          phase: 'retry',
          attempts: 0,
          due: 0,
          needsReceipt: true,
        };
        entries.set(entry.id, entry);
        c.relayOutbox.push(entry);
      }
      for (const entry of entries.values())
        if (entry.anchorId) {
          entry.anchor = entry.anchorMessage
            ? { message: entry.anchorMessage }
            : entries.get(entry.anchorId);
          if (!entry.anchor) {
            entry.phase = 'error';
            entry.error = '原话题前序消息缺少回执，请先核对。';
          }
        }
    }
    const capable = () => !!state.snapshot?.capabilities?.native_dot_relay;
    const active = () => capable() && mentionSlots().some((s) => s.duplex);
    const focused = () => active();
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
    function taskEvidence(m) {
      const c = chat();
      const tasks = c.relayTaskStates?.[m.id] || [];
      const reasons = {
        result_committed: '结果已保存',
        original_operation_uncertain: '原操作结果待核验，请勿重复执行',
        awaiting_result_publication: '执行已结束，等待结果回传',
        original_operation_pending: '原操作尚未结束',
        awaiting_consumer: '等待插件继续反馈',
      };
      const stamp = (value) =>
        Number.isFinite(value)
          ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false })
          : '尚无回执';
      const serverNow = Number.isFinite(c.relayServerNow)
        ? c.relayServerNow + Math.max(0, performance.now() - c.relayReceivedAt) / 1000
        : null;
      return tasks
        .map((t) => {
          const observed =
            (t.pending_count || t.uncertain_count) && Object.hasOwn(t, 'oldest_pending_verified_at')
              ? t.oldest_pending_verified_at
              : t.last_operation_verified_at;
          const age = serverNow !== null && Number.isFinite(observed) ? serverNow - observed : null;
          const freshness =
            age === null || age < 0
              ? '暂无实时状态证据'
              : age < 45
                ? '近期收到执行节点回执'
                : '执行证据已过时，等待核对';
          return `<details class="cc-task-liveness" data-work-id="${E(t.work_item_id)}"><summary>${E(reasons[t.wait_reason] || '等待核对原任务')} · ${E(freshness)}</summary>
          <div>执行状态：${E(t.execution_state)}；结果保存：${t.delivery_state === 'committed' ? '已确认' : '待确认'}；验收：未由房主确认</div>
          ${t.last_progress_summary ? `<div>最近进展：${E(t.last_progress_summary)}</div>` : ''}
          <div>操作回执：${E(stamp(t.last_operation_verified_at))}；输出：${E(stamp(t.last_output_at))}</div>
          <div>插件心跳：${E(stamp(t.last_consumer_heartbeat_at))}；节点联络：${E(stamp(t.last_node_contact_at))}</div>
          <small>${t.scope_boundary?.ordinary_tools_room_scoped ? '普通工具已按服务器上的原凭据限制到房间范围' : '当前仅约束委托步骤，普通工具尚未按房间强隔离'}；不是操作系统沙箱。以上证据不表示模型持续在线。操作数：${E(t.operation_count)}；不确定：${E(t.uncertain_count)}</small>
        </details>`;
        })
        .join('');
    }
    function evidenceProjection() {
      // Execution evidence and its freshness can change without a message edit.
      // Recompute only those bounded records, not every message's rich text.
      const entries = Object.keys(chat().relayTaskStates || {})
        .sort()
        .map((id) => [id, taskEvidence({ id })])
        .filter(([, markup]) => markup);
      return { signature: JSON.stringify(entries), byMessage: new Map(entries) };
    }
    function statuses(m) {
      const view = window.CodePierCollaborationRelay.primaryReceipt(m);
      if (!view) return taskEvidence(m);
      const p = view.projection,
        action = p?.next_action;
      const actions = new Set([
        'open_original_message',
        'inspect_original_receipt',
        'resume_enrollment',
        'open_scope_review',
        'open_host_confirmation',
        'copy_original_join_instruction',
      ]);
      let next = '';
      if (action && action.mutates === false && actions.has(action.kind)) {
        if (
          ['resume_enrollment', 'open_scope_review', 'copy_original_join_instruction'].includes(
            action.kind,
          )
        )
          next = button('dot-connect', '查看原连接', { id: view.dot });
        else if (action.kind === 'open_original_message' && messageById(action.target_ref))
          next = button('source', '查看原消息', { id: action.target_ref });
        else
          next = button(
            'relay-inspect',
            action.kind === 'open_host_confirmation' ? '查看授权说明' : '查看原回执',
            m,
          );
      } else if (view.state === 'waiting_connection')
        next = button('dot-connect', '连接详情', { id: view.dot });
      const waiting = [
        'waiting_input',
        'waiting_authorization',
        'original_operation_uncertain',
      ].includes(view.state);
      const waitingId = waiting
        ? [m.id, view.dot, view.state, p?.reason_code || '', p?.approval_version || ''].join(':')
        : '';
      const detail = (m.relay_receipts || [])
        .map((r) => {
          const v = window.CodePierCollaborationRelay.presentReceipt(r);
          return '<li>' + E(v.label) + '</li>';
        })
        .join('');
      const receiptNote =
        view.state === 'observed' ? '已确认插件收件，不代表已经处理、执行任务或模型持续在线' : '';
      const notice =
        view.state === 'waiting_authorization'
          ? '<p>dot 报告需要确认。请在原 dot 界面查看真实请求；此处不会批准权限。</p>'
          : receiptNote
            ? '<p>' + E(receiptNote) + '</p>'
            : '';
      return `<div class="cc-experience-status" data-primary-state="${E(view.state)}" data-waiting-id="${E(waitingId)}"><span class="cc-dot-receipt" data-relay-state="${E(view.receipt?.state || view.state)}"${receiptNote ? ` title="${E(receiptNote)}"` : ''}>${E(view.label)}</span>${next}</div>
        <details class="cc-receipt-detail" data-receipt-detail="${E(m.id)}"><summary>回执与诊断详情</summary>${notice}<ul>${detail}</ul>
          <details class="cc-diagnostic-preview"><summary>预览脱敏诊断摘要</summary><p>仅含状态、脱敏对象与已核验时间；不含正文、路径或凭据。</p><pre>${E(window.CodePierCollaborationRelay.diagnosticSummary(m))}</pre>${button('relay-diagnostic-copy', '复制这份诊断摘要', m)}</details>
        </details>${taskEvidence(m)}`;
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
        <div class="cc-message-content"><div class="cc-message-meta"><strong>${E(name)}</strong><time title="${E(new Date(m.created * 1000).toLocaleString())}">${E(new Date(m.created * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }))}</time>${partial ? '<small>阶段回复</small>' : ''}${m.source_channel ? `<span class="cc-channel-origin" data-source-channel="${E(m.source_channel)}">${E(m.source_channel === 'telegram' ? 'Telegram' : '微信')} · 同一协作室</span>` : ''}</div>
        ${source}<div class="cc-rich-text cc-prose" data-rich-message="${E(m.id)}">${E(messageText(m))}</div>
        <div class="cc-relay-attachments">${window.CodePierCollaborationChannels?.attachments(m, E) || ''}</div>
        ${partial ? `<div class="cc-stage-card" data-stage-id="${E(m.id)}" data-stage-version="${E(m.version || 1)}"><span>dot 阶段反馈</span><small>不作为执行节点心跳或验收证据</small></div>` : ''}
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
      if ((m.version || 1) < Number(node.dataset.version || 1)) return true;
      let deferred = false;
      const selection = window.getSelection();
      const selected =
        selection &&
        !selection.isCollapsed &&
        (node.contains(selection.anchorNode) || node.contains(selection.focusNode));
      if (selected) {
        // Keep selected body text intact while exposing the current lightweight
        // state. The full patch is retried on selectionchange.
        const oldStatus = node.querySelector('.cc-experience-status');
        const newStatus = next.querySelector('.cc-experience-status');
        if (
          oldStatus &&
          newStatus &&
          !oldStatus.contains(selection.anchorNode) &&
          !oldStatus.contains(selection.focusNode) &&
          oldStatus.innerHTML !== newStatus.innerHTML
        )
          oldStatus.replaceWith(newStatus);
        return 'defer';
      }
      const holder = node.querySelector('[data-rich-message]');
      if (
        holder &&
        typeof chatRichMarkdown === 'function' &&
        holder._chatSource !== messageText(m)
      ) {
        if (holder._chatSource === undefined) holder.replaceChildren();
        const horizontal = [...holder.querySelectorAll('pre, .chat-table-scroll')].map(
          (n) => n.scrollLeft,
        );
        chatRichMarkdown(holder, messageText(m));
        [...holder.querySelectorAll('pre, .chat-table-scroll')].forEach(
          (n, i) => (n.scrollLeft = horizontal[i] || 0),
        );
      }
      const quote = node.querySelector('.cc-relay-quote'),
        nextQuote = next.querySelector('.cc-relay-quote');
      if (quote?.outerHTML !== nextQuote?.outerHTML) {
        if (quote && (quote.matches(':active') || quote.contains(document.activeElement)))
          deferred = true;
        else if (quote && nextQuote) quote.replaceWith(nextQuote);
        else if (quote) quote.remove();
        else if (nextQuote && holder) holder.before(nextQuote);
      }
      for (const selector of [
        '.cc-message-meta',
        '.cc-stage-card',
        '.cc-relay-delivery',
        '.cc-relay-extra',
        '.cc-relay-attachments',
        '.cc-message-actions',
      ]) {
        const old = node.querySelector(selector),
          fresh = next.querySelector(selector);
        if (selector === '.cc-stage-card') {
          if (old && !fresh) {
            old.remove();
            continue;
          }
          if (!old && fresh) {
            node.querySelector('.cc-relay-delivery')?.before(fresh);
            continue;
          }
        }
        if (!old || !fresh || old.innerHTML === fresh.innerHTML) continue;
        if (old.matches(':active') || old.contains(document.activeElement)) {
          deferred = true;
          continue;
        }
        const opened = new Set(
          [...old.querySelectorAll('details[open]')].map(
            (d) => d.dataset.workId || d.dataset.receiptDetail || d.className,
          ),
        );
        old.innerHTML = fresh.innerHTML;
        for (const d of old.querySelectorAll('details'))
          d.open = opened.has(d.dataset.workId || d.dataset.receiptDetail || d.className);
      }
      node.dataset.version = String(m.version || 1);
      return deferred ? 'defer' : true;
    }
    function toolbar() {
      return capable()
        ? `${button('relay-reconcile', '重新核对反馈', {}, 'title="只读核对当前反馈，不重发消息或领取任务"')}${state.snapshot?.can_manage && state.snapshot?.capabilities?.room_scope_v1 ? button('dot-channel-open', '通道与范围') : ''}<details class="cc-pending-settings"><summary>本机待发保存</summary><p>默认关闭。仅保存你已经点击发送的正文，最多 20 条、256 KiB、24 小时。共享设备请勿开启；同源脚本仍可读取。恢复时先核对原回执，未知时不重发。退出、撤权或清理会移除保存。</p>${button('relay-persistence-toggle', localStore()?.enabled() ? '关闭并清理本机保存' : '我同意在本机保存已点击发送的消息')}${button('relay-persistence-review', '查看当前待发消息')}</details>`
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
      }
      autosize();
    }
    function autosize() {
      if (sizeFrame) return;
      sizeFrame = requestAnimationFrame(() => {
        sizeFrame = 0;
        const area = document.querySelector('#cc-message-input');
        if (!area || !active() || !area.getClientRects().length) return;
        const width = area.clientWidth;
        if (sizeMemo?.node === area && sizeMemo.value === area.value && sizeMemo.width === width)
          return;
        sizeMemo = { node: area, value: area.value, width };
        const feed = document.querySelector('.cc-feed');
        const bottom = feed && feed.scrollHeight - feed.clientHeight - feed.scrollTop < 70;
        area.style.height = 'auto';
        const measured = area.scrollHeight;
        area.style.height = Math.min(180, Math.max(52, measured)) + 'px';
        area.style.overflowY = measured > 180 ? 'auto' : 'hidden';
        if (bottom) feed.scrollTop = feed.scrollHeight;
      });
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
            !['authorization_unavailable', 'room_paused', 'revoked', 'expired'].includes(s.status),
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
            `<article class="cc-relay-pending" data-pending-id="${E(e.id)}"><p>${E(e.payload.body_text)}</p><div role="status">${E({ queued: '等待发送', sending: '正在发送…', checking: '正在核对收件回执…', retry: '连接中断，正在恢复', error: '原发送结果待核对' }[e.phase] || '等待发送')}${e.phase === 'error' ? button('relay-retry', '核对原消息', { id: e.id }) + `<span>${E(e.error || '')}</span>` : ''}</div></article>`,
        )
        .join('');
    }
    function paintOutbox(c = chat(), scroll = false) {
      if (c !== chat() || S.page !== 'collaboration') return;
      const node = document.querySelector('#cc-relay-outbox'),
        feed = document.querySelector('.cc-feed');
      const bottom = feed && feed.scrollHeight - feed.clientHeight - feed.scrollTop < 70;
      if (node) {
        const markup = outboxMarkup(c);
        if (node._pendingMarkup !== markup) {
          const template = document.createElement('template');
          template.innerHTML = markup;
          const existing = new Map([...node.children].map((n) => [n.dataset.pendingId, n]));
          const keep = new Set();
          let deferred = false;
          for (const fresh of template.content.children) {
            const id = fresh.dataset.pendingId,
              old = existing.get(id);
            keep.add(id);
            if (!old) node.append(fresh.cloneNode(true));
            else if (old.innerHTML !== fresh.innerHTML) {
              if (old.matches(':active')) {
                deferred = true;
                continue;
              }
              const status = old.querySelector('[role="status"]');
              if (status) status.innerHTML = fresh.querySelector('[role="status"]').innerHTML;
            }
          }
          for (const [id, old] of existing) if (!keep.has(id)) old.remove();
          if (!deferred) node._pendingMarkup = markup;
        }
      }
      if (feed && (bottom || scroll)) feed.scrollTop = feed.scrollHeight;
    }
    function send(form) {
      const c = chat(),
        text = form.elements.request.value.trim();
      if (
        !text ||
        !canSend() ||
        state.composing ||
        !state.snapshot?.can_manage ||
        state.snapshot?.room?.state !== 'active'
      )
        return;
      if (pending(c).length >= 20) {
        network('有较多消息等待发送，请先处理未送达的消息。', true);
        return;
      }
      const payload = {
        ...chatScope(),
        room_id: state.snapshot.room.id,
        conversation_id:
          chatScope().conversation_id || state.snapshot.conversation?.id || state.snapshot.room.id,
        body_text: text,
        ...(localStore()?.enabled() && persistenceScope()
          ? { pending_scope_revision: state.snapshot.pending_scope_revision }
          : {}),
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
        persistenceScope: localStore()?.enabled() ? persistenceScope() : '',
        authorizationRevision: state.snapshot.pending_scope_revision,
      };
      c.relayOutbox ||= [];
      c.relayOutbox.push(entry);
      savePending(entry);
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
      try {
        localStore()?.remove(entry.id);
      } catch {}
      entry.message = result.message;
      entry.anchor = null;
      const c = entry.chat;
      // An acknowledgement may arrive after navigation or before the sync feed.
      // Update its own room immutably so indexed reads and render caches see it.
      c.messages = window.CodePierCollaborationRuntime.merge(c.messages, [result.message]);
      c.relayOutbox = c.relayOutbox.filter((e) => e !== entry);
      if (visible(entry)) {
        patchTimeline();
        paintOutbox(c);
        patchDraftContext();
        onWorkspaceChange();
      }
    }
    async function inspectPending(e) {
      e.phase = 'checking';
      paintOutbox(e.chat);
      const q = new URLSearchParams({
        project: e.payload.project,
        environment_id: e.payload.environment_id,
        conversation_id: e.payload.conversation_id || e.payload.room_id || '',
        client_message_id: e.id,
      });
      try {
        const found = await api('/api/collaboration/message-receipt?' + q, {
          retryDelays: [],
          requestTimeout: 8000,
        });
        if (!belongs(e)) return 'stale';
        if (
          e.persistenceScope &&
          (found.pending_scope_revision !== e.authorizationRevision ||
            found.pending_scope_revision !== state.snapshot?.pending_scope_revision)
        ) {
          e.phase = 'error';
          e.error = '原保存授权世代已变化，停止恢复发送。';
          try {
            localStore()?.clear();
          } catch {}
          return 'blocked';
        }
        if (
          found.authoritative !== true ||
          found.client_message_id !== e.id ||
          !found.conversation_id ||
          found.conversation_id !== (e.payload.conversation_id || e.payload.room_id)
        )
          return 'unknown';
        if (found.state === 'committed') {
          if (!found.message?.id || messageText(found.message) !== e.payload.body_text) {
            e.phase = 'error';
            e.error = '原回执正文与本机意图不一致，已停止发送。';
            return 'blocked';
          }
          adopt(e, found);
        }
        if (e.phase === 'sent') return 'sent';
        // Only this scoped, authenticated receipt contract proves absence.
        return found.state === 'not_committed' ? 'absent' : 'unknown';
      } catch (error) {
        if (!belongs(e)) return 'stale';
        if ([401, 403, 409].includes(error.status)) {
          e.phase = 'error';
          e.error = error.message;
          return 'blocked';
        }
        return 'unknown';
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
          if (e.persistenceScope && e.persistenceScope !== persistenceScope()) {
            e.phase = 'error';
            e.error = '原保存范围或授权版本已变化，请核对原消息。';
            try {
              localStore()?.remove(e.id);
            } catch {}
            break;
          }
          if (
            !state.snapshot?.can_manage ||
            state.snapshot?.room?.state !== 'active' ||
            mentionSlots().some(
              (slot) =>
                slot.id === e.dotId &&
                (['revoked', 'expired'].includes(slot.state) ||
                  ['authorization_unavailable', 'revoked', 'expired', 'room_paused'].includes(
                    slot.status,
                  )),
            )
          ) {
            e.phase = 'error';
            e.error = '当前权限或连接已变化，请核对原范围。';
            break;
          }
          if (e.anchor) {
            e.payload.reply_to_id = e.anchor.message.thread_root_id || e.anchor.message.id;
            savePending(e);
          }
          if (navigator.onLine === false) {
            e.phase = 'retry';
            e.due = Date.now() + 3000;
            network('当前离线，消息和草稿保留在本页。恢复连接后继续发送。', true);
            break;
          }
          if (e.needsReceipt) {
            const found = await inspectPending(e);
            if (found === 'sent') continue;
            if (found !== 'absent') {
              if (found === 'stale' || found === 'blocked') break;
              e.phase = 'error';
              e.error = '原回执仍未知。保留原内容，仅核对，不会自动重复发送。';
              break;
            }
            e.needsReceipt = false;
          }
          e.phase = 'sending';
          e.needsReceipt = true;
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
                ...(e.persistenceScope ? { pending_scope_revision: e.authorizationRevision } : {}),
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
              if (error.code === 'PENDING_SCOPE_CHANGED') {
                try {
                  localStore()?.clear();
                } catch {}
                e.error = '原保存授权范围已变化。正文保留在本页，请核对原范围。';
                break;
              }
              continue;
            }
            const found = await inspectPending(e);
            if (found === 'sent') continue;
            if (found === 'stale' || found === 'blocked') break;
            if (found !== 'absent') {
              e.phase = 'error';
              e.error = '原回执仍未知。保留原内容，仅核对，不会自动重复发送。';
              break;
            }
            e.needsReceipt = false;
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
    function renderRevision(m, c) {
      const tasks = c.relayTaskStates?.[m.id] || [];
      const freshness = tasks.map((t) => {
        const observed =
          (t.pending_count || t.uncertain_count) && Object.hasOwn(t, 'oldest_pending_verified_at')
            ? t.oldest_pending_verified_at
            : t.last_operation_verified_at;
        const age =
          Number.isFinite(c.relayServerNow) && Number.isFinite(observed)
            ? c.relayServerNow - observed
            : null;
        return age === null || age < 0 ? 'unknown' : age < 45 ? 'recent' : 'stale';
      });
      return JSON.stringify([
        m.version || 1,
        m.body_text,
        m.body,
        m.relay_receipts,
        m.attachments,
        m.channel_deliveries,
        m.channel_media,
        tasks.map((t) => [
          t.work_item_id,
          t.wait_reason,
          t.execution_state,
          t.delivery_state,
          t.last_operation_verified_at,
          t.last_output_at,
          t.last_consumer_heartbeat_at,
          t.last_node_contact_at,
          t.last_progress_summary,
          t.scope_boundary,
          t.operation_count,
          t.uncertain_count,
        ]),
        freshness,
      ]);
    }
    async function sync({ readOnly = false } = {}) {
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
                  (c.relayTaskStates?.[m.id] || []).some((t) => t.delivery_state !== 'committed') ||
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
        const before = new Map(c.messages.map((m) => [m.id, renderRevision(m, c)]));
        const oldConnections = JSON.stringify([
          state.snapshot.join_slots,
          state.snapshot.connection_readiness,
          state.snapshot.room.state,
          state.snapshot.can_manage,
        ]);
        c.relayServerNow = result.server_now;
        c.relayReceivedAt = performance.now();
        c.relayTaskStates ||= {};
        for (const id of new Set(
          [...(result.updates || []), ...(result.items || [])].map((m) => m.id),
        ))
          c.relayTaskStates[id] = [];
        for (const task of result.task_states || [])
          (c.relayTaskStates[task.message_id] ||= []).push(task);
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
        if (result.connection_readiness)
          state.snapshot.connection_readiness = result.connection_readiness;
        if (
          state.snapshot.pending_scope_revision &&
          state.snapshot.pending_scope_revision !== result.pending_scope_revision
        ) {
          try {
            localStore()?.clear();
          } catch {}
          c.relayRestoredScope = '';
        }
        state.snapshot.pending_scope_revision = result.pending_scope_revision || null;
        state.snapshot.room.state = result.room_state;
        state.snapshot.can_manage = result.can_manage;
        c.relayFailures = 0;
        network();
        for (const entry of [...pending(c)]) {
          const m = c.messages.find((m) => m.client_message_id === entry.id);
          if (m) adopt(entry, { message: m });
        }
        const dirtyIds = new Set(
          c.messages.filter((m) => before.get(m.id) !== renderRevision(m, c)).map((m) => m.id),
        );
        const connectionDirty =
          oldConnections !==
          JSON.stringify([
            state.snapshot.join_slots,
            state.snapshot.connection_readiness,
            state.snapshot.room.state,
            state.snapshot.can_manage,
          ]);
        c.relayRenderStats ||= { syncs: 0, unchanged: 0, dirty_messages: 0 };
        c.relayRenderStats.syncs++;
        c.relayRenderStats.dirty_messages += dirtyIds.size;
        if (dirtyIds.size || connectionDirty) patchTimeline(false, dirtyIds.size ? dirtyIds : null);
        if (connectionDirty) {
          patchDelegationComposer();
          dots.refreshCards();
        }
        if (dirtyIds.size || connectionDirty) onWorkspaceChange();
        else c.relayRenderStats.unchanged++;
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
        patchTimeline();
      } finally {
        c.relaySync = false;
        if (!readOnly && c === chat() && S.page === 'collaboration') void drain(c);
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
      cancelAnimationFrame(sizeFrame);
      cancelAnimationFrame(selectionFrame);
      sizeFrame = selectionFrame = 0;
      sizeMemo = null;
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    function purge(c) {
      try {
        pendingStore?.clear();
      } catch {}
      c.relayRestoredScope = '';
      c.relayEpoch = (c.relayEpoch || 0) + 1;
      c.relayOutbox = [];
      c.relayAnchor = null;
      c.relayRecent = 0;
      c.relayTaskStates = {};
      c.relayServerNow = null;
      c.relayReceivedAt = 0;
      c.relayRenderStats = null;
    }
    function bind(root) {
      detach();
      chrome();
      restorePending();
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

      let selectedMessage = false;
      const timelineDeferred = () => root.querySelector('.cc-message-list')?._ccDeferred === true;
      const workspaceDeferred = () =>
        (root.matches('.cc-workspace') ? root : root.querySelector('.cc-workspace'))
          ?._ccWorkspaceDeferred === true;
      const select = () => {
        const selection = window.getSelection();
        if (selection && !selection.isCollapsed) {
          const timeline = root.querySelector('.cc-message-list');
          selectedMessage ||=
            !!timeline &&
            (timeline.contains(selection.anchorNode) || timeline.contains(selection.focusNode));
          return;
        }
        if (state.composing) return;
        // Releasing a real message selection gets one reconciliation. Focused
        // controls also retry explicit deferrals; ordinary caret events do none.
        if ((!selectedMessage && !timelineDeferred() && !workspaceDeferred()) || selectionFrame)
          return;
        selectionFrame = requestAnimationFrame(() => {
          selectionFrame = 0;
          if (
            state.composing ||
            state.busy ||
            !root.isConnected ||
            root.inert ||
            S.page !== 'collaboration' ||
            !window.getSelection()?.isCollapsed
          )
            return;
          if (active() && (selectedMessage || timelineDeferred())) patchTimeline();
          selectedMessage = false;
          if (workspaceDeferred()) onWorkspaceChange();
        });
      };
      const area = root.querySelector('#cc-message-input');
      const sizeObserver =
        area && typeof ResizeObserver !== 'undefined' ? new ResizeObserver(autosize) : null;
      sizeObserver?.observe(area);
      window.addEventListener('online', wake);
      document.addEventListener('visibilitychange', wake);
      document.addEventListener('selectionchange', select);
      root.addEventListener('focusout', select);
      root.addEventListener('compositionend', select);
      const onInput = (e) => {
        if (e.target.id === 'cc-message-input') autosize();
      };
      root.addEventListener('input', onInput);
      const dialog = root.querySelector('#cc-drawer');
      const onDialogClick = (e) => {
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
      };
      dialog?.addEventListener('click', onDialogClick);
      cleanup = () => {
        root.removeEventListener('input', onInput);
        dialog?.removeEventListener('click', onDialogClick);
        window.removeEventListener('online', wake);
        document.removeEventListener('visibilitychange', wake);
        document.removeEventListener('selectionchange', select);
        sizeObserver?.disconnect();
        root.removeEventListener('focusout', select);
        root.removeEventListener('compositionend', select);
      };
      void drain(chat());
    }
    function action(name, node) {
      if (!name.startsWith('relay-')) return false;
      if (name === 'relay-persistence-toggle') {
        try {
          const store = localStore();
          if (store?.enabled()) {
            store.clear();
            node.textContent = '我同意在本机保存已点击发送的消息';
          } else if (persistenceScope() && store?.enable()) node.textContent = '关闭并清理本机保存';
          else network('当前账号或授权范围尚未核验，暂不能开启本机保存。', true);
        } catch {
          network('浏览器未允许本机保存，消息仍仅保留在本页。', true);
        }
      }
      if (name === 'relay-persistence-review') {
        const outbox = document.querySelector('#cc-relay-outbox');
        if (outbox) {
          outbox.scrollIntoView({ block: 'nearest', behavior: 'auto' });
          outbox.tabIndex = -1;
          outbox.focus({ preventScroll: true });
        }
        if (!pending(chat()).length) network('当前对话没有待发消息。');
      }
      if (name === 'relay-reconcile') {
        void sync({ readOnly: true });
      }
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
      if (name === 'relay-inspect') {
        const message = messageById(node.dataset.id);
        if (message) {
          const detail = [...document.querySelectorAll('[data-receipt-detail]')].find(
            (n) => n.dataset.receiptDetail === message.id,
          );
          if (detail) {
            detail.open = true;
            detail.querySelector('summary')?.focus({ preventScroll: true });
          }
        }
      }
      if (name === 'relay-diagnostic-copy') {
        const message = messageById(node.dataset.id);
        if (message)
          void navigator.clipboard
            .writeText(window.CodePierCollaborationRelay.diagnosticSummary(message))
            .then(() => {
              if (node.isConnected) node.textContent = '已复制脱敏摘要';
            })
            .catch(() => network('复制未成功，请在预览中选取摘要。', true));
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
      evidenceProjection,
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
