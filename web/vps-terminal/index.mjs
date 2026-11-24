import * as runtime from "./xterm-runtime.mjs";
import { TerminalStore } from "./store.mjs";
import { TerminalLayout } from "./layout.mjs";
import { normalizeUuid } from "./protocol.mjs";

let options = null,
  store = null,
  layout = null,
  identity = null,
  mountVersion = 0;

function ensure() {
  if (!options?.api || !options.getIdentity)
    throw new Error("VPS 终端尚未初始化");
  const current = options.getIdentity();
  if (typeof current !== "string" || !current) throw new Error("请先登录");
  if (store && (identity !== current || store.disposed)) reset();
  if (!store) {
    identity = current;
    const guardedApi = async (...args) => {
      if (options.getIdentity() !== current)
        throw Object.assign(new Error("登录状态已变化"), {
          code: "AUTH_REVOKED",
        });
      const response = await options.api(...args);
      if (options.getIdentity() !== current)
        throw Object.assign(new Error("登录状态已变化"), {
          code: "AUTH_REVOKED",
        });
      return response;
    };
    store = new TerminalStore({
      api: guardedApi,
      runtime,
      identity,
      isCurrent: () => !!layout?.visible,
      onEdit: (id) => options.onEditVps?.(id),
      onNavigate: (id, context) => options.onNavigate?.(id, context),
      onChange: () => {
        layout?.render();
        options.onCountChange?.(store?.count() || 0);
      },
    });
    layout = new TerminalLayout({
      store,
      onBack: () => options.onNavigate?.(null),
    });
  }
  return store;
}

export function configure(config) {
  if (
    typeof config?.api !== "function" ||
    typeof config.getIdentity !== "function"
  )
    throw new Error("Missing terminal integration");
  options = config;
  if (store && identity !== options.getIdentity()) reset();
  return api;
}
export async function open(request) {
  const current = ensure();
  return current.open(request);
}
export async function mount(container, { sessionId } = {}) {
  const current = ensure();
  const version = ++mountVersion;
  if (!(container instanceof HTMLElement))
    throw new Error("Missing terminal container");
  layout.mount(container);
  if (sessionId) {
    try {
      const requested = normalizeUuid(sessionId);
      // Tab activation and splitting already selected the renderer before
      // updating the URL. Re-mounts must not collapse that split or refocus it.
      if (requested !== current.focusedId)
        await current.restore(requested, {
          isCurrent: () => version === mountVersion && container.isConnected,
        });
      if (!current.vpsCatalog.length)
        current.refreshVpsCatalog().catch(() => {});
    } catch (error) {
      if (layout && version === mountVersion && container.isConnected) {
        layout.error.textContent = error.message;
        layout.error.hidden = false;
      }
    }
  } else {
    current.refreshSessions().catch(() => {});
    current.refreshVpsCatalog().catch(() => {});
  }
  return api;
}
export function hide() {
  mountVersion++;
  layout?.hide();
}
export function count() {
  return store?.count() || 0;
}
export function list() {
  return store?.list() || [];
}
export function activeCountsByVPS() {
  return store?.activeCountsByVPS() || Object.create(null);
}
export function reset() {
  mountVersion++;
  store?.dispose();
  layout?.dispose();
  store = null;
  layout = null;
  identity = null;
  options?.onCountChange?.(0);
}
export const logout = reset;
export const dispose = reset;
export const api = Object.freeze({
  configure,
  open,
  mount,
  hide,
  reset,
  logout,
  dispose,
  count,
  list,
  activeCountsByVPS,
});
globalThis.CodePierVpsTerminal = api;
