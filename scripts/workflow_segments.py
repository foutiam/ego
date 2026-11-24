"""Resolve a workflow segment and its reviewed recording without alias fallback."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from threading import Lock

_registry_lock = Lock()

ROOT = Path(__file__).resolve().parents[1]
VIEWER = ROOT / 'outputs/toporetarget-human-pilot-20261026/viewer'
IDENTIFIER = re.compile(r'[a-z0-9][a-z0-9_-]{0,95}')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def fingerprint(value):
    data = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(data.encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError('Invalid recording or segment identifier')
    return value


@dataclass(frozen=True)
class Segment:
    key: str
    recording_id: str
    segment_id: str
    revision: str
    session: Path
    row: dict
    legacy: bool

    def identity(self):
        return dict(clip=self.key, recordingId=self.recording_id, segmentId=self.segment_id,
                    segmentRevision=self.revision)

    def public_row(self):
        return dict(self.row, **{**self.identity(), 'id': self.key, 'display_id': (
                                     'TouchScale · 白杯拿放' if self.row.get('recording_format')=='touchscale_rgbd'
                                     and self.row.get('label')=='Move the mug holding a small amount of water onto the coaster'
                                     else self.row.get('display_name', self.segment_id)),
                                 'session_id': self.recording_id, 'recording_id': self.recording_id,
                                 'registered_capture': not self.legacy})


class Segments:
    def __init__(self, viewer=VIEWER, project=ROOT, registry=None, *, capture_root=None):
        self.viewer = Path(viewer)
        self.project = Path(project).resolve()
        self.registry = Path(registry or self.project / 'data/ego/derived')
        self.capture_root = Path(capture_root).resolve() if capture_root is not None else None

    def session(self, recording, source):
        identifier(recording)
        if source == 'ego_raw':
            root = self.project / 'data/ego/raw/sessions'
        elif source == 'touchscale_rgbd':
            root = self.project / 'data/ego/external/touchscale'
        elif source == 'capture':
            if self.capture_root is None:
                config = read(self.project / 'code/ego_capture/capture_config.json')
                root = self.project / config['output_root'] / 'sessions'
            else:
                root = self.capture_root / 'sessions'
        else:
            raise ValueError('Unknown recording origin')
        root = root.resolve()
        path = (root / recording).resolve()
        declared_root = self.capture_root if source == 'capture' and self.capture_root else self.project
        if not root.is_relative_to(declared_root) or path.parent != root:
            raise ValueError('Recording path escapes its declared source')
        metadata = read(path / 'metadata.json')
        if metadata.get('session_id') != recording:
            raise ValueError('Recording metadata identity differs')
        if metadata.get('status') not in {'saved', 'recorded', 'complete', 'completed', 'finished', 'closed'}:
            raise ValueError('Recording is not closed')
        return path, metadata

    def row(self, recording, row, *, legacy=False):
        name = identifier(row.get('id'))
        if row.get('deleted') or row.get('recorded_status') != 'recorded':
            raise ValueError('Segment is not published')
        for field in (('session_id',) if legacy else ('recording_id', 'session_id')):
            if row.get(field) not in (None, recording):
                raise ValueError('Segment belongs to a different recording')
        start, end = row.get('start_s'), row.get('end_s')
        if any(type(t) not in (int, float) or not math.isfinite(t) for t in (start, end)) or not 0 <= start < end:
            raise ValueError('Invalid recorded interval')
        source = 'ego_raw' if legacy else row.get('recording_format', 'capture')
        session, metadata = self.session(recording, source)
        if source == 'touchscale_rgbd' and metadata.get('recording_format') != source:
            raise ValueError('Imported recording format differs from the registered segment')
        if end > metadata['duration_s'] + .001:
            raise ValueError('Segment exceeds the original recording')
        revision = fingerprint(dict(recording=recording, segment=name, start=start, end=end,
                                   metadata=fingerprint(metadata), row=row))
        key = name if legacy else 'seg-' + revision[:32]
        return Segment(key, recording, name, revision, session, row, legacy)

    def catalog(self):
        path = self.viewer / 'clip-catalog.json'
        return read(path) if path.is_file() else {'clips': []}

    def items(self, catalog=None):
        catalog = self.catalog() if catalog is None else catalog
        entries = []
        primary = catalog.get('primary_session')
        for row in catalog['clips']:
            if row.get('session_id') == primary and not row.get('deleted') and row.get('recorded_status') == 'recorded':
                entries.append(self.row(primary, row, legacy=True))
        for path in sorted(self.registry.glob('*/segments.json')):
            recording = identifier(path.parent.name)
            if not path.resolve().is_relative_to(self.registry.resolve()):
                raise ValueError('Segment registry path escapes its directory')
            data = read(path)
            if data.get('schema') != 'ego-workflow-segments-v1' or data.get('recordingId') != recording:
                raise ValueError('Segment registry identity differs')
            for row in data['segments']:
                if not row.get('deleted') and row.get('recorded_status') == 'recorded':
                    entries.append(self.row(recording, row))
        refs = {(s.recording_id, s.segment_id) for s in entries}
        if len(refs) != len(entries) or len({s.key for s in entries}) != len(entries):
            raise ValueError('Duplicate segment identity in workflow catalog')
        return entries

    def resolve(self, reference, entries=None):
        entries = self.items() if entries is None else entries
        if isinstance(reference, str):
            value = identifier(reference)
            result = [s for s in entries if s.key == value]
            # New names are reusable. A bare name may only resolve an
            # unambiguous historical URL or a canonical scoped key.
            if not value.startswith('seg-') and sum(s.segment_id == value for s in entries) > 1:
                raise ValueError('Ambiguous segment name. Select its recording.')
        elif isinstance(reference, dict):
            recording = identifier(reference.get('recordingId'))
            name = identifier(reference.get('segmentId'))
            result = [s for s in entries if s.recording_id == recording and s.segment_id == name]
            if result and (reference.get('clip', result[0].key) != result[0].key or
                           reference.get('segmentRevision', result[0].revision) != result[0].revision):
                raise ValueError('Segment revision or canonical key differs. Reload the recording.')
        else:
            raise ValueError('A recording segment identity is required')
        if len(result) != 1:
            raise ValueError('Unknown or unpublished recording segment')
        return result[0]

    def public_catalog(self):
        catalog = self.catalog()
        return dict(catalog, clips=[s.public_row() for s in self.items(catalog)])



def review_intervals(recording, duration, guided=None, *, registry=None):
    """Show recorded acquisition slots and already published workflow intervals."""
    registry = registry or Segments()
    items = {}
    if isinstance(guided, dict) and guided.get('session_id') == recording:
        for row in guided.get('segments', []):
            if row.get('deleted') or row.get('status') != 'recorded':
                continue
            start, end = row.get('start_s'), row.get('end_s')
            if not all(type(t) in (int, float) and math.isfinite(t) for t in (start, end)) or not 0 <= start < end <= duration + .001:
                continue
            name = identifier(row.get('key'))
            items[(name, round(start, 3), round(end, 3))] = dict(
                name=name, startS=start, endS=end, category=row.get('task_id'), guided=True,
                guidedSegmentId=row.get('id'), label=row.get('label') or None,
                outcome=row.get('operator_outcome'))
    for segment in registry.items():
        if segment.recording_id != recording:
            continue
        start, end = (segment.row[k] for k in ('start_s', 'end_s'))
        if end > duration + .001:
            continue
        key = (segment.segment_id, round(start, 3), round(end, 3))
        items[key] = dict(items.get(key, {}), name=segment.segment_id, startS=start, endS=end,
                          category=segment.row.get('category'), workflow=segment.identity())
    return sorted(items.values(), key=lambda row: (row['startS'], row['endS'], row['name']))

def register(recording, name, start, end, category='normal', *, segments=None, preview_root=ROOT):
    if category not in {'normal', 'miss', 'adjust', 'abort', 'reference'}:
        raise ValueError('Unsupported segment category')
    segments = segments or Segments()
    row = dict(id=name, start_s=start, end_s=end, category=category, recorded_status='recorded')
    item = segments.row(recording, row)
    quality = read(item.session / 'derived/quality.json')
    if quality.get('session_id') != item.recording_id or not quality.get('stereo_pairs_paired'):
        raise ValueError('First finish recording QC with a verified stereo pair index')
    from workflow_capture_inputs import capture_inputs, publish_json
    capture_inputs(item, project=preview_root)
    path = segments.registry / recording / 'segments.json'
    with _registry_lock:
        if any(s.recording_id == recording and s.segment_id == name for s in segments.items()):
            raise ValueError('该录制已有同名片段，请使用不同名称。')
        data = read(path) if path.exists() else dict(schema='ego-workflow-segments-v1',
                                                     recordingId=recording, segments=[])
        data['segments'].append(row)
        publish_json(path, data)
    return item.identity()


def main():
    parser = argparse.ArgumentParser(description='Register a reviewed recording interval in the Ego workflow.')
    parser.add_argument('recording')
    parser.add_argument('segment')
    parser.add_argument('start', type=float)
    parser.add_argument('end', type=float)
    args = parser.parse_args()
    print(json.dumps(register(args.recording, args.segment, args.start, args.end)))


if __name__ == '__main__':
    main()

