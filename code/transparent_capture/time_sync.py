"""Cross-modal time base: estimate the glove-vs-camera offset, or refuse to.

Why this exists
---------------
`CONTACT_TIME_AND_COORDINATE_CALIBRATION.md` states the gap plainly: a contact event is
localised in host time to about +-16 ms in the pilot session, the camera frames are
timestamped by a different process, and the glove frame carries **no device timestamp at
all**. Any claim that a glove event and a camera frame are simultaneous therefore needs a
measurement. This module is that measurement, and it is allowed to fail.

Method, declared before any number is read
------------------------------------------
1. Both streams are reduced to a scalar envelope on a common grid (default 5 ms): the
   glove envelope is the positive baseline-relative pressure response, the camera envelope
   is the frame-to-frame image difference energy. Both are median-centred.
2. The offset is the lag that maximises the normalised cross-correlation over a declared
   window (default +-250 ms).
3. The estimate is published **only** if the peak exceeds the declared ratio (default 3.0)
   over the noise floor, and only if a stimulus is present in both halves for the drift
   figure. Otherwise the result is `inconclusive` with the reason and the measured peak
   ratio, and `offset_ms` stays `None`.
4. Uncertainty is the grid step plus the half-width of the correlation plateau, not a
   fabricated precision. Drift is the difference between the two halves' offsets per
   minute, and it is `None` unless both halves carry their own significant peak.

Sign convention: the reported offset is the lag applied to the **glove** envelope to align
it with the **camera** envelope, so a positive offset means the glove signal arrived later
than the visual stimulus by that much.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

GRID_MS = 5.0
MAX_LAG_MS = 250.0
MIN_PEAK_RATIO = 3.0
MIN_HALF_SECONDS = 1.0
PLATEAU_FRACTION = 0.98

DEFAULT_RULES = {
    'grid_ms': GRID_MS, 'max_lag_ms': MAX_LAG_MS, 'min_peak_ratio': MIN_PEAK_RATIO,
    'min_half_seconds': MIN_HALF_SECONDS, 'plateau_fraction': PLATEAU_FRACTION,
    'sign_convention': ('offset is the lag applied to the glove envelope to align it with '
                        'the camera envelope; positive means the glove signal is later'),
}


def bin_series(times_s, values, *, grid_s, span_s=None):
    """Mean-bin a series onto a uniform grid. Empty bins are 0 and are reported as empty.

    The count of contributing samples per bin is returned so a caller can see whether a bin
    is an average of one sample or of thirty, instead of assuming a uniform rate.
    """
    times = np.asarray(times_s, dtype=float)
    vals = np.asarray(values, dtype=float)
    if times.size != vals.size:
        raise ValueError('times and values must have the same length')
    finite = np.isfinite(times) & np.isfinite(vals)
    times, vals = times[finite], vals[finite]
    origin = float(times.min()) if times.size else 0.0
    if span_s is None:
        span_s = (float(times.max()) - origin) if times.size else 0.0
    n = int(math.floor(span_s / grid_s)) + 1
    sums = np.zeros(n)
    counts = np.zeros(n, dtype=int)
    if times.size:
        index = np.clip(np.floor((times - origin) / grid_s).astype(int), 0, n - 1)
        np.add.at(sums, index, vals)
        np.add.at(counts, index, 1)
    series = np.zeros(n)
    nz = counts > 0
    series[nz] = sums[nz] / counts[nz]
    return {'grid_s': grid_s, 'origin_s': origin, 'series': series,
            'counts': counts, 'span_s': float(span_s)}


def _centre(series):
    values = np.asarray(series, dtype=float)
    if values.size == 0:
        return values
    values = values - float(np.median(values))
    scale = float(np.max(np.abs(values)))
    return values / scale if scale > 0 else values


def cross_correlation(a, b, *, max_lag_bins):
    """Normalised cross-correlation of two centred series over a symmetric lag window."""
    a = _centre(a)
    b = _centre(b)
    n = min(a.size, b.size)
    a, b = a[:n], b[:n]
    lags = np.arange(-int(max_lag_bins), int(max_lag_bins) + 1)
    out = np.zeros(lags.size)
    for i, lag in enumerate(lags):
        if lag >= 0:
            left, right = a[lag:], (b[:n - lag] if lag else b)
        else:
            left, right = a[:n + lag], b[-lag:]
        if left.size < 2:
            continue
        denom = float(np.sqrt(np.sum(left * left) * np.sum(right * right)))
        out[i] = float(np.sum(left * right) / denom) if denom > 0 else 0.0
    return lags, out


def _peak_ratio(correlation):
    """Peak over the noise floor: median absolute correlation plus a MAD term.

    A single strong correlation peak is what a synchronisation stimulus looks like; a flat
    or noisy correlation is what an unsynchronised recording looks like, and this ratio is
    what separates them instead of a threshold on the peak alone.
    """
    absolute = np.abs(np.asarray(correlation, dtype=float))
    if absolute.size == 0:
        return 0.0, 0.0
    median = float(np.median(absolute))
    mad = float(np.median(np.abs(absolute - median)))
    floor = median + mad
    peak = float(np.max(absolute))
    return peak, (peak / floor if floor > 0 else float('inf'))


def estimate_offset(glove_times_s, glove_values, camera_times_s, camera_values, *,
                    rules=None):
    """The declared procedure, with its refusal conditions applied."""
    rules = dict(DEFAULT_RULES, **(rules or {}))
    grid_s = float(rules['grid_ms']) / 1000.0
    glove_times_s = np.asarray(glove_times_s, dtype=float)
    camera_times_s = np.asarray(camera_times_s, dtype=float)
    span = max(float(glove_times_s.max()) if glove_times_s.size else 0.0,
               float(camera_times_s.max()) if camera_times_s.size else 0.0)
    glove = bin_series(glove_times_s, glove_values, grid_s=grid_s, span_s=span)
    camera = bin_series(camera_times_s, camera_values, grid_s=grid_s, span_s=span)
    result = {
        'rules': rules,
        'grid_bins': int(glove['series'].size),
        'glove_samples': int(np.sum(glove['counts'] > 0)),
        'camera_samples': int(np.sum(camera['counts'] > 0)),
        'span_s': round(span, 6),
        'offset_ms': None, 'uncertainty_ms': None, 'peak_ratio': None,
        'drift_ms_per_min': None, 'status': 'inconclusive', 'reason': None,
        'published': False,
    }
    if glove['series'].size < 8 or camera['series'].size < 8:
        result['reason'] = 'too few samples on the common grid to correlate'
        return result
    max_lag_bins = max(1, int(round((float(rules['max_lag_ms']) / 1000.0) / grid_s)))
    lags, corr = cross_correlation(glove['series'], camera['series'],
                                   max_lag_bins=max_lag_bins)
    peak, ratio = _peak_ratio(corr)
    result['peak_ratio'] = round(ratio, 4) if math.isfinite(ratio) else None
    best = int(np.argmax(np.abs(corr)))
    offset_s = float(lags[best]) * grid_s
    plateau = (np.abs(corr) >= (PLATEAU_FRACTION * peak) if peak > 0
               else np.zeros_like(corr, dtype=bool))
    half_width = 0.5 * float(np.sum(plateau)) * grid_s
    if not math.isfinite(ratio) or ratio < float(rules['min_peak_ratio']):
        result['reason'] = (
            'the correlation peak does not stand out from the noise floor (peak ratio %s '
            '< %s), so this recording does not localise a shared stimulus'
            % (result['peak_ratio'], rules['min_peak_ratio']))
        return result
    result.update(offset_ms=round(offset_s * 1000.0, 3),
                  uncertainty_ms=round((grid_s + half_width) * 1000.0, 3),
                  status='estimated', published=True,
                  reason='a single significant peak: offset and plateau width reported')

    halves = []
    half_bins = glove['series'].size // 2
    for start, stop in ((0, half_bins), (half_bins, glove['series'].size)):
        segment = glove['series'][start:stop]
        segment_camera = camera['series'][start:stop]
        if segment.size < 4 or float(np.max(np.abs(segment))) == 0.0 \
                or float(np.max(np.abs(segment_camera))) == 0.0:
            halves.append(None)
            continue
        sub_lags, sub_corr = cross_correlation(segment, segment_camera,
                                               max_lag_bins=max_lag_bins)
        sub_peak, sub_ratio = _peak_ratio(sub_corr)
        if not math.isfinite(sub_ratio) or sub_ratio < float(rules['min_peak_ratio']):
            halves.append(None)
            continue
        halves.append(float(sub_lags[int(np.argmax(np.abs(sub_corr)))]) * grid_s)
    result['half_offsets_ms'] = [None if half is None else round(half * 1000.0, 3)
                                 for half in halves]
    if all(half is not None for half in halves) and span >= float(rules['min_half_seconds']) * 2:
        delta_s = span / 2.0
        result['drift_ms_per_min'] = round(
            (halves[1] - halves[0]) * 1000.0 / (delta_s / 60.0), 6)
    else:
        result['drift_reason'] = ('each half needs its own significant peak before a drift '
                                  'figure means anything')
    return result


# ------------------------------------------------------------------ session adapters
ROOT = Path(__file__).resolve().parents[2]
STATION_CONFIG = ROOT / 'code/ego_capture/capture_config.json'


def _station_config(path=None):
    path = Path(path) if path else STATION_CONFIG
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def glove_envelope(session_dir, byte_offsets=None):
    """Glove envelope in host seconds: summed positive baseline-relative response.

    Declared rule: for each configured touch byte, the session-wide median of that byte is
    the baseline, and the envelope is the sum over bytes of `max(0, raw - median)`. Raw
    counts, no unit conversion, no threshold: this is an envelope for correlation, not a
    contact decision.
    """
    from .glove_stream import pressure_series
    series = pressure_series(session_dir)
    grids = np.asarray(series.grids)
    if grids.size == 0:
        raise ValueError('the session has no accepted glove frames')
    offsets = list(byte_offsets or _station_config().get('contact_byte_offsets') or [])
    flat = grids.reshape(grids.shape[0], -1)
    columns = [int(o) for o in offsets if 0 <= int(o) < flat.shape[1]]
    if not columns:
        columns = list(range(flat.shape[1]))
    block = flat[:, columns].astype(float)
    baseline = np.median(block, axis=0)
    envelope = np.clip(block - baseline, 0.0, None).sum(axis=1)
    times_ns = np.asarray(series.host_ns, dtype=float)
    finite = np.isfinite(times_ns) & (times_ns > 0)
    if not finite.any():
        raise ValueError('no glove frame carries an arrival time')
    times_s = (times_ns[finite] - times_ns[finite].min()) / 1e9
    return {'times_s': times_s, 'values': envelope[finite],
            'rule': ('sum over configured touch bytes of max(0, raw - per-byte session '
                     'median); raw uint8 counts, no unit conversion'),
            'bytes_used': len(columns), 'frames': int(finite.sum())}


def camera_envelope(session_dir):
    """Camera envelope: mean absolute grayscale difference between sampled frames.

    The station's derived frames are the sampled ones (`derived/frames.jsonl`), so this is
    an envelope at the sampling rate, not at the stream rate, and the report says so.
    """
    from PIL import Image
    session_dir = Path(session_dir)
    path = session_dir / 'derived/frames.jsonl'
    if not path.is_file():
        raise ValueError('the session has no derived/frames.jsonl for a camera envelope')
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
            if line.strip()]
    rows.sort(key=lambda row: float(row.get('t_s', 0.0)))
    times, values, previous = [], [], None
    for row in rows:
        image_path = session_dir / 'derived' / str(row.get('image'))
        if not image_path.is_file():
            continue
        with Image.open(image_path) as handle:
            small = np.asarray(handle.convert('L').resize((160, 90)), dtype=float)
        if previous is not None:
            times.append(float(row['t_s']))
            values.append(float(np.mean(np.abs(small - previous))))
        previous = small
    if not times:
        raise ValueError('no consecutive sampled frames to difference')
    return {'times_s': np.asarray(times, dtype=float), 'values': np.asarray(values, dtype=float),
            'rule': ('mean absolute grayscale difference (160x90) between consecutive '
                     'derived sampled frames; envelope at the sampling rate, not the stream '
                     'rate'),
            'frames': len(times)}


def analyze_session(session_dir, *, rules=None):
    """Run the declared estimator on one real session and describe both envelopes."""
    glove = glove_envelope(session_dir)
    camera = camera_envelope(session_dir)
    estimate = estimate_offset(glove['times_s'], glove['values'],
                               camera['times_s'], camera['values'], rules=rules)
    return {'session': Path(session_dir).name,
            'glove_envelope': {key: value for key, value in glove.items()
                               if key not in ('times_s', 'values')},
            'camera_envelope': {key: value for key, value in camera.items()
                                if key not in ('times_s', 'values')},
            'estimate': estimate,
            'interpretation': ('an estimated offset is a measured clock relation for this '
                               'recording; an inconclusive result means this recording does '
                               'not support one and no value is published')}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='glove/camera time-base estimate')
    parser.add_argument('session_id')
    parser.add_argument('--root', default=str(ROOT / 'outputs/capture-station'))
    parser.add_argument('--out', default=None)
    args = parser.parse_args(argv)
    session_dir = Path(args.root) / 'sessions' / args.session_id
    report = analyze_session(session_dir)
    out = Path(args.out) if args.out else (Path(args.root) / 'reports'
                                           / ('%s-time-sync.report.json' % args.session_id))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'session': report['session'],
                      'status': report['estimate']['status'],
                      'offset_ms': report['estimate']['offset_ms'],
                      'uncertainty_ms': report['estimate']['uncertainty_ms'],
                      'peak_ratio': report['estimate']['peak_ratio'],
                      'reason': report['estimate']['reason'],
                      'report': str(out)}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
