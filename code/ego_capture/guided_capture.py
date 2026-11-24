"""Task intervals over one continuous, recoverable raw-device recording.

The localized strings below are intentional UI instructions, not behavior labels.
Completing a slot records an attempt; it never certifies contact or task success.
"""
from __future__ import annotations

import copy
import json
import time
import uuid

from visuotactile.storage import dump_json, safe_id, session_path


PROFILE = dict(object_id='white_cup_2', name='白杯2（泡沫）', material='foam',
               geometry=dict(height_mm=90, bottom_diameter_mm=48,
                             rim_diameter_mm=80, wall_thickness_mm=2),
               source='user_dimensions_20261020', mass_kg=None,
               rigidity='check_deformation_from_recording')
PROFILES = {
    PROFILE['object_id']: PROFILE,
    'white_cup_1': dict(object_id='white_cup_1', name='白杯1（原白杯，材质未确认）', material='unknown',
                        geometry=dict(height_mm=98, bottom_diameter_mm=55,
                                      rim_diameter_mm=80, wall_thickness_mm=1),
                        source='user_dimensions_confirmed_for_session_20261017_193812_669910',
                        mass_kg=None, rigidity='operator_rigid_object_assumption'),
}


def object_profile(object_id):
    if not isinstance(object_id, str) or object_id not in PROFILES:
        raise ValueError('请选择已登记的杯子。')
    return copy.deepcopy(PROFILES[object_id])


TASKS = [
    dict(id='reference', title='空场与物体参考', minimum=1, seconds='60–120秒',
         instruction='手离开，杯子静置；拍清桌面、杯底和A/B目标区域，再缓慢展示杯体。'),
    dict(id='hand_identity', title='手指身份与掌姿', minimum=1, seconds='90–120秒',
         instruction='五指分别屈伸；固定指形转掌，再改变指形，露出拇指、食指和掌部参考。'),
    dict(id='sync', title='每组同步参考：只按平面，不拿杯子', minimum=10, seconds='每格15–20秒',
         instruction='每组开始、结束各一格。保持手腕静止，只用食指指垫按住固定平面约1秒、抬离约1秒，重复3–5次；双目看清指垫与平面接缝。不要用拿放杯子代替。录完不代表同步已校准。'),
    dict(id='normal', title='正常拿放', minimum=10, seconds='每格15–20秒',
         instruction='按格子中的A/B目标拿放；改变初始位置和速度，放下、松开并撤手。'),
    dict(id='miss', title='首次未夹住后的操作', minimum=10, seconds='每格15–20秒',
         instruction='在桌面附近保留实际尝试，之后自主等待、张开、调整或重试。若直接成功也保留；条目名称不作为未夹住的真值。'),
    dict(id='adjust', title='接触不足后的调整', minimum=10, seconds='每格15–20秒',
         instruction='在桌面附近轻微接触，感觉不稳时自主调整，再尝试完成目标，避免捏变形。'),
    dict(id='abort', title='失败／放弃', minimum=10, seconds='每格15–20秒',
         instruction='按真实情况保留未完成尝试和后续动作，不必为凑满建议格数制造失败或补演成功。刻意未完成与自然失败须区分，实际结果稍后核对。'),
]
INTERACTION_TASKS = [
    dict(id='attempt', title='物体交互'),
    dict(id='normal', title='正常拿放'),
    dict(id='reference', title='空场与交互物体参考'),
    dict(id='hand_identity', title='掌姿参考'),
    dict(id='sync', title='同步参考'),
]
INTERACTION_MODE = 'interaction'
OUTCOMES = ('success', 'failed', 'incomplete')
FILE = 'guided-capture.json'
MAX_SECONDS = 2400
MAX_BYTES = 12 * 1024**3


def recipe(mode=None):
    if mode == INTERACTION_MODE:
        return dict(tasks=copy.deepcopy(INTERACTION_TASKS), countdown_seconds=3)
    return dict(profile=copy.deepcopy(PROFILE), profiles=copy.deepcopy(list(PROFILES.values())),
                tasks=copy.deepcopy(TASKS),
                minimum_slots=sum(t['minimum'] for t in TASKS),
                operation_attempts=40, countdown_seconds=3,
                max_session_seconds=MAX_SECONDS, max_session_bytes=MAX_BYTES,
                raw_capture='continuous_until_finish',
                split_scope='complete_operations_of_one_object_not_object_generalization')


