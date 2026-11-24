"""Cached tactile response candidates and separate human review decisions.

No physical grasp, object identity, exposure alignment or training label is inferred.
Raw decoding, baseline parameters and anatomical regions reuse existing project code.
"""
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'code'))
from transparent_capture.contact_events import DECLARED_PARAMETERS
from transparent_capture.glove_stream import pressure_series

VERSION = 'tactile-event-review-v1'
BASE = ROOT / 'outputs/retarget-timeline-20261017/event-reviews'
LAYOUT = ROOT / 'configs/glove-tactile-layout-v1.json'
MAX_GAP_S = .1
MIN_HOLD_S = .15
MAX_REVIEW_FRAME_DISTANCE_S = .15


def read(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.{os.getpid()}.{threading.get_ident()}.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=True, allow_nan=False), encoding='utf-8')
    os.replace(temporary, path)


def fingerprint(directory):
    names = ['metadata.json', 'glove.bin', 'glove-reads.csv', 'calibration.json',
             'derived/paired_frames.jsonl']
    facts = {name: [p.stat().st_size, p.stat().st_mtime_ns] if (p := directory / name).exists()
             else None for name in names}
    facts['algorithm_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    facts['layout_sha256'] = hashlib.sha256(LAYOUT.read_bytes()).hexdigest()
    facts['parameters'] = DECLARED_PARAMETERS
    return hashlib.sha256(json.dumps(facts, sort_keys=True).encode()).hexdigest()


def episodes(times, values, valid, threshold=8, debounce=3, min_hold_s=0):
    """Return onset brackets and censored episodes; gaps never become releases."""
    output, opened, start, run, previous = [], None, None, 0, None
    continuous_previous = False
    pending_censored = False
    for i, (stamp, value, known) in enumerate(zip(times, values, valid)):
        gap = previous is not None and stamp is not None and stamp - previous > MAX_GAP_S
        if stamp is None or not known or gap:
            if opened is not None:
                output.append(dict(start=opened, end=None, last=i-1, end_reason='missing_observation'))
            opened, start, run = None, None, 0
            previous = stamp
            continuous_previous = False
            # Restart on the next sample; never span a missing interval.
            continue
        active = bool(value >= threshold)
        target = active != (opened is not None)
        if not target:
            run, start = 0, None
        else:
            if not run:
                start = i
                pending_censored = not continuous_previous
            run += 1
            if run >= debounce and stamp - times[start] >= min_hold_s:
                if active:
                    opened = dict(index=start, available_index=i,
                                  left_censored=pending_censored,
                                  bracket_s=[None if pending_censored else times[max(0, start-1)], times[start]])
                else:
                    output.append(dict(start=opened, end=dict(index=start, available_index=i,
                                       bracket_s=[times[max(0, start-1)], times[start]]), last=i,
                                       end_reason='response_below_threshold'))
                    opened = None
                run, start = 0, None
        previous = stamp
        continuous_previous = True
    if opened is not None:
        output.append(dict(start=opened, end=None, last=len(times)-1, end_reason='session_end'))
    return output


def compute(directory, progress=lambda *args: None, *, base=BASE):
    progress('decode', 0, 3)
    meta = read(directory / 'metadata.json')
    if meta.get('status') == 'recording':
        raise ValueError('Wait until the recording has been saved')
    source_revision = fingerprint(directory)
    series = pressure_series(directory)
    if not series.accepted:
        raise ValueError('No accepted tactile samples; contact remains unknown')
    grids = series.grids.astype(np.int16)
    window = DECLARED_PARAMETERS['baseline_window_frames']
    calibration = read(directory / 'calibration.json', {})
    baseline_record = calibration.get('touch_pressure_baseline') or {}
    baseline = np.asarray(baseline_record.get('grid', []), dtype=float)
    saved_baseline = bool(baseline.shape == grids.shape[1:] and np.isfinite(baseline).all())
    if not saved_baseline:
        baseline = grids[:min(window, len(grids))].min(axis=0)
    delta = np.maximum(grids - baseline, 0)
    origin = meta.get('start_mono_ns')
    times = [round((stamp-origin)/1e9, 6) if stamp is not None and origin is not None else None
             for stamp in series.host_mono_ns]
    valid = [stamp is not None and (saved_baseline or i >= window) for i, stamp in enumerate(times)]
    left = set(series.hand_side) == {1}
    regions = [r for r in read(LAYOUT)['regions'] if r.get('role') == 'touch'
               and r.get('state') == 'available' and left]
    groups = {}
    for region in regions:
        group = region['anatomy']
        groups.setdefault(group, []).extend((r, c) for r in region['rows'] for c in region['cols'])
    signals = {name: delta[:, [p[0] for p in cells], [p[1] for p in cells]].max(axis=1)
               for name, cells in groups.items()}
    # Unknown cells remain available for inspection without assigning them anatomy.
    all_score = delta.max(axis=(1, 2))
    threshold = DECLARED_PARAMETERS['delta_threshold_raw_counts']
    debounce = DECLARED_PARAMETERS['debounce_frames']
    progress('detect', 1, 3)
    pairs_file = directory / 'derived/paired_frames.jsonl'
    pairs = [json.loads(line) for line in pairs_file.read_text(encoding='utf-8').splitlines() if line.strip()] if pairs_file.exists() else []
    pair_times = np.array([p['t_s'] for p in pairs])
    events, spans = [], []

    def event(kind, group, boundary, censored=False):
        i = boundary['index']; stamp = times[i]
        nearest = int(np.argmin(abs(pair_times-stamp))) if len(pair_times) and stamp is not None else None
        nearest_delta = round(float(pair_times[nearest]-stamp)*1000, 3) if nearest is not None else None
        if nearest_delta is not None and abs(nearest_delta) > MAX_REVIEW_FRAME_DISTANCE_S*1000:
            nearest = None
        item = dict(id=f'{kind}:{group}:{i}', kind=kind, region=group, t_s=stamp,
                    tactile_frame_index=i, stream_sequence=int(series.stream_sequence[i]),
                    available_at_s=times[boundary['available_index']], onset_bracket_s=boundary['bracket_s'],
                    left_censored=censored, evidence_class='threshold_response_candidate',
                    paired_frame_index=nearest, camera_association='nearest_host_arrival_unverified_latency',
                    camera_t_s=float(pair_times[nearest]) if nearest is not None else None,
                    camera_delta_ms=nearest_delta if nearest is not None else None,
                    nearest_camera_delta_ms=nearest_delta)
        if nearest is not None:
            item['image'], item['image1'] = pairs[nearest]['image'], pairs[nearest]['image1']
        events.append(item)
        return item

    def detect(group, signal, on, off, minimum=0):
        for interval in episodes(times, signal, valid, threshold if not minimum else 1, debounce, minimum):
            b = interval['start']; idx=b['index']
            # An initially active state or a restart after missing samples is left-censored.
            censored = b['left_censored']
            first = event(on, group, b, censored)
            final = event(off, group, interval['end']) if interval['end'] else None
            spans.append(dict(region=group, kind=on, start_s=first['t_s'],
                              end_s=final['t_s'] if final else None,
                              last_observed_s=times[interval['last']], end_reason=interval['end_reason'],
                              left_censored=censored))

    detect('any_taxel', all_score, 'contact_on', 'contact_off')
    for name, signal in signals.items():
        detect(name, signal, 'region_on', 'region_off')
    if 'thumb' in signals and len(signals) > 1:
        opposition = ((signals['thumb'] >= threshold) &
                      np.any(np.stack([v >= threshold for k, v in signals.items() if k != 'thumb']), axis=0))
        detect('thumb_and_other_finger', opposition.astype(int), 'grasp_candidate', 'release_candidate', MIN_HOLD_S)
    events.sort(key=lambda x: (x['t_s'] if x['t_s'] is not None else float('inf'), x['id']))
    stride = max(1, len(times)//1800)
    samples = [dict(t_s=times[i], peak=float(all_score[i]), known=valid[i],
                    fingers={k: float(v[i]) for k,v in signals.items()}) for i in range(0, len(times), stride)]
    report = dict(schema=VERSION, session=directory.name, source_revision=source_revision,
                  duration_s=meta.get('duration_s'), parameters=dict(DECLARED_PARAMETERS,
                  max_gap_s=MAX_GAP_S, grasp_cocontact_hold_s=MIN_HOLD_S,
                  max_review_frame_distance_s=MAX_REVIEW_FRAME_DISTANCE_S,
                  aggregation='max_over_cells_then_debounce_per_region'),
                  hand_side_values=sorted(set(int(x) for x in series.hand_side)),
                  anatomical_mapping_available=left,
                  baseline_source='recorded_rest_grid' if saved_baseline else 'initial_window_minimum_unverified_rest',
                  timing=dict(basis='host_monotonic_arrival_minus_session_start', exposure_offset_s=None,
                              cross_sensor_uncertainty_ms=None, qualified_for_precise_training=False),
                  inference=dict(grasp='thumb and another mapped touch region active together; no object motion proof',
                                 release='end of sustained co-contact; not proof of physical release'),
                  frames=series.accepted, camera_review_frames=len(pairs), samples=samples, events=events,
                  intervals=spans, summary={k:sum(e['kind']==k for e in events) for k in
                  ['contact_on','contact_off','region_on','region_off','grasp_candidate','release_candidate']},
                  limitations=['Baseline does not establish current wearing calibration.',
                               'Camera association uses host arrival; exposure latency is unknown.',
                               'Initial contact, missing data and ongoing contact at the end are censored.',
                               'Human decisions do not calibrate timing or establish robot training labels.'])
    if fingerprint(directory) != source_revision:
        raise ValueError('Recording changed during event calculation; retry against the saved recording')
    write(base / directory.name / 'report.json', report)
    progress('complete', 3, 3)
    return report


class EventJobs:
    """Deduplicate inexpensive event-only derivations without rerunning perception."""
    def __init__(self, directory, calculate=None, base=None, source_revision=None):
        self.directory = directory
        self.base = base or BASE
        self.calculate = calculate or (lambda directory, progress: compute(directory, progress, base=self.base))
        self.fingerprint = source_revision or fingerprint
        self.lock = threading.RLock()
        self.jobs = {}
        self.compute_lock = threading.Lock()

    def get(self, sid):
        directory = self.directory(sid)
        report = read(self.base / sid / 'report.json')
        current = bool(report and report.get('source_revision') == self.fingerprint(directory))
        with self.lock:
            job = dict(self.jobs.get(sid, {}))
        decisions = read(self.base / sid / 'decisions.json', {})
        state = 'complete' if current else job.get('state', 'not_started')
        if not current and state == 'complete':
            state = 'not_started'
        return dict(session=sid, state=state,
                    progress=job, report=report if current else None,
                    decisions=decisions.get('decisions', {}) if current and decisions.get('source_revision') == report['source_revision'] else {})

    def ensure(self, sid):
        directory = self.directory(sid)
        with self.lock:
            state = self.get(sid)
            if state['state'] in ('complete', 'running', 'queued'):
                return dict(state, reused=True)
            self.jobs[sid] = dict(state='queued', completed=0, total=3, phase='queued')
            threading.Thread(target=self.work, args=(sid,directory), daemon=True).start()
            return dict(session=sid, state='queued', reused=False)

    def work(self, sid, directory):
        def progress(phase, completed, total):
            with self.lock:
                self.jobs[sid] = dict(state='complete' if phase=='complete' else 'running',
                                      phase=phase, completed=completed, total=total)
        try:
            with self.compute_lock:
                self.calculate(directory, progress)
        except Exception as error:
            with self.lock:
                self.jobs[sid] = dict(state='failed', message=str(error))

    def review(self, data):
        sid=data.get('session_id')
        with self.lock:
            state = self.get(sid); report=state['report']
            if not report or data.get('source_revision') != report['source_revision']:
                raise ValueError('Event source changed; reload before confirming')
            if data.get('event_id') not in {e['id'] for e in report['events']}:
                raise ValueError('Unknown event identifier')
            if data.get('decision') not in ('confirmed','rejected','unobservable','pending'):
                raise ValueError('Invalid review decision')
            decisions=state['decisions']
            decisions[data['event_id']]=dict(decision=data['decision'], reviewed_unix=time.time(),
                                             scope='human_interpretation_only_not_timing_calibration')
            write(self.base/sid/'decisions.json', dict(session=sid, source_revision=report['source_revision'],
                                                decisions=decisions, qualified_for_precise_training=False))
            return dict(ok=True, decisions=decisions)
