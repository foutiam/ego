/* Select the stationary camera files for the current recording before processing. */
(() => {
  const session = $('calibSyncSession');
  const rows = $('exoRows');
  const summary = $('exoSummary');
  const selectedVideos = $('exoSelectedVideos');
  const status = $('exoStatus');
  const ready = $('exoReady');
  const save = $('processExo');
  const selectionActions = $('exoSelectionActions');
  const settings = $('exoCameraSettings');
  const square = $('exoSquareMm');
  const add = $('addExo');
  const videoPicker = $('chooseIntakeVideo');
  const calibrationPicker = $('chooseIntakeCalibrationVideo');
  const profilePicker = $('chooseIntakeProfile');
  const reportStatus = $('exoReportStatus');
  const maxCameras = 8;
  let catalog = {videos:[], profiles:[]};
  let loadedSession = '';
  let saved = {cameras:[], square_mm:25};
  let busy = false;
  let version = 0;
  let renderedVideos = '';

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

  function selectedRows(setup = choices()) {
    return setup.filter(row => row.video);
  }

  function boardSize(selected) {
    return selected.some(row => !row.timeOnly) ? square.valueAsNumber : saved.square_mm;
  }

  function dirty(selected = selectedRows()) {
    return session.value === loadedSession && Boolean(loadedSession) &&
      (JSON.stringify(selected.map(row => row.payload)) !== JSON.stringify(saved.cameras) ||
       boardSize(selected) !== saved.square_mm);
  }

  function renderSelected(selected, locked) {
    const signature = JSON.stringify([loadedSession, locked, selected.map(row => row.payload)]);
    if (signature === renderedVideos) return;
    renderedVideos = signature;
    selectedVideos.hidden = !selected.length;
    selectedVideos.replaceChildren(...selected.map((row, index) => {
      const item = document.createElement('li');
      item.className = 'exo-selected-video';
      const name = document.createElement('span');
      name.textContent = row.video;
      const remove = document.createElement('button');
      remove.type = 'button';
      remove.className = 'small quiet';
      remove.textContent = '移除';
      remove.setAttribute('aria-label', '移除 ' + row.video);
      remove.disabled = locked;
      remove.onclick = () => {
        row.card.querySelector('select[data-field="video"]').value = '';
        render();
        const buttons = selectedVideos.querySelectorAll('button');
        (buttons[index] || buttons[index - 1] || videoPicker).focus();
      };
      item.append(name, remove);
      return item;
    }));
  }

  function render() {
    const setup = choices();
    const selected = selectedRows(setup);
    const duplicate = new Set(selected.map(row => row.video)).size !== selected.length;
    const missing = selected.some(row => row.missing);
    const validSquare = selected.every(row => row.timeOnly) || square.checkValidity();
    const validSession = Boolean(session.value && session.value === loadedSession);
    const locked = busy || !validSession;
    const changed = dirty(selected);
    save.disabled = locked || duplicate || missing || !validSquare || !changed;
    selectionActions.hidden = !changed;
    for (const button of [add, videoPicker, calibrationPicker, profilePicker]) button.disabled = locked;
    add.disabled ||= rows.children.length >= maxCameras;
    summary.textContent = validSession && selected.length ?
      (changed ? '待保存 ' : '已添加 ') + selected.length + ' 路' : '';
    ready.textContent = !session.value ? ''
      : !validSession ? '正在读取已保存的选择…'
      : duplicate ? '每路 EXO 请选择不同的原片。'
      : missing ? '在相机设置中补齐固定板录像和内参。'
      : !validSquare ? '在相机设置中填写格长。'
      : '';
    for (const row of setup) {
      row.card.querySelector('.exo-table-fields').hidden = row.timeOnly;
      for (const input of row.card.querySelectorAll('select')) input.disabled = locked;
    }
    renderSelected(validSession ? selected : [], locked);
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
    return card;
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

  function clearReport() {
    $('exoReport').hidden = true;
    $('exoCameraResults').hidden = true;
    $('exoCameraResults').replaceChildren();
    reportStatus.textContent = '';
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
      status.textContent = '';
      clearReport();
      render();
      return false;
    }
    const needsSelection = reload || loadedSession !== id;
    if (needsSelection) {
      loadedSession = '';
      status.textContent = '';
      clearReport();
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
      if (request !== version || session.value !== id) return false;
      catalog = choices;
      setCopyableText('exoVideoDirectory', catalog.video_directory);
      setCopyableText('exoProfileDirectory', catalog.profile_directory);
      fill();
      if (state) applySelection(state);
      loadedSession = id;
      return true;
    } catch (error) {
      if (request === version) status.textContent = error.message;
      return false;
    } finally {
      if (request === version) render();
    }
  }

  function assignOriginals(videos) {
    let assigned = 0;
    for (const video of videos) {
      const empty = [...rows.children].find(card => !card.querySelector('[data-field="video"]').value);
      const card = empty || (rows.children.length < maxCameras ? appendCamera() : null);
      if (!card) break;
      for (const input of card.querySelectorAll('select')) {
        const field = input.dataset.field;
        input.value = field === 'video' ? video : field === 'mode' ? 'clock' : '';
      }
      assigned++;
    }
    return assigned;
  }

  async function pick(field, assign = false) {
    const id = session.value;
    const knownVideos = new Set(catalog.videos);
    busy = true;
    render();
    status.textContent = '请选择文件。';
    try {
      await post('/api/intake/files', {session_id:id, field});
      if (id !== session.value || !await refresh()) return;
      refreshQrVideos();
      if (assign) {
        const imported = catalog.videos.filter(label => !knownVideos.has(label) && label.includes('本次导入 '));
        const assigned = assignOriginals(imported);
        status.textContent = assigned < imported.length
          ? '最多可选 ' + maxCameras + ' 路。可在相机设置中更换原片。'
          : !assigned && catalog.videos.length ? '已有素材可在相机设置中选择。' : '';
      } else {
        status.textContent = '可在对应相机的设置中选用。';
      }
    } catch (error) {
      if (id === session.value) status.textContent = error.message;
    } finally {
      busy = false;
      render();
    }
  }

  async function loadReport() {
    const id = session.value;
    if (!id) return;
    clearReport();
    const link = $('exoReport');
    const results = $('exoCameraResults');
    try {
      const path = '/api/exo/result?' + new URLSearchParams({id});
      const report = await api(path);
      if (id !== session.value) return;
      if (report.status !== 'processed') {
        reportStatus.textContent = '这条会话还没有 EXO 处理结果。';
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
    } catch (error) {
      if (id === session.value) reportStatus.textContent = error.message;
    }
  }

  save.onclick = async () => {
    if (save.disabled) return;
    const id = session.value;
    const selected = selectedRows();
    const cameras = selected.map(row => row.payload);
    const square_mm = boardSize(selected);
    busy = true;
    render();
    try {
      const result = await post('/api/intake', {session_id:id, cameras, square_mm});
      if (id === session.value) {
        applySelection(result);
        status.textContent = '';
        refreshIntakeStatus();
      }
    } catch (error) {
      if (id === session.value) status.textContent = error.message;
    } finally {
      busy = false;
      render();
      if (id === session.value && selectionActions.hidden) videoPicker.focus();
    }
  };

  add.onclick = () => { appendCamera(); render(); };
  videoPicker.onclick = () => pick('exo_video_files', true);
  calibrationPicker.onclick = () => pick('exo_video_files');
  profilePicker.onclick = () => pick('exo_profile_files');
  $('refreshExo').onclick = () => refresh();
  $('readExoReport').onclick = loadReport;
  square.oninput = render;
  settings.addEventListener('toggle', () => { if (settings.open && !busy && session.value && loadedSession !== session.value) refresh(); });
  window.refreshIntakeExo = refresh;
  window.refreshExoReadiness = render;
  window.intakeExoDirty = () => busy || dirty();
  window.renderExoJob = job => {
    if (job.session_id === session.value && job.status === 'complete') loadReport();
  };
  appendCamera();
  appendCamera();
})();
