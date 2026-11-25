"""Review optional calibration contacts staged around an ordinary recording."""
from __future__ import annotations

import csv
import json
from functools import lru_cache
import math
import re

import numpy as np

from visuotactile.pipeline import read_json, read_jsonl
from visuotactile.storage import contact_calibration_windows, dump_json, recorded_task_range


INDEX = 'derived/contact-calibration/frames.jsonl'
REVIEW = 'contact-calibration-review.json'
PARTS = ('before', 'after')
CAMERAS = ('cam0', 'cam1', 'exo1', 'exo2', 'exo3')


def camera_options(metadata):
    return list(CAMERAS[:2]) + [role for role in CAMERAS[2:]
                                if (metadata.get('exo_live') or {}).get(role, {}).get('status') == 'saved']


def prepare_exo_frames(folder, minimum_free_bytes):
    import shutil

    import av
    import cv2

    metadata = read_json(folder / 'metadata.json')
    windows = contact_calibration_windows(metadata)
    if not windows:
        return
    root = (folder / INDEX).parent
    root.mkdir(parents=True, exist_ok=True)
    index = root / 'exo-frames.jsonl'
    if index.is_file():
        return
    rows = []
    for role in camera_options(metadata)[2:]:
        record = metadata['exo_live'][role]
        video_name, clock_name = f'exo-live/{role}-live.mp4', f'exo-live/{role}-live.csv'
        if record.get('video') != video_name or record.get('timestamps') != clock_name:
            continue
        if shutil.disk_usage(folder).free <= minimum_free_bytes:
            break
        try:
            video, clocks = folder / video_name, folder / clock_name
            if not all(p.resolve().is_relative_to(folder.resolve()) for p in (video, clocks)):
                continue
            with clocks.open(encoding='utf-8-sig', newline='') as stream:
                receipt = {int(row['video_pts']): (int(row['host_mono_ns']) - metadata['start_mono_ns']) / 1e9
                           for row in csv.DictReader(stream)}
            dest = root / role
            dest.mkdir(exist_ok=True)
            for part, (start, end) in windows.items():
                selected = {pts: timestamp for pts, timestamp in receipt.items() if start <= timestamp < end}
                if not selected:
                    continue
                first, last = min(selected), max(selected)
                with av.open(str(video)) as container:
                    stream = container.streams.video[0]
                    if first > 0:
                        container.seek(int(first / 90000 / float(stream.time_base)), stream=stream, backward=True)
                    for frame in container.decode(stream):
                        if frame.pts is None or frame.time_base is None:
                            continue
                        pts = round(frame.pts * frame.time_base * 90000)
                        if pts > last:
                            break
                        if pts not in selected:
                            continue
                        scale = min(1., 960 / frame.width, 720 / frame.height)
                        width = max(1, round(frame.width * scale))
                        height = max(1, round(frame.height * scale))
                        pixels = frame.reformat(width=width, height=height, format='bgr24').to_ndarray()
                        ok, image = cv2.imencode('.jpg', pixels, [cv2.IMWRITE_JPEG_QUALITY,85])
                        if ok:
                            image.tofile(dest / f'{pts}.jpg')
                            rows.append(dict(cam=role, part=part, device_ns=str(pts),
                                             source_s=pts/90000, receipt_s=selected[pts]))
                            if len(rows) % 200 == 0 and shutil.disk_usage(folder).free <= minimum_free_bytes:
                                raise OSError('Calibration-extract storage limit reached')
        except (OSError, ValueError, IndexError, KeyError, av.FFmpegError, cv2.error):
            # A damaged optional angle must not block the task or the other angles.
            continue
    temp = index.with_suffix('.tmp')
    temp.write_text(''.join(json.dumps(row)+'\n' for row in rows), encoding='utf-8')
    temp.replace(index)


def phase(record):
    if record['task_start_s'] is None:
        return 'before'
    return 'task' if record['task_end_s'] is None else 'after'


