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

export class TerminalLayout {
  constructor({ store, onBack }) {
    this.store = store;
    this.onBack = onBack;
    this.tabs = new Map();
    this.disposed = false;
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
      () => this.run(() => store.duplicate(this.focused())),
      'vt-new',
    );
    this.tabRow.append(this.tabList, this.newButton);
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
    this.endButton = action('结束会话', () => this.confirmClose(), 'vt-end');
    this.toolbar.append(
      this.findButton,
      this.copyButton,
      this.pasteButton,
      this.splitButton,
      this.fullscreenButton,
      this.diagnosticsButton,
      this.makeSettings(),
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
    this.empty = el('div', 'vt-empty');
    this.emptyHeading = el('h2', '', '在这里连接服务器');
    this.emptyNote = el('p', '', '选择一台 VPS，即可打开 SSH 终端。');
    this.empty.append(
      this.emptyHeading,
      this.emptyNote,
      action('返回 VPS', () => onBack?.(), 'vt-primary'),
    );
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
      this.fullscreenButton.textContent =
        document.fullscreenElement === this.root ? '退出全屏' : '全屏';
      this.fitViewport();
    };
    document.addEventListener('fullscreenchange', this.fullscreen);
    this.setupGutter();
    this.render();
  }
  focused() {
    return this.store.records.get(this.store.focusedId) || this.store.active;
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
    if (document.fullscreenElement === this.root) document.exitFullscreen().catch(() => {});
    this.root.hidden = true;
    this.parking.append(this.root);
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
      : '选择一台 VPS，即可打开 SSH 终端。';
    this.newButton.disabled = !store.active || store.creating.size > 0;
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
        const choose = action('', () => store.activate(record.id));
        choose.className = 'vt-tab-select';
        choose.setAttribute('role', 'tab');
        choose.id = `vt-tab-${record.id}`;
        choose.setAttribute('aria-controls', `vt-pane-${record.id}`);
        const dot = el('span', 'vt-tab-dot');
        dot.setAttribute('aria-hidden', 'true');
        const label = el('span', 'vt-tab-label');
        choose.append(dot, label);
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
      choose.title = `${record.label} · ${record.session.device_name || record.session.project_alias || 'SSH'}`;
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
    this.title.textContent = record?.label || '终端';
    if (!this.recent.hidden) this.renderRecent();
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
          `${session.project_alias || 'SSH'} · ${session.mode === 'tmux' ? '持久会话' : '普通会话'}`,
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
    this.confirm.replaceChildren(
      el('span', '', `结束“${record.label}”的远端会话？`),
      action('取消', () => {
        this.confirm.hidden = true;
      }),
      action(
        '结束会话',
        () => {
          this.confirm.hidden = true;
          this.run(() => this.store.close(record.id));
        },
        'vt-danger',
      ),
    );
    this.confirm.hidden = false;
  }
  async toggleFullscreen() {
    try {
      if (document.fullscreenElement === this.root) await document.exitFullscreen();
      else if (this.root.requestFullscreen) await this.root.requestFullscreen();
      else {
        this.root.classList.toggle('vt-expanded');
        this.fullscreenButton.textContent = this.root.classList.contains('vt-expanded')
          ? '退出全屏'
          : '全屏';
        this.fitViewport();
      }
    } catch {
      this.root.classList.toggle('vt-expanded');
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
      if (this.dragging === event.pointerId) this.dragging = null;
    };
    this.gutter.addEventListener('pointerup', end);
    this.gutter.addEventListener('pointercancel', end);
    this.gutter.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      set((this.ratio || 50) + (event.key === 'ArrowRight' ? 5 : -5));
    });
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    window.removeEventListener('resize', this.viewport);
    globalThis.visualViewport?.removeEventListener('resize', this.viewport);
    globalThis.visualViewport?.removeEventListener('scroll', this.viewport);
    document.removeEventListener('fullscreenchange', this.fullscreen);
    if (this.resizeFrame) cancelAnimationFrame(this.resizeFrame);
    this.root.remove();
    this.parking.remove();
    this.tabs.clear();
  }
}
