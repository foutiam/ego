"""Declared marker layout and the hand-frame -> camera-frame registration it enables.

What this adds on top of ``fiducial_registration``
--------------------------------------------------
``fiducial_registration`` answers "where is *this* tag in cam0". This module answers the
question the operator actually asked (2026-10-12): *which tag id sits where on the hand*, so
the pose of the hand in the visual (camera) frame follows from a declared table:

    declared table  (idx -> position/orientation in ``wrist_local``)
    + one detected tag pose  T_cam0<-marker(idx)
    = T_cam0<-wrist = T_cam0<-marker @ T_marker<-wrist

The table is editable from the calibration page: every row is one printed tag, its side in
metres, and where that tag sits on the hand. The provisional default is the operator's
2026-10-12 declaration:

* ``idx 1`` -- the centre of the back of the hand, 4 cm above it (4 cm along ``+x``), printed
  face out of the back of the hand;
* ``idx 2`` -- 2 cm towards the thumb and 2 cm above the back, with the printed face pointing
  towards the thumb, i.e. the tag plane perpendicular to the back of the hand.

The tag frame the solver returns (measured 2026-10-12)
-----------------------------------------------------
A decoded tag is published in a fixed frame, and a row's ``quaternion_xyzw`` has to describe *that*
frame, not the frame one would assume from the tag's shape. Rendering the printable plate from its
own printed frame (the plate's top-left pixel block placed at the solver's corner 0, printed face at
the camera) and re-solving it (``tmp/marker-axis-convention-check.py``) gives, for a tag whose
printed face points at cam0::

    marker +x = the printed right    marker +y = the printed down    marker +z = INTO the printed face

so the printed face normal is ``-z_marker``. That is the opposite of what ``marker_spec()`` said
before 2026-10-12, and the rows below are written from the measured meaning: "printed face along
wrist axis +v" is ``matrix[:, 2] == -v``, and the printed face normal a page reads is
``-matrix[:, 2]`` (``MARKER_AXIS_CONVENTION`` states the same thing in the payloads).

Honesty constraints
-------------------
* The table is an operator **declaration**, never a measurement. Every record this module
  emits carries ``source: operator_declaration_not_measured`` and ``is_estimate: True``; a
  4 cm offset that was typed is not a 4 cm that was measured.
* The frame is named, not implied: positions are ``wrist_local`` metres, and the axis
  meanings are stated in ``FRAME_NOTE`` (+z wrist->fingers, +y towards the thumb, the back of
  the hand faces +x). A row in another frame is refused, not reinterpreted.
* A tag that is detected but not declared is **reported** (``undeclared_ids``) instead of
  being silently dropped or matched to the nearest row.
* A tag that is not detected produces no pose: nothing is smoothed, re-used or extrapolated.
* With two or more tags visible the estimates are compared and the difference is published
  (``agreement``). That is a *self-consistency* figure for the declared table, not an accuracy
  claim: a table where every row is wrong by the same rigid offset would still agree.
* A single-view planar tag has an inherent two-solution pose ambiguity; it is reported by the
  pose layer and is the reason a second tag (or the second eye) is worth having.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

import ego_capture.fiducial_registration as fiducial
import ego_capture.hand_openxr_tracking as tracking
import ego_capture.hand_pose_pipeline as pipeline

# ---------------------------------------------------------------- fixed shape of a layout

LAYOUT_SCHEMA = 'capture-station-marker-layout-v1'
LAYOUT_FRAME = 'wrist_local'
FRAME_NOTE = ('positions are wrist_local metres: +z runs wrist -> middle fingertip, +y points '
              'towards the thumb, and the back of the hand faces +x (the palm faces -x)')
DECLARATION_SOURCE = 'operator_declaration_not_measured'
DECLARATION_NOTE = ('typed by the operator on the calibration page; a declaration, not a '
                    'measurement, so it is never used as an accuracy figure')
# What a solved tag frame means, measured rather than assumed (see the module docstring): every
# payload that carries a declared posture repeats this, so a reader of a record knows which sign
# the quaternion's z column has.
MARKER_AXIS_CONVENTION = ('marker +z points into the printed face, so the printed face normal is '
                          '-z_marker; marker +x is the printed right and +y the printed down '
                          '(measured 2026-10-12 by rendering the plate from its own printed frame '
                          'and re-solving it: tmp/marker-axis-convention-check.py)')

DEFAULT_LAYOUT_IDX = 1
DEFAULT_HEIGHT_ABOVE_BACK_M = 0.04      # 4 cm above the back of the hand (the +x offset)
DEFAULT_CENTRE_ALONG_FINGERS_M = 0.06   # mid metacarpal, i.e. the centre of the hand back
DEFAULT_SIDE_M = fiducial.MARKER_SIDE_M

DEFAULT_IDX_THUMB_FACING = 2            # the user's 2026-10-12 second declaration
DEFAULT_THUMB_SIDE_OFFSET_M = 0.02      # 2 cm towards the left thumb (the +y offset)
DEFAULT_THUMB_FACING_HEIGHT_M = 0.02    # 2 cm above the back of the hand (the +x offset)
SIDE_M_MIN, SIDE_M_MAX = 0.005, 0.5
POSITION_LIMIT_M = 1.0
IDX_MAX = 999
IDS_NOTE = ('marker ids are the tag ids the detector reports; the printable PNG for a declared id '
            'is served by /api/handpose/marker.png?idx=<id>')

# The default tag orientation of every declared posture. Columns are the marker axes expressed in
# wrist_local, so both matrices are right-handed (determinant +1). Marker +z points *into* the
# printed face (measured: MARKER_AXIS_CONVENTION), so a posture that declares "the printed face
# points along wrist +v" is written ``matrix[:, 2] == -v``, and the printed face normal is
# ``-matrix[:, 2]``. ``ORIENTATION_AXIS_TOL_DEG`` is the residual angle within which a declared
# posture is reported as sitting exactly on a wrist axis.
DEFAULT_ROTATION_WRIST_FROM_MARKER = np.array([[0.0, 0.0, -1.0],
                                               [0.0, 1.0, 0.0],
                                               [1.0, 0.0, 0.0]])
# ID2 (user, 2026-10-12): printed face towards the thumb, so the printed face normal is wrist +y
# (marker +z = -y) and the tag plane is perpendicular to the back of the hand. Marker +x (the printed
# right) runs towards the fingertips and marker +y (the printed down) points into the palm.
DEFAULT_ROTATION_WRIST_FROM_MARKER_THUMB_FACING = np.array([[0.0, -1.0, 0.0],
                                                            [0.0, 0.0, -1.0],
                                                            [1.0, 0.0, 0.0]])

ORIENTATION_AXIS_TOL_DEG = 0.5          # tolerance for "the declared face sits on this axis"
AXIS_LABELS = {(+1, 0, 0): '+x (out of the back of the hand)',
               (-1, 0, 0): '-x (into the palm)',
               (0, +1, 0): '+y (towards the thumb)',
               (0, -1, 0): '-y (towards the little finger)',
               (0, 0, +1): '+z (towards the fingertips)',
               (0, 0, -1): '-z (towards the wrist)'}

AGREEMENT_ROTATION_DEG = 2.0            # cross-tag rotation difference that is called a mismatch
AGREEMENT_TRANSLATION_M = 0.005         # 5 mm cross-tag position difference


# ---------------------------------------------------------------- the declared table

def default_marker(idx: int = DEFAULT_LAYOUT_IDX, *, side_m: float = DEFAULT_SIDE_M,
                   position_m=None,
                   height_above_back_m: float = DEFAULT_HEIGHT_ABOVE_BACK_M,
                   centre_along_fingers_m: float = DEFAULT_CENTRE_ALONG_FINGERS_M,
                   thumb_side_offset_m: float = 0.0,
                   rotation=None, note: str | None = None) -> dict:
    """One declared tag, positioned on the back of the hand and oriented by a stated posture.

    The default posture is the operator's ID1: over the centre of the back of the hand, 4 cm above
    it, printed face out of the back of the hand -- which, with marker +z pointing *into* the printed
    face, is ``matrix[:, 2] = [-1, 0, 0]``. Passing ``position_m`` takes precedence over the
    offsets, and ``rotation`` (a 3x3 whose columns are the marker axes in wrist_local, +z into the
    printed face) over the default posture, so the ID2 declaration can be expressed with the same
    function.
    """
    position = ([_number(value, f'marker {idx} position_m') for value in position_m]
                if position_m is not None
                else [float(height_above_back_m), float(thumb_side_offset_m),
                      float(centre_along_fingers_m)])
    matrix = DEFAULT_ROTATION_WRIST_FROM_MARKER if rotation is None else np.asarray(rotation,
                                                                                   dtype=float)
    if note is None:
        note = ('centre of the back of the hand, %.0f cm above it'
                % (100.0 * float(position[0])))
    return {'idx': int(idx),
            'side_m': float(side_m),
            'position_m': position,
            'quaternion_xyzw': list(tracking.matrix_to_quaternion_xyzw(matrix)),
            'note': note,
            'source': DECLARATION_SOURCE}


def default_marker_thumb_facing(idx: int = DEFAULT_IDX_THUMB_FACING,
                                *, side_m: float = DEFAULT_SIDE_M,
                                note: str | None = None) -> dict:
    """The operator's ID2: 2 cm towards the thumb, 2 cm above the back, face towards the thumb."""
    return default_marker(
        idx, side_m=side_m,
        position_m=[DEFAULT_THUMB_FACING_HEIGHT_M, DEFAULT_THUMB_SIDE_OFFSET_M,
                    DEFAULT_CENTRE_ALONG_FINGERS_M],
        rotation=DEFAULT_ROTATION_WRIST_FROM_MARKER_THUMB_FACING,
        note=note if note is not None
        else ('%.0f cm towards the thumb and %.0f cm above the centre of the back of the hand, '
              'printed face towards the thumb (tag plane perpendicular to the back of the hand)'
              % (100.0 * DEFAULT_THUMB_SIDE_OFFSET_M, 100.0 * DEFAULT_THUMB_FACING_HEIGHT_M)))


