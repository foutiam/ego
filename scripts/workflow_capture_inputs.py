"""Prepare and package the actual QC evidence for a registered capture interval."""
from __future__ import annotations

from bisect import bisect_right
from base64 import b64encode
from functools import lru_cache
import csv
import hashlib
import io
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZipFile, ZIP_DEFLATED
import os
import sys

from workflow_segments import ROOT, fingerprint, read

if str(ROOT / 'code') not in sys.path:
    sys.path.insert(0, str(ROOT / 'code'))

from ego_capture.contact_calibration import (
    REVIEW as TACTILE_REVIEW, map_touch_time, move_between_camera_axes, raw_touch_time,
    review_camera_axes, timing_for_recording, visual_time,
)


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def rows(path):
    with path.open(encoding='utf-8-sig') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)



# Maximum age of a host-received frame for display and indexing, not a sync tolerance.
TACTILE_MAX_AGE_S = .1
TACTILE_KEYS = ('time_s', 'raw_values', 'score', 'byte_offsets', 'channel_labels', 'channel_map_source')


def tactile_window(path, start, end, *, include_arrays=False, timing=None):
    import numpy as np

    with np.load(path, allow_pickle=False) as source:
        clocks = np.asarray(source['time_s'], dtype=float)
        if (clocks.ndim != 1 or not np.isfinite(clocks).all() or (np.diff(clocks) < 0).any()
                or source['score'].shape != clocks.shape or len(source['raw_values']) != len(clocks)):
            raise ValueError('Invalid recorded glove clock or sample count')
        active = (timing or {}).get('status') == 'applied'
        corrected = map_touch_time(clocks, timing) if active else clocks
        first, stop = (int(np.searchsorted(corrected, t, side='left')) for t in (start, end))
        if active:
            lo, hi = map(int, np.searchsorted(clocks, timing['taskRangeS'], side='left'))
            first = min(hi, max(lo, first))
            stop = min(hi, max(first, stop))
        if not include_arrays:
            return first, corrected[first:stop]
        arrays = {key: source[key] for key in TACTILE_KEYS if key in source}
        for key in ('raw_values', 'score'):
            arrays[key] = arrays[key][first:stop]
        arrays['time_s'] = corrected[first:stop]
        if active:
            arrays['raw_time_s'] = clocks[first:stop]
        return first, arrays


@lru_cache(maxsize=2)
def _preview_counts(path, size, mtime_ns):
    import numpy as np

    with np.load(path, allow_pickle=False) as source:
        times, counts = source['time_s'], source['raw_values']
        offsets = source['byte_offsets']
        labels = source['channel_labels'] if 'channel_labels' in source else ()
    if (times.ndim != 1 or not np.isfinite(times).all() or (np.diff(times) < 0).any()
            or counts.ndim != 2 or counts.dtype != np.uint8 or counts.shape != (len(times), len(offsets))
            or not len(offsets)):
        raise ValueError('原始触觉数据的时间或通道不一致')
    channels = [{'offset': int(offset), 'label': str(labels[index]) if index < len(labels) else ''}
                for index, offset in enumerate(offsets)]
    return times, counts, channels


def tactile_preview(segment):
    """Raw channel counts on the same clip time axis as 01 playback."""
    import numpy as np

    folder = segment.session
    meta = read(folder / 'metadata.json')
    path = folder / 'derived/glove_signals.npz'
    start, end = (float(segment.row[key]) for key in ('start_s', 'end_s'))
    output = dict(**segment.identity(), startS=start, endS=end, unit='counts')
    if not path.is_file():
        return dict(output, status='missing')
    stat = path.stat()
    times, counts, channels = _preview_counts(path, stat.st_size, stat.st_mtime_ns)
    timing, camera, _ = timing_for_recording(folder, meta)
    active = timing['status'] == 'applied'
    low, high = start, end
    if active:
        host_axis, camera_axis = review_camera_axes(folder, meta, camera)
        aligned_edges = move_between_camera_axes(np.asarray([start, end]), host_axis, camera_axis)
        source_edges = raw_touch_time(aligned_edges, timing)
        task_lo, task_hi = timing['taskRangeS']
        low, high = max(task_lo, min(start, source_edges[0])), min(task_hi, max(end, source_edges[1]))
    lo, hi = (int(i) for i in np.searchsorted(times, [low, high], side='left'))
    selected = times[lo:hi]
    aligned = None
    if active:
        aligned = move_between_camera_axes(map_touch_time(selected, timing), camera_axis, host_axis).tolist()
    return dict(output, status='ready' if len(selected) else 'missing', channels=channels,
                rawTimeS=selected.tolist(), alignedTimeS=aligned,
                rawCounts=b64encode(counts[lo:hi].tobytes()).decode('ascii'),
                source='derived/glove_signals.npz:raw_values')


