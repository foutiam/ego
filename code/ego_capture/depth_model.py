"""GPU-first IEBins depth inference for the capture station.

Device policy (user decision 2026-09-17): GPU first. DEVICE is 'cuda' when a
CUDA device is available, 'cpu' otherwise. No CPU-fallback chains, no retries:
if VRAM is exhausted the load/inference fails with an explicit "VRAM occupied"
error. Everything here reuses the IEBins upstream network and the exact
official evaluation preprocessing (ImageNet normalization, no resize for the
NYU 480x640 input, KITTI width-1216 band input).

torch is imported lazily so a capture-only station start stays fast.

The formal local training run is deferred to the NTU cluster
(P1-FULL = DEFERRED_LOCAL_PIPELINE_PROVEN_NOT_COMPLETED); this module only
serves inference on existing checkpoints.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import io
import os
import queue
import re
import threading
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
MODEL_ZOO = ROOT / 'outputs/iebins-repro/model_zoo'
OVERFIT_DIR = ROOT / 'outputs/iebins-repro/models_overfit/0916/iebins_nyu_overfit'


def device_string():
    """GPU-only policy (user decision 2026-09-17): depth inference must run on
    CUDA. There is no CPU fallback; a missing GPU is a loud, explicit error.
    Capture/status/recording features do not depend on this function."""
    import torch
    if not torch.cuda.is_available():
        hidden = os.environ.get('CUDA_VISIBLE_DEVICES')
        hint = (' CUDA_VISIBLE_DEVICES=%r hides all GPUs; remove it from the '
                'launcher environment.' % hidden) if hidden in ('', '-1') else ''
        raise RuntimeError(
            '深度推理要求 GPU（GPU-only，无 CPU 回退）：torch.cuda.is_available()=False。'
            '%s请检查启动脚本环境变量、NVIDIA 驱动与 CUDA 安装；'
            '采集/状态/录制功能不受影响。' % hint)
    return 'cuda'


# Fixed, checkpoint-specific color scale. Fixed colorbars are displayed on the
# web page using the same ranges, so map and overlay stay comparable.
CHECKPOINTS = [
    {
        'id': 'nyu_official_L',
        'path': MODEL_ZOO / 'nyu_L.pth',
        'dataset': 'nyu',
        'encoder': 'large07',
        'max_depth': 10.0,
        'source': 'official_release',
        'label': '官方 NYUv2 Swin-L（IEBins 发布权重）',
        'provenance': ('官方 checkpoint（P1 step 3 已复现评测：AbsRel 0.0871 / RMSE 0.3134，'
                       '654 样本，偏差 ≤0.0006，见 outputs/iebins-repro/RESULTS_eval_nyu_L_official_ckpt.md）'),
        'input_size': (480, 640),          # (H, W), official eval has no resize
        'resize_note': '画面缩放到 480x640（官方评测输入尺寸）',
    },
    {
        'id': 'kitti_official_L',
        'path': MODEL_ZOO / 'kittieigen_L.pth',
        'dataset': 'kitti',
        'encoder': 'large07',
        'max_depth': 80.0,
        'source': 'official_release',
        'label': '官方 KITTI Eigen Swin-L（IEBins 发布权重）',
        'provenance': ('官方 checkpoint（P1 step 4 复现评测为 491 样本子集：全部 697 帧中官方 '
                       'data_depth_annotated 存档即缺少 206 帧 GT，rgb_missing=0，'
                       'KITTI-206-AUDIT-PASS；AbsRel 0.0515 / RMSE 2.0124）'),
        'input_size': (352, 1216),
        'resize_note': '画面缩放到宽 1216 后取垂直居中的高 352 水平带（kb-crop 语义）',
    },
    {
        'id': 'local_overfit_nyu',
        'path': OVERFIT_DIR / 'model-latest.pth',
        'dataset': 'nyu',
        'encoder': 'large07',
        'max_depth': 10.0,
        'source': 'local_training',
        'label': '本地过拟合链 checkpoint（P1-TRAIN-SMOKE，非正式训练产物）',
        'provenance': ('sanity + overfit resume×3 证明 train/save/resume 链路的本地小步数权重；'
                       '不得用于任何科学结论，仅用于验证本地管线'),
        'input_size': (480, 640),
        'resize_note': '画面缩放到 480x640（官方评测输入尺寸）',
    },
]


def find_checkpoint(model_id):
    for entry in CHECKPOINTS:
        if entry['id'] == model_id:
            return entry
    raise ValueError('Unknown model id: %s' % model_id)


def _sha256_file(path, cache):
    path = Path(path)
    if not path.exists():
        return None
    key = (str(path), path.stat().st_size, path.stat().st_mtime)
    if key not in cache:
        with path.open('rb') as f:
            cache[key] = hashlib.file_digest(f, 'sha256').hexdigest()
    return cache[key]


def depth_stats(depth):
    valid = depth[np.isfinite(depth) & (depth > 1e-3)]
    if not valid.size:
        return {'valid_px': 0, 'min_m': None, 'max_m': None,
                'mean_m': None, 'median_m': None, 'p90_m': None}
    return {'valid_px': int(valid.size),
            'min_m': round(float(valid.min()), 4),
            'max_m': round(float(valid.max()), 4),
            'mean_m': round(float(valid.mean()), 4),
            'median_m': round(float(np.median(valid)), 4),
            'p90_m': round(float(np.percentile(valid, 90)), 4)}


_PATHS_DONE = False


def sys_path_setup():
    """Expose the IEBins upstream package and the mmcv shim (set up once)."""
    global _PATHS_DONE
    if _PATHS_DONE:
        return
    import sys
    for p in (str(ROOT / 'code/iebins-repro-env/mmcv_shim'),
              str(ROOT / 'code/iebins-upstream')):
        if p not in sys.path:
            sys.path.insert(0, p)
    _PATHS_DONE = True


def decode_image_b64(data_b64):
    from PIL import Image
    raw = base64.b64decode(data_b64)
    img = Image.open(io.BytesIO(raw)).convert('RGB')
    return np.asarray(img)


class DepthRunner:
    """Loads one IEBins checkpoint at a time and serves single-frame inference.

    Inference requests go through a worker with a bounded queue of size 1:
    a new request replaces a still-pending older one (newest frame wins).
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.sha_cache = {}
        self.model = None
        self.entry = None
        self.meta = {}
        self.last = None            # last inference result (depth array + info)
        self._requests = queue.Queue(maxsize=1)
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()
        self.torch = None           # imported on first use
        self._last_infer_started = 0.0

    @staticmethod
    def _device_report():
        import torch
        return 'cuda' if torch.cuda.is_available() else 'cuda_unavailable'

    # ---- registry -------------------------------------------------------
    def list_models(self):
        rows = []
        for entry in CHECKPOINTS:
            path = entry['path']
            rows.append({
                'id': entry['id'], 'label': entry['label'], 'dataset': entry['dataset'],
                'encoder': entry['encoder'], 'max_depth': entry['max_depth'],
                'source': entry['source'], 'provenance': entry['provenance'],
                'resize_note': entry['resize_note'],
                'input_size': list(entry['input_size']),
                'exists': path.exists(),
                'bytes': path.stat().st_size if path.exists() else None,
                'sha256': _sha256_file(path, self.sha_cache),
                'path': str(path),
                'loaded': self.entry is not None and self.entry['id'] == entry['id'],
            })
        return {'device': self._device_report() if self.torch else None,
                'models': rows, 'loaded_id': self.entry['id'] if self.entry else None,
                'meta': self.meta or None}

    # ---- loading --------------------------------------------------------
    def _ensure_torch(self):
        if self.torch is None:
            import torch
            self.torch = torch
        return self.torch

    def load(self, model_id):
        entry = find_checkpoint(model_id)
        path = Path(entry['path'])
        if not path.exists():
            raise ValueError('Checkpoint 不存在：%s' % path)
        if self.entry is not None and self.entry['id'] == model_id and self.model is not None:
            return self.status()
        torch = self._ensure_torch()
        # Fail fast on the GPU-only policy before doing any model work.
        device = device_string()
        free_b, _total_b = torch.cuda.mem_get_info()
        if free_b < 3 * 2**30:
            raise RuntimeError(
                'VRAM 被占用：空闲 %.1f GiB，加载 IEBins checkpoint 需要约 3 GiB。'
                '请关闭占用显存的程序后重试。' % (free_b / 2**30))
        sys_path_setup()
        from iebins.networks.NewCRFDepth import NewCRFDepth
        t0 = time.time()
        model = NewCRFDepth(version=entry['encoder'], inv_depth=False,
                            max_depth=entry['max_depth'], pretrained=None)
        ckpt = torch.load(path, map_location='cpu', weights_only=False)
        state = {k[len('module.'):] if k.startswith('module.') else k: v
                 for k, v in ckpt['model'].items()}
        model.load_state_dict(state, strict=True)
        device = device_string()
        model.eval().to(device)
        with self.lock:
            self.model = model
            self.entry = entry
            self.meta = {
                'global_step': ckpt.get('global_step'),
                'params': sum(p.numel() for p in model.parameters()),
                'device': device,
                'gpu_name': torch.cuda.get_device_name(0) if device == 'cuda' else None,
                'load_seconds': round(time.time() - t0, 2),
                'loaded_at': time.time(),
            }
        del ckpt, state
        if device == 'cuda':
            torch.cuda.empty_cache()
        return self.status()

    def status(self):
        if not self.entry:
            return {'loaded': False}
        return {'loaded': True, 'id': self.entry['id'], 'label': self.entry['label'],
                'dataset': self.entry['dataset'], 'max_depth': self.entry['max_depth'],
                'resize_note': self.entry['resize_note'],
                'sha256': _sha256_file(self.entry['path'], self.sha_cache),
                'meta': self.meta}

    # ---- inference ------------------------------------------------------
    def _prepare_input(self, rgb, entry):
        """Official preprocessing: /255, ImageNet normalize, model input size.

        NYU checkpoints: resize the frame to 480x640 (official eval input).
        KITTI checkpoints: resize width to 1216, then take the central 352-high
        band (kb-crop semantics); the note is returned for the metadata.
        """
        import torch
        from PIL import Image
        from torchvision import transforms as T
        h, w = entry['input_size']
        img = Image.fromarray(rgb)
        src = img.size                              # (W, H)
        if entry['dataset'] == 'kitti':
            scale = w / img.width
            img = img.resize((w, max(h, int(round(img.height * scale)))), Image.BILINEAR)
            top = max(0, (img.height - h) // 2)
            img = img.crop((0, top, w, top + h))
        else:
            img = img.resize((w, h), Image.BILINEAR)
        x = np.asarray(img, dtype=np.float32) / 255.0
        x = torch.from_numpy(x.transpose((2, 0, 1)))
        x = T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])(x)
        return x.unsqueeze(0), src, (img.width, img.height)

    def infer_sync(self, rgb, post_process=True, timeout=30.0, max_fps=10.0):
        """Submit one frame to the worker queue and wait for the result.

        The queue holds at most one pending request; a new submission replaces
        a pending one (which is then reported as superseded, never computed).
        """
        done = threading.Event()
        holder = {'result': None, 'superseded': False}
        max_fps = float(max_fps)
        if not 0.1 <= max_fps <= 120.0:
            raise ValueError('max_fps must be between 0.1 and 120')
        req = {'rgb': rgb, 'post_process': bool(post_process), 'max_fps': max_fps,
               'done': done, 'holder': holder}
        try:
            self._requests.put_nowait(req)
        except queue.Full:
            stale = self._requests.get_nowait()
            stale['holder']['superseded'] = True
            stale['done'].set()
            self._requests.put_nowait(req)
        if not done.wait(timeout):
            raise RuntimeError('深度推理超时（%.0f s）' % timeout)
        if holder['superseded']:
            return {'superseded': True}
        result = holder['result']
        if isinstance(result, Exception):
            raise result
        return result

    def _work(self):
        while True:
            req = self._requests.get()
            try:
                next_start = self._last_infer_started + (1.0 / req['max_fps'])
                while True:
                    delay = next_start - time.monotonic()
                    if delay <= 0:
                        break
                    time.sleep(delay)
                self._last_infer_started = time.monotonic()
                req['holder']['result'] = self._infer(req['rgb'], req['post_process'])
                req['holder']['result']['metadata']['max_fps'] = req['max_fps']
            except Exception as exc:                # noqa: BLE001 - reported to caller
                req['holder']['result'] = exc
            finally:
                req['done'].set()

    def _infer(self, rgb, post_process):
        if self.model is None:
            raise ValueError('尚未加载模型：先在「05 模型与深度」选择并加载 checkpoint')
        entry = self.entry
        torch = self._ensure_torch()
        device = next(self.model.parameters()).device
        x, src_size, input_size = self._prepare_input(rgb, entry)
        x = x.to(device)
        t0 = time.time()
        try:
            with torch.no_grad():
                pred, _, _ = self.model(x)
                if post_process:
                    from iebins.utils import flip_lr, post_process_depth
                    pred_f, _, _ = self.model(flip_lr(x))
                    depth = post_process_depth(pred[-1], pred_f[-1])
                else:
                    depth = pred[-1]
            if device.type == 'cuda':
                torch.cuda.synchronize()
        except torch.cuda.OutOfMemoryError as exc:
            torch.cuda.empty_cache()
            free_b, _t = torch.cuda.mem_get_info()
            raise RuntimeError('VRAM 被占用（CUDA OOM）：空闲 %.0f MiB 不足。'
                               '请关闭占用显存的程序后重试。原始错误：%s'
                               % (free_b / 2**20, exc)) from exc
        latency_ms = (time.time() - t0) * 1000.0
        depth = depth.squeeze().float().cpu().numpy()
        depth_jpeg, overlay_jpeg = self._render(rgb, depth, entry, input_size)
        result = {
            'depth_jpeg': depth_jpeg,
            'overlay_jpeg': overlay_jpeg,
            'stats': depth_stats(depth),
            'metadata': {
                'model_id': entry['id'], 'label': entry['label'],
                'sha256': _sha256_file(entry['path'], self.sha_cache),
                'global_step': self.meta.get('global_step'),
                'device': self.meta.get('device'), 'gpu_name': self.meta.get('gpu_name'),
                'post_process': post_process,
                'latency_ms': round(latency_ms, 1),
                'fps': round(1000.0 / max(latency_ms, 1e-6), 2),
                'source_size': list(src_size), 'input_size': list(input_size),
                'resize_note': entry['resize_note'],
                'color_range_m': [0.0, entry['max_depth']],
                'captured_at': time.time(),
            },
        }
        with self.lock:
            self.last = {'depth': depth, 'result': result}
        return result

    def _render(self, rgb, depth, entry, input_size):
        import matplotlib
        matplotlib.use('Agg')
        from matplotlib import cm
        from PIL import Image
        name = 'jet' if entry['dataset'] == 'nyu' else 'magma'
        try:
            cmap = matplotlib.colormaps[name]
        except AttributeError:                      # matplotlib < 3.6
            cmap = cm.get_cmap(name)
        norm = np.clip(depth / max(entry['max_depth'], 1e-6), 0.0, 1.0)
        colored = (cmap(norm)[..., :3] * 255).astype(np.uint8)
        buf = io.BytesIO()
        Image.fromarray(colored).save(buf, 'JPEG', quality=88)
        depth_jpeg = base64.b64encode(buf.getvalue()).decode('ascii')
        base_img = Image.fromarray(rgb).resize(input_size, Image.BILINEAR)
        overlay = Image.blend(base_img, Image.fromarray(colored), alpha=0.45)
        buf = io.BytesIO()
        overlay.save(buf, 'JPEG', quality=88)
        overlay_jpeg = base64.b64encode(buf.getvalue()).decode('ascii')
        return depth_jpeg, overlay_jpeg

    def roi(self, rect):
        """Depth Z-curve and valid-pixel stats inside a rectangle.

        rect = (x0, y0, x1, y1) in normalized [0,1] coordinates of the result
        image. The Z-curve is the per-column mean valid depth inside the rect.
        """
        with self.lock:
            last = dict(self.last) if self.last else None
        if not last:
            raise ValueError('还没有深度结果：先执行一次推理')
        depth = last['depth']
        h, w = depth.shape
        x0 = max(0, min(int(rect[0] * w), w - 1))
        x1 = max(x0 + 1, min(int(rect[2] * w), w))
        y0 = max(0, min(int(rect[1] * h), h - 1))
        y1 = max(y0 + 1, min(int(rect[3] * h), h))
        patch = depth[y0:y1, x0:x1]
        valid = patch[np.isfinite(patch) & (patch > 1e-3)]
        curve = []
        for col in range(patch.shape[1]):
            v = patch[:, col]
            v = v[np.isfinite(v) & (v > 1e-3)]
            curve.append(round(float(v.mean()), 4) if v.size else None)
        stats = depth_stats(patch)
        stats.update({'roi_px': int(patch.size),
                      'valid_ratio': round(float(valid.size) / max(patch.size, 1), 4)})
        return {'curve_x': list(range(x0, x1)), 'curve_z_m': curve,
                'rect_norm': [rect[0], rect[1], rect[2], rect[3]],
                'depth_shape': [h, w], 'stats': stats,
                'color_range_m': last['result']['metadata']['color_range_m'],
                'model_id': last['result']['metadata']['model_id']}


