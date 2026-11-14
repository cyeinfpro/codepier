// Room lifecycle and shared presentation context. Hub owns authorization.
import {
  announce,
  mutationAllowed,
  panelError,
  readingSnapshot,
  remember,
  rememberReading,
  requireMutation,
  restoreAnchor,
  restoreReading,
  roomChip,
  roomName,
  roomSubtitle,
  selection,
} from './presentation.mjs';
import {
  clear,
  html,
  migrationChecklist,
  patchComposer,
  patchEmpty,
  patchHeader,
  patchRooms,
} from './room-shell.mjs';
import {
  applyDeliveryControl,
  attachmentMarkup,
  deliveryControlKey,
  deliveryControlPreviewMarkup,
  deliveryMarkup,
  deliveryPath,
  deliveryReason,
  fileSize,
  loadDeliveryRelationships,
  messageMarkup,
  messageProgress,
  mutationMarkup,
  paintDeliveryControl,
  patchFiles,
  patchMessageContent,
  patchMessages,
  patchMutationRecovery,
  patchPending,
  patchWindowHints,
  patchWorks,
  previewDeliveryControl,
  renderWorkList,
  update,
} from './timeline.mjs';
import {
  createPanel,
  legacyChannelRemediation,
  migrationBlockerMarkup,
  migrationPanel,
  migrationRevision,
  patchRoomPermissions,
  renderMigration,
  sizeEditor,
} from './panels.mjs';
import {
  applyGlobalPermissionExpiry,
  applyPermissionLevel,
  collectPermissionScope,
  continuationPreviewMarkup,
  continuationReason,
  continuationSummary,
  pausedProjectNotice,
  permissionDateValue,
  permissionDraft,
  permissionLifetimeControls,
  permissionLifetimeValue,
  permissionRows,
  permissionSection,
  permissionsPanel,
  previewContinuation,
  refreshContinuationResult,
  renderContinuationResult,
  renderPermissions,
  restrictHiddenChannel,
  savePermissions,
  updateContinuationSelection,
  updatePermissionControls,
} from './permissions.mjs';
import {
  addMemberPanel,
  connectionRenewalMarkup,
  hostBatchPanel,
  hostDetailPanel,
  hostListPanel,
  instructions,
  loadConnections,
  loadHostBatchTargets,
  loadHostImpact,
  loadHostList,
  memberPanel,
  membersPanel,
  refreshFlow,
  refreshHostBatch,
  refreshHostDetail,
  rememberedBatch,
  renderFlow,
  renderHostBatch,
  renderHostBatchTargets,
  renderHostImpact,
  returnToChannel,
  revokeHost,
  startHostBatch,
  updateHostBatchControls,
} from './connections.mjs';
import { mutation } from './mutations.mjs';
import {
  contextCorrectionStale,
  contextSource,
  diagnosticFields,
  loadContext,
  loadDiagnostics,
  loadWakeEvidence,
  renderContext,
  renderDiagnostics,
  settingsPanel,
  wakeEvidenceMarkup,
} from './diagnostics.mjs';
import {
  controlWork,
  renderWork,
  updateWorkRecoveryScope,
  workPanel,
  workRecoveryForm,
} from './work.mjs';
import {
  channelField,
  channelIdentity,
  channelLiveParts,
  channelScopeMarkup,
  inboundHealthMarkup,
  recoverSend,
  refreshChannel,
  renderChannel,
  startWeixin,
  submitChannel,
  updateChannelDependencies,
  uploadEstimateMarkup,
  weixinPanel,
} from './channels.mjs';
import { click } from './events.mjs';

const store = window.CodePierRoomStore.create((...args) => api(...args));
const state = store.state;
const E = (value) => CP.ui.esc(String(value ?? ''));
const icon = (name) =>
  name === 'message'
    ? '<svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 3V6a2 2 0 0 1 2-2Z"/></svg>'
    : CP.ui.icon(name);
const btn = (action, label, extra = '', cls = '') =>
  `<button type="button" class="cr-button ${cls}" data-cr-action="${action}" ${extra}>${label}</button>`;
const field = (label, control) => `<label class="cr-field"><span>${label}</span>${control}</label>`;
const base = '/api/collaboration';
const connectionsPath = base + '/connections';
const hostPath = (id) => connectionsPath + '/' + encodeURIComponent(id);
const batchPath = connectionsPath + '/rebind-batch';
const hostStates = {
  invited: '等待接入',
  pending: '等待接入',
  waiting_host: '等待接入',
  waiting_for_host: '等待接入',
  verifying: '正在验证',
  connected: '已连接',
  ready: '已连接',
  ready_chat: '已连接',
  ready_work: '已连接',
  active: '已登记',
  recovering: '正在恢复',
  awaiting_host_refresh: '等待宿主续订',
  waiting_host_refresh: '等待宿主续订',
  waiting_host_renewal: '等待宿主续订',
  authorization_expired: '授权已到期',
  grant_expired: '授权已到期',
  paused: '已暂停',
  revoked: '已撤销',
};
const connectionState = (item) => hostStates[item.display_state || item.state] || '连接状态待确认';
const connectionName = (item) => item.label || item.name || 'dot 连接';
const connectionRoomSummary = (item) =>
  item.single_room_title || (Number.isInteger(item.room_count) ? `${item.room_count} 个房间` : '');
const reuseAnchor = (item) =>
  item.host_connection_id ? item.reuse_member_id : item.member_id || item.id;
const supportsHostReuse = (item, capabilities) =>
  item.connection_capabilities?.verified_reuse ?? capabilities.verified_reuse ?? false;
const connectionChoice = (item) => item.host_connection_id || reuseAnchor(item);
const connectionChoiceLabel = (item) => {
  if (!item.host_connection_id) return connectionName(item) + ' · ' + (item.room_title || '原房间');
  const last = item.last_round_trip_at || item.verified_at;
  return [
    connectionName(item),
    connectionState(item),
    connectionRoomSummary(item),
    last ? `最近往返 ${dateTime(last)}` : '',
  ]
    .filter(Boolean)
    .join(' · ');
};
const connectionListPath = (cursor, host = true) =>
  connectionsPath +
  '?limit=40' +
  (host ? '&host_connections=true' : '') +
  (cursor ? '&cursor=' + encodeURIComponent(cursor) : '');
