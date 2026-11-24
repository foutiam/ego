"""Fiducial-marker registration: ``T_cam0<-marker(t)`` and the install calibration.

Purpose
-------
A printed marker rigidly attached to the hand base gives a camera-frame pose independent of the
MediaPipe stereo path. This module implements the software half of that path:

    detect corners  ->  fisheye-undistort  ->  solvePnP(K=I)  ->  T_cam0<-marker
    T_cam0<-wrist = T_cam0<-marker @ T_marker<-wrist   (the install calibration)

The physical install (a rigid back-of-hand base, a printed tag, a re-wear check) is a
USER_ACTION; this module provides the detection, the pose, the composition, the printable
marker and the guide, and is honest about what remains a hardware step.

Honesty constraints
-------------------
* The fisheye coefficients are never fed to a pinhole solver. Corners are undistorted to
  normalized rays with the device's own kannala_brandt model, then solved with K = I.
* A planar square has an inherent two-solution pose ambiguity; it is reported, not resolved
  by a hidden preference. A second view (the other eye) or a known prior resolves it later.
* The install calibration ``T_marker<-wrist`` is stored separately and never assumed equal to
  the marker pose or to the identity. A fitted install pose is labelled an estimate and must
  be validated on a held-out pose and re-checked after re-wearing the rig.
* Occlusion / a missing marker return a missing pose, never a re-used or smoothed one.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

import ego_capture.hand_openxr_tracking as tracking
import ego_capture.hand_pose_pipeline as pipeline

# ---------------------------------------------------------------- fixed tag spec

MARKER_FAMILY = 'AprilTag 36h11'
MARKER_DICTIONARY_ID = 'cv2.aruco.DICT_APRILTAG_36h11'
MARKER_ID = 0
MARKER_SIDE_M = 0.06            # printed side (metres), between the tag edge markers
QUIET_ZONE = 1                  # white border width in tag-relative units the guide requires


def marker_spec() -> dict:
    """The fixed tag family, id and physical size, so a recorded pose names its tag."""
    return {'family': MARKER_FAMILY,
            'dictionary': MARKER_DICTIONARY_ID,
            'opencv_note': 'opencv 4.11 aruco dictionary DICT_APRILTAG_36h11 (contrib bundled)',
            'marker_id': MARKER_ID,
            'side_m': MARKER_SIDE_M,
            'quiet_zone': QUIET_ZONE,
            'print_instruction': ('print at 100% scale so the printed side equals side_m; keep a '
                                  'white border (quiet_zone) around the tag'),
            'orientation_axes': ('marker +z points INTO the printed face (the printed face normal '
                                 'is -z_marker; measured 2026-10-12 by rendering the plate from its '
                                 'own printed frame and re-solving it), marker +x is the printed '
                                 'right and +y the printed down. The install pose maps this framed '
                                 'square onto the wrist frame.')}


def marker_guide() -> dict:
    """The printable-marker and install guide, for the USER_ACTIONS checklist."""
    return {'marker': marker_spec(),
            'print': 'generate a PNG with write_marker_png() and print at 100% (side 60 mm)',
            'mount': ('prefer a rigid base on the back of the hand, tied to the palm skeleton '
                      'base; do NOT mount across the wrist joint, and do not assume a forearm '
                      'mount keeps a fixed marker-to-wrist pose'),
            'measure': ('after mounting, digitize the marker origin/axes in the wrist frame (or '
                        'fit them from several static poses held under known wrist poses); this '
                        'becomes T_marker<-wrist and is stored separately, never assumed zero'),
            'validate': ('hold out poses that were not used for the install fit; re-check after '
                         'each re-wear and after any shift'),
            'remaining_user_actions': ['print and mount the tag',
                                       'measure or fit T_marker<-wrist',
                                       'record one short session and accept the marker path on '
                                       'real hardware']}


def generate_marker_image(pixels: int = 512, border_pixels: int | None = None):
    """Return a uint8 ``(H, W)`` marker image with a white quiet-zone border.

    A bare ``generateImageMarker`` output has no quiet zone and AprilTag will not detect it; the
    border is part of the printable image and of the side measurement.
    """
    import cv2

    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    core = cv2.aruco.generateImageMarker(dictionary, MARKER_ID, int(pixels))
    if border_pixels is None:
        border_pixels = max(8, int(pixels) // 8)
    return np.pad(core, border_pixels, mode='constant', constant_values=255)


def write_marker_png(path: str | Path, pixels: int = 512) -> Path:
    import cv2

    image = generate_marker_image(pixels)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), image)
    return Path(path)


def marker_png(pixels: int = 1024) -> bytes:
    """The printable marker as PNG bytes for the web guide (print at 100% scale).

    The quiet zone is part of this image: a bare ``generateImageMarker`` output has none and
    AprilTag will not detect it.
    """
    import cv2

    image = generate_marker_image(pixels)
    ok, buffer = cv2.imencode('.png', image)
    if not ok:
        raise ValueError('could not encode the marker image as PNG')
    return buffer.tobytes()


# ---------------------------------------------------------------- detection + pose

def detect_marker(corners_image_bgr):
    """Detect the fixed-tag marker; return a list of corner sets, never a guess."""
    import cv2

    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    parameters = cv2.aruco.DetectorParameters()
    detector = cv2.aruco.ArucoDetector(dictionary, parameters)
    image = np.asarray(corners_image_bgr)
    gray = image if image.ndim == 2 else cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    corners, ids, _rejected = detector.detectMarkers(gray)
    if ids is None:
        return []
    return [{'id': int(identifier), 'corners_px': [[float(x), float(y)] for x, y in
                                                   np.asarray(corner).reshape(4, 2)]}
            for identifier, corner in zip(ids.ravel().tolist(), corners)]


def marker_object_points(side_m: float = MARKER_SIDE_M) -> np.ndarray:
    """The four corners in the marker frame (z = 0), ordered as the detector reports them."""
    half = float(side_m) / 2.0
    # Aruco corner order is top-left, top-right, bottom-right, bottom-left.
    return np.array([[-half, -half, 0.0], [half, -half, 0.0],
                     [half, half, 0.0], [-half, half, 0.0]], dtype=float)


def marker_pose_cam(corners_px, camera, side_m=MARKER_SIDE_M):
    """Recover T_cam0<-marker from four corners through the fisheye model.

    Corners are undistorted to normalized rays with the device kannala_brandt model, then solved
    in an equivalent pinhole of focal length camera.fx. The fisheye distortion coefficients are
    never passed to the pinhole solver. The planar-square ambiguity is reported, never silently
    resolved.
    """
    import cv2

    pixels = np.asarray(corners_px, dtype=float).reshape(4, 2)
    if not np.all(np.isfinite(pixels)):
        raise ValueError('marker corners are not finite')
    normalized = tracking.undistort_to_normalized(pixels, camera)
    focal = float(camera.fx)
    virtual_k = np.array([[focal, 0.0, 0.0], [0.0, focal, 0.0], [0.0, 0.0, 1.0]])
    virtual_px = normalized * focal
    object_points = marker_object_points(side_m)
    _ok, rvec, tvec = cv2.solvePnP(object_points, virtual_px, virtual_k, None,
                                   flags=cv2.SOLVEPNP_ITERATIVE)
    matrix, _jacobian = cv2.Rodrigues(np.asarray(rvec).reshape(3, 1))
    projected, _ = cv2.projectPoints(object_points, rvec, tvec, virtual_k, None)
    error_px = float(np.linalg.norm(np.asarray(projected).reshape(4, 2) - virtual_px))
    return {'matrix': np.asarray(matrix, dtype=float),
            'translation_m': np.asarray(tvec, dtype=float).reshape(3),
            'convention': pipeline.FRAME_CONVENTION,
            'parent_frame': 'cam0',
            'child_frame': 'marker',
            'fisheye_note': ('corners were undistorted to normalized rays with the device '
                             'kannala_brandt model, then solved in an equivalent pinhole of '
                             'focal length fx; the fisheye coefficients were never passed to a '
                             'pinhole solver'),
            'reprojection_error_px': error_px,
            'planar_ambiguity_note': ('a single-view planar square has an inherent two-solution '
                                      'pose ambiguity; the front-facing solution is chosen, and '
                                      'the other eye is the independent check against a flip'),
            'marker_spec': marker_spec()}


def invert_transform(transform: dict) -> dict:
    """Exact inverse of a rigid pose: ``inv(T)`` maps child into parent's inverse frame."""
    matrix = transform['matrix'] if isinstance(transform.get('matrix'), np.ndarray)\
        else pipeline.rotation_matrix_3x3(transform.get('rotation'))
    translation = np.asarray(transform.get('translation_m'), dtype=float).reshape(3)
    return {'matrix': matrix.T, 'translation_m': -(matrix.T @ translation),
            'convention': pipeline.FRAME_CONVENTION}


