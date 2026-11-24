"""Estimate optical clock mappings while preserving the recorded glove clock."""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
from statistics import median

import av
import cv2
import numpy as np

from visuotactile.pipeline import read_json
from visuotactile.storage import dump_json

REPORT = 'qr-sync.json'
SCAN_SECONDS = 25
SAMPLE_SECONDS = 0.2
BURST_GAP_SECONDS = 2
MIN_DRIFT_SPAN_SECONDS = 10
QR_PATTERN = re.compile(r'EGO1:([0-9a-f]{8}):([0-9]{1,12}):([0-9]{1,3})')


def read_code(detector, image):
    if image is None:
        return None
    height, width = image.shape[:2]
    scale = min(1, 1280 / max(width, height))
    if scale < 1:
        image = cv2.resize(image, (round(width * scale), round(height * scale)),
                           interpolation=cv2.INTER_AREA)
    try:
        found, values, _, _ = detector.detectAndDecodeMulti(image)
        if not found:
            values = (detector.detectAndDecode(image)[0],)
    except cv2.error:
        return None
    valid = {value for value in values if QR_PATTERN.fullmatch(value)}
    if len(valid) != 1:
        return None
    display, clock_ms, rtt_ms = QR_PATTERN.fullmatch(valid.pop()).groups()
    return display, int(clock_ms) / 1000, int(rtt_ms)


def ego_observations(folder, duration, progress):
    rows = [[], []]
    next_time = [-math.inf, -math.inf]
    detector = cv2.QRCodeDetector()
    index = folder / 'derived/frames.jsonl'
    if not index.is_file():
        raise ValueError('请先整理录制，生成保留双目设备时间戳的预览帧')
    progress('扫描 Ego 双目预览帧的首尾时间码…')
    with index.open(encoding='utf-8-sig') as stream:
        for line in stream:
            frame = json.loads(line)
            cam = frame['cam']
            time_s = frame['t_s']
            if cam not in (0, 1) or time_s < next_time[cam]:
                continue
            if SCAN_SECONDS < time_s < duration - SCAN_SECONDS:
                continue
            next_time[cam] = time_s + SAMPLE_SECONDS
            image = frame['image']
            if not re.fullmatch(rf'cam{cam}/[0-9]+[.]jpg', image):
                continue
            filename = folder / 'derived' / image
            if not filename.is_file():
                continue
            pixels = cv2.imdecode(np.fromfile(filename, dtype=np.uint8), cv2.IMREAD_COLOR)
            code = read_code(detector, pixels)
            if code:
                rows[cam].append((frame['device_ns'] / 1e9, *code))
    return rows


def exo_observations(path, progress):
    progress('扫描外视角 ' + path.name + ' 的首尾时间码…')
    detector = cv2.QRCodeDetector()
    observations = []
    with av.open(str(path)) as container:
        if not container.streams.video:
            raise ValueError('外视角文件没有视频流')
        stream = container.streams.video[0]
        if stream.time_base is None:
            raise ValueError('外视角视频缺少 PTS 时间基准')
        base = float(stream.time_base)
        origin = float(stream.start_time * base) if stream.start_time is not None else None
        if origin is None:
            frame = next(container.decode(stream), None)
            if frame is None or frame.pts is None or frame.time_base is None:
                raise ValueError('外视角视频首帧缺少 PTS')
            origin = float(frame.pts * frame.time_base)
        duration = (float(stream.duration) * base if stream.duration is not None else
                    container.duration / av.time_base if container.duration is not None else None)
    starts = [origin]
    if duration and duration > 2 * SCAN_SECONDS:
        starts.append(origin + duration - SCAN_SECONDS)
    for start in starts:
        with av.open(str(path)) as container:
            stream = container.streams.video[0]
            if start > origin:
                container.seek(int(start / base), stream=stream, backward=True)
            next_pts = start
            end = start + (duration if len(starts) == 1 and duration else SCAN_SECONDS)
            for frame in container.decode(stream):
                if frame.pts is None or frame.time_base is None:
                    continue
                pts = float(frame.pts * frame.time_base)
                if pts < next_pts:
                    continue
                if pts > end:
                    break
                next_pts = pts + SAMPLE_SECONDS
                code = read_code(detector, frame.to_ndarray(format='bgr24'))
                if code:
                    observations.append((pts, *code))
    return observations


