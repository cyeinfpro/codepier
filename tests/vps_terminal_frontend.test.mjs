import test from 'node:test';
import assert from 'node:assert/strict';
import {
  INPUT,
  OUTPUT,
  HEADER_BYTES,
  binaryInput,
  counter,
  decodeFrame,
  encodeFrame,
  normalizeUuid,
  limitsFrom,
  reasonText,
} from '../web/vps-terminal/protocol.mjs';
import { TerminalTransport, websocketUrl } from '../web/vps-terminal/transport.mjs';
import { pasteChunks, pasteInChunks, pasteSummary } from '../web/vps-terminal/paste.mjs';
import { TerminalStore } from '../web/vps-terminal/store.mjs';
import { DiagnosticsRequest, formatDiagnostics } from '../web/vps-terminal/diagnostics.mjs';

const SESSION = '12345678123456781234567812345678',
  GENERATION = 'abcdefabcdefabcdefabcdefabcdefab';
const encoder = new TextEncoder();
const turn = () => new Promise((resolve) => setTimeout(resolve, 0));

class Socket {
  static instances = [];
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    this.bufferedAmount = 0;
    Socket.instances.push(this);
  }
  send(data) {
    if (this.readyState !== 1) throw new Error('closed');
    this.sent.push(data);
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  receive(value) {
    this.onmessage?.({
      data:
        typeof value === 'object' && !(value instanceof ArrayBuffer)
          ? JSON.stringify(value)
          : value,
    });
  }
  close(code = 1000) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
  controls() {
    return this.sent.filter((item) => typeof item === 'string').map(JSON.parse);
  }
  frames() {
    return this.sent.filter((item) => item instanceof ArrayBuffer).map((item) => decodeFrame(item));
  }
}
function attached(extra = {}) {
  return {
    type: 'attached',
    session_id: SESSION,
    runtime_generation: GENERATION,
    attachment_id: '11111111111111111111111111111111',
    render_epoch: '1',
    stream_epoch: '1',
    writer_epoch: '1',
    start_offset: '0',
    next_input_seq: '1',
    mode: 'tmux',
    resume_strategy: 'live',
    state: 'ready',
    ...extra,
  };
}
function output(offset, value, extra = {}) {
  return encodeFrame({
    kind: OUTPUT,
    session_id: SESSION,
    runtime_generation: GENERATION,
    stream_epoch: 1n,
    writer_epoch: 1n,
    sequence: BigInt(offset),
    payload: typeof value === 'string' ? encoder.encode(value) : value,
    ...extra,
  });
}
async function fixture(callbacks = {}, options = {}) {
  const requests = [],
    states = [],
    notices = [];
  const transport = new TerminalTransport({
    session: { session_id: SESSION },
    WebSocketImpl: Socket,
    baseUrl: 'https://panel.example.test/vps',
    random: () => 0,
    api: async (path, args) => {
      requests.push({ path, body: JSON.parse(args.body) });
      return { ticket: 'short-lived-ticket', ws_path: '/ws/vps-terminal' };
    },
    callbacks: {
      onState: (value) => states.push(value),
      onNotice: (value) => notices.push(value),
      ...callbacks,
    },
    ...options,
  });
  await transport.connect();
  const socket = Socket.instances.at(-1);
  socket.open();
  return { transport, socket, requests, states, notices };
}

function diagnostics(extra = {}) {
  return {
    session_id: SESSION,
    protocol_version: 1,
    collected_at: 1805800000,
    links: {
      browser: { connected: true, status: 'connected' },
      agent: { connected: true, status: 'connected', control_rtt_ms: 0 },
      ssh: { connected: null, status: 'unconfirmed' },
    },
    bytes: { input: '0', output: '1024' },
    stages_ms: { ssh_connect: 120.4, credential: 'must-not-render' },
    metrics: {
      counters: { input_uncertain_total: 0, private_token: 'must-not-render' },
      gauges: { browser_buffer_bytes: 0 },
      durations_ms: { backpressure: 0 },
    },
    lifecycle: { recover_count: 0, version: { hub: 'fixture', agent: null, protocol: 1 } },
    ...extra,
  };
}

test('diagnostics distinguish unconfirmed and zero values while excluding undocumented fields', () => {
  const result = formatDiagnostics(diagnostics(), SESSION);
  assert.equal(result.links[2].status, '未确认');
  assert.deepEqual(result.rows, [
    ['输入', '0 B'],
    ['输出', '1.0 KiB'],
    ['本次控制往返', '0 ms'],
  ]);
  assert(
    result.details.some(([label, value]) => label === 'SSH 连接与认证' && value === '120.4 ms'),
  );
  assert(!result.details.some(([label]) => label === 'PTY 打开'));
  assert(!JSON.stringify(result).includes('must-not-render'));
  const missing = formatDiagnostics(
    diagnostics({ links: { agent: { status: 'unconfirmed', control_rtt_ms: null } }, bytes: {} }),
    SESSION,
  );
  assert.equal(missing.rows[2][1], '未采集');
  assert.equal(missing.rows[0][1], '未采集');
  assert.equal(missing.links[0].status, '未确认');
  assert.throws(() => formatDiagnostics(diagnostics({ session_id: GENERATION }), SESSION), {
    code: 'PROTOCOL_MISMATCH',
  });
  assert.throws(() => formatDiagnostics({}, SESSION), { code: 'PROTOCOL_MISMATCH' });
});

