/* Workstation paths and read-only device calibration snapshots. */
(() => {
  'use strict';

  const dialog = $('capturePathDialog');
  const form = $('capturePathForm');
  const feedback = $('capturePathStatus');
  const save = $('saveCapturePaths');
  const browseButtons = [...form.querySelectorAll('[data-browse-path]')];
  const fields = Object.fromEntries(browseButtons.map(button => [
    button.dataset.browsePath, form.elements.namedItem(button.dataset.browsePath)
  ]));
  let saved = null;

  function fieldValue(input) {
    return input.tagName === 'TEXTAREA'
      ? input.value.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
      : input.value.trim();
  }

  function dirty() {
    return saved && Object.entries(fields).some(([key, input]) =>
      JSON.stringify(fieldValue(input)) !== JSON.stringify(saved[key]));
  }

  function showPaths(state) {
    saved = state.values;
    for (const [key, input] of Object.entries(fields)) {
      const value = saved[key];
      input.value = Array.isArray(value) ? value.join('\n') : value;
      if (key.endsWith('_root')) input.placeholder = state.effective[key] || '';
    }
    $('activeCapturePath').textContent = '当前录制目录 · ' + state.effective.output_root;
    feedback.textContent = state.restartRequired ? '已保存路径。重启工作台后生效。' : '';
    save.disabled = true;
  }

  function busy(active) {
    for (const field of Object.values(fields)) field.disabled = active;
    for (const button of browseButtons) button.disabled = active;
    $('closeCapturePaths').disabled = active;
    save.disabled = active || !dirty();
  }

  dialog.addEventListener('cancel', event => {
    if ($('closeCapturePaths').disabled) event.preventDefault();
  });
  $('closeCapturePaths').onclick = () => dialog.close();

  for (const entry of document.querySelectorAll('[data-open-capture-paths]')) {
    entry.addEventListener('click', async () => {
      if (dialog.open) return;
      $('captureCalibrationPaths').open = entry.dataset.openCapturePaths === 'calibration';
      $('captureImportPaths').open = false;
      dialog.showModal();
      busy(true);
      feedback.textContent = '正在读取路径…';
      try {
        showPaths(await api('/api/settings/paths'));
      } catch (error) {
        saved = null;
        feedback.textContent = error.message;
      } finally {
        busy(false);
      }
    });
  }

  form.addEventListener('input', () => { save.disabled = !dirty(); });
  for (const button of browseButtons) {
    button.onclick = async () => {
      const key = button.dataset.browsePath;
      const input = fields[key];
      const previous = fieldValue(input);
      busy(true);
      feedback.textContent = '请在本机选择窗口完成选择。';
      try {
        const current = Array.isArray(previous) ? previous.at(-1) || '' : previous;
        const {paths} = await post('/api/settings/choose-path', {field:key, current});
        if (paths.length) {
          input.value = Array.isArray(previous) ? [...new Set([...previous, ...paths])].join('\n') : paths[0];
        }
        feedback.textContent = '';
      } catch (error) {
        feedback.textContent = error.message;
      } finally {
        busy(false);
      }
    };
  }

  form.onsubmit = async event => {
    event.preventDefault();
    if (!dirty()) return;
    const changes = Object.fromEntries(Object.entries(fields).map(([key, input]) => [key, fieldValue(input)]));
    busy(true);
    try {
      showPaths(await post('/api/settings/paths', changes));
    } catch (error) {
      feedback.textContent = error.message;
    } finally {
      busy(false);
    }
  };

  const source = $('deviceEvidenceSource');
  const filePicker = $('deviceEvidenceFile');
  const currentEvidence = $('liveDeviceEvidence');
  const savedEvidence = $('savedDeviceEvidence');
  const status = $('deviceEvidenceStatus');
  const items = $('deviceEvidenceItems');
  let generation = 0;
  let localSnapshot = null;

  function showCurrent() {
    ++generation;
    currentEvidence.hidden = false;
    savedEvidence.hidden = true;
  }

  function addDetail(label, detail) {
    const item = document.createElement('article');
    item.className = 'calib-item';
    const title = document.createElement('h3');
    title.textContent = label;
    const text = document.createElement('p');
    text.textContent = detail;
    item.append(title, text);
    items.append(item);
  }

  async function showSaved(label, load) {
    const request = ++generation;
    currentEvidence.hidden = true;
    savedEvidence.hidden = false;
    items.replaceChildren();
    status.textContent = '正在读取 ' + label;
    try {
      const report = await load();
      if (request !== generation) return;
      status.textContent = label + ' · 档案快照';
      const identity = report.identity?.name;
      if (identity) addDetail('设备', [identity, report.identity.serial].filter(Boolean).join(' · '));
      const streams = Object.entries(report.streams || {});
      if (!streams.length) addDetail('双目内参', '档案无设备内参记录');
      for (const [topic, intrinsics] of streams) {
        const dimensions = intrinsics.width && intrinsics.height ? intrinsics.width + ' × ' + intrinsics.height : '';
        const parameters = intrinsics.K_complete
          ? ['fx', 'fy', 'cx', 'cy'].map(key => key + ' ' + intrinsics[key].toFixed(2)).join(' · ')
          : '无完整内参';
        addDetail(topic, [dimensions, parameters].filter(Boolean).join(' · '));
      }
      const baseline = report.stereo_extrinsics?.baseline_m;
      addDetail('双目外参', Number.isFinite(baseline) ? '设备自报基线 ' + (baseline * 1000).toFixed(3) + ' mm' : '档案无基线记录');
      addDetail('手套静置基线', report.touch_baseline ? '有记录' : '无记录');
      addDetail('独立校验', '未验证');
    } catch (error) {
      if (request === generation) status.textContent = error.message;
    }
  }

  source.onchange = () => {
    if (!source.value) {
      showCurrent();
      return;
    }
    if (source.selectedOptions[0]?.hasAttribute('data-local-file') && localSnapshot) {
      showFile(localSnapshot);
      return;
    }
    showSaved(source.selectedOptions[0].textContent, () =>
      api('/api/calibration/recorded-device?' + new URLSearchParams({id:source.value})));
  };

  $('openDeviceEvidenceFile').onclick = () => filePicker.click();
  filePicker.onchange = () => {
    const file = filePicker.files[0];
    filePicker.value = '';
    if (!file) return;
    // Keep the current-device option available after selecting a file.
    const prior = source.querySelector('[data-local-file]');
    if (prior) prior.remove();
    const option = new Option(file.name, '@local-file');
    option.dataset.localFile = '';
    source.add(option);
    source.value = option.value;
    localSnapshot = file;
    showFile(file);
  };

  function showFile(file) {
    showSaved(file.name, async () => {
      if (file.size > 2_000_000) throw Error('请选择 2 MB 以内的 Ego 标定档案。');
      let snapshot;
      try {
        snapshot = JSON.parse(await file.text());
      } catch {
        throw Error('无法读取标定 JSON。');
      }
      return post('/api/calibration/recorded-device', snapshot);
    });
  }

  window.capturePathSettings = {setSessions(sessions) {
    const selection = source.value;
    const local = source.querySelector('[data-local-file]');
    source.replaceChildren(new Option('当前连接设备', ''), ...sessions.map(session =>
      new Option(session.name || session.session_id, session.session_id)));
    if (local) source.add(local);
    source.value = selection;
    if (!source.value) {
      source.value = '';
      if (selection) showCurrent();
    }
  }};
})();