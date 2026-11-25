/* Recording-scoped event review beside the workflow input player. */
(() => {
  'use strict';

  function create(panel, seek, pause, onEvents = () => {}) {
    panel.innerHTML = [
      '<header><h3 id="pipelineInputEventTitle">事件标注</h3></header>',
      '<section class="input-manual-events" aria-label="手动标注">',
      '<button type="button" class="input-event-add" hidden>标注当前帧</button>',
      '<form class="input-event-form" hidden>',
      '<label>片段时间（s）<input name="time" type="number" min="0" step="any" required></label>',
      '<output class="input-event-time muted"></output>',
      '<label>标签<input name="label" required maxlength="80" placeholder="例如 首次接触"></label>',
      '<label>备注（可选）<input name="note" maxlength="500"></label>',
      '<div class="input-event-row"><button type="submit">保存标注</button>',
      '<button type="button" class="input-event-cancel">取消</button></div></form>',
      '<p class="input-event-status muted" role="status"></p><ul class="input-event-list"></ul></section>',
      '<div class="input-event-automatic"></div>'
    ].join('');
    const find = selector => panel.querySelector(selector);
    const form = find('form');
    const add = find('.input-event-add'), save = form.querySelector('[type="submit"]');
    const stamp = find('.input-event-time'), status = find('.input-event-status'), list = find('ul');
    const label = form.elements.namedItem('label'), note = form.elements.namedItem('note');
    const timestamp = form.elements.namedItem('time');
    const automatic = new window.TactileEventReview(find('.input-event-automatic'), seek,
      {compact:true, expandedList:true, loadSaved:true});
    let context = null, time = null, draft = null, saving = false;
    const format = t => t.toFixed(3) + ' s';
    const inScope = t => context?.scope && Number.isFinite(t) &&
      t >= context.scope.startS && t < context.scope.endS;

    function resetDraft() {
      draft = null;
      form.reset();
      form.hidden = true;
    }

    function draftTime() {
      return context?.scope ? context.scope.startS + timestamp.valueAsNumber : NaN;
    }

    timestamp.oninput = () => {
      stamp.textContent = inScope(draftTime()) ? '录制 ' + format(draftTime()) : '时间超出本段';
    };

    function openDraft(event) {
      pause();
      draft = event || {timeS:time};
      if (!inScope(draft.timeS)) { draft = null; return; }
      timestamp.value = (draft.timeS - context.scope.startS).toFixed(3);
      timestamp.max = String(context.scope.endS - context.scope.startS);
      timestamp.oninput();
      label.value = draft.label || '';
      note.value = draft.note || '';
      form.hidden = false;
      label.focus();
    }

    function renderEvents(data) {
      const scope = data.scope;
      if (data.sessionId !== context.ref.recordingId || scope?.name !== context.scope.name ||
          scope.startS !== context.scope.startS || scope.endS !== context.scope.endS) {
        throw Error('返回的标注与当前片段不匹配');
      }
      status.textContent = data.events.length ? '' : '暂无手动标注';
      onEvents(data.events);
      list.replaceChildren();
      for (const event of data.events) {
        const row = document.createElement('li');
        const jump = document.createElement('button');
        jump.type = 'button';
        jump.textContent = '+' + format(event.timeS - scope.startS) + ' · ' + event.label;
        jump.title = '录制 ' + format(event.timeS);
        jump.onclick = () => seek(event.timeS);
        const edit = document.createElement('button');
        edit.type = 'button';
        edit.textContent = '编辑';
        edit.setAttribute('aria-label', '编辑 ' + event.label);
        edit.onclick = () => { if (!saving) { seek(event.timeS); openDraft(event); } };
        row.append(jump, edit);
        if (event.note) {
          const text = document.createElement('small');
          text.textContent = event.note;
          row.appendChild(text);
        }
        list.appendChild(row);
      }
    }

    async function setClip(clip) {
      if (context?.ref.segmentRevision === clip?.ref.segmentRevision &&
          context?.ref.recordingId === clip?.ref.recordingId) return;
      context = null;
      time = null;
      saving = false;
      save.disabled = false;
      add.hidden = true;
      resetDraft();
      list.replaceChildren();
      automatic.clear();
      onEvents([]);
      panel.hidden = !clip;
      if (!clip) return;
      const entry = {ref:clip.ref, scope:null};
      context = entry;
      add.disabled = true;
      status.textContent = '正在读取所选片段的标注…';
      const pending = automatic.select(clip.ref.recordingId);
      automatic.setRange([clip.start, clip.end]);
      try {
        const detail = await automatic.request('/api/session?' +
          new URLSearchParams({id:clip.ref.recordingId, view:'review'}));
        if (context !== entry) return;
        const matching = detail.segments?.find(row => Object.entries(entry.ref)
          .every(([key, value]) => row.workflow?.[key] === value));
        if (detail.metadata?.session_id !== entry.ref.recordingId || !matching) {
          throw Error('未找到当前录制片段对应的复查区间');
        }
        const {name, startS, endS} = matching;
        entry.scope = {name, startS, endS, custom:false};
        const data = await automatic.request('/api/manual-events?' +
          new URLSearchParams({id:entry.ref.recordingId, scope:JSON.stringify(entry.scope)}));
        if (context !== entry) return;
        renderEvents(data);
        add.hidden = false;
        add.disabled = !inScope(time);
      } catch (error) {
        if (context === entry) status.textContent = '手动标注读取失败 · ' + error.message;
      }
      await pending;
    }

    add.onclick = () => openDraft(null);
    find('.input-event-cancel').onclick = resetDraft;
    form.onsubmit = async event => {
      event.preventDefault();
      if (saving || !draft || !inScope(draftTime()) || !label.value.trim()) return;
      const entry = context;
      const payload = {id:draft.id, timeS:draftTime(), label:label.value.trim(), note:note.value.trim()};
      saving = true;
      save.disabled = true;
      try {
        const data = await automatic.request('/api/manual-events',
          {session_id:entry.ref.recordingId, scope:entry.scope, action:'save', event:payload});
        if (context !== entry) return;
        renderEvents(data);
        resetDraft();
      } catch (error) {
        if (context === entry) status.textContent = '标注保存失败 · ' + error.message;
      } finally {
        if (context === entry) { saving = false; save.disabled = false; }
      }
    };
    return {setClip, setTime(clip, value) {
      if (context?.ref.clip !== clip || !Number.isFinite(value) || time === value) return;
      time = value;
      add.disabled = !inScope(time);
      automatic.setTime(time);
    }};
  }

  window.EgoInputEvents = {create};
})();
