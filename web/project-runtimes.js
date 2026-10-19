'use strict';
// Panel configuration only. Original operations own saves; UI refresh never replays them.
window.CodePierProjectRuntime = (() => {
  const names = {
    project_clean_v1: '清洁模式',
    native_compat: '兼容模式',
    container_v1: '容器模式',
  };
  const hints = {
    project_clean_v1:
      '使用独立 HOME、临时目录和明确工具路径，不加载个人登录脚本。仍使用执行账号权限，不是系统沙箱。',
    native_compat:
      '保留原 Shell、个人环境继承及已有任务行为。适合依赖原生工具、签名或现有 SSH 配置的项目。',
    container_v1:
      '命令在固定镜像中运行，仅挂载此项目；默认不联网。需已安装的 Docker 服务和镜像，不满足条件时不会回退。',
  };
  const credentialKeys = ['github_cli', 'git_config', 'ssh_agent'];
  let sessionOwner = null,
    spaceOwner = null,
    activeDispose = null;
  const pending = new Map();
  let storageOwner = null;
  const storagePrefix = 'codepier-runtime-pending-v1:';
  function persistPending() {
    if (!storageOwner) return;
    try {
      if (!pending.size) sessionStorage.removeItem(storageOwner);
      else sessionStorage.setItem(storageOwner, JSON.stringify([...pending].slice(-16)));
    } catch {
      // Restricted storage keeps in-memory recovery; never retry a save.
    }
  }
  function restorePending() {
    storageOwner =
      S.identity?.id && S.space_id ? storagePrefix + S.identity.id + ':' + S.space_id : null;
    if (!storageOwner) return;
    try {
      const text = sessionStorage.getItem(storageOwner);
      if (!text || text.length > 16384) return;
      const rows = JSON.parse(text);
      if (!Array.isArray(rows)) return;
      const now = Date.now();
      for (const entry of rows.slice(-16)) {
        if (!Array.isArray(entry) || entry.length !== 2) continue;
        const [projectId, record] = entry;
        if (
          typeof projectId !== 'string' ||
          !/^[A-Za-z0-9_.:-]{1,100}$/.test(projectId) ||
          !record ||
          typeof record !== 'object' ||
          typeof record.idempotency_key !== 'string' ||
          !/^[A-Za-z0-9_.:-]{8,128}$/.test(record.idempotency_key) ||
          !Number.isFinite(record.created_at) ||
          record.created_at > now ||
          now - record.created_at > 86400000
        )
          continue;
        // Only opaque lookup metadata survives refresh. No profile, path,
        // environment value, command, token or authority is stored here.
        pending.set(projectId, {
          idempotency_key: record.idempotency_key,
          created_at: record.created_at,
        });
      }
    } catch {
      // Storage is untrusted; malformed data grants no authority and starts no work.
    }
    persistPending();
  }
  const lineItems = (value) =>
    value
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean);
  function markup() {
    return `<form id="project-runtime-form">
      <p class="runtime-intro">按项目选择执行环境。未单独配置的项目默认使用清洁模式；切换不会修改文件权限，也不会重启正在运行的命令。</p>
      <div class="runtime-state" role="status" aria-live="polite"><span class="badge neutral" data-runtime-state>正在读取</span><span data-runtime-current>核对节点当前配置…</span></div>
      <fieldset class="runtime-controls" disabled><legend class="runtime-legend">执行方式</legend>
        <div class="runtime-options">${Object.entries(names)
          .map(
            ([mode, name]) =>
              `<label class="runtime-option"><input type="radio" name="runtime_mode" value="${mode}" ${mode === 'project_clean_v1' ? 'checked' : ''}><span><strong>${name}${mode === 'project_clean_v1' ? '<em>默认 · 推荐</em>' : ''}</strong><small>${mode === 'project_clean_v1' ? '稳定的项目工具链与独立环境' : mode === 'native_compat' ? '沿用现有终端与原生工具' : '固定镜像与受限项目容器'}</small></span></label>`,
          )
          .join('')}</div>
        <p class="runtime-boundary" data-runtime-hint></p>
        <section data-runtime-container hidden><h3>容器配置</h3><p class="form-note">使用已安装的 Linux 镜像与本地 Docker socket。不会自动下载镜像；项目内写入会同步到实际项目文件。</p>
          <div class="form-row"><label class="field">Docker 程序路径<input name="engine" placeholder="/absolute/path/to/docker" maxlength="4096"></label><label class="field">Docker socket<input name="endpoint" placeholder="unix:///absolute/path/to/docker.sock" maxlength="4096"></label></div>
          <label class="field">固定镜像摘要<input name="image" placeholder="sha256:64位十六进制摘要" maxlength="512" spellcheck="false"></label>
          <details class="runtime-advanced"><summary>资源限制</summary><div class="runtime-resource-grid">${[
            ['uid', '用户 UID', 1000, 1, 2147483647, 1],
            ['gid', '组 GID', 1000, 1, 2147483647, 1],
            ['memory_mib', '内存（MiB）', 1024, 64, 65536, 1],
            ['cpus', 'CPU 核数', 1, 0.1, 32, 0.1],
            ['pids_limit', '进程数量', 128, 16, 4096, 1],
            ['tmp_mib', '临时空间（MiB）', 256, 16, 4096, 1],
            ['home_mib', 'HOME 空间（MiB）', 256, 16, 4096, 1],
          ]
            .map(
              ([key, label, value, min, max, step]) =>
                `<label class="field">${label}<input type="number" name="${key}" value="${value}" min="${min}" max="${max}" step="${step}"></label>`,
            )
            .join('')}</div></details>
        </section>
        <section class="runtime-credentials" data-runtime-credentials hidden aria-labelledby="runtime-credentials-title">
          <h3 id="runtime-credentials-title">复用已有登录</h3>
          <p class="form-note">默认复用节点执行账号的已有登录，可对此项目逐项关闭，同时保留清洁环境。令牌与密钥不会返回面板。</p>
          <div class="runtime-credential-options">${[
            ['github_cli', 'GitHub CLI', '复用 gh 已有登录与 GitHub 令牌'],
            ['git_config', 'Git 全局配置', '沿用身份、凭据助手及其他全局设置'],
            ['ssh_agent', 'SSH Agent', '使用已经加载到 SSH Agent 的密钥'],
          ]
            .map(
              ([key, label, hint]) =>
                `<label class="runtime-credential-option"><input type="checkbox" name="runtime_credential_${key}"><span><strong>${label}</strong><small>${hint}</small><small class="runtime-credential-status" data-runtime-credential-state="${key}"></small></span></label>`,
            )
            .join('')}</div>
          <p class="form-note">来源已配置只表示可供命令使用；账号是否有效、远端权限是否足够，由实际操作确认。</p>
        </section>
        <details class="runtime-advanced" data-runtime-toolchain><summary>高级设置 · Shell 与工具路径</summary>
          <label class="field">Shell<select name="runtime_shell"><option value="sh">sh · 非登录</option><option value="bash">bash · 不加载 profile/rc</option><option value="powershell">PowerShell · NoProfile</option></select></label>
          <label class="field">工具路径 PATH<textarea name="runtime_path" rows="4" placeholder=".venv/bin&#10;node_modules/.bin" spellcheck="false"></textarea><small>每行一个目录；清洁模式中的相对路径从项目根目录解析，不读取个人登录 PATH。</small></label>
          <label class="field" data-runtime-pass>允许继承的环境变量名称<textarea name="runtime_pass_env" rows="2" placeholder="HTTPS_PROXY&#10;SSL_CERT_FILE" spellcheck="false"></textarea><small>只填写变量名称，不填写凭据或变量值。</small></label>
        </details>
        <p class="form-note" data-runtime-env-note hidden></p>
      </fieldset>
      <p class="runtime-feedback" data-runtime-feedback role="status" aria-live="polite"></p>
    </form>`;
  }
  async function open(projectId) {
    const project = S.projects.find((item) => item.id === projectId);
    if (!project || !S.session) return;
    if (sessionOwner !== S.session || spaceOwner !== S.space_id) {
      pending.clear();
      sessionOwner = S.session;
      spaceOwner = S.space_id;
      restorePending();
    }
    const session = S.session,
      space = S.space_id,
      page = S.page;
    const dialog = modal(
      `执行环境 · ${project.alias}`,
      markup(),
      `<button class="btn ghost" type="button" data-runtime-refresh>重新读取</button><button class="btn ghost" type="button" data-runtime-review hidden>只读核对当前配置</button><button class="btn ghost" type="button" data-runtime-cancel>取消</button><button class="btn primary" type="submit" form="project-runtime-form" data-runtime-save disabled>保存并应用</button>`,
      true,
    );
    dialog.dataset.presentation = 'editor';
    dialog.dataset.editorForm = 'project-runtime-form';
    const form = $('#project-runtime-form', dialog),
      controls = $('.runtime-controls', dialog);
    const save = $('[data-runtime-save]', dialog),
      refresh = $('[data-runtime-refresh]', dialog),
      review = $('[data-runtime-review]', dialog);
    const state = $('[data-runtime-state]', dialog),
      currentLabel = $('[data-runtime-current]', dialog);
    const feedback = $('[data-runtime-feedback]', dialog),
      controller = new AbortController();
    let disposed = false,
      dirty = false,
      busy = false,
      loading = false,
      loaded = null,
      baseMode = null;
    let timer = null,
      attempts = 0,
      sequence = 0,
      submission = pending.get(projectId) || null;
    let fieldCache = {},
      awaitingActivation = false,
      activationRevision = null,
      readOperation = null;
    const current = () =>
      !disposed &&
      session === S.session &&
      space === S.space_id &&
      page === S.page &&
      dialog.isConnected &&
      $('.modal') === dialog;
    const field = (name) => form.elements.namedItem(name);
    const mode = () => field('runtime_mode').value;
    const request = (url, options = {}) =>
      api(url, { retryDelays: [], requestTimeout: 10000, signal: controller.signal, ...options });
    const url = '/api/projects/' + encodeURIComponent(projectId) + '/runtime';
    function status(label, text) {
      state.textContent = label;
      currentLabel.textContent = text;
    }
    function markDirty(value) {
      dirty = value;
      dialog.dataset.editorDirty = String(value);
    }
    function lock() {
      controls.disabled =
        busy || loading || !!submission || awaitingActivation || !loaded?.can_save;
      save.disabled = controls.disabled || !dirty;
      save.textContent = busy ? '正在保存…' : submission ? '等待原保存结果' : '保存并应用';
      refresh.disabled = busy || loading;
      review.disabled = busy || loading;
      refresh.textContent = submission ? '核对原保存' : '重新读取';
      form.dataset.submission = submission ? 'uncertain' : '';
      form.setAttribute('aria-busy', String(busy));
    }
    function groups() {
      const value = mode(),
        container = value === 'container_v1',
        compat = value === 'native_compat';
      $('[data-runtime-hint]', dialog).textContent = hints[value];
      $('[data-runtime-container]', dialog).hidden = !container;
      $('[data-runtime-toolchain]', dialog).hidden = compat;
      $('[data-runtime-pass]', dialog).hidden = container;
      for (const input of $('[data-runtime-container]', dialog).querySelectorAll('input')) {
        input.disabled = !container;
        input.required = container;
      }
      field('runtime_shell').disabled = compat;
      field('runtime_path').disabled = compat;
      field('runtime_pass_env').disabled = compat || container;
      $('[data-runtime-credentials]', dialog).hidden = compat || container;
      for (const key of credentialKeys) {
        const input = field('runtime_credential_' + key);
        input.disabled = compat || container;
        const observed = loaded?.readiness?.credentials?.[key];
        const saved =
          loaded?.profile?.mode === 'project_clean_v1' &&
          loaded.profile.credentials?.[key] !== false;
        const label = $('[data-runtime-credential-state="' + key + '"]', dialog);
        label.textContent =
          input.checked !== saved
            ? input.checked
              ? '待保存 · 将启用此来源'
              : '待保存 · 将停用此来源'
            : !input.checked
              ? '未启用'
              : observed?.state === 'configured'
                ? '来源已配置'
                : observed?.state === 'missing'
                  ? '未找到可用来源'
                  : '来源待核对';
      }
      const powershell = field('runtime_shell').querySelector('option[value="powershell"]');
      powershell.disabled = container;
      if (container && field('runtime_shell').value === 'powershell')
        field('runtime_shell').value = 'sh';
      const note = $('[data-runtime-env-note]', dialog);
      const keys = loaded?.environment_keys || [];
      note.hidden = !keys.length;
      note.textContent = keys.length
        ? value === loaded.profile?.mode
          ? `节点已有 ${keys.length} 个环境变量值，保持原模式时会保留；页面不读取这些值。`
          : `切换模式不会复制原模式的 ${keys.length} 个环境变量值，请核对项目依赖。`
        : '';
    }
    function captureFields() {
      return {
        shell: field('runtime_shell').value,
        path: field('runtime_path').value,
        pass: field('runtime_pass_env').value,
        credentials: Object.fromEntries(
          credentialKeys.map((key) => [key, field('runtime_credential_' + key).checked]),
        ),
      };
    }
    function show(value) {
      if (!current()) return;
      loaded = value;
      if (value.profile) {
        field('runtime_mode').value = value.profile.mode;
        field('runtime_shell').value = value.profile.shell || 'sh';
        field('runtime_path').value = (value.profile.path || []).join('\n');
        field('runtime_pass_env').value = (value.profile.pass_env || []).join('\n');
        for (const key of credentialKeys)
          field('runtime_credential_' + key).checked =
            value.profile.mode === 'project_clean_v1' && value.profile.credentials?.[key] !== false;
        for (const [key, entry] of Object.entries(value.profile.container || {}))
          if (field(key)) field(key).value = entry;
        baseMode = value.profile.mode;
        fieldCache = { [baseMode]: captureFields() };
      }
      const labels = {
        applied: '已生效',
        pending_reload: '已保存 · 待生效',
        configuration_conflict: '配置冲突',
        offline: '节点离线',
        upgrade_required: '需要更新 Agent',
      };
      status(
        labels[value.state] || '尚未确认',
        value.runtime
          ? `当前执行：${names[value.runtime.mode] || value.runtime.mode}${value.selection_source === 'default' ? ' · 系统默认' : ''}`
          : '尚未取得节点当前执行配置',
      );
      if (value.state === 'pending_reload')
        feedback.textContent = '配置已写入，正在核对 Agent 重新加载。运行中的命令不会重启。';
      else if (value.state === 'applied')
        feedback.textContent =
          value.readiness?.state === 'unavailable'
            ? '配置已加载，但执行依赖尚未就绪。请核对程序、容器服务或工具路径。'
            : '后续新命令使用此配置；文件权限、原任务与历史回执保持不变。';
      else if (value.state === 'configuration_conflict')
        feedback.textContent =
          '项目 ID 与别名配置不一致。明确选择并保存后，将统一绑定到此项目 ID。';
      else if (value.state === 'upgrade_required')
        feedback.textContent =
          '此节点尚未支持面板切换。请先更新 Agent；页面不会把推荐默认值当作实际生效配置。';
      else if (value.state === 'offline')
        feedback.textContent = '节点离线，未发送新的配置。恢复连接后重新读取即可。';
      markDirty(false);
      groups();
      lock();
    }
    function profile() {
      const value = mode();
      if (value === 'native_compat') return { mode: value };
      const result = {
        mode: value,
        shell: field('runtime_shell').value,
        path: lineItems(field('runtime_path').value),
      };
      if (value === 'container_v1') {
        result.container = {};
        for (const key of ['engine', 'endpoint', 'image'])
          result.container[key] = field(key).value.trim();
        for (const key of ['uid', 'gid', 'memory_mib', 'cpus', 'pids_limit', 'tmp_mib', 'home_mib'])
          result.container[key] = Number(field(key).value);
      } else {
        result.pass_env = lineItems(field('runtime_pass_env').value);
        result.credentials = Object.fromEntries(
          credentialKeys.map((key) => [key, field('runtime_credential_' + key).checked]),
        );
      }
      return result;
    }
    function later(action) {
      clearTimeout(timer);
      if (!current() || document.hidden) return;
      if (++attempts > 40) {
        feedback.textContent += ' 自动核对已停止，可点击“重新读取”或“核对原保存”继续。';
        review.hidden = !submission;
        return;
      }
      const delay = Math.min(4000, 500 * 2 ** Math.min(attempts - 1, 3));
      timer = setTimeout(() => {
        if (current() && !document.hidden) action();
      }, delay);
    }
    function finishSave(data) {
      pending.delete(projectId);
      persistPending();
      submission = null;
      awaitingActivation = true;
      activationRevision = data.saved_runtime?.revision || data.runtime?.revision;
      markDirty(false);
      review.hidden = true;
      // A recovered save is a historical receipt, even when it said applied.
      // Always read the current Agent before displaying a current-state claim.
      status('已保存 · 核对生效中', '原保存已完成，正在读取节点当前实际配置');
      feedback.textContent = '不会重新提交，也不会用历史保存结果覆盖后续配置变化。';
      lock();
      later(() => load());
    }
    async function recover() {
      if (!current() || busy || loading || !submission) return;
      loading = true;
      lock();
      try {
        const value = await request(
          url + '/receipts/' + encodeURIComponent(submission.idempotency_key),
        );
        if (!current()) return;
        if (value.state === 'succeeded' && value.result?.ok && value.result.data)
          finishSave(value.result.data);
        else if (['failed', 'cancelled'].includes(value.state)) {
          pending.delete(projectId);
          persistPending();
          submission = null;
          feedback.textContent =
            value.result?.error?.message ||
            value.error ||
            '原保存未完成；草稿已保留。重新读取当前配置后再提交。';
          status('保存未完成', '未声明新模式已生效');
          loaded = { ...loaded, can_save: false };
        } else if (['needs_review', 'interrupted'].includes(value.state)) {
          status('原保存需要核查', '结果未确认，不重发、不自动判定成功或失败');
          feedback.textContent =
            (value.result?.error?.message || value.error || '请检查节点当前配置与原操作。') +
            (value.operation_id ? ' 原操作：' + value.operation_id : '');
          review.hidden = false;
        } else {
          status(
            value.state === 'not_found' ? '保存结果未确认' : '保存处理中',
            '仅查询原保存，不重复提交',
          );
          feedback.textContent =
            value.state === 'not_found'
              ? '尚未找到原保存回执。请继续核对；不会用新编号重复保存。'
              : '正在查询同一个操作的完成回执…';
          later(recover);
        }
      } catch (error) {
        if (current()) {
          feedback.textContent =
            '暂时无法核对原保存。原编号已保留，请点击“核对原保存”。' + (error.message || '');
          if (!error.status || error.status >= 500) later(recover);
        }
      } finally {
        loading = false;
        if (current()) lock();
      }
    }
    async function load(reviewOnly = false) {
      if (!current() || busy || loading || (!reviewOnly && (dirty || submission))) return;
      loading = true;
      lock();
      const ticket = ++sequence;
      try {
        let value;
        if (readOperation) {
          const row = await request('/api/operations/' + encodeURIComponent(readOperation));
          if (!current() || ticket !== sequence) return;
          if (['queued', 'running', 'reconnecting', 'cancelling', 'unknown'].includes(row.state)) {
            later(() => load(reviewOnly));
            return;
          }
          readOperation = null;
          if (!row.result?.ok)
            throw new Error(row.result?.error?.message || row.error || '读取执行环境未完成');
          value = row.result.data;
        } else {
          value = await request(url);
          if (!current() || ticket !== sequence) return;
          if (value.pending && value.operation_id) {
            readOperation = value.operation_id;
            status('正在读取', '沿原读取编号等待结果，不重复发起读取');
            later(() => load(reviewOnly));
            return;
          }
        }
        if (!current() || ticket !== sequence || (dirty && !reviewOnly)) return;
        if (value.project_id !== projectId)
          throw new Error('执行环境响应与当前项目不匹配，未采用该结果。');
        if (awaitingActivation && value.state === 'offline') {
          status('已保存 · 等待节点重连', '配置重新加载中，尚未确认新模式生效');
          feedback.textContent =
            '原保存已完成，正在核对 Agent 重连后的实际配置；不会重复提交或重启命令。';
          later(() => load(reviewOnly));
          return;
        }
        awaitingActivation = value.state === 'pending_reload';
        show(value);
        if (reviewOnly && submission) {
          status(
            '原保存仍待核查',
            value.runtime
              ? '最新观察：' + (names[value.runtime.mode] || value.runtime.mode)
              : '尚未取得节点实际配置',
          );
          feedback.textContent =
            '已只读核对当前配置；这不补写或重放原保存，也不替原操作确认未知结果。';
          return;
        }
        if (
          activationRevision &&
          value.state === 'applied' &&
          value.runtime?.revision !== activationRevision
        )
          feedback.textContent =
            '配置在保存后再次发生变化，页面已读取节点当前实际配置；未覆盖后续修改。';
        if (['pending_reload', 'offline'].includes(value.state)) later(() => load(reviewOnly));
      } catch (error) {
        if (!current()) return;
        status(awaitingActivation ? '已保存 · 生效待核对' : '读取失败', '未发起新的保存');
        feedback.textContent = error.message;
        if (awaitingActivation || readOperation) later(() => load(reviewOnly));
        else loaded = null;
      } finally {
        loading = false;
        if (current()) lock();
      }
    }
    async function submit(event) {
      event?.preventDefault();
      if (
        !current() ||
        busy ||
        loading ||
        submission ||
        !loaded?.can_save ||
        !dirty ||
        !form.reportValidity()
      )
        return;
      clearTimeout(timer);
      attempts = 0;
      const payload = {
        expected_revision: loaded.revision,
        profile: profile(),
        idempotency_key: 'runtime-save-' + uid(),
      };
      submission = { idempotency_key: payload.idempotency_key, created_at: Date.now() };
      pending.set(projectId, submission);
      persistPending();
      busy = true;
      lock();
      feedback.textContent = '正在校验并保存此项目的执行环境…';
      try {
        const result = await request(url, {
          method: 'PUT',
          body: JSON.stringify(payload),
          requestTimeout: 15000,
        });
        if (!current()) return;
        if (result.pending) {
          status('已提交 · 待核对', '原保存正在处理');
          later(recover);
        } else finishSave(result);
      } catch (error) {
        if (!current()) return;
        if (
          [
            'NETWORK_UNCERTAIN',
            'DURABILITY_UNCONFIRMED',
            'INTERRUPTED',
            'JOURNAL_CHANGED',
          ].includes(error.code) ||
          !error.status ||
          error.status >= 500
        ) {
          status('保存结果未确认', '请核对原保存，不要重新提交');
          feedback.textContent =
            '连接中断，保存可能已经完成。点击“核对原保存”读取原回执，不会重复写入。';
        } else {
          pending.delete(projectId);
          persistPending();
          submission = null;
          feedback.textContent = error.message + ' 草稿已保留。';
          status('未保存', '未声明新模式已生效');
          if (
            [
              'SETTINGS_CHANGED',
              'RUNTIME_CONFIG_BUSY',
              'RUNTIME_CONFIG_CHANGED',
              'RUNTIME_CONFIG_RELOAD_REQUIRED',
            ].includes(error.code)
          )
            loaded.can_save = false;
        }
      } finally {
        busy = false;
        if (current()) lock();
      }
    }
    form.addEventListener('submit', submit);
    form.addEventListener('input', () => {
      if (!loading && !busy && loaded?.can_save) {
        clearTimeout(timer);
        markDirty(true);
        lock();
      }
    });
    form.addEventListener('change', (event) => {
      if (loading || busy || !loaded?.can_save) return;
      if (event.target.name === 'runtime_mode') {
        if (baseMode) fieldCache[baseMode] = captureFields();
        const value = mode();
        const fields =
          fieldCache[value] ||
          (value === 'container_v1'
            ? { shell: 'sh', path: '/usr/local/bin\n/usr/bin\n/bin', pass: '' }
            : {
                shell: loaded.clean_default_profile?.shell || 'sh',
                path: (loaded.clean_default_profile?.path || []).join('\n'),
                pass: '',
                credentials: loaded.clean_default_profile?.credentials,
              });
        field('runtime_shell').value = fields.shell;
        field('runtime_path').value = fields.path;
        field('runtime_pass_env').value = fields.pass;
        for (const key of credentialKeys)
          field('runtime_credential_' + key).checked =
            value === 'project_clean_v1' && fields.credentials?.[key] !== false;
        baseMode = value;
      }
      clearTimeout(timer);
      markDirty(true);
      groups();
      lock();
    });
    refresh.onclick = () => {
      attempts = 0;
      if (submission) return recover();
      if (dirty && !confirm('重新读取将放弃此页未保存的输入，是否继续？')) return;
      markDirty(false);
      return load();
    };
    review.onclick = () => {
      attempts = 0;
      return load(true);
    };
    const resume = () => {
      if (!current() || document.hidden || busy || loading) return;
      attempts = 0;
      if (submission && review.hidden) recover();
      else if (awaitingActivation || readOperation || loaded?.state === 'offline') load();
    };
    document.addEventListener('visibilitychange', resume);
    window.addEventListener('online', resume);
    $('[data-runtime-cancel]', dialog).onclick = () => panelDialogs.requestClose(dialog);
    const cleanup = S.modalCleanup;
    const dispose = () => {
      if (disposed) return;
      disposed = true;
      document.removeEventListener('visibilitychange', resume);
      window.removeEventListener('online', resume);
      ++sequence;
      clearTimeout(timer);
      controller.abort();
      loaded = null;
      fieldCache = {};
      cleanup?.();
    };
    activeDispose = dispose;
    S.modalCleanup = () => {
      dispose();
      if (activeDispose === dispose) activeDispose = null;
    };
    groups();
    if (submission) {
      status('有待核对的保存', '沿用原操作编号');
      recover();
    } else load();
  }
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-runtime-project]');
    if (button && !button.disabled)
      open(button.dataset.runtimeProject).catch((error) => toast(error.message, true));
  });
  return {
    open,
    reset() {
      activeDispose?.();
      activeDispose = null;
      pending.clear();
      persistPending();
      storageOwner = null;
      sessionOwner = null;
      spaceOwner = null;
    },
  };
})();
