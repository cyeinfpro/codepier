import assert from 'node:assert/strict';
import test from 'node:test';
import { OUTPUT, encodeFrame } from '../web/vps-terminal/protocol.mjs';
import { TerminalStore } from '../web/vps-terminal/store.mjs';
import { TerminalTransport } from '../web/vps-terminal/transport.mjs';

const SESSION = '12345678123456781234567812345678';
const GENERATION = 'abcdefabcdefabcdefabcdefabcdefab';
const session = { session_id: SESSION, state: 'detached', vps_id: 'vps', project_id: 'project' };
const endpoint = '/api/vps-terminal/capabilities?vps_id=vps&project_id=project';

class TestSocket {
  constructor() {
    this.readyState = 0;
    this.sent = [];
  }
  send(data) {
    this.sent.push(JSON.parse(data));
  }
  open() {
    this.readyState = 1;
    this.onopen();
  }
  receive(data) {
    this.onmessage({
      data: data instanceof ArrayBuffer ? data : JSON.stringify(data),
    });
  }
  close() {
    this.readyState = 3;
  }
}

test('cold restore loads target-specific limits before accepting a 32 KiB frame', async (t) => {
  const paths = [];
  const rendered = [];
  let transport;
  const store = new TerminalStore({
    api: async (path) => {
      paths.push(path);
      if (path === endpoint)
        return { available: true, protocol_version: 1, limits: { frame_bytes: 32768 } };
      if (path.endsWith('/attach-ticket')) return { ticket: 'fixture-ticket' };
      return { ...session };
    },
  });
  store.adopt = (current, { limits }) => {
    assert.deepEqual(paths, [`/api/vps-terminal/sessions/${SESSION}`, endpoint]);
    transport = new TerminalTransport({
      session: current,
      api: store.api,
      limits,
      WebSocketImpl: TestSocket,
      baseUrl: 'https://fixture.example.test',
      callbacks: { onOutput: (bytes) => rendered.push(bytes) },
    });
    return { session: current };
  };
  store.activate = () => {};
  await store.restore(SESSION);
  assert.equal(transport.limits.frame_bytes, 32768);
  t.after(() => transport.dispose());
  await transport.connect();
  transport.socket.open();
  transport.socket.receive({
    type: 'attached',
    runtime_generation: GENERATION,
    attachment_id: 'attachment',
    render_epoch: '1',
    stream_epoch: '1',
    writer_epoch: '1',
  });
  const payload = new Uint8Array(32768).fill(65);
  transport.socket.receive(
    encodeFrame(
      {
        kind: OUTPUT,
        session_id: SESSION,
        runtime_generation: GENERATION,
        stream_epoch: 1n,
        writer_epoch: 1n,
        sequence: 0n,
        payload,
      },
      32768,
    ),
  );
  await transport.renderQueue;
  assert.deepEqual(rendered, [payload]);
});

test('a pending capability cannot adopt or activate a terminal after identity disposal', async () => {
  let resolveCapability;
  let loadStarted;
  const started = new Promise((resolve) => (loadStarted = resolve));
  const store = new TerminalStore({
    api: async (path) => {
      if (path === endpoint) {
        loadStarted();
        return new Promise((resolve) => (resolveCapability = resolve));
      }
      return { ...session };
    },
  });
  store.adopt = store.activate = () => assert.fail('obsolete restore reached the renderer');
  const restore = store.restore(SESSION);
  await started;
  store.dispose();
  resolveCapability({ limits: { frame_bytes: 32768 } });
  assert.equal(await restore, undefined);
});

test('restore does not attach after an unauthorized capability response', async () => {
  const store = new TerminalStore({
    api: async (path) => {
      if (path === endpoint) throw Object.assign(new Error('denied'), { status: 403 });
      return { ...session };
    },
  });
  store.adopt = store.activate = () => assert.fail('unauthorized restore reached the renderer');
  await assert.rejects(store.restore(SESSION), { status: 403 });
});

test('repeated activation during WebSocket opening and OTP reuses one socket and one ticket', async (t) => {
  let tickets = 0;
  const transport = new TerminalTransport({
    session: { ...session },
    api: async () => ({ ticket: `ticket-${++tickets}` }),
    WebSocketImpl: TestSocket,
    baseUrl: 'https://fixture.example.test',
  });
  t.after(() => transport.dispose());
  await Promise.all([transport.connect(), transport.connect()]);
  const first = transport.socket;
  await transport.connect();
  assert.equal(transport.socket, first);
  first.open();
  await transport.connect();
  first.receive({ type: 'auth_challenge', deadline_seconds: 60 });
  await transport.connect();
  assert.equal(tickets, 1);
  assert.equal(transport.socket, first);
  assert.equal(first.sent.filter((message) => message.type === 'authenticate').length, 1);
  transport.closeSocket();
  await transport.connect({ manual: true });
  assert.equal(tickets, 2);
  assert.notEqual(transport.socket, first);
});
