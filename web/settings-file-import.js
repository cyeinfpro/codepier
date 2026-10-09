'use strict';
// Instance-only typed preview/confirm flow. No arbitrary key patch or secret fields.
window.CodePierFileImportSettings = (() => {
  const labels = { streaming_enabled: 'Hub 字节入站', native_relay_enabled: '原生附件转发',
    native_file_hosts: '精确来源主机', native_file_providers: '命名来源 Provider' };
  const sources = { database: '面板显式设置', environment: '部署环境', default: '版本默认' };
  let data = null, draft = {}, preview = null, busy = false, owner = '', generation = 0, pendingToken = 0, root = null;
  const copy = (v) => JSON.parse(JSON.stringify(v));
  const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const ownership = () => (S.session?.user_id || S.session?.username || '') + ':' + S.space_id;
  const text = (v) => v === null ? '继承' : v === true ? '开启' : v === false ? '关闭' : Array.isArray(v) ? v.join('、') || '空列表（明确禁止 / 未启用）' : '未知';
  function detach() { generation++; root = null; }
  function reset() { pendingToken++; detach(); data = null; draft = {}; preview = null; busy = false; owner = ''; }
  function discard() { if (data) draft = Object.fromEntries(Object.entries(data.settings).map(([k,v]) => [k,copy(v.configured_value)])); preview = null; }
  function dirty() { return busy || !!data && Object.keys(draft).some((k) => !equal(draft[k], data.settings[k].configured_value)); }
  function card(key) {
    const item = data.settings[key], value = draft[key];
    const boolean = key.endsWith('_enabled');
    let input;
    if (boolean) {
      input = '<select name="' + key + '">' + [['inherit','继承上级配置'],['true','明确开启'],['false','明确关闭']].map(([id,label]) =>
        '<option value="' + id + '" ' + ((value === null ? 'inherit' : String(value)) === id ? 'selected' : '') + '>' + label + '</option>').join('') + '</select>';
    } else {
      input = '<select name="' + key + '_mode"><option value="inherit" ' + (value === null ? 'selected' : '') + '>继承上级配置</option><option value="explicit" ' +
        (value !== null ? 'selected' : '') + '>使用下面的明确列表</option></select>';
      input += key === 'native_file_hosts' ? '<textarea name="native_file_hosts" rows="3" placeholder="每行一个精确主机名，不接受 URL、IP 或通配符" ' + (value === null ? 'disabled' : '') +
        '>' + esc((value || []).join('\n')) + '</textarea>' :
        '<div class="check-list">' + data.provider_options.map((p) => '<label class="check"><input type="checkbox" name="native_file_providers" value="' + esc(p) + '" ' +
          (value?.includes(p) ? 'checked' : '') + (value === null ? ' disabled' : '') + '>' + esc(p) + '</label>').join('') + '</div>';
    }
    return '<div class="settings-import-field"><label class="field"><strong>' + esc(labels[key]) + '</strong>' + (boolean ? input : '') + '</label>' + (!boolean ? input : '') +
      '<p class="form-note">当前有效值：' + esc(text(item.effective_value)) + ' · ' + esc(sources[item.source] || item.source) + (item.state === 'invalid' ? ' · 配置无效，已限制' : '') +
      '<br>继承值：' + esc(text(item.inherited_value)) + ' · ' + esc(sources[item.inherited_source] || item.inherited_source) + '</p></div>';
  }
  function fieldsHTML() {
    return '<form id="file-import-settings-form"><div class="settings-import-fields">' + Object.keys(labels).map(card).join('') + '</div>' +
      '<p class="form-note">“继承”和空列表不同：明确空主机列表拒绝所有主机；空 Provider 列表不启用任何命名来源。这里不增加本机读取目录或节点权限。</p>' +
      '<div class="actions"><button type="submit" class="btn primary">预览修改</button><button type="button" class="btn ghost" data-file-import-cancel>取消改动</button></div>' +
      '<p id="file-import-status" class="form-note" role="status"></p><div id="file-import-preview"></div></form>';
  }
  async function html() {
    const identity = ownership(), session = S.session, space = S.space_id;
    if (owner !== identity) { reset(); owner = identity; }
    const ticket = ++generation;
    try {
      const latest = await api('/api/settings/file-import');
      if (ticket !== generation || identity !== ownership() || session !== S.session || space !== S.space_id) throw sessionChanged();
      if (!dirty()) { data = latest; discard(); }
      return '<section id="settings-file-import-editor" class="panel"><div class="panel-head"><h2>文件入站设置</h2><span class="settings-mode">实例管理员 · 预览后确认</span></div><div class="panel-body">' +
        '<p>新调用的准入检查立即采用新设置。已经获准进行最后发布的上传，可能在下一次检查前完成；开始下载的文件仍沿用原逐跳来源策略。项目、连接和节点权限继续生效。</p>' +
        fieldsHTML() + '</div></section>';
    } catch (error) {
      if (ticket !== generation || identity !== ownership()) throw error;
      return '<section id="settings-file-import-editor" class="panel"><div class="panel-body"><h2>文件入站设置</h2><p role="status">' +
        esc(error.message) + '</p><button type="button" class="btn" data-action="refresh">重新读取设置</button></div></section>';
    }
  }
  function bind() {
    detach();
    root = $('#settings-file-import-editor');
    const mounted = root;
    if (!mounted) return;
    const form = $('#file-import-settings-form', mounted);
    if (!form) return;
    const ticket = generation, identity = owner, session = S.session, space = S.space_id;
    const current = () => root === mounted && mounted.isConnected && generation === ticket && identity === ownership() && S.session === session && S.space_id === space && S.page === 'settings';
    const status = (message) => { if (current()) $('#file-import-status', mounted).textContent = message; };
    const read = () => {
      for (const key of Object.keys(labels)) {
        if (key.endsWith('_enabled')) {
          const value = form.elements[key].value;
          draft[key] = value === 'inherit' ? null : value === 'true';
        } else {
          const inherit = form.elements[key + '_mode'].value === 'inherit';
          if (key === 'native_file_hosts') {
            form.elements[key].disabled = inherit;
            draft[key] = inherit ? null : [...new Set(form.elements[key].value.split(/[\s,]+/).filter(Boolean))];
          } else {
            $$('[name="native_file_providers"]', form).forEach((control) => { control.disabled = inherit; });
            draft[key] = inherit ? null : $$('[name="native_file_providers"]:checked', form).map((control) => control.value);
          }
        }
      }
      preview = null;
      $('#file-import-preview', form).replaceChildren();
    };
    form.addEventListener('change', read);
    form.addEventListener('input', read);
    $('[data-file-import-cancel]', form).onclick = async () => {
      if (busy) return;
      discard();
      await renderPage(false);
    };
    function disabled(value) {
      $$('input,select,textarea,button', form).forEach((control) => { control.disabled = value; });
      if (!value) {
        for (const key of ['native_file_hosts', 'native_file_providers'])
          $$('[name="' + key + '"]', form).forEach((control) => { control.disabled = draft[key] === null; });
      }
    }
    form.onsubmit = async (event) => {
      event.preventDefault();
      if (busy || !form.reportValidity()) return;
      read();
      const patch = Object.fromEntries(Object.entries(draft).filter(([key,value]) => !equal(value, data.settings[key].configured_value)));
      if (!Object.keys(patch).length) { status('没有需要修改的显式配置。'); return; }
      const operationToken = ++pendingToken;
      busy = true; disabled(true); status('正在验证并预览，尚未保存…');
      try {
        const result = await api('/api/settings/file-import/preview', {
          method: 'POST', body: JSON.stringify({ expected_revision: data.revision, patch }),
        });
        if (!current()) return;
        preview = { result, patch: copy(patch), expected_revision: data.revision };
        $('#file-import-preview', form).innerHTML = '<section class="settings-import-review"><h3>确认这次实例修改</h3><ul>' +
          result.changes.map((change) => '<li><strong>' + esc(labels[change.key] || change.key) + '</strong>：' + esc(text(change.before.effective_value)) + ' → ' +
            esc(text(change.after.effective_value)) + '；来源 ' + esc(sources[change.after.source] || change.after.source) +
            (change.reset_to_inherit ? '（重置为继承）' : '') + '</li>').join('') +
          '</ul><p class="form-note">保存影响整个实例。节点限制不会放宽；已获准的最后发布及原下载策略窗口见上方说明。</p>' +
          '<button type="button" class="btn primary" id="file-import-confirm">确认并保存以上修改</button></section>';
        $('#file-import-confirm', form).onclick = commit;
        status('预览已就绪，尚未保存。修改任何字段会使本次预览失效。');
      } catch (error) { status(error.message + '；草稿已保留。请取消改动后重新读取再核对。'); }
      finally { if (operationToken === pendingToken) busy = false; if (current()) disabled(false); }
    };
    async function commit() {
      if (busy || !preview) return;
      const accepted = preview;
      const operationToken = ++pendingToken;
      busy = true; disabled(true); status('正在保存并重新读取有效值…');
      try {
        await api('/api/settings/file-import', { method: 'PUT',
          body: JSON.stringify({ expected_revision: accepted.expected_revision, patch: accepted.patch,
            confirmation: accepted.result.confirmation }) });
        if (!current()) return;
        const fresh = await api('/api/settings/file-import');
        if (!current()) return;
        for (const key of Object.keys(accepted.patch)) {
          if (!equal(fresh.settings[key].configured_value, accepted.result.snapshot.settings[key].configured_value))
            throw new Error('重新读取发现设置已变化，请核对；草稿仍保留');
        }
        data = fresh; discard(); busy = false;
        toast('文件入站设置已保存并重新核对');
        await renderPage(false);
      } catch (error) { status(error.message + '。未自动重试，草稿与原预览仍保留。'); }
      finally { if (operationToken === pendingToken) busy = false; if (current()) disabled(false); }
    }
  }
  return { html, bind, detach, reset, discard, dirty, pending: () => busy };
})();
