// Fixed review uses the host's JSON-RPC transport only. The parent window is
// fixed at connection time because sandboxed iframe origins can be opaque.
const VERSION = '2026-01-26';
const events = new Map([
  ['ui/notifications/tool-input', 'ontoolinput'],
  ['ui/notifications/tool-result', 'ontoolresult'],
  ['ui/notifications/tool-cancelled', 'ontoolcancelled'],
  ['ui/notifications/host-context-changed', 'onhostcontextchanged'],
]);

export class App {
  constructor(info) {
    this.info = info;
    this.host = window.parent;
    this.pending = new Map();
    this.nextId = 0;
    this.receive = this.receive.bind(this);
  }

  send(message) {
    this.host.postMessage({jsonrpc: '2.0', ...message}, '*');
  }

  request(method, params, timeout) {
    if (this.pending.size >= 32) return Promise.reject(new Error('请求过多，请稍后重试。'));
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('组件请求超时，请查看原操作。'));
      }, timeout);
      this.pending.set(id, {resolve, reject, timer});
      try { this.send({id, method, params}); }
      catch (error) { clearTimeout(timer); this.pending.delete(id); reject(error); }
    });
  }

  receive(event) {
    const row = event.data;
    if (event.source !== this.host || !row || row.jsonrpc !== '2.0') return;
    if (!('method' in row)) {
      const entry = this.pending.get(row.id);
      if (!entry || (('result' in row) === ('error' in row))) return;
      this.pending.delete(row.id);
      clearTimeout(entry.timer);
      if (row.error) entry.reject(new Error(row.error.message || '组件请求失败。'));
      else entry.resolve(row.result);
      return;
    }
    if ('id' in row) {
      if (row.method === 'ui/resource-teardown') {
        Promise.resolve(this.onteardown?.()).then(() => {
          this.send({id: row.id, result: {}});
          this.close();
        });
      } else if (row.method === 'ping') this.send({id: row.id, result: {}});
      else this.send({id: row.id, error: {code: -32601, message: 'Method not found'}});
      return;
    }
    if (!this.ready) return;
    const handler = events.get(row.method);
    if (handler && row.params && typeof row.params === 'object') this[handler]?.(row.params);
  }

  async connect() {
    if (this.host === window) throw new Error('没有组件宿主。');
    window.addEventListener('message', this.receive);
    try {
      const result = await this.request('ui/initialize', {
        appInfo: this.info, appCapabilities: {}, protocolVersion: VERSION,
      }, 15000);
      if (result?.protocolVersion !== VERSION || !result.hostCapabilities || !result.hostContext)
        throw new Error('不支持当前组件协议。');
      this.context = result.hostContext;
      this.ready = true;
      this.send({method: 'ui/notifications/initialized'});
      if (typeof ResizeObserver !== 'undefined') {
        let lastWidth, lastHeight;
        this.observer = new ResizeObserver(() => {
          const width = Math.ceil(window.innerWidth);
          const height = Math.ceil(document.body.getBoundingClientRect().height);
          if (width === lastWidth && height === lastHeight) return;
          lastWidth = width; lastHeight = height;
          this.send({method: 'ui/notifications/size-changed', params: {width, height}});
        });
        this.observer.observe(document.body);
      }
    } catch (error) { this.close(); throw error; }
  }

  getHostContext() { return this.context; }

  callServerTool(params) {
    if (!this.ready) return Promise.reject(new Error('组件未连接。'));
    return this.request('tools/call', params, 120000);
  }

  close() {
    this.ready = false;
    this.observer?.disconnect();
    window.removeEventListener('message', this.receive);
    for (const entry of this.pending.values()) {
      clearTimeout(entry.timer);
      entry.reject(new Error('组件已关闭。'));
    }
    this.pending.clear();
  }
}
