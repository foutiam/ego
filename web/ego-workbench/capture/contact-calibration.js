(() => {
  const parts = [...$('contactReviewParts').querySelectorAll('input')];
  const currentPart = () => parts.find(input => input.checked).value;
  const cameraSelect = $('contactReviewCamera');
  const frameInput = $('contactFrame');
  const touchInput = $('contactTouch');
  const status = $('contactReviewStatus');
  const touchGraph = $('contactTactileGraph');
  const chartInset = 8;
  const cameraNames = {cam0:'头戴 CAM 0', cam1:'头戴 CAM 1', exo1:'EXO 1', exo2:'EXO 2', exo3:'EXO 3'};
  const phaseGuide = {
    before: ['开始任务', '戴手套的手短促、有力地按一下相机能看清的物体，可由另一只手拿住。松开后点「开始任务」，也可直接开始。'],
    task: ['结束任务', '正在录制正式任务。做完后点「结束任务」。'],
    after: ['', '再按一下相机能看清的物体，然后停止并保存。也可直接保存。'],
  };
  const boundary = $('contactTaskBoundary');
  let boundaryBusy = false;
  let generation = 0;
  let data = null;
  let identity = '';
  let enabledForSession = false;
  let saving = false;
  let touchMarked = false;

  window.renderContactPhase = () => {
    const supported = Boolean(state?.contact_calibration_supported);
    const option = $('contactCalibrationOption');
    option.hidden = !supported || Boolean(state?.recording);
    $('contactCalibrationEnabled').disabled = recordStartPending;
    $('calibContactTab').hidden = !supported;
    const guide = phaseGuide[state?.recording?.contact_phase];
    $('liveContactCalibration').hidden = !guide;
    if (!guide) return;
    const prompt = $('liveContactPrompt');
    if (prompt.textContent !== guide[1]) prompt.textContent = guide[1];
    if (boundary.textContent !== guide[0]) boundary.textContent = guide[0];
    boundary.hidden = !guide[0];
    boundary.disabled = boundaryBusy;
  };

  boundary.onclick = async () => {
    const rec = state?.recording;
    const actions = {before:'start', task:'end'};
    const action = actions[rec?.contact_phase];
    if (!action || boundaryBusy) return;
    boundaryBusy = true;
    window.renderContactPhase();
    try {
      const result = await post('/api/record/task-boundary', {session_id: rec.session_id, action});
      if (state?.recording?.session_id === rec.session_id) state.recording.contact_phase = result.phase;
    } catch (error) {
      toast(error.message);
    } finally {
      boundaryBusy = false;
      window.renderContactPhase();
    }
  };

  function showStatus(message = '') {
    status.textContent = message;
    status.hidden = !message;
  }

  function showResults(checks = {}) {
    for (const input of parts) {
      const saved = checks[input.value];
      const badge = input.nextElementSibling.querySelector('small');
      const status = saved?.status;
      badge.textContent = {pair_confirmed:'✓', visual_confirmed:'待触觉', skipped:'已跳过'}[status] || '';
      badge.hidden = !status;
      if (status === 'pair_confirmed') badge.setAttribute('aria-label', '已确认对帧');
      else badge.removeAttribute('aria-label');
    }
  }

  function savedSelection(before, after, point) {
    const result = data?.checks?.[currentPart()];
    if (!result || result.cam !== data.cam || result.camera_clock !== data.camera_clock ||
        result.before?.frame_id !== before?.frame_id || result.after?.frame_id !== after?.frame_id ||
        result.before?.time_s !== before?.time_s || result.after?.time_s !== after?.time_s) return null;
    if (result.status === 'visual_confirmed' && !touchMarked) return result;
    return result.status === 'pair_confirmed' && touchMarked && result.touch.sample === point?.sample ? result : null;
  }

  function showSelectionResult(saved, shift) {
    const previous = data?.checks?.[currentPart()];
    const summary = $('contactReviewResult');
    const clock = $('contactClockBasis');
    const offset = $('contactPreviewShift');
    const model = data?.tactile_alignment;
    const timing = $('contactTimingState');
    const phaseLabels = {before:'任务前', after:'任务后'};
    const timingLabels = {
      applied: '正式片段已使用视触校准',
      awaiting_pairs: model?.missing?.map(phase => phaseLabels[phase]).join('和') + '待对帧',
      incompatible_clocks: '相机时间基准不一致 · 请重新核对两段',
      weak_touch_anchor: phaseLabels[model?.phase] + '触觉起点处无响应 · 请重新选点',
      invalid_anchors: '锚点超出校准区间或顺序无效',
      no_task: '尚未划出正式任务'
    };
    timing.textContent = timingLabels[model?.status] || '';
    timing.hidden = !timing.textContent;
    $('contactPreviewKey').hidden = shift === null;
    offset.hidden = shift === null;
    summary.hidden = true;
    clock.hidden = !data?.frames?.length;
    clock.textContent = data?.camera_clock === 'host_receipt' ? '电脑接收时间' : '二维码对时';
    if (saved?.status === 'pair_confirmed') {
      const [low, high] = saved.delay_ms;
      summary.textContent = high < 0 ? '已保存 · 触觉早 ' + (-high).toFixed(1) + '–' + (-low).toFixed(1) + ' ms'
        : low > 0 ? '已保存 · 触觉晚 ' + low.toFixed(1) + '–' + high.toFixed(1) + ' ms'
          : '已保存 · 触觉 − 视觉 ' + low.toFixed(1) + ' 至 ' + high.toFixed(1) + ' ms';
      const ms = Math.abs(shift * 1000).toFixed(1);
      offset.textContent = Number(ms) === 0 ? '预览无需移动 · 仅本校准段'
        : '触觉' + (shift > 0 ? '后移 ' : '前移 ') + ms + ' ms · 仅预览';
      summary.hidden = false;
    } else if (saved?.status === 'visual_confirmed') {
      summary.textContent = '视觉帧已保存 · 可补标触觉起点';
      summary.hidden = false;
    } else if (previous && previous.status !== 'skipped') {
      summary.textContent = previous.cam === data.cam ? '当前选择未保存' : '已有其他视角的标记';
      summary.hidden = false;
    }
  }

  function touchDomain(touch, saved = null) {
    const rawStart = touch[0].time_s;
    const rawEnd = touch[touch.length - 1].time_s;
    const start = Math.min(rawStart, saved?.before.time_s ?? rawStart);
    const end = Math.max(rawEnd, saved?.after.time_s ?? rawEnd);
    return [start, Math.max(.001, end - start)];
  }

  function drawTouch() {
    const touch = data?.touch || [];
    const point = touch[Number(touchInput.value)];
    const index = Number(frameInput.value);
    const before = data?.frames?.[index - 1], after = data?.frames?.[index];
    const saved = savedSelection(before, after, point);
    const shift = saved?.status === 'pair_confirmed'
      ? (saved.before.time_s + saved.after.time_s) / 2 - saved.touch.time_s : null;
    const confirm = $('contactConfirm');
    confirm.textContent = saved?.status === 'pair_confirmed' ? '对帧已保存'
      : saved?.status === 'visual_confirmed' ? '视觉帧已保存'
        : point && touchMarked ? '确认对帧' : '确认视觉帧';
    confirm.disabled = !before || !after || saving || Boolean(saved);
    $('contactClearTouch').hidden = !touchMarked;
    showSelectionResult(saved, shift);
    if (!point) return;
    $('contactTouchTime').value = touchMarked ? point.time_s.toFixed(3) + ' s' : '点击曲线标记';

    const {width, height} = touchGraph.getBoundingClientRect();
    if (!width || !height) return;
    const pixels = window.devicePixelRatio || 1;
    const backingWidth = Math.round(width * pixels), backingHeight = Math.round(height * pixels);
    if (touchGraph.width !== backingWidth) touchGraph.width = backingWidth;
    if (touchGraph.height !== backingHeight) touchGraph.height = backingHeight;
    const context = touchGraph.getContext('2d');
    context.setTransform(pixels, 0, 0, pixels, 0, 0);
    context.clearRect(0, 0, width, height);
    const [earliest, span] = touchDomain(touch, shift === null ? null : saved);
    const scale = touch.reduce((peak, row) => Math.max(peak, row.response), 1);
    const x = time => chartInset + (width - 2 * chartInset) * (time - earliest) / span;
    const y = response => height - 6 - (height - 12) * response / scale;

    if (shift !== null) {
      const start = x(saved.before.time_s), end = x(saved.after.time_s);
      context.fillStyle = '#89aacf40';
      context.fillRect(start, 0, Math.max(2, end - start), height);
    }
    function wave(delta, color, dashed = false) {
      context.strokeStyle = color;
      context.setLineDash(dashed ? [4, 3] : []);
      context.lineWidth = 1.6;
      context.beginPath();
      touch.forEach((row, i) => context[i ? 'lineTo' : 'moveTo'](x(row.time_s + delta), y(row.response)));
      context.stroke();
      context.setLineDash([]);
    }
    function mark(time, color, dashed = false) {
      if (time < earliest || time > earliest + span) return;
      context.strokeStyle = color;
      context.setLineDash(dashed ? [4, 3] : []);
      context.beginPath();
      context.moveTo(x(time), 2);
      context.lineTo(x(time), height - 2);
      context.stroke();
      context.setLineDash([]);
    }
    wave(0, shift === null ? '#6adcca' : '#ffcc76', shift !== null);
    if (shift === null && after) mark(after.time_s, '#8aa9c7', true);
    if (touchMarked) mark(point.time_s, '#ffcc76');
    if (shift !== null) {
      wave(shift, '#6adcca');
      mark(point.time_s + shift, '#6adcca');
    }
  }

  function showFrame() {
    const index = Number(frameInput.value);
    const frames = data?.frames || [];
    const hasPair = frames.length >= 2;
    $('contactReviewFrames').hidden = !hasPair;
    $('contactReviewTactile').hidden = !hasPair || !data?.touch?.length;
    $('contactSkip').disabled = !enabledForSession || saving;
    $('contactOpenTask').disabled = !enabledForSession || saving || Boolean(window.intakeExoDirty?.()) ||
      ['before', 'after'].some(part => !['pair_confirmed', 'skipped'].includes(data?.checks?.[part]?.status));
    if (!hasPair) {
      drawTouch();
      return;
    }
    const id = $('calibSyncSession').value;
    for (const [side, frame] of [['Previous', frames[index - 1]], ['Current', frames[index]]]) {
      const url = capturePath('/api/contact-calibration/frame?' + new URLSearchParams({id, cam:data.cam, frame:frame.frame_id}));
      const image = $('contact' + side + 'Image');
      if (image.getAttribute('src') !== url) image.src = url;
      $('contact' + side + 'Time').textContent = frame.time_s.toFixed(3) + ' s';
    }
    $('contactFrameCount').value = (index + 1) + ' / ' + frames.length;
    $('contactPreviousFrame').disabled = index <= 1;
    $('contactNextFrame').disabled = index >= frames.length - 1;
    drawTouch();
  }

  async function load({force = false, completedSession = null} = {}) {
    if ($('calibContactTab').hidden || $('intake').hidden || $('calibContact').hidden) return;
    $('retryContactPreview').hidden = true;
    const id = $('calibSyncSession').value;
    const part = currentPart();
    const cam = cameraSelect.value || 'cam0';
    const key = [id, part, cam].join('|');
    const keep = (force || completedSession === id) && identity === key && data?.available;
    if (!force && completedSession !== id && identity === key) return;
    identity = key;
    const token = ++generation;
    const lastFrame = keep && data?.frames?.[Number(frameInput.value)]?.frame_id;
    const lastTouch = keep && touchMarked ? data.touch[Number(touchInput.value)]?.sample : undefined;
    data = null;
    enabledForSession = false;
    touchMarked = false;
    showResults();
    showFrame();
    showStatus(id ? '正在读取画面…' : '请选择录制。');
    if (!id) return;
    try {
      const result = await api('/api/contact-calibration?' + new URLSearchParams({id,part,cam}));
      if (token !== generation) return;
      data = result;
      const cameras = data.cameras || ['cam0','cam1'];
      cameraSelect.replaceChildren(...cameras.map(role => new Option(cameraNames[role], role)));
      cameraSelect.value = cameras.includes(cam) ? cam : cameras[0];
      enabledForSession = Boolean(data.cameras);
      showResults(data.checks);
      if (cameraSelect.value !== cam) return load();
      if (data.available) {
        const frames = data.frames, touches = data.touch;
        const archived = data.checks?.[part];
        const check = archived?.cam === cam ? archived : null;
        const savedFrame = frames.findIndex(row => row.frame_id === (lastFrame || check?.after?.frame_id));
        frameInput.min = 1;
        frameInput.max = Math.max(1, frames.length - 1);
        frameInput.value = savedFrame > 0 ? savedFrame : 1;
        const savedTouch = touches.findIndex(row => row.sample === (keep ? lastTouch : check?.touch?.sample));
        const visualTime = frames[Number(frameInput.value)]?.time_s;
        const nearestTouch = touches.findIndex(row => row.time_s >= visualTime);
        touchInput.min = 0;
        touchInput.max = Math.max(0, touches.length - 1);
        touchInput.value = savedTouch >= 0 ? savedTouch : Math.max(0, nearestTouch);
        touchMarked = savedTouch >= 0;
        showStatus(frames.length < 2 ? '此相机缺少连续画面。可换相机或跳过。' : '');
      } else {
        showStatus(data.message);
        if (data.message.startsWith('等待')) {
          const job = state?.job;
          const preparing = job?.session_id === id && job.status === 'running';
          $('retryContactPreview').hidden = preparing;
          showStatus(preparing ? '正在准备复核画面和触觉…' :
            job?.session_id === id && job.status === 'failed' ? job.message : '复核画面待准备。');
          identity = '';
        }
      }
      showFrame();
    } catch (error) {
      if (token === generation) { identity = ''; showStatus(error.message); }
    }
  }

  async function save(action) {
    if (saving || !enabledForSession) return;
    const session_id = $('calibSyncSession').value;
    const part = currentPart();
    const payload = {session_id, part, action};
    if (action === 'confirm') {
      const index = Number(frameInput.value);
      if (!data?.frames?.[index - 1]) return;
      Object.assign(payload, {cam:data.cam, previous_id:data.frames[index - 1].frame_id, frame_id:data.frames[index].frame_id});
      if (touchMarked) payload.sample = data.touch[Number(touchInput.value)].sample;
    }
    saving = true;
    showFrame();
    try {
      await post('/api/contact-calibration/review', payload);
      refreshIntakeStatus();
      notifyEgoCaptureUpdated();
      if ($('calibSyncSession').value === session_id && currentPart() === part) await load({force:true});
    } catch (error) {
      showStatus(error.message);
    } finally {
      saving = false;
      showFrame();
    }
  }

  window.refreshContactCalibration = (options) => load(options);
  $('calibSyncSession').addEventListener('change', () => load());
  $('contactReviewParts').onchange = cameraSelect.onchange = () => load();
  frameInput.oninput = showFrame;
  for (const [id, step] of [['contactPreviousFrame', -1], ['contactNextFrame', 1]]) {
    $(id).onclick = () => { frameInput.value = Number(frameInput.value) + step; showFrame(); };
  }
  $('contactClearTouch').onclick = () => {
    touchMarked = false;
    drawTouch();
    touchInput.focus();
  };
  touchInput.oninput = () => {
    touchMarked = true;
    drawTouch();
  };
  touchGraph.onclick = event => {
    if (!data?.touch?.length) return;
    const {left, width} = touchGraph.getBoundingClientRect();
    const fraction = Math.max(0, Math.min(1, (event.clientX - left - chartInset) / (width - 2 * chartInset)));
    const points = data.touch;
    const frame = Number(frameInput.value);
    const saved = savedSelection(data.frames[frame - 1], data.frames[frame], points[Number(touchInput.value)]);
    const [start, span] = touchDomain(points, saved?.status === 'pair_confirmed' ? saved : null);
    const selectedTime = start + fraction * span;
    const index = points.reduce((best, row, i) =>
      Math.abs(row.time_s - selectedTime) < Math.abs(points[best].time_s - selectedTime) ? i : best, 0);
    touchInput.value = index;
    touchMarked = true;
    drawTouch();
  };
  new ResizeObserver(drawTouch).observe(touchGraph);
  $('contactConfirm').onclick = () => save('confirm');
  $('contactSkip').onclick = () => save('skip');
  window.openContactCalibration = async (id, {firstPart = false} = {}) => {
    if (!sessions.some(session => session.session_id === id && session.contact_calibration)) return;
    setLiveView(false);
    await chooseSession(id, 'intake');
    $('calibSyncSession').value = id;
    if (firstPart) parts[0].checked = true;
    $('calibContactTab').click();
    $('contactReviewTitle').focus();
    await load();
  };
  $('reviewContactCalibration').onclick = () => window.openContactCalibration(reviewSessionId).catch(error => toast(error.message));
  $('contactOpenTask').onclick = () => openQuality($('calibSyncSession').value).catch(error => toast(error.message));
  $('retryContactPreview').onclick = async () => {
    const button = $('retryContactPreview');
    button.disabled = true;
    try {
      await post('/api/intake/preview', {session_id:$('calibSyncSession').value});
      showStatus('正在准备复核画面和触觉…');
      button.hidden = true;
      await pollState();
    } catch (error) {
      showStatus(error.message);
    } finally {
      button.disabled = false;
    }
  };
})();

