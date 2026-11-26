"""Bounded decoders for the Visio camera and QNBot glove wire formats."""
from __future__ import annotations
import binascii
import dataclasses
import struct
import time

import numpy as np


GLOVE_FRAME_SIZE = 548
GLOVE_HEADER = b'\xa5\x5a'
GLOVE_FRAME_TYPE = 0x01
GLOVE_LENGTH = 546
GLOVE_DATA_TYPE = 0x71
PRESSURE_OFFSET = 6
PRESSURE_SIZE = 23 * 20
EMF_OFFSET = 466
EMF_SIZE = 80
CHECKSUM_OFFSET = 546
GLOVE_HAND_SIDES = {0: 'right', 1: 'left'}


def glove_side(frame):
    """Return the firmware side from an already validated glove frame."""
    return GLOVE_HAND_SIDES.get(frame[EMF_OFFSET + 9])


def _require_glove_frame(frame: bytes | bytearray | memoryview) -> bytes:
    """Validate one complete QNBot frame and return it as immutable bytes."""
    raw = bytes(frame)
    if len(raw) != GLOVE_FRAME_SIZE:
        raise ValueError(f'QNBot frame must be {GLOVE_FRAME_SIZE} bytes, got {len(raw)}')
    if raw[:2] != GLOVE_HEADER:
        raise ValueError('Invalid QNBot frame header')
    if raw[2] != GLOVE_FRAME_TYPE or struct.unpack_from('<H', raw, 3)[0] != GLOVE_LENGTH:
        raise ValueError('Invalid QNBot frame type or length')
    if raw[5] != GLOVE_DATA_TYPE:
        raise ValueError('Invalid QNBot data type')
    expected = struct.unpack_from('<H', raw, CHECKSUM_OFFSET)[0]
    actual = sum(raw[:CHECKSUM_OFFSET]) & 0xFFFF
    if actual != expected:
        raise ValueError(f'Invalid QNBot checksum: expected {expected:#06x}, got {actual:#06x}')
    return raw


def decode_pressure(frame: bytes | bytearray | memoryview) -> np.ndarray:
    """Decode the 23x20 uint8 pressure grid from a validated frame."""
    raw = _require_glove_frame(frame)
    return np.frombuffer(raw, dtype=np.uint8, count=PRESSURE_SIZE,
                         offset=PRESSURE_OFFSET).reshape(23, 20).copy()


def _decode_finger_pose(raw: bytes, offset: int) -> dict[str, object]:
    values = struct.unpack_from('<7h', raw, offset)
    return {
        'position_mm': {
            'x': values[0] / 100.0,
            'y': values[1] / 100.0,
            'z': values[2] / 100.0,
        },
        'quaternion': {
            'w': values[3] / 32767.0,
            'x': values[4] / 32767.0,
            'y': values[5] / 32767.0,
            'z': values[6] / 32767.0,
        },
    }


def decode_emf(frame: bytes | bytearray | memoryview) -> dict[str, object]:
    """Decode EMF sequence fields and the five compact finger poses."""
    raw = _require_glove_frame(frame)
    base = EMF_OFFSET
    fingers = [_decode_finger_pose(raw, base + 10 + 14 * i) for i in range(5)]
    valid_mask = raw[base + 8]
    for index, finger in enumerate(fingers):
        finger['valid'] = bool(valid_mask & (1 << index))
    return {
        'stream_sequence': struct.unpack_from('<I', raw, base)[0],
        'frame_sequence': struct.unpack_from('<I', raw, base + 4)[0],
        'valid_mask': valid_mask,
        'hand_side': raw[base + 9],
        'fingers': fingers,
    }


def decode_frame(frame: bytes | bytearray | memoryview) -> dict[str, object]:
    """Decode pressure and EMF data from one complete validated frame."""
    raw = _require_glove_frame(frame)
    return {'pressure': decode_pressure(raw), 'emf': decode_emf(raw)}


def varint(data, offset=0):
    value = 0
    for shift in range(0, 70, 7):
        if offset >= len(data): raise ValueError('Truncated varint')
        byte = data[offset]; offset += 1; value |= (byte & 127) << shift
        if byte < 128: return value, offset
    raise ValueError('Oversized varint')


def protobuf(data):
    fields = {}; i = 0
    while i < len(data):
        key, i = varint(data, i); number, wire = key >> 3, key & 7
        if not number: raise ValueError('Invalid field zero')
        if wire == 0: value, i = varint(data, i)
        elif wire in (1, 2, 5):
            if wire == 2: size, i = varint(data, i)
            else: size = 8 if wire == 1 else 4
            if i + size > len(data): raise ValueError('Truncated protobuf')
            value = data[i:i + size]; i += size
        else: raise ValueError('Unsupported protobuf wire type')
        fields.setdefault(number, []).append(value)
    return fields


