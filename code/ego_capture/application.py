"""Recording, device calibration, and session processing for the Ego workbench."""
from __future__ import annotations
import csv
import hashlib
import math
import tempfile
import json
import mimetypes
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse

import av
from queue import Empty, SimpleQueue
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_CODE = str(ROOT / 'code')
if _CODE not in sys.path:
    sys.path.insert(0, _CODE)
_SCRIPTS = str(ROOT / 'scripts')
if _SCRIPTS not in sys.path:
    sys.path.append(_SCRIPTS)

from visuotactile.protocol import VisioDecoder                            # noqa: E402
from visuotactile.pipeline import (process_session, build_dataset,        # noqa: E402
                                   delete_dataset, delete_session, read_json,
                                   read_jsonl, validate_annotations)
from visuotactile.storage import session_path, dump_json                  # noqa: E402

from ego_capture import station_paths
from ego_capture.station_devices import StationDevices                # noqa: E402

WEB = ROOT / 'web/ego-workbench/capture'
CONFIG = ROOT / 'code/ego_capture/capture_config.json'
ROUTE = '/ego-capture'
QR_EXO = ROOT / 'data/ego/qr-exo'
QR_JOB = '二维码时钟对齐'


def sha256_file(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


_ORIGIN_LOG_LOCK = threading.Lock()


_ORIGIN_LOGGED_DEVIATIONS = set()


def _log_origin_rejection(diag):
    """Record a non-canonical Origin. Rejections are always written (rare and
    security-relevant); an accepted loopback deviation is written once per
    distinct (path, origin) per server run. The token is never logged.

    Purpose (2026-09-17): all button POSTs failed with a bare 'Origin rejected'
    while GET polling worked, so the actual browser-side Origin value had to
    become visible before the mismatch could be fixed. That diagnosis is done
    (do_POST now documents the portless loopback deviation), but appending one
    line per button POST kept growing
    outputs/capture-station/logs/origin-rejections.jsonl (363 lines / 86 KB by
    2026-09-21) with no recording running. Streaming must not accumulate
    records, so only the first occurrence of each distinct deviation is kept;
    rejections are never deduplicated away."""
    accepted = diag.get('verdict') != 'rejected'
    try:
        log_dir = ROOT / 'outputs/capture-station/logs'
        log_dir.mkdir(parents=True, exist_ok=True)
        with _ORIGIN_LOG_LOCK:
            if accepted:
                key = (diag.get('path'), diag.get('actual_origin'))
                if key in _ORIGIN_LOGGED_DEVIATIONS:
                    return
                _ORIGIN_LOGGED_DEVIATIONS.add(key)
            with (log_dir / 'origin-rejections.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps(diag, ensure_ascii=False) + '\n')
    except OSError:
        pass
    print(('ORIGIN_REJECTED ' if not accepted else 'ORIGIN_LOOPBACK_ACCEPTED ')
          + json.dumps(diag, ensure_ascii=False), flush=True)


class Application:
    def __init__(self, config_path=CONFIG, *, port=8793, standalone=False, connect_devices=True):
        self.config_path = Path(config_path).resolve()
        self.config = read_json(self.config_path)
        self.active_path_values = station_paths.values(self.config)
        self.config['port'] = port
        base = self.config_base = self.config_path.parent if standalone else ROOT
        self.standalone = standalone
        self.connect_devices = connect_devices
        self.qr_phone_url = None
        self.root = (base / self.config['output_root']).resolve()
        self.inbox = (base / self.config.get('import_root', str(self.root / 'imports') if standalone
                                             else 'data/ego/imports')).resolve()
        self.exo_root = (base / self.config.get('exo_video_root', str(self.root / 'exo-inbox') if standalone
                                                 else str(QR_EXO))).resolve()
        self.exo_profiles = (base / self.config.get('exo_profile_root', str(self.root / 'exo-calibrations'))).resolve()
        self.export_root = (base / self.config.get('export_root', str(self.root / 'exports'))).resolve()
        self.exo_video_files = self._configured_files(base, 'exo_video_files')
        self.exo_profile_files = self._configured_files(base, 'exo_profile_files')
        for directory in (self.inbox, self.exo_root, self.exo_profiles, self.export_root):
            directory.mkdir(parents=True, exist_ok=True)
        self.config['output_root'] = str(self.root)
        replay = self.config.get('replay_source')
        self.config['replay_source'] = str((base / replay).resolve()) if replay else ''
        runtime = self.config.get('qnbot_runtime_python')
        if runtime:
            self.config['qnbot_runtime_python'] = str((base / runtime).resolve())
        (self.root / 'sessions').mkdir(parents=True, exist_ok=True)

        self.token = secrets.token_urlsafe(24)
        self.job = None
        self.lock = threading.Lock()
        self.depth = None           # DepthRunner, created on first depth API call
        self.tracker = None         # DepthTracker, created on first track API call
        self.event_jobs = None
        self.git_commit = self._git_commit()
        self.pending_recording_qc = SimpleQueue()
        self.intake_exo_jobs = {}
        self.devices = StationDevices(self.config)
        self.devices.on_recording_saved = self.pending_recording_qc.put
        self.queue_interrupted_alignment()
        self.queue_interrupted_exo()
        if connect_devices:
            self.devices.launch()

    def path_settings(self):
        saved = station_paths.values(read_json(self.config_path))
        effective = dict(output_root=str(self.root), import_root=str(self.inbox),
                         export_root=str(self.export_root), exo_video_root=str(self.exo_root),
                         exo_profile_root=str(self.exo_profiles),
                         qnbot_runtime_python=self.config.get('qnbot_runtime_python') or '',
                         replay_source=self.config.get('replay_source') or '')
        return dict(values=saved, effective=effective, base=str(self.config_base),
                    restartRequired=saved != self.active_path_values)

    def save_paths(self, updates):
        with self.lock:
            current = read_json(self.config_path)
            updated = station_paths.update(current, updates, self.config_base)
            if updated != current:
                dump_json(self.config_path, updated)
        return self.path_settings()

    def segments(self):
        from workflow_segments import Segments
        if self.standalone:
            return Segments(registry=self.root / 'sessions', capture_root=self.root)
        return Segments(capture_root=self.root)


    @staticmethod
    def _git_commit():
        try:
            out = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                                 cwd=str(ROOT), capture_output=True, text=True,
                                 timeout=5, check=True)
            return out.stdout.strip()
        except Exception:
            return 'unknown'

    def depth_module(self):
        from ego_capture import depth_model
        if self.depth is None:
            self.depth = depth_model.DepthRunner()
        return depth_model

    def depth_runner(self):
        self.depth_module()
        return self.depth

    def depth_tracker(self):
        """Lazily create the Z(t) tracker bound to the newest CAM0 frame and
        the threshold-only glove contact candidate (not a ground truth)."""
        dm = self.depth_module()
        if self.tracker is None:
            self.tracker = dm.DepthTracker(
                self.depth,
                frame_source=lambda: self.devices.latest[0],
                contact_source=lambda: self.devices.state['glove'].get('contact_candidate'))
        return self.tracker

    def open_qnbot_calibration(self):
        raise ValueError('官方 QnBot 串口标定已停用：Console 会连接 COM3 telemetry，真实手套数据应走本采集站 COM8 external bridge')

    def handpose_module(self):
        """The camera-frame hand-pose pipeline (transform convention + three views)."""
        from ego_capture import hand_pose_pipeline
        return hand_pose_pipeline

    def fiducial_module(self):
        """The AprilTag marker registration path (printed marker -> T_cam<-wrist)."""
        from ego_capture import fiducial_registration
        return fiducial_registration

    def marker_layout_module(self):
        """The declared marker table (idx -> position on the hand) plus its registration path."""
        from ego_capture import marker_layout
        return marker_layout

    @staticmethod
    def marker_camera_from_record(name, record, image_shape, source):
        """Build the marker solver's camera from a device-reported intrinsics record.

        The image being solved is often smaller than the calibrated frame (960x540 derived frames
        against 1920x1080 raw ones), so the scale is applied through the device's own
        ``from_intrinsics``: the intrinsics are never used unscaled and the model is never
        silently swapped for a pinhole. Returns ``(camera, applied_scale)`` where the scale is
        relative to the calibrated frame, so the report can state what was applied instead of
        re-deriving a scale from the already-scaled camera.
        """
        from ego_capture import marker_layout
        from ego_capture import hand_openxr_tracking as tracking
        if not record or not record.get('K'):
            raise ValueError(f'{name}: no complete device-reported K in {source}')
        camera = tracking.FisheyeCamera.from_intrinsics(name, record, scale=1.0,
                                                        evidence='device_reported')
        scale = marker_layout.scale_for_image(camera, image_shape)
        if abs(scale - 1.0) > 1e-9:
            camera = tracking.FisheyeCamera.from_intrinsics(name, record, scale=scale,
                                                            evidence='device_reported')
        return camera, scale

    def marker_registration(self, data):
        """Register the hand in the visual frame from the declared marker table.

        One image is used: an uploaded frame (``image_b64``), one stored frame of a selected
        session (``session_id`` + optional ``t_s``), or the newest live preview frame. The camera
        comes from the same session's device-reported intrinsics, or from the live device report.
        No geometry is re-derived here: the module owns the maths.
        """
        module = self.marker_layout_module()
        layout = module.load_layout(self.root)
        session_id = str(data.get('session_id') or '')
        if session_id and not re.fullmatch(r'[A-Za-z0-9_.-]{1,90}', session_id):
            raise ValueError('Invalid session identifier')
        source = 'live_preview'
        camera_note = {}
        if data.get('image_b64'):
            from ego_capture import depth_model
            image = depth_model.decode_image_b64(data['image_b64'])
            source = 'uploaded_frame'
            camera, applied_scale = self.marker_camera_from_record(
                'cam0', self._live_intrinsics_cam0(), image.shape, 'device report')
        elif session_id:
            path = session_path(self.root, session_id)
            frames = read_jsonl(path / 'derived/paired_frames.jsonl')
            if not frames:
                raise ValueError('该会话尚未整理（QC）或没有配对帧：请先运行「整理所选录制（QC）」')
            wanted = data.get('t_s')
            frame = (min(frames, key=lambda f: abs(float(f['t_s']) - float(wanted)))
                     if wanted is not None else frames[0])
            image = module.decode_jpeg_bytes((path / 'derived' / frame['image']).read_bytes())
            source = 'session_frame'
            reported = read_json(path / 'calibration.json').get('reported') or {}
            camera, applied_scale = self.marker_camera_from_record(
                'cam0', reported.get('/ego/camera/0/intrinsics') or {}, image.shape,
                'session calibration.json')
            camera_note['session_id'] = session_id
            camera_note['frame'] = {'t_s': frame['t_s'], 'image': frame['image'],
                                    'stereo_delta_ms': frame.get('stereo_delta_ms')}
        else:
            preview = self.devices.previews[0]
            if not preview:
                raise ValueError('还没有实时画面：请连接相机，或选择一条已整理的会话/上传一帧')
            image = module.decode_jpeg_bytes(preview)
            camera, applied_scale = self.marker_camera_from_record(
                'cam0', self._live_intrinsics_cam0(), image.shape, 'device report')
        camera_note.update({'name': camera.name, 'width': camera.width, 'height': camera.height,
                            'fx': camera.fx, 'fy': camera.fy,
                            'distortion_model': camera.distortion_model,
                            'image_shape': [int(image.shape[1]), int(image.shape[0])],
                            'applied_scale': applied_scale,
                            'scale_note': ('the device-reported intrinsics scaled by this factor '
                                           'to the processed image; width/height above are the '
                                           'scaled ones'),
                            'evidence': 'device_reported_not_independently_measured'})
        report = module.registration_from_image(image, camera, layout, camera_note=camera_note)
        report.update({'source_image': source,
                       'layout': module.layout_summary(layout),
                       'layout_from_disk': bool(layout.get('from_disk')),
                       'recorded_at_unix': time.time()})
        out_path = self.root / 'calibration' / 'marker-registration.json'
        out_path.parent.mkdir(parents=True, exist_ok=True)
        dump_json(out_path, report)
        report['record_path'] = str(out_path)
        return report

    def _live_intrinsics_cam0(self):
        """The first device-reported per-eye intrinsics message, as the cam0 candidate.

        Stream order is all the device gives: the physical eye behind it stays unverified until the
        operator's own left/right cover check, which the report says.
        """
        from ego_capture import camera_calibration
        reports = camera_calibration.stream_reports(self.devices.calibration or {})
        for topic, row in reports.items():
            return dict(row, topic=topic,
                        geometric_kind='cam0_candidate_by_stream_order'
                                       '_physical_eye_not_verified')
        raise ValueError('尚未收到相机自报内参：请先连接相机')

    def handpose_report_path(self, report):
        """Resolve a report argument to a path under the code root, refusing escapes."""
        if not report:
            return None
        candidate = Path(report)
        if not candidate.is_absolute():
            candidate = (ROOT / candidate)
        resolved = candidate.resolve()
        if ROOT.resolve() not in resolved.parents and resolved != ROOT.resolve():
            raise ValueError('report must live under the code root')
        if resolved.suffix != '.json':
            raise ValueError('report must be a .json tracking report')
        return resolved

    def handpose_default_report(self):
        """The newest *recorded* OpenXR tracking report, so the panel works without a path.

        A synthetic report is never the default while a real recorded one exists: otherwise the
        panel would show a self-consistent synthetic PASS as if it were a session result. Among
        reports of the same kind the newest ``recorded_at_unix`` (falling back to mtime) wins.
        """
        reports = list((self.root / 'reports').glob('*-openxr-tracking.json'))
        if not reports:
            return None

        def rank(path):
            try:
                report = read_json(path)
            except (ValueError, OSError):
                report = {}
            real = str(report.get('evidence') or '') != 'synthetic'
            recorded = report.get('recorded_at_unix') or path.stat().st_mtime
            return (real, float(recorded))
        return max(reports, key=rank)

    # ---- metrics (A/B/C tiers, honest sources) ---------------------------
    def metrics_report(self):
        metrics_path = ROOT / 'outputs/iebins-repro/station_metrics.json'
        tier_a = None
        if metrics_path.exists():
            try:
                tier_a = json.loads(metrics_path.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                tier_a = None
        snap = self.devices.snapshot()
        cam, glove = snap.get('camera', {}), snap.get('glove', {})
        rec = snap.get('recording') or {}
        per_dev = rec.get('per_device') or {}
        sessions = self.list_sessions()
        qc_done = sum(1 for s in sessions if s.get('processed'))
        reviewed = sum(1 for s in sessions if s.get('reviewed'))

        def dev_stats(state_dev, session_dev, kind):
            return {'frames_cumulative_since_connect': state_dev.get('frames'),
                    'session_frames': (session_dev or {}).get('frames'),
                    'rate_hz_cumulative': state_dev.get('rate_hz'),
                    'session_mean_rate_hz': (session_dev or {}).get('mean_rate_hz'),
                    'invalid_or_partial_cumulative': state_dev.get('invalid_or_partial')}
        tier_b = {
            'label': 'B 级：采集站实时运行指标（自测，非科学结论）',
            'counting_note': rec.get('counting_note') or
                             'frames/bytes 为自串口连接起的累计值；录制中时 per_device 为本次录制增量。',
            'camera': dev_stats(cam, per_dev.get('camera'), 'camera'),
            'glove': dev_stats(glove, per_dev.get('glove'), 'glove'),
            'recording': rec.get('session_id'),
            'disk_free_bytes': snap.get('disk_free_bytes'),
            'sessions_total': len(sessions), 'sessions_qc_done': qc_done,
            'sessions_reviewed': reviewed,
            'note': '仅反映本站设备链路与数据整理状态；不作为任何深度/触觉精度证据。',
        }
        tier_c = {
            'status': 'not_available',
            'note': ('C 级（合成/估计指标）不存在：本站不生成任何合成或估计的指标，'
                     '避免无证据数字。透明物体深度精度的科学评测属于集群正式训练后的评测协议。'),
        }
        return {'A': tier_a, 'B': tier_b, 'C': tier_c, 'git_commit': self.git_commit}

    # ---- sessions -------------------------------------------------------
    def closed_session_path(self, session_id):
        folder = session_path(self.root, session_id)
        if read_json(folder / 'metadata.json').get('status') == 'recording':
            raise ValueError('请先停止并保存录制')
        return folder

    def tactile_events(self):
        from retarget_review_events import EventJobs

        with self.lock:
            if self.event_jobs is None:
                self.event_jobs = EventJobs(self.closed_session_path, base=self.root / 'event-reviews')
            return self.event_jobs

    def review_frame_path(self, session_id, relative):
        folder = session_path(self.root, session_id)
        image = folder / 'derived' / relative
        if image.is_file():
            return image
        # Older station data can share read-only previews with its migrated archive.
        archive = session_path(ROOT / 'data/ego/raw', session_id)
        live_meta = read_json(folder / 'metadata.json')
        old_meta = read_json(archive / 'metadata.json')
        keys = ('session_id', 'start_host_ns', 'start_mono_ns', 'raw_bytes')
        if any(live_meta.get(key) is None or live_meta.get(key) != old_meta.get(key) for key in keys):
            raise ValueError('找不到所选录制的复查图像')
        return archive / 'derived' / relative

    def list_sessions(self):
        rows = []
        for p in sorted((self.root / 'sessions').glob('*/metadata.json'), reverse=True):
            try:
                x = read_json(p)
                q = p.parent / 'derived/quality.json'
                x['processed'] = q.exists()
                x['reviewed'] = read_json(p.parent / 'annotations.json').get('reviewed', False)
                if q.exists():
                    x['quality'] = read_json(q)
                auto = self.automatic_alignment(p.parent)
                if auto:
                    x['qr_alignment'] = {key: auto[key] for key in
                                         ('status', 'drift_aligned_cameras', 'expected_cameras', 'outcomes', 'glove')}
                rows.append(x)
            except (ValueError, OSError):
                continue
        return rows

    def import_source(self, session_id):
        from workflow_segments import identifier

        session_id = identifier(session_id)
        inbox = self.inbox
        if inbox.resolve() != inbox:
            raise ValueError('录制导入目录不可被重定向')
        source = inbox / session_id
        if source.resolve() != source or not source.is_dir() or source.is_symlink():
            raise ValueError('找不到待导入的会话文件夹')
        if (self.root / 'sessions' / session_id).exists() or (ROOT / 'data/ego/raw/sessions' / session_id).exists():
            raise ValueError('目录已有此录制编号')
        required = ('metadata.json', 'calibration.json', 'annotations.json', 'channels.json',
                    'camera.bin', 'glove.bin', 'camera-reads.csv', 'glove-reads.csv')
        optional = ('camera.mcap', 'device.json', 'events.jsonl',
                    'guided-capture.json', 'hand_pose.jsonl', 'openxr_skeleton.jsonl', 'manual-events.json',
                    'segments.json', 'contact-calibration-review.json')
        files = []
        for name in required + optional:
            file = source / name
            if not file.exists() and name in optional:
                continue
            if not file.is_file() or file.resolve() != file or file.is_symlink():
                raise ValueError(f'缺少或无法读取会话文件 {name}')
            if name in required and file.stat().st_size == 0:
                raise ValueError(f'会话文件为空 {name}')
            files.append(file)
        meta = read_json(source / 'metadata.json')
        if not isinstance(meta, dict):
            raise ValueError('录制元数据格式无效')
        duration = meta.get('duration_s')
        if (meta.get('session_id') != session_id or meta.get('format_version') != 1
                or meta.get('status') not in ('saved', 'recorded', 'complete', 'completed', 'finished', 'closed')
                or type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0
                or any(type(meta.get(key)) is not int or meta[key] <= 0 for key in ('start_host_ns', 'start_mono_ns'))):
            raise ValueError('会话身份、保存状态或时钟记录不完整')
        for kind in ('camera', 'glove'):
            with (source / f'{kind}-reads.csv').open(encoding='utf-8-sig', newline='') as handle:
                rows = csv.DictReader(handle)
                if (not {'host_ns', 'host_mono_ns', 'elapsed_s', 'offset', 'length'}.issubset(rows.fieldnames or [])
                        or not next(rows, None)):
                    raise ValueError(f'{kind} 缺少接收时间索引')
        raw_size = sum((source / f'{kind}.bin').stat().st_size for kind in ('camera', 'glove'))
        if raw_size != meta.get('raw_bytes'):
            raise ValueError('原始流长度与会话记录不符')
        annotation = read_json(source / 'annotations.json')
        if not isinstance(annotation, dict):
            raise ValueError('会话标注格式错误')
        if annotation.get('reviewed') is True:
            validate_annotations(annotation, duration)
        if not isinstance(read_json(source / 'calibration.json'), dict):
            raise ValueError('录制标定格式错误')
        segment_file = source / 'segments.json'
        if segment_file.is_file():
            declared = read_json(segment_file)
            if (not isinstance(declared,dict) or declared.get('schema') != 'ego-workflow-segments-v1'
                    or declared.get('recordingId') != session_id or not isinstance(declared.get('segments'),list)):
                raise ValueError('片段索引与会话身份不一致')
            names = set()
            for row in declared['segments']:
                if not isinstance(row,dict):
                    raise ValueError('片段索引格式错误')
                name = identifier(row.get('id'))
                start,end = row.get('start_s'),row.get('end_s')
                if (name in names or row.get('recorded_status') != 'recorded'
                        or not all(type(t) in (int,float) and math.isfinite(t) for t in (start,end))
                        or not 0 <= start < end <= duration + .001):
                    raise ValueError('片段重名或区间超出录制')
                names.add(name)
        from ego_capture.live_exo import ROLES
        live_exo = meta.get('exo_live', {})
        if not isinstance(live_exo, dict) or any(role not in ROLES for role in live_exo):
            raise ValueError('会话的直连 EXO 来源记录无效')
        for role, record in live_exo.items():
            if not isinstance(record, dict) or record.get('status') not in ('saved', 'incomplete'):
                raise ValueError('会话的直连 EXO 保存状态无效')
            for key, suffix in (('video', '.mp4'), ('timestamps', '.csv')):
                relative = record.get(key)
                if relative is None and record['status'] == 'incomplete':
                    continue
                if relative != f'exo-live/{role}-live{suffix}':
                    raise ValueError('会话的直连 EXO 文件名无效')
                file = source / relative
                if not file.is_file() or file.resolve() != file or file.is_symlink():
                    raise ValueError(f'缺少或无法读取会话文件 {relative}')
                files.append(file)
        from ego_capture.exo_calibration import session_assets
        try:
            files.extend(session_assets(source))
        except (KeyError, TypeError) as error:
            raise ValueError('录制包含不完整的 EXO 处理记录') from error
        return meta, files

    def import_catalog(self):
        inbox = self.inbox
        rows = []
        if inbox.resolve() != inbox:
            raise ValueError('录制导入目录不可被重定向')
        for folder in sorted(inbox.iterdir()) if inbox.is_dir() else ():
            if not folder.is_dir():
                continue
            try:
                meta, _ = self.import_source(folder.name)
                rows.append(dict(id=folder.name, object=meta.get('object_id'), duration=meta['duration_s']))
            except (OSError, ValueError, KeyError, TypeError) as error:
                rows.append(dict(id=folder.name, error=str(error)))
        return dict(directory=str(inbox), sessions=rows)

    def import_recording(self, session_id):
        def copy(progress):
            _, files = self.import_source(session_id)
            size = sum(file.stat().st_size for file in files)
            sessions = (self.root / 'sessions').resolve()
            if shutil.disk_usage(sessions).free - size < self.config['minimum_free_bytes']:
                raise ValueError('可用空间不足以保存本次导入')
            with tempfile.TemporaryDirectory(prefix='.import-', dir=self.root) as temporary:
                staging = Path(temporary) / session_id
                staging.mkdir()
                for file in files:
                    progress(f'正在复制 {file.name}')
                    before = file.stat()
                    target = staging / file.relative_to(self.inbox / session_id)
                    target.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copyfile(file, target)
                    after = file.stat()
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or target.stat().st_size != before.st_size:
                        raise ValueError('导入时原始文件已变化，请确认复制完成后重试')
                staging.rename(sessions / session_id)
            self.pending_recording_qc.put(session_id)
            return dict(session_id=session_id)

        self.import_source(session_id)
        return self.run_job('导入录制', copy, session_id=session_id)

    def setup_catalog(self):
        rows = []
        for file in sorted((self.root / 'sessions').glob('*/calibration.json'), reverse=True):
            try:
                rows.append(self.recorded_setup(file.parent.name))
            except (OSError, ValueError, TypeError):
                continue
        return rows

    def recorded_setup(self, session_id):
        folder = self.closed_session_path(session_id)
        file = folder / 'calibration.json'
        raw = file.read_bytes()
        setup = json.loads(raw.decode('utf-8-sig'))
        if not isinstance(setup, dict):
            raise ValueError('录制标定格式错误')
        return dict(session_id=session_id, sha256=hashlib.sha256(raw).hexdigest(),
                    camera_reported=bool(setup.get('reported')),
                    touch_baseline=setup.get('touch_baseline') is not None,
                    stereo_independently_verified=setup.get('independently_verified') is True)

    def review_setup(self, session_id, source_id=None):
        folder = self.closed_session_path(session_id)
        path = folder / 'review-setup.json'
        if source_id is not None:
            selected = self.recorded_setup(source_id) if source_id != session_id else None
            with self.lock:
                dump_json(path, dict(session_id=session_id, comparison_setup=selected))
        saved = read_json(path) if path.is_file() else {}
        if saved and saved.get('session_id') != session_id:
            raise ValueError('会话标定选择的录制身份不一致')
        original = self.recorded_setup(session_id) if (folder / 'calibration.json').is_file() else None
        selected = saved.get('comparison_setup')
        available = True
        if selected:
            try:
                available = self.recorded_setup(selected['session_id']) == selected
            except (OSError, ValueError):
                available = False
        return dict(original=original, comparison=selected, comparison_available=available)

    @staticmethod
    def automatic_alignment(session):
        from ego_capture.qr_sync import AUTO_REPORT
        path = session / 'derived' / AUTO_REPORT
        if not path.is_file():
            return None
        try:
            report = read_json(path)
        except (OSError, ValueError):
            return None
        return (report if isinstance(report, dict) and report.get('schema') == 'ego-qr-clock-v1'
                and report.get('session_id') == session.name else None)

    def queue_interrupted_alignment(self):
        for metadata in (self.root / 'sessions').glob('*/metadata.json'):
            try:
                data = read_json(metadata)
                if (data.get('status') == 'recorded' and data.get('auto_qr_sync')
                        and self.automatic_alignment(metadata.parent) is None):
                    self.pending_recording_qc.put(metadata.parent.name)
            except (OSError, ValueError):
                continue

    def intake_path(self, folder):
        return self.root / 'intake' / f'{folder.name}.json'

    def intake(self, folder):
        path = self.intake_path(folder)
        return read_json(path) if path.is_file() else {}

    @staticmethod
    def intake_versions(selection, checks):
        from ego_capture.contact_calibration import PARTS

        return dict(exo=selection.get('selection_saved_at_ns', 0),
                    **{part: checks.get(part, {}).get('saved_at_ns', 0) for part in PARTS})

    def intake_status(self, folder):
        from ego_capture.contact_calibration import PARTS, review_notes

        meta = read_json(folder / 'metadata.json')
        contact = bool(meta.get('contact_calibration'))
        checks = review_notes(folder) if contact else {}
        selection = self.intake(folder)
        exo_report = self.current_exo_report(folder, selection)
        cameras = exo_report.get('cameras', []) if exo_report else []
        exo_results = [dict(role=camera['role'],
                            mode='passed' if camera['validation']['status'] == 'passed' else 'time_only',
                            clock_mode=camera['clock']['mode'],
                            residual_ms=camera['clock']['median_abs_residual_ms'],
                            holdout_rms_px=camera['validation'].get('holdout_rms_px'))
                       for camera in cameras if camera.get('clock')]
        quality = folder / 'derived/quality.json'
        tactile_status = None
        needs_reprocess = False
        if quality.is_file():
            versions = self.intake_versions(selection, checks)
            published = read_json(quality).get('intake_versions', {})
            needs_reprocess = any(revision != published.get(name, 0) for name, revision in versions.items())
            if contact and not needs_reprocess:
                from ego_capture.contact_calibration import timing_for_recording
                tactile_status = timing_for_recording(folder, meta)[0]['status']
        exo_job = self.intake_exo_jobs.get(folder.name, {})
        return dict(session_id=folder.name, contact=contact, needs_reprocess=needs_reprocess,
                    exo_pending=exo_job.get('status') in ('queued', 'running'),
                    checks={part:checks.get(part, {}).get('status') for part in PARTS} if contact else {},
                    cameras=selection.get('cameras', []), square_mm=selection.get('square_mm', 25),
                    aligned_exo=bool(exo_results), exo_results=exo_results, tactile_status=tactile_status,
                    needs_review=bool(meta.get('capture_intake_required') and contact and any(
                        checks.get(part, {}).get('status') not in ('pair_confirmed', 'skipped') for part in PARTS)))

    def add_intake_files(self, folder, field, paths):
        if field not in station_paths.MULTI:
            raise ValueError('请选择 EXO 视频或内参档案')
        with self.lock:
            selection = self.intake(folder)
            submitted = list(dict.fromkeys([str(path) for path in paths]))
            incoming = station_paths.update({}, {field:submitted}, self.config_base)
            known = selection.get(field, [])
            selection[field] = list(dict.fromkeys([*known, *incoming.get(field, [])]))
            path = self.intake_path(folder)
            path.parent.mkdir(parents=True, exist_ok=True)
            dump_json(path, selection)
        return self.intake_status(folder)

    def save_intake_choices(self, folder, cameras, square_mm):
        from ego_capture.exo_calibration import checked_setup

        if not isinstance(cameras, list) or type(square_mm) not in (int, float) or not 15 <= square_mm <= 40:
            raise ValueError('请核对 EXO 选择和标定板格长')
        with self.lock:
            if (self.job or {}).get('status') == 'running':
                raise ValueError('处理任务运行中，请在完成后保存选择')
            checked = list(checked_setup(cameras, self.exo_videos(folder.name), self.exo_intrinsics(folder.name))) if cameras else []
            selection = self.intake(folder)
            if (checked, square_mm) != (selection.get('cameras', []), selection.get('square_mm', 25)):
                selection.pop('exo_auto', None)
                self.intake_exo_jobs.pop(folder.name, None)
                selection.update(cameras=checked, square_mm=square_mm, selection_saved_at_ns=time.time_ns())
            path = self.intake_path(folder)
            path.parent.mkdir(parents=True, exist_ok=True)
            dump_json(path, selection)
        return self.intake_status(folder)

    @staticmethod
    def current_exo_report(folder, selection):
        path = folder / 'derived/exo-session.json'
        try:
            report = read_json(path)
        except FileNotFoundError:
            return None
        if selection.get('exo_auto') and report.get('selection_version') != selection['selection_saved_at_ns']:
            return None
        return report

    def intake_exo_state(self, folder):
        from ego_capture import exo_station

        selection = self.intake(folder)
        with self.lock:
            job = dict(self.intake_exo_jobs.get(folder.name, {}))
        report = self.current_exo_report(folder, selection) if job.get('status') not in ('queued', 'running') else None
        if report and not job:
            job = dict(status='complete', phase='扫描完成', progress=100)
        catalog = self.exo_videos(folder.name, selection)
        return dict(session_id=folder.name, job=job or None, report=report,
                    profiles=exo_station.state(self)['profiles'],
                    videos=[dict(role=row['role'],
                                 label=catalog[row['video']].name if row['video'] in catalog else row['video'],
                                 profile=row.get('profile') or '') for row in selection.get('cameras', [])])

    def update_intake_exo(self, folder, action, data, paths=()):
        from ego_capture import exo_station

        square_mm = exo_station.square_mm(self)
        with self.lock:
            if self.intake_exo_jobs.get(folder.name, {}).get('status') == 'running':
                raise ValueError('这段录像正在扫描，完成后可调整选择。')
            selection = self.intake(folder)
            old_rows = selection.get('cameras', [])
            rows = [dict(role=row['role'], video=row['video'], auto=True, profile=row.get('profile'))
                    for row in old_rows]
            if action == 'import':
                selected = list(dict.fromkeys(str(Path(path).resolve()) for path in paths))
                validation = station_paths.update({}, {'exo_video_files': selected}, self.config_base)
                known = selection.get('exo_video_files', [])
                selection['exo_video_files'] = list(dict.fromkeys([*known, *validation.get('exo_video_files', [])]))
                catalog = self.exo_videos(folder.name, selection)
                labels = {file: name for name, file in catalog.items()}
                used_roles = {row['role'] for row in rows}
                free_roles = iter(f'exo-{i}' for i in range(1, 9) if f'exo-{i}' not in used_roles)
                used_videos = {catalog.get(row['video']) for row in rows}
                for path in map(Path, selected):
                    if path in used_videos:
                        continue
                    role = next(free_roles, None)
                    if role is None:
                        raise ValueError('每条录制最多添加 8 路 EXO。')
                    rows.append(dict(role=role, video=labels[path], auto=True, profile=None))
            elif action in ('profile', 'remove'):
                row = next((item for item in rows if item['role'] == data.get('role')), None)
                if row is None:
                    raise ValueError('找不到所选录像。')
                if action == 'remove':
                    rows.remove(row)
                else:
                    profile = data.get('profile')
                    if not isinstance(profile, str) or (profile and profile not in self.exo_intrinsics()):
                        raise ValueError('请选择 00 中已添加的相机内参。')
                    row['profile'] = profile or None
            elif action != 'retry':
                raise ValueError('未知的 EXO 操作。')

            changed = rows != old_rows or square_mm != selection.get('square_mm', 25)
            if changed:
                selection.update(cameras=rows, square_mm=square_mm, exo_auto=True,
                                 selection_saved_at_ns=time.time_ns())
                path = self.intake_path(folder)
                path.parent.mkdir(parents=True, exist_ok=True)
                dump_json(path, selection)
            if changed or (action == 'retry' and rows):
                self.intake_exo_jobs[folder.name] = dict(status='queued', phase='等待开始', progress=0)

        self.dispatch_intake_exo()
        return self.intake_exo_state(folder)

    def queue_interrupted_exo(self):
        for path in (self.root / 'intake').glob('*.json'):
            try:
                folder = self.closed_session_path(path.stem)
                selection = read_json(path)
                pending = selection.get('cameras') or (folder / 'derived/exo-session.json').is_file()
                if selection.get('exo_auto') and pending and not self.current_exo_report(folder, selection):
                    self.intake_exo_jobs[folder.name] = dict(status='queued', phase='等待恢复扫描', progress=0)
            except (OSError, ValueError):
                continue

    def dispatch_intake_exo(self):
        from ego_capture.exo_calibration import process_exo

        with self.lock:
            if self.devices.session or (self.job or {}).get('status') == 'running':
                return
            if any(job['status'] == 'running' for job in self.intake_exo_jobs.values()):
                return
            pending = next(((sid, job) for sid, job in self.intake_exo_jobs.items()
                            if job['status'] == 'queued'), None)
            if pending is None:
                return
            session_id, job = pending
            job.update(status='running', phase='准备原始录像', progress=0)

        def work(progress):
            def update(percent, text):
                with self.lock:
                    job.update(progress=max(job['progress'], min(100, int(percent))), phase=text)
                progress(text)

            try:
                folder = self.closed_session_path(session_id)
                selection = self.intake(folder)
                if selection.get('cameras'):
                    process_exo(folder, selection['cameras'], self.exo_root, self.exo_profiles,
                                self.config['minimum_free_bytes'], progress,
                                measured_square_mm=selection['square_mm'],
                                video_catalog=self.exo_videos(session_id, selection),
                                profile_catalog=self.exo_intrinsics(session_id),
                                progress_update=update, selection_version=selection['selection_saved_at_ns'])
                else:
                    self.archive_exo_report(folder)
                with self.lock:
                    job.update(status='complete', phase='扫描完成', progress=100)
            except Exception as error:
                with self.lock:
                    job.update(status='failed', phase=str(error))
                raise
            return {'session_id': session_id}

        try:
            self.run_job('EXO 自动扫描', work, session_id=session_id)
        except ValueError:
            with self.lock:
                job.update(status='queued', phase='等待开始', progress=0)

    @staticmethod
    def archive_exo_report(folder):
        path = folder / 'derived/exo-session.json'
        if path.is_file():
            path.replace(path.with_name(f'exo-session-{time.time_ns()}.json'))

    def prepare_intake_preview(self, session, progress):
        from ego_capture.contact_calibration import prepare_exo_frames
        from ego_capture.qr_sync import analyze_recording

        progress('准备视触复核画面与曲线')
        process_session(session, self.config, progress, infer_pose=False, publish_quality=False)
        prepare_exo_frames(session, self.config['minimum_free_bytes'])
        if self.automatic_alignment(session) is None:
            progress('为复核建立相机时间参考')
            analyze_recording(session, progress)
        return self.intake_status(session)

    def finalize_intake(self, session, selection, progress):
        from ego_capture.contact_calibration import review_notes
        from ego_capture.exo_calibration import process_exo
        from ego_capture.qr_sync import analyze_recording

        versions = self.intake_versions(selection, review_notes(session))
        qc = process_session(session, self.config, progress, publish_quality=False)
        qc['intake_versions'] = versions
        if self.automatic_alignment(session) is None:
            progress('正在扫描录制的相机二维码')
            analyze_recording(session, progress)
        if selection.get('cameras') and not selection.get('exo_auto'):
            progress('正在对齐选定的 EXO 原片')
            process_exo(session, selection['cameras'], self.exo_root, self.exo_profiles,
                        self.config['minimum_free_bytes'], progress,
                        measured_square_mm=selection['square_mm'],
                        video_catalog=self.exo_videos(session.name), profile_catalog=self.exo_intrinsics(session.name))
        elif not selection.get('cameras') and selection.get('selection_saved_at_ns'):
            self.archive_exo_report(session)
        dump_json(session / 'derived/quality.json', qc)
        return qc

    def prepare_recording(self, session, progress, *, reprocess=False):
        from ego_capture.contact_calibration import review_notes
        from ego_capture.qr_sync import analyze_recording

        quality = session / 'derived/quality.json'
        publish = reprocess or not quality.is_file()
        if publish:
            versions = self.intake_versions(self.intake(session), review_notes(session))
            qc = process_session(session, self.config, progress, publish_quality=False)
            qc['intake_versions'] = versions
        else:
            qc = read_json(quality)
        if read_json(session / 'metadata.json').get('contact_calibration'):
            from ego_capture.contact_calibration import prepare_exo_frames
            progress('整理任务前后的校准画面…')
            prepare_exo_frames(session, self.config['minimum_free_bytes'])
        if reprocess or self.automatic_alignment(session) is None:
            progress('基础整理完成，正在扫描各路二维码…')
            analyze_recording(session, progress)
        if publish:
            dump_json(quality, qc)
        return qc

    def dispatch_recording_qc(self):
        """Prepare a closed recording, then align its saved camera sources."""
        self.dispatch_intake_exo()
        if self.devices.session or (self.job or {}).get('status') == 'running':
            return
        try:
            session_id = self.pending_recording_qc.get_nowait()
        except Empty:
            return
        try:
            session = self.closed_session_path(session_id)
            meta = read_json(session / 'metadata.json')
        except (OSError, ValueError):
            return
        if self.automatic_alignment(session) and (session / 'derived/quality.json').is_file():
            return
        if meta.get('capture_intake_required'):
            if not meta.get('contact_calibration') or self.automatic_alignment(session):
                return
            label, prepare = '准备视触复核', self.prepare_intake_preview
        else:
            label, prepare = '整理录制数据（QC）', self.prepare_recording
        try:
            self.run_job(label, lambda progress: prepare(session, progress), session_id=session.name)
        except ValueError:
            # Another local job or capture became active since the idle check.
            self.pending_recording_qc.put(session_id)

    def run_job(self, label, function, *, session_id=None):
        with self.lock:
            if self.job and self.job['status'] == 'running':
                raise ValueError('已有处理任务正在运行')
            if self.devices.session:
                raise ValueError('请先停止录制，再处理数据')
            job = self.job = {'label': label, 'status': 'running',
                              'message': '正在启动', 'started': time.time()}
            if session_id is not None:
                job['session_id'] = session_id

        def work():
            try:
                result = function(lambda text: job.update(message=text))
                job.update(status='complete', message='已完成', result=result)
            except Exception as exc:
                job.update(status='failed', message=str(exc))
            finally:
                job['finished'] = time.time()
        threading.Thread(target=work, daemon=True).start()
        return job

    # ---- honest status --------------------------------------------------
    def calibration_report(self):
        """Camera and glove calibration status, each item carrying its evidence class.

        The camera's own calibration is read from the device: per-eye K/D and the
        `cam0 <- cam1` transform arrive over the serial protocol. They are *reported*
        values, not measurements taken in this project, so the report keeps
        `independently_verified: False` and names what stays unverified -- the physical eye
        behind each stream index, the transform's direction convention, its unit, and any
        accuracy claim. The previous version read the per-topic calibration dict as if it
        were flat, so a session where the device *had* reported K/D still showed
        "not received".
        """
        from ego_capture import camera_calibration
        dev = self.devices
        cam = dev.state['camera']
        report = camera_calibration.device_calibration_report(
            dev.calibration, getattr(dev, 'transforms', {}), dev.identity or {},
            output_root=self.root)
        report['wizard'] = camera_calibration.wizard_steps(
            report, has_recording=bool(self.list_sessions()))
        report['sync'] = {
            'status': 'unverified',
            'clock_status': cam.get('clock_status', 'not_sent'),
            'note': '校时命令回执、设备钟差与视触延迟均未验证；离线 QC 只校验主机时间戳单调性与抖动。'}
        report['glove'] = {
            'status': 'unverified',
            'channel_map_source': self.config.get('contact_channel_map_source', 'unconfirmed'),
            'model_confirmed': False,
            'imu_present': 'unconfirmed',
            'note': 'VID/PID 仅识别为通用 STM32 串口设备；厂牌、通道到手指映射与 IMU 有无待实测确认。'}
        return report

    @staticmethod
    def recorded_device_report(setup):
        from ego_capture.camera_calibration import device_calibration_report

        if not isinstance(setup, dict) or not isinstance(setup.get('reported'), dict):
            raise ValueError('请选择 Ego 会话中的 calibration.json')
        report = device_calibration_report(setup['reported'], setup.get('reported_transforms') or {},
                                           setup.get('camera_identity') or {},
                                           eye_assignment=setup.get('eye_assignment'))
        report.update(touch_baseline=setup.get('touch_baseline') is not None,
                      channel_map_source=setup.get('touch_channel_map_source') or '未记录')
        return report

    def device_report(self):
        """Real serial devices vs virtual cameras are distinguishable paths."""
        from serial.tools import list_ports
        ports = [{'port': p.device, 'description': p.description,
                  'vid': p.vid, 'pid': p.pid} for p in list_ports.comports()]
        return {
            'serial_ports': ports,
            'camera_match': [p for p in ports if p['vid'] == self.config['camera_vid']
                             and p['pid'] == self.config['camera_pid']],
            'glove_match': [p for p in ports if p['vid'] == self.config['glove_vid']
                            and p['pid'] == self.config['glove_pid']],
            'virtual_cameras_note': '头戴双目和手套使用串口协议，EXO 使用 Windows 视频设备。',
        }

    def live_sources(self, *, refresh_video=True):
        devices = self.devices
        serial = self.device_report()
        with devices.lock:
            if not self.connect_devices:
                videos, error = [], None
            else:
                try:
                    videos, error = (devices.exo.discover() if refresh_video else list(devices.exo.catalog.values())), None
                except (ValueError, av.FFmpegError) as exc:
                    videos, error = [], str(exc)
            return dict(serial=serial, video=videos, video_error=error,
                        next_name=devices.next_recording_name, offline=not self.connect_devices,
                        serial_selection={kind: self.config.get(f'{kind}_port_override') or ''
                                          for kind in ('camera', 'glove')},
                        exo=devices.exo.state())

    def _configured_files(self, base, key):
        files = self.config.get(key, [])
        if not isinstance(files, list) or any(not isinstance(path, str) or not path.strip() for path in files):
            raise ValueError(f'{key} 须为文件路径列表')
        return tuple(dict.fromkeys((base / path).resolve() for path in files))

    @staticmethod
    def _add_configured_files(catalog, files, suffixes, prefix='外部文件'):
        available = set(catalog.values())
        for index, path in enumerate(files, 1):
            if path.suffix.lower() not in suffixes or not path.is_file() or path.resolve() != path:
                continue
            if path in available:
                continue
            label = f'{prefix} {index} · {path.name}'
            while label in catalog:
                label = '外部 · ' + label
            catalog[label] = path
            available.add(path)
        return catalog

    def exo_review_views(self, folder, metadata):
        from ego_capture.exo_review import views

        selection = self.intake(folder)
        try:
            imported = self.current_exo_report(folder, selection)
        except (OSError, ValueError, KeyError):
            imported = None
        try:
            alignment = self.automatic_alignment(folder)
        except (OSError, ValueError, KeyError):
            alignment = None
        return views(folder, metadata, imported, alignment)

    def exo_intrinsics(self, session_id=''):
        from ego_capture.exo_calibration import profiles
        catalog = self._add_configured_files(profiles(self.exo_profiles), self.exo_profile_files, {'.json'})
        if session_id:
            folder = self.closed_session_path(session_id)
            files = tuple(Path(path) for path in self.intake(folder).get('exo_profile_files', []))
            self._add_configured_files(catalog, files, {'.json'}, '本次导入')
        return catalog

    def exo_videos(self, session_id='', selection=None):
        from ego_capture.qr_sync import EXO_SUFFIXES, exo_catalog
        videos = self._add_configured_files(exo_catalog(self.exo_root), self.exo_video_files, EXO_SUFFIXES)
        if session_id:
            folder = self.closed_session_path(session_id)
            intake = self.intake(folder) if selection is None else selection
            files = tuple(Path(path) for path in intake.get('exo_video_files', []))
            self._add_configured_files(videos, files, EXO_SUFFIXES, '本次导入')
            row = read_json(folder / 'metadata.json').get('exo_live', {})
            live = exo_catalog(folder / 'exo-live')
            for role, record in row.items():
                name = f'{role}-live.mp4'
                if record.get('status') == 'saved' and name in live:
                    videos[f'本次录制 · {name}'] = live[name]
        return videos

    # ---- export / loader check ------------------------------------------
    def export_session(self, session_id):
        p = self.closed_session_path(session_id)
        dest = self.export_root / f'{session_id}.zip'
        if dest.exists():
            dest.unlink()
        shutil.make_archive(str(dest.with_suffix('')), 'zip', p)
        return {'session_id': session_id, 'export_path': str(dest),
                'bytes': dest.stat().st_size, 'sha256': sha256_file(dest)}

    def delete_session(self, session_id):
        if self.devices.session and self.devices.session.id == session_id:
            raise ValueError('该会话正在录制，请先停止录制再删除')
        if self.job and self.job['status'] == 'running':
            raise ValueError('已有处理任务正在运行，请等待完成后再删除')
        if any(segment.recording_id == session_id for segment in self.segments().items()):
            raise ValueError('此录制已有工作流片段，原始数据需保留。')
        result = delete_session(self.root, session_id)
        export = self.export_root / f'{session_id}.zip'
        if export.exists():
            export.unlink()
        return result

    def loader_check(self, session_id):
        from transparent_capture.loader import SessionLoader
        from transparent_capture.qc import check_session_sync
        from transparent_capture.calibration import check_meta, check_calibrations
        p = session_path(self.root, session_id)
        out = {'session_id': session_id}
        out['meta'] = check_meta(p)
        out['sync'] = check_session_sync(p)
        out['calibrations'] = check_calibrations(p)
        try:
            loader = SessionLoader(p)
            out['streams'] = {s: {'records': len(loader.records(s))}
                              for s in loader.streams()}
        except Exception as exc:
            out['streams'] = {'error': str(exc)}
        derived = p / 'derived/paired_frames.jsonl'
        out['paired_frames'] = len(read_jsonl(derived)) if derived.exists() else 0
        out['quality_processed'] = (p / 'derived/quality.json').exists()
        ready = (out['meta']['pass'] and out['sync']['pass']
                 and out.get('quality_processed', False) and out['paired_frames'] > 0)
        out['training_data_load_ready'] = bool(ready)
        out['readiness_note'] = ('通过仅代表离线加载链路完整（元数据/时间戳/QC/配对帧）；'
                                 '标定未测量与同步未验证仍会如实保留在结果中。')
        return out

    def dataset_loader_check(self, dataset_id):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', dataset_id):
            raise ValueError('Invalid dataset identifier')
        d = self.root / 'datasets' / dataset_id
        manifest_path = d / 'manifest.json'
        if not manifest_path.exists():
            raise ValueError('Dataset not found')
        manifest = read_json(manifest_path)
        samples = []
        with (d / 'samples.jsonl').open(encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    samples.append(json.loads(line))
        missing = [s['sample_id'] for s in samples[:50]
                   if not (Path(s['image0']).exists() and Path(s['image1']).exists())]
        return {'dataset_id': dataset_id, 'manifest': manifest,
                'sample_rows_checked': min(len(samples), 50),
                'missing_images': missing[:10],
                'load_ready': bool(manifest.get('train_ready', False)) and not missing,
                'note': 'load_ready 仅表示清单与图像文件可加载；科学结论仍取决于已复核标签与未验证的标定/同步状态。'}

    # ---- offline replay (read-only sources) ------------------------------
    def replay(self, session_id=None):
        if self.devices.session:
            raise ValueError('请先停止录制')
        if self.devices.replay_thread and self.devices.replay_thread.is_alive():
            raise ValueError('已有回放正在运行')
        if session_id:
            source = session_path(self.root, session_id) / 'camera.bin'
        else:
            source = Path(self.config['replay_source'])
        if not source.exists():
            raise ValueError('没有可用的历史录像')
        self.devices.state['mode'] = 'replay'
        self.devices.replay_stop.clear()

        def run():
            # Preview playback never creates an acquisition or fabricates a
            # synchronized glove stream.
            self.devices.stop.wait(.2)
            for q in self.devices.video_queues:
                q.put(None)
            parser = VisioDecoder()
            with source.open('rb') as f:
                while True:
                    data = f.read(32768)
                    if not data:
                        break
                    if self.devices.replay_stop.is_set() or self.devices.stop.is_set():
                        break
                    self.devices.consume('camera', parser, data, time.time_ns(), time.monotonic_ns())
                    self.devices.replay_stop.wait(.015)
            self.devices.state['notice'] = '历史相机回放结束；此回放没有配对触觉数据'
        self.devices.replay_thread = threading.Thread(target=run, daemon=True)
        self.devices.replay_thread.start()
        return {'mode': 'replay', 'source': str(source)}


class CaptureRoutes:
    """Namespaced HTTP routes mounted by the local Ego workbench."""

    @property
    def app(self):
        return self.server.app

    def send(self, code, data, kind='application/json; charset=utf-8'):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if isinstance(data, str):
            data = data.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_review_video(self, path):
        with path.open('rb') as handle:
            size = path.stat().st_size
            range_header = self.headers.get('Range')
            start, end = 0, size - 1
            if range_header is not None:
                match = re.fullmatch(r'bytes=(\d{0,20})-(\d{0,20})', range_header)
                if match and match[1]:
                    start = int(match[1])
                    end = min(end, int(match[2])) if match[2] else end
                elif match and match[2]:
                    start = max(0, size - int(match[2]))
                else:
                    start = size
                if start > end:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
            self.send_response(206 if range_header is not None else 200)
            self.send_header('Content-Type', 'video/quicktime' if path.suffix.lower() == '.mov' else 'video/mp4')
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'private, no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            if range_header is not None:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.end_headers()
            handle.seek(start)
            remaining = end - start + 1
            try:
                while remaining:
                    block = handle.read(min(1 << 20, remaining))
                    if not block:
                        break
                    self.wfile.write(block)
                    remaining -= len(block)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    def get_capture(self, path, query):
        try:
            app = self.app
            if path in ('/qr-timecode.html', '/qr-timecode.js', '/qr-renderer.js', '/qr-clock', '/qr-board.png'):
                from ego_capture.qr_display import response
                body, kind = response(path)
                return self.send(200, body, kind)
            if path == '/api/calibration/qr-inputs':
                return self.send(200, {'videos': sorted(app.exo_videos(query.get('id', [''])[0])),
                                       'directory': str(app.exo_root)})
            if path == '/api/exo/options':
                return self.send(200, {'video_directory': str(app.exo_root),
                                       'profile_directory': str(app.exo_profiles),
                                       'videos': sorted(app.exo_videos(query.get('id', [''])[0])),
                                       'profiles': sorted(app.exo_intrinsics(query.get('id', [''])[0]))})
            if path == '/api/exo/station':
                from ego_capture import exo_station
                return self.send(200, exo_station.state(app))
            if path == '/api/exo/station/profile':
                from ego_capture.exo_calibration import read_profile
                profile = app.exo_intrinsics().get(query.get('id', [''])[0])
                if profile is None:
                    return self.send(404, {'message': '找不到内参档案'})
                return self.send(200, read_profile(profile))
            if path == '/api/exo/intrinsics-example':
                example = ROOT / 'configs/ego-exo-intrinsics.example.json'
                return self.send(200, example.read_bytes(), 'application/json; charset=utf-8')
            if path == '/api/exo/board.png':
                from ego_capture.exo_calibration import board_png
                return self.send(200, board_png(), 'image/png')
            if path in ('/api/exo/review-video', '/api/exo/review-poster', '/api/exo/review-frame'):
                folder = app.closed_session_path(query.get('id', [''])[0])
                metadata = read_json(folder / 'metadata.json')
                if metadata.get('session_id') != folder.name:
                    raise ValueError('录制身份不一致')
                track_id, revision = (query.get(key, [''])[0] for key in ('track', 'v'))
                track = next((track for track in app.exo_review_views(folder, metadata)
                              if track.id == track_id and track.path is not None and track.version == revision), None)
                if track is None:
                    return self.send(404, {'message': '找不到所选会话的 EXO 录像'})
                if path.endswith('-video'):
                    return self.send_review_video(track.path)
                if path.endswith('-poster'):
                    image = track.poster()
                else:
                    time_s = float(query.get('time_s', ['nan'])[0])
                    image = track.frame(time_s)
                return (self.send(200, image, 'image/jpeg') if image else
                        self.send(404, {'message': '此时间没有可用的 EXO 画面'}))
            if path == '/api/exo/result':
                folder = app.closed_session_path(query.get('id', [''])[0])
                return self.send(200, app.current_exo_report(folder, app.intake(folder)) or {'status': 'missing'})
            if path == '/api/calibration/qr-result':
                from ego_capture.qr_sync import REPORT
                folder = app.closed_session_path(query.get('id', [''])[0])
                report = folder / 'derived' / REPORT
                if report.is_file():
                    return self.send(200, read_json(report))
                exo_report = folder / 'derived' / 'exo-session.json'
                if exo_report.is_file():
                    return self.send(200, read_json(exo_report)['sync'])
                return self.send(200, app.automatic_alignment(folder) or {'status':'missing'})
            if path == '/api/live/sources':
                return self.send(200, app.live_sources())
            if path.startswith('/frame/exo'):
                role = path.removeprefix('/frame/').removesuffix('.jpg')
                with app.devices.lock:
                    feed = app.devices.exo.feeds.get(role)
                    image = feed.preview if feed else None
                if image and time.monotonic_ns() - image[0] < 3_000_000_000:
                    return self.send(200, image[1], 'image/jpeg')
                return self.send(404, {'message': '等待画面'})
            if path == '/api/state':
                out = app.devices.snapshot()
                out.update(token=app.token, job=app.job, output_root=str(app.root),
                           station=app.config.get('station', 'v2'), standalone=app.standalone,
                           qr_phone_url=app.qr_phone_url, contact_calibration_supported=True,
                           recording_blocker=(app.devices.recording_blocker() if app.connect_devices else
                                              '当前为离线整理模式。连接设备并取消 --offline 后可录制。'),
                           code_root=str(ROOT), git_commit=app.git_commit,
                           origin_expected=['http://127.0.0.1:%d' % app.config['port'],
                                            'http://localhost:%d' % app.config['port']],
                           previews_present=[app.devices.previews[0] is not None,
                                             app.devices.previews[1] is not None],
                           preview_hz=app.devices.preview_hz())
                return self.send(200, out)
            if path == '/api/live/meta':
                # Lightweight fast-poll endpoint: only what the preview loop
                # needs, so live preview latency is decoupled from the heavy
                # full /api/state snapshot.
                d = app.devices
                with d.lock:
                    session = d.session
                    rec = (dict(session_id=session.id, name=session.metadata.get('name'),
                                contact_phase=d.contact_phase(session),
                                seconds=(time.monotonic_ns() - session.start_mono_ns) / 1e9,
                                bytes=session.byte_count + d.exo.bytes()) if session else None)
                    now = time.monotonic()
                    payload = {
                        'ts': time.time(), 'server_mono_ns': time.monotonic_ns(),
                        'mode': d.state['mode'], 'notice': d.state['notice'],
                        'cam_shape': [d.state['camera'].get(f'cam{cam}_shape') for cam in (0, 1)],
                        'previews_present': [
                            preview is not None and entry is not None and now - entry['arrived'] < 3
                            for preview, entry in zip(d.previews, d.latest, strict=True)],
                        'exo': d.exo.state(), 'next_name': d.next_recording_name,
                        'recording': rec, 'preview_hz': d.preview_hz()}
                return self.send(200, payload)
            if path == '/api/live/touch':
                # Drains scored touch samples newer than the client cursor so
                # short presses survive between polls (server samples ~20 Hz).
                try:
                    cursor = int(query.get('cursor', ['0'])[0])
                except ValueError:
                    raise ValueError('cursor must be an integer')
                return self.send(200, app.devices.drain_touch(cursor))
            if path == '/api/settings/paths':
                return self.send(200, app.path_settings())
            if path == '/api/calibration':
                return self.send(200, app.calibration_report())
            if path == '/api/calibration/recorded-device':
                folder = app.closed_session_path(query.get('id', [''])[0])
                return self.send(200, app.recorded_device_report(read_json(folder / 'calibration.json')))
            if path == '/api/devices':
                return self.send(200, app.device_report())
            if path == '/api/probe/report':
                return self.send(200, app.devices.probe_report())
            if path == '/api/probe/sequence':
                return self.send(200, app.devices.probe_sequence_status())
            if path == '/api/handpose/contract':
                return self.send(200, app.handpose_module().contract_report())
            if path == '/api/handpose/fixture':
                hp = app.handpose_module()
                return self.send(200, hp.fixture_payload(hp.synthetic_replay_fixture()))
            if path == '/api/handpose/replay':
                hp = app.handpose_module()
                report = app.handpose_report_path(query.get('report', [''])[0])
                return self.send(200, hp.replay_report_payload(report or
                                                              app.handpose_default_report()))
            if path == '/api/handpose/marker':
                return self.send(200, app.fiducial_module().marker_guide())
            if path == '/api/handpose/marker-layout':
                module = app.marker_layout_module()
                layout = module.load_layout(app.root)
                out = dict(layout)
                out['summary'] = module.layout_summary(layout)
                out['default_row'] = module.default_marker()
                out['default_rows'] = module.default_layout()['markers']
                out['orientation_options'] = module.orientation_options()
                out['usage'] = ('每一行是一个二维码标识：IDX 是检测器报告的标识号，位置是它在 '
                                'wrist_local（手局部坐标系）中的米制位置，姿态从下拉框选（朝手背'
                                '外法线 / 朝拇指方向）。位置表是「声明值」，不是实测值；两枚以上'
                                '同时可见时用互相一致性交叉检查。')
                return self.send(200, out)
            if path == '/api/handpose/marker.png':
                idx = str(query.get('idx', [''])[0]).strip()
                if not idx:
                    return self.send(200, app.fiducial_module().marker_png(), 'image/png')
                if not re.fullmatch(r'[0-9]{1,3}', idx):
                    raise ValueError('Invalid marker idx')
                pixels = min(max(int(query.get('pixels', ['1024'])[0]), 128), 4096)
                return self.send(200,
                                 app.marker_layout_module().marker_plate_png(int(idx),
                                                                             pixels=pixels),
                                 'image/png')
            if path == '/api/handpose/marker-registration':
                record = app.root / 'calibration' / 'marker-registration.json'
                if not record.exists():
                    raise ValueError('还没有做过二维码标定：请先点「用当前画面标定手→视觉」')
                return self.send(200, read_json(record))
            if path == '/api/motion/calibration':
                return self.send(200, app.devices.motion_calibration_status())
            if path == '/api/depth/models':
                return self.send(200, app.depth_runner().list_models())
            if path == '/api/train/status':
                return self.send(200, app.depth_module().train_status())
            if path == '/api/depth/status':
                return self.send(200, app.depth_runner().status())
            if path == '/api/depth/track/status':
                return self.send(200, app.depth_tracker().status())
            if path == '/api/depth/metrics':
                return self.send(200, app.depth_module().official_metrics())
            if path == '/api/depth/gallery':
                from ego_capture import eval_gallery
                return self.send(200, eval_gallery.gallery_report())
            if path == '/api/depth/gallery/image':
                from ego_capture import eval_gallery
                dataset = query.get('dataset', [''])[0]
                name = query.get('panel', [''])[0]
                if dataset not in eval_gallery.GALLERY_SOURCES:
                    raise ValueError('Invalid gallery dataset')
                if not re.fullmatch(r'[A-Za-z0-9_.-]{1,90}\.jpg', name):
                    raise ValueError('Invalid panel name')
                p = eval_gallery.GALLERY_ROOT / dataset / name
                if not p.exists():
                    raise ValueError('Panel not found')
                return self.send(200, p.read_bytes(), 'image/jpeg')
            if path == '/api/metrics':
                return self.send(200, app.metrics_report())
            if path == '/api/health':
                return self.send(200, {'service': 'capture-station-event-review', 'token': app.token})
            if path == '/api/events':
                return self.send(200, app.tactile_events().get(query.get('id', [''])[0]))
            if path == '/api/manual-events':
                from ego_capture.manual_events import list_events
                folder = app.closed_session_path(query.get('id', [''])[0])
                scope = json.loads(query.get('scope', ['null'])[0])
                return self.send(200, list_events(folder, scope, registry=app.segments()))
            if path == '/api/sessions':
                return self.send(200, app.list_sessions())
            if path == '/api/imports':
                return self.send(200, app.import_catalog())
            if path == '/api/intake':
                folder = app.closed_session_path(query.get('id', [''])[0])
                return self.send(200, app.intake_status(folder))
            if path == '/api/intake-exo':
                folder = app.closed_session_path(query.get('id', [''])[0])
                return self.send(200, app.intake_exo_state(folder))
            if path == '/api/recorded-setups':
                return self.send(200, app.setup_catalog())
            if path == '/api/recorded-tactile':
                from ego_capture.recorded_tactile import recorded_hand_at
                folder = app.closed_session_path(query.get('id', [''])[0])
                meta = read_json(folder / 'metadata.json')
                time_s = float(query.get('time', ['nan'])[0])
                if meta.get('session_id') != folder.name or not 0 <= time_s <= meta['duration_s']:
                    raise ValueError('触觉时间超出所选录制')
                from ego_capture.contact_calibration import review_camera_axes, timing_for_recording
                timing, camera, _ = timing_for_recording(folder, meta) if meta.get('contact_calibration') else ({}, None, None)
                axes = review_camera_axes(folder, meta, camera) if camera else None
                return self.send(200, dict(sessionId=folder.name, tactileTiming=timing,
                                           sample=recorded_hand_at(folder, meta['start_mono_ns'], time_s,
                                                                   timing=timing, camera_axes=axes)))
            if path == '/api/policy-labels':
                from ego_capture.policy_labels import policy_labels
                return self.send(200, policy_labels({
                    key: query.get(key, [''])[0]
                    for key in ('clip', 'recordingId', 'segmentId', 'segmentRevision')}))
            if path == '/api/guided':
                from ego_capture import guided_capture
                return self.send(200, guided_capture.status(app, query.get('id', [None])[0]))
            if path == '/api/datasets':
                return self.send(200, [read_json(p) for p in
                                       sorted((app.root / 'datasets').glob('*/manifest.json'), reverse=True)])
            if path in ('/api/contact-calibration', '/api/contact-calibration/frame'):
                from ego_capture import contact_calibration
                folder = app.closed_session_path(query.get('id', [''])[0])
                cam = query.get('cam', ['cam0'])[0]
                if path.endswith('/frame'):
                    image = contact_calibration.frame_path(folder, cam, query.get('frame', [''])[0])
                    return self.send(200, image.read_bytes(), 'image/jpeg')
                return self.send(200, contact_calibration.view(folder, query.get('part', ['before'])[0], cam))
            if path == '/api/session':
                p = session_path(app.root, query.get('id', [''])[0])
                from ego_capture import guided_capture
                quality = p / 'derived/quality.json'
                view = query.get('view')
                if view in (['quality'], ['review']):
                    meta = read_json(p / 'metadata.json')
                    if meta.get('session_id') != p.name:
                        raise ValueError('录制身份不一致')
                    detail = {'metadata': meta,
                              'quality': read_json(quality) if quality.is_file() else None,
                              'setup': app.review_setup(p.name)}
                    if view == ['review']:
                        from workflow_segments import review_intervals
                        guided_file = p / guided_capture.FILE
                        guided = read_json(guided_file) if guided_file.is_file() else None
                        detail['segments'] = review_intervals(p.name, meta['duration_s'], guided, registry=app.segments())
                        detail['exo_views'] = [track.describe() for track in app.exo_review_views(p, meta)]
                        detail['frames'] = read_jsonl(p / 'derived/paired_frames.jsonl')
                        if meta.get('contact_calibration'):
                            from visuotactile.storage import recorded_task_range
                            task = recorded_task_range(meta)
                            from ego_capture.contact_calibration import timing_for_recording
                            timing, _, _ = timing_for_recording(p, meta)
                            detail['tactile_timing'] = timing
                            detail['task_range'] = task
                            detail['segments'] = [row for row in detail['segments']
                                                  if task and task[0] <= row['startS'] < row['endS'] <= task[1]]
                            detail['frames'] = [
                                row for row in detail['frames'] if task and task[0] <= row['t_s'] < task[1]]
                            if task and not any(row['name'] == 'task' and (row['startS'],row['endS']) == task
                                                for row in detail['segments']):
                                detail['segments'].insert(0, dict(name='task',label='正式任务',
                                                                  startS=task[0],endS=task[1],category='normal'))
                    return self.send(200, detail)
                signals = []
                if quality.exists():
                    import numpy as np
                    with np.load(p / 'derived/glove_signals.npz', allow_pickle=False) as z:
                        stride = max(1, len(z['time_s']) // 1500)
                        signals = list(zip(z['time_s'][::stride].tolist(), z['score'][::stride].tolist()))
                return self.send(200, {'metadata': read_json(p / 'metadata.json'),
                                       'annotations': read_json(p / 'annotations.json'),
                                       'quality': read_json(quality) if quality.exists() else None,
                                       'guided_capture': read_json(p / guided_capture.FILE) if (p / guided_capture.FILE).exists() else None,
                                       'frames': read_jsonl(p / 'derived/paired_frames.jsonl'),
                                       'signals': signals})
            if path == '/api/export':
                return self.send(200, app.export_session(query.get('id', [''])[0]))
            if path == '/api/loader-check':
                return self.send(200, app.loader_check(query.get('id', [''])[0]))
            if path == '/api/dataset-loader-check':
                return self.send(200, app.dataset_loader_check(query.get('id', [''])[0]))
            if path.startswith('/frame/'):
                cam = int(path.split('/')[2].split('.')[0])
                if cam not in (0, 1):
                    raise ValueError('Invalid camera')
                image = app.devices.previews[cam]
                return self.send(200, image, 'image/jpeg') if image else self.send(404, {'message': '等待画面'})
            if path == '/api/image':
                relative = query.get('image', [''])[0]
                if not re.fullmatch(r'cam[01]/[0-9]+\.jpg', relative):
                    raise ValueError('Invalid image')
                image = app.review_frame_path(query.get('id', [''])[0], relative)
                return self.send(200, image.read_bytes(), 'image/jpeg')
            if path in ('/', '/index.html', '/app.js', '/style.css', '/guided-capture.js',
                        '/review_event_view.js', '/recorded-hand-view.js', '/exo-setup.js', '/contact-calibration.js',
                        '/station-paths.js'):
                p = WEB / ('index.html' if path == '/' else path[1:])
                return self.send(200, p.read_bytes(),
                                 (mimetypes.guess_type(p.name)[0] or 'text/plain') + '; charset=utf-8')
            self.send(404, {'error': 'Not found'})
        except Exception as exc:
            self.send(400, {'error': str(exc)})

    def post_capture(self, path):
        try:
            app = self.app
            expected = f"http://127.0.0.1:{app.config['port']}"
            expected_all = (expected, expected.replace('127.0.0.1', 'localhost'))
            origin = self.headers.get('Origin')
            path_only = urllib.parse.urlsplit(self.path).path
            origin_parts = None
            if origin:
                try:
                    origin_parts = urllib.parse.urlsplit(origin)
                except ValueError:
                    origin_parts = None
            origin_loopback = (
                origin_parts is not None
                and origin_parts.scheme == 'http'
                and origin_parts.hostname in ('127.0.0.1', 'localhost', '::1')
                and origin_parts.port in (None, app.config['port'])
            )
            if origin and not origin_loopback:
                diag = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
                        'path': path_only,
                        'expected_origin': list(expected_all),
                        'actual_origin': origin,
                        'remote_addr': self.client_address[0],
                        'verdict': 'rejected'}
                _log_origin_rejection(diag)
                return self.send(403, {'error': 'Origin rejected', 'origin_diagnostic': diag})
            if origin and origin not in expected_all:
                # Accepted loopback deviation. The Origin header is
                # browser-controlled (page JS cannot set it), so a portless
                # 'http://127.0.0.1' can only come from a local document or a
                # local browser component that rewrites the header. The
                # 2026-09-17 23:12-23:22 incidents showed exactly that on the
                # operator's machine while the page origin itself was correct,
                # so blocking it only locks the operator out; a foreign page
                # would still send its own origin and be rejected above.
                _log_origin_rejection({'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
                                       'path': path_only,
                                       'expected_origin': list(expected_all),
                                       'actual_origin': origin,
                                       'remote_addr': self.client_address[0],
                                       'verdict': 'accepted_loopback_deviation'})
            if self.headers.get('X-Capture-Token') != app.token:
                return self.send(403, {'error': '请刷新本地页面'})
            size = int(self.headers.get('Content-Length', '0'))
            # 10 MB: depth inference uploads a camera frame as base64 JPEG.
            if not 0 <= size <= 10_000_000:
                raise ValueError('Request too large')
            data = json.loads(self.rfile.read(size) or b'{}')
            dev = app.devices
            if path == '/api/settings/paths':
                result = app.save_paths(data)
            elif path == '/api/settings/choose-path':
                result = {'paths': station_paths.choose(data.get('field'), app.config_base, data.get('current', ''))}
            elif path == '/api/exo/station':
                from ego_capture import exo_station
                result = exo_station.save(app, data)
            elif path == '/api/exo/station/calibrate':
                from ego_capture import exo_station
                result = exo_station.calibrate_video(app)
            elif path == '/api/exo/station/profiles':
                from ego_capture import exo_station
                result = exo_station.import_profiles(app)
            elif path == '/api/calibration/recorded-device':
                result = app.recorded_device_report(data)
            elif path == '/api/live/source':
                if not app.connect_devices:
                    raise ValueError('当前为离线整理模式')
                role, source = data.get('role'), data.get('source', '')
                if not isinstance(source, str):
                    raise ValueError('请选择设备列表中的来源')
                with dev.lock:
                    if dev.session:
                        raise ValueError('录制中不能切换来源')
                    if role in ('camera', 'glove'):
                        dev.bind_serial(role, source)
                    else:
                        dev.exo.bind(role, source)
                result = app.live_sources(refresh_video=False)
            elif path == '/api/record/quick':
                if not app.connect_devices:
                    raise ValueError('当前为离线整理模式')
                result = {'session_id': dev.start_quick_recording(
                    data.get('name', ''), contact_calibration=data.get('contact_calibration', False))}
            elif path == '/api/record/task-boundary':
                result = dev.mark_task_boundary(data.get('session_id'), data.get('action'))
            elif path == '/api/contact-calibration/review':
                from ego_capture.contact_calibration import save_check
                folder = app.closed_session_path(data.get('session_id'))
                with app.lock:
                    result = save_check(folder, data.get('part'), data.get('action'), data)
            elif path == '/api/record/start':
                result = {'session_id': dev.start_recording(data)}
            elif path == '/api/record/start-unlimited':
                metadata = dict(data, capture_mode='manual_unlimited')
                result = {'session_id': dev.start_recording(metadata, enforce_recording_limits=False)}
            elif path.startswith('/api/guided/'):
                from ego_capture import guided_capture
                actions = {'/api/guided/begin': guided_capture.begin,
                           '/api/guided/end': guided_capture.end,
                           '/api/guided/select': guided_capture.change_deleted,
                           '/api/guided/annotate': guided_capture.annotate,
                           '/api/guided/finish': guided_capture.finish}
                if path not in actions:
                    raise ValueError('Unknown guided capture action')
                result = actions[path](app, data)
            elif path == '/api/record/stop':
                with dev.lock:
                    session = dev.session
                    result = {'session_id': dev.stop_recording(),
                              'exo': session.metadata.get('exo_live', {}) if session else {}}
            elif path == '/api/touch/baseline':
                dev.calibrate_touch()
            elif path == '/api/calibration/eye-assignment':
                # The protocol names cam0/cam1, never "left/right eye": the binding comes
                # from the operator covering one lens. Only streams the device actually
                # reported may be assigned, and an unassigned stream stays `unknown`.
                from ego_capture import camera_calibration
                known = set(camera_calibration.stream_reports(dev.calibration))
                if not known:
                    raise ValueError('尚未收到相机自报内参，请先连接相机再提交左右目对应')
                submitted = {str(k): v for k, v in (data.get('streams') or {}).items()}
                if not submitted or not set(submitted) <= known:
                    raise ValueError('提交的相机流与设备自报的流不一致')
                result = camera_calibration.write_eye_assignment(app.root, submitted)
            elif path == '/api/intake/files':
                folder = app.closed_session_path(data.get('session_id', ''))
                field = data.get('field')
                if field not in station_paths.MULTI:
                    raise ValueError('请选择 EXO 视频或内参档案')
                chosen = station_paths.choose(field, app.config_base, data.get('current', ''))
                result = app.add_intake_files(folder, field, chosen) if chosen else app.intake_status(folder)
            elif path == '/api/intake':
                folder = app.closed_session_path(data.get('session_id', ''))
                result = app.save_intake_choices(folder, data.get('cameras'), data.get('square_mm', 25))
            elif path in ('/api/intake-exo/import', '/api/intake-exo/profile',
                          '/api/intake-exo/remove', '/api/intake-exo/retry'):
                folder = app.closed_session_path(data.get('session_id', ''))
                action = path.rsplit('/', 1)[-1]
                if action == 'import':
                    chosen = station_paths.choose('exo_video_files', app.config_base, str(app.exo_root))
                    result = (app.update_intake_exo(folder, action, data, chosen)
                              if chosen else app.intake_exo_state(folder))
                else:
                    result = app.update_intake_exo(folder, action, data)
            elif path == '/api/intake/preview':
                folder = app.closed_session_path(data.get('session_id', ''))
                result = app.run_job('准备视触复核', lambda progress: app.prepare_intake_preview(folder, progress),
                                     session_id=folder.name)
            elif path == '/api/exo/process':
                from ego_capture.exo_calibration import process_exo
                folder = app.closed_session_path(data.get('session_id', ''))
                if not (folder / 'derived/quality.json').is_file():
                    raise ValueError('请先整理头戴双目和手套录制')
                result = app.run_job('EXO 标定与对齐', lambda progress: process_exo(
                    folder, data.get('cameras'), app.exo_root, app.exo_profiles,
                    app.config['minimum_free_bytes'], progress,
                    measured_square_mm=data.get('square_mm', 25),
                    video_catalog=app.exo_videos(folder.name), profile_catalog=app.exo_intrinsics(folder.name)),
                    session_id=folder.name)
            elif path == '/api/calibration/qr-sync':
                from ego_capture.qr_sync import analyze
                folder = app.closed_session_path(data.get('session_id', ''))
                files = data.get('videos', [])
                catalog = app.exo_videos(folder.name)
                if not isinstance(files, list) or any(type(name) is not str or name not in catalog for name in files):
                    raise ValueError('请选择导入目录里原始 Exo 视频')
                if len(files) != len(set(files)):
                    raise ValueError('外视角视频不可重复选择')
                videos = [catalog[name] for name in files]
                result = app.run_job(QR_JOB, lambda progress: analyze(folder, videos, progress),
                                     session_id=folder.name)
            elif path == '/api/calibration/sync-analysis':
                # Offline over a finished recording: no device is touched. The estimator
                # publishes an offset only when a shared stimulus is actually localisable;
                # otherwise it returns `inconclusive` with the measured peak ratio.
                from transparent_capture.time_sync import analyze_session
                session_id = str(data.get('session_id') or '')
                if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', session_id):
                    raise ValueError('Invalid session identifier')
                session_dir = app.root / 'sessions' / session_id
                if not session_dir.is_dir():
                    raise ValueError('会话不存在')
                report = analyze_session(session_dir)
                out = app.root / 'reports' / ('%s-time-sync.report.json' % session_id)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                               encoding='utf-8')
                result = {'session_id': session_id, 'report_path': str(out),
                          'estimate': report['estimate']}
            elif path == '/api/probe/baseline':
                result = dev.probe_capture_baseline(min(max(int(data.get('seconds', 3)), 1), 15))
            elif path == '/api/probe/trial':
                result = dev.probe_capture_trial(str(data.get('position', '')), data.get('trial_type', 'press'),
                                                 min(max(int(data.get('seconds', 4)), 1), 15))
            elif path == '/api/probe/sequence/start':
                result = dev.probe_sequence_start(data.get('seconds'), data.get('gap'))
            elif path == '/api/probe/sequence/stop':
                result = dev.probe_sequence_stop()
            elif path == '/api/motion/calibration/start':
                result = dev.motion_calibration_start(data.get('seconds'), data.get('gap'))
            elif path == '/api/motion/calibration/stop':
                result = dev.motion_calibration_stop()
            elif path == '/api/probe/mapping':
                result = dev.probe_set_mapping(data.get('labels', []), str(data.get('note', ''))[:200])
                result = {'ok': True}
            elif path == '/api/marker':
                if not dev.session:
                    raise ValueError('请先开始录制')
                if data.get('kind') not in ('operator_contact', 'operator_release', 'operator_note'):
                    raise ValueError('Invalid marker')
                dev.session.event(data['kind'], note=str(data.get('note', ''))[:1000],
                                  source='operator_button_not_precise_ground_truth')
                result = {'ok': True}
            elif path == '/api/handpose/marker-layout':
                module = app.marker_layout_module()
                stored = module.save_layout(app.root, data.get('layout') or {})
                result = dict(stored)
                result['summary'] = module.layout_summary(stored)
            elif path == '/api/handpose/marker-calibrate':
                result = app.marker_registration(data)
            elif path == '/api/events/ensure':
                p = app.closed_session_path(data['session_id'])
                if not (p / 'derived/quality.json').is_file():
                    raise ValueError('请先整理所选录制（QC）')
                result = app.tactile_events().ensure(p.name)
            elif path == '/api/events/review':
                result = app.tactile_events().review(data)
            elif path == '/api/manual-events':
                from ego_capture.manual_events import change_event
                folder = app.closed_session_path(data['session_id'])
                result = change_event(folder, data['scope'], data['action'], data['event'], registry=app.segments())
            elif path == '/api/imports':
                result = app.import_recording(data['session_id'])
            elif path == '/api/policy-labels/review':
                from ego_capture.policy_labels import review_policy_label
                result = review_policy_label(data['segment'], data['traceSha256'], data['index'], data['choice'])
            elif path == '/api/review-setup':
                result = app.review_setup(data['session_id'], data['source_id'])
            elif path == '/api/process':
                p = app.closed_session_path(data['session_id'])
                status = app.intake_status(p)
                if status['needs_review']:
                    raise ValueError('请在 02 导入与复核确认或跳过任务前后对帧')
                if status['exo_pending']:
                    raise ValueError('EXO 正在扫描，完成后可整理会话。')
                selection = app.intake(p)
                if (read_json(p / 'metadata.json').get('capture_intake_required')
                        or selection.get('cameras') or selection.get('selection_saved_at_ns')):
                    process = lambda progress: app.finalize_intake(p, selection, progress)
                else:
                    process = lambda progress: app.prepare_recording(p, progress, reprocess=True)
                result = app.run_job('整理录制数据（QC）', process, session_id=p.name)
            elif path == '/api/workflow-segments':
                from workflow_segments import register
                p = app.closed_session_path(data['session_id'])
                result = register(p.name, data['name'], data['start_s'], data['end_s'], data.get('category', 'normal'),
                                  segments=app.segments(), preview_root=app.root if app.standalone else ROOT)
                from ego_capture.manual_events import copy_custom_events
                try:
                    copied = copy_custom_events(p, result['segmentId'], data['start_s'], data['end_s'])
                except (OSError, ValueError, KeyError, TypeError):
                    result['manualEventsWarning'] = '片段已登记，手动标注还保存在原自选区间。'
                else:
                    if copied:
                        result['manualEventsCopied'] = copied
            elif path == '/api/annotations':
                p = session_path(app.root, data['session_id'])
                meta = read_json(p / 'metadata.json')
                annotation = data['annotations']
                validate_annotations(annotation, meta['duration_s'])
                dump_json(p / 'annotations.json', annotation)
                result = {'ok': True}
            elif path == '/api/dataset':
                task = data.get('task', 'future_contact')
                if task not in ('future_contact', 'approach_distance'):
                    raise ValueError('Invalid task')
                result = app.run_job('构建训练清单',
                                     lambda progress: build_dataset(app.root, app.config, task))
            elif path == '/api/dataset/delete':
                result = delete_dataset(app.root, data['dataset_id'])
            elif path == '/api/session/delete':
                result = app.delete_session(data['session_id'])
            elif path == '/api/replay':
                result = app.replay(data.get('session_id'))
            elif path == '/api/live':
                dev.replay_stop.set()
                if dev.replay_thread:
                    dev.replay_thread.join(timeout=2)
                dev.state['mode'] = 'live'
                dev.state['notice'] = '实时模式：连接设备后会自动显示'
                result = {'ok': True}
            elif path == '/api/depth/load':
                result = app.depth_runner().load(str(data.get('model_id', '')))
            elif path == '/api/depth/infer':
                dm = app.depth_module()
                rgb = dm.decode_image_b64(data['image_b64'])
                max_fps = float(data.get('max_fps', 10.0))
                if not 0.1 <= max_fps <= 120.0:
                    raise ValueError('max_fps must be between 0.1 and 120')
                result = app.depth_runner().infer_sync(
                    rgb, post_process=bool(data.get('post_process', True)), max_fps=max_fps)
            elif path == '/api/depth/roi':
                rect = data.get('rect')
                if not (isinstance(rect, list) and len(rect) == 4):
                    raise ValueError('rect must be [x0, y0, x1, y1] normalized to [0,1]')
                rect = [float(v) for v in rect]
                if not all(0.0 <= v <= 1.0 for v in rect):
                    raise ValueError('rect coordinates must be within [0,1]')
                result = app.depth_runner().roi(rect)
            elif path == '/api/depth/track/start':
                rect = data.get('roi_rect')
                if rect is not None:
                    if not (isinstance(rect, list) and len(rect) == 4):
                        raise ValueError('roi_rect must be [x0, y0, x1, y1] normalized to [0,1]')
                    rect = [float(v) for v in rect]
                    if not all(0.0 <= v <= 1.0 for v in rect):
                        raise ValueError('roi_rect coordinates must be within [0,1]')
                result = app.depth_tracker().start(
                    interval_ms=data.get('interval_ms', 500), roi_rect=rect)
            elif path == '/api/depth/track/stop':
                result = app.depth_tracker().stop()
            elif path == '/api/depth/track/pause':
                result = app.depth_tracker().pause()
            elif path == '/api/depth/track/resume':
                result = app.depth_tracker().resume()
            elif path == '/api/depth/track/roi':
                rect = data.get('roi_rect')
                if rect is not None:
                    if not (isinstance(rect, list) and len(rect) == 4):
                        raise ValueError('roi_rect must be [x0, y0, x1, y1] normalized to [0,1]')
                    rect = [float(v) for v in rect]
                    if not all(0.0 <= v <= 1.0 for v in rect):
                        raise ValueError('roi_rect coordinates must be within [0,1]')
                result = app.depth_tracker().set_roi(rect)
            elif path == '/api/train':
                # Honest refusal: formal local training is deferred to the NTU
                # cluster (P1-FULL). Report existing log facts, never relaunch.
                result = app.depth_module().train_status()
            elif path == '/api/qnbot/calibration/open':
                result = app.open_qnbot_calibration()
            elif path == '/api/qnbot/calibration/start':
                result = app.devices.start_qnbot_calibration()
            elif path == '/api/qnbot/calibration/capture':
                result = app.devices.capture_qnbot_calibration()
            elif path == '/api/qnbot/calibration/cancel':
                result = app.devices.cancel_qnbot_calibration()
            elif path == '/api/qnbot/calibration/reuse':
                result = app.devices.reuse_qnbot_calibration()
            else:
                return self.send(404, {'error': 'Not found'})
            self.send(200, result)
        except Exception as exc:
            self.send(400, {'error': str(exc)})
