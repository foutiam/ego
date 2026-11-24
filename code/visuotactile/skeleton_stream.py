"""Record schema for the OpenXR-25 wrist-local hand skeleton stream.

Why this module exists
----------------------
The official documentation describes a device API ``device.skeleton().pose()`` returning a
``HandSkeletonPose``: 25 nodes in a wrist-local frame, metres, ``xyzw`` quaternions, wrist at
the origin, **no world pose**. Two things had to be settled before any tracking design could
rest on it:

1. whether the *installed* platform actually exposes that call. It does not: the audit in
   ``code/capture_station/hand_frame_audit.py`` probes ``qnbot_sdk`` 0.2.4,
   ``qnbot_hand_openxr_hand`` 0.1.6 and the engine packages and finds no ``skeleton``
   attribute at all; the 25 nodes the station shows are produced by
   ``code/capture_station/qnbot_retarget_bridge.py`` with first-party MuJoCo forward
   kinematics of the retargeted joint command (``create_skeleton``/``update_skeleton``).
2. how such a stream is stored so that no consumer has to guess. This module fixes the
   record: the frame identifier, the position unit and the quaternion order travel **with
   every record**, nodes are addressed **by name**, and a malformed stream raises instead of
   being silently repaired.

What the record deliberately does not contain
---------------------------------------------
No world-space translation and no wrist orientation in a global frame: the vendor states the
interface does not provide them, so a consumer that needs them must estimate them (that is
the job of ``code/capture_station/hand_openxr_tracking.py``). No IMU field either, so a
downstream reader cannot quietly build a position by double-integrating one.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

RECORD_VERSION = 1
RECORD_KIND = 'openxr_skeleton'

# Frame facts, stated by the vendor documentation and kept per record rather than assumed
# globally, because a second source (a future SDK build) could report different ones.
COORDINATE_FRAME = 'wrist_local'
POSITION_UNIT = 'm'
QUATERNION_ORDER = 'xyzw'
WORLD_POSE_AVAILABLE = False
WORLD_POSE_NOTE = ('the interface provides no world-space wrist translation and no global '
                   'wrist orientation; positions_local are relative to the wrist node')

# Canonical node order. The same tuple appears in the bridge (NODE_NAMES) and in the
# tracking module (OPENXR_NODE_NAMES); a test parses the bridge source and asserts that all
# three agree, so a rename in one place cannot silently change a stream's meaning.
NODE_NAMES = (
    'wrist',
    'thumb_metacarpal', 'thumb_proximal', 'thumb_distal', 'thumb_tip',
    'index_metacarpal', 'index_proximal', 'index_intermediate', 'index_distal', 'index_tip',
    'middle_metacarpal', 'middle_proximal', 'middle_intermediate', 'middle_distal', 'middle_tip',
    'ring_metacarpal', 'ring_proximal', 'ring_intermediate', 'ring_distal', 'ring_tip',
    'little_metacarpal', 'little_proximal', 'little_intermediate', 'little_distal', 'little_tip',
)
NODE_COUNT = len(NODE_NAMES)

HAND_SIDES = ('left', 'right')

# The wrist node is the origin by definition of the frame; the tolerance is numerical only.
WRIST_ORIGIN_TOLERANCE_M = 1e-6
# Unit-quaternion tolerance. The vendor states zero quaternions are rejected, so a record
# whose quaternion is not normalisable is an error, not a value to rescale.
QUATERNION_NORM_TOLERANCE = 1e-3


def node_index(name: str) -> int:
    """Index of a node name in the canonical order, or ValueError.

    Provided for serialisation only. Callers must address nodes **by name**: matching two
    different landmark sets by array position is exactly the mistake this module exists to
    prevent, because the two orders are not the same skeleton.
    """
    try:
        return NODE_NAMES.index(str(name))
    except ValueError as exc:
        raise ValueError(f'unknown OpenXR skeleton node: {name!r}') from exc


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) \
        and math.isfinite(float(value))


def _vector(value, size: int, what: str) -> list:
    if not isinstance(value, (list, tuple)) or len(value) != size:
        raise ValueError(f'{what} must be a sequence of {size} numbers')
    if not all(_finite(v) for v in value):
        raise ValueError(f'{what} contains a non-finite entry')
    return [float(v) for v in value]


def validate_nodes(names, positions, orientations) -> dict:
    """Validate one 25-node sample and return the measured sanity figures.

    Raises instead of repairing: a stream whose node set changed, whose wrist is not at the
    origin, or whose quaternion is not a unit quaternion is not a sample of the documented
    skeleton, and downstream geometry would be wrong in a way no residual would reveal.
    """
    names = [str(name) for name in (names or [])]
    if len(names) != NODE_COUNT:
        raise ValueError(f'expected {NODE_COUNT} node names, got {len(names)}')
    if len(set(names)) != NODE_COUNT:
        raise ValueError('node names are not unique')
    missing = [name for name in NODE_NAMES if name not in names]
    if missing:
        raise ValueError(f'node names missing from the sample: {missing}')
    if not isinstance(positions, (list, tuple)) or len(positions) != NODE_COUNT:
        raise ValueError(f'expected {NODE_COUNT} positions, got '
                         f'{0 if positions is None else len(positions)}')
    if not isinstance(orientations, (list, tuple)) or len(orientations) != NODE_COUNT:
        raise ValueError(f'expected {NODE_COUNT} orientations, got '
                         f'{0 if orientations is None else len(orientations)}')
    wrist_index = names.index('wrist')
    wrist = _vector(positions[wrist_index], 3, 'wrist position')
    wrist_offset = math.sqrt(sum(component * component for component in wrist))
    if wrist_offset > WRIST_ORIGIN_TOLERANCE_M:
        raise ValueError(f'the wrist node is not at the origin (|p| = {wrist_offset:g} m); '
                         'the frame identifier is therefore not wrist_local')
    worst_quaternion_error = 0.0
    for index, quaternion in enumerate(orientations):
        values = _vector(quaternion, 4, f'orientation[{names[index]}]')
        norm = math.sqrt(sum(component * component for component in values))
        if norm <= 0.0:
            raise ValueError(f'zero quaternion for node {names[index]!r}')
        worst_quaternion_error = max(worst_quaternion_error, abs(norm - 1.0))
    return {'wrist_offset_m': wrist_offset,
            'max_quaternion_norm_error': worst_quaternion_error,
            'names_in_canonical_order': names == list(NODE_NAMES)}


def skeleton_record(message, *, host_ns=None, host_mono_ns=None, session_id=None,
                    bridge_build=None, source='bridge_stdout') -> dict:
    """Normalise one bridge ``skeleton_pose`` message into a storable record.

    ``message`` is the ``value`` payload the bridge emits. Every field a reader needs to
    interpret the numbers (frame, unit, quaternion order, node names, hand side) is copied
    into the record; nothing is left to a global convention.
    """
    if not isinstance(message, dict):
        raise ValueError('skeleton_pose payload must be an object')
    frame = str(message.get('coordinate_frame') or '')
    if frame != COORDINATE_FRAME:
        raise ValueError(f'coordinate_frame {frame!r} is not {COORDINATE_FRAME!r}; a record '
                         'must not be written with a frame it did not come from')
    hand_side = str(message.get('hand_side') or '').lower()
    if hand_side not in HAND_SIDES:
        raise ValueError(f'hand_side {message.get("hand_side")!r} is not one of {HAND_SIDES}')
    names = [str(name) for name in (message.get('names') or [])]
    positions = [[float(component) for component in (row or [])]
                 for row in (message.get('positions_local') or [])]
    orientations = [[float(component) for component in (row or [])]
                    for row in (message.get('orientations_local_xyzw') or [])]
    sanity = validate_nodes(names, positions, orientations)
    timestamp = message.get('timestamp_sec')
    return {
        'record_version': RECORD_VERSION,
        'kind': RECORD_KIND,
        'source': source,
        'session_id': session_id,
        'bridge_build': bridge_build,
        'hand_side': hand_side,
        'coordinate_frame': COORDINATE_FRAME,
        'position_unit': POSITION_UNIT,
        'quaternion_order': QUATERNION_ORDER,
        'world_pose_available': WORLD_POSE_AVAILABLE,
        'world_pose_note': WORLD_POSE_NOTE,
        'node_count': NODE_COUNT,
        'names': names,
        'positions_local': positions,
        'orientations_local_xyzw': orientations,
        'bridge_timestamp_sec': float(timestamp) if _finite(timestamp) else None,
        'host_ns': int(host_ns) if _finite(host_ns) else None,
        'host_mono_ns': int(host_mono_ns) if _finite(host_mono_ns) else None,
        **sanity,
    }


def append_record(handle, record: dict) -> None:
    """Append one record to an open text handle as a single JSON line."""
    handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n')


def write_records(path, records) -> Path:
    """Write records to ``path`` atomically (temp file + replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w', encoding='utf-8') as handle:
        for record in records:
            append_record(handle, record)
    temp.replace(path)
    return path


def read_records(path) -> list:
    """Read records, rejecting any line whose schema does not match this module.

    A line that is not an OpenXR-25 record is an error rather than a skipped row: a
    half-matching file is how a coordinate-frame mix-up survives into a result.
    """
    path = Path(path)
    records = []
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError as exc:
            raise ValueError(f'{path}:{number} is not valid JSON: {exc}') from exc
        if record.get('kind') != RECORD_KIND:
            raise ValueError(f'{path}:{number} kind is {record.get("kind")!r}, '
                             f'expected {RECORD_KIND!r}')
        if record.get('coordinate_frame') != COORDINATE_FRAME:
            raise ValueError(f'{path}:{number} coordinate_frame is '
                             f'{record.get("coordinate_frame")!r}')
        if record.get('quaternion_order') != QUATERNION_ORDER:
            raise ValueError(f'{path}:{number} quaternion_order is '
                             f'{record.get("quaternion_order")!r}')
        validate_nodes(record.get('names'), record.get('positions_local'),
                       record.get('orientations_local_xyzw'))
        records.append(record)
    return records


def summarize(records) -> dict:
    """Counts and time span of a record list, for a report rather than for a fit."""
    records = list(records)
    stamps = [record['bridge_timestamp_sec'] for record in records
              if record.get('bridge_timestamp_sec') is not None]
    return {'record_count': len(records),
            'hand_sides': sorted({record.get('hand_side') for record in records}),
            'bridge_builds': sorted({record.get('bridge_build') for record in records
                                     if record.get('bridge_build')}),
            'coordinate_frame': COORDINATE_FRAME,
            'position_unit': POSITION_UNIT,
            'quaternion_order': QUATERNION_ORDER,
            'node_count': NODE_COUNT,
            'timestamp_span_s': (max(stamps) - min(stamps)) if len(stamps) > 1 else None,
            'nodes_in_canonical_order': all(record.get('names_in_canonical_order')
                                            for record in records) if records else None}
