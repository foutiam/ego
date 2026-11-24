"""Prepare and package the actual QC evidence for a registered capture interval."""
from __future__ import annotations

from bisect import bisect_right
import csv
import hashlib
import io
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from zipfile import ZipFile, ZIP_DEFLATED
import os

from workflow_segments import ROOT, fingerprint, read


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


def tactile_window(path, start, end, *, include_arrays=False):
    import numpy as np

    with np.load(path, allow_pickle=False) as source:
        clocks = np.asarray(source['time_s'], dtype=float)
        if (clocks.ndim != 1 or not np.isfinite(clocks).all() or (np.diff(clocks) < 0).any()
                or source['score'].shape != clocks.shape or len(source['raw_values']) != len(clocks)):
            raise ValueError('Invalid recorded glove clock or sample count')
        first, stop = (int(np.searchsorted(clocks, t, side='left')) for t in (start, end))
        if not include_arrays:
            return first, clocks[first:stop]
        arrays = {key: source[key] for key in TACTILE_KEYS if key in source}
        for key in ('time_s','raw_values','score'):
            arrays[key] = arrays[key][first:stop]
        return first, arrays

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
    tactile_first, tactile_times = tactile_window(tactile, start, end)
    exo_report = session / 'derived/exo-session.json'
    session_exo = read(exo_report) if exo_report.is_file() else None
    left_clock = None
    if session_exo:
        if session_exo.get('schema') != 'ego-exo-session-v1' or session_exo.get('session_id') != segment.recording_id:
            raise ValueError('EXO sync belongs to another recording')
        left_clock = session_exo['sync']['cameras'][0]
        if (left_clock.get('name'),left_clock.get('mode')) != ('Ego CAM0','offset_and_drift'):
            raise ValueError('EXO sync has no drift-qualified Ego CAM0 clock')
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
        aligned = elapsed
        if left_clock is not None:
            aligned = (left_clock['host_mono_at_origin_s'] - origin/1e9
                       + left_clock['scale']*(int(pair['device_ns'])/1e9-left_clock['source_origin_s']))
        i = bisect_right(clocks, aligned) - 1
        pose = observed[i][1] if i >= 0 and 0 <= aligned - clocks[i] <= .1 else None
        touch_index = int(tactile_times.searchsorted(aligned, side='right')) - 1
        touch = None
        if touch_index >= 0 and 0 <= aligned - float(tactile_times[touch_index]) <= TACTILE_MAX_AGE_S:
            stamp = float(tactile_times[touch_index])
            touch = dict(windowIndex=touch_index, timeS=stamp, receiptAgeMs=(aligned - stamp) * 1000.)
        frames.append(dict(time_s=elapsed, alignedSessionTimeS=aligned, cameras=cameras, leftDeviceNs=str(pair['device_ns']),
                           stereoDeviceDeltaMs=pair['stereo_delta_ms'],
                           stereoReceiptDeltaMs=pair.get('stereo_receipt_delta_ms'), sdk=pose, tactile=touch))
    if not frames:
        raise ValueError('No prepared stereo observations in the selected interval')
    dependencies = [session / 'metadata.json', session / 'calibration.json', quality, paired, tactile]
    if sdk.is_file():
        dependencies.append(sdk)
    if session_exo:
        dependencies.append(exo_report)
    return dict(schema='ego-capture-interval-inputs-v1', **segment.identity(),
                startS=start, endS=end, recordingMetadata=dict(objectId=meta.get('object_id'),
                    sceneId=meta.get('scene_id'), split=meta.get('split')), associationClock='recording_host_monotonic',
                physicalExposureSyncQualified=False,
                alignment=dict(stereoPairingClock='camera_device_ns', cameraAnchor='cam0_qr_clock' if left_clock else 'cam0_host_receipt',
                               tactileAssociation='latest_in_interval_not_after_cam0', tactileMaxAgeMs=TACTILE_MAX_AGE_S*1000,
                               tactileFile=tactile.relative_to(session).as_posix(),
                               tactileWindowFirstSourceIndex=tactile_first, tactileSamples=len(tactile_times),
                               tactileMatchedCameraFrames=sum(frame['tactile'] is not None for frame in frames),
                               sdkMatchedCameraFrames=sum(frame['sdk'] is not None for frame in frames)),
                files={p.relative_to(session).as_posix(): digest(p) for p in dependencies},
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
                    clock=data['associationClock'], physical_sync_verified=False,
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
            _, sensor = tactile_window(session / data['alignment']['tactileFile'], start, end, include_arrays=True)
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

