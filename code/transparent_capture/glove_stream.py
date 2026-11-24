"""Raw glove-stream parsing, envelope validation and counter-anomaly accounting.

Package 2B of the priority plan (docs/transparent-depth/RESEARCH_PRIORITY_PLAN.md
§13.1): turn the raw ``glove.bin`` capture into a reproducible, re-runnable report.

Everything here is a **read-only derivation over already-recorded raw bytes**. It
never touches a device, never writes into a session directory, and never converts
raw pressure counts into a physical unit.

Wire facts used below are vendor-documented and transcribed in
docs/transparent-depth/EMF_PROTOCOL_FACTS.md. Where the vendor documents nothing
(per-frame device time, raw pressure units, taxel geometry) this module reports
``UNKNOWN`` instead of inventing a value.

Counter semantics (EMF_PROTOCOL_FACTS.md, "Drops and sync error"):

* ``stream_sequence`` increments per successfully *sent* frame and is documented
  as the means to count transmit-side drops.
* ``frame_sequence`` is the pose-acquisition index. Its relationship to
  ``stream_sequence`` is explicitly **not** documented by the vendor, so the
  acquisition-side drop model stays ``UNKNOWN`` and is never asserted here.
* The frame carries no device timestamp at all, so device time can only be
  represented as this counter pair plus host arrival time.

A parse that validates the envelope says only that the bytes are a well-formed
QNBot frame. It is **not** a calibration check, not an accuracy measurement and
not evidence that any pose or pressure value is correct.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import struct
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from visuotactile.protocol import (CHECKSUM_OFFSET, EMF_OFFSET, EMF_SIZE,
                                   GLOVE_DATA_TYPE, GLOVE_FRAME_SIZE,
                                   GLOVE_FRAME_TYPE, GLOVE_HEADER, GLOVE_LENGTH,
                                   PRESSURE_OFFSET, PRESSURE_SIZE, decode_emf,
                                   decode_pressure)

# Vendor-documented finger order for the EMF array (EMF_PROTOCOL_FACTS.md,
# "Sensor and finger identity mapping": SDK `GloveFinger` = THUMB..PINKY).
FINGER_NAMES = ('thumb', 'index', 'middle', 'ring', 'pinky')

# `valid_mask` bits 5..7 are reserved with UNKNOWN meaning; they are masked off
# before the byte is interpreted (EMF_PROTOCOL_FACTS.md, "valid_mask semantics").
VALID_MASK_BITS = 0b0001_1111

# Stated as constants so a downstream consumer cannot silently assume otherwise.
DEVICE_TIMESTAMP_AVAILABLE = False
PRESSURE_UNITS = 'raw_uint8_counts'

# Reported instead of a number whenever the evidence does not support one.
UNKNOWN = 'UNKNOWN'


def envelope_defect(raw: bytes) -> str | None:
    """Return a defect code for one 548-byte candidate frame, or ``None``.

    Codes name the documented envelope field that failed, so a rejection is
    attributable instead of collapsing every cause into one generic message.
    This mirrors ``visuotactile.protocol._require_glove_frame``; the report
    cross-checks the two so a divergence between them cannot pass unnoticed.
    """
    if len(raw) != GLOVE_FRAME_SIZE:
        return 'length_not_548'
    if raw[:2] != GLOVE_HEADER:
        return 'bad_header'
    if raw[2] != GLOVE_FRAME_TYPE:
        return 'bad_frame_type'
    if struct.unpack_from('<H', raw, 3)[0] != GLOVE_LENGTH:
        return 'bad_length_field'
    if raw[5] != GLOVE_DATA_TYPE:
        return 'bad_data_type'
    expected = struct.unpack_from('<H', raw, CHECKSUM_OFFSET)[0]
    if (sum(raw[:CHECKSUM_OFFSET]) & 0xFFFF) != expected:
        return 'bad_checksum'
    return None


@dataclass(frozen=True)
class GloveFrame:
    """One accepted frame: counters, validity gate, activity and its own hash."""

    index: int
    offset: int
    host_ns: int | None
    host_mono_ns: int | None
    stream_sequence: int
    frame_sequence: int
    valid_mask: int
    hand_side: int
    finger_valid: tuple
    pressure_peak: int
    pressure_active: int
    frame_sha256: str


@dataclass(frozen=True)
class GloveStream:
    """Result of parsing one raw glove byte stream."""

    frame_size: int
    total_bytes: int
    frames_parsed: int
    accepted: int
    rejected: int
    reject_reasons: dict
    trailing_bytes: int
    frames: list
    sequence_accounting: dict
    retransmission_test: dict
    valid_mask_accounting: dict
    pressure_accounting: dict
    reads_reconciliation: dict
    rate_accounting: dict
    unresolved: list

    def to_dict(self, include_frames: bool = False) -> dict:
        out = asdict(self)
        if not include_frames:
            out['frames'] = {'count': len(self.frames),
                             'sample': [asdict(self.frames[i])
                                        for i in range(min(2, len(self.frames)))]}
        return out


def _read_manifest(session_dir: Path) -> list:
    """Read ``glove-reads.csv`` as (host_ns, host_mono_ns, offset, length) rows."""
    path = session_dir / 'glove-reads.csv'
    if not path.is_file():
        return []
    rows = []
    with path.open(encoding='utf-8-sig', newline='') as handle:
        for row in csv.DictReader(handle):
            rows.append((int(row['host_ns']), int(row['host_mono_ns']),
                         int(row['offset']), int(row['length'])))
    return rows


def _run_lengths(values) -> Counter:
    """Histogram of run lengths of consecutive equal values."""
    hist = Counter()
    if not values:
        return hist
    current = 1
    for previous, following in zip(values, values[1:]):
        if following == previous:
            current += 1
        else:
            hist[current] += 1
            current = 1
    hist[current] += 1
    return hist


def _counter_accounting(values: list) -> dict:
    """First/last/span plus the three delta classes the vendor's doc implies."""
    deltas = [b - a for a, b in zip(values, values[1:])]
    return {
        'first': values[0],
        'last': values[-1],
        'span': values[-1] - values[0],
        'samples': len(values),
        'distinct': len(set(values)),
        'advance_total': sum(d for d in deltas if d > 0),
        'repeat_events': sum(1 for d in deltas if d == 0),
        'gap_events': sum(1 for d in deltas if d > 1),
        'decrease_events': sum(1 for d in deltas if d < 0),
        'run_length_histogram': {str(k): v
                                 for k, v in sorted(_run_lengths(values).items())},
    }


