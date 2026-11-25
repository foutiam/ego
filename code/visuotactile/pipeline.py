"""Offline derivation and reviewed, object-disjoint learning samples."""
from __future__ import annotations
import csv
import base64
import uuid
import hashlib
import json
import math
import re
import shutil
import time
from pathlib import Path
from collections import OrderedDict

import cv2
import numpy as np
from .protocol import VisioDecoder, GloveDecoder, protobuf, first
from .devices import VideoDecoder
from .pose import HandPose
from .storage import contact_calibration_windows, dump_json, recorded_task_range


def read_json(path): return json.loads(Path(path).read_text(encoding='utf-8-sig'))


# The stereo pair window is applied to the **camera's own sampled timestamp** (`device_ns`), which is
# what "these two views are the same sampled instant" can be checked against offline. It is not an
# exposure or reception claim: host receipt is asynchronous, so the two views' `host_ns` are further
# apart than this window, and the two numbers are reported separately by `delta_summary` below.
STEREO_PAIR_WINDOW_MS = 10.0


def delta_summary(values, bound_ms):
    """`min` / `median` / `max` and the out-of-bound count of a list of millisecond deltas.

    `beyond_bound` is the count of deltas *outside* the declared bound, reported rather than filtered:
    a filter that hides the tail is how a window becomes a synchronisation claim it never measured.
    """
    array = np.asarray(values, dtype=float)
    if array.size == 0:
        return {'count': 0, 'bound_ms': float(bound_ms), 'min': None, 'median': None, 'max': None,
                'beyond_bound': 0}
    return {'count': int(array.size), 'bound_ms': float(bound_ms), 'min': float(array.min()),
            'median': float(np.median(array)), 'max': float(array.max()),
            'beyond_bound': int((array > float(bound_ms)).sum())}


def pair_stereo_frames(left, right):
    """Pair sorted device times by nearest neighbor, with at most one use per exposure."""
    left = sorted(left, key=lambda f: f['device_ns'])
    right = sorted(right, key=lambda f: f['device_ns'])
    if not left or not right:
        return [], [], [], 0
    ts0 = np.asarray([f['device_ns'] for f in left], dtype=np.int64)
    ts1 = np.asarray([f['device_ns'] for f in right], dtype=np.int64)
    insertion = np.searchsorted(ts1, ts0)
    lower = np.maximum(insertion-1, 0)
    upper = np.minimum(insertion, len(ts1)-1)
    indices = np.where(abs(ts1[upper]-ts0) < abs(ts1[lower]-ts0), upper, lower)
    # If several left exposures select the same right exposure, keep the closest.
    winners = {}
    conflicts = 0
    for i, j in enumerate(indices.tolist()):
        gap = abs(int(ts0[i])-int(ts1[j]))
        if gap > STEREO_PAIR_WINDOW_MS*1e6:
            continue
        if j in winners:
            conflicts += 1
            if (gap, i) >= winners[j]:
                continue
        winners[j] = (gap, i)
    paired, device_deltas, receipt_deltas = [], [], []
    for j, (gap, i) in sorted(winners.items(), key=lambda item: item[1][1]):
        f, other = left[i], right[j]
        receipt = abs(other['host_mono_ns']-f['host_mono_ns'])/1e6
        delta = gap/1e6
        device_deltas.append(delta)
        receipt_deltas.append(receipt)
        paired.append(dict(f, image1=other['image'], stereo_delta_ms=delta,
                           stereo_receipt_delta_ms=receipt))
    return paired, device_deltas, receipt_deltas, conflicts

def read_jsonl(path):
    if not Path(path).exists():return []
    return [json.loads(line) for line in Path(path).read_text('utf-8').splitlines() if line.strip()]


