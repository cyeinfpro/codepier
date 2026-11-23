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
    this.vpsCatalog = [];
    this.activeId = null;
    this.secondaryId = null;
    this.focusedId = null;
    this.limits = { ...DEFAULT_LIMITS };
    this.vpsCatalogRequest = null;
    this.version = 0;
    this.disposed = false;
  }
  changed() {
    if (!this.disposed) this.onChange?.();
  }
  count() {
    return this.list().filter((session) => !ENDED.has(session.state)).length;
  }
  list() {
    const sessions = new Map();
    for (const record of this.records.values()) {
      sessions.set(record.id, {
        ...record.session,
        label: record.label,
        state: record.state,
      });
    }
    for (const session of this.remoteSessions) {
      const id = session.session_id || session.id;
      if (id && !sessions.has(id)) sessions.set(id, session);
    }
    return [...sessions.values()];
  }
  activeCountsByVPS() {
    const counts = Object.create(null);
    for (const session of this.list()) {
      if (session.vps_id && !ENDED.has(session.state))
        counts[session.vps_id] = (counts[session.vps_id] || 0) + 1;
    }
    return counts;
  }
  get active() {
    return this.records.get(this.activeId);
  }
  get secondary() {
    return this.records.get(this.secondaryId);
  }
  get focused() {
    return this.records.get(this.focusedId) || this.active;
  }

  async refreshVpsCatalog() {
    if (this.disposed) return [];
    if (this.vpsCatalogRequest) return this.vpsCatalogRequest;
    const request = this.loadVpsCatalog(this.version);
    this.vpsCatalogRequest = request;
    try {
      return await request;
    } finally {
      if (this.vpsCatalogRequest === request) this.vpsCatalogRequest = null;
    }
  }

  async loadVpsCatalog(version) {
    const catalog = new Map();
    let offset = 0;
    try {
      while (true) {
        const result = await this.api('/api/vps?limit=200&offset=' + offset);
        if (this.disposed || version !== this.version) return [];
        const items = result.vps ?? result.items ?? result;
        for (const item of items) {
          if (item?.id && item.enabled !== false && item.terminal?.eligible !== false)
            catalog.set(item.id, item);
        }
        const next = result.next_offset;
        if (next == null) break;
        if (!Number.isSafeInteger(next) || next <= offset)
          throw new Error('Invalid VPS catalog cursor');
        offset = next;
      }
      this.vpsCatalog = [...catalog.values()];
      this.changed();
    } catch {
      // Preserve the last complete inventory instead of replacing it with a
      // partial page or an empty list on a transient failure.
    }
    return this.vpsCatalog;
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

  async open({
    vps,
    vpsId,
    project = null,
    projectId = null,
    mode = 'auto',
    newSession = false,
    split = false,
    isCurrent = this.isCurrent,
  } = {}) {
    if (this.disposed) throw new Error('登录状态已变化');
    const resolvedVpsId = vps?.id || vpsId;
    if (!resolvedVpsId) throw new Error('请选择要连接的 VPS');
    const resolvedProjectId = project?.id || projectId || null;
    const key = resolvedProjectId ? `${resolvedVpsId}:${resolvedProjectId}` : resolvedVpsId;
    if (!newSession) {
      const recent = [...this.records.values()]
        .reverse()
        .find(
          (record) =>
            record.session.vps_id === resolvedVpsId &&
            (!resolvedProjectId || record.session.project_id === resolvedProjectId) &&
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
    const promise = this.create({
      vps,
      vpsId: resolvedVpsId,
      project,
      resolvedProjectId,
      mode,
      key,
      version,
      split,
      isCurrent,
    });
    this.creating.set(key, promise);
    this.changed();
    try {
      return await promise;
    } finally {
      if (this.creating.get(key) === promise) this.creating.delete(key);
      this.changed();
    }
  }

  async capability(vpsId, projectId) {
    if (!vpsId)
      throw Object.assign(new Error(reasonText('PROTOCOL_MISMATCH')), {
        code: 'PROTOCOL_MISMATCH',
      });
    const query = projectId
      ? `?vps_id=${encodeURIComponent(vpsId)}&project_id=${encodeURIComponent(projectId)}`
      : `?vps_id=${encodeURIComponent(vpsId)}`;
    const capability = await this.api(`/api/vps-terminal/capabilities${query}`);
    if (capability.protocol_version !== undefined && capability.protocol_version !== 1)
      throw Object.assign(new Error(reasonText('PROTOCOL_MISMATCH')), {
        code: 'PROTOCOL_MISMATCH',
      });
    return capability;
  }

  async create({
    vps,
    vpsId,
    project,
    resolvedProjectId,
    mode = 'auto',
    key,
    version,
    split,
    isCurrent,
  }) {
    const targetVps = vps?.id ? vps : await this.api(`/api/vps/${encodeURIComponent(vpsId)}`);
    if (this.disposed || version !== this.version) throw new Error('登录状态已变化');
    if (!targetVps?.id) throw new Error('请选择要连接的 VPS');
    const capability = await this.capability(targetVps.id, resolvedProjectId);
    if (this.disposed || version !== this.version) throw new Error('登录状态已变化');
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
    const limits = limitsFrom(capability.limits);
    this.limits = limits;
    if (this.count() >= limits.max_user_sessions)
      throw Object.assign(new Error(reasonText('SESSION_LIMIT')), {
        code: 'SESSION_LIMIT',
      });
    // Keep this request id across a lost POST response. A retry reconciles the
    // original create on the server instead of launching another shell.
    let requestId = this.creationIntents.get(key);
    if (!requestId) {
      requestId = uuid();
      this.creationIntents.set(key, requestId);
    }
    const body = {
      vps_id: targetVps.id,
      ...(resolvedProjectId ? { project_id: resolvedProjectId } : {}),
      request_id: requestId,
      mode: mode || 'auto',
      cols: 100,
      rows: 30,
    };
    const session = await this.api('/api/vps-terminal/sessions', {
      method: 'POST',
      body: JSON.stringify(body),
    });
    if (this.disposed || version !== this.version) return session;
    this.creationIntents.delete(key);
    session.vps_name ||= targetVps.name;
    session.host ||= targetVps.host;
    session.port ||= targetVps.port;
    session.username ||= targetVps.username;
    session.vps_id ||= targetVps.id;
    if (project) {
      if (project.alias) session.project_alias ||= project.alias;
      if (project.device_name) session.device_name ||= project.device_name;
      if (project.id) session.project_id ||= project.id;
    }
    const record = this.adopt(session, {
      vps: targetVps,
      project: project || null,
      limits,
    });
    if (isCurrent()) this.activate(record.id, { split });
    return session;
  }

  adopt(session, { vps, project, limits = DEFAULT_LIMITS } = {}) {
    const id = normalizeUuid(session.session_id || session.id);
    const existing = this.records.get(id);
    if (existing) {
      Object.assign(existing.session, session);
      if (vps) existing.vps = vps;
      if (project) existing.project = project;
      existing.view.syncSession?.(existing.session);
      return existing;
    }
    const record = {
      id,
      session: { ...session, session_id: id },
      vps,
      project,
      state: session.state || 'connecting',
      label: session.vps_name || session.name || 'VPS',
      rttMs: null,
      closing: false,
    };
    const transport = (record.transport = new TerminalTransport({
      session: record.session,
      api: this.api,
      limits,
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
        onRtt: (rtt) => {
          record.rttMs = rtt;
          record.view.setRtt?.(rtt);
        },
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
    if (split && this.activeId && this.activeId !== record.id) {
      this.secondaryId = record.id;
    } else if (this.secondaryId) {
      if (record.id !== this.activeId && record.id !== this.secondaryId) {
        if (this.focusedId === this.secondaryId) this.secondaryId = record.id;
        else this.activeId = record.id;
      }
    } else {
      this.activeId = record.id;
    }
    this.focusedId = record.id;
    this.changed(); // Layout mounts a real renderer before socket output can arrive.
    record.view.show();
    if (
      !ENDED.has(record.state) &&
      (['idle', 'connecting', 'detached'].includes(record.transport.state) ||
        (!record.transport.socket && !record.transport.pauseReconnect))
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
      let limits;
      if (!ENDED.has(session.state)) {
        const capability = await this.capability(session.vps_id, session.project_id);
        if (this.disposed || version !== this.version || !isCurrent()) return;
        limits = limitsFrom(capability.limits);
      }
      record = this.adopt(session, { limits });
    }
    if (isCurrent()) this.activate(id, { split });
    return record.session;
  }
  async duplicate(record = this.focused || this.active, split = false) {
    if (!record) throw new Error('请选择要连接的 VPS');
    const vps =
      record.vps ||
      (record.session?.vps_id
        ? await this.api(`/api/vps/${encodeURIComponent(record.session.vps_id)}`)
        : null);
    if (!vps?.id) throw new Error('请选择要连接的 VPS');
    const isDirect =
      !!record.session?.direct_vps ||
      !!record.session?._direct_vps ||
      String(record.session?.binding_id || '').startsWith('direct:');
    const sessionProjectId =
      !isDirect && record.session?.project_id ? record.session.project_id : null;
    const project = sessionProjectId
      ? Array.isArray(vps.projects)
        ? vps.projects.find((item) => item.id === sessionProjectId) || null
        : record.project?.id === sessionProjectId
          ? record.project
          : null
      : null;
    if (project) return this.open({ vps, project, newSession: true, split });
    return this.open({ vps, newSession: true, split });
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
    if (!this.activeId) this.refreshVpsCatalog().catch(() => {});
  }
  async close(id) {
    const record = this.records.get(id);
    if (!record || record.closing || ENDED.has(record.state)) return;
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
      if (this.records.get(id) !== record || ENDED.has(record.state)) return;
      Object.assign(record.session, result);
      if (ENDED.has(result.state)) {
        // The ordered WS exit/state closes an active renderer after its tail.
        // A confirmed HTTP result can close only an already offline view.
        if (!this.hasStreamingAttachment(record)) record.transport.finish(result);
      } else if (result.state === 'close_unknown') {
        record.state = 'close_unknown';
        record.view.stateChanged({
          state: 'close_unknown',
          reason_code: 'CLOSE_UNKNOWN',
        });
      }
      // Otherwise the live control stream delivers the authoritative exit.
    } catch (error) {
      if (this.records.get(id) !== record || ENDED.has(record.state)) return;
      record.state = 'close_unknown';
      record.view.stateChanged({
        state: 'close_unknown',
        reason_code: 'CLOSE_UNKNOWN',
      });
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
    this.vpsCatalog = [];
    this.activeId = null;
    this.secondaryId = null;
    this.focusedId = null;
  }
}
