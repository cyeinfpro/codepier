"use strict";
// Saved connection management. Passwords stay in the active form only; neither
// metadata state nor browser storage ever receives a saved credential.
const VPS_TERMINAL_ENDED = ["closed", "exited", "failed", "revoked"];
async function vpsInventory(observeFirstPage, signal) {
  const items = [];
  let offset = 0;
  do {
    const r = await api(
      "/api/vps?limit=200&offset=" + offset,
      signal ? { signal } : undefined,
    );
    if (offset === 0) observeFirstPage?.(r);
    items.push(...r.vps);
    offset = r.next_offset;
  } while (offset !== null);
  return items;
}
function vpsIntent() {
  const session = S.session,
    page = S.page,
    space = S.space_id,
    id = (S.vpsIntent = (S.vpsIntent || 0) + 1);
  return () =>
    session === S.session &&
    page === S.page &&
    space === S.space_id &&
    id === S.vpsIntent;
}
function vpsEndpoint(v) {
  return `${v.host.includes(":") ? "[" + v.host + "]" : v.host}:${v.port}`;
}
function vpsShellQuote(value) {
  const text = String(value || "");
  return /^[A-Za-z0-9_@.:%/+-]+$/.test(text)
    ? text
    : `'${text.replace(/'/g, "'\\''")}'`;
}
function vpsSshCommand(v) {
  const host = String(v.host || "")
    .trim()
    .replace(/^\[|\]$/g, "");
  const user = String(v.username || "root").trim() || "root";
  const port = Number(v.port) || 22;
  const ipv6 = host.includes(":") ? " -6" : "";
  const portArg = port === 22 ? "" : ` -p ${port}`;
  const target = vpsShellQuote(`${user}@${host}`);
  const sep = target.startsWith("-") ? " --" : "";
  return `ssh${ipv6}${portArg}${sep} ${target}`;
}
function vpsLayout() {
  if (S.vpsLayout === "list" || S.vpsLayout === "cards") return S.vpsLayout;
  if (typeof localStorage !== "undefined") {
    try {
      const saved = localStorage.getItem("codepier-vps-layout");
      if (saved === "list" || saved === "cards") return (S.vpsLayout = saved);
    } catch {}
  }
  if (
    typeof window !== "undefined" &&
    window.matchMedia?.("(max-width: 760px)")?.matches
  )
    return "list";
  return "cards";
}
const vpsVolatilePins = new Map();
function vpsPinKey() {
  const scope = vpsTerminalIdentity();
  return scope ? "codepier-vps-pins:v1:" + scope : null;
}
function vpsReadPins(key = vpsPinKey()) {
  if (!key) return new Set();
  if (vpsVolatilePins.has(key)) return new Set(vpsVolatilePins.get(key));
  try {
    const saved = JSON.parse(localStorage.getItem(key));
    if (Array.isArray(saved))
      return new Set(saved.filter((id) => typeof id === "string"));
  } catch {}
  return new Set();
}
function vpsWritePins(key, pins) {
  try {
    if (pins.size) localStorage.setItem(key, JSON.stringify([...pins]));
    else localStorage.removeItem(key);
    vpsVolatilePins.delete(key);
  } catch {
    vpsVolatilePins.set(key, pins);
  }
}
function vpsPinnedFirst(items, pins) {
  const pinned = [],
    others = [];
  for (const item of items) (pins.has(item.id) ? pinned : others).push(item);
  return [...pinned, ...others];
}
function vpsTogglePin(button) {
  const key = vpsPinKey(),
    id = button.dataset.id,
    grid = button.closest(".vps-grid");
  if (
    !key ||
    key !== S.vpsPinViewKey ||
    S.page !== "vps" ||
    !grid ||
    !S.vps?.some((v) => v.id === id)
  )
    return;
  const pins = vpsReadPins(key),
    pinned = !pins.has(id);
  if (pinned) pins.add(id);
  else pins.delete(id);
  vpsWritePins(key, pins);
  button.setAttribute("aria-pressed", String(pinned));
  button.title = pinned ? "取消置顶" : "置顶";

  // Reorder real DOM nodes (including their keyboard order) without recreating
  // terminal status, form controls or the search/filter state.
  const cards = new Map(
    [...grid.children].map((card) => [card.dataset.vpsCard, card]),
  );
  const focused = document.activeElement;
  let cursor = grid.firstElementChild;
  for (const v of vpsPinnedFirst(S.vps, pins)) {
    const card = cards.get(v.id);
    if (card === cursor) cursor = cursor.nextElementSibling;
    else if (card) grid.insertBefore(card, cursor);
  }
  if (focused && grid.contains(focused) && document.activeElement !== focused)
    focused.focus();
}
let vpsTerminalOwner = null;
let vpsPresenceRequest = null;
function vpsTerminalIdentity() {
  if (!S.session?.user_id || !S.space_id) return null;
  return JSON.stringify([S.session.user_id, S.space_id]);
}
function vpsTerminalForget() {
  vpsTerminalOwner = null;
  S.vpsPresence = null;
  vpsPresenceRequest = null;
  for (const watch of vpsReadinessWatches.values()) watch.controller.abort();
  vpsReadinessWatches.clear();
  vpsFleetWatch?.controller.abort();
  vpsReadinessBackfill = null;
}
function vpsActiveSessions(vpsId = null) {
  const identity = vpsTerminalIdentity();
  if (!identity) return [];
  const remote =
    S.vpsPresence?.identity === identity ? S.vpsPresence.items : [];
  const local =
    typeof window !== "undefined" && vpsTerminalOwner === identity
      ? window.CodePierVpsTerminal?.list?.() || []
      : [];
  const byId = new Map();
  for (const item of [...remote, ...local]) {
    const id = item?.session_id || item?.id;
    if (id) byId.set(id, item);
  }
  return [...byId.values()].filter(
    (item) =>
      !VPS_TERMINAL_ENDED.includes(item.state) &&
      (!vpsId || item.vps_id === vpsId),
  );
}
function vpsTerminalCount() {
  return vpsActiveSessions().length;
}
async function vpsLoadPresence() {
  const identity = vpsTerminalIdentity();
  if (!identity) return;
  if (vpsPresenceRequest?.identity === identity)
    return vpsPresenceRequest.promise;
  const pending = { identity };
  // The result is scoped to the login/space, not a particular render.
  vpsPresenceRequest = pending;
  pending.promise = (async () => {
    try {
      const result = await api("/api/vps-terminal/sessions");
      if (vpsPresenceRequest !== pending || identity !== vpsTerminalIdentity())
        return;
      if (!Array.isArray(result?.sessions))
        throw new Error("终端会话列表暂时不可用");
      S.vpsPresence = { identity, items: result.sessions };
      vpsSyncTerminalPresence();
    } finally {
      if (vpsPresenceRequest === pending) vpsPresenceRequest = null;
    }
  })();
  return pending.promise;
}
function vpsTerminalNavigate(id, { terminal = false } = {}) {
  if (!S.session || S.page !== "vps") return;
  const destination = id
    ? "vps?view=terminal&session=" + encodeURIComponent(id)
    : terminal
      ? "vps?view=terminal"
      : "vps";
  if (location.hash !== "#" + destination) void navigate(destination);
}
function vpsActiveSessionsInnerHTML(
  sessions = vpsActiveSessions(),
  total = vpsTerminalCount(),
) {
  const count = total || sessions.length;
  if (!count) return "";
  const pills = sessions
    .map((item) => {
      const vps = S.vps?.find((x) => x.id === item.vps_id);
      const name = item.vps_name || item.title || vps?.name || "SSH 终端";
      const hostPort =
        item.endpoint ||
        (vps
          ? vpsEndpoint(vps)
          : item.host
            ? `${item.host}:${item.port || 22}`
            : "");
      const user = item.username || vps?.username || "";
      const target = user && hostPort ? `${user}@${hostPort}` : hostPort;
      const sid = item.session_id || item.id || "";
      const tmux = item.mode === "tmux";
      return `<button type="button" class="btn ghost small vps-session-pill" data-vps-action="resume-session" data-session="${esc(sid)}" data-id="${esc(item.vps_id || "")}"><span class="dot online" aria-hidden="true"></span><strong>${esc(name)}</strong>${target ? `<code>${esc(target)}</code>` : ""}${tmux ? '<span class="badge neutral">tmux 持久</span>' : ""}</button>`;
    })
    .join("");
  return `<div class="vps-active-head"><span class="dot online" aria-hidden="true"></span><strong>活跃终端 · ${count}</strong></div>${pills ? `<div class="vps-session-list">${pills}</div>` : ""}<button type="button" class="btn small vps-sessions-enter" data-vps-action="sessions">${icon("terminal")}进入终端工作台</button>`;
}
function vpsSyncTerminalPresence() {
  const sessions = vpsActiveSessions(),
    count = vpsTerminalCount();
  $$("[data-vps-terminal-count]").forEach((node) => {
    node.textContent = count ? `终端会话 · ${count}` : "终端会话";
  });
  const statNode = $('[data-vps-stat="sessions"]');
  if (statNode) statNode.textContent = String(count);
  const bar = $(".vps-active-sessions");
  if (bar) {
    if (count > 0) {
      bar.hidden = false;
      bar.innerHTML = vpsActiveSessionsInnerHTML(sessions, count);
    } else {
      bar.hidden = true;
      bar.replaceChildren();
    }
  }
  $$("[data-vps-card]").forEach((card) => {
    const id = card.dataset.vpsCard,
      cardCount = sessions.filter((item) => item.vps_id === id).length,
      v = S.vps?.find((item) => item.id === id);
    card.dataset.hasSessions = String(cardCount > 0);
    const badge = card.querySelector("[data-vps-card-sessions]");
    if (badge) {
      badge.hidden = !cardCount;
      badge.textContent = cardCount ? `终端 · ${cardCount}` : "";
    }
    const termBtn = card.querySelector('[data-vps-action="terminal"]');
    if (termBtn) {
      termBtn.innerHTML = `${icon("terminal")}${cardCount ? `返回终端 · ${cardCount}` : "打开终端"}`;
      const blocked = !cardCount && v && vpsTerminalReason(v);
      termBtn.disabled = Boolean(blocked || termBtn.dataset.vpsOpening);
      if (blocked)
        termBtn.setAttribute("aria-describedby", "vps-terminal-hint-" + id);
      else termBtn.removeAttribute("aria-describedby");
    }
    const cta = card.querySelector(".vps-footer-cta");
    let newBtn = card.querySelector('[data-vps-action="terminal-new"]');
    const canNew = cardCount > 0 && v && !vpsTerminalReason(v);
    if (canNew && !newBtn && cta) {
      newBtn = document.createElement("button");
      newBtn.type = "button";
      newBtn.className = "btn ghost small vps-terminal-new";
      newBtn.dataset.vpsAction = "terminal-new";
      newBtn.dataset.id = id;
      newBtn.title = "新开终端会话";
      newBtn.setAttribute("aria-label", `为 ${v?.name || "VPS"} 新开终端`);
      newBtn.innerHTML = icon("plus");
      cta.append(newBtn);
    } else if (!canNew && newBtn) {
      newBtn.remove();
    }
  });
  if ($("#vps-status-filter")?.value === "active") vpsFilter();
}
async function vpsTerminalModule() {
  const owner = vpsTerminalIdentity();
  if (!owner) throw new Error("请先登录并选择空间");
  await window.CodePierAssets.load("vps-terminal");
  if (owner !== vpsTerminalIdentity()) throw sessionChanged();
  vpsTerminalOwner = owner;
  const terminal = window.CodePierVpsTerminal;
  if (!terminal) throw new Error("终端资源未能加载，请重试");
  terminal.configure({
    api,
    getIdentity: vpsTerminalIdentity,
    onNavigate: vpsTerminalNavigate,
    onCountChange: () => vpsSyncTerminalPresence(),
    onEditVps: (id) => vpsEdit(id),
  });
  return terminal;
}
async function vpsTerminalPage(seq) {
  const owner = vpsTerminalIdentity(),
    query = new URLSearchParams(parseHashRoute().query),
    current = () =>
      seq === S.renderSeq &&
      S.page === "vps" &&
      owner === vpsTerminalIdentity();
  const terminal = await vpsTerminalModule();
  if (!current()) return;
  const page = $("#page");
  let host = $("#vps-terminal-host", page);
  if (!host) {
    terminal.hide();
    host = document.createElement("div");
    host.id = "vps-terminal-host";
    page.replaceChildren(host);
  }
  uiPageReady(false);
  await terminal.mount(host, { sessionId: query.get("session") || undefined });
}
function vpsTerminalProjects(v) {
  return (v.projects || [])
    .filter((p) => p.mode === "write" && p.allow_tasks && p.enabled !== false)
    .sort((a, b) => Number(!!b.online) - Number(!!a.online));
}
function vpsTerminalCode(v) {
  if (v.capabilities?.terminal?.supported === false)
    return (
      v.capabilities.terminal.reason_code || "VPS_TRANSPORT_UPGRADE_REQUIRED"
    );
  return v.terminal?.reason_code || v.terminal?.code || "";
}
function vpsTerminalReason(v) {
  if (!v.enabled) return "连接已停用，启用后可打开终端。";
  if (v.capabilities?.terminal?.supported === false)
    return vpsTransportMessage(vpsTerminalCode(v));
  if (v.terminal && typeof v.terminal === "object") {
    if (v.terminal.eligible !== false && v.terminal.available !== false)
      return "";
    const code = vpsTerminalCode(v);
    if (code === "SSH_TERMINAL_FORBIDDEN" || code === "FORBIDDEN")
      return "连接项目需要写入和执行命令权限。";
    if (code === "NO_TERMINAL_AGENT")
      return "当前空间暂无可用电脑，请先添加电脑。";
    if (code === "AGENT_OFFLINE" || code === "NODE_OFFLINE")
      return "连接电脑暂时离线，上线后可重试。";
    if (code === "SHELL_DISABLED") return "连接电脑尚未开启命令执行权限。";
    if (
      [
        "AGENT_UPGRADE_REQUIRED",
        "PROTOCOL_MISMATCH",
        "SSH_DEPENDENCY_MISSING",
      ].includes(code)
    )
      return "连接电脑需要更新才能使用 Web SSH。";
    if (code === "FEATURE_DISABLED") return "当前空间未启用 Web SSH。";
    return v.terminal.reason || "当前无法打开终端，请检查连接电脑状态。";
  }
  if ((v.project_ids || []).length > 0 && vpsTerminalProjects(v).length === 0)
    return "连接项目需要写入和执行命令权限。";
  return "";
}
function vpsCheckReady(v, projects) {
  if (!v.enabled || v.capabilities?.probe?.supported === false) return false;
  if (typeof v.terminal?.check_ready === "boolean")
    return v.terminal.check_ready;
  return !vpsTerminalReason(v) || projects.some((p) => p.online);
}
function vpsChannelLabel(v) {
  if (!v.enabled) return "已停用";
  if (vpsTerminalReason(v)) return "当前不可直连";
  if (v.terminal?.device_name) return `经由 ${v.terminal.device_name} 直连`;
  const projects = vpsTerminalProjects(v);
  const online = projects.find((p) => p.online);
  if (online?.device_name) return `经由 ${online.device_name} 直连`;
  if (v.terminal?.available === true || v.terminal?.eligible === true)
    return "空间电脑直连就绪";
  if (projects.length) return "等待连接电脑上线";
  if (!v.terminal && !(v.project_ids || []).length) return "空间电脑直连";
  return "待授权执行权限";
}
const VPS_READINESS_ACTIVE = new Set([
  "queued",
  "checking",
  "running",
  "installing",
]);
// Include 255s of default server backoff plus connection and probe time.
const VPS_READINESS_BUDGET_MS = 540_000;
// A batch may also spend time installing or waiting for another VPS's slot.
const VPS_FLEET_READINESS_BUDGET_MS = 720_000;
const vpsReadinessWatches = new Map();