def sha256_file(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()


def delete_dataset(root, dataset_id):
    root=Path(root);dataset_id=str(dataset_id).strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',dataset_id):raise ValueError('Invalid dataset identifier')
    dataset=(root/'datasets'/dataset_id).resolve();datasets_root=(root/'datasets').resolve()
    if dataset.parent != datasets_root or not dataset.exists() or not dataset.is_dir():raise ValueError('Dataset not found')
    shutil.rmtree(dataset)
    return {'deleted':dataset_id,'path':str(dataset)}


def delete_session(root, session_id):
    root=Path(root);session_id=str(session_id).strip()
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,90}',session_id):raise ValueError('Invalid session identifier')
    session=(root/'sessions'/session_id).resolve();sessions_root=(root/'sessions').resolve()
    if session.parent != sessions_root or not session.exists() or not session.is_dir():raise ValueError('Session not found')
    shutil.rmtree(session)
    return {'deleted':session_id,'path':str(session)}


def raw_reads(path, kind):
    with (path/f'{kind}.bin').open('rb') as raw, (path/f'{kind}-reads.csv').open(encoding='utf-8-sig') as log:
        for row in csv.DictReader(log):
            n=int(row['length']);offset=int(row['offset'])
            if n<0 or n>8_000_000:raise ValueError('Invalid raw read length')
            raw.seek(offset);data=raw.read(n)
            if len(data)!=n:raise ValueError('Raw capture is incomplete')
            yield data,int(row['host_ns']),int(row['host_mono_ns'])


