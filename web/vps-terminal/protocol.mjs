// Wire contract shared with shared/ssh_terminal_protocol.py. Counts are bytes,
// never JavaScript string lengths; JSON counters remain decimal strings.
export const PROTOCOL_VERSION = 1;
export const HEADER_BYTES = 62;
export const INPUT = 1;
export const OUTPUT = 2;
export const MAX_COUNTER = (1n << 64n) - 1n;
export const DEFAULT_LIMITS = Object.freeze({
  frame_bytes: 16384,
  input_frame_bytes: 16384,
  control_bytes: 8192,
  pending_input_bytes: 65536,
  output_high_water_bytes: 262144,
  output_low_water_bytes: 65536,
  output_pending_bytes: 524288,
  paste_bytes: 262144,
  resize_coalesce_ms: 60,
  heartbeat_seconds: 15,
  auth_timeout_seconds: 5,
  heartbeat_timeout_seconds: 45,
  reconnect_max_ms: 8000,
  max_user_sessions: 8,
  min_cols: 2,
  max_cols: 1000,
  min_rows: 1,
  max_rows: 500,
  scrollback_lines: 10000,
});

export function normalizeUuid(value) {
  if (
    typeof value !== 'string' ||
    !/^(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i.test(value)
  ) {
    throw new Error('Invalid terminal UUID');
  }
  return value.replaceAll('-', '').toLowerCase();
}

export function counter(value, name = 'counter') {
  if (typeof value === 'number' && !Number.isSafeInteger(value)) throw new Error(`Invalid ${name}`);
  if (
    !['bigint', 'string', 'number'].includes(typeof value) ||
    !/^(0|[1-9][0-9]*)$/.test(String(value))
  ) {
    throw new Error(`Invalid ${name}`);
  }
  const parsed = BigInt(value);
  if (parsed < 0n || parsed > MAX_COUNTER) throw new Error(`Invalid ${name}`);
  return parsed;
}

function uuidInto(bytes, at, value) {
  const hex = normalizeUuid(value);
  for (let i = 0; i < 16; i++) bytes[at + i] = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);
}

function uuidFrom(bytes, at) {
  return Array.from(bytes.subarray(at, at + 16), (v) => v.toString(16).padStart(2, '0')).join('');
}

export function encodeFrame(
  { kind, session_id, runtime_generation, stream_epoch, writer_epoch, sequence, payload },
  maxPayload = DEFAULT_LIMITS.frame_bytes,
) {
  if (
    ![INPUT, OUTPUT].includes(kind) ||
    !(payload instanceof Uint8Array) ||
    payload.byteLength > maxPayload ||
    !payload.byteLength
  ) {
    throw new Error('Invalid terminal frame');
  }
  const count = counter(sequence);
  if (
    (kind === INPUT && count === 0n) ||
    (kind === OUTPUT && count + BigInt(payload.byteLength) > MAX_COUNTER)
  )
    throw new Error('Invalid terminal sequence');
  const bytes = new Uint8Array(HEADER_BYTES + payload.byteLength),
    view = new DataView(bytes.buffer);
  bytes[0] = PROTOCOL_VERSION;
  bytes[1] = kind;
  uuidInto(bytes, 2, session_id);
  uuidInto(bytes, 18, runtime_generation);
  view.setBigUint64(34, counter(stream_epoch), false);
  view.setBigUint64(42, counter(writer_epoch), false);
  view.setBigUint64(50, count, false);
  view.setUint32(58, payload.byteLength, false);
  bytes.set(payload, HEADER_BYTES);
  return bytes.buffer;
}

export function decodeFrame(value, maxPayload = DEFAULT_LIMITS.frame_bytes) {
  const bytes =
    value instanceof Uint8Array
      ? value
      : value instanceof ArrayBuffer
        ? new Uint8Array(value)
        : null;
  if (!bytes || bytes.byteLength < HEADER_BYTES) throw new Error('Truncated terminal frame');
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength),
    length = view.getUint32(58, false);
  if (
    bytes[0] !== PROTOCOL_VERSION ||
    ![INPUT, OUTPUT].includes(bytes[1]) ||
    !length ||
    length > maxPayload ||
    HEADER_BYTES + length !== bytes.byteLength
  ) {
    throw new Error('Invalid terminal frame');
  }
  const sequence = view.getBigUint64(50, false);
  if (bytes[1] === INPUT && sequence === 0n) throw new Error('Invalid input sequence');
  if (bytes[1] === OUTPUT && sequence + BigInt(length) > MAX_COUNTER)
    throw new Error('Terminal offset overflow');
  return {
    kind: bytes[1],
    session_id: uuidFrom(bytes, 2),
    runtime_generation: uuidFrom(bytes, 18),
    stream_epoch: view.getBigUint64(34, false),
    writer_epoch: view.getBigUint64(42, false),
    sequence,
    payload: bytes.subarray(HEADER_BYTES),
  };
}

