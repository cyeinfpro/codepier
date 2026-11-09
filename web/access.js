'use strict';
window.CodePierAccess = (() => {
  function projectFields(projects, all = false, selected = []) {
    return `<div class="field"><label>允许访问的项目</label><label class="check"><input type="checkbox" name="all_projects" ${all ? 'checked' : ''}>全部现有及未来新增项目</label><small>开启后，新建项目会自动加入此连接。</small><div class="check-list" data-project-choices>${projects.map((p) => `<label class="check"><input type="checkbox" name="project" value="${esc(p.id)}" ${selected.includes(p.id) ? 'checked' : ''} ${all ? 'disabled' : ''}><span>${esc(p.alias)} <small> · ${esc(p.device_name || '')}</small></span></label>`).join('') || '<small>暂无项目。</small>'}</div></div>`;
  }
  function bindProjects(dialog) {
    const all = $('[name="all_projects"]', dialog);
    if (!all) return;
    const sync = () =>
      $$('[name="project"]', dialog).forEach((field) => {
        field.disabled = all.checked;
      });
    all.addEventListener('change', sync);
    sync();
  }
  function selection(dialog = document.querySelector('.modal')) {
    const all = $('[name="all_projects"]', dialog)?.checked === true;
    return {
      all_projects: all,
      projects: all ? [] : $$('[name="project"]:checked', dialog).map((field) => field.value),
    };
  }
  async function editGrant(id) {
    const login = S.session,
      space = S.space_id,
      page = S.page,
      intent = S.modalIntent || 0;
    const endpoint = '/api/grants/' + encodeURIComponent(id);
    const entry = S.grants.find((g) => g.id === id);
    if (!entry || entry.revoked) return;
    const [grant] = await Promise.all([api(endpoint + '/projects'), loadBasics()]);
    if (
      S.session !== login ||
      S.space_id !== space ||
      S.page !== page ||
      (S.modalIntent || 0) !== intent
    )
      return;
    if (grant.revoked) throw new Error('连接已撤销，请刷新列表。');
    const editableProjects =
      CodePierIdentity.admin() && !grant.profile_id && entry.status !== 'expired';
    const needsReactivation = ['expired', 'pending'].includes(entry.status);
    const date = new Date(
      (entry.authorization_expires || entry.expires || Date.now() / 1000 + 30 * 86400) * 1000,
    );
    const localDate = new Date(date.getTime() - date.getTimezoneOffset() * 60000)
      .toISOString()
      .slice(0, 16);
    const dialog = modal(
      '编辑连接 · ' + entry.label,
      `<form id="grant-edit-form">
        <div class="field"><label for="grant-expiry-date">到期时间（本地时间）</label><input id="grant-expiry-date" name="expires" type="datetime-local" value="${esc(localDate)}" required></div>
        <label class="check"><input type="checkbox" name="unlimited" ${entry.unlimited ? 'checked' : ''}>无限期</label>
        <label class="check" ${needsReactivation ? '' : 'hidden'}><input type="checkbox" name="confirm_reactivate">确认恢复仍可用的凭据</label>
        ${editableProjects ? projectFields(S.projects, grant.projects.includes('*'), grant.projects) : ''}
        <details class="form-advanced" id="grant-edit-preview"><summary>当前实际权限</summary><div role="status"></div></details>
        <p id="grant-edit-status" class="form-note" role="status"></p>
      </form>`,
      '<button class="btn ghost" data-action="close-modal">关闭</button><button class="btn primary" type="submit" form="grant-edit-form">保存</button>',
      true,
    );
    const form = $('#grant-edit-form', dialog),
      status = $('#grant-edit-status', dialog);
    const preview = $('#grant-edit-preview', dialog),
      result = $('[role="status"]', preview);
    const current = () =>
      dialog.isConnected && S.session === login && S.space_id === space && S.page === page;
    const fields = form.elements;
    let baselineDate = fields.expires.value,
      baselineUnlimited = fields.unlimited.checked,
      expiryRevision = entry.expiry_revision;
    const expiryDisabled = () => {
      fields.expires.disabled = fields.unlimited.checked;
    };
    fields.unlimited.onchange = expiryDisabled;
    expiryDisabled();
    bindProjects(dialog);
    let previewEpoch = 0;
    async function loadPreview() {
      if (!preview.open || !current()) return;
      const epoch = ++previewEpoch;
      result.textContent = '正在检查…';
      try {
        const data = await api(endpoint + '/access-preview');
        if (current() && epoch === previewEpoch)
          result.innerHTML = CodePierProfiles.previewHTML(data);
      } catch (error) {
        if (current() && epoch === previewEpoch) result.textContent = error.message;
      }
    }
    preview.ontoggle = loadPreview;
    form.onsubmit = (event) => {
      event.preventDefault();
      if (!current() || !form.reportValidity()) return;
      return busy($('button[type="submit"]', dialog), async () => {
        const unlimited = fields.unlimited.checked,
          dateValue = fields.expires.value;
        const expiryChanged =
          unlimited !== baselineUnlimited || (!unlimited && dateValue !== baselineDate);
        const expires = unlimited ? null : new Date(dateValue).getTime() / 1000;
        if (
          expiryChanged &&
          expires !== null &&
          (!Number.isFinite(expires) || expires <= Date.now() / 1000)
        ) {
          status.textContent = '请选择未来的到期时间。';
          return;
        }
        const selected = selection(dialog);
        const wanted = selected.all_projects ? ['*'] : selected.projects;
        const projectsChanged =
          editableProjects &&
          (wanted.length !== grant.projects.length ||
            wanted.some((id) => !grant.projects.includes(id)));
        const controls = $$('input', form);
        const disabled = controls.map((field) => field.disabled);
        controls.forEach((field) => {
          field.disabled = true;
        });
        let saved = '';
        status.textContent = '正在保存…';
        try {
          if (projectsChanged) {
            const result = await api(endpoint + '/projects', {
              method: 'PUT',
              body: JSON.stringify({
                ...selected,
                expected_projects: grant.projects,
                expected_revision: grant.project_revision,
              }),
            });
            if (!current()) return;
            grant.projects = result.projects;
            grant.project_revision = result.project_revision;
            saved = '项目范围已保存。';
          }
          if (expiryChanged) {
            const result = await api(endpoint + '/expiration', {
              method: 'PUT',
              body: JSON.stringify({
                expires,
                expected_revision: expiryRevision,
                confirm_reactivate: fields.confirm_reactivate.checked,
              }),
            });
            if (!current()) return;
            expiryRevision = result.expiry_revision;
            baselineDate = dateValue;
            baselineUnlimited = unlimited;
            saved = result.reconnect_required ? '期限已保存，请重新连接。' : '期限已保存。';
          }
          closeModal(dialog);
          toast(saved || '连接设置已保存');
          await renderPage(false);
        } catch (error) {
          if (current()) {
            status.textContent =
              saved +
              (error.code === 'NETWORK_UNCERTAIN'
                ? '网络中断，保存结果待确认。请关闭后刷新连接列表。'
                : error.message);
            loadPreview();
          }
        } finally {
          if (current())
            controls.forEach((field, index) => {
              field.disabled = disabled[index];
            });
        }
      });
    };
  }
  return { projectFields, bindProjects, selection, editGrant };
})();