const stages = {
  invited: '等待 dot 接入',
  joining: '正在连接',
  waiting_host: '等待在 dot 中确认',
  verifying: '正在验证回复',
  ready_chat: '可以聊天',
  ready_work: '可以协作',
  degraded: '需要恢复连接',
  paused: '已暂停',
  removed: '已移除',
  choose_account: '选择微信账号',
  scan: '扫码登录',
  confirm_account: '确认微信账号',
  pairing: '确认本人',
  confirm_owner: '确认房间与范围',
  ready: '微信已连接',
  expired: '连接已过期',
};
const workLabels = {
  queued: '排队中',
  leased: '排队中',
  running: '进行中',
  verifying: '检查结果',
  waiting_input: '等你回复',
  waiting_user: '等你回复',
  waiting_member: '等待 dot 处理',
  waiting_authorization: '等待授权',
  waiting_target: '等待执行环境',
  reconciling: '确认执行结果',
  blocked: '暂时受阻',
  unknown: '结果待确认',
  reconnecting: '等待连接恢复',
  succeeded: '已完成',
  completed: '已完成',
  paused: '已暂停',
  cancelling: '正在停止',
  stopping: '正在停止',
  cancelled: '已停止',
  failed: '失败',
  expired: '已过期',
};
const memberBadges = {
  invited: '连接中',
  joining: '连接中',
  waiting_host: '连接中',
  verifying: '连接中',
  ready_chat: '在线',
  ready_work: '在线',
  degraded: '需要处理',
  paused: '已暂停',
  removed: '已移除',
};
const deliveryLabels = {
  queued: '待发送',
  pending: '待发送',
  preparing: '待发送',
  leased: '待发送',
  sending: '发送中',
  waiting_context: '等待发送条件',
  waiting_media: '等待附件',
  blocked: '等待前一条回复',
  accepted: '已受理',
  owner_confirmed: '本人已核对收到',
  needs_attention: '发送失败',
  attention: '发送失败',
  rejected: '发送失败',
  failed: '发送失败',
  unknown: '发送结果待确认',
  insufficient_evidence: '尚无送达确认',
  cancelled: '已取消',
  superseded: '已替换',
};
const finishedWork = (work) =>
  ['succeeded', 'completed', 'cancelled', 'failed', 'expired'].includes(work.state);
const workSourceId = (work) =>
  work.original_source_message_id || work.source_message_id || work.intent?.source_message_id;
const workStatus = (work) => work.display?.title || workLabels[work.state] || '状态待核对';
const contextKinds = {
  objective: '当前目标',
  decision: '已确认决策',
  constraint: '当前约束',
  question: '待回答问题',
  artifact: '相关文件',
};
let root = null,
  timer = null,
  bound = null,
  panel = null,
  panelEpoch = 0,
  pollPromise = null,
  pollAgain = false,
  hints = null,
  hintsConnected = false,
  accessBlocked = false;
let requestedProject = '',
  unsubscribe = null,
  abortEvents = null,
  suspended = false;
const activeRoom = () => store.writable();
const readOnlyReason = () =>
  state.room?.legacy
    ? '这是旧版房间。处理完迁移清单后即可继续使用。'
    : '房间已暂停或归档，恢复后才能发消息。';
const retryableContinuationPreview = new Set([
  'ROOM_CONTINUATION_PREVIEW_EXPIRED',
  'ROOM_CONTINUATION_PREVIEW_CHANGED',
  'ROOM_CONTINUATION_SELECTION_CHANGED',
]);
const safeWorkActions = new Set(['pause', 'cancel', 'stop']);
const safeChannelActions = new Set(['pause', 'disconnect', 'revoke_tasks', 'cancel']);
const currentOwner = () =>
  [S.session?.user_id || S.session?.username || '', S.space_id || ''].join(':');
const memberId = (item) => item.member_id || item.id;
const memberShort = (item) => '#' + memberId(item).slice(0, 8);
const memberOrigin = (item) =>
  item.host_label ||
  (item.host_connection_id
    ? '独立 dot 连接'
    : item.connection_source_member_id
      ? '来自连接 #' + item.connection_source_member_id.slice(0, 8)
      : item.legacy
        ? '原连接待迁移'
        : item.evidence?.host_bound
          ? '本房间已登记的宿主'
          : '宿主尚未登记');
const members = () => (state.room?.members || []).filter((item) => item.state !== 'removed');
const title = (item) => item?.display?.title || stages[item?.stage || item?.state] || '等待连接';
// Rooms carried over from project partitions were titled with raw project
// IDs ("267454898aed… · production"). Show the project alias instead.
const rawPartitionTitle = /^([0-9a-f]{32})(?:\s*·\s*(.+))?$/i;
const projectLabel = (id, room) =>
  S.projects?.find((p) => p.id === id)?.alias ||
  S.projects?.find((p) => p.id === id)?.name ||
  room?.projects?.find((p) => p.project_id === id)?.project_label ||
  room?.projects?.find((p) => p.project_id === id)?.alias ||
  '';
const roomProjects = (room) =>
  (room?.projects || [])
    .filter((p) => p.state !== 'removed')
    .map((p) => projectLabel(p.project_id, room) || p.project_id?.slice(0, 6))
    .filter(Boolean);
let roomFilter = '';
const hue = (value) =>
  [...String(value || '')].reduce((sum, char) => (sum * 31 + char.charCodeAt(0)) % 360, 7);
const actionLabel = (action) =>
  ({
    refresh_invite: '刷新加入说明',
    resume: '继续连接',
    retry_probe: '重新发送连接检查',
    pause: '暂停',
    remove: '移除成员',
    continue_connection: '继续连接',
    recover_connection: '恢复连接',
    wait_for_host_refresh: '在原 dot 继续连接',
    awaiting_host_refresh: '在原 dot 继续连接',
    copy_instructions: '复制加入说明',
    view_details: '查看详情',
    start_qr: '生成二维码',
    poll_qr: '检查扫码状态',
    accept_account: '确认账号',
    bind: '生成配对说明',
    confirm_owner: '完成连接',
    refresh_pairing: '刷新配对说明',
    cancel: '取消连接',
    approve_tasks: '允许微信交办工作',
    revoke_tasks: '仅撤销工作许可',
    rebind: '更换连接',
  })[action] || '继续';
const time = (value) =>
  value
    ? new Date(typeof value === 'number' ? value * 1000 : value).toLocaleTimeString('zh-CN', {
        hour: '2-digit',
        minute: '2-digit',
      })
    : '';
const dateTime = (value) =>
  value === null || value === undefined || value === ''
    ? '未提供'
    : new Date(typeof value === 'number' ? value * 1000 : value).toLocaleString('zh-CN');
