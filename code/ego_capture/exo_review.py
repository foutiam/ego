"""Replay only EXO assets registered to the selected recording."""

from __future__ import annotations

from bisect import bisect_left
import csv
from dataclasses import dataclass
from fractions import Fraction
from functools import lru_cache
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from urllib.parse import urlencode

import av
import cv2

from ego_capture.contact_calibration import move_between_camera_axes, review_camera_axes
from ego_capture.exo_calibration import ROLE, SESSION
from ego_capture.live_exo import ROLES
from visuotactile.pipeline import read_json


_IMPORTED_ASSET = re.compile(r'exo-[0-9a-f]{12}/[0-9]+\.(?:mp4|mov|m4v)', re.I)
_MIRRORED_LIVE = re.compile(r'本次录制 · (exo[1-3])-live\.mp4')
_CLOCK_SCHEMA = 'ego-qr-clock-v1'
_REVIEW_CLOCK = 'recording_computer_monotonic_seconds'


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _asset(folder, relative, pattern):
    if not isinstance(relative, str) or not pattern.fullmatch(relative):
        return None
    path = folder / relative
    return path if path.is_file() and path.resolve() == path else None


def _file_identity(path):
    stat = path.stat()
    return (path, stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=32)
def _video_metadata(path, size, mtime_ns):
    with av.open(str(path)) as container:
        stream = next(iter(container.streams.video), None)
        if stream is None or not stream.time_base:
            raise ValueError('录像没有可读取的画面')
        rate = float(stream.average_rate or 0)
        # Native media time is guaranteed here only when neither timeline needs rebasing.
        media_origin_known = container.start_time == stream.start_time == 0
        interval = 1 / rate if math.isfinite(rate) and rate > 0 else None
        return stream.width, stream.height, interval, media_origin_known


@lru_cache(maxsize=24)
def _imported_index(path, size, mtime_ns):
    source, timeline = [], []
    with path.open(encoding='utf-8-sig') as handle:
        for number, line in enumerate(handle):
            row = json.loads(line)
            pts, session = row['source_pts_s'], row['session_s']
            if (row['frame'] != number or not _number(pts) or not _number(session)
                    or (source and (pts <= source[-1] or session <= timeline[-1]))):
                raise ValueError('EXO 的原片索引或录制时钟无效')
            source.append(pts)
            timeline.append(session)
    if not source:
        raise ValueError('没有生成 EXO 对齐索引')
    return tuple(timeline), tuple(source)


@lru_cache(maxsize=24)
def _live_index(path, size, mtime_ns, time_base, start_ns, expected_frames):
    source, timeline = [], []
    rows = 0
    with path.open(encoding='utf-8-sig', newline='') as handle:
        for number, row in enumerate(csv.DictReader(handle)):
            if int(row['frame']) != number:
                raise ValueError('直连 EXO 视频与接收索引不一致')
            pts = int(row['video_pts']) * time_base
            session = (int(row['host_mono_ns']) - start_ns) / 1e9
            if not (_number(pts) and _number(session)) or (
                    source and (pts <= source[-1] or session < timeline[-1])):
                raise ValueError('直连 EXO 帧索引倒退')
            rows += 1
            if not timeline or session > timeline[-1]:
                source.append(pts)
                timeline.append(session)
    if rows != expected_frames or not source:
        raise ValueError('直连 EXO 接收索引缺帧')
    return tuple(timeline), tuple(source)


def _valid_clock(report, model, name, basis, *, modes):
    return (isinstance(model, dict) and model.get('name') == name and model.get('mode') in modes
            and model.get('source_clock') == basis and bool(report.get('display_id'))
            and model.get('display_id') == report['display_id']
            and all(_number(model.get(key)) for key in ('source_origin_s', 'host_mono_at_origin_s', 'scale'))
            and 0.9899 <= model['scale'] <= 1.0101)


def _sync_report(report, folder):
    return (isinstance(report, dict) and report.get('schema') == _CLOCK_SCHEMA
            and report.get('session_id') == folder.name and report.get('clock') == _REVIEW_CLOCK
            and isinstance(report.get('cameras'), list))


def _exposure_time(pts, model, metadata):
    return (model['host_mono_at_origin_s'] - metadata['start_mono_ns'] / 1e9
            + model['scale'] * (pts - model['source_origin_s']))