test('late diagnostic responses cannot overwrite a newer refresh or repopulate a closed identity', async () => {
  const pending = [],
    results = [],
    errors = [];
  const request = new DiagnosticsRequest({
    sessionId: SESSION,
    load: () => new Promise((resolve) => pending.push(resolve)),
    onResult: (value) => results.push(value),
    onError: (error) => errors.push(error),
  });
  const first = request.refresh();
  const second = request.refresh();
  pending[1](diagnostics());
  await second;
  pending[0](diagnostics({ session_id: GENERATION }));
  await first;
  assert.equal(results.length, 1);
  assert.equal(errors.length, 0);
  const third = request.refresh();
  request.cancel();
  pending[2](diagnostics());
  await third;
  const fourth = request.refresh();
  request.dispose();
  pending[3](diagnostics());
  await fourth;
  assert.equal(results.length, 1);
  assert.equal(errors.length, 0);
});

test('wire header and counters preserve bytes and offsets above Number precision', () => {
  const payload = encoder.encode('中文 🕊️\x1b[31m');
  const frame = encodeFrame({
    kind: OUTPUT,
    session_id: '12345678-1234-5678-1234-567812345678',
    runtime_generation: GENERATION,
    stream_epoch: (1n << 53n) + 1n,
    writer_epoch: 22n,
    sequence: (1n << 54n) + 3n,
    payload,
  });
  assert.equal(frame.byteLength, HEADER_BYTES + payload.length);
  // Golden bytes generated by the project's Python TerminalFrame encoder.
  assert.equal(
    Buffer.from(frame).toString('hex'),
    '010212345678123456781234567812345678abcdefabcdefabcdefabcdefabcdefab00200000000000010000000000000016004000000000000300000013e4b8ade6968720f09f958aefb88f1b5b33316d',
  );
  assert.deepEqual(decodeFrame(frame).payload, payload);
  assert.equal(decodeFrame(frame).sequence, (1n << 54n) + 3n);
  assert.equal(decodeFrame(frame).stream_epoch, (1n << 53n) + 1n);
  assert.equal(decodeFrame(frame).session_id, SESSION);
  assert.throws(() => counter(Number.MAX_SAFE_INTEGER + 1));
  assert.throws(() => normalizeUuid('xx' + SESSION));
  const broken = new Uint8Array(frame);
  broken[0] = 99;
  assert.throws(() => decodeFrame(broken));
  assert.throws(() => decodeFrame(new Uint8Array(frame).subarray(0, -1)));
  assert.deepEqual(binaryInput('\x80\xff\x00'), Uint8Array.of(128, 255, 0));
  assert.throws(() =>
    encodeFrame({
      kind: INPUT,
      session_id: SESSION,
      runtime_generation: GENERATION,
      stream_epoch: 1n,
      writer_epoch: 1n,
      sequence: 0n,
      payload: Uint8Array.of(1),
    }),
  );
});

test('legal server capacity increases are preserved and incompatible bounds fail explicitly', () => {
  const limits = limitsFrom({
    frame_bytes: 65536,
    output_high_water_bytes: 4194304,
    output_low_water_bytes: 0,
    pending_input_bytes: 1048576,
    paste_bytes: 4194304,
    heartbeat_seconds: 1.5,
    resize_coalesce_ms: 0,
    scrollback_lines: 0,
  });
  assert.equal(limits.output_pending_bytes, 4259840);
  assert.equal(limits.paste_bytes, 4194304);
  assert.equal(limits.heartbeat_timeout_seconds, 4.5);
  assert.equal(limits.scrollback_lines, 0);
  assert.throws(() => limitsFrom({ frame_bytes: 1000000 }), { code: 'PROTOCOL_MISMATCH' });
  assert.throws(() => limitsFrom({ output_low_water_bytes: 262144 }), {
    code: 'PROTOCOL_MISMATCH',
  });
});

test('websocket endpoint is same-origin and cannot carry a ticket in its URL', () => {
  assert.equal(
    websocketUrl('/ws/vps-terminal', 'https://panel.test/path'),
    'wss://panel.test/ws/vps-terminal',
  );
  assert.throws(() => websocketUrl('https://other.test/ws', 'https://panel.test/path'));
  assert.throws(() => websocketUrl('/ws?ticket=secret', 'https://panel.test/path'));
  assert.throws(() => websocketUrl('/ws', 'http://public.test/path'));
});

