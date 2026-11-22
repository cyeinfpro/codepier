import {
  DEFAULT_LIMITS,
  INPUT,
  OUTPUT,
  PROTOCOL_VERSION,
  STOP_RETRY_CODES,
  counter,
  decodeFrame,
  encodeFrame,
  errorCode,
  limitsFrom,
  normalizeUuid,
  reasonText,
  uuid,
} from './protocol.mjs';

const RETRIES = 8;
const ACK_INTERVAL_MS = 16;
const ACK_BYTES = 64 * 1024;
const encoder = new TextEncoder();

export function websocketUrl(path, base = globalThis.location?.href) {
  const origin = new URL(base),
    url = new URL(path || '/ws/vps-terminal', origin);
  if (url.origin !== origin.origin || url.search || url.hash || url.username || url.password)
    throw new Error('Invalid terminal websocket URL');
  url.protocol = origin.protocol === 'https:' ? 'wss:' : 'ws:';
  if (
    origin.protocol !== 'https:' &&
    !['localhost', '127.0.0.1', '[::1]'].includes(origin.hostname)
  )
    throw new Error('SSH 终端需要 HTTPS 连接');
  return url.href;
}

// Owns one browser attachment. It never creates or re-creates an SSH session.
export class TerminalTransport {
  constructor({
    session,
    api,
    rendererId = uuid(),
    limits,
    callbacks = {},
    WebSocketImpl = globalThis.WebSocket,
    baseUrl,
    random = Math.random,
  }) {
    this.sessionId = normalizeUuid(session.session_id || session.id);
    this.session = session;
    this.api = api;
    this.rendererId = rendererId;
    this.limits = limitsFrom(limits);
    this.callbacks = callbacks;
    this.WebSocketImpl = WebSocketImpl;
    this.baseUrl = baseUrl;
    this.random = random;
    this.connectionVersion = 0;
    this.renderVersion = 0;
    this.renderedOffset = 0n;
    this.receivedOffset = 0n;
    this.nextInputSeq = 1n;
    this.pendingInputs = new Map();
    this.pendingInputBytes = 0;
    this.pendingOutputBytes = 0;
    this.renderQueue = Promise.resolve();
    this.pendingAck = null;
    this.ackTimer = null;
    this.sentAckOffset = 0n;
    this.state = 'idle';
    this.live = false;
    this.disposed = false;
    this.retryAttempts = 0;
    this.inputUncertain = false;
    this.pauseReconnect = false;
    this.lastSeen = Date.now();
  }

  get canInput() {
    return this.live && this.isOpen() && !!this.attachment;
  }
  get canReply() {
    return this.isOpen() && !!this.attachment && this.resumeStrategy !== 'history_replay';
  }
  isOpen() {
    return !!this.socket && this.socket.readyState === 1;
  }
  current(version) {
    return !this.disposed && version === this.connectionVersion;
  }

  changeState(state, details = {}) {
    this.state = state;
    if (state !== 'ready') this.live = false;
    this.callbacks.onState?.({ state, input_uncertain: this.inputUncertain, ...details });
  }

