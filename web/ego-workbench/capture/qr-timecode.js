(() => {
  'use strict';

  function create({display, status, stamp}) {
    const id = Array.from(crypto.getRandomValues(new Uint8Array(4)),
      value => value.toString(16).padStart(2, '0')).join('');
    const code = new QRCode(display, {width:420, height:420, correctLevel:QRCode.CorrectLevel.M});
    const refreshMs = 40;
    let anchor = null;
    let lastTime = -1;
    let active = false;
    let generation = 0;
    let syncTimer, drawTimer;

    function updateStatus(message) {
      if (status.textContent !== message) status.textContent = message;
    }

    async function measure() {
      const begin = performance.now();
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 1200);
      try {
        const reply = await fetch('./qr-clock', {cache:'no-store', signal:controller.signal});
        if (!reply.ok) return null;
        const {hostMonoMs} = await reply.json();
        const end = performance.now();
        if (!Number.isSafeInteger(hostMonoMs) || end - begin > 120) return null;
        return {offset:hostMonoMs - (begin + end) / 2, rtt:end - begin, measured:end};
      } finally {
        clearTimeout(timeout);
      }
    }

    async function synchronize(token) {
      let best = null;
      for (let index = 0; index < 3 && token === generation; index++) {
        try {
          const sample = await measure();
          if (sample && (!best || sample.rtt < best.rtt)) best = sample;
        } catch { /* Another request can still provide a clock sample. */ }
      }
      if (token !== generation) return;
      if (best) anchor = best;
      syncTimer = setTimeout(() => synchronize(token), 2200);
    }

    function blank() {
      display.dataset.ready = 'false';
      if (stamp) stamp.textContent = '';
    }

    function draw() {
      const now = performance.now();
      if (document.hidden || !anchor || now - anchor.measured > 6500) {
        blank();
        updateStatus('等待采集时钟');
        return;
      }
      const time = Math.floor((now + anchor.offset) / refreshMs) * refreshMs;
      if (time <= lastTime) return;
      const payload = 'EGO1:' + id + ':' + time + ':' + Math.ceil(anchor.rtt);
      code.makeCode(payload);
      display.dataset.ready = 'true';
      if (stamp) stamp.textContent = payload;
      updateStatus(stamp ? '采集时钟已连接 · 网络往返 ' + anchor.rtt.toFixed(1) + ' ms' : '采集时钟已连接');
      lastTime = time;
    }

    blank();
    return {setActive(value) {
      if (active === value) return;
      active = value;
      const token = ++generation;
      clearTimeout(syncTimer);
      clearInterval(drawTimer);
      anchor = null;
      blank();
      if (active) {
        updateStatus('连接采集时钟…');
        void synchronize(token);
        drawTimer = setInterval(draw, refreshMs);
      }
    }};
  }

  window.EgoQrClock = {create};
  const display = document.getElementById('qr');
  if (display) {
    const clock = create({display, status:document.getElementById('status'), stamp:document.getElementById('stamp')});
    const update = () => clock.setActive(!document.hidden);
    document.addEventListener('visibilitychange', update);
    update();
  }
})();