test('output ACK and live input wait until xterm has consumed the recovery barrier', async (t) => {
  let finishWrite;
  const rendered = [];
  const f = await fixture({
    onOutput: (bytes) => {
      rendered.push(bytes);
      return new Promise((resolve) => {
        finishWrite = resolve;
      });
    },
  });
  t.after(() => f.transport.dispose());
  f.socket.receive(attached({ resume_strategy: 'redraw', state: 'restoring' }));
  f.socket.receive(output(0, '中文'));
  f.socket.receive({
    type: 'replay_complete',
    attachment_id: '11111111111111111111111111111111',
    render_epoch: '1',
    stream_epoch: '1',
    final_offset: '6',
  });
  await turn();
  assert.equal(f.transport.canInput, false);
  assert.equal(
    f.socket.controls().filter((x) => x.type === 'output_ack' && x.rendered_offset !== '0').length,
    0,
  );
  assert.equal(rendered[0].byteLength, 6);
  finishWrite();
  await turn();
  assert.equal(f.transport.canInput, true);
  assert.equal(
    f.socket
      .controls()
      .filter((x) => x.type === 'output_ack')
      .at(-1).rendered_offset,
    '6',
  );
});

test('attached output boundary waits for following frames instead of declaring an early gap', async (t) => {
  const f = await fixture({ onOutput: async () => {} });
  t.after(() => f.transport.dispose());
  f.socket.receive(attached({ output_end_offset: '5', resume_strategy: 'redraw' }));
  await turn();
  assert.equal(f.transport.canInput, false);
  assert.equal(f.transport.pauseReconnect, false);
  f.socket.receive(output(0, 'hello'));
  await turn();
  assert.equal(f.transport.canInput, true);
  assert.equal(f.transport.renderedOffset, 5n);
});

test('consumed bytes finish committing after a socket drop before the reconnect cursor is issued', async (t) => {
  let finishWrite;
  const f = await fixture({
    onOutput: () =>
      new Promise((resolve) => {
        finishWrite = resolve;
      }),
  });
  t.after(() => f.transport.dispose());
  f.socket.receive(attached());
  await turn();
  f.socket.receive(output(0, 'hello'));
  await turn();
  f.socket.close(1006);
  f.transport.clearRetry();
  const reconnect = f.transport.connect({ manual: true });
  await turn();
  assert.equal(f.requests.length, 1);
  finishWrite();
  await reconnect;
  assert.equal(f.requests.at(-1).body.rendered_offset, '5');
});

test('output acknowledgments coalesce consumed bytes at 64 KiB or 16 ms', async (t) => {
  const f = await fixture({ onOutput: async () => {} });
  t.after(() => f.transport.dispose());
  f.socket.receive(attached({ output_end_offset: '0' }));
  await f.transport.renderQueue;
  const acks = () => f.socket.controls().filter((item) => item.type === 'output_ack');
  assert.deepEqual(
    acks().map((item) => item.rendered_offset),
    ['0'],
  );
  for (let index = 0; index < 3; index++)
    f.socket.receive(output(index * 16384, new Uint8Array(16384).fill(65)));
  await f.transport.renderQueue;
  assert.equal(f.transport.renderedOffset, 49152n);
  assert.equal(acks().length, 1);
  f.socket.receive(output(49152, new Uint8Array(16384).fill(66)));
  await f.transport.renderQueue;
  assert.equal(acks().at(-1).rendered_offset, '65536');
  assert.equal(acks().length, 2);
  f.socket.receive(output(65536, 'x'));
  await f.transport.renderQueue;
  assert.equal(acks().length, 2);
  await new Promise((resolve) => setTimeout(resolve, 25));
  assert.equal(acks().at(-1).rendered_offset, '65537');
  assert.equal(f.transport.ackTimer, null);
});

test('history barriers and detach flush the final consumed ACK without waiting for the timer', async (t) => {
  const f = await fixture({ onOutput: async () => {} });
  t.after(() => f.transport.dispose());
  f.socket.receive(
    attached({
      resume_strategy: 'history_replay',
      state: 'restoring',
      output_end_offset: '6',
    }),
  );
  f.socket.receive(output(0, '中文'));
  await f.transport.renderQueue;
  assert.equal(f.socket.controls().at(-1).rendered_offset, '6');
  assert.equal(f.transport.canInput, false);
  f.socket.receive({
    type: 'replay_complete',
    attachment_id: '11111111111111111111111111111111',
    render_epoch: '1',
    stream_epoch: '1',
    final_offset: '6',
  });
  await f.transport.renderQueue;
  f.socket.receive(output(6, 'x'));
  await f.transport.renderQueue;
  assert.equal(f.socket.controls().at(-1).rendered_offset, '6');
  f.transport.detach();
  const controls = f.socket.controls();
  assert.equal(controls.at(-2).type, 'output_ack');
  assert.equal(controls.at(-2).rendered_offset, '7');
  assert.equal(controls.at(-1).type, 'detach');
  assert.equal(f.transport.ackTimer, null);
});