  async connect({ manual = false } = {}) {
    if (this.disposed || this.connecting || (this.isOpen() && this.attachment)) return;
    if (manual) {
      this.retryAttempts = 0;
      this.pauseReconnect = false;
    }
    if (this.pauseReconnect) return;
    this.clearRetry();
    const version = ++this.connectionVersion;
    this.connecting = true;
    this.live = false;
    this.attachment = null;
    this.changeState(this.runtimeGeneration ? 'reconnecting' : 'connecting');
    try {
      // Finish bytes already delivered to xterm before advertising the cursor.
      // A socket disappearing must not turn a consumed chunk into duplicate input
      // to the terminal parser on the next attachment.
      await this.renderQueue;
      if (!this.current(version)) return;
      const body = { renderer_id: this.rendererId };
      if (this.runtimeGeneration)
        Object.assign(body, {
          runtime_generation: this.runtimeGeneration,
          known_epoch: this.streamEpoch.toString(),
          rendered_offset: this.renderedOffset.toString(),
        });
      const ticket = await this.api(`/api/vps-terminal/sessions/${this.sessionId}/attach-ticket`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
      if (!this.current(version)) return;
      if (!ticket?.ticket || typeof ticket.ticket !== 'string')
        throw Object.assign(new Error('终端凭据无效'), { code: 'PROTOCOL_MISMATCH' });
      const socket = new this.WebSocketImpl(websocketUrl(ticket.ws_path, this.baseUrl));
      this.socket = socket;
      socket.binaryType = 'arraybuffer';
      let authTicket = ticket.ticket;
      socket.onopen = () => {
        if (!this.current(version)) return socket.close();
        this.lastSeen = Date.now();
        this.sendControl({
          type: 'authenticate',
          ticket: authTicket,
          renderer_id: this.rendererId,
          protocol_version: PROTOCOL_VERSION,
        });
        authTicket = '';
        this.startHeartbeat(version);
      };
      socket.onmessage = (event) => {
        if (!this.current(version)) return;
        this.lastSeen = Date.now();
        try {
          if (typeof event.data === 'string') {
            if (encoder.encode(event.data).byteLength > this.limits.control_bytes)
              throw new Error('Oversized control');
            this.handleControl(JSON.parse(event.data), version);
          } else this.handleOutput(event.data, version);
        } catch {
          this.fail('INVALID_FRAME', {}, true);
        }
      };
      socket.onerror = () => {
        /* onclose owns retry; never start two reconnect loops. */
      };
      socket.onclose = (event) => {
        authTicket = '';
        if (!this.current(version)) return;
        this.stopHeartbeat();
        this.socket = null;
        this.attachment = null;
        this.live = false;
        this.markUncertain();
        if (this.pauseReconnect) return;
        const code =
          event.code === 4401
            ? 'AUTH_REQUIRED'
            : event.code === 4403
              ? 'AUTH_REVOKED'
              : event.code === 4409
                ? 'WRITER_CONFLICT'
                : 'NETWORK_ERROR';
        this.fail(code, {}, STOP_RETRY_CODES.has(code));
      };
      this.openTimer = setTimeout(() => {
        if (this.current(version) && !this.attachment) this.fail('SSH_CONNECT_TIMEOUT');
      }, 30000);
    } catch (error) {
      if (this.current(version))
        this.fail(
          errorCode(error),
          { message: error.message },
          STOP_RETRY_CODES.has(errorCode(error)),
        );
    } finally {
      if (this.current(version)) this.connecting = false;
    }
  }

  handleControl(message, version) {
    if (!message || typeof message.type !== 'string') throw new Error('Missing message type');
    if (message.session_id && normalizeUuid(message.session_id) !== this.sessionId)
      throw new Error('Wrong terminal');
    if (message.type === 'ping') {
      this.sendControl({
        type: 'pong',
        nonce: message.nonce,
        ...(message.timestamp !== undefined ? { timestamp: message.timestamp } : {}),
      });
      return;
    }
    if (message.type === 'pong' || message.type === 'authenticated') return;
    if (message.type === 'attached') {
      clearTimeout(this.openTimer);
      this.clearOutputAck();
      const generation = normalizeUuid(message.runtime_generation),
        epoch = counter(message.stream_epoch),
        writer = counter(message.writer_epoch);
      const start = counter(message.start_offset ?? '0');
      const strategy = message.resume_strategy || 'live';
      if (!['live', 'redraw', 'resume_unconsumed', 'history_replay'].includes(strategy))
        throw new Error('Unknown resume strategy');
      if (!message.attachment_id || !message.render_epoch) throw new Error('Missing attachment');
      if (
        strategy === 'resume_unconsumed' &&
        (this.runtimeGeneration !== generation ||
          this.streamEpoch !== epoch ||
          start !== this.renderedOffset)
      )
        throw new Error('Invalid resume boundary');
      const preserve = strategy === 'resume_unconsumed';
      this.runtimeGeneration = generation;
      this.streamEpoch = epoch;
      this.writerEpoch = writer;
      this.attachment = message.attachment_id;
      this.renderEpoch = message.render_epoch;
      this.resumeStrategy = strategy;
      this.nextInputSeq = counter(message.next_input_seq ?? '1');
      this.receivedOffset = start;
      this.renderedOffset = start;
      this.sentAckOffset = start;
      this.recoveryBoundary = counter(message.output_end_offset ?? start);
      this.renderVersion++;
      this.pendingBarrier = null;
      this.pendingOutputBytes = 0;
      this.renderQueue = Promise.resolve();
      this.session.mode = message.mode;
      this.retryAttempts = 0;
      this.callbacks.onAttached?.({ ...message, preserve });
      this.changeState(strategy === 'live' ? 'connecting' : 'restoring', { mode: message.mode });
      // The attachment boundary can precede its output frames. It is a target,
      // not evidence of a gap merely because the following bytes have not arrived.
      if (strategy !== 'history_replay' && message.state === 'ready')
        this.completeReplay(message, version);
      this.queueOutputAck(true);
      if (this.lastSize) this.sendResize(this.lastSize);
      return;
    }
    if (message.type === 'input_ack') {
      if (
        !this.attachment ||
        counter(message.writer_epoch) !== this.writerEpoch ||
        (message.attachment_id && message.attachment_id !== this.attachment) ||
        (message.render_epoch !== undefined && message.render_epoch !== this.renderEpoch) ||
        (message.stream_epoch !== undefined &&
          counter(message.stream_epoch) !== this.streamEpoch) ||
        (message.runtime_generation !== undefined &&
          normalizeUuid(message.runtime_generation) !== this.runtimeGeneration)
      )
        return;
      if (!['claimed', 'forwarded', 'uncertain', 'already_claimed'].includes(message.status))
        throw new Error('Invalid input acknowledgment status');
      const seq = counter(message.input_seq),
        pending = this.pendingInputs.get(seq.toString());
      if (seq >= this.nextInputSeq) throw new Error('Invalid input acknowledgment');
      // A claim fences retries but does not confirm forwarding to the SSH
      // channel. Keep it pending so a disconnect still surfaces uncertainty.
      if (pending !== undefined && message.status !== 'claimed') {
        this.pendingInputBytes -= pending;
        this.pendingInputs.delete(seq.toString());
      }
      if (message.status === 'uncertain' || message.status === 'already_claimed') {
        this.inputUncertain = true;
        this.callbacks.onNotice?.('INPUT_UNCERTAIN');
      }
      this.callbacks.onWritable?.();
      return;
    }
    if (message.type === 'replay_complete') {
      this.completeReplay(message, version);
      return;
    }
    if (message.type === 'auth_challenge') {
      clearTimeout(this.openTimer);
      this.openTimer = setTimeout(
        () => {
          if (this.current(version)) this.fail('SSH_AUTH_FAILED', { message: '认证已超时' }, true);
        },
        Math.min(180, Math.max(1, message.deadline_seconds || 60)) * 1000,
      );
      this.live = false;
      this.changeState('authenticating');
      this.callbacks.onAuthChallenge?.(message);
      return;
    }
    if (message.type === 'state') {
      Object.assign(this.session, message);
      if (message.state === 'ready' || message.state === 'live')
        this.completeReplay(message, version);
      else if (['exited', 'closed', 'failed', 'revoked', 'close_unknown'].includes(message.state))
        this.finish(message);
      else this.changeState(message.state || 'connecting', message);
      return;
    }
    if (message.type === 'error') {
      const code = message.reason_code || message.code || 'NETWORK_ERROR';
      this.fail(
        code,
        message,
        STOP_RETRY_CODES.has(code) ||
          message.recoverability === 'none' ||
          message.recoverable === false,
      );
      return;
    }
    if (message.type === 'exit') {
      this.finish(message);
      return;
    }
    // Forward-compatible server notifications have no authority to enable input.
  }

  completeReplay(message, version) {
    if (!this.attachment) return;
    // A generic ready/state notification is not proof that history has ended.
    // Keep terminal responses and user input fenced until its explicit barrier.
    if (this.resumeStrategy === 'history_replay' && message.type !== 'replay_complete') return;
    if (message.attachment_id && message.attachment_id !== this.attachment) return;
    if (message.render_epoch && message.render_epoch !== this.renderEpoch) return;
    if (message.stream_epoch !== undefined && counter(message.stream_epoch) !== this.streamEpoch)
      return;
    const renderVersion = this.renderVersion,
      boundary = message.final_offset ?? message.output_end_offset;
    const target = boundary === undefined ? this.receivedOffset : counter(boundary);
    const previous = this.pendingBarrier?.target || 0n;
    this.pendingBarrier = { version, renderVersion, target: target > previous ? target : previous };
    this.renderQueue
      .then(() => {
        this.tryCompleteBarrier();
      })
      .catch(() => this.fail('INVALID_FRAME', {}, true));
  }

  tryCompleteBarrier() {
    const barrier = this.pendingBarrier;
    if (
      !barrier ||
      !this.current(barrier.version) ||
      this.renderVersion !== barrier.renderVersion ||
      !this.attachment ||
      this.renderedOffset < barrier.target
    )
      return;
    this.pendingBarrier = null;
    this.flushOutputAck();
    this.resumeStrategy = 'live';
    this.live = true;
    this.changeState('ready', { mode: this.session.mode });
    this.callbacks.onReady?.();
  }

  handleOutput(value, version) {
    if (!this.attachment) throw new Error('Output before attachment');
    const frame = decodeFrame(value, this.limits.frame_bytes);
    if (
      frame.kind !== OUTPUT ||
      frame.session_id !== this.sessionId ||
      frame.runtime_generation !== this.runtimeGeneration ||
      frame.stream_epoch !== this.streamEpoch ||
      frame.writer_epoch !== this.writerEpoch
    )
      throw new Error('Stale terminal frame');
    const end = frame.sequence + BigInt(frame.payload.byteLength);
    if (end <= this.receivedOffset) return;
    if (frame.sequence > this.receivedOffset) {
      this.fail('HISTORY_GAP', {}, true);
      return;
    }
    const payload = frame.payload.subarray(Number(this.receivedOffset - frame.sequence));
    if (this.pendingOutputBytes + payload.byteLength > this.limits.output_pending_bytes) {
      this.fail('OUTPUT_BACKPRESSURE');
      return;
    }
    this.receivedOffset = end;
    this.pendingOutputBytes += payload.byteLength;
    const renderVersion = this.renderVersion;
    this.renderQueue = this.renderQueue
      .then(async () => {
        if (this.disposed || this.renderVersion !== renderVersion) return;
        await this.callbacks.onOutput?.(payload);
        if (this.disposed || this.renderVersion !== renderVersion) return;
        this.pendingOutputBytes -= payload.byteLength;
        this.renderedOffset = end;
        if (this.current(version) && this.attachment) this.queueOutputAck();
        this.tryCompleteBarrier();
      })
      .catch(() => {
        if (this.current(version)) this.fail('INVALID_FRAME', {}, true);
      });
  }

  queueOutputAck(immediate = false) {
    if (!this.isOpen() || !this.attachment || this.disposed) return;
    this.pendingAck = {
      version: this.connectionVersion,
      attachment: this.attachment,
      renderEpoch: this.renderEpoch,
      streamEpoch: this.streamEpoch,
      offset: this.renderedOffset,
    };
    const finalReplay =
      (this.pendingBarrier && this.renderedOffset >= this.pendingBarrier.target) ||
      (this.resumeStrategy === 'history_replay' && this.renderedOffset >= this.recoveryBoundary);
    if (immediate || finalReplay || this.renderedOffset - this.sentAckOffset >= BigInt(ACK_BYTES)) {
      this.flushOutputAck();
      return;
    }
    if (!this.ackTimer) {
      const version = this.connectionVersion,
        attachment = this.attachment;
      this.ackTimer = setTimeout(() => {
        if (this.current(version) && this.attachment === attachment) this.flushOutputAck();
      }, ACK_INTERVAL_MS);
    }
  }

  flushOutputAck() {
    const pending = this.pendingAck;
    this.clearOutputAck();
    if (
      !pending ||
      !this.current(pending.version) ||
      !this.isOpen() ||
      pending.attachment !== this.attachment ||
      pending.renderEpoch !== this.renderEpoch ||
      pending.streamEpoch !== this.streamEpoch
    )
      return false;
    // The offset is captured only after xterm's write callback, never from its
    // accepted/queued byte count. Timers from an old socket cannot ACK a new one.
    const sent = this.sendControl({
      type: 'output_ack',
      attachment_id: pending.attachment,
      render_epoch: pending.renderEpoch,
      stream_epoch: pending.streamEpoch.toString(),
      rendered_offset: pending.offset.toString(),
    });
    if (sent) this.sentAckOffset = pending.offset;
    return sent;
  }

  clearOutputAck() {
    clearTimeout(this.ackTimer);
    this.ackTimer = null;
    this.pendingAck = null;
  }

  sendBytes(bytes, { automatic = false } = {}) {
    if (!(bytes instanceof Uint8Array) || !bytes.byteLength) return false;
    if (!(automatic ? this.canReply : this.canInput)) return false;
    if (
      bytes.byteLength + this.pendingInputBytes > this.limits.pending_input_bytes ||
      this.socket.bufferedAmount > this.limits.pending_input_bytes
    ) {
      this.callbacks.onNotice?.('INPUT_BACKPRESSURE');
      return false;
    }
    for (let offset = 0; offset < bytes.byteLength; offset += this.limits.input_frame_bytes) {
      const payload = bytes.subarray(offset, offset + this.limits.input_frame_bytes),
        sequence = this.nextInputSeq++;
      this.pendingInputs.set(sequence.toString(), payload.byteLength);
      this.pendingInputBytes += payload.byteLength;
      try {
        this.socket.send(
          encodeFrame(
            {
              kind: INPUT,
              session_id: this.sessionId,
              runtime_generation: this.runtimeGeneration,
              stream_epoch: this.streamEpoch,
              writer_epoch: this.writerEpoch,
              sequence,
              payload,
            },
            this.limits.input_frame_bytes,
          ),
        );
      } catch {
        this.markUncertain();
        this.fail('NETWORK_ERROR');
        return false;
      }
    }
    return true;
  }

  sendControl(message) {
    if (!this.isOpen()) return false;
    const serialized = JSON.stringify(message);
    if (encoder.encode(serialized).byteLength > this.limits.control_bytes) return false;
    try {
      this.socket.send(serialized);
      return true;
    } catch {
      return false;
    }
  }

  resize(cols, rows) {
    if (!Number.isFinite(cols) || !Number.isFinite(rows)) return;
    const size = {
      cols: Math.max(this.limits.min_cols, Math.min(this.limits.max_cols, Math.floor(cols))),
      rows: Math.max(this.limits.min_rows, Math.min(this.limits.max_rows, Math.floor(rows))),
    };
    if (this.lastSize?.cols === size.cols && this.lastSize?.rows === size.rows) return;
    this.lastSize = size;
    clearTimeout(this.resizeTimer);
    this.resizeTimer = setTimeout(() => this.sendResize(size), this.limits.resize_coalesce_ms);
  }

  sendResize(size) {
    if (!this.attachment || !this.isOpen() || this.resumeStrategy === 'history_replay') return;
    this.resizeSeq = (this.resizeSeq || 0n) + 1n;
    this.sendControl({
      type: 'resize',
      attachment_id: this.attachment,
      runtime_generation: this.runtimeGeneration,
      stream_epoch: this.streamEpoch.toString(),
      writer_epoch: this.writerEpoch.toString(),
      resize_seq: this.resizeSeq.toString(),
      ...size,
    });
  }

  answerAuth(challengeId, responses) {
    if (
      !Array.isArray(responses) ||
      responses.some((x) => typeof x !== 'string' || x.length > 4096)
    )
      return false;
    return this.sendControl({ type: 'auth_answer', challenge_id: challengeId, responses });
  }

  markUncertain() {
    if (this.pendingInputs.size) {
      this.inputUncertain = true;
      this.callbacks.onNotice?.('INPUT_UNCERTAIN');
    }
    this.pendingInputs.clear();
    this.pendingInputBytes = 0;
  }

  fail(code, details = {}, fatal = false) {
    if (this.disposed) return;
    this.live = false;
    this.markUncertain();
    this.pauseReconnect = fatal || STOP_RETRY_CODES.has(code);
    this.closeSocket();
    this.changeState(this.pauseReconnect ? 'blocked' : 'reconnecting', {
      ...details,
      reason_code: code,
      message: reasonText(code, details.message),
    });
    if (!this.pauseReconnect) this.scheduleRetry();
  }

  scheduleRetry() {
    if (this.disposed || this.pauseReconnect || this.retryTimer) return;
    if (this.retryAttempts >= RETRIES) {
      this.pauseReconnect = true;
      this.changeState('disconnected', {
        reason_code: 'NETWORK_ERROR',
        message: '仍未连接，请检查网络后重试',
      });
      return;
    }
    const delay =
      Math.min(500 * 2 ** this.retryAttempts++, this.limits.reconnect_max_ms) *
      (0.8 + this.random() * 0.4);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, delay);
  }