function vpsReadinessEnabled(v) {
  return (
    v?.enabled !== false &&
    (v?.readiness_available === true || vpsReadinessJob(v?.readiness) !== null)
  );
}
function vpsReadinessJob(payload) {
  const job = payload?.job ?? payload?.readiness ?? payload;
  if (
    !job ||
    typeof job !== "object" ||
    !/^[A-Za-z0-9_-]{1,128}$/.test(String(job.id ?? "")) ||
    typeof job.state !== "string"
  )
    return null;
  return job;
}
function vpsReadinessVerificationOnly(job) {
  return (
    job.state !== "ready" &&
    (job.verification_only === true ||
      (job.reason_code || job.reason) === "INSTALL_OUTCOME_UNCONFIRMED")
  );
}
function vpsReadinessText(job) {
  if (!job) return "尚未检查";
  const code = job.reason_code || job.reason;
  const verifying = vpsReadinessVerificationOnly(job);
  if (
    verifying &&
    job.state !== "installing" &&
    VPS_READINESS_ACTIVE.has(job.state)
  ) {
    return job.state === "queued" ? "已排队复查安装结果" : "正在复查安装结果…";
  }
  const needsVerification = "安装结果待核验，请检查实际环境后重新扫描";
  if (job.state === "missing") {
    return verifying ? needsVerification : "已确认缺少 tmux；未安装";
  }
  const messages = {
    TMUX_MISSING: "已确认缺少 tmux；未安装",
    ALREADY_INSTALLED: "tmux 已就绪",
    TMUX_ALREADY_INSTALLED: "tmux 已就绪",
    INSTALLED: "tmux 已安装并验证",
    TMUX_INSTALLED: "tmux 已安装并验证",
    VPS_OFFLINE: "服务器暂时不可达",
    OFFLINE: "服务器暂时不可达",
    VPS_PRIVILEGE_REQUIRED: "无可用安装权限；未安装",
    VPS_ENV_UNSUPPORTED: "当前系统或包管理器不支持自动安装",
    UNSUPPORTED: "当前系统或包管理器不支持自动安装",
    TMUX_LOCATION_UNSUPPORTED: "检测到 tmux，但不支持其安装路径",
    TMUX_VERSION_UNSUPPORTED: "tmux 版本低于 2.6，请升级后重新扫描",
    TMUX_INSTALL_FAILED: "tmux 安装失败，请检查服务器的包管理器后再扫描",
    VPS_PROBE_INTERRUPTED: "SSH 环境检查超时或中断，请重新扫描",
    TARGET_CHANGED: "服务器配置已变化，请重新检查",
    TARGET_OR_GRANT_CHANGED: "服务器配置或授权已变化；未继续安装",
    INSTALL_OUTCOME_UNCONFIRMED: needsVerification,
    HOST_KEY_UNKNOWN: "旧版连接检查未完成，请更新面板和连接电脑后重试；未安装",
    HOST_KEY_CHANGED: "旧版连接检查未完成，请更新面板和连接电脑后重试；未安装",
    SSH_AUTH_FAILED: "SSH 登录失败，请检查保存的凭据；未安装",
    SSH_PROTOCOL_NEGOTIATION_FAILED:
      "SSH 协议或算法协商失败，请检查服务器 SSH 配置；未安装",
    VPS_TRANSPORT_UPGRADE_REQUIRED:
      "当前入口缺少新版 VPS 传输能力；请更新后重试",
    VPS_CONFIGURATION_CHANGED: "服务器连接配置已变化；未继续安装",
    HOST_KEY_REVOKED: "旧版连接检查未完成，请更新面板和连接电脑后重试；未安装",
    HOST_KEY_UNVERIFIED:
      "旧版连接检查未完成，请更新面板和连接电脑后重试；未安装",
    HUB_SSH_TRUST_MIGRATION_REQUIRED:
      "旧版连接检查未完成，请更新面板和连接电脑后重试；未安装",
    VPS_AUTHORIZATION_CHANGED: "服务器授权已变化；未继续安装",
    DENIED: "没有检查或安装权限",
    FORBIDDEN: "没有检查或安装权限",
    INSTALL_NOT_AUTHORIZED: "没有安装授权；未安装",
    ENV_CHECK_FAILED: "检查未完成，请稍后重试",
  };
  if (job.state === "queued" && code === "VPS_PROBE_INTERRUPTED") {
    return "SSH 环境检查中断，已排队重试";
  }
  const active = {
    queued:
      code === "VPS_OFFLINE" || code === "OFFLINE"
        ? "服务器暂时不可达，已排队重试"
        : "已排队，稍后自动检查",
    checking: "正在检查环境…",
    running: "正在检查环境…",
    installing: "正在安装 tmux…",
  };
  if (
    job.state === "queued" &&
    job.intent === "install" &&
    code !== "VPS_OFFLINE" &&
    code !== "OFFLINE"
  )
    return "已加入安装队列";
  let message = Object.hasOwn(active, job.state)
    ? active[job.state]
    : Object.hasOwn(messages, code)
      ? messages[code]
      : "";
  if (!message) {
    if (job.state === "ready") message = "tmux 已就绪";
    else message = verifying ? needsVerification : "检查未完成，请稍后重试";
  }
  const version = String(job.tmux_version || "");
  return job.state === "ready" && /^[0-9][a-zA-Z0-9.+-]{0,23}$/.test(version)
    ? `${message} · ${version}`
    : message;
}
function vpsReadinessHTML(v) {
  if (!vpsReadinessEnabled(v) || !vpsReadinessBackfill?.available) return "";
  const job = vpsReadinessJob(v.readiness);
  return `<div class="vps-readiness"><button class="btn ghost small" type="button" data-vps-action="readiness" data-id="${esc(v.id)}">${icon("check")}检查环境</button><small data-vps-env-status role="status" aria-live="polite">${esc(vpsReadinessText(job))}</small></div>`;
}
function vpsReadinessCard(id) {
  return $$("[data-vps-card]").find((node) => node.dataset.vpsCard === id);
}
function vpsReadinessVisible(id) {
  const card = vpsReadinessCard(id);
  return (
    S.page === "vps" &&
    !document.hidden &&
    card?.isConnected &&
    !card.hidden &&
    card?.querySelector(".vps-card-more")?.open &&
    card?.querySelector("[data-vps-env-status]")
  );
}
function vpsReadinessPaint(id, message, active) {
  const card = vpsReadinessCard(id);
  const status = card?.querySelector("[data-vps-env-status]");
  if (status && status.textContent !== message) status.textContent = message;
  const button = card?.querySelector('[data-vps-action="readiness"]');
  if (button && button.disabled !== Boolean(active))
    button.disabled = Boolean(active);
}
function vpsReadinessCancelInvisible() {
  for (const [id, watch] of vpsReadinessWatches)
    if (!watch.current() || !vpsReadinessVisible(id)) watch.controller.abort();
}
function vpsReadinessDelay(ms, signal) {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve(false);
    const done = (keep) => {
      clearTimeout(timer);
      signal.removeEventListener("abort", stop);
      resolve(keep);
    };
    const stop = () => done(false);
    const timer = setTimeout(() => done(true), ms);
    signal.addEventListener("abort", stop, { once: true });
  });
}
async function vpsReadinessWatch(id, submit = false) {
  const v = S.vps?.find((item) => item.id === id);
  const owner = vpsTerminalIdentity();
  const card = vpsReadinessCard(id);
  if (
    !owner ||
    !vpsReadinessBackfill?.available ||
    !vpsReadinessEnabled(v) ||
    !vpsReadinessVisible(id)
  )
    return;
  const running = vpsReadinessWatches.get(id);
  if (running && !running.controller.signal.aborted) return;
  const controller = new AbortController();
  const identity = S.session;
  const current = () =>
    identity === S.session &&
    owner === vpsTerminalIdentity() &&
    S.vps?.find((item) => item.id === id) === v &&
    vpsReadinessCard(id) === card;
  const watch = { controller, current };
  vpsReadinessWatches.set(id, watch);
  const visible = () =>
    current() && !controller.signal.aborted && vpsReadinessVisible(id);
  const endpoint = "/api/vps/" + encodeURIComponent(id) + "/readiness";
  let job = null;
  let pending = false;
  // Server work is independent of this browser. The bound concerns observation only.
  let expired = false;
  const deadline = setTimeout(() => {
    expired = true;
    controller.abort();
  }, VPS_READINESS_BUDGET_MS);
  try {
    vpsReadinessPaint(id, submit ? "正在提交检查…" : "正在读取检查状态…", true);
    const payload = await api(endpoint, {
      ...(submit ? { method: "POST" } : {}),
      signal: controller.signal,
    });
    if (!visible()) return;
    job = vpsReadinessJob(payload);
    if (!job && submit) throw new Error("READINESS_UNKNOWN");
    if (job) {
      v.readiness = job;
      pending = VPS_READINESS_ACTIVE.has(job.state);
    }
    vpsReadinessPaint(id, vpsReadinessText(job), pending);
    let wait = 1000;
    while (job && pending && visible()) {
      if (!(await vpsReadinessDelay(wait, controller.signal)) || !visible())
        return;
      const next = vpsReadinessJob(
        await api(endpoint, { signal: controller.signal }),
      );
      if (!visible()) return;
      if (
        !next ||
        next.id !== job.id ||
        String(next.connection_revision ?? "") !==
          String(job.connection_revision ?? "")
      ) {
        vpsReadinessPaint(
          id,
          "检查任务已变化，重新打开“更多”可读取最新状态",
          false,
        );
        return;
      }
      job = next;
      v.readiness = job;
      pending = VPS_READINESS_ACTIVE.has(job.state);
      vpsReadinessPaint(id, vpsReadinessText(job), pending);
      wait = Math.min(7000, wait * 2);
    }
  } catch (error) {
    if (visible())
      vpsReadinessPaint(
        id,
        error?.status === 403
          ? "没有检查或安装权限"
          : submit && !job
            ? "提交结果未确认，重新打开“更多”查看状态"
            : "检查在后台继续，暂时无法读取进度",
        false,
      );
  } finally {
    clearTimeout(deadline);
    if (vpsReadinessWatches.get(id) === watch) {
      vpsReadinessWatches.delete(id);
      if (current()) {
        if (expired && vpsReadinessVisible(id))
          vpsReadinessPaint(
            id,
            "仍在后台检查，重新打开“更多”可查看进度",
            false,
          );
        else {
          const button = vpsReadinessCard(id)?.querySelector(
            '[data-vps-action="readiness"]',
          );
          if (button) button.disabled = false;
        }
      }
    }
  }
}
function vpsBindReadiness() {
  for (const card of $$("[data-vps-card]")) {
    const id = card.dataset.vpsCard;
    const more = card.querySelector(".vps-card-more");
    if (!more || !card.querySelector("[data-vps-env-status]")) continue;
    more.ontoggle = () => {
      vpsReadinessCancelInvisible();
      if (more.open) void vpsReadinessWatch(id);
    };
    // An open, reused card will not dispatch a new toggle after refresh.
    if (!more.open || card.hidden || document.hidden || S.page !== "vps")
      continue;
    const oldWatch = vpsReadinessWatches.get(id);
    const job = vpsReadinessJob(
      S.vps?.find((item) => item.id === id)?.readiness,
    );
    if (
      oldWatch?.controller.signal.aborted ||
      VPS_READINESS_ACTIVE.has(job?.state)
    )
      void vpsReadinessWatch(id);
  }
}
document.addEventListener("visibilitychange", () => {
  vpsReadinessCancelInvisible();
  if (document.hidden || S.page !== "vps") {
    vpsFleetWatch?.controller.abort();
    return;
  }
  void vpsObserveFleet();
  for (const card of $$("[data-vps-card]"))
    if (vpsReadinessVisible(card.dataset.vpsCard))
      void vpsReadinessWatch(card.dataset.vpsCard);
});
if (typeof window !== "undefined")
  window.addEventListener?.("hashchange", () => {
    // The route may change before its replacement view has been rendered.
    if (
      parseHashRoute().page === "vps" &&
      !new URLSearchParams(parseHashRoute().query).has("view")
    )
      return;
    vpsFleetWatch?.controller.abort();
    for (const watch of vpsReadinessWatches.values()) watch.controller.abort();
  });