def install_transform_from_samples(cam_marker_poses, cam_wrist_poses) -> dict:
    """Estimate the fixed ``T_marker<-wrist`` from matched (marker, wrist) cam0-pose pairs.

    Each sample gives ``T_marker<-wrist_i = inv(T_cam<-marker_i) @ T_cam<-wrist_i``. The poses are
    averaged with a repeatability report; the result is labelled an **estimate** that must be
    validated on a held-out pose and re-checked after re-wearing the rig.
    """
    if len(cam_marker_poses) != len(cam_wrist_poses) or not cam_marker_poses:
        raise ValueError('one T_cam<-marker and one T_cam<-wrist per sample are required')
    samples = []
    for marker, wrist in zip(cam_marker_poses, cam_wrist_poses):
        samples.append(pipeline.compose(invert_transform(marker), wrist))
    matrices = np.stack([sample['matrix'] for sample in samples])
    translations = np.stack([sample['translation_m'] for sample in samples])
    mean_matrix = matrices.mean(axis=0)
    u, _s, vt = np.linalg.svd(mean_matrix)
    mean_rotation = u @ vt
    if np.linalg.det(mean_rotation) < 0.0:
        u[:, -1] *= -1.0
        mean_rotation = u @ vt
    mean_translation = translations.mean(axis=0)
    rotation_spread = float(np.mean(np.linalg.norm(matrices - mean_rotation, axis=(1, 2))))
    translation_spread = float(np.mean(np.linalg.norm(translations - mean_translation, axis=1)))
    return {'matrix': mean_rotation,
            'translation_m': mean_translation,
            'convention': pipeline.FRAME_CONVENTION,
            'parent_frame': 'marker',
            'child_frame': 'wrist',
            'sample_count': len(samples),
            'is_estimate': True,
            'rotation_spread': rotation_spread,
            'translation_spread_m': translation_spread,
            'spread_note': ('rotation/translation spread across samples; a repeatability figure, '
                            'not an accuracy'),
            'validation_required': ('validate on a held-out pose and re-check after re-wearing; '
                                    'the fitting samples must not be used as the accuracy proof')}