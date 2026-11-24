/* Ego workbench capture frontend. Polls /api/state, renders device, touch,
   link stats, calibration status, review/export/loader-check actions. */
'use strict';
const $ = (id) => document.getElementById(id);
const captureQuery = new URLSearchParams(location.search);
if (captureQuery.get('layout') === 'workspace' && window.parent !== window) {
  document.documentElement.classList.add('capture-embedded');
}
const capturePath = (path) => path.startsWith('/') ? '/ego-capture' + path : path;
let requestedReviewSession = captureQuery.get('session');

function notifyEgoCaptureUpdated() {
  if (window.parent !== window) {
    window.parent.postMessage({ type: 'ego-capture-session-updated' }, location.origin);
  }
}
let TOKEN = '';
let state = null;
let sessions = [];
let reviewFrames = [];
let reviewQuality = null;   // the selected session's QC block (contact candidates live here)
let reviewSessionId = '';
let reviewPlaying = false;
let reviewTimer = null;
let reviewPairGeneration = 0, reviewPlayRun = 0;
let reviewSegments = [], pendingReviewSegment = null, selectedReviewRange = null;
let reviewSetup = null, savedSetups = [], policyRequest = 0;
let qualitySessionId = '', qualityLoadGeneration = 0, qualityExpanded = false;
let reviewDuration = 0, reviewTimeS = null, reviewFrameTimeS = null;
let selectedSessionId = '';
let pressureGeneration = 0, pressureTimer, pressureController, latestPressure;
function paintRecordedPressure(sample, text = '当前时刻附近没有手套原始帧。') {
  window.renderRecordedHand?.(sample);
  const canvas = $('recordedPressure'), ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if (!sample?.grid) {
    $('recordedPressureTime').textContent = sample?.gloveStatus || text;
    return;
  }
  const cell = canvas.width / 20;
  for (const [row, values] of sample.grid.entries()) {
    for (const [column, count] of values.entries()) {
      const fraction = count / 255;
      ctx.fillStyle = 'hsl(' + (184 - 151 * fraction) + ' 72% ' + (15 + 44 * fraction) + '%)';
      ctx.fillRect(column * cell, row * cell, cell - 1, cell - 1);
    }
  }
  $('recordedPressureTime').textContent = sample.timeS.toFixed(3) + ' s · 接收时间差 '
    + sample.deltaMs.toFixed(1) + ' ms · 原始 count 0–255';
}
function queueRecordedPressure(time, text) {
  const id = reviewSessionId;
  if (time == null || !id) {
    pressureGeneration++;
    latestPressure = null;
    clearTimeout(pressureTimer);
    pressureTimer = null;
    pressureController?.abort();
    paintRecordedPressure(null, text);
    return;
  }
  latestPressure = {id, time};
  if (pressureTimer) return;
  pressureTimer = setTimeout(async () => {
    pressureTimer = null;
    const generation = ++pressureGeneration;
    const {id, time} = latestPressure;
    pressureController?.abort();
    pressureController = new AbortController();
    try {
      const result = await api('/api/recorded-tactile?' + new URLSearchParams({id, time}),
                               {signal:pressureController.signal});
      if (generation === pressureGeneration && result.sessionId === id && id === reviewSessionId &&
          $('reviewAnnotations').open) paintRecordedPressure(result.sample);
    } catch (error) {
      if (generation === pressureGeneration) paintRecordedPressure(null, error.message);
    }
  }, 85);
}
let importPolling = false, lastImportPoll = 0;
async function loadImports() {
  if (importPolling || $('importPanel').hidden || !$('directory').classList.contains('active')) return;
  importPolling = true;
  lastImportPoll = Date.now();
  try {
    const {directory, sessions: rows} = await api('/api/imports');
    $('captureInboxPath').textContent = directory;
    const pending = rows.filter(row => !row.error);
    const incomplete = rows.filter(row => row.error && !row.error.includes('已有此录制编号'));
    const status = pending.length ? pending.length + ' 条待自动导入'
      : incomplete.length ? incomplete[0].id + ' · ' + incomplete[0].error
      : '正在监测导入目录。';
    $('captureImportStatus').textContent = status;
  } catch (error) {
    $('captureImportStatus').textContent = '导入目录读取失败 · ' + error.message;
  } finally {
    importPolling = false;
  }
}
function setupDescription(setup) {
  return setup ? [
    setup.camera_reported ? '有设备上报的相机参数' : '无设备上报的相机参数',
    setup.touch_baseline ? '有录制时的触觉基线' : '无录制时的触觉基线',
    setup.stereo_independently_verified ? '双目标定已有独立验证' : '双目标定未独立验证'
  ].join(' · ') : '该录制缺少随录标定。';
}
async function loadSavedSetups() {
  savedSetups = await api('/api/recorded-setups');
}
function renderReviewSetup() {
  const select = $('reviewSetup');
  const selected = reviewSetup?.comparison?.session_id;
  select.replaceChildren(new Option('不选对照录制', qualitySessionId));
  for (const row of savedSetups.filter(row => row.session_id !== qualitySessionId)) {
    select.add(new Option(row.session_id, row.session_id));
  }
  if (selected && !savedSetups.some(row => row.session_id === selected)) {
    select.add(new Option(selected + '（未找到）', selected));
  }
  if (selected) select.value = selected;
  select.disabled = !reviewSetup;
  $('reviewSetupStatus').textContent = selected
    ? (reviewSetup.comparison_available ? setupDescription(reviewSetup.comparison) : '原对照标定已变更或缺失。')
    : '可以选择一条录制查看相机参数、触觉基线和验证状态。';
}

function renderFacts(id, rows) {
  $(id).replaceChildren(...rows.flatMap(([label, value]) => {
    const term = document.createElement('dt');
    const detail = document.createElement('dd');
    term.textContent = label;
    detail.textContent = value;
    return [term, detail];
  }));
}

function renderQualityJob(job, pending = false) {
  const record = sessions.find(session => session.session_id === selectedSessionId);
  if (!record) return;
  const ownJob = job?.session_id === selectedSessionId ? job : null;
  const busy = pending || ownJob?.status === 'running';
  const processed = Boolean(record.processed);
  const hasReview = processed && (record.quality?.stereo_pairs_paired > 0 || record.quality?.glove_valid_frames > 0);
  $('qcStatus').textContent = busy ? '整理中' : ownJob?.status === 'failed' ? '整理失败' : processed ? '已整理' : '待整理';
  $('qcJobMessage').textContent = busy ? (ownJob?.message || '正在提交整理…')
    : ownJob?.status === 'failed' ? ownJob.message : '';
  $('process').textContent = processed ? '重新整理' : '开始整理';
  $('process').disabled = busy || job?.status === 'running';
  $('qualityOpenReview').disabled = !hasReview;
}

function formatSyncStats(stats, device = false) {
  if (!stats?.count) return '暂无配对统计';
  const values = ['中位数 ' + Number(stats.median).toFixed(2) + ' ms',
    '最大 ' + Number(stats.max).toFixed(2) + ' ms'];
  if (device && Number.isFinite(stats.bound_ms)) values.push('配对上限 ' + stats.bound_ms + ' ms');
  return values.join(' · ');
}
async function loadQualityDetail(id) {
  const generation = ++qualityLoadGeneration;
  const record = sessions.find(session => session.session_id === id);
  $('qualityContent').hidden = !record;
  if (!record) return;
  qualitySessionId = id;
  $('qualityRecording').textContent = [id, record.object_id].filter(Boolean).join(' · ');
  reviewSetup = null;
  renderReviewSetup();
  const qc = record.quality;
  $('qcMetrics').hidden = !qc;
  $('qcStereoCount').textContent = qc?.stereo_pairs_paired ?? '—';
  $('qcGloveCount').textContent = qc?.glove_valid_frames ?? '—';
  $('qcContactCount').textContent = Array.isArray(qc?.contact_candidates) ? qc.contact_candidates.length : '—';
  $('qualitySummary').textContent = qc ? JSON.stringify(qc, null, 1) : '这条录制还没有整理报告。';
  renderFacts('qcOriginalSetup', [['随录标定', '正在读取…']]);
  renderFacts('qcSyncFacts', [
    ['双目设备时间差', formatSyncStats(qc?.stereo_pair_device_delta_ms, true)],
    ['双目主机接收时间差', formatSyncStats(qc?.stereo_pair_receipt_delta_ms)],
    ['手套与相机的关联', qc ? '按本机接收时间，物理同步待验证' : '待整理'],
    ['整理使用的触觉基线', qc?.baseline_source === 'captured_rest_calibration' ? '随录静置基线'
      : qc?.baseline_source === 'first_two_seconds_unverified' ? '用录制前两秒估算，未验证' : '暂无记录']
  ]);
  renderQualityJob(state?.job);
  try {
    const [detail] = await Promise.all([
      api('/api/session?' + new URLSearchParams({view:'quality', id})), loadSavedSetups()
    ]);
    if (generation !== qualityLoadGeneration || id !== selectedSessionId) return;
    reviewSetup = detail.setup;
    const setup = reviewSetup?.original;
    renderFacts('qcOriginalSetup', setup ? [
      ['相机参数', setup.camera_reported ? '设备已上报' : '无上报记录'],
      ['随录触觉基线', setup.touch_baseline ? '已保存' : '缺少记录'],
      ['双目标定验证', setup.stereo_independently_verified ? '已独立验证' : '未独立验证']
    ] : [['随录标定', '该录制没有随录标定记录']]);
    renderReviewSetup();
  } catch (error) {
    if (generation === qualityLoadGeneration && id === selectedSessionId) {
      renderFacts('qcOriginalSetup', [['随录标定', '读取失败 · ' + error.message]]);
    }
  }
}

let reviewFirst = 0, reviewLast = -1;
let lastJobStatus = '';     // so a finished QC/dataset job refreshes the review view exactly once
let markerRows = [];        // declared marker table rows currently shown on the calibration page
let markerDefaultRow = null;  // the server's provisional default row (ID1, 4 cm above the back)
let markerOrientationOptions = [];  // declared postures a row may use (back-facing / thumb-facing)

