'use strict';
// Explicit owner controls. Opening/refreshing this view never sends or replays work.
window.CodePierCollaborationChannels = {
  status(value) {
    return (
      {
        active: '已启用',
        pairing: '等待微信配对',
        awaiting_owner: '等待核对联系人',
        paused: '已暂停',
        revoked: '已撤销',
        pending: '等待获取',
        ready: '附件可用',
        unavailable: '附件获取失败',
        expired: '附件已到保留期限',
        queued: '等待发送',
        preparing: '准备原投递',
        sending: '正在提交',
        accepted: '通道已接受（未证明送达）',
        owner_confirmed: '人工核对已收到',
        unknown: '发送结果未知，禁止自动重发',
        waiting_context: '等待原联系人上下文',
        waiting_media: '等待原附件',
        attention: '需要核对',
        needs_attention: '存在未完成分项',
        rejected: '分项未发送',
        cancelled: '已取消未发送部分',
        superseded: '已合并到较新进展',
      }[value] ||
      value ||
      '尚无证据'
    );
  },
  capabilities(value, E) {
    if (!value?.features) return '';
    const labels = {
      text: '文字',
      image: '图片',
      file: '文件',
      video: '视频',
      voice: '语音外发',
      edit: '原位编辑',
      typing: '输入状态',
      native_reply: '原生引用',
      receipt_lookup: '平台回执查询',
      read_receipt: '用户已读',
      preview: '在线预览',
      transcription: '音频转写',
    };
    const layers = {
      implemented: '当前实现',
      provider_account_verified: '账号实测',
      authorized: '当前授权',
      session_available: '当前会话',
    };
    const fact = (value) =>
      value === true ? '已核实' : value === false ? '不支持或不可用' : '尚未核实';
    return (
      '<details class="cc-channel-capabilities"><summary>当前通道能力与限制</summary><p>四项均有证据才显示可用；能力说明不会授予权限。</p><dl>' +
      Object.entries(labels)
        .map(([name, label]) => {
          const item = value.features[name] || {};
          return (
            '<dt>' +
            E(label) +
            '：' +
            E(item.enabled === true ? '可用' : fact(item.effective)) +
            '</dt><dd>' +
            Object.entries(layers)
              .map(([key, title]) => E(title + '：' + fact(item[key])))
              .join(' · ') +
            '</dd>'
          );
        })
        .join('') +
      '</dl><p>文本分段上限：' +
      E(value.text?.limit ?? '未知') +
      ' ' +
      E(value.text?.unit || '') +
      '；文件上传上限：' +
      E(value.media?.upload_limit ?? '未知') +
      ' 字节。</p>' +
      '<p>文件逐份核对当前收件人和内容摘要；上传成功不等于消息已发送。阶段反馈合并更新；无编辑能力时保留文字摘要。音频没有转写证据时只作为原音频附件。</p></details>'
    );
  },
  fileDetails(item, E) {
    const stamp = (value) =>
      Number.isFinite(value)
        ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false })
        : '尚未记录';
    const source = item.origin?.work_item_id || item.origin?.operation_id;
    const upload = {
      accepted: '上传已接受',
      combined_with_send: '上传与消息发送合并，暂无独立上传回执',
      unknown: '上传结果尚未确认',
    };
    return (
      '<details class="cc-file-manifest"><summary>文件来源与分享状态</summary><p>类型：' +
      E(item.mime || '尚未核实') +
      '；来源：' +
      E(source || (item.artifact_id ? '原产物' : '原通道附件')) +
      '</p><p>SHA-256：' +
      E(item.sha256 || '尚未取得原字节') +
      '</p><p>保留至：' +
      E(stamp(item.expires_at)) +
      '；到期不会重跑生成任务。</p>' +
      (item.kind === 'voice' ? '<p>原音频附件；尚无可验证转写。</p>' : '') +
      (item.shares || [])
        .map(
          (share) =>
            '<p>' +
            E(share.channel === 'telegram' ? 'Telegram' : '微信') +
            '：' +
            E(upload[share.upload_state] || upload.unknown) +
            '；消息：' +
            E(window.CodePierCollaborationChannels.status(share.send_state)) +
            '</p>',
        )
        .join('') +
      '<small>文件读取与外发批准分别核对。平台接受不代表用户已读。</small></details>'
    );
  },
  attachments(message, E) {
    const label = window.CodePierCollaborationChannels.status;
    const files = (message.attachments || [])
      .map((item) => {
        const valid = /^[a-f0-9]{32}$/.test(item.id || '');
        const href = valid
          ? '/api/collaboration/attachments/' +
            item.id +
            '/content?space_id=' +
            encodeURIComponent(S.space_id || 'legacy')
          : '';
        return `<div class="cc-channel-file" data-attachment-id="${E(item.id)}"><div><strong>${E(item.name || '附件')}</strong><small>${E(item.kind || 'file')} · ${E(label(item.state))}${Number.isFinite(item.size) ? ' · ' + E(String(item.size)) + ' 字节' : ''}</small>${item.reason_code ? `<small>${E(item.reason_code)}</small>` : ''}${window.CodePierCollaborationChannels.fileDetails(item, E)}</div>${item.state === 'ready' && valid ? `<a class="btn" href="${E(href)}" target="_blank" rel="noopener noreferrer" download>读取原附件</a>` : '<span>原消息与顺序已保留</span>'}</div>`;
      })
      .join('');
    const deliveries = (message.channel_deliveries || [])
      .map(
        (delivery) =>
          `<details class="cc-channel-delivery" ${['needs_attention', 'unknown', 'attention'].includes(delivery.state) ? 'open' : ''}><summary>${delivery.channel === 'telegram' ? 'Telegram' : '微信'}：${E(label(delivery.state))}</summary><p>正文与每件附件分别记录。通道接受不等于用户已读；未知投递不会自动重发。</p>${(delivery.parts || []).map((part) => `<div class="cc-channel-part">${E(String(part.ordinal + 1))}. ${part.kind === 'text' ? '正文' : '附件'} · ${E(label(part.state))}${part.reason_code ? ' · ' + E(part.reason_code) : ''}</div>`).join('')}</details>`,
      )
      .join('');
    return files + deliveries;
  },
  create(ctx) {
    const { state, E, field, button, mentionSlots, showDrawer, mutation } = ctx;
    let loaded = null,
      controller = null,
      saving = false;
    const local = { local: true };
    const allowed = () => !!state.snapshot?.can_manage;
    const scope = () => ({
      project: state.project,
      environment_id: state.environment,
      conversation_id:
        state.conversation || state.snapshot?.conversation?.id || state.snapshot?.room?.id || '',
    });
    const capture = () => ({
      generation: state.generation,
      session: S.session,
      space: S.space_id,
      ...scope(),
    });
    const current = (key) =>
      key &&
      key.generation === state.generation &&
      key.session === S.session &&
      key.space === S.space_id &&
      key.project === state.project &&
      key.environment_id === state.environment &&
      key.conversation_id === scope().conversation_id &&
      allowed();
    const select = (name, values) =>
      `<select name="${name}" required><option value="">请选择</option>${values.map(([id, title]) => `<option value="${E(id)}">${E(title)}</option>`).join('')}</select>`;
    const checkbox = (name, label, required = false) =>
      `<label class="cc-channel-check"><input type="checkbox" name="${name}" ${required ? 'required' : ''}>${E(label)}</label>`;
    const listState = (value) => E(window.CodePierCollaborationChannels.status(value));
    const stamp = (value) =>
      Number.isFinite(value)
        ? E(new Date(value * 1000).toLocaleString('zh-CN', { hour12: false }))
        : '尚未记录';
    function card() {
      return allowed() && state.snapshot?.capabilities?.room_scope_v1
        ? `<section class="cc-card cc-channel-settings"><h3>消息连接</h3><p>把微信或 Telegram 接入当前话题。</p>${button('dot-channel-open', '管理连接')}</section>`
        : '';
    }

    // Enrollment is memory-only. No token, QR payload, or verification code is
    // persisted in browser storage or interpolated into a remote image URL.
    let qr = null,
      qrTimer = null,
      qrWatch = null;
    const qrScope = (key) => ({
      project: key.project,
      environment_id: key.environment_id,
      conversation_id: key.conversation_id,
    });
    const qrId = () =>
      typeof crypto.randomUUID === 'function'
        ? crypto.randomUUID()
        : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
            const r = crypto.getRandomValues(new Uint8Array(1))[0] & 15;
            return (c === 'x' ? r : (r & 3) | 8).toString(16);
          });
    const qrLive = (q) =>
      qr === q &&
      current(q.key) &&
      loaded === q.owner &&
      loaded.epoch === state.drawerEpoch &&
      !!document.querySelector('.cc-wx-qr');
    const qrTerminal = (entry) => ['expired', 'cancelled', 'accepted'].includes(entry?.state);
    const qrPolling = (entry) => ['starting', 'wait', 'scaned'].includes(entry?.state);
    const qrRejected = (error) =>
      error?.status >= 400 && error.status < 500 && ![408, 429].includes(error.status);
    const qrRejectMessage = (error) =>
      ({
        WEIXIN_RENEWAL_CHANGED:
          '原连接版本或绑定已变化，本次重连未替换凭据。请取消并重新读取后扫码。',
        WEIXIN_RENEWAL_BOT_MISMATCH:
          '扫码返回的机器人与原连接不一致，未替换原凭据。请取消后核对目标。',
        WEIXIN_ACCOUNT_EXISTS:
          '此机器人已登记，本次扫码未覆盖原凭据。请取消本次扫码，核对已有连接。',
        WEIXIN_QR_NOT_CONFIRMED:
          '本次扫码已过期、状态已改变或尚未确认。请查询原状态，或取消后重新扫码。',
        WEIXIN_MAPPING_CHANGED: '当前项目范围已变化，本次操作未完成。请取消后重新读取通道设置。',
        WEIXIN_SCANNER_MISMATCH: '扫码账号编号与本次确认不一致。请重新查询并核对本人账号。',
        WEIXIN_VERIFY_CODE_INVALID: '验证码未被接受。请按手机微信显示的要求重新核对。',
        WEIXIN_ENROLLMENT_NOT_FOUND: '此扫码请求已不可用。请重新读取当前通道设置。',
      })[error?.code] || '服务端已拒绝此次操作。请核对当前身份、通道设置与扫码状态后重试。';
    function qrTimersOff() {
      clearTimeout(qrTimer);
      clearInterval(qrWatch);
      qrTimer = null;
      qrWatch = null;
    }
    function qrDiscard(q) {
      if (!q) return;
      if (qr === q) {
        qrTimersOff();
        qr = null;
      }
      q.discarded = true;
      // Never carry an old request into a new login, owner or room. A stale
      // start/status response may only cancel its own enrollment in its scope.
      if (
        current(q.key) &&
        q.entry?.enrollment_id &&
        !qrTerminal(q.entry) &&
        !q.busy &&
        !q.cancelAttempted
      ) {
        q.cancelAttempted = true;
        void mutation('weixin-qr-cancel', {
          ...qrScope(q.key),
          enrollment_id: q.entry.enrollment_id,
          expected_version: q.entry.version,
          idempotency_key: q.cancelKey,
        }).catch(() => {}); // Server expiry is the fallback, never a fresh start.
      }
      q.acceptArgs = null;
    }
    function qrImage(entry) {
      const image = entry?.qrcode_image || '';
      return typeof image === 'string' &&
        image.length <= 1500000 &&
        /^data:image\/svg\+xml;base64,[A-Za-z0-9+/]+={0,2}$/.test(image)
        ? '<img class="cc-wx-qr-image" alt="使用微信扫描此登录二维码" referrerpolicy="no-referrer" src="' +
            E(image) +
            '">'
        : '';
    }
    function qrPanel(w) {
      return (
        '<section class="cc-wx-onboarding"><h4>扫码登录微信</h4>' +
        '<p>用本人微信扫码、核对扫码账号，再绑定当前话题。无需手动查找机器人令牌。</p>' +
        '<div class="cc-wx-qr">' +
        qrMarkup(null, w) +
        '</div></section>'
      );
    }
    function qrMarkup(q, w = loaded?.weixin) {
      if (!q)
        return (
          '<div data-channel-form="qr-start">' +
          field(
            '连接方式',
            '<select name="renew_account_id"><option value="">登记新机器人连接</option>' +
              (w?.accounts || [])
                .filter((account) => account.state !== 'revoked')
                .map(
                  (account) =>
                    '<option value="' +
                    E(account.id) +
                    '">重新连接：' +
                    E(account.label || account.bot_id) +
                    ' · ' +
                    E(account.bot_id) +
                    '</option>',
                )
                .join('') +
              '</select>',
          ) +
          '<p>已有连接登录失效时，选择同一机器人重新扫码。最终替换会暂停原通道和任务执行，清除旧连接上下文；重新验证同一联系人后恢复原批准范围。扫码本身不会恢复或扩大权限。</p>' +
          '<p>开始后，本服务向腾讯官方 iLink 申请一次性登录二维码。扫码确认后，登录凭据只在服务端短时保留；你再次核对账号并确认，才会加密保存供后续连接使用。二维码五分钟内有效。</p>' +
          checkbox(
            'qr_storage',
            '我同意为此次连接处理登录凭据，并在核对扫码账号后加密保存，供本服务持续连接使用',
            true,
          ) +
          '<p>请在手机微信授权页阅读并确认其展示的条款与资格要求。</p>' +
          checkbox('qr_terms', '我会在手机微信授权页核对并确认展示的服务条款与使用资格', true) +
          button('dot-channel-qr-start', '扫码登录微信', {}, w?.enabled ? '' : 'disabled') +
          (!w?.enabled ? '<small>请先开启上方的微信通道开关。</small>' : '') +
          '<small>此步骤不绑定话题、不创建 MCP 凭据，也不授予代码执行或文件外发权限。</small></div>'
        );
      const e = q.entry,
        phase = e?.state || 'starting';
      const captions = {
        starting: '正在申请微信登录二维码…',
        wait: '请用本人微信扫码',
        scaned: '已扫码，请在微信中确认登录',
        need_verifycode: '微信要求输入验证码',
        confirmed: '微信已确认，请核对扫码账号',
        expired: '二维码已过期',
        cancelled: '本次扫码已取消',
        verify_code_blocked: '验证码尝试已停止，请重新扫码',
        binded_redirect: '此账号需要在微信中处理已有绑定',
        attention: '扫码状态需要核对',
        accepted: '登录凭据已保存',
      };
      const busy = q.busy ? 'disabled' : '';
      const status =
        (q.renew
          ? '<p>正在重新连接原机器人：<code>' +
            E(q.renew.bot_id) +
            '</code>。取消扫码不会更换原凭据；最终保存将暂停原通道和任务执行，清除旧连接上下文，需重新验证同一联系人后恢复原批准范围。</p>'
          : '') +
        '<p role="status" aria-live="polite">' +
        E(captions[phase] || captions.attention) +
        '</p>';
      const notice = q.notice
        ? '<p class="cc-wx-qr-notice" role="alert">' + E(q.notice) + '</p>'
        : '';
      if (!e)
        return (
          status +
          notice +
          (q.busy
            ? ''
            : q.startRejected
              ? button('dot-channel-qr-reset', '返回扫码设置')
              : button('dot-channel-qr-retry-start', '恢复本次二维码请求')) +
          '<small>请求未确认前不会另建登录。关闭后停止处理，未完成登录会到期失效。</small>'
        );
      const expires = '<small>有效至 ' + stamp(e.expires_at) + '</small>';
      let body = '';
      if (['starting', 'wait', 'scaned'].includes(phase)) {
        body = qrImage(e) || '<p>二维码暂不可显示，请查询本次状态；不会加载外部图片。</p>';
      } else if (phase === 'need_verifycode' && !q.verifyUncertain) {
        body =
          '<div data-channel-form="qr-verify">' +
          field(
            '微信验证码',
            '<input name="verify_code" maxlength="64" autocomplete="one-time-code" required>',
          ) +
          button('dot-channel-qr-verify', '提交本次验证码', {}, busy) +
          '</div>';
      } else if (phase === 'confirmed') {
        body =
          '<div data-channel-form="qr-accept"><p>扫码账号编号：<code>' +
          E(e.scanner_id) +
          '</code></p>' +
          field(
            '连接名称',
            '<input name="label" value="我的微信" maxlength="80" required autocomplete="off">',
          ) +
          (e.consent_receipt_id
            ? '<p>凭据保存与服务条款已分别记录。请核对以上本人扫码账号，点击下方按钮保存；不会新增任务权限。</p>'
            : checkbox(
                'qr_scanner',
                '我已核对以上完整账号编号，确认是本人，已在微信授权页确认相关条款，并同意加密保存凭据供本服务持续连接使用',
                true,
              )) +
          (q.renew
            ? checkbox(
                'qr_replace_existing',
                '我确认替换此原机器人登录凭据，暂停原通道和任务执行并清除旧连接上下文；重新验证同一联系人后恢复原批准范围，不重放旧消息或旧任务',
                true,
              )
            : '') +
          button(
            'dot-channel-qr-accept',
            q.acceptArgs
              ? '恢复原保存请求'
              : q.renew
                ? '确认本人并替换原连接'
                : '确认本人并保存连接',
            {},
            busy,
          ) +
          '<small>保存后还需选择此话题的 dot、生成配对码并核对联系人。</small></div>';
      } else if (phase === 'binded_redirect') {
        body =
          '<p>请先在微信客户端核对已有绑定，再取消本次请求并重新扫码。不会自动跳转或解除原绑定。</p>';
      } else if (phase === 'attention') {
        body = '<p>未确认完成登录。请查询原请求；不要在状态未知时创建新登录。</p>';
      }
      const controls =
        phase === 'accepted'
          ? button('dot-channel-qr-finish', '查看已保存连接并配对', {}, busy)
          : q.acceptArgs
            ? button('dot-channel-qr-status', '查询原保存状态', {}, busy)
            : qrTerminal(e)
              ? button('dot-channel-qr-new', '重新扫码', {}, busy)
              : button('dot-channel-qr-status', '查询本次状态', {}, busy) +
                button('dot-channel-qr-refresh', '取消旧码并重新扫码', {}, busy) +
                button('dot-channel-qr-cancel', '取消本次扫码', {}, busy);
      return status + notice + body + expires + '<div class="cc-actions">' + controls + '</div>';
    }
    function qrPaint(q, force = false) {
      if (!qrLive(q)) return;
      const target = document.querySelector('.cc-wx-qr');
      // Preserve a partially entered code/name during unchanged status polls.
      const signature = JSON.stringify([
        q.entry?.state,
        q.entry?.scanner_id,
        q.notice,
        q.entry?.qrcode_image,
        !!q.acceptArgs,
        !!q.verifyUncertain,
      ]);
      if (force || q.signature !== signature) {
        target.innerHTML = qrMarkup(q);
        q.signature = signature;
      }
      target.setAttribute('aria-busy', String(q.busy));
      for (const input of target.querySelectorAll('button,input'))
        input.disabled = q.busy || (!!q.acceptArgs && input.tagName === 'INPUT');
    }
    function qrSchedule(q) {
      clearTimeout(qrTimer);
      if (!qrLive(q) || !qrPolling(q.entry) || q.notice) return;
      qrTimer = setTimeout(() => {
        if (!qrLive(q)) {
          qrDiscard(q);
          return;
        }
        void qrStatus(q);
      }, 2000);
    }
    function qrWatchScope(q) {
      clearInterval(qrWatch);
      qrWatch = setInterval(() => {
        if (!qrLive(q)) qrDiscard(q);
        else if (
          !q.busy &&
          !q.acceptArgs &&
          q.entry &&
          !qrTerminal(q.entry) &&
          Number.isFinite(q.entry.expires_at) &&
          Date.now() >= q.entry.expires_at * 1000
        ) {
          q.entry = { ...q.entry, state: 'expired', qrcode_image: '' };
          clearTimeout(qrTimer);
          qrPaint(q);
        }
      }, 250);
    }
    function qrReadResult(q, result) {
      if (!result?.enrollment_id || !Number.isInteger(result.version))
        throw new Error('登录状态格式不完整');
      if (q.entry && result.enrollment_id !== q.entry.enrollment_id)
        throw new Error('登录请求编号不匹配');
      if (q.entry && result.version < q.entry.version) return;
      // Explicit public fields only; never retain any unexpected token field.
      q.entry = {
        enrollment_id: result.enrollment_id,
        state: result.state,
        version: result.version,
        expires_at: result.expires_at,
        qrcode_image: result.qrcode_image,
        scanner_id: result.scanner_id || '',
        reason_code: result.reason_code || '',
        account_id: result.account_id || '',
        renew_account_id: result.renew_account_id || '',
        expected_credential_version: result.expected_credential_version,
        renew_bot_id: result.renew_bot_id || '',
        re_pair_required: result.re_pair_required === true,
        consent_receipt_id: /^[a-f0-9]{32}$/.test(result.consent_receipt_id || '')
          ? result.consent_receipt_id
          : '',
      };
      if (qrTerminal(q.entry)) q.acceptArgs = null;
    }
    async function qrStart(q) {
      if (!qrLive(q) || q.busy) return local;
      q.busy = true;
      q.notice = '';
      qrPaint(q);
      try {
        const result = await mutation('weixin-qr-start', {
          ...qrScope(q.key),
          idempotency_key: q.startKey,
          ...(q.renew
            ? {
                renew_account_id: q.renew.id,
                expected_credential_version: q.renew.credential_version,
              }
            : {}),
          confirm_credential_storage: true,
          confirm_provider_terms: true,
        });
        qrReadResult(q, result);
      } catch (error) {
        q.startRejected = qrRejected(error);
        q.notice = q.startRejected
          ? qrRejectMessage(error)
          : '申请结果暂未确认。请恢复本次请求，系统会沿用原请求编号，不会重复申请。';
      } finally {
        q.busy = false;
      }
      if (!qrLive(q)) {
        qrDiscard(q);
        return local;
      }
      qrPaint(q);
      qrSchedule(q);
      return local;
    }
    async function qrStatus(q, verifyCode = '') {
      if (!qrLive(q) || q.busy || !q.entry || qrTerminal(q.entry)) return local;
      q.busy = true;
      q.notice = '';
      qrPaint(q);
      const args = {
        ...qrScope(q.key),
        enrollment_id: q.entry.enrollment_id,
        expected_version: q.entry.version,
        ...(verifyCode ? { verify_code: verifyCode } : {}),
      };
      try {
        // Verification codes must not enter mutation's persistent retry map.
        const result = await api('/api/collaboration/weixin-qr-status', {
          method: 'POST',
          body: JSON.stringify(args),
          retrySafe: false,
        });
        qrReadResult(q, result);
        q.verifyUncertain = false;
      } catch (error) {
        q.verifyUncertain = !qrRejected(error) && (!!verifyCode || q.verifyUncertain);
        q.notice = qrRejected(error)
          ? qrRejectMessage(error)
          : verifyCode
            ? '验证码提交结果暂未确认。请先查询本次状态，不会自动重交验证码。'
            : '连接暂时中断。请查询本次状态，不会创建新二维码。';
      } finally {
        q.busy = false;
      }
      if (!qrLive(q)) {
        qrDiscard(q);
        return local;
      }
      if (q.entry.state === 'accepted' && q.entry.account_id)
        return qrComplete(q, q.entry.account_id);
      qrPaint(q);
      qrSchedule(q);
      return local;
    }
    async function qrCancel(q, refresh = false) {
      if (!qrLive(q) || q.busy || !q.entry) return local;
      q.busy = true;
      q.notice = '';
      clearTimeout(qrTimer);
      qrPaint(q);
      try {
        qrReadResult(
          q,
          await mutation('weixin-qr-cancel', {
            ...qrScope(q.key),
            enrollment_id: q.entry.enrollment_id,
            expected_version: q.entry.version,
            idempotency_key: q.cancelKey,
          }),
        );
        if (!qrTerminal(q.entry)) throw new Error('取消尚未确认');
      } catch {
        q.notice = '尚未确认旧二维码已取消。请查询原状态后重试，未创建新二维码。';
        refresh = false;
      } finally {
        q.busy = false;
      }
      if (!qrLive(q)) {
        qrDiscard(q);
        return local;
      }
      if (q.entry.state === 'accepted') return qrComplete(q, q.entry.account_id);
      if (refresh && qrTerminal(q.entry)) {
        qrTimersOff();
        qr = null;
        document.querySelector('.cc-wx-qr').innerHTML = qrMarkup(null);
      } else qrPaint(q);
      return local;
    }
    async function qrAccept(q, data) {
      if (!qrLive(q) || q.busy || q.entry?.state !== 'confirmed' || !q.entry.scanner_id)
        return local;
      if (!q.acceptArgs) {
        if (
          (!q.entry.consent_receipt_id && !data.checked('qr_scanner')) ||
          (q.renew && !data.checked('qr_replace_existing'))
        )
          return local;
        q.acceptArgs = {
          ...qrScope(q.key),
          enrollment_id: q.entry.enrollment_id,
          expected_version: q.entry.version,
          idempotency_key: q.acceptKey,
          label: data.value('label').trim(),
          confirm_scanner_id: q.entry.scanner_id,
          ...(q.renew ? { confirm_replace_existing: data.checked('qr_replace_existing') } : {}),
          ...(q.entry.consent_receipt_id
            ? { consent_receipt_id: q.entry.consent_receipt_id }
            : { confirm_credential_storage: true, confirm_provider_terms: true }),
        };
      }
      q.busy = true;
      q.notice = '';
      clearTimeout(qrTimer);
      qrPaint(q);
      let result;
      try {
        result = await mutation('weixin-qr-accept', q.acceptArgs);
      } catch (error) {
        if (qrRejected(error)) {
          q.acceptArgs = null;
          q.acceptKey = qrId();
          q.entry = { ...q.entry, state: 'attention', scanner_id: '', qrcode_image: '' };
          q.notice = qrRejectMessage(error);
        } else
          q.notice = '保存结果暂未确认。请恢复原保存请求或查询原保存状态，不要重新扫码或重复绑定。';
      } finally {
        q.busy = false;
      }
      if (!qrLive(q)) {
        qrDiscard(q);
        return local;
      }
      if (!result?.account_id) {
        qrPaint(q);
        return local;
      }
      return qrComplete(q, result.account_id);
    }
    async function qrComplete(q, accountId) {
      if (!qrLive(q)) return local;
      q.entry.state = 'accepted';
      qrTimersOff();
      qr = null;
      await open(document.querySelector('[data-cc-action="dot-channel-open"]'), accountId);
      return {
        local: true,
        message: q.renew
          ? '原微信登录凭据已替换。原通道与任务执行已暂停，请用新配对码重新验证同一联系人，再在面板确认恢复原批准范围；旧任务不会重放。'
          : '微信登录凭据已保存。请继续选择当前话题的 dot 并完成联系人配对。',
      };
    }
    async function qrAct(action, element) {
      const validate =
        ['dot-channel-qr-start', 'dot-channel-qr-verify', 'dot-channel-qr-accept'].includes(
          action,
        ) && !qr?.acceptArgs;
      const data = group(element, validate);
      if (!data) return local;
      if (action === 'dot-channel-qr-start') {
        if (
          !loaded.weixin.enabled ||
          qr ||
          !data.checked('qr_storage') ||
          !data.checked('qr_terms')
        )
          return local;
        const renewalId = data.value('renew_account_id');
        const renewal = renewalId
          ? loaded.weixin.accounts.find((account) => account.id === renewalId)
          : null;
        if (renewalId && (!renewal || !Number.isInteger(renewal.credential_version)))
          throw new Error('原连接版本不可用，请重新读取当前连接后再扫码。');
        qr = {
          renew: renewal
            ? {
                id: renewal.id,
                bot_id: renewal.bot_id,
                credential_version: renewal.credential_version,
              }
            : null,
          key: loaded.key,
          owner: loaded,
          startKey: qrId(),
          cancelKey: qrId(),
          acceptKey: qrId(),
          busy: false,
          entry: null,
          notice: '',
        };
        qrWatchScope(qr);
        return qrStart(qr);
      }
      const q = qr;
      if (!q || !qrLive(q) || q.busy) return local;
      if (action === 'dot-channel-qr-reset' && q.startRejected) {
        qrTimersOff();
        qr = null;
        document.querySelector('.cc-wx-qr').innerHTML = qrMarkup(null);
        return local;
      }
      if (action === 'dot-channel-qr-finish' && q.entry?.state === 'accepted')
        return qrComplete(q, q.entry.account_id);
      if (action === 'dot-channel-qr-retry-start') return qrStart(q);
      if (action === 'dot-channel-qr-status') return qrStatus(q);
      if (action === 'dot-channel-qr-verify') {
        if (q.verifyUncertain || q.entry.state !== 'need_verifycode') return local;
        const code = data.value('verify_code').trim();
        data.root.querySelector('[name="verify_code"]').value = '';
        return qrStatus(q, code);
      }
      if (action === 'dot-channel-qr-cancel') return qrCancel(q);
      if (action === 'dot-channel-qr-refresh') return qrCancel(q, true);
      if (action === 'dot-channel-qr-accept') return qrAccept(q, data);
      if (action === 'dot-channel-qr-new' && qrTerminal(q.entry)) {
        qrTimersOff();
        qr = null;
        document.querySelector('.cc-wx-qr').innerHTML = qrMarkup(null);
      }
      return local;
    }

    const enrollmentPreviews = new WeakMap();
    const enrollmentRequests = new Set();
    const enrollmentSelection = (form) =>
      JSON.stringify(
        ['account_id', 'dot_id', 'expected_peer_id'].map(
          (name) => form.querySelector('[name="' + name + '"]')?.value.trim() || '',
        ),
      );
    function enrollmentForm(channel, data) {
      const accounts = (data.accounts || []).filter(
        (a) =>
          a.state === 'active' &&
          !(data.bindings || []).some(
            (b) => b.state !== 'revoked' && (channel === 'weixin' || b.account_id === a.id),
          ),
      );
      if (!accounts.length) return '';
      const slots = mentionSlots().filter((slot) => slot.duplex && slot.state === 'registered');
      return `<section class="cc-enrollment"><h3>2. 核对授权，私聊验证后自动开通</h3>
        <p>在这里一次核对接入与本人任务授权；发回授权码后，无需再回面板确认联系人或重复开通任务。</p>
        <div class="cc-form" data-channel-form="enrollment" data-enrollment-channel="${E(channel)}">
          ${field(
            '授权的机器人账号',
            select(
              'account_id',
              accounts.map((a) => [a.id, a.label + ' · ' + a.bot_id]),
            ),
          )}
          ${field(
            '接收任务的 dot',
            select(
              'dot_id',
              slots.map((slot) => [slot.id, slot.label]),
            ),
          )}
          <details class="cc-enrollment-peer"><summary>已有本人联系人编号（可选，填写后仅该联系人可验证）</summary>
            ${field(channel === 'telegram' ? '本人的数字 user_id / 私聊 chat_id' : '本人的私聊 peer_id', '<input name="expected_peer_id" maxlength="256" autocomplete="off">')}
          </details>
          <div class="cc-enrollment-summary" role="status" aria-live="polite">选择账号与 dot 后显示确切授权范围。</div>
          <div class="cc-consent-purposes">
            <p data-consent-purpose="owner_identity"><strong>本人身份：</strong>仅本人在所选机器人私聊发送此一次性授权码，该私聊身份将绑定为本人。不要转发授权码。</p>
            <p data-consent-purpose="future_tasks"><strong>未来任务：</strong>允许该本人私聊交办摘要所列项目、能力和执行目标内的新任务，不执行旧消息，不扩大 MCP 连接权限。</p>
            <p data-consent-purpose="result_delivery"><strong>消息与回传：</strong>接收此私聊的新消息到当前话题，任务结果回到原私聊和当前话题。${channel === 'telegram' ? '同时把本话题已授权、当前可见的新消息完整同步到此 Telegram 私聊；不补发历史。' : '仅允许原微信消息的结果回复，不自动开启跨通道完整同步。'}文件外发仍需逐份批准。</p>
            <p data-consent-purpose="until_revoked"><strong>持续期限：</strong>验证完成后，绑定和上述任务范围持续至面板撤销；任务数量、超时、并发以及宿主必要的权限和付款确认仍然有效。</p>
            <p data-consent-purpose="pairing_notices"><strong>接入提醒：</strong>验证前只提示发送授权码，验证后向原私聊发送回执。同一条入站消息只提醒一次；没有可用私聊地址时等待第一条入站消息，不宣称已发送。</p>
          </div>
          ${checkbox('enrollment_agreement', '我已核对并同意以上各项授权', true)}
          <div class="cc-actions">${button('dot-channel-enrollment-approve', '同意并生成本人授权码', {}, 'disabled')}${button('dot-channel-enrollment-preview', '重新核对摘要')}</div>
          <small>各项同意分别留存证据。此处不重复索取凭据保存或服务条款同意；没有对应证据时不会推定已经同意。</small>
        </div></section>`;
    }
    function enrollmentReset(form) {
      enrollmentPreviews.get(form)?.controller?.abort();
      enrollmentPreviews.delete(form);
      const agree = form.querySelector('[name="enrollment_agreement"]');
      agree.checked = false;
      agree.disabled = true;
      form.querySelector('[data-cc-action="dot-channel-enrollment-approve"]').disabled = true;
    }
    async function enrollmentRead(form) {
      const previous = enrollmentPreviews.get(form);
      enrollmentReset(form);
      const owner = loaded;
      const selection = enrollmentSelection(form);
      const [account_id, dot_id, peer] = JSON.parse(selection);
      const channel = form.dataset.enrollmentChannel;
      const output = form.querySelector('.cc-enrollment-summary');
      if (!account_id || !dot_id) {
        output.textContent = '请先选择当前账号和已连接的 dot。';
        return local;
      }
      const request = {
        owner,
        epoch: state.drawerEpoch,
        selection,
        controller: new AbortController(),
      };
      enrollmentPreviews.set(form, request);
      enrollmentRequests.add(request.controller);
      const live = () =>
        form.isConnected &&
        loaded === owner &&
        current(owner.key) &&
        request.epoch === state.drawerEpoch &&
        enrollmentPreviews.get(form) === request &&
        enrollmentSelection(form) === selection;
      output.textContent = '正在读取确切的项目、能力和结果回传范围…';
      try {
        const preview = await api('/api/collaboration/channel-enrollment-preview', {
          method: 'POST',
          body: JSON.stringify({ ...qrScope(owner.key), channel, account_id, dot_id }),
          signal: request.controller.signal,
          retrySafe: true,
        });
        if (!live()) return local;
        if (
          preview.channel !== channel ||
          preview.account_id !== account_id ||
          preview.dot_id !== dot_id ||
          preview.conversation_id !== owner.key.conversation_id ||
          preview.environment_id !== owner.key.environment_id ||
          !/^[a-f0-9]{64}$/.test(preview.scope_digest || '') ||
          preview.text_version !== 'channel-consent-v1' ||
          !preview.project_id ||
          !Array.isArray(preview.capabilities) ||
          !preview.capabilities.length ||
          !Array.isArray(preview.execution_targets) ||
          !preview.execution_targets.length ||
          !Number.isInteger(preview.max_delegations) ||
          preview.max_delegations < 1 ||
          !preview.revoke_path
        )
          throw new Error('服务端授权摘要不完整，不能生成任务授权码。');
        request.preview = preview;
        const changed =
          previous?.preview &&
          previous.selection === selection &&
          previous.preview.scope_digest !== preview.scope_digest;
        const rows = [
          ...(changed ? [['范围变化', '原摘要已失效，请只核对下方当前差异并重新确认。']] : []),
          ['账号', preview.account_label + ' · ' + preview.bot_id],
          ['授权联系人', peer || '仅本人持此码验证的原机器人私聊身份'],
          ['项目范围', preview.project_id + ' / ' + preview.environment_id],
          ['接收任务的 dot', dot_id],
          ['允许能力', preview.capabilities.join(' / ')],
          ['执行目标', preview.execution_targets.join(' / ')],
          ['任务数量', '沿用原策略：累计最多 ' + preview.max_delegations + ' 次'],
          ['结果回传位置', preview.result_destination],
          ['期限与撤销', '持续至撤销 · ' + preview.revoke_path],
        ];
        output.innerHTML =
          '<h4>本次授权摘要</h4><dl>' +
          rows
            .map(([name, value]) => '<dt>' + E(name) + '</dt><dd>' + E(value) + '</dd>')
            .join('') +
          '</dl>';
        form.querySelector('[name="enrollment_agreement"]').disabled = false;
        form.querySelector('[data-cc-action="dot-channel-enrollment-approve"]').disabled = false;
      } catch (error) {
        if (live() && error.name !== 'AbortError')
          output.textContent =
            '无法确认授权范围，尚未生成授权码。请检查 dot 的已批准范围后重新核对。' +
            (error.message || '');
      } finally {
        enrollmentRequests.delete(request.controller);
      }
      return local;
    }
    function onboardingStatus(binding) {
      const entry = binding.onboarding;
      const capabilities = window.CodePierCollaborationChannels.capabilities(
        binding.effective_capabilities,
        E,
      );
      if (!entry) return capabilities;
      const status =
        {
          waiting_inbound: '尚无可用私聊地址，等待第一条入站消息；未发送接入提醒。',
          waiting_context: '等待原私聊的可用回复上下文；回执尚未发送。',
          queued: '接入回执已排队，尚未确认平台接受。',
          sending: '接入回执正在提交，结果尚未确认。',
          accepted: '平台已接受原私聊接入回执，不代表用户已读。',
          unknown: '接入回执结果未知，为避免重复不会自动重发。',
          rejected: '平台拒绝接入回执；请核对原通道状态。',
          cancelled: '原接入回执已取消或授权状态已变化。',
        }[entry.notice_state] || '尚无可核实的私聊回执。';
      const instruction = entry.auto_complete
        ? '授权摘要已确认：仅本人向原机器人私聊发送授权码，验证后自动完成绑定与任务开通，无需回面板重复确认。'
        : entry.state === 'completed'
          ? '本次授权码已完成验证。任务权限以当前授权状态为准。'
          : '原接入预授权已失效或范围已变化，不会自动开通任务；请重新核对。';
      const readiness = entry.readiness;
      const names = {
        account: '账号或凭据世代',
        project_id: '项目',
        environment_id: '环境',
        project_snapshot: '项目映射',
        dot_id: '目标 dot',
        dot_setup_digest: 'dot 范围',
        policy_id: '任务策略',
        policy_version: '策略版本',
        policy_digest: '策略内容',
        capabilities: '允许能力',
        execution_targets: '执行目标',
        target_snapshots: '目标映射',
        max_delegations: '任务预算',
        delivery_mode: '回传方式',
        binding: '绑定版本',
      };
      const recovery = readiness
        ? '<p>已恢复原接入对象，查询不会创建新绑定。</p>' +
          (readiness.ready_for_private_proof
            ? '<p>原范围仍一致，等待本人完成私聊验证。</p>'
            : readiness.changed_fields?.length
              ? '<p>需要重新核对变化：' +
                E(readiness.changed_fields.map((key) => names[key] || '原范围').join('、')) +
                '</p>'
              : readiness.scope_unchanged === null
                ? '<p>当前范围尚未核实，请查看原账号、绑定和授权状态。</p>'
                : '')
        : '';
      return (
        capabilities +
        `<div class="cc-onboarding-status" role="status"><strong>${E(instruction)}</strong><p>${E(status)}</p>${recovery}</div>`
      );
    }
    function channelOwnerPanel(channel, data, binding) {
      if (channel === 'weixin') return ownerTaskPanel(data, binding);
      return ownerTaskPanel(data, {
        ...binding,
        peer_id: binding.chat_id,
        allow_replies: binding.mirror_enabled,
      })
        .replaceAll('dot-channel-owner-', 'dot-channel-tg-owner-')
        .replaceAll('微信', 'Telegram');
    }

    function channelSetting(channel, data) {
      if (!data.setting) return '';
      const name = channel === 'weixin' ? '微信' : 'Telegram';
      return (
        '<div class="cc-channel-switch" data-channel-form="setting-' +
        channel +
        '">' +
        '<div class="cc-channel-heading"><strong>' +
        name +
        '通道开关</strong>' +
        '<span class="cc-channel-pill">' +
        (data.enabled ? '已开启' : '已关闭') +
        '</span></div>' +
        '<p>作用于你在此空间下的全部 ' +
        name +
        ' 连接。关闭后停止新收发；微信还会终止未完成扫码。已有凭据与绑定保留。尚未发送的回复会停止且不再补发；已提交请求无法召回，结果未知时不自动重发。重开可能按原进度补收入站消息，不代替配对或新增授权。</p>' +
        button(
          'dot-channel-setting-' + channel,
          data.enabled ? '关闭 ' + name + ' 通道' : '开启 ' + name + ' 通道',
        ) +
        '</div>'
      );
    }

    function telegramBindings(t) {
      return (t.bindings || [])
        .map(
          (
            b,
          ) => `<section class="cc-channel-binding cc-tg-binding" data-binding-id="${E(b.id)}"><div class="cc-channel-heading"><strong>Telegram · ${listState(b.state)}</strong><span>完整同步</span></div><small>原绑定 ${E(b.id.slice(-8))} · 世代 ${E(b.generation)} · ${b.lifetime_mode === 'until_revoked' ? '持续至撤销' : '到期 ' + stamp(b.expires_at)}</small>
        ${b.user_id ? `<p>本人 user_id：<code>${E(b.user_id)}</code> · 私聊 chat_id：<code>${E(b.chat_id)}</code></p>` : ''}
        ${b.state === 'pairing' ? `<p>${b.onboarding?.auto_complete ? '仅本人在当前机器人私聊发送以下一次性授权码，即可自动连接并开通已批准任务范围：' : '在当前机器人私聊发送以下一次性命令，再回来核对身份：'}</p><textarea readonly rows="2" aria-label="Telegram 配对命令">${E(b.pairing_command || '已过期，请撤销后重新配对')}</textarea>` : ''}
        ${b.state === 'awaiting_owner' ? `<p>实际 user_id：<code>${E(b.pending_user_id)}</code> · chat_id：<code>${E(b.pending_chat_id)}</code></p><div data-channel-form="tg-activate">${checkbox('same_owner', '我已核对数字编号，确认这是我本人的 Telegram 私聊', true)}${button('dot-channel-tg-activate', '确认本人并启用同步', b)}</div>` : ''}
        ${onboardingStatus(b)}${channelOwnerPanel('telegram', t, b)}<small>${b.disable_notification ? 'Telegram 无声发送；仍可能显示通知。' : 'Telegram 按客户端通知设置提醒。'}微信请在客户端设置免打扰。</small><div class="cc-actions">${b.state === 'active' ? button('dot-channel-tg-pause', '暂停 Telegram', b) : b.state === 'paused' ? button('dot-channel-tg-resume', '恢复原绑定', b) : ''}${b.state !== 'revoked' ? button('dot-channel-tg-revoke', '撤销 Telegram', b) : ''}</div></section>`,
        )
        .join('');
    }
    function telegramPanel(t, w, slots) {
      if (!t) return '';
      const names = {
        retention_gap_possible: '离线可能超过24小时，上游历史可能不完整',
        TELEGRAM_WEBHOOK_CONFLICT: '机器人已有Webhook，请人工核对，不会自动替换',
      };
      const bindings = telegramBindings(t);
      const receipts = (t.deliveries || [])
        .map(
          (d) =>
            `<details class="cc-channel-delivery" ${d.state === 'needs_attention' ? 'open' : ''}><summary>${d.channel === 'telegram' ? 'Telegram' : '微信'} · ${listState(d.state)}</summary><small>原消息 ${E(d.message_id.slice(-8))} · 各目标独立记录；通道接受不等于已读。</small>${(d.parts || []).map((p) => `<p>${E(p.ordinal + 1)}. ${listState(p.state)} ${E(p.reason_code || '')}</p>`).join('')}<div data-channel-form="mirror-delivery">${(d.parts || []).some((p) => p.state === 'unknown') ? field('人工核对依据', '<input name="confirmation_note" maxlength="1000" required>') + button('dot-channel-mirror-confirm', '登记人工已收到', d) : ''}${button('dot-channel-mirror-cancel', '取消未发送部分', d)}</div></details>`,
        )
        .join('');
      return `<section class="cc-tg-settings"><div class="cc-channel-heading"><h3>Telegram · 同一协作室</h3><span class="cc-channel-pill">${t.enabled ? '已开启' : '已关闭'}</span></div><p>微信、Telegram 和面板共用一份消息与上下文。镜像不会新建任务。每个私聊明确绑定一个协作室，多个协作室请分别使用机器人；不支持群组和论坛。</p>
        ${channelSetting('telegram', t)}${!t.enabled ? '<p>开启 Telegram 通道后，在下方登记机器人并配对本人私聊。</p>' : ''}
        ${(t.accounts || [])
          .filter((a) => a.reason_code)
          .map(
            (a) =>
              `<p role="status">${E(a.label)}：${E(names[a.reason_code] || a.reason_code)}</p>`,
          )
          .join('')}
        <div class="cc-tg-bindings">${bindings || '<p>当前协作室尚未绑定 Telegram。</p>'}</div><p class="cc-tg-refresh-notice" role="status" aria-live="polite"></p>
        <details class="cc-tg-connect"><summary>连接 Telegram 机器人</summary><div data-channel-form="tg-account">${field('名称', '<input name="label" maxlength="80" required autocomplete="off">')}${field('机器人数字编号', '<input name="bot_id" inputmode="numeric" pattern="[1-9][0-9]*" required autocomplete="off">')}${field('Bot Token', '<input type="password" name="token" maxlength="256" required autocomplete="new-password">')}${checkbox('credentials', '我同意在本服务加密保存此 Telegram 凭据', true) + checkbox('provider_terms', '我已核对并接受 Telegram 的相关服务条款', true)}${button('dot-channel-tg-import', '确认登记 Telegram 凭据', {}, t.enabled ? '' : 'disabled')}<small>仅使用官方API长轮询；已有Webhook会阻止连接，系统不会自动删除或覆盖。</small></div></details>
        <details><summary>将本人私聊绑定此协作室</summary><div data-channel-form="tg-bind">${field(
          '已登记机器人',
          select(
            'account_id',
            (t.accounts || [])
              .filter((a) => a.state === 'active')
              .map((a) => [a.id, a.label + ' · ' + a.bot_id]),
          ),
        )}${field(
          '当前话题 dot',
          select(
            'dot_id',
            slots.map((s) => [s.id, s.label]),
          ),
        )}${checkbox('silent', 'Telegram 无声发送（仍可能弹出通知）')}${checkbox('ingress', '我确认将此私聊消息接入当前协作室', true)}${checkbox('mirror', '我确认将此协作室当前可见且已授权范围内的新消息完整同步到本人私聊', true)}${button('dot-channel-tg-bind', '生成 Telegram 配对命令', {}, t.enabled ? '' : 'disabled')}<small>不补发旧历史，不把外部文字自动当作执行授权。文件先显示说明，可在下方选择文件、预览收件人后单独确认外发。</small></div></details>
        ${(w.bindings || [])
          .filter((b) => b.state === 'active')
          .map(
            (b) =>
              `<div data-channel-form="wx-mirror" class="cc-channel-binding">${checkbox('mirror', '我确认此微信联系人是本人，并将当前协作室的新消息完整同步到此微信私聊', true)}${button('dot-channel-wx-mirror', '确认微信完整同步', b)}<small>只对当前绑定版本有效；微信需在客户端设置免打扰。</small></div>`,
          )
          .join('')}
        <details><summary>向 Telegram 发送已有图片或文件</summary><div data-channel-form="tg-files">
${field(
  '明确的原收件人',
  select(
    'tg_binding',
    (t.bindings || [])
      .filter((b) => b.state === 'active')
      .map((b) => [b.id, 'user_id ' + b.user_id + ' · chat_id ' + b.chat_id]),
  ),
)}
${field(
  '附加到原 dot 最终回复',
  select(
    'tg_message',
    (t.reply_candidates || []).map((m) => [m.id, m.body_text || m.id]),
  ),
)}
<fieldset><legend>选择完整文件清单（最多16件，每件不超过50MB）</legend>${(t.artifact_candidates || []).map((a) => `<label class="cc-channel-check"><input type="checkbox" name="tg_artifact_ids" value="${E(a.artifact_id)}" ${a.bytes > (t.upload_limit || 52428800) ? 'disabled' : ''}>${E(a.name)} · ${E(a.bytes)} 字节${a.bytes > (t.upload_limit || 52428800) ? ' · 超限，请在面板读取' : ''}</label>`).join('') || '<p>当前项目暂无可选产物。</p>'}</fieldset>
${button('dot-channel-tg-file-preview', '预览文件与收件人', {}, t.enabled ? '' : 'disabled')}<div class="tg-file-preview"></div><small>预览不会上传。最终确认仅授权此清单和此原收件人；正文不会重发，文件分项单独记录未知结果。</small></div></details>
<h4>逐目标投递状态</h4>${receipts || '<p>暂无完整同步投递记录。</p>'}</section>`;
    }

    function ownerTaskOption(w, id) {
      const options = w.owner_task_options || [];
      return Array.isArray(options)
        ? options.find((option) => option.binding_id === id)
        : options[id];
    }

    function ownerBudgetLabel(mode, maximum) {
      return mode === 'unlimited'
        ? '不设任务数量上限'
        : mode === 'rolling_24h'
          ? '每滚动 24 小时最多 ' + maximum + ' 次'
          : '累计最多 ' + maximum + ' 次';
    }
    function ownerBudgetForm(option, binding) {
      const authority = binding.owner_authorization;
      const editing = authority?.state === 'active';
      const mode = editing ? authority.budget_mode || 'lifetime' : 'lifetime';
      const maximum =
        editing && authority.max_delegations != null
          ? authority.max_delegations
          : option.max_delegations;
      const modes = option.available_budget_modes || ['lifetime', 'rolling_24h'];
      return (
        '<div data-channel-form="owner-authority" class="cc-form">' +
        field(
          '任务数量限制',
          '<select name="budget_mode">' +
            modes
              .map(
                (value) =>
                  '<option value="' +
                  E(value) +
                  '" ' +
                  (mode === value ? 'selected' : '') +
                  '>' +
                  {
                    lifetime: '累计数量上限',
                    rolling_24h: '每滚动 24 小时的数量上限',
                    unlimited: '不设数量上限',
                  }[value] +
                  '</option>',
              )
              .join('') +
            '</select>',
        ) +
        field(
          '最多多少次任务委派',
          '<input name="max_delegations" type="number" value="' +
            E(maximum) +
            '" min="1" step="1" required>',
        ) +
        '<p class="cc-budget-summary" aria-live="polite">' +
        E(ownerBudgetLabel(mode, maximum)) +
        '。任务超时、并发与必要的付款、权限确认仍然有效。</p>' +
        (editing
          ? '<p>仅修改任务数量，不重新扩大原联系人、项目、能力、回传位置和持续期限。</p>'
          : '<div class="cc-consent-purposes"><p><strong>本人身份：</strong>以上已验证联系人是我本人。</p>' +
            '<p><strong>未来任务与回传：</strong>允许本人今后向以上 dot 交办所列项目、能力和执行目标内的新任务，结果回到原私聊；文件仍需逐份确认。</p>' +
            '<p><strong>持续与撤销：</strong>此绑定及原 dot 范围持续至面板撤销；每项任务限制和宿主必要确认仍保留。可在此处撤销任务授权，或撤销整个绑定。</p></div>' +
            checkbox(
              'confirm_owner_agreement',
              '我已核对并同意以上本人身份、未来任务与回传、持续期限及撤销安排',
              true,
            )) +
        checkbox('confirm_daily_budget', '我另外批准所选滚动 24 小时额度，替代原累计总额度') +
        checkbox(
          'confirm_unlimited_budget',
          '我明确批准不设任务数量上限；项目、能力、目标、任务超时、并发以及付款和权限确认仍然保留',
        ) +
        checkbox('confirm_task_count_budget', '我明确批准上方填写的任务数量上限') +
        (editing
          ? checkbox(
              'confirm_budget_change',
              '我确认修改此原授权的任务数量策略；这会停止此授权下尚未完成的任务，只有之后的新消息使用新额度',
              true,
            )
          : '') +
        '<small>外部消息保留微信来源。shell 以执行账户权限运行，并非操作系统沙箱。平台登录失效时仍需本人重新连接。</small>' +
        button(
          'dot-channel-owner-approve',
          editing ? '确认修改任务数量' : '确认授权微信交办任务',
          binding,
        ) +
        '</div>'
      );
    }
    function ownerTaskPanel(w, binding) {
      const authority = binding.owner_authorization;
      const option = ownerTaskOption(w, binding.id);
      const active = authority?.state === 'active';
      const pending = authority?.state === 'pending_revalidation';
      const displayed = active || pending ? authority : option;
      const exactScope =
        displayed?.project_id &&
        displayed?.policy_id &&
        Array.isArray(displayed.capabilities) &&
        displayed.capabilities.length > 0 &&
        Array.isArray(displayed.execution_targets) &&
        displayed.execution_targets.length > 0 &&
        ((displayed.budget_mode === 'unlimited' && displayed.max_delegations === null) ||
          (Number.isInteger(displayed.max_delegations) && displayed.max_delegations > 0));
      const canApprove =
        w.enabled &&
        binding.state === 'active' &&
        !!binding.peer_id &&
        binding.allow_replies !== false &&
        option?.available === true &&
        !!option.scope_digest &&
        exactScope;
      const dot = mentionSlots().find((slot) => slot.id === binding.dot_id);
      const scopeDetails = exactScope
        ? '<dl class="cc-authority-scope"><div><dt>联系人</dt><dd>' +
          E(binding.peer_id || binding.pending_peer_id) +
          '</dd></div><div><dt>交给 dot</dt><dd>' +
          E(dot?.label || binding.dot_id) +
          '<small>' +
          E(binding.dot_id) +
          '</small></dd></div><div><dt>项目</dt><dd>' +
          E(displayed.project_id) +
          '</dd></div><div><dt>允许操作</dt><dd>' +
          E(displayed.capabilities.join(' / ')) +
          '</dd></div><div><dt>执行目标</dt><dd>' +
          E(displayed.execution_targets.join(' / ')) +
          '</dd></div><div><dt>原策略</dt><dd>' +
          E(displayed.policy_id) +
          (displayed.policy_version ? ' · 版本 ' + E(displayed.policy_version) : '') +
          '</dd></div></dl>'
        : '';
      if (pending)
        return (
          '<div class="cc-wx-owner-authority"><h5>任务已暂停，等待原联系人验证</h5><p>原批准范围保留。请在同一本人微信发送新配对码，回到面板核对后恢复；旧消息与旧任务不会重放。</p>' +
          scopeDetails +
          '<p>保留的额度：' +
          E(ownerBudgetLabel(authority.budget_mode, authority.max_delegations)) +
          '</p>' +
          button('dot-channel-owner-revoke', '撤销保留的任务授权', binding) +
          '</div>'
        );
      if (active)
        return (
          '<div class="cc-wx-owner-authority is-active"><div class="cc-channel-heading"><h5>微信任务已开启</h5><span class="cc-channel-pill">持续至撤销</span></div><p>' +
          E(ownerBudgetLabel(authority.budget_mode, authority.max_delegations)) +
          ' · 进度与结果回到原微信</p><details><summary>查看原批准范围</summary>' +
          scopeDetails +
          '<p>暂停通道期间不执行新任务。每项任务仍受原限制和宿主必要确认约束，文件仍需逐份确认。</p></details>' +
          (canApprove
            ? '<details class="cc-connection-secondary"><summary>修改任务数量限制</summary><p>仅修改同一联系人、dot 和原任务范围内的数量策略。</p>' +
              scopeDetails +
              ownerBudgetForm(option, binding) +
              '</details>'
            : '') +
          '<details class="cc-connection-secondary"><summary>停用微信任务</summary><p>撤销任务授权后，原联系人仍可继续聊天。</p>' +
          button('dot-channel-owner-revoke', '撤销任务授权，保留聊天', binding) +
          '</details></div>'
        );
      if (!option && !authority) return '';
      const reason =
        {
          WEIXIN_BINDING_INACTIVE: '先接通并核对本人私聊。',
          WEIXIN_OWNER_POLICY_UNAVAILABLE: '此 dot 尚无可用的原任务策略。',
          WEIXIN_PAIRING_UNVERIFIED: '先接通并核对本人私聊。',
          CHANNEL_OWNER_SCOPE_CHANGED: '原任务范围已变化，请刷新后核对。',
          CHANNEL_OWNER_REPLIES_REQUIRED:
            '当前绑定未批准回复。需建立允许回复的绑定后才能开启任务。',
          WEIXIN_OWNER_SCOPE_UNAVAILABLE: '当前原授权范围不可用，请核对 dot 与项目。',
          WEIXIN_CHANNEL_DISABLED: '请先开启微信通道。',
        }[option?.reason_code] || '当前连接尚不满足任务授权条件，请核对联系人、dot 与原任务策略。';
      return (
        '<details class="cc-wx-owner-authority"><summary>3. 微信交办任务 · 可选' +
        (authority?.state === 'revoked' ? '（已撤销）' : '') +
        '</summary><p>一次批准后，本人联系人可以在以下原范围内持续交办任务，直到撤销。普通聊天不会自动成为任务。</p>' +
        scopeDetails +
        (canApprove
          ? ownerBudgetForm(option, binding)
          : '<p role="status">' +
            E(reason) +
            (option?.reason_code ? '（' + E(option.reason_code) + '）' : '') +
            '</p>') +
        '</details>'
      );
    }
    function weixinBindings(w) {
      return (w.bindings || [])
        .map(
          (binding) =>
            `<section class="cc-channel-binding" data-binding-id="${E(binding.id)}"><h4>${binding.state === 'pairing' && binding.pairing_until <= Date.now() / 1000 ? '配对码已过期' : listState(binding.state)}</h4><p>原绑定 ${E(binding.id.slice(-8))} · 世代 ${E(binding.generation)} · ${binding.lifetime_mode === 'until_revoked' ? '有效期：持续至面板撤销' : '有效至 ' + stamp(binding.expires_at)}</p>${binding.peer_id ? `<p>原联系人：<code>${E(binding.peer_id)}</code></p>` : ''}${binding.state === 'pairing' ? `<p>${binding.onboarding?.auto_complete ? '仅本人在原机器人私聊发送以下一次性授权码，验证后自动完成连接和任务开通。' : '在要绑定的微信私聊发送以下配对码。此处会自动查询配对状态，仍需本人核对后激活。'}</p><textarea readonly rows="2" aria-label="微信配对码">${E(binding.pairing_until <= Date.now() / 1000 ? '配对码已过期；请点击“重新生成配对码”' : binding.pairing_challenge || '原配对码不可用；请重新核对状态')}</textarea><small>配对期限：${stamp(binding.pairing_until)}。配对不会授予代码执行权限。</small>` : ''}${binding.state === 'awaiting_owner' ? `<p>实际返回的联系人编号：<code>${E(binding.pending_peer_id)}</code></p><div data-channel-form="activate">${checkbox('confirm_peer', binding.owner_authorization?.state === 'pending_revalidation' ? '我已核对是原本人联系人，确认恢复原批准范围；不重放旧任务或旧消息' : '我已核对该联系人和当前房间', true)}${button('dot-channel-activate', '确认激活此联系人', binding)}</div>` : ''}${onboardingStatus(binding)}${ownerTaskPanel(w, binding)}<div class="cc-actions">${binding.state === 'active' ? button('dot-channel-pause', '暂停此通道', binding) : binding.state === 'paused' && !binding.revalidation_required ? button('dot-channel-resume', '恢复原绑定', binding) : ''}${['pairing', 'awaiting_owner'].includes(binding.state) || (binding.state === 'paused' && binding.revalidation_required) ? button('dot-channel-refresh-pairing', '重新生成配对码', binding) : ''}${binding.state !== 'revoked' ? button('dot-channel-revoke', '撤销原绑定', binding) : ''}</div></section>`,
        )
        .join('');
    }
    function weixinHealth(w) {
      const names = {
        WEIXIN_UPDATES_REJECTED: '微信收件接口尚未返回成功应答，配对消息可能还未被接收',
        WEIXIN_AUTH_REJECTED: '微信登录凭据被拒绝，需要重新核对连接',
        WEIXIN_SESSION_EXPIRED: '微信登录已失效，需要重新扫码确认',
        WEIXIN_RATE_LIMITED: '微信接口限流，正在等待原连接重试',
        WEIXIN_POLL_IDLE: '本轮长轮询暂未取得新批次，将继续原连接等待',
        WEIXIN_POLL_FENCE_LOST: '原收件轮次已交接，将沿用当前连接继续查询',
        WEIXIN_POLL_CANCELLED: '本轮收件暂时中断，将沿用原进度继续查询',
        WEIXIN_BINDING_CHANGED: '绑定状态已更新，将按当前绑定继续查询',
        WEIXIN_PAIRING_EXPIRED: '配对码已过期，已停止此次配对收件；请点击“重新生成配对码”',
        WEIXIN_BINDING_INACTIVE: '原绑定已暂停、撤销或到期',
        WEIXIN_CREDENTIAL_CHANGED: '原凭据或绑定世代已变化，需要重新核对',
        WEIXIN_MAPPING_CHANGED: '原项目范围已变化，需要重新核对',
        WEIXIN_CURSOR_MISSING: '微信应答缺少收件进度，尚未确认同步成功',
        TimeoutError: '本轮收件超时，原收件进度保留',
      };
      return (w.accounts || [])
        .map((account) => {
          const synced = Number.isFinite(account.last_sync_at);
          const retry =
            account.state === 'active' &&
            w.enabled &&
            account.reason_code !== 'WEIXIN_PAIRING_EXPIRED' &&
            Number.isFinite(account.next_poll) &&
            account.next_poll > Date.now() / 1000;
          return (
            '<div class="cc-wx-account-health"><strong>' +
            E(account.label || '微信连接') +
            '：' +
            (account.state === 'active' ? '登录凭据已保存' : listState(account.state)) +
            '</strong><p>' +
            (synced
              ? '最近成功收件查询：' +
                stamp(account.last_sync_at) +
                '。这不代表已配对或已收到新消息。'
              : '尚未成功完成收件查询；还不能确认配对消息已被接收。') +
            '</p>' +
            (account.reason_code
              ? '<p role="status">' +
                E(names[account.reason_code] || '原连接需要核对，请保留当前状态查看原因') +
                '（' +
                E(account.reason_code) +
                '）</p>'
              : '') +
            (retry
              ? '<p>下次收件尝试不早于 ' + stamp(account.next_poll) + '；无需重复发送配对码。</p>'
              : account.state === 'attention'
                ? '<p>此连接已停止自动收件，请先处理以上原因。</p>'
                : '') +
            (!w.enabled ? '<p>微信通道已关闭，当前不会收件。</p>' : '') +
            '</div>'
          );
        })
        .join('');
    }

    let pairingRefresh = null;
    const pairingLive = (p) =>
      pairingRefresh === p &&
      !p.stopped &&
      loaded === p.owner &&
      current(p.owner.key) &&
      p.owner.epoch === state.drawerEpoch &&
      !!document.querySelector('.cc-wx-bindings');
    function pairingStop() {
      const p = pairingRefresh;
      pairingRefresh = null;
      if (!p) return;
      p.stopped = true;
      clearTimeout(p.timer);
      clearInterval(p.watch);
      p.controller?.abort();
    }
    function pairingExpire(p) {
      for (const binding of p.owner.weixin.bindings || []) {
        if (binding.state !== 'pairing' || binding.pairing_until > Date.now() / 1000) continue;
        const card = [...document.querySelectorAll('.cc-wx-bindings [data-binding-id]')].find(
          (node) => node.dataset.bindingId === binding.id,
        );
        if (!card) continue;
        card.querySelector('h4').textContent = '配对码已过期';
        const code = card.querySelector('textarea[readonly]');
        if (code) code.value = '配对码已过期；请点击“重新生成配对码”';
      }
    }
    function pairingStart() {
      pairingStop();
      if (
        !loaded?.weixin.enabled ||
        !(loaded.weixin.bindings || []).some(
          (b) =>
            b.state === 'awaiting_owner' ||
            (b.state === 'pairing' && b.pairing_until > Date.now() / 1000),
        )
      )
        return;
      const p = { owner: loaded, timer: null, watch: null, controller: null, failures: 0 };
      pairingRefresh = p;
      p.watch = setInterval(() => {
        if (!pairingLive(p)) pairingStop();
        else pairingExpire(p);
      }, 500);
      p.timer = setTimeout(() => pairingPoll(p), 5000);
    }
    async function pairingPoll(p) {
      if (!pairingLive(p)) {
        pairingStop();
        return;
      }
      pairingExpire(p);
      if (saving || document.hidden) {
        p.timer = setTimeout(() => pairingPoll(p), 5000);
        return;
      }
      p.controller = new AbortController();
      try {
        const fresh = await api(
          '/api/collaboration/weixin?' + new URLSearchParams(qrScope(p.owner.key)),
          { signal: p.controller.signal },
        );
        if (!pairingLive(p) || saving) return;
        p.failures = 0;
        const health = document.querySelector('.cc-wx-health');
        const markup = weixinHealth(fresh);
        if (health.innerHTML !== markup) health.innerHTML = markup;
        const region = document.querySelector('.cc-wx-bindings');
        const previous = p.owner.weixin.bindings || [];
        const displayed = [];
        const displayedOptions = [];
        for (const binding of fresh.bindings || []) {
          const card = [...region.querySelectorAll('[data-binding-id]')].find(
            (node) => node.dataset.bindingId === binding.id,
          );
          const before = previous.find((b) => b.id === binding.id);
          const beforeOption = ownerTaskOption(p.owner.weixin, binding.id);
          const freshOption = ownerTaskOption(fresh, binding.id);
          const changed =
            !before ||
            JSON.stringify(before) !== JSON.stringify(binding) ||
            JSON.stringify(beforeOption) !== JSON.stringify(freshOption);
          if (card && binding.state === 'pairing' && binding.pairing_until <= Date.now() / 1000) {
            card.querySelector('h4').textContent = '配对码已过期';
            const code = card.querySelector('textarea[readonly]');
            if (code) code.value = '配对码已过期；请点击“重新生成配对码”';
          }
          if (
            card &&
            changed &&
            (card.contains(document.activeElement) || card.querySelector('input:checked'))
          ) {
            displayed.push(before);
            if (beforeOption) displayedOptions.push(beforeOption);
            continue;
          }
          displayed.push(binding);
          if (freshOption) displayedOptions.push(freshOption);
          const oneBinding = { ...fresh, bindings: [binding] };
          if (!card) region.insertAdjacentHTML('beforeend', weixinBindings(oneBinding));
          else if (changed) card.outerHTML = weixinBindings(oneBinding);
        }
        for (const card of region.querySelectorAll('[data-binding-id]'))
          if (!displayed.some((b) => b.id === card.dataset.bindingId)) card.remove();
        p.owner.weixin.bindings = displayed;
        p.owner.weixin.owner_task_options = displayedOptions;
        for (const form of region.querySelectorAll('[data-channel-form="owner-authority"]'))
          syncBudgetForm(form);
        const notice = document.querySelector('.cc-wx-refresh-notice');
        notice.textContent = '';
        if (
          !fresh.enabled ||
          !(fresh.bindings || []).some(
            (b) =>
              b.state === 'awaiting_owner' ||
              (b.state === 'pairing' && b.pairing_until > Date.now() / 1000),
          )
        )
          pairingStop();
      } catch (error) {
        if (!pairingLive(p) || error.name === 'AbortError') return;
        if ([401, 403].includes(error.status)) {
          document.querySelector('.cc-wx-refresh-notice').textContent =
            '当前身份无权继续查询原配对，已停止自动刷新。请核对登录和当前房间后重新打开。';
          pairingStop();
          return;
        }
        if (
          !(p.owner.weixin.bindings || []).some(
            (b) =>
              b.state === 'awaiting_owner' ||
              (b.state === 'pairing' && b.pairing_until > Date.now() / 1000),
          )
        ) {
          document.querySelector('.cc-wx-refresh-notice').textContent =
            '配对码已到期，最新服务端状态暂不可用。请重新核对状态后生成新配对码，或继续核对已返回的联系人。';
          pairingStop();
          return;
        }
        p.failures++;
        document.querySelector('.cc-wx-refresh-notice').textContent =
          '暂时无法查询最新配对状态，以上为上次读取结果。将自动重试，也可点击“重新核对状态”。';
      } finally {
        p.controller = null;
        if (pairingLive(p))
          p.timer = setTimeout(
            () => pairingPoll(p),
            Math.min(30000, 5000 * 2 ** Math.min(p.failures, 3)),
          );
      }
    }

    function legacyMarkup(data) {
      const w = data.weixin,
        r = data.scopes;
      const slots = mentionSlots().filter((s) => s.duplex && s.state === 'registered');
      const projects = [
        ...new Map(
          (
            state.snapshot.conversation?.projects || [
              {
                project_id: state.snapshot.room.project_id || state.project,
                project_label: state.project,
                environment_id: state.environment,
              },
            ]
          )
            .filter((p) => p.environment_id === state.environment)
            .map((p) => [p.project_id, p]),
        ).values(),
      ];
      const bindings = weixinBindings(w);
      const policies = (r.items || [])
        .map(
          (item) =>
            `<div class="cc-channel-binding"><strong>连接 ${E(item.grant_id.slice(-8))} · ${listState(item.state)}</strong><p>版本 ${E(item.version)} · ${item.manifest?.lifetime_mode === 'until_revoked' && item.expires_at === 0 ? '长期（持续至撤销）' : '到期 ' + stamp(item.expires_at)} · ${item.manifest?.execution_mode === 'file_tools_only' ? '仅文件工具' : '账户权限执行（不是 OS 沙箱）'}</p><p>${(item.manifest?.project_rules || []).map((p) => E(p.project_id) + '：' + E(p.capabilities.join(' / '))).join('<br>')}</p>${item.state === 'active' ? button('dot-channel-scope-revoke', '停用此房间连接', { id: item.grant_id }) : ''}</div>`,
        )
        .join('');
      const deliveries = (w.deliveries || [])
        .map(
          (d) =>
            `<details class="cc-channel-delivery" data-delivery-id="${E(d.id)}"><summary>${listState(d.state)} · ${E(d.id.slice(-8))}</summary>${(d.parts || []).map((p) => `<p>${E(String(p.ordinal + 1))}. ${p.kind === 'text' ? '正文' : '附件'}：${listState(p.state)} ${E(p.reason_code || '')}</p>`).join('')}<div data-channel-form="delivery">${(d.parts || []).some((p) => p.state === 'unknown') ? field('人工核对依据（不是平台回执）', '<input name="confirmation_note" maxlength="1000" required autocomplete="off">') + button('dot-channel-confirm-received', '登记人工已收到', d) : ''}${button('dot-channel-cancel-unsent', '取消尚未发送部分', d)}</div></details>`,
        )
        .join('');
      return `<div class="cc-channel-pane"><p class="cc-hint">当前项目：${E(state.project)} · ${E(state.environment)}。刷新仅查询证据，不重发消息、不领取任务。</p>${button('dot-channel-open', '重新核对状态')}
      <section><h3>微信私聊通道</h3><p>${w.enabled ? '功能已开启；真实收发仍以每条回执为准。' : '微信通道已关闭。可在此开启，再扫码连接本人微信。'}</p><p>仅支持机器人私聊，不冒充原生群聊。仅在面板核对本人联系人并明确开启任务授权后，才可在原范围交办任务。</p>${channelSetting('weixin', w)}<div class="cc-wx-health">${weixinHealth(w)}</div><p class="cc-wx-refresh-notice" role="status" aria-live="polite"></p>${qrPanel(w)}<div class="cc-wx-bindings">${bindings || '<p>当前房间尚未绑定微信。</p>'}</div>
      <details><summary>高级：登记已有授权机器人</summary><div data-channel-form="account" class="cc-form">${field('名称', '<input name="label" maxlength="80" required autocomplete="off">')}${field('机器人编号', '<input name="bot_id" maxlength="256" required autocomplete="off">')}${field('已有授权令牌', '<input type="password" name="token" maxlength="16384" required autocomplete="new-password">')}${checkbox('confirm_credentials', '我同意在本服务加密保存此已授权凭据', true) + checkbox('confirm_provider_terms', '我已核对并同意微信相关服务条款及使用资格', true)}${button('dot-channel-import', '确认登记凭据', {}, w.enabled ? '' : 'disabled')}<small>凭据加密保存；不创建 MCP 凭据，也不会显示到消息中。</small></div></details>
      <p class="cc-wx-binding-guide" hidden></p><details class="cc-wx-bind"><summary>绑定当前话题与 dot</summary><div data-channel-form="bind" class="cc-form">${field(
        '已登记机器人',
        select(
          'account_id',
          (w.accounts || [])
            .filter((a) => a.state === 'active')
            .map((a) => [a.id, a.label + ' · ' + a.bot_id]),
        ),
      )}${field(
        '此话题的 dot',
        select(
          'dot_id',
          slots.map((s) => [s.id, s.label]),
        ),
      )}${field('绑定有效期', '<select name="lifetime_mode"><option value="timed" selected>定期到期（沿用原默认期限）</option><option value="until_revoked">持续至面板撤销</option></select>')}${checkbox('bind_until_revoked', '如选择持续至撤销，我确认此微信绑定及已批准的 dot 范围持续有效，直到我在面板撤销')}<small>仅影响本次新绑定，不自动延长已有绑定。微信平台登录可能失效，届时仍需本人重新连接；仅绑定不授予任务执行权限。</small>${checkbox('allow_replies', '允许将该 dot 对原微信消息的最终回复发送给原联系人')}${checkbox('allow_media', '允许文件外发方向；每份文件仍需单独确认')}${checkbox('confirm_ingress', '我确认将此微信私聊接入当前话题，并会核对实际联系人', true)}${button('dot-channel-bind', '生成配对码', {}, w.enabled ? '' : 'disabled')}</div></details>
      <details><summary>将已有产物附加到原回复</summary><div data-channel-form="files" class="cc-form">${field(
        '最近的原 dot 最终回复',
        select(
          'message_id',
          (w.reply_candidates || []).map((m) => [m.id, m.body_text || m.id]),
        ),
      )}<fieldset><legend>最近的已生成产物（最多16件）</legend>${(w.artifact_candidates || []).map((a) => `<label class="cc-channel-check"><input type="checkbox" name="artifact_ids" value="${E(a.artifact_id)}">${E(a.name)} · ${E(a.bytes)} 字节</label>`).join('') || '<p>当前项目没有可选产物。</p>'}</fieldset>${w.artifact_candidates_more ? '<p>此处仅列最近50件；更早产物请在产物管理中核对编号。</p>' : ''}${button('dot-channel-file-preview', '预览文件与原收件人', {}, w.enabled ? '' : 'disabled')}<div class="wx-file-preview"></div><small>只传输已有产物，不重跑原生成任务；正文不会重复发送。</small></div></details>
      <h4>最近投递分项</h4>${deliveries || '<p>暂无外发记录。</p>'}</section>
      ${telegramPanel(data.telegram, w, slots)}<section><h3>高级：整条 MCP 凭据限制</h3><p>范围绑定服务器上的原 grant，省略 room_id 也不能绕过。启用或修改会影响所有使用同一凭据的会话；工作目录不是操作系统沙箱。</p>${policies || '<p>尚未启用普通工具的房间限制。</p>'}<details><summary>明确设置连接范围</summary><div data-channel-form="scope" class="cc-form">${field(
        '当前房间已有连接',
        select(
          'grant_id',
          (r.eligible_connections || []).map((g) => [
            g.id,
            g.label +
              ' · ' +
              g.id.slice(-8) +
              (g.scope_version ? '（已有范围，修改会替换原范围）' : ''),
          ]),
        ),
      )}<fieldset><legend>当前环境的项目与逐项目能力</legend>${projects.map((p) => `<div class="cc-channel-project" data-project-id="${E(p.project_id)}"><label><input type="checkbox" name="project_selected" ${p.project_id === (state.snapshot.room.project_id || state.project) ? 'checked disabled' : ''}>${E(p.project_label || p.project_id)}</label>${checkbox('write', '允许项目文件写入')}${checkbox('execute', '允许此项目的 Agent 执行')}</div>`).join('')}</fieldset>${field('凭据限制有效期', '<select name="scope_lifetime"><option value="bounded">定时到期</option><option value="until_revoked">长期（持续至撤销）</option></select>')}${field('限制期限（小时，0 为长期）', '<input type="number" name="hours" value="24" min="0" max="720" step="1" required>')}${checkbox('unsandboxed', '如选择执行，我明确接受 Agent 账户权限；这不是文件系统或网络沙箱')}${checkbox('confirm_scope', '我确认以本次选择替换此凭据的房间范围，且影响它的所有会话', true)}${button('dot-channel-scope-save', '确认设置范围')}<p>严格 OS 隔离尚未验证时不会放行执行。未选择执行时只允许获批文件工具。</p></div></details></section></div>`;
    }

    const viewTitles = {
      overview: '消息连接',
      weixin: '微信连接',
      telegram: 'Telegram 连接',
      advanced: '高级 · 整条凭据限制',
    };
    let channelView = 'overview';
    const drafts = new Map();
    const draftKey = (form) =>
      form.dataset.channelForm + ':' + (form.closest('[data-binding-id]')?.dataset.bindingId || '');
    function syncScopeRootCheck(form) {
      const rows = [...form.querySelectorAll('[data-project-id]')];
      const selected = new Set(
        rows
          .filter((row) => row.querySelector('[name="project_selected"]').checked)
          .map((row) => row.dataset.projectId),
      );
      const blocked = (loaded?.scopes.project_root_checks || []).filter(
        (check) =>
          selected.has(check.project_id) &&
          (check.has_outside_projects ||
            (check.overlap_with_project_ids || []).some((id) => !selected.has(id))),
      );
      const labels = blocked.map(
        (check) =>
          rows
            .find((row) => row.dataset.projectId === check.project_id)
            ?.querySelector('label')
            ?.textContent.trim() || check.project_id,
      );
      const message = labels.length
        ? '目录预检：项目“' +
          labels.join('”、“') +
          '”的根目录包含未选项目映射，当前范围无法保存。请在项目管理核对这些项目的根目录，' +
          '使用独立目录的项目建立话题后再设置限制。调整映射后请刷新状态。'
        : '';
      const notice = form.querySelector('.cc-scope-root-check');
      notice.textContent = message;
      notice.hidden = !message;
      form.querySelector('[data-cc-action="dot-channel-scope-save"]').disabled = !!message;
      return message;
    }
    function scopeSignature(form) {
      return JSON.stringify({
        grant: form.querySelector('[name="grant_id"]').value,
        hours: form.querySelector('[name="hours"]').value,
        projects: [...form.querySelectorAll('[data-project-id]')].map((row) => [
          row.dataset.projectId,
          ...['project_selected', 'write', 'execute'].map(
            (name) => row.querySelector('[name="' + name + '"]').checked,
          ),
        ]),
      });
    }
    function prefillScope(form) {
      const grantId = form.querySelector('[name="grant_id"]').value;
      const saved = loaded.scopes.items.find((item) => item.grant_id === grantId);
      for (const row of form.querySelectorAll('[data-project-id]')) {
        const original = saved?.manifest?.project_rules?.find(
          (rule) => rule.project_id === row.dataset.projectId,
        );
        row.querySelector('[name="project_selected"]').checked =
          !!original || (!saved && row.querySelector('[name="project_selected"]').disabled);
        row.querySelector('[name="write"]').checked = !!original?.capabilities?.includes('write');
        row.querySelector('[name="execute"]').checked =
          !!original?.capabilities?.includes('execute');
      }
      const untilRevoked =
        saved?.manifest?.lifetime_mode === 'until_revoked' && saved.expires_at === 0;
      form.querySelector('[name="hours"]').value = untilRevoked
        ? '0'
        : saved?.expires_at
          ? String(
              Math.min(720, Math.max(1, Math.ceil((saved.expires_at - Date.now() / 1000) / 3600))),
            )
          : '24';
      syncScopeLifetime(form);
      for (const name of ['confirm_scope', 'unsandboxed'])
        form.querySelector('[name="' + name + '"]').checked = false;
      loaded.scopeDraft = saved ? { grantId, signature: scopeSignature(form) } : null;
      syncScopeRootCheck(form);
    }
    function syncScopeLifetime(form, changed = '') {
      const hours = form.querySelector('[name="hours"]');
      const mode = form.querySelector('[name="scope_lifetime"]');
      if (changed === 'scope_lifetime') hours.value = mode.value === 'until_revoked' ? '0' : '24';
      mode.value =
        hours.value.trim() !== '' && Number(hours.value) === 0 ? 'until_revoked' : 'bounded';
      if (changed)
        for (const name of ['confirm_scope', 'unsandboxed'])
          form.querySelector('[name="' + name + '"]').checked = false;
    }
    function rememberDrafts() {
      for (const form of document.querySelectorAll('.cc-channel-pane [data-channel-form]')) {
        const values = {};
        for (const input of form.querySelectorAll('input[name],select[name]')) {
          if (
            ['password', 'checkbox', 'radio'].includes(input.type) ||
            input.readOnly ||
            /verify|token|confirm/i.test(input.name)
          )
            continue;
          values[input.name] = input.value;
        }
        drafts.set(draftKey(form), values);
      }
    }
    function restoreDrafts() {
      for (const form of document.querySelectorAll('.cc-channel-pane [data-channel-form]')) {
        const values = drafts.get(draftKey(form));
        if (!values) continue;
        for (const input of form.querySelectorAll('input[name],select[name]')) {
          if (!(input.name in values) || ['checkbox', 'password'].includes(input.type)) continue;
          if (
            input.tagName === 'SELECT' &&
            ![...input.options].some((o) => o.value === values[input.name])
          )
            continue;
          input.value = values[input.name];
        }
      }
    }
    function paintView(element) {
      showDrawer(viewTitles[channelView], render(loaded), element);
      loaded.epoch = state.drawerEpoch;
      restoreDrafts();
      bindViewInputs();
      pairingStart();
    }

    function syncBudgetForm(form, reset = false) {
      const data = form.closest('.cc-tg-binding') ? loaded?.telegram : loaded?.weixin;
      const binding = data?.bindings.find(
        (b) => b.id === form.closest('[data-binding-id]')?.dataset.bindingId,
      );
      const option = binding && ownerTaskOption(data, binding.id);
      if (!option) return;
      const mode = form.querySelector('[name="budget_mode"]').value;
      const number = form.querySelector('[name="max_delegations"]');
      number.disabled = mode === 'unlimited';
      number.closest('.cc-field').hidden = mode === 'unlimited';
      const original = binding.owner_authorization?.max_delegations ?? option.max_delegations;
      for (const [name, visible] of [
        ['confirm_daily_budget', mode === 'rolling_24h'],
        ['confirm_unlimited_budget', mode === 'unlimited'],
        ['confirm_task_count_budget', mode !== 'unlimited' && Number(number.value) !== original],
      ]) {
        const input = form.querySelector('[name="' + name + '"]');
        input.disabled = !visible;
        input.required = visible;
        input.closest('label').hidden = !visible;
        if (reset || !visible) input.checked = false;
      }
      if (reset)
        for (const input of form.querySelectorAll('input[type="checkbox"]')) input.checked = false;
      form.querySelector('.cc-budget-summary').textContent =
        ownerBudgetLabel(mode, number.value) + '。任务超时、并发与必要的付款、权限确认仍然有效。';
    }
    function bindViewInputs() {
      const pane = document.querySelector('.cc-channel-pane');
      for (const form of pane?.querySelectorAll('[data-channel-form="owner-authority"]') || [])
        syncBudgetForm(form);
      for (const select of pane?.querySelectorAll('select[required]') || []) {
        if (!select.value && select.options.length === 2) select.value = select.options[1].value;
      }
      for (const form of pane?.querySelectorAll('[data-channel-form="enrollment"]') || [])
        void enrollmentRead(form);
      const scopeForm = pane?.querySelector('[data-channel-form="scope"]');
      if (scopeForm) prefillScope(scopeForm);
      pane?.addEventListener('change', (event) => {
        const enrollment = event.target.closest('[data-channel-form="enrollment"]');
        if (enrollment && ['account_id', 'dot_id', 'expected_peer_id'].includes(event.target.name))
          void enrollmentRead(enrollment);
        if (event.target.name === 'grant_id' && scopeForm) prefillScope(scopeForm);
        if (event.target.name === 'project_selected' && scopeForm) syncScopeRootCheck(scopeForm);
        if (
          scopeForm &&
          event.target.closest('[data-channel-form="scope"]') === scopeForm &&
          ['scope_lifetime', 'hours', 'project_selected', 'write', 'execute'].includes(
            event.target.name,
          )
        )
          syncScopeLifetime(scopeForm, event.target.name);
        if (['budget_mode', 'max_delegations'].includes(event.target.name)) {
          const form = event.target.closest('[data-channel-form="owner-authority"]');
          if (form) syncBudgetForm(form, true);
        }
      });
      pane?.addEventListener('input', (event) => {
        const enrollment = event.target.closest('[data-channel-form="enrollment"]');
        if (enrollment && event.target.name === 'expected_peer_id') enrollmentReset(enrollment);
        if (
          scopeForm &&
          event.target.name === 'hours' &&
          event.target.closest('[data-channel-form="scope"]') === scopeForm
        )
          syncScopeLifetime(scopeForm, 'hours');
        if (event.target.name === 'max_delegations') {
          const form = event.target.closest('[data-channel-form="owner-authority"]');
          if (form) syncBudgetForm(form, true);
        }
      });
    }
    function navigate(view, element) {
      if (!group(element, false) || !viewTitles[view]) return local;
      if (
        qr &&
        !qrTerminal(qr.entry) &&
        !window.confirm(
          '离开后将停止本次扫码，未完成的二维码会取消或到期失效。返回时需要重新开始。确定离开？',
        )
      )
        return local;
      rememberDrafts();
      pairingStop();
      qrDiscard(qr);
      channelView = view;
      paintView(element);
      return local;
    }
    function channelSteps(account, binding, tasks, telegram = false) {
      return (
        '<ol class="cc-connection-steps" aria-label="连接进度">' +
        [
          [account, '连接账号'],
          [binding, '私聊验证'],
          [tasks, '交办任务'],
        ]
          .map(
            ([done, title], i) =>
              '<li class="' +
              (done
                ? 'is-complete'
                : (!account && i === 0) ||
                    (account && !binding && i === 1) ||
                    (binding && !tasks && i === 2)
                  ? 'is-current'
                  : '') +
              '"><span aria-hidden="true">' +
              (done ? '✓' : i + 1) +
              '</span><strong>' +
              title +
              '</strong><small>' +
              (done ? '已完成' : '待完成') +
              '</small></li>',
          )
          .join('') +
        '</ol>'
      );
    }
    function channelSummary(data) {
      if (!data) return '当前不可用';
      const bindings = (data.bindings || []).filter((b) => b.state !== 'revoked');
      if (!data.enabled) return '通道已关闭';
      if (bindings.some((b) => b.state === 'awaiting_owner')) return '等待核对联系人';
      if (bindings.some((b) => b.state === 'pairing')) return '等待联系人配对';
      if (bindings.some((b) => b.state === 'active')) return '已绑定 · 收发以回执为准';
      if (bindings.some((b) => b.state === 'paused')) return '绑定已暂停';
      return (data.accounts || []).some((a) => a.state === 'active')
        ? '账号已登记 · 待配对'
        : '尚未连接';
    }
    function overview(data) {
      return (
        '<div class="cc-connection-intro"><h3>选择一个消息入口</h3><p>连接后，在熟悉的聊天里继续当前话题。</p></div><div class="cc-connection-options">' +
        [
          ['weixin', '微信', 'W', '扫码登录，连接本人私聊。'],
          ...(data.telegram ? [['telegram', 'Telegram', 'T', '连接机器人，同步本人私聊。']] : []),
        ]
          .map(
            ([kind, title, icon, description]) =>
              '<section class="cc-connection-option"><div class="cc-connection-icon is-' +
              kind +
              '" aria-hidden="true">' +
              icon +
              '</div><div class="cc-connection-option-copy"><h3>' +
              title +
              '</h3><p>' +
              description +
              '</p><span class="cc-connection-status">' +
              E(channelSummary(data[kind])) +
              '</span></div>' +
              button('dot-channel-view-' + kind, '设置' + title) +
              '</section>',
          )
          .join('') +
        '</div><div class="cc-connection-advanced"><div><strong>高级：整条 MCP 连接的限制</strong><p>影响使用该凭据的所有会话。连接微信、开启微信任务都不需要设置这里。</p></div>' +
        button('dot-channel-view-advanced', '查看高级设置') +
        '</div>'
      );
    }
    function disclosure(title, hint, className, open = false) {
      const node = document.createElement('details');
      node.className = className;
      node.open = open;
      node.innerHTML =
        '<summary><span><strong>' +
        E(title) +
        '</strong>' +
        (hint ? '<small>' + E(hint) + '</small>' : '') +
        '</span></summary>';
      return node;
    }
    function prepareChannel(data) {
      const template = document.createElement('template');
      template.innerHTML = legacyMarkup(data);
      const sections = template.content.querySelector('.cc-channel-pane').children;
      const section =
        channelView === 'weixin'
          ? sections[2]
          : channelView === 'telegram'
            ? template.content.querySelector('.cc-tg-settings')
            : sections[sections.length - 1];
      if (channelView === 'weixin') {
        const w = data.weixin;
        const accounts = (w.accounts || []).filter((a) => a.state === 'active');
        const live = (w.bindings || []).filter((b) => b.state !== 'revoked');
        const active = live.some((b) => b.state === 'active');
        for (const node of [...section.children].slice(0, 3)) node.remove();
        section.classList.add('cc-wx-settings');
        section.insertAdjacentHTML(
          'afterbegin',
          '<p class="cc-connection-intro">集中核对授权，再由本人私聊验证。已有连接不会自动扩大权限。</p>' +
            channelSteps(
              !!accounts.length,
              active,
              live.some((b) => b.owner_authorization?.state === 'active'),
            ),
        );
        const qrNode = section.querySelector('.cc-wx-onboarding');
        const account = disclosure(
          '1. 微信账号',
          accounts.length ? '账号已登记 · 重连与维护' : '扫码登录并核对本人账号',
          'cc-connection-section cc-wx-account-step',
          !accounts.length,
        );
        qrNode.before(account);
        account.append(qrNode);
        const bind = section.querySelector('.cc-wx-bind');
        bind.classList.add('cc-connection-section');
        bind.open = false;
        bind.insertAdjacentHTML('beforebegin', enrollmentForm('weixin', w));
        bind.querySelector('summary').textContent = '高级：仅绑定聊天，稍后单独授权任务';
        const maintenance = disclosure(
          '连接维护与收发记录',
          '',
          'cc-connection-secondary cc-connection-maintenance',
        );
        for (const selector of ['.cc-channel-switch', '.cc-wx-health']) {
          const node = section.querySelector(selector);
          if (node && selector === '.cc-channel-switch' && !w.enabled)
            section.insertBefore(node, account);
          else if (node) maintenance.append(node);
        }
        for (const name of ['account', 'files']) {
          const node = section
            .querySelector('[data-channel-form="' + name + '"]')
            ?.closest('details');
          if (node) {
            node.classList.add('cc-connection-secondary');
            maintenance.append(node);
          }
        }
        const receipts = disclosure(
          '最近投递记录',
          '已接受不等于已读，未知结果不会自动重发',
          'cc-connection-secondary',
        );
        for (const node of [...section.children]) {
          if (
            node.tagName === 'H4' ||
            node.matches('.cc-channel-delivery') ||
            (node.tagName === 'P' && node.textContent === '暂无外发记录。')
          )
            receipts.append(node);
        }
        receipts.querySelector('h4')?.remove();
        maintenance.append(receipts);
        section.append(maintenance);
      } else if (channelView === 'telegram') {
        const t = data.telegram;
        const hasAccount = (t.accounts || []).some((a) => a.state === 'active');
        const active = (t.bindings || []).some((b) => b.state === 'active');
        section.querySelector('.cc-channel-heading')?.remove();
        section.insertAdjacentHTML(
          'afterbegin',
          channelSteps(
            hasAccount,
            active,
            (t.bindings || []).some((b) => b.owner_authorization?.state === 'active'),
            true,
          ),
        );
        const account = section.querySelector('.cc-tg-connect');
        account.classList.add('cc-connection-section');
        account.open = !hasAccount;
        account.querySelector('summary').textContent = '1. 连接 Telegram 机器人';
        const bind = section.querySelector('[data-channel-form="tg-bind"]').closest('details');
        bind.classList.add('cc-connection-section');
        bind.open = false;
        bind.insertAdjacentHTML('beforebegin', enrollmentForm('telegram', t));
        bind.querySelector('summary').textContent = '高级：仅同步聊天，稍后单独授权任务';
        const maintenance = disclosure(
          '同步、文件与连接维护',
          '',
          'cc-connection-secondary cc-connection-maintenance',
        );
        const setting = section.querySelector('.cc-channel-switch');
        if (t.enabled && setting) maintenance.append(setting);
        for (const node of [...section.querySelectorAll('[data-channel-form="wx-mirror"]')])
          maintenance.append(node);
        const files = section.querySelector('[data-channel-form="tg-files"]')?.closest('details');
        if (files) maintenance.append(files);
        for (const node of [...section.children])
          if (node.tagName === 'H4' || node.matches('.cc-channel-delivery'))
            maintenance.append(node);
        section.append(maintenance);
      } else {
        section.classList.add('cc-advanced-settings');
        section.querySelector('h3')?.remove();
        section.querySelector('p')?.remove();
        section.insertAdjacentHTML(
          'afterbegin',
          '<div class="cc-connection-warning"><strong>这会限制整条 MCP 连接</strong><p>所有使用同一凭据的会话都会受影响。增删当前房间项目请使用房间菜单中的“管理房间项目”。这里不是微信连接期限，也不是微信任务授权的必需步骤。</p></div>',
        );
        const form = section.querySelector('[data-channel-form="scope"]');
        const detail = form.closest('details');
        detail.classList.add('cc-connection-section');
        detail.querySelector('summary').textContent = (data.scopes.items || []).length
          ? '修改已有连接的限制'
          : '设置凭据限制';
        detail.open = !(data.scopes.items || []).length;
        form.querySelector('fieldset').classList.add('cc-scope-projects');
        form
          .querySelector('fieldset')
          .insertAdjacentHTML(
            'afterend',
            '<p class="cc-connection-warning cc-scope-root-check" role="status" aria-live="polite" hidden></p>',
          );
        const hours = form.querySelector('[name="hours"]');
        const label = hours.closest('.cc-field');
        if (label) {
          const text = [...label.childNodes].find((n) => n.nodeType === Node.TEXT_NODE);
          if (text) text.textContent = '限制期限（小时，0 为长期）';
          else
            label.querySelector('span') &&
              (label.querySelector('span').textContent = '限制期限（小时，0 为长期）');
        }
        hours.setAttribute('aria-describedby', 'cc-scope-expiry-help');
        hours.insertAdjacentHTML(
          'afterend',
          '<small id="cc-scope-expiry-help">输入 0 或选择“长期”表示此限制持续至撤销；定时默认 24 小时，最长 720 小时。定时到期或撤销后普通工具会被拒绝访问，不会恢复为更宽权限。原凭据自身有效期、项目权限和每次任务超时仍然适用。</small>',
        );
      }
      return section.outerHTML;
    }
    function render(data) {
      return (
        '<div class="cc-channel-pane" data-channel-view="' +
        channelView +
        '"><nav class="cc-connection-nav" aria-label="连接设置">' +
        (channelView !== 'overview'
          ? button('dot-channel-view-overview', '‹ 连接总览')
          : '<span>当前话题的消息入口</span>') +
        button('dot-channel-open', '刷新状态') +
        '</nav>' +
        (channelView === 'overview' ? overview(data) : prepareChannel(data)) +
        '<p class="cc-connection-footnote">消息是否送达，以实际回执为准。</p></div>'
      );
    }
    async function open(element, selectedAccount = '') {
      if (loaded && current(loaded.key) && loaded.epoch === state.drawerEpoch) rememberDrafts();
      else {
        channelView = 'overview';
        drafts.clear();
      }
      if (selectedAccount) channelView = 'weixin';
      pairingStop();
      qrDiscard(qr);
      if (!allowed()) throw new Error('当前身份不能管理房间连接。');
      controller?.abort();
      controller = new AbortController();
      const key = capture();
      showDrawer(viewTitles[channelView], '<p role="status">正在读取连接状态…</p>', element);
      const epoch = state.drawerEpoch;
      const query = new URLSearchParams(scope());
      let result;
      try {
        result = await Promise.all([
          api(
            '/api/collaboration/room-scopes?' +
              new URLSearchParams({
                project: key.project,
                environment_id: key.environment_id,
                conversation_id: key.conversation_id,
              }),
            { signal: controller.signal },
          ),
          api('/api/collaboration/weixin?' + query, { signal: controller.signal }),
          ...(state.snapshot?.capabilities?.telegram_channel_v1
            ? [api('/api/collaboration/telegram?' + query, { signal: controller.signal })]
            : []),
        ]);
      } catch (error) {
        if (!current(key) || epoch !== state.drawerEpoch || error.name === 'AbortError')
          return local;
        throw error;
      }
      if (!current(key) || epoch !== state.drawerEpoch) return local;
      const [scopes, weixin, telegram] = result;
      loaded = { key, scopes, weixin, telegram };
      paintView(element);
      if (selectedAccount) {
        const guide = document.querySelector('.cc-wx-binding-guide');
        const existing = weixin.bindings.find(
          (binding) => binding.account_id === selectedAccount && binding.state !== 'revoked',
        );
        if (existing) {
          guide.hidden = false;
          guide.textContent =
            '微信登录已保存。请在上方原绑定中用新配对码重新验证同一联系人，并在面板确认；原批准范围才会恢复，旧任务与消息不会重放。';
          const card = [...document.querySelectorAll('.cc-wx-bindings [data-binding-id]')].find(
            (node) => node.dataset.bindingId === existing.id,
          );
          card?.querySelector('textarea,button')?.focus();
        } else {
          const form = document.querySelector(
            '[data-channel-form="enrollment"][data-enrollment-channel="weixin"]',
          );
          const select = form?.querySelector('[name="account_id"]');
          guide.hidden = false;
          if (select && [...select.options].some((option) => option.value === selectedAccount)) {
            // Saving login credentials is not task consent. Continue with the
            // exact new account's summary and an unchecked acknowledgement,
            // never reopen the legacy manual-pairing detour.
            select.value = selectedAccount;
            form.closest('.cc-enrollment').prepend(guide);
            guide.textContent =
              '微信登录已保存。下一步核对本次授权摘要；确认后向原私聊发送本人授权码，即可自动开通，无需返回面板重复确认。';
            select.focus();
            await enrollmentRead(form);
          } else {
            document.querySelector('.cc-wx-bind').before(guide);
            guide.textContent =
              '微信登录已保存，但当前话题暂不具备生成本人任务授权码的条件。请检查已有绑定和 dot 范围；尚未新增任何绑定或任务权限。';
          }
        }
      }
      return local;
    }
    function group(element, validate = true) {
      const pane = document.querySelector('.cc-channel-pane');
      if (
        !loaded ||
        !current(loaded.key) ||
        loaded.epoch !== state.drawerEpoch ||
        !pane?.contains(element)
      )
        throw new Error('房间或登录状态已变化，请重新读取当前连接。');
      const root = element.closest('[data-channel-form]');
      if (
        validate &&
        root &&
        [...root.querySelectorAll('input,select,textarea')].some((input) => !input.reportValidity())
      )
        return null;
      const value = (name) => root?.querySelector('[name="' + name + '"]')?.value || '';
      const checked = (name) => root?.querySelector('[name="' + name + '"]')?.checked === true;
      return { root, value, checked };
    }
    async function act(action, element) {
      if (action === 'dot-channel-open') {
        // A state refresh never cancels or creates an enrollment. Preserve its
        // original request identity while QR status or a lost response settles.
        if (qr && qrLive(qr) && !qrTerminal(qr.entry)) {
          if (qr.entry) return qrStatus(qr);
          return { local: true, message: '登录请求尚未确认，请在当前扫码区域恢复原请求。' };
        }
        return open(element);
      }
      if (saving) return local;
      if (action.startsWith('dot-channel-view-'))
        return navigate(action.slice('dot-channel-view-'.length), element);
      if (action.startsWith('dot-channel-qr-')) return qrAct(action, element);
      if (action === 'dot-channel-enrollment-preview')
        return enrollmentRead(group(element, false).root);
      const data = group(
        element,
        ![
          'dot-channel-cancel-unsent',
          'dot-channel-mirror-cancel',
          'dot-channel-tg-file-preview',
          'dot-channel-file-preview',
        ].includes(action),
      );
      if (!data) return local;
      const { root, value, checked } = data,
        key = loaded.key;
      let operation, args;

      if (['dot-channel-setting-weixin', 'dot-channel-setting-telegram'].includes(action)) {
        const channel = action.slice('dot-channel-setting-'.length);
        const channelData = loaded[channel];
        if (!channelData?.setting) throw new Error('请重新读取通道开关。');
        if (
          !window.confirm(
            (channelData.enabled ? '关闭' : '开启') +
              (channel === 'weixin' ? '微信' : 'Telegram') +
              '通道？此设置影响你在当前空间下此通道的全部连接。关闭会停止尚未发送的回复且不再补发；已提交请求无法召回，结果未知时不自动重发。另一通道及已有绑定保持不变。',
          )
        )
          return local;
        operation = 'channel-settings';
        args = {
          channel,
          enabled: !channelData.enabled,
          expected_version: channelData.setting.version,
        };
      } else if (action === 'dot-channel-enrollment-approve') {
        const approved = enrollmentPreviews.get(root);
        if (
          !checked('enrollment_agreement') ||
          !approved?.preview ||
          approved.owner !== loaded ||
          approved.epoch !== state.drawerEpoch ||
          approved.selection !== enrollmentSelection(root)
        )
          throw new Error('账号、联系人或授权摘要已变化，请重新核对并明确同意本次范围。');
        const channel = root.dataset.enrollmentChannel;
        operation = channel + '-bind';
        args = {
          account_id: value('account_id'),
          dot_id: value('dot_id'),
          confirm_external_data_ingress: true,
          ...(channel === 'weixin'
            ? { allow_replies: true, allow_media_outbound: false }
            : { confirm_full_mirror: true }),
          owner_preapproval: {
            text_version: approved.preview.text_version,
            expected_scope_digest: approved.preview.scope_digest,
            expected_peer_id: value('expected_peer_id').trim(),
            confirm_owner_identity: true,
            confirm_future_tasks: true,
            confirm_result_delivery: true,
            confirm_until_revoked: true,
            confirm_pairing_notices: true,
          },
        };
      } else if (action === 'dot-channel-tg-import') {
        operation = 'telegram-account';
        const old = loaded.telegram.accounts.find((a) => a.bot_id === value('bot_id').trim());
        if (
          old &&
          !window.confirm('替换此 Telegram 机器人凭据并撤销旧绑定？不会迁移或重发历史消息。')
        )
          return local;
        args = {
          bot_id: value('bot_id').trim(),
          label: value('label').trim(),
          token: value('token'),
          expected_version: old?.credential_version || 0,
          confirm_credential_storage: checked('credentials'),
          confirm_provider_terms: checked('provider_terms'),
        };
      } else if (action === 'dot-channel-tg-bind') {
        operation = 'telegram-bind';
        args = {
          account_id: value('account_id'),
          dot_id: value('dot_id'),
          confirm_external_data_ingress: checked('ingress'),
          confirm_full_mirror: checked('mirror'),
          disable_notification: checked('silent'),
        };
      } else if (
        [
          'dot-channel-tg-activate',
          'dot-channel-tg-pause',
          'dot-channel-tg-resume',
          'dot-channel-tg-revoke',
        ].includes(action)
      ) {
        const row = loaded.telegram.bindings.find((b) => b.id === element.dataset.id);
        if (!row) throw new Error('原 Telegram 绑定已不在当前列表。');
        if (
          action === 'dot-channel-tg-revoke' &&
          !window.confirm('撤销原 Telegram 绑定？所有未发送部分停止，结果未知的投递不会自动重发。')
        )
          return local;
        operation = 'telegram-control';
        args = {
          binding_id: row.id,
          expected_version: row.version,
          action: action.slice('dot-channel-tg-'.length),
          confirm_same_owner: checked('same_owner'),
          confirm_user_id: checked('same_owner') ? row.pending_user_id : '',
          confirm_chat_id: checked('same_owner') ? row.pending_chat_id : '',
        };
      } else if (['dot-channel-tg-file-preview', 'dot-channel-tg-files'].includes(action)) {
        const binding = loaded.telegram.bindings.find((b) => b.id === value('tg_binding'));
        const message = loaded.telegram.reply_candidates.find((m) => m.id === value('tg_message'));
        const ids = [...root.querySelectorAll('[name="tg_artifact_ids"]:checked')].map(
          (input) => input.value,
        );
        if (!binding || !message || !ids.length || ids.length > 16)
          throw new Error('请选择原收件人、原回复与1至16件实际文件。');
        const selected = {
          binding_id: binding.id,
          expected_binding_version: binding.version,
          message_id: message.id,
          expected_message_version: message.version,
          artifact_ids: ids,
        };
        if (action === 'dot-channel-tg-file-preview') {
          const previewOwner = loaded,
            previewEpoch = state.drawerEpoch;
          saving = true;
          try {
            const preview = await mutation('telegram-files-preview', { ...selected, ...scope() });
            if (
              !current(key) ||
              loaded !== previewOwner ||
              previewEpoch !== state.drawerEpoch ||
              !root.isConnected
            )
              return local;
            loaded.filePreview = { selected, preview };
            root.querySelector('.tg-file-preview').innerHTML =
              `<section class="cc-channel-binding"><strong>核对后才发送</strong><p>项目：${E(preview.project_id || '')} · 原话题：${E(preview.conversation_id || '')} · 绑定版本 ${E(preview.binding_version)}</p><p>Telegram user_id：${E(preview.recipient.user_id)} · chat_id：${E(preview.recipient.chat_id)}</p><p>原回复：${E(preview.message_id.slice(-8))} · 版本 ${E(preview.message_version)}</p>${preview.files.map((f) => `<div>${E(f.name)} · ${E(f.bytes)} 字节<small>SHA-256：${E(f.sha256)}</small><small>保留至：${stamp(f.expires_at)}</small></div>`).join('')}<p>总计 ${E(preview.total_bytes)} 字节</p>${checkbox('file_confirm', '我确认仅向以上本人Telegram私聊发送以上完整文件清单', true)}${button('dot-channel-tg-files', '确认并排队发送这些文件')}</section>`;
          } finally {
            saving = false;
          }
          return local;
        }
        if (
          !loaded.filePreview ||
          JSON.stringify(loaded.filePreview.selected) !== JSON.stringify(selected)
        )
          throw new Error('文件、原回复或收件人已变化，请重新预览。');
        operation = 'telegram-files';
        args = {
          ...selected,
          preview_digest: loaded.filePreview.preview.preview_digest,
          confirm_file_egress: checked('file_confirm'),
        };
      } else if (action === 'dot-channel-wx-mirror') {
        const row = loaded.weixin.bindings.find((b) => b.id === element.dataset.id);
        if (!row) throw new Error('原微信绑定已不在当前列表。');
        operation = 'weixin-mirror-consent';
        args = {
          binding_id: row.id,
          expected_version: row.version,
          confirm_full_mirror: checked('mirror'),
          confirm_same_owner: checked('mirror'),
        };
      } else if (
        [
          'dot-channel-mirror-confirm',
          'dot-channel-mirror-cancel',
          'dot-channel-tg-file-preview',
        ].includes(action)
      ) {
        const row = loaded.telegram.deliveries.find((d) => d.id === element.dataset.id);
        if (!row) throw new Error('原投递记录已不在当前列表。');
        operation = 'channel-delivery-control';
        args = {
          delivery_id: row.id,
          action: action === 'dot-channel-mirror-confirm' ? 'confirm_received' : 'cancel_unsent',
          confirmation_note: value('confirmation_note'),
        };
      } else if (action === 'dot-channel-import') {
        operation = 'weixin-account';
        const existing = loaded.weixin.accounts.find((a) => a.bot_id === value('bot_id').trim());
        if (
          existing &&
          !window.confirm('替换这个机器人已有凭据？旧绑定将撤销，未确认投递不会自动迁移。')
        )
          return local;
        args = {
          label: value('label').trim(),
          bot_id: value('bot_id').trim(),
          token: value('token'),
          expected_version: existing?.credential_version || 0,
          confirm_credential_storage: checked('confirm_credentials'),
          confirm_provider_terms: checked('confirm_provider_terms'),
        };
      } else if (action === 'dot-channel-bind') {
        const lifetime = value('lifetime_mode') || 'timed';
        if (lifetime === 'until_revoked' && !checked('bind_until_revoked'))
          throw new Error('持续至撤销需要勾选本次绑定的有效期确认。');
        operation = 'weixin-bind';
        args = {
          account_id: value('account_id'),
          dot_id: value('dot_id'),
          confirm_external_data_ingress: checked('confirm_ingress'),
          allow_replies: checked('allow_replies'),
          allow_media_outbound: checked('allow_media'),
          lifetime_mode: lifetime,
          confirm_until_revoked: lifetime === 'until_revoked' && checked('bind_until_revoked'),
        };
      } else if (
        [
          'dot-channel-owner-approve',
          'dot-channel-owner-revoke',
          'dot-channel-tg-owner-approve',
          'dot-channel-tg-owner-revoke',
        ].includes(action)
      ) {
        const channel = action.startsWith('dot-channel-tg-owner-') ? 'telegram' : 'weixin';
        const channelData = loaded[channel];
        const original = channelData.bindings.find((b) => b.id === element.dataset.id);
        const row =
          original &&
          (channel === 'telegram'
            ? { ...original, peer_id: original.chat_id, allow_replies: original.mirror_enabled }
            : original);
        if (!row) throw new Error('原本人绑定已不在当前列表。');
        const approve = action.endsWith('owner-approve');
        const option = ownerTaskOption(channelData, row.id);
        const existingAuthority = row.owner_authorization;
        const selectedMode = value('budget_mode') || 'lifetime';
        if (
          approve &&
          existingAuthority?.state === 'active' &&
          selectedMode === (existingAuthority.budget_mode || 'lifetime') &&
          (selectedMode === 'unlimited' ||
            Number(value('max_delegations')) === existingAuthority.max_delegations)
        )
          return { local: true, message: '任务数量设置没有变化，已保留原授权与正在进行的任务。' };
        if (
          approve &&
          (!channelData.enabled ||
            row.state !== 'active' ||
            !row.peer_id ||
            option?.available !== true ||
            !option.scope_digest ||
            row.allow_replies === false ||
            !(existingAuthority?.state === 'active'
              ? checked('confirm_budget_change')
              : checked('confirm_owner_agreement')))
        )
          throw new Error('请重新核对原联系人与任务范围，并逐项确认本人任务授权。');
        if (
          !approve &&
          (!row.owner_authorization ||
            !window.confirm(
              '撤销此本人微信的任务授权？停止通过此授权新增任务并取消关联未完成任务；原绑定聊天仍保留。已完成的外部操作不能撤回。',
            ))
        )
          return local;
        operation = channel + '-owner-authority';
        args = {
          binding_id: row.id,
          expected_binding_version: row.version,
          expected_authority_version: row.owner_authorization?.version || 0,
          action: approve ? 'approve' : 'revoke',
          ...(approve
            ? {
                confirm_peer_id: row.peer_id,
                expected_scope_digest: option.scope_digest,
                confirm_owner_tasks: true,
                confirm_until_revoked: true,
                budget_mode: value('budget_mode') || 'lifetime',
                ...(value('budget_mode') === 'rolling_24h'
                  ? { confirm_daily_budget: checked('confirm_daily_budget') }
                  : {}),
                ...(value('budget_mode') === 'unlimited'
                  ? { confirm_unlimited_budget: checked('confirm_unlimited_budget') }
                  : {}),
                ...(value('budget_mode') !== 'unlimited' &&
                Number(value('max_delegations')) !==
                  (row.owner_authorization?.max_delegations ?? option.max_delegations)
                  ? {
                      max_delegations: Number(value('max_delegations')),
                      confirm_task_count_budget: checked('confirm_task_count_budget'),
                    }
                  : {}),
                ...(row.owner_authorization?.state === 'active'
                  ? { confirm_budget_change: checked('confirm_budget_change') }
                  : {}),
              }
            : {}),
        };
      } else if (
        [
          'dot-channel-activate',
          'dot-channel-pause',
          'dot-channel-resume',
          'dot-channel-refresh-pairing',
          'dot-channel-revoke',
        ].includes(action)
      ) {
        const row = loaded.weixin.bindings.find((b) => b.id === element.dataset.id);
        if (!row) throw new Error('原绑定已不在当前列表。');
        if (
          action === 'dot-channel-revoke' &&
          !window.confirm(
            '撤销原微信绑定？将停止此联系人任务授权、取消关联未完成任务并停止新转发和未发送回复。已提交的操作不能召回；未知投递仍保留，不会重发到新账号。',
          )
        )
          return local;
        if (
          action === 'dot-channel-refresh-pairing' &&
          !window.confirm(
            '重新生成配对码？旧配对码将失效，原批准范围保留；须重新验证联系人并在面板确认，不会重放旧任务或旧消息。',
          )
        )
          return local;
        if (action === 'dot-channel-resume' && row.revalidation_required)
          throw new Error('原联系人尚未重新验证，请使用配对码并在面板确认，不能直接恢复。');
        operation = 'weixin-control';
        args = {
          binding_id: row.id,
          expected_version: row.version,
          action:
            action === 'dot-channel-refresh-pairing'
              ? 'refresh_pairing'
              : action.slice('dot-channel-'.length),
          confirm_peer_id:
            action === 'dot-channel-activate' && checked('confirm_peer') ? row.pending_peer_id : '',
        };
      } else if (action === 'dot-channel-scope-save') {
        const rootWarning = syncScopeRootCheck(root);
        if (rootWarning) throw new Error(rootWarning);
        const grant = loaded.scopes.eligible_connections.find((g) => g.id === value('grant_id'));
        if (!grant) throw new Error('请选择当前已有连接。');
        if (
          loaded.scopeDraft?.grantId === grant.id &&
          loaded.scopeDraft.signature === scopeSignature(root)
        )
          return { local: true, message: '连接范围没有变化，已保留原设置与到期时间。' };
        const hours = Number(value('hours'));
        if (!value('hours').trim() || !Number.isInteger(hours) || hours < 0 || hours > 720)
          throw new Error('请输入 0（长期），或 1 至 720 的整数小时。');
        const rules = [...root.querySelectorAll('[data-project-id]')]
          .filter((p) => p.querySelector('[name="project_selected"]').checked)
          .map((p) => ({
            project_id: p.dataset.projectId,
            capabilities: [
              'read',
              ...(p.querySelector('[name="write"]').checked ? ['write'] : []),
              ...(p.querySelector('[name="execute"]').checked ? ['execute'] : []),
            ],
            execution_targets: ['agent'],
          }));
        const execute = rules.some((r) => r.capabilities.includes('execute'));
        if (execute && !checked('unsandboxed'))
          throw new Error('选择执行需要明确接受账户权限边界；未确认时仅使用文件工具。');
        operation = 'room-scope';
        args = {
          grant_id: grant.id,
          expected_version: grant.scope_version || 0,
          project_rules: rules,
          execution_mode: execute ? 'account_permissions_acknowledged' : 'file_tools_only',
          acknowledge_unsandboxed_exec: checked('unsandboxed'),
          expires_seconds: hours * 3600,
          confirm_connection_wide_ceiling: checked('confirm_scope'),
        };
      } else if (action === 'dot-channel-scope-revoke') {
        const row = loaded.scopes.items.find((r) => r.grant_id === element.dataset.id);
        if (!row || !window.confirm('停用此房间连接？它将被拒绝访问，不会退回更宽的原授权。'))
          return local;
        operation = 'room-scope-control';
        args = { grant_id: row.grant_id, expected_version: row.version, action: 'revoke' };
      } else if (['dot-channel-files', 'dot-channel-file-preview'].includes(action)) {
        const reply = loaded.weixin.reply_candidates.find((m) => m.id === value('message_id'));
        const ids = [...root.querySelectorAll('[name="artifact_ids"]:checked')].map(
          (input) => input.value,
        );
        if (!reply || !ids.length || ids.length > 16)
          throw new Error('请选择原回复与1至16件实际产物。');
        const selected = {
          message_id: reply.id,
          expected_message_version: reply.version,
          artifact_ids: ids,
        };
        if (action === 'dot-channel-file-preview') {
          const owner = loaded,
            epoch = state.drawerEpoch;
          saving = true;
          try {
            const preview = await mutation('weixin-files-preview', { ...selected, ...scope() });
            if (
              !current(key) ||
              loaded !== owner ||
              epoch !== state.drawerEpoch ||
              !root.isConnected
            )
              return local;
            loaded.wxFilePreview = { selected, preview };
            root.querySelector('.wx-file-preview').innerHTML =
              '<section class="cc-channel-binding"><strong>核对后才发送</strong><p>原微信收件人：' +
              E(preview.recipient.peer_id) +
              '；绑定版本 ' +
              E(preview.binding_version) +
              '</p><p>项目：' +
              E(preview.project_id) +
              '；原话题：' +
              E(preview.conversation_id) +
              '</p>' +
              preview.files
                .map(
                  (file) =>
                    '<p>' +
                    E(file.name) +
                    ' · ' +
                    E(file.bytes) +
                    ' 字节<small>SHA-256：' +
                    E(file.sha256) +
                    '</small><small>保留至：' +
                    stamp(file.expires_at) +
                    '</small></p>',
                )
                .join('') +
              checkbox('confirm_files', '我确认仅向以上原微信联系人分享以上完整文件清单', true) +
              button('dot-channel-files', '确认保存文件外发清单') +
              '</section>';
          } finally {
            saving = false;
          }
          return local;
        }
        if (
          !loaded.wxFilePreview ||
          JSON.stringify(loaded.wxFilePreview.selected) !== JSON.stringify(selected)
        )
          throw new Error('文件或原回复已变化，请重新预览文件与收件人。');
        operation = 'weixin-files-reviewed';
        args = {
          ...selected,
          preview_digest: loaded.wxFilePreview.preview.preview_digest,
          confirm_file_egress: checked('confirm_files'),
        };
      } else if (['dot-channel-confirm-received', 'dot-channel-cancel-unsent'].includes(action)) {
        const row = loaded.weixin.deliveries.find((d) => d.id === element.dataset.id);
        if (!row) throw new Error('原投递已不在当前列表。');
        operation = 'weixin-delivery-control';
        args = {
          delivery_id: row.id,
          expected_version: row.version,
          action: action === 'dot-channel-confirm-received' ? 'confirm_received' : 'cancel_unsent',
          confirmation_note: value('confirmation_note'),
        };
      } else return local;
      saving = true;
      try {
        await mutation(operation, { ...args, ...scope() });
      } finally {
        saving = false;
        if (root?.querySelector('[name="token"]')) root.querySelector('[name="token"]').value = '';
      }
      if (!current(key) || loaded?.epoch !== state.drawerEpoch) return local;
      if (root?.querySelector('[name="token"]')) root.querySelector('[name="token"]').value = '';
      await open(document.querySelector('[data-cc-action="dot-channel-open"]'));
      return {
        local: true,
        message:
          action === 'dot-channel-enrollment-approve'
            ? '授权已保存。请仅在本人原私聊发送授权码，验证后自动开通；回执以实际发送状态为准。'
            : '原配置或投递清单已保存；状态以新读取的证据为准。',
      };
    }
    function clear() {
      pairingStop();
      qrDiscard(qr);
      controller?.abort();
      for (const request of enrollmentRequests) request.abort();
      enrollmentRequests.clear();
      controller = null;
      loaded = null;
      channelView = 'overview';
      drafts.clear();
      for (const input of document.querySelectorAll('.cc-channel-pane input[type="password"]'))
        input.value = '';
    }
    return { card, act, clear };
  },
};
