"""Read provenance-bound contact suggestions from completed native 04 replay."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from threading import Lock

import numpy as np

from workflow_segments import ROOT, Segments, read
from visuotactile.storage import dump_json

OUTPUTS = ROOT / 'outputs'
BASE = OUTPUTS / 'source-physics-20261031'
FINGERS = ('thumb', 'index', 'middle', 'ring', 'little')
REVIEW_LOCK = Lock()


def _review_file(segment, trace_sha):
    directory = ROOT / 'data/ego/derived' / segment.recording_id / 'policy-annotations'
    if directory.resolve() != directory or not re.fullmatch('[a-f0-9]{64}', trace_sha):
        raise ValueError('标注保存路径或执行指纹无效')
    return directory / f'{segment.revision}-{trace_sha}.json'


def review_policy_label(identity, trace_sha, event_index, choice):
    if type(event_index) is not int or choice not in ('confirmed', 'rejected', 'pending'):
        raise ValueError('标注编号或复查选择无效')
    result = policy_labels(identity)
    events = result['events']
    if trace_sha != result.get('traceSha256') or not 0 <= event_index < len(events):
        raise ValueError('04 回放或候选已变化，请刷新后再标注')
    segment = Segments().resolve(identity)
    path = _review_file(segment, trace_sha)
    with REVIEW_LOCK:
        saved = read(path) if path.is_file() else dict(schema='ego-source04-policy-review-v1',
                    segment=segment.identity(), traceSha256=trace_sha,
                    sourceManifestSha256=result['sourceManifestSha256'], decisions={})
        if saved['segment'] != segment.identity() or saved['traceSha256'] != trace_sha:
            raise ValueError('已保存标注与本次回放身份不一致')
        key = str(event_index)
        if choice == 'pending':
            saved['decisions'].pop(key, None)
        else:
            saved['decisions'][key] = dict(choice=choice, event=events[event_index])
        path.parent.mkdir(parents=True, exist_ok=True)
        dump_json(path, saved)
    return dict(choice=choice)



def file_sha(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def trace_intervals(trace, start, end):
    clock = trace['state_time']
    loads = trace['contact_load']
    coverage = trace['contact_records_complete']
    valid = trace['state_valid']
    if (clock.ndim != 1 or len(clock) < 2 or not np.isfinite(clock).all()
            or np.any(np.diff(clock) <= 0) or clock[0] < start - 1e-3 or clock[-1] > end + 1e-3
            or loads.shape != (len(clock), len(FINGERS)) or not np.isfinite(loads).all()
            or coverage.shape != clock.shape or valid.shape != clock.shape
            or coverage.dtype.kind != 'b' or valid.dtype.kind != 'b'):
        raise ValueError('04 接触轨迹或录制时钟不一致')
    events = []
    for index, finger in enumerate(FINGERS):
        active = valid & coverage & (loads[:, index] > 1e-5)
        edges = np.flatnonzero(np.diff(np.r_[False, active, False]))
        for first, stop in edges.reshape(-1, 2):
            # Single native steps can be collision chatter. Keep the observed load interval.
            if clock[stop - 1] - clock[first] < .05:
                continue
            events.append(dict(finger=finger, startS=max(start, float(clock[first])),
                               endS=min(end, float(clock[stop - 1])),
                               peakNormalLoadN=float(loads[first:stop, index].max())))
    return sorted(events, key=lambda item: (item['startS'], item['finger']))


def policy_labels(identity):
    from ego_isaac_bridge_worker import current_source

    segment = Segments().resolve(identity)
    start, end = (segment.row[key] for key in ('start_s', 'end_s'))
    empty = dict(segment=segment.identity(), events=[])
    try:
        digest = current_source(segment.key)['sourceManifestSha256']
    except (OSError, ValueError, KeyError):
        return dict(empty, message='此片段尚未形成当前 02／03 来源，待工作流完成后可读取 04 候选。')
    matches = []
    for index in (BASE / 'comparisons').glob('*.json'):
        if not re.fullmatch('[a-f0-9]{24}', index.stem):
            continue
        try:
            comparison = read(index)
            if (comparison.get('clip'), comparison.get('sourceID'), comparison.get('sourceManifestSha256')) != (
                    segment.key, index.stem, digest):
                continue
            manifest = read(BASE / 'source' / index.stem / 'manifest.json')
            binding = manifest.get('sourceBinding') or {}
            if ((binding.get('clip'), binding.get('sessionId'), binding.get('sourceManifestSha256')) != (
                    segment.key, segment.recording_id, digest)
                    or binding.get('interval_s') != [start, end]):
                continue
            for row in comparison.get('methods', {}).values():
                for asset in (row.get('assets', {}).get('rollingReport'), row.get('assets', {}).get('report')):
                    if not asset:
                        continue
                    report = (OUTPUTS / asset).resolve()
                    runs = (BASE / 'runs' / index.stem).resolve()
                    if report.name != 'candidate-window-report.json' or not report.is_relative_to(runs) or not report.is_file():
                        continue
                    matches.append((report.stat().st_mtime_ns, report, index.stem))
        except (OSError, ValueError, TypeError, KeyError):
            continue
    for _, path, source_id in sorted(set(matches), reverse=True):
        try:
            report = read(path)
            audit = report.get('executionAudit') or {}
            native = audit.get('isaac') or {}
            if (report.get('sourceID') != source_id or audit.get('scene', {}).get('kind') != 'recorded'
                    or audit.get('policyCalled') is not True or native.get('actualExecuted') is not True
                    or native.get('continuousExecutionComplete') is not True):
                continue
            replay = report['fullReplayEvidence']
            if replay.get('rollout', {}).get('actualExecuted') is not True:
                continue
            trace = replay['traces'].get('continuous') or replay['traces'].get('candidate')
            states = Path(trace['states']).resolve()
            evidence = Path(trace['evidence']).resolve()
            if (not all(file.is_relative_to(path.parent) for file in (states, evidence))
                    or file_sha(states) != trace['statesSha256'] or file_sha(evidence) != trace['evidenceSha256']):
                continue
            native_evidence = read(evidence)
            if native_evidence.get('actualExecuted') is not True or native_evidence.get('physical') is not True:
                continue
            with np.load(states, allow_pickle=False) as arrays:
                events = trace_intervals(arrays, start, end)
            path_review = _review_file(segment, trace['statesSha256'])
            saved = read(path_review) if path_review.is_file() else {}
            decisions = {key: entry['choice'] for key, entry in saved.get('decisions', {}).items()
                         if key.isdigit() and int(key) < len(events) and entry.get('event') == events[int(key)]}
            message = '已有连续 04 回放，未检测到持续承载候选。' if not events else ''
            return dict(empty, events=events, decisions=decisions, message=message, sourceID=source_id,
                        sourceManifestSha256=digest, traceSha256=trace['statesSha256'],
                        execution=path.parent.parent.name,
                        physicalPass=audit.get('acceptance', {}).get('event') is True)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return dict(empty, message='当前来源暂无匹配的 04 Policy 连续物理回放。运行工作流 04 后可刷新候选。')