def process_session(path, config, progress=lambda _:None, infer_pose=True):
    path=Path(path);meta=read_json(path/'metadata.json')
    if meta['status']=='recording':raise ValueError('请先停止录制')
    target=path/'derived'
    if (target/'quality.json').exists():return read_json(target/'quality.json')
    target.mkdir(exist_ok=True)
    for cam in (0,1):(target/f'cam{cam}').mkdir(exist_ok=True)
    parser=VisioDecoder();video=[VideoDecoder(),VideoDecoder()];last_bucket=[None,None];frames=[];poses=[]
    channel_file=path/'channels.json'
    if channel_file.exists():
        parser.channels={int(k):dict(c,schema_data=base64.b64decode(c['schema_data'])) for k,c in read_json(channel_file).items()}
    recorded_sdk=(path/'openxr_skeleton.jsonl').exists() and (path/'openxr_skeleton.jsonl').stat().st_size>0
    pose_model=HandPose(config['hand']) if infer_pose and not recorded_sdk else None
    origin=meta['start_mono_ns'];fps=config['training']['sample_fps'];start=time.monotonic();written=0
    packet_clocks = [OrderedDict(), OrderedDict()]
    unmatched_frame_clocks = 0
    calibration_windows = contact_calibration_windows(meta)
    calibration_dir = target / 'contact-calibration'
    calibration_frames = []
    if calibration_windows:
        for cam in (0, 1):
            (calibration_dir / f'cam{cam}').mkdir(parents=True, exist_ok=True)
    task_range = recorded_task_range(meta)
    try:
        for data,host,mono in raw_reads(path,'camera'):
            processing_limit=float(config.get('processing_max_seconds',max(900.,min(7200.,float(meta.get('duration_s',0))*3))))
            if time.monotonic()-start>processing_limit:raise ValueError('QC processing time limit reached; original recording retained')
            for msg in parser.feed(data,host,mono):
                c=parser.channels.get(msg.stream_id,{})
                if c.get('schema')!='foxglove.CompressedVideo':continue
                cam=0 if c['topic'].endswith('/0') else 1
                clocks = packet_clocks[cam]
                clocks[msg.device_ns] = (msg.host_ns, msg.host_mono_ns)
                if len(clocks) > 256:
                    clocks.popitem(last=False)
                for decoded in video[cam].decode(first(protobuf(msg.payload),3,b''),msg.device_ns):
                    stamp=decoded.pts
                    frame_clock=clocks.pop(stamp,None)
                    if frame_clock is None:
                        unmatched_frame_clocks += 1
                        continue
                    frame_host, frame_mono = frame_clock
                    receipt_s = (frame_mono - origin) / 1e9
                    part = next((label for label, (lo, hi) in calibration_windows.items()
                                 if lo <= receipt_s < hi), None)
                    bucket = int(stamp / 1e9 * fps)
                    sampled = bucket != last_bucket[cam]
                    if not sampled and part is None:
                        continue
                    rgb = decoded.to_ndarray(format='rgb24')
                    h,w = rgb.shape[:2]
                    scale = min(1., 960 / w)
                    im = cv2.resize(rgb, (round(w*scale),round(h*scale)))
                    if part:
                        review = calibration_dir / f'cam{cam}/{stamp}.jpg'
                        cv2.imencode('.jpg', cv2.cvtColor(im,cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY,85])[1].tofile(review)
                        written += review.stat().st_size
                        calibration_frames.append(dict(cam=f'cam{cam}', part=part, device_ns=str(stamp),
                                                       source_s=stamp/1e9, receipt_s=receipt_s))
                    if not sampled:
                        continue
                    last_bucket[cam] = bucket
                    name=f'cam{cam}/{stamp}.jpg';file=target/name
                    cv2.imwrite(str(file),cv2.cvtColor(im,cv2.COLOR_RGB2BGR),[cv2.IMWRITE_JPEG_QUALITY,88]);written+=file.stat().st_size
                    record={'cam':cam,'device_ns':stamp,'host_ns':frame_host,'host_mono_ns':frame_mono,'t_s':(frame_mono-origin)/1e9,
                            'image':name,'raw_width':w,'raw_height':h,'derived_width':im.shape[1],'derived_height':im.shape[0],
                            'intrinsic_scale_xy':[scale,scale]}
                    frames.append(record)
                    if cam==0 and pose_model:
                        pose=pose_model.infer(rgb,stamp,frame_host,frame_mono);pose['t_s']=record['t_s'];poses.append(pose)
                    if len(frames)%50==0:
                        progress(f'已处理 {len(frames)} 个抽样图像')
                        if written>2*1024**3 or shutil.disk_usage(path).free<config['minimum_free_bytes']:raise ValueError('达到处理存储上限')
        glove=GloveDecoder();signals=[];raw_values=[];times=[]
        offsets=config['contact_byte_offsets']
        for data,host,mono in raw_reads(path,'glove'):
            for raw,end in glove.feed(data):
                raw_values.append(np.frombuffer(raw,dtype=np.uint8)[offsets].copy());times.append((mono-origin)/1e9)
        if not raw_values:raise ValueError('手套记录中没有有效帧')
        values=np.array(raw_values,dtype=float);times=np.array(times);cal=read_json(path/'calibration.json')
        if calibration_windows:
            task_samples = ((times >= task_range[0]) & (times < task_range[1])) if task_range else np.zeros(len(times), dtype=bool)
        else:
            task_samples = np.ones(len(times), dtype=bool)
        task_times = times[task_samples]
        baseline=cal.get('touch_baseline')
        baseline_source='captured_rest_calibration'
        if baseline is None:
            baseline_mask = task_samples & (times <= task_times[0] + 2) if len(task_times) else (times <= times[0] + 2)
            baseline=np.median(values[baseline_mask],axis=0);baseline_source='first_two_seconds_unverified'
        score=np.median(np.maximum(values-np.asarray(baseline),0),axis=1)
        intervals=[];active=False;on=None;change=0
        for t,s in zip(task_times,score[task_samples]):
            desired=s>config['contact_off_delta'] if active else s>=config['contact_on_delta']
            change=change+1 if desired!=active else 0
            if change>=3:
                active=desired;change=0
                if active:on=float(t)
                elif on is not None:intervals.append([on,float(t)]);on=None
        if on is not None:intervals.append([on,float(task_times[-1])])
        labels=np.asarray(config.get('contact_channel_map',[]),dtype='U')
        np.savez_compressed(target/'glove_signals.npz',time_s=times,raw_values=values.astype(np.uint8),score=score,
                            byte_offsets=offsets,channel_labels=labels,
                            channel_map_source=np.asarray(config.get('contact_channel_map_source','unconfirmed')))
        task_frames = (frames if not calibration_windows else
                       [f for f in frames if task_range and task_range[0] <= f['t_s'] < task_range[1]])
        paired, device_deltas, receipt_deltas, duplicate_pairs = pair_stereo_frames(
            (f for f in task_frames if f['cam']==0), (f for f in task_frames if f['cam']==1))
        if calibration_windows:
            (calibration_dir/'frames.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in calibration_frames),
                                                        encoding='utf-8')
        # Keep host-time navigation ordered even when serial reads tie or arrive out of device order.
        paired.sort(key=lambda row: (row['host_mono_ns'], row['device_ns']))

        for name,items in [('frames.jsonl',frames),('paired_frames.jsonl',paired),('hand_pose.jsonl',poses)]:
            (target/name).write_text(''.join(json.dumps(x,ensure_ascii=False,allow_nan=False)+'\n' for x in items),encoding='utf-8')
        quality={'session_id':meta['session_id'],'camera_valid_messages':parser.good,'camera_invalid_or_partial':parser.bad,
                 'glove_valid_frames':glove.good,'glove_checksum_failures':glove.bad,'sampled_images':len(frames),
                 # Two clock bases, reported separately and never merged into a synchronisation claim.
                 # `device` is the camera's own sampled timestamp (the window the pairing decision uses)
                 # and `receipt` is when this host received each message; neither is an exposure time.
                 # `beyond_bound` counts the pairs outside the declared bound instead of hiding them,
                 # and `stereo_pairs_paired` is a pairing count, not a "within 10 ms" statement.
                 'stereo_pairs_paired':len(paired),
                 'stereo_reused_right_candidates_rejected':duplicate_pairs,
                 'unbound_decoded_camera_frames':unmatched_frame_clocks,
                 'camera_receipt_binding':'decoded_pts_to_originating_packet_host_clock',
                 'stereo_pair_device_delta_ms':dict(delta_summary(device_deltas,STEREO_PAIR_WINDOW_MS),
                                                    basis='camera_device_ns'),
                 'stereo_pair_receipt_delta_ms':dict(delta_summary(receipt_deltas,STEREO_PAIR_WINDOW_MS),
                                                     basis='host_monotonic_receipt_ns'),
                 'stereo_pair_clock_basis':'device_and_receipt_deltas_are_clocks_not_exposure_synchronisation',
                 'detected_left_hand_frames':sum(p.get('detected',False) for p in poses),

                 'contact_candidates':intervals,'baseline_source':baseline_source,'pose_source':'recorded_sdk_wrist_local_not_camera_aligned' if recorded_sdk else 'vision_estimate',
                 'recorded_sdk_available':recorded_sdk,'vision_pose_inference_executed':pose_model is not None,
                 'clock_alignment':'host_receipt_not_sensor_sync','processing_seconds':time.monotonic()-start,
                 'raw_sha256':{kind:sha256_file(path/f'{kind}.bin') for kind in ('camera','glove')}}
        dump_json(target/'quality.json',quality);progress('处理完成，请复核接触区间和时间误差');return quality
    finally:
        if pose_model:pose_model.close()