export function limitsFrom(values = {}) {
  const result = { ...DEFAULT_LIMITS };
  const ranges = {
    frame_bytes: [256, 65536],
    input_frame_bytes: [256, 65536],
    control_bytes: [1024, 65536],
    pending_input_bytes: [4096, 1048576],
    output_high_water_bytes: [4096, 4194304],
    output_low_water_bytes: [0, 4194304],
    output_pending_bytes: [4096, 8388608],
    paste_bytes: [4096, 4194304],
    resize_coalesce_ms: [0, 1000],
    heartbeat_seconds: [1, 120],
    heartbeat_timeout_seconds: [3, 360],
    auth_timeout_seconds: [1, 30],
    reconnect_max_ms: [500, 60000],
    max_user_sessions: [1, 128],
    min_cols: [1, 1000],
    max_cols: [1, 1000],
    min_rows: [1, 500],
    max_rows: [1, 500],
    scrollback_lines: [0, 100000],
  };
  const invalid = () => {
    throw Object.assign(new Error('终端配置与当前界面不兼容'), {
      code: 'PROTOCOL_MISMATCH',
    });
  };
  for (const [key, [min, max]] of Object.entries(ranges)) {
    if (values[key] === undefined) continue;
    const value = values[key],
      time = ['heartbeat_seconds', 'heartbeat_timeout_seconds', 'auth_timeout_seconds'].includes(
        key,
      );
    if (
      typeof value !== 'number' ||
      !Number.isFinite(value) ||
      (!time && !Number.isSafeInteger(value)) ||
      value < min ||
      value > max
    )
      invalid();
    result[key] = value;
  }
  if (values.output_pending_bytes === undefined)
    result.output_pending_bytes = result.output_high_water_bytes + result.frame_bytes;
  if (values.heartbeat_timeout_seconds === undefined)
    result.heartbeat_timeout_seconds = result.heartbeat_seconds * 3;
  if (
    result.output_low_water_bytes >= result.output_high_water_bytes ||
    result.frame_bytes > result.output_high_water_bytes ||
    result.output_pending_bytes < result.output_high_water_bytes + result.frame_bytes ||
    result.pending_input_bytes < result.input_frame_bytes ||
    result.paste_bytes < result.input_frame_bytes ||
    result.min_cols > result.max_cols ||
    result.min_rows > result.max_rows ||
    result.heartbeat_timeout_seconds <= result.heartbeat_seconds
  )
    invalid();
  return result;
}

export function binaryInput(value) {
  const bytes = new Uint8Array(value.length);
  for (let i = 0; i < value.length; i++) bytes[i] = value.charCodeAt(i) & 255;
  return bytes;
}

export function uuid() {
  return globalThis.crypto.randomUUID().replaceAll('-', '');
}

export const STOP_RETRY_CODES = new Set([
  'AUTH_REVOKED',
  'AUTH_REQUIRED',
  'LOGIN_REQUIRED',
  'SESSION_EXPIRED',
  'UNAUTHORIZED',
  'FORBIDDEN',
  'TARGET_REVISION_CHANGED',
  'VPS_DISABLED',
  'VPS_NOT_ASSIGNED',
  'NO_TERMINAL_AGENT',
  'SHELL_DISABLED',
  'SHELL_NOT_ALLOWED',
  'VPS_AUTHORIZATION_CHANGED',
  'VPS_CONFIGURATION_CHANGED',
  'SSH_AUTH_FAILED',
  'HOST_KEY_UNKNOWN',
  'HOST_KEY_CHANGED',
  'AGENT_UPGRADE_REQUIRED',
  'PROTOCOL_MISMATCH',
  'REMOTE_SESSION_MISSING',
  'HISTORY_GAP',
  'WRITER_CONFLICT',
  'WRITER_REPLACED',
  'SESSION_NOT_FOUND',
  'SESSION_CLOSED',
  'SESSION_LIMIT',
  'INVALID_FRAME',
  'INPUT_GAP',
  'SESSION_ENDED',
  'SESSION_NOT_RECOVERABLE',
  'STALE_WRITER',
  'SSH_DEPENDENCY_MISSING',
  'AUTH_CHALLENGE_EXPIRED',
  'PERSISTENCE_UNAVAILABLE',
  'FEATURE_DISABLED',
]);

