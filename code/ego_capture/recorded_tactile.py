"""Look up recorded raw glove and wrist-local hand samples by session time."""
import csv
from functools import lru_cache
import json
import re
from threading import Lock

import numpy as np

from .contact_calibration import map_touch_time, move_between_camera_axes, raw_touch_time
from visuotactile import skeleton_stream
from visuotactile.protocol import GloveDecoder, decode_emf, decode_pressure

MAX_DELTA_NS = 75_000_000
SKELETON_STAMP = re.compile(rb'"host_mono_ns"\s*:\s*(\d+)')
SKELETON_INDEX_LOCK = Lock()
GLOVE_INDEX_LOCK = Lock()


@lru_cache(maxsize=2)
def _read_index(path, size, mtime_ns):
    with path.open(encoding='utf-8-sig', newline='') as stream:
        rows = np.asarray([(int(row['host_mono_ns']), int(row['offset']), int(row['length']))
                           for row in csv.DictReader(stream)], dtype=np.int64).reshape(-1, 3)
    if len(rows) == 0:
        return rows
    if (np.any(np.diff(rows[:, 0]) < 0) or np.any(rows[:, 1:] < 0)
            or np.any(rows[1:, 1] != rows[:-1, 1] + rows[:-1, 2])):
        raise ValueError('手套接收时间索引无效')
    return rows


def _packet_at(folder, target):
    index_file = folder / 'glove-reads.csv'
    stat = index_file.stat()
    with GLOVE_INDEX_LOCK:
        rows = _read_index(index_file, stat.st_size, stat.st_mtime_ns)
    first, stop = rows[:, 0].searchsorted([target - MAX_DELTA_NS, target + MAX_DELTA_NS])
    window = rows[max(first - 2, 0):stop]
    if first == stop or len(window) == 0:
        return None
    offset = int(window[0, 1])
    length = int(window[-1, 1] + window[-1, 2]) - offset
    if length > 4 * 1024 ** 2:
        raise ValueError('所选时刻的手套数据块过大')
    with (folder / 'glove.bin').open('rb') as stream:
        stream.seek(offset)
        data = stream.read(length)
    decoder = GloveDecoder()
    ends = window[:, 1] + window[:, 2] - offset
    best = None
    for frame, end in decoder.feed(data):
        row = window[ends.searchsorted(end)]
        stamp = int(row[0])
        delta = abs(stamp - target)
        if delta <= MAX_DELTA_NS and (best is None or delta <= best[0]):
            best = delta, stamp, frame
    return (best[1], best[2]) if best is not None else None


def pressure_at(folder, origin, time_s):
    target = origin + round(time_s * 1e9)
    packet = _packet_at(folder, target)
    if packet is None:
        return None
    stamp, frame = packet
    return dict(timeS=(stamp - origin) / 1e9, deltaMs=(stamp - target) / 1e6,
                grid=decode_pressure(frame).tolist())


@lru_cache(maxsize=2)
def _read_skeleton_index(path, size, mtime_ns):
    """Index timestamps and byte offsets, decoding only the selected pose."""
    records = []
    offset = 0
    with path.open('rb') as stream:
        for line in stream:
            field_start = line.rfind(b'"host_mono_ns"')
            stamp = SKELETON_STAMP.match(line, field_start) if field_start >= 0 else None
            if stamp:
                records.append((int(stamp[1]), offset))
            offset += len(line)
    index = np.asarray(records, dtype=np.int64).reshape(-1, 2)
    if len(index) and np.any(np.diff(index[:, 0]) < 0):
        index = index[index[:, 0].argsort(kind='stable')]
    return index