const supplied = (value) => (value === null || value === undefined ? '未提供' : String(value));
const identifier = (value) => (value ? '#' + String(value).slice(0, 12) : '未提供');
function detach() {
  remember();
  rememberReading();
  clearTimeout(timer);
  timer = null;
  closeHints();
  pollAgain = false;
  store.abort();
  abortEvents?.abort();
  abortEvents = null;
  unsubscribe?.();
  unsubscribe = null;
  CP.ui.closeModalLayer(root?.querySelector('dialog'));
  root = null;
  bound = null;
  panelEpoch++;
  panel = null;
}
// Legacy rooms: one actionable checklist instead of three copies of the same
// read-only sentence. Each line carries the exact next step for that blocker.
const deliveryControlPreviews = new Map();
function closePanel({ restoreFocus = true } = {}) {
  const dialog = root?.querySelector('dialog');
  if (!dialog) return;
  if (!restoreFocus) dialog._crOpener = null;
  panel?.impactController?.abort();
  if (panel?.name === 'work') state.workDetail = null;
  panelEpoch++;
  panel = null;
  CP.ui.closeModalLayer(dialog);
}
function openPanel(name, heading, content, data = {}) {
  panel?.impactController?.abort();
  if (panel?.name === 'work') state.workDetail = null;
  panelEpoch++;
  panel = { name, roomId: state.roomId, epoch: panelEpoch, key: store.key(), ...data };
  const dialog = root.querySelector('dialog');
  dialog._crOpeningEpoch = panel.epoch;
  if (!dialog.open)
    dialog._crOpener = root._panelOpener?.isConnected ? root._panelOpener : document.activeElement;
  dialog.querySelector('h2').textContent = heading;
  dialog.querySelector('.cr-panel-content').innerHTML = content;
  dialog.scrollTop = 0;
  panelError('');
  patchMutationRecovery();
  patchRoomPermissions();
  if (!dialog.open) CP.ui.showModalLayer(dialog);
  const openingEpoch = panel.epoch;
  requestAnimationFrame(() => {
    if (!dialog.open || panel?.epoch !== openingEpoch) return;
    const focused = document.activeElement;
    if (focused && focused !== dialog && dialog.contains(focused)) return;
    const controls = CP.ui.focusable(dialog);
    (controls.find((control) => control.matches('[autofocus]')) || controls[0])?.focus();
  });
}
const permissionCaps = ['read', 'write', 'execute'];
const permissionLevels = {
  '': ['不加入', []],
  read: ['只读', ['read']],
  write: ['读写', ['read', 'write']],
  run: ['读取和运行', ['read', 'execute']],
  execute: ['读写和运行', ['read', 'write', 'execute']],
};
const permissionLevel = (caps) =>
  caps.includes('execute')
    ? caps.includes('write')
      ? 'execute'
      : 'run'
    : caps.includes('write')
      ? 'write'
      : caps.includes('read')
        ? 'read'
        : '';
const permissionOptions = (value = '', bulk = false) =>
  Object.entries(permissionLevels)
    .filter(([key]) => !bulk || key !== 'run')
    .map(
      ([key, [label]]) =>
        `<option value="${key}" ${key === value ? 'selected' : ''}>${label}</option>`,
    )
    .join('');
const permissionKey = (value) =>
  JSON.stringify([value.project_id, value.environment_id || 'production']);
const hostImpactLabel = (item) =>
  item.room_title || item.conversation_title || '房间 ' + identifier(item.conversation_id);
