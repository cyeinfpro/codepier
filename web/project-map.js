"use strict";
// Read an Agent's existing map. No build, model call, or local persistence.
window.CodePierProjectMap = (() => {
  let generation = 0,
    owner = null,
    space = "",
    project = "",
    query = "",
    result = null;
  let busy = false,
    message = "",
    operation = "",
    previous = [],
    audit = null,
    auditCursor = "",
    auditPrevious = [],
    auditError = "",
    auditBusy = false;
  let workspace = "",
    workspaces = [],
    workspaceState = "unread",
    workspaceBusy = false,
    workspaceMessage = "",
    workspaceOperation = "";
  let host = null,
    auditSequence = 0,
    controlsCollapsed = false;
  const isNarrowMap = () =>
    typeof window !== "undefined" &&
    !!window.matchMedia?.("(max-width: 760px)")?.matches;
  const scrollMapResults = () => {
    const target = host?.querySelector(".map-results");
    target?.scrollIntoView?.({
      behavior: window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches
        ? "auto"
        : "smooth",
      block: "start",
    });
  };
  const safe = (value) => esc(String(value ?? ""));
  const isCurrent = (seq, login, selectedSpace, target, selectedWorkspace) =>
    seq === generation &&
    S.session === login &&
    S.space_id === selectedSpace &&
    S.page === "project-map" &&
    project === target &&
    workspace === selectedWorkspace &&
    host?.isConnected;
  const list = (values) =>
    (Array.isArray(values) ? values : [])
      .map((value) =>
        typeof value === "string" ? value : JSON.stringify(value),
      )
      .join(", ");
  function detach() {
    if (busy) message = "本页已停止等待，原操作保留；可在地图调用记录核实。";
    if (workspaceBusy)
      workspaceMessage =
        "本页已停止目录列表等待，原操作" +
        (workspaceOperation ? " " + workspaceOperation : "") +
        "保留；可在操作审计核实。";
    generation++;
    auditSequence++;
    busy = false;
    auditBusy = false;
    workspaceBusy = false;
    host = null;
  }
  function html() {
    detach();
    if (owner !== S.session || space !== S.space_id) {
      owner = S.session;
      space = S.space_id;
      project = "";
      workspace = "";
      workspaces = [];
      workspaceState = "unread";
      workspaceMessage = "";
      query = "";
      result = null;
      operation = "";
      message = "";
      previous = [];
      audit = null;
      auditCursor = "";
      auditPrevious = [];
      auditError = "";
      controlsCollapsed = false;
    }
    if (!S.projects.some((p) => p.id === project)) {
      project = S.projects[0]?.id || "";
      workspace = "";
      workspaces = [];
      workspaceState = "unread";
      workspaceMessage = "";
      result = null;
      audit = null;
      previous = [];
      controlsCollapsed = false;
    }
    return (
      heading("项目地图", "只读浏览电脑上已有的项目地图，无需模型。") +
      '<div id="project-map-center"></div>'
    );
  }
  function entriesHTML() {
    if (!result)
      return '<p class="form-note">选择项目后点击“读取现有地图”。尚未读取不代表地图不存在。</p>';
    if (result.state === "missing")
      return '<div class="map-empty"><h2>该项目暂无地图</h2><p>电脑返回未找到现有地图。此页不会自动创建或构建。</p></div>';
    if (result.state !== "ready")
      return '<p role="alert">地图状态未能确认，请查看原操作记录。</p>';
    const entries = Array.isArray(result.entries) ? result.entries : [];
    return (
      '<div class="map-result-head"><h2>现有地图</h2><span class="badge neutral">仅核验当前页文件</span></div>' +
      '<p class="form-note">版本 <code>' +
      safe(result.revision) +
      "</code>" +
      (result.generated_at
        ? " · 生成于 " + safe(timeText(result.generated_at))
        : "") +
      "。不代表整个仓库都与地图一致。</p>" +
      (result.refresh_required
        ? '<p role="status">当前页含已变化或缺失文件，旧摘要不作为当前事实。</p>'
        : "") +
      (entries.length
        ? '<ul class="project-map-entries">' +
          entries
            .map((entry) => {
              const states = {
                current: "文件已核验",
                stale: "文件已变化",
                missing: "文件缺失",
              };
              const label = states[entry.state] || "状态未知";
              return (
                '<li><div class="map-entry-head"><h3><code>' +
                safe(entry.path) +
                '</code></h3><span class="badge ' +
                (entry.state === "current" ? "neutral" : "failed") +
                '">' +
                label +
                "</span></div>" +
                (entry.state === "current"
                  ? "<p>" +
                    safe(
                      entry.summary ||
                        (entry.content_omitted
                          ? "内容超出本次读取预算，仅返回文件来源。"
                          : "此条目没有摘要。"),
                    ) +
                    "</p>"
                  : '<p class="form-note">不展示旧摘要，请先核对文件与地图版本。</p>') +
                '<details><summary>文件来源与关联</summary><dl class="map-entry-facts"><dt>地图 SHA</dt><dd><code>' +
                safe(entry.sha256 || "未提供") +
                "</code></dd>" +
                (entry.current_sha256
                  ? "<dt>当前 SHA</dt><dd><code>" +
                    safe(entry.current_sha256) +
                    "</code></dd>"
                  : "") +
                (entry.state === "current"
                  ? "<dt>符号</dt><dd>" +
                    safe(list(entry.symbols) || "未提供") +
                    "</dd><dt>引用</dt><dd>" +
                    safe(list(entry.imports) || "未提供") +
                    "</dd><dt>关联文件</dt><dd>" +
                    safe(list(entry.related_paths) || "未提供") +
                    "</dd>"
                  : "") +
                "</dl></details></li>"
              );
            })
            .join("") +
          "</ul>"
        : '<p class="form-note">当前查询没有匹配条目；不代表项目没有地图。</p>') +
      (result.budget_limited
        ? '<p role="status">本次内容预算不足，未返回可继续的页码。请缩小查询范围。</p>'
        : "") +
      '<div class="pagination"><span>匹配 ' +
      safe(result.total_matches ?? "未知") +
      " 项 · 本页 " +
      entries.length +
      ' 项</span><div class="actions">' +
      '<button class="btn ghost small" data-map="previous" ' +
      (!previous.length || busy ? "disabled" : "") +
      ">上一页</button>" +
      '<button class="btn ghost small" data-map="next" ' +
      (!(
        Number.isInteger(result.next_offset) &&
        result.next_offset > result.offset
      ) || busy
        ? "disabled"
        : "") +
      ">下一页</button></div></div>"
    );
  }
  function auditHTML() {
    const rows = (audit?.operations || []).filter((r) =>
      ["project_knowledge_query", "project_knowledge_build"].includes(r.tool),
    );
    return (
      '<section class="panel map-audit"><div class="panel-head"><h2>地图调用记录</h2><button class="btn ghost small" data-map="audit-refresh" ' +
      (auditBusy ? "disabled" : "") +
      ">刷新记录</button></div>" +
      '<p class="form-note">当前项目全部工作目录的地图读取与构建，按原始工具名称分类。原操作与日志保留，不混入普通工具列表。</p>' +
      (auditError
        ? '<p role="alert">' + safe(auditError) + "</p>"
        : !audit
          ? '<p class="form-note">正在读取记录…</p>'
          : rows.length
            ? '<ul class="map-audit-rows">' +
              rows
                .map(
                  (r) =>
                    "<li><div><strong>" +
                    (r.tool === "project_knowledge_build"
                      ? "构建地图"
                      : "读取地图") +
                    '</strong><span class="muted">' +
                    safe(timeText(r.created || r.created_at || r.started)) +
                    " · " +
                    safe(
                      {
                        system_maintenance: "系统维护",
                        request: "请求触发",
                        unknown: "来源未记录",
                      }[r.trigger_source] || "来源未记录",
                    ) +
                    "</span></div><div>" +
                    badge(r.state) +
                    '<button class="btn ghost small" data-action="operation-detail" data-id="' +
                    safe(r.id) +
                    '">查看原操作</button></div></li>',
                )
                .join("") +
              "</ul>"
            : '<p class="form-note">当前项目暂无地图调用记录。</p>') +
      '<div class="pagination"><span>' +
      (audit ? "共 " + safe(audit.total ?? "未知") + " 条" : "") +
      '</span><div class="actions">' +
      '<button class="btn ghost small" data-map="audit-prev" ' +
      (!auditPrevious.length || auditBusy ? "disabled" : "") +
      ">上一页</button>" +
      '<button class="btn ghost small" data-map="audit-next" ' +
      (!audit?.next_cursor || auditBusy ? "disabled" : "") +
      ">下一页</button></div></div></section>"
    );
  }
  function mapControlsHTML(selected, selectedWorkspace) {
    const workspaceLabel = selectedWorkspace
      ? selectedWorkspace.label || selectedWorkspace.workspace_id
      : "项目根目录";
    const summaryText = selected
      ? selected.alias +
        " / " +
        workspaceLabel +
        (query.trim() ? " · " + query.trim() : "")
      : "选择目标项目";
    const quickDisabled =
      !project ||
      busy ||
      workspaceBusy ||
      (workspace && selectedWorkspace?.state !== "ready");
    return (
      '<section class="panel map-controls" data-has-project="' +
      Boolean(project) +
      '" data-collapsed="' +
      Boolean(project && controlsCollapsed) +
      '"><div class="map-mobile-summary"><button type="button" class="btn ghost small map-breadcrumb-trigger" data-map="toggle-controls" aria-expanded="' +
      String(!(project && controlsCollapsed)) +
      '"><span class="map-breadcrumb-text">' +
      safe(summaryText) +
      '</span><span class="map-breadcrumb-action">' +
      (project && controlsCollapsed ? "修改条件" : "收起条件") +
      "</span></button>" +
      (project && controlsCollapsed
        ? '<button type="button" class="btn primary small" data-map="quick-read" ' +
          (quickDisabled ? "disabled" : "") +
          ">读取地图</button>"
        : "") +
      '</div><div class="map-controls-body"><div class="map-fields map-target-fields"><label>项目<select id="map-project" aria-label="地图项目">' +
      S.projects
        .map(
          (p) =>
            '<option value="' +
            safe(p.id) +
            '" ' +
            (p.id === project ? "selected" : "") +
            ">" +
            safe(p.alias) +
            "</option>",
        )
        .join("") +
      '</select></label><label>工作目录<select id="map-workspace" aria-label="地图工作目录"><option value="">项目根目录</option>' +
      workspaces
        .map(
          (w) =>
            '<option value="' +
            safe(w.workspace_id) +
            '" ' +
            (w.workspace_id === workspace ? "selected" : "") +
            (w.state !== "ready" ? " disabled" : "") +
            ">" +
            safe(w.label || w.workspace_id) +
            "</option>",
        )
        .join("") +
      '</select></label><button class="btn ghost" data-map="workspaces" ' +
      (busy || workspaceBusy ? "disabled" : "") +
      '>读取工作目录</button></div><div class="map-fields map-query-fields"><label>查找文件或内容<input id="map-query" maxlength="300" autocapitalize="none" autocorrect="off" spellcheck="false" enterkeyhint="search" value="' +
      safe(query) +
      '" ' +
      (busy ? "disabled" : "") +
      ' placeholder="文件路径、符号或关键词"></label>' +
      '<button class="btn primary" data-map="read" ' +
      (quickDisabled ? "disabled" : "") +
      ">读取现有地图</button>" +
      (busy || workspaceBusy
        ? '<button class="btn ghost" data-map="stop">停止等待</button>'
        : "") +
      "</div>" +
      '<p class="form-note map-target">' +
      (selected
        ? safe(selected.device_name) +
          " · " +
          safe(
            selectedWorkspace?.path ||
              (workspace ? "隔离目录路径未确认" : selected.root),
          ) +
          " · " +
          (selected.online ? "在线" : "离线；读取可能需要等待重连")
        : "暂无可访问项目") +
      '</p><p class="form-note map-workspace-status">' +
      safe(
        workspaceMessage ||
          (workspaceState === "unread"
            ? "尚未读取隔离工作目录列表；当前选择不代表所有地图。"
            : workspaceState === "ready"
              ? "已读取就绪工作目录列表；地图是否存在需逐个只读核实。"
              : "工作目录列表状态未知。"),
      ) +
      "</p></div></section>"
    );
  }
  function paint() {
    if (!host?.isConnected) return;
    const selected = S.projects.find((p) => p.id === project);
    const selectedWorkspace = workspaces.find(
      (w) => w.workspace_id === workspace,
    );
    host.innerHTML =
      mapControlsHTML(selected, selectedWorkspace) +
      '<section class="panel map-results" aria-busy="' +
      busy +
      '"><p class="map-status" role="status">' +
      safe(message) +
      "</p>" +
      (operation
        ? '<p class="form-note">原操作 <code>' + safe(operation) + "</code></p>"
        : "") +
      entriesHTML() +
      "</section>" +
      (project ? auditHTML() : "");
    host.querySelector("#map-project").onchange = (e) => {
      generation++;
      auditSequence++;
      auditBusy = false;
      project = e.target.value;
      workspace = "";
      workspaces = [];
      workspaceState = "unread";
      workspaceMessage = "";
      workspaceBusy = false;
      query = "";
      result = null;
      previous = [];
      busy = false;
      message = "";
      operation = "";
      audit = null;
      auditCursor = "";
      auditPrevious = [];
      auditError = "";
      controlsCollapsed = Boolean(project) && isNarrowMap();
      paint();
      void loadAudit();
    };
    host.querySelector("#map-workspace").onchange = (e) => {
      generation++;
      workspace = e.target.value;
      result = null;
      previous = [];
      busy = false;
      workspaceBusy = false;
      message = "";
      operation = "";
      paint();
    };
    host.querySelector("#map-query").oninput = (e) => {
      query = e.target.value;
    };
    host.querySelector("#map-query").onkeydown = (e) => {
      if (e.key === "Enter" && !e.isComposing && !busy) {
        e.preventDefault();
        void read(0, "new");
      }
    };
    host.querySelectorAll("[data-map]").forEach(
      (button) =>
        (button.onclick = () => {
          const action = button.dataset.map;
          if (action === "toggle-controls") {
            controlsCollapsed = !controlsCollapsed;
            paint();
            if (!controlsCollapsed)
              host?.querySelector("#map-query")?.focus({ preventScroll: true });
            return;
          }
          if (action === "workspaces") void loadWorkspaces();
          if (action === "read" || action === "quick-read") void read(0, "new");
          if (action === "next") void read(result.next_offset, "next");
          if (action === "previous")
            void read(previous[previous.length - 1], "previous");
          if (action === "stop") {
            const stoppingWorkspace = workspaceBusy;
            generation++;
            busy = false;
            workspaceBusy = false;
            if (stoppingWorkspace)
              workspaceMessage =
                "已停止目录列表等待；原操作" +
                (workspaceOperation ? " " + workspaceOperation : "") +
                "可能继续，请在操作审计核实。";
            else
              message =
                "已停止本页等待；原操作可能继续，可在地图调用记录核实。";
            paint();
            void loadAudit();
          }
          if (action === "audit-refresh") void loadAudit();
          if (action === "audit-next") void loadAudit("next");
          if (action === "audit-prev") void loadAudit("previous");
        }),
    );
  }
  async function loadAudit(direction = "refresh") {
    if (auditBusy || !project) return;
    const seq = ++auditSequence,
      login = S.session,
      selectedSpace = S.space_id,
      target = project;
    const cursor =
      direction === "next"
        ? audit?.next_cursor
        : direction === "previous"
          ? auditPrevious[auditPrevious.length - 1]
          : auditCursor;
    if (cursor === undefined || cursor === null) return;
    const current = () =>
      seq === auditSequence &&
      S.session === login &&
      S.space_id === selectedSpace &&
      project === target &&
      S.page === "project-map" &&
      host?.isConnected;
    auditBusy = true;
    paint();
    try {
      const data = await api(
        "/api/call-log?" +
          new URLSearchParams({
            category: "project_map",
            project: target,
            limit: "20",
            cursor,
            include_filters: "false",
          }),
      );
      if (!current()) return;
      if (direction === "next") auditPrevious.push(auditCursor);
      if (direction === "previous") auditPrevious.pop();
      auditCursor = cursor;
      audit = data;
      auditError = "";
    } catch (e) {
      if (!current()) return;
      auditError = e.message + (audit ? "；页码未改变，仍保留上次记录。" : "");
    } finally {
      if (current()) {
        auditBusy = false;
        paint();
      }
    }
  }
  async function loadWorkspaces() {
    if (busy || workspaceBusy || !project) return;
    const seq = ++generation,
      login = S.session,
      selectedSpace = S.space_id,
      target = project,
      selectedWorkspace = workspace;
    const current = () =>
      isCurrent(seq, login, selectedSpace, target, selectedWorkspace);
    workspaceBusy = true;
    workspaceOperation = "";
    workspaceMessage = "正在读取现有工作目录列表…";
    paint();
    try {
      let out = await tool("worktrees_list", {
        project: target,
        workspace_id: "",
      });
      if (!current()) return;
      const id = out.operation_id;
      workspaceOperation = id || "";
      if (out.pending) {
        workspaceMessage = "正在等待目录列表原操作 " + (id || "编号未返回");
        paint();
      }
      while (out.pending) {
        if (!id) throw new Error("目录读取缺少操作编号，请先核实调用记录。");
        out = await tool("operations_wait", {
          operation_id: id,
          wait_seconds: 2,
        });
        if (!current()) return;
        if (!out.pending) {
          if (out.state !== "succeeded" || !out.result?.ok)
            throw new Error(out.result?.error?.message || "目录读取未完成");
          out = out.result.data;
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
      if (!current()) return;
      if (!Array.isArray(out.workspaces))
        throw new Error("未返回可确认的工作目录列表");
      const oldSelected = workspaces.find((w) => w.workspace_id === workspace);
      workspaces = out.workspaces.filter(
        (w) =>
          w.state === "ready" &&
          typeof w.workspace_id === "string" &&
          w.workspace_id,
      );
      workspaceState = "ready";
      const partial =
        (Array.isArray(out.record_errors)
          ? out.record_errors.length
          : !!out.record_errors) ||
        out.workspaces.length >= (Number(out.limit) || 100);
      workspaceMessage =
        "已读取 " +
        workspaces.length +
        " 个就绪隔离目录；最多列出100条受管目录，不代表全部历史地图。" +
        (partial ? "部分目录记录不可读或已达到上限，列表可能不完整。" : "");
      if (workspace && !workspaces.some((w) => w.workspace_id === workspace)) {
        workspaces.push({
          ...oldSelected,
          workspace_id: workspace,
          label: (oldSelected?.label || workspace) + "（不可用）",
          state: "unknown",
        });
        result = null;
        previous = [];
        message = "当前工作目录不在就绪列表，保留原选择。请显式选择其他目录。";
      }
    } catch (e) {
      if (!current()) return;
      workspaceState = "unknown";
      workspaceMessage =
        "工作目录列表未确认：" + e.message + "。不能据此判断是否存在地图。";
    } finally {
      if (
        seq === generation &&
        S.session === login &&
        S.space_id === selectedSpace &&
        S.page === "project-map" &&
        host?.isConnected
      ) {
        workspaceBusy = false;
        paint();
      }
    }
  }
  async function read(offset, direction) {
    if (busy || workspaceBusy || !project) return;
    if (
      workspace &&
      !workspaces.some(
        (w) => w.workspace_id === workspace && w.state === "ready",
      )
    ) {
      message = "当前工作目录不可用，请显式选择其他目录。";
      paint();
      return;
    }
    const seq = ++generation,
      login = S.session,
      selectedSpace = S.space_id,
      target = project,
      selectedWorkspace = workspace,
      search = query.trim();
    if (direction !== "new" && result?.mapQuery !== search) {
      message = "查询已改变，请重新读取第一页。";
      paint();
      return;
    }
    const old = result,
      oldOffset = old?.offset || 0;
    busy = true;
    message = "正在只读查询现有地图…";
    operation = "";
    if (direction === "new") {
      result = null;
      previous = [];
    }
    paint();
    try {
      let out = await tool("project_knowledge_query", {
        project: target,
        workspace_id: selectedWorkspace,
        query: search,
        paths: [],
        offset,
        limit: 20,
        max_chars: 16000,
      });
      if (!isCurrent(seq, login, selectedSpace, target, selectedWorkspace))
        return;
      operation = out.operation_id || "";
      while (out.pending) {
        if (!operation)
          throw new Error(
            "读取状态未确认且缺少操作编号；先核实调用记录，避免重复提交。",
          );
        message = "读取已保存，正在等待原操作结果。";
        paint();
        out = await tool("operations_wait", {
          operation_id: operation,
          wait_seconds: 2,
        });
        if (!isCurrent(seq, login, selectedSpace, target, selectedWorkspace))
          return;
        if (!out.pending) {
          if (out.state !== "succeeded" || !out.result?.ok)
            throw new Error(
              out.result?.error?.message ||
                out.error ||
                "原读取操作未成功，请查看地图调用记录。",
            );
          out = out.result.data;
          break;
        }
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
      if (!isCurrent(seq, login, selectedSpace, target, selectedWorkspace))
        return;
      if (!["ready", "missing"].includes(out?.state))
        throw new Error("返回内容不是可确认的项目地图，请查看原操作。");
      if (
        direction !== "new" &&
        old?.revision &&
        out.revision !== old.revision
      ) {
        result = null;
        previous = [];
        message = "地图版本已变化。请重新读取第一页，避免混用不同版本。";
      } else {
        result = { ...out, mapQuery: search };
        if (direction === "next") previous.push(oldOffset);
        if (direction === "previous") previous.pop();
        message =
          out.state === "ready" ? "已读取现有地图。" : "未找到现有地图。";
      }
    } catch (e) {
      if (!isCurrent(seq, login, selectedSpace, target, selectedWorkspace))
        return;
      message =
        "读取未完成：" + e.message + (result ? "。下方仍为上一次结果。" : "");
    } finally {
      if (isCurrent(seq, login, selectedSpace, target, selectedWorkspace)) {
        busy = false;
        if (result && isNarrowMap()) controlsCollapsed = true;
        paint();
        if (result && isNarrowMap()) scrollMapResults();
        void loadAudit();
      }
    }
  }
  function bind() {
    host = document.querySelector("#project-map-center");
    paint();
    if (project) void loadAudit();
  }
  return { html, bind, detach };
})();