def slot(task_id, number, mode=None):
    tasks = INTERACTION_TASKS if mode == INTERACTION_MODE else TASKS
    task = next((t for t in tasks if t['id'] == task_id), None)
    if task is None or isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= 200:
        raise ValueError('无效的分项类型或编号')
    if mode == INTERACTION_MODE:
        return dict(key=f'{task_id}-{number}', task_id=task_id, number=number)
    operation = task_id in ('normal', 'miss', 'adjust', 'abort')
    block = (number - 1) // 2 + 1 if operation or task_id == 'sync' else None
    split = ('train' if block <= 3 else 'dev' if block == 4 else 'test' if block == 5 else 'unassigned') if block else None
    return dict(key=f'{task_id}-{number}', task_id=task_id, number=number,
                block=block, operation_split=split,
                target=('A' if number % 2 else 'B') if operation else None,
                sync_position=('start' if number % 2 else 'end') if task_id == 'sync' else None,
                extra=number > task['minimum'])


def _read(path):
    return json.loads((path / FILE).read_text('utf-8'))


def _elapsed(session):
    return (time.monotonic_ns() - session.start_mono_ns) / 1e9


def status(app, session_id=None):
    with app.devices.lock:
        current = app.devices.session
        if not session_id and current and current.metadata.get('capture_mode') == 'guided':
            session_id = current.id
        result = dict(recipe=recipe(INTERACTION_MODE), manifest=None, recording=False)
        if session_id:
            path = session_path(app.root, session_id)
            if (path / FILE).exists():
                result['manifest'] = _read(path)
                result['recording'] = bool(current and current.id == session_id)
                result['elapsed_s'] = _elapsed(current) if result['recording'] else None
                if not result['recording'] and result['manifest']['status'] == 'recording':
                    result['manifest'].update(status='interrupted', active_id=None)
                    for entry in result['manifest']['segments']:
                        if entry['status'] == 'recording':
                            entry.update(status='interrupted', stop_reason='writer_not_running')
        return result


def begin(app, data):
    mode = data.get('mode')
    if mode not in (None, INTERACTION_MODE):
        raise ValueError('无效的分项采集模式')
    details = slot(data.get('task_id'), data.get('number'), mode)
    if mode == INTERACTION_MODE:
        details['label'] = _label(data.get('label'))
    dev = app.devices
    with dev.lock:
        session = dev.session
        expected = data.get('session_id')
        if expected and (not session or session.id != expected):
            raise ValueError('这条时间轴已结束；请点击「新建下一条时间轴」后再录制。')
        if session and session.metadata.get('capture_mode') != 'guided':
            raise ValueError('普通录制正在进行，请先停止并保存，再开始分项采集。')
        if not session:
            if mode == INTERACTION_MODE:
                object_id = _interaction_object_id(data.get('object_id'))
                material = data.get('material', 'unknown')
                if material not in ('unknown', 'foam', 'transparent', 'opaque', 'other'):
                    raise ValueError('无效的物体材质')
                profile = object_profile(object_id) if object_id in PROFILES else dict(
                    object_id=object_id, name=object_id, material=material, source='operator_declared')
            else:
                profile = object_profile(data.get('object_id', PROFILE['object_id']))
            metadata = dict(object_id=profile['object_id'], scene_id=data.get('scene_id', 'scene_001'),
                            material=profile['material'], split=data.get('split', 'train'), capture_mode='guided',
                            object_profile=profile, guided_capture_file=FILE,
                            operation_split_scope='within_object_not_object_disjoint')
            dev.start_recording(metadata)
            session = dev.session
            manifest = dict(schema='guided-capture-v1', session_id=session.id, status='recording',
                            profile=copy.deepcopy(profile), recipe=recipe(mode), segments=[], active_id=None,
                            raw_capture='continuous_until_finish',
                            marker_evidence='operator_task_boundaries_not_contact_or_success_ground_truth')
        else:
            manifest = _read(session.path)
            requested_object = data.get('object_id')
            if requested_object is not None and mode == INTERACTION_MODE:
                requested_object = _interaction_object_id(requested_object)
            if requested_object is not None and requested_object != manifest['profile']['object_id']:
                raise ValueError('同一时间轴对应同一个物体。更换物体前请先保存整条时间轴。')
        active = next((s for s in manifest['segments'] if s['id'] == manifest['active_id']), None)
        if active:
            if active['key'] == details['key']:
                return status(app, session.id)
            raise ValueError('请先结束当前格子的录制。')
        if any(s['key'] == details['key'] and s['status'] == 'recorded' and not s.get('deleted')
               for s in manifest['segments']):
            raise ValueError('该格子已采集；请用＋增加，或先移除该片段。')
        entry = dict(details, id=uuid.uuid4().hex, status='recording', deleted=False,
                     start_s=_elapsed(session), end_s=None, observed_outcome=None,
                     contact_verified=False, behavior_reviewed=False)
        manifest['segments'].append(entry)
        manifest['active_id'] = entry['id']
        dump_json(session.path / FILE, manifest)
        session.event('guided_segment_start', segment_id=entry['id'], **details)
        return status(app, session.id)