const hostImpactBinding = (item) => [item.member_id, item.binding_generation].join(':');
const hostBatchLabels = {
  queued: '等待验证',
  pending: '等待验证',
  pending_validation: '等待验证',
  invited: '等待 dot 接入',
  bound: '正在验证',
  activated: '已更换',
  waiting: '等待 dot 接入',
  waiting_host: '等待 dot 接入',
  waiting_connection: '等待 dot 接入',
  prepared: '等待 dot 接入',
  candidate: '等待 dot 接入',
  verifying: '正在验证',
  switched: '已更换',
  succeeded: '已更换',
  completed: '已更换',
  failed: '更换失败',
  expired: '候选已过期',
  cancelled: '更换已取消',
};
const hostBatchErrors = {
  ROOM_MEMBER_INACTIVE: '该房间成员当前已停用',
  ROOM_BINDING_CHANGED: '成员的连接已更换',
  ROOM_BINDING_PENDING: '该成员已有待验证的新连接',
  ROOM_BINDING_CANCELLED: '接入流程已关闭',
  ROOM_BINDING_EXPIRED: '接入邀请已过期',
  CONNECT_FLOW_NOT_FOUND: '原接入流程已不可用',
};
async function recoverAction(id) {
  const receipt = state.mutations.get(id);
  if (
    !receipt ||
    ['recovering', 'not_saved'].includes(receipt.state) ||
    receipt.owner !== state.owner
  )
    return;
  // Exact original receipts remain queryable after a room becomes read-only.
  // Hub returns an existing receipt, or rejects unsaved work using the live
  // read-only/CAS boundary. Never refresh a version or mint a replacement key.
  if (JSON.parse(receipt.body).idempotency_key !== receipt.id)
    throw new Error('原请求身份无法核对，请保留当前记录。');
  const epoch = state.epoch;
  receipt.state = 'recovering';
  patchPending();
  let saved = false;
  try {
    // The exact serialized request survives dialog closure. A new form or
    // newer scope projection must never silently mint a replacement write.
    const result = await api(receipt.url, { method: receipt.method, body: receipt.body });
    if (receipt.owner !== state.owner) return;
    saved = true;
    state.mutations.delete(id);
    patchPending();
    if (result.continuation?.batch_id)
      store.draft(receipt.roomId).permissionContinuation = result.continuation.batch_id;
    if (receipt.kind === 'create') await store.list();
    if (!root || epoch !== state.epoch) return;
    store.invalidateReads();
    if (result.room) store.applyRoom(result);
    if (receipt.kind === 'create') {
      closePanel();
      requestedProject = '';
      await selectRoom(result.room.id);
    } else if (receipt.roomId === state.roomId) {
      if (receipt.kind === 'member' || receipt.kind === 'flow') {
        openPanel('member', result.member?.name || 'dot 连接', '<div data-cr-flow-view></div>', {
          member: result.member,
          flowId: result.flow?.id,
        });
        renderFlow(result);
      } else if (receipt.kind === 'channel') {
        openPanel('weixin', '连接微信', '<div data-cr-channel-view></div>');
        renderChannel(result);
      } else if (receipt.kind === 'work') {
        store.applyWork(result.work);
        workPanel(result.work.id);
      } else if (receipt.kind === 'host-batch' && result.batch_id) {
        if (receipt.hostId) rememberedBatch(receipt.hostId, result.batch_id);
        await hostBatchPanel(result.batch_id, receipt.hostId, result);
      } else if (receipt.kind === 'host-revoke' && receipt.hostId) {
        await hostDetailPanel(receipt.hostId);
      } else closePanel();
      await store.refresh();
    }
    announce('已核对并恢复原操作');
  } catch (error) {
    if (receipt.owner === state.owner) {
      if (!saved && receipt.kind === 'migration' && error.not_saved === true) {
        receipt.state = 'not_saved';
        receipt.error = '服务器已确认原启用请求未保存。请重新核对当前迁移条件，再明确确认。';
        patchPending();
        if (root && epoch === state.epoch && receipt.roomId === state.roomId) {
          if (panel?.name === 'migration') {
            panel.key = '';
            panel.migrationRevision = null;
            const box = root.querySelector('[data-cr-migration-view]');
            if (box) delete box._markup;
            renderMigration();
          }
          await store.refresh().catch(() => {});
          if (epoch === state.epoch)
            panel?.name === 'migration' ? panelError(receipt.error) : announce(receipt.error);
        }
        return;
      }
      if (!saved && receipt.kind === 'channel' && error.status === 409) {
        // A WeChat step is checked against its flow version before any
        // write, so a conflict on the exact original key proves it was not
        // saved. Drop it instead of replaying it on every later click.
        state.mutations.delete(id);
        patchPending();
        if (root && epoch === state.epoch && receipt.roomId === state.roomId) {
          if (panel?.name === 'weixin') {
            panelError('原请求未保存（连接状态已前进），已按当前状态刷新。');
            await refreshChannel().catch(() => {});
          } else announce('原微信连接请求未保存，状态已前进。');
        }
        return;
      }
      if (!saved) receipt.state = 'unknown';
      const message =
        (saved ? '原操作已保存，视图需要重新读取：' : '原操作尚未确认：') + error.message;
      if (!saved) receipt.error = message;
      panel ? panelError(message) : announce(message, true);
      patchPending();
    }
    throw error;
  }
}
async function submitForm(form) {
  const current = panel;
  if (!current || current.busy) return;
  const data = new FormData(form),
    type = form.dataset.crForm;
  if (type === 'create') {
    const result = await mutation('POST', base + '/rooms', {
      title: data.get('title').trim(),
      projects: data
        .getAll('project')
        .map((project) => ({ project, environment_id: 'production' })),
    });
    if (!result) return;
    const room = result.room || result;
    await store.list();
    closePanel();
    requestedProject = '';
    await selectRoom(room.id);
    const pausedNotice = pausedProjectNotice(result);
    if (pausedNotice) announce('房间已创建。' + pausedNotice);
  } else if (type === 'migration') {
    if (current.migrationRevision !== migrationRevision()) {
      renderMigration();
      panelError('房间或迁移条件已变化，请重新核对并确认。');
      return;
    }
    if (!state.room?.legacy || state.room?.migration?.eligible !== true) {
      panelError('请先处理服务器列出的迁移条件，再明确确认启用。');
      return;
    }
    const revision = current.migrationRevision;
    const expectedVersion = current.version;
    const confirmed = await uiConfirmAction(
      {
        title: '启用新版房间？',
        body: '沿用当前已批准的范围。',
        confirmLabel: '启用',
      },
      {
        owner: form,
        current: () => panel === current && revision === migrationRevision(),
      },
    );
    if (!confirmed) return;
    const result = await mutation('POST', store.path() + '/migration', {
      action: 'activate',
      expected_version: expectedVersion,
    });
    if (result) {
      closePanel();
      await store.refresh();
      announce('已切换到新版房间。');
    }
  } else if (type === 'permissions') {
    await savePermissions(form, current);
  } else if (type === 'member') {
    const existing = data.get('connection_mode') === 'existing';
    const chosen = current.connections.find(
      (item) => connectionChoice(item) === data.get('reuse_member_id'),
    );
    if (existing && !chosen) {
      panelError('请选择要使用的 dot 连接。');
      return;
    }
    const verifiedReuse =
      chosen?.host_connection_id && supportsHostReuse(chosen, current.connectionCapabilities || {});
    const reuse = !existing
      ? {}
      : verifiedReuse
        ? { host_connection_id: chosen.host_connection_id }
        : {
            reuse_member_id: reuseAnchor(chosen),
            reuse_binding_generation: chosen.reuse_binding_generation ?? chosen.binding_generation,
          };
    const result = await mutation('POST', store.path() + '/members', {
      name: data.get('name').trim(),
      expected_version: current.version,
      ...(data.get('keep_connected') ? { keep_connected: true } : {}),
      ...reuse,
    });
    if (!result) return;
    await store.refresh();
    if (panel !== current) return;
    openPanel('member', result.member?.name || data.get('name'), '<div data-cr-flow-view></div>', {
      member: result.member,
      flowId: result.flow?.id || result.flow_id,
    });
    renderFlow(result);
  } else if (type === 'work-recovery') {
    const action = form.dataset.workAction;
    const candidates =
      current.work.recovery?.[
        action === 'takeover' ? 'takeover_candidates' : 'restore_candidates'
      ] || [];
    const selected = candidates.find((member) => member.member_id === data.get('target_member_id'));
    if (!selected) {
      panelError('请选择此工作当前允许的成员。');
      return;
    }
    await controlWork(action, selected.member_id);
  } else if (type === 'settings') {
    const result = await mutation('PATCH', store.path(), {
      title: data.get('title').trim(),
      expected_version: current.version,
    });
    if (result) {
      closePanel();
      await store.refresh();
    }
  } else if (type === 'weixin') await submitChannel(form);
}
async function selectRoom(id) {
  remember();
  rememberReading();
  closeHints();
  accessBlocked = false;
  closePanel();
  announce('');
  root?.classList.remove('cr-show-rooms');
  root?.querySelector('[data-cr-action="rooms"]')?.setAttribute('aria-expanded', 'false');
  const list = root?.querySelector('[data-cr-messages]');
  list?.replaceChildren();
  try {
    const promise = store.select(id);
    if (root) root._restoringReading = !!store.draft().reading;
    selection(id);
    const editor = root?.querySelector('#cr-message');
    if (editor) {
      editor.value = store.draft().text;
      sizeEditor(editor);
    }
    patchHeader();
    patchWorks();
    patchPending();
    await promise;
    if (state.roomId === id) {
      patchHeader();
      patchMessages();
      patchWorks();
      patchPending();
      await restoreReading();
      connectHints();
    }
  } catch (error) {
    if (error.name !== 'AbortError') announce(error.message, true);
  }
}
const wakeEvidenceLabels = {
  redeemed: '已开始处理',
  coalesced_before_send: '发送前已合并',
  accepted_waiting_claim: '宿主接口已受理，等待领取',
  accepted_without_observed_claim: '宿主接口已受理，尚未观察到领取',
  insufficient_evidence: '证据不足',
};