# ---- official 9-metric panel (parsed from existing repro eval logs) ------
# Provenance-bound: numbers are read at request time from the real evaluation
# logs written by the P1 step 3/4 reproduction runs. Nothing here is computed
# on the fly; the local overfit checkpoint has NO such eval and reports N/A.
METRIC_NAMES = ['silog', 'abs_rel', 'log10', 'rms', 'sq_rel', 'log_rms',
                'd1', 'd2', 'd3']
_EVAL_RE = re.compile(r'Computing errors for (\d+) eval samples , post_process:\s+(True|False)')
_CKPT_RE = re.compile(r"Loading checkpoint '(.+?)'")
_ROW_RE = re.compile(r'^\s*(-?[\d.]+\s*,\s*){8}-?[\d.]+\s*$')

EVAL_SOURCES = [
    {'model_id': 'nyu_official_L',
     'log': ROOT / 'outputs/iebins-repro/eval-nyu-L.log',
     'dataset': 'nyu',
     'split': '官方 NYU Depth v2 test split（654 张，eigen_crop）',
     'audit_note': '与官方 README 表逐项偏差 ≤0.0006（P1 step 3 PASSED）。'},
    {'model_id': 'kitti_official_L',
     'log': ROOT / 'outputs/iebins-repro/eval-kitti-L.log',
     'dataset': 'kitti',
     'split': '官方 KITTI Eigen test split（697 帧中 491 帧可评测：官方 '
              'data_depth_annotated 存档本身缺少 206 帧 GT，KITTI-206-AUDIT-PASS）',
     'audit_note': '可评测子集上的复现值；样本数以日志中的 491 为准。'},
]


