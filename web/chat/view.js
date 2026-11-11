async function chatPage() {
  if ($('#chat-root')) {
    chatList();
    return;
  }
  chatDetach();
  const c = ChatUI,
    gen = c.generation;
  await loadBasics();
  if (!chatCurrent(gen)) return;
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
  <div class="chat-history-filter"><input id="chat-history-search" type="search" placeholder="搜索会话、项目或内容" aria-label="查找对话"></div>
  <div class="chat-history-chips"><details><summary>项目</summary><select id="chat-history-project" aria-label="筛选历史项目"><option value="">所有项目</option>${S.projects.map((p) => `<option value="${esc(p.id)}">${esc(p.alias)}</option>`).join('')}</select></details><details><summary>状态</summary><select id="chat-history-filter" aria-label="筛选会话"><option value="">全部</option><option value="running">运行中</option><option value="exited">已停止</option><option value="interrupted">已中断</option></select></details></div>
  <div id="chat-history-notice" class="chat-history-notice" role="alert" hidden><span></span><button type="button" id="chat-history-retry">重试列表</button></div>
  <div id="chat-history" class="chat-history" aria-label="历史对话列表" aria-busy="false"></div><div class="chat-history-pages"><button id="chat-history-prev" hidden>较新</button><span id="chat-history-count"></span><button id="chat-history-next" hidden>较早</button></div>
  <footer class="chat-sidebar-footer"><button type="button" id="chat-command-menu" class="chat-sidebar-action" aria-label="快捷操作" title="快捷操作">${chatIcon('command')}<span>快捷操作</span><kbd data-chat-shortcut="command">⌘ K</kbd></button></footer></aside>
  <div class="chat-main">
  <div id="chat-find-bar" class="chat-find-bar" hidden><input id="chat-find-input" type="search" placeholder="在已载入消息中查找" aria-label="消息关键词"><span id="chat-find-count"></span><button id="chat-find-prev" aria-label="上一个匹配">↑</button><button id="chat-find-next" aria-label="下一个匹配">↓</button><button id="chat-find-close" aria-label="关闭查找">×</button></div>
  <div id="chat-scroll" class="chat-scroll" tabindex="0" aria-label="对话消息"><button id="chat-older" class="chat-text-button" hidden>显示更早消息</button><div id="chat-empty" class="chat-empty"><div class="chat-empty-mark">${chatIcon('command')}</div><h2 id="chat-empty-title"></h2><div class="chat-starters"><button data-chat-starter="梳理这个项目的结构，指出值得优先处理的问题。">${chatIcon('folder')}<span><strong>了解项目</strong><small>梳理结构与关键入口</small></span></button><button data-chat-starter="检查当前改动，找出可复现的问题并给出修复建议。">${chatIcon('check')}<span><strong>检查改动</strong><small>找出问题与潜在风险</small></span></button><button data-chat-starter="先阅读项目说明和已有实现，再制定实施计划。">${chatIcon('command')}<span><strong>制定计划</strong><small>把想法拆成具体步骤</small></span></button></div></div><div id="chat-messages" class="chat-messages"></div><button type="button" class="chat-text-button" id="chat-resume" hidden>恢复会话</button></div>
  <button id="chat-latest" class="chat-latest" hidden>↓ 回到最新</button>
  <div class="chat-composer-wrap"><div id="chat-activity" class="chat-activity" hidden role="status"><i aria-hidden="true"></i><span id="chat-activity-text"></span><button type="button" id="chat-activity-queue"></button><div class="chat-run-controls"><select id="chat-send-mode" aria-label="发送方式" title="正在执行时如何发送新消息"><option value="followup">等这轮结束再发</option><option value="steer">插入当前回复</option></select><button type="button" id="chat-interrupt" class="chat-stop-turn" aria-label="中断当前" title="中断当前回复" hidden>${chatIcon('stop')}<span>中断当前</span></button></div></div><div id="chat-outbox" class="chat-outbox" aria-live="polite"></div><div id="chat-files" class="chat-files" aria-live="polite"></div>
  <div id="chat-target-notice" class="chat-catalog-notice" role="status" hidden><span></span><button type="button" id="chat-target-refresh" hidden>检查连接</button><button type="button" id="chat-catalog-retry" hidden>重新加载</button></div>


  <div class="chat-compose-frame">
  <form id="chat-form" class="chat-composer"><textarea id="chat-compose" rows="1" aria-label="消息" aria-describedby="chat-status chat-text-limit" placeholder="描述任务，输入 / 查看指令…" maxlength="${CHAT_MESSAGE_MAX_BYTES}"></textarea>
  <div class="chat-compose-bottom"><div class="chat-compose-controls"><button class="chat-square" type="button" id="chat-attach" aria-label="添加文件或图片" title="上传图片与文件">${chatIcon('plus')}</button><input type="file" id="chat-file-input" multiple hidden><button type="button" id="chat-options-toggle" class="chat-model-picker" aria-label="项目、CLI 与模型" aria-controls="chat-options" aria-expanded="false">${chatIcon('settings')}<span id="chat-config-summary">会话设置</span>${chatIcon('chevron')}</button></div>
  <div class="chat-send-actions"><button type="submit" id="chat-send" class="chat-send" aria-label="发送消息">${chatIcon('send')}</button></div></div></form>
  <button type="button" id="chat-options-shade" class="chat-options-shade" aria-label="关闭会话设置" tabindex="-1" hidden></button>
  <section id="chat-options" class="chat-options" role="region" aria-label="会话设置"><header class="chat-options-head"><div><strong>会话设置</strong></div><button type="button" id="chat-options-close" class="chat-square" aria-label="关闭会话设置">${chatIcon('close')}</button></header>
  <div id="chat-new-context" class="chat-new-context"><label>工作项目<select id="chat-project" aria-label="项目">${S.projects.map((p) => `<option value="${esc(p.id)}" ${p.id === c.project ? 'selected' : ''}>${esc(p.alias)}</option>`).join('')}</select></label><span id="chat-target-info"></span></div>
  <div class="chat-config-bar"><button type="button" id="chat-model-picker" class="chat-model-picker" aria-label="选择模型" aria-haspopup="dialog" aria-expanded="false">${chatIcon('model')}<span id="chat-model-name">选择模型</span>${chatIcon('chevron')}</button><label class="chat-cli-label"><span>CLI</span><select id="chat-provider" aria-label="原生 CLI"><option value="pi">Pi</option><option value="codex">Codex</option><option value="claude">Claude</option></select></label>  <label class="chat-effort-label">${chatIcon('brain')}<span>思考</span><select id="chat-effort-select" aria-label="思考强度"><option value="">本机默认</option></select></label>
  <button type="button" id="chat-cwd-button" class="chat-cwd-button" title="工作目录">${chatIcon('folder')}<span>.</span></button></div>
  <div class="chat-option-tools"><button type="button" id="chat-file-library" aria-label="附件库">${chatIcon('file')}<span>附件库</span></button></div></section></div>
  <div class="chat-setting-feedback"><span id="chat-setting-state" class="chat-setting-state"></span><button type="button" id="chat-settings-retry" class="chat-text-button" hidden>重试设置</button></div>
  <div class="chat-composer-note"><span id="chat-status" role="status" data-quiet="true">Enter 发送 · Shift+Enter 换行</span><span id="chat-text-limit" role="status" hidden></span><button id="chat-retry" hidden>重试</button></div></div>
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
  on('chat-shade', 'click', () => chatDrawer(false));
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
  on('chat-scroll', 'scroll', () => {
    const v = chatView();
    v.follow = chatAtBottom();
    v.scrollTop = $('#chat-scroll').scrollTop;
    $('#chat-latest').hidden = v.follow;
  });
  on('chat-latest', 'click', chatBottom);
  on('chat-older', 'click', () => {
    const v = chatView(),
      el = $('#chat-scroll'),
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
  for (const target of [window, window.visualViewport].filter(Boolean))
    target.addEventListener('resize', chatViewport, { signal: c.listeners.signal });
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
function chatViewport() {
  const root = $('#chat-root');
  if (!root) return;
  const viewport = window.visualViewport;
  const gap = innerWidth <= 760 ? 0 : 14;
  // Focusing a control during a resize can scroll the still-tall document.
  // A negative viewport-relative top is NOT extra layout space: counting it
  // keeps the old oversized height and strands controls below the new viewport.
  const top = Math.max(gap, root.getBoundingClientRect().top);
  root.style.height =
    Math.max(220, (viewport?.height || innerHeight) + (viewport?.offsetTop || 0) - top - gap) +
    'px';
  chatResizeComposer();
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
    root.classList.toggle('drawer-open', open);
    $('.chat-main').inert = open;
    if (open) {
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
  chatResizeComposer();
  chatFiles();
  chatOutbox();
  chatSettings(v.settings);
  if (history) chatRenderHistory();
  $('#chat-find-input').value = v.search || '';
  $('#chat-find-count').textContent = '';
  chatTargetSync();
  chatSyncChrome();
}