def first(fields, number, default=0): return fields.get(number, [default])[0]
def string(fields, number): return first(fields, number, b'').decode('utf-8', errors='replace')
def stamp_ns(raw):
    p = protobuf(raw)
    return first(p, 1) * 1_000_000_000 + first(p, 2)


def unsigned(value):
    out=bytearray()
    while value>127:out.append((value&127)|128);value>>=7
    out.append(value);return bytes(out)


def field(number, value):
    if isinstance(value,bytes):return unsigned(number*8+2)+unsigned(len(value))+value
    return unsigned(number*8)+unsigned(value)


def command(target, body_number, command_id):
    import datetime
    payload=b''
    if body_number==21:
        offset=int(datetime.datetime.now().astimezone().utcoffset().total_seconds()/60)
        payload=field(1,time.time_ns()//1000)+field(2,(offset<<1)^(offset>>63))
    cmd=field(1,target.encode())+field(2,command_id)+field(body_number,payload)
    ns=time.monotonic_ns();header=field(1,4)+field(2,command_id)+field(3,field(1,ns//10**9)+field(2,ns%10**9))
    core=bytes([len(header)])+header+cmd;core+=struct.pack('<H',binascii.crc_hqx(core,65535))
    out=bytearray([0]);at=0;size=1
    for byte in core:
        if byte==0:out[at]=size;at=len(out);out.append(0);size=1
        else:
            out.append(byte);size+=1
            if size==255:out[at]=size;at=len(out);out.append(0);size=1
    out[at]=size;return bytes(out)+b'\0'


def un_cobs(data):
    out = bytearray(); i = 0
    while i < len(data):
        size = data[i]; i += 1
        if not size or i + size - 1 > len(data): raise ValueError('Invalid COBS')
        out.extend(data[i:i + size - 1]); i += size - 1
        if size < 255 and i < len(data): out.append(0)
    return bytes(out)


@dataclasses.dataclass
class Message:
    stream_id: int
    sequence: int
    device_ns: int
    payload: bytes
    host_ns: int
    host_mono_ns: int
    end_offset: int


class VisioDecoder:
    def __init__(self):
        self.buffer = bytearray(); self.consumed = 0; self.good = 0; self.bad = 0
        self.channels = {}; self.identity = {}; self.max_buffer = 8_000_000

    def feed(self, data, host_ns=None, mono_ns=None):
        host_ns = time.time_ns() if host_ns is None else host_ns
        mono_ns = time.monotonic_ns() if mono_ns is None else mono_ns
        self.buffer.extend(data); messages = []
        while True:
            at = self.buffer.find(0)
            if at < 0: break
            encoded = bytes(self.buffer[:at]); del self.buffer[:at + 1]
            self.consumed += at + 1
            if not encoded: continue
            try:
                raw = un_cobs(encoded)
                if len(raw) < 4 or binascii.crc_hqx(raw[:-2], 65535) != int.from_bytes(raw[-2:], 'little'):
                    raise ValueError('CRC mismatch')
                header_size = raw[0]
                if header_size > len(raw) - 3: raise ValueError('Invalid header size')
                h = protobuf(raw[1:1 + header_size]); payload = raw[1 + header_size:-2]
                msg = Message(first(h, 1), first(h, 2), stamp_ns(first(h, 3, b'')), payload, host_ns, mono_ns, self.consumed)
                if msg.stream_id == 1:
                    p = protobuf(payload)
                    self.identity = {'name':string(p,1), 'firmware':string(p,3), 'hardware':string(p,4),
                                     'serial':string(p,5), 'boot_unix_seconds':first(p,6), 'role':string(p,7)}
                    for raw_channel in p.get(2, []):
                        c = protobuf(raw_channel)
                        self.channels[first(c,1)] = {'topic':string(c,2), 'encoding':string(c,3),
                                                     'schema':string(c,4), 'schema_data':first(c,5,b''), 'schema_encoding':string(c,6)}
                self.good += 1; messages.append(msg)
            except (ValueError, IndexError, struct.error): self.bad += 1
        if len(self.buffer) > self.max_buffer:
            self.consumed += len(self.buffer); self.buffer.clear(); self.bad += 1
        return messages


class GloveDecoder:
    HEADER = GLOVE_HEADER
    def __init__(self):
        self.buffer = bytearray(); self.consumed = 0; self.good = 0; self.bad = 0
    def feed(self, data):
        self.buffer.extend(data); result = []
        while len(self.buffer) >= 548:
            i = self.buffer.find(self.HEADER)
            if i < 0:
                n = len(self.buffer) - 5; del self.buffer[:n]; self.consumed += n; break
            if i:
                del self.buffer[:i]; self.consumed += i
            if len(self.buffer) < 548: break
            f = bytes(self.buffer[:548])
            try:
                _require_glove_frame(f)
            except ValueError:
                del self.buffer[0]; self.consumed += 1; self.bad += 1; continue
            del self.buffer[:548]; self.consumed += 548; self.good += 1
            result.append((f, self.consumed))
        return result


def camera_calibration(payload):
    p = protobuf(payload)
    number=lambda n:int.from_bytes(first(p,n),'little') if isinstance(first(p,n),bytes) else first(p,n)
    out = {'width':number(2), 'height':number(3), 'distortion_model':string(p,4), 'frame_id':string(p,9)}
    # Foxglove CameraCalibration arrays are packed doubles.
    for n, name in ((5,'D'),(6,'K'),(7,'R'),(8,'P')):
        vals = []
        for raw in p.get(n, []):
            vals.extend(struct.unpack('<' + 'd' * (len(raw)//8), raw))
        out[name] = vals
    return out


def frame_transform(payload):
    """Foxglove FrameTransform: the device's own rigid transform between two frames.

    The stereo camera publishes this on `/ego/camera/1/extrinsics` and
    `/ego/imu/0/extrinsics` (observed in the real session 20260915-222256-5ef746),
    which is the only place the device states a *relative* pose: the per-eye
    CameraCalibration carries K/D with `R = I` and a zero translation column in `P`,
    so it does NOT contain the stereo baseline.

    Field numbers follow the published foxglove.FrameTransform schema: 1 timestamp,
    2 parent_frame_id, 3 child_frame_id, 4 translation, 5 rotation. Both vectors are
    nested messages of packed little-endian doubles, exactly like the calibration
    arrays above.

    Units and direction are NOT asserted here: the payload is returned as received.
    `ego_capture.camera_calibration` states what is documented, what is only
    plausible, and what stays unverified (notably the child-in-parent vs
    parent-in-child convention).
    """
    p = protobuf(payload)

    def vector(field, size, name):
        nested = first(p, field, b'')
        if not nested:
            raise ValueError('FrameTransform is missing ' + name)
        fields = protobuf(nested)
        values = []
        for n in range(1, size + 1):
            raw = first(fields, n, b'')
            if not isinstance(raw, bytes) or len(raw) != 8:
                raise ValueError('FrameTransform %s is truncated' % name)
            values.append(struct.unpack('<d', raw)[0])
        return tuple(values)

    return {'parent_frame_id': string(p, 2), 'child_frame_id': string(p, 3),
            'translation': vector(4, 3, 'translation'),
            'rotation': vector(5, 4, 'rotation_xyzw')}


def _s(text):
    b = text.encode(); return struct.pack('<I', len(b)) + b


class McapWriter:
    """Uncompressed MCAP stream; raw serial remains the authoritative capture."""
    MAGIC = b'\x89MCAP0\r\n'
    def __init__(self, path):
        self.file = path.open('xb'); self.file.write(self.MAGIC); self.schemas = {}; self.channels = set()
        self.record(1, _s('') + _s('diss-visuotactile-0.1')); self.closed = False
    def record(self, opcode, data): self.file.write(bytes([opcode]) + struct.pack('<Q',len(data)) + data)
    def write(self, msg, c):
        if not c: return
        if msg.stream_id not in self.channels:
            key = (c['schema'],c['schema_data'])
            if key not in self.schemas:
                sid = len(self.schemas) + 1; self.schemas[key] = sid; blob = c['schema_data']
                self.record(3,struct.pack('<H',sid)+_s(c['schema'])+_s(c.get('schema_encoding') or 'protobuf')+struct.pack('<I',len(blob))+blob)
            sid = self.schemas[key]
            self.record(4,struct.pack('<HH',msg.stream_id,sid)+_s(c['topic'])+_s(c['encoding'])+struct.pack('<I',0)); self.channels.add(msg.stream_id)
        self.record(5,struct.pack('<HIQQ',msg.stream_id,msg.sequence & 0xffffffff,msg.device_ns,msg.device_ns)+msg.payload)
    def close(self):
        if self.closed: return
        self.record(15,struct.pack('<I',0)); self.record(2,struct.pack('<QQI',0,0,0)); self.file.write(self.MAGIC); self.file.close(); self.closed=True
