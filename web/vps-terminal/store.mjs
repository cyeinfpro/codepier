import { TerminalTransport } from './transport.mjs';
import {
  DEFAULT_LIMITS,
  errorCode,
  limitsFrom,
  normalizeUuid,
  reasonText,
  uuid,
} from './protocol.mjs';
import { TerminalView } from './terminal-view.mjs';

const ENDED = new Set(['exited', 'closed', 'failed', 'revoked']);

// This store belongs to one authenticated user/Space identity. Nothing here is
// persisted in browser storage: no transcript, credentials, tickets or keystrokes.
export class TerminalStore {
  constructor({ api, runtime, onChange, onEdit, onNavigate, identity, isCurrent = () => true }) {
    this.api = api;
    this.runtime = runtime;
    this.onChange = onChange;
    this.onEdit = onEdit;
    this.onNavigate = onNavigate;
    this.identity = identity;
    this.isCurrent = isCurrent;
    this.records = new Map();
    this.creationIntents = new Map();
    this.creating = new Map();
    this.remoteSessions = [];
    this.activeId = null;
    this.secondaryId = null;
    this.limits = { ...DEFAULT_LIMITS };
    this.version = 0;
    this.disposed = false;
  }
  changed() {
    if (!this.disposed) this.onChange?.();
  }
  count() {
    return [...this.records.values()].filter((record) => !ENDED.has(record.state)).length;
  }
  list() {
    return [...this.records.values()].map((record) => ({
      ...record.session,
      label: record.label,
      state: record.state,
    }));
  }
  get active() {
    return this.records.get(this.activeId);
  }
  get secondary() {
    return this.records.get(this.secondaryId);
  }

  async refreshSessions() {
    const version = this.version;
    try {
      const result = await this.api('/api/vps-terminal/sessions');
      if (this.disposed || version !== this.version) return [];
      this.remoteSessions = (result.sessions || []).slice(0, 100);
      this.changed();
      return this.remoteSessions;
    } catch (error) {
      if (
        ['AUTH_REQUIRED', 'LOGIN_REQUIRED', 'SESSION_EXPIRED', 'UNAUTHORIZED'].includes(
          errorCode(error),
        )
      )
        this.dispose();
      throw error;
    }
  }

  async open({ vps, project, newSession = false, split = false, isCurrent = this.isCurrent }) {
    if (this.disposed) throw new Error('登录状态已变化');
    if (!vps?.id || !project?.id) throw new Error('请选择 VPS 和连接项目');
    const key = `${vps.id}:${project.id}`;
    if (!newSession) {
      const recent = [...this.records.values()]
        .reverse()
        .find(
          (record) =>
            record.session.vps_id === vps.id &&
            record.session.project_id === project.id &&
            !ENDED.has(record.state),
        );
      if (recent) {
        if (isCurrent()) this.activate(recent.id, { split });
        return recent.session;
      }
    }
    if (this.creating.has(key)) {
      const session = await this.creating.get(key);
      if (!this.disposed && isCurrent()) this.activate(session.session_id || session.id, { split });
      return session;
    }
    const version = this.version;
    const promise = this.create({ vps, project, key, version, split, isCurrent });
    this.creating.set(key, promise);
    this.changed();
    try {
      return await promise;
    } finally {
      if (this.creating.get(key) === promise) this.creating.delete(key);
      this.changed();
    }
  }