const REASONS = {
  AUTH_REVOKED: '操作权限已撤销',
  AUTH_REQUIRED: '请重新登录后连接',
  LOGIN_REQUIRED: '请重新登录后连接',
  SESSION_EXPIRED: '登录已过期',
  UNAUTHORIZED: '请重新登录后连接',
  FORBIDDEN: '此账号没有终端权限',
  TARGET_REVISION_CHANGED: '连接配置已更新，请新建终端',
  VPS_DISABLED: 'VPS 连接已停用',
  VPS_NOT_ASSIGNED: '当前账号暂无此 VPS 的连接权限',
  NO_TERMINAL_AGENT: '暂无可用的连接电脑，请先启动连接电脑',
  SHELL_DISABLED: '连接电脑尚未启用终端命令功能',
  SHELL_NOT_ALLOWED: '连接电脑未允许打开此终端',
  VPS_AUTHORIZATION_CHANGED: '服务器访问授权已更新，请新建终端',
  VPS_CONFIGURATION_CHANGED: '服务器连接配置已更新，请新建终端',
  AGENT_OFFLINE: '连接电脑离线',
  AGENT_UPGRADE_REQUIRED: '请更新连接电脑后打开终端',
  FEATURE_DISABLED: '管理员已暂停新建终端',
  SSH_CONNECT_TIMEOUT: 'SSH 连接超时',
  SSH_AUTH_FAILED: 'SSH 登录失败，请检查连接凭据',
  HOST_KEY_UNKNOWN: '主机指纹尚未受信任',
  HOST_KEY_CHANGED: '主机指纹已变化，请核验服务器',
  SESSION_LIMIT: '已达到终端会话上限',
  HISTORY_GAP: '无法完整恢复原画面，请新建终端',
  INPUT_UNCERTAIN: '有输入未确认，未自动重发',
  INPUT_GAP: '输入顺序已中断，请重新连接',
  INPUT_BACKPRESSURE: '输入暂未发送，请等待远端接收后再输入',
  REMOTE_SESSION_MISSING: '原远端会话已不存在',
  OUTPUT_INCOMPLETE: '连接结束，部分输出未完整收到',
  PROTOCOL_MISMATCH: '终端版本不兼容，请更新面板和连接电脑',
  SSH_CHANNEL_CLOSED: 'SSH 连接已断开',
  WRITER_CONFLICT: '此终端已在另一窗口打开',
  WRITER_REPLACED: '此终端已由另一窗口接管',
  SESSION_NOT_FOUND: '终端会话已不存在',
  SESSION_CLOSED: '终端会话已结束',
  CLOSE_UNKNOWN: '结束结果尚未确认，请刷新状态',
  INVALID_FRAME: '终端连接收到无效数据',
  OUTPUT_BACKPRESSURE: '终端输出过快，正在重新连接',
  NETWORK_ERROR: '网络连接中断',
  SESSION_ENDED: '原会话已经结束',
  SESSION_NOT_RECOVERABLE: '原 SSH 连接已结束，请新建终端',
  STALE_WRITER: '终端已由另一窗口接管',
  SSH_DEPENDENCY_MISSING: '请更新连接电脑后打开终端',
  AUTH_CHALLENGE_EXPIRED: '认证问题已过期，请重新连接',
  PERSISTENCE_UNAVAILABLE: '此 VPS 暂不支持持久会话',
  SSH_CONNECTION_FAILED: 'SSH 连接失败，请检查服务器和网络',
  SSH_CONNECT_FAILED: '面板未能启动 SSH 会话，请打开连接诊断',
  SSH_TERMINAL_FORBIDDEN: '连接电脑未授权此项目运行终端',
  HOST_KEY_REVOKED: '主机指纹已被撤销，请通过可信渠道核验',
  HOST_TRUST_CONFIGURATION: '连接电脑的 SSH 信任配置无法用于网页终端',
  HOST_TRUST_UNAVAILABLE: '连接电脑无法读取或更新 SSH 信任记录',
  HOST_TRUST_BUSY: '连接电脑正在更新 SSH 信任记录，请稍后重试',
  PERSISTENCE_PROBE_FAILED: '无法确认远端会话恢复能力，请查看连接诊断',
  TERMINAL_CONFIG_MISMATCH: '面板与连接电脑的终端配置不匹配',
  VPS_CREDENTIAL_UNAVAILABLE: '面板无法读取已保存的连接凭据，请检查 VPS 连接设置',
  PANEL_UPDATING: '面板正在维护，请等待更新完成后重试',
  AGENT_MAINTENANCE: '连接电脑正在维护，请等待更新完成后重试',
};

export function reasonText(code, fallback = '') {
  if (Object.hasOwn(REASONS, code)) return REASONS[code];
  if (typeof fallback === 'string' && fallback.length > 0 && fallback.length <= 240)
    return fallback;
  const publicCode = typeof code === 'string' && /^[A-Z][A-Z0-9_]{0,63}$/.test(code);
  return publicCode ? `终端连接暂不可用（${code}）` : '终端连接暂不可用';
}

export function errorCode(error) {
  return (
    error?.reason_code ||
    error?.code ||
    (error?.status === 401
      ? 'AUTH_REQUIRED'
      : error?.status === 403
        ? 'FORBIDDEN'
        : 'NETWORK_ERROR')
  );
}