test('a dropped socket discards its ACK timer and resumes from the consumed cursor', async (t) => {
  const f = await fixture({ onOutput: async () => {} });
  t.after(() => f.transport.dispose());
  f.socket.receive(attached({ output_end_offset: '0' }));
  await f.transport.renderQueue;
  f.socket.receive(output(0, 'x'));
  await f.transport.renderQueue;
  assert.notEqual(f.transport.ackTimer, null);
  f.socket.close(1006);
  f.transport.clearRetry();
  assert.equal(f.transport.ackTimer, null);
  await f.transport.connect({ manual: true });
  assert.equal(f.requests.at(-1).body.rendered_offset, '1');
  const next = Socket.instances.at(-1);
  next.open();
  next.receive(
    attached({
      attachment_id: '22222222222222222222222222222222',
      render_epoch: '2',
      writer_epoch: '2',
      start_offset: '1',
      output_end_offset: '1',
      resume_strategy: 'resume_unconsumed',
    }),
  );
  await f.transport.renderQueue;
  await new Promise((resolve) => setTimeout(resolve, 25));
  const acks = next.controls().filter((item) => item.type === 'output_ack');
  assert.equal(acks.length, 1);
  assert.equal(acks[0].attachment_id, '22222222222222222222222222222222');
  assert.equal(acks[0].rendered_offset, '1');
});

test('reconnect never replays unacknowledged input and follows server next_input_seq', async (t) => {
  const f = await fixture();
  t.after(() => f.transport.dispose());
  f.socket.receive(attached());
  await turn();
  assert.equal(f.transport.sendBytes(encoder.encode('sudo do-something\r')), true);
  assert.equal(f.socket.frames()[0].sequence, 1n);
  f.socket.close(1006);
  f.transport.clearRetry();
  assert.equal(f.transport.inputUncertain, true);
  assert.equal(f.transport.sendBytes(encoder.encode('queued-secret')), false);
  await f.transport.connect({ manual: true });
  const second = Socket.instances.at(-1);
  second.open();
  second.receive(
    attached({ resume_strategy: 'resume_unconsumed', next_input_seq: '2', state: 'restoring' }),
  );
  second.receive({
    type: 'replay_complete',
    attachment_id: '11111111111111111111111111111111',
    render_epoch: '1',
    stream_epoch: '1',
    final_offset: '0',
  });
  await turn();
  assert.equal(second.frames().length, 0);
  assert.equal(f.transport.sendBytes(encoder.encode('x')), true);
  assert.equal(second.frames()[0].sequence, 2n);
  assert.equal(new TextDecoder().decode(second.frames()[0].payload), 'x');
});

test('output overlap is deduplicated and a gap fences the renderer instead of pretending recovery', async (t) => {
  const chunks = [];
  const f = await fixture({ onOutput: async (bytes) => chunks.push(new Uint8Array(bytes)) });
  t.after(() => f.transport.dispose());
  f.socket.receive(attached());
  await turn();
  f.socket.receive(output(0, 'abc'));
  f.socket.receive(output(2, 'cde'));
  await turn();
  assert.deepEqual(
    chunks.map((x) => new TextDecoder().decode(x)),
    ['abc', 'de'],
  );
  assert.equal(f.transport.renderedOffset, 5n);
  f.socket.receive(output(8, 'gap'));
  assert.equal(f.transport.pauseReconnect, true);
  assert.equal(f.states.at(-1).reason_code, 'HISTORY_GAP');
  assert.equal(f.transport.canInput, false);
});

test('input acknowledgments are attachment fenced and a claim remains uncertain until forwarding', async (t) => {
  const f = await fixture();
  t.after(() => f.transport.dispose());
  f.socket.receive(attached());
  await turn();
  f.transport.sendBytes(encoder.encode('x'));
  const acknowledgment = {
    type: 'input_ack',
    attachment_id: '11111111111111111111111111111111',
    render_epoch: '1',
    runtime_generation: GENERATION,
    stream_epoch: '1',
    writer_epoch: '1',
    input_seq: '1',
    status: 'forwarded',
  };
  for (const stale of [
    { attachment_id: '22222222222222222222222222222222' },
    { render_epoch: '0' },
    { stream_epoch: '0' },
    { writer_epoch: '0' },
    { runtime_generation: SESSION },
  ]) {
    f.socket.receive({ ...acknowledgment, ...stale });
    assert.equal(f.transport.pendingInputBytes, 1);
  }
  f.socket.receive({ ...acknowledgment, status: 'claimed' });
  assert.equal(f.transport.pendingInputBytes, 1);
  f.socket.receive(acknowledgment);
  assert.equal(f.transport.pendingInputBytes, 0);
  f.transport.sendBytes(encoder.encode('y'));
  f.socket.receive({ ...acknowledgment, input_seq: '2', status: 'claimed' });
  f.socket.close(1006);
  f.transport.clearRetry();
  assert.equal(f.transport.inputUncertain, true);
});

test('history replay suppresses automatic terminal responses; live recovery allows them', async (t) => {
  const f = await fixture();
  t.after(() => f.transport.dispose());
  f.socket.receive(attached({ resume_strategy: 'history_replay', state: 'restoring' }));
  assert.equal(f.transport.sendBytes(encoder.encode('\x1b[1;1R'), { automatic: true }), false);
  f.socket.receive({
    type: 'replay_complete',
    attachment_id: '11111111111111111111111111111111',
    render_epoch: '1',
    stream_epoch: '1',
    final_offset: '0',
  });
  await turn();
  assert.equal(f.transport.sendBytes(encoder.encode('\x1b[1;1R'), { automatic: true }), true);
});

