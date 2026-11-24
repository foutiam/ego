"""Offline depth-eval comparison gallery for the capture station.

Builds per-dataset RGB / GT / prediction / error-map panels from the official
test splits already on disk (the same images and preprocessing used by the
P1 step 3/4 reproduction evaluations), using the loaded official checkpoint.
This is a qualitative display aid; the 9-metric panel (depth_model.
official_metrics) remains the quantitative provenance-bound record.

Provenance and semantics:
  * preprocessing replicates the iebins eval dataloader exactly: NYU 480x640
    native (no resize), KITTI bottom-center 352x1216 kb-crop (no resize);
  * GT scale: NYU png/1000, KITTI png/256;
  * prediction stays at model-input resolution (identical to official eval);
  * error map is |pred-gt|/gt on valid GT pixels only (1e-3..max_depth),
    fixed 0-0.5 color range;
  * images are chosen deterministically (evenly spaced split-file indices),
    never cherry-picked; skipped entries are recorded in the manifest.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import numpy as np

from ego_capture import depth_model as dm

UPSTREAM = dm.ROOT / 'code/iebins-upstream'
GALLERY_ROOT = dm.ROOT / 'outputs/capture-station/depth_eval_gallery'
PANEL_SAMPLES = 4

GALLERY_SOURCES = {
    'nyu': {
        'model_id': 'nyu_official_L',
        'split_file': UPSTREAM / 'data_splits/nyudepthv2_test_files_with_gt.txt',
        'data_root': dm.ROOT / 'outputs/iebins-repro/datasets/nyu/official_splits/test',
        'kb_crop': False,
        'gt_scale': 1000.0,
        'max_depth': 10.0,
    },
    'kitti': {
        'model_id': 'kitti_official_L',
        'split_file': UPSTREAM / 'data_splits/eigen_test_files_with_gt.txt',
        'data_root': dm.ROOT / 'outputs/iebins-repro/datasets/kitti',
        'kb_crop': True,
        'gt_scale': 256.0,
        'max_depth': 80.0,
    },
}


def _kb_crop(img):
    h, w = img.shape[:2]
    top = int(h - 352)
    left = int((w - 1216) / 2)
    return img[top:top + 352, left:left + 1216]


def _read_gt(path, scale):
    from PIL import Image
    return np.asarray(Image.open(path), dtype=np.float32) / scale


def _tensorize(rgb):
    """Official eval preprocessing (no resize: fed at native size)."""
    import torch
    from PIL import Image
    from torchvision import transforms as T
    x = np.asarray(Image.fromarray(rgb), dtype=np.float32) / 255.0
    x = torch.from_numpy(x.transpose((2, 0, 1)))
    x = T.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])(x)
    return x.unsqueeze(0)


def _colorize(values, cmap_name, vmax):
    import matplotlib
    matplotlib.use('Agg')
    from matplotlib import cm
    try:
        cmap = matplotlib.colormaps[cmap_name]
    except AttributeError:                          # matplotlib < 3.6
        cmap = cm.get_cmap(cmap_name)
    norm = np.clip(values / max(vmax, 1e-6), 0.0, 1.0)
    return (cmap(norm)[..., :3] * 255).astype(np.uint8)


def build_gallery(dataset='nyu', runner=None, progress=lambda text: None):
    """Generate the comparison panel + manifest for one official test split."""
    src = GALLERY_SOURCES.get(dataset)
    if not src:
        raise ValueError('Unknown gallery dataset: %s' % dataset)
    out_dir = GALLERY_ROOT / dataset
    out_dir.mkdir(parents=True, exist_ok=True)
    if not src['split_file'].exists():
        raise ValueError('官方 split 文件不存在：%s' % src['split_file'])

    entries = []
    for line in src['split_file'].read_text().splitlines():
        parts = line.split()
        if len(parts) >= 2:
            entries.append((parts[0], parts[1]))
    if not entries:
        raise ValueError('split 文件为空：%s' % src['split_file'])

    if runner is None:
        runner = dm.DepthRunner()
    status = runner.load(src['model_id'])
    if not status.get('loaded'):
        raise ValueError('checkpoint 加载失败：%s' % src['model_id'])

    import torch
    from iebins.utils import flip_lr, post_process_depth   # noqa: E402
    from PIL import Image                                   # noqa: E402
    device = next(runner.model.parameters()).device

    stride = max(1, len(entries) // PANEL_SAMPLES)
    panels, skipped = [], []
    for start_idx in range(0, len(entries), stride):
        if len(panels) >= PANEL_SAMPLES:
            break
        # Deterministic selection: evenly spaced start index; if that entry is
        # absent on disk (partial local extraction), advance to the next
        # split entry — never random, never cherry-picked; every skip is
        # recorded in the manifest.
        idx = start_idx
        while idx < len(entries):
            rgb_rel, gt_rel = entries[idx]
            rgb_path = src['data_root'] / rgb_rel
            # KITTI gt_rel lacks the capture-date directory; upstream eval
            # dataloader prepends rgb_rel's first path component (dataloader
            # line: os.path.join(gt_path, sample[0].split('/')[0], sample[1])).
            gt_path = (src['data_root'] / rgb_rel.split('/')[0] / gt_rel
                       if src['kb_crop'] else src['data_root'] / gt_rel)
            if rgb_path.exists() and gt_path.exists():
                break
            if not rgb_path.exists() and not gt_path.exists():
                reason = 'rgb_and_gt_missing'
            elif not rgb_path.exists():
                reason = 'rgb_missing'
            else:
                reason = 'gt_missing'
            skipped.append({'index': idx, 'rgb': rgb_rel, 'reason': reason})
            idx += 1
        if idx >= len(entries):
            break
        progress('%s %d/%d' % (dataset, len(panels) + 1, PANEL_SAMPLES))
        rgb = np.asarray(Image.open(rgb_path).convert('RGB'))
        if src['kb_crop']:
            rgb = _kb_crop(rgb)
        gt = _read_gt(gt_path, src['gt_scale'])
        if src['kb_crop']:
            gt = _kb_crop(gt)

        x = _tensorize(rgb).to(device)
        t0 = time.time()
        with torch.no_grad():
            pred, _, _ = runner.model(x)
            pred_f, _, _ = runner.model(flip_lr(x))
            depth = post_process_depth(pred[-1], pred_f[-1])
            if device.type == 'cuda':
                torch.cuda.synchronize()
        latency_ms = (time.time() - t0) * 1000.0
        pred = np.clip(depth.squeeze().float().cpu().numpy(), 1e-3, src['max_depth'])

        valid = np.isfinite(gt) & (gt > 1e-3) & (gt < src['max_depth'])
        if not valid.any():
            skipped.append({'index': idx, 'rgb': rgb_rel, 'reason': 'no_valid_gt'})
            continue
        absrel = np.zeros_like(pred)
        absrel[valid] = np.abs(pred[valid] - gt[valid]) / gt[valid]

        name = Path(rgb_rel).stem.replace('/', '_') or ('frame_%d' % idx)
        panel_name = 'panel_%s.jpg' % name[:60]
        cmap_name = 'jet' if dataset == 'nyu' else 'magma'
        gt_c = _colorize(np.nan_to_num(gt), cmap_name, src['max_depth'])
        gt_c[~valid] = 0
        pred_c = _colorize(pred, cmap_name, src['max_depth'])
        err_c = _colorize(absrel, 'inferno', 0.5)
        err_c[~valid] = 0
        cols = [rgb, gt_c, pred_c, err_c]
        h = min(c.shape[0] for c in cols)
        w = min(c.shape[1] for c in cols)
        cols = [c[:h, :w] for c in cols]
        panel = np.concatenate(cols, axis=1)
        Image.fromarray(panel).save(out_dir / panel_name, 'JPEG', quality=88)

        panels.append({'index': idx, 'rgb': rgb_rel, 'gt': gt_rel,
                       'panel': panel_name,
                       'rgb_sha256': _sha256(rgb_path),
                       'gt_valid_px': int(valid.sum()),
                       'pred_median_m': round(float(np.median(pred[valid])), 4),
                       'gt_median_m': round(float(np.median(gt[valid])), 4),
                       'mean_absrel_on_gt': round(float(absrel[valid].mean()), 4),
                       'latency_ms': round(latency_ms, 1)})

    manifest = {
        'dataset': dataset, 'model_id': src['model_id'],
        'label': dm.find_checkpoint(src['model_id'])['label'],
        'checkpoint_sha256': dm._sha256_file(
            dm.find_checkpoint(src['model_id'])['path'], {}),
        'preprocess': ('kb_crop 352x1216 bottom-center, GT png/256'
                       if src['kb_crop'] else
                       'native 480x640, GT png/1000'),
        'split_file': str(src['split_file']),
        'split_entries': len(entries), 'panels': panels, 'skipped': skipped,
        'built_at': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'note': ('定性对照图：RGB / GT / 预测 / 误差图依次排列；误差图固定 0-0.5 '
                 'AbsRel 色标，黑色=无 GT。图选自官方 split 的等间隔抽样（非挑选），'
                 '仅作显示参考，定量结论以 9 指标面板的复现评测日志为准。'),
    }
    (out_dir / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1), encoding='utf-8')
    return manifest


def gallery_report():
    out = {'datasets': []}
    for dataset in GALLERY_SOURCES:
        mpath = GALLERY_ROOT / dataset / 'manifest.json'
        if mpath.exists():
            try:
                out['datasets'].append(json.loads(mpath.read_text(encoding='utf-8')))
            except (ValueError, OSError):
                out['datasets'].append({'dataset': dataset,
                                        'status': 'manifest_unreadable'})
        else:
            out['datasets'].append({'dataset': dataset, 'status': 'not_built',
                                    'note': '尚未生成：离线运行 build_gallery（GPU）'})
    return out


def _sha256(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()