def camera_clock(folder, cam):
    metadata = read_json(folder / 'metadata.json')
    reviewed_intake = metadata.get('capture_intake_required') and metadata.get('contact_calibration')
    sources = ('qr-auto-sync.json',) if cam.startswith('exo') or reviewed_intake else (
        'exo-session.json', 'qr-sync.json', 'qr-auto-sync.json')
    name = ('EXO ' + cam[-1]) if cam.startswith('exo') else ('Ego CAM' + cam[-1])
    for filename in sources:
        path = folder / 'derived' / filename
        if not path.is_file():
            continue
        report = read_json(path)
        if report.get('session_id') != folder.name:
            continue
        report = report.get('sync', report)
        if cam.startswith('exo') and not any(row.get('name') == name and row.get('video') == f'exo-live/{cam}-live.mp4'
                                              for row in report.get('outcomes', [])):
            continue
        display = report.get('display_id')
        for model in report.get('cameras', []):
            if (model.get('name') == name and model.get('mode') == 'offset_and_drift'
                    and display and model.get('display_id') == display
                    and all(type(model.get(k)) in (float, int) and math.isfinite(model[k])
                            for k in ('source_origin_s', 'host_mono_at_origin_s', 'scale'))):
                return model, filename
    return None, None


def review_notes(folder, filename=REVIEW):
    path = folder / filename
    if not path.is_file():
        return {}
    data = read_json(path)
    if (data.get('schema') != 'ego-contact-calibration-review-v1'
            or data.get('session_id') != folder.name or not isinstance(data.get('parts'), dict)):
        raise ValueError('校准复核记录格式或录制编号不符')
    return data['parts']


def capture_camera_clock(folder):
    """Shared primary camera clock used by the registered-recording paths."""
    metadata = read_json(folder / 'metadata.json')
    reviewed_intake = metadata.get('capture_intake_required') and metadata.get('contact_calibration')
    names = ('qr-auto-sync.json',) if reviewed_intake else ('exo-session.json', 'qr-sync.json', 'qr-auto-sync.json')
    for name in names:
        path = folder / 'derived' / name
        if not path.is_file():
            continue
        report = read_json(path)
        if report.get('session_id') != folder.name:
            raise ValueError('相机对时记录不属于当前录制')
        if name == 'exo-session.json':
            if report.get('schema') != 'ego-exo-session-v1':
                raise ValueError('无效的 EXO 对时记录')
            report = report.get('sync', {})
        elif report.get('schema') != 'ego-qr-clock-v1':
            raise ValueError('无效的二维码对时记录')
        display = report.get('display_id')
        cameras = {item.get('name'): item for item in report.get('cameras', [])
                   if item.get('mode') == 'offset_and_drift' and display and item.get('display_id') == display}
        if 'Ego CAM0' in cameras and (name == 'exo-session.json' or 'Ego CAM1' in cameras):
            return cameras['Ego CAM0'], path
    return None, None


def visual_time(frame, origin, camera):
    if camera is None:
        return float(frame['t_s'])
    return (camera['host_mono_at_origin_s'] - origin / 1e9
            + camera['scale'] * (int(frame['device_ns']) / 1e9 - camera['source_origin_s']))


def tactile_alignment(folder, metadata=None, *, camera_clock_path=None, camera=None):
    """Fit offset between reviewed task-side anchors, never extrapolate into other takes."""
    metadata = metadata if metadata is not None else read_json(folder / 'metadata.json')
    if not metadata.get('contact_calibration'):
        return {'status': 'not_requested'}
    task = recorded_task_range(metadata)
    if task is None:
        return {'status': 'no_task'}
    checks = review_notes(folder)
    missing = [part for part in PARTS if checks.get(part, {}).get('status') != 'pair_confirmed']
    if missing:
        return {'status': 'awaiting_pairs', 'missing': missing}
    anchors = []
    windows = contact_calibration_windows(metadata)
    for part in PARTS:
        check = checks[part]
        cam = check['cam']
        current, basis = camera_clock(folder, cam)
        if camera is None:
            compatible = check['camera_clock'] == 'host_receipt'
        else:
            compatible = (bool(current) and check['camera_clock'] == basis
                          and current['display_id'] == camera['display_id'])
        if not compatible:
            return {'status': 'incompatible_clocks'}
        if camera is not None:
            # A changed QR model invalidates the saved visual timestamps until reconfirmed.
            filename = 'exo-frames.jsonl' if cam.startswith('exo') else 'frames.jsonl'
            index = (folder / INDEX).with_name(filename)
            selected = {r['device_ns']: r for r in read_jsonl(index) if r['cam'] == cam and r['part'] == part}
            for field in ('before', 'after'):
                saved = check[field]
                row = selected.get(saved['frame_id'])
                if row is None:
                    return {'status': 'incompatible_clocks'}
                projected = (current['host_mono_at_origin_s'] - metadata['start_mono_ns'] / 1e9
                             + current['scale'] * (row['source_s'] - current['source_origin_s']))
                if abs(projected - saved['time_s']) > 1e-6:
                    return {'status': 'incompatible_clocks'}
        raw = check['touch']['time_s']
        start, end = check['before']['time_s'], check['after']['time_s']
        window = windows.get(part)
        if not (window and all(math.isfinite(v) for v in (raw, start, end))
                and window[0] <= raw < window[1] and start <= end
                and all(window[0] <= check[key]['receipt_s'] < window[1] for key in ('before', 'after'))):
            return {'status': 'invalid_anchors'}
        samples = touch_series(folder, window)
        index = next((i for i, row in enumerate(samples) if row['sample'] == check['touch']['sample']), -1)
        if index < 0 or samples[index]['time_s'] != raw:
            return {'status': 'invalid_anchors'}
        if not any(item['response'] > 0 for item in samples[index:index+2]):
            return {'status': 'weak_touch_anchor', 'phase': part}
        anchors.append(dict(part=part, camera=cam, rawS=raw, visualS=(start + end) / 2,
                            visualBracketS=[start, end], frameIds=[check['before']['frame_id'], check['after']['frame_id']],
                            tactileSample=check['touch']['sample']))
    first, last = anchors
    if camera is None and first['camera'] != last['camera']:
        return {'status': 'incompatible_clocks'}
    if not (first['rawS'] < last['rawS'] and first['visualBracketS'][1] < last['visualBracketS'][0]):
        return {'status': 'invalid_anchors'}
    scale = (last['visualS'] - first['visualS']) / (last['rawS'] - first['rawS'])
    if not math.isfinite(scale) or scale <= 0:
        return {'status': 'invalid_anchors'}
    return {'status': 'applied', 'method': 'manual_bracketing_linear_v1', 'anchors': anchors,
            'scale': scale, 'rawOriginS': first['rawS'], 'visualOriginS': first['visualS'],
            'taskRangeS': list(task), 'cameraClock': camera_clock_path.name if camera_clock_path else 'host_receipt',
            'physicallyVerified': False}