def fit_clock(rows, name):
    # Group repeated sightings into bursts. One burst can estimate offset only.
    rows = sorted(set(rows))
    if len({clock for _, _, clock, _ in rows}) < 3:
        raise ValueError(f'{name} 识别不足 3 个有效时间码')
    displays = {display for _, display, _, _ in rows}
    if len(displays) != 1:
        raise ValueError(f'{name} 混入不同页面的时间码，请用同一个屏幕、保持页面开启')
    groups = []
    for row in rows:
        if not groups or row[0] - groups[-1][-1][0] > BURST_GAP_SECONDS:
            groups.append([])
        if groups[-1] and row[2] < groups[-1][-1][2] - 0.05:
            raise ValueError(f'{name} 的二维码时间出现倒退')
        groups[-1].append(row)
    groups = [group for group in groups if len({row[2] for row in group}) >= 3]
    if not groups:
        raise ValueError(f'{name} 没有一段连续拍到至少 3 个时间码')
    origin = groups[0][0][0]
    anchors = []
    for group in groups:
        center = median(row[0] for row in group)
        clock = center + median(row[2] - row[0] for row in group)
        anchors.append({'source_s': center, 'host_mono_s': clock, 'frames': len(group)})
    rate = 1.0
    drift = anchors[-1]['source_s'] - anchors[0]['source_s'] >= MIN_DRIFT_SPAN_SECONDS
    if drift:
        rate = ((anchors[-1]['host_mono_s'] - anchors[0]['host_mono_s']) /
                (anchors[-1]['source_s'] - anchors[0]['source_s']))
        if abs(rate - 1) > 0.01:
            raise ValueError(f'{name} 时钟速率差异常，请检查原视频 PTS 和二维码画面')
    used = [row for group in groups for row in group]
    host_origin = median(row[2] - rate * (row[0] - origin) for row in used)
    errors_ms = [abs(row[2] - host_origin - rate * (row[0] - origin)) * 1000 for row in used]
    return {
        'display_id': displays.pop(), 'mode': 'offset_and_drift' if drift else 'offset_only',
        'source_origin_s': origin, 'host_mono_at_origin_s': host_origin, 'scale': rate,
        'observations': len(used), 'anchors': anchors,
        'median_abs_residual_ms': median(errors_ms), 'max_abs_residual_ms': max(errors_ms),
        'max_display_rtt_ms': max(row[3] for row in used),
    }


def analyze(folder, videos=(), progress=lambda _: None, *, publish=True):
    folder = Path(folder)
    meta = read_json(folder / 'metadata.json')
    if meta.get('session_id') != folder.name or meta.get('status') != 'recorded':
        raise ValueError('请选择一条已保存的本机录制')
    start = meta['start_mono_ns'] / 1e9
    duration = meta['duration_s']
    eyes = ego_observations(folder, duration, progress)
    scans = [(f'Ego CAM{cam}', 'device_timestamp_seconds', rows) for cam, rows in enumerate(eyes)]
    scans.extend((path.name, 'video_pts_seconds', exo_observations(path, progress)) for path in videos)
    cameras = []
    displays = set()
    for name, source, rows in scans:
        model = fit_clock(rows, name)
        if any(clock < start - 1 or clock > start + duration + 1 for _, _, clock, _ in rows):
            raise ValueError(f'{name} 的二维码时间超出所选录制，请核对是否同一次采集')
        displays.add(model['display_id'])
        cameras.append({'name': name, 'source_clock': source, **model})
    if len(displays) != 1:
        raise ValueError('Ego 双目和 Exo 须拍摄同一个持续开启的二维码页面')
    signals = folder / 'derived/glove_signals.npz'
    if not signals.is_file():
        raise ValueError('未找到手套接收时间轴，请先完成录制整理')
    with np.load(signals, allow_pickle=False) as data:
        receipt = data['time_s']
    if (not receipt.size or not np.isfinite(receipt).all() or
            (np.diff(receipt) < 0).any() or receipt[0] < 0 or receipt[-1] > duration):
        raise ValueError('手套接收时间轴缺失或无效')
    report = {
        'schema': 'ego-qr-clock-v1', 'session_id': folder.name,
        'clock': 'recording_computer_monotonic_seconds',
        'display_id': displays.pop(),
        'camera_mapping': 'host_mono_s = host_mono_at_origin_s + scale * (source_s - source_origin_s)',
        'glove': {'source': 'serial_host_receipt', 'record_count': int(receipt.size),
                  'host_mono_at_origin_s': start, 'scale': 1.0,
                  'first_session_s': float(receipt[0]), 'last_session_s': float(receipt[-1]),
                  'internal_sample_clock_available': False, 'sensor_to_receipt_offset_ms': None},
        'cameras': cameras, 'stereo_scan_basis': 'derived_frame_pixels_with_original_device_timestamps',
        'clock_fit_validated_by_independent_stimulus': False,
    }
    if publish:
        dump_json(folder / 'derived' / REPORT, report)
    return report


EXO_SUFFIXES = {'.mp4', '.mov', '.m4v'}


def exo_catalog(directory):
    return {path.name: path for path in directory.iterdir()
            if path.suffix.lower() in EXO_SUFFIXES and path.is_file() and
            not path.is_symlink() and path.resolve().parent == directory.resolve()} if directory.is_dir() else {}
