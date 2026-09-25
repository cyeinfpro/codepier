'use strict';
// Read-only discovery has its own bounded lifetime. Never reuse mutation retry
// semantics, persist failed promises, or publish responses from an older login.
function chatCatalogLocation(
  project = ChatUI.project,
  cli = ChatUI.selected?.provider || ChatUI.provider,
  cwd = ChatUI.cwd,
) {
  const p = S.projects.find((p) => p.id === project);
  return {
    project,
    cli,
    cwd,
    scope: JSON.stringify([p?.device_id || project, cli]),
    context: JSON.stringify([project, p?.root || '', cwd]),
  };
}
function chatCatalogCache(location) {
  const entries = ChatUI.catalogCache;
  if (!entries.has(location.scope)) {
    if (entries.size >= 64) {
      const first = entries.keys().next().value;
      for (const request of entries.get(first).pending.values()) request.cancel();
      entries.delete(first);
    }
    entries.set(location.scope, {
      data: null,
      contexts: new Map(),
      levels: new Map(),
      pending: new Map(),
      revision: 0,
    });
  }
  return entries.get(location.scope);
}
function chatCatalogAbortAll() {
  for (const cache of ChatUI.catalogCache.values())
    for (const request of [...cache.pending.values()]) request.cancel();
}
function chatCacheCatalog(cache, context, data, model, refresh, commandsOnly = false) {
  if (refresh) cache.levels.clear();
  if (!commandsOnly) {
    cache.data = { cli: data.cli, models: data.models, capabilities: data.capabilities };
    const current = chatModelKey(data.model);
    if (current && Array.isArray(data.thinking_levels))
      cache.levels.set(current, data.thinking_levels);
  }
  const previous = cache.contexts.get(context) || {};
  if (!model || data.commands !== undefined) {
    if (cache.contexts.size >= 64 && !cache.contexts.has(context))
      cache.contexts.delete(cache.contexts.keys().next().value);
    cache.contexts.set(
      context,
      commandsOnly
        ? { ...previous, commands: data.commands }
        : !model
          ? { ...previous, ...data, defaultsLoaded: true }
          : { ...previous, commands: data.commands },
    );
  }
}
function chatCachedCatalog(cache, context, model = '') {
  if (!cache.data) return null;
  const local = cache.contexts.get(context),
    data = { ...local, ...cache.data };
  data.models = data.models.map((m) => {
    const known = cache.levels.get(chatModelKey(m));
    return known ? { ...m, thinkingLevels: known } : m;
  });
  if (model) {
    data.model =
      data.models.find((m) => chatModelKey(m) === model || m.id === model || m.model === model) ||
      model;
    data.thinking_levels =
      cache.levels.get(chatModelKey(data.model)) || data.model?.supportedReasoningEfforts || [];
    data.thinkingLevel = '';
    delete data.reasoningEffort;
    delete data.effort;
  }
  return data;
}
function chatCatalogReady(cache, location, model, requireDefaults) {
  if (!cache.data) return false;
  if (!model) return !requireDefaults || !!cache.contexts.get(location.context)?.defaultsLoaded;
  const row = cache.data.models.find(
    (m) => chatModelKey(m) === model || m.id === model || m.model === model,
  );
  return (
    cache.levels.has(model) ||
    (!!row && (Array.isArray(row.supportedReasoningEfforts) || location.cli === 'codex'))
  );
}
function chatCatalogRequestKey(
  location,
  model = '',
  includeCommands = false,
  requireDefaults = false,
) {
  return JSON.stringify([
    includeCommands || requireDefaults ? location.context : '',
    model,
    includeCommands,
  ]);
}
function chatRequestCatalog(
  cache,
  location,
  { model = '', refresh = false, includeCommands = false, requireDefaults = false } = {},
) {
  const { scope, context, project, cli, cwd } = location,
    identity = S.session;
  const key = chatCatalogRequestKey(location, model, includeCommands, requireDefaults),
    old = cache.pending.get(key);
  // A deliberate refresh supersedes a stalled ordinary read. Repeated clicks
  // on the same refresh still join one probe instead of spawning processes.
  if (old && (!refresh || old.refresh)) return old.promise;
  if (old) old.cancel();
  const revision = refresh && !includeCommands ? ++cache.revision : cache.revision;
  const controller = new AbortController(),
    record = { refresh, controller, alive: true, promise: null, cancel: null };
  let rejectDeadline, timer;
  const deadline = new Promise((resolve, reject) => {
    rejectDeadline = reject;
  });
  record.cancel = () => {
    if (!record.alive) return;
    record.alive = false;
    controller.abort();
    if (cache.pending.get(key) === record) cache.pending.delete(key);
    clearTimeout(timer);
    rejectDeadline(Object.assign(new Error('目录查询已被新的请求替代'), { name: 'AbortError' }));
  };
  timer = setTimeout(() => {
    if (!record.alive) return;
    record.alive = false;
    controller.abort();
    rejectDeadline(
      Object.assign(new Error('读取本机模型超时。请确认节点在线后重试，无需刷新页面。'), {
        code: 'CLI_CATALOG_TIMEOUT',
      }),
    );
  }, 22000);
  const request = api('/api/native/chat_catalog', {
    method: 'POST',
    body: JSON.stringify({
      project,
      args: { cli, cwd, ...(model ? { model } : {}), refresh, include_commands: includeCommands },
    }),
    retrySafe: true,
    retryDelays: [],
    requestTimeout: 18000,
    signal: controller.signal,
  });
  record.promise = Promise.race([request, deadline])
    .then((data) => {
      if (!data || !Array.isArray(data.models))
        throw new Error('本机未返回有效的模型目录，请重试或检查 CLI 安装。');
      const valid = {
        ...data,
        models: data.models.filter((m) => m && typeof m === 'object' && chatModelKey(m)),
      };
      const now = chatCatalogLocation(project, cli, cwd);
      if (
        record.alive &&
        S.session === identity &&
        ChatUI.catalogCache.get(scope) === cache &&
        now.scope === scope &&
        now.context === context
      ) {
        if (revision === cache.revision)
          chatCacheCatalog(cache, context, valid, model, refresh && !includeCommands);
        else if (includeCommands) chatCacheCatalog(cache, context, valid, model, false, true);
      }
      return valid;
    })
    .catch((error) => {
      if (error.code === 'NETWORK_UNCERTAIN')
        throw Object.assign(
          new Error('模型目录连接暂时不可用。重试会重新读取，不会创建会话或发送消息。'),
          { code: 'CLI_CATALOG_TIMEOUT' },
        );
      throw error;
    })
    .finally(() => {
      record.alive = false;
      clearTimeout(timer);
      if (cache.pending.get(key) === record) cache.pending.delete(key);
    });
  cache.pending.set(key, record);
  return record.promise;
}
function chatRenderCatalog(v, cache, location, model) {
  const context = location.scope + '\n' + location.context;
  if (v.catalogContext && v.catalogContext !== context) {
    v.commands = null;
    v.commandsError = '';
  }
  v.catalogContext = context;
  v.catalog = chatCachedCatalog(cache, location.context, model);
  if (v.catalog?.commands !== undefined && (!ChatUI.selected || v.commands === null))
    v.commands = v.catalog.commands;
  chatSettings(v.settings);
  if ($('#chat-model-search')) chatRenderModels($('#chat-model-search').value);
  if (ChatUI.inspector) chatInspectorRender();
  const refreshing = !!v.catalogRefreshing;
  for (const id of ['chat-model-refresh', 'chat-catalog-retry']) {
    const button = $('#' + id);
    if (!button) continue;
    button.disabled = refreshing;
    button.setAttribute('aria-busy', String(refreshing));
    if (id === 'chat-catalog-retry') button.textContent = refreshing ? '正在重试…' : '重新加载';
  }
}
async function chatLoadCatalog(refresh = false, model, requireDefaults = false) {
  const c = ChatUI,
    v = chatView(),
    gen = c.generation,
    ticket = ++c.catalogTicket,
    location = chatCatalogLocation();
  if (!location.project) return;
  model = model === undefined ? v.selectedModel || '' : model;
  const cache = chatCatalogCache(location),
    ready = chatCatalogReady(cache, location, model, requireDefaults);
  v.catalogError = '';
  v.catalogLoading = refresh || !ready;
  v.catalogRefreshing = refresh;
  chatRenderCatalog(v, cache, location, model);
  const pending = cache.pending.get(chatCatalogRequestKey(location, model, false, requireDefaults));
  if (!v.catalogLoading && !pending) return;
  try {
    await (!refresh && pending
      ? pending.promise
      : chatRequestCatalog(cache, location, { model, refresh, requireDefaults }));
  } catch (error) {
    if (chatCurrent(gen, v) && ticket === c.catalogTicket && error.name !== 'AbortError')
      v.catalogError = error.message;
  } finally {
    if (chatCurrent(gen, v) && ticket === c.catalogTicket) {
      v.catalogLoading = false;
      v.catalogRefreshing = false;
      chatRenderCatalog(v, cache, location, model);
    }
  }
}
async function chatLoadCommands(refresh = false) {
  const v = chatView(),
    gen = ChatUI.generation,
    location = chatCatalogLocation(),
    cache = chatCatalogCache(location);
  if (
    !location.project ||
    (v.commandsLoading && v.commandsGeneration === gen) ||
    (!refresh && v.commands !== null)
  )
    return;
  const cached = cache.contexts.get(location.context)?.commands;
  if (!refresh && cached !== undefined) {
    v.commands = cached;
    return;
  }
  v.commandsLoading = true;
  v.commandsGeneration = gen;
  v.commandsError = '';
  try {
    const previous = v.commands,
      data = await chatRequestCatalog(cache, location, {
        refresh,
        includeCommands: true,
        requireDefaults: true,
      });
    const now = chatCatalogLocation();
    if (
      chatCurrent(gen, v) &&
      now.scope === location.scope &&
      now.context === location.context &&
      v.commands === previous
    )
      v.commands = data.commands || [];
  } catch (error) {
    if (chatCurrent(gen, v) && error.name !== 'AbortError') v.commandsError = error.message;
  } finally {
    if (v.commandsGeneration === gen) v.commandsLoading = false;
  }
  if (chatCurrent(gen, v)) {
    if (ChatUI.inspector === 'commands') chatInspectorRender();
    if (/^[/\$][^\s]*$/.test(v.draft) && !v.commandsError) chatSlash();
  }
}