def orientation_options() -> list:
    """The declared postures a row may use, as quaternions plus what each one means.

    Both postures are *declarations* like every row: the list exists so the page can show and set
    the orientation of a row instead of silently keeping one, not because either is measured.
    """
    return [{'key': 'back_normal',
             'label': '打印面朝手背外法线（贴在手背上的平面）',
             'quaternion_xyzw': list(tracking.matrix_to_quaternion_xyzw(
                 DEFAULT_ROTATION_WRIST_FROM_MARKER)),
             'face_normal_wrist_axis': '+x',
             'note': ('tag plane parallel to the back of the hand, printed face out of the back; '
                      'the printed right (marker +x) runs towards the fingertips and the printed '
                      'down (marker +y) towards the thumb'),
             'source': DECLARATION_SOURCE},
            {'key': 'thumb_normal',
             'label': '打印面朝拇指方向（与手背面垂直）',
             'quaternion_xyzw': list(tracking.matrix_to_quaternion_xyzw(
                 DEFAULT_ROTATION_WRIST_FROM_MARKER_THUMB_FACING)),
             'face_normal_wrist_axis': '+y',
             'note': ('printed face towards the thumb, so the tag plane is perpendicular to the '
                      'back of the hand; the printed right (marker +x) runs towards the fingertips '
                      'and the printed down (marker +y) towards the palm'),
             'source': DECLARATION_SOURCE}]


