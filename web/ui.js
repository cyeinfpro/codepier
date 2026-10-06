'use strict';
// Presentation-only helpers. No credentials, commands or writes are cached here.
function uiHelp(title, body) {
  return `<details class="page-help"><summary>${icon('shield')}${esc(title)}</summary><div class="page-help-body">${body}</div></details>`;
}
function uiMenu(label, body) {
  return `<details class="tool-menu"><summary class="btn ghost">${icon('menu')}${esc(label)}</summary><div class="tool-menu-items">${body}</div></details>`;
}
function uiFocusable(root) {
  return $$(
    'button:not(:disabled),a[href],input:not(:disabled):not([type="hidden"]),select:not(:disabled),textarea:not(:disabled),summary,[tabindex="0"]',
    root,
  ).filter(
    (el) =>
      el.tabIndex >= 0 &&
      el.getClientRects().length &&
      !el.closest('[inert]') &&
      getComputedStyle(el).visibility === 'visible',
  );
}
function uiTrapTab(e, root) {
  if (e.key !== 'Tab') return;
  const items = uiFocusable(root),
    first = items[0],
    last = items.at(-1);
  if (!first) {
    e.preventDefault();
    root.focus();
    return;
  }
  if (e.shiftKey && (document.activeElement === first || !root.contains(document.activeElement))) {
    e.preventDefault();
    last.focus();
  } else if (
    !e.shiftKey &&
    (document.activeElement === last || !root.contains(document.activeElement))
  ) {
    e.preventDefault();
    first.focus();
  }
}
const uiMobileMedia = matchMedia('(max-width:900px)');
const uiDockPages = new Set(['overview', 'projects', 'native', 'workflows']);
function uiSyncNavigation() {
  $$('[data-nav]').forEach((b) => {
    const chosen = b.dataset.nav === S.page;
    b.classList.toggle('active', chosen);
    if (chosen) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  const more = $('.mobile-dock [data-action="toggle-menu"]');
  if (more) more.classList.toggle('active', !uiDockPages.has(S.page));
}
function uiSetMenu(value, restore = true, origin = null) {
  const side = $('.sidebar'),
    main = $('.main'),
    scrim = $('.sidebar-scrim');
  if (!side) return;
  const mobile = uiMobileMedia.matches;
  const open = mobile && (value === undefined ? !side.classList.contains('open') : !!value);
  if (open) {
    const trigger = origin || document.activeElement?.closest?.('[data-action="toggle-menu"]');
    if (trigger?.isConnected) S.uiMenuTrigger = trigger;
  }
  side.classList.toggle('open', open);
  side.inert = mobile && !open;
  side.setAttribute('aria-hidden', String(mobile && !open));
  if (scrim) scrim.hidden = !open;
  if (main) main.inert = open;
  $$('[data-action="toggle-menu"][aria-controls="sidebar"]').forEach((button) =>
    button.setAttribute('aria-expanded', String(open)),
  );
  document.body.style.overflow = open || $('.modal') ? 'hidden' : '';
  if (open) {
    side.setAttribute('role', 'dialog');
    side.setAttribute('aria-modal', 'true');
    requestAnimationFrame(() => {
      if (side.isConnected && side.classList.contains('open') && !$('.modal'))
        $('.mobile-close', side)?.focus({ preventScroll: true });
    });
  } else {
    side.removeAttribute('role');
    side.removeAttribute('aria-modal');
    const trigger =
      S.uiMenuTrigger?.isConnected && S.uiMenuTrigger.getClientRects().length
        ? S.uiMenuTrigger
        : $('.mobile-menu');
    if (restore && mobile && trigger?.isConnected) trigger.focus({ preventScroll: true });
    S.uiMenuTrigger = null;
  }
}
function uiSetPane(pane, focus = false) {
  if (!['files', 'editor'].includes(pane)) return;
  S.work.pane = pane;
  const area = $('.workspace');
  if (area) area.dataset.pane = pane;
  $$('[data-ui-pane]').forEach((b) => {
    const selected = b.dataset.uiPane === pane;
    b.classList.toggle('active', selected);
    b.setAttribute('aria-pressed', String(selected));
  });
  if (focus && matchMedia('(max-width:640px)').matches) {
    const target = pane === 'editor' ? $('#code-editor') : $('[data-action="tree-home"]');
    target?.focus({ preventScroll: true });
  }
}
function uiLabelFields(root) {
  $$('.field', root).forEach((field) => {
    const label = field.matches('label') ? field : $(':scope > label', field);
    if (!label || label.classList.contains('check')) return;
    let caption = label;
    if (label === field) {
      caption = $(':scope > .field-label', label);
      if (!caption) {
        caption = document.createElement('span');
        caption.className = 'field-label';
        const text = [...label.childNodes].filter((node) => node.nodeType === Node.TEXT_NODE);
        caption.append(...text);
        label.prepend(caption);
      }
    }
    const group = $(':scope > .check-list', field);
    if (group && !label.control) {
      if (!label.id) label.id = 'group-' + uid();
      group.setAttribute('role', 'group');
      group.setAttribute('aria-labelledby', label.id);
      return;
    }
    const control = label.control || $('input:not([type="hidden"]),select,textarea', field);
    if (!control) return;
    if (!control.id) control.id = 'field-' + uid();
    if (!label.contains(control)) label.htmlFor = control.id;
    if (control.required && !$('.field-required', caption)) {
      const mark = document.createElement('span');
      mark.className = 'field-required';
      mark.textContent = '必填';
      mark.setAttribute('aria-hidden', 'true');
      caption.append(mark);
    }
    const descriptions = new Set(
      (control.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean),
    );
    $$(':scope > small,:scope > .field-hint', field).forEach((hint) => {
      if (!hint.id) hint.id = 'hint-' + uid();
      descriptions.add(hint.id);
    });
    if (descriptions.size) control.setAttribute('aria-describedby', [...descriptions].join(' '));
  });
}
// Native constraints remain authoritative. No validation request or global DOM scan.
document.addEventListener(
  'invalid',
  (e) => {
    const control = e.target;
    if (control.matches?.('.field input,.field select,.field textarea')) {
      control.setAttribute('aria-invalid', 'true');
      let disclosure = control.closest('details:not([open])');
      while (disclosure) {
        disclosure.open = true;
        disclosure = disclosure.parentElement?.closest('details:not([open])');
      }
    }
  },
  true,
);
document.addEventListener('input', (e) => {
  const control = e.target;
  if (control.matches?.('.field input,.field select,.field textarea') && control.validity.valid)
    control.removeAttribute('aria-invalid');
});
function uiFilterProjects() {
  const input = $('#project-query');
  if (!input) return;
  const query = input.value.trim().toLocaleLowerCase();
  S.uiProjectQuery = input.value;
  let count = 0;
  $$('[data-project-row]').forEach((row) => {
    row.hidden = !row.dataset.projectSearch.includes(query);
    if (!row.hidden) count++;
  });
  const status = $('#project-count');
  if (status) status.textContent = `${count} / ${S.projects.length} 个项目`;
  const empty = $('#project-no-match');
  if (empty) empty.hidden = count > 0;
}
function uiFilterTools() {
  const input = $('#tool-query');
  if (!input) return;
  const query = input.value.trim().toLocaleLowerCase();
  let count = 0;
  $$('[data-tool-name]').forEach((chip) => {
    chip.hidden = !chip.dataset.toolName.includes(query);
    if (!chip.hidden) count++;
  });
  const countNode = $('#tool-count');
  if (countNode) countNode.textContent = `${count} 项`;
}
// Preserve the original click target until a held pointer gesture finishes.
// Replacing #page between pointerdown and pointerup makes browsers drop click.
const uiPagePointers = new Set(),
  uiPagePointerWaiters = new Set();
document.addEventListener(
  'pointerdown',
  (event) => {
    if (event.button === 0 && event.target.closest('#page')) {
      uiPagePointers.add(event.pointerId);
      document.body.classList.add('ui-page-press');
    }
  },
  true,
);
function uiReleasePagePointer(event) {
  if (event.type === 'blur') uiPagePointers.clear();
  else uiPagePointers.delete(event.pointerId);
  if (!uiPagePointers.size)
    setTimeout(() => {
      if (uiPagePointers.size) return;
      document.body.classList.remove('ui-page-press');
      for (const resolve of uiPagePointerWaiters) resolve();
      uiPagePointerWaiters.clear();
    }, 0); // Let the matching click dispatch before a background render resumes.
}
for (const type of ['pointerup', 'pointercancel'])
  window.addEventListener(type, uiReleasePagePointer, true);
window.addEventListener('blur', uiReleasePagePointer);
async function uiWaitForPagePointer() {
  while (uiPagePointers.size) await new Promise((resolve) => uiPagePointerWaiters.add(resolve));
}
function uiCapturePage() {
  const page = $('#page');
  if (!page) return null;
  const active = document.activeElement;
  return {
    opened: $$('details[open]', page).map((d) => d.id || $('summary', d)?.textContent),
    field:
      page.contains(active) && active.id
        ? {
            id: active.id,
            value: active.value,
            start: active.selectionStart,
            end: active.selectionEnd,
          }
        : null,
  };
}
function uiPageReady(animate = false, presentation = null) {
  const page = $('#page');
  if (!page) return;
  document.title = `${nav.find((n) => n[0] === S.page)?.[2] || '控制台'} · CodePier`;
  page.dataset.page = S.page;
  page.classList.remove('management-page');
  if (animate) {
    page.classList.remove('page-arrive');
    void page.offsetWidth;
    page.classList.add('page-arrive');
  }
  uiSyncNavigation();
  $$('.data-table', page).forEach((table) => {
    table.classList.add('responsive-table');
    table.setAttribute('role', 'table');
    const headers = $$('thead th', table);
    headers.forEach((th) => th.setAttribute('scope', 'col'));
    $$('tbody tr', table).forEach((row) => {
      row.setAttribute('role', 'row');
      [...row.cells].forEach((cell, i) => {
        cell.dataset.label = headers[i]?.textContent?.trim() || '';
        cell.setAttribute('role', 'cell');
      });
    });
  });
  uiManagementLayout(page);
  uiLabelFields(page);
  const projectInput = $('#project-query');
  if (projectInput) {
    projectInput.value = S.uiProjectQuery || '';
    projectInput.oninput = uiFilterProjects;
    uiFilterProjects();
  }
  const tools = $('#tool-query');
  if (tools) {
    tools.oninput = uiFilterTools;
    uiFilterTools();
  }
  if (S.page === 'workbench') uiSetPane(S.work.pane || (S.work.path ? 'editor' : 'files'));
  const heading = $('h1', page);
  if (heading) heading.tabIndex = -1;
  if (presentation) {
    $$('details', page).forEach((d) => {
      if (presentation.opened.includes(d.id || $('summary', d)?.textContent)) d.open = true;
    });
    const saved = presentation.field,
      field = saved && document.getElementById(saved.id);
    if (field && page.contains(field)) {
      if (saved.value !== undefined) field.value = saved.value;
      field.focus({ preventScroll: true });
      if (typeof saved.start === 'number' && typeof field.setSelectionRange === 'function')
        try {
          field.setSelectionRange(saved.start, saved.end);
        } catch {}
      if (field.id === 'project-query') uiFilterProjects();
    }
  }
}
async function uiCommand() {
  if (!S.session || $('.modal')) return;
  uiSetMenu(false, false);
  const session = S.session;
  const dialog = modal(
    '快速前往',
    `<div class="command-search">${icon('search')}<input id="command-query" role="combobox" aria-expanded="true" aria-autocomplete="list" aria-label="搜索页面或项目" placeholder="搜索页面或项目" autocomplete="off" aria-controls="command-results"></div><div class="command-results" id="command-results" role="listbox" aria-label="页面与项目"></div><p class="command-hint">↑ ↓ 选择 · Enter 打开 · Esc 关闭</p>`,
  );
  dialog.classList.add('command-dialog');
  const input = $('#command-query', dialog),
    results = $('#command-results', dialog);
  let projects = [...S.projects],
    filtered = [],
    active = -1;
  const entries = () => [
    ...nav
      .filter(([id]) => CodePierIdentity.canNavigate(id))
      .map(([id, ico, label]) => ({ id, ico, label, kind: 'page', hint: '页面' })),
    ...projects.map((p) => ({
      id: p.id,
      ico: 'folder',
      label: p.alias,
      kind: 'project',
      hint: p.device_name || '项目',
    })),
  ];
  const itemKey = (item) => item && item.kind + ':' + item.id;
  function sync() {
    $$('[data-command-index]', results).forEach((el, i) =>
      el.setAttribute('aria-selected', String(i === active)),
    );
    const option = active >= 0 ? $(`[data-command-index="${active}"]`, results) : null;
    if (option) input.setAttribute('aria-activedescendant', option.id);
    else input.removeAttribute('aria-activedescendant');
  }
  function render(reset = true) {
    const selected = itemKey(filtered[active]),
      q = input.value.trim().toLocaleLowerCase();
    filtered = entries().filter((item) =>
      (item.label + ' ' + item.id + ' ' + item.hint).toLocaleLowerCase().includes(q),
    );
    active = reset ? -1 : filtered.findIndex((item) => itemKey(item) === selected);
    const previous = new Map(
      $$('[data-command-key]', results).map((el) => [el.dataset.commandKey, el]),
    );
    const buttons = filtered.map((item, i) => {
      const key = itemKey(item),
        button = previous.get(key) || document.createElement('button');
      button.type = 'button';
      button.className = 'command-item';
      button.id = `command-option-${i}`;
      button.dataset.commandKey = key;
      button.dataset.commandIndex = String(i);
      button.setAttribute('role', 'option');
      button.tabIndex = -1;
      button.setAttribute('aria-selected', 'false');
      const content = `${icon(item.ico)}<span>${esc(item.label)}<small>${esc(item.hint)}</small></span>${icon('arrow')}`;
      if (button.innerHTML !== content) button.innerHTML = content;
      return button;
    });
    if (!buttons.length)
      results.innerHTML = '<div class="empty" role="status">没有匹配的页面或项目</div>';
    else if (
      buttons.length !== results.children.length ||
      buttons.some((button, i) => results.children[i] !== button)
    )
      results.replaceChildren(...buttons);
    sync();
  }
  async function choose(index) {
    const item = filtered[index];
    if (!item || session !== S.session || !dialog.isConnected) return;
    if (item.kind === 'project' && S.work.dirty && !confirm('打开项目将丢弃当前未保存草稿，继续？'))
      return;
    closeModal(dialog);
    if (item.kind === 'project') resetWork(item.id);
    await navigate(item.kind === 'project' ? 'workbench' : item.id);
  }
  input.oninput = () => render();
  results.onclick = (e) => {
    const button = e.target.closest('[data-command-index]');
    if (button)
      choose(Number(button.dataset.commandIndex)).catch((err) => toast(err.message, true));
  };
  results.onfocusin = (e) => {
    const option = e.target.closest('[data-command-index]');
    if (option) {
      active = Number(option.dataset.commandIndex);
      sync();
    }
  };
  input.onkeydown = (e) => {
    if (e.isComposing) return;
    if (['ArrowDown', 'ArrowUp'].includes(e.key)) {
      e.preventDefault();
      if (!filtered.length) return;
      active =
        active < 0
          ? e.key === 'ArrowDown'
            ? 0
            : filtered.length - 1
          : (active + (e.key === 'ArrowDown' ? 1 : -1) + filtered.length) % filtered.length;
      sync();
      $(`[data-command-index="${active}"]`, results)?.scrollIntoView({ block: 'nearest' });
    }
    if (e.key === 'Enter') {
      e.preventDefault();
      choose(Math.max(0, active)).catch((err) => toast(err.message, true));
    }
  };
  render();
  input.focus();
  try {
    const r = await api('/api/projects');
    if (dialog.isConnected && session === S.session) {
      projects = r.projects;
      S.projects = r.projects;
      render(false);
    }
  } catch (error) {
    if (dialog.isConnected && session === S.session) toast(error.message, true);
  }
}
document.addEventListener('click', (e) => {
  const button = e.target.closest('[data-ui]');
  if (button && !button.disabled) {
    if (button.dataset.ui === 'command') uiCommand().catch((err) => toast(err.message, true));
    if (button.dataset.ui === 'close-menu') uiSetMenu(false);
    if (button.dataset.ui === 'clear-project-query') {
      const input = $('#project-query');
      if (input) {
        input.value = '';
        uiFilterProjects();
        input.focus();
      }
    }
  }
  const pane = e.target.closest('[data-ui-pane]');
  if (pane) uiSetPane(pane.dataset.uiPane, true);
  // Native details retain keyboard semantics. Collapse only after the original action handler has run.
  $$('.tool-menu[open]').forEach((menu) => {
    if (!menu.contains(e.target) || e.target.closest('button'))
      setTimeout(() => menu.removeAttribute('open'), 0);
  });
});
document.addEventListener('keydown', (e) => {
  if (e.defaultPrevented || e.target?.closest?.('dialog.chat-sheet')) return;
  // Native terminal editor keys belong to Pi/Codex, not the panel palette.
  if (e.target?.closest?.('#native-terminal') && !$('.modal')) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k' && !e.isComposing) {
    if (!S.session) return;
    e.preventDefault();
    if ($('.command-dialog')) closeModal();
    else if (!$('.modal')) uiCommand().catch((err) => toast(err.message, true));
    return;
  }
  if ($('.modal')) return;
  const side = $('.sidebar.open');
  if (side) {
    if (e.key === 'Escape') {
      e.preventDefault();
      uiSetMenu(false);
    } else uiTrapTab(e, side);
    return;
  }
  if (e.key === 'Escape') {
    $$('.tool-menu[open]').forEach((menu) => {
      menu.removeAttribute('open');
      $('summary', menu)?.focus();
    });
  }
});
const uiViewportChanged = () => uiSetMenu(false, false);
if (typeof uiMobileMedia.addEventListener === 'function')
  uiMobileMedia.addEventListener('change', uiViewportChanged);
else if (typeof uiMobileMedia.addListener === 'function')
  uiMobileMedia.addListener(uiViewportChanged);
function uiSyncVisualViewport() {
  const viewport = window.visualViewport;
  const covered = viewport
    ? Math.max(0, window.innerHeight - viewport.height - viewport.offsetTop)
    : 0;
  document.documentElement.style.setProperty('--ui-visual-viewport-bottom', covered + 'px');
  document.body.classList.toggle('ui-keyboard-open', covered > 96);
}
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', uiSyncVisualViewport);
  window.visualViewport.addEventListener('scroll', uiSyncVisualViewport);
  uiSyncVisualViewport();
}