const channelSteps = [
  ['扫码', ['choose_account', 'scan', 'confirm_account', 'needs_reconnect']],
  [
    '确认范围与绑定',
    ['choose_member', 'waiting_member', 'waiting_project', 'route_conflict', 'confirm_binding'],
  ],
  [
    '发送配对码',
    [
      'pairing',
      'pairing_expired',
      'confirm_owner',
      'ready',
      'paused',
      'disconnected',
      'closed',
      'cancelled',
    ],
  ],
];
const channelCopy = {
  choose_account: ['连接你的微信', '用手机微信扫码登录，或选择已登记的微信。'],
  scan: ['用手机微信扫一扫', '扫码后在手机上确认登录。二维码 5 分钟内有效，本页会自动刷新状态。'],
  confirm_account: ['确认扫码的是你', '核对手机返回的扫码身份。确认后加密保存这个微信的登录凭据。'],
  needs_reconnect: [
    '微信需要重新登录',
    '原登录已过期或被微信拒绝。重新扫码后沿用原账号和原房间连接。',
  ],
  choose_member: [
    '选择接收微信消息的 dot',
    '房间里有多个 dot 但还没有主责。微信消息会交给这里选择的 dot 处理和回复。',
  ],
  waiting_member: [
    '先为房间添加一个 dot',
    '微信消息需要一个 dot 来接收和回复。添加并连上 dot 后回到这里继续。',
  ],
  waiting_project: ['先为原连接关联项目', '旧连接需要先关联一个项目才能继续。'],
  route_conflict: ['先解除原来的微信连接', ''],
  confirm_binding: [
    '确认这个房间的私聊范围',
    '允许你本人微信私聊里的消息进入这个房间；也可以选择允许你在微信里直接交办工作。',
  ],
  pairing: ['在微信里发送配对码', '把配对码发到机器人私聊，这里会自动继续。'],
  pairing_expired: ['配对码已过期', '配对码 15 分钟内有效。生成新的配对码后再发送一次。'],
  confirm_owner: ['确认是你本人发送的', '已收到配对码。核对发送者后完成连接。'],
  ready: ['微信已连接', ''],
  paused: ['微信已暂停', '暂停期间不接收新的微信消息。恢复后沿用原联系人和原房间。'],
  disconnected: ['微信已断开', '可以重新确认连接范围后再次配对。'],
  cancelled: ['已取消连接', '可以随时重新开始。'],
  closed: ['微信已解除', '历史消息保留在房间。'],
};
const channelReasons = {
  WEIXIN_QR_EXPIRED: '二维码已过期，请重新生成。',
  WEIXIN_QR_ATTENTION: '微信没有返回可用的扫码结果，请重新生成二维码。',
  WEIXIN_QR_CANCELLED: '上一次扫码已取消。',
  WEIXIN_QR_RESPONSE_UNCONFIRMED: '暂时连不上微信扫码服务，请稍后重新生成二维码。',
  WEIXIN_QR_RESPONSE_UNSUPPORTED: '微信返回了无法识别的扫码结果，请重新生成二维码。',
  WEIXIN_CREDENTIAL_CHANGED: '微信登录凭据已变化，请重新扫码。',
  WEIXIN_ACCOUNT_CONFLICT: '这个微信已由其他账号登记，不能在这里接入。',
  WEIXIN_SESSION_EXPIRED: '微信登录已过期，请重新扫码。',
  WEIXIN_AUTH_REJECTED: '微信拒绝了当前登录凭据，请重新扫码。',
  WEIXIN_RENEWAL_BOT_MISMATCH: '扫码的不是原来的微信，请用原微信重新扫码。',
  WEIXIN_DISABLED: '空间管理员已关闭微信接入。',
};
const quietInbound = new Set([
  '',
  'WEIXIN_POLL_IDLE',
  'WEIXIN_POLL_FENCE_LOST',
  'WEIXIN_BINDING_CHANGED',
  'WEIXIN_POLL_CANCELLED',
  'WEIXIN_PAIRING_EXPIRED',
]);
const safeImage = (value) =>
  typeof value === 'string' &&
  value.length <= 1500000 &&
  /^data:image\/svg\+xml;base64,[A-Za-z0-9+/]+={0,2}$/.test(value);
const reasonText = (code) =>
  !code ? '' : channelReasons[code] || (/^[A-Z0-9_]+$/.test(code) ? '状态代码：' + code : code);
const facts = (rows) =>
  `<dl class="cr-wx-facts">${rows
    .filter(([, value]) => value)
    .map(([name, value]) => `<div><dt>${E(name)}</dt><dd>${E(value)}</dd></div>`)
    .join('')}</dl>`;