def default_layout() -> dict:
    """The provisional table: the operator's 2026-10-12 ID1 and ID2 declarations."""
    return validate_layout({'frame': LAYOUT_FRAME,
                            'markers': [default_marker(), default_marker_thumb_facing()],
                            'defaults_note': ('provisional (operator, 2026-10-12): ID1 = centre of '
                                              'the back of the hand, 4 cm above it, face out of the '
                                              'back; ID2 = 2 cm towards the thumb and 2 cm above '
                                              'the back, face towards the thumb (perpendicular to '
                                              'the back). This is the provisional default, not the '
                                              'operator declaration on disk')})


def layout_path(root) -> Path:
    """Where the declared table lives: ``<output_root>/calibration/marker_layout.json``."""
    return Path(root) / 'calibration' / 'marker_layout.json'


def _number(value, what: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{what} must be a number, got {type(value).__name__}')
    number = float(value)
    if not np.isfinite(number):
        raise ValueError(f'{what} must be finite')
    return number


def _vector(values, size: int, what: str) -> list:
    if not isinstance(values, (list, tuple)) or len(values) != size:
        raise ValueError(f'{what} must be a {size}-element list')
    return [_number(v, f'{what}[{i}]') for i, v in enumerate(values)]


def validate_layout(payload) -> dict:
    """Normalise and validate a declared table; raise on anything ambiguous.

    Unknown keys are dropped, rows are sorted by id, and the quaternion is normalised (a zero
    quaternion is refused rather than replaced by the identity).
    """
    if not isinstance(payload, dict):
        raise ValueError('a marker layout must be an object')
    frame = str(payload.get('frame') or LAYOUT_FRAME)
    if frame != LAYOUT_FRAME:
        raise ValueError(f'the declared table must be in {LAYOUT_FRAME}, got {frame!r}: this '
                         'module has no conversion for another frame')
    rows = payload.get('markers')
    if not isinstance(rows, list) or not rows:
        raise ValueError('a marker layout needs at least one row')
    markers = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('every marker row must be an object')
        idx = row.get('idx')
        if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx <= IDX_MAX:
            raise ValueError(f'marker idx must be an integer in 0..{IDX_MAX}, got {idx!r}')
        if idx in seen:
            raise ValueError(f'marker idx {idx} is declared twice')
        seen.add(idx)
        side_m = _number(row.get('side_m', DEFAULT_SIDE_M), f'marker {idx} side_m')
        if not SIDE_M_MIN <= side_m <= SIDE_M_MAX:
            raise ValueError(f'marker {idx} side_m {side_m} is outside '
                             f'{SIDE_M_MIN}..{SIDE_M_MAX} m')
        position = _vector(row.get('position_m'), 3, f'marker {idx} position_m')
        if any(abs(v) > POSITION_LIMIT_M for v in position):
            raise ValueError(f'marker {idx} position_m is more than {POSITION_LIMIT_M} m from the '
                             'wrist origin; check the unit (metres)')
        quaternion = _vector(row.get('quaternion_xyzw'), 4, f'marker {idx} quaternion_xyzw')
        norm = float(np.linalg.norm(quaternion))
        if norm <= 1e-9:
            raise ValueError(f'marker {idx} quaternion_xyzw is zero, so it names no orientation')
        quaternion = [v / norm for v in quaternion]
        matrix = tracking.quaternion_xyzw_to_matrix(quaternion)
        pipeline.rotation_matrix_3x3(matrix)          # orthonormal, determinant +1
        note = row.get('note', '')
        if not isinstance(note, str):
            raise ValueError(f'marker {idx} note must be text')
        markers.append({'idx': idx, 'side_m': side_m, 'position_m': position,
                        'quaternion_xyzw': quaternion,
                        'rotation': [float(v) for v in np.asarray(matrix).reshape(-1)],
                        'note': note[:200],
                        'source': str(row.get('source') or DECLARATION_SOURCE)})
    return {'schema': LAYOUT_SCHEMA,
            'frame': LAYOUT_FRAME,
            'frame_note': FRAME_NOTE,
            'markers': sorted(markers, key=lambda m: m['idx']),
            'declared_count': len(markers),
            'defaults_note': str(payload.get('defaults_note') or ''),
            'source': DECLARATION_SOURCE,
            'evidence': 'operator_declaration_not_measured',
            'notes': [DECLARATION_NOTE,
                      'an undeclared but detected id is reported, never matched to a row'],
            'ids_note': IDS_NOTE}


def load_layout(root) -> dict:
    """Read the declared table, falling back to the provisional default when none was saved.

    The fallback is labelled (``from_disk: False``) so a page can say the shown table is the
    provisional default rather than something the operator declared.
    """
    path = layout_path(root)
    if not path.exists():
        layout = dict(default_layout())
        layout['from_disk'] = False
        layout['layout_path'] = str(path)
        layout['loaded_note'] = ('no saved table yet: showing the provisional default '
                                 '(ID1 = centre of the back of the hand, 4 cm above it, face out '
                                 'of the back; ID2 = 2 cm towards the thumb and 2 cm above the '
                                 'back, face towards the thumb)')
        return layout
    layout = validate_layout(json.loads(path.read_text(encoding='utf-8-sig')))
    layout['from_disk'] = True
    layout['layout_path'] = str(path)
    layout['loaded_note'] = 'loaded from disk; the rows are the operator declaration'
    return layout


def save_layout(root, payload) -> dict:
    """Validate and store the declared table; returns the stored (normalised) layout."""
    layout = validate_layout(payload)
    path = layout_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(layout, ensure_ascii=False, indent=2), encoding='utf-8')
    out = dict(layout)
    out['from_disk'] = True
    out['layout_path'] = str(path)
    out['saved'] = True
    return out