def timing_for_recording(folder, metadata=None):
    camera, source = capture_camera_clock(folder)
    return tactile_alignment(folder, metadata, camera_clock_path=source, camera=camera), camera, source


def map_touch_time(time, model):
    if model.get('status') != 'applied':
        return time
    return model['visualOriginS'] + (time - model['rawOriginS']) * model['scale']


def raw_touch_time(time, model):
    if model.get('status') != 'applied':
        return time
    return model['rawOriginS'] + (time - model['visualOriginS']) / model['scale']


@lru_cache(maxsize=4)
def _review_clock_arrays(path, size, mtime_ns, origin, source_origin, host_at_origin, scale):
    pairs = sorted(read_jsonl(path), key=lambda row: row['t_s'])
    raw = np.asarray([row['t_s'] for row in pairs])
    aligned = np.asarray([host_at_origin - origin / 1e9 +
                          scale * (int(row['device_ns']) / 1e9 - source_origin) for row in pairs])
    return raw, aligned


def review_camera_axes(folder, metadata, camera):
    path = folder / 'derived/paired_frames.jsonl'
    if camera is None or not path.is_file():
        return np.empty(0), np.empty(0)
    stat = path.stat()
    return _review_clock_arrays(path, stat.st_size, stat.st_mtime_ns, int(metadata['start_mono_ns']),
                                camera['source_origin_s'], camera['host_mono_at_origin_s'], camera['scale'])


def move_between_camera_axes(time, source, target):
    if not len(source):
        return time
    # Use the exposure offsets through the clip and the nearest offset at its ends.
    if (np.diff(source) < 0).any():
        ordered = np.argsort(source, kind='stable')
        source, target = source[ordered], target[ordered]
    return time + np.interp(time, source, target - source)



@lru_cache(maxsize=8)
def _touch_series(path, size, mtime_ns, start, end):
    timeseries = []
    if path.is_file():
        with np.load(path, allow_pickle=False) as data:
            times, values = data['time_s'], data['raw_values']
            lo, hi = np.searchsorted(times, [start, end], side='left')
            times, values = times[lo:hi], values[lo:hi]
            if len(times):
                # The quiet period is intentionally recorded before the tap.
                baseline = np.median(values[times <= times[0] + .35], axis=0)
                peak = np.maximum(values - baseline, 0).max(axis=1)
                stamps, indices, counts = np.unique(times, return_index=True, return_counts=True)
                for stamp, value, count, index in zip(stamps, np.maximum.reduceat(peak, indices), counts, indices):
                    timeseries.append({'sample': int(lo + index), 'time_s': float(stamp),
                                       'response': round(float(value), 2), 'batch': int(count)})
    return timeseries


