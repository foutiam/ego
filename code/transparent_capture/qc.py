"""Quality gates for transparent-depth capture sessions.

Thresholds live in docs/transparent-depth/capture_qc_rules.md; every check here
reports (pass, value) so failures carry their measured value for review.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

# Default thresholds; override via rules dict.
DEFAULT_RULES = {
    'blur_max_laplacian_variance': 50.0,   # per-view sharpness lower bound
    'min_rgb_exposure': 0.01,              # reject frames darker than 1% mean
    'max_rgb_exposure': 0.98,              # reject saturated frames
    'min_depth_valid_ratio': 0.05,         # fraction of valid depth pixels
    'max_depth_valid_ratio': 0.98,         # implausible full-validity frames
    'min_depth_m': 0.1,
    'max_depth_m': 20.0,
    'max_sync_offset_ms': 12.0,
}


def _throw(value):
    raise ValueError(f'missing required session field: {value}')


def rgb_frame_metrics(rgb: np.ndarray) -> dict:
    """rgb: HxWx3 uint8. Returns sharpness/exposure metrics.

    Laplacian variance uses the conventional 0-255 grayscale scale so the
    authoritative threshold (50.0 in capture_qc_rules.md) is meaningful; on a
    [0,1] scale the variance is bounded by 16 and the rule would be unsatisfiable.
    """
    gray = rgb.mean(axis=2)
    # Laplacian-based sharpness (second central difference), no external blurs
    lap = (-4 * gray
           + np.roll(gray, 1, 0) + np.roll(gray, -1, 0)
           + np.roll(gray, 1, 1) + np.roll(gray, -1, 1))
    return {
        'laplacian_variance': float(lap.var()),
        'mean_exposure': float(rgb.mean() / 255.0),
    }


def depth_frame_metrics(depth_m: np.ndarray, rules=None) -> dict:
    """depth_m: HxW float metres; non-finite/0 = invalid."""
    rules = rules or DEFAULT_RULES
    finite = np.isfinite(depth_m)
    in_range = finite & (depth_m >= rules['min_depth_m']) & (depth_m > 0) & (depth_m <= rules['max_depth_m'])
    valid = in_range & (depth_m > 0)
    ratio = float(valid.mean())
    return {
        'valid_ratio': ratio,
        'depth_mean': float(depth_m[valid].mean()) if valid.any() else float('nan'),
    }


def check_rgb_frame(rgb: np.ndarray, rules=None) -> dict:
    rules = rules or DEFAULT_RULES
    m = rgb_frame_metrics(rgb)
    checks = {
        'sharp': (m['laplacian_variance'] >= rules['blur_max_laplacian_variance']),
        'not_dark': (m['mean_exposure'] >= rules['min_rgb_exposure']),
        'not_saturated': (m['mean_exposure'] <= rules['max_rgb_exposure']),
    }
    m.update({'checks': checks, 'pass': all(checks.values())})
    return m


def check_depth_frame(depth_m: np.ndarray, rules=None) -> dict:
    rules = rules or DEFAULT_RULES
    m = depth_frame_metrics(depth_m, rules)
    checks = {
        'enough_valid': (m['valid_ratio'] >= rules['min_depth_valid_ratio']),
        'not_suspiciously_full_valid': (m['valid_ratio'] <= rules['max_depth_valid_ratio']),
    }
    m.update({'checks': checks, 'pass': all(checks.values())})
    return m


def read_log_accounting(session_dir: str | Path, bound_ms: float | None = None) -> dict:
    """Per-stream accounting for what a `<stream>-reads.csv` can and cannot prove.

    Why the numbers are kept apart. The read log timestamps **reads**, not frames (its own
    reconciliation says so: one logged chunk can carry a partial frame or several frames), so
    consecutive rows legitimately share one `host_mono_ns`. And the monotonic clock a capture host
    stamps `host_mono_ns` with is not infinitely fine: on Windows `time.monotonic_ns()` is
    `GetTickCount64()`, resolution 15.625 ms, so `|host - mono|` moves by exactly one tick as that
    clock steps. Measured on the only real recording this direction has (2026-09-15, 5 s): the
    camera log's 5,188 rows hold 319 distinct mono values, every step is 0 or >= 15 ms, **no step is
    negative**, and the |host - mono| spread is 16.30 ms -- one tick, not drift.

    So a rule written as "monotonic and jitter <= 12 ms" is unsatisfiable on that host for a reason
    that says nothing about the recording. The accounting separates:

        shared_arrival_steps       equality, a property of the log: counted, never failed
        negative_steps             a real ordering violation: the only thing "monotonic" fails on
        clock_quantum_ns           the measured resolution of the stamping clock, from the log's own
                                   smallest positive step between distinct values
        jitter_unexplained_ms      the |host - mono| spread minus what one clock quantum explains

    and the declared bound is applied to `jitter_unexplained_ms`. The raw jitter, the measured
    quantum and the declared bound are all still reported, so a reader can disagree with the
    correction instead of having to trust it. `declared_bound_below_the_clock_resolution` says
    plainly when the bound cannot be met at this clock's resolution, which is a property of the
    recorder -- use a finer clock -- and not of any session.

    `monotonic` is kept in the output for existing callers and now means non-decreasing;
    `strictly_monotonic` carries the old meaning next to it.
    """
    session_dir = Path(session_dir)
    bound = DEFAULT_RULES['max_sync_offset_ms'] if bound_ms is None else float(bound_ms)
    report = {
        'streams': {}, 'pass': True, 'declared_bound_ms': bound,
        'rule': ('a read log may repeat a timestamp (it timestamps reads, not frames); a negative '
                 'step is an ordering violation; the declared bound applies to the part of the '
                 '|host - mono| spread that the measured clock resolution does not explain'),
    }
    reads = sorted(session_dir.glob('*-reads.csv'))
    if not reads:
        report['pass'] = False
        report['error'] = 'no stream read csv found'
        return report
    for csv_path in reads:
        import csv as _csv
        rows = list(_csv.DictReader(csv_path.open(encoding='utf-8')))
        if not rows:
            report['streams'][csv_path.name] = {'rows': 0, 'pass': False,
                                                'error': 'empty stream csv'}
            report['pass'] = False
            continue
        host = np.array([int(r['host_ns']) for r in rows], dtype=np.int64)
        mono = np.array([int(r['host_mono_ns']) for r in rows], dtype=np.int64)
        elapsed = np.array([float(r['elapsed_s']) for r in rows])
        steps = np.diff(mono)
        distinct = np.unique(mono)
        positive = np.diff(distinct)
        positive = positive[positive > 0]
        quantum_ns = int(positive.min()) if positive.size else None
        offsets = host - mono
        jitter_ns = int(offsets.max() - offsets.min()) if offsets.size > 1 else 0
        explained_ns = quantum_ns or 0
        unexplained_ns = max(0, jitter_ns - explained_ns)
        elapsed_steps = np.diff(elapsed)
        entry = {
            'rows': len(rows),
            'non_decreasing': bool(np.all(steps >= 0)),
            'strictly_monotonic': bool(np.all(steps > 0)),
            'monotonic': bool(np.all(steps >= 0)),
            'shared_arrival_steps': int((steps == 0).sum()),
            'negative_steps': int((steps < 0).sum()),
            'distinct_arrival_timestamps': int(distinct.size),
            'clock_quantum_ns': quantum_ns,
            'clock_quantum_ms': None if quantum_ns is None else quantum_ns / 1e6,
            'clock_jitter_ms': jitter_ns / 1e6,
            'jitter_explained_by_clock_resolution_ms': explained_ns / 1e6,
            'jitter_unexplained_ms': unexplained_ns / 1e6,
            'declared_bound_below_the_clock_resolution': bool(
                quantum_ns is not None and bound < quantum_ns / 1e6),
            'elapsed_non_decreasing': (bool(np.all(elapsed_steps >= 0))
                                       if elapsed_steps.size else True),
            'max_elapsed_step_ms': (float(np.abs(elapsed_steps).max() * 1e3)
                                    if elapsed_steps.size else 0.0),
        }
        entry['pass'] = bool(entry['non_decreasing']
                             and entry['jitter_unexplained_ms'] <= bound)
        report['streams'][csv_path.name] = entry
        if not entry['pass']:
            report['pass'] = False
    return report



def check_session_sync(session_dir: str | Path, tol_ms: float | None = None) -> dict:
    """Validates per-stream host/mono timestamp alignment and the ordering of the read logs.

    Reads the Session format written by code/visuotactile/storage.py
    (<stream>-reads.csv with host_ns, host_mono_ns, elapsed_s, offset, length). The accounting
    itself lives in :func:`read_log_accounting`, so this gate and the human-intake gate cannot
    disagree about what a read log proves; this wrapper only adds the session's identity.
    """
    from .calibration import read_meta
    session_dir = Path(session_dir)
    meta = read_meta(session_dir)
    report = {'session_id': meta.get('session_id', session_dir.name)}
    report.update(read_log_accounting(session_dir, bound_ms=tol_ms))
    return report


def describe_rules():
    return dict(DEFAULT_RULES)