test('generic ready notifications cannot unlock history before its explicit consumed barrier', async (t) => {
  let finishWrite;
  const f = await fixture({
    onOutput: () => new Promise((resolve) => (finishWrite = resolve)),
  });
  t.after(() => f.transport.dispose());
  f.socket.receive(attached({ resume_strategy: 'history_replay', output_end_offset: '6' }));
  f.socket.receive({ type: 'state', state: 'ready' });
  await turn();
  assert.equal(f.transport.canInput, false);
  assert.equal(f.transport.canReply, false);
  f.socket.receive(output(0, '中文'));
  await turn();
  f.socket.receive({ type: 'state', state: 'live', output_end_offset: '6' });
  finishWrite();
  await f.transport.renderQueue;
  assert.equal(f.transport.renderedOffset, 6n);
  assert.equal(f.transport.canInput, false);
  assert.equal(f.transport.canReply, false);
  f.socket.receive({
    type: 'replay_complete',
    attachment_id: '11111111111111111111111111111111',
    render_epoch: '1',
    stream_epoch: '1',
    final_offset: '6',
  });
  await turn();
  assert.equal(f.transport.canInput, true);
  assert.equal(f.transport.canReply, true);
});

test('login expiry and the feature switch stop automatic retries with actionable reasons', async (t) => {
  for (const [code, message] of [
    ['LOGIN_REQUIRED', '请重新登录后连接'],
    ['FEATURE_DISABLED', '管理员已暂停新建终端'],
  ]) {
    const f = await fixture();
    t.after(() => f.transport.dispose());
    f.socket.receive(attached());
    await turn();
    f.socket.receive({ type: 'error', reason_code: code });
    assert.equal(f.transport.retryTimer, null);
    assert.equal(f.transport.pauseReconnect, true);
    assert.equal(f.transport.canInput, false);
    assert.equal(f.states.at(-1).message, message);
    assert.equal(reasonText(code), message);
  }
});

test('expired login variants dispose terminal state during session refresh', async () => {
  for (const code of ['AUTH_REQUIRED', 'LOGIN_REQUIRED', 'SESSION_EXPIRED', 'UNAUTHORIZED']) {
    const store = new TerminalStore({
      identity: 'user:space',
      runtime: {},
      api: async () => {
        throw Object.assign(new Error('expired'), { code });
      },
    });
    const disposed = [];
    store.records.set(SESSION, {
      transport: { detach: () => disposed.push('transport') },
      view: { dispose: () => disposed.push('view') },
    });
    store.creationIntents.set('vps:project', SESSION);
    await assert.rejects(store.refreshSessions(), { code });
    assert.equal(store.disposed, true);
    assert.equal(store.records.size, 0);
    assert.equal(store.creationIntents.size, 0);
    assert.deepEqual(disposed, ['transport', 'view']);
  }
});

test('permission revocation stops retry and cannot keep writing on the previous socket', async (t) => {
  const f = await fixture();
  t.after(() => f.transport.dispose());
  f.socket.receive(attached());
  await turn();
  f.socket.receive({ type: 'error', reason_code: 'AUTH_REVOKED' });
  assert.equal(f.transport.retryTimer, null);
  assert.equal(f.transport.pauseReconnect, true);
  assert.equal(f.transport.sendBytes(encoder.encode('whoami\r')), false);
  assert.equal(f.states.at(-1).reason_code, 'AUTH_REVOKED');
});

test('only an explicit tmux reauthentication result reconnects an exited attachment', async (t) => {
  const f = await fixture();
  t.after(() => f.transport.dispose());
  f.socket.receive(attached());
  await turn();
  f.socket.receive({
    type: 'exit',
    final_offset: '0',
    output_complete: false,
    remote_session_state: 'unknown',
    recoverability: 'reauthenticate',
    reason_code: 'SSH_CONNECTION_LOST',
  });
  await turn();
  assert.equal(f.transport.pauseReconnect, false);
  assert.equal(f.states.at(-1).state, 'reconnecting');
  f.transport.clearRetry();
  const other = await fixture();
  t.after(() => other.transport.dispose());
  other.socket.receive(attached());
  await turn();
  other.socket.receive({
    type: 'exit',
    final_offset: '0',
    output_complete: true,
    remote_session_state: 'missing',
    recoverability: 'reauthenticate',
    reason_code: 'REMOTE_SESSION_MISSING',
  });
  await turn();
  assert.equal(other.transport.pauseReconnect, true);
  assert.equal(other.states.at(-1).state, 'exited');
  assert.equal(other.transport.retryTimer, null);
});

test('uncertain create response retains one idempotency request id for retry', async () => {
  const bodies = [];
  let attempts = 0;
  const store = new TerminalStore({
    identity: 'user:space',
    runtime: {},
    api: async (path, args) => {
      if (path.includes('capabilities')) return { available: true, saved_vps_host_verification: 'skip', limits: {} };
      bodies.push(JSON.parse(args.body));
      if (++attempts === 1)
        throw Object.assign(new Error('response lost'), { code: 'NETWORK_UNCERTAIN' });
      return { session_id: SESSION, state: 'connecting' };
    },
  });
  store.adopt = (session) => ({ id: session.session_id });
  store.activate = () => {};
  const request = {
    vps: { id: 'vps-1', name: 'VPS' },
    project: { id: 'project-1' },
    newSession: true,
  };
  await assert.rejects(store.open(request));
  await store.open(request);
  assert.equal(bodies.length, 2);
  assert.equal(bodies[0].request_id, bodies[1].request_id);
  assert.equal(store.creationIntents.size, 0);
  store.dispose();
});