def _interaction_object_id(value):
    if not isinstance(value, str):
        raise ValueError('请填写交互物体编号')
    return safe_id(value)


def _label(value):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise ValueError('片段标签须为文字')
    value = value.strip()
    if len(value) > 64 or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError('片段标签限 64 个字符，须为单行')
    return value


def annotate(app, data):
    """Attach the operator's label and declared result to one finished attempt."""
    label = _label(data.get('label'))
    outcome = data.get('outcome') or None
    if outcome is not None and outcome not in OUTCOMES:
        raise ValueError('无效的分项采集结果')
    with app.devices.lock:
        path = session_path(app.root, data.get('session_id'))
        manifest = _read(path)
        entry = next((s for s in manifest['segments'] if s['id'] == data.get('segment_id')), None)
        if not entry or entry['status'] != 'recorded' or entry.get('deleted'):
            raise ValueError('只能标注已完成的分项片段')
        entry.update(label=label, operator_outcome=outcome)
        dump_json(path / FILE, manifest)
        return status(app, manifest['session_id'])


def end(app, data):
    with app.devices.lock:
        session = app.devices.session
        if not session or session.id != data.get('session_id'):
            raise ValueError('当前会话已停止，请刷新分项采集状态。')
        manifest = _read(session.path)
        entry = next((s for s in manifest['segments'] if s['id'] == data.get('segment_id')), None)
        if not entry:
            raise ValueError('Unknown segment')
        if entry['status'] != 'recording':
            return status(app, session.id)
        if manifest['active_id'] != entry['id']:
            raise ValueError('Active segment changed')
        entry.update(end_s=_elapsed(session), status='recorded')
        entry['duration_s'] = entry['end_s'] - entry['start_s']
        manifest['active_id'] = None
        dump_json(session.path / FILE, manifest)
        session.event('guided_segment_end', segment_id=entry['id'], slot_key=entry['key'])
        return status(app, session.id)


def change_deleted(app, data):
    with app.devices.lock:
        path = session_path(app.root, data.get('session_id'))
        manifest = _read(path)
        entry = next((s for s in manifest['segments'] if s['id'] == data.get('segment_id')), None)
        if entry and entry['status'] == 'recording' and not (app.devices.session and app.devices.session.id == manifest['session_id']):
            entry.update(status='interrupted', stop_reason='writer_not_running')
            manifest.update(status='interrupted', active_id=None)
        if not entry or entry['status'] == 'recording':
            raise ValueError('请先结束该片段，再移除。')
        deleted = data.get('deleted')
        if not isinstance(deleted, bool):
            raise ValueError('deleted must be a boolean')
        if not deleted and any(s['id'] != entry['id'] and s['key'] == entry['key']
                               and s['status'] == 'recorded' and not s.get('deleted') for s in manifest['segments']):
            raise ValueError('该格子已重新采集，不能恢复为重复片段。')
        entry['deleted'] = deleted
        entry['selection_changed_host_ns'] = time.time_ns()
        entry.setdefault('selection_history', []).append(dict(deleted=deleted, host_ns=entry['selection_changed_host_ns']))
        dump_json(path / FILE, manifest)
        return status(app, manifest['session_id'])


def finish(app, data):
    with app.devices.lock:
        session = app.devices.session
        if not session or session.id != data.get('session_id'):
            return status(app, data.get('session_id'))
        manifest = _read(session.path)
        if manifest['active_id']:
            end(app, dict(session_id=session.id, segment_id=manifest['active_id']))
        app.devices.stop_recording()
        return status(app, data.get('session_id'))


def close_manifest(session, reason):
    """Finalize task state also on device loss, safety limits and server shutdown."""
    if not (session.path / FILE).exists():
        return
    manifest = _read(session.path)
    for entry in manifest['segments']:
        if entry['status'] == 'recording':
            entry.update(end_s=_elapsed(session), status='interrupted', stop_reason=reason)
            entry['duration_s'] = entry['end_s'] - entry['start_s']
    manifest.update(status='saved', active_id=None, stop_reason=reason)
    dump_json(session.path / FILE, manifest)
    session.metadata['guided_capture'] = dict(file=FILE,
        recorded_segments=sum(s['status'] == 'recorded' and not s.get('deleted') for s in manifest['segments']),
        operation_split_scope='complete_operations_not_object_disjoint')
