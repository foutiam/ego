"""Camera-device-reported calibration: what the device states, and what that is not.

Why this module exists
----------------------
Webpage 02 showed the camera calibration as "not received" even in a session where the
device *had* reported it. The report read a per-topic dict as if it were one flat
calibration (`cal.get('K')` on `{'/ego/camera/0/intrinsics': {...}}` is always `None`),
and it labelled the stereo extrinsics `unmeasured`. The second half was true of *our*
measurements but wrong about the *device*: the camera publishes its own `cam0 <- cam1`
transform, which is exactly the data the project established early on can be read out of
the camera device.

Evidence, per item, from the real pilot session `20260915-222256-5ef746`
----------------------------------------------------------------------
- per-eye intrinsics: 5x `foxglove.CameraCalibration` on `/ego/camera/{0,1}/intrinsics`
  -> 1920x1080, `distortion_model: kannala_brandt`, K, D (4 coefficients), `R = I`,
  `P = K` with a zero translation column.
- stereo relative pose: 5x `foxglove.FrameTransform` on `/ego/camera/1/extrinsics`
  -> parent `cam0`, child `cam1`, `|t| = 0.062047257 m`, byte-identical except the
  timestamp across the session.
- camera identity: stream-1 handshake -> `GILABS-ZJE0qSnG`, firmware `1.0.2`,
  hardware `audio_ego_v3`, serial `4ee9fe65efdf4f3e`, role `ego`.

What this module refuses to do
------------------------------
1. It never calls a device-reported value *measured*. Every value keeps
   `evidence: 'device_reported'`, and the report keeps `independently_verified: False`.
2. It does not decide which stream is the physical left eye. `cam0`/`cam1` are frame
   names, not eyes; that needs an operator observation (cover one lens) and stays
   `unknown` until one is recorded.
3. It does not turn the transform into a camera-to-glove relationship. The device states
   `cam0 <- cam1`; it says nothing about where the EMF base or the hand is.
4. It does not treat the device's unit as proven. Foxglove's convention is metres and
   0.062 m is a plausible stereo baseline, but that is a plausibility argument, not a
   measurement, so the unit assumption is named in the output.
5. It does not treat `R = I` / `P = K` as "rectified". With a fisheye distortion model
   and no rectification rotation, pinhole geometry must not be assumed for these streams.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path

EVIDENCE_DEVICE_REPORTED = 'device_reported'
STATUS_REPORTED = 'device_reported_unverified'
STATUS_NOT_RECEIVED = 'not_received'
STATUS_UNKNOWN = 'unknown'

# The device publishes per-eye intrinsics on these topic suffixes; the transform that
# carries the stereo relative pose is the one whose parent/child frames are the two eyes.
INTRINSICS_TOPIC_HINT = '/intrinsics'
EXTRINSICS_TOPIC_HINT = '/extrinsics'

# Recorded operator observation binding a stream to a physical eye. It is a file rather
# than a constant because the answer cannot be derived from the protocol.
EYE_ASSIGNMENT_RELPATH = 'calibration/eye-assignment.json'
PHYSICAL_EYES = ('left', 'right', 'unknown')


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(float(value))


def _topic_sort_key(topic):
    """Order streams by their numeric camera index when present, else by name."""
    digits = ''.join(ch for ch in str(topic) if ch.isdigit())
    return (int(digits) if digits else 10 ** 6, str(topic))


def stream_report(calibration):
    """Summarise one `foxglove.CameraCalibration` message without adding meaning to it.

    Only what the device stated is returned. `P`'s translation column is reported as
    zero rather than as a baseline, because for this device it is zero: the baseline
    lives in the separate FrameTransform message.
    """
    cal = dict(calibration or {})
    k = list(cal.get('K') or [])
    d = list(cal.get('D') or [])
    p = list(cal.get('P') or [])
    r = list(cal.get('R') or [])
    width, height = cal.get('width'), cal.get('height')
    out = {
        'evidence': EVIDENCE_DEVICE_REPORTED,
        'status': STATUS_REPORTED,
        'frame_id': cal.get('frame_id') or None,
        'width': width if isinstance(width, int) and width > 0 else None,
        'height': height if isinstance(height, int) and height > 0 else None,
        'distortion_model': cal.get('distortion_model') or None,
        'D': d or None,
        'K': k if len(k) == 9 else None,
        'R': r if len(r) == 9 else None,
        'P': p if len(p) == 12 else None,
        'K_complete': len(k) == 9,
        'R_is_identity': all(abs(r[3 * i + j] - (1.0 if i == j else 0.0)) < 1e-9
                             for i in range(3) for j in range(3)) if len(r) == 9 else None,
        'P_translation_zero': (p[3] == 0.0 and p[7] == 0.0 and p[11] == 0.0)
        if len(p) == 12 else None,
        'note': 'as received from the device; not independently measured here',
    }
    if out['K'] is not None:
        fx, fy, cx, cy = k[0], k[4], k[2], k[5]
        out.update(fx=fx, fy=fy, cx=cx, cy=cy,
                   square_pixel_ratio=round(fx / fy, 6) if fy else None,
                   horizontal_fov_deg=round(2.0 * math.degrees(math.atan(
                       (out['width'] / 2.0) / fx)), 3) if out['width'] and fx else None,
                   vertical_fov_deg=round(2.0 * math.degrees(math.atan(
                       (out['height'] / 2.0) / fy)), 3) if out['height'] and fy else None)
    else:
        out.update(fx=None, fy=None, cx=None, cy=None, square_pixel_ratio=None,
                   horizontal_fov_deg=None, vertical_fov_deg=None)
    if out['distortion_model'] and 'kannala' in str(out['distortion_model']).lower():
        out['geometry_model'] = 'fisheye_kannala_brandt_unrectified'
        out['geometry_note'] = ('a fisheye model with R = I and a zero P translation column: '
                                'these streams are NOT rectified, so pinhole/radtan geometry '
                                'must not be assumed')
    else:
        out['geometry_model'] = 'unclassified'
        out['geometry_note'] = 'the device did not name a model this module recognises'
    return out


def stream_reports(calibration):
    """All per-eye intrinsic messages, keyed by topic, in stream order."""
    rows = {}
    for topic, cal in sorted((calibration or {}).items(),
                             key=lambda kv: _topic_sort_key(kv[0])):
        if not isinstance(cal, dict):
            continue
        if INTRINSICS_TOPIC_HINT in str(topic) or cal.get('K'):
            rows[str(topic)] = stream_report(cal)
    return rows


def baseline_m(transform):
    """Magnitude of the reported translation, in the unit the device sent."""
    t = list((transform or {}).get('translation') or [])
    if len(t) != 3 or not all(_finite(v) for v in t):
        return None
    return math.sqrt(sum(float(v) ** 2 for v in t))


def rotation_angle_deg(rotation_xyzw):
    """Angle of a quaternion, in degrees, after normalising it.

    Reported next to the raw quaternion so a reader can see that the stereo pair is nearly
    co-oriented (0.55 deg here) without re-deriving it.
    """
    q = [float(v) for v in (rotation_xyzw or [])]
    if len(q) != 4 or not all(_finite(v) for v in q):
        return None
    norm = math.sqrt(sum(v * v for v in q))
    if norm <= 0.0:
        return None
    w = min(1.0, max(-1.0, abs(q[3] / norm)))
    return round(2.0 * math.degrees(math.acos(w)), 6)


def stereo_extrinsic(transforms):
    """The device's own `parent <- child` transform for the two camera frames.

    The transform whose parent or child names a camera frame is the stereo one; the same
    message type also carries the IMU mounting, and that one is reported separately.
    Nothing here decides the *direction* convention (child-in-parent vs parent-in-child):
    the magnitude is convention-free, the direction is not.
    """
    chosen = None
    for topic, value in sorted((transforms or {}).items()):
        if not isinstance(value, dict):
            continue
        parent = str(value.get('parent_frame_id') or '')
        child = str(value.get('child_frame_id') or '')
        if 'cam' not in parent.lower() and 'cam' not in child.lower():
            continue
        chosen = (str(topic), value)
        break
    if chosen is None:
        return {'status': STATUS_NOT_RECEIVED, 'evidence': EVIDENCE_DEVICE_REPORTED,
                'topic': None, 'parent_frame_id': None, 'child_frame_id': None,
                'baseline_m': None, 'translation_m': None, 'rotation_xyzw': None,
                'rotation_angle_deg': None,
                'reason': ('the device sent no camera-to-camera FrameTransform in this '
                           'session, so no device-reported baseline exists')}
    topic, value = chosen
    translation = [float(v) for v in (value.get('translation') or [])] or None
    rotation = [float(v) for v in (value.get('rotation') or [])] or None
    magnitude = baseline_m(value)
    return {
        'status': STATUS_REPORTED,
        'evidence': EVIDENCE_DEVICE_REPORTED,
        'topic': topic,
        'parent_frame_id': value.get('parent_frame_id') or None,
        'child_frame_id': value.get('child_frame_id') or None,
        'translation_m': translation,
        'baseline_m': None if magnitude is None else round(magnitude, 9),
        'rotation_xyzw': rotation,
        'rotation_angle_deg': rotation_angle_deg(rotation),
        'unit_assumption': 'metres',
        'unit_note': ('Foxglove FrameTransform is documented in metres and the magnitude is '
                      'a plausible stereo baseline; the device itself states no unit, so this '
                      'stays an assumption until an independent measurement agrees'),
        'direction_note': ('reported as parent <- child per the Foxglove schema; the opposite '
                           'reading is not excluded by the payload, and the two differ by the '
                           'sign of the baseline, which matters for depth direction'),
        'independently_verified': False,
    }


def imu_extrinsic(transforms):
    """The device's `cam0 <- imu0` transform, when it sent one (reported, not used yet)."""
    for topic, value in sorted((transforms or {}).items()):
        if not isinstance(value, dict):
            continue
        child = str(value.get('child_frame_id') or '')
        if 'imu' not in child.lower() and 'imu' not in str(topic).lower():
            continue
        return {'status': STATUS_REPORTED, 'evidence': EVIDENCE_DEVICE_REPORTED,
                'topic': str(topic),
                'parent_frame_id': value.get('parent_frame_id') or None,
                'child_frame_id': child or None,
                'translation_m': [float(v) for v in (value.get('translation') or [])] or None,
                'rotation_xyzw': [float(v) for v in (value.get('rotation') or [])] or None,
                'rotation_angle_deg': rotation_angle_deg(value.get('rotation')),
                'note': ('device-reported mounting of the camera IMU; not used by any '
                         'coordinate chain in this project yet')}
    return {'status': STATUS_NOT_RECEIVED, 'evidence': EVIDENCE_DEVICE_REPORTED,
            'reason': 'no IMU FrameTransform was received in this session'}