test('a restore finishing after navigation cannot reopen the departed workspace', async () => {
  let finish,
    visible = true;
  const store = new TerminalStore({
    identity: 'user:space',
    runtime: {},
    isCurrent: () => visible,
    api: () => new Promise((resolve) => (finish = resolve)),
  });
  const pending = store.restore(SESSION);
  visible = false;
  finish({ session_id: SESSION, mode: 'tmux', state: 'detached' });
  await pending;
  assert.equal(store.records.size, 0);
  assert.equal(store.activeId, null);
});

test('explicitly detaching tabs moves the route to the remaining session or overview', () => {
  const navigation = [];
  const store = new TerminalStore({
    identity: 'user:space',
    runtime: {},
    api: async () => ({ sessions: [] }),
    onNavigate: (...args) => navigation.push(args),
  });
  for (const id of [SESSION, GENERATION])
    store.records.set(id, { id, state: 'ready', transport: { detach() {} }, view: { dispose() {} } });
  store.activeId = SESSION;
  store.secondaryId = GENERATION;
  store.focusedId = SESSION;
  store.detach(SESSION);
  assert.deepEqual(navigation.at(-1), [GENERATION, { terminal: true }]);
  store.detach(GENERATION);
  assert.deepEqual(navigation.at(-1), [null, { terminal: true }]);
  assert.equal(store.records.size, 0);
});

for (const operation of ['refresh', 'close']) {
  test(`HTTP ${operation} metadata cannot cut the live stream's final output`, async (t) => {
    const chunks = [];
    const f = await fixture({
      onOutput: async (bytes) => chunks.push(new TextDecoder().decode(bytes)),
    });
    t.after(() => f.transport.dispose());
    f.socket.receive(attached({ output_end_offset: '0' }));
    await f.transport.renderQueue;
    const viewStates = [];
    const store = new TerminalStore({
      identity: 'user:space',
      runtime: {},
      api: async () => ({ session_id: SESSION, state: 'exited', output_complete: true }),
    });
    const record = {
      id: SESSION,
      session: f.transport.session,
      state: 'ready',
      closing: false,
      transport: f.transport,
      view: { stateChanged: (state) => viewStates.push(state) },
    };
    store.records.set(SESSION, record);
    if (operation === 'refresh') await store.refresh(record);
    else await store.close(SESSION);
    assert.equal(f.transport.isOpen(), true);
    assert.equal(f.transport.state, 'ready');
    assert.equal(
      viewStates.some((item) => item.state === 'exited'),
      false,
    );
    f.socket.receive(output(0, 'final-output'));
    f.socket.receive({ type: 'exit', final_offset: '12', output_complete: true });
    await f.transport.renderQueue;
    await turn();
    assert.deepEqual(chunks, ['final-output']);
    assert.equal(f.transport.state, 'exited');
  });
}

test('HTTP refresh can reconcile an ended session when no output stream remains', async () => {
  const finished = [];
  const store = new TerminalStore({
    identity: 'user:space',
    runtime: {},
    api: async () => ({ session_id: SESSION, state: 'exited', output_complete: false }),
  });
  const record = {
    id: SESSION,
    session: {},
    state: 'close_unknown',
    transport: { finish: (state) => finished.push(state) },
    view: { stateChanged() {} },
  };
  store.records.set(SESSION, record);
  await store.refresh(record);
  assert.equal(finished.length, 1);
  assert.equal(finished[0].state, 'exited');
});

test('paste handles Unicode boundaries, byte limits and cancellation without appending Enter', async () => {
  const original = '中文🙂'.repeat(2000),
    chunks = [...pasteChunks(original, 1024)];
  assert.equal(chunks.join(''), original);
  assert.ok(chunks.every((x) => encoder.encode(x).byteLength <= 1024));
  assert.equal(pasteSummary('a\nb', 10).preview, true);
  assert.equal(pasteSummary('中文', 4).tooLarge, true);
  const sent = [],
    abort = new AbortController();
  const result = await pasteInChunks('1234567890', {
    paste: (text) => {
      sent.push(text);
      abort.abort();
    },
    writable: () => true,
    connected: () => true,
    signal: abort.signal,
    maxChunkBytes: 4,
    delay: async () => {},
  });
  assert.deepEqual(sent, ['1234']);
  assert.equal(result.cancelled, true);
  assert.equal(result.sent, 4);
});