@dataclass
class ReviewExo:
    id: str
    label: str
    source: str
    folder: Path
    duration: float
    path: Path | None = None
    sync: str = 'unavailable'
    reason: str | None = None
    index: tuple = ((), ())
    axes: tuple | None = None
    media_clock: dict | None = None
    media_anchors: bool = False

    def __post_init__(self):
        if self.path is None:
            return
        self.file_identity = _file_identity(self.path)
        self.dimensions = _video_metadata(*self.file_identity)
        _, size, mtime = self.file_identity
        key = f'{self.folder.name}/{self.id}/{self.path.relative_to(self.folder)}/{size}/{mtime}'
        self.version = sha256(key.encode()).hexdigest()[:20]

    def url(self, kind):
        query = urlencode(dict(id=self.folder.name, track=self.id, v=self.version))
        return f'/api/exo/review-{kind}?{query}'

    def review_time(self, exposure):
        if self.axes is None:
            return exposure
        raw, optical = self.axes
        return float(move_between_camera_axes(exposure, optical, raw))

    def exposure_time(self, review):
        if self.axes is None:
            return review
        raw, optical = self.axes
        return float(move_between_camera_axes(review, raw, optical))

    def coverage(self):
        timeline, _ = self.index
        if not timeline:
            return None
        start = max(0., self.review_time(timeline[0]))
        end = min(self.duration, self.review_time(timeline[-1]))
        return (start, end) if start <= end else None

    def describe(self):
        result = dict(id=self.id, label=self.label, source=self.source, sync=self.sync, reason=self.reason)
        if self.path is None:
            return result
        width, height, interval, media_origin_known = self.dimensions
        result.update(video_url=self.url('video'), poster_url=self.url('poster'),
                      width=width, height=height, frame_interval_s=interval)
        bounds = self.coverage()
        if bounds is None and self.sync != 'unavailable':
            result.update(sync='unavailable', reason='原片没有覆盖当前录制的时段')
        if bounds is not None:
            result.update(start_s=bounds[0], end_s=bounds[1], frame_url=self.url('frame'))
            if media_origin_known and self.media_clock is not None:
                result['media_clock'] = self.media_clock
            if media_origin_known and self.media_anchors:
                times, pts = self.index
                result['media_anchors'] = tuple(zip(times, pts))
        return result

    def poster(self):
        return _frame_jpeg(*self.file_identity, None)

    def frame(self, review_time):
        bounds = self.coverage()
        if bounds is None or not bounds[0] <= review_time <= bounds[1]:
            return None
        times, pts = self.index
        target = self.exposure_time(review_time)
        position = bisect_left(times, target)
        candidates = range(max(0, position - 1), min(len(times), position + 1))
        nearest = min(candidates, key=lambda i: abs(times[i] - target))
        # Prevent a previous frame from masquerading as coverage across an absent interval.
        allowed = min(.15, max(.03, (self.dimensions[2] or .04) * 1.5))
        return (_frame_jpeg(*self.file_identity, pts[nearest])
                if abs(times[nearest] - target) <= allowed else None)


@lru_cache(maxsize=64)
def _frame_jpeg(path, size, mtime_ns, pts):
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        if pts is not None:
            container.seek(round(pts / float(stream.time_base)), stream=stream, backward=True)
        for frame in container.decode(stream):
            if pts is not None:
                if frame.pts is None:
                    continue
                actual = float(frame.pts * frame.time_base)
                tolerance = max(float(stream.time_base) * .45, .0000002)
                if actual < pts - tolerance:
                    continue
                if actual > pts + tolerance:
                    return None
            width = min(frame.width, 640)
            image = frame.reformat(width=width, height=max(1, round(frame.height * width / frame.width)),
                                   format='bgr24').to_ndarray()
            ok, encoded = cv2.imencode('.jpg', image, (cv2.IMWRITE_JPEG_QUALITY, 82))
            return encoded.tobytes() if ok else None
    return None


def _apply_qr(track, clock, head, metadata, index):
    times, pts = index
    if not all(abs(times[i] - _exposure_time(pts[i], clock, metadata)) <= .001
               for i in (0, len(times) - 1)):
        raise ValueError('EXO 原片索引与二维码时钟不一致')
    if times[-1] < 0 or times[0] > track.duration:
        raise ValueError('EXO 与当前录制没有时间交集')
    if (head is not None and clock['mode'] == 'offset_and_drift'
            and head['display_id'] == clock['display_id']):
        raw, optical = review_camera_axes(track.folder, metadata, head)
        if len(raw) and len(optical):
            track.axes = raw, optical
    if track.axes is None:
        track.sync = 'host_receipt'
        track.reason = ('EXO 有二维码偏移，头戴画面使用电脑接收时间' if clock['mode'] == 'offset_only'
                        else 'EXO 已用二维码对时，头戴画面使用电脑接收时间')
        offset = -_exposure_time(0., clock, metadata) / clock['scale']
        track.media_clock = dict(basis='review_s', offset_s=offset, scale=1 / clock['scale'])
    else:
        track.sync = clock['mode']
        track.reason = None
        offset = ((head['host_mono_at_origin_s'] - clock['host_mono_at_origin_s']
                   - head['scale'] * head['source_origin_s']) / clock['scale'] + clock['source_origin_s'])
        track.media_clock = dict(basis='cam0_device_s', offset_s=offset, scale=head['scale'] / clock['scale'])
    track.index = times, pts


def _head_clock(report):
    return next((model for model in report['cameras']
                 if _valid_clock(report, model, 'Ego CAM0', 'device_timestamp_seconds',
                                 modes=('offset_and_drift',))), None)


