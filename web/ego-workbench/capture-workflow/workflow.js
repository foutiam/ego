'use strict';
const element = id => document.getElementById(id);
const picker = element('clip');
const slider = element('frame');
const status = element('status');
let frames = [];
let revision = 0;

async function read(path) {
  const response = await fetch(path, {cache:'no-store'});
  const value = await response.json();
  if (!response.ok) throw Error(value.error || response.statusText);
  return value;
}

function renderFrame() {
  const i = Number(slider.value);
  const frame = frames[i];
  if (!frame) return;
  frame.views.forEach((view, eye) => { element('cam' + eye).src = view; });
  element('time').textContent = (i + 1) + ' / ' + frames.length +
    ' · 会话 ' + frame.time_s.toFixed(3) + ' s' +
    (frame.touch ? ' · 有对应的触觉接收记录' : '');
}

async function openClip() {
  const version = ++revision;
  element('preview').hidden = true;
  if (!picker.value) return;
  status.textContent = '正在读取片段';
  try {
    const input = await read('/workflow/api/input?' + new URLSearchParams({clip:picker.value}));
    if (version !== revision) return;
    frames = input.frames;
    slider.max = Math.max(0, frames.length - 1);
    slider.disabled = frames.length < 2;
    slider.value = 0;
    element('preview').hidden = false;
    status.textContent = input.recordingId + ' · ' + frames.length +
      ' 对双目画面 · ' + (input.cameraAnchor === 'cam0_qr_clock' ? '二维码对时' : '主机接收时间关联');
    renderFrame();
  } catch (error) {
    if (version === revision) status.textContent = error.message;
  }
}

async function refresh() {
  const version = ++revision;
  const selected = picker.value || new URLSearchParams(location.search).get('clip');
  picker.disabled = true;
  element('preview').hidden = true;
  try {
    const {clips} = await read('/workflow/api/catalog');
    if (version !== revision) return;
    picker.replaceChildren(...clips.map(clip => new Option(clip.segmentId + ' · ' + clip.recordingId, clip.clip)));
    picker.disabled = !clips.length;
    if (clips.length) {
      if (clips.some(clip => clip.clip === selected)) picker.value = selected;
      await openClip();
    } else {
      picker.replaceChildren(new Option('暂无片段', ''));
      status.textContent = '录制整理后，在选段与标注中登记片段。';
    }
  } catch (error) {
    if (version === revision) status.textContent = error.message;
  }
}

picker.addEventListener('change', openClip);
slider.addEventListener('input', renderFrame);
element('refresh').addEventListener('click', refresh);
refresh();
