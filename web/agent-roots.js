'use strict';

// Owner-facing Agent configuration. MCP project grants remain separate.
window.CodePierRoots = (() => {
  const pendingStates = new Set(['queued', 'running', 'reconnecting', 'cancelling', 'unknown']);
  const pending = new Map();
  let owner = null,
    ownerSpace = null,
    storageKey = null;

  function context() {
    if (owner === S.session && ownerSpace === S.space_id) return;
    owner = S.session;
    ownerSpace = S.space_id;
    pending.clear();
    storageKey =
      S.identity?.id && S.space_id
        ? `codepier-roots-pending-v1:${S.identity.id}:${S.space_id}`
        : null;
    if (!storageKey) return;
    try {
      const text = sessionStorage.getItem(storageKey);
      if (!text || text.length > 16384) return;
      const rows = JSON.parse(text);
      if (!Array.isArray(rows)) return;
      for (const [id, record] of rows.slice(-16).filter(Array.isArray)) {
        if (
          typeof id !== 'string' ||
          !/^[a-f0-9]{32}$/.test(id) ||
          !record ||
          typeof record.key !== 'string' ||
          !/^[A-Za-z0-9_.:-]{8,128}$/.test(record.key) ||
          !Number.isFinite(record.created) ||
          record.created > Date.now() ||
          Date.now() - record.created > 86400000
        )
          continue;
        pending.set(id, { key: record.key, created: record.created });
      }
    } catch {
      // Browser storage is an untrusted lookup hint, never an instruction to write.
    }
  }

  function remember(deviceId, record) {
    context();
    if (record) pending.set(deviceId, { key: record.key, created: record.created });
    else pending.delete(deviceId);
    if (!storageKey) return;
    try {
      if (pending.size) sessionStorage.setItem(storageKey, JSON.stringify([...pending].slice(-16)));
      else sessionStorage.removeItem(storageKey);
    } catch {
      // The in-memory receipt remains usable when browser storage is disabled.
    }
  }

  function markup() {
    return `<div class="agent-roots">
      <header class="roots-heading"><div><h3>授权目录</h3><p>为这台节点设置可访问的路径与权限。</p></div><div class="roots-toolbar"><button type="button" class="btn ghost small" data-roots="refresh">${icon('refresh')}刷新</button><button type="button" class="btn small" data-roots="add" disabled>${icon('plus')}新增路径</button></div></header>
      <div class="roots-state" role="status" aria-live="polite"><span class="badge neutral" data-roots-state>正在读取</span><p class="roots-status" data-roots-status>核对 Agent 当前配置…</p></div>
      <div class="roots-list" data-roots-list></div>
      <section class="roots-editor" data-roots-editor hidden>
        <h4 data-roots-title>新增授权路径</h4>
        <label class="field">节点上的目录路径<input data-root-field="path" autocomplete="off" spellcheck="false" maxlength="4096" placeholder="/Volumes/CodePier/Projects 或 D:/Projects"></label>
        <div class="roots-permissions" data-roots-permissions><label class="field">文件访问<select data-root-field="mode"><option value="write">可读取和编辑</option><option value="read">只读</option></select></label><label class="check"><input type="checkbox" data-root-field="tasks">允许任务执行</label></div>
        <div class="roots-scope" data-roots-scope></div>
        <div class="roots-editor-actions"><button type="button" class="btn ghost small" data-roots="cancel">收起</button><button type="button" class="btn primary small roots-primary" data-roots="save" disabled>验证并授权</button></div>
      </section>
      <p class="roots-feedback" data-roots-feedback role="status" aria-live="polite"></p>
      <div class="roots-toolbar" data-roots-recovery hidden><button type="button" class="btn small" data-roots="recover">核对保存结果</button></div>
    </div>`;
  }

  function mount(host, deviceId, options = {}) {
    context();
    host._codepierRootsDispose?.();
    const login = S.session,
      space = S.space_id,
      endpoint = `/api/devices/${encodeURIComponent(deviceId)}/roots`;
    let disposed = false,
      timer = null,
      loading = false,
      saving = false,
      snapshot = null,
      action = 'upsert',
      editingExisting = false,
      record = pending.get(deviceId) || null,
      awaiting = null,
      reviewNotice = null,
      changedHere = false,
      initialized = false,
      recoveryMisses = 0;
    host.innerHTML = markup();
    const find = (selector) => $(selector, host),
      field = (name) => find(`[data-root-field="${name}"]`),
      editor = find('[data-roots-editor]'),
      feedback = find('[data-roots-feedback]'),
      saveButton = find('[data-roots="save"]');
    const current = () =>
      !disposed &&
      host.isConnected &&
      S.session === login &&
      S.space_id === space &&
      (!options.current || options.current());
    const checkDevice = (value) => {
      if (!value || typeof value !== 'object' || (value.device_id && value.device_id !== deviceId))
        throw new Error('响应节点与当前节点不一致，未采用该结果。');
    };
    const later = (fn, delay = 1800) => {
      clearTimeout(timer);
      if (current())
        timer = setTimeout(() => {
          if (current()) fn();
        }, delay);
    };
    const state = (label, message, tone = 'neutral') => {
      if (!current()) return;
      const badge = find('[data-roots-state]');
      badge.textContent = label;
      badge.className = `badge ${tone}`;
      find('[data-roots-status]').textContent = message || '';
    };
    const tell = (message = '', error = false) => {
      if (!current()) return;
      feedback.textContent = message;
      feedback.dataset.tone = error ? 'error' : '';
    };
    const lock = () => {
      if (!current()) return;
      const blocked = loading || saving || !!record || !!awaiting;
      const editable = snapshot?.state === 'applied' && !!snapshot?.config_sha256;
      host.setAttribute('aria-busy', String(loading || saving));
      host.dataset.submission = record || awaiting ? 'uncertain' : '';
      find('[data-roots="add"]').disabled = blocked || !editable;
      find('[data-roots="refresh"]').disabled = loading || saving;
      saveButton.disabled = blocked || !editable;
      find('[data-roots="cancel"]').disabled = saving;
      for (const control of $$('[data-root-field]', host)) control.disabled = blocked || !editable;
      field('path').readOnly = action === 'remove' || editingExisting;
      if (action !== 'remove' && field('mode').value === 'read') {
        field('tasks').checked = false;
        field('tasks').disabled = true;
      }
      for (const button of $$('[data-root-index]', host)) button.disabled = blocked || !editable;
      find('[data-roots-recovery]').hidden = !record && !awaiting;
      find('[data-roots="recover"]').disabled = loading || saving;
    };
    const scope = () => {
      const path = field('path').value.trim();
      find('[data-roots-scope]').innerHTML =
        action === 'remove'
          ? `<strong>撤销此条目录授权</strong><code>${esc(path || '未填写路径')}</code><p>项目文件会保留。父目录授权仍可能覆盖此路径，具体权限以最匹配的目录为准。</p>`
          : `<strong>${field('mode').value === 'read' ? '只读' : '读取和编辑'}${field('tasks').checked ? ' · 允许任务' : ''}</strong><code>${esc(path || '填写后显示授权范围')}</code><p>范围包含此目录及子目录；更具体的目录条目优先。项目映射与调用端权限仍分别校验。</p>`;
    };
    function edit(root = {}, nextAction = 'upsert', focus = true) {
      if (saving || record || awaiting || !current()) return;
      action = nextAction;
      editingExisting = !!root.path && !!snapshot?.roots?.some((item) => item.path === root.path);
      editor.hidden = false;
      field('path').value = root.path || '';
      field('mode').value = root.writable === false ? 'read' : 'write';
      field('tasks').checked = root.allow_tasks === true;
      find('[data-roots-title]').textContent =
        action === 'remove' ? '撤销目录授权' : editingExisting ? '设置目录权限' : '新增授权路径';
      find('[data-roots-permissions]').hidden = action === 'remove';
      saveButton.textContent = action === 'remove' ? '撤销此目录授权' : '验证并授权';
      saveButton.classList.toggle('danger', action === 'remove');
      saveButton.classList.toggle('primary', action !== 'remove');
      tell();
      scope();
      lock();
      if (focus) field('path').focus({ preventScroll: true });
    }

    function show(value) {
      checkDevice(value);
      const roots = Array.isArray(value.roots)
        ? value.roots
            .map((root) => (typeof root === 'string' ? { path: root } : root))
            .filter((root) => root && typeof root.path === 'string')
        : [];
      snapshot = { ...value, roots };
      find('[data-roots-list]').innerHTML =
        roots
          .map(
            (root, index) =>
              `<article class="roots-row"><div class="roots-row-main"><code>${esc(root.path)}</code><small>${typeof root.writable !== 'boolean' ? '权限未声明' : root.writable ? '可读取和编辑' : '只读'}${root.allow_tasks ? ' · 允许任务执行' : ''}${root.exists === false ? ' · 目录暂不可用' : ''}</small></div><div class="roots-row-actions"><button type="button" class="btn ghost small" data-root-index="${index}" data-root-action="upsert">编辑</button><button type="button" class="btn ghost small" data-root-index="${index}" data-root-action="remove">撤销</button></div></article>`,
          )
          .join('') ||
        `<p class="form-note">${value.observed_config === false ? '当前目录暂无法读取。' : '尚无授权目录。'}</p>`;
      const labels = {
        applied: ['已生效', `${roots.length} 个授权目录`],
        offline: ['节点离线', '上线后可读取当前目录并校验修改。'],
        pending_reload: ['已保存 · 生效待核对', '正在等待 Agent 加载配置…'],
        unsupported: ['需要更新 Agent', '当前版本尚不支持面板管理授权目录。'],
        disabled: ['节点已停用', '启用节点后可校验和修改。'],
      };
      const message = labels[value.state] || ['暂不可编辑', value.reason || '正在核对节点状态。'];
      state(...message);
      if (value.reason) tell(value.reason);
      if (!initialized && !record && !awaiting && options.initial) {
        initialized = true;
        edit(options.initial, 'upsert', false);
        if (options.autoSave && value.state === 'applied' && value.config_sha256) {
          const initialPath = field('path').value;
          const initialMode = field('mode').value;
          const initialTasks = field('tasks').checked;
          setTimeout(() => {
            if (
              options.autoSave() &&
              !editor.hidden &&
              field('path').value === initialPath &&
              field('mode').value === initialMode &&
              field('tasks').checked === initialTasks
            )
              save();
          }, 0);
        }
      }
      lock();
    }

    async function completed(receipt) {
      let value = receipt;
      while (current()) {
        if (value.device_id && value.device_id !== deviceId)
          throw new Error('操作回执与当前节点不一致，未采用该结果。');
        if (value.result) {
          if (pendingStates.has(value.state)) {
            await pause(1200);
          } else if (
            value.result.ok &&
            (value.state === 'succeeded' ||
              (value.state === 'needs_review' && value.result.data?.state === 'needs_review'))
          ) {
            checkDevice(value.result.data);
            return value.result.data;
          } else {
            const error = Object.assign(
              new Error(value.result?.error?.message || value.error || '节点未完成本次操作。'),
              value.result?.error || {},
            );
            error.operation_id = value.operation_id || value.id;
            error.terminal = true;
            error.needsReview =
              ['needs_review', 'interrupted'].includes(value.state) ||
              [
                'DURABILITY_UNCONFIRMED',
                'WRITE_OUTCOME_UNKNOWN',
                'CONFIG_WRITE_UNCERTAIN',
                'CONFIG_OUTCOME_UNKNOWN',
              ].includes(error.code);
            throw error;
          }
        } else if (!value.pending && !pendingStates.has(value.state)) {
          return value;
        } else {
          await pause(1200);
        }
        if (!current()) return null;
        const id = value.operation_id || value.id;
        if (!id) throw new Error('节点没有返回可核对的操作编号。');
        value = await api(`/api/operations/${encodeURIComponent(id)}`);
      }
      return null;
    }

    async function finishApplied(value) {
      if (!current()) return;
      checkDevice(value);
      if (reviewNotice) {
        show(value);
        state('配置已加载 · 保存结果待核对', '当前权限已加载，原保存的持久性仍未确认。');
        tell(reviewNotice, true);
        return;
      }
      const matched = !awaiting || value.config_sha256 === awaiting;
      const notify = matched && changedHere;
      remember(deviceId, null);
      record = null;
      awaiting = null;
      changedHere = false;
      show(value);
      editor.hidden = true;
      lock();
      tell(matched ? '目录授权已生效。' : '配置随后发生了变化，已显示 Agent 当前实际权限。');
      invalidateBasics();
      if (notify && current()) await options.onApplied?.(value);
    }

    async function load() {
      if (!current() || loading || saving) return;
      loading = true;
      lock();
      try {
        const value = await completed(await api(endpoint));
        if (!current() || !value) return;
        show(value);
        if (awaiting && value.state === 'applied') await finishApplied(value);
        else if (awaiting || value.state === 'pending_reload') later(load);
      } catch (error) {
        if (!current()) return;
        state(awaiting ? '已保存 · 生效待核对' : '读取失败', '可刷新核对当前节点状态。');
        tell(error.message, true);
        if (awaiting && (!error.status || error.status >= 500)) later(load, 4000);
      } finally {
        loading = false;
        lock();
      }
    }

    async function accept(value) {
      if (!current() || !value) return;
      checkDevice(value);
      awaiting = value.config_sha256 || value.sha256 || null;
      if (!awaiting) throw new Error('保存回执缺少配置版本，正在继续核对原操作。');
      if (value.state === 'needs_review' || value.outcome_confirmed === false) {
        reviewNotice = value.message || '保存结果尚未确认，请按原操作核对配置与存储状态。';
        state('保存结果待核对', '正在读取当前配置，原操作编号会继续保留。');
        tell(reviewNotice, true);
        later(load, 600);
        return;
      }
      reviewNotice = null;
      state('已保存 · 生效待核对', '正在核对 Agent 已加载的权限…');
      if (value.state === 'applied') await finishApplied(value);
      else later(load, 600);
    }

    async function recover() {
      if (!current() || loading || saving) return;
      if (!record) return load();
      loading = true;
      lock();
      try {
        state('正在核对保存', '沿原操作查询结果…');
        const receipt = await api(`${endpoint}/requests/${encodeURIComponent(record.key)}`);
        if (!current()) return;
        if (receipt.state === 'not_found') {
          recoveryMisses += 1;
          state('保存结果未确认', '尚未找到原操作回执。');
          tell(
            recoveryMisses < 15
              ? '已保留原操作编号，正在继续查询。'
              : '暂未取得原保存回执。可点击“核对保存结果”继续查询。',
          );
          if (recoveryMisses < 15) later(recover, 4000);
          return;
        }
        const value = await completed(receipt.operation || receipt);
        if (current() && value) await accept(value);
      } catch (error) {
        if (!current()) return;
        if (error.terminal && !error.needsReview) {
          remember(deviceId, null);
          record = null;
          awaiting = null;
          changedHere = false;
          state('未完成', '输入已保留，可修正后重新校验。');
        } else {
          state('保存结果待核对', '原操作会继续保留。');
          if (!error.status || error.status >= 500) later(recover, 4000);
        }
        tell(error.message, true);
      } finally {
        loading = false;
        lock();
      }
    }

    async function save() {
      if (!current() || loading || saving || record || awaiting || saveButton.disabled) return;
      const path = field('path').value.trim();
      field('path').setCustomValidity(path ? '' : '请输入节点上的目录路径。');
      if (!field('path').reportValidity()) return;
      saving = true;
      lock();
      const change = {
        action,
        path,
        writable: field('mode').value === 'write',
        allow_tasks: field('mode').value === 'write' && field('tasks').checked,
        expected_sha256: snapshot.config_sha256,
      };
      let submitted = false;
      try {
        state('正在验证', '检查目录、权限和配置版本…');
        tell();
        const preview = await completed(
          await api(`${endpoint}/preview`, {
            method: 'POST',
            body: JSON.stringify({ ...change, idempotency_key: 'roots-preview-' + uid() }),
            requestTimeout: 30000,
          }),
        );
        if (!current() || !preview) return;
        if (preview.valid === false) throw new Error(preview.reason || '目录验证未通过。');
        record = { key: 'roots-save-' + uid(), created: Date.now() };
        remember(deviceId, record);
        changedHere = true;
        recoveryMisses = 0;
        submitted = true;
        lock();
        state('正在保存', 'Agent 将备份配置并加载新的权限…');
        const receipt = await api(endpoint, {
          method: 'POST',
          body: JSON.stringify({ ...change, idempotency_key: record.key }),
          requestTimeout: 30000,
        });
        if (!current()) return;
        const value = await completed(receipt);
        if (current() && value) await accept(value);
      } catch (error) {
        if (!current()) return;
        const definitive =
          !submitted ||
          error.admitted === false ||
          (error.terminal && !error.needsReview) ||
          (!error.operation_id && error.status >= 400 && error.status < 500);
        if (definitive) {
          remember(deviceId, null);
          record = null;
          awaiting = null;
          changedHere = false;
          state('未保存', '输入已保留，可修正后重新校验。');
          if (
            ['CONFIG_CHANGED', 'CONFIG_CONFLICT', 'SHA_CONFLICT', 'SETTINGS_CHANGED'].includes(
              error.code,
            )
          ) {
            snapshot = null;
            later(load, 300);
          }
        } else {
          state('保存结果待核对', '正在恢复原操作回执…');
          later(recover, 1800);
        }
        tell(error.message, true);
      } finally {
        saving = false;
        lock();
      }
    }

    find('[data-roots="add"]').onclick = () => edit();
    find('[data-roots="cancel"]').onclick = () => {
      editor.hidden = true;
      tell();
    };
    find('[data-roots="refresh"]').onclick = () => (record ? recover() : load());
    find('[data-roots="recover"]').onclick = () => {
      recoveryMisses = 0;
      return record && !awaiting ? recover() : load();
    };
    saveButton.onclick = save;
    find('[data-roots-list]').onclick = (event) => {
      const button = event.target.closest('[data-root-index]');
      if (!button || button.disabled) return;
      const root = snapshot?.roots?.[Number(button.dataset.rootIndex)];
      if (root) edit(root, button.dataset.rootAction);
    };
    for (const input of $$('[data-root-field]', host)) {
      input.addEventListener('input', () => {
        input.setCustomValidity('');
        scope();
      });
      input.addEventListener('change', () => {
        lock();
        scope();
      });
      input.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') {
          event.preventDefault();
          save();
        }
      });
    }
    if (record) recover();
    else load();

    function dispose() {
      disposed = true;
      clearTimeout(timer);
    }
    host._codepierRootsDispose = dispose;
    return { dispose };
  }

  async function open(deviceId) {
    const session = S.session,
      space = S.space_id,
      page = S.page,
      intent = S.modalIntent || 0;
    await loadBasics();
    if (
      S.session !== session ||
      S.space_id !== space ||
      S.page !== page ||
      (S.modalIntent || 0) !== intent
    )
      return;
    const device = S.devices.find((item) => item.id === deviceId);
    if (!device) throw new Error('节点不存在或已移除，请刷新后重试。');
    const dialog = modal(
      '授权目录 · ' + device.name,
      '<div id="agent-roots-manager"></div>',
      '<button class="btn ghost" data-action="close-modal">完成</button>',
    );
    const manager = mount($('#agent-roots-manager', dialog), deviceId);
    const previousCleanup = S.modalCleanup;
    S.modalCleanup = () => {
      previousCleanup?.();
      manager.dispose();
    };
  }

  return { mount, open };
})();
