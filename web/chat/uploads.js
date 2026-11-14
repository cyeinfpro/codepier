function chatFileLocked(view, file) {
  return !!view.busy || !!view.pending?.attachments?.includes(file?.file);
}
function chatFiles() {
  const holder = $('#chat-files');
  if (!holder) return;
  holder.replaceChildren();
  for (const f of chatView().files) {
    const chip = document.createElement('span');
    chip.className = 'chat-file';
    if (f.preview) {
      const image = document.createElement('img');
      image.src = f.preview;
      image.alt = f.name;
      chip.append(image);
    }
    const text = document.createElement('span');
    text.textContent = f.name;
    const state = document.createElement('small');
    state.textContent = f.error ? '上传失败' : f.ready ? '已校验' : (f.progress || 0) + '%';
    state.title = f.errorMessage || '';
    const remove = document.createElement('button');
    remove.type = 'button';
    remove.textContent = '×';
    remove.setAttribute('aria-label', '移除附件 ' + f.name);
    remove.disabled = chatFileLocked(chatView(), f);
    remove.title = remove.disabled ? '正在确认消息中的附件，暂不能移除' : '移除附件';
    remove.onclick = () => {
      if (chatFileLocked(chatView(), f)) return;
      f.removed = true;
      const v = chatView();
      v.files = v.files.filter((x) => x !== f);
      if (f.preview) URL.revokeObjectURL(f.preview);
      chatFiles();
    };
    chip.append(text, state);
    if (f.error && f.source) {
      const retry = document.createElement('button');
      retry.type = 'button';
      retry.textContent = '重试';
      retry.onclick = () => chatUploadEntry(f);
      chip.append(retry);
    }
    chip.append(remove);
    holder.append(chip);
  }
  chatSyncChrome();
}
function chatImageBytes(files) {
  return files.reduce((total, entry) => {
    const file = entry.source || entry;
    return (
      total +
      (/\.(png|jpe?g|webp|gif)$/i.test(entry.name || file.name || '')
        ? Number(file.size || entry.size || 0)
        : 0)
    );
  }, 0);
}
async function chatUpload(files) {
  const v = chatView(),
    project = ChatUI.project,
    identity = S.session;
  if (!project) {
    chatStatus('请先选择项目');
    return;
  }
  if (v.files.length + files.length > CHAT_ATTACHMENT_MAX_COUNT) {
    chatStatus('每条消息最多 10 个附件', true);
    return;
  }
  if (chatImageBytes([...v.files, ...files]) > CHAT_ATTACHMENT_MAX_BYTES) {
    chatStatus('每条消息的图片合计不能超过 20 MiB', true);
    return;
  }
  const entries = files.map((file) => {
    const entry = { name: file.name, ready: false, source: file, progress: 0 };
    if (file.type.startsWith('image/') && file.type !== 'image/svg+xml')
      entry.preview = URL.createObjectURL(file);
    return entry;
  });
  v.files.push(...entries);
  chatFiles();
  for (const entry of entries) {
    if (S.session !== identity) break;
    if (!entry.removed) await chatUploadEntry(entry, v, project, identity);
  }
}
async function chatUploadEntry(
  entry,
  v = chatView(),
  project = ChatUI.project,
  identity = S.session,
) {
  const file = entry.source;
  if (entry.uploading || entry.removed) return;
  const visible = () =>
    S.session === identity && S.page === 'native' && $('#chat-root') && chatView() === v;
  const mapping = () => {
    const p = S.projects.find((p) => p.id === project);
    return JSON.stringify([project, p?.root, p?.device_id]);
  };
  entry.mapping ??= mapping();
  const valid = () => {
    if (S.session !== identity || entry.removed) return false;
    if (entry.mapping !== mapping())
      throw new Error('项目已改变，上传已停止。请核对工作项目后移除并重新添加附件。');
    return true;
  };
  entry.uploading = true;
  entry.error = false;
  entry.errorMessage = '';
  if (visible()) chatFiles();
  try {
    if (!file.size || file.size > CHAT_ATTACHMENT_MAX_BYTES)
      throw new Error('每个附件须为 1 字节–20 MiB');
    const raw = new Uint8Array(await file.arrayBuffer()),
      sha256 = await nativeSha256(raw);
    if (!valid()) return;
    const catalog = await chatAPI('upload_list', {}, project);
    if (!valid()) return;
    const existing = (catalog.files || []).find(
        (x) => x.name === file.name && x.size === raw.length && x.sha256 === sha256,
      ),
      id = existing?.file || entry.file || uid();
    entry.file = id;
    let reply = await chatAPI(
      'upload_begin',
      { file: id, name: file.name, size: raw.length, sha256 },
      project,
    );
    if (!Number.isInteger(reply.received) || reply.received < 0 || reply.received > raw.length)
      throw new Error('附件进度无效，请重试。');
    while (reply.received < raw.length) {
      if (!valid()) return;
      const offset = reply.received,
        part = raw.slice(offset, offset + CHAT_UPLOAD_CHUNK_BYTES);
      let binary = '';
      for (const byte of part) binary += String.fromCharCode(byte);
      const payload = { file: id, offset, data: btoa(binary), sha256: await nativeSha256(part) };
      let error;
      for (let attempt = 0; attempt < 3; attempt++) {
        if (!valid()) return;
        try {
          reply = await chatAPI('upload_chunk', payload, project);
          error = null;
          break;
        } catch (e) {
          error = e;
          if (attempt < 2) await new Promise((r) => setTimeout(r, 300 * (attempt + 1)));
        }
      }
      if (error) throw error;
      if (
        !Number.isInteger(reply.received) ||
        reply.received <= offset ||
        reply.received > raw.length
      )
        throw new Error('附件进度未得到有效确认，请重试原附件。');
      entry.progress = Math.round((reply.received / raw.length) * 100);
      if (visible()) chatFiles();
    }
    if (!valid()) return;
    const finished = await chatAPI('upload_finish', { file: id }, project);
    if (!finished.ready) throw new Error('附件尚未完成校验');
    if (valid()) Object.assign(entry, { ready: true, progress: 100 });
  } catch (e) {
    entry.error = true;
    entry.errorMessage = e.message;
    if (visible()) chatStatus(e.message, true);
  } finally {
    entry.uploading = false;
    if (visible()) chatFiles();
  }
}