async function api(path, opts = {}) {
  const res = await fetch(capturePath(path), Object.assign({ headers: { 'X-Capture-Token': TOKEN } }, opts));
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    if (body.origin_diagnostic) {
      const d = body.origin_diagnostic;
      showOriginBanner(d.actual_origin);
      throw new Error('Origin rejected：服务端已记录诊断（actual=' + d.actual_origin +
        '，expected=' + d.expected_origin.join(' 或 ') + '，path=' + d.path +
        '）。请刷新 Ego 交互数据工作台后重试。');
    }
    throw new Error(body.error || body.message || res.statusText);
  }
  return body;
}
const post = (path, data) => api(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Capture-Token': TOKEN }, body: JSON.stringify(data || {}) });

$('workflowSegmentPublish')?.addEventListener('click', async () => {
  const button = $('workflowSegmentPublish');
  const message = $('workflowSegmentStatus');
  const start = $('workflowSegmentStart').value;
  const end = $('workflowSegmentEnd').value;
  if (!reviewSessionId || reviewSessionId !== selectedSessionId || !start || !end || !$('workflowSegmentName').value.trim()) {
    message.textContent = '请选择录制并填写片段名和起止时间。';
    return;
  }
  const sessionId = reviewSessionId;
  button.disabled = true;
  try {
    const segment = await post('/api/workflow-segments', {
      session_id: sessionId,
      name: $('workflowSegmentName').value.trim(),
      start_s: Number(start),
      end_s: Number(end),
      category: $('workflowSegmentCategory').value
    });
    notifyEgoCaptureUpdated();
    if (segment.manualEventsWarning) toast(segment.manualEventsWarning);
    if (reviewSessionId === sessionId && selectedSessionId === sessionId) {
      pendingReviewSegment = {session:sessionId, name:segment.segmentId};
      await loadSessionDetail(sessionId);
    }
  } catch (error) {
    if (selectedSessionId === sessionId) message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

/* ---- same-origin guard banner ----
   The 2026-09-17 incident: every button POST failed with a bare 'Origin
   rejected' while GET polling worked. The server now logs {ts, path,
   expected_origin, actual_origin, remote_addr} (never the token) on each
   rejection, and this banner compares the page's own origin with the value
   the server expects, before any POST is attempted. */
let ORIGIN_EXPECTED = null;
function showOriginBanner(actual) {
  const b = $('originBanner'); if (!b) return;
  b.classList.remove('hidden');
  b.textContent = '⚠ Origin 不匹配：按钮请求会被服务端拒绝。本页 origin=' +
    location.origin + '，服务端期望=' + (ORIGIN_EXPECTED || []).join(' 或 ') +
    (actual ? '，被拒请求实际发送=' + actual : '') +
    '。请在 Ego 交互数据工作台中重新打开采集面板。';
}
function checkOrigin(st) {
  ORIGIN_EXPECTED = st.origin_expected || null;
  if (ORIGIN_EXPECTED && !ORIGIN_EXPECTED.includes(location.origin)) showOriginBanner(null);
  else { const b = $('originBanner'); if (b) b.classList.add('hidden'); }
}

function toast(msg) {
  if (window.parent !== window && captureQuery.get('layout') === 'workspace') {
    window.parent.postMessage({ type: 'ego-capture-feedback', message: String(msg) }, location.origin);
    return;
  }
  const t = $('toast'); t.textContent = msg; t.classList.remove('hidden');
  clearTimeout(t._tm); t._tm = setTimeout(() => t.classList.add('hidden'), 4000);
}

function dot(el, cls) { el.className = cls; }
function deviceCls(s) {
  if (!s || !s.connected) return 'bad';
  if (s.stale) return 'warn';
  return 'ok';
}
function fmtBytes(n) {
  if (n == null) return '—';
  if (n > 1e9) return (n / 1e9).toFixed(2) + ' GiB';
  if (n > 1e6) return (n / 1e6).toFixed(1) + ' MiB';
  return Math.round(n / 1024) + ' KiB';
}

let recordStartPending = false;
function setRecordingButtons(recording) {
  $('startRecord').disabled = recording || recordStartPending;
  $('startUnlimitedRecord').disabled = recording || recordStartPending;
  $('stopRecord').disabled = !recording;
}

function renderState() {
  if (!state) return;
  checkOrigin(state);
  const dev = state;
  dot($('cameraDot'), deviceCls(dev.camera));
  $('cameraStatus').textContent = dev.camera.message || (dev.camera.connected ? '已连接' : '未连接');
  dot($('gloveDot'), deviceCls(dev.glove));
  $('gloveStatus').textContent = dev.glove.message || (dev.glove.connected ? '已连接' : '未连接');
  const pose = dev.pose || {};
  dot($('poseDot'), pose.detected ? 'ok' : (pose.model_ready ? 'warn' : 'bad'));
  $('poseStatus').textContent = pose.message || '…';
  $('notice').textContent = dev.notice || '';
  const replaying = dev.mode === 'replay';
  $('modeTag').textContent = replaying ? '回放模式（无触觉）' : '实时模式';
  $('modeBadge').textContent = replaying ? '离线回放' : '实时';
  const c = dev.camera, g = dev.glove;
  $('camFrames').textContent = c.frames ?? 0;
  $('camBad').textContent = c.invalid_or_partial ?? 0;
  $('camRate').textContent = (c.rate_hz ?? '—') + ' Hz';
  $('camBytes').textContent = fmtBytes(c.bytes);
  $('gloveFrames').textContent = g.frames ?? 0;
  $('gloveBad').textContent = g.invalid_or_partial ?? 0;
  $('gloveRate').textContent = (g.rate_hz ?? '—') + ' Hz';
  $('gloveBytes').textContent = fmtBytes(g.bytes);
  if (dev.disk_free_bytes != null) $('diskFree').textContent = fmtBytes(dev.disk_free_bytes);
  const pd = (dev.recording && dev.recording.per_device) || null;
  const fmtSess = (d) => d ? (d.frames + ' 帧 · ' + fmtBytes(d.bytes) +
    (d.mean_rate_hz != null ? ' · ' + d.mean_rate_hz + ' Hz' : '')) : '—';
  $('sessCam').textContent = fmtSess(pd && pd.camera);
  $('sessGlove').textContent = fmtSess(pd && pd.glove);
  $('clockStatus').textContent = '相机校时：' + (c.clock_status || '未发送') +
    '。视触时间对应仍未验证；同步验证需按「02 标定与同步」操作。';
  const rec = dev.recording;
  const calibrationState = g.calibration_state;
  /* Which bridge source is live: the page is served from disk on every request
     but the bridge process keeps running the module it imported, so after a code
     change this stamp is the only visible difference between "the fix ran" and
     "the station still needs a restart". bridge_build comes from the bridge's
     'ready' message, build from its calibration states. */
  const bridgeBuild = (calibrationState && calibrationState.build) || g.bridge_build || '';
  if (calibrationState && $('qnbotCalibrationState')) {
    /* The buttons follow the bridge's real phase. A run is active only between
       'starting' and its terminal phase, and the bridge emits 'complete' only
       after the SDK actually returned a calibration record, so a failed or
       refused run can no longer leave the panel claiming success. */
    const phase = calibrationState.phase;
    const active = phase === 'starting' || phase === 'awaiting_capture' ||
      phase === 'capturing' || phase === 'finalizing';
    const detail = calibrationState.message ||
      (phase === 'awaiting_capture'
        ? '等待采集当前姿势：' + (calibrationState.prompt || '')
        : phase === 'capturing'
          ? '采集中：' + (calibrationState.prompt || '') + '（' + (calibrationState.captured ?? 0) +
            '/' + (calibrationState.sample_count ?? '?') + '）'
          : phase === 'starting'
            ? '标定启动中：本次会重新采集，请按提示摆姿势'
            : phase === 'finalizing'
              ? '标定记录已写入，正在启用官方输出…'
              : phase === 'complete'
                ? '标定完成，官方输出已启用'
                : phase === 'reused'
                  ? '使用上次标定成果（未重新采集）：官方输出已启用'
                  : phase === 'absent'
                    ? '尚无标定记录：官方输出未启用'
                    : phase === 'error' ? '标定失败' : phase || '标定中');
    /* The bridge reuses a stored record at startup, so this panel must be able to
       report that state: with a record on disk the official output is up and the
       page must not keep asking for a calibration that already exists. */
    lastCalibPhase = phase;
    $('qnbotCalibrationState').textContent = detail;
    $('qnbotCalibrationState').title = [calibrationState.record_id, calibrationState.record_written, bridgeBuild]
      .filter(Boolean).join(' · ');
    $('startQnBotCalibration').disabled = active;
    $('reuseQnBotCalibration').disabled = active;
    $('captureQnBotCalibration').disabled = phase !== 'awaiting_capture';
    $('cancelQnBotCalibration').disabled = !active;
  } else if ($('qnbotCalibrationState')) {
    /* No calibration message yet (fresh station, bridge still starting). The
       build stamp is the one thing worth showing: it is how an operator can see
       that the running bridge is the source they expect. */
    $('qnbotCalibrationState').textContent = '尚未开始' +
      (bridgeBuild ? ' · 桥构建 ' + bridgeBuild : '');
  }
  $('calibBaselineStatus').textContent = !g.connected ? '手套未连接' :
    g.pressure_baseline_ready ? '已采集压感基线' : g.baseline_ready ? '已采集字节基线' : '待采集静置基线';
  $('calibTouchBaseline').disabled = !g.connected;
  if (rec) {
    $('recordTimer').textContent = rec.seconds.toFixed(1) + ' s · ' + fmtBytes(rec.bytes);
    setRecordingButtons(true);
    $('recordingInfo').textContent = '会话 ' + rec.session_id + (rec.limits_enabled === false ? ' · 不限额录制' : '') + ' · 原始数据流写入本地';
  } else {
    $('recordTimer').textContent = '未录制';
    setRecordingButtons(false);
    $('recordingInfo').textContent = '';
  }
  $('touchValue').textContent = '原始相对值，尚未换算为压力';
  /* Preview frames, touch curve and sensor bars are owned by the fast
     /api/live/meta and /api/live/touch loops, not by this 2 s status poll. */
  if (state.job) renderJob(state.job);
  if (qualityExpanded && $('directory').classList.contains('active')) renderQualityJob(state.job);
  if (Date.now() - lastImportPoll > 12000) loadImports();
  $('outputPath').textContent = state.output_root;
  if (pose.timing_ms) {
    $('poseStatus').textContent += ' · 推理 ' + pose.timing_ms.ema_ms + ' ms（EMA）';
  }
}

/* ---- touch rendering ---- */
function drawTouch(hist) {
  const cv = $('touchCanvas'), ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.strokeStyle = '#25303a'; ctx.strokeRect(0.5, 0.5, cv.width - 1, cv.height - 1);
  if (!hist.length) { ctx.fillStyle = '#93a3b0'; ctx.fillText('等待手套数据', 12, 24); return; }
  let monoMax = hist[hist.length - 1][1], span = 10e9;
  const peak = Math.max(1, ...hist.map(s => s[2] == null ? 0 : s[2]));
  ctx.strokeStyle = '#4fc3f7'; ctx.beginPath();
  for (const [, mono, score] of hist) {
    if (score == null) continue;
    const x = cv.width - (monoMax - mono) / span * cv.width;
    const y = cv.height - 8 - Math.min(score / peak, 1) * (cv.height - 20);
    x >= 0 ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.stroke();
  ctx.fillStyle = '#93a3b0'; ctx.fillText('最近 10 s · 原始相对响应 · 峰值 ' + peak.toFixed(1), 10, 14);
}
const FINGER_COLORS = {index:'#4fc3f7', middle:'#aed581', ring:'#ffb74d', pinky:'#f48fb1', thumb:'#9575cd'};
const FINGER_ZH = {index:'食指', middle:'中指', ring:'无名指', pinky:'小指', thumb:'拇指'};
function drawSensors(g) {
  const cv = $('sensorCanvas'), ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, cv.width, cv.height);
  const vals = g.values || [], n = vals.length || 1, w = cv.width / n;
  const max = Math.max(1, ...vals);
  const labels = g.channel_labels || [];
  const every = n > 28 ? 2 : 1; // thin out CH labels so 54 bars stay readable
  vals.forEach((v, i) => {
    const h = v / max * (cv.height - 24);
    const lab = labels[i] || '';
    const finger = Object.keys(FINGER_COLORS).find(f => lab.startsWith(f));
    const base = finger ? FINGER_COLORS[finger] : '#93a3b0';
    ctx.fillStyle = base + Math.round(40 + 160 * v / max).toString(16).padStart(2, '0');
    ctx.fillRect(i * w + 1, cv.height - 18 - h, Math.max(2, w - 2), h);
    if (i % every === 0) {
      ctx.fillStyle = '#93a3b0'; ctx.font = '8px sans-serif';
      ctx.fillText(String(g.offsets ? g.offsets[i] : i), i * w, cv.height - 5);
    }
  });
  ctx.fillStyle = '#93a3b0'; ctx.font = '11px sans-serif';
  const confirmed = g.channel_map_source && g.channel_map_source !== 'unconfirmed';
  ctx.fillText(n + ' 通道原始字节值 · ' + (confirmed ? '分组来自探针证据（未显式确认）' : '位置映射未确认'), 8, 12);
  $('sensorSummary').textContent = g.finger_scores && confirmed
    ? '各指峰值（位点中位正增量）：' + Object.entries(g.finger_scores)
        .map(([f, v]) => (FINGER_ZH[f] || f) + ' ' + v.toFixed(1)).join(' · ')
    : (g.channel_map_source === 'unconfirmed'
      ? '通道到手指的映射未确认：以上 CH 编号为字节偏移，不代表手指。' : '');
}

/* One colour mapping for the raw pressure grid, shared by this card's heatmap (page 01) and the
   pose viewport's no-pose panel, so the two cannot drift apart: blue at idle, red at the frame
   maximum. */
function pressureCellColor(level) {
  const t = clamp01(level);
  return 'hsl(' + (210 - 210 * t) + ' 75% ' + (28 + t * 42) + '%)';
}

function drawPressure(grid) {
  const cv = $('pressureCanvas'), ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, cv.width, cv.height);
  if (!Array.isArray(grid) || grid.length !== 23) {
    ctx.fillStyle = '#93a3b0'; ctx.fillText('等待 23×20 压感网格', 12, 24); return;
  }
  const rows = 23, cols = 20, cellW = cv.width / cols, cellH = (cv.height - 24) / rows;
  let maximum = 1;
  grid.forEach(row => row.forEach(value => { maximum = Math.max(maximum, value); }));
  grid.forEach((row, y) => row.forEach((value, x) => {
    ctx.fillStyle = pressureCellColor(value / maximum);
    ctx.fillRect(x * cellW, 20 + y * cellH, Math.ceil(cellW), Math.ceil(cellH));
  }));
  ctx.strokeStyle = '#25303a'; ctx.strokeRect(0.5, 20.5, cv.width - 1, cv.height - 24 - 1);
  ctx.fillStyle = '#93a3b0'; ctx.font = '11px sans-serif';
  /* The panel stays raw on purpose: it is the signal view. The residual is printed next to it so the
     resting offset (glove fit, strap, a fold) is visibly separated from a load, because the colour
     ramp alone cannot tell the two apart. */
  const residual = handPressureBaseline() ? handPressureResidualGrid() : null;
  const residualMax = residual ? handPressureGridMax(residual) : null;
  ctx.fillText('23×20 原始 uint8 压感网格 · 当前最大值 ' + maximum
    + (residual ? ' · 减静置基线后最大 ' + residualMax : ' · 未采集静置基线'), 8, 13);
  $('pressureValue').textContent = '压感原始值 0–255 · 最大 ' + maximum
    + (residual ? ' · 静置基线残差最大 ' + residualMax + '（3D 图层按残差阈值画接触）'
                : ' · 未采集静置基线：3D 图层不画接触圆垫');
}

const EMF_FINGERS = ['拇指', '食指', '中指', '无名指', '小指'];
function renderEmfPose(emf) {
  const grid = $('emfPoseGrid');
  if (!emf || !Array.isArray(emf.fingers)) return;
  grid.replaceChildren();
  const side = Number(emf.hand_side) === 1 ? '左手' : (Number(emf.hand_side) === 0 ? '右手' : '未知手');
  const valid = emf.fingers.filter((f) => f.valid).length;
  $('emfPoseTag').textContent = side + ' · seq ' + emf.stream_sequence
    + ' · 有效 ' + valid + '/' + emf.fingers.length;
  emf.fingers.forEach((finger, index) => {
    const item = document.createElement('div'); item.className = 'pose-item';
    const position = finger.position_mm || {};
    const quaternion = finger.quaternion || {};
    const status = finger.valid ? '有效' : '无效';
    /* A zero bit means the vendor says these 14 bytes must not be used as a pose, so the page
       says which bit failed instead of leaving a bare '无效' that reads like a page error. */
    const why = finger.valid ? '' : '<small>valid_mask 第 ' + index + ' 位 = 0：设备本帧未上报该指位姿</small>';
    item.innerHTML = '<b>' + EMF_FINGERS[index] + '</b><span class="pose-valid ' +
      (finger.valid ? 'ok' : 'bad') + '">' + status + '</span>' +
      '<small>xyz mm: ' + (finger.valid ?
        [position.x, position.y, position.z].map(v => Number(v).toFixed(2)).join(' / ') : '不使用') +
      '</small><small>quat wxyz: ' + (finger.valid ?
        [quaternion.w, quaternion.x, quaternion.y, quaternion.z].map(v => Number(v).toFixed(3)).join(' / ') : '不使用') +
      '</small>' + why;
    grid.append(item);
  });
}

function renderJointCommand(glove) {
  const command = glove && glove.joint_command;
  const status = (glove && glove.retargeting_status) || '未连接';
  /* The bridge keeps the last calibration_state it emitted, so this badge can
     report the real phase instead of claiming 'awaiting calibration' forever
     after a run already completed (or failed) in this session. */
  const calib = glove && glove.calibration_state;
  const calibPhase = calib && calib.phase;
  /* 'reused' means the workflow is running on the record already on disk, which
     the bridge does at startup (no re-measurement). Anything that reads "please
     calibrate first" has to treat that like a completed calibration, or the page
     asks for a calibration that already exists. */
  const calibEnabled = calibPhase === 'complete' || calibPhase === 'reused';
  const calibSource = calibPhase === 'reused' ? '使用上次标定成果' : '本次已重新采集';
  $('retargetStatus').textContent = status !== 'ready' ? status
    : (calibPhase === 'complete' ? 'SDK 已连接 · 标定完成'
      : (calibPhase === 'reused' ? 'SDK 已连接 · 使用上次标定成果'
        : (calibPhase === 'error' ? 'SDK 已连接 · 标定失败'
          : (calibPhase === 'absent' ? 'SDK 已连接 · 尚无标定记录' : 'SDK 已连接 · 等待标定'))));
  $('skeletonStatus').textContent = glove && glove.skeleton_api
    ? (glove.skeleton_pose
      ? '25 点骨骼：已生成 wrist_local 坐标，位置 m，四元数 xyzw。'
      : (calibEnabled
        ? '25 点骨骼模型已加载，官方输出已启用（' + calibSource + '），等待 skeleton_pose 输出。'
        : '25 点骨骼模型已加载，但尚无输出；请先完成 OpenXR 手套标定。'))
    : '25 点骨骼不可用：' + ((glove && glove.skeleton_error) || 'SDK skeleton model 未加载');
  const box = $('jointCommandGrid');
  if (!command || !command.joints) {
    box.replaceChildren();
    const empty = document.createElement('span'); empty.className = 'muted';
    empty.textContent = status === 'ready'
      ? (calibEnabled
        ? '官方算法已连接（' + calibSource + '），尚无关节角输出；有效手套帧到达后即输出。'
        : '官方算法已连接，但尚无关节角输出；请先完成 glove.left → hand.openxr_hand.left 标定。')
      : '等待官方关节角输出。';
    box.append(empty);
    return;
  }
  box.replaceChildren();
  Object.entries(command.joints).forEach(([name, value]) => {
    const item = document.createElement('div'); item.className = 'joint-item';
    item.innerHTML = '<b>' + name + '</b><span>' + Number(value).toFixed(4) + ' rad</span>';
    box.append(item);
  });
  if (glove.skeleton_pose && Array.isArray(glove.skeleton_pose.names)) {
    const pose = glove.skeleton_pose;
    pose.names.map((name, index) => [name, index]).forEach(([name, index]) => {
      const item = document.createElement('div'); item.className = 'joint-item';
      const p = pose.positions_local[index] || [0, 0, 0];
      item.innerHTML = '<b>骨骼 ' + name + '</b><span>(' + p.map(v => Number(v).toFixed(3)).join(', ') + ') m</span>';
      box.append(item);
    });
  }
}

/* ---- 25-point hand skeleton viewport (2026-09-20) ----
   Until now the skeleton_pose nodes were only listed as text rows, so there was
   no visual hand pose at all. The bridge already sends everything a real
   wireframe needs: skeleton_pose = {names[25], positions_local[25][3] in metres,
   orientations_local_xyzw[25][4]} in the wrist_local frame. The node list is
   fixed (wrist + 4 thumb joints + 4 fingers x 5 joints), so the parent chain
   below rebuilds the topology from names alone — no extra server field, no
   invented geometry. Model axes, verified against live data: +z runs along the
   fingers (wrist 0 -> extended middle tip ~0.15 m), +y points to the thumb,
   +x is the palm normal. */
/* Last calibration phase the bridge reported, kept for the empty viewport: with a
   stored record the bridge enables the official output at startup, so the empty
   state must say "waiting for the first frame", not "calibrate first". */
let lastCalibPhase = null;
const HAND_CHAINS = [
  ['thumb', ['thumb_metacarpal', 'thumb_proximal', 'thumb_distal', 'thumb_tip']],
  ['index', ['index_metacarpal', 'index_proximal', 'index_intermediate', 'index_distal', 'index_tip']],
  ['middle', ['middle_metacarpal', 'middle_proximal', 'middle_intermediate', 'middle_distal', 'middle_tip']],
  ['ring', ['ring_metacarpal', 'ring_proximal', 'ring_intermediate', 'ring_distal', 'ring_tip']],
  ['little', ['little_metacarpal', 'little_proximal', 'little_intermediate', 'little_distal', 'little_tip']],
];
const HAND_BONES = (() => {
  const bones = [];
  for (const [finger, nodes] of HAND_CHAINS) {
    let parent = 'wrist';
    for (const node of nodes) { bones.push([parent, node, finger]); parent = node; }
  }
  return bones;
})();
const HAND_TIPS = HAND_CHAINS.map(([finger, nodes]) => [finger, nodes[nodes.length - 1]]);
const HAND_TIP_NAMES = new Set(HAND_TIPS.map(([, tip]) => tip));
const HAND_NODE_FINGER = (() => {
  const map = { wrist: 'wrist' };
  for (const [finger, nodes] of HAND_CHAINS) for (const node of nodes) map[node] = finger;
  return map;
})();
const HAND_COLORS = { wrist: '#e7edf2', thumb: '#9575cd', index: '#4fc3f7', middle: '#aed581', ring: '#ffb74d', little: '#f48fb1' };
const HAND_FINGER_ZH = { wrist: '腕', thumb: '拇指', index: '食指', middle: '中指', ring: '无名指', little: '小指' };

const HAND_VIEW_W = 460, HAND_VIEW_H = 540, HAND_VIEW_DPR = 2;
const HAND_STEREO_W = 720, HAND_STEREO_H = 236, HAND_STEREO_DPR = 2;
const HAND_MODEL_CENTER = [0, 0, 0.075];  // wrist sits at z=0, extended tip ~0.15 m
const HAND_REF_RADIUS = 0.15;             // metres, fixed: one px-per-metre for every
                                          // frame, so a curled hand honestly reads smaller
const HAND_VIEW_HOME = { yaw: -0.55, pitch: 0.28 };
/* finger_scores keys come from contact_sites[].finger (index/middle/ring/pinky/thumb, see
   station_config.json), while the skeleton chains call the little finger 'little'. The tactile
   layer translates the one to the other instead of guessing a match. */
const HAND_TACTILE_SITE = { thumb: 'thumb', index: 'index', middle: 'middle', ring: 'ring', little: 'pinky' };
/* ---- pressure layer: one pad per taxel, placed on the pose (2026-09-23) ----
   The glove reports one 23x20 uint8 pressure grid per frame. Its region partition is
   documented in docs/references/matrixhand/EMF_PROTOCOL_FACTS.md. The colored palm is rows 0–14
   and columns 0–15 in the right-hand layout. Rows 17–22 are the four-column strips for each
   finger. The left-hand layout mirrors column c to 19 - c. The 100 unassigned cells stay in
   the raw matrix.

   What the vendor does NOT document: per-taxel metric coordinates, taxel pitch, and which end of
   the strip is the fingertip. The load is therefore drawn at taxel resolution but every position
   is derived from the pose's own measured geometry, and the two display conventions are printed on
   the canvas instead of being assumed away:

   1. finger strip. Taxel (row r, column c) becomes one pad on that finger's own knuckle-to-tip
      path, at u = (r - 17 + 0.5) / 6 of the path length (the six documented rows tile the finger
      uniformly, row 17 at the knuckle), offset across the finger by (c - (c0 + 1.5)) x column
      pitch. That pitch is measured, not assumed: the strip's 20 columns span the hand at the
      knuckle line in the documented little->thumb order, so pitch = |little knuckle - thumb
      knuckle| / 19, and the across-finger direction is that same measured axis made perpendicular
      to the bone. Several taxels of one row are therefore several pads across the finger, and the
      six rows are six pads along it: an entire finger is never merged into one cylinder.
   2. palm. The vendor documents a single colored block. The display places its counts in
      4 row bands x 5 column bands, each pad
      placed in the measured palm plane (wrist -> middle knuckle for rows, little -> thumb knuckle
      for columns) at its band centre and carrying its band's maximum.

   Only taxels at or above a threshold are drawn, and the threshold is applied to the *residual*:
   raw count minus the resting baseline the operator saved with 「采集静置基线 · 3 秒」, clamped at
   zero. A worn glove that touches nothing is not a clean zero: measured on the recorded 2026-09-19
   rest capture its raw grid reached 59 with 35 cells at or above 5, and on the rest_pose trial six
   seconds later the residual still reached 4 on single cells (while a hard thumb press reaches 20+
   and a fist 200+). So the floor is 5 residual counts and never below 15% of the residual's own
   maximum, and the residual, the threshold and the number of cells they dropped are all printed:
   the layer never hides what it did not draw. Units stay raw counts: the vendor documents no unit,
   no calibration curve and no zero definition, so this is never labelled a force. */
const HAND_PRESSURE_GRID = [23, 20];
const HAND_PRESSURE_PALM_ROWS = [0, 14];
const HAND_PRESSURE_PALM_COLS = [0, 15];
const HAND_PRESSURE_STRIP_ROWS = [17, 22];
const HAND_PRESSURE_COLS = { thumb: [16, 19], index: [12, 15], middle: [8, 11], ring: [4, 7], little: [0, 3] };
/* Display bands within the documented palm block. */
const HAND_PRESSURE_PALM_BANDS = { rows: 4, cols: 5 };
/* The residual floor. Not 2: a worn glove's own drift above its saved baseline reaches 4 counts on
   single cells (2026-09-19 rest_pose trial, 793 frames, versus a baseline captured 6 s earlier), and
   a floor of 2 drew ~8 pads on that resting hand. 5 clears the measured drift (0 cells at or above
   5 in all 793 resting frames) while every real contact stays well above it (hard thumb press:
   residual 20+, 11 pads; palm press: 33 pads; fist: 200+). */
const HAND_PRESSURE_RESIDUAL_MIN_COUNT = 5;
const HAND_PRESSURE_PAD_MIN_FRACTION = 0.15;
/* A frame can have every cell above the threshold; the cap keeps the drawing readable and the
   number of pads it suppressed is printed. */
const HAND_PRESSURE_PAD_MAX = 160;
/* Numbers are printed on the strongest pads only: a taxel map can carry dozens of pads and a label
   on each one covers the pose. The colour scale and the read-out carry the rest. */
const HAND_PRESSURE_LABEL_MAX = 12;
/* Metric radius bounds, scaled by the raw count and capped at half the local taxel pitch, so two
   neighbouring taxels stay two pads instead of merging into one blob. */
const HAND_PRESSURE_PAD_RADIUS_M = { finger: [0.0018, 0.0030], palm: [0.0025, 0.0075] };
const HAND_PRESSURE_PAD_PITCH_FRACTION = 0.5;
/* The cells the 2026-09-17 channel-localisation probe found responding, transcribed from
   station_config.json contact_sites (pressure index = frame byte - 6, row = index // 20, col =
   index % 20) and kept in the raw frame the probe measured (a left-hand glove). role=bend is the
   index bend sensor, not contact, and is never cited as contact evidence. The coverage is what the
   probe pressed, not the whole strip: ring only columns 14-15, pinky_3 only column 16. Rows 0-18
   have no site at all, so a palm pad carries no probe evidence either way. */
const HAND_PRESSURE_SITES = [
  { id: 'index_1', finger: 'index', row: 21, cols: [4, 7], role: 'touch' },
  { id: 'index_2', finger: 'index', row: 22, cols: [4, 7], role: 'touch' },
  { id: 'index_3', finger: 'index', row: 20, cols: [4, 7], role: 'touch' },
  { id: 'index_bend', finger: 'index', row: 19, cols: [4, 7], role: 'bend' },
  { id: 'middle_1', finger: 'middle', row: 22, cols: [8, 11], role: 'touch' },
  { id: 'middle_2', finger: 'middle', row: 21, cols: [8, 11], role: 'touch' },
  { id: 'middle_3', finger: 'middle', row: 20, cols: [8, 11], role: 'touch' },
  { id: 'ring_1', finger: 'ring', row: 22, cols: [14, 15], role: 'touch' },
  { id: 'ring_2', finger: 'ring', row: 21, cols: [14, 15], role: 'touch' },
  { id: 'ring_3', finger: 'ring', row: 20, cols: [14, 15], role: 'touch' },
  { id: 'pinky_1', finger: 'pinky', row: 22, cols: [16, 19], role: 'touch' },
  { id: 'pinky_2', finger: 'pinky', row: 21, cols: [16, 18], role: 'touch' },
  { id: 'pinky_3', finger: 'pinky', row: 20, cols: [16, 16], role: 'touch' },
  { id: 'thumb_1', finger: 'thumb', row: 20, cols: [0, 3], role: 'touch' },
  { id: 'thumb_2', finger: 'thumb', row: 21, cols: [0, 3], role: 'touch' },
  { id: 'thumb_3', finger: 'thumb', row: 22, cols: [0, 3], role: 'touch' },
];

/* Small vector helpers for the pad geometry. A point is [x, y, z] in metres in the model frame.
   They are module-level because both the placement (handPressurePadGeometry) and the pad builder
   (handPressurePads) need them, and a second private copy is how two mappings drift apart. */
const padSub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const padAdd = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const padScale = (a, s) => [a[0] * s, a[1] * s, a[2] * s];
const padDot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const padNorm = (a) => Math.hypot(a[0], a[1], a[2]);
const padUnit = (a) => {
  const length = padNorm(a);
  return length > 1e-9 ? padScale(a, 1 / length) : null;
};
const padLerp = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t,
  a[2] + (b[2] - a[2]) * t];
/* The part of b perpendicular to the unit vector a: the only direction a column offset may use,
   because it is the measured hand-width axis read in the bone's own frame. */
const padPerp = (b, a) => padUnit(padSub(b, padScale(a, padDot(b, a))));

/* Even split of a documented index range into display bands: 17 palm rows into 4, 20 columns into
   5. Kept as index ranges so a band's value is a max over the documented cells it covers. */
function handPressureBands(total, count) {
  const bands = [];
  for (let i = 0; i < count; i++) {
    const start = Math.floor(i * total / count);
    const end = Math.floor((i + 1) * total / count) - 1;
    if (end >= start) bands.push([start, end]);
  }
  return bands;
}

/* Where a taxel's pad sits on the pose, in metres in the model frame. Everything here is measured
   from the same node positions the wireframe uses; the only conventions are the ones the canvas
   prints (uniform tiling of the six documented rows, row 17 at the knuckle, and the palm's own
   display lattice). A finger or the palm whose nodes are missing gets no geometry and no pad. */
function handPressurePadGeometry(names, positions) {
  const index = new Map(names.map((name, i) => [name, i]));
  const at3 = (name) => {
    const i = index.get(name);
    if (i == null) return null;
    const row = positions[i];
    if (!Array.isArray(row) || row.length < 3) return null;
    const point = [Number(row[0]), Number(row[1]), Number(row[2])];
    return point.every((value) => isFinite(value)) ? point : null;
  };
  const sub = padSub, plus = padAdd, times = padScale, norm = padNorm, unit = padUnit;
  const lerp3 = padLerp, perpendicular = padPerp;

  /* The strip's column axis: the measured knuckle-to-knuckle span of the whole hand, which is what
     the documented 20 columns are ordered across (little -> thumb in the right-hand frame). */
  const littleKnuckle = at3('little_proximal'), thumbKnuckle = at3('thumb_proximal');
  const stripSpanM = littleKnuckle && thumbKnuckle ? norm(sub(thumbKnuckle, littleKnuckle)) : null;
  const stripAxis = littleKnuckle && thumbKnuckle ? unit(sub(thumbKnuckle, littleKnuckle)) : null;
  const columnPitchM = stripSpanM ? stripSpanM / (HAND_PRESSURE_GRID[1] - 1) : null;

  /* One path per finger, from its knuckle node to its tip. The metacarpal node sits at the wrist in
     this model (measured: wrist -> middle metacarpal 17.9 mm, middle metacarpal -> middle proximal
     73.8 mm), so the strip's rows belong on the phalanges and not on the palm bone. */
  const fingers = {};
  for (const [finger, chain] of HAND_CHAINS) {
    const path = chain.slice(1).map(at3);
    if (path.some((point) => !point)) { fingers[finger] = null; continue; }
    const segments = [];
    let lengthM = 0;
    for (let i = 0; i + 1 < path.length; i++) {
      const length = norm(sub(path[i + 1], path[i]));
      segments.push({ from: path[i], to: path[i + 1], length, start: lengthM });
      lengthM += length;
    }
    const bone = unit(sub(path[path.length - 1], path[0]));
    fingers[finger] = lengthM > 0 ? {
      lengthM, path,
      rowPitchM: lengthM / (HAND_PRESSURE_STRIP_ROWS[1] - HAND_PRESSURE_STRIP_ROWS[0] + 1),
      columnPitchM,
      lateral: stripAxis && bone ? perpendicular(stripAxis, bone) : null,
      pointAt(u) {
        const target = Math.min(1, Math.max(0, u)) * lengthM;
        let segment = segments[segments.length - 1];
        for (const candidate of segments) {
          if (target <= candidate.start + candidate.length) { segment = candidate; break; }
        }
        const t = segment.length > 0
          ? Math.min(1, Math.max(0, (target - segment.start) / segment.length)) : 0;
        return { point: lerp3(segment.from, segment.to, t), tangent: unit(sub(segment.to, segment.from)) };
      },
    } : null;
  }

  /* The palm plane: rows from the wrist to the middle knuckle (the palm bone measured on this
     pose), columns from the little to the thumb knuckle made perpendicular to the row axis. */
  const wrist = at3('wrist'), middleKnuckle = at3('middle_proximal');
  const palm = (() => {
    if (!wrist || !middleKnuckle || !stripAxis || !columnPitchM) return null;
    const rowVector = sub(middleKnuckle, wrist);
    const rowAxis = unit(rowVector);
    const colAxis = rowAxis ? perpendicular(stripAxis, rowAxis) : null;
    if (!rowAxis || !colAxis) return null;
    const lengthM = norm(rowVector);
    return {
      lengthM, rowAxis, columnPitchM, origin: wrist,
      rowBandPitchM: lengthM / HAND_PRESSURE_PALM_BANDS.rows,
      colBandPitchM: stripSpanM / HAND_PRESSURE_PALM_BANDS.cols,
      pointAt(rowFraction, colFraction) {
        return plus(plus(wrist, times(rowAxis, lengthM * rowFraction)),
          times(colAxis, (colFraction - 0.5) * stripSpanM));
      },
    };
  })();
  return { fingers, palm, columnPitchM, stripSpanM, available: !!(columnPitchM && palm) };
}
/* Every taxel at or above the threshold as one pad. Returns null only when the grid itself is
   absent; a frame with nothing above the threshold returns an empty pad list plus the numbers the
   read-out needs to say so, so an empty layer is never silently empty. */
function handPressurePads(names, positions, side, options = {}) {
  const plus = padAdd, times = padScale, sub = padSub;
  const {rawGrid = handPressureGrid(), baseline = handPressureBaseline(), rawCounts = false} = options;
  if (!Array.isArray(rawGrid) || rawGrid.length !== HAND_PRESSURE_GRID[0] ||
      !rawGrid.every(row => Array.isArray(row) && row.length === HAND_PRESSURE_GRID[1])) return null;
  const geometry = handPressurePadGeometry(names, positions);
  const left = side === 'left';
  const rawColumn = (c) => (left ? HAND_PRESSURE_GRID[1] - 1 - c : c);
  const palmRows = HAND_PRESSURE_PALM_ROWS[1] - HAND_PRESSURE_PALM_ROWS[0] + 1;
  const palmCols = HAND_PRESSURE_PALM_COLS[1] - HAND_PRESSURE_PALM_COLS[0] + 1;
  const stripRows = HAND_PRESSURE_STRIP_ROWS[1] - HAND_PRESSURE_STRIP_ROWS[0] + 1;
  const cells = palmRows * palmCols + stripRows * HAND_PRESSURE_GRID[1];
  const rawMax = handPressureGridMax(rawGrid);
  /* No saved rest baseline means this grid has no zero reference at all, so nothing may be called
     contact: the raw counts are still reported in words (the layer never hides load it can see), but
     no pad is drawn. A worn glove by itself reaches counts that a raw threshold reads as contact --
     measured on the 2026-09-19 rest capture, that was 21 taxels at threshold 9 with nothing touched. */
  if (!rawCounts && !baseline) {
    return { pads: [], available: false, baseline_ready: false, cells, raw_max: rawMax,
             baseline_max: null, max: 0, threshold: null, side: side || null, above: 0, drawn: 0,
             suppressed: 0, unplaceable: 0, palmMax: 0, palmBandsAbove: 0, palmPads: 0,
             fingerCounts: {}, top: null, labelled: 0 };
  }
  const displayGrid = rawCounts ? rawGrid : handPressureResidualGrid(rawGrid, baseline);
  const cell = (row, c) => {
    const value = Number(displayGrid[row][rawColumn(c)]);
    return isFinite(value) ? value : 0;
  };
  const palmGrid = displayGrid.slice(HAND_PRESSURE_PALM_ROWS[0], HAND_PRESSURE_PALM_ROWS[1] + 1)
    .map(row => left ? row.slice(-palmCols) : row.slice(0, palmCols));
  const stripGrid = displayGrid.slice(HAND_PRESSURE_STRIP_ROWS[0], HAND_PRESSURE_STRIP_ROWS[1] + 1);
  const max = handPressureGridMax([...palmGrid, ...stripGrid]);
  const threshold = rawCounts ? 1 : Math.max(HAND_PRESSURE_RESIDUAL_MIN_COUNT,
    Math.ceil(max * HAND_PRESSURE_PAD_MIN_FRACTION));
  const siteOf = (row, rawCol) => HAND_PRESSURE_SITES.find(
    (site) => site.row === row && rawCol >= site.cols[0] && rawCol <= site.cols[1]) || null;
  /* Metric radius scaled by the raw count, capped by the local taxel pitch so two neighbouring
     taxels stay two pads. */
  const radiusOf = (region, value, pitchM) => {
    const bounds = HAND_PRESSURE_PAD_RADIUS_M[region];
    const level = max > 0 ? Math.min(1, Math.max(0, value / max)) : 0;
    const cap = pitchM ? pitchM * HAND_PRESSURE_PAD_PITCH_FRACTION : bounds[1];
    return Math.min(bounds[0] + (bounds[1] - bounds[0]) * level, cap);
  };
  const mid = (bounds) => bounds[0] + (bounds[1] - bounds[0]) / 2;
  const candidates = [];
  let above = 0, unplaceable = 0;
  for (const [finger, columns] of Object.entries(HAND_PRESSURE_COLS)) {
    const fingerGeometry = geometry.fingers[finger];
    for (let row = HAND_PRESSURE_STRIP_ROWS[0]; row <= HAND_PRESSURE_STRIP_ROWS[1]; row++) {
      for (let c = columns[0]; c <= columns[1]; c++) {
        const value = cell(row, c);
        if (value < threshold) continue;
        above += 1;
        if (!fingerGeometry) { unplaceable += 1; continue; }
        const at = fingerGeometry.pointAt((row - HAND_PRESSURE_STRIP_ROWS[0] + 0.5) / stripRows);
        const centre = fingerGeometry.lateral
          ? plus(at.point, times(fingerGeometry.lateral,
              (c - mid(columns)) * fingerGeometry.columnPitchM))
          : at.point;
        const half = fingerGeometry.rowPitchM * 0.45;
        candidates.push({
          region: 'finger', finger, row, column: c, rawColumn: rawColumn(c), value,
          site: siteOf(row, rawColumn(c)), centre,
          axis: [sub(centre, times(at.tangent, half)), plus(centre, times(at.tangent, half))],
          radiusM: radiusOf('finger', value,
            Math.min(fingerGeometry.columnPitchM || Infinity, fingerGeometry.rowPitchM)),
        });
      }
    }
  }
  /* The palm's own display lattice: one pad per band, carrying that band's maximum. */
  const rowBands = handPressureBands(palmRows, HAND_PRESSURE_PALM_BANDS.rows);
  const colBands = handPressureBands(palmCols, HAND_PRESSURE_PALM_BANDS.cols);
  let palmMax = 0, palmBandsAbove = 0;
  for (const [r0, r1] of rowBands) {
    for (const [c0, c1] of colBands) {
      let value = 0;
      for (let row = r0; row <= r1; row++) {
        for (let col = c0; col <= c1; col++) {
          const count = cell(HAND_PRESSURE_PALM_ROWS[0] + row, HAND_PRESSURE_PALM_COLS[0] + col);
          value = Math.max(value, count);
          if (count >= threshold) above += 1;
        }
      }
      if (value > palmMax) palmMax = value;
      if (value < threshold) continue;
      palmBandsAbove += 1;
      if (!geometry.palm) { unplaceable += 1; continue; }
      const centre = geometry.palm.pointAt((mid([r0, r1]) + 0.5) / palmRows,
        (mid([c0, c1]) + 0.5) / palmCols);
      const half = geometry.palm.rowBandPitchM * 0.45;
      candidates.push({
        region: 'palm', finger: 'wrist', row: null, column: null, rawColumn: null,
        band: { rows: [r0 + HAND_PRESSURE_PALM_ROWS[0], r1 + HAND_PRESSURE_PALM_ROWS[0]], cols: [c0, c1] },
        value, site: null, centre,
        axis: [sub(centre, times(geometry.palm.rowAxis, half)),
               plus(centre, times(geometry.palm.rowAxis, half))],
        radiusM: radiusOf('palm', value,
          Math.min(geometry.palm.rowBandPitchM, geometry.palm.colBandPitchM)),
      });
    }
  }
  /* Strongest first: the cap keeps the strongest pads, and the number it dropped is reported
     instead of disappearing silently. */
  const ranked = candidates.slice().sort((a, b) => b.value - a.value);
  const pads = ranked.slice(0, HAND_PRESSURE_PAD_MAX);
  pads.forEach((pad, i) => { pad.label = i < HAND_PRESSURE_LABEL_MAX; });
  const fingerCounts = {};
  let palmPads = 0;
  for (const pad of pads) {
    if (pad.region === 'palm') { palmPads += 1; continue; }
    fingerCounts[pad.finger] = (fingerCounts[pad.finger] || 0) + 1;
  }
  return {
    pads, max, threshold, side: side || null, available: true, baseline_ready: Boolean(baseline),
    rawCounts,
    raw_max: rawMax, baseline_max: baseline ? handPressureGridMax(baseline) : null,
    cells, above, drawn: pads.length, suppressed: Math.max(0, ranked.length - pads.length),
    unplaceable, palmMax, palmBandsAbove, palmPads, fingerCounts,
    top: pads.length ? pads[0] : null,
    labelled: pads.filter((pad) => pad.label).length,
  };
}

const handView = { yaw: HAND_VIEW_HOME.yaw, pitch: HAND_VIEW_HOME.pitch, zoom: 1, drag: false, lastX: 0, lastY: 0, lastPose: null, legendBuilt: false,
                   frame: 'wrist_local', tactile: true, pressure: true, cam0: null, cam0Busy: false, cam0Error: null };
const clamp01 = (v) => v < 0 ? 0 : (v > 1 ? 1 : v);

/* Model -> screen. View basis: screen-right = +y (thumb side), screen-up = +z
   (fingers), depth = +x, so the camera sits on the -x side and looks at the
   PALM. Which side is the palm was measured, not assumed: under the joint ranges
   the shipped model declares for itself, flexion carries the fingertips towards
   -x (13 of the 14 flexion joints, mean x-component -0.66, and a human finger
   joint flexes a long way but hyperextends only ~15 deg, so the large end of
   each range is flexion), and flexion always moves a fingertip towards the palm.
   The thumb CMC sits at x = -17.9 mm in the same frame, i.e. also on the palm
   side. The earlier "+x is the palm normal" reading therefore showed the BACK of
   the hand, and a correct LEFT hand then read as a right hand (2026-09-21
   report). Mild perspective is a depth cue only; the metric scale stays uniform,
   so angles are not distorted. */
function handProjector() {
  const cosYaw = Math.cos(handView.yaw), sinYaw = Math.sin(handView.yaw);
  const cosPitch = Math.cos(handView.pitch), sinPitch = Math.sin(handView.pitch);
  const scale = Math.min(HAND_VIEW_W, HAND_VIEW_H) * 0.44 / HAND_REF_RADIUS * handView.zoom;
  const focal = 0.55;
  const [centerX, centerY, centerZ] = HAND_MODEL_CENTER;
  return function project(x, y, z) {
    const viewX = y - centerY, viewY = z - centerZ, viewZ = x - centerX;
    const x1 = viewX * cosYaw + viewZ * sinYaw, z1 = viewZ * cosYaw - viewX * sinYaw;
    const y2 = viewY * cosPitch - z1 * sinPitch, z2 = viewY * sinPitch + z1 * cosPitch;
    const k = focal / Math.max(focal * 0.3, focal + z2);
    /* Element 3 is this point's px-per-metre (scale x perspective). The cylinder pads need it to
       tell a foreshortened axis from a short one, and taking it from the projector keeps one
       geometry in the file instead of a second guess at the same rotation. */
    return [HAND_VIEW_W / 2 + x1 * k * scale, HAND_VIEW_H / 2 + 20 - y2 * k * scale, z2, k * scale];
  };
}

/* ---- camera-frame mode + EMF tactile layer (2026-09-22) ----
   The viewport below renders wrist_local only. Two additions live here:

   1. a cam0 mode: the same 25 nodes shown in the camera world frame through the backend
      convention p_cam0 = R_cam0_from_wrist @ p_wrist + t. Positions come from /api/handpose/replay
      (a recorded session's fitted T_cam<-wrist) or, when no session carries one, from
      /api/handpose/fixture, which is labelled synthetic. A missing transform stays missing: the
      viewport says so and draws nothing rather than falling back to the identity.
   2. an EMF tactile layer: finger_scores (per-finger median positive response over the configured
      contact sites) colour the finger chains and scale a halo at each tip. These are raw relative
      counts, not calibrated forces, and every label says so. */
function handFramePositions(pose) {
  const liveNames = (pose && pose.names) || [];
  const livePositions = (pose && pose.positions_local) || null;
  const liveOk = Array.isArray(livePositions) && livePositions.length === liveNames.length
    && liveNames.length > 0;
  if (handView.frame !== 'cam0') {
    return { names: liveNames, positions: liveOk ? livePositions : null, frame: 'wrist_local',
             source: 'bridge skeleton_pose', available: liveOk,
             reason: '等待 25 点骨骼数据（bridge skeleton_pose）' };
  }
  const cam0 = handView.cam0;
  const view = cam0 && cam0.views && cam0.views.cam0;
  if (!cam0) {
    return { names: [], positions: null, frame: 'cam0', source: null, available: false,
             reason: handView.cam0Error || '尚未加载相机系数据' };
  }
  if (!view || !view.available) {
    return { names: [], positions: null, frame: 'cam0', source: cam0.source, available: false,
             reason: (view && view.reason) || handView.cam0Error || 'cam0 位姿不可用' };
  }
  const names = (cam0.views.wrist_local && cam0.views.wrist_local.names) || liveNames;
  const positions = view.positions_cam0_m;
  return { names, positions, frame: 'cam0', source: cam0.source,
           available: Array.isArray(positions), reason: null,
           wristOrigin: view.wrist_origin_cam0_m, evidence: cam0.evidence, label: cam0.label };
}

function handTactileScores() {
  const glove = (state && state.glove) || {};
  const scores = glove.finger_scores;
  if (!scores) return null;
  const rows = {};
  let max = 0;
  for (const [chain, site] of Object.entries(HAND_TACTILE_SITE)) {
    const value = Number(scores[site]);
    if (!isFinite(value)) continue;
    rows[chain] = value;
    if (value > max) max = value;
  }
  if (!Object.keys(rows).length) return null;
  return { rows, max, baseline_ready: !!glove.baseline_ready,
           site_source: glove.touch_sites_source || null };
}

/* The glove's raw 23x20 grid on its own. It is a different part of the frame from the EMF finger
   pose block, so it stays live while that block is dead: measured on the running station
   2026-09-22, 203/460 cells non-zero with max 17, while valid_mask = 0 and skeleton_pose = None at
   the same moment. handPressurePads() above needs the 25 node positions on top of this, because it
   attaches the grid to a pose; this accessor is what the no-pose branch can still use. */
function handPressureGrid() {
  const grid = (state && state.glove && state.glove.pressure) || null;
  if (!Array.isArray(grid) || grid.length !== HAND_PRESSURE_GRID[0]) return null;
  if (!Array.isArray(grid[0]) || grid[0].length !== HAND_PRESSURE_GRID[1]) return null;
  return grid;
}

/* The resting zero reference for the grid above, captured by the operator's 「采集静置基线 · 3 秒」
   action and delivered with /api/state. It is the per-cell rest median in the *raw* frame (the
   left-hand column mirroring happens after the subtraction, exactly where the raw grid's own
   mirroring happens). Absent means absent: without it the layer cannot separate a worn glove's own
   offset from contact, and it says so instead of drawing that offset as load. */
function handPressureBaseline() {
  const baseline = (state && state.glove && state.glove.pressure_baseline) || null;
  if (!Array.isArray(baseline) || baseline.length !== HAND_PRESSURE_GRID[0]) return null;
  if (!Array.isArray(baseline[0]) || baseline[0].length !== HAND_PRESSURE_GRID[1]) return null;
  return baseline;
}

/* Raw grid minus the resting baseline, clamped at zero, in the raw frame. Measured on the recorded
   2026-09-19 rest capture (glove worn, nothing touched): the raw grid reached 59 with 58 cells at or
   above 2, so a threshold on raw counts drew contact pads on a hand that was touching nothing. */
function handPressureResidualGrid(grid = handPressureGrid(), baseline = handPressureBaseline()) {
  if (!grid || !baseline) return null;
  return grid.map((row, r) => row.map(
    (value, c) => Math.max(0, (Number(value) || 0) - (Number(baseline[r][c]) || 0))));
}

/* Largest raw count in the grid. The grid stays live while the EMF finger pose block is dead, so the
   no-pose branch has to be able to say how much load it is carrying in words: the 3D layer cannot be
   drawn without nodes, and a flat grid is not what this viewport is for. */
function handPressureGridMax(grid) {
  let maximum = 0;
  for (const row of grid) {
    for (const value of row) {
      const number = Number(value);
      if (isFinite(number) && number > maximum) maximum = number;
    }
  }
  return maximum;
}

/* The pose the page draws is only as live as the last skeleton_pose it received. The
   bridge emits one only when a glove frame reaches it, and the station forwards a frame
   only when all five EMF finger poses are valid, so a dead EMF block freezes the pose
   without any error. Comparing the pose's own timestamp with the wall clock is what makes
   that visible; a frozen pose is otherwise indistinguishable from a still hand. */
const HAND_POSE_STALE_SEC = 2.0;
function handPoseAge(pose) {
  const stamp = pose ? Number(pose.timestamp_sec) : NaN;
  if (!isFinite(stamp) || stamp <= 0) return { age_sec: null, stale: false };
  const age = Date.now() / 1000 - stamp;
  return { age_sec: age, stale: age > HAND_POSE_STALE_SEC };
}

/* Why the pose is frozen, read from the glove frame itself rather than guessed.
   Two different faults look identical from the pose alone: the EMF finger block
   never streaming at all (both sequence counters still 0) and frames arriving with
   no valid finger. The counters separate them, so both are reported. The station's
   own status block is quoted as well, because it says 'ready' and reports no error
   in either case -- nothing there flags the dropped frame, which is what sends the
   operator looking in the wrong place. */
function handPoseStallReason() {
  const glove = (state && state.glove) || {};
  const emf = glove.emf;
  if (!emf || !Array.isArray(emf.fingers)) return null;
  const valid = emf.fingers.filter((f) => f.valid).length;
  if (valid === emf.fingers.length) return null;
  const streamSeq = Number(emf.stream_sequence);
  const frameSeq = Number(emf.frame_sequence);
  const streamed = streamSeq > 0 || frameSeq > 0;
  const skeletonError = glove.skeleton_error;
  return 'EMF 位姿块 valid_mask=' + Number(emf.valid_mask)
    + '（' + valid + '/' + emf.fingers.length + ' 指有效，stream_sequence=' + streamSeq
    + '，frame_sequence=' + frameSeq + '）：'
    + (streamed
        ? '帧在到达，但没有任何一指有效；'
        : '两个序列号都还是 0，即该指位姿流从未推进（不是逐帧有效性失败）；')
    + 'retarget 门控要求五指全部有效，因此帧被丢弃，bridge 不再输出 skeleton_pose。'
    + '站内状态块不报此故障（retargeting_status=' + (glove.retargeting_status || '未连接')
    + '，skeleton_error=' + (skeletonError === null || skeletonError === undefined
        ? 'None' : String(skeletonError)) + '）。';
}

/* Cool -> warm ramp: one hue would hide which finger is loaded, and the scale bar below the
   canvas repeats the mapping as a number so colour is never the only carrier. */
function tactileColor(value, max) {
  const t = max > 0 ? clamp01(value / max) : 0;
  const r = Math.round(64 + 191 * t), g = Math.round(140 - 78 * t), b = Math.round(228 - 186 * t);
  return 'rgb(' + r + ',' + g + ',' + b + ')';
}

async function loadHandCam0(force) {
  if (handView.cam0Busy) return;
  if (handView.cam0 && !force) return;
  handView.cam0Busy = true;
  renderHandCamBanner('正在读取会话报告与相机系数据…');
  try {
    let replay = null, replayError = null;
    try { replay = await api('/api/handpose/replay'); } catch (e) { replayError = e.message; }
    const replayViews = replay && replay.views;
    if (replayViews && replayViews.available) {
      handView.cam0 = { source: 'replay', evidence: replay.evidence, label: replay.label,
                        report_path: replay.report_path, views: replayViews.views,
                        stereo: replay.stereo, transform_series: replay.transform_series,
                        registration_source: replay.registration_source, note: null };
    } else {
      const fixture = await api('/api/handpose/fixture');
      handView.cam0 = { source: 'fixture', evidence: fixture.evidence, label: fixture.label,
                        report_path: replay && replay.report_path, views: fixture.views,
                        stereo: fixture.stereo,
                        registration_source: fixture.registration_source,
                        transform_series: replay && replay.transform_series,
                        note: (replayViews && replayViews.reason)
                          || (replay && replay.transform_series && replay.transform_series.reason)
                          || replayError };
    }
    handView.cam0Error = null;
  } catch (e) {
    handView.cam0Error = e.message;
  } finally {
    handView.cam0Busy = false;
    renderHandCamBanner();
    redrawHand();
  }
}

function renderHandCamBanner(progress) {
  const box = $('handCamBanner');
  const tag = $('handFrameTag');
  if (tag) tag.textContent = handView.frame === 'cam0' ? 'cam0' : 'wrist_local';
  if (!box) return;
  if (handView.frame !== 'cam0') { box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  const cam0 = handView.cam0;
  const view = cam0 && cam0.views && cam0.views.cam0;
  const usable = !!(view && view.available);
  const parts = ['坐标系 <b>cam0</b>：p_cam0 = R_cam0_from_wrist @ p_wrist + t'
    + '（t 是腕原点在 cam0 中的位置）。'];
  if (progress) {
    parts.push(progress);
  } else if (usable) {
    if (cam0.source === 'replay') {
      parts.push('数据来源：<b>会话报告回放</b>（' + (cam0.label || '未命名报告') + '）。');
    } else {
      parts.push('数据来源：<b>合成 fixture</b>（只验证 wrist_local → cam0 → 双眼鱼眼投影这条'
        + '软件链路，不是硬件、标定或精度证据）。');
      if (cam0.note) parts.push('会话报告未提供 T_cam<-wrist(t)：' + cam0.note);
    }
    const origin = (view.wrist_origin_cam0_m || []).map((v) => Number(v).toFixed(3));
    if (origin.length === 3) parts.push('腕原点 cam0 = ' + origin.join(', ') + ' m。');
    const px = ((cam0.views.stereo_overlay || {}).cam0_px) || [];
    const visible = px.filter((p) => p && p[0] != null).length;
    parts.push('双眼可见节点 ' + visible + '/' + px.length
      + '（画幅外或相机背后的点记为无效，不做边缘截断）。');
  } else {
    parts.push('cam0 位姿<b>不可用</b>：'
      + ((view && view.reason) || handView.cam0Error || '尚未加载')
      + '。缺少 T_cam<-wrist 时此处保持缺失，不会用单位矩阵顶替。');
  }
  box.innerHTML = parts.join(' ');
  box.classList.toggle('warn', !usable);
}

function redrawHand() {
  safeRender('手部骨骼可视化', () => {
    drawHandSkeleton(handView.lastPose);
    renderHandReadout(handView.lastPose);
  });
  safeRender('双眼投影检查', () => drawHandStereo());
}

function bindHandFrameControls() {
  const select = $('handFrameSelect');
  if (select) select.addEventListener('change', () => {
    handView.frame = select.value === 'cam0' ? 'cam0' : 'wrist_local';
    if (handView.frame === 'cam0' && !handView.cam0 && !handView.cam0Busy) loadHandCam0(false);
    else { renderHandCamBanner(); redrawHand(); }
  });
  const tactile = $('handTactileToggle');
  if (tactile) {
    handView.tactile = tactile.checked;
    tactile.addEventListener('change', () => { handView.tactile = tactile.checked; redrawHand(); });
  }
  const pressure = $('handPressureToggle');
  if (pressure) {
    handView.pressure = pressure.checked;
    pressure.addEventListener('change', () => { handView.pressure = pressure.checked; redrawHand(); });
  }
  const reload = $('handCamReload');
  if (reload) reload.addEventListener('click', () => loadHandCam0(true));
  renderHandCamBanner();
}

/* Left/right fisheye projection check: where the 25 nodes land in each eye, drawn on the two
   sensor rectangles. The projection comes from the backend through the device's own
   kannala_brandt model; a node behind or outside the image plane arrives as null and is counted,
   never clamped to the border. */
function drawHandStereo() {
  const cv = $('handStereoCanvas');
  if (!cv) return;
  const ctx = cv.getContext('2d');
  const W = HAND_STEREO_W, H = HAND_STEREO_H;
  ctx.setTransform(HAND_STEREO_DPR, 0, 0, HAND_STEREO_DPR, 0, 0);
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#0b0e11'; ctx.fillRect(0, 0, W, H);
  ctx.font = '10px sans-serif';
  const note = $('handStereoNote');
  const cam0 = handView.cam0;
  const overlay = cam0 && cam0.views && cam0.views.stereo_overlay;
  const names = (cam0 && cam0.views && cam0.views.wrist_local
    && cam0.views.wrist_local.names) || [];
  const stereo = (cam0 && cam0.stereo) || null;
  if (!overlay || !overlay.available || !stereo || !names.length) {
    ctx.fillStyle = '#93a3b0';
    ctx.fillText('双眼投影不可用：'
      + ((overlay && overlay.reason) || handView.cam0Error || '尚未加载相机系数据'), 10, 20);
    if (note) note.textContent = '投影需要 T_cam<-wrist 与设备上报的鱼眼内参同时存在；缺失时不做近似。';
    return;
  }
  const gap = 12;
  const boxW = (W - gap * 3) / 2;
  const panels = [['cam0（左眼）', 'cam0_px', stereo.camera0],
                  ['cam1（右眼）', 'cam1_px', stereo.camera1]];
  const counts = [];
  panels.forEach((panel, index) => {
    const [title, key, camera] = panel;
    const x0 = gap + index * (boxW + gap), y0 = 22;
    const width = Number(camera.width) || 1920, height = Number(camera.height) || 1080;
    const boxH = Math.min(H - y0 - 8, boxW * (height / width));
    ctx.strokeStyle = '#25303a'; ctx.lineWidth = 1;
    ctx.strokeRect(x0, y0, boxW, boxH);
    ctx.fillStyle = '#6c7b88';
    ctx.fillText(title + ' · ' + width + '×' + height + ' · fx ' + Number(camera.fx).toFixed(1)
      + ' · ' + (camera.distortion_model || '—'), x0, y0 - 6);
    const pixels = overlay[key] || [];
    let inside = 0, outside = 0;
    pixels.forEach((point, i) => {
      const name = names[i] || '';
      if (!point || point[0] == null || point[1] == null) { outside += 1; return; }
      if (point[0] < 0 || point[0] > width || point[1] < 0 || point[1] > height) {
        outside += 1; return;
      }
      inside += 1;
      const finger = HAND_NODE_FINGER[name] || 'wrist';
      ctx.fillStyle = HAND_COLORS[finger] || '#93a3b0';
      ctx.beginPath();
      ctx.arc(x0 + point[0] / width * boxW, y0 + point[1] / height * boxH,
        name === 'wrist' ? 3.6 : 2.2, 0, Math.PI * 2);
      ctx.fill();
      if (name === 'wrist') { ctx.strokeStyle = '#e7edf2'; ctx.lineWidth = 1.2; ctx.stroke(); }
    });
    counts.push(title.slice(0, 4) + ' 画幅内 ' + inside + ' · 画幅外/无效 ' + outside);
  });
  if (note) {
    note.textContent = counts.join(' ｜ ')
      + '（设备上报 kannala_brandt 鱼眼投影；相机背后的点记为无效，不截断到边缘）'
      + (cam0 && cam0.source === 'fixture'
        ? ' · 当前为合成 fixture：手形是合成替身，节点像素不对应你的手' : '');
  }
}

function drawHandSkeleton(pose) {
  const cv = $('handSkeletonCanvas');
  if (!cv) return;
  handView.lastPose = pose;
  const ctx = cv.getContext('2d');
  const W = HAND_VIEW_W, H = HAND_VIEW_H;
  ctx.setTransform(HAND_VIEW_DPR, 0, 0, HAND_VIEW_DPR, 0, 0);
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = '#0b0e11'; ctx.fillRect(0, 0, W, H);
  ctx.font = '11px sans-serif';

  const frame = handFramePositions(pose);
  const names = frame.names;
  const positions = frame.positions;
  const tactile = handView.tactile ? handTactileScores() : null;
  const count = Array.isArray(names) ? names.length : 0;
  if (!frame.available || !count || !Array.isArray(positions) || positions.length !== count) {
    ctx.fillStyle = '#93a3b0';
    ctx.fillText(frame.frame === 'cam0' ? 'cam0 相机世界系位姿不可用'
      : '等待 25 点骨骼数据（bridge skeleton_pose）', 12, 22);
    ctx.fillStyle = '#6c7b88';
    if (frame.frame === 'cam0') {
      ctx.fillText(frame.reason || '缺少 T_cam<-wrist：缺失就是缺失，不用单位矩阵顶替。', 12, 40);
      ctx.fillText('相机系位姿需要一个已拟合的 T_cam<-wrist(t)：'
        + '会话报告的 OpenXR 半程，或 AprilTag 标定。', 12, 56);
    } else {
      ctx.fillText(lastCalibPhase === 'reused'
        ? '官方输出已启用（使用上次标定成果，未重新采集）：等待第一帧有效手套数据。'
        : lastCalibPhase === 'complete'
          ? '官方输出已启用（本次已重新采集）：等待第一帧有效手套数据。'
          : lastCalibPhase === 'absent'
            ? '尚无标定记录：请点击「开始 / 重新 external 标定」完成一次标定。'
            : '需要 skeleton_api=true 且完成 glove.left → hand.openxr_hand.left 标定。', 12, 40);
    }
    /* "No pose at all" and "frozen pose" are the same fault seen at different times: the EMF
       gate drops every frame before the bridge can emit one. Saying so here is what keeps this
       branch from reading as a page that is merely still waiting. */
    const stallReason = handPoseStallReason();
    if (stallReason) {
      ctx.fillStyle = '#ffb74d';
      ctx.fillText('⚠ 位姿停更原因（不是仍在等待）：', 12, frame.frame === 'cam0' ? 72 : 58);
      ctx.fillStyle = '#6c7b88';
      ctx.fillText(stallReason, 12, frame.frame === 'cam0' ? 86 : 72);
    }
    /* The pressure grid comes from a different part of the frame than the EMF pose block, so it can
       be updating right here while this branch is taken. Saying so in words keeps that split visible
       without drawing a flat grid in the viewport whose whole point is the 3D pose: the pad layer
       needs node positions and there are none, so this branch states the load it cannot draw. */
    const rawGrid = handView.pressure ? handPressureGrid() : null;
    if (rawGrid) {
      const residual = handPressureBaseline() ? handPressureResidualGrid() : null;
      ctx.fillStyle = '#6c7b88';
      ctx.fillText('压感：23×20 网格仍在更新（原始最大 ' + handPressureGridMax(rawGrid)
        + (residual ? ' · 减静置基线后最大 ' + handPressureGridMax(residual) : ' · 未采集静置基线')
        + '），但无位姿节点可贴 → 3D 圆垫图层画不出。', 12,
      frame.frame === 'cam0' ? 104 : 90);
    }
    return;
  }

  const index = new Map(names.map((name, i) => [name, i]));
  const project = handProjector();
  const projected = positions.map(p => project(Number(p[0]) || 0, Number(p[1]) || 0, Number(p[2]) || 0));
  const at = (name) => { const i = index.get(name); return i == null ? null : projected[i]; };
  const wrist = at('wrist');
  /* Far-to-near once: the bone pass, the pressure layer and the node pass all need the same
     ordering, and drawing all three in that one order is what makes them read as one scene. */
  const nodes = names.map((name, i) => ({ name, p: projected[i], finger: HAND_NODE_FINGER[name] || 'wrist' }))
    .sort((a, b) => b.p[2] - a.p[2]);

  // Metric reference: polar grid in the wrist plane (model z = 0), 5/10/15 cm.
  ctx.strokeStyle = '#1b2733'; ctx.lineWidth = 1;
  for (const radius of [0.05, 0.10, 0.15]) {
    ctx.beginPath();
    for (let step = 0; step <= 48; step++) {
      const angle = step / 48 * Math.PI * 2;
      const q = project(Math.cos(angle) * radius, Math.sin(angle) * radius, 0);
      step ? ctx.lineTo(q[0], q[1]) : ctx.moveTo(q[0], q[1]);
    }
    ctx.stroke();
  }
  if (wrist) {
    ctx.strokeStyle = '#16202a';
    for (let spoke = 0; spoke < 8; spoke++) {
      const angle = spoke / 8 * Math.PI * 2;
      const q = project(Math.cos(angle) * 0.15, Math.sin(angle) * 0.15, 0);
      ctx.beginPath(); ctx.moveTo(wrist[0], wrist[1]); ctx.lineTo(q[0], q[1]); ctx.stroke();
    }
  }

  // Farther bones first, so nearer ones overlay them and the pose reads in 3D.
  const depthAlpha = (depth) => 0.95 - 0.4 * clamp01((depth + 0.05) / 0.10);
  const bones = HAND_BONES.map(([from, to, finger]) => {
    const pa = at(from), pb = at(to);
    return pa && pb ? { finger, pa, pb, depth: (pa[2] + pb[2]) / 2 } : null;
  }).filter(Boolean).sort((a, b) => b.depth - a.depth);
  ctx.lineCap = 'round'; ctx.lineWidth = 2.6;
  for (const bone of bones) {
    ctx.globalAlpha = depthAlpha(bone.depth);
    ctx.strokeStyle = HAND_COLORS[bone.finger] || '#93a3b0';
    ctx.beginPath(); ctx.moveTo(bone.pa[0], bone.pa[1]); ctx.lineTo(bone.pb[0], bone.pb[1]); ctx.stroke();
  }

  // EMF tactile layer: a loaded finger is redrawn over its own wireframe, thicker with the
  // response, and a halo plus the number at its tip carries the same value. The base wireframe
  // stays visible underneath so the layer never hides which finger is which.
  if (tactile) {
    const span = Math.max(1, tactile.max);
    for (const bone of bones) {
      const value = tactile.rows[bone.finger];
      if (value == null || value <= 0) continue;
      ctx.globalAlpha = 0.88;
      ctx.strokeStyle = tactileColor(value, span);
      ctx.lineWidth = 2.6 + 3.6 * clamp01(value / span);
      ctx.beginPath(); ctx.moveTo(bone.pa[0], bone.pa[1]); ctx.lineTo(bone.pb[0], bone.pb[1]); ctx.stroke();
    }
    ctx.lineWidth = 1.6; ctx.font = '10px sans-serif';
    for (const [finger, tip] of HAND_TIPS) {
      const value = tactile.rows[finger];
      const point = at(tip);
      if (value == null || !point) continue;
      const t = clamp01(value / span);
      ctx.globalAlpha = 0.92;
      ctx.strokeStyle = tactileColor(value, span);
      ctx.beginPath(); ctx.arc(point[0], point[1], 5 + 10 * t, 0, Math.PI * 2); ctx.stroke();
      ctx.globalAlpha = 0.8;
      ctx.fillStyle = tactileColor(value, span);
      ctx.textAlign = 'center';
      ctx.fillText(Math.round(value), point[0], point[1] - 9 - 10 * t);
      ctx.textAlign = 'left';
    }
    ctx.globalAlpha = 1;
  }

  /* EMF pressure layer: the glove's own 23x20 grid, attached to the pose by the taxel mapping above
     and drawn as pads in this same projection, so the load sits on the hand instead of in a flat
     inset. Drawn before the node circles so a loaded node still reads as a node. */
  const baseline = handPressureBaseline();
  const pressure = handView.pressure
    ? handPressurePads(names, positions, pose && pose.hand_side, {baseline, rawCounts: !baseline})
    : null;
  if (pressure) drawPressurePadLayer(ctx, pressure, { project });

  ctx.font = '10px sans-serif';
  for (const node of nodes) {
    const color = HAND_COLORS[node.finger] || '#93a3b0';
    const isWrist = node.name === 'wrist';
    const isTip = HAND_TIP_NAMES.has(node.name);
    ctx.globalAlpha = depthAlpha(node.p[2]);
    ctx.beginPath();
    ctx.arc(node.p[0], node.p[1], isWrist ? 5.2 : (isTip ? 4.1 : 3.1), 0, Math.PI * 2);
    ctx.fillStyle = color; ctx.fill();
    if (isWrist || isTip) {
      ctx.lineWidth = 1.3; ctx.strokeStyle = isWrist ? '#e7edf2' : color; ctx.stroke();
      const right = node.p[0] < W / 2;
      ctx.fillStyle = '#93a3b0';
      ctx.textAlign = right ? 'left' : 'right';
      ctx.fillText(isWrist ? '腕 0,0,0' : HAND_FINGER_ZH[node.finger] + '尖',
        node.p[0] + (right ? 9 : -9), node.p[1] + 3);
      ctx.textAlign = 'left';
    }
  }
  ctx.globalAlpha = 1;

  // Wrist frame triad: the three real model axes, 5 cm each.
  if (wrist) {
    ctx.lineWidth = 1.6; ctx.font = '10px sans-serif';
    for (const [ax, ay, az, color, label] of [
      [0.05, 0, 0, '#ef5350', '+x'], [0, 0.05, 0, '#66bb6a', '+y'], [0, 0, 0.05, '#4fc3f7', '+z']]) {
      const q = project(ax, ay, az);
      ctx.globalAlpha = 0.7; ctx.strokeStyle = color;
      ctx.beginPath(); ctx.moveTo(wrist[0], wrist[1]); ctx.lineTo(q[0], q[1]); ctx.stroke();
      ctx.fillStyle = color; ctx.fillText(label, q[0] + 3, q[1] - 2);
    }
    ctx.globalAlpha = 1;
  }

  const side = pose && pose.hand_side === 'left' ? '左'
    : (pose && pose.hand_side === 'right' ? '右' : ((pose && pose.hand_side) || '—'));
  const stamp = pose ? (Number(pose.timestamp_sec) || 0).toFixed(2) + ' s' : '—';
  const age = handPoseAge(pose);
  ctx.fillStyle = '#93a3b0';
  ctx.fillText('25 点骨骼 · ' + frame.frame + ' · 单位 m · ' + side + '手 · t=' + stamp
    + (frame.source ? ' · 来源 ' + frame.source : ''), 10, 16);
  /* A frozen pose and a still hand look identical in the drawing, so the age is printed and
     the cause is named whenever the pose is older than the poll can explain. */
  if (age.stale) {
    ctx.fillStyle = '#ffb74d';
    ctx.fillText('⚠ 位姿已冻结 ' + age.age_sec.toFixed(1) + ' s：画面是上一次 bridge 输出，不是当前手形。', 10, 30);
    const stall = handPoseStallReason();
    if (stall) { ctx.fillStyle = '#6c7b88'; ctx.fillText(stall, 10, 44); }
  }
  if (frame.frame === 'cam0') {
    ctx.fillStyle = '#6c7b88';
    ctx.fillText('三色轴线 = cam0 坐标轴画在腕原点处 · 相机原点即 (0, 0, 0) · 腕原点 '
      + (frame.wristOrigin || []).map((v) => Number(v).toFixed(3)).join(', ') + ' m', 10, age.stale ? 58 : 30);
  }
  /* A synthetic stand-in must never be read as the operator's own hand. The banner above the canvas
     says where the data came from, but the drawing is what the eye reads, so the canvas says it too,
     including that the EMF layers drawn on it carry live numbers on a synthetic shape. Kept to short
     lines: the canvas does not wrap text. */
  if (frame.frame === 'cam0' && frame.source === 'fixture') {
    const y0 = age.stale ? 72 : 44;
    ctx.fillStyle = '#ffb74d';
    ctx.fillText('⚠ 合成手形（非实时手）：cam0 位姿来自 fixture，不是你的手。', 10, y0);
    ctx.fillStyle = '#6c7b88';
    ctx.fillText('EMF 数值实时，但贴在该合成手形上：位置不对应真实手。', 10, y0 + 14);
    ctx.fillText('真实 cam0 位姿需要已拟合的 T_cam<-wrist（AprilTag 标定或 OpenXR 骨骼会话）。', 10, y0 + 28);
  }
  ctx.fillStyle = '#6c7b88';
  if (handView.pressure) {
    /* Three short lines instead of one long one: the canvas does not wrap text, so a long caption is
       silently clipped at the right edge. The counts, the threshold and what the threshold dropped
       are stated here and in full in the read-out. The threshold is applied to the *residual* (raw
       minus the saved rest baseline), so the caption names which of the two it is showing. */
    ctx.fillText(pressure
      ? (pressure.rawCounts
        ? '原始压感计数（含静置底噪）· 峰值 ' + pressure.raw_max + ' · ' + pressure.drawn + ' 个显示点'
        : '压感圆垫（相对静置基线的计数增量）· 阈值 ≥' + pressure.threshold
          + ' · ' + pressure.drawn + ' 个显示点')
      : '压感图层：等待 23×20 原始压感网格。', 10, H - 52);
    ctx.fillText('指条行 ' + HAND_PRESSURE_STRIP_ROWS[0] + '–' + HAND_PRESSURE_STRIP_ROWS[1]
      + '（6 行 × 4 列）→ 各指自身骨段，行 ' + HAND_PRESSURE_STRIP_ROWS[0] + ' = 指根；掌区行 '
      + HAND_PRESSURE_PALM_ROWS[0] + '–' + HAND_PRESSURE_PALM_ROWS[1] + ' → '
      + HAND_PRESSURE_PALM_BANDS.rows + '×' + HAND_PRESSURE_PALM_BANDS.cols + ' 显示格', 10, H - 38);
    ctx.fillText('taxel 米制坐标厂商未文档化，铺开是显示约定；半径 '
      + (HAND_PRESSURE_PAD_RADIUS_M.finger[0] * 1000) + '–'
      + (HAND_PRESSURE_PAD_RADIUS_M.palm[1] * 1000) + ' mm 随计数并受 taxel 间距封顶。', 10, H - 24);
  }
  ctx.fillText('腕平面刻度 5/10/15 cm · yaw ' + (handView.yaw * 180 / Math.PI).toFixed(0) +
    '° pitch ' + (handView.pitch * 180 / Math.PI).toFixed(0) + '° · ' + handView.zoom.toFixed(2) + '×', 10, H - 10);
  drawTactileScale(ctx, W, H, tactile, pressure);
}

/* One pad: a cylinder between two projected end centres, with its cap ellipse squashed by the axis's
   own foreshortening. The projector reports each point's px-per-metre, so sin(theta) = projected
   length / (true length x px-per-metre) is read straight off it: the cap closes to an edge when the
   cylinder points at the camera and opens to a full circle when it points away. */
function drawPressureCylinder(ctx, a, b, lengthM, radiusPx, color, alpha) {
  const dx = b[0] - a[0], dy = b[1] - a[1];
  const lengthPx = Math.hypot(dx, dy);
  const scale = (Math.abs(a[3]) + Math.abs(b[3])) / 2 || 1;
  const sin = lengthM > 0 ? Math.min(1, lengthPx / (lengthM * scale)) : 1;
  const squash = Math.sqrt(Math.max(0, 1 - sin * sin));
  const angle = lengthPx > 0.01 ? Math.atan2(dy, dx) : 0;
  const perp = lengthPx > 0.01 ? [-dy / lengthPx, dx / lengthPx] : [0, 1];
  ctx.save();
  // body: the silhouette between the two caps
  ctx.globalAlpha = alpha;
  ctx.beginPath();
  ctx.moveTo(a[0] + perp[0] * radiusPx, a[1] + perp[1] * radiusPx);
  ctx.lineTo(b[0] + perp[0] * radiusPx, b[1] + perp[1] * radiusPx);
  ctx.lineTo(b[0] - perp[0] * radiusPx, b[1] - perp[1] * radiusPx);
  ctx.lineTo(a[0] - perp[0] * radiusPx, a[1] - perp[1] * radiusPx);
  ctx.closePath();
  ctx.fillStyle = color; ctx.fill();
  ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.stroke();
  // far cap first, near cap over it, so the tube reads as a body with a direction
  for (const [end, cap] of [[a, 0.55], [b, 1]]) {
    ctx.globalAlpha = Math.min(1, alpha * cap);
    ctx.beginPath();
    ctx.ellipse(end[0], end[1], radiusPx, radiusPx * squash, angle, 0, Math.PI * 2);
    ctx.fillStyle = color; ctx.fill();
  }
  // a highlight along one side: the shading that makes a flat silhouette read as a tube
  ctx.globalAlpha = Math.min(1, alpha * 0.45);
  ctx.strokeStyle = '#ffffff';
  ctx.lineCap = 'round';
  ctx.lineWidth = Math.max(1, radiusPx * 0.45);
  ctx.beginPath();
  ctx.moveTo(a[0] + perp[0] * radiusPx * 0.35, a[1] + perp[1] * radiusPx * 0.35);
  ctx.lineTo(b[0] + perp[0] * radiusPx * 0.35, b[1] + perp[1] * radiusPx * 0.35);
  ctx.stroke();
  ctx.restore();
}

/* The glove's own 23x20 pressure frame, attached to the pose and drawn in the same projection as
   the wireframe. Every taxel above the threshold becomes one short cylinder lying on the bone it
   belongs to (handPressurePads above places it), so the load sits on the hand in 3D instead of in a
   flat inset and several contact sites on one finger stay several pads. Radius and colour carry the
   raw count, the depth alpha and the far-to-near order come from the same projector as the
   wireframe, and the strongest pads print their number so colour is never the only carrier. Nothing
   is drawn for a taxel at or below the threshold: a zero-count tube on a clean wireframe is not a
   measurement. Raw counts from the glove, never converted to force. */
function drawPressurePadLayer(ctx, field, geometry) {
  const span = Math.max(1, field.max);
  const project = geometry.project;
  ctx.save();
  ctx.font = '10px sans-serif';
  const drawn = field.pads.map((pad) => {
    const a = project(pad.axis[0][0], pad.axis[0][1], pad.axis[0][2]);
    const b = project(pad.axis[1][0], pad.axis[1][1], pad.axis[1][2]);
    const centre = project(pad.centre[0], pad.centre[1], pad.centre[2]);
    return { pad, a, b, centre, depth: centre[2] };
  }).sort((one, two) => two.depth - one.depth);
  for (const item of drawn) {
    const { pad, a, b, centre } = item;
    const level = clamp01(pad.value / span);
    const lengthM = Math.hypot(pad.axis[1][0] - pad.axis[0][0], pad.axis[1][1] - pad.axis[0][1],
      pad.axis[1][2] - pad.axis[0][2]);
    const scale = (Math.abs(a[3]) + Math.abs(b[3])) / 2 || 1;
    const near = 1 - clamp01((centre[2] + 0.05) / 0.10);
    drawPressureCylinder(ctx, a, b, lengthM, Math.max(1.4, pad.radiusM * scale),
      tactileColor(pad.value, span), Math.min(0.95, 0.34 + 0.44 * level + 0.14 * near));
    if (!pad.label) continue;
    ctx.globalAlpha = 0.95;
    ctx.fillStyle = '#e7edf2';
    ctx.fillText(String(Math.round(pad.value)), centre[0] + 6, centre[1] - 6);
  }
  ctx.restore();
}

/* Colour scale for the tactile layers, with the numbers spelled out so the colour is not the only
   carrier of the value. The pressure scale is a *residual* scale (raw counts minus the saved rest
   baseline), so its label names the baseline; without a baseline the pressure layer draws nothing
   and the scale says that instead of scaling raw counts that include the resting offset. */
function drawTactileScale(ctx, W, H, tactile, pressure) {
  /* Raised to H - 86 to leave the three pressure caption lines below it, so no line is clipped at
     the canvas edge. */
  const x = 12, y = H - 86, width = 170, height = 10;
  ctx.save();
  ctx.font = '10px sans-serif';
  const scale = pressure ? {max: pressure.max, label: pressure.rawCounts
    ? '触觉点 · 原始计数（含静置底噪）'
    : '触觉点 · 相对静置基线的计数增量'}
    : (tactile ? {max: tactile.max, label: 'EMF 触感 · 相对静置基线的计数增量'} : null);
  if (!scale || scale.max == null) {
    ctx.fillStyle = '#6c7b88';
    ctx.fillText(scale ? scale.label
      : (handView.tactile || handView.pressure
        ? 'EMF 触感 / 压感图层：等待 finger_scores 或 23×20 压感网格'
        : 'EMF 触感与压感图层已关闭'), x, y);
    ctx.restore();
    return;
  }
  ctx.fillStyle = '#6c7b88';
  ctx.fillText(scale.label, x, y - 4);
  const span = Math.max(1, scale.max);
  for (let i = 0; i < width; i++) {
    ctx.fillStyle = tactileColor(span * i / width, span);
    ctx.fillRect(x + i, y, 1, height);
  }
  ctx.strokeStyle = '#25303a'; ctx.lineWidth = 1;
  ctx.strokeRect(x, y, width, height);
  ctx.fillStyle = '#93a3b0';
  ctx.fillText('0', x, y + height + 11);
  ctx.textAlign = 'right';
  ctx.fillText(String(Math.round(scale.max)), x + width, y + height + 11);
  ctx.textAlign = 'left';
  ctx.restore();
}

/* Legend and numeric read-out for the skeleton viewport. Everything shown here
   is derived from the received skeleton_pose only. */
function buildHandLegend() {
  const box = $('handLegend');
  if (!box || handView.legendBuilt) return;
  box.replaceChildren();
  for (const key of ['wrist', 'thumb', 'index', 'middle', 'ring', 'little']) {
    const row = document.createElement('div');
    const swatch = document.createElement('span');
    swatch.className = 'swatch'; swatch.style.background = HAND_COLORS[key];
    const text = document.createElement('span');
    text.textContent = HAND_FINGER_ZH[key];
    row.append(swatch, text);
    box.append(row);
  }
  handView.legendBuilt = true;
}

function renderHandReadout(pose) {
  const el = $('handReadout');
  if (!el) return;
  const frame = handFramePositions(pose);
  const cam0 = handView.cam0;
  const names = frame.names, positions = frame.positions;
  const count = Array.isArray(names) ? names.length : 0;
  if (!frame.available || !count || !Array.isArray(positions) || positions.length !== count) {
    /* The never-arrived case needs the same explanation as the frozen one: on its own this
       text is indistinguishable from a page that is simply still waiting. */
    const stallReason = handPoseStallReason();
    el.textContent = (frame.frame === 'cam0'
      ? '相机世界系（cam0）位姿不可用：' + (frame.reason || '缺少 T_cam<-wrist')
      : '等待 25 点骨骼数据（bridge skeleton_pose）。')
      + (stallReason ? ' 位姿停更原因（不是仍在等待）：' + stallReason : '');
    /* The grid is a different field from the EMF pose block and can be live with no pose at all, so
       the read-out names why the 3D layer is missing instead of leaving the load unexplained. */
    if (handView.pressure && handPressureGrid()) {
      el.textContent += ' 压感：23×20 网格仍在更新，但无位姿节点可贴 → 3D 圆垫图层不可用。';
    }
    return;
  }
  const index = new Map(names.map((name, i) => [name, i]));
  const span = (a, b) => {
    const ia = index.get(a), ib = index.get(b);
    if (ia == null || ib == null) return null;
    const pa = positions[ia], pb = positions[ib];
    return Math.hypot(pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2]);
  };
  const mm = (v) => v == null ? '—' : (v * 1000).toFixed(1) + ' mm';
  const tips = HAND_TIPS.map(([finger, tip]) => {
    const i = index.get(tip);
    const z = i == null ? null : Number(positions[i][2]);
    return HAND_FINGER_ZH[finger] + ' ' + (z == null || !isFinite(z) ? '—' : (z * 1000).toFixed(0));
  }).join(' · ');
  const quat = (pose && pose.orientations_local_xyzw || [])[0];
  const lines = [];
  lines.push('<div>坐标系 <b>' + frame.frame + '</b> · 节点 <b>' + count + '/' + count
    + '</b> · 来源 ' + (frame.source || '—') + '</div>');
  lines.push('<div>腕→中指根 <b>' + mm(span('wrist', 'middle_metacarpal')) + '</b> · 腕→中指尖 <b>'
    + mm(span('wrist', 'middle_tip')) + '</b> · 拇指尖↔小指尖 <b>'
    + mm(span('thumb_tip', 'little_tip')) + '</b></div>');
  lines.push('<div>指尖 z（' + (frame.frame === 'cam0' ? 'cam0 深度方向' : '沿手指轴')
    + '，mm）：' + tips + '</div>');
  if (frame.frame === 'cam0') {
    const origin = (frame.wristOrigin || []).map((v) => Number(v).toFixed(3));
    lines.push('<div>腕原点 cam0 = <b>'
      + (origin.length === 3 ? origin.join(', ') + ' m' : '—')
      + '</b> · 相机原点 (0, 0, 0)</div>');
    lines.push('<div>位姿来源：'
      + (frame.source === 'replay' ? '会话报告回放的 T_cam<-wrist(t)' : '合成 fixture（非硬件证据，手形是合成替身、不是你的手）')
      + '；缺失时保持缺失，不用单位矩阵顶替。</div>');
    if (cam0 && cam0.registration_source) {
      lines.push('<div>配准来源 registration_source = <b>' + cam0.registration_source
        + '</b>（切换来源必须显示，不静默）</div>');
    }
  } else {
    lines.push('<div>腕部四元数 xyzw：'
      + (quat && quat.length === 4 ? quat.map((v) => Number(v).toFixed(3)).join(' / ') : '—')
      + '</div>');
  }
  /* Freshness and the reason a pose stopped arriving: without these the card shows a frozen
     frame that is indistinguishable from a still hand. */
  const age = handPoseAge(pose);
  if (age.age_sec != null) {
    lines.push('<div>位姿新鲜度：<b>' + age.age_sec.toFixed(1) + ' s</b> 前'
      + (age.stale ? ' — <b>已冻结</b>，画面不是当前手形' : '（实时）') + '</div>');
  }
  const stall = handPoseStallReason();
  if (stall) lines.push('<div>位姿停更原因：' + stall + '</div>');
  const baseline = handPressureBaseline();
  const pressure = handView.pressure
    ? handPressurePads(names, positions, pose && pose.hand_side, {baseline, rawCounts: !baseline})
    : null;
  if (pressure?.rawCounts) {
    lines.push('<div>触觉点显示原始计数（含静置底噪）。原始峰值 <b>'
      + Math.round(pressure.raw_max) + '</b> · ' + pressure.drawn + ' 个显示点'
      + ' · 未采集静置基线</div>');
  } else if (pressure) {
    const top = pressure.top;
    lines.push('<div>压感 3D 圆垫图层（相对静置基线的增量，原始计数，非力值）：静置基线最大 '
      + Math.round(pressure.baseline_max) + ' · 本帧原始最大 ' + Math.round(pressure.raw_max)
      + ' · 残差最大 ' + Math.round(pressure.max) + '</div>');
    lines.push('<div>阈值 ≥' + pressure.threshold
      + '（= max(' + HAND_PRESSURE_RESIDUAL_MIN_COUNT + ', '
      + Math.round(HAND_PRESSURE_PAD_MIN_FRACTION * 100) + '% × 残差最大 '
      + Math.round(pressure.max) + ')）· 画出 <b>' + pressure.drawn + '</b> 个圆垫'
      + '（指条 ' + (pressure.drawn - pressure.palmPads) + ' / 掌区 ' + pressure.palmPads + '）</div>');
    if (top) {
      const where = top.region === 'palm'
        ? '掌区行 ' + top.band.rows[0] + '–' + top.band.rows[1] + ' 列 ' + top.band.cols[0]
          + '–' + top.band.cols[1] + '（4×5 显示格）'
        : (HAND_FINGER_ZH[top.finger] || top.finger) + ' · 指条行 ' + top.row + ' 列 ' + top.column;
      lines.push('<div>最高圆垫：' + where + ' = <b>' + Math.round(top.value) + '</b>'
        + (top.site ? ' · 位点 ' + top.site.id + '（role=' + top.site.role + '）'
                    : ' · 不在 2026-09-17 探针位点内') + '</div>');
    } else {
      lines.push('<div>无圆垫：没有任何 taxel 的残差达到阈值（残差最大 '
        + Math.round(pressure.max) + '）。</div>');
    }
    const perFinger = Object.entries(pressure.fingerCounts).sort((a, b) => b[1] - a[1])
      .map(([finger, count]) => (HAND_FINGER_ZH[finger] || finger) + ' ' + count);
    lines.push('<div>每指圆垫：' + (perFinger.length ? perFinger.join(' · ') : '无')
      + ' · 掌区 ' + pressure.palmPads + '（本帧掌区残差最大 ' + Math.round(pressure.palmMax) + '）</div>');
    lines.push('<div>阈值以下未画 ' + (pressure.cells - pressure.above) + '/' + pressure.cells
      + ' 格 · 超上限 ' + HAND_PRESSURE_PAD_MAX + ' 未画 ' + pressure.suppressed + ' 个'
      + (pressure.unplaceable ? ' · 缺位姿几何未画 ' + pressure.unplaceable + ' 个' : '')
      + ' · 标数字 ' + pressure.labelled + ' 个</div>');
  }
  const tactile = handView.tactile ? handTactileScores() : null;
  if (tactile) {
    const rows = Object.entries(tactile.rows).sort((a, b) => b[1] - a[1]);
    lines.push('<div>EMF 触感（原始计数，非力值）：'
      + rows.map(([finger, value]) => '<b>' + (HAND_FINGER_ZH[finger] || finger) + '</b> '
        + Number(value).toFixed(1)).join(' · ') + '</div>');
    lines.push('<div>触感来源：contact_sites 位点中位正增量'
      + (tactile.site_source ? '（' + tactile.site_source + '）' : '')
      + (tactile.baseline_ready ? '' : ' · 基线未就绪') + '</div>');
  } else if (handView.tactile) {
    lines.push('<div>EMF 触感图层：等待 finger_scores（需先保存基线）。</div>');
  }
  el.innerHTML = lines.join('');
}

/* Orbit / zoom / reset. Redraws from the cached last frame so the view responds
   immediately even between the 50 ms data polls. */
function bindHandView() {
  const cv = $('handSkeletonCanvas');
  if (!cv) return;
  buildHandLegend();
  bindHandFrameControls();
  const redraw = () => redrawHand();
  cv.addEventListener('pointerdown', (e) => {
    handView.drag = true; handView.lastX = e.clientX; handView.lastY = e.clientY;
    cv.classList.add('dragging');
    try { cv.setPointerCapture(e.pointerId); } catch (err) { /* capture is optional */ }
  });
  cv.addEventListener('pointermove', (e) => {
    if (!handView.drag) return;
    handView.yaw += (e.clientX - handView.lastX) * 0.012;
    handView.pitch = Math.max(-1.45, Math.min(1.45, handView.pitch + (e.clientY - handView.lastY) * 0.012));
    handView.lastX = e.clientX; handView.lastY = e.clientY;
    redraw();
  });
  const release = () => { handView.drag = false; cv.classList.remove('dragging'); };
  cv.addEventListener('pointerup', release);
  cv.addEventListener('pointercancel', release);
  cv.addEventListener('wheel', (e) => {
    e.preventDefault();
    handView.zoom = Math.max(0.4, Math.min(3.5, handView.zoom * (e.deltaY < 0 ? 1.12 : 0.89)));
    redraw();
  }, { passive: false });
  cv.addEventListener('dblclick', () => {
    handView.yaw = HAND_VIEW_HOME.yaw; handView.pitch = HAND_VIEW_HOME.pitch; handView.zoom = 1;
    redraw();
  });
  redraw();
}

/* ---- job panel ---- */
let shownJobCompletion;
function renderJob(job) {
  if (job.label === 'EXO 标定与对齐') window.renderExoJob?.(job);
  if (job.label === '二维码时钟对齐' && job.session_id === $('calibSyncSession').value) {
    if (job.status === 'complete') renderQrReport(job.result);
    else $('qrResult').textContent = job.message;
  }
  const p = $('jobPanel');
  if (job.status === 'running') { p.classList.remove('hidden'); $('jobTitle').textContent = job.label; $('jobText').textContent = job.message; lastJobStatus = job.status; }
  else {
    p.classList.add('hidden');
    const completion = job.finished && job.started + ':' + job.finished;
    const finishedNow = Boolean(completion && shownJobCompletion !== completion);
    if (finishedNow) shownJobCompletion = completion;
    if (job.status === 'failed' && lastJobStatus !== job.status) toast('任务失败：' + job.message);
    else if (job.status === 'complete' && finishedNow) toast(job.label + ' 完成');
    lastJobStatus = job.status;
    if (finishedNow) {
      if (job.status === 'complete' && job.label === '整理录制数据（QC）') notifyEgoCaptureUpdated();
      refreshAfterJob(job);
    }
  }
}
/* The 2026-10-12 report: after 「整理（QC）」 finished, the session list still said 未QC and the
   player still refused with 「没有足够的配对帧」, because the list and the selected session's
   frames were only ever fetched when the review tab was entered. A finished job now refreshes
   both, so the QC button is enough to get from 未QC to a playable, annotated session. */
async function refreshAfterJob(job) {
  try {
    await loadSessions({refreshDetail:job.session_id === selectedSessionId});
    if (job.label === '导入录制') await loadImports();

  } catch (e) { /* the panel already reported the job; a refresh failure is not a job failure */ }
}

/* ---- calibration tab ---- */
const BADGE = { unverified: ['未验证', 'unverified'], unmeasured: ['未测量', 'unmeasured'], device_reported_unverified: ['设备自报·未验证', 'unverified'], not_received: ['未收到', 'none'], confirmed: ['已确认', 'ok'], measured: ['已测量', 'ok'], unknown: ['未知', 'unverified'], observed: ['已观测', 'ok'], done: ['已完成', 'ok'], needs_operator: ['待你操作', 'unverified'], needs_recording: ['待录制', 'unverified'], waiting_for_device: ['待连接设备', 'unverified'], blocked_on_user_material: ['待你提供标定板', 'unverified'], blocked_on_space_points: ['依赖上一步', 'unverified'], ready: ['可执行', 'ok'] };
function badge(st) { const b = BADGE[st] || [st || '未知', 'none']; return `<span class="badge ${b[1]}">${b[0]}</span>`; }
function eyeLabel(v) { return { left: '物理左目', right: '物理右目', unknown: '未确认' }[v] || '未确认'; }
async function loadCalibration() {
  try {
    const cal = await api('/api/calibration');
    const ident = cal.identity || {};
    const streams = cal.streams || {};
    const eyeOf = (cal.physical_eye && cal.physical_eye.streams) || {};
    const items = [];
    items.push(['双目相机身份', ident.name ? `${ident.name} · SN ${ident.serial || '—'} · 固件 ${ident.firmware || '—'} · ${ident.hardware || ''}`
      : '未连接', ident.name ? 'device_reported_unverified' : 'not_received',
      '连接后由串口协议自报（stream 1 握手）；型号信息未独立验证。']);
    Object.keys(streams).forEach(topic => {
      const s = streams[topic];
      items.push([`内参 · ${topic}`, s.K_complete
        ? `fx ${s.fx.toFixed(2)} fy ${s.fy.toFixed(2)} cx ${s.cx.toFixed(2)} cy ${s.cy.toFixed(2)} · ${s.width}×${s.height} · ${s.distortion_model} · HFOV ${s.horizontal_fov_deg}°`
        : '无完整 K', s.status,
        `物理眼：${eyeLabel(eyeOf[topic])}。${s.geometry_note}`]);
    });
    const st = cal.stereo_extrinsics || {};
    items.push(['双目外参（设备自报）', st.baseline_m != null
      ? `${st.parent_frame_id} → ${st.child_frame_id} · 基线 ${(st.baseline_m * 1000).toFixed(3)} mm · 相对旋转 ${st.rotation_angle_deg}°`
      : (st.reason || '未收到'), st.status,
      `${st.unit_note || ''} ${st.direction_note || ''}`.trim() || '设备未提供该消息。']);
    const imu = cal.imu_extrinsics || {};
    items.push(['相机 IMU 外参（设备自报）', imu.status === 'device_reported_unverified'
      ? `${imu.parent_frame_id} → ${imu.child_frame_id} · |t| ${Math.hypot(...(imu.translation_m || [0, 0, 0])).toFixed(4)} m · 旋转 ${imu.rotation_angle_deg}°`
      : (imu.reason || '未收到'), imu.status, imu.note || '设备自报；本项目坐标链尚未使用。']);
    items.push(['校准标识 calibration_id', cal.calibration_id || '—',
      cal.calibration_id ? 'device_reported_unverified' : 'not_received',
      '由设备自报值内容寻址生成：值变则变，仅改排版不变。它不是厂家校准编号。']);
    items.push(['时间同步', '校时：' + (cal.sync.clock_status || '未发送'), cal.sync.status, cal.sync.note]);
    items.push(['左手手套', '通道映射来源：' + cal.glove.channel_map_source, cal.glove.status, cal.glove.note]);
    $('calibPanels').innerHTML = items.map(([t, v, stt, note]) =>
      `<div class="calib-item"><h3>${t}${badge(stt)}</h3><p>${v}</p><p>${note}</p></div>`).join('');
    renderCalibWizard(cal);
    const devrep = await api('/api/devices');
    const ports = devrep.serial_ports.map(p => `${p.port} (VID ${p.vid}/PID ${p.pid}) ${p.description}`).join('; ') || '无串口设备';
    $('deviceReport').textContent = '串口设备：' + ports +
      '。双目匹配：' + (devrep.camera_match.length ? devrep.camera_match[0].port : '未发现') +
      '；手套匹配：' + (devrep.glove_match.length ? devrep.glove_match[0].port : '未发现') + '。' + devrep.virtual_cameras_note;
  } catch (e) { toast('标定状态获取失败：' + e.message); }
}

/* ---- calibration wizard: state comes from the server, never from a pressed button ---- */
function renderCalibWizard(cal) {
  const wizard = cal.wizard || { steps: [], completed: 0 };
  if ($('calibWizard')) {
    $('calibWizard').innerHTML =
      `<p class="muted">向导完成 ${wizard.completed}/${wizard.steps.length} 步。每一步的完成状态由设备与已录文件推导，按钮被按下不等于该步通过。</p>` +
      wizard.steps.map((s, i) => `<div class="wizard-step"><h4>${i + 1}. ${s.title} ${badge(s.state)}</h4>` +
        `<p class="muted">${s.detail}</p>` +
        (s.acceptance ? `<p class="muted small-text">通过条件：${s.acceptance}</p>` : '') +
        (s.measured && s.measured.baseline_m != null
          ? `<p class="muted small-text">设备自报基线：${(s.measured.baseline_m * 1000).toFixed(3)} mm</p>` : '') +
        `</div>`).join('');
  }
  if ($('calibUnverified')) {
    $('calibUnverified').innerHTML = (cal.unverified_items || []).map(x => `<li>${x}</li>`).join('');
  }
  if ($('eyeCheckRows')) {
    const streams = cal.streams || {};
    const eyeOf = (cal.physical_eye && cal.physical_eye.streams) || {};
    $('eyeCheckRows').innerHTML = Object.keys(streams).length
      ? Object.keys(streams).map(topic => `<div class="toolbar"><label>${topic}<select data-eye-topic="${topic}">` +
          ['unknown', 'left', 'right'].map(v => `<option value="${v}"${eyeOf[topic] === v ? ' selected' : ''}>${eyeLabel(v)}</option>`).join('') +
          `</select></label></div>`).join('')
      : '<p class="muted">等待双目连接</p>';
    $('submitEyeCheck').disabled = !Object.keys(streams).length;
  }
}
async function submitEyeAssignment() {
  const rows = Array.from(document.querySelectorAll('[data-eye-topic]'));
  if (!rows.length) { toast('尚未收到相机自报数据流'); return; }
  const streams = {};
  rows.forEach(r => { streams[r.dataset.eyeTopic] = r.value; });
  try {
    await post('/api/calibration/eye-assignment', { streams });
    toast('左右目对应已记录');
    await loadCalibration();
  } catch (e) { toast('提交失败：' + e.message); }
}
async function refreshQrVideos() {
  const picker = $('qrVideos');
  const selected = new Set(Array.from(picker.selectedOptions, option => option.value));
  try {
    const {videos, directory} = await api('/api/calibration/qr-inputs');
    $('qrVideoDirectory').textContent = directory;
    picker.replaceChildren(...videos.map(name => new Option(name, name, false, selected.has(name))));
  } catch (error) { $('qrResult').textContent = error.message; }
}

function renderQrReport(report) {
  const link = $('qrReport');
  link.hidden = !report?.cameras?.length;
  if (link.hidden) {
    $('qrResult').textContent = '所选录制还没有二维码同步报告。';
    return;
  }
  link.href = capturePath('/api/calibration/qr-result?id=' + encodeURIComponent(report.session_id));
  const cameras = report.cameras.map(camera => camera.name + ' · ' + camera.observations +
    ' 帧 · ' + (camera.mode === 'offset_only' ? '仅偏移' : '偏移与漂移') +
    ' · 拟合残差中位数 ' + camera.median_abs_residual_ms.toFixed(1) + ' ms');
  $('qrResult').textContent = cameras.join(' ｜ ') + ' ｜ 手套 ' + report.glove.record_count + ' 帧（接收时间）';
}

async function useQrReport(run) {
  const id = $('calibSyncSession').value;
  if (!id) { toast('请先选择一条已保存的录制'); return; }
  $('qrReport').hidden = true;
  $('qrResult').textContent = run ? '正在提交对齐任务…' : '正在读取已有报告…';
  try {
    if (run) {
      const videos = Array.from($('qrVideos').selectedOptions, option => option.value);
      await post('/api/calibration/qr-sync', {session_id:id, videos});
    } else {
      const report = await api('/api/calibration/qr-result?id=' + encodeURIComponent(id));
      if ($('calibSyncSession').value === id) renderQrReport(report);
    }
  } catch (error) {
    if ($('calibSyncSession').value === id) $('qrResult').textContent = error.message;
  }
}

async function runSyncAnalysis() {
  const id = $('calibSyncSession').value;
  if (!id) { toast('请先选择一条录制'); return; }
  if ($('syncResult')) $('syncResult').textContent = '正在离线估计（不占用采集设备）…';
  try {
    const r = await post('/api/calibration/sync-analysis', { session_id: id });
    if ($('calibSyncSession').value !== id) return;
    const e = r.estimate || {};
    $('syncResult').textContent = e.status === 'estimated'
      ? `会话 ${id}：偏移 ${e.offset_ms} ms（不确定度 ±${e.uncertainty_ms} ms，峰值比 ${e.peak_ratio}）` +
        (e.drift_ms_per_min != null ? ` · 漂移 ${e.drift_ms_per_min} ms/min` : ' · 漂移：两半未各自出现显著峰，未给出')
      : `会话 ${id}：INCONCLUSIVE — ${e.reason || '未给出原因'}`;
  } catch (e) { if ($('calibSyncSession').value === id) $('syncResult').textContent = '估计失败：' + e.message; }
}

/* ---- declared marker table (idx -> position on the hand) + hand->vision registration ----
   The table is what turns "a tag was detected" into "the hand is here in the camera frame":
   T_cam0<-wrist = T_cam0<-marker @ T_marker<-wrist, with T_marker<-wrist taken from the row
   the operator declared. Rows are editable here; the default is ID1 = centre of the back of
   the hand, 4 cm above it, printed face out of the back (+x in wrist_local), and ID2 = 2 cm
   towards the thumb and 2 cm above the back with the printed face towards the thumb, so its
   tag plane is perpendicular to the back of the hand (+y in wrist_local). Each row shows which
   declared posture it uses, so an orientation is never carried silently. A row's quaternion is
   written in the *measured* tag frame: marker +z points into the printed face, so "printed face
   along wrist +x / +y" is a z column of -x / -y (the payload repeats this as
   marker_axis_convention). */
const MARKER_AXIS_ZH = {
  '+x': '手背外法线方向（+x）', '-x': '手心方向（-x）',
  '+y': '拇指方向（+y）', '-y': '小指方向（-y）',
  '+z': '手指方向（+z）', '-z': '手腕方向（-z）'
};
function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
async function loadMarkerLayout() {
  if (!$('markerLayoutRows')) return;
  try {
    const data = await api('/api/handpose/marker-layout');
    markerDefaultRow = data.default_row || null;
    markerOrientationOptions = data.orientation_options || [];
    renderMarkerLayout(data);
  } catch (e) {
    $('markerLayoutRows').innerHTML = '<p class="muted">标记位置表读取失败：' + esc(e.message) + '</p>';
  }
}
function renderMarkerLayout(data) {
  markerRows = (data.summary && data.summary.rows) || [];
  $('markerLayoutRows').innerHTML = markerRows.map((row, index) => markerRowHtml(row, index)).join('')
    || '<p class="muted">表中没有任何标记行（至少需要一行才能标定）。</p>';
  $('markerLayoutNote').textContent =
    (data.loaded_note || '') +
    ' 坐标系：' + ((data.summary && data.summary.frame_note) || '') +
    ' 证据等级：' + ((data.summary && data.summary.evidence) || '') +
    ' · 文件：' + (data.layout_path || '—');
}
function markerOrientationKey(row) {
  /* Which declared posture a row's quaternion is; 'custom' means it matches none of them, so the
     row keeps its own quaternion instead of being silently rewritten to a preset. */
  const quaternion = row && row.quaternion_xyzw;
  if (!quaternion) return 'custom';
  let best = 'custom';
  let bestDelta = 1e-6;
  markerOrientationOptions.forEach(option => {
    const preset = option.quaternion_xyzw || [];
    const same = Math.abs(quaternion[0] - preset[0]) + Math.abs(quaternion[1] - preset[1]) +
      Math.abs(quaternion[2] - preset[2]) + Math.abs(quaternion[3] - preset[3]);
    const flipped = Math.abs(quaternion[0] + preset[0]) + Math.abs(quaternion[1] + preset[1]) +
      Math.abs(quaternion[2] + preset[2]) + Math.abs(quaternion[3] + preset[3]);
    if (Math.min(same, flipped) < bestDelta) { bestDelta = Math.min(same, flipped); best = option.key; }
  });
  return best;
}
function markerOrientationLabel(row) {
  const summary = (row && row.orientation) || {};
  if (!summary.face_normal_axis_key) return '';
  const face = MARKER_AXIS_ZH[summary.face_normal_axis_key] || summary.face_normal_axis_key;
  const flags = (summary.parallel_to_back_of_hand ? '，与手背面平行' : '') +
    (summary.perpendicular_to_back_of_hand ? '，与手背面垂直' : '');
  let text = '打印面法线 → ' + face + flags + ' · 偏离主轴 ' +
    summary.face_normal_off_axis_deg.toFixed(2) + '°';
  const footprint = summary.tag_footprint;
  if (footprint && footprint.dips_below_hand_back) {
    text += ' · ⚠ 按此中心与边长（' + (footprint.half_side_m * 2000).toFixed(0) +
      ' mm 边长）标签最低边在 x = ' + (footprint.lowest_edge_x_m * 1000).toFixed(1) +
      ' mm，低于手背面：要么该偏移指最低边，要么打印边长更小';
  }
  return text;
}
function markerRowHtml(row, index) {
  const millimetres = (value) => (value === null || value === undefined ? '' : value.toFixed(1));
  const key = markerOrientationKey(row);
  const options = markerOrientationOptions.map(option =>
    '<option value="' + option.key + '"' + (option.key === key ? ' selected' : '') + '>' +
    esc(option.label) + '</option>').join('') +
    '<option value="custom"' + (key === 'custom' ? ' selected' : '') +
    '>自定义（保留本行四元数）</option>';
  return '<div class="dataset-row marker-row" data-marker-row="' + index + '">' +
    '<label>IDX<input data-marker-field="idx" type="number" min="0" max="999" step="1" value="' + row.idx + '"></label>' +
    '<label>边长 mm<input data-marker-field="side_mm" type="number" min="5" max="500" step="1" value="' + Math.round(row.side_mm) + '"></label>' +
    '<label>上方 x mm<input data-marker-field="x_mm" type="number" step="1" value="' + millimetres(row.position_mm[0]) + '"></label>' +
    '<label>拇指侧 y mm<input data-marker-field="y_mm" type="number" step="1" value="' + millimetres(row.position_mm[1]) + '"></label>' +
    '<label>手指向 z mm<input data-marker-field="z_mm" type="number" step="1" value="' + millimetres(row.position_mm[2]) + '"></label>' +
    '<label>姿态<select data-marker-field="orientation">' + options + '</select></label>' +
    '<label>说明<input data-marker-field="note" value="' + esc(row.note) + '"></label>' +
    '<a class="small" href="' + capturePath(row.print_url) + '" target="_blank" rel="noopener">打印 PNG</a>' +
    '<button class="small quiet danger" data-marker-remove="' + index + '">删除</button>' +
    '<span class="muted small-text" data-marker-orientation-text="' + index + '">' +
    esc(markerOrientationLabel(row)) + '</span></div>';
}
function markerPresetQuaternion(key) {
  const option = markerOrientationOptions.find(item => item.key === key);
  return option ? option.quaternion_xyzw : null;
}
function readMarkerRows() {
  const holders = Array.from(document.querySelectorAll('[data-marker-row]'));
  return holders.map(holder => {
    const field = (name) => {
      const box = holder.querySelector('[data-marker-field="' + name + '"]');
      return box ? box.value : '';
    };
    const previous = markerRows[+holder.dataset.markerRow] || {};
    const key = field('orientation') || 'custom';
    return {
      idx: Math.trunc(parseFloat(field('idx'))),
      side_m: (parseFloat(field('side_mm')) || 0) / 1000,
      position_m: ['x_mm', 'y_mm', 'z_mm'].map(name => (parseFloat(field(name)) || 0) / 1000),
      quaternion_xyzw: markerPresetQuaternion(key) || previous.quaternion_xyzw ||
        (markerDefaultRow && markerDefaultRow.quaternion_xyzw) || [0, 0, 1, 0],
      note: field('note') || ''
    };
  });
}
function renderMarkerRowsEdited(note) {
  renderMarkerLayout({
    summary: { rows: markerRows, frame_note: 'positions are wrist_local metres',
               evidence: 'operator_declaration_not_measured' },
    loaded_note: note, layout_path: ''
  });
}
async function saveMarkerLayout() {
  const rows = readMarkerRows();
  if (!rows.length) return toast('至少需要一行标记位置');
  try {
    const stored = await post('/api/handpose/marker-layout',
      { layout: { frame: 'wrist_local', markers: rows } });
    renderMarkerLayout(stored);
    toast('标记位置表已保存：' + rows.length + ' 行（声明值，非实测）');
  } catch (e) { toast('保存失败：' + e.message); }
}
function addMarkerLayoutRow() {
  /* A new row copies the last row's position (a starting point to edit) but starts from the
     declared back-facing posture, so it does not silently inherit a thumb-facing orientation from
     the row above it; the posture is visible in the row's select. */
  const last = markerRows[markerRows.length - 1] ||
    { idx: 1, side_mm: 60, position_mm: [40, 0, 60] };
  const nextIdx = Math.trunc(last.idx) + 1;
  markerRows.push({ idx: nextIdx, side_mm: last.side_mm, side_m: last.side_mm / 1000,
    position_mm: last.position_mm.slice(), position_m: last.position_mm.map(v => v / 1000),
    quaternion_xyzw: markerPresetQuaternion('back_normal') ||
      (markerDefaultRow && markerDefaultRow.quaternion_xyzw) || [0, 0, 1, 0],
    note: '新行：请填写该标识在手背上的位置，并确认姿态下拉框',
    print_url: '/api/handpose/marker.png?idx=' + nextIdx });
  renderMarkerRowsEdited('未保存的编辑（点「保存标记位置表」写入）。');
}
function removeMarkerLayoutRow(index) {
  markerRows.splice(index, 1);
  renderMarkerRowsEdited('未保存的编辑（点「保存标记位置表」写入）。');
}
async function runMarkerCalibration(useSessionFrame) {
  const payload = {};
  if (useSessionFrame) {
    const id = $('sessionSelect') && $('calibSyncSession').value;
    if (!id) return toast('请先在「01 录制目录」或「02 选段与标注」选择录制');
    payload.session_id = id;
  }
  const box = $('markerCalibResult');
  box.classList.add('show');
  box.textContent = useSessionFrame ? '正在用所选会话的一帧检测二维码并求解手→视觉位姿…'
    : '正在用当前实时画面检测二维码并求解手→视觉位姿…';
  try {
    const report = await post('/api/handpose/marker-calibrate', payload);
    renderMarkerRegistration(report);
  } catch (e) {
    box.textContent = '标定未完成：' + e.message +
      '\n注意：没检测到标记不等于标定完成，也不会写入任何默认值。';
  }
}
function renderMarkerRegistration(report) {
  const box = $('markerCalibResult');
  const agreement = report.agreement || {};
  const lines = [];
  lines.push('图像来源：' + report.source_image + ' · 检测到 ' + (report.estimate_count || 0) + ' 枚声明标记');
  lines.push('一致性：' + agreement.status + ' — ' + (agreement.reason || ''));
  (report.estimates || []).forEach(estimate => {
    const wrist = estimate.wrist_pose_cam0 || {};
    const t = (wrist.translation_m || []).map(v => (v * 1000).toFixed(1)).join(', ');
    lines.push('  IDX ' + estimate.idx + '：手原点在 cam0 = [' + t + '] mm · 重投影 ' +
      (estimate.reprojection_error_px == null ? '—' : estimate.reprojection_error_px.toFixed(3)) +
      ' px · 声明位置 [' + estimate.declared_position_m.map(v => (v * 1000).toFixed(0)).join(', ') + '] mm');
  });
  if ((report.undeclared_ids || []).length) {
    lines.push('检测到但未声明：' + report.undeclared_ids.join(', ') + '（不会自动匹配到任何行）');
  }
  if ((report.missing_ids || []).length) {
    lines.push('已声明但未检测到：' + report.missing_ids.join(', ') + '（不给位姿）');
  }
  lines.push('相机：' + JSON.stringify(report.camera || {}));
  (report.notes || []).forEach(note => lines.push('· ' + note));
  lines.push('记录文件：' + (report.record_path || '—'));
  box.textContent = lines.join('\n');
}

/* ---- sessions / review / export ---- */
async function loadSessions({refreshDetail = false} = {}) {
  const previous = selectedSessionId;
  sessions = (await api('/api/sessions')).filter(session => session.status !== 'recording');
  window.guidedCapture?.setSessions(sessions);
  if (requestedReviewSession) {
    selectedSessionId = requestedReviewSession;
    requestedReviewSession = null;
  }
  if (!sessions.some(session => session.session_id === selectedSessionId)) selectedSessionId = '';
  if (previous !== selectedSessionId) {
    qualityExpanded = false;
    resetReview();
  }
  syncSessionPickers();
  renderSessionTable();
  if (qualityExpanded && $('directory').classList.contains('active') &&
      (refreshDetail || qualitySessionId !== selectedSessionId)) await loadQualityDetail(selectedSessionId);
  if ($('review').classList.contains('active') &&
      (refreshDetail || reviewSessionId !== selectedSessionId || pendingReviewSegment)) {
    await loadSessionDetail(selectedSessionId);
  }
}
function clearCalibrationResult() {
  $('qrReport').hidden = true;
  $('qrResult').textContent = '';
  $('syncResult').textContent = '';
  const missing = !$('calibSyncSession').value;
  for (const id of ['runQrSync', 'readQrReport', 'runSyncCheck', 'calibOpenSnapshot', 'useSessionFrame']) {
    $(id).disabled = missing;
  }
}
function syncSessionPickers() {
  const calibration = $('calibSyncSession');
  const previousCalibration = calibration.value;
  const sourceChanged = $('sessionSelect').value !== selectedSessionId;
  for (const picker of ['sessionSelect', 'reviewSession', 'calibSyncSession']) {
    const select = $(picker);
    select.replaceChildren(new Option('选择录制', ''));
    for (const session of sessions) {
      const label = picker === 'sessionSelect' ? session.session_id
        : [session.session_id, session.object_id, Number(session.duration_s || 0).toFixed(1) + ' s'].filter(Boolean).join(' · ');
      select.add(new Option(label, session.session_id));
    }
    select.value = select === calibration && !sourceChanged && previousCalibration ? previousCalibration : selectedSessionId;
    if (!select.value) select.value = '';
  }
  if (calibration.value !== previousCalibration) clearCalibrationResult();
}
function renderSessionTable() {
  const quality = $('quality');
  $('sessionCount').textContent = sessions.length + ' 条';
  const root = $('sessionTable');
  const focused = root.contains(document.activeElement)
    ? {id:document.activeElement.dataset.sessionId, view:document.activeElement.dataset.view}
    : null;
  const rows = sessions.map(session => {
    const row = document.createElement('div');
    row.className = 'session-entry';
    row.dataset.recordingId = session.session_id;
    const details = document.createElement('div');
    details.className = 'session-entry-details';
    const title = document.createElement('strong');
    title.textContent = session.session_id;
    const detail = document.createElement('span');
    detail.className = 'muted';
    detail.textContent = [session.object_id, session.scene_id, session.split,
      Number(session.duration_s || 0).toFixed(1) + ' s'].filter(Boolean).join(' · ');
    const status = document.createElement('span');
    status.textContent = session.processed ? (session.reviewed ? '已复核' : '已整理') : '待整理';
    status.className = 'tag';
    const actions = document.createElement('div');
    actions.className = 'session-entry-actions';
    for (const [view, label] of [['quality','整理详情'], ['review','选段与标注']]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = view === 'review' && session.processed ? 'small primary' : 'small';
      button.dataset.sessionId = session.session_id;
      button.dataset.view = view;
      if (view === 'quality') {
        button.setAttribute('aria-expanded', String(qualityExpanded && selectedSessionId === session.session_id));
        button.setAttribute('aria-controls', 'quality');
      }
      button.setAttribute('aria-label', label + ' · ' + session.session_id);
      button.textContent = label;
      actions.append(button);
    }
    details.append(title, detail, status);
    row.append(details, actions);
    return row;
  });
  if (!rows.length) {
    const empty = document.createElement('p');
    empty.className = 'muted';
    empty.textContent = '保存或导入后，会话会显示在这里。';
    rows.push(empty);
  }
  const selectedRow = rows.findIndex(item => item.dataset.recordingId === selectedSessionId);
  quality.hidden = !(qualityExpanded && selectedRow >= 0);
  quality.classList.toggle('active', !quality.hidden);
  if (!quality.hidden) rows.splice(selectedRow + 1, 0, quality);
  else $('directoryList').after(quality);
  root.replaceChildren(...rows);
  if (focused) {
    [...root.querySelectorAll('button')].find(button => button.dataset.sessionId === focused.id &&
      button.dataset.view === focused.view)?.focus({preventScroll:true});
  }
}

function resetReview() {
  reviewLoadGeneration++;
  policyRequest++;
  reviewSessionId = '';
  reviewTimeS = null;
  reviewFrames = [];
  reviewQuality = null;
  selectedReviewRange = null;
  reviewFirst = 0;
  reviewLast = -1;
  reviewEventView?.clear();
  resetManualEvents();
  $('reviewAnnotations').open = false;
  $('reviewPolicy').open = false;
  $('reviewPublish').open = false;
  $('loaderResult').textContent = '';
  $('loaderResult').classList.remove('show');
  clearReviewDraft();
  stopReviewPlayback();
  showReviewFrame(-1);
  updateReviewActions();
}
async function chooseSession(id, view, name = null) {
  if (id && !sessions.some(session => session.session_id === id)) await loadSessions();
  if (id && !sessions.some(session => session.session_id === id)) {
    toast('在录制目录找不到这条录制。');
    return;
  }
  if (selectedSessionId !== id) {
    qualityExpanded = false;
    resetReview();
  }
  selectedSessionId = id;
  pendingReviewSegment = name ? {session:id, name} : null;
  syncSessionPickers();
  renderSessionTable();
  await showCaptureTab(view);
}
async function openQuality(id) {
  if (!id) return;
  if (id !== selectedSessionId) resetReview();
  if (id !== qualitySessionId) {
    $('qualityRecording').textContent = id;
    $('qualityContent').hidden = true;
  }
  selectedSessionId = id;
  qualityExpanded = true;
  syncSessionPickers();
  setLiveView(false);
  renderSessionTable();
  await showCaptureTab('directory');
  await loadQualityDetail(id);
  if (selectedSessionId === id && qualityExpanded) $('qualityTitle').focus();
}
async function openSessionReview(id, name = null) {
  await chooseSession(id, 'review', name);
}
async function deleteSessionById(id) {
  if (!id) return;
  if (!confirm('确认删除会话 ' + id + '？此操作不可撤销，会删除原始数据、整理结果和已保存的复核标签。')) return;
  try {
    await post('/api/session/delete', { session_id: id });
    toast('已删除：' + id);
    if (id === selectedSessionId) {
      selectedSessionId = '';
      qualityExpanded = false;
      resetReview();
    }
    notifyEgoCaptureUpdated();
    await loadSessions();
  } catch (e) { toast('删除失败：' + e.message); }
}
let reviewLoadGeneration = 0, reviewEventView = null;
async function loadSessionDetail(id) {
  const generation = ++reviewLoadGeneration;
  $('reviewEmpty').hidden = Boolean(id);
  if (!id) {
    $('reviewContent').hidden = true;
    $('sessionSummary').textContent = '选择录制后可以复查双目、触觉和事件。';
    return;
  }
  const position = reviewSessionId === id ? reviewTimeS : null;
  if (reviewSessionId !== id) {
    $('reviewContent').hidden = true;
    $('sessionSummary').textContent = '正在读取录制…';
  }
  try {
    const detail = await api('/api/session?' + new URLSearchParams({view:'review', id}));
    if (generation !== reviewLoadGeneration || id !== selectedSessionId) return;
    reviewSessionId = id;
    reviewQuality = detail.quality;
    reviewDuration = Number(detail.metadata?.duration_s) || 0;
    reviewFrames = [...(detail.frames || [])].sort((a,b) => a.t_s-b.t_s);
    $('sessionSummary').textContent = [detail.metadata?.object_id, reviewDuration.toFixed(1) + ' s',
      reviewFrames.length + ' 组双目', reviewQuality ? '已整理' : '待整理'].filter(Boolean).join(' · ');
    $('reviewContent').hidden = false;
    $('reviewOpenQuality').hidden = Boolean(reviewQuality && reviewFrames.length);
    renderReviewSegments(detail.segments || [], id, position);
  } catch (error) {
    if (generation === reviewLoadGeneration && id === selectedSessionId) {
      $('sessionSummary').textContent = '录制读取失败 · ' + error.message;
    }
  }
}

let manualLoad = 0, manualScope = '', manualEditing = null;
function manualContext() {
  const range = selectedReviewRange;
  if (!reviewSessionId || !range || range.session !== reviewSessionId) return null;
  return {id:reviewSessionId, scope:{name:range.name,startS:range.startS,endS:range.endS,custom:Boolean(range.custom)}};
}
function clearManualDraft() {
  manualEditing = null;
  $('manualEventLabel').value = '';
  $('manualEventNote').value = '';
  $('manualEventCancel').hidden = true;
  $('manualEventSave').textContent = '保存标注';
}
function resetManualEvents() {
  manualLoad++;
  manualScope = '';
  clearManualDraft();
  $('manualEventList').replaceChildren();
  $('manualEventStatus').textContent = '';
}
function renderManualActions() {
  const context = manualContext();
  $('manualEventForm').hidden = !context;
  const time = manualEditing?.timeS ?? reviewFrameTimeS ?? reviewTimeS;
  $('manualEventTime').textContent = formatReviewTime(time);
  $('manualEventSave').disabled = !context || !Number.isFinite(time) ||
    time < context.scope.startS || time >= context.scope.endS;
}
function renderManualEvents(events) {
  const list = $('manualEventList');
  list.replaceChildren();
  $('manualEventStatus').textContent = events.length ? '本段手动标注 · ' + events.length + ' 条' : '本段暂无手动标注';
  for (const event of events) {
    const row = document.createElement('div');
    row.className = 'manual-event-row';
    const jump = document.createElement('button');
    jump.type = 'button';
    jump.className = 'small';
    jump.textContent = formatReviewTime(event.timeS) + ' · ' + event.label;
    jump.onclick = () => seekReviewTime(event.timeS);
    const edit = document.createElement('button');
    edit.type = 'button';
    edit.className = 'small';
    edit.textContent = '编辑';
    edit.setAttribute('aria-label', '编辑 ' + event.label);
    edit.onclick = () => {
      seekReviewTime(event.timeS);
      manualEditing = event;
      $('manualEventLabel').value = event.label;
      $('manualEventNote').value = event.note || '';
      $('manualEventCancel').hidden = false;
      $('manualEventSave').textContent = '保存修改';
      renderManualActions();
      $('manualEventLabel').focus();
    };
    row.append(jump, edit);
    if (event.note) {
      const note = document.createElement('div');
      note.className = 'muted';
      note.textContent = event.note;
      row.append(note);
    }
    list.append(row);
  }
}
async function loadManualEvents() {
  const context = manualContext(), key = context ? JSON.stringify(context) : '';
  if (!context) {
    resetManualEvents();
    $('manualEventStatus').textContent = '选择录制分段或应用自选区间后，可在当前时刻补标。';
    return;
  }
  if (manualScope === key) return;
  resetManualEvents();
  manualScope = key;
  const generation = manualLoad;
  $('manualEventStatus').textContent = '正在读取本段手动标注…';
  try {
    const result = await api('/api/manual-events?' +
      new URLSearchParams({id:context.id,scope:JSON.stringify(context.scope)}));
    if (generation === manualLoad && result.sessionId === context.id) renderManualEvents(result.events);
  } catch (error) {
    if (generation === manualLoad) {
      manualScope = '';
      $('manualEventStatus').textContent = '手动标注读取失败 · ' + error.message;
    }
  }
}
$('manualEventLabel').addEventListener('focus', stopReviewPlayback);
$('manualEventNote').addEventListener('focus', stopReviewPlayback);
$('manualEventCancel').onclick = () => { clearManualDraft(); renderManualActions(); };
$('manualEventForm').onsubmit = async event => {
  event.preventDefault();
  stopReviewPlayback();
  const context = manualContext();
  const timeS = manualEditing?.timeS ?? reviewFrameTimeS ?? reviewTimeS, label = $('manualEventLabel').value.trim();
  if (!context || !label || !Number.isFinite(timeS) || timeS < context.scope.startS ||
      timeS >= context.scope.endS) return;
  const generation = ++manualLoad;
  $('manualEventSave').disabled = true;
  try {
    const result = await post('/api/manual-events', {session_id:context.id,scope:context.scope,action:'save',
      event:{id:manualEditing?.id,timeS,label,note:$('manualEventNote').value.trim()}});
    if (generation !== manualLoad || result.sessionId !== context.id) return;
    clearManualDraft();
    renderManualEvents(result.events);
  } catch (error) {
    if (generation === manualLoad) $('manualEventStatus').textContent = '保存失败 · ' + error.message;
  } finally {
    if (generation === manualLoad) renderManualActions();
  }
};

function updateReviewActions() {
  const active = Boolean(reviewSessionId && reviewSessionId === selectedSessionId);
  const workflow = selectedReviewRange?.workflow;
  $('reviewOpenWorkflow').hidden = !active || !workflow || window.parent === window;
  $('reviewOpenEvents').disabled = !active;
  renderManualActions();
  $('reviewMarkStart').disabled = reviewTimeS === null;
  $('reviewMarkEnd').disabled = reviewTimeS === null;
  $('reviewAnnotationScope').textContent = selectedReviewRange
    ? (selectedReviewRange.label || selectedReviewRange.name) + ' · ' + formatReviewTime(selectedReviewRange.startS)
      + '–' + formatReviewTime(selectedReviewRange.endS)
    : '整条会话';
}
function updateReviewAnnotations() {
  const opening = $('reviewAnnotations').open;
  $('reviewOpenEvents').setAttribute('aria-expanded', String(opening));
  $('reviewOpenEvents').textContent = opening ? '收起事件标注' : '展开事件标注';
  if (!opening || !$('review').classList.contains('active') || !reviewSessionId) return;
  loadManualEvents();
  if (reviewTimeS !== null) queueRecordedPressure(reviewTimeS);
  if (!reviewQuality || !window.TactileEventReview) {
    reviewEventView?.clear();
    reviewEventView = null;
    $('eventReview').textContent = reviewQuality ? '事件组件未加载。' : '完成 02 整理后再复查事件。';
    return;
  }
  if (!reviewEventView) {
    $('eventReview').replaceChildren();
    reviewEventView = new window.TactileEventReview($('eventReview'), seekReviewTime, {expandedList:true});
  }
  if (reviewEventView.sid !== reviewSessionId) reviewEventView.select(reviewSessionId);
  reviewEventView.setRange(selectedReviewRange ? [selectedReviewRange.startS, selectedReviewRange.endS] : null);
  if (reviewTimeS !== null) reviewEventView.setTime(reviewTimeS);
  if ($('reviewPolicy').open) loadPolicyLabels();
}
function lowerReviewFrame(time) {
  let lo=0, hi=reviewFrames.length;
  while(lo<hi){const mid=(lo+hi)>>>1;if(reviewFrames[mid].t_s<time)lo=mid+1;else hi=mid;}
  return lo;
}
function formatReviewTime(seconds) {
  return Number.isFinite(seconds) ? seconds.toFixed(3) + ' s' : '—';
}
function updateReviewCursor(time) {
  reviewTimeS = time;
  if ($('reviewAnnotations').open) {
    reviewEventView?.setTime(time);
    queueRecordedPressure(time);
  }
}
function seekReviewTime(time, tolerance = .15) {
  stopReviewPlayback();
  if (!Number.isFinite(time) || !reviewSessionId) return;
  const start = selectedReviewRange?.startS ?? 0;
  const end = selectedReviewRange?.endS ?? reviewDuration;
  time = Math.max(start, Math.min(time, selectedReviewRange ? Math.max(start, end - .000001) : end));
  if (reviewLast >= reviewFirst) {
    const next = Math.min(reviewLast, Math.max(reviewFirst, lowerReviewFrame(time)));
    const previous = Math.max(reviewFirst, next - 1);
    const index = Math.abs(reviewFrames[previous].t_s - time) <= Math.abs(reviewFrames[next].t_s - time)
      ? previous : next;
    if (Math.abs(reviewFrames[index].t_s - time) <= tolerance) {
      showReviewFrame(index, time);
      return;
    }
    $('reviewSlider').value = index;
  }
  showReviewFrame(-1, time);
}
async function loadPolicyLabels() {
  const request = ++policyRequest;
  const identity = selectedReviewRange?.workflow;
  const status = $('policyLabelStatus');
  $('refreshPolicyLabels').disabled = !identity;
  $('policyLabels').replaceChildren();
  if (!identity) {
    status.textContent = '选择已加入工作流的片段后查看。';
    return;
  }
  if (!$('reviewAnnotations').open || !$('reviewPolicy').open) return;
  status.textContent = '正在查找当前片段的工作流 04 回执…';
  try {
    const result = await api('/api/policy-labels?' + new URLSearchParams(identity));
    if (request !== policyRequest) return;
    if (!result.events?.length) {
      status.textContent = result.message;
      return;
    }
    status.textContent = 'Isaac 回放候选 · ' + result.execution + (result.physicalPass ? ' · 物理任务已通过' : ' · 物理任务未验收');
    const fingerNames = {thumb:'拇指', index:'食指', middle:'中指', ring:'无名指', little:'小指'};
    for (const [index, event] of result.events.entries()) {
      const row = document.createElement('div');
      row.className = 'policy-label';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'small';
      button.textContent = event.startS.toFixed(3) + '–' + event.endS.toFixed(3) + ' s · ' + fingerNames[event.finger] + '承载候选';
      button.onclick = () => seekReviewTime(event.startS);
      const choice = document.createElement('select');
      choice.setAttribute('aria-label', button.textContent + '的复查结果');
      choice.add(new Option('待复查', 'pending'));
      choice.add(new Option('保留标注', 'confirmed'));
      choice.add(new Option('否定候选', 'rejected'));
      let saved = result.decisions?.[index] || 'pending';
      choice.value = saved;
      choice.onchange = async () => {
        choice.disabled = true;
        try {
          const decision = await post('/api/policy-labels/review', {
            segment:identity, traceSha256:result.traceSha256, index, choice:choice.value
          });
          saved = decision.choice;
          toast('已保存此片段的 04 候选复查。');
        } catch (error) {
          toast(error.message);
          choice.value = saved;
        } finally {
          choice.disabled = false;
        }
      };
      row.append(button, choice);
      $('policyLabels').append(row);
    }
  } catch (error) {
    if (request === policyRequest) status.textContent = '读取 04 候选失败 · ' + error.message;
  }
}
function selectReviewRange(range, position = null) {
  stopReviewPlayback();
  const previous = selectedReviewRange;
  selectedReviewRange = range;
  if (range && (range.session !== previous?.session || range.name !== previous.name ||
      range.startS !== previous.startS || range.endS !== previous.endS)) $('reviewAnnotations').open = true;
  const picker = $('reviewSegment');
  const custom = picker.querySelector('option[value=custom]');
  if (range?.custom) {
    if (!custom) picker.add(new Option('自选区间', 'custom'));
    picker.value = 'custom';
  } else {
    custom?.remove();
  }
  reviewFirst = range ? lowerReviewFrame(range.startS) : 0;
  reviewLast = range ? lowerReviewFrame(range.endS) - 1 : reviewFrames.length - 1;
  const count = Math.max(0, reviewLast - reviewFirst + 1);
  const slider = $('reviewSlider');
  slider.min = count ? reviewFirst : 0;
  slider.max = count ? reviewLast : 0;
  slider.value = count ? reviewFirst : 0;
  slider.disabled = count < 2;
  $('reviewPlay').disabled = count < 2;
  const start = range?.startS ?? 0, end = range?.endS ?? reviewDuration;
  $('reviewTimelineStart').textContent = formatReviewTime(start);
  $('reviewTimelineEnd').textContent = formatReviewTime(end);
  $('reviewRangeStats').textContent = formatReviewTime(start) + ' – ' + formatReviewTime(end)
    + ' · ' + count + ' 组双目';
  if (position !== null && position >= start && (range ? position < end : position <= end)) {
    seekReviewTime(position);
  } else {
    showReviewFrame(count ? reviewFirst : -1, count ? null : start);
  }
  updateReviewActions();
  if ($('reviewAnnotations').open) updateReviewAnnotations();
}
function clearReviewDraft() {
  $('workflowSegmentName').value = '';
  $('workflowSegmentStart').value = '';
  $('workflowSegmentEnd').value = '';
  $('workflowSegmentCategory').value = 'normal';
  $('workflowSegmentStatus').textContent = '';
}
function renderReviewSegments(segments, id, position) {
  const select = $('reviewSegment'), previous = selectedReviewRange;
  const wanted = pendingReviewSegment?.session === id ? pendingReviewSegment.name : null;
  if (wanted) pendingReviewSegment = null;
  reviewSegments = segments;
  select.replaceChildren(new Option('整条会话', ''));
  for (const [index,row] of segments.entries()) {
    const outcome = {success:'成功',failed:'失败',incomplete:'未完成'}[row.outcome];
    const description = [row.label || row.name,
      row.startS.toFixed(2) + '–' + row.endS.toFixed(2) + ' s',
      outcome, row.workflow ? '已登记' : '录制分段'].filter(Boolean).join(' · ');
    select.add(new Option(description, String(index)));
  }
  const index = segments.findIndex(row => wanted ? row.name === wanted :
    previous?.session === id && row.name === previous.name &&
    row.startS === previous.startS && row.endS === previous.endS);
  if (!wanted && index < 0 && previous?.session === id && previous.custom) {
    selectReviewRange(previous, position);
    return;
  }
  select.value = index < 0 ? '' : String(index);
  chooseReviewSegment(wanted ? null : position);
}
function chooseReviewSegment(position = null) {
  const value = $('reviewSegment').value;
  if (value === 'custom') return;
  const row = value === '' ? null : reviewSegments[Number(value)];
  selectReviewRange(row ? {...row, session:reviewSessionId} : null, position);
  if (!row) {
    clearReviewDraft();
    return;
  }
  $('workflowSegmentName').value = row.name;
  $('workflowSegmentStart').value = row.startS.toFixed(6);
  $('workflowSegmentEnd').value = row.endS.toFixed(6);
  const options = [...$('workflowSegmentCategory').options];
  $('workflowSegmentCategory').value = row.category === 'attempt'
    ? (row.outcome === 'failed' ? 'miss' : 'normal')
    : options.some(option => option.value === row.category) ? row.category : 'reference';
  $('workflowSegmentStatus').textContent = row.workflow ? '该区间已加入工作流。' : '核对双目和触觉后可加入工作流。';
}
$('reviewSegment').addEventListener('change', () => chooseReviewSegment());
$('reviewPreviewRange').onclick = () => {
  const start = $('workflowSegmentStart').valueAsNumber, end = $('workflowSegmentEnd').valueAsNumber;
  if (!(start >= 0 && end > start && end <= reviewDuration)) {
    $('workflowSegmentStatus').textContent = '起止时间须在本次录制内，且终点晚于起点。';
    return;
  }
  $('reviewSegment').value = '';
  $('workflowSegmentStatus').textContent = '已应用自选区间。';
  selectReviewRange({startS:start,endS:end,name:'自选区间',session:reviewSessionId,custom:true});
};
for (const [button,target] of [['reviewMarkStart','workflowSegmentStart'],['reviewMarkEnd','workflowSegmentEnd']]) {
  $(button).onclick = () => {
    if (reviewTimeS !== null) $(target).value = reviewTimeS.toFixed(6);
  };
}
window.openCaptureSegment = (session, name) => openSessionReview(session, name).catch(error => toast(error.message));

function decodeReviewImage(url) {
  const candidate = new Image();
  candidate.src = url;
  return candidate.decode().then(() => url, () => '');
}
async function showReviewFrame(index, time = null) {
  const generation = ++reviewPairGeneration;
  const id = reviewSessionId;
  const frame = index >= reviewFirst && index <= reviewLast ? reviewFrames[index] : null;
  const cursor = time ?? frame?.t_s ?? null;
  reviewFrameTimeS = frame?.t_s ?? null;
  const count = Math.max(0, reviewLast - reviewFirst + 1);
  const slider = $('reviewSlider');
  const note = $('reviewPlaybackNote');
  updateReviewCursor(cursor);
  slider.setAttribute('aria-valuetext', formatReviewTime(cursor));
  if (frame) slider.value = index;
  $('reviewPrevious').disabled = !count || Number(slider.value) <= reviewFirst;
  $('reviewNext').disabled = !count || Number(slider.value) >= reviewLast;
  $('reviewTime').textContent = formatReviewTime(cursor);
  $('reviewFrameTag').textContent = (frame ? index - reviewFirst + 1 : 0) + ' / ' + count + ' 帧';
  updateReviewActions();
  const images = [$('reviewImage0'), $('reviewImage1')];
  if (!frame || !id) {
    for (const image of images) image.removeAttribute('src');
    note.textContent = !id ? '' : !reviewQuality ? '请先整理录制，生成双目与手套复查数据。'
      : count ? '这个时刻附近没有双目配对画面。' : '所选区间没有双目配对画面。';
    return;
  }
  const urls = [frame.image, frame.image1].map(image =>
    capturePath('/api/image?' + new URLSearchParams({id,image})));
  const loaded = await Promise.all(urls.map(decodeReviewImage));
  if (generation !== reviewPairGeneration || id !== reviewSessionId) return;
  for (const [eye,url] of loaded.entries()) {
    if (url) images[eye].src = url;
    else images[eye].removeAttribute('src');
  }
  note.textContent = loaded.every(Boolean)
    ? (time !== null ? '对应画面 ' + formatReviewTime(frame.t_s) + ' · ' : '')
      + '双目设备时间差 ' + (frame.stereo_delta_ms ?? '—') + ' ms'
    : '一个或两个相机画面载入失败。';
}
function stopReviewPlayback() {
  reviewPlaying = false;
  reviewPlayRun++;
  clearTimeout(reviewTimer);
  reviewTimer = null;
  $('reviewPlay').textContent = '播放';
}
function scheduleReviewFrame(index, run, frameShownAt) {
  if (!reviewPlaying || run !== reviewPlayRun) return;
  const speed = Number($('reviewSpeed').value) || 1;
  const next = index < reviewLast ? index + 1 : $('reviewLoop').checked ? reviewFirst : -1;
  const interval = next > index ? (reviewFrames[next].t_s - reviewFrames[index].t_s) * 1000 : 200;
  const delay = Math.max(0, Math.max(20, interval) / speed - (performance.now() - frameShownAt));
  reviewTimer = setTimeout(async () => {
    if (!reviewPlaying || run !== reviewPlayRun) return;
    if (next < 0) { stopReviewPlayback(); return; }
    const now = performance.now();
    await showReviewFrame(next);
    scheduleReviewFrame(next, run, now);
  }, delay);
}
$('reviewPlay').addEventListener('click', async () => {
  if (reviewPlaying) { stopReviewPlayback(); return; }
  if (reviewLast - reviewFirst < 1) return;
  reviewPlaying = true;
  $('reviewPlay').textContent = '暂停';
  const run = ++reviewPlayRun;
  let index = Number($('reviewSlider').value);
  if (index >= reviewLast) index = reviewFirst;
  const now = performance.now();
  await showReviewFrame(index);
  scheduleReviewFrame(index, run, now);
});
for (const [button,step] of [['reviewPrevious',-1], ['reviewNext',1]]) {
  $(button).onclick = () => {
    stopReviewPlayback();
    showReviewFrame(Number($('reviewSlider').value) + step);
  };
}
$('sessionTable').addEventListener('click', event => {
  const button = event.target.closest('[data-session-id]');
  if (!button) return;
  const id = button.dataset.sessionId;
  if (button.dataset.view === 'quality') {
    if (qualityExpanded && selectedSessionId === id) closeQuality(id);
    else openQuality(id).catch(error => toast(error.message));
  } else {
    chooseSession(id, 'review').catch(error => toast(error.message));
  }
});
$('reviewSession').onchange = event => chooseSession(event.target.value, 'review').catch(error => toast(error.message));
function closeQuality(id) {
  qualityExpanded = false;
  renderSessionTable();
  const buttons = [...$('sessionTable').querySelectorAll('button[data-view=quality]')];
  buttons.find(button => button.dataset.sessionId === id)?.focus({preventScroll:true});
}
$('closeQuality').onclick = () => closeQuality(selectedSessionId);
$('qualityOpenReview').onclick = () => showCaptureTab('review');
$('reviewOpenQuality').onclick = () => openQuality(selectedSessionId).catch(error => toast(error.message));
$('openDeviceCalibration').onclick = () => showCaptureTab('calib');
$('reviewOpenEvents').onclick = () => {
  $('reviewAnnotations').open = !$('reviewAnnotations').open;
  if ($('reviewAnnotations').open && matchMedia('(max-width: 870px)').matches) {
    $('reviewAnnotations').scrollIntoView({block:'nearest'});
  }
};
$('reviewAnnotations').addEventListener('toggle', updateReviewAnnotations);
$('reviewPolicy').addEventListener('toggle', () => {
  if ($('reviewPolicy').open && $('reviewAnnotations').open) loadPolicyLabels();
});
$('deleteSession').addEventListener('click', () => deleteSessionById($('sessionSelect').value));
$('reviewSetup').onchange = async event => {
  const id = qualitySessionId;
  if (!id || id !== selectedSessionId) return;
  const select = event.target;
  select.disabled = true;
  try {
    const result = await post('/api/review-setup', {session_id:id, source_id:select.value});
    if (qualitySessionId === id && selectedSessionId === id) reviewSetup = result;
  } catch (error) { toast(error.message); }
  if (qualitySessionId === id && selectedSessionId === id) renderReviewSetup();
};
$('refreshPolicyLabels').onclick = loadPolicyLabels;
$('reviewOpenWorkflow').onclick = () => {
  const identity = selectedReviewRange?.workflow;
  if (identity) window.parent.postMessage({
    type:'ego-capture-open-workflow', sessionId:identity.recordingId, clip:identity.clip
  }, location.origin);
};

let liveQrVisible = true;
let liveQrRecording = false;
let liveQrRecordingRevision = 0;
let liveQrClock;
function setLiveQrRecording(recording) {
  if (liveQrRecording === recording) return;
  liveQrRecording = recording;
  updateLiveQr();
}

function updateLiveQr() {
  const dialog = $('liveQrDialog');
  const live = !$('directory').hidden && !$('live').hidden;
  const available = liveQrVisible && liveQrRecording === true;
  const active = live && available && !document.hidden;
  const display = $('liveQrCode');
  const status = $('liveQrStatus');
  display.hidden = !liveQrRecording;
  $('enlargeLiveQr').disabled = !available;
  if ((!live || !available) && dialog.open) dialog.close();
  if (active && !liveQrClock) {
    liveQrClock = window.EgoQrClock?.create({display, status});
  }
  liveQrClock?.setActive(active);
  if (!liveQrRecording) {
    status.textContent = liveQrRecording === null ? '录制状态未连接，二维码已暂停' : '未录制，二维码已暂停';
  }
}
$('toggleLiveQr').onclick = () => {
  liveQrVisible = !liveQrVisible;
  $('liveQrBody').hidden = !liveQrVisible;
  $('toggleLiveQr').textContent = liveQrVisible ? '收起' : '显示二维码';
  $('toggleLiveQr').setAttribute('aria-expanded', String(liveQrVisible));
  updateLiveQr();
};
$('enlargeLiveQr').onclick = () => {
  $('liveQrDialogBody').append($('liveQrDisplay'));
  $('liveQrDialog').showModal();
};
$('liveQrDialog').addEventListener('close', () => $('liveQrBody').append($('liveQrDisplay')));
document.addEventListener('visibilitychange', updateLiveQr);

async function openCalibrationLive(showQr) {
  if (showQr && !liveQrVisible) $('toggleLiveQr').click();
  await showCaptureTab('directory');
  setLiveView(true);
  if (showQr) $('toggleLiveQr').focus();
  else $('liveTitle').focus();
}
$('calibOpenLive').onclick = () => openCalibrationLive(false);
$('calibShowLiveQr').onclick = () => openCalibrationLive(true);

const calibrationViews = [...document.querySelectorAll('[data-calib-view]')];
function refreshCalibrationView() {
  const active = calibrationViews.find(button => button.getAttribute('aria-pressed') === 'true');
  $('calibRecordingTools').hidden = active?.dataset.calibView === 'calibDevices';
  if (active?.dataset.calibView === 'calibClocks') refreshQrVideos();
  if (active?.dataset.calibView === 'calibAdvanced') loadMarkerLayout();
}
for (const button of calibrationViews) {
  button.onclick = () => {
    for (const candidate of calibrationViews) {
      candidate.setAttribute('aria-pressed', String(candidate === button));
      $(candidate.dataset.calibView).hidden = candidate !== button;
    }
    refreshCalibrationView();
  };
}
$('calibSyncSession').addEventListener('change', clearCalibrationResult);
$('calibOpenSnapshot').onclick = () => {
  openQuality($('calibSyncSession').value).catch(error => toast(error.message));
};
clearCalibrationResult();

function setLiveView(enabled) {
  $('live').hidden = !enabled;
  $('live').classList.toggle('active', enabled);
  $('captureLive').setAttribute('aria-expanded', String(enabled));
  $('directoryHeading').hidden = enabled;
  $('directoryList').hidden = enabled;
  $('importPanel').hidden = enabled;
  $('quality').hidden = enabled || !qualityExpanded;
  if (!enabled) loadImports();
  updateLiveQr();
}
$('captureLive').onclick = () => {
  setLiveView(true);
  $('liveTitle').focus();
};
$('liveBackToDirectory').onclick = () => {
  setLiveView(false);
  $('captureLive').focus();
};

const captureTabs = [...document.querySelectorAll('.capture-tabs [role="tab"]')];
async function showCaptureTab(id) {
  const selected = captureTabs.find(tab => tab.dataset.tab === id);
  if (!selected) return;
  for (const tab of captureTabs) {
    const active = tab === selected;
    tab.classList.toggle('active', active);
    tab.setAttribute('aria-selected', String(active));
    tab.tabIndex = active ? 0 : -1;
    const panel = $(tab.dataset.tab);
    panel.hidden = !active;
    panel.classList.toggle('active', active);
  }
  updateLiveQr();
  if (id !== 'review') stopReviewPlayback();
  if (id === 'directory' && !$('importPanel').hidden) loadImports();
  if (id === 'review') {
    if (reviewSessionId !== selectedSessionId || !reviewSessionId || pendingReviewSegment ||
        Boolean(reviewQuality) !== Boolean(sessions.find(s => s.session_id === selectedSessionId)?.processed)) {
      await loadSessionDetail(selectedSessionId);
    } else {
      updateReviewAnnotations();
    }
  }
  if (id === 'calib') {
    loadCalibration();
    refreshCalibrationView();
  }
}
for (const tab of captureTabs) {
  tab.addEventListener('click', () => {
    if (tab.dataset.tab === 'directory') setLiveView(false);
    showCaptureTab(tab.dataset.tab);
  });
  tab.addEventListener('keydown', event => {
    const index = captureTabs.indexOf(tab);
    const next = {ArrowRight: (index+1)%captureTabs.length,
      ArrowLeft: (index+captureTabs.length-1)%captureTabs.length,
      Home: 0, End: captureTabs.length-1}[event.key];
    if (next === undefined) return;
    event.preventDefault();
    showCaptureTab(captureTabs[next].dataset.tab);
    captureTabs[next].focus();
  });
}
window.addEventListener('message', event => {
  if (event.source !== window.parent || event.origin !== location.origin) return;
  const {type,sessionId,clip,message} = event.data || {};
  if (type === 'ego-capture-workflow-error' && selectedReviewRange?.workflow?.clip === clip && selectedSessionId === sessionId) {
    toast(message);
  }
  if (type === 'ego-capture-open-review' && typeof sessionId === 'string' && /^[a-z0-9_-]{1,96}$/i.test(sessionId)) {
    openSessionReview(sessionId).catch(error => toast(error.message));
  }
});
$('reviewSlider').addEventListener('input', e => { stopReviewPlayback(); showReviewFrame(+e.target.value); });
$('refreshCalib').addEventListener('click', loadCalibration);
if ($('submitEyeCheck')) $('submitEyeCheck').addEventListener('click', submitEyeAssignment);
if ($('runSyncCheck')) $('runSyncCheck').addEventListener('click', runSyncAnalysis);
$('refreshQrVideos').addEventListener('click', refreshQrVideos);
$('runQrSync').addEventListener('click', () => useQrReport(true));
$('readQrReport').addEventListener('click', () => useQrReport(false));
$('startQnBotCalibration').addEventListener('click', async () => {
  /* Starting a run means re-measuring: the bridge forces the SDK to ignore any
     stored calibration record and to re-capture every stage. Disable the button
     right away so a double click cannot send a second start before the next poll
     reports the run's phase. */
  $('startQnBotCalibration').disabled = true;
  try {
    await post('/api/qnbot/calibration/start');
    $('qnbotCalibrationState').textContent = '标定启动中：本次会重新采集，请按提示摆姿势';
    $('captureQnBotCalibration').disabled = true;
    $('cancelQnBotCalibration').disabled = false;
  } catch (e) {
    $('startQnBotCalibration').disabled = false;
    toast(e.message);
  }
});
$('reuseQnBotCalibration').addEventListener('click', async () => {
  /* Enable the official output from the record already on disk. The bridge checks
     the record with the SDK's own matching rules and only then calls
     glove.start(), so this cannot begin a capture flow; the panel keeps saying
     that nothing was re-measured. */
  $('reuseQnBotCalibration').disabled = true;
  try {
    await post('/api/qnbot/calibration/reuse');
    $('qnbotCalibrationState').textContent = '正在启用已存标定记录…';
  } catch (e) {
    $('reuseQnBotCalibration').disabled = false;
    toast(e.message);
  }
});
$('captureQnBotCalibration').addEventListener('click', async () => { try { await post('/api/qnbot/calibration/capture'); } catch(e){toast(e.message);} });
$('cancelQnBotCalibration').addEventListener('click', async () => { try { await post('/api/qnbot/calibration/cancel'); } catch(e){toast(e.message);} });
$('refreshSessions').addEventListener('click', () => loadSessions().catch(error => toast(error.message)));
/* Marker layout controls: the declared IDX -> position table, and the hand->vision registration
   it feeds. The table is written to outputs/capture-station/calibration/marker_layout.json. */
if ($('addMarkerRow')) $('addMarkerRow').addEventListener('click', addMarkerLayoutRow);
if ($('saveMarkerLayout')) $('saveMarkerLayout').addEventListener('click', saveMarkerLayout);
if ($('runMarkerCalibration')) $('runMarkerCalibration').addEventListener('click', () => runMarkerCalibration(false));
if ($('useSessionFrame')) $('useSessionFrame').addEventListener('click', () => runMarkerCalibration(true));
if ($('markerLayoutRows')) {
  $('markerLayoutRows').addEventListener('click', (e) => {
    const remove = e.target.closest('[data-marker-remove]');
    if (remove) removeMarkerLayoutRow(+remove.dataset.markerRemove);
  });
  /* Changing the posture dropdown updates the row's readout from the declared preset itself, so the
     page never shows a stale posture between the edit and the save. */
  $('markerLayoutRows').addEventListener('change', (e) => {
    const select = e.target.closest('[data-marker-field="orientation"]');
    if (!select) return;
    const holder = select.closest('[data-marker-row]');
    const readout = holder && holder.querySelector('[data-marker-orientation-text]');
    const option = markerOrientationOptions.find(item => item.key === select.value);
    if (readout) readout.textContent = option
      ? (option.label + ' · ' + option.note)
      : '自定义：保留本行四元数（需重算后才显示法线方向）';
  });
}
/* The 3 s window closes server-side, so the button confirms what was actually saved instead of
   leaving the operator to guess. Both zero references come out of that one window: the byte baseline
   the contact candidate uses, and the 23x20 resting grid the 3D taxel layer subtracts. */
async function captureTouchBaseline() {
  try {
    await post('/api/touch/baseline');
    toast('开始记录基线：请保持手套静置 3 秒');
    setTimeout(async () => {
      await pollState();
      const glove = (state && state.glove) || {};
      if (!glove.baseline_ready) {
        toast('基线未保存：' + ((state && state.notice) || '数据不足'));
        return;
      }
      const summary = glove.pressure_baseline_summary || {};
      toast(handPressureBaseline()
        ? ('基线已保存：字节基线 + 23×20 静置基线（最大 ' + summary.max + '、≥3 的格 '
           + summary.cells_at_least_3 + '，原始计数）')
        : '字节基线已保存，但压感静置基线未保存');
      redrawHand();
    }, 3600);
  } catch (e) { toast(e.message); }
}
$('baseline').addEventListener('click', captureTouchBaseline);
$('calibTouchBaseline').addEventListener('click', captureTouchBaseline);
async function startRecording(unlimited) {
  if (recordStartPending || state?.recording) return;
  recordStartPending = true;
  setRecordingButtons(false);
  try {
    await post(unlimited ? '/api/record/start-unlimited' : '/api/record/start', {
      object_id: $('objectId').value, scene_id: $('sceneId').value,
      material: $('material').value, split: $('split').value
    });
    toast(unlimited ? '不限额录制已开始' : '录制已开始');
    await pollState();
  } catch (e) { toast(e.message); }
  finally {
    recordStartPending = false;
    setRecordingButtons(Boolean(state?.recording));
  }
}
$('startRecord').addEventListener('click', () => startRecording(false));
$('startUnlimitedRecord').addEventListener('click', () => startRecording(true));
$('stopRecord').addEventListener('click', async () => {
  try {
    if (window.guidedCapture && window.guidedCapture.ownsRecording()) {
      await window.guidedCapture.finish(); return;
    }
    const r = await post('/api/record/stop');
    // A status request sent before stopping can still return the previous recording.
    liveQrRecordingRevision++;
    setLiveQrRecording(false);
    if (r.session_id) {
      requestedReviewSession = r.session_id;
      notifyEgoCaptureUpdated();
      toast('已保存，正在安排自动整理：' + r.session_id);
    }
    loadSessions();
  }
  catch (e) { toast(e.message); }
});
const marker = (kind) => async () => { try { await post('/api/marker', { kind }); toast('已标记'); } catch (e) { toast(e.message); } };
$('contactMarker').addEventListener('click', marker('operator_contact'));
$('releaseMarker').addEventListener('click', marker('operator_release'));
$('replay').addEventListener('click', async () => { try { await post('/api/replay', {}); toast('历史相机回放中（无配对触觉）'); } catch (e) { toast(e.message); } });
$('liveMode').addEventListener('click', async () => { try { await post('/api/live', {}); } catch (e) { toast(e.message); } });
$('process').addEventListener('click', async () => {
  const id = qualitySessionId;
  if (!id || id !== selectedSessionId) return;
  renderQualityJob(state?.job, true);
  try {
    const job = await post('/api/process', { session_id:id });
    if (selectedSessionId === id) renderQualityJob(job);
    await pollState();
  } catch (error) {
    if (selectedSessionId === id) {
      renderQualityJob(state?.job);
      $('qcJobMessage').textContent = error.message;
    }
  }
});
$('exportBtn').addEventListener('click', async () => {
  const id = $('sessionSelect').value; if (!id) return toast('请选择会话');
  try {
    const r = await fetch(capturePath('/api/export?id=' + encodeURIComponent(id)), { headers: { 'X-Capture-Token': TOKEN } });
    const b = await r.json();
    if (!r.ok) throw new Error(b.error);
    $('loaderResult').textContent='导出完成：\n'+JSON.stringify(b,null,1);
    $('loaderResult').classList.add('show');
  } catch (e) { toast(e.message); }
});
/* ---- split polling loops (2026-09-17) ----
   Before: one setInterval(poll, 600) fetched the heavy /api/state for
   everything, throttling both live preview and tactile display to ~1.6 Hz.
   Now:
     - pollMeta   (~70 ms): tiny /api/live/meta + fresh JPEG frames per camera.
     - pollTouch  (~50 ms): cursor drain of /api/live/touch (server samples
       ~20 Hz), so short presses survive; sensor bars/badge update here too.
     - pollState  (2 s):    full /api/state for device status, link counters,
       pose message, job panel, sessions stats.
   Each loop has an in-flight guard; UI shows 数据过期 flags when a loop
   keeps failing. Raw capture is untouched (server-side only). */
const touchBuf = [];                     // drained [seq, mono, score] samples
let touchCursor = 0;
let metaBusy = false, touchBusy = false, stateBusy = false;
let metaStaleSince = 0, touchStaleSince = 0, stateStaleSince = 0;

function markStale(sinceVar, el) {
  const stale = sinceVar && Date.now() - sinceVar > 3000;
  const tag = $(el);
  if (tag) tag.classList.toggle('hidden', !stale);
  return stale;
}

/* An auxiliary panel failing must not silently hide the primary output.
   Regression 2026-09-20: renderEmfPose threw on a missing #emfPoseTag id and
   the shared try/catch in pollTouch swallowed it, so the joint angles and the
   25-point skeleton were never rendered even though the server sent them. */
function safeRender(label, fn) {
  try { fn(); } catch (e) { console.error(label + ' 渲染失败：', e); }
}

async function pollMeta() {
  if (metaBusy) return; metaBusy = true;
  const recordingRevision = liveQrRecordingRevision;
  let recording = null;
  try {
    const m = await api('/api/live/meta');
    recording = Boolean(m.recording);
    metaStaleSince = 0;
    const replaying = m.mode === 'replay';
    $('modeTag').textContent = replaying ? '回放模式（无触觉）' : '实时模式';
    $('modeBadge').textContent = replaying ? '离线回放' : '实时';
    $('notice').textContent = m.notice || '';
    if (state) { state.mode = m.mode; state.recording = m.recording; }
    const rec = m.recording;
    if (rec) {
      $('recordTimer').textContent = rec.seconds.toFixed(1) + ' s · ' + fmtBytes(rec.bytes);
      setRecordingButtons(true);
      $('recordingInfo').textContent = '会话 ' + rec.session_id + (rec.limits_enabled === false ? ' · 不限额录制' : '') + ' · 原始数据流写入本地';
    } else {
      $('recordTimer').textContent = '未录制';
      setRecordingButtons(false);
      $('recordingInfo').textContent = '';
    }
    for (const cam of [0, 1]) {
      const img = $('cam' + cam), empty = $('empty' + cam);
      if (m.previews_present[cam] && !replaying) {
        img.src = capturePath('/frame/' + cam + '.jpg?t=' + m.server_mono_ns);
        img.style.display = 'block'; empty.style.display = 'none';
        const hz = (m.preview_hz || [])[cam] || 0;
        $('resolution' + cam).textContent =
          (m.cam_shape[cam] ? m.cam_shape[cam][0] + 'x' + m.cam_shape[cam][1] + ' · ' : '') +
          '实测 ' + hz.toFixed(0) + ' fps（显示流）';
      } else {
        img.style.display = 'none'; empty.style.display = 'block';
      }
    }
  } catch (e) {
    if (!metaStaleSince) metaStaleSince = Date.now();
  } finally {
    if (recordingRevision === liveQrRecordingRevision) setLiveQrRecording(recording);
    markStale(metaStaleSince, 'previewStale');
    metaBusy = false;
  }
}

async function pollTouch() {
  if (touchBusy) return; touchBusy = true;
  try {
    const t = await api('/api/live/touch?cursor=' + touchCursor);
    touchStaleSince = 0;
    touchCursor = t.cursor;
    if (t.window_reset) touchBuf.length = 0;
    for (const s of t.samples) touchBuf.push(s);
    while (touchBuf.length > 600) touchBuf.shift();
    drawTouch(touchBuf);
    if (t.glove && state) {
      state.glove = Object.assign({}, state.glove, t.glove);
      const g = t.glove;
      $('touchBadge').textContent = g.baseline_ready
        ? ('基线已保存（相对响应' + (g.pressure_baseline_ready ? ' + 压感静置基线' : '；压感静置基线缺失') + '）')
        : (g.calibrating ? '正在采集基线…' : '等待基线');
      // Primary output first: the 24 joint angles and the 25-point skeleton must
      // render even if an auxiliary panel below throws (see safeRender note).
      renderJointCommand(g);
      // 25-point skeleton: the wireframe viewport is the primary pose output,
      // the numeric node rows live in the collapsed <details> below it.
      // The cam0 mode draws from its own cached payload, so it must redraw even while the glove
      // has no pose; the EMF tactile layer rides on the same glove frame as the pose. The third
      // condition covers a bridge that has never emitted a pose at all: the EMF gate drops every
      // frame before the bridge can emit one, and without it this card would keep its "waiting"
      // text forever, which is the silent case the card exists to remove.
      if (g.skeleton_pose || handView.frame === 'cam0' || handPoseStallReason()) safeRender('手部骨骼可视化', () => {
        drawHandSkeleton(g.skeleton_pose || null);
        renderHandReadout(g.skeleton_pose || null);
      });
      if (handView.frame === 'cam0') safeRender('双眼投影检查', () => drawHandStereo());
      if (g.values) safeRender('传感器条', () => drawSensors(Object.assign({}, state.glove, g)));
      if (g.pressure) safeRender('压感网格', () => drawPressure(g.pressure));
      if (g.emf) safeRender('EMF 姿态', () => renderEmfPose(g.emf));
      if (g.score != null) {
        let topFinger = '';
        if (g.finger_scores) {
          const best = Object.entries(g.finger_scores).sort((a, b) => b[1] - a[1])[0];
          if (best && best[1] > 2) topFinger = ' · 最强 ' + (FINGER_ZH[best[0]] || best[0]);
        }
        $('touchValue').textContent =
          '相对响应 ' + g.score.toFixed(1) + topFinger + ' · 原始相对值，尚未换算为压力';
      }
    }
    if (!t.glove_stale) markStale(0, 'touchStale');
  } catch (e) { if (!touchStaleSince) touchStaleSince = Date.now(); }
  finally { markStale(touchStaleSince, 'touchStale'); touchBusy = false; }
}

async function pollState() {
  if (stateBusy) return; stateBusy = true;
  try {
    state = await api('/api/state');
    $('standaloneLinks').hidden = !state.standalone;
    TOKEN = state.token;
    stateStaleSince = 0;
    renderState();
  } catch (e) { /* server restarting or idle; retry */ if (!stateStaleSince) stateStaleSince = Date.now(); }
  finally { stateBusy = false; }
}

pollState().then(() => Promise.all([loadSessions(), loadImports()])).catch(error => toast(error.message));
setInterval(pollMeta, 70);      // ≈14 Hz meta poll; actual fps is capped by the
setInterval(pollTouch, 50);     // server preview encoder / glove sample rate
setInterval(pollState, 2000);

/* ---- 04 glove channel-localization probe (UA-P2-04) ----
   Operator-driven evidence tool: candidate channels only, never auto-confirm. */
const PROBE_POSITIONS = ['thumb_tip','index_tip','middle_tip','ring_tip','pinky_tip',
  'thumb_pad','index_pad','middle_pad','ring_pad','pinky_pad','palm_center','palm_heel',
  'hand_neutral','hand_rotate','table_slide'];
const PROBE_POSITION_ZH = {thumb_tip:'拇指尖',index_tip:'食指尖',middle_tip:'中指尖',ring_tip:'无名指尖',
  pinky_tip:'小指尖',thumb_pad:'拇指肚',index_pad:'食指肚',middle_pad:'中指肚',ring_pad:'无名指肚',
  pinky_pad:'小指肚',palm_center:'掌心',palm_heel:'掌根',hand_neutral:'手部中立',
  hand_rotate:'手旋转',table_slide:'桌面滑动',hand_relaxed:'正常放松手掌',
  hand_fist:'手掌握拳',fingers_together:'五指并拢'};
const PROBE_COLORS = ['#168764','#3a9e8f','#bc694a','#7d6bb0','#b09a3a','#5b8fbf',
  '#9e3a63','#4f9e3a','#8a8a8a','#2f4858','#c2703a','#3a6e9e'];
let probeResult = null, probeTrial = null;

function probeInitPos() {
  $('probePos').replaceChildren(...PROBE_POSITIONS.map(p => {
    const o = document.createElement('option');
    o.value = p; o.textContent = p + ' · ' + PROBE_POSITION_ZH[p];
    return o;
  }));
}

function drawProbeChart(t) {
  const c = $('probeTrialCanvas'), ctx = c.getContext('2d'), w = c.width, h = c.height;
  ctx.clearRect(0, 0, w, h);
  const curve = (t && t.curve) || [];
  if (!curve.length) { ctx.fillStyle = '#93a3b0'; ctx.fillText('尚无曲线数据', 10, 20); return; }
  const n = curve.length, ch = curve[0].length;
  let lo = 0, hi = 1;
  for (const row of curve) for (const v of row) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  const span = Math.max(1, hi - lo);
  // With 54 channels a full overlay is unreadable: draw only the 12 channels
  // with the largest |peak_abs_delta| (fall back to all when unspecified).
  let shown = [...Array(ch).keys()];
  if (t && t.peak_abs_delta && t.peak_abs_delta.length === ch) {
    shown = t.peak_abs_delta.map((v, i) => [i, Math.abs(v)]).sort((a, b) => b[1] - a[1])
      .slice(0, 12).map(o => o[0]);
  }
  for (const i of shown) {
    ctx.strokeStyle = PROBE_COLORS[i % PROBE_COLORS.length]; ctx.beginPath();
    for (let j = 0; j < n; j++) {
      const x = 4 + (w - 8) * j / (n - 1), y = h - 6 - (h - 16) * (curve[j][i] - lo) / span;
      j ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.stroke();
  }
  ctx.fillStyle = '#93a3b0';
  ctx.fillText('范围 ' + lo.toFixed(0) + '–' + hi.toFixed(0) + '（相对基线字节值；未标定）', 6, 12);
}

function drawProbeBars(peaks, dirs) {
  const c = $('probeTrialBars'), ctx = c.getContext('2d'), w = c.width, h = c.height;
  ctx.clearRect(0, 0, w, h);
  if (!peaks || !peaks.length) { ctx.fillStyle = '#93a3b0'; ctx.fillText('尚无峰值数据', 10, 20); return; }
  const order = peaks.map((v, i) => [i, Math.abs(v)]).sort((a, b) => b[1] - a[1]).slice(0, 12);
  const mx = Math.max(1, ...order.map(o => o[1]));
  ctx.font = '10px Segoe UI';
  order.forEach(([i, v], k) => {
    const y = 8 + k * 9.2, bw = (w - 130) * v / mx;
    ctx.fillStyle = PROBE_COLORS[i % PROBE_COLORS.length];
    ctx.fillRect(96, y, Math.max(1, bw), 6);
    ctx.fillStyle = dirs && dirs[i] === 'down' ? '#ef9a9a' : '#e7edf2';
    ctx.fillText('CH' + i + (dirs && dirs[i] === 'down' ? ' ↓' : ' ↑'), 40, y + 6);
    ctx.fillStyle = '#93a3b0'; ctx.fillText(v.toFixed(0), 100 + bw + 4, y + 6);
  });
}

function probeChip(text, cls) {
  const s = document.createElement('span');
  s.className = 'probe-chip ' + (cls || ''); s.textContent = text; return s;
}

function renderProbe() {
  const r = probeResult;
  if (!r) return;
  $('probeVersionTag').textContent = '映射版本 ' + (r.version || 0) + ' · ' +
    (r.source === 'operator_confirmed_via_channel_localization_probe' ? '操作员已确认' : '未确认');
  const b = r.baseline;
  $('probeBaselineTag').textContent = b ? ('基线 ' + b.captured_at) : '基线未采集';
  const st = $('probeStatuses'); st.replaceChildren();
  const STATUS_ZH = {baseline_zeroing:'基线清零', position_mapping:'位置映射',
    contact_detection:'接触检测', force_calibration:'力学标定'};
  Object.entries(r.statuses || {}).forEach(([k, v]) =>
    st.append(probeChip((STATUS_ZH[k] || k) + '：' + String(v).replace(/_/g, ' '))));
  const t = probeTrial;
  if (t && t.trial_id) {
    const pa = t.peak_abs_delta || [];
    $('probeTrialInfo').textContent = '最近一次：' + (PROBE_POSITION_ZH[t.position] || t.position) +
      ' · ' + t.trial_type + ' · ' + t.samples + ' 样本 · ' + t.captured_at +
      ' · 峰值|Δ| CH' + pa.indexOf(Math.max(...pa, 0));
    drawProbeChart(t); drawProbeBars(t.peak_abs_delta, t.direction);
  }
  const box = $('probeCandidates'); box.replaceChildren();
  const cands = Object.entries(r.candidates || {});
  if (!cands.length) {
    const p = document.createElement('p'); p.className = 'muted';
    p.textContent = '尚无候选：每个位置需要 ≥2 次按压试验，另需 ≥2 次 rest 对照。'; box.append(p);
  }
  cands.forEach(([pos, c]) => {
    const row = document.createElement('div'); row.className = 'probe-row';
    let txt = '<b>' + (PROBE_POSITION_ZH[pos] || pos) + '</b> <span class="dim">(' + c.press_trials + ' 次按压)</span> · ';
    if (c.control_status !== 'ok') txt += '<strong>缺 rest 对照，不给压力候选</strong> · ';
    txt += 'z候选 [' + (c.responder_channels_z || []).join(',') + '] · 可复现 [' +
      (c.repeatable_channels || []).join(',') + '] · 压力样 [' +
      (c.pressure_like_channels || []).join(',') + ']';
    row.innerHTML = txt;
    box.append(row);
  });
  renderProbeDiscovery(r);
  renderProbeLabels(r);
  renderProbeMatrix(r);
  const audit = r.baseline && r.baseline.decode_audit;
  if (audit && audit.frames) {
    $('probeDecodeAudit').innerHTML = '每帧 ' + audit.frame_bytes + ' 字节 × ' + audit.frames + ' 帧<br>' +
      JSON.stringify(audit.channels || audit, null, 1).slice(0, 900);
  }
}

async function probeRefresh() {
  try { probeResult = await api('/api/probe/report'); renderProbe(); }
  catch (e) { /* devices not ready yet; next tick retries */ }
}

function renderProbeDiscovery(r) {
  const disc = r.discovery || {};
  const dbox = $('probeDiscovery'); dbox.replaceChildren();
  if (!(disc.bytes || []).length) return;
  const head = document.createElement('div'); head.className = 'probe-disc';
  head.innerHTML = '全帧扫描（响应最强的 ' + disc.bytes.length + ' 个字节）：' +
    (disc.strongest_outside_selected_offsets
      ? '<strong>最强响应字节在选定偏移之外 → 解码假设可能不完整，需重新审计</strong>' : '最强响应仍在选定偏移内');
  dbox.append(head);
  disc.bytes.forEach(x => {
    const row = document.createElement('div'); row.className = 'probe-row';
    row.innerHTML = '字节 ' + x.byte + (x.in_selected_offsets ? '（选定偏移）' : '<strong>（偏移外）</strong>') +
      ' · z中位 ' + x.median_p90_z + ' · 位置 ' + x.positions.map(p => PROBE_POSITION_ZH[p] || p).join('/');
    dbox.append(row);
  });
}

function renderProbeLabels(r) {
  const lab = $('probeLabels'); const labels = r.labels || [];
  if (lab.childElementCount !== labels.length) {
    lab.replaceChildren(...labels.map((v, i) => {
      const l = document.createElement('label');
      l.textContent = 'CH' + i + '（' + (r.offsets ? r.offsets[i] : '?') + '）';
      const inp = document.createElement('input'); inp.value = v; inp.maxLength = 20;
      l.append(inp); return l;
    }));
  } else {
    [...lab.querySelectorAll('input')].forEach((inp, i) => { if (inp.value !== labels[i]) inp.value = labels[i]; });
  }
}

function renderProbeMatrix(r) {
  const m = $('probeMatrix');
  if (!(r.matrix || []).length) return;
  const rows = r.matrix.map(row => {
    const d = document.createElement('div'); d.className = 'probe-row';
    const peaks = row.median_peak || row.peak_delta || [];
    const ch = row.channels || peaks.length;
    d.innerHTML = '<b>' + (PROBE_POSITION_ZH[row.position] || row.position) + '</b> · ' +
      row.trial_type + ' · ' + ch + 'CH · ' + (row.trials || 1) + ' 次 · 阈值响应 ' +
      (peaks.map((v, i) => Math.abs(v) >= r.on_delta ? 'CH' + i : '').filter(Boolean).join(',') || '无') +
      (row.captured_at ? ' <span class="dim">' + row.captured_at + '</span>' : '');
    return d;
  });
  if (r.legacy_rows) {
    const note = document.createElement('div'); note.className = 'muted';
    note.textContent = '另有 ' + r.legacy_rows + ' 行旧通道数记录（不同解码时代），仅展示、不参与候选计算。';
    rows.unshift(note);
  }
  m.replaceChildren(...rows);
}

/* ---- probe actions ---- */
probeInitPos();
probeRefresh();
setInterval(() => { if (!$('calib').hidden && $('probe').open) probeRefresh().catch(error => toast(error.message)); }, 3000);
$('probeRefresh').addEventListener('click', probeRefresh);
$('probeBaseline').addEventListener('click', async () => {
  try {
    probeTrial = (await post('/api/probe/baseline', { seconds: 3 })).baseline;
    toast('定位基线已保存：相对字节基线，不是压力标定');
    probeRefresh();
  } catch (e) { toast(e.message); }
});
$('probePress').addEventListener('click', async () => {
  try {
    probeTrial = (await post('/api/probe/trial', { position: $('probePos').value, trial_type: 'press', seconds: 4 })).trial;
    toast('按压试验已记录 · ' + $('probePos').value);
    probeRefresh();
  } catch (e) { toast(e.message); }
});
$('probeBend').addEventListener('click', async () => {
  try {
    probeTrial = (await post('/api/probe/trial', { position: $('probePos').value, trial_type: 'bend_control', seconds: 4 })).trial;
    toast('弯曲对照已记录'); probeRefresh();
  } catch (e) { toast(e.message); }
});
$('probeRest').addEventListener('click', async () => {
  try {
    probeTrial = (await post('/api/probe/trial', { position: $('probePos').value, trial_type: 'rest', seconds: 4 })).trial;
    toast('Rest 对照已记录：手保持不动，不按压'); probeRefresh();
  } catch (e) { toast(e.message); }
});
async function captureHandState(position, trialType, label) {
  try {
    probeTrial = (await post('/api/probe/trial', { position, trial_type: trialType, seconds: 4 })).trial;
    toast(label + '已记录'); probeRefresh();
  } catch (e) { toast(e.message); }
}
$('probeRelaxed').addEventListener('click', () => captureHandState('hand_relaxed', 'rest_pose', '正常放松手掌静置'));
$('probeFist').addEventListener('click', () => captureHandState('hand_fist', 'fist_pose', '手掌握拳'));
$('probeTogether').addEventListener('click', () => captureHandState('fingers_together', 'together_pose', '五指并拢'));
$('probeSaveMap').addEventListener('click', async () => {
  const labels = [...$('probeLabels').querySelectorAll('input')].map(i => i.value.trim() || 'unknown');
  try {
    const x = await post('/api/probe/mapping', { labels, note: $('probeNote').value });
    toast('映射已保存，版本 ' + x.version + '（操作员确认记录）');
    probeRefresh();
  } catch (e) { toast(e.message); }
});

/* ---- one-click guided probe sequence ----
   Server runs a fixed step script (baseline, bend controls, tip/pad presses,
   palm, rotation, slide, closing rest). Each step is a real capture; the UI
   only shows the cue and a local countdown synced via step_end_epoch. */
let seqPollTimer = null;

function probeSeqRender(st) {
  const box = $('probeSeqStatus');
  if (!st || (!st.running && !(st.results || []).length)) {
    box.innerHTML = '<span class="muted">尚未运行。一键序列共 24 步：放松基线 → 放松/握拳/五指并拢 → 逐指弯曲对照 → 逐指尖/指肚按压 → 掌心/掌根 → 手旋转/桌面滑动 → 收尾静置。每步 3 秒采集 + 1 秒准备。</span>';
    $('probeSeqTag').textContent = '未运行';
    $('probeSeqStart').disabled = false; $('probeSeqStop').disabled = true;
    return;
  }
  let html = '';
  const cur = st.current;
  if (st.running && cur) {
    const remain = Math.max(0, cur.step_end_epoch - Date.now() / 1000);
    const frac = Math.max(0, Math.min(1, 1 - remain / (cur.seconds + (cur.gap || 0))));
    html += '<div class="seq-cue"><b>第 ' + (cur.index + 1) + '/' + st.total + ' 步</b> ' + cur.instruction + '</div>' +
      '<div class="seq-timer">' + remain.toFixed(1) + ' <small>s</small></div>' +
      '<div class="seq-bar"><div style="width:' + (frac * 100).toFixed(1) + '%"></div></div>' +
      '<div class="muted">' + (cur.status === 'preparing' ? '准备中…' : '采集中…') + '</div>';
  } else if (st.running) {
    html += '<div class="muted">序列启动中…</div>';
  } else {
    const oks = (st.results || []).filter(r => r.ok).length;
    html += '<div><b>序列' + (st.stopped ? '已手动停止' : '结束') + '</b> · 成功 ' + oks +
      ' / 失败 ' + ((st.results || []).length - oks) + '</div>';
  }
  const rows = (st.results || []).map(r =>
    '<div class="probe-row">' + (r.ok ? '✓' : '<strong>✗ ' + (r.error || '失败') + '</strong>') +
    ' · 第' + (r.index + 1) + '步 · ' + r.instruction + '</div>').join('');
  $('probeSeqResults').innerHTML = rows || '<span class="muted">尚无完成步骤。</span>';
  $('probeSeqStart').disabled = !!st.running;
  $('probeSeqStop').disabled = !st.running;
  $('probeSeqTag').textContent = st.running ? '运行中 · 第 ' + ((st.index ?? -1) + 1) + '/' + st.total + ' 步'
    : (st.stopped ? '已停止' : '已结束');
  box.innerHTML = html;
}

function probeSeqLoop() {
  if (seqPollTimer) return; // already polling
  const tick = async () => {
    let st = null;
    try { st = await api('/api/probe/sequence'); } catch (e) { /* retry next tick */ }
    probeSeqRender(st);
    if (st && st.running) seqPollTimer = setTimeout(tick, 250);
    else { seqPollTimer = null; if (st && (st.results || []).length) probeRefresh(); }
  };
  seqPollTimer = setTimeout(tick, 0);
}

$('probeSeqStart').addEventListener('click', async () => {
  try {
    await post('/api/probe/sequence/start', {});
    toast('序列已开始：跟随屏幕提示做动作');
    probeSeqLoop();
  } catch (e) { toast(e.message); }
});
$('probeSeqStop').addEventListener('click', async () => {
  try { await post('/api/probe/sequence/stop', {}); toast('将在当前步采集结束后停止'); }
  catch (e) { toast(e.message); }
});
probeSeqLoop(); // resume the countdown if the page was reloaded mid-sequence

/* ---- motion-bank calibration (evidence-only axis grouping) ----
   Same runner pattern as the probe sequence: the server executes a fixed
   script (rest -> single-axis slow rotation -> rest), the UI only shows the
   cue and a local countdown, then renders the automatic grouping result.
   Everything shown is evidence only; no motion byte feeds any feature. */
let mcalPollTimer = null;

function mcalRenderResult(st) {
  const box = $('mcalResult');
  const res = st && st.result;
  if (!res) { box.innerHTML = '尚无标定结果。'; return; }
  if (res.error) { box.innerHTML = '<div>分析未完成：' + (res.detail || res.error) + '</div>'; return; }
  let html = '<div><b>候选轴分组（仅证据）</b> · 活动字节 ' + (res.active_bytes || []).length +
    ' 个 · 分组 ' + (res.groups || []).length + ' 个 · 静置帧 ' + res.rest_frames + '</div>';
  html += (res.groups || []).map(g => {
    const shown = g.bytes.slice(0, 16).join(',');
    const more = g.bytes.length > 16 ? '…共 ' + g.bytes.length + ' 个' : '';
    const bias = g.rest_bias.slice(0, 8).map(v => Math.round(v)).join('/');
    const stepP90 = Object.keys(g.per_step_p90).map(k =>
      k + ':' + Math.round(Math.max.apply(null, g.per_step_p90[k]))).join(' · ');
    return '<div class="probe-row"><b>' + (g.flag_like ? '标志簇' : '候选轴组') +
      '</b> · 字节 ' + shown + more + ' · 偏置 ' + bias +
      ' · 各步响应 ' + stepP90 +
      (g.u16_pairs.length ? ' · u16对 ' + g.u16_pairs.map(p => p.join('+')).join(',') : '') +
      '</div>';
  }).join('');
  if (!(res.groups || []).length) {
    const d = res.diagnostic || {}, dr = d.rest_drift_summary || {};
    const al = d.activity_lift || {}, top = (d.top_candidates || [])[0];
    if ((al.bytes || []).length) {
      const a = (al.top || [])[0] || {};
      const mp = Math.max.apply(null, Object.keys(a.motion_activity || {}).map(k => a.motion_activity[k]).concat([0]));
      html += '<div class="probe-row"><b>未发现可标定运动轴，但检测到运动闸门字节</b> ' +
        (al.bytes || []).join(',') + ' · 静止偏离帧占比 ≤ ' + a.rest_activity + ' → 运动最小 ' +
        a.motion_frac_min + ' · 运动 p90 最高 ' + mp + '（中位数不变）' +
        '<br><span class="muted">含义：这几个字节只在手运动时抖动，是"手在动/没动"的活动信号，不是关节角度，' +
        '不能当作轴读数；可用于剔除被运动污染的触觉帧。证据等级：仅证据，未接入任何特征。</span></div>';
    } else {
      html += '<div class="probe-row"><b>也未发现运动闸门字节</b> · 判据：要求某字节在' +
        '<b>每一个</b>静止窗口都安静（偏离帧占比 ≤ ' + (al.rest_frame_ceiling === undefined ? '?' : al.rest_frame_ceiling) +
        '）、且在<b>每一个</b>运动窗口都活跃（占比 ≥ ' + (al.motion_frame_floor === undefined ? '?' : al.motion_frame_floor) +
        '，p90 抬升 ≥ ' + (al.lift_floor === undefined ? '?' : al.lift_floor) + ' 计数）。' +
        '<br><span class="muted">用最坏窗口而不是中位窗口，因为一个窗口可能只是采到某个慢方波的相位；' +
        '未解码尾部的整体活动量在静止步与运动步上同样高，因此本帧既没有轴，也没有干净的"手在动"闸门。' +
        '证据等级：仅证据，未接入任何特征。</span></div>';
    }
    html += '<div class="probe-row"><b>未发现运动锁定字节</b> · 静止漂移 中位/最大 ' +
      (dr.median === undefined ? '?' : dr.median) + '/' + (dr.max === undefined ? '?' : dr.max) +
      ' 计数 · 最强候选 ' +
      (top ? 'b' + top.byte + '（drift_score ' + top.drift_score + '，自身静止漂移 ' + top.rest_drift + '）'
           : '无') +
      '<br><span class="muted">判据：某字节在运动步的中位数偏移必须明显超过它自己在相邻静止步之间的漂移；' +
      '未解码尾部（466–547）不是稳定偏置区，因此本帧内不存在可标定的运动轴。原始帧已保存，结论可复算。</span></div>';
  }
  box.innerHTML = html;
}

function mcalRender(st) {
  const box = $('mcalStatus');
  if (!st || (!st.running && !(st.results || []).length && !st.result)) {
    box.innerHTML = '<span class="muted">尚未运行。9 步：静止参考 → 前臂旋转 → 腕上下摆 → 腕左右摆 → 整臂平移，各步之间回到静止。每步 4 秒采集 + 1 秒准备。</span>';
    $('mcalTag').textContent = '未运行';
    $('mcalStart').disabled = false; $('mcalStop').disabled = true;
    mcalRenderResult(st);
    return;
  }
  let html = '';
  const cur = st.current;
  if (st.running && cur) {
    const remain = Math.max(0, cur.step_end_epoch - Date.now() / 1000);
    const frac = Math.max(0, Math.min(1, 1 - remain / (cur.seconds + (cur.gap || 0))));
    html += '<div class="seq-cue"><b>第 ' + (cur.index + 1) + '/' + st.total + ' 步</b> ' + cur.instruction + '</div>' +
      '<div class="seq-timer">' + remain.toFixed(1) + ' <small>s</small></div>' +
      '<div class="seq-bar"><div style="width:' + (frac * 100).toFixed(1) + '%"></div></div>' +
      '<div class="muted">' + (cur.status === 'preparing' ? '准备中…' : '采集中…') + '</div>';
  } else if (st.running) {
    html += '<div class="muted">标定启动中…</div>';
  } else {
    const oks = (st.results || []).filter(r => r.ok).length;
    html += '<div><b>标定' + (st.stopped ? '已手动停止' : '结束') + '</b> · 成功 ' + oks +
      ' / 失败 ' + ((st.results || []).length - oks) +
      (st.error ? ' · <strong>✗ ' + st.error + '</strong>' : '') + '</div>';
  }
  const rows = (st.results || []).map(r =>
    '<div class="probe-row">' + (r.ok ? '✓ ' + r.frames + '帧' : '<strong>✗ ' + (r.error || '失败') + '</strong>') +
    ' · 第' + (r.index + 1) + '步 · ' + r.instruction + '</div>').join('');
  $('mcalResults').innerHTML = rows || '<span class="muted">尚无完成步骤。</span>';
  $('mcalStart').disabled = !!st.running;
  $('mcalStop').disabled = !st.running;
  $('mcalTag').textContent = st.running ? '运行中 · 第 ' + ((st.index ?? -1) + 1) + '/' + st.total + ' 步'
    : (st.stopped ? '已停止' : '已结束');
  box.innerHTML = html;
  mcalRenderResult(st);
}

function mcalLoop() {
  if (mcalPollTimer) return; // already polling
  const tick = async () => {
    let st = null;
    try { st = await api('/api/motion/calibration'); } catch (e) { /* retry next tick */ }
    mcalRender(st);
    if (st && st.running) mcalPollTimer = setTimeout(tick, 250);
    else mcalPollTimer = null;
  };
  mcalPollTimer = setTimeout(tick, 0);
}

$('mcalStart').addEventListener('click', async () => {
  try {
    await post('/api/motion/calibration/start', {});
    toast('运动标定已开始：跟随屏幕提示做动作');
    mcalLoop();
  } catch (e) { toast(e.message); }
});
$('mcalStop').addEventListener('click', async () => {
  try { await post('/api/motion/calibration/stop', {}); toast('将在当前步采集结束后停止'); }
  catch (e) { toast(e.message); }
});
mcalLoop(); // resume the countdown if the page was reloaded mid-run

/* ---- 25-point hand skeleton viewport ---- */
bindHandView();