def calibration_id(identity, streams, stereo):
    """Content-addressed id over the *values the device reported*.

    Versioning rule: the id changes when a reported value, the camera serial or the
    firmware changes, and it does not change when the report is merely reformatted. It is a
    first-party id (the vendor exposes only `FactoryCalibrationStatus`, no id and no
    format), so a session can be tied to the calibration it was recorded under.
    """
    identity = identity or {}
    payload = {
        'serial': identity.get('serial'),
        'firmware': identity.get('firmware'),
        'hardware': identity.get('hardware'),
        'streams': {topic: {'frame_id': row.get('frame_id'), 'width': row.get('width'),
                            'height': row.get('height'),
                            'model': row.get('distortion_model'),
                            'K': row.get('K'), 'D': row.get('D')}
                    for topic, row in sorted((streams or {}).items())},
        'stereo': {'parent': (stereo or {}).get('parent_frame_id'),
                   'child': (stereo or {}).get('child_frame_id'),
                   'translation_m': (stereo or {}).get('translation_m'),
                   'rotation_xyzw': (stereo or {}).get('rotation_xyzw')},
    }
    if not payload['streams']:
        return None
    blob = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return 'camcal-' + hashlib.sha256(blob).hexdigest()[:16]


# --------------------------------------------------------------------------- eye check
def eye_assignment_path(output_root):
    return Path(output_root) / EYE_ASSIGNMENT_RELPATH