function vpsActionsHTML(v) {
  const projects = vpsTerminalProjects(v),
    reason = vpsTerminalReason(v),
    checkBlocked = !vpsCheckReady(v, projects),
    checkTitle = !v.enabled
      ? "已停用，启用后可检查"
      : checkBlocked
        ? "当前没有可用的检查通道"
        : "",
    count = vpsActiveSessions(v.id).length,
    terminalCode = vpsTerminalCode(v),
    showProjectFix =
      v.enabled &&
      (v.project_ids || []).length > 0 &&
      !projects.length &&
      reason === "连接项目需要写入和执行命令权限。",
    showDeviceFix =
      v.enabled &&
      [
        "NO_TERMINAL_AGENT",
        "AGENT_OFFLINE",
        "NODE_OFFLINE",
        "SHELL_DISABLED",
        "AGENT_UPGRADE_REQUIRED",
        "PROTOCOL_MISMATCH",
        "SSH_DEPENDENCY_MISSING",
      ].includes(terminalCode);
  return `<footer><div class="vps-footer-cta"><button class="btn primary small" type="button" data-vps-action="terminal" data-id="${esc(v.id)}" ${reason && !count ? `disabled aria-describedby="vps-terminal-hint-${esc(v.id)}"` : ""}>${icon("terminal")}${count ? `返回终端 · ${count}` : "打开终端"}</button>${count && !reason ? `<button class="btn ghost small vps-terminal-new" type="button" data-vps-action="terminal-new" data-id="${esc(v.id)}" title="新开终端会话" aria-label="为 ${esc(v.name)} 新开终端">${icon("plus")}</button>` : ""}</div></footer><details class="vps-card-more"><summary class="btn ghost small" aria-label="${esc(v.name)} 的更多操作"><span class="vps-more-label">更多</span><span class="vps-more-icon" aria-hidden="true">⋯</span></summary><div class="vps-card-details"><div class="vps-address"><code>${esc(v.username)}@${esc(vpsEndpoint(v))}</code></div>${v.notes ? `<p class="vps-notes">${esc(v.notes)}</p>` : ""}<div class="vps-address-actions"><button class="btn ghost small" type="button" data-vps-action="edit" data-id="${esc(v.id)}" aria-label="编辑 ${esc(v.name)}">${icon("edit")}编辑</button><button class="btn ghost small" type="button" data-vps-action="check" data-id="${esc(v.id)}" ${checkBlocked ? `disabled title="${esc(checkTitle)}"` : ""}>检查连接</button><button class="btn ghost small" type="button" data-vps-action="copy-ssh" data-id="${esc(v.id)}">${icon("terminal")}复制 SSH 命令</button><button class="btn ghost small" type="button" data-vps-action="copy" data-id="${esc(v.id)}">${icon("copy")}复制地址</button></div>${vpsReadinessHTML(v)}</div></details>${reason ? `<p class="vps-terminal-hint" id="vps-terminal-hint-${esc(v.id)}">${esc(reason)}${showProjectFix ? `<button class="btn ghost small" type="button" data-action="edit-project" data-id="${esc(v.project_ids[0])}">项目权限</button>` : showDeviceFix ? '<button class="btn ghost small" type="button" data-nav="devices">电脑管理</button>' : ""}</p>` : ""}<div class="vps-terminal-inline" data-vps-terminal-inline hidden></div>`;
}
function vpsTerminalInline(id) {
  const card = $$("[data-vps-card]").find(
    (node) => node.dataset.vpsCard === id,
  );
  return card?.querySelector("[data-vps-terminal-inline]") || null;
}
function vpsTerminalMessage(id, message, { error = false, action } = {}) {
  const root = vpsTerminalInline(id);
  if (!root) return;
  root.hidden = false;
  root.replaceChildren();
  const text = document.createElement("p");
  text.textContent = message;
  text.setAttribute("role", error ? "alert" : "status");
  root.append(text);
  if (action) {
    const link = document.createElement("button");
    link.type = "button";
    link.className = "btn small";
    link.textContent = action.label;
    link.addEventListener("click", action.run);
    root.append(link);
  }
}
const VPS_SAVED_UPGRADE_NOTICE = "当前面板或连接电脑版本不兼容，请更新后重试。";
const VPS_SAVED_LEGACY_CODES = new Set([
  "AGENT_UPGRADE_REQUIRED",
  "HOST_KEY_UNKNOWN",
  "HOST_KEY_CHANGED",
  "HOST_KEY_REVOKED",
  "HOST_KEY_UNVERIFIED",
  "HOST_TRUST_CONFIGURATION",
  "HOST_TRUST_UNAVAILABLE",
  "HOST_TRUST_BUSY",
  "SSH_HOST_KEY_CHANGED",
  "SSH_HOST_KEY_UNTRUSTED",
]);
const VPS_V2_UPGRADE_NOTICE =
  "当前入口缺少新版 VPS 传输能力，请更新面板或连接电脑后重试。";
