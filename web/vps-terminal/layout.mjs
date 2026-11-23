function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function action(text, fn, className = '') {
  const node = el('button', `vt-button ${className}`, text);
  node.type = 'button';
  node.addEventListener('click', fn);
  return node;
}
function formatVpsEndpoint(item) {
  const user = item?.username ? `${item.username}@` : '';
  if (item?.host) return `${user}${item.host}:${item.port || 22}`;
  return item?.username || '';
}
function matchesVpsQuery(vps, query) {
  const needle = (query || '').trim().toLowerCase();
  if (!needle) return true;
  return [vps.name, vps.host, vps.username, vps.region, vps.provider, vps.system]
    .filter(Boolean)
    .some((field) => String(field).toLowerCase().includes(needle));
}

const QUICK_CONNECT_LIMIT = 40;
const isComposing = (event) => event.isComposing || event.keyCode === 229;

export class TerminalLayout {
  constructor({ store, onBack }) {
    this.store = store;
    this.onBack = onBack;
    this.tabs = new Map();
    this.disposed = false;
    this.quickIndex = 0;
    this.confirmSessionId = null;
    this.lastFocusedTabId = null;

    this.root = el('section', 'vt-workspace');
    this.root.setAttribute('aria-label', 'VPS 终端工作区');

    this.heading = el('div', 'vt-heading');
    const back = action('← VPS', () => onBack?.(), 'vt-back');
    this.title = el('h1', '', '终端');
    this.summary = el('span', 'vt-summary');
    this.recentButton = action('最近会话', () => this.toggleRecent());
    this.heading.append(back, this.title, this.summary, this.recentButton);

    this.tabRow = el('div', 'vt-tab-row');
    this.tabList = el('div', 'vt-tabs');
    this.tabList.setAttribute('role', 'tablist');
    this.tabList.setAttribute('aria-label', 'SSH 会话');
    this.newButton = action(
      '＋ 新建',
      () => {
        const current = this.focused();
        if (current) {
          this.closeQuickConnect();
          this.run(() => store.duplicate(current));
        } else {
          this.toggleQuickConnect();
        }
      },
      'vt-new',
    );
    this.quickConnectButton = action('▾', () => this.toggleQuickConnect(), 'vt-new-pick');
    this.quickConnectButton.setAttribute('aria-label', '选择服务器新建终端');
    this.quickConnectButton.setAttribute('aria-haspopup', 'dialog');
    this.quickConnectButton.setAttribute('aria-expanded', 'false');
    this.quickConnect = this.makeQuickConnect();
    this.tabRow.append(this.tabList, this.newButton, this.quickConnectButton, this.quickConnect);

    this.toolbar = el('div', 'vt-toolbar');
    this.toolbar.setAttribute('aria-label', '终端操作');
    this.findButton = action('查找', () => this.focused()?.view.toggleSearch());
    this.copyButton = action('复制', () => this.focused()?.view.copy());
    this.pasteButton = action('粘贴', () => this.focused()?.view.clipboardPaste());
    this.splitButton = action('分屏', () =>
      this.store.secondaryId
        ? store.unsplit()
        : this.run(() => store.duplicate(this.focused(), true)),
    );
    this.fullscreenButton = action('全屏', () => this.toggleFullscreen());
    this.diagnosticsButton = action('诊断', () => this.focused()?.view.toggleDiagnostics());
    this.settingsDetails = this.makeSettings();
    this.endButton = action('结束会话', () => this.confirmClose(), 'vt-end');
    this.toolbar.append(
      this.findButton,
      this.copyButton,
      this.pasteButton,
      this.splitButton,
      this.fullscreenButton,
      this.diagnosticsButton,
      this.settingsDetails,
      this.endButton,
    );

    this.error = el('div', 'vt-workspace-error');
    this.error.hidden = true;
    this.error.setAttribute('role', 'alert');
    this.confirm = el('div', 'vt-close-confirm');
    this.confirm.hidden = true;
    this.recent = el('section', 'vt-recent');
    this.recent.hidden = true;

    this.deck = el('div', 'vt-deck');
    this.primary = el('div', 'vt-slot vt-primary-slot');
    this.secondary = el('div', 'vt-slot vt-secondary-slot');
    this.gutter = el('div', 'vt-gutter');
    this.gutter.tabIndex = 0;
    this.gutter.setAttribute('role', 'separator');
    this.gutter.setAttribute('aria-label', '调整终端分屏宽度');
    this.gutter.setAttribute('aria-orientation', 'vertical');
    this.gutter.setAttribute('aria-valuemin', '25');
    this.gutter.setAttribute('aria-valuemax', '75');
    this.gutter.setAttribute('aria-valuenow', '50');
    this.deck.append(this.primary, this.gutter, this.secondary);

    this.stash = el('div', 'vt-stash');
    this.stash.hidden = true;

    this.empty = this.makeEmpty();

    this.root.append(
      this.heading,
      this.tabRow,
      this.toolbar,
      this.error,
      this.confirm,
      this.recent,
      this.deck,
      this.empty,
      this.stash,
    );
    this.parking = el('div', 'vt-parking');
    this.parking.hidden = true;
    document.body.append(this.parking);
    this.parking.append(this.root);

    this.viewport = () => this.fitViewport();
    window.addEventListener('resize', this.viewport);
    globalThis.visualViewport?.addEventListener('resize', this.viewport);
    globalThis.visualViewport?.addEventListener('scroll', this.viewport);

    this.fullscreen = () => {
      this.syncFullscreenLabel();
      this.fitViewport();
    };
    document.addEventListener('fullscreenchange', this.fullscreen);

    this.outsidePointer = (event) => {
      if (!this.visible || this.disposed) return;
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (this.settingsDetails?.open && !this.settingsDetails.contains(target)) {
        this.settingsDetails.open = false;
      }
      if (
        !this.quickConnect.hidden &&
        !this.quickConnect.contains(target) &&
        !this.quickConnectButton.contains(target) &&
        !this.newButton.contains(target)
      ) {
        this.closeQuickConnect();
      }
      if (
        !this.recent.hidden &&
        !this.recent.contains(target) &&
        !this.recentButton.contains(target)
      ) {
        this.recent.hidden = true;
      }
    };
    document.addEventListener('pointerdown', this.outsidePointer);

    this.escapeHandler = (event) => {
      if (
        !this.visible ||
        this.disposed ||
        event.key !== 'Escape' ||
        event.defaultPrevented ||
        isComposing(event)
      )
        return;
      if (!this.quickConnect.hidden) {
        event.preventDefault();
        this.closeQuickConnect({ restoreFocus: true });
        return;
      }
      if (this.settingsDetails?.open) {
        event.preventDefault();
        this.settingsDetails.open = false;
        this.settingsDetails.querySelector('summary')?.focus();
        return;
      }
      if (!this.recent.hidden) {
        event.preventDefault();
        this.recent.hidden = true;
        this.recentButton.focus();
        return;
      }
      if (!this.confirm.hidden) {
        event.preventDefault();
        this.confirm.hidden = true;
        this.confirmSessionId = null;
        return;
      }
      if (this.root.classList.contains('vt-expanded')) {
        event.preventDefault();
        this.root.classList.remove('vt-expanded');
        this.syncFullscreenLabel();
        this.fitViewport();
      }
    };
    document.addEventListener('keydown', this.escapeHandler);

    this.setupGutter();
    this.render();
  }