function closeHints() {
  hints?.close();
  hints = null;
  hintsConnected = false;
}
function wake() {
  if (pollPromise) {
    pollAgain = true;
    return;
  }
  clearTimeout(timer);
  void poll();
}
function connectHints() {
  if (
    !root ||
    !state.roomId ||
    hints ||
    document.hidden ||
    accessBlocked ||
    typeof EventSource === 'undefined'
  )
    return;
  const roomId = state.roomId,
    owner = state.owner;
  const source = (hints = new EventSource(
    store.path() + '/events' + (S.space_id ? '?space_id=' + encodeURIComponent(S.space_id) : ''),
  ));
  const current = () =>
    hints === source &&
    !!root &&
    state.roomId === roomId &&
    state.owner === owner &&
    S.page === 'collaboration';
  let revision = '';
  source.onopen = () => {
    if (current()) {
      hintsConnected = true;
      schedule();
    }
  };
  source.onerror = () => {
    if (current()) {
      hintsConnected = false;
      wake();
    }
  };
  source.addEventListener('change', (event) => {
    if (!current()) return;
    let value;
    try {
      value = JSON.parse(event.data);
    } catch {
      return;
    }
    if (!value.revision || value.revision === revision) return;
    revision = value.revision;
    wake();
  });
  source.addEventListener('access_changed', () => {
    if (!current()) return;
    // Clear the old projection before waiting on a new authorization check.
    const invalidatedPanel = panel;
    closeHints();
    store.abort();
    store.clearWindows();
    root._restoringReading = false;
    closePanel();
    // A project edit can invalidate its own room stream before its PUT
    // response arrives. Retain only the identity of that closed submission,
    // never its old projection or permission. A later panel changes the fence.
    if (invalidatedPanel?.name === 'permissions' && invalidatedPanel.busy)
      invalidatedPanel.accessInvalidation = {
        owner,
        roomId,
        epoch: state.epoch,
        openingEpoch: invalidatedPanel.epoch,
      };
    const saved = invalidatedPanel?.savedPermissionFeedback;
    if (
      saved &&
      saved.owner === owner &&
      saved.roomId === roomId &&
      saved.openingEpoch === invalidatedPanel.epoch
    ) {
      saved.epoch = state.epoch;
      saved.accessInvalidated = true;
      root._crProjectSaveFeedback = saved;
      announce(saved.message);
    }
    root.querySelector('.cr-panel-content').replaceChildren();
    const pending = root.querySelector('[data-cr-pending]');
    pending.replaceChildren();
    pending._markup = null;
    state.room = null;
    state.messages = [];
    state.works = [];
    state.worksCursor = null;
    state.worksComplete = true;
    state.worksVisibilityRevision = null;
    state.cursor = null;
    state.visibilityRevision = null;
    state.messageRevision = 0;
    delete store.draft(roomId).reading;
    state.rooms = state.rooms.filter((room) => room.id !== roomId);
    patchHeader();
    patchMessages();
    patchWorks();
    accessBlocked = true;
    Promise.allSettled([store.refresh(), store.list()]).then((results) => {
      if (!root || state.roomId !== roomId || state.owner !== owner) return;
      const failure = results.find((result) => result.status === 'rejected');
      const denied = results.some(
        (result) => result.status === 'rejected' && [401, 403, 404].includes(result.reason?.status),
      );
      if (denied) announce('房间权限已变化，请重新选择可用房间。', true);
      else {
        // A successful write may supersede this read. Abort is a transport
        // fence, never evidence that the owner lost room access.
        accessBlocked = false;
        patchHeader();
        patchPending();
        connectHints();
        if (failure) wake();
        else schedule();
      }
    });
  });
}
async function poll() {
  if (!root || document.hidden || accessBlocked || S.page !== 'collaboration') return schedule();
  if (pollPromise) return pollPromise;
  const epoch = state.epoch,
    savedFeedback = root._crProjectSaveFeedback;
  pollPromise = (async () => {
    try {
      await store.refresh();
      if (epoch !== state.epoch) return;
      if (root && root._crProjectSaveFeedback === savedFeedback) delete root._crProjectSaveFeedback;
      if (panel?.name === 'member') await refreshFlow();
      if (panel?.name === 'host-detail') await refreshHostDetail();
      if (panel?.name === 'host-batch' && !panel.batchComplete) await refreshHostBatch();
      if (panel?.name === 'weixin') await refreshChannel();
      if (panel?.name === 'work' && !panel.busy) await store.work(panel.work.id);
      if (panel?.name === 'permissions') void refreshContinuationResult(panel);
    } catch (error) {
      if (error.name !== 'AbortError' && root && epoch === state.epoch) {
        const saved = root._crProjectSaveFeedback;
        const confirmed =
          saved &&
          saved.owner === state.owner &&
          saved.roomId === state.roomId &&
          saved.epoch === state.epoch &&
          !panel &&
          saved.openingEpoch === root.querySelector('dialog')?._crOpeningEpoch;
        announce(
          (confirmed ? saved.message + ' 视图刷新失败：' : '刷新暂时失败，草稿与原工作已保留。') +
            error.message,
          true,
        );
      }
    }
  })().finally(() => {
    pollPromise = null;
    if (pollAgain && root && !document.hidden && !accessBlocked) {
      pollAgain = false;
      clearTimeout(timer);
      timer = setTimeout(poll, 0);
    } else schedule();
  });
  return pollPromise;
}
function schedule() {
  clearTimeout(timer);
  if (root && !accessBlocked)
    timer = setTimeout(poll, panel?.flowId ? 1500 : hintsConnected ? 15000 : 5000);
}
function bind() {
  root = document.querySelector('.cr-shell');
  if (!root || bound === root) return;
  bound = root;
  abortEvents?.abort();
  abortEvents = new AbortController();
  const signal = abortEvents.signal;
  unsubscribe?.();
  unsubscribe = store.on(update);
  const editor = root.querySelector('#cr-message');
  root._restoringReading = !!store.draft().reading;
  editor.value = store.draft().text;
  sizeEditor(editor);
  patchHeader();
  patchMessages();
  patchWorks();
  patchPending();
  void restoreReading().catch((error) => {
    if (error.name !== 'AbortError') announce(error.message, true);
  });
  root.querySelector('[data-cr-older]').hidden = !state.cursor;
  accessBlocked = false;
  connectHints();
  if (state.loadError) {
    announce(state.loadError, true);
    delete state.loadError;
  }
  root.addEventListener(
    'click',
    (event) => {
      const button = event.target.closest('[data-cr-action]');
      if (!button || !root.contains(button) || button.disabled) return;
      if (!button.closest('dialog')) root._panelOpener = button;
      event.preventDefault();
      click(button).catch((error) => {
        if (error.name !== 'AbortError')
          panel ? panelError(error.message) : announce(error.message, true);
      });
    },
    { signal },
  );
  root.addEventListener(
    'submit',
    (event) => {
      event.preventDefault();
      if (event.target.id === 'cr-composer') {
        remember();
        const draft = store.draft(),
          roomId = state.roomId,
          owner = state.owner;
        if (
          (!draft.text.trim() && !draft.files.length) ||
          !activeRoom() ||
          state.room.state === 'paused' ||
          contextCorrectionStale() ||
          draft.files.some((entry) => entry.state !== 'ready')
        )
          return;
        if (
          [...state.pending.values()].some(
            (item) => item.roomId === state.roomId && store.sameDraft(item),
          )
        )
          return;
        store.send().catch(async (error) => {
          if (state.roomId !== roomId || state.owner !== owner) return;
          if (error.code === 'CONTEXT_VERSION_CHANGED') await store.refresh().catch(() => {});
          if (state.roomId === roomId && state.owner === owner) announce(error.message, true);
        });
      } else if (event.target.matches('[data-cr-form]')) {
        const current = panel,
          owner = state.owner,
          epoch = state.epoch;
        if (current && !current.busy) panelError('');
        submitForm(event.target).catch((error) => {
          if (
            error.name === 'AbortError' ||
            current !== panel ||
            owner !== state.owner ||
            epoch !== state.epoch
          )
            return;
          // mutation() owns HTTP/CAS messages; also surface failures thrown
          // before it runs, while preserving this submission's precise hint.
          const existing = root?.querySelector('[data-cr-panel-error]')?.textContent;
          panelError(existing || error.message || '未能完成提交，请核对当前选择。');
        });
      }
    },
    { signal },
  );
  editor.addEventListener(
    'input',
    () => {
      remember();
      sizeEditor(editor);
      patchComposer();
    },
    { signal },
  );
  root.querySelector('[data-cr-room-filter]')?.addEventListener(
    'input',
    (event) => {
      roomFilter = event.target.value;
      patchRooms();
    },
    { signal },
  );
  editor.addEventListener(
    'compositionstart',
    () => {
      editor._composing = true;
    },
    { signal },
  );
  editor.addEventListener(
    'compositionend',
    () => {
      editor._composing = false;
    },
    { signal },
  );
  editor.addEventListener(
    'keydown',
    (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !editor._composing) {
        event.preventDefault();
        root.querySelector('#cr-composer').requestSubmit();
      }
    },
    { signal },
  );
  root.addEventListener(
    'change',
    (event) => {
      if (
        event.target.name === 'target_member_id' &&
        event.target.form?.dataset.crForm === 'work-recovery'
      )
        updateWorkRecoveryScope(event.target.form);
      if (event.target.id === 'cr-file') {
        const selected = [...event.target.files];
        event.target.value = '';
        try {
          for (const entry of store.addFiles(selected)) {
            store.upload(entry).catch((error) => {
              if (entry.owner === state.owner && entry.roomId === state.roomId)
                announce(error.message, true);
            });
          }
        } catch (error) {
          announce(error.message, true);
        }
      }
      if (event.target.name === 'connection_mode' && panel?.name === 'add-member') {
        panel.modeTouched = true;
        const existing = event.target.value === 'existing',
          select = root.querySelector('[name="reuse_member_id"]');
        root.querySelector('[data-cr-connections]').hidden = !existing;
        select.disabled = !existing;
        select.required = existing;
        if (existing && !panel.connectionsLoaded) void loadConnections();
      }
      if (event.target.matches('[data-cr-host-member]') && panel?.name === 'host-detail')
        updateHostBatchControls();
      if (event.target.name === 'reuse_member_id' && panel?.name === 'add-member') {
        const source = panel.connections.find(
          (item) => connectionChoice(item) === event.target.value,
        );
        const name = root.querySelector('[data-cr-form="member"] [name="name"]');
        if (source && (!name.value.trim() || name.value === panel.lastSourceName))
          name.value = connectionName(source);
        panel.lastSourceName = source ? connectionName(source) : '';
      }
      if (event.target.name === 'member_id' && event.target.closest('#cr-composer'))
        store.draft().member = event.target.value;
      if (['allow_owner_tasks', 'confirm_follow_room'].includes(event.target.name))
        updateChannelDependencies();
      if (event.target.matches('[data-permission-level]')) {
        applyPermissionLevel(event.target, event.target.value);
        updatePermissionControls();
      }
      if (event.target.matches('[data-permission-bulk-level]')) {
        // 「全部项目设为」: every grantable project gets the highest level it
        // allows within the requested one; 「不加入」 removes them all.
        const section = event.target.closest('[data-permission-section]');
        const requested = permissionLevels[event.target.value]?.[1];
        if (!requested) return;
        for (const project of section.querySelectorAll(
          '[data-permission-project]:not(:disabled)',
        )) {
          const select = project
            .closest('[data-permission-row]')
            .querySelector('[data-permission-level]');
          const available = [...select.options].filter((option) => !option.disabled);
          const best = available
            .reverse()
            .find((option) =>
              permissionLevels[option.value][1].every((cap) => requested.includes(cap)),
            );
          if (best) applyPermissionLevel(select, best.value, true);
        }
        event.target.value = '-';
        updatePermissionControls();
      }
      if (
        event.target.matches('[data-permission-global-expiry], [data-permission-global-date] input')
      ) {
        applyGlobalPermissionExpiry(event.target.closest('[data-permission-section]'));
        updatePermissionControls();
      }
      if (event.target.matches('[data-permission-target-option]')) updatePermissionControls();
      if (
        event.target.matches('[data-permission-lifetime-mode],[data-permission-lifetime-until]')
      ) {
        event.target.closest('[data-permission-lifetime]').dataset.lifetimeEdited = 'true';
        updatePermissionControls();
      }
      if (event.target.matches('[data-permission-mode]')) updatePermissionControls();
      const form = event.target.closest('[data-cr-form="permissions"]');
      if (!form || !activeRoom() || panel?.name !== 'permissions') return;
      if (event.target.matches('[data-cr-continuation-all]')) {
        for (const task of form.querySelectorAll('[data-cr-continuation-task]:not(:disabled)'))
          task.checked = event.target.checked;
        updateContinuationSelection(form);
      } else if (event.target.matches('[data-cr-continuation-task]')) {
        updateContinuationSelection(form);
      } else if (
        event.target.matches('[data-cr-continue-selected]') ||
        (form.querySelector('[data-cr-continue-selected]').checked &&
          event.target.closest('[data-permission-section], [data-permission-mode]'))
      ) {
        void previewContinuation(form, panel);
      }
    },
    { signal },
  );
  root.querySelector('dialog').addEventListener(
    'close',
    () => {
      const dialog = root?.querySelector('dialog');
      // A queued close event can belong to an earlier opening of this same
      // dialog. Do not tear down a panel that has already been reopened.
      if (dialog?.open) return;
      panelEpoch++;
      if (panel?.name === 'work') state.workDetail = null;
      panel = null;
      const opener = dialog?._crOpener;
      if (!opener?.disabled) CP.ui.restoreLayerFocus(opener);
    },
    { signal },
  );
  root.querySelector('.cr-feed').addEventListener(
    'scroll',
    () => {
      const feed = root.querySelector('.cr-feed');
      rememberReading();
      if (!state.newerMessages && feed.scrollHeight - feed.scrollTop - feed.clientHeight < 90)
        root.querySelector('[data-cr-latest]').hidden = true;
    },
    { signal, passive: true },
  );
  document.addEventListener(
    'visibilitychange',
    () => {
      if (document.hidden) closeHints();
      else {
        connectHints();
        wake();
      }
    },
    { signal },
  );
  schedule();
}
async function open(project) {
  requestedProject = project || '';
  const matching = state.rooms.find((room) =>
    (room.projects || []).some((p) => p.project_id === project),
  );
  if (matching) selection(matching.id);
  if (S.page === 'collaboration' && root) {
    if (matching) return selectRoom(matching.id);
    createPanel();
    return;
  }
  return navigate('collaboration');
}
window.addEventListener('pagehide', () => {
  if (typeof S !== 'undefined' && S.page === 'collaboration') {
    suspended = true;
    detach();
  }
});
window.addEventListener('pageshow', (event) => {
  if (event.persisted && suspended) {
    suspended = false;
    if (S.page === 'collaboration' && S.session)
      renderPage(false).catch((error) => toast(error.message, true));
  }
});
window.CodePierCollaboration = {
  html,
  bind,
  detach,
  clear,
  open,
  presentation: { inboundHealthMarkup, uploadEstimateMarkup },
};

