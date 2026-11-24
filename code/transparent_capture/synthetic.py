"""Deterministic synthetic session generator for offline pipeline verification.

These sessions are SYNTHETIC BY RECORDED FACT: metadata marks
evidence='synthetic_software_smoke' and carry no device identities, matching
the workspace rule that synthetic results are never passed off as real-device
capture or calibration.
"""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path

import numpy as np

from .calibration import intrinsics_fingerprint

RULES = dict(size=(64, 48), n_frames=8)


def _frame_bytes(seed, idx, kind):
    rng = np.random.default_rng(seed * 1009 + idx * 7)
    h, w = RULES['size']
    if kind == 'synthetic_rgb':
        arr = rng.integers(0, 128, size=(h, w, 3), dtype=np.uint8)
        return arr.tobytes()
    if kind == 'synthetic_depth':
        arr = (np.linspace(0.5, 8.0, num=h))[:, None] * np.ones((1, w))
        return arr.astype('<f4').view('<u1').tobytes()
    raise ValueError(kind)


def make_session(root: Path, seed: int = 0, calibrate: bool = True) -> Path:
    root = Path(root)
    sid = f'transparent-depth-synth-{seed:03d}'
    session_dir = root / 'sessions' / sid
    session_dir.mkdir(parents=True, exist_ok=False)
    start_host_ns = 1_700_000_000_000_000_000 + seed
    start_mono_ns = 100_000_000_000_000 + seed
    meta = {
        'session_id': sid,
        'format_version': 1,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'start_host_ns': start_host_ns,
        'start_mono_ns': start_mono_ns,
        'status': 'recorded',
        'evidence': 'synthetic_software_smoke',
        'excluded_from_training_data': True,
        'calibrations': {},
    }
    if calibrate:
        cal = {'model': 'pinhole', 'width': RULES['size'][0], 'height': RULES['size'][1],
               'fx': 40.0, 'fy': 40.0, 'cx': 32.0, 'cy': 24.0,
               'fingerprint_source': 'synthetic_fixed'}
        cal['fingerprint'] = intrinsics_fingerprint(cal)
        meta['calibrations']['camera0'] = cal
    for stream, kind in (('camera0', 'synthetic_rgb'), ('camera1', 'synthetic_depth')):
        bin_path = session_dir / f'{stream}.bin'
        with bin_path.open('wb') as bin_f, \
                (session_dir / f'{stream}-reads.csv').open('w', newline='', encoding='utf-8') as csv_f:
            w = csv.writer(csv_f)
            w.writerow(['host_ns', 'host_mono_ns', 'elapsed_s', 'offset', 'length'])
            offset = 0
            for idx in range(RULES['n_frames']):
                payload = _frame_bytes(seed, idx, kind)
                bin_f.write(payload)
                host_ns = start_host_ns + idx * 33_300_000      # 30 Hz
                mono_ns = start_mono_ns + idx * 33_300_000
                w.writerow([host_ns, mono_ns, (mono_ns - start_mono_ns) / 1e9, offset, len(payload)])
                offset += len(payload)
    (session_dir / 'metadata.json').write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    return session_dir
