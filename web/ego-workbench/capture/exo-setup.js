/* Associate calibrated exterior cameras with the selected recording. */
(() => {
  const panel = document.getElementById('exoSetup');
  if (!panel) return;
  const rows = document.getElementById('exoRows');
  const session = document.getElementById('calibSyncSession');
  const status = document.getElementById('exoStatus');
  const result = document.getElementById('exoReport');
  const ready = document.getElementById('exoReady');
  const cameraResults = document.getElementById('exoCameraResults');
  const square = document.getElementById('exoSquareMm');
  const start = document.getElementById('processExo');
  const add = document.getElementById('addExo');
  let catalog = {videos: [], profiles: []};
  let busy = false;
  let shownCompletion;

  function select(label, empty, field, options) {
    const wrap = document.createElement('label');
    wrap.append(document.createTextNode(label));
    const control = document.createElement('select');
    control.dataset.field = field;
    control.dataset.empty = empty;
    control.dataset.options = options;
    wrap.append(control);
    return wrap;
  }

  function fillPicks() {
    for (const control of rows.querySelectorAll('select')) {
      const current = control.value;
      const choices = catalog[control.dataset.options];
      control.replaceChildren(new Option(control.dataset.empty, ''),
        ...choices.map(name => new Option(name, name)));
      control.value = choices.includes(current) ? current : '';
    }
  }

  function currentSetup() {
    return [...rows.children].map(card => {
      const values = Object.fromEntries([...card.querySelectorAll('select')]
        .map(control => [control.dataset.field, control.value]));
      return {card, enabled:card.querySelector('input').checked, details:card.querySelector('output'),
        payload:{role:card.dataset.role, video:values.video, table_video:values.table_video,
          profile:values.profile, intrinsic_video:values.profile ? '' : values.intrinsic_video},
        missing:[
          !values.video && '原始录像',
          !values.table_video && '固定板录像',
          !(values.profile || values.intrinsic_video) && '内参档案或内参录像'
        ].filter(Boolean)};
    });
  }

  function render() {
    const setup = currentSetup();
    const active = setup.filter(row => row.enabled);
    const selected = session.selectedOptions[0];
    const complete = active.filter(row => !row.missing.length);
    const files = active.map(row => row.payload.video).filter(Boolean);
    const duplicate = new Set(files).size !== files.length;
    const issue = !catalog.videos.length ? '导入目录暂无视频。复制文件后点「刷新文件」。'
      : !session.value ? '请选择上方已整理的会话。'
      : selected?.dataset.processed === 'false' ? '这条录制待整理。先到录制目录完成整理。'
      : !active.length ? '至少启用一路 EXO。'
      : duplicate ? '各路 EXO 必须选择不同的原始录像。'
      : !square.checkValidity() || !Number.isFinite(square.valueAsNumber) ? '请输入标定板的实测格长。'
      : complete.length !== active.length ? complete.length + '/' + active.length + ' 路文件齐全'
      : '';
    start.disabled = busy || Boolean(issue);
    ready.textContent = busy ? '正在处理，完成后会显示逐路结果。'
      : issue || '文件齐全，可计算 ' + active.length + ' 路 EXO 标定与对齐。';
    add.disabled = busy || rows.children.length >= 8;
    for (const {card,enabled,details,payload,missing} of setup) {
      card.querySelector('input').disabled = busy;
      for (const input of card.querySelectorAll('select')) {
        input.disabled = busy || !enabled || (input.dataset.field === 'intrinsic_video' && !!payload.profile);
      }
      details.dataset.ready = String(enabled && !missing.length);
      details.textContent = !enabled ? '已停用' : missing.length ? '待选：' + missing.join('、') : '文件齐全';
    }
  }

  function appendCamera() {
    const number = rows.children.length + 1;
    const card = document.createElement('fieldset');
    card.className = 'card exo-camera';
    card.dataset.role = 'exo-' + number;
    const legend = document.createElement('legend');
    const enabled = document.createElement('input');
    enabled.type = 'checkbox';
    enabled.checked = true;
    const named = document.createElement('label');
    named.append(enabled, ' EXO ' + number);
    legend.append(named);
    card.append(legend,
      select('原始录像 · 动作与首尾二维码', '例如 exo' + number + '-task.mp4', 'video', 'videos'),
      select('桌面标定板录像 · 板固定', '例如 exo' + number + '-table.mp4', 'table_video', 'videos'),
      select('内参档案', '没有档案时选下方录像', 'profile', 'profiles'),
      select('内参录像 · 从多个角度拍板', '例如 exo' + number + '-intrinsics.mp4', 'intrinsic_video', 'videos'),
      document.createElement('output'));
    card.addEventListener('change', render);
    rows.append(card);
    fillPicks();
    render();
  }

  async function refresh() {
    try {
      const id = session.value;
      const incoming = await api('/api/exo/options?' + new URLSearchParams({id}));
      if (id !== session.value) return;
      catalog = incoming;
      setCopyableText('exoVideoDirectory', catalog.video_directory);
      setCopyableText('exoProfileDirectory', catalog.profile_directory);
      fillPicks();
      render();
    } catch (error) { status.textContent = error.message; }
  }

  async function loadReport() {
    const id = session.value;
    result.hidden = true;
    cameraResults.hidden = true;
    cameraResults.replaceChildren();
    if (!id) return;
    try {
      const query = '/api/exo/result?' + new URLSearchParams({id});
      const report = await api(query);
      if (session.value !== id) return;
      if (report.status !== 'processed') {
        status.textContent = '这条会话还没有 EXO 处理结果。';
        return;
      }
      result.href = capturePath(query);
      result.hidden = false;
      for (const camera of report.cameras) {
        const row = document.createElement('li');
        row.textContent = [
          camera.role.toUpperCase() + ' · ' + camera.original_name,
          '桌面标定 RMS ' + camera.validation.holdout_rms_px.toFixed(2) + ' px',
          'QR 残差中位数 ' + camera.clock.median_abs_residual_ms.toFixed(1) + ' ms',
          '已估计偏移和漂移'
        ].join(' ｜ ');
        cameraResults.append(row);
      }
      cameraResults.hidden = false;
      status.textContent = 'EXO 已处理。头戴桌面定位：' + report.ego.cameras.map((eye, index) =>
        'CAM' + index + ' ' + eye.table_pose_frames + '/' + eye.frames).join(' ｜ ');
    } catch (error) {
      if (session.value === id) status.textContent = error.message;
    }
  }

  function reportJob(job) {
    if (!job || job.label !== 'EXO 标定与对齐') return;
    busy = job.status === 'running';
    if (job.session_id === session.value) {
      if (job.status === 'complete') {
        if (shownCompletion !== job.started) {
          shownCompletion = job.started;
          loadReport();
          refresh();
        }
      } else {
        status.textContent = job.message || job.progress || job.error || job.status;
      }
    }
    render();
  }

  add.addEventListener('click', appendCamera);
  document.getElementById('refreshExo').addEventListener('click', refresh);
  document.getElementById('readExoReport').addEventListener('click', loadReport);
  document.querySelector('[data-calib-view="calibClocks"]').addEventListener('click', refresh);
  square.addEventListener('input', render);
  session.addEventListener('change', () => {
    status.textContent = '';
    render();
    refresh();
    loadReport();
  });
  start.addEventListener('click', async () => {
    if (start.disabled) return;
    const cameras = currentSetup().filter(row => row.enabled).map(row => row.payload);
    try {
      busy = true;
      render();
      status.textContent = '正在提交 EXO 标定与对齐…';
      const job = await post('/api/exo/process', {
        session_id:session.value, cameras, square_mm:square.valueAsNumber
      });
      reportJob(job);
    } catch (error) {
      busy = false;
      render();
      status.textContent = error.message;
    }
  });
  window.renderExoJob = reportJob;
  window.refreshExoReadiness = render;
  appendCamera();
  appendCamera();
})()