def validate_annotations(annotation, duration):
    if not annotation.get('reviewed'):raise ValueError('请先结合录像复核事件，再勾选已审核')
    uncertainty=annotation.get('sync_uncertainty_ms')
    if isinstance(uncertainty,bool) or not isinstance(uncertainty,(int,float)) or not math.isfinite(uncertainty) or not 0<uncertainty<=2000:
        raise ValueError('请填写实测/复核支持的时间不确定度（0–2000 ms）；不能填0冒充精确同步')
    intervals=annotation.get('contact_intervals',[]);last=-1
    for pair in intervals:
        if len(pair)!=2 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in pair):raise ValueError('接触区间格式错误')
        a,b=pair
        if not 0<=a<b<=duration or a<last:raise ValueError('接触区间须按时间排列、不重叠且位于录像内')
        last=b
    for q in annotation.get('distance_queries',[]):
        if not q.get('reference_verified') or not q.get('reference_source'):raise ValueError('距离标签需要独立参照及其来源')
        if q.get('reference_source') in ('mediapipe','hand_relative_model_m','tactile_raw'):raise ValueError('模型手姿和触觉原始值不能充当真实距离')
        if not 0<=float(q['t_s'])<=duration:raise ValueError('距离查询时刻超出录像')
        if not isinstance(q.get('no_contact',False),bool):raise ValueError('no_contact 必须为布尔值')
        if not q.get('path_known_before_query'):raise ValueError('距离查询需要事先确定的接近路径')
        if q.get('path_frame')!='camera0':raise ValueError('路径方向必须使用 camera0 坐标系，并注明 path_frame')
        direction=q.get('path_direction')
        if not isinstance(direction,list) or len(direction)!=3 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in direction) or not .99<=np.linalg.norm(direction)<=1.01:
            raise ValueError('路径方向须为有限的三维单位向量')
        travel=q.get('travel_m')
        if not isinstance(travel,(int,float)) or not math.isfinite(travel) or travel<=0:raise ValueError('需要有效的预定行程 travel_m')
        if not q.get('no_contact',False):
            lo,hi=float(q['lower_m']),float(q['upper_m'])
            if not math.isfinite(lo+hi) or not 0<=lo<hi:raise ValueError('距离标签必须是有误差范围的有效米制区间')
            if hi>travel:raise ValueError('接触距离区间超出预定行程')
    return annotation


