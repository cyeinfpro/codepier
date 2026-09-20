(() => {
  if (globalThis.__codepierDOMAgent) return;
  const documentId = crypto.randomUUID().replaceAll('-', '');
  let observation = null, elementsById = new Map(), counter = 0;
  const sensitiveName = /password|passwd|secret|api.?key|token|authorization|credit.?card|cc-number|one.?time/i;

  function fail(code, message) { const error = new Error(message); error.code = code; throw error; }
  function visible(element) {
    if (!(element instanceof Element) || !element.isConnected || element.closest('[hidden],[aria-hidden="true"],script,style,noscript,template')) return false;
    const style = getComputedStyle(element), rect = element.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
  }
  function inViewport(element) {
    const rect = element.getBoundingClientRect();
    return rect.bottom > 0 && rect.right > 0 && rect.top < innerHeight && rect.left < innerWidth;
  }
  function sensitive(element) {
    return ['password', 'hidden', 'file'].includes(element.type) || sensitiveName.test([element.name, element.id, element.autocomplete].join(' '));
  }
  function label(element) {
    return (element.getAttribute('aria-label') || element.labels?.[0]?.innerText || element.innerText || element.getAttribute('title') || element.getAttribute('placeholder') || '').trim().slice(0, 300);
  }
  function fingerprint(element) {
    return JSON.stringify([element.tagName, element.getAttribute('role'), element.type, label(element), element.disabled === true, element.readOnly === true,
      sensitive(element) ? '' : element.value ?? '', element.getAttribute('href'), element.getAttribute('formaction'), element.getAttribute('target')]);
  }
  function allowed(value, origins) {
    const url = new URL(value, location.href);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || !origins.includes(url.origin)) fail('BROWSER_ORIGIN_DENIED', '操作可能进入未经授权的网站');
    return url;
  }
  function snapshot() {
    elementsById = new Map();
    let text = '', visited = 0, truncated = false;
    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      if (++visited > 12000) { truncated = true; break; }
      const node = walker.currentNode;
      if (!visible(node.parentElement)) continue;
      const value = (node.nodeValue || '').replace(/\s+/g, ' ').trim();
      if (!value) continue;
      const remaining = 24000 - text.length;
      if (value.length + 1 > remaining) { text += value.slice(0, remaining); truncated = true; break; }
      text += value + '\n';
    }
    const nodes = document.querySelectorAll('a[href],button,input,textarea,select,[contenteditable="true"],[role="button"],[role="link"],[role="checkbox"],[role="tab"]');
    const elements = [];
    if (nodes.length > 4000) truncated = true;
    for (let index = 0; index < nodes.length && index < 4000; index++) {
      const element = nodes[index];
      if (!visible(element) || !inViewport(element) || sensitive(element)) continue;
      if (elements.length >= 200) { truncated = true; break; }
      const key = 'e' + (++counter);
      const row = {id: key, tag: element.tagName.toLowerCase(), role: element.getAttribute('role') || '', label: label(element), type: element.type || '', disabled: element.disabled === true};
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(element.tagName)) row.value = String(element.value || '').slice(0, 1000);
      elements.push(row);
      elementsById.set(key, {element, fingerprint: fingerprint(element)});
    }
    observation = {token: crypto.randomUUID().replaceAll('-', ''), url: location.href};
    return {url: location.href, title: document.title.slice(0, 300), text, elements, document_id: documentId, observation_token: observation.token, content_truncated: truncated};
  }
  function action(message) {
    if (message.document_id !== documentId || !observation || message.observation_token !== observation.token || observation.url !== location.href) {
      fail('BROWSER_STALE_OBSERVATION', '页面或观察已变化，请重新读取');
    }
    const operation = message.operation;
    if (!operation || !['click', 'fill', 'select', 'scroll', 'key', 'navigate'].includes(operation.action)) fail('BROWSER_ACTION_UNSUPPORTED', '动作不受支持');
    observation = null; // Consume before effects, including a page navigation that loses its reply.
    if (operation.action === 'navigate') { allowed(operation.value, message.allowed_origins); return {confirmed: true}; }
    if (operation.action === 'scroll' && !operation.element_id) {
      if (!Number.isInteger(operation.delta_y) || Math.abs(operation.delta_y) > 2000) fail('BROWSER_ACTION_UNSUPPORTED', '滚动距离超出限制');
      window.scrollBy({top: operation.delta_y, behavior: 'instant'}); return {confirmed: true};
    }
    const entry = elementsById.get(operation.element_id), element = entry?.element;
    if (!element || !visible(element) || !inViewport(element) || sensitive(element) || entry.fingerprint !== fingerprint(element)) {
      fail('BROWSER_ELEMENT_CHANGED', '目标元素已变化、不可见或属于敏感输入，请重新观察');
    }
    if (element.disabled || element.readOnly && ['fill', 'select'].includes(operation.action)) fail('BROWSER_ELEMENT_CHANGED', '目标不可编辑或已禁用');
    const rect = element.getBoundingClientRect();
    const x = Math.max(1, Math.min(innerWidth - 1, rect.left + rect.width / 2));
    const y = Math.max(1, Math.min(innerHeight - 1, rect.top + rect.height / 2));
    const hit = document.elementFromPoint(x, y);
    if (hit !== element && !element.contains(hit) && !hit?.contains(element)) fail('BROWSER_ELEMENT_CHANGED', '目标被其他内容覆盖，请重新观察');
    if (operation.action === 'click') {
      const link = element.closest('a[href]');
      if (link) {
        allowed(link.href, message.allowed_origins);
        if (link.target && link.target !== '_self') fail('BROWSER_ACTION_UNSUPPORTED', '不通过后台工具新建标签页或弹出窗口');
      }
      if (element.form && (element.type === 'submit' || element.tagName === 'BUTTON' && !element.type)) {
        allowed(element.formAction || element.form.action, message.allowed_origins);
        if (element.form.target && element.form.target !== '_self') fail('BROWSER_ACTION_UNSUPPORTED', '不操作会新建窗口的表单');
      }
      element.click();
    } else if (operation.action === 'fill') {
      if (typeof operation.value !== 'string' || operation.value.length > 10000) fail('BROWSER_ACTION_UNSUPPORTED', '输入长度无效');
      if (element.isContentEditable) { element.focus({preventScroll: true}); element.textContent = operation.value; }
      else if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement) {
        const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
        if (!setter) fail('BROWSER_ACTION_UNSUPPORTED', '输入控件不可写');
        setter.call(element, operation.value);
      } else fail('BROWSER_ACTION_UNSUPPORTED', '只允许向明确的输入控件填写');
      element.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: operation.value}));
      element.dispatchEvent(new Event('change', {bubbles: true}));
    } else if (operation.action === 'select') {
      if (!(element instanceof HTMLSelectElement) || element.multiple || ![...element.options].some(option => option.value === operation.value && !option.disabled)) {
        fail('BROWSER_ACTION_UNSUPPORTED', '选择值不属于此下拉框的可用选项');
      }
      element.value = operation.value;
      element.dispatchEvent(new Event('input', {bubbles: true}));
      element.dispatchEvent(new Event('change', {bubbles: true}));
    } else if (operation.action === 'key') {
      if (!['Enter', 'Escape', 'Tab', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Backspace', 'Delete', 'Home', 'End'].includes(operation.value)) {
        fail('BROWSER_ACTION_UNSUPPORTED', '按键不在允许范围');
      }
      element.focus({preventScroll: true});
      element.dispatchEvent(new KeyboardEvent('keydown', {key: operation.value, bubbles: true, cancelable: true}));
      element.dispatchEvent(new KeyboardEvent('keyup', {key: operation.value, bubbles: true}));
    } else if (operation.action === 'scroll') {
      if (!Number.isInteger(operation.delta_y) || Math.abs(operation.delta_y) > 2000) fail('BROWSER_ACTION_UNSUPPORTED', '滚动距离超出限制');
      element.scrollBy({top: operation.delta_y, behavior: 'instant'});
    }
    return {confirmed: true};
  }
  chrome.runtime.onMessage.addListener((message, sender, reply) => {
    if (sender.id !== chrome.runtime.id || message?.source !== 'codepier-native') return false;
    try {
      allowed(location.href, message.allowed_origins || []);
      if (message.expected_url !== location.href) fail('BROWSER_TAB_CHANGED', '页面已导航，请重新观察');
      if (!['snapshot', 'action'].includes(message.action)) fail('BROWSER_ACTION_UNSUPPORTED', '未知动作');
      reply({ok: true, data: message.action === 'snapshot' ? snapshot() : action(message)});
    } catch (error) {
      reply({ok: false, code: error.code || 'BROWSER_DOCUMENT_UNAVAILABLE', message: error.message || '页面未确认操作'});
    }
    return false;
  });
  globalThis.__codepierDOMAgent = {version: 1, document_id: documentId};
})();