function vpsTransportMessage(code) {
  const messages = {
    SSH_PROTOCOL_NEGOTIATION_FAILED:
      "SSH 协议或算法协商失败，请检查服务器 SSH 配置。",
    VPS_TRANSPORT_UPGRADE_REQUIRED: VPS_V2_UPGRADE_NOTICE,
    VPS_CONFIGURATION_CHANGED: "服务器连接配置已变化，请核对后重试。",
    VPS_AUTHORIZATION_CHANGED: "服务器访问授权已变化，请核对权限后重试。",
    AGENT_OFFLINE: "连接电脑暂时离线。电脑恢复在线后可以重试。",
    NO_TERMINAL_AGENT: "当前空间暂无可用电脑，请先添加电脑或等待电脑上线。",
    SHELL_DISABLED: "连接电脑尚未开启命令执行权限。",
    FORBIDDEN: "你没有此服务器的操作权限，请联系空间管理员。",
  };
  return Object.hasOwn(messages, code) ? messages[code] : VPS_V2_UPGRADE_NOTICE;
}
function vpsSupportsConnection(v, entry) {
  if (!v || typeof v !== "object") return false;
  if (
    Object.hasOwn(v, "connection_contract") ||
    Object.hasOwn(v, "host_identity_verification")
  ) {
    return (
      v.connection_contract === "vps_ssh_v2" &&
      v.host_identity_verification === "disabled" &&
      v.capabilities?.[entry]?.supported === true
    );
  }
  return v.saved_vps_host_verification === "skip";
}
function vpsSavedLegacyFailure(...values) {
  return values.some((value) => VPS_SAVED_LEGACY_CODES.has(value));
}
async function vpsOpenTerminal(id, button, { newSession = false } = {}) {
  const current = vpsIntent();
  if (button) {
    button.disabled = true;
    button.dataset.vpsOpening = "true";
  }
  try {
    if (!newSession && vpsTerminalIdentity()) {
      let existing = vpsActiveSessions(id)[0];
      if (!existing) {
        // A new browser has no local record. Check the Hub before deciding
        // this click is a request to create a new SSH session.
        try {
          await vpsLoadPresence();
        } catch {
          throw new Error("暂时无法确认有没有正在运行的终端，请重试。");
        }
        if (!current()) return;
        existing = vpsActiveSessions(id)[0];
      }
      if (existing) {
        const sid = existing.session_id || existing.id;
        await navigate("vps?view=terminal&session=" + encodeURIComponent(sid));
        return;
      }
    }
    const v = await api("/api/vps/" + encodeURIComponent(id));
    if (!current()) return;
    if (!vpsSupportsConnection(v, "terminal")) {
      const code =
        v?.capabilities?.terminal?.reason_code ||
        "VPS_TRANSPORT_UPGRADE_REQUIRED";
      throw Object.assign(new Error(vpsTransportMessage(code)), { code });
    }
    const unavailable = vpsTerminalReason(v);
    if (unavailable) {
      const error = new Error(unavailable);
      error.code = vpsTerminalCode(v);
      throw error;
    }
    const via = v.terminal?.device_name;
    vpsTerminalMessage(
      id,
      via ? `正在经由 ${via} 连接…` : `正在连接 ${v.name}…`,
    );
    const terminal = await vpsTerminalModule();
    if (!current()) return;
    await terminal.open({ vps: v, newSession, isCurrent: current });
  } catch (error) {
    if (!current()) return;
    const code = error.code || error.detail?.code || error.reason_code;
    const upgrade =
        vpsSavedLegacyFailure(code) ||
        [
          "PROTOCOL_MISMATCH",
          "SSH_DEPENDENCY_MISSING",
          "VPS_TRANSPORT_UPGRADE_REQUIRED",
        ].includes(code),
      offline = ["AGENT_OFFLINE", "NODE_OFFLINE", "NO_TERMINAL_AGENT"].includes(
        code,
      ),
      shellDisabled = code === "SHELL_DISABLED",
      forbidden = ["FORBIDDEN", "SSH_TERMINAL_FORBIDDEN"].includes(code);
    const message = upgrade
      ? code === "VPS_TRANSPORT_UPGRADE_REQUIRED"
        ? VPS_V2_UPGRADE_NOTICE
        : VPS_SAVED_UPGRADE_NOTICE
      : code === "NO_TERMINAL_AGENT"
        ? "当前空间暂无可用电脑，请先添加电脑或等待电脑上线。"
        : offline
          ? "连接电脑暂时离线。电脑恢复在线后可以重试。"
          : shellDisabled
            ? "连接电脑尚未开启命令执行权限，请前往电脑管理开启后重试。"
            : forbidden
              ? "你没有此服务器的终端执行权限，请联系空间管理员。"
              : error.message || "终端暂时无法连接，请重试";
    if (vpsTerminalInline(id)) {
      vpsTerminalMessage(id, message, {
        error: true,
        action:
          upgrade || offline || shellDisabled
            ? { label: "查看连接电脑", run: () => navigate("devices") }
            : undefined,
      });
    } else if (typeof toast === "function") {
      toast(message, true);
    }
  } finally {
    if (button?.isConnected) {
      button.removeAttribute("data-vps-opening");
      button.disabled = false;
      vpsSyncTerminalPresence();
    }
  }
}
function vpsReplacePage(html) {
  // The workspace lives outside the replaceable inventory, even if this
  // function is called directly by a background refresh.
  window.CodePierVpsTerminal?.hide();
  const page = $("#page"),
    next = document.createElement("template");
  next.innerHTML = html;
  const grid = $(".vps-grid", page),
    freshGrid = $(".vps-grid", next.content);
  if (!grid || !freshGrid) {
    page.replaceChildren(next.content);
    return;
  }
  grid.dataset.layout = freshGrid.dataset.layout || vpsLayout();
  const existing = new Map(
    $$("[data-vps-card]", grid).map((card) => [card.dataset.vpsCard, card]),
  );
  let cursor = grid.firstElementChild;
  for (const card of [...freshGrid.children]) {
    const previous = existing.get(card.dataset.vpsCard);
    const node =
      previous && vpsComparableCard(previous) === vpsComparableCard(card)
        ? previous
        : card;
    if (node === previous) vpsSyncReusedCardReadiness(node, card);
    if (node !== previous && previous?.querySelector(".vps-card-more[open]"))
      node.querySelector(".vps-card-more")?.setAttribute("open", "");
    if (node === cursor) cursor = cursor.nextElementSibling;
    else if (cursor?.dataset.vpsCard === card.dataset.vpsCard) {
      const following = cursor.nextElementSibling;
      cursor.replaceWith(node);
      cursor = following;
    } else grid.insertBefore(node, cursor);
  }
  while (cursor) {
    const following = cursor.nextElementSibling;
    cursor.remove();
    cursor = following;
  }
  const previousBackfill = $(".vps-backfill", page),
    nextBackfill = $(".vps-backfill", next.content);
  if (previousBackfill && nextBackfill)
    nextBackfill.open = previousBackfill.open;
  const nodes = [...next.content.childNodes],
    index = nodes.indexOf(freshGrid);
  for (const node of [...page.childNodes]) if (node !== grid) node.remove();
  grid.before(...nodes.slice(0, index));
  grid.after(...nodes.slice(index + 1));
}
function vpsComparableCard(card) {
  const copy = card.cloneNode(true),
    inline = copy.querySelector("[data-vps-terminal-inline]");
  // Metadata refreshes must not wipe an open connection status or error alert.
  if (inline) {
    inline.hidden = true;
    inline.replaceChildren();
  }
  for (const button of copy.querySelectorAll("[data-vps-opening]")) {
    button.disabled = false;
    button.removeAttribute("data-vps-opening");
  }
  copy.querySelector(".vps-card-more")?.removeAttribute("open");
  // Observation status is transient, not a structural card change.
  copy.querySelector("[data-vps-env-status]")?.replaceChildren();
  copy
    .querySelector('[data-vps-action="readiness"]')
    ?.removeAttribute("disabled");
  copy.removeAttribute("data-has-sessions");
  copy.hidden = false;
  return copy.outerHTML;
}
function vpsSyncReusedCardReadiness(card, fresh) {
  const watch = vpsReadinessWatches.get(card.dataset.vpsCard);
  if (watch?.current() && !watch.controller.signal.aborted) return;
  const status = card.querySelector("[data-vps-env-status]");
  const nextStatus = fresh.querySelector("[data-vps-env-status]");
  if (!status || !nextStatus) return;
  if (status.textContent !== nextStatus.textContent)
    status.textContent = nextStatus.textContent;
  const button = card.querySelector('[data-vps-action="readiness"]');
  const nextButton = fresh.querySelector('[data-vps-action="readiness"]');
  if (button && nextButton && button.disabled !== nextButton.disabled)
    button.disabled = nextButton.disabled;
}
function vpsCardHTML(
  v,
  _sessions,
  pinned = false,
  pinEnabled = Boolean(vpsPinKey()),
) {
  const ready = !vpsTerminalReason(v),
    blockedLabel = v.terminal?.eligible === false ? "待授权" : "未就绪",
    projects = v.projects || [],
    hasSessions = (_sessions || []).some((item) => item.vps_id === v.id),
    dotTone = !v.enabled ? "offline" : ready ? "online" : "warning";
  return `<article class="panel vps-card" data-vps-card="${esc(v.id)}" data-has-sessions="${hasSessions}"><header><span class="dot ${dotTone} vps-row-dot" aria-hidden="true"></span><div class="vps-title-block"><h2>${esc(v.name)}</h2><small class="vps-host-sub mono">${esc(vpsEndpoint(v))}${v.region ? " · " + esc(v.region) : ""}</small></div><div class="vps-badges"><span class="badge ${!v.enabled ? "offline" : "neutral"}">${!v.enabled ? "已停用" : ready ? '<span class="dot online" aria-hidden="true"></span>就绪' : blockedLabel}</span></div>${pinEnabled ? `<button class="icon-btn vps-pin" type="button" data-vps-action="toggle-pin" data-id="${esc(v.id)}" title="${pinned ? "取消置顶" : "置顶"}" aria-label="置顶 ${esc(v.name)}" aria-pressed="${pinned}"><svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M16 3l5 5-3 2-3 5v3l-3 1-7-7 1-3h3l5-3 2-3zM9 15l-6 6"/></svg></button>` : ""}</header><div class="vps-projects" role="group" aria-label="关联项目">${projects.map((p) => `<span class="badge neutral">${esc(p.alias)}</span>`).join("") || '<span class="muted tiny">未关联（仅人工终端）</span>'}</div>${vpsActionsHTML(v)}</article>`;
}
let vpsReadinessBackfill = null;
const VPS_FLEET_NAMES_PER_REASON = 6;
const VPS_FLEET_VISIBLE_NAMES = 12;

