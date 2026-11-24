"""Append-only session recorder (offline-first).

Writes the same Session directory format consumed by
`transparent_capture.loader.SessionLoader` (and produced by the visuotactile
stack's storage.py):

    <session_id>/
        <stream>.bin          raw frame payloads, append-only
        <stream>-reads.csv    host_ns, host_mono_ns, elapsed_s, offset, length
        metadata.json         session metadata incl. calibrations

Recording a REAL device session is a separate, later step (see
docs/transparent-depth/USER_ACTIONS.md); this module only owns the on-disk
contract so synthetic tests can verify record -> load -> QC round-trips.
"""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path

from .calibration import REQUIRED_META_KEYS


class SessionRecorder:
    """Record raw frame payloads into a Session-format directory.

    Streams are created lazily on first write. Payloads are opaque bytes;
    decoding is owned by the caller (see dataset_builder decode helpers).
    """

    def __init__(self, root: str | Path, metadata: dict, streams: list[str]):
        missing = [k for k in REQUIRED_META_KEYS if k not in metadata]
        if missing:
            raise ValueError(f'metadata missing required keys: {missing}')
        self.root = Path(root)
        self.session_id = str(metadata['session_id'])
        self.dir = self.root / 'sessions' / self.session_id
        if self.dir.exists():
            raise FileExistsError(f'session directory already exists: {self.dir}')
        self.dir.mkdir(parents=True)
        self._start_mono_ns = int(metadata['start_mono_ns'])
        self._meta = dict(metadata)
        self._meta.setdefault('created_at', time.strftime('%Y-%m-%dT%H:%M:%S%z'))
        self._files: dict[str, object] = {}
        self._csvs: dict[str, tuple] = {}
        self._planned_streams = list(streams)

    def _ensure_stream(self, stream: str):
        if stream in self._files:
            return
        bin_path = self.dir / f'{stream}.bin'
        csv_path = self.dir / f'{stream}-reads.csv'
        f = bin_path.open('ab')
        csv_f = csv_path.open('w', newline='', encoding='utf-8')
        writer = csv.writer(csv_f)
        writer.writerow(['host_ns', 'host_mono_ns', 'elapsed_s', 'offset', 'length'])
        self._files[stream] = f
        self._csvs[stream] = (csv_f, writer, f.tell())

    def write(self, stream: str, payload: bytes, host_ns: int, mono_ns: int):
        """Append one frame payload with its timestamps. Append-only."""
        self._ensure_stream(stream)
        f = self._files[stream]
        csv_f, writer, offset = self._csvs[stream]
        offset = f.tell()
        f.write(payload)
        writer.writerow([int(host_ns), int(mono_ns),
                         (int(mono_ns) - self._start_mono_ns) / 1e9, offset, len(payload)])
        self._csvs[stream] = (csv_f, writer, offset + len(payload))

    def close(self, status: str = 'recorded') -> Path:
        for f in self._files.values():
            f.close()
        for csv_f, _, _ in self._csvs.values():
            csv_f.close()
        self._meta['status'] = status
        (self.dir / 'metadata.json').write_text(
            json.dumps(self._meta, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
        return self.dir