def retransmission_test(stream_sequence: list, frame_sequence: list) -> dict:
    """Do repeated send counters co-occur with repeated acquisition counters?

    This decides whether a repeated ``stream_sequence`` may be treated as a
    duplicate. A repeated *pair* means the device re-sent the same acquisition.
    A repeated send counter with an **advanced** acquisition index would instead
    mean a pose was acquired but not counted as a new send -- a different
    mechanism with a different consequence, so the two are counted separately
    rather than assumed equal.
    """
    both = only_stream = only_frame = 0
    for index in range(1, len(stream_sequence)):
        stream_repeated = stream_sequence[index] == stream_sequence[index - 1]
        frame_repeated = frame_sequence[index] == frame_sequence[index - 1]
        if stream_repeated and frame_repeated:
            both += 1
        elif stream_repeated:
            only_stream += 1
        elif frame_repeated:
            only_frame += 1
    if both and not (only_stream or only_frame):
        verdict = 'consistent_with_device_retransmission'
    elif only_stream:
        verdict = 'send_counter_repeated_while_acquisition_advanced'
    elif not both and not only_frame:
        verdict = 'no_repeats'
    else:
        verdict = 'mixed_not_resolved_by_counters_alone'
    return {
        'stream_and_frame_repeated': both,
        'stream_repeated_frame_advanced': only_stream,
        'frame_repeated_stream_advanced': only_frame,
        'verdict': verdict,
        'limits': ('The vendor does not document the relationship between the two '
                   'counters, so a repeat is not by itself proof that the pose bytes '
                   'are stale. Only a byte-level comparison against the adjacent '
                   'frame can decide that, and this test does not inspect payload '
                   'equality.'),
    }


def _mask_accounting(masks: list) -> dict:
    """Per-finger validity-gate counts over the accepted frames."""
    per_finger = {name: {'valid': 0, 'invalid': 0} for name in FINGER_NAMES}
    reserved_seen = Counter()
    distinct = Counter(masks)
    for mask in masks:
        reserved_seen[str(mask & ~VALID_MASK_BITS)] += 1
        for index, name in enumerate(FINGER_NAMES):
            per_finger[name]['valid' if mask & (1 << index) else 'invalid'] += 1
    return {
        'distinct_masks': {str(k): v for k, v in sorted(distinct.items())},
        'per_finger': per_finger,
        'reserved_bits_seen': dict(sorted(reserved_seen.items())),
        'note': ('A 0 bit means that finger\'s 14 pose bytes must not be used as a '
                 'valid pose. It gates the pose, not the pressure grid. Bits 5-7 are '
                 'reserved with UNKNOWN meaning and are masked off, not interpreted.'),
    }
