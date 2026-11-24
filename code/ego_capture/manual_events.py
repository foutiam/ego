"""Persist operator events on their saved recording intervals."""
from __future__ import annotations

import math
from threading import Lock
from uuid import uuid4

from ego_capture import guided_capture
from visuotactile.pipeline import read_json
from visuotactile.storage import dump_json
from workflow_segments import fingerprint, identifier, review_intervals

FILE = 'manual-events.json'
SCHEMA = 'ego-manual-event-review-v1'
_LOCK = Lock()


def _seconds(value):
    if type(value) not in (float, int) or not math.isfinite(value):
        raise ValueError('请填写有效的录制时间')
    return float(value)


def _interval_key(start, end):
    return round(float(start) * 1_000_000), round(float(end) * 1_000_000)


def _resolve_scope(folder, requested, *, registry=None):
    metadata = read_json(folder / 'metadata.json')
    if metadata.get('session_id') != folder.name:
        raise ValueError('录制身份不一致')
    if not isinstance(requested, dict):
        raise ValueError('请先选择录制分段')
    duration = _seconds(metadata.get('duration_s'))
    custom = requested.get('custom') is True
    start, end = (_seconds(requested.get(field)) for field in ('startS', 'endS'))
    if not 0 <= start < end <= duration + (0 if custom else .001):
        raise ValueError('所选区间超出录制范围')
    requested_bounds = _interval_key(start, end)
    if requested_bounds[0] >= requested_bounds[1]:
        raise ValueError('所选区间过短')
    if custom:
        start_us, end_us = requested_bounds
        return dict(name='自选区间', startS=start_us / 1_000_000, endS=end_us / 1_000_000, custom=True)
    name = identifier(requested.get('name'))
    guided_file = folder / guided_capture.FILE
    guided = read_json(guided_file) if guided_file.is_file() else None
    for row in review_intervals(folder.name, duration, guided, registry=registry):
        if row['name'] == name and _interval_key(row['startS'], row['endS']) == requested_bounds:
            return dict(name=name, startS=row['startS'], endS=row['endS'], custom=False)
    raise ValueError('录制分段已变化，请刷新并重新选择')


def _identity(scope):
    return fingerprint(dict(name=scope['name'], bounds=_interval_key(scope['startS'], scope['endS']),
                            custom=scope['custom']))


def _read_store(folder):
    path = folder / FILE
    if not path.exists():
        return dict(schema=SCHEMA, sessionId=folder.name, segments={})
    stored = read_json(path)
    if (not isinstance(stored, dict) or stored.get('schema') != SCHEMA
            or stored.get('sessionId') != folder.name or not isinstance(stored.get('segments'), dict)):
        raise ValueError('已保存的手动标注与当前录制不一致')
    return stored


def _response(folder, scope, events):
    return dict(sessionId=folder.name, scope=scope,
                events=sorted(events.values(), key=lambda item: (item['timeS'], item['id'])))


def list_events(folder, requested, *, registry=None):
    scope = _resolve_scope(folder, requested, registry=registry)
    with _LOCK:
        saved = _read_store(folder)['segments'].get(_identity(scope), {})
    return _response(folder, scope, saved.get('events', {}))


def change_event(folder, requested, action, event, *, registry=None):
    scope = _resolve_scope(folder, requested, registry=registry)
    if action not in ('save', 'delete') or not isinstance(event, dict):
        raise ValueError('无法识别标注操作')
    event_id = event.get('id')
    with _LOCK:
        store = _read_store(folder)
        bucket = store['segments'].setdefault(_identity(scope), dict(scope=scope, events={}))
        events = bucket['events']
        if (action == 'delete' or event_id is not None) and (
                not isinstance(event_id, str) or event_id not in events):
            raise ValueError('这个标注已变化，请刷新后重试')
        if action == 'delete':
            del events[event_id]
        else:
            time_s = _seconds(event.get('timeS'))
            label, note = event.get('label'), event.get('note', '')
            if not scope['startS'] <= time_s < scope['endS']:
                raise ValueError('当前时间不在所选区间内')
            if not isinstance(label, str) or not label.strip() or len(label) > 80:
                raise ValueError('请填写 1–80 字的事件标签')
            if not isinstance(note, str) or len(note) > 500:
                raise ValueError('备注最多 500 字')
            event_id = event_id or uuid4().hex
            events[event_id] = dict(id=event_id, timeS=time_s, label=label.strip(), note=note.strip())
        dump_json(folder / FILE, store)
        return _response(folder, scope, events)


def copy_custom_events(folder, name, start, end):
    """Copy current custom marks into a newly published, independently editable segment."""
    custom_scope = dict(name='自选区间', startS=start, endS=end, custom=True)
    recorded_scope = dict(name=name, startS=start, endS=end, custom=False)
    with _LOCK:
        store = _read_store(folder)
        segments = store['segments']
        custom_events = segments.get(_identity(custom_scope), {}).get('events', {})
        if not custom_events:
            return 0
        target = segments.setdefault(_identity(recorded_scope), dict(scope=recorded_scope, events={}))
        events = target['events']
        added = 0
        for event_id, event in custom_events.items():
            if event_id not in events:
                events[event_id] = dict(event)
                added += 1
        if added:
            dump_json(folder / FILE, store)
        return added
