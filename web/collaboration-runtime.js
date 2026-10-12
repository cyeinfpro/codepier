'use strict';
// Ephemeral presentation helpers. No persistence, authorization caching or network writes.
window.CodePierCollaborationRuntime = (() => {
  const indexes = new WeakMap();
  const animations = new Map();
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  function index(messages) {
    let result = indexes.get(messages);
    if (!result) {
      result = new Map(messages.map((message) => [message.id, message]));
      indexes.set(messages, result);
    }
    return result;
  }
  function merge(messages, incoming) {
    if (!incoming?.length) return messages;
    const original = index(messages);
    let next = null;
    for (const message of incoming) {
      if (!message?.id) continue;
      const previous = (next || original).get(message.id);
      if (previous && (previous.version || 0) > (message.version || 1)) continue;
      // Receipts can change without a message version increment. Retain object
      // identity only when the complete record is unchanged, not just its version.
      if (previous === message || (previous && JSON.stringify(previous) === JSON.stringify(message)))
        continue;
      next ||= new Map(original);
      next.set(message.id, message);
    }
    if (!next) return messages;
    const result = [...next.values()].sort((a, b) =>
      (a.server_sequence || 0) - (b.server_sequence || 0) ||
      (a.created || 0) - (b.created || 0) || a.id.localeCompare(b.id));
    indexes.set(result, next);
    return result;
  }
  function text(node, value) {
    if (node && node.textContent !== String(value)) node.textContent = String(value);
  }
  function enter(node, { x = 0, y = 8, duration = 220 } = {}) {
    if (!node?.isConnected || node.hidden || !node.animate || reduced.matches || document.hidden) return;
    const previous = animations.get(node);
    const current = previous ? getComputedStyle(node) : null;
    const first = current
      ? { opacity: current.opacity, transform: current.transform }
      : { opacity: 0, transform: `translate3d(${x}px, ${y}px, 0)` };
    previous?.cancel();
    const animation = node.animate([first, { opacity: 1, transform: 'translate3d(0, 0, 0)' }], {
      duration, easing: 'cubic-bezier(0.2, 0.8, 0.2, 1)',
    });
    animations.set(node, animation);
    animation.finished.catch(() => {}).finally(() => {
      if (animations.get(node) === animation) animations.delete(node);
    });
  }
  function cancelWithin(root) {
    for (const [node, animation] of animations) {
      if (!root || root === node || root.contains(node)) {
        animation.cancel();
        animations.delete(node);
      }
    }
  }
  reduced.addEventListener('change', () => { if (reduced.matches) cancelWithin(); });
  return { index, merge, text, enter, cancelWithin };
})();
