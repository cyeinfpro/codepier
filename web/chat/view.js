function chatActionAvailabilityEntry(v, actionName) {
  const list =
    v?.v2ActionAvailability ||
    v?.settings?.action_availability ||
    v?.catalog?.action_availability ||
    v?.v2State?.projection?.capabilities?.action_availability;
  if (!list) return null;
  if (Array.isArray(list)) return list.find((a) => a && a.action === actionName) || null;
  if (typeof list === 'object') return list[actionName] || null;
  return null;
}
function chatActionAvailabilityText(avail) {
  if (
    !avail ||
    (avail.supported !== false && avail.authorized !== false && avail.ready !== false)
  ) {
    return '';
  }
  const remedyMap = {
    select_node: '请重新选择可用项目或电脑',
    update_agent: '请升级本机代理版本',
    reconnect: '请等待或检查本机重连',
    choose_supported_option: '请改选当前支持的选项',
    resolve_current_run: '请先等待或中断当前回复',
    review_access: '请检查项目写入与执行权限',
  };
  const reasonMap = {
    unsupported: '当前原生版本不支持此操作',
    unauthorized: '当前账号无此操作权限',
    offline: '电脑离线',
    busy: '当前回复尚未结束',
    invalid_option: '当前选项不受支持',
  };
  const reason =
    avail.message ||
    avail.reason ||
    (avail.reason_code && reasonMap[avail.reason_code]) ||
    (!avail.supported
      ? '当前原生版本不支持此操作'
      : !avail.authorized
        ? '当前账号无此操作权限'
        : '当前操作暂不可用');
  const remedy = remedyMap[avail.remedy] || '';
  return remedy ? `${reason}；${remedy}` : reason;
}
function chatSyncAvailabilityHints() {
  const v = chatView();
  const sendHint = chatActionAvailabilityText(chatActionAvailabilityEntry(v, 'send'));
  if (sendHint && $('#chat-send') && $('#chat-send-blocker')) {
    $('#chat-send').disabled = true;
    $('#chat-send-blocker').textContent = sendHint;
    $('#chat-send-blocker').hidden = false;
  }
  const resumeAvail = chatActionAvailabilityEntry(v, 'resume_session');
  const resumeHint = chatActionAvailabilityText(resumeAvail);
  const resumeEl = $('#chat-resume-hint');
  if (resumeEl) {
    resumeEl.textContent = resumeHint;
    resumeEl.hidden = !resumeHint || $('#chat-resume')?.hidden;
    if (resumeHint && $('#chat-resume')) {
      $('#chat-resume').disabled = true;
      $('#chat-resume').title = resumeHint;
    }
  }
  const modelHint = chatActionAvailabilityText(chatActionAvailabilityEntry(v, 'change_model'));
  const effortHint = chatActionAvailabilityText(chatActionAvailabilityEntry(v, 'change_options'));
  const settingsHintEl = $('#chat-settings-availability');
  if (settingsHintEl) {
    const combined = [modelHint, effortHint].filter(Boolean).join('；');
    settingsHintEl.textContent = combined;
    settingsHintEl.hidden = !combined;
  }
  if (modelHint && $('#chat-model-picker')) {
    $('#chat-model-picker').disabled = true;
    $('#chat-model-picker').title = modelHint;
  } else if ($('#chat-model-picker')) {
    $('#chat-model-picker').disabled = false;
  }
  if (effortHint && $('#chat-effort-select')) {
    $('#chat-effort-select').disabled = true;
    $('#chat-effort-select').title = effortHint;
  }
}
function chatCaptureScrollAnchor(v) {
  const el = $('#chat-scroll');
  const holder = $('#chat-messages');
  if (!el || !holder) return null;
  if (chatViewportActive(v) && v.v2Window) {
    return CP.nativeWindow.captureAnchor(v.v2Window);
  }
  const elTop = el.getBoundingClientRect().top;
  let firstRow = null;
  for (const child of holder.children) {
    const rect = child.getBoundingClientRect();
    if (rect.bottom > elTop + 1) {
      firstRow = child;
      break;
    }
  }
  return {
    owner: ChatUI.generation,
    userScrollGeneration: v.v2Window?.userScrollGeneration || 0,
    key: firstRow?.dataset?.rowKey || firstRow?.dataset?.groupKey || firstRow?.dataset?.key || null,
    relativeTop: firstRow ? firstRow.getBoundingClientRect().top - elTop : 0,
    prevHeight: el.scrollHeight,
    prevTop: el.scrollTop,
  };
}
function chatRestoreScrollAnchor(v, anchor) {
  const el = $('#chat-scroll');
  const holder = $('#chat-messages');
  if (!el || !holder || !anchor) return;
  if (chatViewportActive(v) && v.v2Window) {
    if (
      anchor.owner !== v.v2Window.owner ||
      anchor.userScrollGeneration !== v.v2Window.userScrollGeneration
    ) {
      return;
    }
    const targetTop = CP.nativeWindow.resolveSurvivingAnchor(v.v2Window, anchor, el.scrollTop);
    chatSetScrollTop(v, el, targetTop);
    return;
  }
  if (
    anchor.owner !== ChatUI.generation ||
    anchor.userScrollGeneration !== (v.v2Window?.userScrollGeneration || 0)
  ) {
    return;
  }
  if (anchor.key) {
    const target = [...holder.children].find(
      (n) => (n.dataset?.rowKey || n.dataset?.groupKey || n.dataset?.key) === anchor.key,
    );
    if (target) {
      const delta =
        target.getBoundingClientRect().top - el.getBoundingClientRect().top - anchor.relativeTop;
      chatSetScrollTop(v, el, Math.max(0, el.scrollTop + delta));
      return;
    }
  }
  chatSetScrollTop(v, el, Math.max(0, anchor.prevTop + el.scrollHeight - anchor.prevHeight));
}
async function chatLoadOlderHistoryV2() {
  const c = ChatUI;
  const v = chatView();
  const gen = c.generation;
  const sid = c.selected?.id;
  const before = v.v2State?.projection?.history?.nextToken;
  if (!sid || !v.v2State || !before || v.v2HistoryBusy) return;
  const olderBtn = $('#chat-older');
  const anchor = chatCaptureScrollAnchor(v);
  v.v2HistoryBusy = true;
  if (olderBtn) {
    olderBtn.disabled = true;
    olderBtn.textContent = '正在加载更早消息…';
  }
  try {
    const page = await api(
      '/api/native/sessions/' +
        encodeURIComponent(sid) +
        '/history?' +
        new URLSearchParams({ before }),
    );
    if (!chatCurrent(gen, v) || c.selected?.id !== sid || !v.v2State) return;
    if (page.stream_epoch && page.stream_epoch !== v.v2State.streamEpoch) {
      chatStatus('会话视图已更新，请重新打开或等待同步完成', true);
      return;
    }
    v.v2State = CP.nativeState.mergeHistoryPage(v.v2State, chatNormalizeV2HistoryPage(page));
    if (v.v2Scope) v.v2Scope.authoritative = v.v2State;
    if (!chatViewportActive(v)) {
      v.limit = Math.max(v.limit, v.v2State.projection.order.length);
    }
    chatSyncV2ToView(v, null, v.v2State.dirtyKeys, true);
    chatRenderHistory(false);
    chatRestoreScrollAnchor(v, anchor);
    chatV2Cache()?.enqueue(v.v2State, () => chatCurrent(gen, v));
  } catch (err) {
    if (chatCurrent(gen, v)) chatStatus(err.message || '加载更早消息失败', true);
  } finally {
    v.v2HistoryBusy = false;
    if (olderBtn && chatCurrent(gen, v)) {
      olderBtn.disabled = false;
      olderBtn.textContent = '显示更早消息';
      olderBtn.hidden = !v.v2State?.projection?.history?.hasMore;
    }
  }
}
function chatSyncPartialBodyNotice(item) {
  if (!item?.node) return;
  const existing = item.node.querySelector('.chat-body-partial');
  if (!item.data?.partial) {
    existing?.remove();
    return;
  }
  let bar = existing;
  if (!bar) {
    bar = document.createElement('div');
    bar.className = 'chat-body-partial';
    const note = document.createElement('span');
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chat-text-button';
    btn.onclick = () => chatLoadItemBody(item.key);
    bar.append(note, btn);
    item.node.append(bar);
  }
  const v = chatView();
  const loading = !!v.v2BodyRequests?.get(item.key);
  const totalBytes = item.data.total_bytes ?? item.data.body?.byte_length;
  const loadedBytes = item.data.body_loaded_bytes || 0;
  const sizeHint =
    typeof totalBytes === 'number' && totalBytes > 0
      ? `（已载入 ${Math.min(loadedBytes, totalBytes).toLocaleString()} / ${totalBytes.toLocaleString()} 字节）`
      : '';
  bar.firstElementChild.textContent = '正文较长，当前仅显示预览' + sizeHint;
  const btn = bar.lastElementChild;
  btn.disabled = loading;
  btn.textContent = loading ? '正在加载…' : '加载完整正文';
}
async function chatLoadItemBody(itemKey) {
  const c = ChatUI;
  const v = chatView();
  const gen = c.generation;
  const sid = c.selected?.id;
  if (!sid || !v.v2State || v.v2BodyRequests?.get(itemKey)) return;
  v.v2BodyRequests ??= new Map();
  v.v2BodyRequests.set(itemKey, true);
  const itemUi = v.items.get(itemKey);
  if (itemUi) chatSyncPartialBodyNotice(itemUi);
  try {
    let pItem = v.v2State.projection.items.get(itemKey);
    if (!pItem) return;
    let contentGen = pItem.content_generation ?? pItem.body?.content_generation ?? 1;
    let startByte = pItem.body_loaded_bytes ?? 0;
    for (let step = 0; step < 64; step += 1) {
      if (!chatCurrent(gen, v) || c.selected?.id !== sid || !v.v2State) return;
      let chunk;
      try {
        chunk = await api(
          '/api/native/sessions/' +
            encodeURIComponent(sid) +
            '/items/' +
            encodeURIComponent(itemKey) +
            '/body?' +
            new URLSearchParams({
              generation: String(contentGen),
              start_byte: String(startByte),
            }),
        );
      } catch (err) {
        if (err?.code === 'BODY_REVISION_EXPIRED' && step === 0) {
          const detail = await api(
            '/api/native/sessions/' +
              encodeURIComponent(sid) +
              '/items/' +
              encodeURIComponent(itemKey),
          );
          if (!chatCurrent(gen, v) || !detail?.item) return;
          contentGen =
            detail.item.content_generation ?? detail.item.body?.content_generation ?? contentGen;
          startByte = 0;
          continue;
        }
        throw err;
      }
      if (!chatCurrent(gen, v) || !v.v2State) return;
      v.v2State = CP.nativeState.mergeBodyChunk(v.v2State, itemKey, chunk);
      if (v.v2Scope) v.v2Scope.authoritative = v.v2State;
      pItem = v.v2State.projection.items.get(itemKey);
      startByte = chunk.end_byte;
      if (!chunk.has_more) break;
    }
    chatSyncV2ToView(v, null, new Set([itemKey]), false);
    if (!c.frame) c.frame = requestAnimationFrame(chatFlush);
    chatV2Cache()?.enqueue(v.v2State, () => chatCurrent(gen, v));
  } catch (err) {
    if (chatCurrent(gen, v)) chatStatus(err.message || '加载完整正文失败', true);
  } finally {
    v.v2BodyRequests?.delete(itemKey);
    const cur = v.items.get(itemKey);
    if (cur && chatCurrent(gen, v)) chatSyncPartialBodyNotice(cur);
  }
}
function chatSyncToolTruncatedNotice(item, st) {
  if (!item?.node) return;
  const details = item.node.querySelector('details');
  if (!details) return;
  let notice = details.querySelector('.chat-tool-truncated');
  if (!st?.truncatedBefore) {
    notice?.remove();
    return;
  }
  if (!notice) {
    notice = document.createElement('div');
    notice.className = 'chat-tool-truncated';
    const text = document.createElement('span');
    text.textContent = '输出较长，仅展示末尾内容';
    const link = document.createElement('a');
    link.className = 'chat-text-button';
    link.textContent = '导出完整记录';
    link.download = 'conversation.md';
    notice.append(text, link);
    details.insertBefore(notice, item.body);
  }
  const sid = ChatUI.selected?.id;
  if (sid) {
    notice.lastElementChild.href =
      '/api/native/sessions/' +
      encodeURIComponent(sid) +
      '/export?' +
      new URLSearchParams({ format: 'md', space_id: S.space_id || '' });
  }
}
function chatEnsureOutputManager(v) {
  if (
    v.v2OutputSubscriptions ||
    typeof CP === 'undefined' ||
    !CP.nativeRuntime?.ToolOutputSubscriptionManager
  ) {
    return v.v2OutputSubscriptions;
  }
  const controllers = new Map();
  const mgr = new CP.nativeRuntime.ToolOutputSubscriptionManager({
    onSubscribe: (toolKey, spec) => {
      const c = ChatUI;
      const gen = c.generation;
      const sid = c.selected?.id;
      if (!sid) return;
      const ac = new AbortController();
      controllers.set(toolKey, ac);
      void (async () => {
        let afterOffset = spec.afterOffset ?? null;
        while (
          !ac.signal.aborted &&
          chatCurrent(gen, v) &&
          c.selected?.id === sid &&
          mgr.getState(toolKey)
        ) {
          try {
            const qs = new URLSearchParams({ stream: 'true' });
            if (afterOffset !== null && afterOffset !== undefined) {
              qs.set('after_offset', String(afterOffset));
            }
            if (S.space_id) qs.set('space_id', S.space_id);
            const res = await fetch(
              '/api/native/sessions/' +
                encodeURIComponent(sid) +
                '/items/' +
                encodeURIComponent(toolKey) +
                '/output?' +
                qs,
              {
                credentials: 'same-origin',
                headers: {
                  Accept: 'text/event-stream',
                  ...(S.space_id ? { 'X-CodePier-Space': S.space_id } : {}),
                  ...(S.session?.csrf ? { 'X-RD-CSRF': S.session.csrf } : {}),
                },
                signal: ac.signal,
              },
            );
            if (!res.ok || !res.body || ac.signal.aborted) break;
            let needsReplace = false;
            await CP.nativeRuntime.parseSseFrames(
              res.body,
              async (frame) => {
                if (ac.signal.aborted || !chatCurrent(gen, v) || !frame.data) return;
                if (frame.event === 'error') return;
                let payload;
                try {
                  payload = JSON.parse(frame.data);
                } catch {
                  return;
                }
                const st = mgr.receiveFrame(toolKey, payload);
                if (!st) return;
                if (st.needsReplace) {
                  needsReplace = true;
                  afterOffset = null;
                  return;
                }
                afterOffset = st.toOffset;
                const item = v.items.get(toolKey);
                if (item && item.body) {
                  item.text = st.text;
                  item.body.textContent = st.text;
                  chatSyncToolTruncatedNotice(item, st);
                }
                if (!st.running) ac.abort();
              },
              ac.signal,
            );
            if (!needsReplace) break;
          } catch {
            break;
          }
        }
      })();
    },
    onUnsubscribe: (toolKey) => {
      const ac = controllers.get(toolKey);
      controllers.delete(toolKey);
      ac?.abort();
    },
  });
  v.v2OutputSubscriptions = mgr;
  return mgr;
}
function chatToolVisibleInDom(item) {
  if (!item?.node) return false;
  const details = item.node.querySelector('details');
  if (!details?.open) return false;
  const group = item.node.closest('.chat-process');
  return !group || group.open;
}
function chatOpenToolOutput(item, v = chatView()) {
  if (!chatV2Enabled('native_log_on_demand') || !v?.v2State) return;
  const mgr = chatEnsureOutputManager(v);
  if (!mgr) return;
  mgr.open(item.key, {
    outputEpoch: item.data?.output_ref?.output_epoch || '',
    running: item.data?.active !== false,
  });
}
function chatCloseToolOutput(itemKey, v = chatView()) {
  v?.v2OutputSubscriptions?.close(itemKey);
}
function chatOnToolDetailsToggle(item, _details) {
  const v = chatView();
  if (!chatV2Enabled('native_log_on_demand') || !v?.v2State) return;
  if (chatToolVisibleInDom(item)) chatOpenToolOutput(item, v);
  else chatCloseToolOutput(item.key, v);
}
function chatSyncGroupToolOutputs(v, groupNode) {
  if (!chatV2Enabled('native_log_on_demand') || !v?.v2State || !groupNode) return;
  for (const el of groupNode.querySelectorAll('.chat-message-tool')) {
    const key = el.dataset.key;
    const item = key ? v.items.get(key) : null;
    if (!item) continue;
    if (chatToolVisibleInDom(item)) chatOpenToolOutput(item, v);
    else chatCloseToolOutput(item.key, v);
  }
}
function chatApplyReviewEvidenceState(item) {
  if (!item?.node || !item.reviewMeta || !item.reviewNote) return;
  const v = chatView();
  const data = item.data || {};
  const ctrl = v.v2Control || {};
  const receipt = data.receipt;
  const matchCtrl = (entry) => entry && (!receipt || !entry.receipt || entry.receipt === receipt);
  const evidenceState =
    data.evidence_state ??
    (matchCtrl(ctrl.evidence_frozen?.body) ? ctrl.evidence_frozen.body.evidence_state : null) ??
    (matchCtrl(ctrl.provider_completed?.body) ? ctrl.provider_completed.body.evidence_state : null);
  const reviewState =
    data.review_state ??
    (matchCtrl(ctrl.review_ready?.body) ? ctrl.review_ready.body.review_state : null);
  if (!evidenceState && !reviewState) return;

  let badgeLabel = '';
  let badgeState = '';
  if (evidenceState === 'capture_pending') {
    badgeLabel = '采集中';
    badgeState = 'pending';
  } else if (
    evidenceState === 'unavailable' ||
    evidenceState === 'not_requested' ||
    reviewState === 'unavailable'
  ) {
    badgeLabel = '暂不可用';
    badgeState = 'unavailable';
  } else if (
    reviewState === 'generating' ||
    ((evidenceState === 'frozen' || evidenceState === 'partial') &&
      reviewState !== 'ready' &&
      data.available !== true &&
      data.available !== false)
  ) {
    badgeLabel = '审阅生成中';
    badgeState = 'generating';
  } else if (evidenceState === 'frozen' || evidenceState === 'partial' || reviewState === 'ready') {
    badgeLabel = '已固定';
    badgeState = 'frozen';
  }

  const summary = item.reviewNode?.querySelector('summary');
  if (summary && badgeLabel) {
    let badge = summary.querySelector('.chat-evidence-badge');
    if (!badge) {
      badge = document.createElement('span');
      badge.className = 'chat-evidence-badge';
      summary.insertBefore(badge, item.reviewMeta);
    }
    badge.textContent = badgeLabel;
    badge.dataset.state = badgeState;
  }

  if (
    evidenceState === 'unavailable' ||
    evidenceState === 'capture_pending' ||
    evidenceState === 'not_requested' ||
    reviewState === 'unavailable'
  ) {
    item.reviewMeta.textContent = badgeLabel || '未能核实';
    item.reviewNote.textContent =
      data.text ||
      {
        capture_pending: '正在采集本轮改动快照，完成后更新。',
        not_requested: '本轮未请求工作区改动快照。',
        unavailable: '工作区改动快照暂不可用，请在电脑本机核实。',
      }[evidenceState] ||
      '改动审阅暂不可用，请在电脑本机核实。';
  } else if (badgeState === 'generating') {
    item.reviewMeta.textContent = badgeLabel;
    item.reviewNote.textContent = '已固定本轮前后快照，正在生成改动审阅…';
  }
}
async function chatPage() {
  if ($('#chat-root')) {
    chatList();
    return;
  }
  chatDetach();
  const c = ChatUI,
    gen = c.generation;
  await Promise.all([loadBasics(), chatLoadNativeCapabilities()]);
  if (!chatCurrent(gen)) return;
  chatCheckProjectMappings();
  c.project = S.projects.some((p) => p.id === c.project)
    ? c.project
    : S.work.project || S.projects[0]?.id || '';
  c.listeners = new AbortController();
  $('#page').innerHTML =
    `<section id="chat-root" class="chat-workspace" aria-label="CLI 对话工作区">
  <header class="chat-header chat-workspace-bar">${chatWorkspaceBar()}<button class="chat-square chat-drawer-toggle" id="chat-history-toggle" aria-label="切换会话历史" title="会话历史 · ⌘ B" aria-expanded="false">${chatIcon('menu')}</button>
  <div class="chat-heading"><h1 tabindex="-1" id="chat-title">新对话</h1><div class="chat-subline"><span id="chat-subtitle"></span><span id="chat-connection" class="chat-connection" title="连接状态"><i></i><span>就绪</span></span></div></div><div class="chat-header-right">
  <button id="chat-find-toggle" class="chat-square" aria-label="搜索当前对话" title="搜索当前对话">${chatIcon('search')}</button><button id="chat-focus" class="chat-square chat-focus-button" aria-label="切换专注模式" title="专注模式">${chatIcon('expand')}</button><button id="chat-inspector-toggle" class="chat-square" aria-label="会话详情" title="会话详情">${chatIcon('settings')}</button>
  <details class="chat-overflow"><summary aria-label="更多操作">···</summary><div><button id="chat-rename">重命名</button><details class="chat-export-menu"><summary>导出</summary><button id="chat-export">Markdown</button><button id="chat-export-json">JSON</button></details><button id="chat-stop">停止会话</button><button id="chat-delete" class="danger">删除会话</button></div></details></div></header>
  <div class="chat-body">
  <button id="chat-shade" class="chat-shade" aria-label="关闭会话历史"></button>
  <aside class="chat-sidebar" aria-label="会话历史"><button type="button" id="chat-new" class="chat-new-button" title="新对话" aria-label="新对话">${chatIcon('plus')}<span>新对话</span><kbd data-chat-shortcut="new">⌘ ⇧ O</kbd></button>
  <div class="chat-history-heading"><strong>全部会话</strong><button type="button" id="chat-history-clear" class="chat-text-button" hidden>清除筛选</button></div>
  <div class="chat-history-filter"><input id="chat-history-search" type="search" enterkeyhint="search" autocapitalize="none" autocorrect="off" spellcheck="false" placeholder="搜索会话、项目或内容" aria-label="查找对话"></div>
  <div class="chat-history-chips"><details><summary>项目</summary><select id="chat-history-project" aria-label="筛选历史项目"><option value="">所有项目</option>${S.projects.map((p) => `<option value="${esc(p.id)}">${esc(p.alias)}</option>`).join('')}</select></details><details><summary>状态</summary><select id="chat-history-filter" aria-label="筛选会话"><option value="">全部</option><option value="running">运行中</option><option value="exited">已停止</option><option value="interrupted">已中断</option></select></details></div>
  <div id="chat-history-notice" class="chat-history-notice" role="alert" hidden><span></span><button type="button" id="chat-history-retry">重试列表</button></div>
  <div id="chat-history" class="chat-history" aria-label="历史对话列表" aria-busy="false"></div><div class="chat-history-pages"><button id="chat-history-prev" hidden>较新</button><span id="chat-history-count"></span><button id="chat-history-next" hidden>较早</button></div>
  <footer class="chat-sidebar-footer"><button type="button" id="chat-command-menu" class="chat-sidebar-action" aria-label="快捷操作" title="快捷操作">${chatIcon('command')}<span>快捷操作</span><kbd data-chat-shortcut="command">⌘ K</kbd></button></footer></aside>
  <div class="chat-main">
  <div id="chat-find-bar" class="chat-find-bar" hidden><input id="chat-find-input" type="search" enterkeyhint="search" autocapitalize="none" autocorrect="off" spellcheck="false" placeholder="在已载入消息中查找" aria-label="消息关键词"><span id="chat-find-count"></span><button id="chat-find-prev" aria-label="上一个匹配">↑</button><button id="chat-find-next" aria-label="下一个匹配">↓</button><button id="chat-find-close" aria-label="关闭查找">×</button></div>
  <div id="chat-scroll" class="chat-scroll" tabindex="0" aria-label="对话消息"><button id="chat-older" class="chat-text-button" hidden>显示更早消息</button><div id="chat-empty" class="chat-empty"><div class="chat-empty-mark">${chatIcon('command')}</div><h2 id="chat-empty-title"></h2><div class="chat-starters"><button data-chat-starter="梳理这个项目的结构，指出值得优先处理的问题。">${chatIcon('folder')}<span><strong>了解项目</strong><small>梳理结构与关键入口</small></span></button><button data-chat-starter="检查当前改动，找出可复现的问题并给出修复建议。">${chatIcon('check')}<span><strong>检查改动</strong><small>找出问题与潜在风险</small></span></button><button data-chat-starter="先阅读项目说明和已有实现，再制定实施计划。">${chatIcon('command')}<span><strong>制定计划</strong><small>把想法拆成具体步骤</small></span></button></div></div><div id="chat-messages" class="chat-messages"></div><button type="button" class="chat-text-button" id="chat-resume" hidden>恢复会话</button><span id="chat-resume-hint" class="chat-availability-hint" role="status" hidden></span></div>
  <button id="chat-latest" class="chat-latest" hidden>↓ 回到最新</button>
  <div class="chat-composer-wrap"><div id="chat-activity" class="chat-activity" hidden role="status"><i aria-hidden="true"></i><span id="chat-activity-text"></span><button type="button" id="chat-activity-queue"></button><div class="chat-run-controls"><select id="chat-send-mode" aria-label="发送方式" title="正在执行时如何发送新消息"><option value="followup">等这轮结束再发</option><option value="steer">插入当前回复</option></select><button type="button" id="chat-interrupt" class="chat-stop-turn" aria-label="中断当前" title="中断当前回复" hidden>${chatIcon('stop')}<span>中断当前</span></button></div></div><div id="chat-outbox" class="chat-outbox" aria-live="polite"></div><div id="chat-files" class="chat-files" aria-live="polite"></div>
  <div id="chat-target-notice" class="chat-catalog-notice" role="status" hidden><span></span><button type="button" id="chat-target-refresh" hidden>检查连接</button><button type="button" id="chat-catalog-retry" hidden>重新加载</button></div>


  <div class="chat-compose-frame">
  <form id="chat-form" class="chat-composer"><textarea id="chat-compose" rows="1" enterkeyhint="send" aria-label="消息" aria-describedby="chat-status chat-text-limit" placeholder="描述任务，输入 / 查看指令…" maxlength="${CHAT_MESSAGE_MAX_BYTES}"></textarea>
  <div class="chat-compose-bottom"><div class="chat-compose-controls"><button class="chat-square" type="button" id="chat-attach" aria-label="添加文件或图片" title="上传图片与文件">${chatIcon('plus')}</button><input type="file" id="chat-file-input" multiple hidden><button type="button" id="chat-options-toggle" class="chat-model-picker" aria-label="会话设置" aria-haspopup="dialog" aria-controls="chat-options" aria-expanded="false">${chatIcon('settings')}<span id="chat-config-summary">会话设置</span>${chatIcon('chevron')}</button></div>
  <div class="chat-send-actions"><button type="submit" id="chat-send" class="chat-send" aria-label="发送消息">${chatIcon('send')}</button></div></div></form>
  <button type="button" id="chat-options-shade" class="chat-options-shade" aria-label="关闭会话设置" tabindex="-1" hidden></button>
  <section id="chat-options" class="chat-options" role="region" aria-label="会话设置"><header class="chat-options-head"><div><strong>会话设置</strong></div><button type="button" id="chat-options-close" class="chat-square" aria-label="关闭会话设置">${chatIcon('close')}</button></header>
  <div id="chat-new-context" class="chat-new-context"><label class="chat-setting-field"><span>工作项目</span><select id="chat-project" aria-label="项目">${S.projects.map((p) => `<option value="${esc(p.id)}" ${p.id === c.project ? 'selected' : ''}>${esc(p.alias)}</option>`).join('')}</select></label><div class="chat-cli-shortcuts"><span class="chat-setting-caption">CLI 快捷选择</span><div class="chat-cli-segmented" role="group" aria-label="新对话使用的 CLI">${[
    ['pi', 'Pi'],
    ['codex', 'Codex'],
    ['claude', 'Claude Code'],
  ]
    .map(
      ([id, label]) =>
        `<button type="button" data-chat-cli="${id}" aria-pressed="${id === c.provider}">${label}</button>`,
    )
    .join('')}</div></div><span id="chat-target-info"></span></div>
  <div class="chat-config-bar"><div class="chat-setting-field"><label for="chat-model-picker">模型</label><button type="button" id="chat-model-picker" class="chat-model-picker" aria-label="选择模型" aria-haspopup="dialog" aria-expanded="false">${chatIcon('model')}<span id="chat-model-name">选择模型</span>${chatIcon('chevron')}</button></div><label class="chat-cli-label chat-setting-field"><span>会话 CLI</span><select id="chat-provider" aria-label="原生 CLI"><option value="pi">Pi</option><option value="codex">Codex</option><option value="claude">Claude</option></select></label>  <label class="chat-effort-label chat-setting-field">${chatIcon('brain')}<span>思考</span><select id="chat-effort-select" aria-label="思考强度"><option value="">本机默认</option></select></label>
  <label class="chat-selection-label chat-setting-field"><span>配置生效</span><select id="chat-selection-mode" aria-label="配置生效方式"><option value="inherit_at_dispatch">发送时跟随当前</option><option value="explicit">本条固定</option></select></label>
  <div class="chat-setting-field"><label for="chat-cwd-button">工作目录</label><button type="button" id="chat-cwd-button" class="chat-cwd-button" title="工作目录">${chatIcon('folder')}<span>.</span></button></div></div>
  <p id="chat-settings-availability" class="chat-availability-hint" role="status" hidden></p>
  <div class="chat-option-tools"><button type="button" id="chat-file-library" aria-label="附件库">${chatIcon('file')}<span>附件库</span></button></div></section></div>
  <div class="chat-setting-feedback"><span id="chat-setting-state" class="chat-setting-state"></span><button type="button" id="chat-settings-retry" class="chat-text-button" hidden>重试设置</button></div>
  <p id="chat-send-blocker" class="chat-popover-help" role="status" hidden></p><div class="chat-composer-note"><span id="chat-status" role="status" data-quiet="true">Enter 发送 · Shift+Enter 换行</span><span id="chat-text-limit" role="status" hidden></span><button id="chat-retry" hidden>重试</button></div></div>
  </div>
  <aside id="chat-inspector" class="chat-inspector" hidden><header><strong>会话详情</strong><button id="chat-inspector-close" class="chat-square" aria-label="关闭会话详情">${chatIcon('close')}</button></header><nav class="chat-inspector-tabs">${[
    ['overview', '概览'],
    ['changes', '改动'],
    ['files', '文件'],
    ['commands', '指令'],
    ['queue', '队列'],
  ]
    .map(([id, label]) => `<button data-chat-tab="${id}">${label}</button>`)
    .join('')}</nav><div id="chat-inspector-body"></div></aside>
  </div><div id="chat-popover" class="chat-popover" role="dialog" aria-label="模型选择" hidden></div><div id="chat-slash" class="chat-slash" role="listbox" aria-label="可用指令" hidden></div></section>`;
  const on = (id, event, fn) =>
    $('#' + id).addEventListener(
      event,
      (e) => {
        const gen = c.generation;
        try {
          const r = fn(e);
          if (r?.catch)
            r.catch((err) => {
              if (chatCurrent(gen)) chatStatus(err.message, true);
            });
        } catch (err) {
          if (chatCurrent(gen)) chatStatus(err.message, true);
        }
      },
      { signal: c.listeners.signal },
    );
  on('chat-compose', 'input', (e) => {
    chatView().draft = e.target.value;
    chatResizeComposer();
    chatSlash();
    chatSyncChrome();
  });
  on('chat-compose', 'keydown', (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (
      !$('#chat-slash').hidden &&
      ['ArrowDown', 'ArrowUp', 'Tab', 'Enter', 'Escape'].includes(e.key)
    ) {
      e.preventDefault();
      chatSlashKey(e.key);
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      chatSend();
    }
  });
  on('chat-form', 'submit', (e) => {
    e.preventDefault();
    chatSend();
  });
  on('chat-new', 'click', () => chatNewSession());
  on('chat-options-toggle', 'click', () =>
    chatOptions(!$('#chat-options').classList.contains('is-open')),
  );
  on('chat-options-close', 'click', () => chatOptions(false, true));
  on('chat-options-shade', 'click', () => chatOptions(false, true));
  on('chat-target-refresh', 'click', async () => {
    const b = $('#chat-target-refresh');
    b.disabled = true;
    try {
      await chatRefreshTargets();
    } finally {
      b.disabled = false;
    }
  });
  on('chat-history-clear', 'click', chatHistoryClear);
  on('chat-history-retry', 'click', chatList);
  on('chat-history-project', 'change', (e) => {
    c.historyProject = e.target.value;
    c.historyOffset = 0;
    chatList();
  });
  on('chat-project', 'change', (e) => chatSwitch(null, e.target.value, { cwd: '.' }));
  on('chat-provider', 'change', (e) => chatSwitch(null, c.project, { provider: e.target.value }));
  // The segmented control in the new-chat context drives the same select, so
  // there is a single switching path.
  for (const button of document.querySelectorAll('#chat-new-context [data-chat-cli]'))
    button.addEventListener('click', () => {
      const select = $('#chat-provider');
      if (!select || select.disabled || select.value === button.dataset.chatCli) return;
      select.value = button.dataset.chatCli;
      select.dispatchEvent(new Event('change', { bubbles: true }));
    });
  $('#chat-history-search').value = c.query;
  $('#chat-history-filter').value = c.filter;
  let searchTimer;
  on('chat-history-search', 'input', (e) => {
    c.query = e.target.value;
    c.historyOffset = 0;
    c.listTicket++;
    chatHistoryControls();
    clearTimeout(searchTimer);
    const ticket = c.generation;
    searchTimer = setTimeout(() => {
      if (chatCurrent(ticket)) chatList();
    }, 200);
  });
  on('chat-history-filter', 'change', (e) => {
    c.filter = e.target.value;
    c.historyOffset = 0;
    chatList();
  });
  on('chat-history-prev', 'click', () => {
    c.historyOffset = Math.max(0, c.historyOffset - 50);
    $('#chat-history').scrollTop = 0;
    chatList();
  });
  on('chat-history-next', 'click', () => {
    c.historyOffset += 50;
    $('#chat-history').scrollTop = 0;
    chatList();
  });
  on('chat-history-toggle', 'click', () =>
    chatDrawer(
      innerWidth > 760 ? !!c.historyCollapsed : !$('#chat-root').classList.contains('drawer-open'),
    ),
  );
  on('chat-shade', 'click', () => {
    if ($('#chat-root').classList.contains('drawer-open')) chatDrawer(false);
    if (c.inspector && innerWidth <= 760) chatInspector('');
  });
  on('chat-root', 'keydown', (e) => {
    if (
      (e.metaKey || e.ctrlKey) &&
      e.key === 'f' &&
      e.target.closest('#chat-root') &&
      !e.target.closest('dialog')
    ) {
      e.preventDefault();
      chatFindToggle(true);
    }
  });
  on('chat-root', 'click', (e) => {
    const starter = e.target.closest('[data-chat-starter]'),
      tab = e.target.closest('[data-chat-tab]');
    if (starter) chatReplaceDraft(starter.dataset.chatStarter);
    if (tab) chatInspector(tab.dataset.chatTab);
    if (!e.target.closest('#chat-popover,#chat-model-picker,#chat-cwd-button')) chatClosePopover();
  });
  let scrollFrame = 0;
  on('chat-scroll', 'scroll', () => {
    const v = chatView();
    const el = $('#chat-scroll');
    if (v.v2Window) {
      const isProg =
        v.v2Window.programmaticScroll &&
        Math.abs(el.scrollTop - (v.v2Window.expectedScrollTop ?? el.scrollTop)) <= 2;
      if (!isProg) {
        v.v2Window.userScrollGeneration += 1;
      }
      v.v2Window.programmaticScroll = false;
    }
    v.follow = chatAtBottom();
    if (v.v2Window) v.v2Window.followBottom = v.follow;
    v.scrollTop = el.scrollTop;
    $('#chat-latest').hidden = v.follow;
    if (chatViewportActive(v) && !scrollFrame) {
      const scrollGen = c.generation;
      scrollFrame = requestAnimationFrame(() => {
        scrollFrame = 0;
        if (chatCurrent(scrollGen, v) && chatViewportActive(v)) {
          chatRenderViewportWindow(v);
        }
      });
    }
  });
  on('chat-latest', 'click', chatBottom);
  on('chat-older', 'click', () => {
    const v = chatView();
    if (v.v2State) {
      void chatLoadOlderHistoryV2();
      return;
    }
    const el = $('#chat-scroll'),
      h = el.scrollHeight,
      t = el.scrollTop;
    v.limit += 100;
    chatRenderHistory(false);
    el.scrollTop = t + el.scrollHeight - h;
  });
  on('chat-attach', 'click', () => $('#chat-file-input').click());
  on('chat-file-input', 'change', (e) => {
    chatUpload([...e.target.files]);
    e.target.value = '';
  });
  on('chat-root', 'dragover', (e) => {
    if (e.dataTransfer?.types.includes('Files')) {
      e.preventDefault();
      $('#chat-root').classList.add('is-dragging');
    }
  });
  on('chat-root', 'dragleave', (e) => {
    if (!e.currentTarget.contains(e.relatedTarget)) e.currentTarget.classList.remove('is-dragging');
  });
  on('chat-root', 'drop', (e) => {
    e.preventDefault();
    $('#chat-root').classList.remove('is-dragging');
    chatUpload([...e.dataTransfer.files]);
  });
  on('chat-compose', 'paste', (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) {
      e.preventDefault();
      chatUpload(files);
    }
  });
  on('chat-send-mode', 'change', (e) => {
    chatView().mode = e.target.value;
    chatSyncChrome();
  });
  on('chat-selection-mode', 'change', (e) => {
    chatView().selectionMode = e.target.value;
    chatSyncChrome();
  });
  on('chat-interrupt', 'click', chatInterrupt);
  on('chat-retry', 'click', () => chatSend(true));
  on('chat-model-picker', 'click', () => chatModelPopover());
  on('chat-effort-select', 'change', (e) => chatChooseEffort(e.target.value));
  on('chat-effort-select', 'focus', () => {
    if (!chatModelCurrent()) chatLoadCatalog(false, undefined, true);
  });
  on('chat-catalog-retry', 'click', () => chatLoadCatalog(true));
  on('chat-settings-retry', 'click', () => chatSubmitSettings(null, true));
  on('chat-cwd-button', 'click', chatCwdPopover);
  on('chat-file-library', 'click', () => chatInspector(c.inspector === 'files' ? '' : 'files'));
  on('chat-inspector-toggle', 'click', () => chatInspector(c.inspector ? '' : 'overview'));
  on('chat-inspector-close', 'click', () => chatInspector(''));
  on('chat-focus', 'click', () => {
    c.focus = !c.focus;
    document.body.classList.toggle('chat-focus', c.focus);
    chatSyncChrome();
    chatViewport();
  });
  on('chat-activity-queue', 'click', () => chatInspector('queue'));
  on('chat-find-toggle', 'click', () => chatFindToggle($('#chat-find-bar').hidden));
  on('chat-find-close', 'click', () => chatFindToggle(false));
  on('chat-find-input', 'input', (e) => {
    chatView().search = e.target.value;
    chatView().match = 0;
    chatFind();
  });
  on('chat-find-input', 'keydown', (e) => {
    if (e.key === 'Enter' && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      chatView().match += e.shiftKey ? -1 : 1;
      chatFind();
    }
  });
  on('chat-find-prev', 'click', () => {
    chatView().match--;
    chatFind();
  });
  on('chat-find-next', 'click', () => {
    chatView().match++;
    chatFind();
  });
  on('chat-rename', 'click', chatRename);
  on('chat-resume', 'click', chatResume);
  on('chat-export', 'click', () => chatExport('md'));
  on('chat-export-json', 'click', () => chatExport('json'));
  on('chat-stop', 'click', chatStop);
  on('chat-delete', 'click', chatDelete);
  document.addEventListener(
    'focusin',
    (e) => {
      const v = chatView();
      if (!chatViewportActive(v) || !v.v2Window) return;
      const msg = e.target?.closest?.('#chat-messages [data-row-key]');
      for (const [k, reasons] of [...v.v2Window.pinSet.entries]) {
        if (reasons.has('focus')) v.v2Window.pinSet.unpin(k, 'focus');
      }
      if (msg?.dataset.rowKey) v.v2Window.pinSet.pin(msg.dataset.rowKey, 'focus');
    },
    { signal: c.listeners.signal },
  );
  document.addEventListener(
    'selectionchange',
    () => {
      const v = chatView();
      if (!chatViewportActive(v) || !v.v2Window) return;
      for (const [k, reasons] of [...v.v2Window.pinSet.entries]) {
        if (reasons.has('selection')) v.v2Window.pinSet.unpin(k, 'selection');
      }
      const sel = window.getSelection?.();
      if (!sel || sel.isCollapsed || !sel.rangeCount) return;
      const node = sel.anchorNode?.nodeType === 1 ? sel.anchorNode : sel.anchorNode?.parentElement;
      const msg = node?.closest?.('#chat-messages [data-row-key]');
      if (msg?.dataset.rowKey) v.v2Window.pinSet.pin(msg.dataset.rowKey, 'selection');
    },
    { signal: c.listeners.signal },
  );
  chatBindViewportEvents(c);
  chatMountChrome();
  chatMountHistory();
  chatDrawer(false, true);
  if (c.focus) document.body.classList.add('chat-focus');
  chatRestore();
  chatViewport();
  chatList();
  chatLoadCatalog();
  if (c.selected) chatConnect();
  if (c.inspector) chatInspector(c.inspector);
}
function chatScheduleViewport() {
  if (ChatUI.viewportFrame) return;
  ChatUI.viewportFrame = requestAnimationFrame(() => {
    ChatUI.viewportFrame = 0;
    chatViewport();
  });
}
function chatBindViewportEvents(c) {
  const signal = c.listeners.signal;
  window.addEventListener('resize', chatScheduleViewport, { signal });
  if (window.visualViewport) {
    window.visualViewport.addEventListener('resize', chatScheduleViewport, {
      signal,
    });
    window.visualViewport.addEventListener('scroll', chatScheduleViewport, {
      signal,
    });
  }
  document.addEventListener(
    'keydown',
    (e) => {
      const root = $('#chat-root');
      if (!root || e.isComposing || e.keyCode === 229) return;
      const vv = window.visualViewport;
      const keyboardHeight = innerHeight - (vv?.height || innerHeight);
      if (
        keyboardHeight < 60 &&
        (e.metaKey || e.ctrlKey || e.key.length === 1 || e.key === 'Enter' || e.key === 'Tab')
      ) {
        root.dataset.hardwareKeyboard = 'true';
      }
    },
    { signal },
  );
}
function chatViewport() {
  const root = $('#chat-root');
  if (!root) return;
  const v = chatView(),
    wasAtBottom = !!v.order.length && (v.follow !== false || chatAtBottom()),
    viewport = window.visualViewport,
    offsetTop = Math.max(0, viewport?.offsetTop || 0);
  if (viewport && innerHeight - viewport.height >= 100) {
    delete root.dataset.hardwareKeyboard;
  }
  if (offsetTop > 0 && viewport) {
    root.style.transform = `translateY(${offsetTop}px)`;
    root.style.height = `${viewport.height}px`;
  } else {
    root.style.removeProperty('transform');
    if (innerWidth <= 760 && viewport) {
      root.style.height = `${viewport.height}px`;
    } else {
      const gap = innerWidth <= 760 ? 0 : 14;
      // Focusing a control during a resize can scroll the still-tall document.
      // A negative viewport-relative top is NOT extra layout space: counting it
      // keeps the old oversized height and strands controls below the new viewport.
      const top = Math.max(gap, root.getBoundingClientRect().top);
      root.style.height = Math.max(220, (viewport?.height || innerHeight) - top - gap) + 'px';
    }
  }
  chatResizeComposer();
  if (wasAtBottom) {
    const el = $('#chat-scroll');
    if (el) {
      if (!chatViewportActive(v) && typeof el.scrollTo === 'function') {
        el.scrollTo({
          top: el.scrollHeight,
          behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
        });
      } else {
        chatBottom();
      }
      if ($('#chat-latest')) $('#chat-latest').hidden = true;
    }
  }
}
function chatDrawer(open, switching = false) {
  const root = $('#chat-root');
  if (!root) return;
  const sidebar = $('.chat-sidebar'),
    hidden = innerWidth <= 760 && !open;
  sidebar.inert = hidden;
  sidebar.setAttribute('aria-hidden', String(hidden));
  if (innerWidth > 760) {
    if (!switching) ChatUI.historyCollapsed = !open;
    root.classList.toggle('history-collapsed', !!ChatUI.historyCollapsed);
    root.classList.remove('drawer-open');
  } else {
    if (open && ChatUI.inspector) chatInspector('');
    root.classList.toggle('drawer-open', open);
    $('.chat-main').inert = open;
    if (open) {
      $('#chat-shade')?.setAttribute('aria-label', '关闭会话历史');
      chatOptions(false);
      $('#chat-new').focus();
    } else if (!switching) $('#chat-history-toggle').focus({ preventScroll: true });
  }
  if (innerWidth > 760) $('.chat-main').inert = false;
  $('#chat-history-toggle').setAttribute(
    'aria-expanded',
    String(innerWidth > 760 ? !ChatUI.historyCollapsed : open),
  );
  chatScheduleLayout();
}
function chatResizeComposer() {
  const el = $('#chat-compose');
  if (el) {
    el.style.height = 'auto';
    el.style.height =
      Math.min(
        Math.max(44, (window.visualViewport?.height || innerHeight) * 0.18),
        el.scrollHeight,
      ) + 'px';
    chatScheduleLayout();
  }
}
async function chatReplaceDraft(text) {
  const v = chatView(),
    gen = ChatUI.generation,
    draft = v.draft;
  if (draft.trim() && draft !== text) {
    const answer = await chatDialog({
      title: '替换当前草稿？',
      body: '输入框已有未发送内容。取消会保留原草稿，现有附件不会被删除。',
      confirmLabel: '替换草稿',
    });
    if (!answer || !chatCurrent(gen, v) || v.draft !== draft) return false;
  }
  chatSetDraft(text);
  return true;
}
function chatSetDraft(text) {
  const v = chatView();
  v.draft = text;
  $('#chat-compose').value = text;
  chatResizeComposer();
  $('#chat-compose').focus();
  chatSyncChrome();
}
function chatRestore(history = true) {
  const c = ChatUI,
    v = chatView();
  v.sessionStatus = c.selected?.status || 'new';
  $('#chat-compose').value = v.draft;
  $('#chat-provider').value = c.selected?.provider || c.provider;
  $('#chat-provider').disabled = !!c.selected;
  $('#chat-title').textContent = c.selected?.title || '新对话';
  $('#chat-subtitle').textContent = S.projects.find((p) => p.id === c.project)?.alias || '选择项目';
  $('#chat-cwd-button span').textContent = c.cwd;
  $('#chat-cwd-button').title = '工作目录：' + (c.selected?.cwd || c.cwd);
  $('#chat-send').disabled = v.busy;
  $('#chat-retry').hidden = !v.pending;
  $('#chat-interrupt').hidden = !v.active;
  $('#chat-send-mode').value = v.mode;
  if ($('#chat-selection-mode'))
    $('#chat-selection-mode').value = v.selectionMode || 'inherit_at_dispatch';
  chatResizeComposer();
  chatFiles();
  chatOutbox();
  chatSettings(v.settings);
  if (history) chatRenderHistory();
  $('#chat-find-input').value = v.search || '';
  $('#chat-find-count').textContent = '';
  chatTargetSync();
  chatSyncChrome();
  chatSyncAvailabilityHints();
}