test('shows actionable Web SSH failures and a bounded unknown reason code', () => {
  assert.match(reasonText('SSH_CONNECT_FAILED'), /面板未能启动 SSH 会话/);
  assert.match(reasonText('SSH_CONNECTION_FAILED'), /检查服务器和网络/);
  assert.match(reasonText('HOST_TRUST_CONFIGURATION'), /版本不兼容/);
  assert.match(reasonText('HOST_KEY_REVOKED'), /版本不兼容/);
  assert.match(reasonText('HOST_KEY_CHANGED'), /请更新后重试/);
  assert.match(reasonText('PERSISTENCE_PROBE_FAILED'), /恢复能力/);
  assert.match(reasonText('VPS_CREDENTIAL_UNAVAILABLE'), /连接设置/);
  assert.equal(reasonText('UNMAPPED_PUBLIC_CODE'), '终端连接暂不可用（UNMAPPED_PUBLIC_CODE）');
  assert.equal(reasonText('UNMAPPED_PUBLIC_CODE', '允许的说明'), '允许的说明');
  assert.equal(
    reasonText('UNMAPPED_PUBLIC_CODE', 'x'.repeat(241)),
    '终端连接暂不可用（UNMAPPED_PUBLIC_CODE）',
  );
  assert.equal(reasonText('plain text <not a code>'), '终端连接暂不可用');
  assert.equal(reasonText('toString'), '终端连接暂不可用');
  assert.equal(reasonText('__proto__'), '终端连接暂不可用');
});

test('direct VPS open works without project_id, backfills host metadata, and supports vpsId lookup', async () => {
  const calls = [];
  let seq = 0;
  const store = new TerminalStore({
    identity: 'user:space',
    runtime: {},
    api: async (path, args) => {
      calls.push({ path, body: args?.body ? JSON.parse(args.body) : null });
      if (path === '/api/vps/vps-lookup')
        return {
          id: 'vps-lookup',
          name: '新加坡节点',
          host: '198.51.100.8',
          port: 2222,
          username: 'debian',
        };
      if (path.startsWith('/api/vps-terminal/capabilities'))
        return { available: true, saved_vps_host_verification: 'skip', protocol_version: 1, limits: {} };
      if (path === '/api/vps-terminal/sessions' && args?.method === 'POST') {
        const id = (++seq).toString(16).padStart(32, '0');
        return { session_id: id, state: 'connecting', mode: 'tmux', direct_vps: true };
      }
      throw new Error(`Unexpected path: ${path}`);
    },
  });
  store.adopt = (session, meta) => {
    const record = {
      id: session.session_id,
      session,
      ...meta,
      state: session.state,
      transport: { state: 'ready', socket: {}, connect() {}, detach() {} },
      view: { show() {}, focus() {}, dispose() {} },
    };
    store.records.set(record.id, record);
    return record;
  };
  await assert.rejects(store.open({}), /请选择要连接的 VPS/);
  const direct = await store.open({
    vps: { id: 'vps-direct', name: '东京直连', host: '203.0.113.10', port: 22, username: 'ubuntu' },
  });
  assert.equal(calls[0].path, '/api/vps-terminal/capabilities?vps_id=vps-direct');
  assert.equal('project_id' in calls[1].body, false);
  assert.equal(direct.host, '203.0.113.10');
  assert.equal(direct.port, 22);
  assert.equal(direct.username, 'ubuntu');
  assert.equal(direct.project_id, undefined);
  const reused = await store.open({ vpsId: 'vps-direct' });
  assert.equal(reused.session_id, direct.session_id);
  assert.equal(calls.length, 2);
  const byId = await store.open({ vpsId: 'vps-lookup', newSession: true });
  assert.equal(calls[2].path, '/api/vps/vps-lookup');
  assert.equal(calls[3].path, '/api/vps-terminal/capabilities?vps_id=vps-lookup');
  assert.equal(byId.host, '198.51.100.8');
  assert.equal(byId.port, 2222);
  store.dispose();
});

test('duplicate opens a project-free session when session is direct or project assignment changed', async () => {
  const bodies = [];
  let seq = 10;
  const store = new TerminalStore({
    identity: 'user:space',
    runtime: {},
    api: async (path, args) => {
      if (path === '/api/vps/vps-unassigned')
        return {
          id: 'vps-unassigned',
          name: '法兰克福',
          host: '192.0.2.9',
          port: 22,
          username: 'root',
          projects: [],
        };
      if (path.startsWith('/api/vps-terminal/capabilities')) return { available: true, saved_vps_host_verification: 'skip', limits: {} };
      if (path === '/api/vps-terminal/sessions' && args?.method === 'POST') {
        const body = JSON.parse(args.body);
        bodies.push(body);
        const id = (++seq).toString(16).padStart(32, '0');
        return { session_id: id, vps_id: body.vps_id, state: 'ready', mode: 'tmux' };
      }
      throw new Error(`Unexpected path: ${path}`);
    },
  });
  store.adopt = (session, meta) => {
    const record = {
      id: session.session_id,
      session,
      ...meta,
      state: 'ready',
      transport: { state: 'ready', socket: {}, connect() {}, detach() {} },
      view: { show() {}, focus() {}, dispose() {} },
    };
    store.records.set(record.id, record);
    return record;
  };
  const orphanRecord = store.adopt({
    session_id: SESSION,
    vps_id: 'vps-unassigned',
    project_id: 'removed-project',
    state: 'ready',
  });
  const duplicated = await store.duplicate(orphanRecord, true);
  assert.equal(bodies.length, 1);
  assert.equal(bodies[0].vps_id, 'vps-unassigned');
  assert.equal('project_id' in bodies[0], false);
  assert.ok(duplicated.session_id);
  store.dispose();
});

