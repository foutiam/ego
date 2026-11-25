"""Capture-station device stack with session-scoped counting semantics.

Provenance: the serial/decode/pose/session logic is the read-only
``visuotactile`` package from ``D:/diss/prj/code`` (content reuse is allowed
per AGENTS.md; the old checkout itself is never modified). This file adds
ONLY the counting fix found in the 2026-09-15 audit of session
20260915-222256-5ef746:

Bug: ``visuotactile.devices.Devices._reader()`` initializes ``total`` and the
parser's ``good``/``bad`` counters at serial-connect time, not at record
start, so the values snapshotted into ``metadata.json['quality']`` (and shown
in the UI) are connection-lifetime cumulative numbers, not session values.

Fix (display/semantics only; raw recording bytes were always correct):
  * at ``start_recording`` the current cumulative counters are captured as a
    per-device baseline;
  * at ``stop_recording`` the metadata quality gains a ``session`` block
    (deltas over the recording window: frames/bytes/invalid/duration/mean
    rate) next to the legacy ``since_connection`` cumulative block, both
    clearly labeled;
  * ``snapshot()`` exposes live per-device session deltas under
    ``recording.per_device`` so the UI can show session-scoped numbers while
    recording.

No conversion, smoothing or re-counting of raw data is performed anywhere.
"""
from __future__ import annotations

import json
import time

from .live_exo import LiveExo
from visuotactile.storage import dump_json

from visuotactile.devices import Devices  # noqa: E402  (read-only reuse source)

COUNTING_NOTE = (
    'session=录制窗口内的增量（本帧起始/结束计数之差）；'
    'since_connection=自串口连接起的累计值（旧字段，保留用于核对）。'
    '原始字节流本身从未受影响：quality 由录制停止时的计数差值得出。')


