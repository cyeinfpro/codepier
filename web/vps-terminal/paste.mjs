const encoder = new TextEncoder();

export function pasteSummary(text, maxBytes) {
  const bytes = encoder.encode(text).byteLength;
  return {
    bytes,
    lines: text.split(/\r\n|\r|\n/).length,
    tooLarge: bytes > maxBytes,
    preview: /[\r\n\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(text) || bytes > 4096,
  };
}

// Split by Unicode scalar boundaries. Each xterm.paste call completes its own
// bracketed-paste sequence, so cancellation never leaves a remote parser inside it.
export function* pasteChunks(text, maxBytes = 4096) {
  let chunk = '',
    bytes = 0;
  for (const char of text) {
    const size = encoder.encode(char).byteLength;
    if (bytes && bytes + size > maxBytes) {
      yield chunk;
      chunk = '';
      bytes = 0;
    }
    chunk += char;
    bytes += size;
  }
  if (chunk) yield chunk;
}

export async function pasteInChunks(
  text,
  {
    paste,
    writable,
    connected,
    signal,
    onProgress,
    maxChunkBytes = 4096,
    delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  },
) {
  const total = encoder.encode(text).byteLength;
  let sent = 0;
  for (const chunk of pasteChunks(text, maxChunkBytes)) {
    const required = encoder.encode(chunk).byteLength + 16;
    while (!writable(required)) {
      if (signal?.aborted) return { sent, total, cancelled: true };
      if (!connected()) throw new Error('连接已中断，剩余内容未发送');
      await delay(25);
    }
    if (signal?.aborted) return { sent, total, cancelled: true };
    if (!connected()) throw new Error('连接已中断，剩余内容未发送');
    const accepted = paste(chunk);
    if (accepted === false) throw new Error('粘贴已停止，剩余内容未发送');
    sent += encoder.encode(chunk).byteLength;
    onProgress?.(sent, total);
    await delay(0);
  }
  return { sent, total, cancelled: false };
}
