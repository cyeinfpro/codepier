'use strict';
(function initCodePierAppearance(window, document) {
  const KEY = 'codepier-appearance';
  const LEGACY_KEY = 'codepier-chat-appearance';
  const VALUES = new Set(['auto', 'light', 'dark']);
  const media =
    typeof window.matchMedia === 'function'
      ? window.matchMedia('(prefers-color-scheme: dark)')
      : null;
  let preference = 'auto';
  let scheme = 'light';

  function read(key) {
    try {
      return window.localStorage.getItem(key);
    } catch {
      return null;
    }
  }
  function write(key, value) {
    try {
      window.localStorage.setItem(key, value);
      return true;
    } catch {
      return false;
    }
  }
  function valid(value) {
    return VALUES.has(value);
  }
  function resolve(value) {
    return value === 'auto' && media?.matches ? 'dark' : value === 'dark' ? 'dark' : 'light';
  }
  function detail() {
    return { preference, scheme };
  }
  function syncControls() {
    document.querySelectorAll('[data-ui-appearance],#chat-appearance').forEach((control) => {
      if ('value' in control && control.value !== preference) control.value = preference;
      if (control.matches('[data-ui-appearance]')) control.setAttribute('aria-label', '全站外观');
    });
  }
  function syncThemeColor() {
    let meta = document.querySelector('meta[name="theme-color"]');
    if (!meta && document.head) {
      meta = document.createElement('meta');
      meta.name = 'theme-color';
      document.head.append(meta);
    }
    if (meta) meta.content = scheme === 'dark' ? '#191919' : '#f4f4f4';
  }
  function apply(emit) {
    scheme = resolve(preference);
    document.documentElement.dataset.appearance = scheme;
    document.documentElement.style.colorScheme = scheme;
    syncThemeColor();
    syncControls();
    if (emit) window.dispatchEvent(new CustomEvent('codepier:appearance', { detail: detail() }));
  }
  function setPreference(value) {
    if (!valid(value)) throw new TypeError('外观偏好必须是 auto、light 或 dark');
    preference = value;
    write(KEY, value);
    apply(true);
    return detail();
  }
  function subscribe(callback) {
    if (typeof callback !== 'function') throw new TypeError('外观订阅者必须是函数');
    const listener = (event) => callback(event.detail);
    window.addEventListener('codepier:appearance', listener);
    callback(detail());
    return () => window.removeEventListener('codepier:appearance', listener);
  }
  function systemChanged() {
    if (preference === 'auto') apply(true);
  }
  function storageChanged(event) {
    let storage = null;
    try {
      storage = window.localStorage;
    } catch {}
    if (event.storageArea && storage && event.storageArea !== storage) return;
    let next = null;
    if (event.key === null) next = 'auto';
    else if (event.key === KEY) next = valid(event.newValue) ? event.newValue : 'auto';
    else if (event.key === LEGACY_KEY && !read(KEY))
      next = valid(event.newValue) ? event.newValue : 'auto';
    if (next === null) return;
    preference = next;
    apply(true);
  }

  const saved = read(KEY);
  const legacy = read(LEGACY_KEY);
  preference = valid(saved) ? saved : valid(legacy) ? legacy : 'auto';
  if (!valid(saved) && valid(legacy)) write(KEY, legacy);

  window.CodePierAppearance = Object.freeze({
    getPreference: () => preference,
    getScheme: () => scheme,
    setPreference,
    subscribe,
  });

  document.addEventListener('change', (event) => {
    const control = event.target.closest?.('[data-ui-appearance]');
    if (control && valid(control.value)) setPreference(control.value);
  });
  window.addEventListener('storage', storageChanged);
  if (media) {
    if (typeof media.addEventListener === 'function')
      media.addEventListener('change', systemChanged);
    else if (typeof media.addListener === 'function') media.addListener(systemChanged);
  }
  // Static first paint is synced once; dynamic controls are rendered with the
  // current preference. Streaming transcripts never trigger document scans.
  document.addEventListener('DOMContentLoaded', syncControls, { once: true });
  apply(false);
})(window, document);
