'use strict';
// One room projection. Drafts and pending receipts are ephemeral and owner-scoped.
window.CodePierRoomStore = (() => {
  // These bound retained projections and DOM, not the durable room history.
  const MESSAGE_WINDOW_LIMIT = 200;
  const WORK_WINDOW_LIMIT = 100;
  function create(request) {
    const state = {
      owner: '',
      rooms: [],
      roomsCursor: null,
      includeArchived: false,
      room: null,
      roomId: '',
      messages: [],
      works: [],
      worksCursor: null,
      worksComplete: true,
      worksVisibilityRevision: null,
      workDetail: null,
      messageHistory: false,
      newerMessages: false,
      messageWindowLimited: false,
      workHistory: false,
      newerWorks: false,
      workWindowLimited: false,
      cursor: null,
      visibilityRevision: null,
      messageRevision: 0,
      epoch: 0,
      drafts: new Map(),
      pending: new Map(),
      mutations: new Map(),
      reads: new Map(),
      listeners: new Set(),
    };
    // A precise receipt proves one row exists, not that the history between
    // that row and the loaded page has been read. These sets stay window-sized.
    const messageReceipts = new Set(),
      workReceipts = new Set();
    function pruneReceipts(receipts, rows) {
      const visible = new Set(rows.map((row) => row.id));
      for (const id of receipts) if (!visible.has(id)) receipts.delete(id);
    }
    const emit = (domain) => state.listeners.forEach((fn) => fn(domain, state));
    const path = (id = state.roomId) => '/api/collaboration/rooms/' + encodeURIComponent(id);
    const key = () => 'room:' + (crypto.randomUUID?.() || Date.now() + ':' + Math.random());
    const roomProjection = (id = state.roomId) =>
      (state.room?.id === id && state.room) || state.rooms.find((room) => room.id === id);
    function writable(id = state.roomId) {
      const room = roomProjection(id);
      return (
        !!room && !room.read_only && !room.legacy && !['paused', 'archived'].includes(room.state)
      );
    }
    function assertWritable(id) {
      // This is a local stale-view fence. Every write is independently checked
      // by Hub; a receipt never overrides a known read-only room projection.
      const room = roomProjection(id);
      if (room && !writable(id))
        throw Object.assign(
          new Error(
            room.legacy
              ? '原房间当前只读。完成迁移并明确启用后才能发送；草稿已保留。'
              : '房间当前只读，草稿与原回执已保留。',
          ),
          { code: 'ROOM_READ_ONLY' },
        );
    }
    function clearWindows() {
      messageReceipts.clear();
      workReceipts.clear();
      state.messageHistory = false;
      state.newerMessages = false;
      state.messageWindowLimited = false;
      state.workHistory = false;
      state.newerWorks = false;
      state.workWindowLimited = false;
      state.workDetail = null;
    }
    function reset(owner = '') {
      abort();
      clearWindows();
      state.owner = owner;
      state.rooms = [];
      state.roomsCursor = null;
      state.includeArchived = false;
      state.room = null;
      state.roomId = '';
      state.messages = [];
      state.works = [];
      state.worksCursor = null;
      state.worksComplete = true;
      state.worksVisibilityRevision = null;
      state.cursor = null;
      state.visibilityRevision = null;
      state.messageRevision = 0;
      state.drafts.clear();
      state.pending.clear();
      state.mutations.clear();
    }
    function abort() {
      state.epoch++;
      invalidateReads();
    }
    function invalidateReads() {
      for (const item of state.reads.values()) item.controller.abort();
      state.reads.clear();
    }
    async function read(name, url, apply) {
      if (state.reads.has(name)) return state.reads.get(name).promise;
      const controller = new AbortController(),
        epoch = state.epoch;
      const record = { controller };
      record.promise = request(url, { signal: controller.signal })
        .then((value) => {
          if (epoch === state.epoch && !controller.signal.aborted) apply(value);
          return value;
        })
        .finally(() => {
          if (state.reads.get(name) === record) state.reads.delete(name);
        });
      state.reads.set(name, record);
      return record.promise;
    }
    function applyRoom(value) {
      const room = value.room || value;
      if (!room?.id || room.id !== state.roomId) return;
      if (state.room && Number(room.revision || 0) < Number(state.room.revision || 0)) return;
      const previousContext = state.room?.context;
      if (
        previousContext &&
        room.context &&
        previousContext.visibility_revision === room.context.visibility_revision &&
        Number(previousContext.version) > Number(room.context.version)
      )
        room.context = previousContext;
      state.room = room;
      const index = state.rooms.findIndex((item) => item.id === room.id);
      if (index < 0) state.rooms.unshift(room);
      else state.rooms[index] = room;
      emit('room');
    }
    function applyContext(value, expectedVisibility) {
      if (!state.room || value.conversation_id !== state.roomId) return;
      const previous = state.room.context;
      if (
        expectedVisibility !== undefined &&
        previous &&
        previous.visibility_revision !== expectedVisibility &&
        value.visibility_revision !== previous.visibility_revision
      )
        return;
      if (previous && Number(previous.version) > Number(value.version)) return;
      state.room.context = value;
      emit('context');
    }
    const messageOrder = (a, b) =>
      Number(a.server_sequence || a.sequence || a.created_at || a.created || 0) -
        Number(b.server_sequence || b.sequence || b.created_at || b.created || 0) ||
      a.id.localeCompare(b.id);
    function mergeMessages(items, { mode = 'receipt', nextCursor } = {}) {
      const previousItems = state.messages;
      const indexed = new Map(previousItems.map((item) => [item.id, item]));
      let confirmed = false;
      for (const item of items || []) {
        if (!item?.id) continue;
        const previous = indexed.get(item.id);
        if (previous && Number(previous.version || 0) > Number(item.version || 0)) continue;
        if (mode === 'receipt' && !previous) messageReceipts.add(item.id);
        indexed.set(item.id, item);
        if (item.client_message_id) {
          const receiptId = item.client_message_id.startsWith('work-input:')
            ? item.client_message_id.slice(11)
            : item.client_message_id;
          const receipt = state.pending.get(receiptId);
          if (receipt) {
            retireReceipt(receipt);
            confirmed ||= receipt.roomId === state.roomId;
          }
        }
      }
      const merged = [...indexed.values()].sort(messageOrder);
      const retained = () => previousItems.map((item) => indexed.get(item.id));
      const reading = state.drafts.get(state.roomId)?.reading;
      const page = (items || [])
        .map((item) => indexed.get(item.id))
        .filter(Boolean)
        .sort(messageOrder);
      const unseen = page.some((item) => !previousItems.some((old) => old.id === item.id));
      const disjoint =
        previousItems.length &&
        page.length &&
        !page.some((item) =>
          previousItems.some((old) => old.id === item.id && !messageReceipts.has(old.id)),
        );
      if (mode === 'anchor') {
        state.messages = page.slice(0, MESSAGE_WINDOW_LIMIT);
        state.cursor = nextCursor || '';
        state.messageHistory = true;
        state.newerMessages = true;
        state.messageWindowLimited = true;
      } else if (mode === 'latest' && state.messageHistory) {
        state.messages = retained();
        state.newerMessages ||= unseen;
      } else if (mode === 'latest' && disjoint) {
        state.messageWindowLimited = true;
        if (reading && !reading.atEnd) {
          state.messages = retained();
          state.messageHistory = true;
          state.newerMessages = true;
        } else {
          state.messages = page.slice(-MESSAGE_WINDOW_LIMIT);
          state.cursor = nextCursor || '';
          state.newerMessages = false;
        }
      } else if (merged.length > MESSAGE_WINDOW_LIMIT) {
        state.messageWindowLimited = true;
        const anchor =
          reading && !reading.atEnd ? merged.findIndex((item) => item.id === reading.anchorId) : -1;
        if (mode === 'older' && anchor < MESSAGE_WINDOW_LIMIT) {
          state.messages = merged.slice(0, MESSAGE_WINDOW_LIMIT);
          state.cursor = nextCursor || '';
          state.messageHistory = true;
          state.newerMessages = true;
        } else if (mode === 'latest' && (!reading || reading.atEnd)) {
          // A real latest page carries the original cursor for every row
          // removed from the old window. Never synthesize a signed cursor.
          state.messages = page.slice(-MESSAGE_WINDOW_LIMIT);
          state.cursor = nextCursor || '';
          state.newerMessages = false;
        } else {
          // Keep the reader's whole current window. Explicitly loading an
          // older page or returning to latest is the only window transition.
          state.messages = retained();
          if (mode !== 'older') {
            state.newerMessages = true;
            state.messageHistory ||= !!(reading && !reading.atEnd);
          }
        }
      } else {
        state.messages = merged;
        if (
          mode === 'older' ||
          (mode === 'latest' && (state.cursor === null || !previousItems.length))
        )
          state.cursor = nextCursor || '';
      }
      if (mode !== 'receipt') for (const item of page) messageReceipts.delete(item.id);
      pruneReceipts(messageReceipts, state.messages);
      emit('messages');
      if (confirmed) emit('sent');
    }
    function preferredWork(previous, incoming) {
      if (!previous) return incoming;
      const previousIntent = Number(previous.intent_revision || 0),
        incomingIntent = Number(incoming.intent_revision || 0);
      return incomingIntent > previousIntent ||
        (incomingIntent === previousIntent &&
          Number(incoming.version || 0) >= Number(previous.version || 0))
        ? incoming
        : previous;
    }
    function applyWork(work) {
      if (!work?.id) return;
      if (state.workDetail?.id === work.id)
        state.workDetail = preferredWork(state.workDetail, work);
      const index = state.works.findIndex((item) => item.id === work.id);
      if (index < 0) {
        if (state.works.length < WORK_WINDOW_LIMIT && !state.workHistory) {
          state.works.push(work);
          workReceipts.add(work.id);
        } else {
          state.newerWorks = true;
          state.workWindowLimited = true;
        }
      } else state.works[index] = preferredWork(state.works[index], work);
      pruneReceipts(workReceipts, state.works);
      emit('works');
    }
    function mergeWorkPage(items = [], { older = false, nextCursor, complete } = {}) {
      const previous = state.works,
        indexed = new Map(previous.map((item) => [item.id, item]));
      for (const item of items) {
        if (!item?.id) continue;
        indexed.set(item.id, preferredWork(indexed.get(item.id), item));
        if (state.workDetail?.id === item.id)
          state.workDetail = preferredWork(state.workDetail, item);
      }
      const order = (a, b) =>
        Number(b.created_at || b.created || 0) - Number(a.created_at || a.created || 0) ||
        b.id.localeCompare(a.id);
      const merged = [...indexed.values()].sort(order),
        retained = () => previous.map((item) => indexed.get(item.id));
      const reading = state.drafts.get(state.roomId)?.reading;
      const unseen = items.some((item) => !previous.some((old) => old.id === item.id));
      const disjoint =
        previous.length &&
        items.length &&
        !items.some((item) =>
          previous.some((old) => old.id === item.id && !workReceipts.has(old.id)),
        );
      let advance = older || state.worksCursor === null || !previous.length;
      if (!older && state.workHistory) {
        state.works = retained();
        state.newerWorks ||= unseen;
        advance = false;
      } else if (!older && disjoint) {
        state.workWindowLimited = true;
        if (reading && !reading.atEnd && previous.some((item) => item.id === reading.workId)) {
          state.works = retained();
          state.workHistory = true;
          state.newerWorks = true;
          advance = false;
        } else {
          state.works = items
            .map((item) => indexed.get(item.id))
            .sort(order)
            .slice(0, WORK_WINDOW_LIMIT);
          state.newerWorks = false;
          advance = true;
        }
      } else if (merged.length > WORK_WINDOW_LIMIT) {
        state.workWindowLimited = true;
        const anchor =
          reading && !reading.atEnd ? merged.findIndex((item) => item.id === reading.workId) : -1;
        if (older && (anchor < 0 || anchor >= merged.length - WORK_WINDOW_LIMIT)) {
          state.works = merged.slice(-WORK_WINDOW_LIMIT);
          state.workHistory = true;
          state.newerWorks = true;
        } else if (!older && (anchor < 0 || items.some((item) => item.id === reading.workId))) {
          state.works = items
            .map((item) => indexed.get(item.id))
            .sort(order)
            .slice(0, WORK_WINDOW_LIMIT);
          state.newerWorks = false;
          advance = true;
        } else {
          state.works = retained();
          state.workHistory = true;
          state.newerWorks ||= unseen;
          advance = false;
        }
      } else state.works = merged;
      if (advance && nextCursor !== undefined) {
        state.worksCursor = nextCursor || '';
        state.worksComplete = complete === true;
      }
      for (const item of items) workReceipts.delete(item.id);
      pruneReceipts(workReceipts, state.works);
      emit('works');
    }
    async function list({ more = false, includeArchived = state.includeArchived } = {}) {
      state.includeArchived = includeArchived;
      const cursor = more ? state.roomsCursor || '' : '';
      const query =
        '?limit=40' +
        (includeArchived ? '&include_archived=true' : '') +
        (cursor ? '&cursor=' + encodeURIComponent(cursor) : '');
      return read('rooms:' + query, '/api/collaboration/rooms' + query, (value) => {
        if (state.includeArchived !== includeArchived) return;
        state.rooms = more
          ? [
              ...new Map(
                [...state.rooms, ...(value.items || [])].map((room) => [room.id, room]),
              ).values(),
            ]
          : value.items || [];
        state.roomsCursor = value.next_cursor || null;
        emit('rooms');
      });
    }
    async function select(id) {
      abort();
      clearWindows();
      state.roomId = id || '';
      state.room = null;
      state.messages = [];
      state.works = [];
      state.worksCursor = null;
      state.worksComplete = true;
      state.worksVisibilityRevision = null;
      state.cursor = null;
      state.visibilityRevision = null;
      state.messageRevision = 0;
      emit('selection');
      if (!id) return;
      return refresh();
    }
    async function messages(older = false) {
      if (!state.roomId) return;
      const suffix = older && state.cursor ? '&cursor=' + encodeURIComponent(state.cursor) : '';
      return read(older ? 'older' : 'messages', path() + '/messages?limit=50' + suffix, (value) =>
        applyMessagePage(value, older ? 'older' : 'latest'),
      );
    }
    function applyMessagePage(value, mode) {
      if (
        value.revision !== undefined &&
        Number(value.revision) < Math.max(state.messageRevision, Number(state.room?.revision || 0))
      )
        return;
      if (value.revision !== undefined) state.messageRevision = Number(value.revision);
      const visibility = value.visibility_revision || value.scope_revision;
      const reading = state.drafts.get(state.roomId)?.reading;
      if (reading?.visibilityRevision && visibility && reading.visibilityRevision !== visibility)
        delete state.drafts.get(state.roomId).reading;
      if (visibility && state.visibilityRevision && visibility !== state.visibilityRevision) {
        clearWindows();
        state.messages = [];
        state.works = [];
        state.worksCursor = null;
        state.worksComplete = true;
        state.worksVisibilityRevision = null;
        state.cursor = null;
        emit('works');
      }
      if (visibility) state.visibilityRevision = visibility;
      mergeMessages(value.messages, { mode, nextCursor: value.next_cursor });
      if (
        value.works_visibility_revision &&
        state.worksVisibilityRevision &&
        value.works_visibility_revision !== state.worksVisibilityRevision
      ) {
        state.works = [];
        state.worksCursor = null;
        state.worksComplete = true;
        state.workHistory = false;
        state.newerWorks = false;
        state.workWindowLimited = false;
        state.workDetail = null;
      }
      if (value.works_visibility_revision)
        state.worksVisibilityRevision = value.works_visibility_revision;
      mergeWorkPage(value.works, {
        nextCursor: value.works_next_cursor,
        complete: value.works_pagination_complete,
      });
      emit('page');
    }
    async function latestMessages() {
      invalidateReads();
      state.messageHistory = false;
      state.newerMessages = false;
      state.messages = [];
      state.cursor = null;
      emit('messages');
      return messages();
    }
    async function restoreMessage(id) {
      const epoch = state.epoch;
      const exact = await read(
        'anchor:' + id,
        path() + '/messages?message_id=' + encodeURIComponent(id),
        () => {},
      );
      if (epoch !== state.epoch) return;
      if (!exact.history_cursor) throw new Error('原消息当前没有可恢复的历史窗口。');
      return read(
        'anchor-page:' + id,
        path() + '/messages?limit=50&cursor=' + encodeURIComponent(exact.history_cursor),
        (value) => applyMessagePage(value, 'anchor'),
      );
    }
    async function latestWorks() {
      invalidateReads();
      state.workHistory = false;
      state.newerWorks = false;
      state.works = [];
      state.worksCursor = null;
      return works();
    }
    async function restoreWork(id) {
      const epoch = state.epoch;
      const exact = await read(
        'work-anchor:' + id,
        '/api/collaboration/work/' + encodeURIComponent(id),
        () => {},
      );
      if (epoch !== state.epoch) return;
      if (!exact.history_cursor) throw new Error('原工作当前没有可恢复的历史窗口。');
      return read(
        'work-anchor-page:' + id,
        path() + '/work?limit=50&cursor=' + encodeURIComponent(exact.history_cursor),
        (value) => {
          if (
            value.revision !== undefined &&
            Number(value.revision) <
              Math.max(state.messageRevision, Number(state.room?.revision || 0))
          )
            return;
          state.works = [];
          state.worksCursor = null;
          state.worksVisibilityRevision = value.visibility_revision || null;
          state.workHistory = true;
          state.newerWorks = true;
          state.workWindowLimited = true;
          state.workDetail = null;
          mergeWorkPage(value.works, {
            older: true,
            nextCursor: value.next_cursor,
            complete: value.pagination_complete,
          });
        },
      );
    }
    async function works(more = false) {
      if (!state.roomId) return;
      const cursor = more ? state.worksCursor || '' : '';
      const epoch = state.epoch;
      let restart = false;
      try {
        const value = await read(
          'works:' + cursor,
          path() + '/work?limit=50' + (cursor ? '&cursor=' + encodeURIComponent(cursor) : ''),
          (value) => {
            if (
              value.revision !== undefined &&
              Number(value.revision) <
                Math.max(state.messageRevision, Number(state.room?.revision || 0))
            )
              return;
            if (
              value.visibility_revision &&
              state.worksVisibilityRevision &&
              value.visibility_revision !== state.worksVisibilityRevision
            ) {
              state.works = [];
              state.worksCursor = null;
              state.workHistory = false;
              state.newerWorks = false;
              state.workWindowLimited = false;
              state.workDetail = null;
              if (cursor) {
                restart = true;
                emit('works');
                return;
              }
            }
            if (value.visibility_revision)
              state.worksVisibilityRevision = value.visibility_revision;
            mergeWorkPage(value.works, {
              older: !!cursor,
              nextCursor: value.next_cursor,
              complete: value.pagination_complete,
            });
          },
        );
        if (!restart) return value;
      } catch (error) {
        if (!cursor || error.status !== 400 || error.code !== 'INVALID_CURSOR') throw error;
        restart = true;
      }
      if (restart && epoch === state.epoch) {
        state.works = [];
        state.worksCursor = null;
        state.worksComplete = false;
        state.workHistory = false;
        state.newerWorks = false;
        state.workWindowLimited = false;
        state.workDetail = null;
        emit('works');
        return works(false);
      }
    }
    async function work(id) {
      return read('work:' + id, '/api/collaboration/work/' + encodeURIComponent(id), (value) => {
        if (
          value.work?.id &&
          (!value.work.conversation_id || value.work.conversation_id === state.roomId)
        ) {
          if (state.workDetail?.id === value.work.id)
            state.workDetail = preferredWork(state.workDetail, value.work);
          applyWork(value.work);
        }
      });
    }
    async function refresh() {
      if (!state.roomId) return;
      const epoch = state.epoch;
      const results = await Promise.allSettled([read('room', path(), applyRoom), messages()]);
      const failure = results.find((result) => result.status === 'rejected');
      if (failure) throw failure.reason;
      // Independent reads can straddle a committed mutation. If the room
      // snapshot is newer, converge the message projection immediately rather
      // than treating an intentionally discarded response as a completed load.
      if (
        epoch === state.epoch &&
        results[1].value?.revision !== undefined &&
        state.messageRevision < Number(state.room?.revision || 0)
      )
        await messages();
    }
    function draft(id = state.roomId) {
      if (!state.drafts.has(id))
        state.drafts.set(id, {
          text: '',
          member: '',
          reply: '',
          work: '',
          workVersion: 0,
          contextCorrection: null,
          files: [],
        });
      return state.drafts.get(id);
    }
    function fileChanged(entry) {
      if (entry.owner === state.owner && entry.roomId === state.roomId) emit('draft');
    }
    function addFiles(files, roomId = state.roomId) {
      assertWritable(roomId);
      const target = draft(roomId);
      if (target.files.length + files.length > 16) throw new Error('每条消息最多添加 16 个附件。');
      if (files.some((file) => !file.size || file.size > 8 * 1024 * 1024))
        throw new Error('每个附件须为 1 字节至 8 MiB。');
      const entries = files.map((file) => ({
        id: key(),
        owner: state.owner,
        roomId,
        file,
        name: file.name,
        size: file.size,
        state: 'queued',
      }));
      target.files.push(...entries);
      if (roomId === state.roomId) emit('draft');
      return entries;
    }
    function removeFile(id, roomId = state.roomId) {
      const target = draft(roomId);
      target.files = target.files.filter((entry) => entry.id !== id);
      if (roomId === state.roomId) emit('draft');
    }
    function upload(entry) {
      if (entry.promise) return entry.promise;
      const present = () =>
        entry.owner === state.owner && state.drafts.get(entry.roomId)?.files.includes(entry);
      if (!present()) return Promise.resolve();
      try {
        assertWritable(entry.roomId);
      } catch (error) {
        return Promise.reject(error);
      }
      entry.state = 'uploading';
      entry.error = '';
      fileChanged(entry);
      entry.promise = request(path(entry.roomId) + '/attachments', {
        method: 'POST',
        body: entry.file,
        requestTimeout: Math.min(180000, 15000 + Math.ceil(entry.file.size / 65536) * 1000),
        headers: {
          'Content-Type': 'application/octet-stream',
          'X-File-Name': encodeURIComponent(entry.name),
          'X-Upload-Key': entry.id,
        },
      })
        .then((result) => {
          if (!result.attachment?.id)
            throw Object.assign(new Error('正在核对附件是否已保存'), { code: 'NETWORK_UNCERTAIN' });
          if (present()) {
            entry.attachment = result.attachment;
            entry.state = 'ready';
            fileChanged(entry);
          }
          return result;
        })
        .catch((error) => {
          if (present()) {
            entry.state =
              error.code === 'NETWORK_UNCERTAIN' || error.status >= 500 ? 'unknown' : 'failed';
            entry.error = error.message;
            fileChanged(entry);
          }
          throw error;
        })
        .finally(() => {
          entry.promise = null;
        });
      return entry.promise;
    }
    function snapshot(value = draft()) {
      return {
        text: value.text,
        member: value.member,
        reply: value.reply,
        work: value.work,
        workVersion:
          value.workVersion || state.works.find((work) => work.id === value.work)?.version,
        contextCorrection: value.contextCorrection
          ? {
              ids: [...value.contextCorrection.ids],
              version: value.contextCorrection.version,
            }
          : null,
        attachments: (value.files || [])
          .filter((entry) => entry.state === 'ready')
          .map((entry) => ({ id: entry.attachment.id, name: entry.attachment.name || entry.name })),
      };
    }
    function sameDraft(receipt, value = draft()) {
      const next = snapshot(value);
      return (
        ['text', 'member', 'reply', 'work'].every((field) => receipt.body[field] === next[field]) &&
        JSON.stringify(receipt.body.contextCorrection || null) ===
          JSON.stringify(next.contextCorrection) &&
        JSON.stringify((receipt.body.attachments || []).map((item) => item.id)) ===
          JSON.stringify(next.attachments.map((item) => item.id))
      );
    }
    function retireReceipt(receipt) {
      state.pending.delete(receipt.id);
      const originalDraft = draft(receipt.roomId);
      const accepted = new Set((receipt.body.attachments || []).map((item) => item.id));
      originalDraft.files = originalDraft.files.filter(
        (entry) => !accepted.has(entry.attachment?.id),
      );
      if (
        ['text', 'member', 'reply', 'work'].every(
          (field) => originalDraft[field] === receipt.body[field],
        ) &&
        JSON.stringify(snapshot(originalDraft).contextCorrection) ===
          JSON.stringify(receipt.body.contextCorrection || null)
      )
        Object.assign(originalDraft, {
          text: '',
          reply: '',
          work: '',
          workVersion: 0,
          contextCorrection: null,
        });
    }
    function completeReceipt(receipt, result) {
      if (receipt.owner !== state.owner) return;
      retireReceipt(receipt);
      if (receipt.roomId === state.roomId) {
        mergeMessages(result.message ? [result.message] : []);
        applyWork(result.work);
        if (result.revision !== undefined)
          state.messageRevision = Math.max(state.messageRevision, Number(result.revision));
        emit('sent');
      }
    }
    // The caller owns this receipt across network ambiguity. Never mint a new
    // message identity merely because fetch did not return a response.
    async function send(receipt) {
      const owner = state.owner;
      const epoch = state.epoch;
      if (receipt && receipt.owner !== owner) return;
      assertWritable(receipt?.roomId || state.roomId);
      if (!receipt && draft().files.some((entry) => entry.state !== 'ready'))
        throw new Error('请等待附件上传完成，或移除未完成的附件。');
      if (!receipt && draft().contextCorrection && draft().work)
        throw new Error('请先完成或取消上下文纠正，再补充工作。');
      receipt ||= { id: key(), owner, roomId: state.roomId, body: snapshot(), state: 'sending' };
      state.pending.set(receipt.id, receipt);
      receipt.state = 'sending';
      emit('pending');
      try {
        const supplement = !!receipt.body.work;
        const result = await request(
          supplement
            ? '/api/collaboration/work/' + encodeURIComponent(receipt.body.work) + '/control'
            : path(receipt.roomId) + '/messages',
          {
            method: 'POST',
            body: JSON.stringify(
              supplement
                ? {
                    action: 'supplement',
                    expected_version: receipt.body.workVersion,
                    body_text: receipt.body.text,
                    idempotency_key: receipt.id,
                    attachment_ids: receipt.body.attachments.map((item) => item.id),
                  }
                : {
                    body_text: receipt.body.text,
                    attachment_ids: receipt.body.attachments.map((item) => item.id),
                    client_message_id: receipt.id,
                    idempotency_key: receipt.id,
                    ...(receipt.body.member ? { member_id: receipt.body.member } : {}),
                    ...(receipt.body.reply ? { reply_to_id: receipt.body.reply } : {}),
                    ...(receipt.body.contextCorrection
                      ? {
                          supersedes_context_ids: receipt.body.contextCorrection.ids,
                          expected_context_version: receipt.body.contextCorrection.version,
                        }
                      : {}),
                  },
            ),
          },
        );
        if (owner !== state.owner) return result;
        if (epoch !== state.epoch) {
          retireReceipt(receipt);
          if (receipt.roomId === state.roomId) {
            emit('sent');
            void messages().catch(() => {});
          }
          return result;
        }
        completeReceipt(receipt, result);
        if (supplement && receipt.roomId === state.roomId) void messages().catch(() => {});
        return result;
      } catch (error) {
        if (owner !== state.owner) throw error;
        receipt.state =
          error.code === 'NETWORK_UNCERTAIN' || error.status >= 500 ? 'unknown' : 'failed';
        receipt.error = error.message;
        emit('pending');
        throw error;
      }
    }
    return {
      state,
      path,
      key,
      writable,
      reset,
      abort,
      invalidateReads,
      clearWindows,
      list,
      select,
      refresh,
      messages,
      latestMessages,
      restoreMessage,
      works,
      latestWorks,
      restoreWork,
      work,
      draft,
      addFiles,
      removeFile,
      upload,
      sameDraft,
      send,
      completeReceipt,
      applyRoom,
      applyContext,
      applyWork,
      mergeMessages,
      limits: { messages: MESSAGE_WINDOW_LIMIT, works: WORK_WINDOW_LIMIT },
      on(fn) {
        state.listeners.add(fn);
        return () => state.listeners.delete(fn);
      },
    };
  }
  return { create };
})();