  clearRetry() {
    clearTimeout(this.retryTimer);
    this.retryTimer = null;
  }
  startHeartbeat(version) {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (!this.current(version)) return;
      if (Date.now() - this.lastSeen > this.limits.heartbeat_timeout_seconds * 1000) {
        this.fail('NETWORK_ERROR');
        return;
      }
      this.sendControl({ type: 'ping', nonce: uuid(), timestamp: Date.now() });
    }, this.limits.heartbeat_seconds * 1000);
  }
  stopHeartbeat() {
    clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = null;
  }
  closeSocket() {
    this.flushOutputAck();
    clearTimeout(this.openTimer);
    clearTimeout(this.resizeTimer);
    this.stopHeartbeat();
    const socket = this.socket;
    this.socket = null;
    this.attachment = null;
    if (socket) {
      socket.onclose = null;
      socket.onmessage = null;
      socket.onopen = null;
      socket.onerror = null;
      try {
        socket.close(1000);
      } catch {
        /* Already closed. */
      }
    }
    this.connectionVersion++;
    this.connecting = false;
  }
  finish(message) {
    const version = this.connectionVersion;
    this.live = false;
    this.pauseReconnect = true;
    this.clearRetry();
    this.renderQueue.then(() => {
      if (!this.current(version)) return;
      this.markUncertain();
      this.closeSocket();
      Object.assign(this.session, message);
      if (
        this.session.mode === 'tmux' &&
        message.recoverability === 'reauthenticate' &&
        !['missing', 'ended'].includes(message.remote_session_state) &&
        !['USER_CLOSED', 'REMOTE_SESSION_MISSING', 'AUTH_REVOKED'].includes(message.reason_code)
      ) {
        // The Hub has retained the logical session. Reattach exactly that id;
        // this branch never calls create and never replays uncertain input.
        this.pauseReconnect = false;
        this.changeState('reconnecting', {
          reason_code: message.reason_code || 'SSH_CHANNEL_CLOSED',
          message: 'SSH 已断开，正在恢复原会话',
          output_complete: message.output_complete,
        });
        this.scheduleRetry();
        return;
      }
      this.changeState(message.state || 'exited', {
        ...message,
        ...(message.output_complete === false
          ? { reason_code: 'OUTPUT_INCOMPLETE', message: reasonText('OUTPUT_INCOMPLETE') }
          : {}),
      });
    });
  }
  detach() {
    if (this.disposed) return;
    this.flushOutputAck();
    this.sendControl({ type: 'detach', request_id: uuid(), reason: 'view_closed' });
    this.dispose();
  }
  dispose() {
    if (this.disposed) return;
    this.flushOutputAck();
    this.disposed = true;
    this.live = false;
    this.clearRetry();
    this.closeSocket();
    this.pendingInputs.clear();
    this.pendingInputBytes = 0;
    this.callbacks = {};
  }
}
