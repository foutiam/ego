"""ChArUco calibration and session-local EXO imports."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4

import av
import cv2
import numpy as np

from ego_capture.qr_sync import analyze, exo_catalog
from visuotactile.storage import dump_json

BOARD = {'id': 'ego-charuco-8x5-25mm-v1', 'width': 8, 'height': 5,
         'square_m': .025, 'marker_m': .018}
PROFILE = 'ego-exo-calibration-v1'
SESSION = 'ego-exo-session-v1'
MAX_VIEWS = 40
MAX_VIDEO_SECONDS = 120
MAX_RMS_PX = 3.
ROLE = re.compile(r'exo-[1-9][0-9]?')


def _board(square_mm=25.):
    size = float(square_mm) / 1000
    if not np.isfinite(size) or not .015 <= size <= .040:
        raise ValueError('请输入标定板的实测格长（15–40 mm）')
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    return cv2.aruco.CharucoBoard((BOARD['width'], BOARD['height']), size,
                                 size * BOARD['marker_m'] / BOARD['square_m'], dictionary)


def board_png():
    image = _board().generateImage((2200, 1450), marginSize=100)
    ok, png = cv2.imencode('.png', image)
    if not ok:
        raise ValueError('无法生成标定板')
    # pHYs declares 10 px/mm so print software can preserve the intended dimensions.
    import struct
    import zlib
    physical = b'pHYs' + struct.pack('!IIB', 10000, 10000, 1)
    chunk = struct.pack('!I', 9) + physical + struct.pack('!I', zlib.crc32(physical))
    raw = png.tobytes()
    return raw[:33] + chunk + raw[33:]


def _matrix(value, shape):
    matrix = np.asarray(value, dtype=float)
    if matrix.shape != shape or not np.isfinite(matrix).all():
        raise ValueError('标定矩阵的尺寸或数值不合法')
    return matrix


def load_intrinsics(profile, size):
    intrinsics = profile.get('intrinsics', profile)
    if intrinsics.get('image_size') != list(size):
        raise ValueError('内参分辨率与视频不匹配。请选择对应档案或重新拍内参录像')
    model = intrinsics.get('model')
    if model not in ('pinhole', 'fisheye'):
        raise ValueError('镜头模型须为 pinhole 或 fisheye')
    camera = _matrix(intrinsics.get('K'), (3, 3))
    distortion = np.asarray(intrinsics.get('D'), dtype=float).ravel()
    if (camera[0, 0] <= 0 or camera[1, 1] <= 0 or
            not np.allclose(camera[2], [0, 0, 1]) or
            len(distortion) not in ((4,) if model == 'fisheye' else (4, 5, 8, 12, 14)) or
            not np.isfinite(distortion).all()):
        raise ValueError('内参焦距或畸变系数不合法')
    return camera, distortion, model


def read_profile(path):
    if path.stat().st_size > 128 * 1024:
        raise ValueError('标定档案应为小于 128 KiB 的 JSON')
    record = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(record, dict):
        raise ValueError('标定档案应为 JSON 对象')
    intrinsics = record.get('intrinsics', record)
    if not isinstance(intrinsics, dict):
        raise ValueError('内参字段不合法')
    size = intrinsics.get('image_size')
    if not (isinstance(size, list) and len(size) == 2 and all(type(n) is int and n > 0 for n in size)):
        raise ValueError('标定档案须声明 image_size [width, height]')
    load_intrinsics(record, size)
    return record


def profiles(directory):
    return {p.name: p for p in directory.glob('*.json')
            if not p.is_symlink() and p.is_file() and p.resolve().parent == directory}


def _video_frames(path, step_seconds=None):
    with av.open(str(path)) as container:
        if not container.streams.video:
            raise ValueError('外视角文件没有视频流')
        next_time = -np.inf
        previous = -np.inf
        start = None
        for frame in container.decode(container.streams.video[0]):
            if frame.pts is None or frame.time_base is None:
                raise ValueError('视频帧缺少原始 PTS')
            pts = float(frame.pts * frame.time_base)
            if pts <= previous:
                raise ValueError('视频帧 PTS 重复或倒退')
            previous = pts
            if pts < next_time:
                continue
            if start is None:
                start = pts
            if step_seconds is not None and pts - start > MAX_VIDEO_SECONDS:
                break
            next_time = pts + (step_seconds or 0.)
            yield pts, frame


def _views(path, board):
    detector = cv2.aruco.CharucoDetector(board)
    size, output = None, []
    for pts, frame in _video_frames(path, .25):
        dimensions = (frame.width, frame.height)
        if size is not None and dimensions != size:
            raise ValueError('标定视频中分辨率发生变化')
        size = dimensions
        corners, ids, _, _ = detector.detectBoard(frame.to_ndarray(format='gray'))
        if ids is None or len(ids) < 8 or board.checkCharucoCornersCollinear(ids):
            continue
        obj, img = board.matchImagePoints(corners, ids)
        output.append((pts, obj.reshape(-1, 3), img.reshape(-1, 2)))
        if len(output) == MAX_VIEWS:
            break
    if len(output) < 6:
        raise ValueError('可识别的标定板画面不足。请让整块板清晰入镜并补拍')
    return size, output


def _fit_pose(obj, image, camera, distortion, model):
    pixels, used_dist = image, distortion
    if model == 'fisheye':
        pixels = cv2.fisheye.undistortPoints(image.reshape(-1, 1, 2), camera, distortion, P=camera)
        used_dist = None
    ok, rvec, tvec = cv2.solvePnP(obj, pixels, camera, used_dist, flags=cv2.SOLVEPNP_ITERATIVE)
    if not ok:
        raise ValueError('无法求出标定板位姿')
    rotation = cv2.Rodrigues(rvec)[0]
    if np.any((obj @ rotation.T + tvec.ravel())[:, 2] <= 0):
        raise ValueError('标定板位姿不在相机前方')
    return rvec, tvec


def _errors(obj, image, rvec, tvec, camera, distortion, model):
    if model == 'fisheye':
        projected = cv2.fisheye.projectPoints(obj.reshape(-1, 1, 3), rvec, tvec, camera, distortion)[0]
    else:
        projected = cv2.projectPoints(obj, rvec, tvec, camera, distortion)[0]
    return np.linalg.norm(projected.reshape(-1, 2) - image, axis=1)


def _estimate_intrinsics(size, views):
    if len(views) < 12:
        raise ValueError('内参录像至少需要 12 个看清标定板的画面，请从多个角度补拍')
    train = [row for idx, row in enumerate(views) if idx % 4 != 3]
    held = [row for idx, row in enumerate(views) if idx % 4 == 3]
    _, camera, distortion, rotations, _ = cv2.calibrateCamera(
        [obj for _, obj, _ in train], [img for _, _, img in train], size, None, None)
    directions = np.array([cv2.Rodrigues(r)[0][:, 2] for r in rotations])
    centers = np.array([img.mean(axis=0) for _, _, img in train]) / size
    if (np.max(np.ptp(directions[:, :2], axis=0)) < .18 or
            np.max(np.ptp(centers, axis=0)) < .15):
        raise ValueError('内参录像缺少不同倾角或画面位置。请移动、倾斜标定板后补拍')
    residuals = np.concatenate([
        _errors(obj, img, *_fit_pose(obj, img, camera, distortion, 'pinhole'),
                camera, distortion, 'pinhole') for _, obj, img in held])
    rms = float(np.sqrt(np.mean(residuals ** 2)))
    if not np.isfinite(rms) or rms > MAX_RMS_PX:
        raise ValueError(f'内参留出画面重投影误差 {rms:.2f} px，请检查板尺寸、清晰度或导入镜头档案')
    return camera, distortion.ravel(), 'pinhole', rms


def calibrate(table_video, *, imported=None, intrinsic_video=None, square_mm=25.):
    board = _board(square_mm)
    size, views = _views(table_video, board)
    intrinsic_rms = None
    if imported is not None:
        camera, distortion, model = load_intrinsics(imported, size)
    elif intrinsic_video is not None:
        capture_size, varied = _views(intrinsic_video, board)
        if capture_size != size:
            raise ValueError('内参录像与桌面标定录像的分辨率不同')
        camera, distortion, model, intrinsic_rms = _estimate_intrinsics(size, varied)
    else:
        raise ValueError('请选择匹配的内参档案，或另外录制一段移动和倾斜标定板的录像')

    # Fixed EXO: solve once for the static table and score on different exposures.
    train = [row for idx, row in enumerate(views) if idx % 3 != 2]
    held = [row for idx, row in enumerate(views) if idx % 3 == 2]
    obj = np.concatenate([row[1] for row in train])
    img = np.concatenate([row[2] for row in train])
    rvec, tvec = _fit_pose(obj, img, camera, distortion, model)
    errors = np.concatenate([_errors(obj, img, rvec, tvec, camera, distortion, model)
                             for _, obj, img in held])
    rms = float(np.sqrt(np.mean(errors ** 2)))
    if not np.isfinite(rms) or rms > MAX_RMS_PX:
        raise ValueError(f'桌面标定留出画面误差 {rms:.2f} px。请固定机位和标定板、核对内参后重拍')
    rotation = cv2.Rodrigues(rvec)[0]
    translation = tvec.ravel()
    return dict(schema=PROFILE, board=dict(BOARD, measured_square_mm=float(square_mm)),
                intrinsics=dict(model=model, image_size=list(size), K=camera.tolist(), D=distortion.tolist(),
                                source='imported' if imported is not None else 'video_fit', holdout_rms_px=intrinsic_rms),
                extrinsics=dict(convention='p_camera = R_camera_from_table @ p_table + t_camera_from_table_m',
                                R_camera_from_table=rotation.tolist(), t_camera_from_table_m=translation.tolist(),
                                camera_position_in_table_m=(-rotation.T @ translation).tolist()),
                validation=dict(status='passed', fit_views=len(train), holdout_views=len(held),
                                holdout_rms_px=rms, holdout_p95_px=float(np.percentile(errors, 95))))


def checked_setup(rows, videos, calibration_profiles):
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
        raise ValueError('请选择 1–8 路 EXO')
    used_roles, used_videos = set(), set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('EXO 记录格式不合法')
        role = row.get('role')
        names = [row.get(k) for k in ('video', 'table_video', 'intrinsic_video')]
        if not isinstance(role, str) or not ROLE.fullmatch(role) or role in used_roles:
            raise ValueError('EXO 编号无效或重复')
        time_only = row.get('time_only') is True
        required = names[:1] if time_only else names[:2]
        if any(not isinstance(name, str) or name not in videos for name in required) or (
                not time_only and names[2] and (not isinstance(names[2], str) or names[2] not in videos)):
            raise ValueError(f'{role}：请选择原始录像和所需的标定录像')
        if videos[names[0]] in used_videos:
            raise ValueError('每路 EXO 须使用不同的原始视频')
        profile = row.get('profile')
        if not time_only:
            if profile and (not isinstance(profile, str) or profile not in calibration_profiles):
                raise ValueError(f'{role}：请选择可用的标定档案')
            if not (profile or names[2]):
                raise ValueError(f'{role}：请选择内参档案或内参录像')
        used_roles.add(role)
        used_videos.add(videos[names[0]])
        yield dict(role=role, video=names[0], time_only=time_only,
                   table_video=None if time_only else names[1],
                   intrinsic_video=None if time_only or profile else names[2],
                   profile=None if time_only else profile)


def _session_time(clock, origin_host_s, source_s):
    return (clock['host_mono_at_origin_s'] - origin_host_s
            + clock['scale'] * (source_s - clock['source_origin_s']))


def _ego_timeline(folder, destination, clocks, start_host_s, square_mm, *, spatial=True):
    """Map each exposure to the station clock and solve the moving camera where visible."""
    report = json.loads((folder / 'calibration.json').read_text(encoding='utf-8-sig'))
    reported = report.get('reported') or {}
    board = _board(square_mm)
    detector = cv2.aruco.CharucoDetector(board)
    counts = [{'frames': 0, 'table_pose_frames': 0} for _ in range(2)]
    index = folder / 'derived' / 'frames.jsonl'
    with index.open(encoding='utf-8-sig') as frames, destination.open('w', encoding='utf-8') as handle:
        for line in frames:
            frame = json.loads(line)
            eye = frame['cam']
            if eye not in (0, 1) or not re.fullmatch(rf'cam{eye}/[0-9]+[.]jpg', frame['image']):
                continue
            stamp = int(frame['device_ns'])
            entry = dict(camera=eye, device_ns=str(stamp), image='derived/' + frame['image'],
                         session_s=_session_time(clocks[eye], start_host_s, stamp / 1e9),
                         table_pose=None, table_pose_status='missing_device_intrinsics')
            counts[eye]['frames'] += 1
            lens = reported.get(f'/ego/camera/{eye}/intrinsics') or {}
            if (spatial and 'kannala' in str(lens.get('distortion_model', '')).lower()
                    and len(lens.get('K') or ()) == 9 and len(lens.get('D') or ()) == 4
                    and lens.get('width', 0) > 0 and lens.get('height', 0) > 0):
                entry['table_pose_status'] = 'board_not_verified'
                image = folder / entry['image']
                pixels = cv2.imdecode(np.fromfile(image,dtype=np.uint8),cv2.IMREAD_GRAYSCALE) if image.is_file() else None
                if pixels is not None:
                    corners, ids, _, _ = detector.detectBoard(pixels)
                    if ids is not None and len(ids) >= 10 and not board.checkCharucoCornersCollinear(ids):
                        obj, img = board.matchImagePoints(corners, ids)
                        obj, img = obj.reshape(-1, 3), img.reshape(-1, 2)
                        camera = np.asarray(lens['K'], float).reshape(3, 3)
                        scale = np.diag([pixels.shape[1]/lens['width'], pixels.shape[0]/lens['height'], 1.])
                        camera = scale @ camera
                        distortion = np.asarray(lens['D'], float)
                        keep = np.arange(len(obj)) % 4 != 3
                        try:
                            rv, tv = _fit_pose(obj[keep],img[keep],camera,distortion,'fisheye')
                            error = _errors(obj[~keep],img[~keep],rv,tv,camera,distortion,'fisheye')
                            rms = float(np.sqrt(np.mean(error ** 2)))
                            if np.isfinite(rms) and rms <= MAX_RMS_PX:
                                entry.update(table_pose_status='board_supported',
                                             table_pose=dict(R_camera_from_table=cv2.Rodrigues(rv)[0].tolist(),
                                                             t_camera_from_table_m=tv.ravel().tolist(),
                                                             holdout_rms_px=rms))
                                counts[eye]['table_pose_frames'] += 1
                        except (ValueError, cv2.error):
                            pass
            handle.write(json.dumps(entry, allow_nan=False) + '\n')
    return counts


def process_exo(folder, rows, video_root, profile_root, minimum_free_bytes, progress, *,
                measured_square_mm=25, video_catalog=None, profile_catalog=None):
    """Publish a portable EXO snapshot only after calibration and synchronization pass."""
    videos = exo_catalog(video_root) if video_catalog is None else video_catalog
    calibration_profiles = profiles(profile_root) if profile_catalog is None else profile_catalog
    rows = list(checked_setup(rows, videos, calibration_profiles))
    sources = list(dict.fromkeys(videos[row[key]] for row in rows
                                for key in ('video', 'table_video', 'intrinsic_video') if row.get(key)))
    required = sum(path.stat().st_size for path in sources)
    if shutil.disk_usage(folder).free - required < minimum_free_bytes:
        raise ValueError('录制目录剩余空间不足，无法保存 EXO 原始数据')
    report_path = folder / 'derived' / 'exo-session.json'
    generation = 'exo-' + uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix='.exo-', dir=folder) as tmp:
        tmp = Path(tmp)
        assets = tmp / generation
        assets.mkdir()
        copied = {}
        for number, path in enumerate(sources, 1):
            progress(f'保存 EXO 原始文件 {number}/{len(sources)}')
            target = assets / f'{number:02d}{path.suffix.lower()}'
            before = path.stat()
            shutil.copyfile(path, target)
            after = path.stat()
            if ((before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns)
                    or target.stat().st_size != before.st_size):
                raise ValueError('导入期间原始视频发生变化，请等文件复制完成后重试')
            copied[path] = target
        cameras = []
        for row in rows:
            role = row['role']
            video = copied[videos[row['video']]]
            if row['time_only']:
                cameras.append(dict(role=role, video=f'{generation}/{video.name}', original_name=row['video'],
                                    validation={'status': 'time_only'}))
                continue
            profile_name = row.get('profile')
            profile_path = calibration_profiles[profile_name] if profile_name else None
            progress(f'{role}：解析桌面标定板')
            profile = calibrate(copied[videos[row['table_video']]],
                                imported=read_profile(profile_path) if profile_path else None,
                                intrinsic_video=copied[videos[row['intrinsic_video']]] if row.get('intrinsic_video') else None,
                                square_mm=measured_square_mm)
            profile_file = assets / f'{role}-calibration.json'
            dump_json(profile_file, profile)
            with av.open(str(video)) as container:
                stream = container.streams.video[0] if container.streams.video else None
                if stream is None or [stream.width, stream.height] != profile['intrinsics']['image_size']:
                    raise ValueError(f'{role} 的正式录像与标定录像分辨率不匹配')
            cameras.append(dict(role=role, video=f'{generation}/{video.name}', original_name=row['video'],
                                table_video=f'{generation}/{copied[videos[row["table_video"]]].name}',
                                intrinsic_video=(f'{generation}/{copied[videos[row["intrinsic_video"]]].name}'
                                                 if row.get('intrinsic_video') else None),
                                calibration=f'{generation}/{profile_file.name}',
                                imported_profile=profile_name,
                                calibration_sha256=hashlib.sha256(profile_file.read_bytes()).hexdigest(),
                                validation=profile['validation']))
        # Use the snapshotted files so timestamps and the stored media cannot diverge.
        report = analyze(folder, [tmp / row['video'] for row in cameras], progress, publish=False)
        if any(camera['mode'] != 'offset_and_drift' for camera in report['cameras']):
            raise ValueError('至少一路相机未找到足够间隔的首尾二维码，无法估计漂移。请核对原始录像')
        start = json.loads((folder / 'metadata.json').read_text(encoding='utf-8'))['start_mono_ns'] / 1e9
        progress('头戴双目：生成对齐帧索引与桌面位姿')
        ego_frames = assets / 'ego-frames.jsonl'
        ego_coverage = _ego_timeline(folder, ego_frames, report['cameras'][:2], start, measured_square_mm,
                                     spatial=any(camera.get('calibration') for camera in cameras))
        for camera, clock in zip(cameras, report['cameras'][2:], strict=True):
            clock['name'] = camera['role']
            timeline = assets / f'{camera["role"]}-frames.jsonl'
            progress(f'{camera["role"]}：生成对齐帧索引')
            with timeline.open('w', encoding='utf-8') as handle:
                for index, (pts, _) in enumerate(_video_frames(tmp / camera['video'])):
                    receipt = dict(frame=index, source_pts_s=pts, session_s=_session_time(clock,start,pts))
                    handle.write(json.dumps(receipt, allow_nan=False) + '\n')
            camera.update(clock=clock, frames=f'{generation}/{timeline.name}')
        # A unique generation preserves any existing result if the new run fails.
        assets.rename(folder / generation)
        result = dict(schema=SESSION, session_id=folder.name, status='processed', cameras=cameras,
                      sync=report, board=BOARD, measured_square_mm=float(measured_square_mm))
        result['ego'] = dict(frames=f'{generation}/{ego_frames.name}', cameras=ego_coverage,
                             table_pose_convention='p_camera = R_camera_from_table @ p_table + t_camera_from_table_m')
        for camera in cameras:
            if 'calibration' not in camera:
                continue
            name = f"{camera['role']}-{camera['calibration_sha256'][:12]}.json"
            existing = profile_root / name
            if not existing.exists():
                shutil.copyfile(folder / camera['calibration'], existing)
            camera['reusable_profile'] = name
        dump_json(report_path, result)
        return result

def session_assets(folder):
    """Return declared session-relative EXO files for a safe station-to-station import."""
    pointer = folder / 'derived' / 'exo-session.json'
    if not pointer.exists():
        return []
    manifest = json.loads(pointer.read_text(encoding='utf-8-sig'))
    if manifest.get('schema') != SESSION or manifest.get('session_id') != folder.name:
        raise ValueError('EXO 处理记录属于另一条录制')
    assets = {manifest['ego']['frames']}
    for camera in manifest['cameras']:
        assets.update(camera[key] for key in ('video','table_video','intrinsic_video','calibration','frames')
                      if camera.get(key))
    verified = []
    for relative in sorted(assets):
        if not isinstance(relative,str) or not re.fullmatch(r'exo-[0-9a-f]{12}/[a-zA-Z0-9_-]+[.](mp4|mov|m4v|json|jsonl)',relative):
            raise ValueError('EXO 资产路径格式无效')
        asset = folder / relative
        if asset != asset.resolve() or not asset.is_file():
            raise ValueError(f'缺少 EXO 录制资产 {relative}')
        verified.append(asset)
    for camera in manifest['cameras']:
        if camera.get('calibration') and hashlib.sha256((folder/camera['calibration']).read_bytes()).hexdigest() != camera['calibration_sha256']:
            raise ValueError('EXO 标定档案与处理记录不匹配')
    return [pointer, *verified]
