'use strict';
// WebCrypto is preferred, with a locally pinned UMD SHA256 implementation for
// ordinary HTTP+IP+port deployments. No upload leaves the configured CodePier Hub.
async function nativeSha256(bytes) {
  if (globalThis.crypto?.subtle) {
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
    return Array.from(digest, x => x.toString(16).padStart(2, '0')).join('');
  }
  if (typeof globalThis.sha256 !== 'function') {
    throw new Error('附件校验组件未加载，请刷新面板后重试');
  }
  return globalThis.sha256(bytes);
}
