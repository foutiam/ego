"""Sparse ContactEvent emission from a recorded glove session.

Implements the record defined in `docs/transparent-depth/CONTACT_EVENT_SCHEMA.md` over
already-recorded raw bytes. Read-only: it opens no device, writes nothing into the session
directory and converts nothing into a physical unit.

What it decides, and what it refuses to decide:

- **The threshold parameters are declared here, before any number is read.** Baseline
  window, delta threshold and debounce length are module constants, recorded in every
  output. A threshold chosen after looking at the data is not evidence, and a contact
  detector whose sensitivity is tuned on the same session it reports cannot be checked.
- **`unknown` is a legal state.** Before the baseline window is complete there is no
  delta to threshold, so `contact_state` is `unknown` rather than `off`: coercing it to
  `off` would invent a measurement.
- **No taxel-to-finger mapping is invented here, and none is duplicated.** The vendor figure
  and the first-party cued-press audit (2026-10-06) both locate the finger strip, and the
  cell-to-region identity for those cells lives in `configs/glove-tactile-layout-v1.json`;
  this emitter stays with taxel ids and leaves the resolution to a consumer that reads that
  file, so the mapping has exactly one definition. A taxel event therefore carries
  `finger: null` with the reason, `position_or_region: 'taxel:<i>'`, and `spatial_uncertainty`
  `null`. `pose_valid` is per *finger*, so for a taxel event it is `null` with the raw mask
  carried alongside; that deviation from the schema's declared type is documented in the
  schema file rather than left implicit.
- **`timestamp` is host arrival time**, relative to the first accepted frame, and the base
  is named. The wire frame carries no device timestamp, so device-time ordering is the
  counter pair, and the host-side timing uncertainty is measured rather than assumed.

The output is a sparse event stream: one `on` event per (cell, contact episode) and one
`off` event when it ends, not one event per frame.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

from .glove_stream import FINGER_NAMES, PRESSURE_UNITS, UNKNOWN, pressure_series

PROJECT = Path(__file__).resolve().parents[2]
DEFAULT_SESSION_ROOT = PROJECT / 'outputs/capture-station/sessions'
DEFAULT_OUTPUT_ROOT = PROJECT / 'outputs/capture-station/reports'

# Declared before any number is read. See the module docstring.
DECLARED_PARAMETERS = {
    'baseline_window_frames': 50,
    'baseline_rule': ('per-cell minimum over the declared baseline window, which is the '
                      'first N accepted frames of the session'),
    'delta_threshold_raw_counts': 8,
    'debounce_frames': 3,
    'state_before_the_baseline_window': 'unknown',
    'event_rule': 'one on event per contact episode and one off event when it ends',
    'units': PRESSURE_UNITS,
    'not_a_calibrated_force': ('raw uint8 counts above each cell\'s own baseline; the '
                               'vendor documents no unit, curve or zero level'),
    'sensor_id': 'qnbot_glove',
}


def _hand_name(hand_side: int) -> str:
    """`0=right, 1=left`, as the parser reports it from the wire frame."""
    return {0: 'right', 1: 'left'}.get(int(hand_side), 'UNKNOWN')


def _timing(series) -> dict:
    """Host-side timing facts, measured from the arrival times that were recorded."""
    stamps = [value for value in series.host_mono_ns if value is not None]
    if len(stamps) < 2:
        return {'time_base': None,
                'time_uncertainty_s': None,
                'time_uncertainty_basis': ('no host monotonic arrival times in this '
                                           'session, so no uncertainty can be measured'),
                'implied_hz': None}
    intervals = np.diff(np.asarray(stamps, dtype=np.float64)) / 1e9
    distinct = len(set(stamps))
    return {
        'time_base': 'host_monotonic_ns minus the first accepted frame (seconds)',
        'time_uncertainty_s': float(np.percentile(intervals, 95)),
        'time_uncertainty_basis': {
            'method': ('95th percentile of inter-frame host arrival intervals, mapped by '
                       'the read that completed each frame'),
            'mapping': ('read-log byte offsets, not read-log row index: one read can carry '
                        'a partial frame or several frames'),
            'frames_with_a_timestamp': len(stamps),
            'distinct_arrival_timestamps': distinct,
            'frames_per_distinct_timestamp': round(len(stamps) / distinct, 2),
            'arrival_clock_granularity_s': (float(np.median(intervals[intervals > 0]))
                                            if (intervals > 0).any() else None),
            'interval_ms': {'p50': float(np.percentile(intervals, 50) * 1e3),
                            'p95': float(np.percentile(intervals, 95) * 1e3),
                            'max': float(intervals.max() * 1e3)},
            'not_a_device_bound': ('the vendor documents no latency or jitter bound, so '
                                   'this is host-side only and says nothing about when '
                                   'the device sampled the frame'),
        },
        'implied_hz': (float(1.0 / np.median(intervals))
                       if intervals.size and np.median(intervals) > 0 else None),
        'implied_hz_reason': (None if intervals.size and np.median(intervals) > 0 else
                              'the arrival log records one timestamp per read, not per '
                              'frame, so several frames share a timestamp and no '
                              'per-frame rate is derivable from it'),
    }


def _calibration_id(session_dir) -> dict:
    """Our own calibration record id, or an explicit statement that there is none."""
    path = Path(session_dir) / 'calibration.json'
    if not path.is_file():
        return {'value': UNKNOWN,
                'reason': 'the session carries no calibration.json'}
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as error:
        return {'value': UNKNOWN, 'reason': f'unreadable calibration.json: {error}'}
    for key in ('calibration_id', 'id', 'fingerprint'):
        if payload.get(key):
            return {'value': str(payload[key]), 'reason': None, 'path': str(path)}
    return {'value': UNKNOWN,
            'reason': ('calibration.json exists but records no id; the vendor exposes '
                       'only FactoryCalibrationStatus, which is not a calibration id'),
            'keys_present': sorted(payload)[:20]}


def _debounced_states(active):
    """Turn a per-frame boolean into on/off/unknown with the declared debounce.

    Returns a list of states, one per frame. `unknown` covers the frames before the
    baseline window is complete, where there is no delta to compare against a threshold.
    A run of `debounce_frames` consecutive active frames is what turns a cell on; the same
    run length is required to turn it off, so a single-frame spike is not a contact and a
    single-frame dropout does not end one.
    """
    debounce = DECLARED_PARAMETERS['debounce_frames']
    states = []
    state = False
    run = 0
    for is_active in active:
        if is_active is None:
            states.append('unknown')
            run = 0
            continue
        if is_active == state:
            run = 0
        else:
            run += 1
            if run >= debounce:
                state = is_active
                run = 0
        states.append('on' if state else 'off')
    return states


def _event(series, index, cell, row, col, state, raw, intensity_delta, duration,
           timing, calibration, first_stamp, censored_at_session_end=False):
    """One schema record. Every field the evidence does not support is `null` + a reason."""
    stamp = series.host_mono_ns[index] if index < len(series.host_mono_ns) else None
    event = {
        'session': series.session_id,
        'timestamp': (round((stamp - first_stamp) / 1e9, 6)
                      if stamp is not None and first_stamp is not None else None),
        'time_base': timing['time_base'],
        'sequence': {'stream': series.stream_sequence[index],
                     'frame': series.frame_sequence[index]},
        'sensor': f"qnbot_glove_{_hand_name(series.hand_side[index])}",
        'taxel': cell,
        'taxel_grid': {'row': row, 'col': col,
                       'row_rule': 'row = index // 20', 'col_rule': 'index % 20'},
        'finger': None,
        'finger_unknown_reason': ('the vendor documents the finger order but not the '
                                  'pressure-grid geometry, so this taxel cannot be '
                                  'attributed to a finger'),
        'contact_state': state,
        'position_or_region': f'taxel:{cell}',
        'coordinate_frame': None,
        'raw_intensity': raw,
        'intensity_delta': intensity_delta,
        'pose_valid': None,
        'pose_valid_unknown_reason': ('pose validity is per finger and this event has no '
                                      'resolved finger; the frame-level mask is carried '
                                      'in raw_valid_mask'),
        'raw_valid_mask': series.valid_mask[index],
        'finger_valid': {name: bool(series.valid_mask[index] >> bit & 1)
                         for bit, name in enumerate(FINGER_NAMES)},
        'calibration_id': calibration['value'],
        'time_uncertainty': timing['time_uncertainty_s'],
        'spatial_uncertainty': None,
        'spatial_uncertainty_reason': ('per-taxel metric geometry is unparsed, so no '
                                       'millimetre value can be emitted'),
        'units': PRESSURE_UNITS,
    }
    if duration is not None:
        event['duration_frames'] = duration
    if censored_at_session_end:
        event['censored_at_session_end'] = True
        event['censored_reason'] = ('the cell was still above threshold at the last '
                                    'accepted frame; the episode has no observed end')
    return event


def emit_events(session_dir, bin_name: str = 'glove.bin') -> dict:
    """Emit the sparse ContactEvent stream for one recorded session."""
    session_dir = Path(session_dir)
    series = pressure_series(session_dir, bin_name=bin_name)
    parameters = DECLARED_PARAMETERS
    window = int(parameters['baseline_window_frames'])
    threshold = int(parameters['delta_threshold_raw_counts'])
    if series.accepted == 0:
        raise ValueError(f'no accepted glove frame in {session_dir}')
    grids = series.grids.astype(np.int16)
    baseline = grids[:min(window, len(grids))].min(axis=0)
    delta = grids - baseline
    timing = _timing(series)
    calibration = _calibration_id(session_dir)
    first_stamp = next((value for value in series.host_mono_ns if value is not None), None)
    rows, cols = np.nonzero(delta.max(axis=0) >= threshold)
    events = []
    for row, col in sorted(zip(rows.tolist(), cols.tolist())):
        cell = int(row * grids.shape[2] + col)
        active = [None if index < min(window, len(grids))
                  else bool(delta[index][row][col] >= threshold)
                  for index in range(len(grids))]
        states = _debounced_states(active)
        previous = 'unknown'
        opened_at = None
        for index, state in enumerate(states):
            if state == previous:
                continue
            if state == 'on':
                opened_at = index
                events.append(_event(series, index, cell, row, col, 'on',
                                     int(grids[index][row][col]),
                                     int(delta[index][row][col]), None, timing,
                                     calibration, first_stamp))
            elif previous == 'on' and state == 'off':
                events.append(_event(series, index, cell, row, col, 'off',
                                     int(grids[index][row][col]),
                                     int(delta[index][row][col]), index - opened_at,
                                     timing, calibration, first_stamp))
                opened_at = None
            previous = state
        if previous == 'on' and opened_at is not None:
            events.append(_event(series, len(states) - 1, cell, row, col, 'on',
                                 int(grids[-1][row][col]), int(delta[-1][row][col]), None,
                                 timing, calibration, first_stamp,
                                 censored_at_session_end=True))
    events.sort(key=lambda event: (event['sequence']['frame'], event['taxel']))
    diagnostics = {}
    for probe in (4, 6, 8, 12, 16):
        probe_rows, probe_cols = np.nonzero(delta.max(axis=0) >= probe)
        frames_on = 0
        episodes = 0
        for row, col in zip(probe_rows.tolist(), probe_cols.tolist()):
            states = _debounced_states([None if index < min(window, len(grids))
                                        else bool(delta[index][row][col] >= probe)
                                        for index in range(len(grids))])
            frames_on += states.count('on')
            episodes += sum(1 for previous, state in zip(['unknown'] + states, states)
                            if state == 'on' and previous != 'on')
        diagnostics[str(probe)] = {'cells_reaching_the_threshold': int(len(probe_rows)),
                                   'frames_in_the_on_state': frames_on,
                                   'episodes': episodes}
    summary = {
        'accepted_frames': series.accepted,
        'cells_with_any_delta': int((delta.max(axis=0) > 0).sum()),
        'cells_reaching_the_threshold': len(rows),
        'events': len(events),
        'by_state': dict(Counter(event['contact_state'] for event in events)),
        'censored_at_session_end': sum(1 for event in events
                                       if event.get('censored_at_session_end')),
        'first_event_frame': events[0]['sequence']['frame'] if events else None,
        'last_event_frame': events[-1]['sequence']['frame'] if events else None,
        'max_delta_raw_counts': int(delta.max()),
        'baseline_window_complete': series.accepted > window,
        'threshold_diagnostics': {
            'thresholds_raw_counts': sorted(int(key) for key in diagnostics),
            'per_threshold': diagnostics,
            'declared_threshold': threshold,
            'rule': ('diagnostic only. The declared threshold is not changed by these '
                     'numbers in this report: a threshold chosen from the session it is '
                     'reported on is not a pre-registered decision'),
        },
    }
    return {
        'schema': 'docs/transparent-depth/CONTACT_EVENT_SCHEMA.md',
        'session': series.session_id,
        'session_dir': str(session_dir),
        'bin': str(session_dir / bin_name),
        'sensor': f"qnbot_glove_{_hand_name(series.hand_side[0])}",
        'hand_side': {'wire_value': series.hand_side[0],
                      'name': _hand_name(series.hand_side[0])},
        'declared_parameters': parameters,
        'calibration': calibration,
        'timing': timing,
        'summary': summary,
        'events': events,
        'not_a_calibrated_force': parameters['not_a_calibrated_force'],
        'unresolved': series.unresolved + [
            'taxel-to-finger mapping: the vendor documents the finger order but not the '
            'pressure-grid geometry, so no taxel is attributed to a finger here',
            'spatial uncertainty: per-taxel geometry is unparsed, so no millimetre value '
            'is emitted',
        ],
        'not_a_contact_label_for_robot_actions': (
            'these events are human supervision signals; they are not robot action labels '
            'and must not be written into a robot action record'),
    }


def write_events(report, out_path) -> Path:
    """Write the event stream as JSONL plus the report as JSON, outside the session.

    The raw session directory is never written to: derived artifacts go to the reports
    directory so a re-run can be compared against the recording instead of replacing it.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open('w', encoding='utf-8', newline='\n') as stream:
        for event in report['events']:
            stream.write(json.dumps(event, sort_keys=True) + '\n')
    summary_path = out_path.with_suffix('.report.json')
    summary_path.write_text(json.dumps(report, indent=1, sort_keys=True) + '\n',
                            encoding='utf-8')
    return out_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session', nargs='?', default=None,
                        help='session directory, or a session id under the sessions root')
    parser.add_argument('--out', default=None,
                        help='JSONL path; default is the reports directory')
    parser.add_argument('--print-summary', action='store_true')
    args = parser.parse_args(argv)
    if args.session is None:
        parser.error('a session directory or session id is required')
    session = Path(args.session)
    if not session.is_dir():
        session = DEFAULT_SESSION_ROOT / str(args.session)
    if not session.is_dir():
        parser.error(f'no session directory at {session}')
    report = emit_events(session)
    out = Path(args.out) if args.out else (
        DEFAULT_OUTPUT_ROOT / f'{session.name}-contact-events.jsonl')
    write_events(report, out)
    summary = {'session': report['session'], 'events': report['summary']['events'],
               'by_state': report['summary']['by_state'],
               'cells_reaching_the_threshold':
                   report['summary']['cells_reaching_the_threshold'],
               'out': str(out), 'report': str(out.with_suffix('.report.json'))}
    print(json.dumps(summary, indent=1))
    if args.print_summary:
        print(json.dumps(report['declared_parameters'], indent=1))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
