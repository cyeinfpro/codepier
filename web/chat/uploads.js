const CHAT_HASH_SLICE_BYTES = 512 * 1024;
const CHAT_BASE64_SUBCHUNK_BYTES = 8 * 1024;
const CHAT_SHA256_K = new Int32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function chatSha256Block(H, W, block, off) {
  for (let i = 0; i < 16; i++) {
    const j = off + i * 4;
    W[i] =
      (block[j] << 24) |
      (block[j + 1] << 16) |
      (block[j + 2] << 8) |
      block[j + 3] |
      0;
  }
  for (let i = 16; i < 64; i++) {
    const x = W[i - 15],
      y = W[i - 2];
    const s0 =
      (((x >>> 7) | (x << 25)) ^ ((x >>> 18) | (x << 14)) ^ (x >>> 3)) | 0;
    const s1 =
      (((y >>> 17) | (y << 15)) ^ ((y >>> 19) | (y << 13)) ^ (y >>> 10)) | 0;
    W[i] = (W[i - 16] + s0 + W[i - 7] + s1) | 0;
  }
  let [a, b, c, d, e, f, g, h] = H;
  for (let i = 0; i < 64; i++) {
    const s1 =
      (((e >>> 6) | (e << 26)) ^
        ((e >>> 11) | (e << 21)) ^
        ((e >>> 25) | (e << 7))) |
      0;
    const ch = ((e & f) ^ (~e & g)) | 0;
    const t1 = (h + s1 + ch + CHAT_SHA256_K[i] + W[i]) | 0;
    const s0 =
      (((a >>> 2) | (a << 30)) ^
        ((a >>> 13) | (a << 19)) ^
        ((a >>> 22) | (a << 10))) |
      0;
    const maj = ((a & b) ^ (a & c) ^ (b & c)) | 0;
    const t2 = (s0 + maj) | 0;
    h = g;
    g = f;
    f = e;
    e = (d + t1) | 0;
    d = c;
    c = b;
    b = a;
    a = (t1 + t2) | 0;
  }
  H[0] = (H[0] + a) | 0;
  H[1] = (H[1] + b) | 0;
  H[2] = (H[2] + c) | 0;
  H[3] = (H[3] + d) | 0;
  H[4] = (H[4] + e) | 0;
  H[5] = (H[5] + f) | 0;
  H[6] = (H[6] + g) | 0;
  H[7] = (H[7] + h) | 0;
}

function chatCreateSha256() {
  const H = new Int32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c,
    0x1f83d9ab, 0x5be0cd19,
  ]);
  const W = new Int32Array(64);
  const tail = new Uint8Array(64);
  let tailLen = 0,
    totalBytes = 0;
  return {
    update(chunk) {
      totalBytes += chunk.length;
      let pos = 0;
      if (tailLen > 0) {
        const take = Math.min(64 - tailLen, chunk.length);
        tail.set(chunk.subarray(0, take), tailLen);
        tailLen += take;
        pos += take;
        if (tailLen === 64) {
          chatSha256Block(H, W, tail, 0);
          tailLen = 0;
        }
      }
      while (pos + 64 <= chunk.length) {
        chatSha256Block(H, W, chunk, pos);
        pos += 64;
      }
      if (pos < chunk.length) {
        tailLen = chunk.length - pos;
        tail.set(chunk.subarray(pos), 0);
      }
    },
    digest() {
      tail[tailLen++] = 0x80;
      if (tailLen > 56) {
        tail.fill(0, tailLen, 64);
        chatSha256Block(H, W, tail, 0);
        tailLen = 0;
      }
      tail.fill(0, tailLen, 56);
      const view = new DataView(tail.buffer);
      view.setUint32(
        56,
        Math.floor((totalBytes * 8) / 0x100000000) >>> 0,
        false,
      );
      view.setUint32(60, (totalBytes * 8) >>> 0, false);
      chatSha256Block(H, W, tail, 0);
      return Array.from(H, (v) => (v >>> 0).toString(16).padStart(8, "0")).join(
        "",
      );
    },
  };
}

function chatBytesToBase64(bytes) {
  const parts = [];
  for (let i = 0; i < bytes.length; i += CHAT_BASE64_SUBCHUNK_BYTES) {
    parts.push(
      String.fromCharCode.apply(
        null,
        bytes.subarray(i, i + CHAT_BASE64_SUBCHUNK_BYTES),
      ),
    );
  }
  return btoa(parts.join(""));
}

async function chatStreamFileSha256(file, valid) {
  const totalSize = Number(file.size || 0);
  if (totalSize <= CHAT_HASH_SLICE_BYTES) {
    const slice = new Uint8Array(await file.slice(0, totalSize).arrayBuffer());
    if (!valid()) return null;
    return nativeSha256(slice);
  }
  const hasher = chatCreateSha256();
  for (let offset = 0; offset < totalSize; offset += CHAT_HASH_SLICE_BYTES) {
    if (!valid()) return null;
    const end = Math.min(totalSize, offset + CHAT_HASH_SLICE_BYTES);
    const chunk = new Uint8Array(await file.slice(offset, end).arrayBuffer());
    hasher.update(chunk);
  }
  return hasher.digest();
}

