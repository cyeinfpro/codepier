// View lifetime only. Durable task/update monitors own separate receipts.
export function createPanel({ owner = () => null } = {}) {
  let active = null;
  function detach() {
    active?.dispose();
    active = null;
  }
  function mount(root) {
    detach();
    if (!root) return null;
    const identity = owner(),
      controller = new AbortController(),
      timers = new Set(),
      cleanups = new Set();
    const scope = {
      root,
      signal: controller.signal,
      current: () =>
        active === scope && !controller.signal.aborted && root.isConnected && owner() === identity,
      listen(target, type, listener, options = {}) {
        if (!target || controller.signal.aborted) return;
        const guarded = (event) => {
          if (scope.current()) return listener(event);
        };
        target.addEventListener(type, guarded, options);
        cleanups.add(() => target.removeEventListener(type, guarded, options));
      },
      later(callback, delay) {
        if (controller.signal.aborted) return null;
        const timer = setTimeout(() => {
          timers.delete(timer);
          if (scope.current()) callback();
        }, delay);
        timers.add(timer);
        return timer;
      },
      dispose() {
        controller.abort();
        for (const timer of timers) clearTimeout(timer);
        timers.clear();
        for (const cleanup of cleanups) cleanup();
        cleanups.clear();
        if (active === scope) active = null;
      },
    };
    active = scope;
    return scope;
  }
  return { mount, detach };
}