# ---------------------------------------------------------------- declared orientation as a pose

def marker_in_wrist(marker: dict) -> dict:
    """``T_wrist<-marker`` of one declared row, as a validated transform record."""
    rotation = marker.get('rotation')
    matrix = (np.asarray(rotation, dtype=float).reshape(3, 3) if rotation is not None
              else tracking.quaternion_xyzw_to_matrix(marker['quaternion_xyzw']))
    return {'matrix': pipeline.rotation_matrix_3x3(matrix),
            'translation_m': np.asarray(marker['position_m'], dtype=float).reshape(3),
            'convention': pipeline.FRAME_CONVENTION,
            'parent_frame': LAYOUT_FRAME,
            'child_frame': 'marker%d' % marker['idx'],
            'is_constant_extrinsic': True,
            'source': marker.get('source') or DECLARATION_SOURCE}


def wrist_pose_cam0_from_marker(marker_pose_cam0: dict, declaration: dict) -> dict:
    """``T_cam0<-wrist = T_cam0<-marker @ T_marker<-wrist`` for one declared tag.

    The declaration is treated as the fixed install pose: it is what turns "where the tag is in
    the picture" into "where the hand is in the visual frame".
    """
    install = (declaration if isinstance(declaration, dict) and 'matrix' in declaration
               else marker_in_wrist(declaration))
    composed = pipeline.compose(marker_pose_cam0, fiducial.invert_transform(install))
    composed.update({'parent_frame': 'cam0',
                     'child_frame': LAYOUT_FRAME,
                     'is_constant_extrinsic': False,
                     'source': 'fiducial_marker',
                     'quaternion_xyzw': list(tracking.matrix_to_quaternion_xyzw(
                         composed['matrix'])),
                     'position_unit': pipeline.POSITION_UNIT,
                     'quaternion_order': pipeline.QUATERNION_ORDER})
    return composed


