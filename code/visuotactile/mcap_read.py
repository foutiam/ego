"""Minimal reader for the MCAP streams that `protocol.McapWriter` produces.

The writer has existed since the first capture sessions, but nothing in this checkout
could read its output back, so the recorded messages (including the device's
`foxglove.CameraCalibration` and `foxglove.FrameTransform` records) were only reachable
through the raw serial log. This reader is deliberately small: opcode 3 (schema), 4
(channel) and 5 (message), uncompressed, which is exactly what the writer emits.

It is a *reader*: no field is interpreted here beyond the framing, and the raw serial log
stays the authoritative capture (as `storage.Session` states).
"""
from __future__ import annotations

import struct
from pathlib import Path

MAGIC = b'\x89MCAP0\r\n'


def _read_string(blob, offset):
    size = struct.unpack('<I', blob[offset:offset + 4])[0]
    return blob[offset + 4:offset + 4 + size].decode('utf-8', 'replace'), offset + 4 + size


def iter_messages(path):
    """Yield `{'topic', 'schema', 'sequence', 'device_ns', 'payload'}` per message.

    Records are read in file order. `device_ns` is the timestamp the writer stamped from
    the device, which is what makes the recorded order checkable.
    """
    data = Path(path).read_bytes()
    if data[:8] != MAGIC:
        raise ValueError('not an MCAP stream (bad magic)')
    schemas = {}
    channels = {}
    offset = 8
    while offset < len(data) - 8:
        opcode = data[offset]
        size = struct.unpack('<Q', data[offset + 1:offset + 9])[0]
        body = data[offset + 9:offset + 9 + size]
        offset += 9 + size
        if opcode == 3:
            schema_id = struct.unpack('<H', body[:2])[0]
            name, cursor = _read_string(body, 2)
            _encoding, cursor = _read_string(body, cursor)
            schemas[schema_id] = name
        elif opcode == 4:
            channel_id, schema_id = struct.unpack('<HH', body[:4])
            topic, cursor = _read_string(body, 4)
            encoding, cursor = _read_string(body, cursor)
            channels[channel_id] = {'schema': schemas.get(schema_id), 'topic': topic,
                                    'encoding': encoding}
        elif opcode == 5:
            channel_id, sequence, device_ns = struct.unpack('<HIQ', body[:14])
            channel = channels.get(channel_id)
            if channel is None:
                continue
            yield {'topic': channel['topic'], 'schema': channel['schema'],
                   'sequence': sequence, 'device_ns': device_ns,
                   'payload': body[22:]}


def channel_summary(path):
    """Message counts per topic, for a quick 'what did this session actually record' check."""
    counts = {}
    schemas = {}
    for message in iter_messages(path):
        key = message['topic']
        counts[key] = counts.get(key, 0) + 1
        schemas[key] = message['schema']
    return {topic: {'messages': count, 'schema': schemas.get(topic)}
            for topic, count in sorted(counts.items())}