def capture_inputs(segment, *, project=ROOT):
    """Use the recorded host clock for association and retain the device clock."""
    session = segment.session
    meta = read(session / 'metadata.json')
    if meta.get('recording_format') == 'touchscale_rgbd':
        from workflow_touchscale import capture
        return capture(segment)
    origin = int(meta['start_mono_ns'])
    start, end = (segment.row[field] for field in ('start_s', 'end_s'))
    paired = session / 'derived/paired_frames.jsonl'
    sdk = session / 'openxr_skeleton.jsonl'
    quality = session / 'derived/quality.json'
    qc = read(quality)
    if qc.get('session_id') != segment.recording_id:
        raise ValueError('QC belongs to another recording')
    tactile = session / 'derived/glove_signals.npz'
    review_path = session / TACTILE_REVIEW
    review_revision = digest(review_path) if review_path.is_file() else None
    timing, left_clock, clock_path = timing_for_recording(session, meta)
    applied = timing['status'] == 'applied'
    tactile_start, tactile_end = start, end
    if applied and left_clock:
        host_axis, camera_axis = review_camera_axes(session, meta, left_clock)
        tactile_start, tactile_end = (float(move_between_camera_axes(t, host_axis, camera_axis)) for t in (start, end))
    tactile_first, tactile_times = tactile_window(tactile, tactile_start, tactile_end, timing=timing)
    session_exo = bool(clock_path and clock_path.name == 'exo-session.json')
    observed = []
    for record in rows(sdk) if sdk.is_file() else ():
        if record.get('session_id') != segment.recording_id or record.get('hand_side') != 'left':
            raise ValueError('SDK packet belongs to another recording or source hand')
        elapsed = (int(record['host_mono_ns']) - origin) / 1e9
        observed.append((elapsed, record))
    observed.sort(key=lambda value: value[0])
    clocks = [entry[0] for entry in observed]
    frames = []
    preview_root = (session / 'derived').resolve()
    for pair in rows(paired):
        elapsed = float(pair['t_s'])
        if not start <= elapsed < end:
            continue
        cameras = []
        for name in ('image', 'image1'):
            path = (preview_root / pair[name]).resolve(strict=True)
            if not path.is_relative_to(preview_root):
                raise ValueError('Stereo preview belongs to another recording')
            cameras.append(path.relative_to(project).as_posix())
        aligned = visual_time(pair, origin, left_clock)
        i = bisect_right(clocks, aligned) - 1
        pose = observed[i][1] if i >= 0 and 0 <= aligned - clocks[i] <= .1 else None
        touch_index = int(tactile_times.searchsorted(aligned, side='right')) - 1
        touch = None
        if touch_index >= 0 and 0 <= aligned - float(tactile_times[touch_index]) <= TACTILE_MAX_AGE_S:
            stamp = float(tactile_times[touch_index])
            touch = dict(windowIndex=touch_index, timeS=stamp, receiptAgeMs=(aligned - stamp) * 1000.)
            if applied:
                raw_stamp = raw_touch_time(stamp, timing)
                touch.update(rawTimeS=raw_stamp, associationAgeMs=touch['receiptAgeMs'],
                             receiptAgeMs=(aligned - raw_stamp) * 1000.)
        frames.append(dict(time_s=elapsed, alignedSessionTimeS=aligned, cameras=cameras, leftDeviceNs=str(pair['device_ns']),
                           stereoDeviceDeltaMs=pair['stereo_delta_ms'],
                           stereoReceiptDeltaMs=pair.get('stereo_receipt_delta_ms'), sdk=pose, tactile=touch))
    if not frames:
        raise ValueError('No prepared stereo observations in the selected interval')
    dependencies = [session / 'metadata.json', session / 'calibration.json', quality, paired, tactile]
    if sdk.is_file():
        dependencies.append(sdk)
    if clock_path:
        dependencies.append(clock_path)
    bindings = {p.relative_to(session).as_posix(): digest(p) for p in dependencies}
    if applied:
        if not review_revision or digest(review_path) != review_revision:
            raise ValueError('视触复核记录正在更新，请重新读取片段')
        bindings[TACTILE_REVIEW] = review_revision
    return dict(schema='ego-capture-interval-inputs-v1', **segment.identity(),
                startS=start, endS=end, recordingMetadata=dict(objectId=meta.get('object_id'),
                    sceneId=meta.get('scene_id'), split=meta.get('split')), associationClock='recording_host_monotonic',
                physicalExposureSyncQualified=False,
                alignment=dict(stereoPairingClock='camera_device_ns', cameraAnchor='cam0_qr_clock' if left_clock else 'cam0_host_receipt',
                               tactileAssociation='latest_in_interval_not_after_cam0', tactileMaxAgeMs=TACTILE_MAX_AGE_S*1000,
                               tactileFile=tactile.relative_to(session).as_posix(),
                               tactileClock=timing, tactileWindowS=[tactile_start, tactile_end],
                               tactileWindowFirstSourceIndex=tactile_first, tactileSamples=len(tactile_times),
                               tactileMatchedCameraFrames=sum(frame['tactile'] is not None for frame in frames),
                               sdkMatchedCameraFrames=sum(frame['sdk'] is not None for frame in frames)),
                files=bindings,
                exoSession='derived/exo-session.json' if session_exo else None,
                frames=frames)