# ---------------------------------------------------------------- printable tag per id

def marker_plate_png(idx: int, *, pixels: int = 1024, quiet_modules: int = fiducial.QUIET_ZONE,
                     dictionary_id: int | None = None) -> bytes:
    """A printable PNG of one declared id, with the quiet zone the detector needs.

    A bare ``generateImageMarker`` output has no quiet zone and AprilTag will not detect it, so the
    border is part of the printable image (and of the printed side length).
    """
    import cv2

    if not 0 <= int(idx) <= IDX_MAX:
        raise ValueError(f'marker idx must be in 0..{IDX_MAX}, got {idx!r}')
    dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id or cv2.aruco.DICT_APRILTAG_36h11)
    core = cv2.aruco.generateImageMarker(dictionary, int(idx), int(pixels))
    border = max(8, int(pixels) // 8)
    plate = np.pad(core, border * max(1, int(quiet_modules)), mode='constant', constant_values=255)
    ok, buffer = cv2.imencode('.png', plate)
    if not ok:
        raise ValueError('could not encode the marker image as PNG')
    return buffer.tobytes()


# ---------------------------------------------------------------- detection and the hand pose

def detect_markers(image, camera, layout: dict) -> dict:
    """Detect every tag in one image and solve the pose of the **declared** ones.

    A detected id that the table does not declare is reported in ``undeclared_ids``: it is not
    dropped and it is not matched to the nearest row. A declared id that is not seen produces no
    pose entry at all.
    """
    declared = {row['idx']: row for row in validate_layout(layout)['markers']}
    found = fiducial.detect_marker(image)
    seen = []
    undeclared = []
    for detection in found:
        if detection['id'] not in declared:
            undeclared.append({'id': detection['id'], 'corners_px': detection['corners_px'],
                               'note': 'detected but not declared in the marker table'})
            continue
        row = declared[detection['id']]
        pose = fiducial.marker_pose_cam(detection['corners_px'], camera, row['side_m'])
        seen.append({'idx': row['idx'], 'side_m': row['side_m'],
                     'corners_px': detection['corners_px'],
                     'reprojection_error_px': pose['reprojection_error_px'],
                     'planar_ambiguity_note': pose['planar_ambiguity_note'],
                     'marker_pose_cam0': pose,
                     'declaration': {'position_m': row['position_m'],
                                     'quaternion_xyzw': row['quaternion_xyzw'],
                                     'note': row['note'], 'source': row['source']}})
    return {'declared_ids': sorted(declared),
            'detected': seen,
            'detected_ids': [row['idx'] for row in seen],
            'undeclared_ids': [row['id'] for row in undeclared],
            'undeclared': undeclared,
            'missing_ids': sorted(set(declared) - {row['idx'] for row in seen})}


def _angle_deg(a, b) -> float:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    cosine = float(np.clip(float(a @ b) / (np.linalg.norm(a) * np.linalg.norm(b)), -1.0, 1.0))
    return float(np.degrees(np.arccos(cosine)))


def _rotation_distance_deg(first, second) -> float:
    """Compare complete orientations, including rotation about a shared first axis."""
    relative = np.asarray(first, dtype=float).T @ np.asarray(second, dtype=float)
    cosine = np.clip((np.trace(relative) - 1.0) / 2.0, -1.0, 1.0)
    return float(np.degrees(np.arccos(cosine)))


def registration_report(detections, layout: dict, *, camera_note: dict | None = None) -> dict:
    """``T_cam0<-wrist`` per declared tag seen, plus the cross-tag agreement between them.

    ``detections`` is the payload of :func:`detect_markers`. The agreement block is a
    self-consistency figure for the declared table: two tags give two independent
    ``T_cam0<-wrist`` estimates, and their difference is published whether or not it is small.
    """
    rows = {row['idx']: row for row in validate_layout(layout)['markers']}
    if isinstance(detections, dict):
        seen = detections.get('detected') or []
        undeclared = detections.get('undeclared_ids') or []
    else:
        seen = list(detections or [])
        undeclared = []
    missing = sorted(set(rows) - {detection['idx'] for detection in seen})
    estimates = []
    for detection in seen:
        row = rows.get(detection['idx'])
        if row is None:
            raise ValueError(f"detection for an undeclared marker id {detection['idx']}")
        pose = detection.get('marker_pose_cam0')
        if pose is None:
            raise ValueError('every detection needs a marker_pose_cam0 record')
        wrist = wrist_pose_cam0_from_marker(pose, marker_in_wrist(row))
        estimates.append({'idx': row['idx'], 'side_m': row['side_m'],
                          'declared_position_m': row['position_m'],
                          'reprojection_error_px': detection.get('reprojection_error_px'),
                          'marker_pose_cam0': {'matrix': np.asarray(pose['matrix']).tolist(),
                                               'translation_m': [float(v) for v in
                                                                 pose['translation_m']]},
                          'wrist_pose_cam0': {'matrix': np.asarray(wrist['matrix']).tolist(),
                                              'translation_m': [float(v) for v in
                                                                wrist['translation_m']],
                                              'quaternion_xyzw': wrist['quaternion_xyzw'],
                                              'parent_frame': 'cam0',
                                              'child_frame': LAYOUT_FRAME,
                                              'convention': pipeline.FRAME_CONVENTION}})
    pairs = []
    for index, first in enumerate(estimates):
        for second in estimates[index + 1:]:
            a = np.asarray(first['wrist_pose_cam0']['matrix'], dtype=float)
            b = np.asarray(second['wrist_pose_cam0']['matrix'], dtype=float)
            ta = np.asarray(first['wrist_pose_cam0']['translation_m'], dtype=float)
            tb = np.asarray(second['wrist_pose_cam0']['translation_m'], dtype=float)
            pairs.append({'a': first['idx'], 'b': second['idx'],
                          'rotation_difference_deg': _rotation_distance_deg(a, b),
                          'translation_difference_m': float(np.linalg.norm(ta - tb))})
    if not estimates:
        status, reason = 'no_marker', 'no declared tag was detected, so there is no pose'
    elif len(estimates) == 1:
        status, reason = ('single_marker_no_cross_check',
                          'one tag gives one estimate; the declared table is unchecked and the '
                          'single-view planar pose ambiguity is unresolved')
    else:
        worst_rotation = max(pair['rotation_difference_deg'] for pair in pairs)
        worst_translation = max(pair['translation_difference_m'] for pair in pairs)
        if (worst_rotation <= AGREEMENT_ROTATION_DEG
                and worst_translation <= AGREEMENT_TRANSLATION_M):
            status = 'consistent'
            reason = (f'{len(estimates)} tags agree within {worst_rotation:.2f} deg / '
                      f'{worst_translation * 1000.0:.1f} mm')
        else:
            status = 'disagree'
            reason = (f'{len(estimates)} tags disagree by up to {worst_rotation:.2f} deg / '
                      f'{worst_translation * 1000.0:.1f} mm: the declared table and the tag poses '
                      'do not describe the same rigid hand')
    return {'parent_frame': 'cam0',
            'child_frame': LAYOUT_FRAME,
            'convention': pipeline.FRAME_CONVENTION,
            'position_unit': pipeline.POSITION_UNIT,
            'quaternion_order': pipeline.QUATERNION_ORDER,
            'source': 'fiducial_marker',
            'evidence': 'operator_declaration_not_measured',
            'is_estimate': True,
            'estimates': estimates,
            'estimate_count': len(estimates),
            'agreement': {'status': status, 'reason': reason,
                          'thresholds': {'rotation_deg': AGREEMENT_ROTATION_DEG,
                                         'translation_m': AGREEMENT_TRANSLATION_M},
                          'pairs': pairs,
                          'is_accuracy_claim': False,
                          'note': ('cross-tag agreement is a self-consistency check of the '
                                   'declared table; a common rigid error in every row would still '
                                   'agree')},
            'undeclared_ids': list(undeclared),
            'missing_ids': list(missing),
            'camera': camera_note or {},
            'notes': ['the table is a declaration typed on the calibration page, not a measurement',
                      'the tag pose comes from one view, so the planar two-solution ambiguity is '
                      'reported and not resolved here',
                      'no smoothing, re-use or extrapolation: an unseen tag has no pose'],
            'remaining_user_actions': ['print the tag for the id at 100% scale and mount it flat',
                                       'confirm the declared offset of every id on the hand',
                                       'hold out poses to check the table, reported separately '
                                       'from the declaration']}


def registration_from_image(image, camera, layout: dict, *, camera_note: dict | None = None) -> dict:
    """Detect the declared tags in one image and return the hand-in-camera registration."""
    detections = detect_markers(image, camera, layout)
    report = registration_report(detections, layout, camera_note=camera_note)
    report['detections'] = {'declared_ids': detections['declared_ids'],
                            'detected_ids': detections['detected_ids'],
                            'undeclared_ids': detections['undeclared_ids'],
                            'missing_ids': detections['missing_ids']}
    return report


def decode_jpeg_bytes(payload) -> np.ndarray:
    """Decode a JPEG/PNG byte string into a grayscale image for tag detection.

    Kept here (not in the server) so the image codec stays with the detection code, and so the
    same path serves a live preview frame and a stored session frame.
    """
    import cv2

    buffer = np.frombuffer(payload, dtype=np.uint8)
    image = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
    if image is None:
        raise ValueError('the frame could not be decoded as an image')
    return image


def scale_for_image(camera, image_shape) -> float:
    """The scale between a camera's calibrated frame and the image being processed.

    The station records 960x540 derived frames next to 1920x1080 raw ones, and the live preview
    follows the device frame. Solving a pose on an image whose size differs from the calibration
    without this factor would silently distort it, so the factor is computed and reported instead
    of being assumed to be 1.
    """
    width = int(image_shape[1])
    if not camera.width:
        raise ValueError('the camera carries no calibrated width, so an image scale cannot be '
                         'derived')
    return width / float(camera.width)


def _axis_key(axis) -> str:
    """``'+x'``/``'-y'``/... for one dominant wrist axis, so a page can label it in its own words."""
    for index, name in enumerate('xyz'):
        if axis[index]:
            return ('+' if axis[index] > 0 else '-') + name
    return ''


def _dominant_axis(vector) -> tuple:
    """The wrist axis a declared direction lies on, plus its residual angle in degrees."""
    vector = np.asarray(vector, dtype=float)
    index = int(np.argmax(np.abs(vector)))
    key = [0, 0, 0]
    key[index] = 1 if vector[index] >= 0 else -1
    residual = _angle_deg(vector, np.asarray(key, dtype=float))
    return tuple(key), residual


def marker_footprint(row: dict, matrix) -> dict:
    """Where the declared square's corners land along the hand-back normal, as arithmetic.

    A tag is a square of side ``side_m`` centred on the declared position, so its extremes along
    wrist +x are the centre plus/minus the half side projected onto that axis. A lowest edge below
    ``x = 0`` means the declared centre puts part of the printed square *inside* the hand, which is
    a mounting nobody can build: the row is reported, not silently corrected.
    """
    half = float(row['side_m']) / 2.0
    position = [float(value) for value in row['position_m']]
    along_back_normal = half * (abs(float(matrix[0, 0])) + abs(float(matrix[0, 1])))
    lowest = float(position[0] - along_back_normal)
    return {'half_side_m': half,
            'extent_along_back_normal_m': float(along_back_normal),
            'lowest_edge_x_m': lowest,
            'highest_edge_x_m': float(position[0] + along_back_normal),
            'dips_below_hand_back': bool(lowest < 0.0),
            'note': ('the declared square centred on position_m with side_m; the lowest edge below '
                     'x = 0 would sit inside the hand, so either the offset refers to that edge or '
                     'the printed side is smaller')}


def orientation_summary(row: dict) -> dict:
    """What a row's declared quaternion means, as arithmetic on that declaration.

    This states the posture that was declared (which wrist axis the printed face normal lies on,
    which way the printed +x runs, and whether the tag plane is perpendicular to the back of the
    hand). It is arithmetic on the operator's numbers, so it is still a declaration and never a
    measurement of where the tag actually is.
    """
    matrix = (np.asarray(row['rotation'], dtype=float).reshape(3, 3)
              if row.get('rotation') is not None
              else tracking.quaternion_xyzw_to_matrix(row['quaternion_xyzw']))
    normal = -matrix[:, 2]                       # printed face normal is -marker z (measured)
    in_plane_x = matrix[:, 0]                   # marker +x: along the printed square, printed right
    normal_axis, normal_residual = _dominant_axis(normal)
    x_axis, x_residual = _dominant_axis(in_plane_x)
    to_back_normal = _angle_deg(normal, np.array([1.0, 0.0, 0.0]))
    return {'face_normal_wrist': [float(v) for v in normal],
            'face_normal_axis': AXIS_LABELS[normal_axis],
            'face_normal_axis_key': _axis_key(normal_axis),
            'face_normal_off_axis_deg': normal_residual,
            'marker_x_wrist': [float(v) for v in in_plane_x],
            'marker_x_axis': AXIS_LABELS[x_axis],
            'marker_x_axis_key': _axis_key(x_axis),
            'marker_x_off_axis_deg': x_residual,
            'angle_to_back_of_hand_normal_deg': to_back_normal,
            'parallel_to_back_of_hand': (abs(normal_residual) <= ORIENTATION_AXIS_TOL_DEG
                                         and normal_axis == (1, 0, 0)),
            'perpendicular_to_back_of_hand': (abs(to_back_normal - 90.0)
                                              <= ORIENTATION_AXIS_TOL_DEG),
            'axis_tolerance_deg': ORIENTATION_AXIS_TOL_DEG,
            'marker_axis_convention': MARKER_AXIS_CONVENTION,
            'is_measurement': False,
            'tag_footprint': (marker_footprint(row, matrix)
                              if row.get('side_m') and row.get('position_m') else None),
            'note': ('arithmetic on the declared quaternion: it says which posture was declared, '
                     'not where the tag was measured to be')}


def layout_summary(layout: dict) -> dict:
    """The page-facing view of the declared table (metres and millimetres, plus a print link)."""
    normalised = validate_layout(layout)
    return {'schema': normalised['schema'],
            'frame': normalised['frame'],
            'frame_note': normalised['frame_note'],
            'evidence': normalised['evidence'],
            'source': normalised['source'],
            'declared_count': normalised['declared_count'],
            'defaults_note': normalised['defaults_note'],
            'marker_axis_convention': MARKER_AXIS_CONVENTION,
            'notes': normalised['notes'],
            'ids_note': normalised['ids_note'],
            'rows': [{'idx': row['idx'],
                      'side_m': row['side_m'],
                      'side_mm': row['side_m'] * 1000.0,
                      'position_m': row['position_m'],
                      'position_mm': [value * 1000.0 for value in row['position_m']],
                      'quaternion_xyzw': row['quaternion_xyzw'],
                      'orientation': orientation_summary(row),
                      'note': row['note'],
                      'source': row['source'],
                      'print_url': '/api/handpose/marker.png?idx=%d' % row['idx']} for row in
                     normalised['markers']]}