function chatFileLocked(view, file) {
  return !!view.busy || !!view.pending?.attachments?.includes(file?.file);
}
function chatFiles() {
  const holder = $("#chat-files");
  if (!holder) return;
  holder.replaceChildren();
  for (const f of chatView().files) {
    const chip = document.createElement("span");
    chip.className = "chat-file" + (f.error ? " is-error" : "");
    if (f.preview) {
      const image = document.createElement("img");
      image.src = f.preview;
      image.alt = f.name;
      chip.append(image);
    }
    const text = document.createElement("span");
    text.textContent = f.name;
    const state = document.createElement("small");
    state.textContent = f.error
      ? "上传失败"
      : f.ready
        ? "已校验"
        : (f.progress || 0) + "%";
    state.title = f.errorMessage || "";
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "×";
    remove.setAttribute("aria-label", "移除附件 " + f.name);
    remove.disabled = chatFileLocked(chatView(), f);
    remove.title = remove.disabled
      ? "正在确认消息中的附件，暂不能移除"
      : "移除附件";
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
      const retry = document.createElement("button");
      retry.type = "button";
      retry.textContent = "重试";
      retry.onclick = () => chatUploadEntry(f);
      chip.append(retry);
    }
    chip.append(remove);
    if (f.errorMessage) {
      const errLine = document.createElement("small");
      errLine.className = "chat-file-error";
      errLine.setAttribute("role", "alert");
      errLine.textContent = f.errorMessage;
      errLine.title = f.errorMessage;
      chip.append(errLine);
    }
    holder.append(chip);
  }
  chatSyncChrome();
}
function chatImageBytes(files) {
  return files.reduce((total, entry) => {
    const file = entry.source || entry;
    return (
      total +
      (/\.(png|jpe?g|webp|gif)$/i.test(entry.name || file.name || "")
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
    chatStatus("请先选择项目");
    return;
  }
  if (v.files.length + files.length > CHAT_ATTACHMENT_MAX_COUNT) {
    chatStatus("每条消息最多 10 个附件", true);
    return;
  }
  if (chatImageBytes([...v.files, ...files]) > CHAT_ATTACHMENT_MAX_BYTES) {
    chatStatus("每条消息的图片合计不能超过 20 MiB", true);
    return;
  }
  const entries = files.map((file) => {
    const entry = { name: file.name, ready: false, source: file, progress: 0 };
    if (file.type.startsWith("image/") && file.type !== "image/svg+xml")
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
async function chatUploadChunkWithRetry(payload, project, valid) {
  let error;
  for (let attempt = 0; attempt < 3; attempt++) {
    if (!valid()) return null;
    try {
      return await chatAPI("upload_chunk", payload, project);
    } catch (e) {
      error = e;
      if (attempt < 2)
        await new Promise((r) => setTimeout(r, 300 * (attempt + 1)));
    }
  }
  throw error;
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
    S.session === identity &&
    S.page === "native" &&
    $("#chat-root") &&
    chatView() === v;
  const mapping = () => {
    const p = S.projects.find((p) => p.id === project);
    return JSON.stringify([project, p?.root, p?.device_id]);
  };
  entry.mapping ??= mapping();
  const valid = () => {
    if (S.session !== identity || entry.removed) return false;
    if (entry.mapping !== mapping())
      throw new Error(
        "项目已改变，上传已停止。请核对工作项目后移除并重新添加附件。",
      );
    return true;
  };
  entry.uploading = true;
  entry.error = false;
  entry.errorMessage = "";
  if (visible()) chatFiles();
  try {
    const totalSize = Number(file.size || 0);
    if (!totalSize || totalSize > CHAT_ATTACHMENT_MAX_BYTES)
      throw new Error("每个附件须为 1 字节–20 MiB");
    const sha256 = await chatStreamFileSha256(file, valid);
    if (!sha256 || !valid()) return;
    const catalog = await chatAPI("upload_list", {}, project);
    if (!valid()) return;
    const existing = (catalog.files || []).find(
        (x) =>
          x.name === file.name && x.size === totalSize && x.sha256 === sha256,
      ),
      id = existing?.file || entry.file || uid();
    entry.file = id;
    let reply = await chatAPI(
      "upload_begin",
      { file: id, name: file.name, size: totalSize, sha256 },
      project,
    );
    if (
      !Number.isInteger(reply.received) ||
      reply.received < 0 ||
      reply.received > totalSize
    )
      throw new Error("附件进度无效，请重试。");
    while (reply.received < totalSize) {
      if (!valid()) return;
      const offset = reply.received,
        end = Math.min(totalSize, offset + CHAT_UPLOAD_CHUNK_BYTES),
        part = new Uint8Array(await file.slice(offset, end).arrayBuffer());
      const payload = {
        file: id,
        offset,
        data: chatBytesToBase64(part),
        sha256: await nativeSha256(part),
      };
      reply = await chatUploadChunkWithRetry(payload, project, valid);
      if (!reply) return;
      if (
        !Number.isInteger(reply.received) ||
        reply.received <= offset ||
        reply.received > totalSize
      )
        throw new Error("附件进度未得到有效确认，请重试原附件。");
      entry.progress = Math.round((reply.received / totalSize) * 100);
      if (visible()) chatFiles();
    }
    if (!valid()) return;
    const finished = await chatAPI("upload_finish", { file: id }, project);
    if (!finished.ready) throw new Error("附件尚未完成校验");
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
