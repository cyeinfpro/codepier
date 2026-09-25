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
  $$('.field > label', root).forEach((label) => {
    const field = label.closest('.field');
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
    if (control.required && !$('.field-required', label)) {
      const mark = document.createElement('span');
      mark.className = 'field-required';
      mark.textContent = '必填';
      mark.setAttribute('aria-hidden', 'true');
      label.append(mark);
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
    if (control.matches?.('.field input,.field select,.field textarea'))
      control.setAttribute('aria-invalid', 'true');
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
    ...nav.map(([id, ico, label]) => ({ id, ico, label, kind: 'page', hint: '页面' })),
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
