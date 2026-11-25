'use strict';

const element = id => document.getElementById(id);
const picker = element('clip');
const slider = element('frame');
const status = element('status');
const play = element('play');
const previous = element('prev');
const next = element('next');
const tactile = window.EgoInputTimeline.create(element('tactile'), seek);
const events = window.EgoInputEvents.create(element('events'), seek, pause, tactile.setEvents);
let input = null;
let frameIndex = -1;
let revision = 0;
let animation = null;

async function read(path) {
  const response = await fetch(path, {cache:'no-store'});
  const value = await response.json();
  if (!response.ok) throw Error(value.error || response.statusText);
  return value;
}

function show(index) {
  if (!input) return;
  const total = input.frames.length;
  const selected = Math.max(0, Math.min(index, total - 1));
  if (selected === frameIndex) return;
  frameIndex = selected;
  const frame = input.frames[selected];
  slider.value = selected;
  frame.views.forEach((view, eye) => { element('cam' + eye).src = view; });
  element('frameCount').textContent = (selected + 1) + ' / ' + total;
  element('time').textContent = '片段 +' + (frame.time_s - input.startS).toFixed(3)
    + ' s · 录制 ' + frame.time_s.toFixed(3) + ' s';
  previous.disabled = selected === 0;
  next.disabled = selected === total - 1;
  tactile.setTime(input.ref.clip, frame.time_s);
  events.setTime(input.ref.clip, frame.time_s);
}

function frameAt(time) {
  const frames = input.frames;
  let lo = 0, hi = frames.length;
  while (lo < hi) {
    const mid = (lo + hi) >>> 1;
    if (frames[mid].time_s < time) lo = mid + 1;
    else hi = mid;
  }
  if (lo === frames.length) return lo - 1;
  return lo > 0 && time - frames[lo - 1].time_s <= frames[lo].time_s - time ? lo - 1 : lo;
}

function pause() {
  if (animation !== null) cancelAnimationFrame(animation);
  animation = null;
  play.textContent = '播放';
}

function seek(time) {
  pause();
  if (input) show(frameAt(time));
}

function move(index) {
  pause();
  show(index);
}

function toggle() {
  if (animation !== null) {
    pause();
    return;
  }
  if (!input || input.frames.length < 2) return;
  const last = input.frames.length - 1;
  if (frameIndex === last) show(0);
  const firstTime = input.frames[frameIndex].time_s;
  const started = performance.now();
  const tick = now => {
    show(frameAt(firstTime + (now - started) / 1000));
    if (frameIndex === last) pause();
    else animation = requestAnimationFrame(tick);
  };
  play.textContent = '暂停';
  animation = requestAnimationFrame(tick);
}

function clear() {
  pause();
  input = null;
  frameIndex = -1;
  element('preview').hidden = true;
  tactile.setClip(null);
  events.setClip(null);
}

async function openClip() {
  const version = ++revision;
  clear();
  if (!picker.value) return;
  status.textContent = '正在读取片段';
  try {
    const loaded = await read('/workflow/api/input?' + new URLSearchParams({clip:picker.value}));
    if (version !== revision) return;
    input = loaded;
    slider.max = input.frames.length - 1;
    slider.disabled = play.disabled = input.frames.length < 2;
    element('preview').hidden = false;
    status.textContent = input.ref.recordingId + ' · '
      + (input.cameraAnchor === 'cam0_qr_clock' ? '二维码对时' : '主机接收时间')
      + (input.tactileTiming?.status === 'applied' ? ' · 触觉已校准' : '');
    const clip = {ref:input.ref, start:input.startS, end:input.endS};
    tactile.setClip(clip);
    events.setClip(clip);
    show(0);
  } catch (error) {
    if (version === revision) {
      clear();
      status.textContent = error.message;
    }
  }
}

async function refresh() {
  const version = ++revision;
  const selected = picker.value || new URLSearchParams(location.search).get('clip');
  picker.disabled = true;
  clear();
  status.textContent = '正在读取片段';
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
slider.addEventListener('input', () => move(Number(slider.value)));
previous.addEventListener('click', () => move(frameIndex - 1));
next.addEventListener('click', () => move(frameIndex + 1));
play.addEventListener('click', toggle);
element('refresh').addEventListener('click', refresh);
refresh();