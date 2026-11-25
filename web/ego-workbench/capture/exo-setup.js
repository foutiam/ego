(() => {
  const session = $('calibSyncSession');
  const importButton = $('chooseIntakeVideo');
  const summary = $('exoSummary');
  const videoList = $('exoSelectedVideos');
  const progressBlock = $('exoProgressBlock');
  const progressText = $('exoProgressText');
  const progressBar = $('exoProgress');
  const actions = $('exoActions');
  const status = $('exoStatus');
  const retry = $('retryIntakeExo');
  const rescan = $('rescanIntakeExo');
  const profilesPanel = $('exoProfiles');
  const profileOptions = $('exoProfileOptions');
  const diagnosticsPanel = $('exoDiagnostics');
  const reportStatus = $('exoReportStatus');
  const cameraResults = $('exoCameraResults');
  const download = $('exoReport');
  const calibrationButton = $('exoOpenCalibration');
  const empty = {videos: [], profiles: [], job: null, report: null};
  const pending = new Set();
  let model = empty;
  let modelId = '';
  let request = 0;
  let timer;
  let message = '';
  let listKey = '';
  let previousReport;
  let previousReportState;
  const timelineObservers = [];

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  function setText(node, value) {
    if (node.textContent !== value) node.textContent = value;
  }

  const activeJob = job => job?.status === 'queued' || job?.status === 'running';
  const cameraName = role => String(role).toUpperCase().replace(/^EXO-?(\d+)$/, 'EXO $1');
  const clockVerified = camera => camera.clock_status
    ? ['offset_and_drift', 'offset_only'].includes(camera.clock_status)
    : Boolean(camera.clock);
  const boardVerified = camera => camera.validation?.status === 'passed';
  const boardStatus = camera => ({
    passed: '桌面已标定',
    board_not_detected: '未识别到标定板',
    board_insufficient: '标定板观测不足',
    intrinsics_needed: '需要相机内参',
    table_unverified: '桌面标定未通过核验',
    scan_failed: '扫描未完成',
    time_only: '仅对时'
  })[camera.validation?.status] || '桌面未标定';

  function sourceStatus(camera) {
    const clock = clockVerified(camera)
      ? (camera.clock_status === 'offset_only' ? '已对时（仅偏移）' : '已对时')
      : '未对时';
    return [clock, boardStatus(camera)].join(' · ');
  }

  function setTimer() {
    clearTimeout(timer);
    if (activeJob(model.job) && modelId === session.value && !$('intake').hidden) {
      timer = setTimeout(refresh, 900);
    }
  }

  function listRows(videos, profiles, results, locked, working) {
    const byRole = new Map(results.map(camera => [camera.role, camera]));
    const key = JSON.stringify([session.value, videos, profiles, locked, working,
      results.map(camera => [camera.role, sourceStatus(camera)])]);
    if (key === listKey) return;
    listKey = key;
    videoList.replaceChildren();
    profileOptions.replaceChildren();
    for (const video of videos) {
      const name = video.label || cameraName(video.role);
      const row = element('li', 'exo-selected-video');
      const info = element('div', 'exo-video-info');
      info.append(element('strong', '', name));
      const camera = byRole.get(video.role);
      if (camera && !working) info.append(element('small', 'muted', sourceStatus(camera)));
      const remove = element('button', 'small quiet', '移除');
      remove.type = 'button';
      remove.disabled = locked;
      remove.setAttribute('aria-label', '移除 ' + name);
      remove.onclick = async () => {
        await mutate('remove', {role: video.role});
        if (!importButton.disabled && !remove.isConnected) importButton.focus();
      };
      row.append(info, remove);
      videoList.append(row);

      if (profiles.length) {
        const field = element('label', 'exo-profile-option');
        field.append(element('span', '', name));
        const select = document.createElement('select');
        select.append(new Option('自动估计', ''));
        for (const profile of profiles) {
          const id = typeof profile === 'string' ? profile : profile.id;
          const label = typeof profile === 'string' ? profile : profile.label || id;
          if (id) select.append(new Option(label, id));
        }
        if (video.profile && !Array.from(select.options).some(option => option.value === video.profile)) {
          select.append(new Option('原内参档案不可用', video.profile));
        }
        select.value = video.profile || '';
        select.disabled = locked;
        select.setAttribute('aria-label', name + ' 的内参');
        select.onchange = () => mutate('profile', {role: video.role, profile: select.value});
        field.append(select);
        profileOptions.append(field);
      }
    }
    videoList.hidden = !videos.length;
    profilesPanel.hidden = !profiles.length || !videos.length;
    if (profilesPanel.hidden) profilesPanel.open = false;
  }

  function drawTimeline(details, name) {
    const count = Number(details.source_frame_count);
    if (!Number.isInteger(count) || count < 1) return null;
    const lanes = [
      {label: '二维码', color: '#79dbca', marks: details.qr || [], checked: details.scanned_qr_frames},
      {label: '标定板', color: '#f1bf73', marks: details.board || [], checked: details.scanned_board_frames}
    ];
    for (const lane of lanes) {
      lane.points = new Map(lane.marks.filter(mark => Number.isInteger(mark.frame) &&
        mark.frame >= 0 && mark.frame < count).map(mark => [mark.frame, mark]));
    }

    const figure = element('figure', 'exo-timeline');
    const key = element('figcaption', 'exo-timeline-legend');
    for (const lane of lanes) {
      const chip = element('span', 'exo-timeline-key');
      const swatch = element('i');
      swatch.style.backgroundColor = lane.color;
      const scanned = Number.isInteger(lane.checked) ? ' / 已扫 ' + lane.checked + ' 帧' : '';
      chip.append(swatch, document.createTextNode(lane.label + ' ' + lane.points.size + ' 帧' + scanned));
      key.append(chip);
    }
    const canvas = element('canvas', 'exo-timeline-plot');
    canvas.setAttribute('aria-hidden', 'true');
    const controls = element('div', 'exo-timeline-controls');
    const input = document.createElement('input');
    input.type = 'range';
    input.min = 0;
    input.max = count - 1;
    input.step = 1;
    input.setAttribute('aria-label', name + ' 原片帧');
    const firstMark = lanes.flatMap(lane => [...lane.points.keys()])
      .reduce((first, frame) => Math.min(first, frame), count);
    input.value = firstMark < count ? firstMark : 0;
    input.disabled = count < 2;
    const output = element('output', 'muted');
    controls.append(input, output);
    figure.append(key, canvas, controls);

    const inset = 23;
    const lineY = [18, 42];
    function paint() {
      const width = canvas.clientWidth;
      const height = canvas.clientHeight;
      if (width <= inset * 2 || !height) return;
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      const ctx = canvas.getContext('2d');
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      const span = width - inset * 2;
      const position = frame => inset + frame / Math.max(1, count - 1) * span;
      const foreground = getComputedStyle(figure).getPropertyValue('--dim').trim() || '#a4b4c2';
      for (const [index, lane] of lanes.entries()) {
        const y = lineY[index];
        ctx.fillStyle = 'rgba(126,161,188,0.13)';
        ctx.fillRect(inset, y - 5, span, 10);
        ctx.fillStyle = lane.color;
        const visibleColumns = new Set([...lane.points.keys()].map(frame => Math.round(position(frame))));
        for (const x of visibleColumns) ctx.fillRect(x - 1, y - 5, 3, 10);
      }
      const selected = position(input.valueAsNumber);
      ctx.strokeStyle = foreground;
      ctx.beginPath();
      ctx.moveTo(selected, 8);
      ctx.lineTo(selected, 54);
      ctx.stroke();
      ctx.font = '11px system-ui';
      ctx.fillStyle = foreground;
      const ticks = [...new Set([0, Math.round((count - 1) / 2), count - 1])];
      for (const frame of ticks) {
        ctx.textAlign = frame === 0 ? 'left' : frame === count - 1 ? 'right' : 'center';
        ctx.fillText(String(frame + 1), position(frame), 71);
      }
    }

    function update() {
      const frame = input.valueAsNumber;
      const observed = [];
      for (const lane of lanes) {
        const mark = lane.points.get(frame);
        if (!mark) continue;
        const corners = Number.isInteger(mark.corners) ? '（' + mark.corners + ' 角点）' : '';
        observed.push(lane.label + corners);
      }
      const time = lanes.map(lane => lane.points.get(frame)?.source_pts_s).find(Number.isFinite);
      setText(output, ['第 ' + (frame + 1) + ' 帧', Number.isFinite(time) ? time.toFixed(3) + ' s PTS' : '',
        observed.length ? observed.join('、') : '该帧无识别记录'].filter(Boolean).join(' · '));
      input.setAttribute('aria-valuetext', output.textContent);
      paint();
    }

    input.addEventListener('input', update);
    canvas.addEventListener('pointerdown', event => {
      const bounds = canvas.getBoundingClientRect();
      const span = bounds.width - inset * 2;
      if (span < 1) return;
      const fraction = Math.max(0, Math.min(1, (event.clientX - bounds.left - inset) / span));
      const frame = Math.round(fraction * (count - 1));
      const lane = lanes.find((_, index) => Math.abs(event.clientY - bounds.top - lineY[index]) <= 9);
      let nearest = frame;
      let distance = 9 / span * Math.max(1, count - 1);
      for (const mark of lane?.points.keys() || []) {
        const delta = Math.abs(mark - frame);
        if (delta > distance) continue;
        nearest = mark;
        distance = delta;
      }
      input.value = nearest;
      update();
    });
    const observer = new ResizeObserver(paint);
    observer.observe(canvas);
    timelineObservers.push(observer);
    update();
    return figure;
  }

  function reportCards(report, state, working, videos) {
    if (!diagnosticsPanel.open) return;
    const available = working ? null : report;
    const key = [session.value, working, state, videos.length].join('|');
    if (available === previousReport && key === previousReportState) return;
    previousReport = available;
    previousReportState = key;
    for (const observer of timelineObservers.splice(0)) observer.disconnect();
    cameraResults.replaceChildren();
    calibrationButton.hidden = true;
    download.hidden = true;
    rescan.hidden = true;

    const cameras = available?.cameras || [];
    if (!cameras.length) {
      setText(reportStatus, working ? '扫描完成后显示识别帧。'
        : state === 'failed' ? '本次整理未生成报告。' : '暂无识别记录。');
      return;
    }
    const legacy = videos.length && cameras.some(camera => !camera.diagnostics);
    rescan.hidden = !legacy;
    setText(reportStatus, legacy ? '这份报告未保存逐帧识别记录。' : '');
    const id = session.value;
    download.href = capturePath('/api/exo/result?' + new URLSearchParams({id}));
    download.hidden = false;
    calibrationButton.hidden = !cameras.some(camera => camera.validation?.status === 'intrinsics_needed');
    for (const camera of cameras) {
      const card = element('article', 'exo-scan-card');
      const head = element('header', 'exo-scan-heading');
      const name = cameraName(camera.role);
      head.append(element('h4', '', name + (camera.original_name ? ' · ' + camera.original_name : '')),
        element('span', 'muted', sourceStatus(camera)));
      card.append(head);
      const residual = camera.clock?.median_abs_residual_ms;
      const rms = camera.validation?.holdout_rms_px;
      const metrics = [];
      if (Number.isFinite(residual)) metrics.push('对时残差 ' + residual.toFixed(1) + ' ms');
      else if (camera.clock_reason) metrics.push(camera.clock_reason);
      if (Number.isFinite(rms)) metrics.push('桌面留出误差 ' + rms.toFixed(2) + ' px');
      if (!boardVerified(camera) && camera.validation?.reason) metrics.push(camera.validation.reason);
      if (metrics.length) card.append(element('p', 'exo-scan-metrics muted', metrics.join(' · ')));
      if (camera.diagnostics) {
        const timeline = drawTimeline(camera.diagnostics, name);
        if (timeline) card.append(timeline);
      }
      cameraResults.append(card);
    }
  }

  function render() {
    const id = session.value;
    const {videos, profiles, job, report} = modelId === id ? model : empty;
    const working = activeJob(job);
    const waiting = pending.has(id);
    const locked = working || waiting;
    importButton.disabled = !id || locked;
    const results = report?.cameras || [];
    listRows(videos, profiles, results, locked, working);

    let title = '';
    if (videos.length) {
      const synced = results.filter(clockVerified).length;
      const calibrated = results.filter(boardVerified).length;
      title = !working && results.length
        ? videos.length + ' 路 · ' + synced + ' 路已对时 · ' + calibrated + ' 路桌面已标定'
        : videos.length + ' 路';
    }
    setText(summary, title);
    summary.hidden = !title;
    progressBlock.hidden = !working;
    if (working) {
      const percent = Number.isFinite(job.progress) ? Math.min(100, Math.max(0, Math.round(job.progress))) : null;
      if (percent === null) progressBar.removeAttribute('value');
      else if (!progressBar.hasAttribute('value') || progressBar.value !== percent) progressBar.value = percent;
      setText(progressText, [job.phase || (job.status === 'queued' ? '等待整理 EXO…' : '正在整理 EXO…'),
        percent === null ? '' : percent + '%'].filter(Boolean).join(' · '));
    }
    const failed = job?.status === 'failed' || job?.status === 'interrupted';
    const retryable = failed && videos.length > 0 && !locked;
    const stateMessage = waiting ? '正在读取选择…' : (modelId === id ? message : '') ||
      (failed ? job.phase || 'EXO 整理未完成。' : '');
    setText(status, stateMessage);
    retry.hidden = !retryable;
    actions.hidden = !stateMessage && !retryable;
    diagnosticsPanel.hidden = !videos.length && !results.length;
    if (diagnosticsPanel.hidden) diagnosticsPanel.open = false;
    reportCards(report, job?.status, working, videos);
  }

  function accept(id, next, changed = false) {
    if (id !== session.value) return;
    const previous = modelId === id ? model : empty;
    const terminal = next.job?.status && !activeJob(next.job) &&
      (activeJob(previous.job) || previous.job?.status !== next.job.status);
    modelId = id;
    model = Object.assign({}, empty, next);
    message = '';
    render();
    setTimer();
    if (changed || terminal) {
      refreshIntakeStatus();
      if (terminal && model.job?.status === 'complete') notifyEgoCaptureUpdated();
    } else if (model.job?.status !== previous.job?.status) updateIntakeProceed();
  }

  async function refresh() {
    clearTimeout(timer);
    const id = session.value;
    if ($('intake').hidden || !id || pending.has(id)) {
      render();
      return;
    }
    if (modelId !== id) {
      modelId = id;
      model = empty;
      message = '';
      render();
    }
    const version = ++request;
    try {
      const next = await api('/api/intake-exo?' + new URLSearchParams({id}));
      if (version === request) accept(id, next);
    } catch (error) {
      if (version === request && id === session.value) {
        message = error.message;
        render();
        setTimer();
      }
    }
  }

  async function mutate(action, detail = {}) {
    const id = session.value;
    if (!id || pending.has(id) || (modelId === id && activeJob(model.job))) return;
    ++request;
    clearTimeout(timer);
    pending.add(id);
    message = '';
    render();
    updateIntakeProceed();
    try {
      const next = await post('/api/intake-exo/' + action, {session_id: id, ...detail});
      pending.delete(id);
      accept(id, next, true);
    } catch (error) {
      pending.delete(id);
      if (id === session.value) {
        message = error.message;
        render();
        updateIntakeProceed();
        setTimer();
      }
    }
  }

  importButton.onclick = () => mutate('import');
  retry.onclick = rescan.onclick = () => mutate('retry');
  calibrationButton.onclick = async () => {
    await showCaptureTab('calib');
    document.querySelector('[data-calib-view="calibDevices"]')?.click();
    const panel = $('exoStationCalibration');
    if (panel) {
      panel.open = true;
      panel.scrollIntoView({block: 'nearest'});
      panel.querySelector('summary')?.focus({preventScroll: true});
    }
  };
  diagnosticsPanel.addEventListener('toggle', () => {
    if (diagnosticsPanel.open) render();
    else {
      for (const observer of timelineObservers.splice(0)) observer.disconnect();
      previousReportState = undefined;
    }
  });

  window.refreshIntakeExo = refresh;
  window.refreshExoReadiness = render;
  window.intakeExoDirty = () => pending.has(session.value) ||
    (modelId === session.value && activeJob(model.job));
  window.renderExoJob = job => {
    if (job.session_id === session.value && !$('intake').hidden &&
        job.status !== model.job?.status) refresh();
  };
})();
