/* Associate calibrated exterior cameras with the selected recording. */
(() => {
  const panel = document.getElementById('exoSetup');
  if (!panel) return;
  const rows = document.getElementById('exoRows');
  const session = document.getElementById('calibSyncSession');
  const status = document.getElementById('exoStatus');
  const result = document.getElementById('exoReport');
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

  function render() {
    start.disabled = busy || !session.value || !catalog.videos.length;
    add.disabled = busy || rows.children.length >= 8;
    for (const card of rows.children) {
      const enabled = card.querySelector('input').checked;
      const profile = card.querySelector('[data-field="profile"]').value;
      for (const input of card.querySelectorAll('select')) {
        input.disabled = busy || !enabled ||
          (input.dataset.field === 'intrinsic_video' && !!profile);
      }
      card.querySelector('input').disabled = busy;
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
      select('原始录像', '选择原始 MP4 / MOV', 'video', 'videos'),
      select('桌面标定板录像', '选择固定板录像', 'table_video', 'videos'),
      select('内参档案', '没有档案，使用内参录像', 'profile', 'profiles'),
      select('内参录像', '从多个角度拍标定板', 'intrinsic_video', 'videos'));
    card.addEventListener('change', render);
    rows.append(card);
    fillPicks();
    render();
  }

  async function refresh() {
    try {
      catalog = await api('/api/exo/options');
      document.getElementById('exoVideoDirectory').textContent = catalog.video_directory;
      document.getElementById('exoProfileDirectory').textContent = catalog.profile_directory;
      fillPicks();
      render();
    } catch (error) { status.textContent = error.message; }
  }

  async function loadReport() {
    const id = session.value;
    result.hidden = true;
    if (!id) return;
    try {
      const query = '/api/exo/result?' + new URLSearchParams({id});
      const report = await api(query);
      if (session.value !== id || report.status !== 'processed') return;
      result.href = capturePath(query);
      result.hidden = false;
      const cameras = report.cameras.map(camera =>
        camera.role.toUpperCase() + ' · 桌面重投影 ' +
        camera.validation.holdout_rms_px.toFixed(2) + ' px · QR 残差中位数 ' +
        camera.clock.median_abs_residual_ms.toFixed(1) + ' ms');
      const head = report.ego.cameras.map((eye, index) =>
        'CAM' + index + ' 桌面定位 ' + eye.table_pose_frames + '/' + eye.frames);
      status.textContent = [...cameras, ...head].join(' ｜ ');
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
  session.addEventListener('change', () => {
    status.textContent = '';
    render();
    loadReport();
  });
  start.addEventListener('click', async () => {
    const cameras = [...rows.children].filter(row => row.querySelector('input').checked).map(row => {
      const entry = {role:row.dataset.role};
      for (const control of row.querySelectorAll('select')) {
        if (!control.disabled) entry[control.dataset.field] = control.value;
      }
      return entry;
    });
    if (!cameras.length || cameras.some(c => !c.video || !c.table_video || !(c.profile || c.intrinsic_video))) {
      status.textContent = '每路须选择原始录像和桌面板录像，并提供内参档案或内参录像。';
      return;
    }
    try {
      busy = true;
      render();
      status.textContent = '正在提交 EXO 标定与对齐…';
      const job = await post('/api/exo/process', {
        session_id:session.value, cameras, square_mm:document.getElementById('exoSquareMm').valueAsNumber
      });
      reportJob(job);
    } catch (error) {
      busy = false;
      render();
      status.textContent = error.message;
    }
  });
  window.renderExoJob = reportJob;
  appendCamera();
  appendCamera();
})()
