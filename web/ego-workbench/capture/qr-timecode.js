(() => {
  'use strict';

  const TICK_MS = 40;
  const MAX_RTT_MS = 120;
  const MAX_AGE_MS = 6500;

  function displayId() {
    const bytes = new Uint8Array(4);
    if (window.crypto?.getRandomValues) window.crypto.getRandomValues(bytes);
    else for (let index = 0; index < bytes.length; index++) bytes[index] = Math.floor(Math.random() * 256);
    return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
  }

  function create({display, status, stamp, retry}) {
    const id = displayId();
    const code = new QRCode(display, {width:420, height:420, correctLevel:QRCode.CorrectLevel.M});
    let anchor = null;
    let issue = '';
    let lastTime = -1;
    let active = false;
    let generation = 0;
    let requestId = 0;
    let pending, syncTimer, drawTimer;

    function updateStatus(message, canRetry = false) {
      if (status.textContent !== message) status.textContent = message;
      if (retry) retry.hidden = !canRetry;
    }

    function measure() {
      return new Promise(resolve => {
        const request = new XMLHttpRequest();
        request.open('GET', './qr-clock?probe=' + id + '-' + ++requestId);
        request.timeout = 1600;
        const begin = performance.now();
        let headersAt = null;
        pending = request;
        request.onreadystatechange = () => {
          if (request.readyState === 2 && headersAt === null) headersAt = performance.now();
        };
        const finish = result => {
          if (pending === request) pending = null;
          resolve(result);
        };
        request.onload = () => {
          const end = headersAt ?? performance.now();
          if (request.status !== 200) {
            finish({issue:'采集时钟返回错误（HTTP ' + request.status + '）。'});
            return;
          }
          let hostMonoMs;
          try { hostMonoMs = JSON.parse(request.responseText).hostMonoMs; }
          catch { /* Report malformed or missing time from this endpoint. */ }
          if (!Number.isSafeInteger(hostMonoMs) || hostMonoMs <= 0) {
            finish({issue:'采集时钟返回的时间无效。'});
          } else if (end - begin > MAX_RTT_MS) {
            finish({issue:'手机与电脑之间延迟过高（' + Math.ceil(end - begin) +
              ' ms）。请保持同一 Wi-Fi，靠近接入点后重试。'});
          } else {
            finish({offset:hostMonoMs - (begin + end) / 2, rtt:end - begin, measured:end});
          }
        };
        request.onerror = () => finish({issue:'手机无法访问采集时钟。请确认与电脑连接到同一 Wi-Fi。'});
        request.ontimeout = () => finish({issue:'采集时钟响应超时。请确认与电脑连接到同一 Wi-Fi。'});
        request.onabort = () => finish(null);
        request.send();
      });
    }

    async function synchronize(token) {
      let best = null;
      for (let index = 0; index < 3 && token === generation; index++) {
        const sample = await measure();
        if (token !== generation || !sample) return;
        if (sample.issue) {
          issue = sample.issue;
          continue;
        }
        if (!best || sample.rtt < best.rtt) best = sample;
        if (!anchor || performance.now() - anchor.measured > MAX_AGE_MS || sample.rtt <= anchor.rtt) {
          anchor = sample;
        }
        draw();
      }
      if (token !== generation) return;
      if (best) {
        anchor = best;
        issue = '';
      }
      syncTimer = setTimeout(() => synchronize(token), anchor ? 2200 : 650);
    }

    function blank() {
      display.dataset.ready = 'false';
      if (stamp) stamp.textContent = '';
    }

    function draw() {
      const now = performance.now();
      if (document.hidden || !anchor || now - anchor.measured > MAX_AGE_MS) {
        blank();
        updateStatus(issue || '正在连接采集时钟…', Boolean(issue));
        return;
      }
      const time = Math.floor((now + anchor.offset) / TICK_MS) * TICK_MS;
      if (time <= lastTime) return;
      const payload = 'EGO1:' + id + ':' + time + ':' + Math.ceil(anchor.rtt);
      code.makeCode(payload);
      display.dataset.ready = 'true';
      if (stamp) stamp.textContent = payload;
      updateStatus(stamp ? '采集时钟已连接 · 网络往返 ' + anchor.rtt.toFixed(1) + ' ms' : '采集时钟已连接');
      lastTime = time;
    }

    function setActive(value) {
      if (active === value) return;
      active = value;
      const token = ++generation;
      clearTimeout(syncTimer);
      clearInterval(drawTimer);
      if (pending) pending.abort();
      anchor = null;
      issue = '';
      blank();
      if (active) {
        updateStatus('正在连接采集时钟…');
        void synchronize(token);
        drawTimer = setInterval(draw, TICK_MS);
      }
    }

    blank();
    if (retry) retry.addEventListener('click', () => {
      setActive(false);
      setActive(!document.hidden);
    });
    return {setActive};
  }

  window.EgoQrClock = {create};
  const display = document.getElementById('qr');
  if (display) {
    const clock = create({display, status:document.getElementById('status'),
      stamp:document.getElementById('stamp'), retry:document.getElementById('retry')});
    const screen = document.getElementById('phoneDisplay');
    const clockPanel = document.getElementById('clockPanel');
    const boardPanel = document.getElementById('boardPanel');
    const boardImage = document.getElementById('boardImage');
    const buttons = Array.from(document.querySelectorAll('[data-display-mode]'));
    let mode = 'qr';
    const update = () => clock.setActive(!document.hidden && mode !== 'board');
    for (const button of buttons) {
      button.addEventListener('click', () => {
        mode = button.dataset.displayMode;
        screen.dataset.mode = mode;
        clockPanel.hidden = mode === 'board';
        boardPanel.hidden = mode === 'qr';
        if (!boardPanel.hidden && !boardImage.hasAttribute('src')) {
          boardImage.src = location.pathname.startsWith('/ego-capture/') ? './api/exo/board.png' : './qr-board.png';
        }
        for (const choice of buttons) choice.setAttribute('aria-pressed', String(choice === button));
        update();
      });
    }
    boardImage?.addEventListener('error', () => {
      boardImage.removeAttribute('src');
      document.getElementById('boardNote').textContent = '标定板加载失败，请刷新页面或核对工作台提供的手机地址。';
    });
    document.addEventListener('visibilitychange', update);
    window.addEventListener('pageshow', update);
    update();
  }
})();

