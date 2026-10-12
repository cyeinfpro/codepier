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
        return `<div class="cc-channel-file" data-attachment-id="${E(item.id)}"><div><strong>${E(item.name || '附件')}</strong><small>${E(item.kind || 'file')} · ${E(label(item.state))}${Number.isFinite(item.size) ? ' · ' + E(String(item.size)) + ' 字节' : ''}</small>${item.reason_code ? `<small>${E(item.reason_code)}</small>` : ''}</div>${item.state === 'ready' && valid ? `<a class="btn" href="${E(href)}" target="_blank" rel="noopener noreferrer" download>读取原附件</a>` : '<span>原消息与顺序已保留</span>'}</div>`;
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
        ? `<section class="cc-card cc-channel-settings"><h3>通道与执行边界</h3><p>在此管理当前房间的微信与 Telegram 连接、文件外发和凭据范围。打开此页只查询状态；保存配置需逐项确认。</p>${button('dot-channel-open', '管理通道与范围')}</section>`
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
        '<p role="status" aria-live="polite">' + E(captions[phase] || captions.attention) + '</p>';
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
          checkbox(
            'qr_scanner',
            '我已核对以上完整账号编号，确认是本人，已在微信授权页确认相关条款，并同意加密保存凭据供本服务持续连接使用',
            true,
          ) +
          button(
            'dot-channel-qr-accept',
            q.acceptArgs ? '恢复原保存请求' : '确认本人并保存连接',
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
        if (!data.checked('qr_scanner')) return local;
        q.acceptArgs = {
          ...qrScope(q.key),
          enrollment_id: q.entry.enrollment_id,
          expected_version: q.entry.version,
          idempotency_key: q.acceptKey,
          label: data.value('label').trim(),
          confirm_scanner_id: q.entry.scanner_id,
          confirm_credential_storage: true,
          confirm_provider_terms: true,
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
        message: '微信登录凭据已保存。请继续选择当前话题的 dot 并完成联系人配对。',
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
        qr = {
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

    function telegramPanel(t, w, slots) {
      if (!t) return '';
      const names = {
        retention_gap_possible: '离线可能超过24小时，上游历史可能不完整',
        TELEGRAM_WEBHOOK_CONFLICT: '机器人已有Webhook，请人工核对，不会自动替换',
      };
      const bindings = (t.bindings || [])
        .map(
          (
            b,
          ) => `<section class="cc-channel-binding cc-tg-binding" data-binding-id="${E(b.id)}"><div class="cc-channel-heading"><strong>Telegram · ${listState(b.state)}</strong><span>完整同步</span></div><small>原绑定 ${E(b.id.slice(-8))} · 世代 ${E(b.generation)} · 到期 ${stamp(b.expires_at)}</small>
        ${b.user_id ? `<p>本人 user_id：<code>${E(b.user_id)}</code> · 私聊 chat_id：<code>${E(b.chat_id)}</code></p>` : ''}
        ${b.state === 'pairing' ? `<p>在当前机器人私聊发送以下一次性命令，再回来核对身份：</p><textarea readonly rows="2" aria-label="Telegram 配对命令">${E(b.pairing_command || '已过期，请撤销后重新配对')}</textarea>` : ''}
        ${b.state === 'awaiting_owner' ? `<p>实际 user_id：<code>${E(b.pending_user_id)}</code> · chat_id：<code>${E(b.pending_chat_id)}</code></p><div data-channel-form="tg-activate">${checkbox('same_owner', '我已核对数字编号，确认这是我本人的 Telegram 私聊', true)}${button('dot-channel-tg-activate', '确认本人并启用同步', b)}</div>` : ''}
        <small>${b.disable_notification ? 'Telegram 无声发送；仍可能显示通知。' : 'Telegram 按客户端通知设置提醒。'}微信请在客户端设置免打扰。</small><div class="cc-actions">${b.state === 'active' ? button('dot-channel-tg-pause', '暂停 Telegram', b) : b.state === 'paused' ? button('dot-channel-tg-resume', '恢复原绑定', b) : ''}${b.state !== 'revoked' ? button('dot-channel-tg-revoke', '撤销 Telegram', b) : ''}</div></section>`,
        )
        .join('');
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
        ${bindings || '<p>当前协作室尚未绑定 Telegram。</p>'}
        <details class="cc-tg-connect"><summary>连接 Telegram 机器人</summary><div data-channel-form="tg-account">${field('名称', '<input name="label" maxlength="80" required autocomplete="off">')}${field('机器人数字编号', '<input name="bot_id" inputmode="numeric" pattern="[1-9][0-9]*" required autocomplete="off">')}${field('Bot Token', '<input type="password" name="token" maxlength="256" required autocomplete="new-password">')}${checkbox('credentials', '我确认在本服务加密保存此凭据，并接受 Telegram 的相关条款', true)}${button('dot-channel-tg-import', '确认登记 Telegram 凭据', {}, t.enabled ? '' : 'disabled')}<small>仅使用官方API长轮询；已有Webhook会阻止连接，系统不会自动删除或覆盖。</small></div></details>
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

    function weixinBindings(w) {
      return (w.bindings || [])
        .map(
          (binding) =>
            `<section class="cc-channel-binding" data-binding-id="${E(binding.id)}"><h4>${binding.state === 'pairing' && binding.pairing_until <= Date.now() / 1000 ? '配对码已过期' : listState(binding.state)}</h4><p>原绑定 ${E(binding.id.slice(-8))} · 世代 ${E(binding.generation)} · 有效至 ${stamp(binding.expires_at)}</p>${binding.peer_id ? `<p>原联系人：<code>${E(binding.peer_id)}</code></p>` : ''}${binding.state === 'pairing' ? `<p>在要绑定的微信私聊发送以下配对码。此处会自动查询配对状态，仍需本人核对后激活。</p><textarea readonly rows="2" aria-label="微信配对码">${E(binding.pairing_until <= Date.now() / 1000 ? '配对码已过期；请撤销原绑定后重新生成' : binding.pairing_challenge || '原配对码不可用；请重新核对状态')}</textarea><small>配对期限：${stamp(binding.pairing_until)}。配对不会授予代码执行权限。</small>` : ''}${binding.state === 'awaiting_owner' ? `<p>实际返回的联系人编号：<code>${E(binding.pending_peer_id)}</code></p><div data-channel-form="activate">${checkbox('confirm_peer', '我已核对该联系人和当前房间', true)}${button('dot-channel-activate', '确认激活此联系人', binding)}</div>` : ''}<div class="cc-actions">${binding.state === 'active' ? button('dot-channel-pause', '暂停此通道', binding) : binding.state === 'paused' ? button('dot-channel-resume', '恢复原绑定', binding) : ''}${binding.state !== 'revoked' ? button('dot-channel-revoke', '撤销原绑定', binding) : ''}</div></section>`,
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
        WEIXIN_PAIRING_EXPIRED: '配对码已过期，已停止此次配对收件；请撤销原绑定后重新生成',
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
        if (code) code.value = '配对码已过期；请撤销原绑定后重新生成';
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
        for (const binding of fresh.bindings || []) {
          const card = [...region.querySelectorAll('[data-binding-id]')].find(
            (node) => node.dataset.bindingId === binding.id,
          );
          const before = previous.find((b) => b.id === binding.id);
          const changed = !before || JSON.stringify(before) !== JSON.stringify(binding);
          if (card && binding.state === 'pairing' && binding.pairing_until <= Date.now() / 1000) {
            card.querySelector('h4').textContent = '配对码已过期';
            const code = card.querySelector('textarea[readonly]');
            if (code) code.value = '配对码已过期；请撤销原绑定后重新生成';
          }
          if (
            card &&
            changed &&
            (card.contains(document.activeElement) || card.querySelector('input:checked'))
          ) {
            displayed.push(before);
            continue;
          }
          displayed.push(binding);
          if (!card)
            region.insertAdjacentHTML('beforeend', weixinBindings({ bindings: [binding] }));
          else if (changed) card.outerHTML = weixinBindings({ bindings: [binding] });
        }
        for (const card of region.querySelectorAll('[data-binding-id]'))
          if (!displayed.some((b) => b.id === card.dataset.bindingId)) card.remove();
        p.owner.weixin.bindings = displayed;
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
            '配对码已到期，最新服务端状态暂不可用。请重新核对状态后撤销原绑定或继续核对联系人。';
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

    function render(data) {
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
            `<div class="cc-channel-binding"><strong>连接 ${E(item.grant_id.slice(-8))} · ${listState(item.state)}</strong><p>版本 ${E(item.version)} · 到期 ${stamp(item.expires_at)} · ${item.manifest?.execution_mode === 'file_tools_only' ? '仅文件工具' : '账户权限执行（不是 OS 沙箱）'}</p><p>${(item.manifest?.project_rules || []).map((p) => E(p.project_id) + '：' + E(p.capabilities.join(' / '))).join('<br>')}</p>${item.state === 'active' ? button('dot-channel-scope-revoke', '停用此房间连接', { id: item.grant_id }) : ''}</div>`,
        )
        .join('');
      const deliveries = (w.deliveries || [])
        .map(
          (d) =>
            `<details class="cc-channel-delivery" data-delivery-id="${E(d.id)}"><summary>${listState(d.state)} · ${E(d.id.slice(-8))}</summary>${(d.parts || []).map((p) => `<p>${E(String(p.ordinal + 1))}. ${p.kind === 'text' ? '正文' : '附件'}：${listState(p.state)} ${E(p.reason_code || '')}</p>`).join('')}<div data-channel-form="delivery">${(d.parts || []).some((p) => p.state === 'unknown') ? field('人工核对依据（不是平台回执）', '<input name="confirmation_note" maxlength="1000" required autocomplete="off">') + button('dot-channel-confirm-received', '登记人工已收到', d) : ''}${button('dot-channel-cancel-unsent', '取消尚未发送部分', d)}</div></details>`,
        )
        .join('');
      return `<div class="cc-channel-pane"><p class="cc-hint">当前项目：${E(state.project)} · ${E(state.environment)}。刷新仅查询证据，不重发消息、不领取任务。</p>${button('dot-channel-open', '重新核对状态')}
      <section><h3>微信私聊通道</h3><p>${w.enabled ? '功能已开启；真实收发仍以每条回执为准。' : '微信通道已关闭。可在此开启，再扫码连接本人微信。'}</p><p>仅支持机器人私聊，不冒充原生群聊。外部文字不能自动变成房主的执行授权。</p>${channelSetting('weixin', w)}<div class="cc-wx-health">${weixinHealth(w)}</div><p class="cc-wx-refresh-notice" role="status" aria-live="polite"></p>${qrPanel(w)}<div class="cc-wx-bindings">${bindings || '<p>当前房间尚未绑定微信。</p>'}</div>
      <details><summary>高级：登记已有授权机器人</summary><div data-channel-form="account" class="cc-form">${field('名称', '<input name="label" maxlength="80" required autocomplete="off">')}${field('机器人编号', '<input name="bot_id" maxlength="256" required autocomplete="off">')}${field('已有授权令牌', '<input type="password" name="token" maxlength="16384" required autocomplete="new-password">')}${checkbox('confirm_credentials', '我确认保存此已授权凭据，并已核对服务条款与使用资格', true)}${button('dot-channel-import', '确认登记凭据', {}, w.enabled ? '' : 'disabled')}<small>凭据加密保存；不创建 MCP 凭据，也不会显示到消息中。</small></div></details>
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
      )}${checkbox('allow_replies', '允许将该 dot 对原微信消息的最终回复发送给原联系人')}${checkbox('allow_media', '允许文件外发方向；每份文件仍需单独确认')}${checkbox('confirm_ingress', '我确认将此微信私聊接入当前话题，并会核对实际联系人', true)}${button('dot-channel-bind', '生成配对码', {}, w.enabled ? '' : 'disabled')}</div></details>
      <details><summary>将已有产物附加到原回复</summary><div data-channel-form="files" class="cc-form">${field(
        '最近的原 dot 最终回复',
        select(
          'message_id',
          (w.reply_candidates || []).map((m) => [m.id, m.body_text || m.id]),
        ),
      )}<fieldset><legend>最近的已生成产物（最多16件）</legend>${(w.artifact_candidates || []).map((a) => `<label class="cc-channel-check"><input type="checkbox" name="artifact_ids" value="${E(a.artifact_id)}">${E(a.name)} · ${E(a.bytes)} 字节</label>`).join('') || '<p>当前项目没有可选产物。</p>'}</fieldset>${w.artifact_candidates_more ? '<p>此处仅列最近50件；更早产物请在产物管理中核对编号。</p>' : ''}${checkbox('confirm_files', '我确认仅向这条原微信消息的联系人分享所选文件', true)}${button('dot-channel-files', '确认保存文件外发清单', {}, w.enabled ? '' : 'disabled')}<small>只传输已有产物，不重跑原生成任务；正文不会重复发送。</small></div></details>
      <h4>最近投递分项</h4>${deliveries || '<p>暂无外发记录。</p>'}</section>
      ${telegramPanel(data.telegram, w, slots)}<section><h3>房间凭据范围</h3><p>范围绑定服务器上的原 grant，省略 room_id 也不能绕过。启用或修改会影响所有使用同一凭据的会话；工作目录不是操作系统沙箱。</p>${policies || '<p>尚未启用普通工具的房间限制。</p>'}<details><summary>明确设置连接范围</summary><div data-channel-form="scope" class="cc-form">${field(
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
      )}<fieldset><legend>当前环境的项目与逐项目能力</legend>${projects.map((p) => `<div class="cc-channel-project" data-project-id="${E(p.project_id)}"><label><input type="checkbox" name="project_selected" ${p.project_id === (state.snapshot.room.project_id || state.project) ? 'checked disabled' : ''}>${E(p.project_label || p.project_id)}</label>${checkbox('write', '允许项目文件写入')}${checkbox('execute', '允许此项目的 Agent 执行')}</div>`).join('')}</fieldset>${field('有效小时数', '<input type="number" name="hours" value="24" min="1" max="720" step="1" required>')}${checkbox('unsandboxed', '如选择执行，我明确接受 Agent 账户权限；这不是文件系统或网络沙箱')}${checkbox('confirm_scope', '我确认以本次选择替换此凭据的房间范围，且影响它的所有会话', true)}${button('dot-channel-scope-save', '确认设置范围')}<p>严格 OS 隔离尚未验证时不会放行执行。未选择执行时只允许获批文件工具。</p></div></details></section></div>`;
    }
    async function open(element, selectedAccount = '') {
      pairingStop();
      qrDiscard(qr);
      if (!allowed()) throw new Error('当前身份不能管理房间连接。');
      controller?.abort();
      controller = new AbortController();
      const key = capture();
      showDrawer('通道与执行边界', '<p role="status">正在读取原连接与投递状态…</p>', element);
      const epoch = state.drawerEpoch;
      const query = new URLSearchParams(scope());
      let result;
      try {
        result = await Promise.all([
          api(
            '/api/collaboration/room-scopes?' +
              new URLSearchParams({ project: key.project, environment_id: key.environment_id }),
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
      showDrawer('通道与执行边界', render(loaded), element);
      loaded.epoch = state.drawerEpoch;
      pairingStart();
      if (selectedAccount) {
        const select = document.querySelector('.cc-wx-bind [name="account_id"]');
        if (select && [...select.options].some((option) => option.value === selectedAccount)) {
          select.value = selectedAccount;
          document.querySelector('.cc-wx-bind').open = true;
          const guide = document.querySelector('.cc-wx-binding-guide');
          guide.hidden = false;
          guide.textContent =
            '微信登录已保存。请选择当前话题的 dot，再生成配对码；核对实际联系人后才会启用通道。';
          select.focus();
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
      if (action === 'dot-channel-open') return open(element);
      if (saving) return local;
      if (action.startsWith('dot-channel-qr-')) return qrAct(action, element);
      const data = group(
        element,
        ![
          'dot-channel-cancel-unsent',
          'dot-channel-mirror-cancel',
          'dot-channel-tg-file-preview',
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
          confirm_provider_terms: checked('credentials'),
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
          saving = true;
          try {
            const preview = await mutation('telegram-files-preview', { ...selected, ...scope() });
            if (!current(key) || loaded.epoch !== state.drawerEpoch) return local;
            loaded.filePreview = { selected, preview };
            root.querySelector('.tg-file-preview').innerHTML =
              `<section class="cc-channel-binding"><strong>核对后才发送</strong><p>Telegram user_id：${E(preview.recipient.user_id)} · chat_id：${E(preview.recipient.chat_id)}</p><p>原回复：${E(preview.message_id.slice(-8))} · 版本 ${E(preview.message_version)}</p>${preview.files.map((f) => `<div>${E(f.name)} · ${E(f.bytes)} 字节<small>SHA-256：${E(f.sha256)}</small></div>`).join('')}<p>总计 ${E(preview.total_bytes)} 字节</p>${checkbox('file_confirm', '我确认仅向以上本人Telegram私聊发送以上完整文件清单', true)}${button('dot-channel-tg-files', '确认并排队发送这些文件')}</section>`;
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
          confirm_provider_terms: checked('confirm_credentials'),
        };
      } else if (action === 'dot-channel-bind') {
        operation = 'weixin-bind';
        args = {
          account_id: value('account_id'),
          dot_id: value('dot_id'),
          confirm_external_data_ingress: checked('confirm_ingress'),
          allow_replies: checked('allow_replies'),
          allow_media_outbound: checked('allow_media'),
        };
      } else if (
        [
          'dot-channel-activate',
          'dot-channel-pause',
          'dot-channel-resume',
          'dot-channel-revoke',
        ].includes(action)
      ) {
        const row = loaded.weixin.bindings.find((b) => b.id === element.dataset.id);
        if (!row) throw new Error('原绑定已不在当前列表。');
        if (
          action === 'dot-channel-revoke' &&
          !window.confirm('撤销原微信绑定并停止新转发？未知投递仍保留，不会重发到新账号。')
        )
          return local;
        operation = 'weixin-control';
        args = {
          binding_id: row.id,
          expected_version: row.version,
          action: action.slice('dot-channel-'.length),
          confirm_peer_id:
            action === 'dot-channel-activate' && checked('confirm_peer') ? row.pending_peer_id : '',
        };
      } else if (action === 'dot-channel-scope-save') {
        const grant = loaded.scopes.eligible_connections.find((g) => g.id === value('grant_id'));
        if (!grant) throw new Error('请选择当前已有连接。');
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
          expires_seconds: Number(value('hours')) * 3600,
          confirm_connection_wide_ceiling: checked('confirm_scope'),
        };
      } else if (action === 'dot-channel-scope-revoke') {
        const row = loaded.scopes.items.find((r) => r.grant_id === element.dataset.id);
        if (!row || !window.confirm('停用此房间连接？它将被拒绝访问，不会退回更宽的原授权。'))
          return local;
        operation = 'room-scope-control';
        args = { grant_id: row.grant_id, expected_version: row.version, action: 'revoke' };
      } else if (action === 'dot-channel-files') {
        const reply = loaded.weixin.reply_candidates.find((m) => m.id === value('message_id'));
        const ids = [...root.querySelectorAll('[name="artifact_ids"]:checked')].map(
          (input) => input.value,
        );
        if (!reply || !ids.length || ids.length > 16)
          throw new Error('请选择原回复与1至16件实际产物。');
        operation = 'weixin-files';
        args = {
          message_id: reply.id,
          expected_message_version: reply.version,
          artifact_ids: ids,
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
      return { local: true, message: '原配置或投递清单已保存；状态以新读取的证据为准。' };
    }
    function clear() {
      pairingStop();
      qrDiscard(qr);
      controller?.abort();
      controller = null;
      loaded = null;
      for (const input of document.querySelectorAll('.cc-channel-pane input[type="password"]'))
        input.value = '';
    }
    return { card, act, clear };
  },
};
