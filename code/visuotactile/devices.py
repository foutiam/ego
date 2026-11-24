# Provenance: working copy synced 2026-09-16 from D:/diss/prj/code/visuotactile/devices.py
# (uncommitted working tree there; origin of the 8776 probe prototype). This copy is the
# maintained version for the transparent-depth direction; design extensions applied here
# only (full-frame discovery scan, bidirectional/z-score criteria, rest control, fixed
# position enum). The prj checkout is read-only for this direction and is NOT modified.
from __future__ import annotations
from __future__ import annotations
import collections
import base64
import json
import queue
import re
import subprocess
import threading
import time
import uuid
from fractions import Fraction
from pathlib import Path
import av
import numpy as np
import serial
from serial.tools import list_ports
from .protocol import (VisioDecoder, GloveDecoder, decode_frame, protobuf, first,
                       string, camera_calibration, frame_transform, command,
                       PRESSURE_OFFSET, PRESSURE_SIZE)
from .pose import HandPose, jpeg_preview
from .skeleton_stream import skeleton_record
from .storage import Session, dump_json, safe_id

# Minimum number of glove frames a rest window must deliver before a baseline is saved. The glove
# runs ~190 frames/s (measured: 571 samples in a 3 s probe window on 2026-09-19), so a 3 s window
# clears this by ~3x; the gate exists so a window that was cut short saves nothing instead of
# saving a baseline from a handful of frames.
BASELINE_MIN_SAMPLES = 200
PRESSURE_GRID_SHAPE = (23, 20)


def pressure_baseline_from_samples(samples):
    """Per-cell median of a rest window's pressure grids, as a ``23x20`` uint8 array.

    ``samples`` is one flat 460-cell grid per glove frame (``decode_pressure(...).reshape(-1)``).
    The median is used for the same reason the byte baseline uses it: one frame that already
    carries a contact must not pull the zero reference up. Returns ``None`` for an empty capture,
    and raises for a grid of the wrong size rather than silently reshaping it.
    """
    if not samples:
        return None
    stack = np.stack([np.asarray(sample, dtype=np.uint8).reshape(-1) for sample in samples])
    if stack.shape[1] != PRESSURE_SIZE:
        raise ValueError(f'pressure grid must be {PRESSURE_SIZE} cells, got {stack.shape[1]}')
    return np.rint(np.median(stack, axis=0)).astype(np.uint8).reshape(PRESSURE_GRID_SHAPE)


def pressure_baseline_from_full_frame(byte_median):
    """The pressure grid out of a full-frame per-byte median (the probe baseline's own capture).

    ``byte_median`` is the median of every frame byte, so the grid sits at
    ``PRESSURE_OFFSET..PRESSURE_OFFSET+PRESSURE_SIZE``. Returns ``None`` when that array is too
    short to contain the grid, so a partial capture reports nothing instead of a wrong grid.
    """
    flat = np.asarray(byte_median, dtype=float).reshape(-1)
    if flat.size < PRESSURE_OFFSET + PRESSURE_SIZE:
        return None
    window = flat[PRESSURE_OFFSET:PRESSURE_OFFSET + PRESSURE_SIZE]
    return np.rint(window).astype(np.uint8).reshape(PRESSURE_GRID_SHAPE)


def pressure_baseline_summary(grid):
    """What a resting grid looks like in words, for notices and reports (raw counts, no units)."""
    if grid is None:
        return None
    values = np.asarray(grid)
    return {'shape': [int(v) for v in values.shape], 'max': int(values.max()),
            'mean': round(float(values.mean()), 2),
            'cells_at_least_2': int((values >= 2).sum()),
            'cells_at_least_3': int((values >= 3).sum()),
            'note': ('逐格静置中位数（原始计数，无单位、无标定曲线）；不是压力，也不是接触判定')}



class VideoDecoder:
    """HEVC packet decoder. mode 'sw' uses the software libhevc decoder;
    mode 'nvdec' uses NVIDIA hevc_cuvid. There is no silent fallback: a
    requested NVDEC decoder that cannot be created raises here, and the video
    thread stops with a loud device message instead of quietly decoding on
    another path. Raw stream recording is unaffected either way (the reader
    thread writes bytes before decode). Reference frames are never dropped:
    packets are always fed in order; staleness stays a display-layer policy."""
    def __init__(self, mode='sw'):
        self.mode='sw' if mode not in ('sw','nvdec') else mode
        if mode=='nvdec':
            self.codec=av.CodecContext.create('hevc_cuvid','r');self.codec.thread_count=1
        else:
            self.codec=av.CodecContext.create('hevc','r');self.codec.thread_count=2
        self.ready=False;self.errors=0
    def decode(self, data, device_ns):
        if not self.ready:
            # Await the capture's own codec parameter set; never borrow old video references.
            import re
            found=next((m.start() for m in re.finditer(b'\x00\x00\x00?\x01',data)
                        if m.end()<len(data) and (data[m.end()]>>1)&63==32),None)
            if found is None:return []
            data=data[found:];self.ready=True
        packet=av.Packet(data);packet.pts=device_ns;packet.dts=device_ns;packet.time_base=Fraction(1,1_000_000_000)
        try:return self.codec.decode(packet)
        except av.error.FFmpegError:
            self.errors+=1;return []