function vpsFleetName(value) {
  if (typeof value !== "string") return "";
  const chars = [...value.trim().replace(/\s+/gu, " ")];
  return chars.length > 48 ? chars.slice(0, 47).join("") + "…" : chars.join("");
}

function vpsFleetResult(items) {
  const result = {
    ready: 0,
    missing: 0,
    eligible: 0,
    needsBulkRescan: 0,
    queued: 0,
    scanning: 0,
    installing: 0,
    verifying: 0,
    awaitingVerification: 0,
    failed: 0,
    unchecked: 0,
  };
  const reasons = new Map();
  const revisions = [];
  for (const item of items) {
    if (!vpsReadinessEnabled(item)) continue;
    const job = vpsReadinessJob(item.readiness);
    const verificationOnly = job && vpsReadinessVerificationOnly(job);
    if (job)
      revisions.push(
        JSON.stringify([
          item.id,
          job.id,
          job.state,
          job.intent,
          job.updated,
          job.host_verification,
          job.reason_code,
          verificationOnly,
        ]),
      );
    if (!job) result.unchecked++;
    else if (job.state === "ready") result.ready++;
    else if (job.state === "missing" && !verificationOnly) {
      result.missing++;
      if (job.host_verification !== "skip") result.needsBulkRescan++;
      else if (job.install_eligible === true) result.eligible++;
    } else if (VPS_READINESS_ACTIVE.has(job.state)) {
      if (job.state === "installing") result.installing++;
      else if (verificationOnly) result.verifying++;
      else if (job.state === "queued") result.queued++;
      else if (job.intent === "install") result.installing++;
      else result.scanning++;
    } else {
      const reason = job.reason_code || job.reason;
      if (verificationOnly && reason !== "TMUX_INSTALL_FAILED")
        result.awaitingVerification++;
      else result.failed++;
      const label =
        reason === "HUB_SSH_TRUST_MIGRATION_REQUIRED"
          ? "旧版扫描未完成，请重新扫描"
          : vpsReadinessText(job);
      const group = reasons.get(label) || { count: 0, names: [] };
      group.count++;
      if (group.names.length < VPS_FLEET_NAMES_PER_REASON) {
        const name = vpsFleetName(item.name);
        if (name) group.names.push(name);
      }
      reasons.set(label, group);
    }
  }
  const counts = [
    `就绪 ${result.ready}`,
    `缺少 ${result.missing}`,
    `排队 ${result.queued}`,
    `扫描中 ${result.scanning}`,
    `安装中 ${result.installing}`,
  ];
  if (result.verifying) counts.push(`复查 ${result.verifying}`);
  if (result.awaitingVerification)
    counts.push(`待核验 ${result.awaitingVerification}`);
  counts.push(`失败 ${result.failed}`, `未检查 ${result.unchecked}`);
  let namesLeft = VPS_FLEET_VISIBLE_NAMES;
  return {
    counts: counts.join(" · "),
    reasons: [...reasons]
      .sort((a, b) => b[1].count - a[1].count)
      .map(([label, { count, names }]) => {
        const shown = names.slice(0, namesLeft);
        namesLeft -= shown.length;
        const remaining = count - shown.length;
        if (shown.length && remaining) shown.push(`另 ${remaining} 台`);
        const detail = shown.length ? `（${shown.join("、")}）` : "";
        return `${count} 台：${label}${detail}`;
      })
      .join("；"),
    active:
      result.queued + result.scanning + result.installing + result.verifying >
      0,
    eligible: result.eligible,
    verifying: result.verifying,
    needsBulkRescan: result.needsBulkRescan,
    staleMissing: result.missing - result.eligible - result.needsBulkRescan,
    revision: revisions.sort().join("\n"),
  };
}
function vpsFleetPolicy(value) {
  if (
    Object.hasOwn(value, "readiness_connection_contract") ||
    Object.hasOwn(value, "readiness_host_identity_verification")
  ) {
    return value.readiness_connection_contract === "vps_ssh_v2" &&
      value.readiness_host_identity_verification === "disabled"
      ? "skip"
      : null;
  }
  return value.readiness_fleet_host_verification;
}
function vpsFleetAvailable(view) {
  return view?.available === true && view.fleetPolicy === "skip";
}
function vpsFleetButtons(view) {
  const busy =
    !vpsFleetAvailable(view) ||
    Boolean(
      view.pending || view.awaiting || view.result?.active || view.uncertain,
    );
  return {
    scanDisabled: busy,
    installDisabled: busy || !(view.result?.eligible > 0),
  };
}
function vpsFleetMessage(view) {
  if (view.message) return view.message;
  const help =
    "扫描当前空间中启用的 VPS，不受项目筛选影响。扫描只检查 SSH 和 tmux；一键安装只处理近期扫描确认缺少 tmux 的服务器。";
  const rescan = view.result?.needsBulkRescan;
  const stale = view.result?.staleMissing;
  return (
    help +
    (view.result?.verifying ? " 正在复查前次安装的结果。" : "") +
    (rescan ? " " + rescan + " 台需要批量重扫。" : "") +
    (stale ? " " + stale + " 台的缺失结果已过期或不可安装，请重新扫描。" : "")
  );
}
function vpsReadinessBackfillHTML(view) {
  if (!vpsFleetAvailable(view)) return "";
  const { scanDisabled, installDisabled } = vpsFleetButtons(view);
  return `<details class="panel vps-backfill"><summary class="btn ghost small">当前空间 · 批量终端环境 <small data-vps-fleet-counts role="status">${esc(view.result?.counts || "")}</small></summary><div class="vps-readiness"><button class="btn small" type="button" data-vps-action="readiness-backfill" ${scanDisabled ? "disabled" : ""}>扫描全部 SSH</button><button class="btn small" type="button" data-vps-action="readiness-backfill-install" ${installDisabled ? "disabled" : ""}>一键安装</button><small data-vps-backfill-status role="status">${esc(vpsFleetMessage(view))}</small></div><small data-vps-fleet-reasons ${view.result?.reasons ? "" : "hidden"}>${esc(view.result?.reasons || "")}</small></details>`;
}
function vpsReadinessBackfillPaint(view) {
  if (S.page !== "vps" || view !== vpsReadinessBackfill) return;
  const { scanDisabled, installDisabled } = vpsFleetButtons(view);
  const scan = $('[data-vps-action="readiness-backfill"]');
  const install = $('[data-vps-action="readiness-backfill-install"]');
  const status = $("[data-vps-backfill-status]");
  const counts = $("[data-vps-fleet-counts]");
  const reasons = $("[data-vps-fleet-reasons]");
  if (scan) scan.disabled = scanDisabled;
  if (install) install.disabled = installDisabled;
  if (status && status.textContent !== vpsFleetMessage(view))
    status.textContent = vpsFleetMessage(view);
  if (counts && counts.textContent !== view.result?.counts)
    counts.textContent = view.result?.counts || "";
  if (reasons) {
    const next = view.result?.reasons || "";
    reasons.hidden = !next;
    if (reasons.textContent !== next) reasons.textContent = next;
  }
}
let vpsFleetWatch = null;
async function vpsObserveFleet(view = vpsReadinessBackfill) {
  if (
    !vpsFleetAvailable(view) ||
    (!view.awaiting && !view.uncertain && !view.result?.active) ||
    !vpsFleetVisible(view)
  )
    return;
  const existing = vpsFleetWatch;
  if (existing?.view === view && !existing.controller.signal.aborted) return;
  existing?.controller.abort();
  const controller = new AbortController();
  const watch = { view, controller };
  vpsFleetWatch = watch;
  const session = S.session;
  const current = () =>
    vpsFleetWatch === watch &&
    session === S.session &&
    !controller.signal.aborted &&
    vpsFleetVisible(view);
  const deadline = setTimeout(() => {
    if (current()) {
      view.message = "后台任务仍可能继续；刷新页面查看最新进度。";
      vpsReadinessBackfillPaint(view);
    }
    controller.abort();
  }, VPS_FLEET_READINESS_BUDGET_MS);
  try {
    let delay = 1600;
    let failures = 0;
    let uncertainReads = 0;
    while (current()) {
      if (!(await vpsReadinessDelay(delay, controller.signal)) || !current())
        break;
      delay = Math.min(7000, delay * 2);
      let available = false;
      let items;
      try {
        items = await vpsInventory((first) => {
          available =
            first.readiness_manual_actions === true &&
            vpsFleetPolicy(first) === "skip";
        }, controller.signal);
      } catch (error) {
        if (!current()) break;
        if ([401, 403].includes(error?.status) || ++failures >= 3) throw error;
        continue;
      }
      if (!current()) break;
      if (!available) {
        view.fleetPolicy = null;
        view.message = "批量操作暂不可用，请刷新页面。";
        vpsReadinessBackfillPaint(view);
        break;
      }
      failures = 0;
      const next = vpsFleetResult(items);
      view.result = next;
      if (
        view.uncertain &&
        next.revision === view.ambiguityBaseline &&
        !next.active
      ) {
        if (++uncertainReads >= 4) {
          view.message =
            "已核对，尚无新的任务记录；无法确认提交结果，请刷新页面后再操作。";
          vpsReadinessBackfillPaint(view);
          break;
        }
        vpsReadinessBackfillPaint(view);
        continue;
      }
      view.awaiting = false;
      view.uncertain = false;
      if (!next.active) view.message = "";
      vpsReadinessBackfillPaint(view);
      if (!next.active) break;
    }
  } catch {
    if (current()) {
      view.message =
        "暂时无法刷新进度；后台任务仍可能继续。请刷新页面查看结果。";
      vpsReadinessBackfillPaint(view);
    }
  } finally {
    clearTimeout(deadline);
    if (vpsFleetWatch === watch) vpsFleetWatch = null;
  }
}
function vpsFleetVisible(view) {
  return (
    !document.hidden &&
    S.page === "vps" &&
    view === vpsReadinessBackfill &&
    view.owner === vpsTerminalIdentity() &&
    Boolean($("[data-vps-fleet-counts]"))
  );
}
async function vpsReadinessBackfillStart(install = false) {
  const view = vpsReadinessBackfill;
  if (
    !vpsFleetAvailable(view) ||
    view.owner !== vpsTerminalIdentity() ||
    S.page !== "vps"
  )
    return;
  const buttons = vpsFleetButtons(view);
  if (install ? buttons.installDisabled : buttons.scanDisabled) return;
  const session = S.session;
  const current = () =>
    vpsReadinessBackfill === view &&
    session === S.session &&
    view.owner === vpsTerminalIdentity();
  view.pending = true;
  view.ambiguityBaseline = view.result?.revision ?? "";
  view.message = install ? "正在提交安装…" : "正在提交只读扫描…";
  vpsReadinessBackfillPaint(view);
  try {
    const endpoint = install
      ? "/api/vps/readiness/install"
      : "/api/vps/readiness/scan";
    const result = await api(endpoint, { method: "POST" });
    if (!current()) return;
    if (!Number.isSafeInteger(result?.queued_count) || result.queued_count < 0)
      throw new Error("READINESS_BACKFILL_UNKNOWN");
    view.awaiting = result.queued_count > 0;
    view.message = result.queued_count
      ? `已将 ${result.queued_count} 台加入${install ? "安装队列" : "只读扫描队列"}。`
      : install
        ? "没有符合安装条件的服务器。"
        : "没有新的服务器需要扫描。";
  } catch (error) {
    if (!current()) return;
    view.uncertain = error?.status !== 403;
    view.message =
      error?.status === 403
        ? "没有当前空间的批量操作权限。"
        : "提交结果待确认。刷新页面查看状态，无需重复提交。";
  } finally {
    if (current()) {
      view.pending = false;
      vpsReadinessBackfillPaint(view);
      void vpsObserveFleet(view);
    }
  }
}

