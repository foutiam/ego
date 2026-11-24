/* One continuous recording with named, independent interaction attempts. */
'use strict';
(() => {
  const element = id => document.getElementById(id);
  const outcomes = [['', '待标注'], ['success', '成功'], ['failed', '失败'], ['incomplete', '未完成']];
  const olderTaskTitles = {miss:'首次未夹住', adjust:'调整', abort:'失败或放弃'};
  let recipe = null, manifest = null, recording = false;
  let busy = false, polling = false, countdown = null, countdownTimer = null;
  let elapsed = 0, lastUpdate = Date.now(), remembered = null, savedTimelines = [];
  let selectedId = null, lastDeleted = null, signature = '', taskSignature = '';

  function node(tag, text, classes) {
    const control = document.createElement(tag);
    if (text) control.textContent = text;
    if (classes) control.className = classes;
    if (tag === 'button') control.type = 'button';
    return control;
  }
  function labeled(label, control) {
    const container = node('label', label);
    container.append(control);
    return container;
  }
  function labelInput() {
    const input = node('input');
    input.type = 'text';
    input.maxLength = 64;
    return input;
  }

  const segmentType = node('select');
  const nextLabel = labelInput();
  nextLabel.placeholder = '例如 侧面夹取';
  const toggleAttempt = node('button', '录制分项', 'small primary');
  toggleAttempt.disabled = true;
  const controls = element('guidedGroups');
  controls.classList.add('toolbar');
  controls.replaceChildren(labeled('分项类型', segmentType), labeled('片段标签（可选）', nextLabel), toggleAttempt);

  const active = () => manifest?.segments.find(segment => segment.id === manifest.active_id);
  const completed = () => manifest?.segments.filter(segment => segment.status === 'recorded' && !segment.deleted) ?? [];
  const currentTime = () => elapsed + (recording ? (Date.now() - lastUpdate) / 1000 : 0);
  const titleFor = taskId => recipe?.tasks.find(task => task.id === taskId)?.title || olderTaskTitles[taskId] || taskId;
  const displayLabel = entry => entry.label || titleFor(entry.task_id);
  const displayOutcome = value => outcomes.find(([id]) => id === value)?.[1] || '待标注';

  function remember(id) {
    remembered = id;
  }
  function apply(result) {
    recipe = result.recipe;
    manifest = result.manifest;
    recording = result.recording;
    elapsed = result.elapsed_s || 0;
    lastUpdate = Date.now();
    if (manifest) remember(manifest.session_id);
    render();
  }
  function message(text, error = false) {
    element('guidedStatus').textContent = text;
    element('guidedStatus').classList.toggle('error', error);
  }
  function renderSavedTimelines() {
    const source = element('guidedSource');
    source.disabled = recording || busy || Boolean(countdown);
    const rows = new Map(savedTimelines.map(row => [row.session_id, row.object_id]));
    if (manifest && !rows.has(manifest.session_id)) rows.set(manifest.session_id, manifest.profile?.object_id);
    const key = JSON.stringify([...rows]);
    if (source.dataset.options !== key) {
      source.replaceChildren(new Option('新建时间轴', ''));
      for (const [id, objectId] of rows) source.add(new Option([id, objectId].filter(Boolean).join(' · '), id));
      source.dataset.options = key;
    }
    source.value = manifest?.session_id || '';
  }
  function renderSelection() {
    const rows = element('guidedRows');
    rows.replaceChildren();
    const selected = completed().find(entry => entry.id === selectedId);
    element('guidedSelected').textContent = selected
      ? displayLabel(selected) + ' · ' + selected.start_s.toFixed(2) + '–' + selected.end_s.toFixed(2) + ' s'
      : '';
    element('guidedReview').hidden = !selected;
    element('guidedReview').disabled = !selected || recording || busy;
    if (!selected) return;

    const form = node('form', '', 'toolbar guided-annotation');
    const label = labelInput();
    label.value = selected.label || '';
    label.placeholder = titleFor(selected.task_id);
    const outcome = node('select');
    for (const [id,text] of outcomes) outcome.add(new Option(text, id));
    outcome.value = selected.operator_outcome || '';
    const save = node('button', '保存标签', 'small');
    save.type = 'submit';
    save.disabled = busy || Boolean(active());
    form.onsubmit = async event => {
      event.preventDefault();
      if (busy || active()) return;
      save.disabled = true;
      try {
        await updateSegment('/api/guided/annotate', selected,
          {label:label.value.trim(), outcome:outcome.value}, '已保存此分项的标签和结果。');
      } catch (_) {
        save.disabled = false;
      }
    };
    const remove = node('button', '移除片段', 'small quiet');
    remove.disabled = save.disabled;
    remove.onclick = async () => {
      try {
        await updateSegment('/api/guided/select', selected, {deleted:true}, '已从时间轴移除片段。');
        lastDeleted = selected.id;
        selectedId = null;
        render();
      } catch (_) { /* Keep the selected attempt when saving fails. */ }
    };
    form.append(labeled('片段标签', label), labeled('结果标注', outcome), save, remove);
    rows.append(form);
  }
  function render() {
    renderSavedTimelines();
    if (!recipe) return;
    const running = active(), done = completed(), locked = Boolean(manifest);
    const plan = JSON.stringify(recipe.tasks);
    if (plan !== taskSignature) {
      segmentType.replaceChildren(...recipe.tasks.map(task => new Option(task.title, task.id)));
      taskSignature = plan;
    }
    segmentType.disabled = Boolean(countdown || running || busy || (locked && !recording));
    nextLabel.disabled = segmentType.disabled;
    toggleAttempt.disabled = busy || (locked && !recording) ||
      (!running && !countdown && !locked && !element('objectId').value.trim());
    toggleAttempt.textContent = countdown ? '取消准备' : running ? '结束分项' : '录制分项';
    element('guidedProgress').textContent = '已记录 ' + done.length + ' 段';
    element('guidedFinish').disabled = busy || !recording || Boolean(countdown);
    element('guidedNew').disabled = busy || recording || Boolean(countdown) || !locked;
    element('guidedUndo').classList.toggle('hidden', !lastDeleted);
    element('guidedUndo').disabled = busy || Boolean(running || countdown);
    element('guidedObjectHint').textContent = locked
      ? '交互物体 ' + manifest.profile.object_id + ' · ' + (recording ? '连续录制中' : '已保存')
      : '使用上方的物体编号、场景和数据划分。';
    const legacyObject = element('guidedObjectSelect');
    if (legacyObject) legacyObject.closest('.guided-object').hidden = true;
    element('guidedSession').textContent = locked ? '会话 ' + manifest.session_id : '';
    const nextSignature = JSON.stringify([manifest, selectedId, busy]);
    if (signature !== nextSignature) {
      signature = nextSignature;
      const timeline = element('guidedTimeline');
      timeline.replaceChildren(...done.map(entry => {
        const item = node('button', displayLabel(entry) + ' · ' + entry.start_s.toFixed(1) + '–'
          + entry.end_s.toFixed(1) + ' s · ' + displayOutcome(entry.operator_outcome), 'guided-interval');
        item.dataset.slotKey = entry.key;
        item.setAttribute('aria-pressed', String(selectedId === entry.id));
        item.onclick = () => { selectedId = entry.id; render(); };
        return item;
      }));
      renderSelection();
    }
    if (!busy && !countdown && running) {
      message('录制中 · ' + displayLabel(running) + ' · '
        + Math.max(0, currentTime() - running.start_s).toFixed(1) + ' 秒');
    }
  }
  function clearCountdown() {
    clearInterval(countdownTimer);
    countdownTimer = null;
    countdown = null;
  }
  function cancelCountdown() {
    clearCountdown();
    message('已取消准备。');
    render();
  }
  async function startAttempt(request) {
    clearCountdown();
    busy = true;
    render();
    message('正在开始分项…');
    try {
      apply(await post('/api/guided/begin', request));
      selectedId = null;
    } catch (error) {
      await recoverState();
      if (!active()) message(error.message, true);
    } finally {
      busy = false;
      render();
    }
  }
  async function endAttempt() {
    const entry = active();
    if (!entry || busy) return;
    busy = true;
    render();
    try {
      apply(await post('/api/guided/end', {session_id:manifest.session_id, segment_id:entry.id}));
      selectedId = entry.id;
      message('分项已结束。可以标注这一段，或继续录制下一段。');
    } catch (error) {
      message(error.message, true);
    } finally {
      busy = false;
      render();
    }
  }
  function prepareAttempt() {
    if (busy) return;
    if (countdown) { cancelCountdown(); return; }
    if (active()) { endAttempt(); return; }
    if (manifest && !recording) return;
    const taskId = segmentType.value;
    const previous = manifest?.segments.filter(segment => segment.task_id === taskId) || [];
    const number = 1 + Math.max(0, ...previous.map(segment => segment.number));
    if (number > 200) {
      message('该分项类型已达到 200 段。请保存后新建时间轴。', true);
      return;
    }
    const request = {
      mode:'interaction', task_id:taskId, number, label:nextLabel.value.trim(),
      session_id:manifest?.session_id, scene_id:element('sceneId').value,
      object_id:manifest?.profile.object_id || element('objectId').value.trim(),
      material:element('material').value, split:element('split').value
    };
    if (!request.object_id) { message('请填写交互物体编号。', true); return; }
    countdown = {seconds:recipe.countdown_seconds || 3, request};
    const update = () => message('准备 ' + (request.label || titleFor(taskId)) + ' · ' + countdown.seconds + ' 秒后开始');
    update();
    render();
    countdownTimer = setInterval(() => {
      if (--countdown.seconds === 0) startAttempt(request);
      else update();
    }, 1000);
  }
  async function updateSegment(path, entry, fields, success) {
    if (busy || active()) return;
    busy = true;
    try {
      apply(await post(path, {session_id:manifest.session_id, segment_id:entry.id, ...fields}));
      message(success);
    } catch (error) {
      message(error.message, true);
      throw error;
    } finally {
      busy = false;
      render();
    }
  }
  async function finish() {
    if (busy || !manifest || !recording) return;
    clearCountdown();
    busy = true;
    render();
    message('正在保存整条时间轴…');
    try {
      apply(await post('/api/guided/finish', {session_id:manifest.session_id}));
      notifyEgoCaptureUpdated();
      message('时间轴已保存。可选择录制分段进入复查。');
      await loadSessions();
    } catch (error) {
      message(error.message, true);
    } finally {
      busy = false;
      render();
    }
  }
  function newTimeline() {
    if (recording || busy || countdown) return;
    manifest = null;
    selectedId = null;
    lastDeleted = null;
    remember(null);
    nextLabel.value = '';
    message('可连续录制同一物体的多个分项，每段可单独标注。');
    render();
  }
  async function recoverState() {
    try { apply(await api('/api/guided' + (remembered ? '?id=' + encodeURIComponent(remembered) : ''))); }
    catch (_) { /* Keep the previous state when the connection drops. */ }
  }
  async function poll() {
    if (polling || busy || countdown) return;
    polling = true;
    const requested = remembered;
    try {
      const result = await api('/api/guided' + (requested ? '?id=' + encodeURIComponent(requested) : ''));
      if (!busy && remembered === requested) {
        const first = !recipe;
        apply(result);
        if (first && !active()) message('可连续录制同一物体的多个分项，每段可单独标注。');
      }
    } catch (error) {
      if (remembered && !recipe) {
        remember(null);
        await recoverState();
        if (recipe) { message('可开始新的分项录制。'); return; }
      }
      message('无法读取分项录制 · ' + error.message, true);
    } finally {
      polling = false;
    }
  }

  toggleAttempt.onclick = prepareAttempt;
  element('objectId').addEventListener('input', () => render());
  element('guidedSource').onchange = async event => {
    const id = event.target.value;
    if (!id) { newTimeline(); return; }
    busy = true;
    selectedId = null;
    lastDeleted = null;
    render();
    try {
      apply(await api('/api/guided?id=' + encodeURIComponent(id)));
      message('已打开保存的时间轴。');
    } catch (error) {
      message(error.message, true);
    } finally {
      busy = false;
      render();
    }
  };
  element('guidedReview').onclick = () => {
    const selected = completed().find(entry => entry.id === selectedId);
    if (selected && !recording) window.openCaptureSegment?.(manifest.session_id, selected.key);
  };
  element('guidedFinish').onclick = finish;
  element('guidedNew').onclick = newTimeline;
  element('guidedUndo').onclick = async () => {
    if (!lastDeleted || busy || active()) return;
    try {
      await updateSegment('/api/guided/select', {id:lastDeleted}, {deleted:false}, '已恢复片段。');
      lastDeleted = null;
      render();
    } catch (_) { /* The removed segment remains available to restore. */ }
  };
  document.addEventListener('keydown', event => {
    if (event.repeat || event.isComposing || !element('live').classList.contains('active')) return;
    if (event.key === 'Escape' && countdown) {
      event.preventDefault();
      cancelCountdown();
    } else if (event.key === 'Enter' && active() &&
        !event.target.closest('input,select,textarea,button,[contenteditable=true]')) {
      event.preventDefault();
      endAttempt();
    }
  }, true);
  window.addEventListener('beforeunload', event => {
    if (countdown) clearCountdown();
    if (recording) { event.preventDefault(); event.returnValue = ''; }
  });
  window.guidedCapture = {
    finish,
    ownsRecording: () => recording,
    setSessions(rows) {
      savedTimelines = rows.filter(row => row.guided_capture?.file === 'guided-capture.json');
      renderSavedTimelines();
    }
  };
  poll();
  setInterval(poll, 1500);
})();