def pack_capture(segment, destination, *, project=ROOT):
    """Archive original transport and exact interval indexes without claiming frame completeness."""
    data = capture_inputs(segment, project=project)
    session = segment.session
    start, end = data['startS'], data['endS']
    if end - start > 120:
        raise ValueError('The bounded export accepts at most 120 source seconds')
    destination = Path(destination)
    source_names = ('metadata.json', 'calibration.json', 'channels.json', 'camera.bin',
                    'camera-reads.csv', 'glove.bin', 'glove-reads.csv')
    originals = {name: digest(session / name) for name in source_names}
    key = fingerprint(dict(identity=segment.identity(), files=data['files'], original=originals,
                           exporter=digest(Path(__file__))))[:24]
    folder = destination / segment.key / key
    folder.mkdir(parents=True, exist_ok=True)
    archive, sidecar = folder / (segment.segment_id + '-aligned-ego.zip'), folder / 'manifest.json'
    if archive.is_file() and sidecar.is_file():
        value = read(sidecar)
        if value.get('archive_sha256') == digest(archive):
            return archive, sidecar, value
    manifest = dict(schema='ego-registered-capture-export-v1', **segment.identity(),
                    session=segment.recording_id, interval=dict(start_s=start, end_s=end),
                    clock=data['associationClock'], tactile_timing=data['alignment']['tactileClock'],
                    physical_sync_verified=False,
                    camera_frame_coverage='sampled QC previews plus original transport reads',
                    camera_slice_standalone_decodable=False,
                    source_sha256=originals, files=[])
    with NamedTemporaryFile(dir=folder, suffix='.tmp', delete=False) as stream:
        temporary = Path(stream.name)
    try:
        with ZipFile(temporary, 'w', compression=ZIP_DEFLATED) as package:
            def add(name, payload):
                package.writestr(name, payload)
                manifest['files'].append(dict(archive_path=name, bytes=len(payload),
                                              sha256=hashlib.sha256(payload).hexdigest()))
            add('interval.json', json.dumps(data, ensure_ascii=False).encode())
            import numpy as np
            alignment = data['alignment']
            _, sensor = tactile_window(session / alignment['tactileFile'], *alignment['tactileWindowS'],
                                       timing=alignment['tactileClock'], include_arrays=True)
            buffer = io.BytesIO()
            np.savez_compressed(buffer, **sensor)
            add('aligned/glove-signals.npz', buffer.getvalue())
            for name in ('metadata.json', 'calibration.json', 'channels.json'):
                add('source/' + name, (session / name).read_bytes())
            for kind in ('camera', 'glove'):
                index = []
                offset = 0
                checksum = hashlib.sha256()
                with (session / (kind + '.bin')).open('rb') as raw, (
                        session / (kind + '-reads.csv')).open(encoding='utf-8-sig') as log, (
                        package.open('source/' + kind + '.bin', 'w', force_zip64=True)) as target:
                    for row in csv.DictReader(log):
                        if not start <= float(row['elapsed_s']) < end:
                            continue
                        size = int(row['length'])
                        position = int(row['offset'])
                        if not 0 <= size <= 8_000_000 or position < 0:
                            raise ValueError('Invalid capture read interval')
                        raw.seek(position)
                        content = raw.read(size)
                        if len(content) != size:
                            raise ValueError('Capture data is truncated')
                        target.write(content)
                        checksum.update(content)
                        index.append(dict(row, source_offset=position, offset=offset))
                        offset += size
                if not index:
                    raise ValueError('No ' + kind + ' reads inside selected interval')
                manifest['files'].append(dict(archive_path='source/' + kind + '.bin', bytes=offset,
                                              sha256=checksum.hexdigest()))
                add('source/' + kind + '-reads.json', json.dumps(index).encode())
            sdk = session / 'openxr_skeleton.jsonl'
            start_ns = int(read(session / 'metadata.json')['start_mono_ns'])
            if sdk.is_file():
                samples = [row for row in rows(sdk) if start <= (int(row['host_mono_ns']) - start_ns) / 1e9 < end]
                add('source/openxr_skeleton.jsonl', ''.join(json.dumps(row) + '\n' for row in samples).encode())
            for frame in data['frames']:
                for name in frame['cameras']:
                    member = 'previews/' + '/'.join(Path(name).parts[-2:])
                    if member not in package.NameToInfo:
                        add(member, (project / name).read_bytes())
            package.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
        manifest.update(archive_sha256=digest(temporary), archive_bytes=temporary.stat().st_size)
        os.replace(temporary, archive)
        publish_json(sidecar, manifest)
    finally:
        temporary.unlink(missing_ok=True)
    return archive, sidecar, manifest