test('activating tabs in split mode preserves split and loads third tab into the focused pane', () => {
  const THIRD = '33333333333333333333333333333333';
  const navigated = [];
  const store = new TerminalStore({
    identity: 'user:space',
    runtime: {},
    api: async () => ({}),
    onNavigate: (id) => navigated.push(id),
  });
  for (const id of [SESSION, GENERATION, THIRD]) {
    store.records.set(id, {
      id,
      session: { session_id: id },
      state: 'ready',
      transport: { state: 'ready', socket: {}, connect() {}, detach() {} },
      view: { show() {}, focus() {}, dispose() {} },
    });
  }
  store.activate(SESSION);
  store.activate(GENERATION, { split: true });
  assert.equal(store.activeId, SESSION);
  assert.equal(store.secondaryId, GENERATION);
  assert.equal(store.focusedId, GENERATION);

  store.activate(SESSION);
  assert.equal(store.activeId, SESSION);
  assert.equal(store.secondaryId, GENERATION);
  assert.equal(store.focusedId, SESSION);
  assert.equal(navigated.at(-1), SESSION);

  store.activate(GENERATION);
  assert.equal(store.activeId, SESSION);
  assert.equal(store.secondaryId, GENERATION);
  assert.equal(store.focusedId, GENERATION);
  assert.equal(navigated.at(-1), GENERATION);

  store.activate(THIRD);
  assert.equal(store.activeId, SESSION);
  assert.equal(store.secondaryId, THIRD);
  assert.equal(store.focusedId, THIRD);

  store.activate(SESSION);
  store.activate(GENERATION);
  assert.equal(store.activeId, GENERATION);
  assert.equal(store.secondaryId, THIRD);
  assert.equal(store.focusedId, GENERATION);
});

test('transport computes ping/pong RTT only for matching nonces', async (t) => {
  let clock = 1000;
  const rtts = [];
  const f = await fixture({ onRtt: (ms) => rtts.push(ms) }, { now: () => clock });
  t.after(() => f.transport.dispose());
  f.socket.receive(attached());
  await turn();
  const nonce = f.transport.sendPing();
  assert.equal(typeof nonce, 'string');
  assert.equal(f.socket.controls().at(-1).type, 'ping');
  clock = 1042;
  f.socket.receive({ type: 'pong', nonce: 'unmatched-nonce' });
  assert.deepEqual(rtts, []);
  f.socket.receive({ type: 'pong', nonce });
  assert.deepEqual(rtts, [42]);
  assert.equal(f.transport.rttMs, 42);
});

test('direct reason codes and VPS catalog refresh degrade cleanly', async () => {
  for (const code of [
    'NO_TERMINAL_AGENT',
    'VPS_DISABLED',
    'SHELL_DISABLED',
    'SHELL_NOT_ALLOWED',
    'VPS_AUTHORIZATION_CHANGED',
    'VPS_CONFIGURATION_CHANGED',
  ]) {
    assert.notEqual(reasonText(code), '终端连接暂不可用');
  }
  assert.equal(reasonText('VPS_NOT_ASSIGNED').includes('项目'), false);

  const storeWithCatalog = new TerminalStore({
    identity: 'user:space',
    runtime: {},
    api: async () => ({
      vps: [
        { id: 'v1', name: '可用', enabled: true },
        { id: 'v2', name: '停用', enabled: false },
        { id: 'v3', name: '无权限', enabled: true, terminal: { eligible: false } },
      ],
    }),
  });
  const list = await storeWithCatalog.refreshVpsCatalog();
  assert.deepEqual(
    list.map((item) => item.id),
    ['v1'],
  );
  storeWithCatalog.dispose();
  assert.deepEqual(storeWithCatalog.vpsCatalog, []);

  const fallbackStore = new TerminalStore({
    identity: 'user:space',
    runtime: {},
    api: async () => {
      throw new Error('unimplemented /api/vps');
    },
  });
  assert.deepEqual(await fallbackStore.refreshVpsCatalog(), []);
  fallbackStore.dispose();
});

test('new terminal tab refuses stale Hub before sending create, even with cached capability', async () => {
  const calls = [];
  const store = new TerminalStore({
    identity: 'user:space',
    runtime: {},
    api: async (path, options) => {
      calls.push({ path, method: options?.method });
      if (path.startsWith('/api/vps-terminal/capabilities'))
        return { available: true, protocol_version: 1, limits: {} };
      throw new Error("Unexpected request after stale capability");
    },
  });
  await assert.rejects(store.open({
    vps: { id: 'vps-1', name: 'VPS', saved_vps_host_verification: 'skip' },
    newSession: true,
  }), { code: 'AGENT_UPGRADE_REQUIRED' });
  assert.equal(calls.length, 1);
  assert.match(calls[0].path, /capabilities/);
  assert.notEqual(calls[0].method, 'POST');
  store.dispose();
});