async function vpsHTML(seq) {
  vpsLoadPresence().catch(() => {});
  const owner = vpsTerminalIdentity();
  let backfill;
  const [items] = await Promise.all([
    vpsInventory((first) => {
      backfill = {
        owner,
        available: first.readiness_manual_actions === true,
        fleetPolicy: vpsFleetPolicy(first),
      };
    }),
    loadBasics(),
  ]);
  if (seq !== S.renderSeq || owner !== vpsTerminalIdentity()) return "";
  S.vps = items;
  if (owner && backfill?.available) {
    const previous = vpsReadinessBackfill;
    const result = vpsFleetResult(items);
    if (previous?.owner === owner) {
      previous.fleetPolicy = backfill.fleetPolicy;
      previous.result = result;
    } else {
      vpsReadinessBackfill = { ...backfill, result };
    }
  } else {
    vpsReadinessBackfill = null;
  }
  const pinKey = (S.vpsPinViewKey = vpsPinKey()),
    pins = vpsReadPins(pinKey),
    sessions = vpsActiveSessions(),
    activeTotal = vpsTerminalCount(),
    readyCount = items.filter((v) => !vpsTerminalReason(v)).length,
    linkedProjectCount = new Set(items.flatMap((v) => v.project_ids || []))
      .size,
    layout = vpsLayout(),
    statusFilter = S.vpsStatus || "";
  return (
    heading(
      "VPS",
      "",
      "统一管理 SSH 服务器凭据、直连 Web 终端与 AI 项目授权",
      `<button class="btn" type="button" data-vps-action="sessions">${icon("terminal")}<span data-vps-terminal-count>终端会话${activeTotal ? " · " + activeTotal : ""}</span></button><button class="btn primary" type="button" data-vps-action="add">${icon("plus")}添加 VPS</button>`,
    ) +
    `<section class="panel vps-summary" aria-label="服务器概览"><div class="vps-stat"><strong data-vps-stat="total">${items.length}</strong><span>服务器总数</span></div><div class="vps-stat"><strong data-vps-stat="ready">${readyCount}</strong><span>可直连就绪</span></div><div class="vps-stat"><strong data-vps-stat="sessions">${activeTotal}</strong><span>活跃终端</span></div><div class="vps-stat"><strong data-vps-stat="projects">${linkedProjectCount}</strong><span>已关联项目</span></div></section>
    <section class="panel vps-active-sessions" aria-label="活跃终端会话" ${activeTotal ? "" : "hidden"}>${activeTotal ? vpsActiveSessionsInnerHTML(sessions, activeTotal) : ""}</section>
    ${vpsReadinessBackfillHTML(vpsReadinessBackfill)}<div class="vps-filters"><label class="vps-search">${icon("search")}<input id="vps-query" type="search" autocapitalize="none" autocorrect="off" spellcheck="false" enterkeyhint="search" placeholder="搜索名称、IP、账号、服务商、地区、系统或备注" aria-label="搜索 VPS" value="${esc(S.vpsQuery || "")}"></label><select id="vps-project-filter" aria-label="按项目筛选"><option value="">全部项目</option><option value="__unassigned__" ${S.vpsProject === "__unassigned__" ? "selected" : ""}>未分配</option>${S.projects.map((p) => `<option value="${esc(p.id)}" ${S.vpsProject === p.id ? "selected" : ""}>${esc(p.alias)}</option>`).join("")}</select><select id="vps-status-filter" aria-label="按状态筛选"><option value="">全部状态</option><option value="ready" ${statusFilter === "ready" ? "selected" : ""}>可直连</option><option value="active" ${statusFilter === "active" ? "selected" : ""}>活跃终端</option><option value="disabled" ${statusFilter === "disabled" ? "selected" : ""}>已停用</option></select><span class="muted tiny" id="vps-count" role="status"></span><div class="tabs vps-layout-tabs" role="group" aria-label="显示方式"><button type="button" class="${layout === "cards" ? "active" : ""}" data-vps-layout="cards" aria-pressed="${layout === "cards"}">卡片</button><button type="button" class="${layout === "list" ? "active" : ""}" data-vps-layout="list" aria-pressed="${layout === "list"}">列表</button></div></div>
    <section class="vps-grid" data-layout="${esc(layout)}">${vpsPinnedFirst(
      items,
      pins,
    )
      .map((v) => vpsCardHTML(v, sessions, pins.has(v.id), Boolean(pinKey)))
      .join("")}</section>
    <div id="vps-empty" class="empty panel" hidden><p>${items.length ? "没有匹配的 VPS" : "还没有保存服务器连接"}</p>${items.length ? "" : '<button class="btn primary" type="button" data-vps-action="add">添加第一台 VPS</button>'}</div>
    <p class="form-note">添加服务器即可直接打开 Web 终端；勾选关联项目后，也可以在对话中让 AI 连接服务器排查。</p>`
  );
}
function vpsFilter() {
  const q = ($("#vps-query")?.value || "").trim().toLocaleLowerCase(),
    project = $("#vps-project-filter")?.value || "",
    status = $("#vps-status-filter")?.value || "",
    sessions = vpsActiveSessions();
  S.vpsQuery = $("#vps-query")?.value || "";
  S.vpsProject = project;
  S.vpsStatus = status;
  let count = 0,
    visibleAssigned = 0;
  $$("[data-vps-card]").forEach((card) => {
    const v = S.vps.find((item) => item.id === card.dataset.vpsCard);
    if (!v) return;
    const searchText = [
      v.name,
      v.host,
      v.username,
      v.provider,
      v.region,
      v.system,
      v.notes,
    ]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase();
    const statusMatch =
      !status ||
      (status === "ready"
        ? !vpsTerminalReason(v)
        : status === "active"
          ? sessions.some((item) => item.vps_id === v.id)
          : status === "disabled"
            ? !v.enabled
            : true);
    const match =
      (!q || searchText.includes(q)) &&
      (!project ||
        (project === "__unassigned__"
          ? !v.project_ids.length
          : v.project_ids.includes(project))) &&
      statusMatch;
    const revealed = card.hidden && match;
    card.hidden = !match;
    if (revealed && vpsReadinessVisible(v.id)) void vpsReadinessWatch(v.id);
    if (match) {
      count++;
      if (v.project_ids.length) visibleAssigned++;
    }
  });
  if ($("#vps-count"))
    $("#vps-count").textContent = `${count} 台 · ${visibleAssigned} 台已关联`;
  if ($("#vps-empty")) $("#vps-empty").hidden = count > 0;
  vpsReadinessCancelInvisible();
}
function bindVps() {
  if ($("#vps-query")) $("#vps-query").oninput = vpsFilter;
  if ($("#vps-project-filter")) $("#vps-project-filter").onchange = vpsFilter;
  if ($("#vps-status-filter")) $("#vps-status-filter").onchange = vpsFilter;
  vpsFilter();
  vpsBindReadiness();
  void vpsObserveFleet();
}
function vpsProjectChoices(selected = []) {
  return `<div class="check-list vps-check-list">${
    S.projects
      .map(
        (
          p,
        ) => `<div class="vps-project-choice"><label class="check"><input type="checkbox" name="project_ids" value="${esc(p.id)}" ${selected.includes(p.id) ? "checked" : ""}><span>${esc(p.alias)}<small>${esc(p.device_name)} · ${p.online ? "在线" : "离线"}</small></span></label>
    ${p.mode !== "write" || !p.allow_tasks ? `<small data-vps-permission-hint ${selected.includes(p.id) ? "" : "hidden"}>此项目还不能运行命令。 <button class="btn ghost small" type="button" data-action="edit-project" data-id="${esc(p.id)}">去开启</button></small>` : ""}</div>`,
      )
      .join("") || "<p>暂无项目，可以先保存 VPS 并直接使用终端。</p>"
  }</div>`;
}
async function vpsEdit(id = null) {
  const current = vpsIntent();
  const [v] = await Promise.all([
    id ? api("/api/vps/" + id) : Promise.resolve({}),
    loadBasics(),
  ]);
  if (!current()) return;
  const dialog = modal(
    id ? "编辑 VPS · " + v.name : "添加 VPS",
    `<form id="vps-form" autocomplete="off"><div class="vps-form-grid"><section class="form-section vps-form-main"><div class="vps-section-head"><h3>SSH 连接凭据</h3><p class="form-note">密码在服务端加密存储，可用于检查连接或打开浏览器终端。已保存 VPS 的受管连接不核验历史主机指纹。请核对目标地址，仅在可信网络上使用。</p></div><div class="field"><label>VPS 名称</label><input name="name" required maxlength="80" value="${esc(v.name || "")}" placeholder="例如：香港面板、广州轻量"></div><div class="form-row"><div class="field"><label>IP / 主机名</label><input name="host" required maxlength="253" value="${esc(v.host || "")}" placeholder="服务器 IP 或域名" autocapitalize="none" autocorrect="off" spellcheck="false"></div><div class="field vps-port"><label>SSH 端口</label><input name="port" type="number" inputmode="numeric" required min="1" max="65535" value="${v.port || 22}"></div></div><div class="form-row"><div class="field"><label>SSH 账号</label><input name="username" required maxlength="128" value="${esc(v.username || "root")}" autocapitalize="none" autocorrect="off" spellcheck="false"></div><div class="field"><label>SSH 密码</label><input name="password" type="password" ${id ? "" : "required"} maxlength="4096" autocomplete="new-password" autocapitalize="none" autocorrect="off" spellcheck="false" placeholder="${id ? "已保存，留空保持原密码" : "仅用于连接服务器"}"><small>${id ? "填入新密码即可替换。" : "密码在服务端加密保存。"}</small></div></div><label class="check"><input type="checkbox" name="enabled" ${v.enabled !== false ? "checked" : ""}>启用此连接</label><details class="vps-extra" ${v.provider || v.region || v.system || v.notes ? "open" : ""}><summary>服务商、地区与备注</summary><div class="form-row"><div class="field"><label>服务商</label><input name="provider" maxlength="120" value="${esc(v.provider || "")}" placeholder="可选"></div><div class="field"><label>地区</label><input name="region" maxlength="120" value="${esc(v.region || "")}" placeholder="可选"></div></div><div class="field"><label>系统 / 配置</label><input name="system" maxlength="160" value="${esc(v.system || "")}" placeholder="例如 Debian 13 · 2 核 4 GB"></div><div class="field"><label>备注</label><textarea name="notes" rows="2" maxlength="2000" placeholder="用途或部署目录，避免填写密码">${esc(v.notes || "")}</textarea></div></details></section><section class="form-section vps-form-side"><div class="vps-section-head"><h3>AI 项目授权 · 可选</h3><p class="form-note">打开终端无需分配项目；勾选后允许 AI 在所选项目中连接此服务器。</p></div><div class="field"><label>分配给项目（可多选）</label>${vpsProjectChoices(v.project_ids)}</div></section></div><p id="vps-form-error" class="vps-error" role="alert" hidden></p></form>`,
    `${id ? '<button class="btn danger" type="button" id="vps-delete">删除连接</button>' : ""}<button class="btn ghost" type="button" data-action="close-modal">取消</button><button class="btn" type="button" id="vps-save-terminal">保存并打开终端</button><button class="btn primary" type="button" id="vps-save">保存并检查</button>`,
  );
  dialog.classList.add("vps-edit-dialog");
  const form = $("#vps-form", dialog),
    save = $("#vps-save", dialog),
    saveTerminal = $("#vps-save-terminal", dialog);
  const session = S.session,
    space = S.space_id,
    page = S.page;
  const editCurrent = () =>
    dialog.isConnected &&
    session === S.session &&
    space === S.space_id &&
    page === S.page;
  form.addEventListener("change", (event) => {
    const input = event.target;
    if (input.name === "project_ids") {
      const hint = input
        .closest(".vps-project-choice")
        ?.querySelector("[data-vps-permission-hint]");
      if (hint) hint.hidden = !input.checked;
    }
  });
  const submit = ({ openTerminal = false } = {}) => {
    if (!form.reportValidity()) return;
    const trigger = openTerminal && saveTerminal ? saveTerminal : save,
      peer = trigger === save ? saveTerminal : save;
    if (peer) peer.disabled = true;
    return busy(trigger, async () => {
      const error = $("#vps-form-error", dialog);
      error.hidden = true;
      try {
        const data = Object.fromEntries(new FormData(form));
        data.port = Number(data.port);
        data.enabled = form.elements.enabled.checked;
        data.project_ids = $$('input[name="project_ids"]:checked', form).map(
          (x) => x.value,
        );
        if (id) data.expected_version = v.version;
        if (!data.password) delete data.password;
        const saved = await api("/api/vps" + (id ? "/" + id : ""), {
          method: id ? "PUT" : "POST",
          body: JSON.stringify(data),
        });
        form.elements.password.value = "";
        if (!editCurrent()) return;
        toast("VPS 已保存");
        const targetId = saved.id || id;
        if (openTerminal) {
          delete dialog.dataset.editorDirty;
          form.reset();
          closeModal(dialog);
          if (S.page === "vps" || S.page === "projects")
            await renderPage(false).catch((e) => toast(e.message, true));
          if (
            saved.enabled !== false &&
            session === S.session &&
            space === S.space_id &&
            page === S.page
          ) {
            if (page !== "vps") await navigate("vps");
            if (
              session === S.session &&
              space === S.space_id &&
              S.page === "vps"
            )
              await vpsOpenTerminal(targetId);
          }
          return;
        }
        const check = vpsCheck(targetId, { dialog, saved, autoRun: true });
        if (S.page === "vps" || S.page === "projects")
          renderPage(false).catch((e) => toast(e.message, true));
        await check;
      } catch (e) {
        if (dialog.isConnected) {
          error.textContent =
            e.code === "NETWORK_UNCERTAIN"
              ? "网络中断，保存结果待确认。请刷新 VPS 列表。"
              : e.message;
          error.hidden = false;
        }
      } finally {
        if (peer?.isConnected) peer.disabled = false;
      }
    });
  };
  save.onclick = () => submit();
  if (saveTerminal) saveTerminal.onclick = () => submit({ openTerminal: true });
  form.onsubmit = (e) => {
    e.preventDefault();
    submit();
  };
  if (id)
    $("#vps-delete", dialog).onclick = async () => {
      const button = $("#vps-delete", dialog),
        intent = S.vpsIntent;
      if (button.disabled) return;
      const ok = await uiConfirmAction(
        {
          title: `删除“${v.name}”？`,
          body: "服务器会保留，连接和项目分配将移除。",
          confirmLabel: "删除连接",
          danger: true,
        },
        {
          owner: dialog,
          current: () => $(".modal") === dialog && S.vpsIntent === intent,
        },
      );
      if (!ok) return;
      return busy(button, async () => {
        await api("/api/vps/" + id + "?expected_version=" + v.version, {
          method: "DELETE",
        });
        form.reset();
        closeModal(dialog);
        toast("已删除连接");
        await renderPage(false);
      });
    };
}
async function vpsAssignProject(projectId) {
  const current = vpsIntent();
  const [items] = await Promise.all([vpsInventory(), loadBasics()]);
  if (!current()) return;
  const project = S.projects.find((p) => p.id === projectId);
  if (!project) throw new Error("项目已变化，请刷新");
  const selected = items
    .filter((v) => v.project_ids.includes(projectId))
    .map((v) => v.id);
  const dialog = modal(
    "项目 VPS · " + project.alias,
    `<p class="form-note">选择这个项目可使用的服务器。连接由 ${esc(project.device_name)} 发起；不会改变其他项目的分配。</p><div class="field"><label>分配 VPS（可多选）</label><div class="check-list vps-check-list">${items.map((v) => `<label class="check"><input type="checkbox" name="vps_ids" value="${esc(v.id)}" ${selected.includes(v.id) ? "checked" : ""}><span>${esc(v.name)}${v.enabled ? "" : " · 已停用"}<small>${esc(vpsEndpoint(v))} · ${esc(v.username)}</small></span></label>`).join("") || "<p>还没有 VPS，请先到 VPS 管理添加连接。</p>"}</div></div>`,
    buttons("project-vps-save", "保存分配"),
  );
  $("#project-vps-save", dialog).onclick = () =>
    busy($("#project-vps-save", dialog), async () => {
      await api("/api/projects/" + projectId + "/vps", {
        method: "PUT",
        body: JSON.stringify({
          vps_ids: $$('input[name="vps_ids"]:checked', dialog).map(
            (x) => x.value,
          ),
          expected_vps_ids: selected,
        }),
      });
      closeModal(dialog);
      toast("VPS 分配已更新");
      await renderPage(false);
    });
}
function vpsPaintCheckReceipt(receipt, success, output, status) {
  const result = receipt.result;
  const error = result?.error || receipt.error;
  const legacy =
    !success &&
    vpsSavedLegacyFailure(
      error?.code,
      result?.data?.ssh_error,
      receipt.ssh_error,
      receipt.error_code,
      receipt.reason_code,
    );
  const failure = legacy
    ? VPS_SAVED_UPGRADE_NOTICE
    : typeof error === "string"
      ? error
      : error?.message;
  status.textContent = success ? "连接检查成功" : failure || "检查未完成";
  output.textContent = legacy
    ? VPS_SAVED_UPGRADE_NOTICE
    : receipt.output ||
      result?.data?.output ||
      failure ||
      (success ? "连接检查完成，未返回输出。" : "检查已结束，未返回输出。");
}
async function vpsCheck(
  id,
  { dialog = null, saved = null, autoRun = false } = {},
) {
  const session = S.session,
    space = S.space_id,
    current = vpsIntent(),
    v = saved || (await api("/api/vps/" + encodeURIComponent(id)));
  if (!current() || (dialog && !dialog.isConnected)) return;
  const projects = vpsTerminalProjects(v),
    probeSupported = vpsSupportsConnection(v, "probe"),
    probeCode =
      v?.capabilities?.probe?.reason_code || "VPS_TRANSPORT_UPGRADE_REQUIRED",
    termReason = vpsSupportsConnection(v, "terminal")
      ? vpsTerminalReason(v)
      : vpsTransportMessage(v?.capabilities?.terminal?.reason_code),
    eligible = probeSupported && vpsCheckReady(v, projects);
  const title = "检查连接 · " + v.name;
  const initialState = !v.enabled
    ? "连接已停用。"
    : !probeSupported
      ? vpsTransportMessage(probeCode)
      : !eligible
        ? termReason || "当前无法执行连接检查。"
        : "等待检查。";
  const body = `<p class="form-note">${esc(vpsEndpoint(v))} · ${esc(v.username)}。读取主机名、系统和磁盘用量。</p>    <p id="vps-check-state" class="form-note" role="status">${esc(initialState)}</p><div class="code-box"><pre id="vps-check-output"></pre></div>`;
  const footer = `<button class="btn ghost" type="button" data-action="close-modal">关闭</button><button class="btn" type="button" data-vps-action="check-terminal" data-id="${esc(id)}" ${termReason ? "disabled" : ""}>打开终端</button><button class="btn primary" type="button" id="vps-check-run" ${eligible ? "" : "disabled"}>开始检查</button>`;
  if (dialog) {
    dialog.classList.remove("modal-editor", "vps-edit-dialog");
    delete dialog.dataset.presentation;
    delete dialog.dataset.editorDirty;
    delete dialog.dataset.editorForm;
    $("#modal-title", dialog).textContent = title;
    $(".modal-body", dialog).innerHTML = body;
    $(".modal-footer", dialog).innerHTML = footer;
    uiLabelFields(dialog);
    $("#vps-check-run", dialog).focus();
  } else dialog = modal(title, body, footer);
  const button = $("#vps-check-run", dialog),
    status = $("#vps-check-state", dialog),
    output = $("#vps-check-output", dialog);
  let checkKey = null,
    operation = null,
    finished = false;
  button.onclick = async () => {
    if (finished) {
      checkKey = null;
      operation = null;
      finished = false;
    }
    await busy(button, async () => {
      try {
        if (!operation) {
          if (!checkKey) checkKey = "vps-check-" + uid();
          status.textContent = "正在检查…";
          const result = await api(
            "/api/vps/" + encodeURIComponent(id) + "/check",
            {
              method: "POST",
              body: JSON.stringify({
                timeout_seconds: 30,
                idempotency_key: checkKey,
              }),
            },
          );
          if (
            !dialog.isConnected ||
            session !== S.session ||
            space !== S.space_id
          )
            return;
          if (result.pending && result.operation_id) {
            operation = result.operation_id;
          } else {
            const success =
              result.state === "succeeded" ||
              (result.ok === true && !result.state);
            vpsPaintCheckReceipt(result, success, output, status);
            finished = true;
            return;
          }
        }
        if (!dialog.isConnected) return;
        status.textContent = "正在检查…";
        while (
          dialog.isConnected &&
          session === S.session &&
          space === S.space_id
        ) {
          const op = await tool("operations_wait", {
            operation_id: operation,
            wait_seconds: 8,
            output_limit: 12000,
          });
          if (
            !dialog.isConnected ||
            session !== S.session ||
            space !== S.space_id
          )
            return;
          if (op.pending) {
            status.textContent = "正在检查…";
            output.textContent = "等待连接电脑返回…";
          } else {
            finished = true;
            vpsPaintCheckReceipt(op, op.state === "succeeded", output, status);
            return;
          }
        }
      } catch (e) {
        if (e.operation_id) operation = e.operation_id;
        if (
          dialog.isConnected &&
          session === S.session &&
          space === S.space_id
        ) {
          status.textContent = vpsSavedLegacyFailure(e.code, e.reason_code)
            ? VPS_SAVED_UPGRADE_NOTICE
            : e.message || "检查未完成。";
        }
      }
    });
    if (dialog.isConnected) {
      button.textContent = finished ? "重新检查" : "重试";
      button.disabled = false;
    }
  };
  if (autoRun && eligible) button.click();
}
document.addEventListener("click", async (e) => {
  const layoutBtn = e.target.closest("[data-vps-layout]");
  if (layoutBtn) {
    const value = layoutBtn.dataset.vpsLayout;
    if (["cards", "list"].includes(value)) {
      S.vpsLayout = value;
      const grid = $(".vps-grid");
      if (grid) grid.dataset.layout = value;
      $$("[data-vps-layout]").forEach((item) => {
        const active = item.dataset.vpsLayout === value;
        item.classList.toggle("active", active);
        item.setAttribute("aria-pressed", String(active));
      });
      try {
        localStorage.setItem("codepier-vps-layout", value);
      } catch {}
    }
    return;
  }
  const button = e.target.closest("[data-vps-action]");
  if (!button || button.disabled) return;
  try {
    switch (button.dataset.vpsAction) {
      case "toggle-pin":
        vpsTogglePin(button);
        break;
      case "terminal":
        await vpsOpenTerminal(button.dataset.id, button);
        break;
      case "terminal-new":
        await vpsOpenTerminal(button.dataset.id, button, { newSession: true });
        break;
      case "resume-session": {
        const sid = button.dataset.session;
        await navigate(
          sid
            ? "vps?view=terminal&session=" + encodeURIComponent(sid)
            : "vps?view=terminal",
        );
        break;
      }
      case "sessions":
        await navigate("vps?view=terminal");
        break;
      case "add":
        await vpsEdit();
        break;
      case "edit":
        await vpsEdit(button.dataset.id);
        break;
      case "readiness-backfill":
        await vpsReadinessBackfillStart();
        break;
      case "readiness-backfill-install":
        await vpsReadinessBackfillStart(true);
        break;
      case "readiness":
        await vpsReadinessWatch(button.dataset.id, true);
        break;
      case "check":
        await vpsCheck(button.dataset.id);
        break;
      case "check-terminal": {
        const activeModal = button.closest(".modal");
        if (activeModal) closeModal(activeModal);
        if (S.page !== "vps") await navigate("vps");
        await vpsOpenTerminal(button.dataset.id);
        break;
      }
      case "copy": {
        const v = S.vps?.find((item) => item.id === button.dataset.id);
        if (v) await copy(vpsEndpoint(v));
        break;
      }
      case "copy-ssh": {
        const v = S.vps?.find((item) => item.id === button.dataset.id);
        if (v) {
          await copy(vpsSshCommand(v), "已复制 SSH 命令");
          if (typeof toast === "function") toast("已复制 SSH 命令");
        }
        break;
      }
    }
  } catch (error) {
    toast(error.message, true);
  }
});
