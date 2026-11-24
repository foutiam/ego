"""Calibration fingerprint and session-meta helpers.

Real calibration placement (checkerboard / depth plane measurement) is a user
action and is never fabricated here; these helpers only verify that recorded
metadata is internally complete and parseable.
"""
from __future__ import annotations

import json
from pathlib import Path

REQUIRED_META_KEYS = ('session_id', 'format_version', 'start_host_ns',
                      'start_mono_ns', 'status')

REQUIRED_CALIBRATION_FIELDS = ('model', 'width', 'height',
                               'fx', 'fy', 'cx', 'cy')


def read_meta(session_dir: str | Path) -> dict:
    p = Path(session_dir) / 'metadata.json'
    return json.loads(p.read_text(encoding='utf-8'))


def read_calibrations(session_dir: str | Path) -> dict:
    meta = read_meta(session_dir)
    return meta.get('calibrations', {}) if isinstance(meta, dict) else {}


def check_meta(session_dir: str | Path) -> dict:
    meta = read_meta(Path(session_dir))
    missing = [k for k in REQUIRED_META_KEYS if k not in meta]
    return {'pass': not missing, 'missing_keys': missing,
            'status': meta.get('status'), 'format_version': meta.get('format_version')}


def check_calibrations(session_dir: str | Path) -> dict:
    cals = read_calibrations(session_dir)
    report = {}
    for stream, cal in cals.items():
        missing = [k for k in REQUIRED_CALIBRATION_FIELDS if k not in cal]
        report[stream] = {
            'pass': not missing,
            'missing_fields': missing,
            'fingerprint': cal.get('fingerprint'),
            'fingerprint_source': cal.get('fingerprint_source', 'unknown'),
        }
    return {'pass': all(v['pass'] for v in report.values()), 'streams': report}


def intrinsics_fingerprint(cal: dict) -> str:
    """Stable short fingerprint of intrinsic parameters (order-independent).

    Used to detect calibration drift between sessions without pretending any
    physical measurement happened.
    """
    import hashlib
    items = sorted((k, cal[k]) for k in ('model', 'width', 'height', 'fx', 'fy', 'cx', 'cy')
                   if k in cal)
    payload = json.dumps(items, sort_keys=True).encode('utf-8')
    return hashlib.sha256(payload).hexdigest()[:16]