def _imported_views(folder, metadata, report):
    if (not isinstance(report, dict) or report.get('session_id') != folder.name
            or report.get('schema') != SESSION or not isinstance(report.get('cameras'), list)):
        return [], set()
    clock_report = report.get('sync')
    valid_report = _sync_report(clock_report, folder)
    head = _head_clock(clock_report) if valid_report else None
    views, mirrored = [], set()
    for camera in report['cameras']:
        if not isinstance(camera, dict):
            continue
        role = camera.get('role')
        if not isinstance(role, str) or not ROLE.fullmatch(role) or any(row.id == role for row in views):
            continue
        video = _asset(folder, camera.get('video'), _IMPORTED_ASSET)
        label = str(camera.get('original_name') or role)
        mirror = _MIRRORED_LIVE.fullmatch(label)
        label = re.sub(r'^(?:本次导入 \d+|外部文件 \d+|本次录制) · ', '', label)
        reason = camera.get('clock_reason') or '没有可用的二维码对时'
        try:
            track = ReviewExo(role, label, 'imported', folder, metadata['duration_s'], video, reason=reason)
        except (OSError, ValueError, av.error.FFmpegError):
            track = ReviewExo(role, label, 'imported', folder, metadata['duration_s'], reason='原片不可读取')
        views.append(track)
        clock = camera.get('clock')
        if track.path is None:
            track.reason = '原片不可读取'
            continue
        if mirror:
            mirrored.add((mirror[1], role))
        if not (valid_report and _valid_clock(clock_report, clock, role, 'video_pts_seconds',
                                             modes=('offset_only', 'offset_and_drift'))
                and clock in clock_report['cameras']):
            continue
        index_pattern = re.compile(re.escape(video.parent.name) + '/' + re.escape(role) + r'-frames\.jsonl')
        index_path = _asset(folder, camera.get('frames'), index_pattern)
        try:
            if index_path is None:
                raise ValueError('缺少 EXO 对齐索引')
            _apply_qr(track, clock, head, metadata, _imported_index(*_file_identity(index_path)))
        except (ValueError, OSError, KeyError, TypeError) as error:
            track.reason = str(error)
    return views, mirrored


def _live_views(folder, metadata, report):
    videos = metadata.get('exo_live') or {}
    valid_report = _sync_report(report, folder)
    head = _head_clock(report) if valid_report else None
    views = []
    for role in ROLES:
        source = videos.get(role)
        if not isinstance(source, dict):
            continue
        name = 'EXO ' + role[-1]
        label = (source.get('source') or {}).get('label') or name
        duration = metadata['duration_s']
        video = _asset(folder, source.get('video'), re.compile(rf'exo-live/{role}-live\.mp4'))
        if source.get('status') != 'saved':
            video = None
        try:
            track = ReviewExo(role, label, 'live', folder, duration, video)
        except (OSError, ValueError, av.error.FFmpegError):
            track = ReviewExo(role, label, 'live', folder, duration)
        views.append(track)
        if track.path is None:
            track.reason = '直连录像不完整或缺失'
            continue
        path = _asset(folder, source.get('timestamps'), re.compile(rf'exo-live/{role}-live\.csv'))
        try:
            if path is None:
                raise ValueError('未保存相机接收时间')
            index = _live_index(*_file_identity(path), float(Fraction(source['video_time_base'])),
                                metadata['start_mono_ns'], source['frames'])
        except (ValueError, OSError, KeyError, TypeError, ZeroDivisionError) as error:
            track.reason = str(error)
            continue
        model = next((item for item in report['cameras']
                      if _valid_clock(report, item, name, 'video_pts_seconds',
                                      modes=('offset_only', 'offset_and_drift'))), None) if valid_report else None
        uses_asset = valid_report and any(item.get('name') == name and item.get('video') == source.get('video')
                                          for item in report.get('outcomes', []))
        if model is not None and uses_asset:
            pts = index[1]
            optical = tuple(_exposure_time(value, model, metadata) for value in pts)
            try:
                _apply_qr(track, model, head, metadata, (optical, pts))
                continue
            except (ValueError, OSError, KeyError):
                pass
        track.sync = 'host_receipt'
        track.reason = '用本机采集时记录的接收时间对齐'
        track.index = index
        track.media_anchors = True
    return views


def views(folder, metadata, imported, live_alignment):
    imported_views, mirrored = _imported_views(folder, metadata, imported)
    live_views = _live_views(folder, metadata, live_alignment)
    for live_id, imported_id in mirrored:
        duplicate = next((track for track in live_views if track.id == live_id), None)
        if duplicate is None:
            continue
        converted = next(track for track in imported_views if track.id == imported_id)
        if converted.coverage() is not None or duplicate.path is None:
            live_views.remove(duplicate)
        else:
            imported_views.remove(converted)
    return live_views + imported_views
