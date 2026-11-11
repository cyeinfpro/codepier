(() => {
  const groups = JSON.parse(document.getElementById('codepier-route-assets')?.textContent || '{}');
  const loaded = new Map();
  function asset(url) {
    if (loaded.has(url)) return loaded.get(url);
    const css = new URL(url, location.href).pathname.endsWith('.css');
    const node = document.createElement(css ? 'link' : 'script');
    if (css) {
      node.rel = 'stylesheet';
      node.href = url;
    } else {
      node.src = url;
      node.async = false;
    }
    const promise = new Promise((resolve, reject) => {
      function finish(error) {
        node.onload = node.onerror = null;
        if (error) {
          node.remove();
          loaded.delete(url);
          reject(error);
        } else resolve();
      }
      node.onload = () => finish();
      node.onerror = () => finish(new Error('页面资源未能加载，请重试'));
      document.head.append(node);
    });
    loaded.set(url, promise);
    return promise;
  }
  window.CodePierAssets = Object.freeze({
    async load(page) {
      // Keep classic-script dependencies ordered. Successful assets survive a
      // later failure so retries never re-execute top-level declarations.
      for (const url of groups[page] || []) await asset(url);
    },
  });
})();