def touch_series(folder, interval):
    path = folder / 'derived/glove_signals.npz'
    if not path.is_file():
        return []
    stat = path.stat()
    return _touch_series(path, stat.st_size, stat.st_mtime_ns, *interval)


def view(folder, part, cam):
    if part not in PARTS or cam not in CAMERAS:
        raise ValueError('请选择校准时段和相机')
    metadata = read_json(folder / 'metadata.json')
    if not metadata.get('contact_calibration'):
        return {'available': False, 'message': '这条录制没有启用任务前后校准'}
    checks = review_notes(folder)
    timing, primary, _ = timing_for_recording(folder, metadata)
    cameras = camera_options(metadata)
    interval = contact_calibration_windows(metadata).get(part)
    if interval is None:
        return {'available': False, 'message': '该段没有校准录像，可以跳过', 'checks': checks, 'cameras': cameras, 'tactile_alignment': timing}
    index = folder / INDEX
    if cam.startswith('exo'):
        index = index.with_name('exo-frames.jsonl')
    if not index.is_file():
        return {'available': False, 'message': '等待录制整理生成逐帧画面', 'checks': checks, 'cameras': cameras, 'tactile_alignment': timing}
    start, end = interval
    model, source = camera_clock(folder, cam) if primary else (None, None)
    if model and model['display_id'] != primary['display_id']:
        model, source = None, None
    if metadata.get('capture_intake_required') and not (folder / 'derived/qr-auto-sync.json').is_file():
        return {'available': False, 'message': '等待复核画面和相机时间轴', 'checks': checks,
                'cameras': cameras, 'tactile_alignment': timing}
    frames = []
    for row in sorted((r for r in read_jsonl(index) if r['cam'] == cam and r['part'] == part),
                      key=lambda item: int(item['device_ns'])):
        stamp = row['device_ns']
        aligned = (model['host_mono_at_origin_s'] - metadata['start_mono_ns'] / 1e9
                   + model['scale'] * (row['source_s'] - model['source_origin_s'])) if model else row['receipt_s']
        frames.append({'frame_id': stamp, 'time_s': aligned, 'receipt_s': row['receipt_s']})

    timeseries = touch_series(folder, interval)
    return {'available': True, 'frames': frames, 'touch': timeseries, 'checks': checks,
            'camera_clock': source or 'host_receipt', 'part': part, 'cam': cam, 'cameras': cameras,
            'tactile_alignment': timing}


def save_check(folder, part, action, data):
    metadata = read_json(folder / 'metadata.json')
    if not metadata.get('contact_calibration') or part not in PARTS:
        raise ValueError('请选择启用了校准的录制与时段')
    if action == 'skip':
        result = {'status': 'skipped'}
    elif action == 'confirm':
        candidate = view(folder, part, data.get('cam'))
        frames = candidate.get('frames', [])
        selected = str(data.get('frame_id', ''))
        index = next((i for i, row in enumerate(frames) if row['frame_id'] == selected), -1)
        if index < 1 or data.get('previous_id') != frames[index - 1]['frame_id']:
            raise ValueError('请确认相邻的未接触帧与首次接触帧')
        before, after = frames[index - 1:index + 1]
        if before['time_s'] > after['time_s']:
            raise ValueError('这一对画面的接收时间乱序，请换视角或使用已对时画面')
        result = {'status': 'visual_confirmed', 'cam': candidate['cam'],
                  'camera_clock': candidate['camera_clock'], 'before': before, 'after': after}
        sample = data.get('sample')
        if sample is not None:
            touch = next((row for row in candidate['touch'] if row['sample'] == sample), None)
            if touch is None:
                raise ValueError('触觉时间不属于这个校准片段')
            delta = [(touch['time_s'] - r['time_s']) * 1000 for r in (after, before)]
            result.update(status='pair_confirmed', touch=touch, delay_ms=sorted(round(v, 2) for v in delta))
    else:
        raise ValueError('请选择确认或跳过')
    parts = review_notes(folder)
    parts[part] = result
    dump_json(folder / REVIEW, {'schema':'ego-contact-calibration-review-v1',
                               'session_id':folder.name,'parts':parts})
    return result


def frame_path(folder, cam, stamp):
    if cam not in CAMERAS or not re.fullmatch(r'[0-9]{1,20}', stamp):
        raise ValueError('无效的校准帧编号')
    path = (folder / INDEX).parent / cam / f'{stamp}.jpg'
    if not path.is_file() or not path.resolve().is_relative_to(folder.resolve()):
        raise ValueError('找不到所选校准帧')
    return path