  focused() {
    return this.store.records.get(this.store.focusedId) || this.store.active;
  }

  availableServers() {
    if (this.store.vpsCatalog.length) return this.store.vpsCatalog;
    const seen = new Set();
    const list = [];
    for (const record of this.store.records.values()) {
      if (record.vps?.id && !seen.has(record.vps.id) && record.vps.enabled !== false) {
        seen.add(record.vps.id);
        list.push(record.vps);
      }
    }
    return list;
  }

  async run(fn) {
    this.error.hidden = true;
    try {
      return await fn();
    } catch (error) {
      if (!this.disposed) {
        this.error.textContent = error.message || '暂时无法完成，请重试';
        this.error.hidden = false;
      }
    }
  }

  mount(container) {
    if (this.disposed) return;
    if (this.root.parentNode !== container) container.replaceChildren(this.root);
    this.root.hidden = false;
    this.visible = true;
    this.render();
    this.fitViewport();
  }

  hide() {
    if (this.disposed) return;
    this.visible = false;
    this.closeQuickConnect();
    if (this.settingsDetails) this.settingsDetails.open = false;
    if (document.fullscreenElement === this.root) document.exitFullscreen().catch(() => {});
    this.root.hidden = true;
    this.parking.append(this.root);
  }

  makeEmpty() {
    const container = el('div', 'vt-empty');
    const hero = el('div', 'vt-empty-hero');
    this.emptyHeading = el('h2', '', '在这里连接服务器');
    this.emptyNote = el('p', '', '选择一台 VPS，即可打开 SSH 终端。');
    hero.append(this.emptyHeading, this.emptyNote);

    this.emptyQuick = el('div', 'vt-empty-quick');
    this.emptyQuick.hidden = true;
    this.emptySearchInput = el('input', 'vt-empty-search');
    this.emptySearchInput.type = 'search';
    this.emptySearchInput.placeholder = '搜索服务器名称、IP、账号或地区';
    this.emptySearchInput.setAttribute('aria-label', '搜索可用服务器');
    this.emptySearchInput.addEventListener('input', () => this.renderEmptyServers());
    this.emptySearchInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        const first = this.filteredEmptyServers()[0];
        if (first && !this.store.creating.size) {
          event.preventDefault();
          this.run(() => this.store.open({ vps: first }));
        }
      }
    });
    this.emptyGrid = el('div', 'vt-empty-grid');
    this.emptyQuick.append(this.emptySearchInput, this.emptyGrid);

    this.emptyResume = el('div', 'vt-empty-resume');
    this.emptyResume.hidden = true;
    this.emptyResumeTitle = el('strong', 'vt-empty-section-title', '恢复远端持久会话');
    this.emptyResumeList = el('div', 'vt-empty-resume-list');
    this.emptyResume.append(this.emptyResumeTitle, this.emptyResumeList);

    const actions = el('div', 'vt-empty-actions');
    this.emptyBackButton = action('返回 VPS', () => this.onBack?.(), 'vt-primary');
    actions.append(this.emptyBackButton);

    container.append(hero, this.emptyQuick, this.emptyResume, actions);
    return container;
  }

  filteredEmptyServers() {
    return this.store.vpsCatalog.filter((vps) => matchesVpsQuery(vps, this.emptySearchInput.value));
  }

  renderEmptyServers() {
    const servers = this.store.vpsCatalog;
    this.emptyQuick.hidden = !servers.length;
    this.emptyBackButton.classList.toggle('vt-primary', !servers.length);
    if (!servers.length) {
      this.emptyGrid.replaceChildren();
      return;
    }
    const filtered = this.filteredEmptyServers();
    this.emptyGrid.replaceChildren();
    if (!filtered.length) {
      this.emptyGrid.append(el('p', 'vt-muted', '没有匹配的服务器'));
      return;
    }
    const busy = this.store.creating.size > 0;
    for (const vps of filtered.slice(0, 40)) {
      const card = el('div', 'vt-server-card');
      const info = el('div', 'vt-server-info');
      const titleRow = el('div', 'vt-server-title-row');
      titleRow.append(el('strong', 'vt-server-name', vps.name || 'VPS'));
      for (const tag of [vps.region, vps.system].filter(Boolean)) {
        titleRow.append(el('span', 'vt-badge', tag));
      }
      const endpoint = el('span', 'vt-server-endpoint', formatVpsEndpoint(vps) || 'SSH');
      info.append(titleRow, endpoint);
      const connectBtn = action(
        '连接',
        () => this.run(() => this.store.open({ vps })),
        'vt-primary vt-server-connect',
      );
      connectBtn.disabled = busy;
      card.append(info, connectBtn);
      this.emptyGrid.append(card);
    }
  }

  renderEmptyResume() {
    const sessions = this.store.remoteSessions
      .filter((session) => !['exited', 'closed', 'failed', 'revoked'].includes(session.state))
      .slice(0, 8);
    this.emptyResume.hidden = !sessions.length;
    this.emptyResumeList.replaceChildren();
    for (const session of sessions) {
      const id = session.session_id || session.id;
      const item = action('', () => this.run(() => this.store.restore(id)), 'vt-empty-session');
      item.append(
        el('strong', '', session.vps_name || session.name || 'VPS'),
        el(
          'span',
          'vt-muted',
          `${formatVpsEndpoint(session) || session.device_name || 'SSH'} · ${
            session.mode === 'tmux' ? '持久会话' : '普通会话'
          }`,
        ),
      );
      this.emptyResumeList.append(item);
    }
  }

  makeQuickConnect() {
    const popover = el('div', 'vt-quick-connect');
    popover.hidden = true;
    popover.setAttribute('role', 'dialog');
    popover.setAttribute('aria-label', '快速连接服务器');
    this.quickSearchInput = el('input', 'vt-quick-search');
    this.quickSearchInput.type = 'search';
    this.quickSearchInput.placeholder = '搜索名称、IP、账号或地区';
    this.quickSearchInput.setAttribute('aria-label', '搜索要连接的服务器');
    this.quickSearchInput.addEventListener('input', () => {
      this.quickIndex = 0;
      this.renderQuickConnect();
    });
    popover.addEventListener('keydown', (event) => {
      if (isComposing(event)) return;
      const items = this.filteredQuickServers();
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        this.closeQuickConnect({ restoreFocus: true });
        return;
      }
      if (!items.length) return;
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        this.quickIndex = (this.quickIndex + 1) % items.length;
        this.syncQuickSelection();
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        this.quickIndex = (this.quickIndex - 1 + items.length) % items.length;
        this.syncQuickSelection();
      } else if (event.key === 'Enter' && event.target === this.quickSearchInput) {
        event.preventDefault();
        const target = items[this.quickIndex] || items[0];
        if (target) this.openFromQuickConnect(target, event.shiftKey && !this.narrow);
      }
    });
    this.quickList = el('div', 'vt-quick-list');
    popover.append(this.quickSearchInput, this.quickList);
    return popover;
  }

  filteredQuickServers() {
    return this.availableServers()
      .filter((vps) => matchesVpsQuery(vps, this.quickSearchInput.value))
      .slice(0, QUICK_CONNECT_LIMIT);
  }

  toggleQuickConnect(force = this.quickConnect.hidden) {
    if (!force) {
      this.closeQuickConnect();
      return;
    }
    if (this.settingsDetails) this.settingsDetails.open = false;
    this.recent.hidden = true;
    this.quickConnect.hidden = false;
    this.quickConnectButton.setAttribute('aria-expanded', 'true');
    this.quickSearchInput.value = '';
    this.quickIndex = 0;
    this.renderQuickConnect();
    this.quickSearchInput.focus();
    this.store.refreshVpsCatalog().then(() => {
      if (!this.disposed && !this.quickConnect.hidden) this.renderQuickConnect();
    });
  }

  closeQuickConnect({ restoreFocus = false } = {}) {
    if (this.quickConnect.hidden) return;
    this.quickConnect.hidden = true;
    this.quickConnectButton.setAttribute('aria-expanded', 'false');
    if (restoreFocus) {
      if (this.focused()) this.focused().view.focus();
      else this.quickConnectButton.focus();
    }
  }

  openFromQuickConnect(vps, split = false) {
    this.closeQuickConnect();
    return this.run(() => this.store.open({ vps, newSession: true, split }));
  }

  syncQuickSelection() {
    const rows = [...this.quickList.querySelectorAll('.vt-quick-row')];
    rows.forEach((row, index) => {
      const active = index === this.quickIndex;
      row.classList.toggle('vt-quick-row-active', active);
      if (active) row.scrollIntoView?.({ block: 'nearest' });
    });
  }

  renderQuickConnect() {
    const items = this.filteredQuickServers();
    if (this.quickIndex >= items.length) this.quickIndex = 0;
    this.quickList.replaceChildren();
    if (!items.length) {
      const empty = el('div', 'vt-quick-empty');
      empty.append(
        el('span', 'vt-muted', '暂无匹配的可用服务器'),
        action('返回 VPS', () => {
          this.closeQuickConnect();
          this.onBack?.();
        }),
      );
      this.quickList.append(empty);
      return;
    }
    items.forEach((vps, index) => {
      const row = el('div', 'vt-quick-row');
      if (index === this.quickIndex) row.classList.add('vt-quick-row-active');
      const main = action('', () => this.openFromQuickConnect(vps, false), 'vt-quick-main');
      const titleLine = el('span', 'vt-quick-title');
      titleLine.append(el('strong', '', vps.name || 'VPS'));
      if (vps.region) titleLine.append(el('span', 'vt-badge', vps.region));
      main.append(titleLine, el('span', 'vt-quick-endpoint', formatVpsEndpoint(vps) || 'SSH'));
      const actions = el('span', 'vt-quick-actions');
      actions.append(action('新标签', () => this.openFromQuickConnect(vps, false), 'vt-quick-btn'));
      if (!this.narrow) {
        actions.append(
          action('右分屏', () => this.openFromQuickConnect(vps, true), 'vt-quick-btn'),
        );
      }
      row.append(main, actions);
      this.quickList.append(row);
    });
  }

  render() {
    if (this.disposed) return;
    const store = this.store,
      records = [...store.records.values()];
    const narrow = window.matchMedia('(max-width: 700px)').matches;
    this.narrow = narrow;
    this.splitButton.hidden = narrow;
    if (narrow && store.secondaryId && store.focusedId === store.secondaryId)
      [store.activeId, store.secondaryId] = [store.secondaryId, store.activeId];
    this.summary.textContent = records.length ? `${store.count()} 个会话` : '';
    this.empty.hidden = !!store.active;
    this.deck.hidden = !store.active;
    this.emptyHeading.textContent = store.creating.size ? '正在打开终端' : '在这里连接服务器';
    this.emptyNote.textContent = store.creating.size
      ? '正在确认连接电脑与 SSH 能力…'
      : '选择一台 VPS，即可直接打开 SSH 终端。';
    if (!store.active) {
      this.renderEmptyServers();
      this.renderEmptyResume();
    }
    this.newButton.disabled = store.creating.size > 0;
    this.quickConnectButton.disabled = store.creating.size > 0;
    this.splitButton.textContent = store.secondaryId ? '合并分屏' : '分屏';
    this.root.classList.toggle('vt-has-split', !!store.secondaryId);

    for (const [id, tab] of this.tabs)
      if (!store.records.has(id)) {
        tab.remove();
        this.tabs.delete(id);
      }
    for (const record of records) {
      let tab = this.tabs.get(record.id);
      if (!tab) {
        tab = el('div', 'vt-tab');
        tab.dataset.sessionId = record.id;
        tab.addEventListener('mousedown', (event) => {
          if (event.button === 1) event.preventDefault();
        });
        tab.addEventListener('auxclick', (event) => {
          if (event.button === 1) {
            event.preventDefault();
            store.detach(record.id);
          }
        });
        const choose = action('', () => store.activate(record.id));
        choose.className = 'vt-tab-select';
        choose.setAttribute('role', 'tab');
        choose.id = `vt-tab-${record.id}`;
        choose.setAttribute('aria-controls', `vt-pane-${record.id}`);
        const dot = el('span', 'vt-tab-dot');
        dot.setAttribute('aria-hidden', 'true');
        const label = el('span', 'vt-tab-label');
        const badge = el('span', 'vt-tab-badge', 'tmux');
        badge.setAttribute('aria-hidden', 'true');
        choose.append(dot, label, badge);
        choose.addEventListener('dblclick', () => this.rename(record, tab));
        choose.addEventListener('keydown', (event) => {
          if (event.key === 'F2') {
            event.preventDefault();
            this.rename(record, tab);
          }
          if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
            event.preventDefault();
            const ids = [...store.records.keys()],
              current = ids.indexOf(record.id);
            const index =
              event.key === 'Home'
                ? 0
                : event.key === 'End'
                  ? ids.length - 1
                  : (current + (event.key === 'ArrowRight' ? 1 : -1) + ids.length) % ids.length;
            store.activate(ids[index]);
            this.tabs.get(ids[index])?.querySelector('[role=tab]')?.focus();
          }
        });
        const close = action('×', () => store.detach(record.id), 'vt-tab-close');
        close.setAttribute('aria-label', `关闭 ${record.label} 的标签，保留远端会话`);
        tab.append(choose, close);
        this.tabs.set(record.id, tab);
        this.tabList.append(tab);
      }
      const selected = record.id === store.activeId || record.id === store.secondaryId,
        focused = record.id === store.focusedId;
      const choose = tab.querySelector('[role=tab]');
      choose.setAttribute('aria-selected', String(selected));
      choose.tabIndex = focused || (!store.focusedId && record.id === store.activeId) ? 0 : -1;
      tab.classList.toggle('vt-tab-active', selected);
      tab.classList.toggle('vt-tab-focused', focused);
      tab.dataset.state = record.state;
      tab.querySelector('.vt-tab-label').textContent = record.label;
      const badge = tab.querySelector('.vt-tab-badge');
      if (badge) badge.hidden = record.session.mode !== 'tmux';
      choose.title = [
        record.label,
        formatVpsEndpoint(record.session) || record.session.device_name || 'SSH',
        record.session.mode === 'tmux' && 'tmux 持久会话',
      ]
        .filter(Boolean)
        .join(' · ');
      if (focused && this.lastFocusedTabId !== record.id) {
        this.lastFocusedTabId = record.id;
        tab.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
      }
      const viewRoot = record.view.root;
      viewRoot.id = `vt-pane-${record.id}`;
      viewRoot.setAttribute('role', 'tabpanel');
      viewRoot.setAttribute('aria-labelledby', choose.id);
      const slot =
        record.id === store.activeId
          ? this.primary
          : record.id === store.secondaryId
            ? this.secondary
            : this.stash;
      if (viewRoot.parentNode !== slot) slot.append(viewRoot);
      viewRoot.classList.toggle('vt-pane-focused', focused);
      if (selected && this.visible) record.view.show();
    }

    const record = this.focused();
    if (!record) this.lastFocusedTabId = null;
    if (
      !this.confirm.hidden &&
      (!record ||
        record.id !== this.confirmSessionId ||
        ['exited', 'closed', 'revoked'].includes(record.state))
    ) {
      this.confirm.hidden = true;
      this.confirmSessionId = null;
    }

    for (const node of [
      this.findButton,
      this.copyButton,
      this.splitButton,
      this.endButton,
      this.diagnosticsButton,
    ])
      node.disabled = !record;
    this.pasteButton.disabled = !record?.transport.canInput;
    this.endButton.disabled =
      !record || record.closing || ['exited', 'closed', 'revoked'].includes(record.state);
    this.endButton.textContent = record?.closing ? '正在结束' : '结束会话';
    if (!this.recent.hidden) this.renderRecent();
    if (!this.quickConnect.hidden) this.renderQuickConnect();
  }

  rename(record, tab) {
    if (tab.querySelector('input')) return;
    const select = tab.querySelector('.vt-tab-select'),
      input = el('input', 'vt-tab-name');
    input.value = record.label;
    input.maxLength = 40;
    input.setAttribute('aria-label', '会话名称');
    select.hidden = true;
    tab.prepend(input);
    input.focus();
    input.select();
    const save = () => {
      if (!input.isConnected) return;
      this.store.rename(record.id, input.value);
      input.remove();
      select.hidden = false;
      select.focus();
    };
    input.addEventListener('blur', save, { once: true });
    input.addEventListener('keydown', (event) => {
      if (isComposing(event)) return;
      if (event.key === 'Enter') {
        event.preventDefault();
        save();
      }
      if (event.key === 'Escape') {
        event.preventDefault();
        input.value = record.label;
        save();
      }
    });
  }

  async toggleRecent() {
    this.closeQuickConnect();
    if (this.settingsDetails) this.settingsDetails.open = false;
    this.recent.hidden = !this.recent.hidden;
    if (this.recent.hidden) return;
    this.recent.replaceChildren(el('p', 'vt-muted', '正在读取会话…'));
    await this.run(() => this.store.refreshSessions());
    this.renderRecent();
  }

  renderRecent() {
    const heading = el('div', 'vt-recent-heading');
    heading.append(
      el('strong', '', '最近会话'),
      action('收起', () => {
        this.recent.hidden = true;
      }),
    );
    const list = el('div', 'vt-recent-list');
    const sessions = this.store.remoteSessions
      .filter((session) => !['exited', 'closed'].includes(session.state))
      .slice(0, 20);
    for (const session of sessions) {
      const id = session.session_id || session.id,
        row = action(
          '',
          () =>
            this.run(async () => {
              await this.store.restore(id);
              this.recent.hidden = true;
            }),
          'vt-recent-item',
        );
      row.append(
        el('strong', '', session.vps_name || session.name || 'VPS'),
        el(
          'span',
          'vt-muted',
          `${formatVpsEndpoint(session) || session.device_name || 'SSH'} · ${
            session.mode === 'tmux' ? '持久会话' : '普通会话'
          }`,
        ),
      );
      list.append(row);
    }
    if (!sessions.length) list.append(el('p', 'vt-muted', '暂无可恢复的会话'));
    this.recent.replaceChildren(heading, list);
  }

  makeSettings() {
    const details = el('details', 'vt-settings'),
      summary = el('summary', 'vt-button', '显示');
    details.addEventListener('toggle', () => {
      if (details.open) {
        this.closeQuickConnect();
        this.recent.hidden = true;
      }
    });
    const panel = el('div', 'vt-settings-panel');
    const sizes = el('div', 'vt-settings-row');
    sizes.append(
      el('span', '', '字号'),
      action('−', () => {
        const view = this.focused()?.view;
        if (view) view.setFontSize(view.fontSize - 1);
      }),
      action('＋', () => {
        const view = this.focused()?.view;
        if (view) view.setFontSize(view.fontSize + 1);
      }),
    );
    const themeLabel = el('label', 'vt-settings-row', '主题'),
      themes = el('select');
    themes.setAttribute('aria-label', '终端主题');
    for (const [value, label] of [
      ['system', '跟随面板'],
      ['light', '浅色'],
      ['dark', '深色'],
    ]) {
      const option = el('option', '', label);
      option.value = value;
      themes.append(option);
    }
    themes.addEventListener('change', () => {
      for (const record of this.store.records.values()) record.view.setTheme(themes.value);
    });
    themeLabel.append(themes);
    const accessible = el('label', 'vt-settings-row'),
      check = el('input');
    check.type = 'checkbox';
    check.addEventListener('change', () => {
      for (const record of this.store.records.values()) record.view.setScreenReader(check.checked);
    });
    accessible.append(check, document.createTextNode('屏幕阅读器模式'));
    panel.append(sizes, themeLabel, accessible);
    details.append(summary, panel);
    return details;
  }

  confirmClose() {
    const record = this.focused();
    if (!record || record.closing) return;
    this.confirmSessionId = record.id;
    this.confirm.replaceChildren(
      el('span', '', `结束“${record.label}”的远端会话？`),
      action('取消', () => {
        this.confirm.hidden = true;
        this.confirmSessionId = null;
      }),
      action(
        '结束会话',
        () => {
          this.confirm.hidden = true;
          this.confirmSessionId = null;
          this.run(() => this.store.close(record.id));
        },
        'vt-danger',
      ),
    );
    this.confirm.hidden = false;
  }

  syncFullscreenLabel() {
    const active =
      document.fullscreenElement === this.root || this.root.classList.contains('vt-expanded');
    this.fullscreenButton.textContent = active ? '退出全屏' : '全屏';
  }

  async toggleFullscreen() {
    try {
      if (document.fullscreenElement === this.root) await document.exitFullscreen();
      else if (this.root.classList.contains('vt-expanded')) {
        this.root.classList.remove('vt-expanded');
        this.syncFullscreenLabel();
        this.fitViewport();
      } else if (this.root.requestFullscreen) await this.root.requestFullscreen();
      else {
        this.root.classList.add('vt-expanded');
        this.syncFullscreenLabel();
        this.fitViewport();
      }
    } catch {
      this.root.classList.toggle('vt-expanded');
      this.syncFullscreenLabel();
      this.fitViewport();
    }
  }

  fitViewport() {
    if (!this.visible || this.disposed || this.resizeFrame) return;
    this.resizeFrame = requestAnimationFrame(() => {
      this.resizeFrame = null;
      const viewport = globalThis.visualViewport,
        height = viewport?.height || window.innerHeight;
      if (this.narrow !== window.matchMedia('(max-width: 700px)').matches) this.render();
      this.root.classList.toggle(
        'vt-keyboard-open',
        !!viewport && height < window.innerHeight * 0.75,
      );
      const offsetTop = viewport?.offsetTop || 0,
        bounds = this.root.getBoundingClientRect(),
        top = Math.max(0, bounds.top - offsetTop);
      let bottom = height;
      // The panel's fixed mobile navigation remains usable below the terminal.
      // Measure its actual safe-area/viewport geometry instead of assuming a
      // device-specific height. Fullscreen does not share that navigation.
      const dock = document.querySelector('.mobile-dock');
      if (
        dock &&
        document.fullscreenElement !== this.root &&
        !this.root.classList.contains('vt-expanded')
      ) {
        const style = getComputedStyle(dock),
          dockBounds = dock.getBoundingClientRect(),
          dockTop = dockBounds.top - offsetTop;
        if (
          style.display !== 'none' &&
          style.visibility !== 'hidden' &&
          dockBounds.width > 0 &&
          dockBounds.height > 0 &&
          dockBounds.right > bounds.left &&
          dockBounds.left < bounds.right &&
          dockTop > top
        )
          bottom = Math.min(bottom, dockTop);
      }
      this.root.style.setProperty(
        '--vt-available-height',
        `${Math.max(0, Math.floor(bottom - top - 12))}px`,
      );
      this.store.active?.view.fit();
      this.store.secondary?.view.fit();
    });
  }

  setupGutter() {
    const set = (value) => {
      this.ratio = Math.max(25, Math.min(75, value));
      this.deck.style.setProperty('--vt-split', `${this.ratio}%`);
      this.gutter.setAttribute('aria-valuenow', String(Math.round(this.ratio)));
    };
    this.gutter.addEventListener('dblclick', () => {
      set(50);
      this.store.active?.view.fit();
      this.store.secondary?.view.fit();
    });
    this.gutter.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      event.preventDefault();
      this.dragging = event.pointerId;
      this.gutter.setPointerCapture(event.pointerId);
    });
    this.gutter.addEventListener('pointermove', (event) => {
      if (this.dragging !== event.pointerId) return;
      const bounds = this.deck.getBoundingClientRect();
      set(((event.clientX - bounds.left) / bounds.width) * 100);
    });
    const end = (event) => {
      if (this.dragging === event.pointerId) {
        this.dragging = null;
        this.store.active?.view.fit();
        this.store.secondary?.view.fit();
      }
    };
    this.gutter.addEventListener('pointerup', end);
    this.gutter.addEventListener('pointercancel', end);
    this.gutter.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      set((this.ratio || 50) + (event.key === 'ArrowRight' ? 5 : -5));
      this.store.active?.view.fit();
      this.store.secondary?.view.fit();
    });
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    window.removeEventListener('resize', this.viewport);
    globalThis.visualViewport?.removeEventListener('resize', this.viewport);
    globalThis.visualViewport?.removeEventListener('scroll', this.viewport);
    document.removeEventListener('fullscreenchange', this.fullscreen);
    document.removeEventListener('pointerdown', this.outsidePointer);
    document.removeEventListener('keydown', this.escapeHandler);
    if (this.resizeFrame) cancelAnimationFrame(this.resizeFrame);
    this.root.remove();
    this.parking.remove();
    this.tabs.clear();
  }
}
