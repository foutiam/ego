"""Offline session loader (no devices attached).

Reads Session-format directories written by code/visuotactile/storage.py:
  <stream>.bin raw payloads + <stream>-reads.csv (host_ns, host_mono_ns,
  elapsed_s, offset, length) + metadata.json (+ hand_pose.jsonl/events.jsonl).
Decoding of stream-specific payloads stays in the device stack; this loader
exposes raw frame records with timestamps plus filesystem refs so offline
tools (QC, training-data builder) can iterate deterministically.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from .calibration import read_meta, intrinsics_fingerprint


@dataclass
class StreamRecord:
    stream: str
    index: int
    host_ns: int
    host_mono_ns: int
    elapsed_s: float
    offset: int
    length: int
    bin_path: str


class SessionLoader:
    def __init__(self, session_dir: str | Path):
        p = Path(session_dir)
        if not (p / 'metadata.json').is_file():
            raise FileNotFoundError(f'not a Session directory: {p}')
        self.path = p
        self.meta = read_meta(p)

    @property
    def session_id(self):
        return self.meta.get('session_id', self.path.name)

    def streams(self):
        return sorted(x.name[:-10] for x in self.path.glob('*-reads.csv'))

    def records(self, stream: str) -> list[StreamRecord]:
        csv_path = self.path / f'{stream}-reads.csv'
        if not csv_path.is_file():
            raise FileNotFoundError(csv_path)
        out = []
        with csv_path.open(encoding='utf-8', newline='') as f:
            for i, row in enumerate(csv.DictReader(f)):
                out.append(StreamRecord(
                    stream=stream, index=i,
                    host_ns=int(row['host_ns']), host_mono_ns=int(row['host_mono_ns']),
                    elapsed_s=float(row['elapsed_s']), offset=int(row['offset']),
                    length=int(row['length']), bin_path=str(self.path / f'{stream}.bin')))
        return out

    def raw_payload(self, rec: StreamRecord) -> bytes:
        with open(rec.bin_path, 'rb') as f:
            f.seek(rec.offset)
            return f.read(rec.length)

    def calibration(self, stream: str) -> dict:
        return (self.meta.get('calibrations') or {}).get(stream, {})

    def calibration_fingerprints(self) -> dict[str, str]:
        return {s: intrinsics_fingerprint(self.calibration(s)) for s in self.streams()
                if self.calibration(s)}
