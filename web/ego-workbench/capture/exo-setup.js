/* Select the stationary camera files for the current recording before processing. */
(() => {
  const session = $('calibSyncSession');
  const rows = $('exoRows');
  const status = $('exoStatus');
  const ready = $('exoReady');
  const save = $('processExo');
  const square = $('exoSquareMm');
  const add = $('addExo');
  const videoPicker = $('chooseIntakeVideo');
  const profilePicker = $('chooseIntakeProfile');
  let catalog = {videos:[], profiles:[]};
  let loadedSession = '';
  let saved = {cameras:[], square_mm:25};
  let busy = false;
  let version = 0;

  function select(label, field, options, placeholder) {
    const wrap = document.createElement('label');
    wrap.append(document.createTextNode(label));
    const input = document.createElement('select');
    input.dataset.field = field;
    if (options) {
      input.dataset.options = options;
      input.dataset.empty = placeholder;
    } else {
      input.replaceChildren(new Option('仅对时', 'clock'), new Option('对时＋桌面标定', 'table'));
    }
    wrap.append(input);
    return wrap;
  }

  function fill() {
    for (const input of rows.querySelectorAll('select[data-options]')) {
      const previous = input.value;
      const choices = catalog[input.dataset.options];
      input.replaceChildren(new Option(input.dataset.empty, ''), ...choices.map(name => new Option(name, name)));
      if (choices.includes(previous)) input.value = previous;
    }
  }

  function choices() {
    return [...rows.children].map(card => {
      const fields = Object.fromEntries([...card.querySelectorAll('select')]
        .map(input => [input.dataset.field, input.value]));
      const timeOnly = fields.mode === 'clock';
      return {card, video:fields.video, timeOnly,
        missing: Boolean(fields.video && !timeOnly && (!fields.table_video || !(fields.profile || fields.intrinsic_video))),
        payload:{role:card.dataset.role, video:fields.video, time_only:timeOnly,
          table_video:timeOnly ? null : fields.table_video,
          intrinsic_video:timeOnly || fields.profile ? null : fields.intrinsic_video,
          profile:timeOnly ? null : fields.profile || null}};
    });
  }

  function selectedPayload() {
    return choices().filter(row => row.video).map(row => row.payload);
  }

  function dirty() {
    return session.value === loadedSession && Boolean(loadedSession) &&
      (JSON.stringify(selectedPayload()) !== JSON.stringify(saved.cameras) || square.valueAsNumber !== saved.square_mm);
  }

  function render() {
    const setup = choices();
    const selected = setup.filter(row => row.video);
    const duplicate = new Set(selected.map(row => row.video)).size !== selected.length;
    const missing = selected.some(row => row.missing);
    const validSession = Boolean(session.value && session.value === loadedSession);
    save.disabled = busy || !validSession || duplicate || missing || !square.checkValidity() || !dirty();
    for (const button of [add, videoPicker, profilePicker]) button.disabled = busy || !validSession;
    add.disabled ||= rows.children.length >= 8;
    ready.textContent = !session.value ? '请选择录制会话。'
      : !validSession ? '正在读取已保存的选择…'
      : duplicate ? '每路 EXO 请选择不同的原片。'
      : missing ? '请选择固定板视频和内参，或改选「仅对时」。'
      : !square.checkValidity() ? '请输入实测格长。'
      : dirty() ? '请保存当前 EXO 选择。'
      : saved.cameras.length ? '已保存 ' + saved.cameras.length + ' 路 EXO。' : '本次可使用头戴双目和手套。';
    for (const row of setup) {
      const tableFields = row.card.querySelector('.exo-table-fields');
      tableFields.hidden = row.timeOnly;
      for (const input of row.card.querySelectorAll('select')) input.disabled = busy || !validSession;
    }
    updateIntakeProceed();
  }

  function appendCamera() {
    const number = rows.children.length + 1;
    const card = document.createElement('fieldset');
    card.className = 'card exo-camera';
    card.dataset.role = 'exo-' + number;
    const legend = document.createElement('legend');
    legend.textContent = 'EXO ' + number;
    const table = document.createElement('div');
    table.className = 'exo-table-fields';
    table.append(select('固定板录像', 'table_video', 'videos', '选择桌面标定板录像'),
      select('内参档案', 'profile', 'profiles', '选择 JSON 或另选内参录像'),
      select('内参录像', 'intrinsic_video', 'videos', '选择多角度拍板录像'));
    card.append(legend, select('原始录像', 'video', 'videos', '不使用'),
      select('处理方式', 'mode'), table);
    card.addEventListener('change', event => {
      if (event.target.dataset.field === 'mode' && event.target.value === 'table') $('exoTableTools').open = true;
      render();
    });
    rows.append(card);
    fill();
  }

  function applySelection(state) {
    saved = {cameras:state.cameras, square_mm:state.square_mm};
    square.value = String(saved.square_mm);
    const count = Math.max(2, ...saved.cameras.map(row => Number(row.role.split('-')[1])));
    while (rows.children.length < count) appendCamera();
    for (const card of rows.children) {
      const row = saved.cameras.find(item => item.role === card.dataset.role);
      for (const input of card.querySelectorAll('select')) {
        const field = input.dataset.field;
        input.value = field === 'mode' ? row && !row.time_only ? 'table' : 'clock' : row?.[field] || '';
      }
    }
  }

  async function refresh({reload = false} = {}) {
    const id = session.value;
    const request = ++version;
    if (!id) {
      catalog = {videos:[], profiles:[]};
      loadedSession = '';
      rows.replaceChildren();
      appendCamera();
      appendCamera();
      render();
      return;
    }
    const needsSelection = reload || loadedSession !== id;
    if (needsSelection) {
      loadedSession = '';
      status.textContent = '';
      if (rows.children.length > 2) {
        rows.replaceChildren();
        appendCamera();
        appendCamera();
      }
    }
    render();
    try {
      const query = '?' + new URLSearchParams({id});
      const [choices, state] = await Promise.all([
        api('/api/exo/options' + query), needsSelection ? api('/api/intake' + query) : null
      ]);
      if (request !== version || session.value !== id) return;
      catalog = choices;
      setCopyableText('exoVideoDirectory', catalog.video_directory);
      setCopyableText('exoProfileDirectory', catalog.profile_directory);
      fill();
      if (state) applySelection(state);
      loadedSession = id;
    } catch (error) {
      if (request === version) status.textContent = error.message;
    } finally {
      if (request === version) render();
    }
  }

  async function pick(field) {
    busy = true;
    render();
    status.textContent = '在电脑上选择要导入的档案。';
    try {
      await post('/api/intake/files', {session_id:session.value, field});
      await refresh();
      refreshQrVideos();
      status.textContent = '在 EXO 卡片选择对应文件。';
    } catch (error) {
      status.textContent = error.message;
    } finally {
      busy = false;
      render();
    }
  }

  async function loadReport() {
    const id = session.value;
    if (!id) return;
    const link = $('exoReport');
    const results = $('exoCameraResults');
    link.hidden = true;
    results.hidden = true;
    results.replaceChildren();
    try {
      const path = '/api/exo/result?' + new URLSearchParams({id});
      const report = await api(path);
      if (id !== session.value) return;
      if (report.status !== 'processed') {
        status.textContent = '这条会话还没有 EXO 处理结果。';
        return;
      }
      link.href = capturePath(path);
      link.hidden = false;
      for (const camera of report.cameras) {
        const row = document.createElement('li');
        const geometry = camera.validation.status === 'time_only' ? '仅对时'
          : '桌面标定 RMS ' + camera.validation.holdout_rms_px.toFixed(2) + ' px';
        row.textContent = [camera.role.toUpperCase(), camera.original_name, geometry,
          'QR 残差 ' + camera.clock.median_abs_residual_ms.toFixed(1) + ' ms'].join(' · ');
        results.append(row);
      }
      results.hidden = false;
      status.textContent = 'EXO 对齐结果';
    } catch (error) {
      if (id === session.value) status.textContent = error.message;
    }
  }

  save.onclick = async () => {
    if (save.disabled) return;
    const id = session.value;
    const cameras = selectedPayload();
    const square_mm = square.valueAsNumber;
    busy = true;
    render();
    try {
      const result = await post('/api/intake', {session_id:id, cameras, square_mm});
      if (id === session.value) {
        applySelection(result);
        status.textContent = 'EXO 选择已保存。';
        refreshIntakeStatus();
      }
    } catch (error) {
      status.textContent = error.message;
    } finally {
      busy = false;
      render();
    }
  };

  add.onclick = () => { appendCamera(); render(); };
  videoPicker.onclick = () => pick('exo_video_files');
  profilePicker.onclick = () => pick('exo_profile_files');
  $('refreshExo').onclick = () => refresh();
  $('readExoReport').onclick = loadReport;
  square.oninput = render;
  window.refreshIntakeExo = refresh;
  window.refreshExoReadiness = render;
  window.intakeExoDirty = dirty;
  window.renderExoJob = job => {
    if (job.session_id === session.value && job.status === 'complete') loadReport();
  };
  appendCamera();
  appendCamera();
})();