import { errorCode, normalizeUuid, reasonText } from "./protocol.mjs";

const STAGES = [
  ["ssh_connect", "SSH 连接与认证"],
  ["pty_open", "PTY 打开"],
  ["hub_prepare", "Hub 准备"],
  ["attach", "终端附着"],
];
const COUNTERS = [
  ["reconnect_total", "重连次数"],
  ["history_gap_total", "历史缺口"],
  ["input_uncertain_total", "输入未确认"],
  ["ssh_exit_total", "SSH 退出"],
  ["protocol_reject_total", "协议拒绝"],
  ["backpressure_total", "输出背压次数"],
];
const GAUGES = [
  ["browser_buffer_bytes", "浏览器输出队列"],
  ["agent_queue_bytes", "连接电脑输出队列"],
  ["ssh_ring_bytes", "SSH 历史缓冲"],
  ["ssh_outstanding_bytes", "SSH 未消费输出"],
];
const finite = (value) =>
  typeof value === "number" && Number.isFinite(value) && value >= 0;
const count = (value) => Number.isSafeInteger(value) && value >= 0;

function bytes(value) {
  let number;
  if (count(value)) number = BigInt(value);
  else if (typeof value === "string" && /^[0-9]{1,20}$/.test(value))
    number = BigInt(value);
  else return "未采集";
  if (number > (1n << 64n) - 1n) return "未采集";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"];
  let scale = 1n,
    unit = 0;
  while (number >= scale * 1024n && unit < units.length - 1) {
    scale *= 1024n;
    unit++;
  }
  return unit === 0
    ? `${number} B`
    : `${number / scale}.${((number % scale) * 10n) / scale} ${units[unit]}`;
}

function duration(value) {
  return finite(value) ? `${Math.round(value * 10) / 10} ms` : "未采集";
}

function link(label, value, descriptions) {
  const status = Object.hasOwn(descriptions, value?.status)
    ? descriptions[value.status]
    : ["未确认", "unknown"];
  return { label, status: status[0], tone: status[1] };
}

// Pick documented diagnostics fields explicitly. Extra payload keys never enter
// the UI; all display values remain text, including server version strings.
export function formatDiagnostics(payload, sessionId) {
  let sameSession = false;
  try {
    sameSession =
      normalizeUuid(payload?.session_id) === normalizeUuid(sessionId);
  } catch {
    /* A malformed diagnostic identity has no authority over this pane. */
  }
  if (!sameSession || payload.protocol_version !== 1)
    throw Object.assign(new Error("诊断结果不属于当前会话或版本不兼容"), {
      code: "PROTOCOL_MISMATCH",
    });
  const links = [
    link("浏览器 ↔ Hub", payload.links?.browser, {
      connected: ["已连接", "connected"],
      detached: ["已分离", "disconnected"],
    }),
    link("Hub ↔ 连接电脑", payload.links?.agent, {
      connected: ["已连接", "connected"],
      offline: ["离线", "disconnected"],
      unconfirmed: ["未确认", "unknown"],
    }),
    link("连接电脑 ↔ VPS", payload.links?.ssh, {
      connected: ["已连接", "connected"],
      disconnected: ["已断开", "disconnected"],
      unconfirmed: ["未确认", "unknown"],
    }),
  ];
  const rows = [
    ["输入", bytes(payload.bytes?.input)],
    ["输出", bytes(payload.bytes?.output)],
    ["本次控制往返", duration(payload.links?.agent?.control_rtt_ms)],
  ];
  const details = [];
  for (const [key, label] of STAGES)
    if (finite(payload.stages_ms?.[key]))
      details.push([label, duration(payload.stages_ms[key])]);
  if (!details.length) details.push(["阶段耗时", "未采集"]);
  for (const [key, label] of COUNTERS)
    if (count(payload.metrics?.counters?.[key]))
      details.push([label, String(payload.metrics.counters[key])]);
  for (const [key, label] of GAUGES)
    if (count(payload.metrics?.gauges?.[key]))
      details.push([label, bytes(payload.metrics.gauges[key])]);
  if (finite(payload.metrics?.durations_ms?.backpressure))
    details.push([
      "输出背压累计",
      duration(payload.metrics.durations_ms.backpressure),
    ]);
  if (count(payload.lifecycle?.recover_count))
    details.push(["持久会话恢复", String(payload.lifecycle.recover_count)]);
  for (const [key, label] of [
    ["hub", "Hub 版本"],
    ["agent", "连接电脑版本"],
  ]) {
    const version = payload.lifecycle?.version?.[key];
    details.push([
      label,
      typeof version === "string" && version.length <= 120 ? version : "未采集",
    ]);
  }
  details.push(["终端协议", String(payload.protocol_version)]);
  const reason =
    typeof payload.reason_code === "string" &&
    /^[A-Z_0-9]{1,64}$/.test(payload.reason_code)
      ? reasonText(payload.reason_code, payload.reason_code)
      : "";
  return {
    links,
    rows,
    details,
    reason,
    collectedAt:
      finite(payload.collected_at) && payload.collected_at <= 8640000000000
        ? new Date(payload.collected_at * 1000).toLocaleTimeString()
        : null,
  };
}

