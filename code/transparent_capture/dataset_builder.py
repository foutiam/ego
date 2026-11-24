"""Offline dataset-builder scaffolding for transparent-depth capture sessions.

Walks recorded sessions, applies the QC gates from
`docs/transparent-depth/capture_qc_rules.md` (qc.DEFAULT_RULES), and emits a
manifest of frames eligible for training-label extraction. The builder NEVER
performs manual measurements and never marks synthetic sessions as real
capture: sessions with evidence='synthetic_software_smoke' are emitted with
excluded_from_training_data=true.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .calibration import check_calibrations, check_meta, read_meta
from .loader import SessionLoader
from .qc import check_depth_frame, check_rgb_frame, check_session_sync


def decode_rgb_u8(payload: bytes, shape: tuple[int, int, int]) -> np.ndarray:
    """Decode a raw uint8 RGB payload."""
    arr = np.frombuffer(payload, dtype=np.uint8)
    return arr.reshape(shape)


def decode_depth_f32(payload: bytes, shape: tuple[int, int]) -> np.ndarray:
    """Decode a raw little-endian float32 metric-depth payload."""
    arr = np.frombuffer(payload, dtype='<f4')
    return arr.reshape(shape)


def build_manifest(sessions_root: str | Path, rgb_shape=(480, 640, 3),
                   depth_shape=(480, 640), rules=None) -> dict:
    """Build a QC-gated manifest for all sessions under a sessions root.

    Synthetic sessions are carried with excluded_from_training_data=true; the
    manifest records per-session QC verdicts so review stays offline and
    reproducible.
    """
    root = Path(sessions_root)
    manifest = {'sessions': [], 'format_version': 1}
    for session_dir in sorted((root / 'sessions').glob('*')):
        if not (session_dir / 'metadata.json').is_file():
            continue
        meta = read_meta(session_dir)
        entry = {
            'session_id': meta.get('session_id', session_dir.name),
            'evidence': meta.get('evidence', 'unknown'),
            'excluded_from_training_data':
                meta.get('evidence') == 'synthetic_software_smoke',
            'meta_check': check_meta(session_dir),
            'calibration_check': check_calibrations(session_dir),
            'sync_check': check_session_sync(session_dir),
            'frames': {'total': 0, 'rgb_pass': 0, 'depth_pass': 0, 'both_pass': 0},
        }
        ok = entry['meta_check']['pass'] and entry['sync_check']['pass']
        try:
            loader = SessionLoader(session_dir)
            for stream in loader.streams():
                for rec in loader.records(stream):
                    entry['frames']['total'] += 1
                    payload = loader.raw_payload(rec)
                    if stream.endswith('_rgb') or stream == 'camera0':
                        rgb = decode_rgb_u8(payload, rgb_shape)
                        if check_rgb_frame(rgb, rules)['pass']:
                            entry['frames']['rgb_pass'] += 1
                    else:
                        depth = decode_depth_f32(payload, depth_shape)
                        if check_depth_frame(depth, rules)['pass']:
                            entry['frames']['depth_pass'] += 1
            entry['frames']['both_pass'] = min(entry['frames']['rgb_pass'],
                                               entry['frames']['depth_pass'])
        except Exception as exc:  # keep the manifest informative, never crash the walk
            entry['error'] = repr(exc)
            ok = False
        entry['pass'] = bool(ok)
        manifest['sessions'].append(entry)
    manifest['pass'] = bool(manifest['sessions']) and all(s['pass'] for s in manifest['sessions'])
    return manifest


def write_manifest(manifest: dict, out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n',
                        encoding='utf-8')
    return out_path