  async create({ vps, project, key, version, split, isCurrent }) {
    const capability = await this.api(
      `/api/vps-terminal/capabilities?vps_id=${encodeURIComponent(vps.id)}&project_id=${encodeURIComponent(project.id)}`,
    );
    if (this.disposed || version !== this.version) throw new Error('登录状态已变化');
    if (capability.protocol_version !== undefined && capability.protocol_version !== 1)
      throw Object.assign(new Error(reasonText('PROTOCOL_MISMATCH')), {
        code: 'PROTOCOL_MISMATCH',
      });
    if (
      capability.available === false ||
      capability.supported === false ||
      capability.enabled === false
    ) {
      throw Object.assign(
        new Error(
          reasonText(capability.reason_code || 'AGENT_UPGRADE_REQUIRED', capability.message),
        ),
        { code: capability.reason_code || 'AGENT_UPGRADE_REQUIRED' },
      );
    }
    this.limits = limitsFrom(capability.limits);
    if (this.count() >= this.limits.max_user_sessions)
      throw Object.assign(new Error(reasonText('SESSION_LIMIT')), { code: 'SESSION_LIMIT' });
    // Keep this request id across a lost POST response. A retry reconciles the
    // original create on the server instead of launching another shell.
    let requestId = this.creationIntents.get(key);
    if (!requestId) {
      requestId = uuid();
      this.creationIntents.set(key, requestId);
    }
    const session = await this.api('/api/vps-terminal/sessions', {
      method: 'POST',
      body: JSON.stringify({
        vps_id: vps.id,
        project_id: project.id,
        request_id: requestId,
        mode: 'auto',
        cols: 100,
        rows: 30,
      }),
    });
    if (this.disposed || version !== this.version) return session;
    this.creationIntents.delete(key);
    session.vps_name ||= vps.name;
    session.project_alias ||= project.alias;
    session.device_name ||= project.device_name;
    session.username ||= vps.username;
    session.vps_id ||= vps.id;
    session.project_id ||= project.id;
    const record = this.adopt(session, { vps, project });
    if (isCurrent()) this.activate(record.id, { split });
    return session;
  }

  adopt(session, { vps, project } = {}) {
    const id = normalizeUuid(session.session_id || session.id);
    const existing = this.records.get(id);
    if (existing) {
      Object.assign(existing.session, session);
      return existing;
    }
    const record = {
      id,
      session: { ...session, session_id: id },
      vps,
      project,
      state: session.state || 'connecting',
      label: session.vps_name || session.name || 'VPS',
      closing: false,
    };
    const transport = (record.transport = new TerminalTransport({
      session: record.session,
      api: this.api,
      limits: this.limits,
      callbacks: {
        onOutput: (bytes) => record.view.write(bytes),
        onAttached: (message) => record.view.attached(message),
        onState: (details) => {
          record.state = details.state;
          record.lastState = details;
          record.view.stateChanged(details);
          this.changed();
        },
        onNotice: (code) => record.view.showNotice(code),
        onAuthChallenge: (message) => record.view.authChallenge(message),
      },
    }));
    record.view = new TerminalView({
      session: record.session,
      transport,
      runtime: this.runtime,
      onFocus: () => {
        if (this.focusedId !== id) {
          this.focusedId = id;
          this.changed();
        }
      },
      onEdit: () => this.onEdit?.(record.session.vps_id),
      onDiagnostics: () => this.api(`/api/vps-terminal/sessions/${record.id}/diagnostics`),
      onNewSession: () =>
        this.duplicate(record).catch((error) =>
          record.view.showNotice(errorCode(error), error.message),
        ),
      onTakeover: () =>
        this.takeover(record).catch((error) =>
          record.view.showNotice(errorCode(error), error.message),
        ),
      onRefreshState: () =>
        this.refresh(record).catch((error) =>
          record.view.showNotice(errorCode(error), error.message),
        ),
    });
    this.records.set(id, record);
    this.changed();
    return record;
  }

  activate(id, { split = false } = {}) {
    const record = this.records.get(normalizeUuid(id));
    if (!record) return;
    if (split && this.activeId && this.activeId !== record.id) this.secondaryId = record.id;
    else {
      this.activeId = record.id;
      if (this.secondaryId === record.id) this.secondaryId = null;
    }
    this.focusedId = record.id;
    this.changed(); // Layout mounts a real renderer before socket output can arrive.
    record.view.show();
    if (
      ['idle', 'connecting', 'detached'].includes(record.transport.state) ||
      (!record.transport.socket && !record.transport.pauseReconnect)
    )
      record.transport.connect();
    record.view.focus();
    this.onNavigate?.(record.id);
  }
  async restore(id, { split = false, isCurrent = this.isCurrent } = {}) {
    id = normalizeUuid(id);
    let record = this.records.get(id);
    if (!record) {
      const version = this.version;
      const session = await this.api(`/api/vps-terminal/sessions/${id}`);
      if (this.disposed || version !== this.version || !isCurrent()) return;
      record = this.adopt(session);
    }
    if (isCurrent()) this.activate(id, { split });
    return record.session;
  }
  async duplicate(record = this.active, split = false) {
    if (!record) throw new Error('请先选择 VPS');
    const vps =
      record.vps || (await this.api(`/api/vps/${encodeURIComponent(record.session.vps_id)}`));
    const project =
      record.project || (vps.projects || []).find((item) => item.id === record.session.project_id);
    if (!project) throw new Error('项目分配已变化，请返回 VPS 选择连接项目');
    return this.open({ vps, project, newSession: true, split });
  }
  rename(id, name) {
    const record = this.records.get(id);
    if (record) {
      record.label = name.trim().slice(0, 40) || record.session.vps_name || 'VPS';
      this.changed();
    }
  }
  unsplit() {
    this.secondaryId = null;
    this.focusedId = this.activeId;
    this.changed();
  }

