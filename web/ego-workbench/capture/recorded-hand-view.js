/* Project the saved wrist-local hand samples beside the recording timeline. */
(() => {
  'use strict';

  const canvas = document.getElementById('recordedHandCanvas');
  const status = document.getElementById('recordedHandStatus');
  const emfToggle = document.getElementById('recordedEmfToggle');
  const taxelToggle = document.getElementById('recordedTaxelToggle');
  if (!canvas || !status || !emfToggle || !taxelToggle) return;

  let sample = null;
  const view = {yaw: -0.32, pitch: 0.18};
  function makeProjector(width, height) {
    const scale = Math.min(width / 0.21, height / 0.235);
    const cYaw = Math.cos(view.yaw), sYaw = Math.sin(view.yaw);
    const cPitch = Math.cos(view.pitch), sPitch = Math.sin(view.pitch);
    return (x, y, z) => {
      const across = y * cYaw + x * sYaw;
      const depth = x * cYaw - y * sYaw;
      const along = z - 0.075;
      return [width / 2 + across * scale,
              height / 2 - (along * cPitch - depth * sPitch) * scale,
              depth * cPitch + along * sPitch, scale];
    };
  }

  function draw() {
    const ctx = canvas.getContext('2d');
    const {width, height} = canvas;
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = '#0b111a';
    ctx.fillRect(0, 0, width, height);
    const pose = sample?.skeleton_pose;
    if (!pose) {
      status.textContent = sample?.skeletonStatus || '定位到录制时刻后显示腕部局部骨架。';
      return;
    }

    const project = makeProjector(width, height);
    const nodes = new Map(pose.names.map((name, index) =>
      [name, project(...pose.positions_local[index])]));
    const side = sample.emf?.hand_side === 1 ? 'left' : sample.emf?.hand_side === 0 ? 'right' : null;
    const overlaysAvailable = side === pose.hand_side &&
      Math.abs(sample.timeS - pose.timeS) <= 0.075;
    const scores = overlaysAvailable && emfToggle.checked
      ? Object.fromEntries(Object.entries(HAND_TACTILE_SITE).filter(([, site]) =>
          Number.isFinite(sample.finger_scores?.[site])).map(([finger, site]) =>
          [finger, sample.finger_scores[site]]))
      : {};
    const peak = Math.max(1, ...Object.values(scores));
    const bones = HAND_BONES.map(([from, to, finger]) =>
      [nodes.get(from), nodes.get(to), finger]).filter(([a, b]) => a && b)
      .sort(([a, b], [c, d]) => c[2] + d[2] - a[2] - b[2]);

    ctx.lineCap = 'round';
    for (const [a, b, finger] of bones) {
      const value = scores[finger] || 0;
      ctx.strokeStyle = value > 0 ? tactileColor(value, peak) : HAND_COLORS[finger];
      ctx.lineWidth = 2 + 3 * value / peak;
      ctx.beginPath();
      ctx.moveTo(a[0], a[1]);
      ctx.lineTo(b[0], b[1]);
      ctx.stroke();
    }

    const pads = overlaysAvailable && taxelToggle.checked
      ? handPressurePads(pose.names, pose.positions_local, pose.hand_side,
        {rawGrid:sample.grid, baseline:sample.pressure_baseline, rawCounts:true})
      : null;
    if (pads) drawPressurePadLayer(ctx, pads, {project});

    for (const [name, [x, y]] of nodes) {
      ctx.fillStyle = HAND_COLORS[HAND_NODE_FINGER[name]] || '#e7edf2';
      ctx.beginPath();
      ctx.arc(x, y, name === 'wrist' ? 4 : 2.4, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    for (const [finger, tip] of HAND_TIPS) {
      const point = nodes.get(tip);
      if (!point) continue;
      ctx.fillStyle = HAND_COLORS[finger];
      const value = scores[finger];
      ctx.fillText(HAND_FINGER_ZH[finger] + (value == null ? '' : ' ' + value),
        point[0], point[1] - 11);
    }
    ctx.textAlign = 'start';

    const messages = ['腕部局部坐标', '骨架 ' + pose.timeS.toFixed(3) + ' s'];
    if (!overlaysAvailable && (emfToggle.checked || taxelToggle.checked)) {
      messages.push(sample.gloveStatus || '同手的触觉帧未在 75 ms 内配对');
    } else {
      if (emfToggle.checked) {
        const fingers = sample.emf.fingers;
        const valid = fingers.filter(finger => finger.valid).length;
        messages.push('EMF ' + valid + '/' + fingers.length + ' 指有效');
        if (!Object.keys(scores).length) messages.push('录制缺少 EMF 触感基线');
      }
      if (taxelToggle.checked) messages.push(pads
        ? '触觉点 ' + pads.drawn + ' 个 · 叠加点峰值 ' + pads.max
        : '此时刻没有原始压感');
    }
    status.textContent = messages.join(' · ');
  }

  window.renderRecordedHand = (value) => {
    sample = value;
    draw();
  };
  emfToggle.addEventListener('change', draw);
  taxelToggle.addEventListener('change', draw);
  let drag = null;
  canvas.addEventListener('pointerdown', (event) => {
    if (!sample?.skeleton_pose || event.button !== 0) return;
    drag = [event.clientX, event.clientY];
    canvas.setPointerCapture?.(event.pointerId);
  });
  canvas.addEventListener('pointermove', (event) => {
    if (!drag) return;
    view.yaw += (event.clientX - drag[0]) * 0.012;
    view.pitch = Math.max(-1.4, Math.min(1.4, view.pitch + (event.clientY - drag[1]) * 0.012));
    drag = [event.clientX, event.clientY];
    draw();
  });
  canvas.addEventListener('lostpointercapture', () => { drag = null; });
  canvas.addEventListener('dblclick', () => {
    view.yaw = -0.32;
    view.pitch = 0.18;
    draw();
  });
})();