def read_eye_assignment(output_root):
    path = eye_assignment_path(output_root)
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_eye_assignment(output_root, assignment, *, now=None):
    """Record the operator's cover-one-lens observation, or explicitly record `unknown`.

    `assignment` maps a stream topic to `left` / `right` / `unknown`. A stream left out of
    the mapping is stored as `unknown`, never defaulted to an eye. This observation is what
    makes the physical-eye question answerable at all: the protocol only names `cam0` and
    `cam1`.
    """
    if not isinstance(assignment, dict) or not assignment:
        raise ValueError('eye assignment must be a non-empty mapping')
    topics = {}
    for topic, eye in assignment.items():
        eye = str(eye).lower()
        if eye not in PHYSICAL_EYES:
            raise ValueError('physical eye must be one of %s' % (PHYSICAL_EYES,))
        topics[str(topic)] = eye
    record = {
        'record_version': 1,
        'method': 'operator_observed_cover_one_lens',
        'method_note': ('the operator covers one lens and reports which stream loses its '
                        'image; this binds a stream index to a physical eye, which the '
                        'protocol does not state'),
        'streams': topics,
        'unknown_is_legal': True,
        'recorded_at_unix': float(now if now is not None else time.time()),
        'not_claimed': ['that the cover test resolves the FrameTransform direction',
                        'that any metric accuracy follows from it'],
    }
    path = eye_assignment_path(output_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    return record


# ------------------------------------------------------------------------- full report
def device_calibration_report(calibration, transforms, identity, *, eye_assignment=None,
                              output_root=None):
    """The calibration page's data, with the evidence class of every item stated."""
    streams = stream_reports(calibration)
    stereo = stereo_extrinsic(transforms)
    imu = imu_extrinsic(transforms)
    if eye_assignment is None and output_root is not None:
        eye_assignment = read_eye_assignment(output_root)
    recorded = {str(k): str(v).lower() for k, v in
                ((eye_assignment or {}).get('streams') or {}).items()}
    eye_rows = {topic: recorded.get(topic, STATUS_UNKNOWN) for topic in streams}
    eyes_resolved = bool(eye_rows) and all(
        value in ('left', 'right') for value in eye_rows.values())
    identity = identity or {}
    return {
        'schema': 'camera-device-calibration-report-v1',
        'evidence': EVIDENCE_DEVICE_REPORTED,
        'independently_verified': False,
        'identity': {
            'name': identity.get('name'), 'serial': identity.get('serial'),
            'firmware': identity.get('firmware'), 'hardware': identity.get('hardware'),
            'role': identity.get('role'),
            'note': 'device-reported at connect time (stream 1 handshake)',
        },
        'streams': streams,
        'stream_count': len(streams),
        'stereo_extrinsics': stereo,
        'imu_extrinsics': imu,
        'physical_eye': {
            'status': 'observed' if eyes_resolved else STATUS_UNKNOWN,
            'streams': eye_rows,
            'record': eye_assignment,
            'note': ('stream index -> physical eye is not stated by the protocol; it is bound '
                     'by the operator cover-one-lens observation, and stays unknown until one '
                     'is recorded'),
        },
        'calibration_id': calibration_id(identity, streams, stereo),
        'unverified_items': [
            'the physical left/right eye behind each stream index (needs the operator check)',
            'the direction convention of the reported transform (child-in-parent vs '
            'parent-in-child): the magnitude is convention-free, the sign is not',
            'the unit of the reported translation (metres assumed, not measured)',
            'any accuracy claim: a device-reported calibration is not a validation, and '
            'nothing here was checked against a board or against depth consistency',
            'whether the two streams are rectified (they are not: fisheye model with R = I)',
        ],
        'not_claimed': [
            'that the camera is calibrated to any stated accuracy',
            'that the glove/EMF frame is related to either camera frame',
            'that the reported baseline is independently confirmed',
        ],
    }


def wizard_steps(report, *, has_recording=False):
    """The operator wizard, generated from the report so it cannot drift from the state.

    Each step states what is required, who performs it, and what acceptance means. Steps
    that need a physical action say so; nothing here marks one complete on its own.
    """
    stereo = (report or {}).get('stereo_extrinsics') or {}
    eyes = (report or {}).get('physical_eye') or {}
    steps = [
        {'id': 'devices', 'title': '设备与左右目确认', 'owner': 'station',
         'state': 'done' if (report or {}).get('stream_count') else 'waiting_for_device',
         'detail': ('相机身份、逐目内参与设备自报外参在连接后自动读出；自报值不等于实测值'),
         'measured': {'streams': (report or {}).get('stream_count') or 0,
                      'baseline_m': stereo.get('baseline_m')}},
        {'id': 'eye_check', 'title': '遮住一只镜头，确认物理左右目', 'owner': 'operator',
         'state': 'done' if eyes.get('status') == 'observed' else 'needs_operator',
         'detail': '遮住一只镜头，观察哪一路画面变暗，然后在页面提交对应关系',
         'acceptance': '两路都记录为 left/right；unknown 不计为完成'},
        {'id': 'rest_baseline', 'title': '静置基线（手套，不接触物体）', 'owner': 'operator',
         'state': 'needs_operator',
         'detail': '点击「采集静置基线 · 3 秒」，结果写入会话 calibration.json'},
        {'id': 'time_sync', 'title': '工作站验收：视触延迟抽检', 'owner': 'operator',
         'state': 'needs_operator',
         'detail': ('首次验收、换设备或同步异常时做首／中／尾敲击；动作要同时出现在画面与压感上，相关性不足时'
                    '报 INCONCLUSIVE 而不是给一个数')},
        {'id': 'space_points', 'title': '空间对应点采样（标定板 / 已知尺寸夹具）', 'owner': 'operator',
         'state': 'blocked_on_user_material',
         'detail': ('需要标定板实际尺寸或已知间距的夹具；尺度依据必须由用户确认，'
                    '不接受「叠加图看起来对齐」')},
        {'id': 'held_out', 'title': '独立留出点验证', 'owner': 'execution',
         'state': 'blocked_on_space_points',
         'detail': '用未参与拟合的点报告留出误差，与拟合残差分开报告'},
        {'id': 'short_trial', 'title': '短会话试采 → 回放 → QC → loader', 'owner': 'station',
         'state': 'ready' if has_recording else 'needs_recording',
         'detail': ('原始数据先保留；派生数据可重生成。未标定也可以保存原始试采，'
                    '但不会自动标为 train_ready')},
    ]
    return {'steps': steps,
            'completed': sum(1 for step in steps if step['state'] == 'done'),
            'operator_actions': [step['id'] for step in steps
                                 if step['state'] == 'needs_operator'],
            'note': ('wizard state is derived from the device and the recorded files; no step '
                     'is marked done by having a button pressed without its acceptance')}