// A late refresh must not repopulate a closed pane or an old user's workspace.
export class DiagnosticsRequest {
  constructor({ sessionId, load, onResult, onError, onLoading }) {
    Object.assign(this, { sessionId, load, onResult, onError, onLoading });
    this.version = 0;
    this.disposed = false;
  }
  async refresh() {
    if (this.disposed) return;
    const version = ++this.version;
    this.onLoading?.(true);
    try {
      const result = await this.load();
      if (this.disposed || this.version !== version) return;
      this.onResult?.(formatDiagnostics(result, this.sessionId));
    } catch (error) {
      if (!this.disposed && this.version === version) this.onError?.(error);
    } finally {
      if (!this.disposed && this.version === version) this.onLoading?.(false);
    }
  }
  cancel() {
    this.version++;
    if (!this.disposed) this.onLoading?.(false);
  }
  dispose() {
    this.cancel();
    this.disposed = true;
    this.load = null;
    this.onResult = this.onError = this.onLoading = null;
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export class DiagnosticsPanel {
  constructor({ sessionId, load, onResize }) {
    this.onResize = onResize;
    this.root = el("section", "vt-diagnostics");
    this.root.hidden = true;
    this.root.setAttribute("role", "region");
    this.root.setAttribute("aria-label", "当前会话连接诊断");
    const heading = el("div", "vt-diagnostics-heading");
    this.refreshButton = el("button", "vt-button", "刷新");
    this.refreshButton.type = "button";
    this.refreshButton.addEventListener("click", () => this.request.refresh());
    const close = el("button", "vt-button", "收起");
    close.type = "button";
    close.addEventListener("click", () => this.toggle(false));
    heading.append(el("strong", "", "连接诊断"), this.refreshButton, close);
    this.status = el("p", "vt-diagnostics-status");
    this.status.setAttribute("role", "status");
    this.content = el("div", "vt-diagnostics-content");
    this.root.append(heading, this.status, this.content);
    this.request = new DiagnosticsRequest({
      sessionId,
      load,
      onLoading: (loading) => {
        this.root.setAttribute("aria-busy", String(loading));
        this.refreshButton.disabled = loading;
        if (loading) {
          this.status.textContent = "正在读取连接状态…";
          this.content.replaceChildren();
        }
        this.onResize?.();
      },
      onResult: (result) => this.render(result),
      onError: (error) => {
        this.status.textContent = reasonText(
          errorCode(error),
          error.message || "暂时无法读取诊断",
        );
        this.content.replaceChildren();
      },
    });
  }
  toggle(force = this.root.hidden) {
    this.root.hidden = !force;
    if (force) this.request.refresh();
    else {
      this.request.cancel();
      this.content.replaceChildren();
      this.status.textContent = "";
    }
    this.onResize?.();
  }
  render(result) {
    this.status.textContent = result.collectedAt
      ? `状态快照 · ${result.collectedAt}`
      : "状态快照";
    const links = el("div", "vt-diagnostics-links");
    for (const item of result.links) {
      const box = el("div", "vt-diagnostics-link");
      box.dataset.status = item.tone;
      box.append(el("span", "", item.label), el("strong", "", item.status));
      links.append(box);
    }
    const makeRows = (rows) => {
      const list = el("dl", "vt-diagnostics-values");
      for (const [label, value] of rows)
        list.append(el("dt", "", label), el("dd", "", value));
      return list;
    };
    const more = el("details", "vt-diagnostics-details");
    more.append(
      el("summary", "", "耗时、缓冲与版本"),
      makeRows(result.details),
    );
    more.addEventListener("toggle", () => this.onResize?.());
    this.content.replaceChildren(links, makeRows(result.rows));
    if (result.reason)
      this.content.append(el("p", "vt-diagnostics-reason", result.reason));
    this.content.append(
      el(
        "p",
        "vt-diagnostics-note",
        "控制往返是本次 Hub 与连接电脑的状态查询耗时。",
      ),
      more,
    );
    this.onResize?.();
  }
  dispose() {
    this.request.dispose();
    this.onResize = null;
    this.root.replaceChildren();
    this.root.remove();
  }
}