def _skeleton_at(folder, origin, target):
    path = folder / 'openxr_skeleton.jsonl'
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None, '录制未保存手套骨架。'
    with SKELETON_INDEX_LOCK:
        index = _read_skeleton_index(path, stat.st_size, stat.st_mtime_ns)
    if not len(index):
        return None, '录制未保存带时间的手套骨架。'
    right = int(index[:, 0].searchsorted(target))
    candidates = index[max(0, right - 1):right + 1]
    stamp, offset = min(candidates, key=lambda row: abs(int(row[0]) - target))
    if abs(int(stamp) - target) > MAX_DELTA_NS:
        return None, '此时刻附近没有手套骨架帧。'
    with path.open('rb') as stream:
        stream.seek(int(offset))
        pose = json.loads(stream.readline())
    if (pose.get('kind') != skeleton_stream.RECORD_KIND
            or pose.get('coordinate_frame') != skeleton_stream.COORDINATE_FRAME
            or pose.get('position_unit') != skeleton_stream.POSITION_UNIT
            or pose.get('session_id') != folder.name):
        return None, '录制的手套骨架来源或坐标系无法核验。'
    try:
        skeleton_stream.validate_nodes(pose['names'], pose['positions_local'],
                                       pose['orientations_local_xyzw'])
    except (KeyError, TypeError, ValueError):
        return None, '录制的手套骨架帧不完整。'
    pose['timeS'] = (int(stamp) - origin) / 1e9
    pose['deltaMs'] = (int(stamp) - target) / 1e6
    return pose, ''


@lru_cache(maxsize=4)
def _recorded_calibration(path, size, mtime_ns):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def _finger_scores(frame, calibration):
    """Use the saved zero and same touch-site score as the live view."""
    offsets = calibration.get('touch_offsets')
    baseline = calibration.get('touch_baseline')
    if not isinstance(offsets, list) or not isinstance(baseline, list) or len(offsets) != len(baseline):
        return {}
    rest = {offset: count for offset, count in zip(offsets, baseline)
            if isinstance(offset, int) and 0 <= offset < len(frame)
            and isinstance(count, (float, int)) and np.isfinite(count)}
    scores = {}
    for site in calibration.get('touch_sites') or []:
        if site.get('role') != 'touch' or not site.get('finger'):
            continue
        samples = site.get('bytes') or []
        if not samples or any(offset not in rest for offset in samples):
            continue
        value = round(float(np.median([max(0, frame[offset] - rest[offset]) for offset in samples])), 1)
        finger = site['finger']
        scores[finger] = max(scores.get(finger, 0), value)
    return scores


def recorded_hand_at(folder, origin, time_s, *, timing=None, camera_axes=None):
    target = origin + round(time_s * 1e9)
    adjusted = (timing or {}).get('status') == 'applied'
    visual_s = move_between_camera_axes(time_s, *camera_axes) if adjusted and camera_axes is not None else time_s
    tactile_s = raw_touch_time(visual_s, timing) if adjusted else time_s
    tactile_target = origin + round(tactile_s * 1e9)
    sample = dict(grid=None, emf=None, finger_scores={}, pressure_baseline=None,
                  gloveStatus='此时刻附近没有手套数据帧。')
    try:
        packet = _packet_at(folder, tactile_target)
    except FileNotFoundError:
        packet = None
        sample['gloveStatus'] = '录制未保存可读取的手套原始数据。'
    if packet is not None and adjusted:
        lo, hi = timing['taskRangeS']
        raw_s = (packet[0] - origin) / 1e9
        sample_s = map_touch_time(raw_s, timing)
        left, right = move_between_camera_axes(np.asarray([lo, hi]), *camera_axes) if camera_axes is not None else (lo, hi)
        if not (lo <= raw_s < hi and left <= sample_s < right and abs(sample_s - visual_s) <= MAX_DELTA_NS / 1e9):
            packet = None
    if packet is not None:
        stamp, frame = packet
        path = folder / 'calibration.json'
        try:
            stat = path.stat()
            calibration = _recorded_calibration(path, stat.st_size, stat.st_mtime_ns)
        except FileNotFoundError:
            calibration = {}
        raw_s = (stamp - origin) / 1e9
        sample_s = map_touch_time(raw_s, timing) if adjusted else raw_s
        sample.update(timeS=sample_s, rawTimeS=raw_s, timingApplied=adjusted,
                      deltaMs=(sample_s - visual_s) * 1000.0, grid=decode_pressure(frame).tolist(), emf=decode_emf(frame),
                      finger_scores=_finger_scores(frame, calibration),
                      pressure_baseline=(calibration.get('touch_pressure_baseline') or {}).get('grid'),
                      gloveStatus='')
    sample['skeleton_pose'], sample['skeletonStatus'] = _skeleton_at(folder, origin, target)
    return sample