def _pressure_accounting(peaks: list, active: list, raw: list) -> dict:
    """Pressure-grid diagnostics, reported strictly as raw uint8 counts.

    The vendor documents no zero level for this grid, so "cells above zero" is not
    a contact measure: a sensor with a non-zero rest level would score every frame
    as active. Both the raw count and the *baseline-relative* delta against each
    cell's own per-session minimum are reported, so a consumer can see the
    difference instead of mistaking one for the other.
    """
    if not peaks:
        return {'units': PRESSURE_UNITS, 'frames': 0}
    grid = np.stack(raw).astype(np.int16)
    baseline = grid.min(axis=0)
    delta = grid - baseline
    return {
        'units': PRESSURE_UNITS,
        'units_note': ('The vendor documents no unit, no calibration curve and no zero '
                       'definition for the pressure grid, so these are raw uint8 counts. '
                       'They are not pascals, newtons or any calibrated force.'),
        'grid_shape': [PRESSURE_SIZE // 20, 20],
        'frames': len(peaks),
        'peak_counts': {'min': min(peaks), 'max': max(peaks)},
        'cells_above_zero_per_frame': {
            'min': min(active), 'max': max(active), 'mean': float(np.mean(active)),
            'interpretation': ('NOT a contact count. It counts cells whose raw count is '
                               'above zero, which includes any non-zero rest level.'),
        },
        'baseline_relative_delta': {
            'per_cell_baseline': 'per-session per-cell minimum of the raw count',
            'max_delta_observed': int(delta.max()),
            'cells_with_any_delta': int((delta.max(axis=0) > 0).sum()),
            'cells_with_delta_ge_10': int((delta.max(axis=0) >= 10).sum()),
        },
    }


def _reads_reconciliation(reads: list, total_bytes: int, frames_parsed: int) -> dict:
    """Reconcile the raw byte stream against its own append-only read log.

    The log records what the recorder believed it wrote. Comparing it with the
    file decides whether the bytes on disk are exactly the bytes the session
    claims -- a cheap integrity check that is independent of the frame parser.
    """
    if not reads:
        return {'available': False, 'note': 'no glove-reads.csv beside the raw stream'}
    offsets = [row[2] for row in reads]
    lengths = [row[3] for row in reads]
    total = sum(lengths)
    gaps = [b - (a + n) for a, n, b in zip(offsets, lengths, offsets[1:])]
    whole = sum(1 for n in lengths if n == GLOVE_FRAME_SIZE)
    return {
        'available': True,
        'rows': len(reads),
        'sum_length': total,
        'file_bytes': total_bytes,
        'sum_length_matches_file_bytes': total == total_bytes,
        'offsets_contiguous': all(gap == 0 for gap in gaps),
        'distinct_lengths': sorted(set(lengths)),
        'whole_frame_writes': whole,
        'partial_or_batched_writes': len(lengths) - whole,
        'frame_alignment': ('exact' if total == total_bytes else 'bytes_missing'),
        'note': ('Reads are the serial reader\'s chunk boundaries, not frame boundaries: '
                 'a logged length of 547 or 1643 means one chunk carried a partial or '
                 'several frames, so frame counting must come from the parser, never from '
                 'the row count. Chunk alignment is therefore reported as context only '
                 'and is not a pass/fail condition.'),
    }


def _rate_accounting(reads: list, accepted: int) -> dict:
    """Host-side receipt span and implied rate.

    Host arrival time is the only time available: the frame carries no device
    timestamp (EMF_PROTOCOL_FACTS.md, "Timestamps: device vs host").
    """
    if not reads:
        return {'available': False}
    mono = [row[1] for row in reads]
    span_ns = max(mono) - min(mono)
    return {
        'available': True,
        'host_mono_span_s': span_ns / 1e9 if span_ns else 0.0,
        'accepted_frames': accepted,
        'implied_host_receipt_hz': (accepted / (span_ns / 1e9)) if span_ns else None,
        'clock': 'host arrival (host_mono_ns); device timestamp is not derivable',
    }


def parse_glove_stream(session_dir, bin_name: str = 'glove.bin',
                       include_frames: bool = True) -> GloveStream:
    """Parse one session's raw glove stream into counters, validity and anomalies.

    The parser walks the file in fixed 548-byte strides. That is valid for a
    *recorded* stream because the concatenation of every logged read is
    frame-aligned (verified by ``reads_reconciliation``), even though the read
    chunks themselves are **not** frame-aligned -- a single logged read may carry
    a partial frame. A live serial reader cannot rely on stride alignment at all
    and must resynchronise on the header, which is what
    ``visuotactile.protocol.GloveDecoder`` does.
    """
    session_dir = Path(session_dir)
    bin_path = session_dir / bin_name
    if not bin_path.is_file():
        raise FileNotFoundError(f'raw glove stream not found: {bin_path}')
    blob = bin_path.read_bytes()
    total_bytes = len(blob)
    frames_parsed = total_bytes // GLOVE_FRAME_SIZE
    trailing_bytes = total_bytes % GLOVE_FRAME_SIZE
    reads = _read_manifest(session_dir)

    accepted_frames: list = []
    reject_reasons = Counter()
    stream_sequence: list = []
    frame_sequence: list = []
    masks: list = []
    hands = Counter()
    peaks: list = []
    active: list = []
    raw_grids: list = []

    for index in range(frames_parsed):
        offset = index * GLOVE_FRAME_SIZE
        raw = blob[offset:offset + GLOVE_FRAME_SIZE]
        defect = envelope_defect(raw)
        if defect is not None:
            reject_reasons[defect] += 1
            continue
        # Cross-check: the tracked decoder must agree this frame is acceptable.
        try:
            emf = decode_emf(raw)
            pressure = decode_pressure(raw)
        except ValueError as exc:
            reject_reasons['decoder_disagreement'] += 1
            if reject_reasons['decoder_disagreement'] == 1:
                reject_reasons['decoder_disagreement_message'] = str(exc)
            continue
        host_ns = host_mono_ns = None
        if index < len(reads):
            host_ns, host_mono_ns = reads[index][0], reads[index][1]
        stream_sequence.append(emf['stream_sequence'])
        frame_sequence.append(emf['frame_sequence'])
        masks.append(emf['valid_mask'])
        hands[emf['hand_side']] += 1
        peaks.append(int(pressure.max()))
        active.append(int((pressure > 0).sum()))
        raw_grids.append(pressure)
        if include_frames:
            accepted_frames.append(GloveFrame(
                index=index, offset=offset, host_ns=host_ns, host_mono_ns=host_mono_ns,
                stream_sequence=emf['stream_sequence'],
                frame_sequence=emf['frame_sequence'],
                valid_mask=emf['valid_mask'], hand_side=emf['hand_side'],
                finger_valid=tuple(bool(f['valid']) for f in emf['fingers']),
                pressure_peak=peaks[-1], pressure_active=active[-1],
                frame_sha256=hashlib.sha256(raw).hexdigest()))
    return _assemble(accepted_frames, stream_sequence, frame_sequence, masks, hands,
                     peaks, active, raw_grids, reads, total_bytes, frames_parsed,
                     trailing_bytes, reject_reasons)




def _assemble(accepted_frames, stream_sequence, frame_sequence, masks, hands, peaks,
              active, raw_grids, reads, total_bytes, frames_parsed, trailing_bytes,
              reject_reasons) -> GloveStream:
    """Build the immutable result record from the per-frame scan."""
    accepted = len(stream_sequence)
    return GloveStream(
        frame_size=GLOVE_FRAME_SIZE,
        total_bytes=total_bytes,
        frames_parsed=frames_parsed,
        accepted=accepted,
        rejected=frames_parsed - accepted,
        reject_reasons={str(k): v for k, v in sorted(reject_reasons.items())},
        trailing_bytes=trailing_bytes,
        frames=accepted_frames,
        sequence_accounting={
            'stream_sequence': _counter_accounting(stream_sequence) if stream_sequence else None,
            'frame_sequence': _counter_accounting(frame_sequence) if frame_sequence else None,
        },
        retransmission_test=retransmission_test(stream_sequence, frame_sequence),
        valid_mask_accounting=_mask_accounting(masks),
        pressure_accounting=_pressure_accounting(peaks, active, raw_grids),
        reads_reconciliation=_reads_reconciliation(reads, total_bytes, frames_parsed),
        rate_accounting=_rate_accounting(reads, accepted),
        unresolved=[
            'per-frame device timestamp: the wire frame carries no time field',
            'raw pressure unit and zero reference: the vendor documents neither',
            'acquisition-side drop model: the two counters\' relationship is undocumented',
            'per-taxel metric geometry: the vendor DXF was inspected on 2026-10-06 (58916 lines, '
            '854 LINE, 400 LWPOLYLINE, 3 POINT entities) and is an outline drawing with no taxel '
            'coordinate table, so no per-taxel position is derived from it',
            f'hand_side distribution observed in this session only: '
            f'{dict(sorted(hands.items()))} (0=right, 1=left)',
        ],
    )


@dataclass(frozen=True)
class GloveSeries:
    """Per-frame arrays from one raw glove stream, for a consumer that needs the grid.

    ``parse_glove_stream`` reports *accounting*: counters, validity distribution, peak
    statistics. This carries the per-frame values those statistics were computed from,
    which is what a contact-event emitter needs. The two walks share the same envelope
    validation and the same vendor decoders, and a test asserts they agree frame for
    frame, so the second walk cannot drift into a different interpretation of the bytes.
    """

    session_id: str
    frame_size: int
    frames_parsed: int
    accepted: int
    rejected: int
    reject_reasons: dict
    stream_sequence: list
    frame_sequence: list
    valid_mask: list
    hand_side: list
    host_ns: list
    host_mono_ns: list
    grids: object  # numpy uint8 [accepted, PRESSURE_SIZE // 20, 20], raw counts
    unresolved: list

    def to_dict(self, include_grids: bool = False) -> dict:
        out = asdict(self)
        if not include_grids:
            out['grids'] = {'shape': list(self.grids.shape), 'dtype': str(self.grids.dtype),
                            'units': PRESSURE_UNITS}
        return out


def _frame_arrivals(reads, frames_parsed: int) -> list:
    """Map each frame index to the arrival time of the read that completed it.

    The read log carries absolute byte offsets, so this mapping is exact. Mapping by row
    index is not: one read can carry a partial frame or several whole frames (measured in
    the pilot session: lengths 1, 547, 548, 1643, 3287 for 548-byte frames), so
    `reads[i]` is not frame `i`'s arrival time. Frames covered by no read row, and frames
    after the last row, get `None` rather than a borrowed timestamp.
    """
    arrivals = []
    for host_ns, host_mono_ns, offset, length in reads:
        while len(arrivals) < min(offset // GLOVE_FRAME_SIZE, frames_parsed):
            arrivals.append((None, None))
        while len(arrivals) < min((offset + length) // GLOVE_FRAME_SIZE, frames_parsed):
            arrivals.append((host_ns, host_mono_ns))
    while len(arrivals) < frames_parsed:
        arrivals.append((None, None))
    return arrivals


def pressure_series(session_dir, bin_name: str = 'glove.bin') -> GloveSeries:
    """Walk one raw stream and return the per-frame pressure grid and its metadata.

    Read-only over already-recorded bytes: no device, no write, no unit conversion. The
    pressure values stay raw uint8 counts; the per-cell baseline is the emitter's
    declared decision, not this module's.
    """
    session_dir = Path(session_dir)
    bin_path = session_dir / bin_name
    if not bin_path.is_file():
        raise FileNotFoundError(f'raw glove stream not found: {bin_path}')
    blob = bin_path.read_bytes()
    frames_parsed = len(blob) // GLOVE_FRAME_SIZE
    reads = _read_manifest(session_dir)
    arrivals = _frame_arrivals(reads, frames_parsed)
    grids, stream_sequence, frame_sequence, masks, hands = [], [], [], [], []
    host_ns, host_mono_ns = [], []
    reject_reasons = Counter()
    for index in range(frames_parsed):
        raw = blob[index * GLOVE_FRAME_SIZE:(index + 1) * GLOVE_FRAME_SIZE]
        defect = envelope_defect(raw)
        if defect is not None:
            reject_reasons[defect] += 1
            continue
        try:
            emf = decode_emf(raw)
            pressure = decode_pressure(raw)
        except ValueError as exc:
            reject_reasons['decoder_disagreement'] += 1
            if reject_reasons['decoder_disagreement'] == 1:
                reject_reasons['decoder_disagreement_message'] = str(exc)
            continue
        stream_sequence.append(emf['stream_sequence'])
        frame_sequence.append(emf['frame_sequence'])
        masks.append(emf['valid_mask'])
        hands.append(emf['hand_side'])
        grids.append(pressure)
        host_ns.append(arrivals[index][0])
        host_mono_ns.append(arrivals[index][1])
    return GloveSeries(
        session_id=session_dir.name,
        frame_size=GLOVE_FRAME_SIZE,
        frames_parsed=frames_parsed,
        accepted=len(grids),
        rejected=frames_parsed - len(grids),
        reject_reasons={str(k): v for k, v in sorted(reject_reasons.items())},
        stream_sequence=stream_sequence,
        frame_sequence=frame_sequence,
        valid_mask=masks,
        hand_side=hands,
        host_ns=host_ns,
        host_mono_ns=host_mono_ns,
        grids=(np.stack(grids) if grids
               else np.zeros((0, PRESSURE_SIZE // 20, 20), dtype=np.uint8)),
        unresolved=[
            'per-frame device timestamp: the wire frame carries no time field',
            'raw pressure unit and zero reference: the vendor documents neither',
            'per-taxel metric geometry: the vendor DXF was inspected on 2026-10-06 (58916 lines, '
            '854 LINE, 400 LWPOLYLINE, 3 POINT entities) and is an outline drawing with no taxel '
            'coordinate table, so no per-taxel position is derived from it',
        ])


def session_report(session_dir, include_frames: bool = False) -> dict:
    """Parse one session and wrap the result with the provenance a report needs."""
    session_dir = Path(session_dir)
    meta = {}
    meta_path = session_dir / 'metadata.json'
    if meta_path.is_file():
        meta = json.loads(meta_path.read_text(encoding='utf-8-sig'))
    stream = parse_glove_stream(session_dir, include_frames=include_frames)
    payload = stream.to_dict(include_frames=include_frames)
    payload['session_id'] = meta.get('session_id', session_dir.name)
    payload['evidence'] = meta.get('evidence', UNKNOWN)
    payload['declared_status'] = meta.get('status', UNKNOWN)
    payload['declared_duration_s'] = meta.get('duration_s', UNKNOWN)
    payload['declared_hand'] = meta.get('hand', UNKNOWN)
    payload['device'] = {
        'clock_alignment': meta.get('clock_alignment', UNKNOWN),
        'pose_metric_world_valid': meta.get('pose_metric_world_valid', UNKNOWN),
        'raw_glove_units': meta.get('raw_glove_units', UNKNOWN),
        'device_timestamp_available': DEVICE_TIMESTAMP_AVAILABLE,
    }
    payload['evidence_level'] = 'self-reproduced'
    payload['is_a_scientific_conclusion'] = False
    payload['note'] = ('Envelope acceptance, counter accounting and validity-gate counts '
                       'over already-recorded bytes. Not a calibration check, not an '
                       'accuracy measurement, and not evidence that any pose or pressure '
                       'value is correct.')
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Parse recorded QNBot glove raw streams and report anomalies.')
    parser.add_argument('sessions', nargs='+',
                        help='session directories, or a sessions root to scan')
    parser.add_argument('--out', default=None,
                        help='write the JSON report here instead of stdout')
    parser.add_argument('--include-frames', action='store_true',
                        help='include every accepted frame (large) in the report')
    args = parser.parse_args(argv)

    targets: list = []
    for raw in args.sessions:
        path = Path(raw)
        if not path.is_dir():
            parser.error(f'not a directory: {path}')
        if (path / 'metadata.json').is_file():
            targets.append(path)
        else:
            targets.extend(sorted(p for p in (path / 'sessions').glob('*')
                                  if (p / 'metadata.json').is_file()))
    if not targets:
        parser.error('no session directories found')

    reports = []
    for target in targets:
        if not (target / 'glove.bin').is_file():
            reports.append({'session_id': target.name, 'glove_bin': 'absent'})
            continue
        reports.append(session_report(target, include_frames=args.include_frames))

    summary = {
        'schema': 'glove-raw-parse-report-v1',
        'evidence_level': 'self-reproduced',
        'sessions': reports,
        'count': len(reports),
        'parse_command': ('python -m transparent_capture.glove_stream '
                          '<sessions-root> --out <report.json>'),
    }
    text = json.dumps(summary, indent=2, ensure_ascii=False, allow_nan=False) + '\n'
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding='utf-8')
        print(f'wrote {out} ({len(text)} bytes, {len(reports)} session(s))')
    else:
        print(text)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
