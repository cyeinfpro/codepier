'use strict';
// One room projection. Drafts and pending receipts are ephemeral and owner-scoped.
window.CodePierRoomStore = (() => {
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
    const emit = (domain) => state.listeners.forEach((fn) => fn(domain, state));
    const path = (id = state.roomId) => '/api/collaboration/rooms/' + encodeURIComponent(id);
    const key = () => 'room:' + (crypto.randomUUID?.() || Date.now() + ':' + Math.random());
    function reset(owner = '') {
      abort();
      state.owner = owner;
      state.rooms = [];
      state.roomsCursor = null;
      state.includeArchived = false;
      state.room = null;
      state.roomId = '';
      state.messages = [];
      state.works = [];
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
      state.room = room;
      const index = state.rooms.findIndex((item) => item.id === room.id);
      if (index < 0) state.rooms.unshift(room);
      else state.rooms[index] = room;
      emit('room');
    }
    function mergeMessages(items) {
      const indexed = new Map(state.messages.map((item) => [item.id, item]));
      let confirmed = false;
      for (const item of items || []) {
        if (!item?.id) continue;
        const previous = indexed.get(item.id);
        if (previous && Number(previous.version || 0) > Number(item.version || 0)) continue;
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
      state.messages = [...indexed.values()].sort(
        (a, b) =>
          Number(a.server_sequence || a.sequence || a.created_at || a.created || 0) -
            Number(b.server_sequence || b.sequence || b.created_at || b.created || 0) ||
          a.id.localeCompare(b.id),
      );
      emit('messages');
      if (confirmed) emit('sent');
    }
    function applyWork(work) {
      if (!work?.id) return;
      const index = state.works.findIndex((item) => item.id === work.id);
      if (index < 0) state.works.push(work);
      else {
        const previous = state.works[index];
        const previousIntent = Number(previous.intent_revision || 0);
        const incomingIntent = Number(work.intent_revision || 0);
        if (
          incomingIntent > previousIntent ||
          (incomingIntent === previousIntent &&
            Number(work.version || 0) >= Number(previous.version || 0))
        )
          state.works[index] = work;
      }
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
      state.roomId = id || '';
      state.room = null;
      state.messages = [];
      state.works = [];
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
      return read(older ? 'older' : 'messages', path() + '/messages?limit=50' + suffix, (value) => {
        if (
          value.revision !== undefined &&
          Number(value.revision) <
            Math.max(state.messageRevision, Number(state.room?.revision || 0))
        )
          return;
        if (value.revision !== undefined) state.messageRevision = Number(value.revision);
        const visibility = value.visibility_revision || value.scope_revision;
        const reading = state.drafts.get(state.roomId)?.reading;
        if (reading?.visibilityRevision && visibility && reading.visibilityRevision !== visibility)
          delete state.drafts.get(state.roomId).reading;
        if (visibility && state.visibilityRevision && visibility !== state.visibilityRevision) {
          state.messages = [];
          state.works = [];
          state.cursor = null;
          emit('works');
        }
        if (visibility) state.visibilityRevision = visibility;
        mergeMessages(value.messages);
        for (const work of value.works || []) applyWork(work);
        if (older || state.cursor === null) state.cursor = value.next_cursor || '';
        emit('page');
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
          files: [],
        });
      return state.drafts.get(id);
    }
    function fileChanged(entry) {
      if (entry.owner === state.owner && entry.roomId === state.roomId) emit('draft');
    }
    function addFiles(files, roomId = state.roomId) {
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
      entry.state = 'uploading';
      entry.error = '';
      fileChanged(entry);
      entry.promise = request(path(entry.roomId) + '/attachments', {
        method: 'POST',
        body: entry.file,
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
        attachments: (value.files || [])
          .filter((entry) => entry.state === 'ready')
          .map((entry) => ({ id: entry.attachment.id, name: entry.attachment.name || entry.name })),
      };
    }
    function sameDraft(receipt, value = draft()) {
      const next = snapshot(value);
      return (
        ['text', 'member', 'reply', 'work'].every((field) => receipt.body[field] === next[field]) &&
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
        )
      )
        Object.assign(originalDraft, { text: '', reply: '', work: '', workVersion: 0 });
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
      if (!receipt && draft().files.some((entry) => entry.state !== 'ready'))
        throw new Error('请等待附件上传完成，或移除未完成的附件。');
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
      reset,
      abort,
      invalidateReads,
      list,
      select,
      refresh,
      messages,
      draft,
      addFiles,
      removeFile,
      upload,
      sameDraft,
      send,
      completeReceipt,
      applyRoom,
      applyWork,
      mergeMessages,
      on(fn) {
        state.listeners.add(fn);
        return () => state.listeners.delete(fn);
      },
    };
  }
  return { create };
})();