// Shared management composition. Only presentation state is retained in memory.
function uiPermissionState(title, reason) {
  return (
    heading(title, '', '当前空间的访问权限') +
    `<section class="permission-state"><span class="permission-symbol">${icon('lock')}</span><h2>此页面需要相应的管理权限</h2><p>${esc(reason)}</p><p class="muted">当前空间：${esc(CodePierIdentity.current()?.label || '未选择')}</p><button class="btn" data-nav="identity">返回我的账号</button></section>`
  );
}
function uiSectionTabs(host, groups) {
  const key = S.page;
  const tabs = S.managementTabs || (S.managementTabs = {});
  const bar = document.createElement('nav');
  bar.className = 'management-tabs';
  bar.setAttribute('role', 'tablist');
  bar.setAttribute('aria-label', '页面分区');
  const content = document.createElement('div');
  content.className = 'management-tab-content';
  const choose = (id, focus = false) => {
    tabs[key] = id;
    for (const button of bar.children) {
      const active = button.dataset.section === id;
      button.setAttribute('aria-selected', String(active));
      button.tabIndex = active ? 0 : -1;
      if (active && focus) button.focus();
    }
    for (const panel of content.children) panel.hidden = panel.dataset.section !== id;
  };
  for (const group of groups) {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = group.label;
    button.id = 'section-tab-' + group.id;
    button.dataset.section = group.id;
    button.setAttribute('role', 'tab');
    button.setAttribute('aria-controls', 'section-panel-' + group.id);
    button.onclick = () => choose(group.id);
    const pane = document.createElement('section');
    pane.id = 'section-panel-' + group.id;
    pane.className = 'management-grid';
    pane.dataset.section = group.id;
    pane.setAttribute('role', 'tabpanel');
    pane.setAttribute('aria-labelledby', button.id);
    pane.tabIndex = 0;
    pane.append(...group.panels);
    bar.append(button);
    content.append(pane);
  }
  bar.onkeydown = (event) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
    const current = [...bar.children].indexOf(document.activeElement);
    if (current < 0) return;
    event.preventDefault();
    const next =
      event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? groups.length - 1
          : (current + (event.key === 'ArrowRight' ? 1 : -1) + groups.length) % groups.length;
    choose(groups[next].id, true);
  };
  host.append(bar, content);
  choose(groups.some((g) => g.id === tabs[key]) ? tabs[key] : groups[0].id);
}
function uiManagementLayout(page) {
  if (
    !['identity', 'members', 'identity-admin', 'profiles', 'roles', 'mcp-gateway'].includes(S.page)
  )
    return;
  const host = $('#profiles-page,#roles-page,#gateway-page', page) || page;
  host.classList.add('management-page');
  const panels = $$(':scope > .panel', host);
  if (panels.length > 1 && S.page === 'mcp-gateway') {
    uiSectionTabs(host, [
      { id: 'services', label: '服务与账号', panels: panels.slice(0, 2) },
      { id: 'tools', label: '已发布工具', panels: panels.slice(2, 3) },
      { id: 'delegation', label: '我的委派', panels: panels.slice(3, 4) },
      { id: 'activity', label: '调用记录', panels: panels.slice(4) },
    ]);
  } else if (panels.length > 1) {
    const grid = document.createElement('div');
    grid.className = 'management-grid management-grid-' + S.page;
    panels[0].before(grid);
    grid.append(...panels);
  }
  $$('.grant-row', host).forEach((row) => {
    const main = row.firstElementChild;
    if (main?.tagName === 'DIV') main.classList.add('grant-info');
  });
  $$('.panel-body', host).forEach((body, index) => {
    const rows = $$(':scope > .grant-row', body);
    if (rows.length < 5) return;
    const filter = document.createElement('div');
    filter.className = 'entity-filter';
    const input = document.createElement('input');
    input.type = 'search';
    const filters = S.managementFilters || (S.managementFilters = {});
    const filterKey = S.page + ':' + index;
    input.id = 'management-filter-' + S.page + '-' + index;
    input.value = filters[filterKey] || '';
    input.placeholder = '搜索名称、范围或标识';
    input.setAttribute('aria-label', '筛选' + (body.previousElementSibling?.textContent || '列表'));
    const count = document.createElement('span');
    count.setAttribute('role', 'status');
    const update = () => {
      const query = input.value.trim().toLocaleLowerCase();
      filters[filterKey] = input.value;
      let visible = 0;
      rows.forEach((row) => {
        row.hidden = !row.textContent.toLocaleLowerCase().includes(query);
        if (!row.hidden) visible++;
      });
      count.textContent = `${visible} / ${rows.length} 项`;
    };
    input.oninput = update;
    filter.append(input, count);
    body.prepend(filter);
    update();
  });
}

