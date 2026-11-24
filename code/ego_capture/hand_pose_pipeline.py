"""Camera-frame hand-pose pipeline: one transform convention, three views, unified sources.

Why this module exists
----------------------
The web pose visual renders the OpenXR-25 skeleton in ``wrist_local`` only. Two things were
missing and are settled here as code, not prose:

1. A single, fixed backend transform convention so the camera frame is shown and recorded
   consistently: ``p_cam0 = R_cam0_from_wrist @ p_wrist + t`` (child-in-parent: the rotation
   maps wrist-local directions into the cam0 frame, the translation is the wrist origin
expressed in cam0). The frontend, if it re-implements the transform, must reproduce this
   exact convention; the tests assert the two agree.
2. Three honest views of the same skeleton: wrist_local (hand shape), cam0 3-D (camera origin
   plus the hand's translation), and stereo image overlay (each node projected through the
   device's kannala_brandt fisheye model, never a pinhole).

Refusals encoded as code
------------------------
* A missing transform stays missing. There is no identity fallback, so a session without a
  fitted ``T_cam<-wrist`` can never pretend the wrist is at the camera origin.
* A rotation that is not orthonormal with determinant +1 (a reflection or a scaled matrix)
  is rejected, and a zero quaternion is rejected.
* Fisheye projection uses the device-reported kannala_brandt model only; points behind or on
  the image plane are flagged invalid, not clamped.
* Registration sources (``replay_registration``, ``stereo_landmark_registration``,
  ``fiducial_marker``, ``synthetic_registration``) share one record shape with the same
  direction, unit, time and quality fields; switching sources must be recorded, never silent.
* Time correspondence is established by clock proximity with a documented delta, never by
  array position and never by "the same poll returned both".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

import ego_capture.hand_openxr_tracking as tracking
from visuotactile import skeleton_stream

# ---------------------------------------------------------------- fixed conventions

FRAME_CONVENTION = ('p_cam0 = R_cam0_from_wrist @ p_wrist + t (child-in-parent: R maps '
                    'wrist-local directions into cam0, t is the wrist origin expressed in '
                    'cam0)')
QUATERNION_ORDER = 'xyzw'
POSITION_UNIT = 'm'

REGISTRATION_SOURCES = (
    'replay_registration',
    'stereo_landmark_registration',
    'fiducial_marker',
    'synthetic_registration',
)


class TransformValidationError(ValueError):
    """A transform record that cannot be interpreted as a rigid camera-frame pose."""


# ---------------------------------------------------------------- transform validation

def rotation_matrix_3x3(matrix) -> np.ndarray:
    """Return a 3x3 rotation, or raise. Matrices are finite, orthonormal, determinant +1.

    A reflection (det -1) or a scaled/affine matrix is rejected, not silently corrected.
    """
    m = np.asarray(matrix, dtype=float)
    if m.shape != (3, 3):
        raise TransformValidationError(f'a rotation must be 3x3, got {m.shape}')
    if not np.all(np.isfinite(m)):
        raise TransformValidationError('a rotation matrix contains a non-finite entry')
    if not np.allclose(m @ m.T, np.eye(3), atol=1e-4, rtol=1e-4):
        raise TransformValidationError('the rotation matrix is not orthonormal')
    if not np.isclose(np.linalg.det(m), 1.0, atol=1e-4):
        raise TransformValidationError('the rotation matrix is a reflection or scaled'
                                       ' (determinant != +1)')
    return m


def transform_from_record(record: dict) -> dict:
    """Interpret a transform record (as written by ``RigidFit.as_record``) into a validated pose.

    Accepts either a flat 9-element ``rotation`` or a ``quaternion_xyzw``. The returned
    ``matrix`` and ``translation_m`` are the authoritative backend values; the quaternion order
    and the convention travel with the record for any consumer that needs them.
    """
    if not isinstance(record, dict):
        raise TransformValidationError('a transform record must be a dict')
    rotation = record.get('rotation')
    if rotation is None and record.get('quaternion_xyzw') is not None:
        rotation = list(tracking.quaternion_xyzw_to_matrix(record['quaternion_xyzw']).reshape(-1))
    if rotation is None:
        raise TransformValidationError('the transform record carries no rotation and no '
                                       'quaternion_xyzw')
    if len(list(rotation)) != 9:
        raise TransformValidationError(f'a flat rotation must have 9 entries, got {len(rotation)}')
    matrix = rotation_matrix_3x3(np.asarray(rotation, dtype=float).reshape(3, 3))
    translation = list(record.get('translation_m') or [])
    if len(translation) != 3:
        raise TransformValidationError('the transform record carries no 3-vector translation_m')
    translation = np.asarray(translation, dtype=float)
    if not np.all(np.isfinite(translation)):
        raise TransformValidationError('the translation contains a non-finite entry')
    return {'matrix': matrix,
            'translation_m': translation,
            'parent_frame': str(record.get('parent_frame') or 'cam0'),
            'child_frame': str(record.get('child_frame') or 'wrist_local'),
            'convention': FRAME_CONVENTION,
            'quaternion_order': QUATERNION_ORDER,
            'position_unit': POSITION_UNIT,
            'source': record.get('source'),
            'is_constant_extrinsic': record.get('is_constant_extrinsic', False)}


def apply_to_wrist(points_wrist_local, transform: dict) -> np.ndarray:
    """Map wrist-local points into cam0 with the fixed convention.

    ``transform`` is the validated dict from ``transform_from_record`` (or an equivalent with
    ``matrix`` / ``translation_m``).
    """
    matrix = transform.get('matrix')
    if matrix is None:
        matrix = transform.get('rotation')
    matrix = matrix if isinstance(matrix, np.ndarray) else rotation_matrix_3x3(matrix)
    translation = transform.get('translation_m')
    if translation is None:
        translation = transform.get('translation')
    translation = np.asarray(translation, dtype=float)
    points = np.asarray(points_wrist_local, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError(f'points_wrist_local must have shape (N, 3), got {points.shape}')
    if not np.all(np.isfinite(points)):
        raise ValueError('points_wrist_local contains a non-finite entry')
    return (matrix @ points.T).T + translation


def apply_to_orientations(orientations_xyzw, transform: dict) -> list:
    """Rotate node quaternions by the transform's rotation, so node orientation follows.

    Node orientation, when displayed, must compose with the frame rotation; rotating only the
    positions would leave the node frames pointing along the wrist-local axes.
    """
    matrix = transform['matrix'] if isinstance(transform.get('matrix'), np.ndarray)\
        else rotation_matrix_3x3(transform.get('rotation'))
    rotated = []
    for quaternion in orientations_xyzw:
        if quaternion is None or len(quaternion) != 4:
            rotated.append(None)
            continue
        node_matrix = tracking.quaternion_xyzw_to_matrix(quaternion)
        composed = matrix @ node_matrix
        rotated.append(list(tracking.matrix_to_quaternion_xyzw(composed)))
    return rotated


def compose(transform_parent_child: dict, transform_child_grandchild: dict) -> dict:
    """Compose two rigid transforms ``T_p<-c`` and ``T_c<-g`` into ``T_p<-g``.

    Used for the fiducial path: ``T_cam0<-marker @ T_marker<-wrist``. The two inputs use the
    same convention, so the composition is a plain matrix/vector product; nothing is assumed
    about coincident origins.
    """
    matrix_a = transform_parent_child['matrix'] if isinstance(transform_parent_child.get('matrix'),
                                                               np.ndarray)\
        else rotation_matrix_3x3(transform_parent_child.get('rotation'))
    matrix_b = transform_child_grandchild['matrix'] if isinstance(
        transform_child_grandchild.get('matrix'), np.ndarray)\
        else rotation_matrix_3x3(transform_child_grandchild.get('rotation'))
    translation_a = np.asarray(transform_parent_child['translation_m'], dtype=float)
    translation_b = np.asarray(transform_child_grandchild['translation_m'], dtype=float)
    return {'matrix': matrix_a @ matrix_b,
            'translation_m': matrix_a @ translation_b + translation_a,
            'convention': FRAME_CONVENTION,
            'is_constant_extrinsic': False}


# ---------------------------------------------------------------- camera projection

def project_into_camera(points_cam0, camera: tracking.FisheyeCamera) -> tuple:
    """Project cam0-frame 3-D points to pixels through the device fisheye model.

    Returns ``(pixels, valid)``: a point behind or on the image plane is flagged invalid rather
    than clamped, so a hand that has moved out of view is shown as lost, not glued to an edge.
    """
    points = np.asarray(points_cam0, dtype=float)
    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError(f'points_cam0 must have shape (N, 3), got {points.shape}')
    valid = np.all(np.isfinite(points), axis=1) & (points[:, 2] > 0.0)
    pixels = np.full((points.shape[0], 2), np.nan, dtype=float)
    if np.any(valid):
        pixels[valid] = tracking.project_points(points[valid], camera)
    return pixels, valid.tolist()


def project_wrist_to_stereo(points_wrist_local, transform: dict,
                            stereo: tracking.StereoGeometry) -> dict:
    """Map wrist-local points to cam0 pixels and cam1 pixels through the fisheye model."""
    points_cam0 = apply_to_wrist(points_wrist_local, transform)
    points_cam1 = stereo.to_cam1(points_cam0)
    pixels0, valid0 = project_into_camera(points_cam0, stereo.camera0)
    pixels1, valid1 = project_into_camera(points_cam1, stereo.camera1)
    return {'points_cam0_m': [list(row) for row in points_cam0],
            'cam0_px': [[None if not valid0[i] else float(pixels0[i, 0]),
                         None if not valid0[i] else float(pixels0[i, 1])]
                        for i in range(points_cam0.shape[0])],
            'cam1_px': [[None if not valid1[i] else float(pixels1[i, 0]),
                         None if not valid1[i] else float(pixels1[i, 1])]
                        for i in range(points_cam0.shape[0])],
            'valid': valid0}


# ---------------------------------------------------------------- session geometry

def session_dir(session_id: str, sessions_root: Path) -> Path:
    path = Path(sessions_root) / session_id
    if not path.is_dir():
        raise ValueError(f'no session directory: {path}')
    return path


def read_session_calibration(path: Path) -> dict:
    file = path / 'calibration.json'
    if not file.is_file():
        raise ValueError(f'{file} is missing: the session did not record a calibration')
    return json.loads(file.read_text(encoding='utf-8'))


def stereo_from_session_calibration(calibration: dict, scale: float) -> tracking.StereoGeometry:
    """Build the stereo geometry from the device-reported values in a calibration.json."""
    reported = calibration.get('reported') or {}
    intrinsics0 = reported.get('/ego/camera/0/intrinsics')
    intrinsics1 = reported.get('/ego/camera/1/intrinsics')
    if not intrinsics0 or not intrinsics1:
        raise ValueError('calibration.json carries no device-reported intrinsics for both eyes')
    camera0 = tracking.FisheyeCamera.from_intrinsics('cam0', intrinsics0, scale=scale)
    camera1 = tracking.FisheyeCamera.from_intrinsics('cam1', intrinsics1, scale=scale)
    transform = _stereo_transform_from_calibration(calibration)
    if transform is None:
        raise ValueError('calibration.json carries no device-reported cam0<-cam1 transform')
    return tracking.StereoGeometry.from_device_transform(camera0, camera1, transform)


def _stereo_transform_from_calibration(calibration: dict) -> Optional[dict]:
    """Find the device-reported camera-to-camera transform inside a calibration record.

    The station stores the stereo extrinsic alongside calibration; the transform whose parent or
    child names a camera frame is the one that carries the baseline.
    """
    for key in ('stereo_extrinsic', 'cam0_cam1', 'frame_transform', 'transform'):
        candidate = calibration.get(key)
        if isinstance(candidate, dict):
            return candidate
    reported = calibration.get('reported') or {}
    for value in reported.values():
        if isinstance(value, dict) and 'translation' in value and 'rotation' in value:
            parent = str(value.get('parent_frame_id') or value.get('parent') or '')
            child = str(value.get('child_frame_id') or value.get('child') or '')
            if 'cam' in parent.lower() or 'cam' in child.lower():
                return value
    return None


# ---------------------------------------------------------------- replay loading

def load_skeleton_records(session_path: Path):
    file = session_path / 'openxr_skeleton.jsonl'
    if not file.is_file():
        return []
    return skeleton_stream.read_records(file)


def load_transform_series(report_path: Optional[Path]) -> dict:
    """Read the per-sample ``T_cam<-wrist(t)`` series out of a tracking report.

    Each sample carries the transform record (or ``ok: False`` with a ``failure``), its timestamps
    and matched nodes. Two report shapes are recognised: a ``samples`` list (one entry per fitted
    frame, written when the OpenXR half actually ran) and the OpenXR-half summary block
    (``openxr``) that a report carries when the skeleton stream was absent. The second case is
    reported as a precise missing-transform reason rather than a silently empty series, so a
    report that only ran the stereo half cannot be mistaken for a fitted camera pose.
    """
    if report_path is None or not Path(report_path).is_file():
        return {'samples': [], 'source': None, 'sample_count': 0, 'missing': True,
                'reason': 'no transform report was provided or found'}
    report = json.loads(Path(report_path).read_text(encoding='utf-8'))
    samples = report.get('samples') or []
    openxr = report.get('openxr') or {}
    series = {'samples': samples,
              'source': report.get('label') or report.get('mode'),
              'sample_count': len(samples),
              'missing': not samples,
              'evidence': report.get('evidence') or 'self_reproduced',
              'is_constant_extrinsic': report.get('is_constant_extrinsic', False),
              'openxr_status': openxr.get('status'),
              'openxr_reason': openxr.get('reason'),
              'openxr_record_count': openxr.get('record_count')}
    if not samples:
        if openxr.get('status') == 'not_run':
            series['reason'] = ('this report ran the stereo half only: its OpenXR half is '
                                'not_run, so no T_cam<-wrist(t) series exists ('
                                + str(openxr.get('reason') or 'no skeleton stream') + ')')
        else:
            series['reason'] = 'the report carries no per-sample transform series'
    return series


def pair_skeletons_to_transforms(records, samples, *, max_delta_s=0.05) -> dict:
    """Join skeleton records to fitted transforms by host_mono_ns, with an explicit delta."""
    left = [{'host_mono_ns': sample.get('timestamps', {}).get('host_mono_ns'),
             'index': index} for index, sample in enumerate(samples)]
    pairing = tracking.pair_by_timestamp(left, records, left_time='host_mono_ns',
                                         right_time='host_mono_ns', max_delta_s=max_delta_s)
    return pairing


def three_views(skeleton_record: dict, transform: dict | None,
                stereo: tracking.StereoGeometry | None) -> dict:
    """Build the three-view payload for one skeleton sample.

    ``transform`` is a validated dict from ``transform_from_record``; ``None`` means "missing".
    The cam0/cam1 views are produced only when both the transform and the stereo geometry exist.
    """
    names = list(skeleton_record.get('names') or [])
    positions = skeleton_record.get('positions_local') or []
    orientations = skeleton_record.get('orientations_local_xyzw') or []
    views = {
        'wrist_local': {'names': names,
                        'positions_local': [list(p) for p in positions],
                        'orientations_local_xyzw': [list(q) if q else None for q in orientations],
                        'coordinate_frame': skeleton_stream.COORDINATE_FRAME,
                        'position_unit': skeleton_stream.POSITION_UNIT,
                        'quaternion_order': skeleton_stream.QUATERNION_ORDER},
        'cam0': None,
        'stereo_overlay': None,
    }
    if transform is None or stereo is None:
        views['cam0'] = {'available': False, 'reason': _missing_reason(transform, stereo)}
        views['stereo_overlay'] = {'available': False,
                                   'reason': _missing_reason(transform, stereo)}
        return views
    points_wrist = np.asarray(positions, dtype=float).reshape(-1, 3)
    points_cam0 = apply_to_wrist(points_wrist, transform)
    projection = project_wrist_to_stereo(points_wrist, transform, stereo)
    views['cam0'] = {'available': True,
                     'positions_cam0_m': [list(row) for row in points_cam0],
                     'orientations_cam0_xyzw': apply_to_orientations(orientations, transform),
                     'camera_origin_m': [0.0, 0.0, 0.0],
                     'wrist_origin_cam0_m': list(points_cam0[name_index('wrist', names)])
                     if 'wrist' in names else None,
                     'convention': FRAME_CONVENTION}
    views['stereo_overlay'] = {'available': True, **projection}
    return views


def name_index(name: str, names: list) -> int:
    try:
        return names.index(name)
    except ValueError:
        return 0


def _missing_reason(transform, stereo) -> str:
    if transform is None:
        return ('no fitted T_cam<-wrist(t): a missing transform is shown as missing, never as '
                'the identity')
    if stereo is None:
        return 'no device-reported stereo geometry'
    return 'unavailable'


# ---------------------------------------------------------------- synthetic fixture

# ---------------------------------------------------------------- canonical synthetic hand
# The fixture's hand shape. Synthetic and hand-authored, never a measurement of a real hand: the
# numbers are the typical-adult proportions the stereo-tracking experiment already uses for its own
# synthetic hand (tools/openxr_stereo_tracking_experiment.py::synthetic_hand), kept in that hand's
# own frame here (fingers +y, thumb +x, palm out-of-plane -z) and re-expressed below. Reusing one
# set of proportions keeps the repository from growing a second, silently different synthetic hand.
_CANONICAL_HAND_FINGERS_ALONG_Y = {
    'thumb': {'metacarpal': (0.028, 0.008, 0.000), 'proximal': (0.042, 0.022, 0.002),
              'distal': (0.056, 0.036, 0.004), 'tip': (0.064, 0.045, 0.005)},
    'index': {'metacarpal': (0.022, 0.012, -0.002), 'proximal': (0.024, 0.085, -0.002),
              'intermediate': (0.026, 0.128, -0.001), 'distal': (0.027, 0.152, 0.000),
              'tip': (0.028, 0.168, 0.001)},
    'middle': {'metacarpal': (0.006, 0.012, -0.002), 'proximal': (0.006, 0.090, -0.002),
               'intermediate': (0.006, 0.137, -0.001), 'distal': (0.006, 0.163, 0.000),
               'tip': (0.006, 0.180, 0.001)},
    'ring': {'metacarpal': (-0.010, 0.012, -0.002), 'proximal': (-0.010, 0.083, -0.002),
             'intermediate': (-0.010, 0.126, -0.001), 'distal': (-0.010, 0.149, 0.000),
             'tip': (-0.010, 0.164, 0.001)},
    'little': {'metacarpal': (-0.026, 0.012, -0.002), 'proximal': (-0.026, 0.072, -0.002),
               'intermediate': (-0.026, 0.108, -0.001), 'distal': (-0.026, 0.127, 0.000),
               'tip': (-0.026, 0.140, 0.001)},
}


def canonical_rest_pose() -> dict:
    """The fixture hand as ``{node: [x, y, z]}`` in ``wrist_local`` metres.

    Synthetic and hand-authored. ``_CANONICAL_HAND_FINGERS_ALONG_Y`` is re-expressed in this
    model's own frame by the rotation that maps that hand's axes onto it: this model runs +z along
    the fingers (wrist 0 -> extended middle tip ~0.18 m), +y towards the thumb, and its palm faces
    -x (the live model's thumb metacarpal sits at x < 0, i.e. on the palm side), so a point maps
    ``(x, y, z) -> (z, x, y)``. That is a rotation of one hand, not a second set of proportions.

    The shape is fixed and deterministic on purpose. It is a readable stand-in for the software
    chain (transform -> fisheye projection -> viewport); it is not a hand measurement, and the
    node order is ``skeleton_stream.NODE_NAMES`` so the three views stay index-aligned.
    """
    pose = {'wrist': [0.0, 0.0, 0.0]}  # wrist at the origin by definition
    for finger, joints in _CANONICAL_HAND_FINGERS_ALONG_Y.items():
        for joint, (x, y, z) in joints.items():
            pose['%s_%s' % (finger, joint)] = [z, x, y]
    missing = [name for name in skeleton_stream.NODE_NAMES if name not in pose]
    if missing:
        raise ValueError('the canonical hand is missing nodes: %s' % missing)
    return {name: pose[name] for name in skeleton_stream.NODE_NAMES}


def synthetic_replay_fixture():
    """A self-contained replay for tests and the demo, clearly labelled synthetic.

    Builds a wrist-local skeleton, a known ``T_cam<-wrist``, and a stereo geometry from the real
    device numbers, then returns the three views plus the ground-truth pose. This exercises the
    end-to-end path without a recorded session; it is not evidence about the hardware.

    The hand is ``canonical_rest_pose()``, and that is the point of the fixture: a *shape* has to be
    projected for the chain to be checkable at all. It used to be Gaussian noise per node, which
    made the cam0 viewport unreadable (consecutive joints 162 mm apart on a 100 mm thumb, middle tip
    95 mm from the wrist) and left every projection check meaningless, because a random cloud can be
    projected anywhere: it could neither pass nor fail the chain it exists to exercise.
    """
    names = list(skeleton_stream.NODE_NAMES)
    pose = canonical_rest_pose()
    positions_local = np.array([pose[name] for name in names], dtype=float)
    quaternion = (0.15, -0.25, 0.05, 0.95)
    matrix = tracking.quaternion_xyzw_to_matrix(quaternion)
    translation = np.array([0.03, 0.04, 0.45])
    transform = {'matrix': matrix, 'translation_m': translation,
                 'convention': FRAME_CONVENTION, 'source': 'synthetic_registration',
                 'is_constant_extrinsic': False,
                 'quaternion_xyzw': list(quaternion)}
    camera = tracking.FisheyeCamera(name='cam0', width=1920, height=1080,
                                    K=REAL_K, D=REAL_D, scale=1.0)
    stereo = tracking.StereoGeometry(camera0=camera, camera1=camera,
                                     rotation_cam0_cam1=(1.0, 0.0, 0.0,
                                                         0.0, 1.0, 0.0,
                                                         0.0, 0.0, 1.0),
                                     translation_cam0_cam1_m=(-REAL_BASELINE_M, 0.0, 0.0))
    orientations = [[0.0, 0.0, 0.0, 1.0] for _ in names]  # identity (x,y,z,w)
    record = {'names': names, 'positions_local': [list(p) for p in positions_local],
              'orientations_local_xyzw': orientations, 'hand_side': 'left',
              'bridge_timestamp_sec': 0.0, 'host_mono_ns': 0}
    views = three_views(record, transform, stereo)
    return {'record': record, 'transform': transform, 'stereo': stereo, 'views': views,
            'ground_truth': {'rotation': list(matrix.reshape(-1)),
                             'translation_m': list(translation)},
            'evidence': 'synthetic',
            'label': 'synthetic-hand-pose-replay'}


# Real device cam0 intrinsics, reused from the tracking test fixtures so the projection
# checks run against the numbers the camera actually sent (pilot session).
REAL_K = (747.7525910100658, 0.0, 978.7595140515151,
          0.0, 747.7243263333199, 523.8922314746137,
          0.0, 0.0, 1.0)
REAL_D = (-0.027552895959142093, -0.022449663831563247,
          0.031956948956912964, -0.01558984753939523)
REAL_BASELINE_M = 0.062047257


# ---------------------------------------------------------------- report-sourced geometry

def camera_from_record(name: str, record: dict) -> tracking.FisheyeCamera:
    """Rebuild a ``FisheyeCamera`` from a recorded ``FisheyeCamera.as_record()`` dict.

    A report stores both eyes' intrinsics, so a replay can be rebuilt from the report alone. The
    distortion model is checked here too: a stream that is not kannala_brandt must not be fed to
    the fisheye projection.
    """
    entry = dict(record or {})
    K = list(entry.get('K') or [])
    D = list(entry.get('D') or [])
    if len(K) != 9 or len(D) != 4:
        raise ValueError(f'{name}: the recorded camera needs 9 K values and 4 D values, got '
                         f'{len(K)} and {len(D)}')
    model = str(entry.get('distortion_model') or tracking.KANNALA_BRANDT)
    if 'kannala' not in model.lower():
        raise ValueError(f'{name}: the recorded distortion_model {model!r} is not kannala_brandt, '
                         'so this pipeline has no model for the stream')
    return tracking.FisheyeCamera(
        name=name, width=int(entry.get('width') or 0), height=int(entry.get('height') or 0),
        K=tuple(float(v) for v in K), D=tuple(float(v) for v in D),
        scale=float(entry.get('scale', 1.0)), distortion_model=model,
        evidence=str(entry.get('evidence') or 'device_reported'),
        independently_verified=bool(entry.get('independently_verified', False)))


def stereo_from_report(report: dict) -> tuple:
    """Rebuild the stereo geometry from a tracking report's recorded ``stereo_geometry``.

    Returns ``(stereo, provenance)``. The report is the authoritative replay source: it carries
    both eyes' intrinsics and the device-reported cam0<-cam1 transform, so replaying a report does
    not depend on the session directory (calibration.json / camera.mcap) being present.
    """
    record = report.get('stereo_geometry') or report.get('stereo_geometry_used')
    if not isinstance(record, dict):
        return None, {'available': False,
                      'reason': 'the report carries no stereo_geometry record'}
    camera0 = camera_from_record('cam0', record.get('camera0') or {})
    camera1 = camera_from_record('cam1', record.get('camera1') or {})
    rotation = list(record.get('rotation_cam0_cam1') or [])
    translation = list(record.get('translation_cam0_cam1_m') or [])
    if len(rotation) != 9 or len(translation) != 3:
        raise ValueError('the recorded stereo_geometry needs a 9-value rotation and a 3-value '
                         'translation_cam0_cam1_m')
    stereo = tracking.StereoGeometry(
        camera0=camera0, camera1=camera1,
        rotation_cam0_cam1=tuple(float(v) for v in rotation),
        translation_cam0_cam1_m=tuple(float(v) for v in translation),
        source=str(record.get('source') or 'device_reported_frame_transform'),
        independently_verified=bool(record.get('independently_verified', False)))
    return stereo, {'available': True,
                    'source': stereo.source,
                    'evidence': record.get('evidence') or 'device_reported',
                    'independently_verified': stereo.independently_verified,
                    'baseline_m': stereo.baseline_m,
                    'convention': record.get('convention'),
                    'camera0': {'width': camera0.width, 'height': camera0.height,
                                'scale': camera0.scale, 'fx': camera0.fx, 'fy': camera0.fy,
                                'distortion_model': camera0.distortion_model},
                    'camera1': {'width': camera1.width, 'height': camera1.height,
                                'scale': camera1.scale, 'fx': camera1.fx, 'fy': camera1.fy,
                                'distortion_model': camera1.distortion_model}}


# ---------------------------------------------------------------- web payloads

def to_jsonable(value):
    """Recursively convert numpy scalars/arrays so a payload survives ``json.dumps``.

    Non-finite floats become ``None``: a point behind the camera has no pixel, so it is reported as
    "no value" rather than as a number the frontend could plot.
    """
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, np.ndarray):
        return [to_jsonable(v) for v in value.tolist()]
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if np.isfinite(number) else None
    return value


def contract_report() -> dict:
    """The fixed data contract, so the frontend can display what it is actually rendering.

    Purely descriptive: it names the convention, the quaternion order, the unit, the projection
    model and the allowed registration sources. It carries no measurement of its own.
    """
    return {'frame_convention': FRAME_CONVENTION,
            'quaternion_order': QUATERNION_ORDER,
            'position_unit': POSITION_UNIT,
            'projection_model': ('device-reported kannala_brandt fisheye; a point behind or on the '
                                 'image plane is invalid, never clamped'),
            'registration_sources': list(REGISTRATION_SOURCES),
            'missing_transform_policy': ('a missing T_cam<-wrist stays missing: no identity '
                                         'fallback and no reuse of a previous sample'),
            'time_pairing': ('host_mono_ns proximity with an explicit max_delta_s, never array '
                             'order'),
            'camera_views': ['wrist_local', 'cam0', 'stereo_overlay']}


def fixture_payload(fixture: dict) -> dict:
    """Self-contained payload for the synthetic three-view endpoint.

    The ``evidence``/``notice`` fields keep the synthetic label attached to the numbers: this
    exercises the software chain and is not hardware, calibration or accuracy evidence.
    """
    stereo = fixture['stereo']
    return to_jsonable({
        'label': fixture['label'],
        'evidence': fixture['evidence'],
        'notice': ('合成 fixture：只验证 wrist_local -> cam0 -> 左/右眼鱼眼投影这条软件链路，'
                   '不是硬件、标定或精度证据。'),
        'convention': FRAME_CONVENTION,
        'quaternion_order': QUATERNION_ORDER,
        'position_unit': POSITION_UNIT,
        'registration_source': 'synthetic_registration',
        'stereo': {'baseline_m': stereo.baseline_m,
                   'camera0': stereo.camera0.as_record(),
                   'camera1': stereo.camera1.as_record(),
                   'source': stereo.source},
        'views': fixture['views'],
        'ground_truth': fixture['ground_truth'],
    })


def replay_report_payload(report_path: Optional[Path]) -> dict:
    """Replay payload for a recorded tracking report: geometry, series status and views.

    A report whose OpenXR half is ``not_run`` returns an explicit missing-transform status: the
    wrist_local view is still described, the cam0/stereo views say they are unavailable and name
    the missing stream. Nothing is substituted with an identity pose, and the stereo geometry that
    the report does carry is still reconstructed so the projection path can be inspected.
    """
    if report_path is None or not Path(report_path).is_file():
        return {'available': False, 'report_path': None,
                'reason': 'no tracking report was provided or found',
                'hint': 'pass report=<path> relative to the code root, or record a session first'}
    report = json.loads(Path(report_path).read_text(encoding='utf-8'))
    stereo, stereo_provenance = stereo_from_report(report)
    series = load_transform_series(report_path)
    openxr = report.get('openxr') or {}
    payload = {
        'available': True,
        'report_path': str(report_path),
        'label': report.get('label') or report.get('mode'),
        'evidence': report.get('evidence'),
        'session': report.get('session'),
        'session_dir': report.get('session_dir'),
        'verdict': report.get('verdict'),
        'limitations': report.get('limitations'),
        'acceptance': report.get('acceptance'),
        'audit': report.get('audit'),
        'openxr_correspondence': report.get('openxr_correspondence'),
        'frames': _frames_summary(report.get('frames')),
        'stereo': stereo_provenance,
        'openxr': {'status': openxr.get('status'), 'reason': openxr.get('reason'),
                   'record_count': openxr.get('record_count'),
                   'how_to_record': openxr.get('how_to_record')},
        'camera_frame_landmarks': report.get('camera_frame_landmarks'),
        'transform_series': {'available': not series['missing'],
                             'sample_count': series['sample_count'],
                             'source': series.get('source'),
                             'evidence': series.get('evidence'),
                             'reason': series.get('reason')},
        'registration_source': ('replay_registration' if not series['missing'] else None),
        'views': None,
    }
    if not series['missing']:
        payload['views'] = _views_for_first_sample(series, stereo)
    return to_jsonable(payload)


def _frames_summary(frames) -> dict:
    """Describe a report's ``frames`` without assuming a shape.

    A recorded report stores a *list* of stereo detection frames; the synthetic report stores a
    *named dict* of scenario frames. Reporting the shape and the first keys lets the panel show
    what it is looking at instead of guessing, and keeps a dict-shaped report from being read as
    an empty series.
    """
    if isinstance(frames, list):
        first = frames[0] if frames and isinstance(frames[0], dict) else {}
        return {'shape': 'list', 'count': len(frames), 'first_keys': sorted(first)}
    if isinstance(frames, dict):
        return {'shape': 'dict', 'count': len(frames), 'names': sorted(frames)}
    return {'shape': None, 'count': 0, 'first_keys': [], 'names': []}


def _views_for_first_sample(series: dict, stereo) -> dict:
    """Three views for the first fitted sample of a report's transform series.

    The report's sample carries the fitted transform; the matched wrist-local skeleton has to come
    from the session's ``openxr_skeleton.jsonl`` (the report stores only the matched subset). When
    the skeleton is not attached, the transform is reported without inventing a hand shape.
    """
    sample = (series.get('samples') or [None])[0]
    if not isinstance(sample, dict):
        return {'available': False, 'reason': 'the series carries no sample'}
    record = sample.get('skeleton_record') or {}
    if not sample.get('ok') or not sample.get('transform'):
        return {'available': False,
                'reason': ('the first sample carries no fitted transform: '
                           + str(sample.get('failure') or 'unknown failure')),
                'timestamps': sample.get('timestamps')}
    try:
        transform = transform_from_record(sample['transform'])
    except TransformValidationError as exc:
        return {'available': False,
                'reason': f'the first sample transform is invalid: {exc}',
                'timestamps': sample.get('timestamps')}
    if not record:
        return {'available': False,
                'reason': ('the report carries a transform but not the matched wrist-local '
                           'skeleton; pair it with the session openxr_skeleton.jsonl to render '
                           'the hand'),
                'timestamps': sample.get('timestamps'),
                'transform': to_jsonable({'rotation': list(transform['matrix'].reshape(-1)),
                                          'translation_m': list(transform['translation_m'])})}
    return {'available': True,
            'views': three_views(record, transform, stereo),
            'timestamps': sample.get('timestamps'),
            'matched_node_count': sample.get('matched_node_count')}