/* Clip-scoped raw touch and manual labels, driven by the existing source player clock. */
(() => {
  'use strict';

  const leftPad = 45, rightPad = 15;
  const labels = {thumb:'拇指', index:'食指', middle:'中指', ring:'无名指', pinky:'小指'};
  const fmt = t => t.toFixed(3) + ' s';

  function channelLabel(channel) {
    const [finger, site] = channel.label.split('_');
    const annotation = labels[finger] ? labels[finger] + (site === 'bend' ? '弯曲' : ' ' + (site || '')) : '';
    return [annotation.trim(), '字节 ' + channel.offset].filter(Boolean).join(' · ');
  }

  function create(panel, seek) {
    panel.innerHTML = [
      '<header class="input-timeline-head"><h3 id="inputTactileTitle">原始触觉</h3>',
      '<div class="input-timeline-controls">',
      '<select class="input-touch-channel" aria-label="触觉通道" disabled><option>触压通道峰值</option></select>',
      '<select class="input-touch-clock" aria-label="触觉时间基准" hidden>',
      '<option value="aligned">校准时间</option><option value="raw">原始接收时间</option></select>',
      '</div></header>',
      '<p class="input-timeline-readout"></p>',
      '<div class="input-timeline-plot" hidden>',
      '<canvas class="input-waveform" aria-hidden="true"></canvas>',
      '<canvas class="input-waveform-cursor" role="slider" tabindex="0" aria-label="触觉时间轴" aria-orientation="horizontal"></canvas>',
      '</div>',
      '<div class="input-timeline-markers" aria-label="时间标注" hidden></div>'
    ].join('');
    const pick = selector => panel.querySelector(selector);
    const graph = pick('.input-timeline-plot'), wave = pick('.input-waveform'), cursor = pick('.input-waveform-cursor');
    const readout = pick('.input-timeline-readout'), channel = pick('.input-touch-channel'), clock = pick('.input-touch-clock');
    const markers = pick('.input-timeline-markers');
    let clip = null, payload = null, current = null, controller;
    let rawCounts = null, times = [], values = [], events = [], key = '';
    let width = 0, height = 0, endY = 0, maxY = 1;

    function scaleX(time) {
      return leftPad + (width - leftPad - rightPad) * (time - clip.start) / (clip.end - clip.start);
    }
    function atPixel(x) {
      const f = Math.max(0, Math.min(1, (x - leftPad) / Math.max(1, width - leftPad - rightPad)));
      return clip.start + f * (clip.end - clip.start);
    }
    function localSeek(time) {
      if (clip) seek(Math.max(clip.start, Math.min(time, clip.end - .000001)));
    }

    function renderMarkers() {
      markers.hidden = !clip || !events.length;
      markers.replaceChildren();
      if (markers.hidden) return;
      for (const event of events) {
        const fraction = (event.timeS - clip.start) / (clip.end - clip.start);
        if (fraction < 0 || fraction >= 1) continue;
        const marker = document.createElement('button');
        marker.type = 'button';
        marker.className = 'input-timeline-marker';
        marker.style.left = fraction * 100 + '%';
        marker.title = event.label + ' · +' + fmt(event.timeS - clip.start);
        marker.setAttribute('aria-label', marker.title);
        marker.onclick = () => localSeek(event.timeS);
        markers.appendChild(marker);
      }
    }

    function chooseSeries() {
      if (!payload) return;
      const n = payload.channels.length;
      const index = Number(channel.value);
      const included = payload.channels.map((item, i) => /bend/i.test(item.label) ? -1 : i).filter(i => i >= 0);
      const columns = index >= 0 ? [index] : included.length ? included : payload.channels.map((_, i) => i);
      const stamps = clock.value === 'raw' ? payload.rawTimeS : payload.alignedTimeS || payload.rawTimeS;
      const points = [];
      for (let i = 0; i < stamps.length; i++) {
        if (stamps[i] < clip.start || stamps[i] >= clip.end) continue;
        let peak = 0;
        for (const col of columns) peak = Math.max(peak, rawCounts[i * n + col]);
        points.push([stamps[i], peak]);
      }
      if (stamps === payload.alignedTimeS) points.sort((a, b) => a[0] - b[0]);
      times = [];
      values = [];
      for (const [time, peak] of points) {
        const last = times.length - 1;
        if (times[last] === time) values[last] = Math.max(values[last], peak);
        else {
          times.push(time);
          values.push(peak);
        }
      }
      paint();
    }

    function context(canvas) {
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(height * ratio);
      const c = canvas.getContext('2d');
      c.setTransform(ratio, 0, 0, ratio, 0, 0);
      return c;
    }

    function paint() {
      if (!payload) return;
      ({width, height} = graph.getBoundingClientRect());
      if (!width || !height || width <= leftPad + rightPad) return;
      endY = height - 25;
      const peak = values.reduce((previous, value) => Math.max(previous, value), 0);
      maxY = Math.max(5, Math.ceil(peak / 5) * 5);
      const y = value => endY - (endY - 11) * value / maxY;
      const c = context(wave);
      context(cursor);
      c.font = '11px system-ui,sans-serif';
      c.strokeStyle = '#26394b';
      c.fillStyle = '#9aaebf';
      c.textAlign = 'right';
      for (const value of [0, Math.round(maxY / 2), maxY]) {
        const line = y(value);
        c.beginPath();
        c.moveTo(leftPad, line);
        c.lineTo(width - rightPad, line);
        c.stroke();
        c.fillText(String(value), leftPad - 7, line + 4);
      }
      c.textAlign = 'center';
      const duration = clip.end - clip.start;
      const ticks = Math.min(8, Math.max(3, Math.floor(width / 120)));
      const exponent = 10 ** Math.floor(Math.log10(duration / ticks));
      const step = ([1, 2, 2.5, 5, 10].find(n => n >= duration / ticks / exponent) || 10) * exponent;
      for (let t = 0; t < duration; t += step) {
        const x = scaleX(clip.start + t);
        if (t > 0 && width - rightPad - x < 50) continue;
        c.beginPath();
        c.moveTo(x, 11);
        c.lineTo(x, endY);
        c.stroke();
        c.fillText(Number(t.toFixed(3)) + (t === 0 ? ' s' : ''), x, height - 8);
      }

      c.textAlign = 'right';
      c.fillText(duration.toFixed(3) + ' s', width - rightPad, height - 8);
      c.strokeStyle = '#78d9c7';
      c.lineWidth = 1.25;
      c.beginPath();
      // Draw extrema per pixel so narrow peaks survive on smaller screens.
      let lastX = -1, seen = false, lastTime, low, high;
      const flush = () => {
        if (!seen) return;
        c.lineTo(lastX, y(low));
        c.lineTo(lastX, y(high));
      };
      for (let i = 0; i < times.length; i++) {
        const x = Math.floor(scaleX(times[i]));
        if (x === lastX && times[i] - lastTime <= .1) {
          low = Math.min(low, values[i]);
          high = Math.max(high, values[i]);
        } else {
          flush();
          const next = !seen || times[i] - lastTime > .1 ? 'moveTo' : 'lineTo';
          c[next](x, y(values[i]));
          lastX = x;
          low = high = values[i];
          seen = true;
        }
        lastTime = times[i];
      }
      flush();
      c.stroke();
      paintCursor();
    }

    function paintCursor() {
      if (!clip || !payload || !width) return;
      const c = cursor.getContext('2d');
      c.clearRect(0, 0, width, height);
      const t = Number.isFinite(current) ? Math.max(clip.start, Math.min(current, clip.end)) : clip.start;
      const x = scaleX(t);
      c.strokeStyle = '#d8ede9';
      c.lineWidth = 1;
      c.beginPath();
      c.moveTo(x, 8);
      c.lineTo(x, endY);
      c.stroke();
      let lo = 0, hi = times.length;
      while (lo < hi) {
        const mid = (lo + hi) >>> 1;
        if (times[mid] < t) lo = mid + 1;
        else hi = mid;
      }
      const bin = lo >= times.length || lo > 0 && t - times[lo - 1] <= times[lo] - t ? lo - 1 : lo;
      const observed = bin >= 0 && Math.abs(times[bin] - t) <= .1;
      const count = observed ? values[bin] + ' counts' : '该时刻无样本';
      readout.textContent = '+' + fmt(t - clip.start) + ' · 录制 ' + fmt(t) + ' · ' + count;
      cursor.setAttribute('aria-valuemin', '0');
      cursor.setAttribute('aria-valuemax', (clip.end - clip.start).toFixed(3));
      cursor.setAttribute('aria-valuenow', (t - clip.start).toFixed(3));
      cursor.setAttribute('aria-valuetext', readout.textContent);
    }

    async function setClip(next) {
      const nextKey = next?.ref.segmentRevision || '';
      if (key === nextKey) return;
      key = nextKey;
      controller?.abort();
      payload = null;
      current = null;
      events = [];
      times = values = [];
      clip = next;
      panel.hidden = !clip;
      graph.hidden = true;
      channel.disabled = true;
      clock.hidden = true;
      renderMarkers();
      if (!clip) return;
      readout.textContent = '正在读取该片段的原始触觉…';
      const request = new AbortController();
      controller = request;
      try {
        const response = await fetch('/api/workflow/tactile-series?' + new URLSearchParams(clip.ref),
                                     {signal:request.signal, cache:'no-store'});
        const data = await response.json();
        if (controller !== request) return;
        if (!response.ok || !Object.entries(clip.ref).every(([k,v]) => data[k] === v) ||
            data.startS !== clip.start || data.endS !== clip.end) throw Error('触觉记录未能匹配该片段');
        if (data.status !== 'ready') {
          readout.textContent = '该片段无可读的原始手套触觉记录。';
          return;
        }
        rawCounts = Uint8Array.from(atob(data.rawCounts), char => char.charCodeAt(0));
        if (rawCounts.length !== data.rawTimeS.length * data.channels.length ||
            (data.alignedTimeS && data.alignedTimeS.length !== data.rawTimeS.length)) {
          throw Error('触觉通道与样本数不一致');
        }
        payload = data;
        channel.replaceChildren(new Option('触压通道峰值', '-1'),
          ...data.channels.map((item, i) => new Option(channelLabel(item), String(i))));
        channel.disabled = false;
        clock.hidden = !data.alignedTimeS;
        clock.value = data.alignedTimeS ? 'aligned' : 'raw';
        graph.hidden = false;
        chooseSeries();
      } catch (error) {
        if (controller === request && !request.signal.aborted) readout.textContent = error.message;
      }
    }

    channel.onchange = clock.onchange = chooseSeries;
    cursor.onclick = event => {
      localSeek(atPixel(event.clientX - cursor.getBoundingClientRect().left));
    };
    cursor.onkeydown = event => {
      if (!clip) return;
      const t = Number.isFinite(current) ? current : clip.start;
      const step = event.shiftKey ? .5 : .1;
      const target = {Home:clip.start, End:clip.end, ArrowLeft:t-step, ArrowRight:t+step,
                      ArrowDown:t-step, ArrowUp:t+step}[event.key];
      if (target === undefined) return;
      event.preventDefault();
      event.stopPropagation();
      localSeek(target);
    };
    new ResizeObserver(paint).observe(graph);
    return {setClip, setEvents(saved) {
      events = saved;
      renderMarkers();
    }, setTime(id, time) {
      if (clip?.ref.clip !== id || !Number.isFinite(time) || current === time) return;
      current = time;
      paintCursor();
    }};
  }

  window.EgoInputTimeline = {create};
})();