class StationDevices(Devices):
    """Devices with session-scoped counter baselines (see module docstring)."""

    def __init__(self, config):
        super().__init__(config)
        # kind -> {'mono_ns', 'frames', 'bytes', 'invalid_or_partial'} captured
        # at record start; cleared when the recording stops.
        self.count_basis = {}
        self.exo = LiveExo()
        self.sequence_file = self.root / 'capture-sequence.json'
        try:
            self.sequence = int(json.loads(self.sequence_file.read_text(encoding='utf-8'))['last_recording'])
        except (OSError, ValueError, TypeError, KeyError):
            self.sequence = 0

    @property
    def next_recording_name(self):
        return f'录制 {self.sequence + 1:03d}'

    def start_quick_recording(self, name='', *, enforce_recording_limits=True, contact_calibration=False):
        if type(contact_calibration) is not bool:
            raise ValueError('校准设置格式无效')
        if not isinstance(name, str) or len(name) > 80 or any(ord(c) < 32 for c in name):
            raise ValueError('录制名称请使用 80 字以内的单行文字')
        with self.lock:
            if self.session:
                raise ValueError('已有录制正在进行')
            blocker = self.recording_blocker()
            if blocker:
                raise ValueError(blocker)
            number = self.sequence + 1
            # Reserve before opening streams, so a reset or deletion cannot reuse an ID.
            dump_json(self.sequence_file, {'last_recording': number})
            self.sequence = number
            metadata = dict(name=name.strip() or f'录制 {number:03d}',
                            capture_number=number, object_id='unassigned', scene_id='scene_001',
                            material='unknown', split='unassigned', capture_mode='quick')
            if contact_calibration:
                metadata['contact_calibration'] = dict(task_start_s=None, task_end_s=None)
            return self.start_recording(metadata, enforce_recording_limits=enforce_recording_limits)

    def contact_phase(self, session=None):
        from .contact_calibration import phase
        session = self.session if session is None else session
        record = session.metadata.get('contact_calibration') if session else None
        return phase(record) if record else None

    def mark_task_boundary(self, session_id, action):
        with self.lock:
            s = self.session
            if not s or s.id != session_id or not s.metadata.get('contact_calibration'):
                raise ValueError('当前录制没有启用任务前后校准')
            phase = self.contact_phase(s)
            if (phase, action) not in (('before', 'start'), ('task', 'end')):
                if (phase, action) in (('task', 'start'), ('after', 'end')):
                    return {'phase': phase}
                raise ValueError('当前录制步骤已变更，请核对后重试')
            timestamp = (time.monotonic_ns() - s.start_mono_ns) / 1e9
            updated = dict(s.metadata['contact_calibration'], **{f'task_{action}_s': timestamp})
            metadata = dict(s.metadata, contact_calibration=updated)
            dump_json(s.path / 'metadata.json', metadata)
            s.metadata = metadata
            s.event('contact_calibration_task_' + action, boundary_s=timestamp)
            return {'phase': self.contact_phase(s)}

    def recording_blocker(self):
        with self.lock:
            reason = super().recording_blocker()
            if reason:
                return reason
            for role, feed in self.exo.state().items():
                if not feed['ready']:
                    return f"{role.upper()} 暂无画面，请重选相机来源或停用该路。"

    def check_recording_limits(self):
        with self.lock:
            super().check_recording_limits()
            if not self.session:
                return
            stopped = [role.upper() for role, feed in self.exo.state().items() if not feed['ready'] or feed['error']]
            if stopped:
                self.stop_recording('EXO 视频流中断：' + '、'.join(stopped))

    def close(self):
        try:
            super().close()
        finally:
            with self.lock:
                self.exo.close()

    # -- baselines ---------------------------------------------------------
    def _capture_basis(self):
        basis = {}
        for kind in ('camera', 'glove'):
            s = self.state[kind]
            basis[kind] = {'mono_ns': time.monotonic_ns(),
                           'frames': int(s.get('frames', 0)),
                           'bytes': int(s.get('bytes', 0)),
                           'invalid_or_partial': int(s.get('invalid_or_partial', 0))}
        return basis

    def _session_counts(self, kind, start_mono_ns=None):
        """Live deltas for one device over the current recording window."""
        s = self.state[kind]
        basis = self.count_basis.get(kind) or {}
        start = basis.get('mono_ns', start_mono_ns)
        end = time.monotonic_ns()
        duration_s = max((end - start) / 1e9, 0.0)
        counts = {
            'frames': max(0, int(s.get('frames', 0)) - int(basis.get('frames', 0))),
            'bytes': max(0, int(s.get('bytes', 0)) - int(basis.get('bytes', 0))),
            'invalid_or_partial': max(0, int(s.get('invalid_or_partial', 0))
                                      - int(basis.get('invalid_or_partial', 0))),
            'duration_s': round(duration_s, 3),
        }
        counts['mean_rate_hz'] = (round(counts['frames'] / duration_s, 2)
                                  if duration_s > 0.2 else None)
        return counts

    # -- recording lifecycle ----------------------------------------------
    def start_recording(self, metadata, *, enforce_recording_limits=True):
        with self.lock:                       # RLock: super() re-acquires it
            if self.session:
                raise ValueError('已有录制正在进行')
            metadata = dict(metadata, auto_qr_sync=True, capture_intake_required=True, capture_sources={
                'stereo': dict(port=self.state['camera'].get('port'), camera_serial=self.identity.get('serial'),
                               streams={'cam0': '/0', 'cam1': '/1'}),
                'glove': dict(port=self.state['glove'].get('port')),
                'exo': {role: dict(feed.source) for role, feed in self.exo.feeds.items()},
            })
            from .guided_capture import PROFILES, object_profile
            if metadata.get('object_id') in PROFILES:
                profile = object_profile(metadata['object_id'])
                metadata = dict(metadata, object_profile=profile, material=profile['material'])
            self.count_basis = self._capture_basis()
            try:
                identity = super().start_recording(metadata, enforce_recording_limits=enforce_recording_limits)
                if metadata.get('capture_mode') == 'guided':
                    from .guided_capture import MAX_BYTES, MAX_SECONDS
                    self.session.config = dict(self.session.config,
                                               max_session_seconds=MAX_SECONDS,
                                               max_session_bytes=MAX_BYTES)
                try:
                    self.exo.start(self.session)
                    self.session.external_bytes = self.exo.bytes
                except Exception:
                    self.stop_recording('无法创建 EXO 录制文件')
                    raise
                return identity
            except Exception:
                self.count_basis = {}
                raise

    def stop_recording(self, reason='用户停止'):
        with self.lock:
            if not self.session:
                return None
            s = self.session
            if self.contact_phase(s) == 'task':
                s.metadata['contact_calibration']['task_end_s'] = (time.monotonic_ns() - s.start_mono_ns) / 1e9
            from .guided_capture import close_manifest
            close_manifest(s, reason)
            self.exo.finish(s)
            self.session = None
            start_mono = self.count_basis.get('camera', {}).get('mono_ns',
                                                                s.start_mono_ns)
            quality = {'counting': {'note': COUNTING_NOTE,
                                    'session_start_mono_ns': start_mono}}
            for kind in ('camera', 'glove'):
                quality[kind] = {'session': self._session_counts(kind, s.start_mono_ns),
                                 'since_connection': dict(self.state[kind]),
                                 'counting_note': COUNTING_NOTE}
            s.close(reason, quality)
            self.state['notice'] = '录制已保存：' + s.id
            self.count_basis = {}
            on_saved = getattr(self, 'on_recording_saved', None)
            if on_saved is not None and not self.stop.is_set():
                on_saved(s.id)
            return s.id

    # -- live snapshot ------------------------------------------------------
    def snapshot(self):
        out = super().snapshot()
        if self.session:
            out['recording']['per_device'] = {
                kind: self._session_counts(kind, self.session.start_mono_ns)
                for kind in ('camera', 'glove')}
            out['recording']['counting_note'] = COUNTING_NOTE
            out['recording']['limits_enabled'] = self.session.enforce_recording_limits
            out['recording']['contact_phase'] = self.contact_phase()
        return out