  async takeover(record = this.active) {
    if (!record) return;
    // A writer conflict can occur before this window ever receives an attached
    // frame. Fetch the authoritative CAS value instead of guessing epoch zero.
    const current = await this.api(`/api/vps-terminal/sessions/${record.id}`);
    Object.assign(record.session, current);
    await this.api(`/api/vps-terminal/sessions/${record.id}/takeover`, {
      method: 'POST',
      body: JSON.stringify({
        expected_writer_epoch: String(current.writer_epoch || '0'),
        request_id: uuid(),
      }),
    });
    record.transport.closeSocket();
    record.transport.pauseReconnect = false;
    await record.transport.connect({ manual: true });
  }

  async refresh(record = this.active) {
    if (!record || this.disposed) return;
    const current = await this.api(`/api/vps-terminal/sessions/${record.id}`);
    if (this.disposed || !this.records.has(record.id)) return;
    Object.assign(record.session, current);
    // HTTP state is a snapshot, not an output barrier. A live stream may still
    // carry the final bytes after its database row has changed to "exited".
    if (this.hasStreamingAttachment(record)) {
      this.changed();
      return;
    }
    record.state = current.state;
    if (['exited', 'closed', 'failed', 'revoked'].includes(current.state))
      record.transport.finish(current);
    else record.view.stateChanged(current);
    this.changed();
  }

  hasStreamingAttachment(record) {
    const transport = record.transport;
    return (
      !transport.disposed && !!(transport.socket || transport.attachment || transport.connecting)
    );
  }

  detach(id) {
    const record = this.records.get(id);
    if (!record) return;
    record.transport.detach();
    record.view.dispose();
    this.records.delete(id);
    if (this.secondaryId === id) this.secondaryId = null;
    if (this.activeId === id) {
      this.activeId = this.secondaryId || [...this.records.keys()].at(-1) || null;
      this.secondaryId = null;
    }
    this.focusedId = this.activeId;
    this.changed();
    this.onNavigate?.(this.focusedId, { terminal: true });
    this.refreshSessions().catch(() => {});
  }
  async close(id) {
    const record = this.records.get(id);
    if (!record || record.closing) return;
    record.closing = true;
    record.state = 'closing';
    record.transport.live = false;
    record.view.stateChanged({ state: 'closing' });
    this.changed();
    record.closeRequestId ||= uuid();
    try {
      const body = { request_id: record.closeRequestId };
      if (record.transport.runtimeGeneration)
        body.runtime_generation = record.transport.runtimeGeneration;
      const result = await this.api(`/api/vps-terminal/sessions/${id}/close`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
      Object.assign(record.session, result);
      if (['exited', 'closed'].includes(result.state)) {
        // The ordered WS exit/state closes an active renderer after its tail.
        // A confirmed HTTP result can close only an already offline view.
        if (!this.hasStreamingAttachment(record)) record.transport.finish(result);
      } else if (result.state === 'close_unknown') {
        record.state = 'close_unknown';
        record.view.stateChanged({ state: 'close_unknown', reason_code: 'CLOSE_UNKNOWN' });
      }
      // Otherwise the live control stream delivers the authoritative exit.
    } catch (error) {
      record.state = 'close_unknown';
      record.view.stateChanged({ state: 'close_unknown', reason_code: 'CLOSE_UNKNOWN' });
      throw error;
    } finally {
      record.closing = false;
      this.changed();
    }
  }
  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.version++;
    for (const record of this.records.values()) {
      record.transport.detach();
      record.view.dispose();
    }
    this.records.clear();
    this.creationIntents.clear();
    this.creating.clear();
    this.remoteSessions = [];
    this.activeId = null;
    this.secondaryId = null;
    this.focusedId = null;
  }
}