def causal_contact_label(t, intervals, horizon, uncertainty, duration):
    """A label only when all times in the uncertainty interval agree."""
    if t+horizon+uncertainty>duration:return None
    # Frames at/after contact are not evidence for predicting that contact.
    if any(a-uncertainty<=t<=b+uncertainty for a,b in intervals):return None
    onsets=[a for a,b in intervals if a>t]
    if any(abs(a-(t+horizon))<=uncertainty or abs(a-t)<=uncertainty for a in onsets):return None
    return int(any(t+uncertainty<a<t+horizon-uncertainty for a in onsets))


def build_dataset(root, config, task='future_contact', allow_synthetic=False):
    root=Path(root);samples=[];object_splits={};reasons=[];sources=[];capture_splits={}
    for p in sorted((root/'sessions').glob('*/metadata.json')):
        path=p.parent;meta=read_json(p);annotations_path=path/'annotations.json'
        if meta.get('evidence')!='real_devices' and not (allow_synthetic and meta.get('evidence')=='synthetic_software_fixture'):continue
        if meta.get('split') == 'unassigned':
            reasons.append(f"{meta['session_id']}: 尚未分配数据集")
            continue
        if not (path/'derived/quality.json').exists():reasons.append(f"{meta['session_id']}: 尚未处理");continue
        annotation=read_json(annotations_path)
        if not annotation.get('reviewed'):reasons.append(f"{meta['session_id']}: 尚未审核");continue
        validate_annotations(annotation,meta['duration_s'])
        obj=meta['object_id'];split=meta['split']
        if obj in object_splits and object_splits[obj]!=split:raise ValueError('同一物体跨数据集，拒绝构建')
        object_splits[obj]=split
        quality=read_json(path/'derived/quality.json')
        for kind,digest in quality.get('raw_sha256',{}).items():
            key=(kind,digest)
            if key in capture_splits and capture_splits[key]!=split:raise ValueError('同一原始采集跨数据集，拒绝构建')
            capture_splits[key]=split
        frames=read_jsonl(path/'derived/paired_frames.jsonl');poses={p['device_ns']:p for p in read_jsonl(path/'derived/hand_pose.jsonl')}
        sources.append({'session_id':meta['session_id'],'object_id':obj,'split':split,
                        'annotations_sha256':hashlib.sha256(annotations_path.read_bytes()).hexdigest(),
                        'quality_sha256':hashlib.sha256((path/'derived/quality.json').read_bytes()).hexdigest(),
                        'raw_sha256':quality.get('raw_sha256',{})})
        for frame in frames:
            t=frame['t_s'];pose=poses.get(frame['device_ns'],{});xy=pose.get('landmarks',[[0,0,0]]*21)
            features=[float(v) for pt in xy for v in pt[:2]]+[float(pose.get('detected',False)),float(pose.get('confidence',0))]
            row={'sample_id':f"{meta['session_id']}-{frame['device_ns']}",'session_id':meta['session_id'],'object_id':obj,
                 'split':split,'t_s':t,'device_ns':frame['device_ns'],'image0':str(path/'derived'/frame['image']),
                 'image1':str(path/'derived'/frame['image1']),'features':features,'stereo_delta_ms':frame['stereo_delta_ms']}
            if task=='future_contact':
                label=causal_contact_label(t,annotation['contact_intervals'],config['training']['horizon_s'],annotation['sync_uncertainty_ms']/1000,meta['duration_s'])
                if label is None:continue
                row['target']=label
            elif task=='approach_distance':
                candidates=[q for q in annotation.get('distance_queries',[]) if 0<=q['t_s']-t<=.5/config['training']['sample_fps']]
                if not candidates:continue
                q=min(candidates,key=lambda x:abs(x['t_s']-t));row['target']=q
                if any(a-annotation['sync_uncertainty_ms']/1000<=q['t_s']<=b+annotation['sync_uncertainty_ms']/1000 for a,b in annotation['contact_intervals']):continue
                if q['travel_m']>config['training']['distance_support_m']:raise ValueError('预定行程超出距离模型支持范围')
                if not q.get('path_known_before_query'):raise ValueError('距离训练需要在查询之前确定的接近路径')
                direction=q.get('path_direction')
                if not isinstance(direction,list) or len(direction)!=3 or not all(isinstance(v,(int,float)) and math.isfinite(v) for v in direction):raise ValueError('距离查询缺少三维已知路径方向')
                norm=float(np.linalg.norm(direction))
                if not .99<=norm<=1.01:raise ValueError('路径方向须为单位向量')
                row['features']+=direction+[float(q.get('travel_m',config['training']['distance_support_m']))]
            else:raise ValueError('Unknown learning task')
            samples.append(row)
    problems=[];counts={s:sum(x['split']==s for x in samples) for s in ('train','val','test')}
    for split,n in counts.items():
        if n<8:problems.append(f'{split} 只有 {n} 条有效样本，至少需要8条')
        if task=='future_contact' and {r['target'] for r in samples if r['split']==split}!={0,1}:problems.append(f'{split} 需要同时包含接触前正样本和负样本')
    if len(object_splits)<3:problems.append('至少需要3个不同物体，分别用于训练、验证和测试')
    name=time.strftime('%Y%m%d-%H%M%S')+'-'+task+'-'+uuid.uuid4().hex[:4];dest=root/'datasets'/name;dest.mkdir(parents=True)
    file=dest/'samples.jsonl';file.write_text(''.join(json.dumps(x,ensure_ascii=False,allow_nan=False)+'\n' for x in samples),encoding='utf-8')
    manifest={'dataset_id':name,'task':task,'evidence':'synthetic_software_fixture' if allow_synthetic else 'real_devices','development_only':True,'samples':len(samples),'split_counts':counts,
              'object_splits':object_splits,'sources':sources,'excluded_sessions':reasons,'train_ready':not problems,'blocking_reasons':problems,
              'input_policy':'Stereo images and same-frame vision pose; no current/future glove values or future pose as model input',
              'horizon_s':config['training']['horizon_s'],'samples_sha256':hashlib.sha256(file.read_bytes()).hexdigest(),
              'note':'Minimum software readiness is not sufficient data for a publication or performance claim.'}
    dump_json(dest/'manifest.json',manifest);return manifest