def configuration_receipt(segment, selection, method):
    """Freeze configuration without passing it off as a simulator result."""
    metadata = read(segment.session / 'metadata.json')
    fields = dict(schema='ego-workflow-configuration-v1', **segment.identity(),
                  actualExecuted=False, method=method)
    if method == 'pipeline_object_config':
        from ego_object_priors import resolve
        selected = selection.get('object', 'recorded')
        object_id = metadata.get('object_id') if selected == 'recorded' else selected
        if not object_id:
            raise ValueError('录制物体缺少身份声明')
        fields['profile'] = resolve(object_id)
        fields['recordedObject'] = metadata.get('object_id')
        low, high = selection.get('sizeMin', 1), selection.get('sizeMax', 1)
        if any(type(value) not in (int, float) for value in (low, high)) or not 0 < low <= high:
            raise ValueError('物体尺寸范围无效')
        fields['sizeRange'] = [low, high]
    else:
        scene = selection.get('sceneSet', 'deployment')
        if scene not in {'deployment', 'source_scene', 'native_local', 'random100'}:
            raise ValueError('未知的环境配置')
        if scene == 'native_local':
            seed = selection.get('seed')
            if (selection.get('scene') not in {'baseline', 'near-left', 'near-tilt'} or
                    type(seed) is not int or not 0 <= seed <= 2**31 - 1):
                raise ValueError('请选择有效本地场景和随机种子')
        fields['targetScene'] = {k: selection.get(k) for k in ('robot','sceneSet','scene','seed','environment','engine')}
    fields['configurationSha256'] = fingerprint(fields)
    return fields


def publish_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False, mode='w', encoding='utf-8') as stream:
        temp = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