def _parse_eval_log(path):
    """Return the last 'Computing errors' block of an official eval log."""
    text = Path(path).read_text(encoding='utf-8', errors='replace')
    match = None
    for m in _EVAL_RE.finditer(text):
        match = m
    if not match:
        return None
    tail = text[match.end():].splitlines()
    ckpt = None
    ck = _CKPT_RE.search(text[:match.start()])
    if ck:
        ckpt = ck.group(1)
    row = None
    for line in tail[:6]:
        if _ROW_RE.match(line):
            row = [float(v) for v in line.split(',')]
            break
    if row is None or len(row) != len(METRIC_NAMES):
        return None
    return {'samples': int(match.group(1)), 'post_process': match.group(2) == 'True',
            'checkpoint_path': ckpt,
            'metrics': dict(zip(METRIC_NAMES, row))}


def official_metrics():
    """9 official metrics per checkpoint, bound to split/preprocess/source."""
    rows = []
    for src in EVAL_SOURCES:
        entry = find_checkpoint(src['model_id'])
        parsed = _parse_eval_log(src['log']) if src['log'].exists() else None
        row = {'model_id': src['model_id'], 'label': entry['label'],
               'dataset': src['dataset'], 'encoder': entry['encoder'],
               'max_depth': entry['max_depth'], 'split': src['split'],
               'audit_note': src['audit_note'],
               'checkpoint_sha256': _sha256_file(entry['path'], {}),
               'log': str(src['log']), 'log_exists': src['log'].exists(),
               'status': 'ok' if parsed else 'log_unreadable'}
        if parsed:
            row.update({'samples': parsed['samples'],
                        'post_process': parsed['post_process'],
                        'checkpoint_path_in_log': parsed['checkpoint_path'],
                        'metrics': parsed['metrics'],
                        'source': 'reproduced_eval_log（本机复现评测日志，非官方原文）'})
        rows.append(row)
    return {'rows': rows,
            'local_training': {
                'status': 'not_available',
                'note': '本地过拟合链 checkpoint（P1-TRAIN-SMOKE）只有 loss 曲线，'
                        '从未做过 9 指标评测；此处如实报告 N/A，不得编造数值。',
                'metrics': None},
            'note': '以上 9 指标属于「官方 checkpoint × 官方测试集」的复现评测记录，'
                    '不是本站采集画面上的精度，也不是透明物体深度的科学结论。',
            }


