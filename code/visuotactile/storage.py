from __future__ import annotations
import csv
import datetime as dt
import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from .protocol import McapWriter
from .skeleton_stream import append_record


def dump_json(path, obj):
    path=Path(path); temp=path.with_suffix(path.suffix+'.tmp')
    temp.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8');temp.replace(path)


def safe_id(text):
    text=str(text).strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}',text):
        raise ValueError('物体编号请使用 1–64 位英文字母、数字、横线或下划线')
    return text


class Session:
    def __init__(self, root, metadata, config, *, enforce_recording_limits=True):
        self.enforce_recording_limits = enforce_recording_limits
        if enforce_recording_limits and shutil.disk_usage(root.parent).free < config['minimum_free_bytes']:raise ValueError('录制目录可用空间低于工作站设置')
        self.id=dt.datetime.now().strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
        self.path=root/'sessions'/self.id;self.path.mkdir(parents=True)
        self.config=config;self.lock=threading.RLock();self.closed=False;self.byte_count=0
        self.start_ns=time.time_ns();self.start_mono_ns=time.monotonic_ns()
        self.metadata=dict(metadata,session_id=self.id,created_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                           start_host_ns=self.start_ns,start_mono_ns=self.start_mono_ns,status='recording',
                           evidence='real_devices',hand='Left',pose_source='vision_estimate',
                           clock_alignment='host_receipt_unverified',raw_glove_units='unknown',
                           pose_metric_world_valid=False,format_version=1)
        self.files={};self.csvs={}
        for kind in ('camera','glove'):
            self.files[kind]=open(self.path/f'{kind}.bin','xb')
            f=open(self.path/f'{kind}-reads.csv','w',newline='',encoding='utf-8');self.files[kind+'_csv']=f
            w=csv.writer(f);w.writerow(['host_ns','host_mono_ns','elapsed_s','offset','length']);self.csvs[kind]=w
        self.files['poses']=open(self.path/'hand_pose.jsonl','w',encoding='utf-8')
        # Raw capture of the bridge's OpenXR-25 wrist-local stream (visuotactile/skeleton_stream.py).
        # It is stored, not interpreted: the frame, unit, quaternion order and node names travel
        # inside each record so an offline reader cannot confuse them with the EMF block's w,x,y,z.
        self.files['skeleton']=open(self.path/'openxr_skeleton.jsonl','w',encoding='utf-8')
        self.files['events']=open(self.path/'events.jsonl','w',encoding='utf-8')
        self.mcap=McapWriter(self.path/'camera.mcap')
        dump_json(self.path/'metadata.json',self.metadata)
        dump_json(self.path/'annotations.json',{'reviewed':False,'sync_uncertainty_ms':None,'contact_intervals':[],
                                               'distance_queries':[],'note':'首次处理产生候选事件；必须结合录像复核。'})

    def raw(self, kind, data, host_ns, mono_ns):
        with self.lock:
            if self.closed:return
            f=self.files[kind];offset=f.tell();f.write(data);self.byte_count+=len(data)
            self.csvs[kind].writerow([host_ns,mono_ns,(mono_ns-self.start_mono_ns)/1e9,offset,len(data)])

    def message(self,msg,channel):
        with self.lock:
            if not self.closed:self.mcap.write(msg,channel)

    def pose(self, pose):
        with self.lock:
            if not self.closed:self.files['poses'].write(json.dumps(pose,allow_nan=False)+'\n')

    def skeleton(self, record):
        """Append one validated OpenXR-25 sample (see visuotactile/skeleton_stream.py).

        A record that does not match the schema raises in the caller, which counts and reports it
        rather than writing a line a later reader would have to guess about.
        """
        with self.lock:
            if not self.closed:append_record(self.files['skeleton'],record)

    def event(self, kind, **details):
        with self.lock:
            if self.closed:return
            now=time.monotonic_ns()
            self.files['events'].write(json.dumps(dict(kind=kind,host_ns=time.time_ns(),host_mono_ns=now,
                                                       elapsed_s=(now-self.start_mono_ns)/1e9,**details),ensure_ascii=False)+'\n')
            self.files['events'].flush()

    def limit_reason(self):
        if not self.enforce_recording_limits:
            return None
        if (time.monotonic_ns()-self.start_mono_ns)/1e9>=self.config['max_session_seconds']:return '达到本条录制时长上限'
        if self.byte_count>=self.config['max_session_bytes']:return '达到本条录制大小上限'
        if shutil.disk_usage(self.path).free<self.config['minimum_free_bytes']:return 'D盘剩余空间低于20 GiB'
        return None

    def close(self, reason, quality=None):
        with self.lock:
            if self.closed:return
            self.event('recording_stop',reason=reason)
            self.closed=True
            for f in self.files.values():f.close()
            self.mcap.close()
            self.metadata.update(status='recorded',stop_reason=reason,duration_s=(time.monotonic_ns()-self.start_mono_ns)/1e9,
                                 raw_bytes=self.byte_count,quality=quality or {})
            dump_json(self.path/'metadata.json',self.metadata)


def session_path(root, session_id):
    # The API accepts identifiers, never an arbitrary filesystem path.
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,90}',str(session_id)):raise ValueError('Invalid session identifier')
    p=(root/'sessions'/session_id).resolve()
    if p.parent!=(root/'sessions').resolve() or not p.is_dir():raise ValueError('Session not found')
    return p
