"""Camera-frame hand tracking from fisheye stereo plus the OpenXR-25 wrist-local skeleton.

Scope, and what this module refuses to do
-----------------------------------------
The wrist-local skeleton gives 25 nodes in a frame attached to the wrist, with no world pose.
Turning it into camera-frame positions therefore needs an *estimate per frame*, not a stored
constant: the camera is head-mounted and the wrist moves independently, so
``T_cam<-wrist`` is a function of time. This module implements that estimate:

    OpenXR-25 wrist_local  --(anatomical correspondence by NAME)-->  MediaPipe-21 landmarks
    MediaPipe-21 in cam0 and cam1  --(fisheye-aware triangulation)-->  camera-frame 3-D
    camera-frame 3-D + wrist_local  --(robust 3-D-3-D registration)-->  T_cam<-wrist(t)

and reports the residuals, the inlier set and the visibility of every frame.

Refusals encoded as code, not as prose:

* **No IMU position.** ``assert_position_source_allowed`` rejects any attempt to label a
  position as coming from IMU double integration, and no record in this module has an IMU
  field.
* **No wrist_local differences as travel.** ``reject_wrist_local_travel_claim`` raises: two
  ``positions_local`` samples are expressed in two different (moving) wrist frames, so their
  difference is not a displacement in any fixed frame.
* **No array-index matching.** Correspondences are looked up by node/landmark *name*;
  ``mediapipe_index``/``openxr_index`` exist for serialisation only and raise on unknown
  names.
* **No silent scale.** The rigid fit can report a fitted scale (a check on the metric chain),
  but it is never applied unless the caller asks for ``with_scale=True``.
* **A synthetic fixture is not evidence.** Every record carries an ``evidence`` string, and
  the experiment writes ``synthetic`` for closed-loop checks and ``real_devices`` for a run
  over a recorded session.

Fixed geometry versus per-frame quantity
----------------------------------------
One-time (fixed sensor geometry, calibrated once and reused): the fisheye intrinsics of each
eye, the stereo relative pose, the derived-frame scale factor, and the landmark offsets of the
correspondence table. Per frame: ``T_cam<-wrist(t)``, the visibility mask, the residuals and
the inlier set. ``FIXED_SENSOR_GEOMETRY_ITEMS`` / ``PER_FRAME_ITEMS`` name the two lists so a
report cannot conflate them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from visuotactile import skeleton_stream

# ---------------------------------------------------------------- node and landmark tables

# Canonical OpenXR-25 order, re-exported so both sides of the pipeline use one tuple.
OPENXR_NODE_NAMES = skeleton_stream.NODE_NAMES
OPENXR_NODE_COUNT = skeleton_stream.NODE_COUNT

# MediaPipe Hands landmark order (the 21-point hand model of mediapipe 0.10.21, as used by
# code/visuotactile/pose.py). Names, not positions, are the interface.
MEDIAPIPE_LANDMARK_NAMES = (
    'WRIST',
    'THUMB_CMC', 'THUMB_MCP', 'THUMB_IP', 'THUMB_TIP',
    'INDEX_FINGER_MCP', 'INDEX_FINGER_PIP', 'INDEX_FINGER_DIP', 'INDEX_FINGER_TIP',
    'MIDDLE_FINGER_MCP', 'MIDDLE_FINGER_PIP', 'MIDDLE_FINGER_DIP', 'MIDDLE_FINGER_TIP',
    'RING_FINGER_MCP', 'RING_FINGER_PIP', 'RING_FINGER_DIP', 'RING_FINGER_TIP',
    'PINKY_MCP', 'PINKY_PIP', 'PINKY_DIP', 'PINKY_TIP',
)
MEDIAPIPE_LANDMARK_COUNT = len(MEDIAPIPE_LANDMARK_NAMES)

# Anatomical correspondence, stated as (OpenXR node -> MediaPipe landmark). It is derived from
# the documented joint semantics of the two models, NOT measured; the experiment's per-node
# residuals are what test it. The two models agree on 21 joints exactly:
#   * the OpenXR *_metacarpal node of a finger is the metacarpal root inside the palm, for
#     which MediaPipe has no landmark (its *_MCP is the metacarpal head, i.e. the OpenXR
#     *_proximal node);
#   * the thumb chain maps CMC/MCP/IP onto metacarpal/proximal/distal, because the OpenXR
#     thumb has no separate CMC-vs-MCP distinction.
OPENXR_TO_MEDIAPIPE = {
    'wrist': 'WRIST',
    'thumb_metacarpal': 'THUMB_CMC',
    'thumb_proximal': 'THUMB_MCP',
    'thumb_distal': 'THUMB_IP',
    'thumb_tip': 'THUMB_TIP',
    'index_proximal': 'INDEX_FINGER_MCP',
    'index_intermediate': 'INDEX_FINGER_PIP',
    'index_distal': 'INDEX_FINGER_DIP',
    'index_tip': 'INDEX_FINGER_TIP',
    'middle_proximal': 'MIDDLE_FINGER_MCP',
    'middle_intermediate': 'MIDDLE_FINGER_PIP',
    'middle_distal': 'MIDDLE_FINGER_DIP',
    'middle_tip': 'MIDDLE_FINGER_TIP',
    'ring_proximal': 'RING_FINGER_MCP',
    'ring_intermediate': 'RING_FINGER_PIP',
    'ring_distal': 'RING_FINGER_DIP',
    'ring_tip': 'RING_FINGER_TIP',
    'little_proximal': 'PINKY_MCP',
    'little_intermediate': 'PINKY_PIP',
    'little_distal': 'PINKY_DIP',
    'little_tip': 'PINKY_TIP',
}

# OpenXR nodes with no MediaPipe landmark: the four in-palm metacarpal roots.
UNMAPPED_OPENXR_NODES = tuple(name for name in OPENXR_NODE_NAMES
                              if name not in OPENXR_TO_MEDIAPIPE)
# MediaPipe landmarks with no OpenXR node.
MEDIAPIPE_TO_OPENXR = {landmark: node for node, landmark in OPENXR_TO_MEDIAPIPE.items()}
UNMAPPED_MEDIAPIPE_LANDMARKS = tuple(name for name in MEDIAPIPE_LANDMARK_NAMES
                                     if name not in MEDIAPIPE_TO_OPENXR)

CORRESPONDENCE_EVIDENCE = 'anatomical_derivation_from_documented_joint_semantics'

_BASIS_WRIST = 'both models place their origin at the radiocarpal joint centre'
_BASIS_THUMB = {
    'metacarpal': 'thumb CMC joint (base of the first metacarpal)',
    'proximal': 'thumb MCP joint (base of the proximal phalanx)',
    'distal': 'thumb IP joint (base of the distal phalanx)',
    'tip': 'fingertip of the thumb',
}
_BASIS_FINGER = {
    'proximal': 'MCP joint (metacarpal head) = MediaPipe *_MCP',
    'intermediate': 'PIP joint = MediaPipe *_PIP',
    'distal': 'DIP joint = MediaPipe *_DIP',
    'tip': 'fingertip = MediaPipe *_TIP',
}
_BASIS_UNMAPPED = ('the in-palm metacarpal root has no MediaPipe landmark; a naive '
                   'index-by-index match would silently shift every joint of the finger by '
                   'one node')


def correspondence_basis(node: str) -> str:
    """Why this node is paired with that landmark, or why it is unmapped."""
    if node == 'wrist':
        return _BASIS_WRIST
    if node not in OPENXR_TO_MEDIAPIPE:
        return _BASIS_UNMAPPED
    finger, _, joint = node.partition('_')
    table = _BASIS_THUMB if finger == 'thumb' else _BASIS_FINGER
    return table[joint]


FIXED_SENSOR_GEOMETRY_ITEMS = (
    'fisheye intrinsics of cam0 and cam1 (K, D, image size, derived-frame scale)',
    'stereo relative pose cam0 <- cam1 (device-reported, unverified)',
    'landmark correspondence table (OpenXR node -> MediaPipe landmark)',
    'landmark offset between the two models at each mapped joint (measured as a fit residual, '
    'not assumed to be zero)',
)
PER_FRAME_ITEMS = (
    'T_cam<-wrist(t): rotation and translation, one estimate per synchronized sample',
    'visible/matched node set and the inlier set of that frame',
    'per-node residuals and the frame RMSE',
    'fingertip trajectory in the camera frame (derived from the per-frame transforms)',
)

FORBIDDEN_POSITION_SOURCES = ('imu_double_integration', 'accelerometer_integration',
                              'gyroscope_integration', 'dead_reckoning_from_imu')


def assert_position_source_allowed(source: str) -> str:
    """Refuse any position whose provenance is IMU integration.

    The wrist IMU (if one exists at all - the glove's IMU presence is still UNKNOWN) gives
    orientation and, at best, an acceleration whose double integration drifts without bound. A
    drifting integral must never become a coordinate, so the source name is rejected in code
    rather than discouraged in prose.
    """
    text = str(source).strip().lower()
    if text in FORBIDDEN_POSITION_SOURCES:
        raise ValueError(f'position source {source!r} is not allowed: IMU integration is not a '
                         'position measurement')
    return text


def mediapipe_index(name: str) -> int:
    """Serialisation index of a MediaPipe landmark name; raises on an unknown name."""
    try:
        return MEDIAPIPE_LANDMARK_NAMES.index(str(name))
    except ValueError as exc:
        raise ValueError(f'unknown MediaPipe landmark: {name!r}') from exc


def openxr_index(name: str) -> int:
    """Serialisation index of an OpenXR node name; raises on an unknown name."""
    return skeleton_stream.node_index(name)


def correspondence_table() -> dict:
    """The correspondence as a record, with its bijection check and its evidence class."""
    rows = [{'openxr_node': node,
             'mediapipe_landmark': OPENXR_TO_MEDIAPIPE.get(node),
             'basis': correspondence_basis(node)}
            for node in OPENXR_NODE_NAMES]
    mapped_nodes = [row['openxr_node'] for row in rows if row['mediapipe_landmark']]
    mapped_landmarks = [row['mediapipe_landmark'] for row in rows if row['mediapipe_landmark']]
    return {
        'evidence': CORRESPONDENCE_EVIDENCE,
        'not_measured_note': ('derived from the documented joint semantics of the two models; '
                              'the per-node residuals of the tracking experiment are the test, '
                              'and a node with a persistent outlier residual is evidence that '
                              'its row is wrong'),
        'rows': rows,
        'mapped_count': len(mapped_nodes),
        'openxr_node_count': OPENXR_NODE_COUNT,
        'mediapipe_landmark_count': MEDIAPIPE_LANDMARK_COUNT,
        'unmapped_openxr_nodes': list(UNMAPPED_OPENXR_NODES),
        'unmapped_mediapipe_landmarks': list(UNMAPPED_MEDIAPIPE_LANDMARKS),
        'is_bijection_on_mapped_subset': (len(set(mapped_nodes)) == len(mapped_nodes)
                                          and len(set(mapped_landmarks)) == len(mapped_landmarks)
                                          and sorted(mapped_landmarks)
                                          == sorted(MEDIAPIPE_LANDMARK_NAMES)),
        'index_matching_forbidden': True,
        'index_matching_note': ('the two orders are different skeletons; matching them '
                                'positionally would shift every finger joint by one node, so '
                                'every lookup here is by name'),
    }


def validate_correspondence() -> dict:
    """Raise if the correspondence is not a name-based bijection; return the table."""
    table = correspondence_table()
    if not table['is_bijection_on_mapped_subset']:
        raise ValueError('the OpenXR<->MediaPipe correspondence is not a bijection')
    if table['mapped_count'] != MEDIAPIPE_LANDMARK_COUNT:
        raise ValueError(f'only {table["mapped_count"]} of {MEDIAPIPE_LANDMARK_COUNT} '
                         'landmarks are mapped')
    return table


# ---------------------------------------------------------------- fisheye camera model

KANNALA_BRANDT = 'kannala_brandt'
UNDISTORT_MAX_ITERATIONS = 60
UNDISTORT_TOLERANCE = 1e-13


@dataclass(frozen=True)
class FisheyeCamera:
    """One eye, carrying the model the device reported rather than a pinhole assumption.

    The camera reports ``distortion_model: kannala_brandt`` with four coefficients and
    ``R = I``: the streams are **not rectified**, so a pinhole model must not be used. Every
    conversion in this module therefore goes through the fisheye model, and
    ``undistortion_round_trip_px`` reports the error of that conversion as a number instead of
    a claim.
    """

    name: str
    width: int
    height: int
    K: tuple
    D: tuple
    scale: float = 1.0
    distortion_model: str = KANNALA_BRANDT
    evidence: str = 'device_reported'
    independently_verified: bool = False

    @property
    def fx(self) -> float:
        return float(self.K[0])

    @property
    def fy(self) -> float:
        return float(self.K[4])

    @property
    def cx(self) -> float:
        return float(self.K[2])

    @property
    def cy(self) -> float:
        return float(self.K[5])

    def as_record(self) -> dict:
        return {'name': self.name, 'width': self.width, 'height': self.height,
                'K': list(self.K), 'D': list(self.D), 'scale': self.scale,
                'distortion_model': self.distortion_model, 'fx': self.fx, 'fy': self.fy,
                'cx': self.cx, 'cy': self.cy, 'evidence': self.evidence,
                'independently_verified': self.independently_verified,
                'rectified': False,
                'note': ('fisheye with R = I: not rectified, so pinhole geometry is not valid '
                         'for this stream')}

    @classmethod
    def from_intrinsics(cls, name, calibration, *, scale=1.0, evidence='device_reported'):
        """Build from one device ``CameraCalibration`` entry (as recorded in calibration.json).

        ``scale`` converts the calibration to a derived frame (the station records 960x540
        previews next to 1920x1080 raw frames and stores ``intrinsic_scale_xy`` per frame).
        """
        entry = dict(calibration or {})
        K = list(entry.get('K') or [])
        D = list(entry.get('D') or [])
        if len(K) != 9:
            raise ValueError(f'{name}: the device did not report a complete K (9 values)')
        if len(D) != 4:
            raise ValueError(f'{name}: expected 4 kannala_brandt coefficients, got {len(D)}')
        model = str(entry.get('distortion_model') or '')
        if 'kannala' not in model.lower():
            raise ValueError(f'{name}: distortion_model {model!r} is not kannala_brandt, so '
                             'this module has no model for the stream')
        scale = float(scale)
        scaled = [K[0] * scale, K[1], K[2] * scale,
                  K[3], K[4] * scale, K[5] * scale,
                  K[6], K[7], K[8]]
        width = entry.get('width')
        height = entry.get('height')
        return cls(name=name,
                   width=int(round(float(width) * scale)) if width else 0,
                   height=int(round(float(height) * scale)) if height else 0,
                   K=tuple(float(v) for v in scaled), D=tuple(float(v) for v in D),
                   scale=scale, distortion_model=model, evidence=evidence)


def _points(points, size: int, what: str) -> np.ndarray:
    array = np.asarray(points, dtype=np.float64)
    if array.ndim != 2 or array.shape[1] != size:
        raise ValueError(f'{what} must have shape (N, {size}), got {array.shape}')
    if not np.all(np.isfinite(array)):
        raise ValueError(f'{what} contains a non-finite entry')
    return array


def _theta_from_theta_d(theta_d: np.ndarray, D: np.ndarray) -> np.ndarray:
    """Solve theta_d = theta (1 + k1 t^2 + k2 t^4 + k3 t^6 + k4 t^8) for theta by Newton.

    Measured against ``cv2.fisheye.undistortPoints`` (OpenCV 4.11.0) over the full field of
    view: max |difference| 5.9e-8 in normalized coordinates, so this numpy path and OpenCV
    agree without the module depending on OpenCV. The single-step alternative
    (``theta = atan(r_d)`` then scale by ``theta/theta_d``) is wrong by 0.60 in the same test
    and is therefore not used.
    """
    theta = theta_d.copy()
    for _ in range(UNDISTORT_MAX_ITERATIONS):
        t2 = theta * theta
        t4 = t2 * t2
        t6 = t4 * t2
        t8 = t4 * t4
        value = theta * (1.0 + D[0] * t2 + D[1] * t4 + D[2] * t6 + D[3] * t8) - theta_d
        slope = 1.0 + 3.0 * D[0] * t2 + 5.0 * D[1] * t4 + 7.0 * D[2] * t6 + 9.0 * D[3] * t8
        step = value / np.where(np.abs(slope) < 1e-12, 1e-12, slope)
        theta = theta - step
        if float(np.max(np.abs(step))) < UNDISTORT_TOLERANCE:
            break
    return theta


def undistort_to_normalized(points_px, camera: FisheyeCamera) -> np.ndarray:
    """Pixels -> normalized camera coordinates (x = X/Z, y = Y/Z) under kannala_brandt.

    The inverse of the device's own model: the distorted normalized radius equals
    ``theta_d``, so the ray direction is preserved and the radius becomes ``tan(theta)``.
    """
    pixels = _points(points_px, 2, 'points_px')
    x_d = (pixels[:, 0] - camera.cx) / camera.fx
    y_d = (pixels[:, 1] - camera.cy) / camera.fy
    r_d = np.hypot(x_d, y_d)
    theta = _theta_from_theta_d(r_d, np.asarray(camera.D, dtype=np.float64))
    scale = np.where(r_d > 1e-12, np.tan(theta) / np.maximum(r_d, 1e-12), 1.0)
    return np.stack([x_d * scale, y_d * scale], axis=1)


def rays_from_pixels(points_px, camera: FisheyeCamera) -> np.ndarray:
    """Pixels -> rays ``(x, y, 1)`` in that camera's frame (z forward)."""
    normalized = undistort_to_normalized(points_px, camera)
    return np.concatenate([normalized, np.ones((normalized.shape[0], 1))], axis=1)


def project_points(points_3d, camera: FisheyeCamera) -> np.ndarray:
    """3-D points in the camera frame -> pixels, through the device's fisheye model."""
    points = _points(points_3d, 3, 'points_3d')
    depth = points[:, 2]
    if np.any(depth <= 0.0):
        raise ValueError('project_points received a point behind or on the image plane')
    x = points[:, 0] / depth
    y = points[:, 1] / depth
    r = np.hypot(x, y)
    theta = np.arctan(r)
    D = np.asarray(camera.D, dtype=np.float64)
    t2 = theta * theta
    t4 = t2 * t2
    t6 = t4 * t2
    t8 = t4 * t4
    theta_d = theta * (1.0 + D[0] * t2 + D[1] * t4 + D[2] * t6 + D[3] * t8)
    scale = np.where(r > 1e-12, theta_d / np.maximum(r, 1e-12), 1.0)
    return np.stack([camera.fx * x * scale + camera.cx,
                     camera.fy * y * scale + camera.cy], axis=1)


def undistortion_round_trip_px(points_px, camera: FisheyeCamera) -> np.ndarray:
    """Per-point |project(undistort(p)) - p| in pixels: the conversion's own error."""
    pixels = _points(points_px, 2, 'points_px')
    normalized = undistort_to_normalized(pixels, camera)
    rays = np.concatenate([normalized, np.ones((normalized.shape[0], 1))], axis=1)
    return np.linalg.norm(project_points(rays, camera) - pixels, axis=1)


# ---------------------------------------------------------------- stereo geometry

@dataclass(frozen=True)
class StereoGeometry:
    """The stereo pair, with the transform direction written out rather than implied.

    ``rotation_cam0_cam1`` maps a vector expressed in cam1 into cam0, and
    ``translation_cam0_cam1_m`` is the origin of cam1 expressed in cam0:

        X_cam0 = R_cam0_cam1 @ X_cam1 + t_cam0_cam1

    which is the child-in-parent (``parent cam0, child cam1``) reading of the device's own
    ``FrameTransform``. The magnitude of that translation is convention-free; the sign is not,
    so the direction is stated here and reported next to every result.
    """

    camera0: FisheyeCamera
    camera1: FisheyeCamera
    rotation_cam0_cam1: tuple
    translation_cam0_cam1_m: tuple
    source: str = 'device_reported_frame_transform'
    independently_verified: bool = False

    @property
    def baseline_m(self) -> float:
        return float(np.linalg.norm(np.asarray(self.translation_cam0_cam1_m, dtype=float)))

    @property
    def rotation(self) -> np.ndarray:
        return np.asarray(self.rotation_cam0_cam1, dtype=float).reshape(3, 3)

    @property
    def translation(self) -> np.ndarray:
        return np.asarray(self.translation_cam0_cam1_m, dtype=float)

    def as_record(self) -> dict:
        return {'convention': 'X_cam0 = R_cam0_cam1 @ X_cam1 + t_cam0_cam1 (child-in-parent)',
                'rotation_cam0_cam1': list(self.rotation_cam0_cam1),
                'translation_cam0_cam1_m': list(self.translation_cam0_cam1_m),
                'baseline_m': self.baseline_m,
                'source': self.source,
                'evidence': 'device_reported',
                'independently_verified': self.independently_verified,
                'camera0': self.camera0.as_record(),
                'camera1': self.camera1.as_record()}

    @classmethod
    def from_device_transform(cls, camera0, camera1, transform, *, source=None,
                              independently_verified=False):
        """Build from a recorded ``foxglove.FrameTransform`` entry."""
        entry = dict(transform or {})
        translation = [float(v) for v in (entry.get('translation') or [])]
        rotation = [float(v) for v in (entry.get('rotation') or [])]
        if len(translation) != 3:
            raise ValueError('the device-reported transform carries no 3-vector translation')
        if len(rotation) != 4:
            raise ValueError('the device-reported transform carries no quaternion')
        qx, qy, qz, qw = rotation
        matrix = quaternion_xyzw_to_matrix((qx, qy, qz, qw))
        return cls(camera0=camera0, camera1=camera1,
                   rotation_cam0_cam1=tuple(matrix.reshape(-1)),
                   translation_cam0_cam1_m=tuple(translation),
                   source=source or f"device_reported_frame_transform:{entry.get('topic') or ''}",
                   independently_verified=independently_verified)

    def to_cam0(self, points_cam1) -> np.ndarray:
        """Points expressed in cam1 -> cam0."""
        points = _points(points_cam1, 3, 'points_cam1')
        return (self.rotation @ points.T).T + self.translation

    def to_cam1(self, points_cam0) -> np.ndarray:
        """Points expressed in cam0 -> cam1 (the exact inverse of ``to_cam0``)."""
        points = _points(points_cam0, 3, 'points_cam0')
        return (self.rotation.T @ (points - self.translation).T).T


def quaternion_xyzw_to_matrix(quaternion) -> np.ndarray:
    """Unit-quaternion (x, y, z, w) -> rotation matrix; raises on a zero quaternion."""
    values = np.asarray(quaternion, dtype=float).reshape(-1)
    if values.size != 4:
        raise ValueError('a quaternion must have 4 components')
    norm = float(np.linalg.norm(values))
    if norm <= 0.0:
        raise ValueError('zero quaternion')
    x, y, z, w = (values / norm).tolist()
    return np.array([
        [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
        [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
        [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
    ], dtype=float)


def matrix_to_quaternion_xyzw(matrix) -> tuple:
    """Rotation matrix -> unit quaternion (x, y, z, w), sign-normalised to w >= 0."""
    m = np.asarray(matrix, dtype=float).reshape(3, 3)
    trace = float(m[0, 0] + m[1, 1] + m[2, 2])
    if trace > 0.0:
        s = math.sqrt(trace + 1.0) * 2.0
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2.0
        w = (m[2, 1] - m[1, 2]) / s
        x = 0.25 * s
        y = (m[0, 1] + m[1, 0]) / s
        z = (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2.0
        w = (m[0, 2] - m[2, 0]) / s
        x = (m[0, 1] + m[1, 0]) / s
        y = 0.25 * s
        z = (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2.0
        w = (m[1, 0] - m[0, 1]) / s
        x = (m[0, 2] + m[2, 0]) / s
        y = (m[1, 2] + m[2, 1]) / s
        z = 0.25 * s
    values = np.array([x, y, z, w], dtype=float)
    values /= np.linalg.norm(values)
    if values[3] < 0.0:
        values = -values
    return tuple(float(v) for v in values)


# ---------------------------------------------------------------- stereo triangulation

@dataclass(frozen=True)
class Triangulation:
    """Camera-frame 3-D landmarks of one synchronized stereo pair, with their residuals.

    Residuals are reported in three ways, because one number hides different failure modes:
    pixel reprojection error in each eye (what the calibration can be judged by), the ray
    angle error in degrees (scale-free), and the reconstructed depth (a landmark seen at 5 m
    with 1 px error is not the same measurement as one at 0.4 m).
    """

    node_names: tuple
    points_cam0_m: tuple
    residual_px_cam0: tuple
    residual_px_cam1: tuple
    ray_angle_deg_cam0: tuple
    ray_angle_deg_cam1: tuple
    depth_cam0_m: tuple
    valid: tuple
    evidence: str = 'self_reproduced'
    frame: str = 'cam0'

    def as_record(self) -> dict:
        valid = [index for index, flag in enumerate(self.valid) if flag]
        pixels = [self.residual_px_cam0[index] for index in valid] + \
                 [self.residual_px_cam1[index] for index in valid]
        return {
            'frame': self.frame,
            'evidence': self.evidence,
            'node_count': len(self.node_names),
            'valid_node_count': len(valid),
            'invalid_nodes': [self.node_names[index] for index, flag in enumerate(self.valid)
                              if not flag],
            'nodes': [{'node': self.node_names[index],
                       'point_cam0_m': list(self.points_cam0_m[index]),
                       'residual_px_cam0': self.residual_px_cam0[index],
                       'residual_px_cam1': self.residual_px_cam1[index],
                       'ray_angle_deg_cam0': self.ray_angle_deg_cam0[index],
                       'ray_angle_deg_cam1': self.ray_angle_deg_cam1[index],
                       'depth_cam0_m': self.depth_cam0_m[index],
                       'valid': self.valid[index]}
                      for index in range(len(self.node_names))],
            'median_residual_px': float(np.median(pixels)) if pixels else None,
            'max_residual_px': float(np.max(pixels)) if pixels else None,
            'median_depth_m': (float(np.median([self.depth_cam0_m[index] for index in valid]))
                               if valid else None),
        }


def _skew(vector) -> np.ndarray:
    """Skew-symmetric matrix of a 3-vector, so that ``_skew(a) @ X = a x X``."""
    x, y, z = (float(v) for v in np.asarray(vector, dtype=float).reshape(3))
    return np.array([[0.0, -z, y], [z, 0.0, -x], [-y, x, 0.0]], dtype=float)


def triangulate_rays(rays0, rays1, stereo: StereoGeometry):
    """Triangulate two rays per point (cam0 and cam1 frames) into cam0 coordinates.

    Linear (midpoint/DLT) solution of ``a x X = 0`` and ``b x (X - t) = 0`` with
    ``b = R_cam0_cam1 @ d1``: both ray constraints are stacked and solved by least squares, so
    a small calibration error is spread over both views instead of being attributed to one.
    Returns ``(points_cam0, valid_mask)``; a point behind either camera is flagged invalid
    rather than clamped.
    """
    d0 = _points(rays0, 3, 'rays0')
    d1 = _points(rays1, 3, 'rays1')
    if d0.shape != d1.shape:
        raise ValueError('the two ray sets must have the same shape')
    d0 = d0 / np.linalg.norm(d0, axis=1, keepdims=True)
    d1 = d1 / np.linalg.norm(d1, axis=1, keepdims=True)
    d1_in_cam0 = (stereo.rotation @ d1.T).T
    translation = stereo.translation
    points = np.zeros((d0.shape[0], 3), dtype=float)
    for index in range(d0.shape[0]):
        a = d0[index]
        b = d1_in_cam0[index]
        matrix = np.vstack([_skew(a), _skew(b)])
        right = np.concatenate([np.zeros(3), _skew(b) @ translation])
        solution, *_ = np.linalg.lstsq(matrix, right, rcond=None)
        points[index] = solution
    points_cam1 = stereo.to_cam1(points)
    valid = (points[:, 2] > 0.0) & (points_cam1[:, 2] > 0.0)
    return points, valid


def _ray_angle_deg(rays, points) -> np.ndarray:
    """Angle in degrees between a measured ray and the ray to the reconstructed point."""
    directions = np.asarray(rays, dtype=float)
    directions = directions / np.linalg.norm(directions, axis=1, keepdims=True)
    targets = np.asarray(points, dtype=float)
    norms = np.linalg.norm(targets, axis=1, keepdims=True)
    targets = targets / np.where(norms > 0.0, norms, 1.0)
    cosine = np.clip(np.sum(directions * targets, axis=1), -1.0, 1.0)
    return np.degrees(np.arccos(cosine))


def triangulate_landmarks(points0_px, points1_px, stereo: StereoGeometry,
                          node_names) -> Triangulation:
    """Pixels in cam0 and cam1 (same node order, matched by name upstream) -> 3-D + residuals."""
    names = [str(name) for name in node_names]
    pixels0 = _points(points0_px, 2, 'points0_px')
    pixels1 = _points(points1_px, 2, 'points1_px')
    if pixels0.shape != pixels1.shape:
        raise ValueError('the two pixel sets must have the same shape')
    if len(names) != pixels0.shape[0]:
        raise ValueError('node names and points must have the same length')
    rays0 = rays_from_pixels(pixels0, stereo.camera0)
    rays1 = rays_from_pixels(pixels1, stereo.camera1)
    points, valid = triangulate_rays(rays0, rays1, stereo)
    fallback = np.tile(np.array([0.0, 0.0, 1.0]), (points.shape[0], 1))
    usable = np.where(valid[:, None], points, fallback)
    residual0 = np.linalg.norm(project_points(usable, stereo.camera0) - pixels0, axis=1)
    residual1 = np.linalg.norm(project_points(stereo.to_cam1(usable), stereo.camera1)
                               - pixels1, axis=1)
    angle0 = _ray_angle_deg(rays0, points)
    angle1 = _ray_angle_deg(rays1, stereo.to_cam1(points))
    return Triangulation(node_names=tuple(names),
                         points_cam0_m=tuple(tuple(float(v) for v in row) for row in points),
                         residual_px_cam0=tuple(float(v) for v in residual0),
                         residual_px_cam1=tuple(float(v) for v in residual1),
                         ray_angle_deg_cam0=tuple(float(v) for v in angle0),
                         ray_angle_deg_cam1=tuple(float(v) for v in angle1),
                         depth_cam0_m=tuple(float(v) for v in points[:, 2]),
                         valid=tuple(bool(v) for v in valid))


# ---------------------------------------------------------------- rigid 3-D-3-D registration

RANSAC_DEFAULT_THRESHOLD_M = 0.010
RANSAC_DEFAULT_ITERATIONS = 400
RANSAC_DEFAULT_SEED = 17
RANSAC_MIN_INLIERS = 6


@dataclass(frozen=True)
class RigidFit:
    """One per-frame estimate of ``X_cam0 = R @ X_wrist_local + t``.

    Named ``RigidFit`` and not "extrinsic calibration" on purpose: the camera is head-mounted
    and the wrist moves independently, so this is a per-frame pose estimate, and
    ``is_constant_extrinsic`` is written into every record as ``False``. What *is* fixed and
    calibrated once is the sensor geometry and the landmark correspondence (see
    ``FIXED_SENSOR_GEOMETRY_ITEMS``).
    """

    rotation: tuple
    translation_m: tuple
    scale: float
    scale_applied: bool
    node_names: tuple
    residuals_m: tuple
    inlier_mask: tuple
    threshold_m: float
    iterations: int
    seed: int
    evidence: str = 'self_reproduced'
    parent_frame: str = 'cam0'
    child_frame: str = 'wrist_local'

    @property
    def rotation_matrix(self) -> np.ndarray:
        return np.asarray(self.rotation, dtype=float).reshape(3, 3)

    @property
    def translation(self) -> np.ndarray:
        return np.asarray(self.translation_m, dtype=float)

    @property
    def quaternion_xyzw(self) -> tuple:
        return matrix_to_quaternion_xyzw(self.rotation_matrix)

    @property
    def inliers(self) -> tuple:
        return tuple(name for name, flag in zip(self.node_names, self.inlier_mask) if flag)

    @property
    def outliers(self) -> tuple:
        return tuple(name for name, flag in zip(self.node_names, self.inlier_mask) if not flag)

    @property
    def inlier_ratio(self) -> float:
        return (sum(1 for flag in self.inlier_mask if flag) / len(self.inlier_mask)) \
            if self.inlier_mask else 0.0

    def residual_stats(self) -> dict:
        inlier_residuals = [value for value, flag in zip(self.residuals_m, self.inlier_mask) if flag]
        return {'rmse_inlier_m': (float(np.sqrt(np.mean(np.square(inlier_residuals))))
                                  if inlier_residuals else None),
                'max_inlier_residual_m': (float(max(inlier_residuals))
                                          if inlier_residuals else None),
                'max_residual_m': float(max(self.residuals_m)) if self.residuals_m else None,
                'inlier_count': len(self.inliers),
                'outlier_count': len(self.outliers)}

    def apply(self, points_wrist_local) -> np.ndarray:
        """Map wrist-local points into the camera frame with this fit."""
        points = _points(points_wrist_local, 3, 'points_wrist_local')
        scaled = self.scale if self.scale_applied else 1.0
        return (scaled * (self.rotation_matrix @ points.T)).T + self.translation

    def as_record(self) -> dict:
        return {'parent_frame': self.parent_frame,
                'child_frame': self.child_frame,
                'convention': ('X_cam0 = R @ X_wrist_local + t  (child-in-parent: the rotation '
                               'maps wrist_local directions into cam0, the translation is the '
                               'wrist origin expressed in cam0)'),
                'rotation': list(self.rotation),
                'quaternion_xyzw': list(self.quaternion_xyzw),
                'translation_m': list(self.translation_m),
                'scale_fitted': self.scale,
                'scale_applied': self.scale_applied,
                'scale_note': ('a fitted scale near 1 is a consistency check on the metric chain '
                               '(device-reported stereo baseline, metres, landmark model); it is '
                               'reported but never applied unless with_scale=True was requested'),
                'is_constant_extrinsic': False,
                'per_frame_estimate': True,
                'node_count': len(self.node_names),
                'inliers': list(self.inliers),
                'outliers': list(self.outliers),
                'inlier_ratio': self.inlier_ratio,
                'residuals_m': {name: residual for name, residual in zip(self.node_names,
                                                                        self.residuals_m)},
                'ransac': {'threshold_m': self.threshold_m, 'iterations': self.iterations,
                           'seed': self.seed},
                'evidence': self.evidence,
                **self.residual_stats()}


def fit_rigid_transform(source, target, *, with_scale=False):
    """Least-squares rigid (or similarity) transform: ``target ~= s R source + t``.

    Umeyama's closed form with the reflection guard, so a degenerate configuration cannot
    return a mirrored rotation.
    """
    source_points = _points(source, 3, 'source')
    target_points = _points(target, 3, 'target')
    if source_points.shape != target_points.shape:
        raise ValueError('source and target must have the same shape')
    if source_points.shape[0] < 3:
        raise ValueError('at least 3 correspondences are required')
    source_mean = source_points.mean(axis=0)
    target_mean = target_points.mean(axis=0)
    centred_source = source_points - source_mean
    centred_target = target_points - target_mean
    covariance = centred_target.T @ centred_source / source_points.shape[0]
    u, singular, vt = np.linalg.svd(covariance)
    correction = np.eye(3)
    if np.linalg.det(u) * np.linalg.det(vt) < 0.0:
        correction[2, 2] = -1.0
    rotation = u @ correction @ vt
    source_variance = float(np.sum(centred_source ** 2) / source_points.shape[0])
    scale = float(np.sum(singular * np.diag(correction)) / source_variance) \
        if (with_scale and source_variance > 0.0) else 1.0
    translation = target_mean - scale * (rotation @ source_mean)
    return rotation, translation, scale


class RegistrationFailed(RuntimeError):
    """Raised when a frame cannot produce a fit with enough inliers."""


def _triangle_area(points) -> float:
    a, b, c = points
    return 0.5 * float(np.linalg.norm(np.cross(b - a, c - a)))


def robust_rigid_fit(source, target, node_names, *, threshold_m=RANSAC_DEFAULT_THRESHOLD_M,
                     iterations=RANSAC_DEFAULT_ITERATIONS, seed=RANSAC_DEFAULT_SEED,
                     min_inliers=RANSAC_MIN_INLIERS, with_scale=False,
                     evidence='self_reproduced') -> RigidFit:
    """Robust per-frame registration with an explicit inlier set and residuals.

    A wrong correspondence row, a landmark that MediaPipe placed on the wrong finger, or a
    fingertip that the other eye could not see all appear as a small set of large residuals.
    RANSAC over 3-point subsets (rejecting degenerate/collinear ones) finds the largest
    consistent set, the fit is then refined on the inliers, and every point keeps its own
    residual so a systematic per-node error stays visible instead of being averaged away.
    """
    source_points = _points(source, 3, 'source')
    target_points = _points(target, 3, 'target')
    names = [str(name) for name in node_names]
    if source_points.shape != target_points.shape:
        raise ValueError('source and target must have the same shape')
    if len(names) != source_points.shape[0]:
        raise ValueError('node names and points must have the same length')
    count = source_points.shape[0]
    if count < max(3, min_inliers):
        raise RegistrationFailed(f'{count} correspondences cannot support a fit with '
                                 f'{min_inliers} inliers')
    rng = np.random.default_rng(seed)
    spread = float(np.sqrt(np.mean(np.sum((source_points - source_points.mean(axis=0)) ** 2,
                                         axis=1))))
    area_floor = max(1e-9, (spread ** 2) * 1e-3)
    best_mask = None
    best_cost = None
    attempts = 0
    for _ in range(int(iterations)):
        subset = rng.choice(count, size=3, replace=False)
        if _triangle_area(source_points[subset]) < area_floor:
            continue
        attempts += 1
        try:
            rotation, translation, _ = fit_rigid_transform(source_points[subset],
                                                           target_points[subset])
        except (ValueError, np.linalg.LinAlgError):
            continue
        predicted = (rotation @ source_points.T).T + translation
        residuals = np.linalg.norm(predicted - target_points, axis=1)
        mask = residuals < threshold_m
        inliers = int(np.count_nonzero(mask))
        if inliers < 3:
            continue
        cost = (inliers, -float(np.sum(residuals[mask])))
        if best_cost is None or cost > best_cost:
            best_cost = cost
            best_mask = mask
    if best_mask is None or int(np.count_nonzero(best_mask)) < min_inliers:
        raise RegistrationFailed(
            f'no rigid fit reached {min_inliers} inliers within {threshold_m * 1000:.1f} mm '
            f'(best: {0 if best_mask is None else int(np.count_nonzero(best_mask))} inliers, '
            f'{attempts} non-degenerate samples tried)')
    mask = best_mask
    rotation, translation, scale = 1.0, None, 1.0
    for _ in range(3):
        rotation, translation, scale = fit_rigid_transform(source_points[mask],
                                                           target_points[mask],
                                                           with_scale=with_scale)
        predicted = (rotation @ source_points.T).T + translation
        residuals = np.linalg.norm(predicted - target_points, axis=1)
        updated = residuals < threshold_m
        if np.array_equal(updated, mask) or int(np.count_nonzero(updated)) < 3:
            break
        mask = updated
    if int(np.count_nonzero(mask)) < min_inliers:
        raise RegistrationFailed(f'the refined fit kept only '
                                 f'{int(np.count_nonzero(mask))} inliers')
    fitted_scale = scale
    if not with_scale:
        # Report the scale the data would prefer, but never apply it: applying it would hide
        # a wrong stereo baseline or a wrong unit behind a factor that always fits.
        fitted_scale = fit_rigid_transform(source_points[mask], target_points[mask],
                                          with_scale=True)[2]
    predicted = (rotation @ source_points.T).T + translation
    residuals = np.linalg.norm(predicted - target_points, axis=1)
    return RigidFit(rotation=tuple(float(v) for v in rotation.reshape(-1)),
                    translation_m=tuple(float(v) for v in np.asarray(translation).reshape(3)),
                    scale=float(fitted_scale),
                    scale_applied=bool(with_scale),
                    node_names=tuple(names),
                    residuals_m=tuple(float(v) for v in residuals),
                    inlier_mask=tuple(bool(v) for v in mask),
                    threshold_m=float(threshold_m),
                    iterations=int(iterations),
                    seed=int(seed),
                    evidence=evidence)


# ---------------------------------------------------------------- per-frame series and travel

@dataclass(frozen=True)
class WristTrackSample:
    """One synchronized sample: the wrist-local skeleton, its camera-frame fit, its residuals."""

    index: int
    hand_side: str
    timestamps: dict
    wrist_local: dict
    fit: RigidFit | None = None
    failure: str | None = None
    stereo: dict | None = None
    pixels: dict | None = None

    def as_record(self) -> dict:
        record = {'index': self.index,
                  'hand_side': self.hand_side,
                  'timestamps': dict(self.timestamps),
                  'matched_node_count': len(self.wrist_local),
                  'matched_nodes': sorted(self.wrist_local),
                  'stereo': self.stereo,
                  'ok': self.fit is not None}
        if self.pixels is not None:
            record['pixels'] = self.pixels
        if self.fit is None:
            record['failure'] = self.failure
        else:
            record['transform'] = self.fit.as_record()
        return record


def fingertip_travel(samples, node, *, frame='cam0') -> dict:
    """Displacement and path length of one node **in the camera frame**, per fitted sample.

    This is the only place a travel figure is produced. It goes through each frame's
    ``T_cam<-wrist`` first, so the result is a displacement in a fixed frame even though the
    hand rotates while it moves; a difference between two ``positions_local`` samples would be
    expressed in two different wrist frames and is refused by
    ``reject_wrist_local_travel_claim``.
    """
    name = str(node)
    if name not in OPENXR_NODE_NAMES:
        raise ValueError(f'unknown node {node!r}')
    track = []
    for sample in samples:
        if sample.fit is None or name not in sample.wrist_local:
            continue
        position_local = np.asarray(sample.wrist_local[name], dtype=float).reshape(1, 3)
        position_camera = sample.fit.apply(position_local)[0]
        stamp = sample.timestamps.get('bridge_timestamp_sec')
        track.append((float(stamp if stamp is not None else sample.index), position_camera))
    if len(track) < 2:
        raise ValueError(f'{name}: {len(track)} usable samples is not a trajectory '
                         '(at least 2 fitted frames are required)')
    positions = np.array([position for _, position in track], dtype=float)
    times = np.array([time for time, _ in track], dtype=float)
    steps = np.linalg.norm(np.diff(positions, axis=0), axis=1)
    displacement = positions[-1] - positions[0]
    distance = float(np.linalg.norm(displacement))
    return {'frame': frame,
            'node': name,
            'sample_count': len(track),
            'start_m': positions[0].tolist(),
            'end_m': positions[-1].tolist(),
            'displacement_m': distance,
            'displacement_direction_unit': (displacement / distance).tolist()
            if distance > 0.0 else None,
            'path_length_m': float(np.sum(steps)),
            'max_step_m': float(np.max(steps)),
            'median_step_m': float(np.median(steps)),
            'per_step_m': [float(v) for v in steps],
            'duration_s': float(times[-1] - times[0]) if times[-1] > times[0] else None,
            'mean_speed_m_s': (float(np.sum(steps) / (times[-1] - times[0]))
                               if times[-1] > times[0] else None),
            'evidence': 'self_reproduced',
            'wrist_local_differences_used': False,
            'note': ('each position is T_cam<-wrist(t) applied to the wrist-local node, so the '
                     'figure is a camera-frame displacement; path length > displacement is '
                     'expected for a moving hand and is reported rather than smoothed away')}


def reject_wrist_local_travel_claim(*_args, **_kwargs):
    """Refuse to compute travel from ``positions_local`` differences, always.

    The wrist node is the origin of ``wrist_local`` by definition, so a difference between two
    samples is a change of the hand's shape and orientation as seen from its own wrist - not a
    displacement in any fixed frame. Absolute hand travel needs the per-frame camera-frame
    estimate, and this function exists so that the shortcut cannot be taken silently.
    """
    raise ValueError(
        'a difference of two wrist_local samples is not a displacement in any fixed frame: the '
        'frame moves with the wrist and its origin is the wrist node itself. Use '
        'fingertip_travel() on fitted per-frame transforms (T_cam<-wrist(t)) instead')


def scale_consistency(samples, *, tolerance=0.05) -> dict:
    """Check the fitted scales of a series: a value far from 1 means the metric chain is wrong.

    A scale of 1 is not assumed, it is *tested*: this is the figure a wrong stereo baseline, a
    wrong unit or a wrong derived-frame scale would move.
    """
    scales = [sample.fit.scale for sample in samples if sample.fit is not None]
    if not scales:
        return {'sample_count': 0, 'median_scale': None, 'tolerance': tolerance,
                'consistent': None, 'reason': 'no fitted frame carried a scale estimate'}
    median = float(np.median(scales))
    inside = sum(1 for value in scales if abs(value - 1.0) <= tolerance)
    return {'sample_count': len(scales), 'median_scale': median,
            'min_scale': float(np.min(scales)), 'max_scale': float(np.max(scales)),
            'tolerance': tolerance, 'samples_within_tolerance': inside,
            'consistent': bool(abs(median - 1.0) <= tolerance),
            'note': ('the scale is never applied to a fit; a median outside 1 +/- tolerance is '
                     'evidence that the stereo geometry, the unit or the derived-frame scale is '
                     'wrong, and the result must not be used as a metric position')}


def metric_accuracy_against_reference(measured_m, reference_m, *, reference_source,
                                      independent: bool = True) -> dict:
    """Per-item error against an independent reference (a known-size object or fixture).

    Kept separate from the fit residuals on purpose: the residual says how well the model fits
    its own observations, this says how close the metric result is to a length measured by
    something else.
    """
    measured = np.asarray(measured_m, dtype=float).reshape(-1)
    reference = np.asarray(reference_m, dtype=float).reshape(-1)
    if measured.shape != reference.shape:
        raise ValueError('measured and reference values must have the same shape')
    errors = measured - reference
    return {'reference_source': reference_source,
            'independent_reference': bool(independent),
            'item_count': int(measured.size),
            'errors_m': [float(v) for v in errors],
            'mean_error_m': float(np.mean(errors)) if measured.size else None,
            'mean_absolute_error_m': float(np.mean(np.abs(errors))) if measured.size else None,
            'max_absolute_error_m': float(np.max(np.abs(errors))) if measured.size else None,
            'note': ('an accuracy figure is only as independent as its reference; a synthetic '
                     'reference is a closed-loop check and must not be reported as hardware '
                     'accuracy')}


def pair_by_timestamp(left, right, *, left_time='host_mono_ns', right_time='host_mono_ns',
                      max_delta_s=0.05) -> dict:
    """Pair two streams by clock proximity, one-to-one, never by array position.

    The two streams are sampled independently (MediaPipe at the station's pose rate, the bridge
    once per glove frame), so index i of one is not index i of the other. Every pair carries its
    own time difference, and unmatched entries are returned rather than dropped silently.
    """
    def _stamp(record, key):
        value = record.get(key)
        if value is None:
            return None
        return float(value) / 1e9 if str(key).endswith('_ns') else float(value)

    left_stamps = [_stamp(record, left_time) for record in left]
    right_stamps = [_stamp(record, right_time) for record in right]
    candidates = []
    for left_index, left_stamp in enumerate(left_stamps):
        if left_stamp is None:
            continue
        for right_index, right_stamp in enumerate(right_stamps):
            if right_stamp is None:
                continue
            delta = abs(left_stamp - right_stamp)
            if delta <= max_delta_s:
                candidates.append((delta, left_index, right_index))
    candidates.sort()
    used_left, used_right, pairs = set(), set(), []
    for delta, left_index, right_index in candidates:
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        pairs.append({'left_index': left_index, 'right_index': right_index, 'delta_s': delta})
    pairs.sort(key=lambda row: row['delta_s'])
    deltas = [row['delta_s'] for row in pairs]
    return {'pairs': pairs,
            'pair_count': len(pairs),
            'left_count': len(left),
            'right_count': len(right),
            'unmatched_left': [index for index in range(len(left)) if index not in used_left],
            'unmatched_right': [index for index in range(len(right)) if index not in used_right],
            'median_delta_s': float(np.median(deltas)) if deltas else None,
            'max_delta_s': float(np.max(deltas)) if deltas else None,
            'max_delta_allowed_s': float(max_delta_s),
            'matched_by': f'closest {left_time} / {right_time} within {max_delta_s} s',
            'index_matching_used': False}
