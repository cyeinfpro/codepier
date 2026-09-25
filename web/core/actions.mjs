// Explicit registration makes unknown/duplicate actions testable. A failed action
// releases its local guard; durable mutations still use their original request key.
export function create() {
  const handlers = new Map(),
    pending = new WeakSet();
  function register(names, handler) {
    if (
      !Array.isArray(names) ||
      !names.length ||
      new Set(names).size !== names.length ||
      typeof handler !== 'function'
    )
      throw new TypeError('Invalid action registration');
    if (
      names.some(
        (name) => typeof name !== 'string' || !/^[a-z][a-z0-9-]*$/.test(name) || handlers.has(name),
      )
    )
      throw new Error('Invalid or duplicate action');
    for (const name of names) handlers.set(name, handler);
  }
  async function dispatch(name, button, event) {
    const handler = handlers.get(name);
    if (!handler || !button || button.disabled || pending.has(button)) return false;
    pending.add(button);
    try {
      await handler(button, event);
      return true;
    } finally {
      pending.delete(button);
    }
  }
  return { register, dispatch, names: () => [...handlers.keys()] };
}