class Devices:
    def __init__(self, config):
        self.config=config;self.root=Path(config['output_root']);self.root.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock();self.stop=threading.Event();self.threads=[];self.session=None
        self.state={'camera':{'connected':False,'message':'等待连接双目相机'},
                    'glove':{'connected':False,'message':'等待连接左手手套'},
                    'pose':{'detected':False,'message':'等待左手进入画面'},'notice':'','mode':'live'}
        self.latest=[None,None];self.previews=[None,None];self.video_queues=[queue.Queue(90),queue.Queue(90)]
        self.calibration={};self.identity={};self.channels={};self.ports={};self.baseline=None;self.baseline_until=0;self.baseline_samples=[]
        # Resting zero reference for the 23x20 pressure grid. It is a *different* quantity from the
        # byte baseline above (which covers the configured contact bytes only), so it is captured
        # in the same rest window but kept separately: the grid is what the page's 3D taxel layer
        # thresholds, and without a zero reference every worn-glove offset reads as contact.
        self.pressure_baseline=None;self.pressure_baseline_samples=[];self.pressure_baseline_source=None
        # Device-reported rigid transforms (topic -> FrameTransform). The camera
        # publishes `/ego/camera/1/extrinsics` (parent cam0, child cam1) and
        # `/ego/imu/0/extrinsics`; until 2026-09-21 these messages were received,
        # recorded and then dropped here, so the stereo baseline the device states
        # never reached the calibration page.
        self.transforms={}
        self.contact=False;self.contact_count=0;self.touch_history=collections.deque(maxlen=600)
        # Monotonic per-sample sequence numbers aligned with touch_history, so
        # /api/live/touch can drain samples newer than a client cursor without
        # dropping short presses between polls.
        self.touch_seq=collections.deque(maxlen=600);self.touch_counter=0
        self.last_preview=[0.,0.];self.last_glove_ui=0.;self.last_pose_ns=-1
        # Rolling encode timestamps per camera for honest preview-fps readout.
        self.preview_times_lock=threading.Lock()
        self.preview_times=[collections.deque(maxlen=64),collections.deque(maxlen=64)]
        self.last_pose_preview=0.
        self.replay_stop=threading.Event();self.replay_thread=None
        self.clock_command_id=None
        # Operator-driven channel localization probe. Everything starts
        # unconfirmed; a confirmed mapping only exists after saved trials.
        self.probe_root=self.root/'probe';self.probe_root.mkdir(parents=True,exist_ok=True)
        self.probe=None;self.probe_lock=threading.Lock()
        self.seq=None;self.seq_thread=None;self.seq_stop=threading.Event()
        self.mcal=None;self.mcal_thread=None;self.mcal_stop=threading.Event()
        self.mcal_last=None
        self.retarget_process=None;self.retarget_thread=None;self.retarget_lock=threading.Lock();self.retarget_timestamp=0.0
        try:
            p=self.probe_root/'motion-calibration.json'
            if p.exists():self.mcal_last=json.loads(p.read_text(encoding='utf-8'))
        except (OSError,ValueError):self.mcal_last=None
        self.probe_baseline=None;self.probe_baseline_meta=None;self.probe_frame_baseline=None
        self.probe_map=self._load_probe_map()
        self.contact_sites=self._parse_contact_sites()

    def launch(self):
        self._start_retarget_bridge()
        for target,args in [(self._reader,('camera',)),(self._reader,('glove',)),(self._video,(0,)),(self._video,(1,)),(self._pose,())]:
            t=threading.Thread(target=target,args=args,daemon=True);t.start();self.threads.append(t)

    def _start_retarget_bridge(self):
        project=Path(__file__).resolve().parents[2]
        python=project/'src/matrixhand/qnbot-sim-console-windows-x64-portable/resources/runtime/python/python.exe'
        bridge=project/'code/ego_capture/qnbot_retarget_bridge.py'
        python = Path(self.config.get('qnbot_runtime_python') or python)
        if not python.is_file() or not bridge.is_file():
            self.state['glove']['retargeting_status']='SDK runtime not found'
            return
        try:
            log=self.root/'logs';log.mkdir(parents=True,exist_ok=True)
            stderr=(log/'qnbot-retarget-bridge.err.log').open('a',encoding='utf-8')
            self.retarget_process=subprocess.Popen(
                [str(python),str(bridge),str(self.root)],
                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=stderr,
                # errors='replace': the vendor runtime encodes its stdout with the
                # Windows ANSI codepage (gbk on this machine), so a single non-ASCII
                # byte from the bridge used to raise UnicodeDecodeError inside
                # _read_retarget_output and kill that thread, silently freezing every
                # later joint_command/skeleton_pose/calibration_state. The bridge now
                # emits ASCII-only JSON as well; this keeps a future stray byte from
                # taking the whole channel down.
                text=True,encoding='utf-8',errors='replace',bufsize=1)
            self.retarget_thread=threading.Thread(target=self._read_retarget_output,
                                                   name='qnbot-retarget-output',daemon=True)
            self.retarget_thread.start()
            self.state['glove']['retargeting_status']='starting'
        except OSError as exc:
            self.state['glove']['retargeting_status']=f'SDK unavailable: {exc}'

    def _read_retarget_output(self):
        process=self.retarget_process
        if process is None or process.stdout is None:return
        for line in process.stdout:
            try:message=json.loads(line)
            except (TypeError,ValueError):continue
            with self.lock:
                if message.get('kind')=='ready':
                    # bridge_build names the bridge source this live process is running.
                    # The page is served from disk on every request but the bridge keeps
                    # executing the module it imported, so after a code change this stamp
                    # is what shows whether the station really picked the change up
                    # (2026-09-21: a finished fix looked as if it had never been applied).
                    self.state['glove'].update(retargeting_status='ready',skeleton_api=bool(message.get('skeleton_api')),
                                               skeleton_error=message.get('skeleton_error'),
                                               bridge_build=message.get('build'))
                elif message.get('kind')=='joint_command':
                    self.state['glove']['joint_command']=message.get('value')
                elif message.get('kind')=='skeleton_pose':
                    self.state['glove']['skeleton_pose']=message.get('value')
                    self._record_skeleton(message.get('value'))
                elif message.get('kind')=='calibration_state':
                    self.state['glove']['calibration_state']=message
                elif message.get('kind')=='error':
                    self.state['glove']['retargeting_status']='error: '+str(message.get('message',''))
        with self.lock:
            if not str(self.state['glove'].get('retargeting_status','')).startswith('error'):
                self.state['glove']['retargeting_status']=f'bridge exited ({process.poll()})'

    def _record_skeleton(self, message):
        """Append one bridge skeleton sample to the running session, if there is one.

        Raw capture only: this stores what the bridge already emits so the offline tracking
        experiment can pair it with the recorded stereo frames. Nothing here estimates a pose or
        writes a camera-frame coordinate, and no field of the record is interpreted - the frame
        name, unit and quaternion order are copied into it (visuotactile/skeleton_stream.py).

        A malformed sample is counted and reported in the live state instead of raising into this
        reader thread, which also carries joint_command and calibration_state: one bad sample must
        not take the whole retarget channel down.
        """
        if not isinstance(message, dict):
            return
        try:
            record = skeleton_record(
                message, host_ns=time.time_ns(), host_mono_ns=time.monotonic_ns(),
                session_id=self.session.id if self.session else None,
                bridge_build=self.state['glove'].get('bridge_build'))
        except ValueError as exc:
            with self.lock:
                glove = self.state['glove']
                glove['skeleton_record_error'] = str(exc)
                glove['skeleton_record_rejected'] = int(glove.get('skeleton_record_rejected', 0)) + 1
            return
        with self.lock:
            glove = self.state['glove']
            glove['skeleton_recorded'] = int(glove.get('skeleton_recorded', 0)) + 1
            glove['skeleton_record_error'] = None
            if self.session:
                self.session.skeleton(record)

    def _push_retarget(self, emf, timestamp):
        process=self.retarget_process
        if process is None or process.stdin is None or process.poll() is not None:return
        fingers=emf.get('fingers',[])
        if len(fingers)!=5 or not all(f.get('valid') for f in fingers):return
        names=('thumb','index','middle','ring','pinky')
        payload={}
        for name,finger in zip(names,fingers):
            p=finger['position_mm'];q=finger['quaternion']
            payload[name]={'position':[p['x']/1000,p['y']/1000,p['z']/1000],
                           'quaternion_wxyz':[q['w'],q['x'],q['y'],q['z']]}
        try:
            with self.retarget_lock:
                timestamp=max(float(timestamp),self.retarget_timestamp+1e-6)
                self.retarget_timestamp=timestamp
                process.stdin.write(json.dumps({'kind':'frame','timestamp_sec':timestamp,
                                                'fingers':payload},separators=(',',':'))+'\n')
                process.stdin.flush()
        except (BrokenPipeError,OSError):
            if not str(self.state['glove'].get('retargeting_status','')).startswith('error'):
                self.state['glove']['retargeting_status']='stopped'

    def _bridge_command(self, kind):
        p=self.retarget_process
        if p is None or p.stdin is None or p.poll() is not None: raise ValueError('external bridge 未运行')
        with self.retarget_lock:
            p.stdin.write(json.dumps({'kind':kind})+'\n');p.stdin.flush()

    def start_qnbot_calibration(self):
        self._bridge_command('calibration_start');return {'ok':True}

    def capture_qnbot_calibration(self):
        self._bridge_command('calibration_capture');return {'ok':True}

    def cancel_qnbot_calibration(self):
        self._bridge_command('calibration_cancel');return {'ok':True}

    def reuse_qnbot_calibration(self):
        """Enable the official output from the record already on disk.

        No measurement happens: the bridge checks the stored record with the SDK's
        own matching rules and only then starts the workflow, so a station whose
        calibration already exists can render the hand pose without re-calibrating.
        """
        self._bridge_command('calibration_reuse');return {'ok':True}

    def available_ports(self):
        return [{'port':p.device,'description':p.description,'vid':p.vid,'pid':p.pid} for p in list_ports.comports()]

    def _find_port(self, kind):
        override=self.config.get(kind+'_port_override')
        ports=list(list_ports.comports())
        if override:return next((p for p in ports if p.device==override),None)
        found=[p for p in ports if p.vid==self.config[kind+'_vid'] and p.pid==self.config[kind+'_pid']]
        if len(found)>1:raise ValueError('发现多个同类设备，请在高级设置中指定串口')
        return found[0] if found else None

    def _reader(self, kind):
        while not self.stop.is_set():
            if self.state['mode']!='live':self.stop.wait(.5);continue
            port=None
            try:
                port=self._find_port(kind)
                if not port:
                    with self.lock:self.state[kind].update(connected=False,message='设备未连接')
                    self.stop.wait(2);continue
                parser=VisioDecoder() if kind=='camera' else GloveDecoder()
                link=serial.Serial(port=None,baudrate=115200,timeout=.03,write_timeout=.5)
                link.dtr=kind=='camera';link.rts=kind=='camera';link.port=port.device;link.open()
                self.ports[kind]=link
                with self.lock:
                    # VID 0483 / PID 5739 only identifies a generic STM32 CDC device:
                    # brand, model, channel table and IMU presence stay unconfirmed.
                    self.state[kind].update(connected=True,port=port.device,vid=port.vid,pid=port.pid,
                                            description=port.description,model_confirmed=False,
                                            message='已连接，等待数据',frames=0,last_data_mono_ns=0)
                if kind=='camera':
                    for q in self.video_queues:
                        while not q.empty():
                            try:q.get_nowait()
                            except queue.Empty:break
                        q.put(None)
                first_received=None;total=0;clock_stage=0;command_base=int(time.time())%2_000_000_000
                while not self.stop.is_set() and self.state['mode']=='live':
                    data=link.read(min(max(link.in_waiting,1),65536))
                    if not data:continue
                    host=time.time_ns();mono=time.monotonic_ns();first_received=first_received or mono;total+=len(data)
                    with self.lock:
                        if self.session:self.session.raw(kind,data,host,mono)
                        self.state[kind].update(last_data_mono_ns=mono,bytes=total,message='正在接收')
                    self.consume(kind,parser,data,host,mono)
                    if kind=='camera' and parser.identity.get('name'):
                        elapsed=(mono-first_received)/1e9
                        if clock_stage==0 and elapsed>1:
                            link.write(command(parser.identity['name'],19,command_base));clock_stage=1
                        elif clock_stage==1 and elapsed>3:
                            self.clock_command_id=command_base+1
                            link.write(command(parser.identity['name'],21,self.clock_command_id));clock_stage=2
                            with self.lock:self.state['camera']['clock_status']='校时命令已发送，等待回执'
                    with self.lock:
                        self.state[kind]['frames']=parser.good;self.state[kind]['invalid_or_partial']=parser.bad
                        self.state[kind]['rate_hz']=round(parser.good/max((mono-first_received)/1e9,.1),1)
                        if self.session:
                            why=self.session.limit_reason()
                            if why:self.stop_recording(why)
            except Exception as exc:
                with self.lock:
                    self.state[kind].update(connected=False,message=f'连接中断：{exc}')
                    if self.session:self.stop_recording(f'{kind}连接中断')
                self.stop.wait(2)
            finally:
                link=self.ports.pop(kind,None)
                if link:
                    try:link.close()
                    except Exception:pass

    def consume(self, kind, parser, data, host, mono):
        if kind=='glove':
            for frame,offset in parser.feed(data):self._glove_frame(frame,host,mono)
            return
        for msg in parser.feed(data,host,mono):
            if msg.stream_id==1:
                expected=self.config.get('camera_serial')
                if expected and parser.identity.get('serial') and parser.identity['serial']!=expected:
                    raise ValueError('发现的相机序列号与配置不一致，请核对连接设备')
                with self.lock:
                    self.identity=parser.identity
                    self.channels=parser.channels.copy()
                    self.state['camera']['identity']=parser.identity
                    if self.session:dump_json(self.session.path/'device.json',parser.identity)
            c=parser.channels.get(msg.stream_id)
            if not c:continue
            with self.lock:
                if self.session:self.session.message(msg,c)
            if c['schema']=='foxglove.CameraCalibration':
                try:self.calibration[c['topic']]=camera_calibration(msg.payload)
                except (ValueError,TypeError):pass
            if c['schema']=='foxglove.FrameTransform':
                # Device-reported relative pose (cam0<-cam1, cam0<-imu0). Kept as
                # received; interpretation and verification status live in
                # ego_capture.camera_calibration.
                try:self.transforms[c['topic']]=frame_transform(msg.payload)
                except (ValueError,TypeError):pass
            if c['schema'].endswith('CommandResult'):
                p=protobuf(msg.payload)
                if first(p,1)==self.clock_command_id:
                    with self.lock:self.state['camera']['clock_status']='日期校时成功；设备间同步仍需复核' if first(p,2) else '相机拒绝校时'
            if c['schema']=='foxglove.CompressedVideo':
                p=protobuf(msg.payload);topic=c['topic'];cam=0 if topic.endswith('/0') else 1
                try:self.video_queues[cam].put_nowait((first(p,3,b''),msg))
                except queue.Full:
                    with self.lock:self.state['camera']['preview_queue_drops']=self.state['camera'].get('preview_queue_drops',0)+1
            elif c['topic'].endswith('/quat'):
                p=protobuf(msg.payload)
                import struct
                q=[struct.unpack('<d',first(p,i,b'\0'*8))[0] for i in range(1,5)]
                with self.lock:self.state['camera']['imu_quaternion']=q

    def _video(self, cam):
        try:
            decoder=VideoDecoder(self.config.get('video_decoder','sw'))
        except Exception as exc:
            # Loud stop, never a silent software fallback: raw recording keeps
            # working, but no preview is shown until the decoder is fixed.
            with self.lock:
                self.state['camera'].update(connected=self.state['camera'].get('connected',False),
                    message=f'视频解码器({self.config.get("video_decoder","sw")})启动失败：{exc}')
            return
        while not self.stop.is_set():
            try:item=self.video_queues[cam].get(timeout=.5)
            except queue.Empty:continue
            if item is None:decoder=VideoDecoder(self.config.get('video_decoder','sw'));continue
            data,msg=item
            for frame in decoder.decode(data,msg.device_ns):
                rgb=frame.to_ndarray(format='rgb24');now=time.monotonic()
                entry={'rgb':rgb,'device_ns':frame.pts if frame.pts is not None else msg.device_ns,
                       'host_ns':msg.host_ns,'host_mono_ns':msg.host_mono_ns,'sequence':msg.sequence,'arrived':now}
                with self.lock:
                    self.latest[cam]=entry
                    self.state['camera'][f'cam{cam}_shape']=[rgb.shape[1],rgb.shape[0]]
                due=now-self.last_preview[cam]>1/self.config['preview_fps']
                if cam==0 and self.state['pose'].get('model_ready'):
                    # The pose thread publishes overlay previews at pose_fps;
                    # this thread only backfills a plain preview when that has
                    # gone stale, so cam0 never freezes if pose stalls.
                    due=due and now-self.last_pose_preview>2.0
                if due:
                    encoded=jpeg_preview(rgb)
                    self._note_preview(cam)
                    with self.lock:self.previews[cam]=encoded;self.state['camera'][f'cam{cam}_shape']=[rgb.shape[1],rgb.shape[0]]
                    self.last_preview[cam]=now

    def _pose(self):
        detector=None
        try:
            detector=HandPose(self.config['hand'],
                              min_detection=self.config.get('pose_min_detection_confidence',.5),
                              fallback_min_detection=self.config.get('pose_fallback_min_detection_confidence',.25),
                              roi_retry=self.config.get('pose_roi_retry',True))
            with self.lock:self.state['pose']['model_ready']=True
            while not self.stop.wait(1/self.config['pose_fps']):
                with self.lock:entry=self.latest[0]
                if not entry or entry['device_ns']==self.last_pose_ns or time.monotonic()-entry['arrived']>2:continue
                pose=detector.infer(entry['rgb'],entry['device_ns'],entry['host_ns'],entry['host_mono_ns'])
                self.last_pose_ns=entry['device_ns'];pose['model_ready']=True
                if pose['detected']:
                    pose['message'] = f"{(pose.get('hand') or '手').upper()} 视觉姿态已识别"
                    if pose.get('gloved_fallback'):
                        pose['message'] += f"（经{ 'ROI' if pose.get('detection_pass')=='roi-retry' else '低阈值' }复检；分类置信度仅供参考）"
                else:
                    pose['message'] = ('未检测到手部骨架（已尝试ROI与低阈值复检）；戴手套会显著降低MediaPipe稳定性，'
                                       '可调整照明、视角、距离，或短暂露出指尖帮助定位')
                preview=jpeg_preview(entry['rgb'],pose)
                self.last_pose_preview=time.monotonic();self._note_preview(0)
                with self.lock:
                    self.state['pose']=pose;self.previews[0]=preview
                    if self.session:self.session.pose(pose)
        except Exception as exc:
            with self.lock:self.state['pose']={'detected':False,'model_ready':False,'message':f'手姿模型未启动：{exc}'}
        finally:
            if detector:detector.close()

    def _parse_contact_sites(self):
        """Parse contact_sites config into value-index views over the flat
        contact_byte_offsets read. Sites group bytes measured to respond
        together (one glove sensor site = 1-4 bytes). role='bend' sites are
        kept for display but excluded from contact scoring, because they fire
        on finger bending, not on object contact. Missing/unknown site bytes
        raise at startup: a silently empty site would disable a finger."""
        offsets=list(self.config['contact_byte_offsets'])
        index={b:i for i,b in enumerate(offsets)}
        sites=[]
        for s in self.config.get('contact_sites') or []:
            missing=[b for b in s['bytes'] if b not in index]
            if missing:raise ValueError(f"contact_sites 位点 {s.get('id')} 引用了未在 contact_byte_offsets 中的字节: {missing}")
            sites.append({'id':s['id'],'finger':s.get('finger',''),'role':s.get('role','touch'),
                          'bytes':list(s['bytes']),'idx':[index[b] for b in s['bytes']]})
        if not sites:
            # Legacy config without contact_sites: one site over all channels
            # reproduces the old median-over-all-channels score.
            sites=[{'id':'all','finger':'','role':'touch','bytes':list(offsets),'idx':list(range(len(offsets)))}]
        return sites

    def _glove_frame(self, frame, host, mono):
        decoded=decode_frame(frame)
        emf=decoded['emf']
        pressure=decoded['pressure']
        self._push_retarget(emf, time.time_ns()/1e9)
        values=np.frombuffer(frame,dtype=np.uint8)[self.config['contact_byte_offsets']].astype(float)
        with self.probe_lock:
            probe=self.probe
            if probe and time.monotonic()<=probe['until']:
                probe['samples'].append((time.monotonic_ns(),values))
                if probe['want_raw'] and len(probe['raw_frames'])<probe['raw_limit']:
                    probe['raw_frames'].append(bytes(frame))
        now=time.monotonic()
        with self.lock:
            if self.baseline_until:
                self.baseline_samples.append(values)
                # The grid rides in the same window: one operator action ("keep the glove still for
                # 3 s") has to produce every zero reference the page uses, or the pressure layer
                # keeps a zero it never measured.
                self.pressure_baseline_samples.append(pressure.reshape(-1))
                if now>=self.baseline_until:
                    if len(self.baseline_samples)>=BASELINE_MIN_SAMPLES:
                        self.baseline=np.median(self.baseline_samples,axis=0)
                        self.pressure_baseline=pressure_baseline_from_samples(self.pressure_baseline_samples)
                        self.pressure_baseline_source='rest_window_median'
                        summary=pressure_baseline_summary(self.pressure_baseline) or {}
                        # The flag is written here, not only on the next throttled UI tick: the page's
                        # badge must not keep saying "no baseline" for another 50 ms after the save.
                        self.state['glove']['pressure_baseline_ready']=self.pressure_baseline is not None
                        self.state['notice']=('触觉基线已保存（含 23×20 压感静置基线：最大 %s、≥3 的格 %s）；'
                                              '这是相对响应，尚未换算压力'
                                              % (summary.get('max'), summary.get('cells_at_least_3')))
                    else:self.state['notice']='数据不足，基线未保存'
                    self.baseline_until=0;self.baseline_samples=[];self.pressure_baseline_samples=[]
            # Site-level scoring (offsets-v3): each configured site is scored by
            # the median positive delta of its bytes, so one pressed site can
            # outvote the many idle channels. Overall score is the max over
            # touch-role sites; bend-role sites are displayed but never scored
            # into contact (they fire on finger bending, see contact_sites_note).
            delta=None if self.baseline is None else values-self.baseline
            score=None;site_scores={}
            if delta is not None:
                for site in self.contact_sites:
                    site_scores[site['id']]=round(float(np.median(np.maximum(delta[site['idx']],0.0))),1)
                touch_vals=[site_scores[s['id']] for s in self.contact_sites if s['role']=='touch']
                score=max(touch_vals) if touch_vals else float(np.median(np.maximum(delta,0)))
            old=self.contact
            if score is not None:
                desired=score>self.config['contact_off_delta'] if self.contact else score>=self.config['contact_on_delta']
                self.contact_count=self.contact_count+1 if desired!=self.contact else 0
                if self.contact_count>=3:self.contact=desired;self.contact_count=0
            if self.session and self.contact!=old:self.session.event('contact_candidate_on' if self.contact else 'contact_candidate_off',score=score)
            if now-self.last_glove_ui>.05:
                self.touch_counter+=1
                self.touch_history.append([mono,score]);self.touch_seq.append(self.touch_counter)
                labels=list(self.config.get('contact_channel_map',[]))
                finger_scores={}
                for s in self.contact_sites:
                    if s['role']=='touch':
                        v=site_scores.get(s['id'])
                        if v is not None and v>finger_scores.get(s['finger'],-1):finger_scores[s['finger']]=v
                self.state['glove'].update(values=values.tolist(),offsets=self.config['contact_byte_offsets'],
                                           channel_labels=labels,
                                           channel_map_source=self.config.get('contact_channel_map_source','unconfirmed'),
                                           site_scores=site_scores,finger_scores=finger_scores,
                                           contact_candidate=self.contact,
                                           score=score,baseline_ready=self.baseline is not None,calibrating=bool(self.baseline_until),
                                           pressure_baseline_ready=self.pressure_baseline is not None,
                                           emf=emf,pressure=pressure.tolist())
                self.last_glove_ui=now

    def calibration_record(self):
        """Per-session provenance: reported calibration plus the honest glove
        identity and channel-mapping status (nothing here is hardware-confirmed)."""
        s=self.state['glove']
        return {'reported':self.calibration,'reported_transforms':self.transforms,
                'independently_verified':False,
                'touch_offsets':self.config['contact_byte_offsets'],
                'touch_sites':[{'id':st['id'],'finger':st['finger'],'role':st['role'],'bytes':st['bytes']}
                               for st in self.contact_sites],
                'touch_sites_source':self.config.get('contact_sites_source','unconfirmed'),
                'touch_scoring':'per_site_median_positive_delta_max_over_touch_sites (offsets-v3)',
                'touch_channel_labels':list(self.config.get('contact_channel_map',[])),
                'touch_channel_map_source':self.config.get('contact_channel_map_source','unconfirmed'),
                'touch_baseline':None if self.baseline is None else self.baseline.tolist(),
                # The 23x20 grid's own resting zero, recorded with the session: the 3D taxel layer
                # thresholds a *residual*, so a session without this grid cannot be re-scored as
                # contact later without inventing the zero it used.
                'touch_pressure_baseline':None if self.pressure_baseline is None else
                dict({'source':self.pressure_baseline_source,
                      'grid':[[int(v) for v in row] for row in self.pressure_baseline]},
                     **(pressure_baseline_summary(self.pressure_baseline) or {})),
                'glove_identity':{'vid':s.get('vid'),'pid':s.get('pid'),'port':s.get('port'),
                                  'description':s.get('description')},
                'glove_model_confirmed':False,
                'glove_imu_present':'unconfirmed',
                'touch_channel_map_version':self.probe_map.get('version',0),
                'touch_channel_labels_source':self.probe_map.get('source','unconfirmed'),
                'touch_probe_statuses':dict(self.probe_map.get('statuses',{}))}

    def calibrate_touch(self):
        with self.lock:
            if not self.state['glove']['connected']:raise ValueError('请先连接左手手套')
            self.baseline_samples=[];self.pressure_baseline_samples=[];self.baseline_until=time.monotonic()+3;self.contact=False
            self.state['notice']='开始记录基线：请保持手套不接触物体，静置3秒'

    def start_recording(self, metadata, *, enforce_recording_limits=True):
        with self.lock:
            if self.session:raise ValueError('已有录制正在进行')
            if self.state['mode']!='live':raise ValueError('回放模式不能当作真实设备采集')
            for kind in ('camera','glove'):
                s=self.state[kind]
                if not s['connected'] or (time.monotonic_ns()-s.get('last_data_mono_ns',0))/1e9>3:raise ValueError(f'{kind}未连接或数据已停止')
                if s.get('frames',0)<1:raise ValueError(f'{kind}尚未收到完整有效数据帧')
            if any(not entry or time.monotonic()-entry['arrived']>3 for entry in self.latest):raise ValueError('请等待两路相机都出现实时画面后再录制')
            metadata['object_id']=safe_id(metadata.get('object_id',''))
            metadata['scene_id']=safe_id(metadata.get('scene_id','scene_001'))
            split=metadata.get('split','train')
            if split not in ('train','val','test'):raise ValueError('Invalid split')
            # One physical object must keep the same split across sessions.
            for p in (self.root/'sessions').glob('*/metadata.json'):
                old=json.loads(p.read_text('utf-8'))
                if old.get('object_id')==metadata['object_id'] and old.get('split')!=split:raise ValueError('该物体已属于另一数据划分，不能跨训练/验证/测试集')
            self.session=Session(self.root,metadata,self.config,
                                 enforce_recording_limits=enforce_recording_limits)
            dump_json(self.session.path/'device.json',self.identity)
            dump_json(self.session.path/'channels.json',{str(k):dict(c,schema_data=base64.b64encode(c['schema_data']).decode('ascii')) for k,c in self.channels.items()})
            dump_json(self.session.path/'calibration.json',self.calibration_record())
            self.session.event('recording_start');self.state['notice']='录制已开始；视频和手套原始数据保存在电脑本地'
            return self.session.id

    def stop_recording(self, reason='用户停止'):
        with self.lock:
            if not self.session:return None
            s=self.session;self.session=None;s.close(reason,{'camera':self.state['camera'],'glove':self.state['glove']})
            self.state['notice']='录制已保存：'+s.id;return s.id

    def _note_preview(self,cam):
        with self.preview_times_lock:
            self.preview_times[cam].append(time.monotonic())

    def preview_hz(self):
        """Measured JPEG preview encode rate per camera over a ~5 s window
        (honest display fps; not the camera frame rate)."""
        now=time.monotonic();out=[]
        for cam in (0,1):
            with self.preview_times_lock:
                ts=[t for t in self.preview_times[cam] if now-t<=5.0]
            if len(ts)>1:
                span=now-ts[0]
                out.append(round((len(ts)-1)/span,1) if span>0 else 0.0)
            else:
                out.append(0.0)
        return out

    def drain_touch(self,cursor=0):
        """Drain scored touch samples with sequence number > cursor.

        Server samples ~20 Hz into a 600-sample ring; the UI pulls at its own
        rate, so short presses are not lost to slow or bursty polling.
        cursor=0 (or an expired cursor) returns the whole ring window with
        window_reset=True. Includes the latest glove display values so the
        sensor bars and touch badge update at tactile rate, not /api/state rate."""
        with self.lock:
            seqs=list(self.touch_seq);samples=list(self.touch_history)
        out=[];newest=cursor
        for seq,(mono,score) in zip(seqs,samples):
            if seq>cursor:
                out.append([seq,mono,score])
                newest=max(newest,seq)
        g=self.state['glove']
        return {'cursor':newest,'samples':out,
                'window_reset':bool(cursor>0 and seqs and seqs[0]>cursor),
                'glove_stale':bool(g.get('stale',False)),
                'glove':{'values':g.get('values'),'offsets':g.get('offsets'),
                         'score':g.get('score'),
                         'contact_candidate':g.get('contact_candidate'),
                         'baseline_ready':g.get('baseline_ready'),
                         'pressure_baseline_ready':g.get('pressure_baseline_ready'),
                         'calibrating':g.get('calibrating'),
                         'finger_scores':g.get('finger_scores'),
                         'emf':g.get('emf'),
                         'pressure':g.get('pressure'),
                         'joint_command':g.get('joint_command'),
                         'retargeting_status':g.get('retargeting_status'),
                         'skeleton_api':g.get('skeleton_api'),
                         'skeleton_error':g.get('skeleton_error'),
                         'calibration_state':g.get('calibration_state'),
                         'skeleton_pose':g.get('skeleton_pose')},
                'server_mono_ns':time.monotonic_ns()}

    def snapshot(self):
        with self.lock:
            out=json.loads(json.dumps(self.state));out['recording']=None
            if self.session:out['recording']={'session_id':self.session.id,'seconds':(time.monotonic_ns()-self.session.start_mono_ns)/1e9,'bytes':self.session.byte_count}
            out['touch_history']=list(self.touch_history)[-200:];out['server_mono_ns']=time.monotonic_ns()
            # The resting grid is the page's zero reference for the 3D taxel layer, so it travels
            # with the state (2 s poll) instead of riding the ~20 Hz touch poll, and it is None --
            # not zeros -- until the operator actually captured it: an absent zero must stay absent,
            # or the layer would draw the whole worn-glove offset as contact.
            out['glove']['pressure_baseline_ready']=self.pressure_baseline is not None
            out['glove']['pressure_baseline_source']=self.pressure_baseline_source
            out['glove']['pressure_baseline']=(None if self.pressure_baseline is None else
                                               [[int(v) for v in row] for row in self.pressure_baseline])
            out['glove']['pressure_baseline_summary']=pressure_baseline_summary(self.pressure_baseline)
            out['probe']={'baseline_ready':self.probe_baseline is not None,
                          'version':self.probe_map.get('version',0),
                          'source':self.probe_map.get('source','unconfirmed'),
                          'statuses':dict(self.probe_map.get('statuses',{})),
                          'capturing':self.probe is not None,
                          'sequence':self._probe_sequence_summary(),
                          'motion_calibration_running':self._motion_calibration_running()}
            for kind in ('camera','glove'):
                out[kind]['stale']=(time.monotonic_ns()-out[kind].get('last_data_mono_ns',0))/1e9>3
            if not self.latest[0] or time.monotonic()-self.latest[0]['arrived']>2:
                out['pose']['detected']=False;out['pose']['message']='等待左手进入实时画面'
            return out

    def check_recording_limits(self):
        # Independent of incoming bytes, so a silent USB stall cannot leave a recording open.
        with self.lock:
            if not self.session:return
            reason=self.session.limit_reason()
            if not reason:
                for kind in ('camera','glove'):
                    if (time.monotonic_ns()-self.state[kind].get('last_data_mono_ns',0))/1e9>5:
                        reason=f'{kind}数据停止超过5秒';break
            if reason:self.stop_recording(reason)

    def close(self):
        self.stop.set();self.replay_stop.set();self.stop_recording('查看器关闭')
        if self.retarget_process is not None:
            try:
                if self.retarget_process.stdin:
                    self.retarget_process.stdin.write('{"kind":"stop"}\n');self.retarget_process.stdin.flush()
                self.retarget_process.wait(timeout=3)
            except (BrokenPipeError,OSError,subprocess.TimeoutExpired):
                self.retarget_process.kill()
        for t in self.threads:t.join(timeout=2)

    # ---- operator-driven channel localization probe -------------------------
    # Goal: map the 12 extracted byte channels to glove positions using real
    # operator presses. Nothing here is hardware-confirmed by itself; the
    # response matrix and candidates are suggestions until the operator saves
    # an explicit mapping. Force (Newton) calibration is never claimed here.
    DEFAULT_STATUSES={'baseline_zeroing':'not_done_relative_only','position_mapping':'unconfirmed',
                      'contact_detection':'threshold_only_no_force_units','force_calibration':'not_performed_newton'}
    # Canonical operator position codes. A fixed enum keeps trials comparable and
    # prevents duplicate free-text labels like 食指尖 vs index_tip pointing at the
    # same physical spot with different names.
    PROBE_POSITIONS=['thumb_tip','index_tip','middle_tip','ring_tip','pinky_tip',
                     'thumb_pad','index_pad','middle_pad','ring_pad','pinky_pad',
                     'palm_center','palm_heel','hand_neutral','hand_rotate','table_slide']
    PROBE_POSITIONS += ['hand_relaxed','hand_fist','fingers_together']

    def _load_probe_map(self):
        p=self.probe_root/'channel_map.json'
        if p.exists():
            try:
                data=json.loads(p.read_text('utf-8'))
                if isinstance(data.get('labels'),list) and len(data['labels'])==len(self.config['contact_byte_offsets']):
                    self.config['contact_channel_map']=list(data['labels'])
                    self.config['contact_channel_map_source']=data.get('source','unconfirmed')
                    data.setdefault('statuses',dict(self.DEFAULT_STATUSES));return data
            except (OSError,ValueError):pass
        return {'version':0,'labels':list(self.config.get('contact_channel_map',['unknown']*len(self.config['contact_byte_offsets']))),
                'source':self.config.get('contact_channel_map_source','unconfirmed'),
                'note':'','updated_at':None,'statuses':dict(self.DEFAULT_STATUSES)}

    def _probe_capture(self, seconds, want_raw=False, raw_limit=40):
        s=self.state['glove']
        if not s.get('connected') or (time.monotonic_ns()-s.get('last_data_mono_ns',0))/1e9>3:
            raise ValueError('左手手套未连接或数据已停止')
        if self.state['mode']!='live':raise ValueError('回放模式没有真实触觉数据，不能做定位测试')
        if not 1<=seconds<=15:raise ValueError('采集时长须在1–15秒之间')
        with self.probe_lock:
            if self.probe:raise ValueError('已有一次采集正在进行，请等它结束')
            self.probe={'until':time.monotonic()+seconds,'samples':[],
                        'want_raw':want_raw,'raw_frames':[],'raw_limit':raw_limit if want_raw else 0}
        while time.monotonic()<self.probe['until'] and self.state['glove'].get('connected'):
            time.sleep(.05)
        with self.probe_lock:
            capture=self.probe;self.probe=None
        if not capture['samples']:raise ValueError('采集窗口内没有手套数据')
        return capture

    @staticmethod
    def _byte_stats(column):
        sat=float(np.mean((column==0)|(column==255)))
        return {'median':round(float(np.median(column)),1),'std':round(float(column.std()),2),
                'min':int(column.min()),'max':int(column.max()),'saturated_fraction':round(sat,3)}

    def _decode_audit(self, raw_frames):
        """Evidence about the byte layout behind each extracted channel.

        The current code reads single bytes at fixed offsets. The offset
        pattern (x, x+1, x+3 every 20 bytes) suggests wider little-endian
        words, but that interpretation stays UNCONFIRMED until validated."""
        offsets=self.config['contact_byte_offsets']
        if not raw_frames:
            return {'frames':0,'interpretation':'no_frames','channels':[]}
        arr=np.frombuffer(b''.join(raw_frames),dtype=np.uint8).reshape(len(raw_frames),-1)
        channels=[]
        for i,x in enumerate(offsets):
            entry={'channel':i+1,'offset':x,
                   'byte_x':self._byte_stats(arr[:,x]),
                   'byte_x1':self._byte_stats(arr[:,x+1]) if x+1<arr.shape[1] else None,
                   'byte_x3':self._byte_stats(arr[:,x+3]) if x+3<arr.shape[1] else None}
            if x+1<arr.shape[1]:
                u16=arr[:,x].astype(np.int64)+(arr[:,x+1].astype(np.int64)<<8)
                entry['u16_le_candidate']={'median':round(float(np.median(u16)),1),
                                           'std':round(float(u16.std()),2),'interpretation':'unconfirmed'}
            channels.append(entry)
        return {'frames':len(raw_frames),'frame_bytes':int(arr.shape[1]),
                'extracted_width':'single_byte_per_channel_by_current_code',
                'interpretation':'offset_triple_pattern_suggests_wider_words;_width_and_endianness_UNCONFIRMED',
                'channels':channels}

    def _frame_scan(self, raw_frames, frame_baseline):
        """Whole-frame response scan over ALL bytes, not only the 12 selected offsets.

        This is the discovery half of the probe: if the real sensor responds on a
        byte outside the configured offsets, the scan surfaces it instead of the
        selected channels just reporting 'no response'. Absolute deltas are used
        so a byte that DROPS on pressure is found as readily as one that rises.
        The scan is evidence only; it never confirms a mapping by itself."""
        if not raw_frames or not frame_baseline:return {'frames':0,'frame_bytes':0,'top':[]}
        arr=np.frombuffer(b''.join(raw_frames),dtype=np.uint8).reshape(len(raw_frames),-1)
        med=np.asarray(frame_baseline['median'],dtype=np.float32)
        std=np.asarray(frame_baseline['std'],dtype=np.float32)
        w=min(arr.shape[1],med.shape[0])
        if w==0:return {'frames':len(raw_frames),'frame_bytes':int(arr.shape[1]),'top':[]}
        delta=np.abs(arr[:,:w].astype(np.int16)-med[:w])
        z90=np.percentile(delta,90,axis=0)/(std[:w]+1.0)
        offsets=set(self.config['contact_byte_offsets'])
        order=np.argsort(z90)[::-1][:8]
        top=[{'byte':int(i),
              'p90_abs_delta':round(float(np.percentile(delta[:,i],90)),1),
              'p90_z':round(float(z90[i]),1),
              'max_abs_delta':int(delta[:,i].max()),
              'in_selected_offsets':int(i) in offsets}
             for i in order if float(z90[i])>2.0]
        return {'frames':len(raw_frames),'frame_bytes':int(arr.shape[1]),'top':top}

    def probe_capture_baseline(self, seconds=3):
        seq=getattr(self,'seq',None)
        if seq and seq.get('running') and threading.current_thread() is not getattr(self,'seq_thread',None):
            raise ValueError('自动序列进行中，请先停止或等它结束')
        if self._motion_calibration_running():raise ValueError('运动标定进行中，请先停止或等它结束')
        capture=self._probe_capture(seconds,want_raw=True,raw_limit=240)
        values=np.stack([v for _,v in capture['samples']])
        med=np.median(values,axis=0);std=values.std(axis=0)
        stamp=time.strftime('%Y%m%d-%H%M%S')
        # Full-frame per-byte baseline so later trials can scan every byte,
        # not just the 12 selected offsets.
        frame_baseline=None
        if capture['raw_frames']:
            arr=np.frombuffer(b''.join(capture['raw_frames']),dtype=np.uint8).reshape(len(capture['raw_frames']),-1)
            frame_baseline={'median':[round(float(x),1) for x in np.median(arr,axis=0)],
                            'std':[round(float(x),2) for x in arr.std(axis=0)],
                            'frames':int(arr.shape[0]),'frame_bytes':int(arr.shape[1])}
        # The pressure grid's own zero reference falls out of the same capture: frame_baseline is a
        # per-byte median of whole frames, so its grid window IS the resting grid. Deriving it here
        # keeps one operator round from producing two different zeros for the same resting glove.
        grid_baseline=(None if frame_baseline is None
                       else pressure_baseline_from_full_frame(frame_baseline['median']))
        meta={'captured_at':stamp,'seconds':seconds,'samples':int(len(values)),
              'median':[round(float(x),2) for x in med],'std':[round(float(x),2) for x in std],
              'offsets':list(self.config['contact_byte_offsets']),
              'decode_version':'offsets-v3-sites-touch-bend+fullframe-scan',
              'frame_baseline':frame_baseline,
              'pressure_baseline':pressure_baseline_summary(grid_baseline),
              'pressure_baseline_source':'probe_full_frame_median',
              'device':{'vid':self.state['glove'].get('vid'),'pid':self.state['glove'].get('pid'),
                        'port':self.state['glove'].get('port'),'description':self.state['glove'].get('description')},
              'decode_audit':self._decode_audit(capture['raw_frames']),
              'mapping_version_at_capture':self.probe_map.get('version',0)}
        npz=self.probe_root/('baseline-'+stamp+'.npz')
        payload={'time_mono_ns':np.array([t for t,_ in capture['samples']],dtype=np.int64),
                 'values':values.astype(np.uint8)}
        if frame_baseline is not None:
            payload['byte_median']=np.array(frame_baseline['median'],dtype=np.float32)
            payload['byte_std']=np.array(frame_baseline['std'],dtype=np.float32)
        np.savez_compressed(npz,**payload)
        meta['raw_npz']=npz.name
        dump_json(self.probe_root/'baseline.json',meta)
        with self.probe_lock:
            self.probe_baseline=med;self.probe_baseline_meta=meta;self.probe_frame_baseline=frame_baseline
        # Keep the existing contact-detection baseline in sync with the probe baseline.
        with self.lock:
            self.baseline=med;self.baseline_until=0;self.baseline_samples=[]
            self.pressure_baseline=grid_baseline;self.pressure_baseline_samples=[]
            self.pressure_baseline_source=('probe_full_frame_median' if grid_baseline is not None
                                           else self.pressure_baseline_source)
            statuses=dict(self.probe_map.get('statuses',{}))
            statuses['baseline_zeroing']='done_relative_only_no_newton'
            self.probe_map['statuses']=statuses
            self.state['notice']=('定位测试基线已保存：这是相对字节值基线，不是压力标定'
                                  + ('；23×20 压感静置基线同批保存' if grid_baseline is not None
                                     else '；本次未取到完整帧，压感静置基线仍未保存'))
        return {'ok':True,'baseline':meta}

    def probe_capture_trial(self, position, trial_type, seconds=4):
        if position not in self.PROBE_POSITIONS:
            raise ValueError('位置代号必须是以下之一：'+', '.join(self.PROBE_POSITIONS))
        if trial_type not in ('press','bend_control','rest','motion_control','rest_pose','fist_pose','together_pose'):
            raise ValueError('无效的试验类型（press、bend_control、rest、motion_control 或手势状态）')
        seq=getattr(self,'seq',None)
        if seq and seq.get('running') and threading.current_thread() is not getattr(self,'seq_thread',None):
            raise ValueError('自动序列进行中，请先停止或等它结束')
        if self._motion_calibration_running():raise ValueError('运动标定进行中，请先停止或等它结束')
        if self.probe_baseline is None:raise ValueError('请先采集静置基线')
        capture=self._probe_capture(seconds,want_raw=True,raw_limit=1000)
        values=np.stack([v for _,v in capture['samples']])
        delta=values.astype(np.int16)-self.probe_baseline.astype(np.int16)
        # Bidirectional evidence: resistive sensors often DROP on pressure, and a
        # little-endian u16 interpretation could wrap. Only-up peaks would miss both.
        peak_up=delta.max(axis=0);peak_down=delta.min(axis=0)
        peak_abs=np.abs(delta).max(axis=0)
        positive=np.maximum(delta,0)
        p90_abs=np.percentile(np.abs(delta),90,axis=0)
        bstd=np.asarray((self.probe_baseline_meta or {}).get('std') or [0.0]*values.shape[1],dtype=float)
        z90=p90_abs/(bstd+1.0)
        n=values.shape[1]
        direction=['up' if int(peak_up[i])>=-int(peak_down[i]) else 'down' for i in range(n)]
        scan=self._frame_scan(capture['raw_frames'],self.probe_frame_baseline)
        record={'trial_id':time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6],
                'position':position,'trial_type':trial_type,'seconds':seconds,
                'samples':int(len(values)),'captured_at':time.strftime('%Y-%m-%dT%H:%M:%S'),
                'peak_delta':[round(float(x),2) for x in peak_up],
                'peak_abs_delta':[round(float(x),2) for x in peak_abs],
                'peak_down_delta':[round(float(x),2) for x in peak_down],
                'direction':direction,
                'mean_positive_delta':[round(float(x),2) for x in positive.mean(axis=0)],
                'p90_abs_delta':[round(float(x),2) for x in p90_abs],
                'p90_abs_z':[round(float(x),1) for x in z90],
                'baseline_captured_at':(self.probe_baseline_meta or {}).get('captured_at'),
                'mapping_version_at_trial':self.probe_map.get('version',0),
                'decode_version':'offsets-v3-sites-touch-bend+fullframe-scan',
                'frame_scan':scan,
                'curve':[[round(float(x),1) for x in row] for row in values[::max(1,len(values)//240)]]}
        npz=self.probe_root/('trial-'+record['trial_id']+'.npz')
        payload={'time_mono_ns':np.array([t for t,_ in capture['samples']],dtype=np.int64),
                 'values':values.astype(np.uint8)}
        if capture['raw_frames']:
            payload['raw']=np.frombuffer(b''.join(capture['raw_frames']),dtype=np.uint8).reshape(len(capture['raw_frames']),-1)
        np.savez_compressed(npz,**payload)
        record['raw_npz']=npz.name
        with (self.probe_root/'trials.jsonl').open('a',encoding='utf-8') as f:
            f.write(json.dumps(record,ensure_ascii=False)+'\n')
        return {'ok':True,'trial':record}

    # ---- guided tactile-signal sequence (UA-P2-04) ---------------------------
    # A fixed script of short steps (default 3 s capture + 1 s preparation gap)
    # covering rest/baseline, per-finger bend controls, tip and pad presses,
    # palm presses, hand rotation and table slide. Every step is a REAL
    # probe_capture_baseline / probe_capture_trial call, so evidence quality is
    # identical to manual captures; the runner only automates timing and cue
    # display and never interprets results (candidates stay operator-reviewed).
    PROBE_SEQUENCE_SECONDS=3;PROBE_SEQUENCE_GAP=1.0

    def _probe_sequence_steps(self):
        fingers=[('thumb','拇指'),('index','食指'),('middle','中指'),
                 ('ring','无名指'),('pinky','小指')]
        steps=[{'kind':'baseline','position':'hand_neutral','trial_type':'rest',
            'instruction':'正常放松手掌，五指自然伸展，保持不动（自动采集静置基线）'}]
        steps.append({'kind':'trial','position':'hand_relaxed','trial_type':'rest_pose',
                  'instruction':'正常放松手掌静置，保持自然姿态，不接触任何物体'})
        steps.append({'kind':'trial','position':'hand_fist','trial_type':'fist_pose',
                  'instruction':'手掌自然握拳，保持握拳姿态，不接触任何物体'})
        steps.append({'kind':'trial','position':'fingers_together','trial_type':'together_pose',
                  'instruction':'五指并拢伸直，保持手掌姿态，不接触任何物体'})
        for code,zh in fingers:
            steps.append({'kind':'trial','position':code+'_pad','trial_type':'bend_control',
                          'instruction':'只弯曲'+zh+'，其余手指放松，不接触任何东西'})
        for code,zh in fingers:
            steps.append({'kind':'trial','position':code+'_tip','trial_type':'press',
                          'instruction':zh+'尖按住桌面，保持到计时结束'})
        for code,zh in fingers:
            steps.append({'kind':'trial','position':code+'_pad','trial_type':'press',
                          'instruction':zh+'肚用力按压桌面，保持到计时结束'})
        steps.append({'kind':'trial','position':'palm_center','trial_type':'press',
                      'instruction':'掌心按压桌面，保持到计时结束'})
        steps.append({'kind':'trial','position':'palm_heel','trial_type':'press',
                      'instruction':'掌根按压桌面，保持到计时结束'})
        steps.append({'kind':'trial','position':'hand_rotate','trial_type':'motion_control',
                      'instruction':'手悬空，手腕缓慢旋转一圈（不接触桌面）'})
        steps.append({'kind':'trial','position':'table_slide','trial_type':'motion_control',
                      'instruction':'手套贴桌面缓慢滑动一段距离'})
        steps.append({'kind':'trial','position':'hand_neutral','trial_type':'rest',
                      'instruction':'恢复静置，手放松伸直不动'})
        return steps

    def _probe_sequence_summary(self):
        # Light summary for /api/state; called under self.lock (the same lock
        # the runner uses for mutations), so reads are consistent.
        s=self.seq
        if not s:return {'running':False}
        out={'running':bool(s.get('running')),'index':s.get('index'),'total':s.get('total'),
             'ok':sum(1 for r in s.get('results',[]) if r['ok']),
             'error':sum(1 for r in s.get('results',[]) if not r['ok'])}
        if s.get('current'):
            cur=s['current']
            out['current']={'index':cur['index'],'instruction':cur['instruction'],
                            'seconds':cur['seconds'],'gap':cur.get('gap',0),
                            'step_end_epoch':cur['step_end_epoch'],'status':cur['status']}
        return out

    def probe_sequence_start(self,seconds=None,gap=None):
        if self.seq and self.seq.get('running'):raise ValueError('自动序列已在进行中')
        if self._motion_calibration_running():raise ValueError('运动标定进行中，请先等它结束')
        if self.probe is not None:raise ValueError('已有一次采集正在进行，请等它结束')
        if self.state['mode']!='live':raise ValueError('回放模式没有真实触觉数据，不能做定位测试')
        if not self.state['glove'].get('connected'):raise ValueError('左手手套未连接')
        seconds=int(self.PROBE_SEQUENCE_SECONDS if seconds is None else seconds)
        gap=float(self.PROBE_SEQUENCE_GAP if gap is None else gap)
        if not 1<=seconds<=5:raise ValueError('每步时长须在1–5秒之间')
        if not 0<=gap<=5:raise ValueError('步间隔须在0–5秒之间')
        steps=self._probe_sequence_steps()
        self.seq={'running':True,'steps':steps,'total':len(steps),'index':-1,'current':None,
                  'results':[],'error':None,'seconds':seconds,'gap':gap,'stopped':False,
                  'started_at':time.strftime('%Y-%m-%dT%H:%M:%S')}
        self.seq_stop.clear()
        self.seq_thread=threading.Thread(target=self._probe_sequence_runner,
                                         name='probe-sequence',daemon=True)
        self.seq_thread.start()
        return {'ok':True,'total':len(steps),'seconds':seconds,'gap':gap}

    def _probe_sequence_runner(self):
        s=self.seq
        try:
            for i,step in enumerate(s['steps']):
                if self.seq_stop.is_set():break
                now=time.time()
                cur={'index':i,'instruction':step['instruction'],'kind':step['kind'],
                     'position':step.get('position'),'trial_type':step.get('trial_type'),
                     'seconds':s['seconds'],'gap':s['gap'],
                     'step_start_epoch':now,'step_end_epoch':now+s['gap']+s['seconds'],
                     'status':'preparing'}
                with self.lock:s['index']=i;s['current']=cur
                time.sleep(s['gap'])  # preparation gap: cue shows before capture starts
                if self.seq_stop.is_set():break
                with self.lock:cur['status']='capturing'
                try:
                    if step['kind']=='baseline':self.probe_capture_baseline(s['seconds'])
                    else:self.probe_capture_trial(step['position'],step['trial_type'],s['seconds'])
                    ok,err=True,None
                except (ValueError,RuntimeError,OSError) as e:
                    ok,err=False,str(e)
                with self.lock:
                    cur['status']='ok' if ok else 'error'
                    s['results'].append({'index':i,'instruction':step['instruction'],
                                         'position':step.get('position'),
                                         'trial_type':step.get('trial_type'),
                                         'ok':ok,'error':err})
            with self.lock:
                s['running']=False;s['current']=None
                s['stopped']=self.seq_stop.is_set()
                s['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%S')
        except Exception as e:  # never leave the sequence stuck in running=True
            with self.lock:
                s['running']=False;s['current']=None;s['error']=str(e)

    def probe_sequence_status(self):
        with self.lock:
            s=self.seq
            if not s:return {'running':False}
            out={'running':bool(s.get('running')),'index':s.get('index'),'total':s.get('total'),
                 'seconds':s.get('seconds'),'gap':s.get('gap'),'stopped':s.get('stopped',False),
                 'error':s.get('error'),'started_at':s.get('started_at'),
                 'finished_at':s.get('finished_at'),
                 'results':[dict(r) for r in s.get('results',[])],'current':None}
            if s.get('current'):out['current']=dict(s['current'])
            return out

    # ---- guided motion-bank calibration (evidence line for the motion bank) --
    # The frame carries an unprocessed motion-sensor bank (smooth analog bytes
    # with non-zero rest bias plus 0/254 flag bytes) outside the touch offsets.
    # This sequence automates the identification protocol: rest -> single-axis
    # slow rotation -> rest, per axis. A grouping pass then clusters
    # motion-coupled bytes into candidate axes (smooth coupling + synchronous
    # variation) and estimates per-byte rest bias and travel. Everything it
    # produces is EVIDENCE ONLY: no motion byte enters features or mappings
    # without a separate decision, and the captures never touch trials.jsonl
    # (they must not pollute the touch-probe statistics).
    MOTION_CAL_SECONDS=4;MOTION_CAL_GAP=1.0

    def _motion_calibration_running(self):
        m=getattr(self,'mcal',None)
        return bool(m and m.get('running'))

    def _motion_calibration_steps(self):
        return [
            {'kind':'rest','code':'rest_start',
             'instruction':'手完全静止，五指放松，保持不动（运动传感偏置参考）'},
            {'kind':'motion','code':'forearm_roll',
             'instruction':'前臂缓慢旋转（像转门把手），匀速转过去再转回来，手不触碰任何东西'},
            {'kind':'rest','code':'rest_mid1','instruction':'手完全静止，放松'},
            {'kind':'motion','code':'wrist_pitch',
             'instruction':'手腕缓慢上下摆动（手掌立起、放平、再立起），前臂位置基本不动'},
            {'kind':'rest','code':'rest_mid2','instruction':'手完全静止，放松'},
            {'kind':'motion','code':'wrist_yaw',
             'instruction':'手腕缓慢左右摆动（像扇扇子），不旋转前臂，手不触碰任何东西'},
            {'kind':'rest','code':'rest_mid3','instruction':'手完全静止，放松'},
            {'kind':'motion','code':'arm_translate',
             'instruction':'整只手臂缓慢上下平移（手掌朝向保持不变，离桌面远一点）'},
            {'kind':'rest','code':'rest_end',
             'instruction':'手完全静止，保持不动（结束参考）'},
        ]

    def motion_calibration_start(self,seconds=None,gap=None):
        if self._motion_calibration_running():raise ValueError('运动标定已在进行中')
        seq=getattr(self,'seq',None)
        if seq and seq.get('running'):raise ValueError('定位序列进行中，请先等它结束')
        if self.probe is not None:raise ValueError('已有一次采集正在进行，请等它结束')
        if self.state['mode']!='live':raise ValueError('回放模式没有真实数据，不能做运动标定')
        if not self.state['glove'].get('connected'):raise ValueError('左手手套未连接')
        seconds=int(self.MOTION_CAL_SECONDS if seconds is None else seconds)
        gap=float(self.MOTION_CAL_GAP if gap is None else gap)
        if not 1<=seconds<=10:raise ValueError('每步时长须在1–10秒之间')
        if not 0<=gap<=5:raise ValueError('步间隔须在0–5秒之间')
        steps=self._motion_calibration_steps()
        self.mcal={'running':True,'steps':steps,'total':len(steps),'index':-1,'current':None,
                   'results':[],'error':None,'seconds':seconds,'gap':gap,'stopped':False,
                   'result':None,'started_at':time.strftime('%Y-%m-%dT%H:%M:%S')}
        self.mcal_stop.clear()
        self.mcal_thread=threading.Thread(target=self._motion_calibration_runner,
                                          name='motion-calibration',daemon=True)
        self.mcal_thread.start()
        return {'ok':True,'total':len(steps),'seconds':seconds,'gap':gap}

    def _motion_calibration_runner(self):
        m=self.mcal
        segments=[]
        try:
            for i,step in enumerate(m['steps']):
                if self.mcal_stop.is_set():break
                now=time.time()
                cur={'index':i,'instruction':step['instruction'],'kind':step['kind'],
                     'code':step['code'],'seconds':m['seconds'],'gap':m['gap'],
                     'step_start_epoch':now,'step_end_epoch':now+m['gap']+m['seconds'],
                     'status':'preparing'}
                with self.lock:m['index']=i;m['current']=cur
                time.sleep(m['gap'])  # preparation gap: cue shows before capture starts
                if self.mcal_stop.is_set():break
                with self.lock:cur['status']='capturing'
                arr=None;ok=False;err=None;saved=None;frames=0
                try:
                    capture=self._probe_capture(m['seconds'],want_raw=True,raw_limit=3000)
                    arr=np.frombuffer(b''.join(capture['raw_frames']),
                                      dtype=np.uint8).reshape(len(capture['raw_frames']),-1)
                    stamp=time.strftime('%Y%m%d-%H%M%S')
                    npz=self.probe_root/('motioncal-'+stamp+'-'+step['code']+'.npz')
                    np.savez_compressed(npz,
                        time_mono_ns=np.array([t for t,_ in capture['samples']],dtype=np.int64),
                        raw=arr)
                    saved=npz.name;frames=int(arr.shape[0]);ok=True
                    segments.append({'code':step['code'],'kind':step['kind'],'raw':arr})
                except (ValueError,RuntimeError,OSError) as e:
                    err=str(e)
                with self.lock:
                    cur['status']='ok' if ok else 'error'
                    m['results'].append({'index':i,'code':step['code'],'kind':step['kind'],
                                         'instruction':step['instruction'],'ok':ok,'error':err,
                                         'frames':frames,'npz':saved})
            # Automatic grouping pass over the collected segments (evidence only).
            result=None
            if not self.mcal_stop.is_set() and len(segments)>=2:
                try:
                    result=self._motion_calibration_analyze(
                        segments,excluded_offsets=self.config['contact_byte_offsets'])
                except (ValueError,RuntimeError,OSError) as e:
                    result={'error':'analysis_failed','detail':str(e)}
            with self.lock:
                m['running']=False;m['current']=None
                m['stopped']=self.mcal_stop.is_set()
                m['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%S')
                if result is not None:
                    m['result']=result;self.mcal_last=result
                    dump_json(self.probe_root/'motion-calibration.json',result)
                    with (self.probe_root/'motion_calibration.jsonl').open('a',encoding='utf-8') as f:
                        f.write(json.dumps({'finished_at':m['finished_at'],
                                            'stopped':m['stopped'],'result':result},
                                           ensure_ascii=False)+'\n')
        except Exception as e:  # never leave the calibration stuck in running=True
            with self.lock:
                m['running']=False;m['error']=str(e);m['current']=None

    def motion_calibration_status(self):
        m=getattr(self,'mcal',None)
        if not m:return {'running':False,'result':getattr(self,'mcal_last',None)}
        out={'running':bool(m.get('running')),'index':m.get('index'),'total':m.get('total'),
             'seconds':m.get('seconds'),'gap':m.get('gap'),
             'results':[dict(r) for r in m.get('results',[])],
             'stopped':m.get('stopped',False),'finished_at':m.get('finished_at'),
             'error':m.get('error'),
             'result':m.get('result') or getattr(self,'mcal_last',None)}
        if m.get('current'):out['current']=dict(m['current'])
        return out

    def motion_calibration_stop(self):
        if not self._motion_calibration_running():return {'ok':True,'running':False}
        self.mcal_stop.set()
        return {'ok':True,'running':True,'note':'当前步采集结束后停止'}

    @staticmethod
    def _motion_calibration_analyze(segments,excluded_offsets=()):
        """Group motion-coupled bytes into candidate axes. Evidence only.

        The premise is tested here, never assumed. Working hypothesis: the frame
        carries an unprocessed motion bank (rest-stable analog bytes with a bias
        plus a per-axis travel). The 2026-09-18 real-glove run did not support
        it: in the undecoded tail (bytes 466-547) the rest drift between two
        neighbouring rest steps is as large as any commanded-axis response, so
        no byte may be claimed as a motion channel (see `diagnostic` and the
        reproduction in runtime/analyze_motion_calibration.py).

        Method (deliberately simple, no hidden assumptions):
        - rest segments give per-byte median (rest bias) and noise;
        - a byte is motion-coupled when some motion step moves it far above
          its rest noise (p90 activity >= 30 above rest p90 and >= 40);
        - active bytes are clustered by Pearson correlation of their centered
          time courses across all motion frames (|r| >= 0.8), sign preserved
          against the highest-variance member of the cluster;
        - a cluster whose members respond to every motion step alike, or whose
          motion values are nearly all 0/254, is flagged as flag-like rather
          than axis-like;
        - adjacent byte pairs inside a cluster are reported as unconfirmed u16
          little-endian candidates, mirroring the touch decode audit;
        - drift diagnostic: per-step medians give a rest-drift scale (largest
          median change between two neighbouring rest steps) and a per-step
          residual for each motion step against the mean of its two flanking
          rests; drift_score = |residual| / (rest drift + 1). A byte is only
          axis evidence when its motion response clearly exceeds its own rest
          drift, and the strongest candidates are reported even when the result
          is empty, so an empty result states why instead of staying silent;
        - motion-presence check, kept outside the axis logic: per step, the
          fraction of frames deviating >= 2 counts from that step's own median
          (immune to drift and to counter ramps). A gate byte needs the
          worst-case rest fraction <= 0.02, the worst-case motion fraction
          >= 0.25 and a worst-case p90 lift >= 4 counts. Worst case, not median,
          because a window can sit inside one phase of an unrelated slow square
          wave, and because rest windows can themselves be busy. Reported as
          `diagnostic.activity_lift` with status `motion_activity_gate_only`,
          never clustered as an axis, because a gate says the hand moved and
          carries no joint angle. The 2026-09-18 run reports no gate either."""
        rest=np.concatenate([s['raw'] for s in segments if s['kind']=='rest']).astype(np.float64)
        motion=[s for s in segments if s['kind']=='motion']
        if rest.shape[0]<50 or not motion:
            return {'error':'insufficient_segments','rest_frames':int(rest.shape[0]),
                    'motion_steps':len(motion)}
        rmed=np.median(rest,axis=0);rp90=np.percentile(np.abs(rest-rmed),90,axis=0)
        Mraw=np.concatenate([s['raw'] for s in motion],axis=0)
        excluded=set(int(x) for x in excluded_offsets)
        per_step={s['code']:np.percentile(np.abs(s['raw'].astype(np.float64)-rmed),90,axis=0)
                  for s in motion}
        strongest=np.max(np.stack([per_step[s['code']] for s in motion]),axis=0)
        active=[int(b) for b in range(Mraw.shape[1])
                if b not in excluded and strongest[b]-rp90[b]>=30.0 and strongest[b]>=40.0]
        # Drift-aware diagnostic. The rest-drift scale says how far a byte moves
        # between two neighbouring rest steps with no commanded motion, so a
        # motion-step residual can be judged against the byte's own instability
        # instead of against an assumed-stable bias.
        step_med={s['code']:np.median(s['raw'].astype(np.float64),axis=0) for s in segments}
        layout=[(s['kind'],s['code']) for s in segments]
        rests=[code for kind,code in layout if kind=='rest']
        drift=np.zeros(Mraw.shape[1])
        for before,after in zip(rests,rests[1:]):
            drift=np.maximum(drift,np.abs(step_med[after]-step_med[before]))
        residual={}
        for i,(kind,code) in enumerate(layout):
            if kind!='motion':continue
            before=next((layout[j][1] for j in range(i-1,-1,-1) if layout[j][0]=='rest'),None)
            after=next((layout[j][1] for j in range(i+1,len(layout)) if layout[j][0]=='rest'),None)
            if before is None or after is None:continue
            residual[code]=step_med[code]-0.5*(step_med[before]+step_med[after])
        candidates=[b for b in range(Mraw.shape[1]) if b not in excluded]
        # Motion-presence check, kept separate from axis detection. A gate byte
        # must be quiet in EVERY rest window and active in EVERY motion window,
        # because one window can sit inside a phase of an unrelated slow square
        # wave. Two 2026-09-18 over-claims are rejected by that worst-case rule:
        # - byte 507 (rest p90 0.0 vs motion p90 253) toggles 10-32 times inside
        #   rest steps too, so only its duty cycle moved;
        # - bytes 23/43/65/25/11/31, which looked like gates when only the
        #   motion windows were compared, are active in rest_mid2 (b23 0.126 of
        #   frames, b43 0.129) and b65/b25/b45 are silent during arm_translate.
        # The undecoded tail's global activity is flat across all nine steps
        # (0.48-0.63 of frames deviate >= 2 counts in rest and motion alike), so
        # on this evidence the packet exposes neither an axis nor a clean gate.
        # Activity is measured against each step's OWN median, which is immune
        # to drift and to counter ramps.
        DEV=2.0
        def _within(seg):
            d=np.abs(seg['raw']-np.median(seg['raw'],axis=0))
            return (d>=DEV).mean(axis=0),np.percentile(d,90,axis=0)
        rest_w=[_within(s) for s in segments if s['kind']=='rest']
        motion_w=[_within(s) for s in segments if s['kind']=='motion']
        if rest_w and motion_w:
            rest_activity=np.max(np.stack([f for f,_ in rest_w]),axis=0)
            rest_p90_max=np.max(np.stack([p for _,p in rest_w]),axis=0)
            motion_frac_min=np.min(np.stack([f for f,_ in motion_w]),axis=0)
            motion_p90_min=np.min(np.stack([p for _,p in motion_w]),axis=0)
        else:
            rest_activity=np.zeros(Mraw.shape[1]);rest_p90_max=np.zeros(Mraw.shape[1])
            motion_frac_min=np.zeros(Mraw.shape[1]);motion_p90_min=np.zeros(Mraw.shape[1])
        lift=motion_p90_min-rest_p90_max
        lift_bytes=[int(b) for b in candidates
                    if rest_activity[b]<=0.02 and motion_frac_min[b]>=0.25 and lift[b]>=4.0]
        strongest_residual=(np.max(np.abs(np.stack([residual[c] for c in residual])),axis=0)
                            if residual else np.zeros(Mraw.shape[1]))
        score=strongest_residual/(drift+1.0)
        diag={'rest_drift_summary':{'median':round(float(np.median(drift[candidates])),1),
                                    'p90':round(float(np.percentile(drift[candidates],90)),1),
                                    'max':round(float(drift[candidates].max()),1)},
              'drift_score_max':round(float(score[candidates].max()),2),
              'method':('drift_score = max over motion steps of |step median - mean(flanking rest medians)|'
                        ' / (rest drift + 1), where rest drift is the largest median change between two'
                        ' neighbouring rest steps'),
              'activity_lift':{'rest_frame_ceiling':0.02,'motion_frame_floor':0.25,'lift_floor':4.0,
                               'bytes':lift_bytes,
                               'method':('per step, fraction of frames deviating >= 2 counts from that'
                                         ' step\'s own median, and p90 of that deviation; a gate byte'
                                         ' needs max-over-rest fraction <= 0.02, min-over-motion'
                                         ' fraction >= 0.25 and min-over-motion p90 minus'
                                         ' max-over-rest p90 >= 4 counts (worst case, not median,'
                                         ' so a window that merely samples a phase of a slow'
                                         ' square wave is rejected)'),
                               'top':[{'byte':int(b),'rest_activity':round(float(rest_activity[b]),3),
                                       'rest_p90_max':round(float(rest_p90_max[b]),1),
                                       'lift':round(float(lift[b]),1),
                                       'motion_frac_min':round(float(motion_frac_min[b]),3),
                                       'motion_activity':{s['code']:round(float(_within(s)[1][b]),1)
                                                          for s in motion}}
                                      for b in sorted(lift_bytes,key=lambda b:-lift[b])[:8]]},
              'top_candidates':[{'byte':int(b),'drift_score':round(float(score[b]),2),
                                 'rest_drift':round(float(drift[b]),1),
                                 'rest_bias':round(float(rmed[b]),1),
                                 'residuals':{c:round(float(residual[c][b]),1) for c in residual}}
                                for b in sorted(candidates,key=lambda b:-score[b])[:8]]}
        out={'status':'candidate_axes_evidence_only',
             'captured_at':time.strftime('%Y-%m-%dT%H:%M:%S'),
             'rest_frames':int(rest.shape[0]),
             'motion_steps':[s['code'] for s in motion],
             'excluded_offsets_count':len(excluded),
             'active_bytes':active,'groups':[],'diagnostic':diag,
             'interpretation':'evidence_not_integrated_into_features'}
        if len(active)<2:
            if lift_bytes:
                top=diag['activity_lift']['top'][0]
                out['status']='motion_activity_gate_only'
                out['note']=('no rest-stable bias+travel axis found; %d byte(s) instead show a repeatable'
                             ' motion-vs-rest activity gate (top byte %d: deviating frames max %.3f of'
                             ' rest windows vs min %.3f of motion windows, within-step p90 up to %.1f'
                             ' during motion, median unchanged), so motion presence is detectable in'
                             ' this packet but no axis is decodable from it'
                             %(len(lift_bytes),top['byte'],top['rest_activity'],
                               top['motion_frac_min'],max(top['motion_activity'].values())))
            else:
                out['status']='no_motion_locked_bytes_found'
                out['note']=('no motion-locked byte found: rest drift (median %.1f, max %.1f counts) covers'
                             ' the strongest motion residual (drift_score max %.2f), so the undecoded tail is'
                             ' not a rest-stable analog bank and no axis can be claimed; the motion-presence'
                             ' check also found no byte that is quiet in every rest window and active in'
                             ' every motion window (worst-case rest deviating-frame fraction <= %.2f,'
                             ' motion >= %.2f, p90 lift >= %.1f counts)'
                             %(diag['rest_drift_summary']['median'],diag['rest_drift_summary']['max'],
                               diag['drift_score_max'],diag['activity_lift']['rest_frame_ceiling'],
                               diag['activity_lift']['motion_frame_floor'],
                               diag['activity_lift']['lift_floor']))
            return out
        M=Mraw[:,active].astype(np.float64).T
        M=M-M.mean(axis=1,keepdims=True)
        nrm=np.linalg.norm(M,axis=1);nrm[nrm==0]=1.0
        C=(M@M.T)/np.outer(nrm,nrm)
        parent=list(range(len(active)))
        def find(x):
            while parent[x]!=x:parent[x]=parent[parent[x]];x=parent[x]
            return x
        for i in range(len(active)):
            for j in range(i+1,len(active)):
                if abs(C[i,j])>=0.8:
                    a,b=find(i),find(j)
                    if a!=b:parent[a]=b
        clusters={}
        for i in range(len(active)):clusters.setdefault(find(i),[]).append(i)
        for members in sorted(clusters.values(),key=len,reverse=True):
            idxs=[active[i] for i in members]
            rep_pos=int(np.argmax([M[i].std() for i in members]))
            rep=members[rep_pos]
            mvals=Mraw[:,idxs]
            flag_frac=float(np.mean((mvals==0)|(mvals==254)))
            responds_all=[sum(1 for code in per_step if per_step[code][b]-rp90[b]>=30.0)
                          for b in idxs]
            flag_like=bool(flag_frac>0.9 or float(np.mean(np.asarray(responds_all)>=len(motion)*0.8))>0.5)
            u16_pairs=[[b,b+1] for b in idxs[:-1] if (b+1) in idxs]
            out['groups'].append({
                'bytes':idxs,'size':len(idxs),'rep_byte':idxs[rep_pos],
                'corr_to_rep':[round(float(C[members[k],rep]),2) for k in range(len(members))],
                'rest_bias':[round(float(rmed[b]),1) for b in idxs],
                'travel_p95_p5':[round(float(np.percentile(mvals[:,k],95)
                                             -np.percentile(mvals[:,k],5)),1)
                                 for k in range(len(idxs))],
                'per_step_p90':{code:[round(float(per_step[code][b]),1) for b in idxs]
                                for code in per_step},
                'flag_like':flag_like,'flag_fraction':round(flag_frac,3),
                'u16_pairs':u16_pairs,
                'interpretation':'evidence_not_integrated_into_features'})
        return out


    def probe_sequence_stop(self):
        if not self.seq or not self.seq.get('running'):return {'ok':True,'running':False}
        self.seq_stop.set()
        return {'ok':True,'running':True,'note':'当前步采集结束后停止'}

    def probe_report(self):
        trials=[]
        p=self.probe_root/'trials.jsonl'
        if p.exists():
            for line in p.read_text('utf-8').splitlines():
                try:trials.append(json.loads(line))
                except ValueError:continue
        groups={}
        # Rows from an older decode era (e.g. legacy 12-channel beside current
        # 54-channel) must never be stacked into one numpy array; they become
        # separate display groups and are excluded from candidate math, so a
        # mixed-history trials.jsonl cannot crash the report.
        n_cur=len(self.config['contact_byte_offsets'])
        legacy_rows=0
        for t in trials:
            n=len(t.get('peak_delta') or [])
            if n!=n_cur:
                legacy_rows+=1
                groups.setdefault((t['position'],t['trial_type'],'legacy%d'%n),[]).append(t)
            else:
                groups.setdefault((t['position'],t['trial_type'],'current'),[]).append(t)
        matrix=[]
        for (position,ttype,era),items in sorted(groups.items()):
            peak=np.array([t['peak_delta'] for t in items],dtype=float)
            matrix.append({'position':position,'trial_type':ttype,'trials':len(items),
                           'channels':int(peak.shape[1]),
                           'median_peak':[round(float(x),1) for x in np.median(peak,axis=0)],
                           'min_peak':[round(float(x),1) for x in peak.min(axis=0)],
                           'max_peak':[round(float(x),1) for x in peak.max(axis=0)]})
        on=self.config['contact_on_delta']
        candidates={}
        # Discovery aggregation: which bytes anywhere in the frame respond across
        # press trials, and are they inside the 12 selected offsets or not?
        per_byte={}
        offsets_set=set(self.config['contact_byte_offsets'])
        for (position,ttype,era),items in sorted(groups.items()):
            if ttype!='press':continue
            for t in items:
                for b in (t.get('frame_scan') or {}).get('top',[]):
                    e=per_byte.setdefault(int(b['byte']),{'z':[],'positions':set()})
                    e['z'].append(float(b['p90_z']));e['positions'].add(position)
        discovery=sorted(({'byte':k,'median_p90_z':round(float(np.median(v['z'])),1),
                           'min_p90_z':round(float(min(v['z'])),1),'positions':sorted(v['positions']),
                           'in_selected_offsets':k in offsets_set}
                          for k,v in per_byte.items()),
                         key=lambda d:-d['median_p90_z'])[:12]
        # Rest control: glove worn, hand still, nothing pressed. Gives the false
        # positive level every candidate must be compared against.
        rest_rows=[t for (p,tt,e),items in groups.items() if tt=='rest' and e=='current' for t in items]
        rest_med_z=None
        if rest_rows:
            rest_med_z=np.median(np.array([t['p90_abs_z'] for t in rest_rows],dtype=float),axis=0)
        for (position,ttype,era),items in sorted(groups.items()):
            if ttype!='press' or era!='current' or len(items)<2:continue
            peak=np.array([t['peak_delta'] for t in items],dtype=float)
            p90abs=np.array([t['p90_abs_delta'] for t in items],dtype=float)
            z90=np.array([t['p90_abs_z'] for t in items],dtype=float)
            dirs=[t.get('direction') or ['up']*len(t['peak_delta']) for t in items]
            n_ch=len(items[0]['peak_delta'])
            # Legacy threshold view (rise-only, fixed delta) kept for comparison.
            responders=[i for i in range(n_ch) if float(np.median(peak[:,i]))>=on]
            repeatable_thr=[i for i in responders if float(peak[:,i].min())>=on]
            # Primary z-score view: robust per-trial magnitude vs baseline noise,
            # then direction consistency so a channel flapping up/down is not a candidate.
            responders_z=[i for i in range(n_ch) if float(np.median(z90[:,i]))>=5.0]
            repeatable=[i for i in responders_z
                        if float(z90[:,i].min())>=3.0 and float(p90abs[:,i].min())>=on
                        and len({d[i] for d in dirs})==1]
            bend_med=None
            for (p3,t3,e3),b_items in groups.items():
                if p3==position and t3=='bend_control' and e3=='current':
                    bend_med=np.median(np.array([b['p90_abs_delta'] for b in b_items],dtype=float),axis=0)
            control='ok' if rest_med_z is not None else 'missing_rest_control'
            pressure_like=[i for i in repeatable
                           if (bend_med is None or float(bend_med[i])<on
                               or float(bend_med[i])<float(np.median(p90abs[:,i]))*0.5)
                           and (rest_med_z is None or float(rest_med_z[i])<2.0)]
            if control!='ok':pressure_like=[]
            candidates[position]={'press_trials':len(items),
                                  'responder_channels':responders,
                                  'repeatable_channels_threshold':repeatable_thr,
                                  'responder_channels_z':responders_z,
                                  'repeatable_channels':repeatable,
                                  'pressure_like_channels':pressure_like,
                                  'control_status':control,
                                  'rest_trials':len(rest_rows),
                                  'multi_channel':len(repeatable)>1,
                                  'status':'candidate_only_never_auto_confirmed'}
        return {'labels':list(self.probe_map['labels']),'source':self.probe_map.get('source','unconfirmed'),
                'version':self.probe_map.get('version',0),'statuses':dict(self.probe_map.get('statuses',{})),
                'baseline':getattr(self,'probe_baseline_meta',None),'matrix':matrix,'candidates':candidates,
                'discovery':{'bytes':discovery,
                             'strongest_outside_selected_offsets':bool(discovery and not discovery[0]['in_selected_offsets'])},
                'positions':list(self.PROBE_POSITIONS),
                'offsets':list(self.config['contact_byte_offsets']),'trials_total':len(trials),
                'decode_channels':n_cur,'legacy_rows':legacy_rows,
                'on_delta':self.config.get('contact_on_delta',10),'off_delta':self.config.get('contact_off_delta',5)}

    def probe_set_mapping(self, labels, note=''):
        if not isinstance(labels,list) or len(labels)!=len(self.config['contact_byte_offsets']):raise ValueError(f'需要恰好 {len(self.config["contact_byte_offsets"])} 个通道标签')
        clean=[]
        for l in labels:
            l=str(l or '').strip()
            if l and not re.fullmatch(r'[\w\-·]{1,24}',l):raise ValueError('通道标签只能含文字、数字、横线或·，最长24字符')
            clean.append(l or 'unknown')
        statuses=dict(self.probe_map.get('statuses',{}))
        confirmed=any(l!='unknown' for l in clean)
        statuses['position_mapping']='confirmed_by_operator' if confirmed else 'unconfirmed'
        self.probe_map={'version':self.probe_map.get('version',0)+1,
                        'labels':clean,'source':'operator_confirmed_via_channel_probe' if confirmed else 'unconfirmed',
                        'note':str(note or '')[:500],'updated_at':time.strftime('%Y-%m-%dT%H:%M:%S'),
                        'statuses':statuses}
        dump_json(self.probe_root/'channel_map.json',self.probe_map)
        with self.lock:
            self.config['contact_channel_map']=list(clean)
            self.config['contact_channel_map_source']=self.probe_map['source']
            self.state['glove']['channel_labels']=list(clean)
            self.state['glove']['channel_map_source']=self.probe_map['source']
        return {'ok':True,'version':self.probe_map['version'],'labels':clean,'statuses':statuses}