// One rule at a time, without removing any control from the submitting form.
function uiPolicyEditor(dialog, preferLast = false) {
  const form = $('#role-form', dialog);
  if (!form) return;
  let layout = $('.policy-layout', form);
  if (!layout) {
    layout = document.createElement('div');
    layout.className = 'policy-layout';
    const navigation = document.createElement('aside');
    navigation.className = 'policy-navigation';
    navigation.innerHTML =
      '<h3>权限规则</h3><p class="form-note">每条规则独立限定资源；选择一条进行编辑。</p><div class="policy-rule-list"></div><div class="policy-add-actions"></div>';
    const body = document.createElement('div');
    body.className = 'policy-content';
    const projectRules = $('#role-project-rules', form),
      devices = $('#role-device-rules', form);
    projectRules.before(layout);
    body.append(projectRules, devices);
    $('.policy-add-actions', navigation).append(
      $('#role-add-project-rule', form),
      $('#role-add-device-rule', form),
    );
    layout.append(navigation, body);
  }
  const list = $('.policy-rule-list', layout),
    rules = $$('fieldset.role-rule', form);
  let selected = Number(dialog.dataset.policyIndex || 0);
  if (preferLast instanceof Element) {
    selected = rules.indexOf(preferLast);
    dialog.dataset.editorDirty = 'true';
  } else if (preferLast) selected = rules.length - 1;
  selected = Math.max(0, Math.min(selected, rules.length - 1));
  const existing = new Map([...list.children].map((button) => [button.dataset.ruleKey, button]));
  const buttons = [];
  const choose = (index, focus = false) => {
    dialog.dataset.policyIndex = String(index);
    rules.forEach((rule, i) => {
      rule.hidden = i !== index;
    });
    [...list.children].forEach((button, i) => {
      button.setAttribute('aria-pressed', String(i === index));
      if (focus && i === index) button.focus();
    });
  };
  rules.forEach((rule, index) => {
    rule.dataset.policyKey ||= uid();
    const button = existing.get(rule.dataset.policyKey) || document.createElement('button');
    button.type = 'button';
    button.dataset.ruleKey = rule.dataset.policyKey;
    let title = button.querySelector('span'),
      description = button.querySelector('small');
    if (!title) {
      title = document.createElement('span');
      description = document.createElement('small');
      button.append(title, description);
    }
    title.textContent = `${index + 1} · ${rule.hasAttribute('data-project-rule') ? '项目规则' : '设备委派'}`;
    const update = () => {
      const label = rule._policyButton?.querySelector('small');
      if (!label) return;
      const all = $('[name="all_projects"]', rule)?.checked;
      const resources = $$('[name="project"]:checked,[name="device"]:checked', rule).length;
      label.textContent = all
        ? '全部当前与未来项目'
        : resources
          ? `已选择 ${resources} 项资源`
          : '尚未选择资源';
    };
    rule._policyButton = button;
    button.onclick = () => choose(index, true);
    if (!rule.dataset.policyBound) {
      rule.addEventListener('change', update);
      rule.dataset.policyBound = 'true';
    }
    update();
    buttons.push(button);
  });
  let cursor = list.firstElementChild;
  for (const button of buttons) {
    if (button === cursor) cursor = cursor.nextElementSibling;
    else list.insertBefore(button, cursor);
  }
  for (const button of [...list.children]) if (!buttons.includes(button)) button.remove();
  let empty = $('.policy-empty', layout);
  if (!empty) {
    empty = document.createElement('p');
    empty.className = 'policy-empty';
    empty.textContent = '先添加项目规则或设备委派，再选择允许的能力与资源。';
    $('.policy-content', layout).append(empty);
  }
  empty.hidden = rules.length > 0;
  choose(selected);
  uiLabelFields(form);
}

// Menu movement follows the visible items; native select controls remain native.
document.addEventListener('keydown', (event) => {
  if (event.defaultPrevented || !['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key))
    return;
  const menu = event.target.closest?.('.tool-menu,.chat-overflow');
  if (!menu) return;
  const origin = menu.querySelector('summary');
  if (!menu.open && event.target !== origin) return;
  event.preventDefault();
  menu.open = true;
  const items = [...menu.querySelectorAll('button:not(:disabled),a[href]')].filter(
    (item) => item.getClientRects().length,
  );
  if (!items.length) return;
  const index = items.indexOf(document.activeElement);
  const next =
    event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? items.length - 1
        : index < 0
          ? event.key === 'ArrowUp'
            ? items.length - 1
            : 0
          : (index + (event.key === 'ArrowUp' ? -1 : 1) + items.length) % items.length;
  items[next].focus();
});