# ---- temporal depth tracking Z(t) ----------------------------------------
# Decoupled from the UI: one worker thread pulls the newest CAM0 frame at a
# fixed interval and runs inference through the existing bounded queue
# (latest-frame-first; a pending older request is superseded, never queued).
# History is bounded and keyed by frame identity (sequence/device_ns), model
# (model_id + sha256), preprocessing (post_process=True, checkpoint input
# size) and ROI. A new segment starts on model/ROI change or after a stale /
# lost-frame gap; no smoothing, masking or interpolation is ever applied.

TRACK_MAX_SAMPLES = 3600
TRACK_MAX_MARKERS = 500


class DepthTracker:
    """Periodic Z(t) sampling of the newest CAM0 frame (display aid only)."""

    def __init__(self, depth_runner, frame_source, contact_source=None):
        self.runner = depth_runner
        self.frame_source = frame_source          # () -> latest CAM0 entry | None
        self.contact_source = contact_source      # () -> bool | None (threshold-only)
        self.lock = threading.Lock()
        self.state = 'stopped'                    # stopped | running | paused
        self.interval_s = 0.5
        self.roi_rect = None                      # normalized [x0,y0,x1,y1] | None
        self.samples = collections.deque(maxlen=TRACK_MAX_SAMPLES)
        self.segments = []                        # {'id','start_t','reason'}
        self.markers = collections.deque(maxlen=TRACK_MAX_MARKERS)
        self.counters = {'ticks': 0, 'inferences': 0, 'stale_skips': 0,
                         'superseded': 0, 'errors': 0}
        self.contact_prev = None
        self.last_error = None
        self._wake = threading.Event()
        self._thread = None
        self.note = ('Z(t) 为固定间隔抽样显示：模型/ROI 变化或画面缺失会开启新段，'
                     '无平滑、无插值、无掩膜；触觉竖线是阈值候选事件，不是接触真值。'
                     '此曲线不是深度精度证据。')

    # -- lifecycle ---------------------------------------------------------
    def start(self, interval_ms=500, roi_rect=None):
        st = self.runner.status()
        if not st.get('loaded'):
            raise ValueError('先加载 checkpoint，再开始 Z(t) 追踪')
        interval_s = min(max(float(interval_ms) / 1000.0, 0.2), 5.0)
        with self.lock:
            self.interval_s = interval_s
            if roi_rect is not None and (not isinstance(roi_rect, list)
                                         or len(roi_rect) != 4):
                raise ValueError('roi_rect must be [x0, y0, x1, y1] or null')
            self.roi_rect = ([float(v) for v in roi_rect]
                             if roi_rect is not None else None)
            self.state = 'running'
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self.status()

    def pause(self):
        with self.lock:
            if self.state == 'running':
                self.state = 'paused'
        return self.status()

    def resume(self):
        with self.lock:
            if self.state == 'paused':
                self.state = 'running'
        return self.status()

    def stop(self):
        with self.lock:
            self.state = 'stopped'
        return self.status()

    def set_roi(self, roi_rect):
        with self.lock:
            if roi_rect is not None and (not isinstance(roi_rect, list)
                                         or len(roi_rect) != 4):
                raise ValueError('roi_rect must be [x0, y0, x1, y1] or null')
            rect = [float(v) for v in roi_rect] if roi_rect is not None else None
            # Config change only; the next sampled data point creates the new
            # segment (reason 'roi_change') — appending here as well would
            # double-count every ROI update.
            self.roi_rect = rect
        return self.status()


    # -- worker ------------------------------------------------------------
    def _loop(self):
        while True:
            with self.lock:
                state = self.state
            if state == 'stopped':
                return
            t0 = time.monotonic()
            if state == 'running':
                try:
                    self._tick()
                except Exception as exc:        # noqa: BLE001 - recorded, loop continues
                    with self.lock:
                        self.last_error = str(exc)
                        self.counters['errors'] += 1
            self._wake.wait(max(0.02, self.interval_s - (time.monotonic() - t0)))
            self._wake.clear()

    def _roi_stats(self, depth, rect):
        h, w = depth.shape
        if rect is not None:
            x0 = max(0, min(int(rect[0] * w), w - 1))
            x1 = max(x0 + 1, min(int(rect[2] * w), w))
            y0 = max(0, min(int(rect[1] * h), h - 1))
            y1 = max(y0 + 1, min(int(rect[3] * h), h))
            patch = depth[y0:y1, x0:x1]
        else:
            patch = depth
        return depth_stats(patch)

    def _tick(self):
        entry = self.frame_source()
        now_mono = time.monotonic()
        if not entry or (now_mono - entry.get('arrived', 0)) > 2.5:
            with self.lock:
                self.counters['stale_skips'] += 1
            return
        out = self.runner.infer_sync(entry['rgb'], post_process=True, timeout=30.0)
        if out.get('superseded'):
            with self.lock:
                self.counters['superseded'] += 1
            return
        with self.runner.lock:
            depth = None if self.runner.last is None else self.runner.last['depth']
        if depth is None:
            return
        sha = out['metadata']['sha256']
        sample = {
            't': round(time.time(), 3),
            'mono': round(now_mono, 3),
            'seq': entry.get('sequence'),
            'device_ns': entry.get('device_ns'),
            'latency_ms': out['metadata']['latency_ms'],
            'model_id': out['metadata']['model_id'],
            'sha256_12': sha[:12] if sha else None,
            'roi': list(self.roi_rect) if self.roi_rect else None,
            'z': self._roi_stats(depth, self.roi_rect),
        }
        with self.lock:
            self.counters['ticks'] += 1
            self.counters['inferences'] += 1
            last = self.samples[-1] if self.samples else None
            reason = None
            if last is None:
                reason = 'start'
            elif (sample['model_id'] != last['model_id']
                  or sample['sha256_12'] != last['sha256_12']):
                reason = 'model_change'
            elif sample['roi'] != last['roi']:
                reason = 'roi_change'
            elif sample['mono'] - last['mono'] > max(3.0, 3 * self.interval_s):
                reason = 'time_gap'
            elif (sample['seq'] is not None and last['seq'] is not None
                  and sample['seq'] < last['seq']):
                reason = 'lost_or_reset_frames'
            if reason:
                self.segments.append({'id': len(self.segments),
                                      'start_t': sample['t'], 'reason': reason})
            sample['segment'] = len(self.segments) - 1
            self.samples.append(sample)
        cand = self.contact_source() if self.contact_source else None
        if cand is not None:
            with self.lock:
                if self.contact_prev is not None and cand != self.contact_prev:
                    self.markers.append({'t': sample['t'],
                                         'kind': ('contact_candidate_on' if cand
                                                  else 'contact_candidate_off')})
                self.contact_prev = cand

    # -- reporting ---------------------------------------------------------
    def status(self, max_points=1200):
        with self.lock:
            samples = list(self.samples)
            segments = list(self.segments)
            markers = list(self.markers)
            counters = dict(self.counters)
            state, interval_s = self.state, self.interval_s
            roi = list(self.roi_rect) if self.roi_rect else None
            last_err = self.last_error
        model = {'model_id': None, 'sha256_12': None}
        if samples:
            model['model_id'] = samples[-1]['model_id']
            model['sha256_12'] = samples[-1]['sha256_12']
        stride = max(1, len(samples) // max_points)
        return {'state': state, 'interval_ms': round(interval_s * 1000),
                'roi_rect': roi, 'total_samples': len(samples),
                'downsampled': stride > 1, 'stride': stride,
                'samples': samples[::stride], 'segments': segments,
                'markers': markers, 'counters': counters,
                'last_error': last_err, 'note': self.note, **model}


# ---- /api/train/status support (honest deferred-training report) ---------
TRAIN_OUT = ROOT / 'outputs/iebins-repro/train_main.out'
TRAIN_PID = ROOT / 'outputs/iebins-repro/train_main.pid'
_STEP_RE = re.compile(
    r'\[epoch\]\[s/s_per_e/gs\]: \[(\d+)\]\[(\d+)/(\d+)/(\d+)\], '
    r'lr: ([0-9.eE+-]+), loss: ([0-9.eE+-]+)')


def _pid_alive(pid):
    """PID liveness on Windows: OpenProcess + QueryFullProcessImageName.

    OpenProcess alone can succeed for a just-terminated PID, so the image path
    query is required; if it fails the PID is treated as dead. POSIX fallback
    is signal 0.
    """
    if os.name == 'nt':
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            size = ctypes.c_ulong(1024)
            buf = ctypes.create_unicode_buffer(size.value)
            ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf,
                                                     ctypes.byref(size))
            return bool(ok) and size.value > 0
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def train_status():
    """Parse the local formal-training log; the run was stopped by user order.

    Status facts only: what the log contains, whether the PID still lives, and
    the standing decision P1-FULL = DEFERRED_LOCAL_PIPELINE_PROVEN_NOT_COMPLETED.
    """
    matches = []
    if TRAIN_OUT.exists():
        for line in TRAIN_OUT.read_text(encoding='utf-8', errors='replace').splitlines():
            m = _STEP_RE.search(line)
            if m:
                matches.append(m)
    steps = [{'epoch': int(m.group(1)), 'step_in_epoch': int(m.group(2)),
              'steps_per_epoch': int(m.group(3)), 'global_step': int(m.group(4)),
              'lr': float(m.group(5)), 'loss': float(m.group(6))} for m in matches]
    pid_alive = False
    if TRAIN_PID.exists():
        try:
            pid_alive = _pid_alive(int(TRAIN_PID.read_text().strip()))
        except (OSError, ValueError):
            pid_alive = False
    stride = max(1, len(steps) // 600)
    curve = [{'gs': s['global_step'], 'loss': s['loss']} for s in steps[::stride]]
    last = steps[-1] if steps else None
    age_s = (time.time() - TRAIN_OUT.stat().st_mtime) if TRAIN_OUT.exists() else None
    return {
        'status': 'deferred_to_cluster',
        'decision': 'P1-FULL = DEFERRED_LOCAL_PIPELINE_PROVEN_NOT_COMPLETED',
        'note': ('正式本地训练已按用户决定停止并推迟到 NTU 集群（实测约 46.5 s/step × 50 epoch ≈ 122 天，'
                 '本地 4080 仅用于证明管线）。train/save/resume 链路由 sanity + overfit resume×3 证明。'
                 '本页只如实展示已存在的日志，绝不将其当作训练结果。'),
        'pid_alive': pid_alive,
        'log': str(TRAIN_OUT),
        'log_age_s': round(age_s, 0) if age_s is not None else None,
        'total_steps_logged': len(steps),
        'last': last,
        'loss_curve': curve,
    }

