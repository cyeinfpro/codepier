"use strict";
window.CodePierRoles = (() => {
  const { roleActionLabels: labels, roleSummary: summary } = CodePierAccess;
  const note =
    "动态角色的能力和项目范围会影响所有已同意该角色的连接，无需重新 OAuth。旧固定授权保持原状，角色设置由你管理。";
  async function html() {
    const result = await api("/api/access-roles");
    return `<div id="roles-page">${heading("访问角色", "LIVE ROLE POLICY", "", `<button class="btn primary" id="role-create">新建角色</button>`)}${notice(note, true)}<section class="panel"><div class="panel-head"><h2>共用政策 · 独立身份</h2><span class="badge neutral">${result.roles.length} / ${result.limit}</span></div><div class="panel-body">${result.roles.map((role) => `<div class="grant-row"><div>${summary(role)}<small>${role.bound_profiles} 个身份 · ${role.active_grants} 个未撤销角色授权</small><p class="mono">${esc(role.id)}</p></div><button class="btn small" data-role-edit="${esc(role.id)}">编辑 / 暂停</button></div>`).join("") || empty("先建立 secretary 等角色，再将主秘书、小秘书 Profile 绑定到它。")}</div></section><div class="actions"><button class="btn" data-nav="profiles">管理身份 Profiles</button><button class="btn ghost" data-nav="connect">查看角色 MCP 地址</button></div></div>`;
  }
  function bind() {
    $("#role-create").onclick = () => edit();
    $$("[data-role-edit]").forEach((b) => {
      b.onclick = () => edit(b.dataset.roleEdit);
    });
  }
  function choices(items, selected, name) {
    return `<div class="check-list">${items.map((item) => `<label class="check"><input type="checkbox" name="${name}" value="${esc(item.id)}" ${selected.includes(item.id) ? "checked" : ""}>${esc(item.alias || item.name)}</label>`).join("") || "<small>尚无资源。</small>"}</div>`;
  }
  function projectRule(
    rule = {
      actions: ["read"],
      projects: [],
      all_projects: false,
      created_projects: false,
      excluded_projects: [],
    },
  ) {
    return `<fieldset data-project-rule class="role-rule"><legend>项目规则</legend><div class="actions"><button type="button" class="btn ghost small" data-remove-rule>移除此规则</button></div><div class="check-list">${Object.keys(
      labels,
    )
      .filter((a) => !a.includes("."))
      .map(
        (action) =>
          `<label class="check"><input type="checkbox" name="action" value="${action}" ${rule.actions.includes(action) ? "checked" : ""} ${action === "read" ? "disabled" : ""}>${labels[action]}</label>`,
      )
      .join(
        "",
      )}</div><label class="check"><input type="checkbox" name="all_projects" ${rule.all_projects ? "checked" : ""}>全部现有及未来项目</label><label class="check"><input type="checkbox" name="created_projects" ${rule.created_projects ? "checked" : ""}>包含本角色通过委派创建的项目</label><details ${!rule.all_projects ? "open" : ""}><summary>指定项目</summary>${choices(S.projects, rule.projects, "project")}</details><details><summary>此规则排除的项目</summary>${choices(S.projects, rule.excluded_projects, "excluded")}</details></fieldset>`;
  }
  function deviceRule(
    rule = {
      actions: ["devices.read"],
      devices: [],
      root_prefixes: [],
      max_project_mode: "read",
      allow_tasks: false,
    },
  ) {
    return `<fieldset data-device-rule class="role-rule"><legend>设备与项目创建委派</legend><div class="actions"><button type="button" class="btn ghost small" data-remove-rule>移除此规则</button></div><div class="check-list">${["devices.read", "projects.create"].map((a) => `<label class="check"><input type="checkbox" name="action" value="${a}" ${rule.actions.includes(a) ? "checked" : ""} ${a === "devices.read" ? "disabled" : ""}>${labels[a]}</label>`).join("")}</div>${choices(S.devices, rule.devices, "device")}<label class="field">新项目权限上限<select name="max_project_mode"><option value="read" ${rule.max_project_mode === "read" ? "selected" : ""}>只读</option><option value="write" ${rule.max_project_mode === "write" ? "selected" : ""}>可写</option></select></label><label class="check"><input type="checkbox" name="allow_tasks" ${rule.allow_tasks ? "checked" : ""}>允许新项目启用任务（仍需 Agent 允许）</label><label class="field">可映射目录上限（每行一个绝对路径）<textarea name="root_prefixes" rows="2" autocapitalize="none" autocorrect="off" spellcheck="false" placeholder="留空表示仍按该设备的 Agent 本机允许目录检查">${esc(rule.root_prefixes.join("\n"))}</textarea></label><p class="form-note">仅创建新映射，不创建磁盘目录、不改已有映射、不改角色、不扩大 Agent 权限。要让新项目可使用，另加「本角色创建的项目」或「全部项目」规则。</p></fieldset>`;
  }
  function syncMobileRuleBar(dialog) {
    const listNode = dialog.querySelector(".policy-rule-list");
    if (!listNode) return;
    let bar = dialog.querySelector(".policy-rule-mobile-bar");
    if (!bar) {
      bar = document.createElement("div");
      bar.className = "policy-rule-mobile-bar";
      listNode.before(bar);
    }
    const buttons = $$("button", listNode);
    if (!buttons.length) {
      bar.hidden = true;
      bar.replaceChildren();
      return;
    }
    bar.hidden = false;
    const activeIdx = Math.max(
      0,
      buttons.findIndex((b) => b.getAttribute("aria-pressed") === "true"),
    );
    bar.innerHTML =
      `<span class="policy-rule-count">共 ${buttons.length} 条规则 · 当前第 ${activeIdx + 1} 条</span>` +
      (buttons.length > 1
        ? `<select class="policy-rule-jumper" aria-label="快速切换规则">${buttons.map((b, i) => `<option value="${i}" ${i === activeIdx ? "selected" : ""}>${i + 1} · ${esc(b.textContent.trim())}</option>`).join("")}</select>`
        : "");
    const jumper = bar.querySelector(".policy-rule-jumper");
    if (jumper) {
      jumper.onchange = () => {
        const target = buttons[Number(jumper.value)];
        target?.click();
        target?.scrollIntoView?.({ block: "nearest" });
      };
    }
    if (!listNode.dataset.mobileRuleWired) {
      listNode.dataset.mobileRuleWired = "1";
      listNode.addEventListener("click", (ev) => {
        if (ev.target.closest("button")) syncMobileRuleBar(dialog);
      });
    }
  }
  const selected = (box, name) =>
    $$(`input[name="${name}"]:checked`, box).map((x) => x.value);
  async function edit(id = null) {
    const login = S.session,
      page = S.page,
      intent = S.modalIntent || 0;
    try {
      const role = id
        ? await api("/api/access-roles/" + encodeURIComponent(id))
        : null;
      if (
        login !== S.session ||
        page !== S.page ||
        intent !== (S.modalIntent || 0)
      )
        return;
      const key = crypto.randomUUID();
      let submission = null;
      const dialog = modal(
        role ? "编辑动态角色" : "新建动态角色",
        `<form id="role-form"><label class="field">角色名称<input name="label" maxlength="80" required value="${esc(role?.label || "")}" placeholder="例如 secretary / reviewer"></label><label class="check"><input type="checkbox" name="enabled" ${!role || role.enabled ? "checked" : ""}>启用角色</label><p class="form-note">${esc(note)}</p><p class="form-note">${role ? `本次修改会作用于 ${role.bound_profiles} 个身份、${role.active_grants} 个未撤销角色授权。` : ""}每条规则仅对它选择的资源生效；不同规则可以叠加，但不会把某项目的执行权套用到其他项目。</p><div id="role-project-rules">${(role?.project_rules || []).map(projectRule).join("")}</div><button class="btn small" type="button" id="role-add-project-rule">添加项目规则</button><div id="role-device-rules">${(role?.device_rules || []).map(deviceRule).join("")}</div><button class="btn small" type="button" id="role-add-device-rule">添加设备委派</button><p class="form-note">完整 Shell 不是项目沙箱。暂停只阻止后续操作，不会回滚或自动终止已开始的任务。</p><p id="role-save-status" class="form-note" role="status"></p></form>`,
        `<button class="btn ghost" data-action="close-modal">取消</button><button class="btn primary" type="submit" form="role-form">保存角色政策</button>`,
      );
      const form = $("#role-form", dialog),
        status = $("#role-save-status", dialog);
      const wire = (preferLast = false) => {
        $$("[data-remove-rule]", dialog).forEach((b) => {
          b.onclick = () => {
            b.closest("fieldset").remove();
            dialog.dataset.editorDirty = "true";
            uiPolicyEditor(dialog);
            (
              dialog.querySelector(
                '.policy-rule-list button[aria-pressed="true"]',
              ) || dialog.querySelector("#role-add-project-rule")
            ).focus();
          };
        });
        uiPolicyEditor(dialog, preferLast);
        syncMobileRuleBar(dialog);
      };
      $("#role-add-project-rule", dialog).onclick = () => {
        if ($$("[data-project-rule]", dialog).length >= 32) return;
        $("#role-project-rules", dialog).insertAdjacentHTML(
          "beforeend",
          projectRule(),
        );
        wire($("#role-project-rules", dialog).lastElementChild);
      };
      $("#role-add-device-rule", dialog).onclick = () => {
        if ($$("[data-device-rule]", dialog).length >= 32) return;
        $("#role-device-rules", dialog).insertAdjacentHTML(
          "beforeend",
          deviceRule(),
        );
        wire($("#role-device-rules", dialog).lastElementChild);
      };
      wire();
      form.onsubmit = (event) => {
        event.preventDefault();
        busy($('button[type="submit"]', dialog), async () => {
          if (!form.reportValidity()) return;
          const body = {
            label: form.elements.label.value,
            enabled: form.elements.enabled.checked,
            project_rules: $$("[data-project-rule]", form).map((b) => ({
              actions: selected(b, "action"),
              projects: selected(b, "project"),
              all_projects: $('[name="all_projects"]', b).checked,
              created_projects: $('[name="created_projects"]', b).checked,
              excluded_projects: selected(b, "excluded"),
            })),
            device_rules: $$("[data-device-rule]", form).map((b) => ({
              actions: selected(b, "action"),
              devices: selected(b, "device"),
              root_prefixes: $('[name="root_prefixes"]', b)
                .value.split("\n")
                .map((s) => s.trim())
                .filter(Boolean),
              max_project_mode: $('[name="max_project_mode"]', b).value,
              allow_tasks: $('[name="allow_tasks"]', b).checked,
            })),
            connector_rules: role?.connector_rules || [],
            ...(role
              ? { expected_version: role.version }
              : { idempotency_key: key }),
          };
          const signature = JSON.stringify(body);
          if (submission?.uncertain && submission.signature !== signature) {
            status.textContent =
              "上次请求尚需核对。请恢复原输入后重试，或先刷新角色列表核对；不能改用新参数重发。";
            return;
          }
          if (!submission || submission.signature !== signature)
            submission = { signature, body, uncertain: false };
          const controls = $$("input,select,textarea,button", form),
            disabled = controls.map((x) => x.disabled);
          controls.forEach((x) => {
            x.disabled = true;
          });
          status.textContent = "正在保存政策…";
          try {
            await api(
              "/api/access-roles" + (id ? "/" + encodeURIComponent(id) : ""),
              {
                method: id ? "PUT" : "POST",
                body: JSON.stringify(submission.body),
              },
            );
            if (login !== S.session || !dialog.isConnected) return;
            closeModal(dialog);
            toast("角色已保存；现有角色连接按新政策执行");
            if (S.page === page) await renderPage(false);
          } catch (error) {
            const ambiguous =
              error.status >= 500 ||
              error.status === 408 ||
              error.code === "NETWORK_UNCERTAIN";
            submission.uncertain =
              ambiguous ||
              ["IDEMPOTENCY_CONFLICT", "IDEMPOTENCY_RESPONSE_EXPIRED"].includes(
                error.code,
              );
            if (login === S.session && dialog.isConnected)
              status.textContent = ambiguous
                ? "保存结果未确认。请先核对角色列表；仅可使用原参数重试。"
                : error.message;
          } finally {
            if (login === S.session && dialog.isConnected)
              controls.forEach((x, i) => {
                x.disabled = disabled[i];
              });
          }
        });
      };
    } catch (error) {
      if (login === S.session) toast(error.message, true);
    }
  }
  return { html, bind, edit };
})();