export {
  E,
  abortEvents,
  accessBlocked,
  actionLabel,
  activeRoom,
  addMemberPanel,
  announce,
  applyDeliveryControl,
  applyGlobalPermissionExpiry,
  applyPermissionLevel,
  attachmentMarkup,
  base,
  batchPath,
  bind,
  bound,
  btn,
  channelCopy,
  channelField,
  channelIdentity,
  channelLiveParts,
  channelReasons,
  channelScopeMarkup,
  channelSteps,
  clear,
  click,
  closeHints,
  closePanel,
  collectPermissionScope,
  connectHints,
  connectionChoice,
  connectionChoiceLabel,
  connectionListPath,
  connectionName,
  connectionRenewalMarkup,
  connectionRoomSummary,
  connectionState,
  connectionsPath,
  contextCorrectionStale,
  contextKinds,
  contextSource,
  continuationPreviewMarkup,
  continuationReason,
  continuationSummary,
  controlWork,
  createPanel,
  currentOwner,
  dateTime,
  deliveryControlKey,
  deliveryControlPreviewMarkup,
  deliveryControlPreviews,
  deliveryLabels,
  deliveryMarkup,
  deliveryPath,
  deliveryReason,
  detach,
  diagnosticFields,
  facts,
  field,
  fileSize,
  finishedWork,
  hints,
  hintsConnected,
  hostBatchErrors,
  hostBatchLabels,
  hostBatchPanel,
  hostDetailPanel,
  hostImpactBinding,
  hostImpactLabel,
  hostListPanel,
  hostPath,
  hostStates,
  html,
  hue,
  icon,
  identifier,
  inboundHealthMarkup,
  instructions,
  legacyChannelRemediation,
  loadConnections,
  loadContext,
  loadDeliveryRelationships,
  loadDiagnostics,
  loadHostBatchTargets,
  loadHostImpact,
  loadHostList,
  loadWakeEvidence,
  memberBadges,
  memberId,
  memberOrigin,
  memberPanel,
  memberShort,
  members,
  membersPanel,
  messageMarkup,
  messageProgress,
  migrationBlockerMarkup,
  migrationChecklist,
  migrationPanel,
  migrationRevision,
  mutation,
  mutationAllowed,
  mutationMarkup,
  open,
  openPanel,
  paintDeliveryControl,
  panel,
  panelEpoch,
  panelError,
  patchComposer,
  patchEmpty,
  patchFiles,
  patchHeader,
  patchMessageContent,
  patchMessages,
  patchMutationRecovery,
  patchPending,
  patchRoomPermissions,
  patchRooms,
  patchWindowHints,
  patchWorks,
  pausedProjectNotice,
  permissionCaps,
  permissionDateValue,
  permissionDraft,
  permissionKey,
  permissionLevel,
  permissionLevels,
  permissionLifetimeControls,
  permissionLifetimeValue,
  permissionOptions,
  permissionRows,
  permissionSection,
  permissionsPanel,
  poll,
  pollAgain,
  pollPromise,
  previewContinuation,
  previewDeliveryControl,
  projectLabel,
  quietInbound,
  rawPartitionTitle,
  readOnlyReason,
  readingSnapshot,
  reasonText,
  recoverAction,
  recoverSend,
  refreshChannel,
  refreshContinuationResult,
  refreshFlow,
  refreshHostBatch,
  refreshHostDetail,
  remember,
  rememberReading,
  rememberedBatch,
  renderChannel,
  renderContext,
  renderContinuationResult,
  renderDiagnostics,
  renderFlow,
  renderHostBatch,
  renderHostBatchTargets,
  renderHostImpact,
  renderMigration,
  renderPermissions,
  renderWork,
  renderWorkList,
  requestedProject,
  requireMutation,
  restoreAnchor,
  restoreReading,
  restrictHiddenChannel,
  retryableContinuationPreview,
  returnToChannel,
  reuseAnchor,
  revokeHost,
  roomChip,
  roomFilter,
  roomName,
  roomProjects,
  roomSubtitle,
  root,
  safeChannelActions,
  safeImage,
  safeWorkActions,
  savePermissions,
  schedule,
  selectRoom,
  selection,
  settingsPanel,
  sizeEditor,
  stages,
  startHostBatch,
  startWeixin,
  state,
  store,
  submitChannel,
  submitForm,
  supplied,
  supportsHostReuse,
  suspended,
  time,
  timer,
  title,
  unsubscribe,
  update,
  updateChannelDependencies,
  updateContinuationSelection,
  updateHostBatchControls,
  updatePermissionControls,
  updateWorkRecoveryScope,
  uploadEstimateMarkup,
  wake,
  wakeEvidenceLabels,
  wakeEvidenceMarkup,
  weixinPanel,
  workLabels,
  workPanel,
  workRecoveryForm,
  workSourceId,
  workStatus,
};
