"""ChArUco calibration and session-local EXO imports."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4

import av
import cv2
import numpy as np

from ego_capture.qr_sync import (
    AUTO_REPORT,
    MAX_AUTO_MEDIAN_RESIDUAL_MS,
    analyze,
    camera_clock,
    clock_report,
    ego_observations,
    exo_catalog,
    glove_clock,
    read_code,
    recording_time,
    sample_interval,
)
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

    return _fixed_board_profile(size, views, camera, distortion, model, square_mm,
                                imported=imported is not None, intrinsic_rms=intrinsic_rms)


def _fixed_board_profile(size, views, camera, distortion, model, square_mm, *, imported, intrinsic_rms):
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
                                source='imported' if imported else 'video_fit', holdout_rms_px=intrinsic_rms),
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
        automatic = row.get('auto') is True
        time_only = not automatic and row.get('time_only') is True
        manual_table = not (automatic or time_only)
        required = names[:2] if manual_table else names[:1]
        if any(not isinstance(name, str) or name not in videos for name in required) or (
                manual_table and names[2] and (not isinstance(names[2], str) or names[2] not in videos)):
            raise ValueError(f'{role}：请选择原始录像和所需的标定录像')
        if videos[names[0]] in used_videos:
            raise ValueError('每路 EXO 须使用不同的原始视频')
        profile = row.get('profile')
        if not time_only:
            if profile and (not isinstance(profile, str) or profile not in calibration_profiles):
                raise ValueError(f'{role}：请选择可用的标定档案')
            if manual_table and not (profile or names[2]):
                raise ValueError(f'{role}：请选择内参档案或内参录像')
        used_roles.add(role)
        used_videos.add(videos[names[0]])
        if automatic:
            yield dict(role=role, video=names[0], auto=True, profile=profile or None)
        else:
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


def _scan_video(path, board, notify):
    """Use one decode for QR, ChArUco and the original exposure timeline."""
    diagnostics = dict(source_frame_count=0, source_start_pts_s=None, source_end_pts_s=None,
                       scanned_qr_frames=0, scanned_board_frames=0, qr=[], board=[])
    times, observations, views = [], [], []
    size = None
    scan_error = None
    calibration_error = None
    next_qr = next_board = -math.inf
    qr_detector = cv2.QRCodeDetector()
    board_detector = cv2.aruco.CharucoDetector(board)
    last_percent = -1
    try:
        with av.open(str(path)) as container:
            if not container.streams.video:
                raise ValueError('EXO 原片没有视频流')
            stream = container.streams.video[0]
            duration = (float(stream.duration * stream.time_base) if stream.duration is not None and stream.time_base
                        else container.duration / av.time_base if container.duration is not None else None)
            for index, frame in enumerate(container.decode(stream)):
                diagnostics['source_frame_count'] = index + 1
                if frame.pts is None or frame.time_base is None:
                    raise ValueError('视频帧缺少原始 PTS')
                pts = float(frame.pts * frame.time_base)
                if times and pts <= times[-1]:
                    raise ValueError('视频帧 PTS 重复或倒退')
                times.append(pts)
                elapsed = pts - times[0]
                fraction = (elapsed / duration if duration and duration > 0 else
                            (index + 1) / stream.frames if stream.frames else 0.)
                percent = min(99, int(100 * fraction))
                if percent > last_percent:
                    notify(percent)
                    last_percent = percent
                qr_due = pts + 1e-8 >= next_qr
                board_due = pts + 1e-8 >= next_board
                if not (qr_due or board_due):
                    continue
                gray = frame.to_ndarray(format='gray')
                identity = dict(frame=index, source_pts_s=pts)
                if qr_due:
                    next_qr = pts + sample_interval(elapsed, duration)
                    diagnostics['scanned_qr_frames'] += 1
                    code = read_code(qr_detector, gray)
                    if code:
                        observations.append((pts, *code))
                        diagnostics['qr'].append(identity)
                if board_due:
                    next_board = pts + .25
                    diagnostics['scanned_board_frames'] += 1
                    dimensions = (frame.width, frame.height)
                    if size is not None and size != dimensions:
                        calibration_error = '视频中分辨率发生变化'
                    size = size or dimensions
                    corners, ids, _, _ = board_detector.detectBoard(gray)
                    if ids is not None:
                        diagnostics['board'].append(dict(identity, corners=len(ids)))
                        if len(ids) >= 8 and not board.checkCharucoCornersCollinear(ids):
                            obj, img = board.matchImagePoints(corners, ids)
                            views.append((pts, obj.reshape(-1, 3), img.reshape(-1, 2)))
            if not times:
                raise ValueError('EXO 原片没有可解码的录像帧')
    except (OSError, ValueError, av.error.FFmpegError, cv2.error) as exc:
        scan_error = str(exc)
    if times:
        diagnostics.update(source_start_pts_s=times[0], source_end_pts_s=times[-1])
    notify(100)
    return dict(diagnostics=diagnostics, observations=observations, views=views, times=times,
                size=size, error=scan_error, calibration_error=calibration_error)


def _same_board_view(size, views):
    """Check repeated identified corners before trying to fit a camera from a fixed board."""
    tracks = {}
    for _, obj, image in views:
        for position, pixel in zip(obj, image, strict=True):
            tracks.setdefault(tuple(position), []).append(pixel)
    repeated = [pixels for pixels in tracks.values() if len(pixels) >= 4]
    if len(repeated) < 4:
        return False
    travel = [np.ptp(np.percentile(pixels, [10, 90], axis=0), axis=0) / size for pixels in repeated]
    return float(np.median(np.max(travel, axis=1))) < .03


def _auto_calibration(scan, profile_path, square_mm):
    if scan['error']:
        return None, dict(status='scan_failed', reason=scan['error'])
    if scan['calibration_error']:
        return None, dict(status='table_unverified', reason=scan['calibration_error'])
    views = scan['views']
    if not scan['diagnostics']['board']:
        return None, dict(status='board_not_detected', reason='未在抽检帧中识别到桌面标定板')
    if len(views) < 6:
        return None, dict(status='board_insufficient', reason='标定板清晰画面不足 6 帧')
    if len(views) > MAX_VIEWS:
        views = [views[index] for index in np.linspace(0, len(views) - 1, MAX_VIEWS, dtype=int)]
    size = scan['size']
    try:
        if profile_path:
            camera, distortion, model = load_intrinsics(read_profile(profile_path), size)
            intrinsic_rms = None
        elif _same_board_view(size, views):
            raise ValueError('标定板视角基本固定，请在 00 导入这台相机的内参')
        else:
            camera, distortion, model, intrinsic_rms = _estimate_intrinsics(size, views)
    except (OSError, ValueError, cv2.error) as exc:
        return None, dict(status='intrinsics_needed', reason=str(exc))
    try:
        profile = _fixed_board_profile(size, views, camera, distortion, model, square_mm,
                                       imported=bool(profile_path), intrinsic_rms=intrinsic_rms)
    except (ValueError, cv2.error) as exc:
        return None, dict(status='table_unverified', reason=str(exc))
    return profile, profile['validation']


def _session_optical_models(folder, start, duration, progress):
    """Reuse the session's stereo optical reference when already computed."""
    path = folder / 'derived' / AUTO_REPORT
    try:
        saved = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        saved = {}
    if saved.get('session_id') == folder.name and saved.get('schema') == 'ego-qr-clock-v1':
        return [camera for camera in saved.get('cameras', []) if camera['name'] in ('Ego CAM0', 'Ego CAM1')]
    try:
        rows = ego_observations(folder, duration, progress)
    except (OSError, ValueError, cv2.error):
        return []
    models = []
    for index, observations in enumerate(rows):
        try:
            model = camera_clock(observations, f'Ego CAM{index}', 'device_timestamp_seconds', start, duration)
            if model['median_abs_residual_ms'] <= MAX_AUTO_MEDIAN_RESIDUAL_MS:
                models.append(model)
        except ValueError:
            continue
    return models


def _automatic_clock_report(folder, start, duration, cameras, scans, progress):
    head = _session_optical_models(folder, start, duration, progress)
    headset_displays = {camera['display_id'] for camera in head}
    display = next(iter(headset_displays)) if len(headset_displays) == 1 else None
    for camera, scan in zip(cameras, scans, strict=True):
        camera.update(clock=None, clock_status='unavailable')
        if scan['error']:
            camera['clock_reason'] = scan['error']
            continue
        # Independent EXO may start recording before the head-mounted devices.
        visible = [row for row in scan['observations'] if start - 1 <= row[2] <= start + duration + 1]
        if display:
            visible = [row for row in visible if row[1] == display]
        if scan['observations'] and not visible:
            camera['clock_reason'] = '未识别到与当前录制匹配的时间码'
            continue
        try:
            model = camera_clock(visible, camera['role'], 'video_pts_seconds', start, duration)
            if model['median_abs_residual_ms'] > MAX_AUTO_MEDIAN_RESIDUAL_MS:
                raise ValueError('二维码拟合残差过大')
            camera.update(clock=model, clock_status=model['mode'])
            if model['mode'] == 'offset_only':
                camera['clock_reason'] = '有效观察跨度不足，已估计偏移，漂移未验证'
        except ValueError as exc:
            camera['clock_reason'] = str(exc)
    if not display and not headset_displays:
        exo_displays = {camera['clock']['display_id'] for camera in cameras if camera['clock']}
        if len(exo_displays) == 1:
            display = next(iter(exo_displays))
    for camera in cameras:
        if camera['clock'] is not None and camera['clock']['display_id'] != display:
            camera.update(clock=None, clock_status='unavailable', clock_reason='相机拍到不同的二维码页面')
    # A stereo mapping is published only if both models are validated on the same page.
    head = sorted((camera for camera in head if camera['display_id'] == display and
                   camera['mode'] == 'offset_and_drift'), key=lambda camera: camera['name'])
    head = head if len(head) == 2 else []
    try:
        glove = glove_clock(folder, start, duration)
    except (OSError, ValueError, KeyError) as exc:
        glove = dict(source='unavailable', record_count=0, sensor_to_receipt_offset_ms=None, error=str(exc))
    models = head + [camera['clock'] for camera in cameras if camera['clock'] is not None]
    report = clock_report(folder, display, models, glove)
    report.update(method='automatic_imported_exo',
                  outcomes=[dict(name=camera['role'], status=camera['clock_status'],
                                 reason=camera.get('clock_reason'), observations=len(scan['observations']),
                                 residual_ms=(camera['clock'] or {}).get('median_abs_residual_ms'))
                            for camera, scan in zip(cameras, scans, strict=True)])
    return report, head


def _asset_profile(path, row, copied, videos, calibration_profiles, measured_square_mm):
    profile_name = row.get('profile')
    profile_path = calibration_profiles[profile_name] if profile_name else None
    profile = calibrate(copied[videos[row['table_video']]],
                        imported=read_profile(profile_path) if profile_path else None,
                        intrinsic_video=copied[videos[row['intrinsic_video']]] if row.get('intrinsic_video') else None,
                        square_mm=measured_square_mm)
    with av.open(str(path)) as container:
        stream = container.streams.video[0] if container.streams.video else None
        if stream is None or [stream.width, stream.height] != profile['intrinsics']['image_size']:
            raise ValueError(f'{row["role"]} 的正式录像与标定录像分辨率不匹配')
    return profile


def _write_camera_profile(camera, profile, assets, generation):
    profile_file = assets / f'{camera["role"]}-calibration.json'
    dump_json(profile_file, profile)
    camera.update(calibration=f'{generation}/{profile_file.name}',
                  calibration_sha256=hashlib.sha256(profile_file.read_bytes()).hexdigest(),
                  validation=profile['validation'])


def process_exo(folder, rows, video_root, profile_root, minimum_free_bytes, progress, *,
                measured_square_mm=25, video_catalog=None, profile_catalog=None,
                progress_update=None, selection_version=None):
    """Publish a portable snapshot of usable and partial imported camera results."""
    videos = exo_catalog(video_root) if video_catalog is None else video_catalog
    calibration_profiles = profiles(profile_root) if profile_catalog is None else profile_catalog
    rows = list(checked_setup(rows, videos, calibration_profiles))
    automatic = any(row.get('auto') for row in rows)
    board = _board(measured_square_mm) if automatic else None

    def notify(percent, message):
        if progress_update is not None:
            progress_update(percent, message)

    sources = list(dict.fromkeys(videos[row[key]] for row in rows
                                for key in ('video', 'table_video', 'intrinsic_video') if row.get(key)))
    required = sum(path.stat().st_size for path in sources)
    if shutil.disk_usage(folder).free - required < minimum_free_bytes:
        raise ValueError('录制目录剩余空间不足，无法保存 EXO 原始数据')
    report_path = folder / 'derived' / 'exo-session.json'
    report_path.parent.mkdir(exist_ok=True)
    generation = 'exo-' + uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix='.exo-', dir=folder) as tmp:
        tmp = Path(tmp)
        assets = tmp / generation
        assets.mkdir()
        copied = {}
        copied_bytes = 0
        last_copy_percent = -1
        for number, path in enumerate(sources, 1):
            message = f'保存 EXO 原始文件 {number}/{len(sources)}'
            progress(message)
            notify(round(12 * copied_bytes / max(1, required)), message)
            target = assets / f'{number:02d}{path.suffix.lower()}'
            before = path.stat()
            if progress_update is None:
                shutil.copyfile(path, target)
                copied_bytes += before.st_size
            else:
                with path.open('rb') as source, target.open('wb') as destination:
                    while block := source.read(8 * 1024 * 1024):
                        destination.write(block)
                        copied_bytes += len(block)
                        percent = min(12, round(12 * copied_bytes / max(1, required)))
                        if percent > last_copy_percent:
                            notify(percent, message)
                            last_copy_percent = percent
            after = path.stat()
            if ((before.st_size,before.st_mtime_ns) != (after.st_size,after.st_mtime_ns)
                    or target.stat().st_size != before.st_size):
                raise ValueError('导入期间原始视频发生变化，请等文件复制完成后重试')
            copied[path] = target
        cameras, scans = [], []
        for index, row in enumerate(rows):
            role = row['role']
            video = copied[videos[row['video']]]
            camera = dict(role=role, video=f'{generation}/{video.name}', original_name=row['video'])
            cameras.append(camera)
            profile_name = row.get('profile')
            if automatic:
                message = f'EXO {role.removeprefix("exo-")} · 扫描二维码和标定板'
                progress(message)
                scan = _scan_video(video, board, lambda percent: notify(
                    round(12 + 70 * (index + .94 * percent / 100) / len(rows)), message))
                scans.append(scan)
                camera['diagnostics'] = scan['diagnostics']
            if row.get('auto'):
                profile, validation = _auto_calibration(scan, calibration_profiles.get(profile_name),
                                                        measured_square_mm)
                camera.update(imported_profile=profile_name, validation=validation)
                if profile:
                    _write_camera_profile(camera, profile, assets, generation)
            elif row['time_only']:
                camera['validation'] = {'status': 'time_only'}
            else:
                table_video = f'{generation}/{copied[videos[row["table_video"]]].name}'
                intrinsic_video = (f'{generation}/{copied[videos[row["intrinsic_video"]]].name}'
                                   if row.get('intrinsic_video') else None)
                camera.update(table_video=table_video, intrinsic_video=intrinsic_video, imported_profile=profile_name)
                progress(f'{role}：解析桌面标定板')
                try:
                    profile = _asset_profile(video, row, copied, videos, calibration_profiles, measured_square_mm)
                    _write_camera_profile(camera, profile, assets, generation)
                except (OSError, ValueError, av.error.FFmpegError, cv2.error) as exc:
                    if not automatic:
                        raise
                    camera['validation'] = dict(status='table_unverified', reason=str(exc))
        # Use the snapshotted files so timestamps and the stored media cannot diverge.
        _, start, duration = recording_time(folder)
        if automatic:
            notify(84, '拟合相机对时与桌面标定')
            report, head = _automatic_clock_report(folder, start, duration, cameras, scans, progress)
        else:
            report = analyze(folder, [tmp / row['video'] for row in cameras], progress, publish=False)
            if any(camera['mode'] != 'offset_and_drift' for camera in report['cameras']):
                raise ValueError('至少一路相机未找到足够间隔的首尾二维码，无法估计漂移。请核对原始录像')
            head = report['cameras'][:2]
            for camera, clock in zip(cameras, report['cameras'][2:], strict=True):
                clock['name'] = camera['role']
                camera['clock'] = clock

        ego = {}
        if head and (folder / 'derived/frames.jsonl').is_file() and (folder / 'calibration.json').is_file():
            progress('头戴双目：生成对齐帧索引与桌面位姿')
            notify(88, '生成双目对齐索引')
            ego_frames = assets / 'ego-frames.jsonl'
            try:
                coverage = _ego_timeline(folder, ego_frames, head, start, measured_square_mm,
                                         spatial=any(camera.get('calibration') for camera in cameras))
            except (OSError, ValueError, KeyError, cv2.error) as exc:
                if not automatic:
                    raise
                ego['reason'] = str(exc)
                ego_frames.unlink(missing_ok=True)
            else:
                ego.update(frames=f'{generation}/{ego_frames.name}', cameras=coverage,
                           table_pose_convention='p_camera = R_camera_from_table @ p_table + t_camera_from_table_m')
        for index, camera in enumerate(cameras):
            clock = camera.get('clock')
            if clock is None:
                continue
            timeline = assets / f'{camera["role"]}-frames.jsonl'
            progress(f'{camera["role"]}：生成对齐帧索引')
            notify(91 + round(7 * index / len(cameras)), f'生成 EXO {index + 1} 对齐索引')
            times = scans[index]['times'] if automatic else (pts for pts, _ in _video_frames(tmp / camera['video']))
            with timeline.open('w', encoding='utf-8') as handle:
                for frame, pts in enumerate(times):
                    receipt = dict(frame=frame, source_pts_s=pts, session_s=_session_time(clock, start, pts))
                    handle.write(json.dumps(receipt, allow_nan=False) + '\n')
            camera['frames'] = f'{generation}/{timeline.name}'
        complete = all((camera.get('clock') or {}).get('mode') == 'offset_and_drift' and
                       camera['validation']['status'] in ('passed', 'time_only') for camera in cameras)
        # A unique generation preserves any existing result if the new run fails.
        assets.rename(folder / generation)
        result = dict(schema=SESSION, session_id=folder.name, status='processed' if complete else 'partial',
                      cameras=cameras, sync=report, board=BOARD, measured_square_mm=float(measured_square_mm),
                      ego=ego)
        if selection_version is not None:
            result['selection_version'] = selection_version
        for camera in cameras:
            if 'calibration' not in camera:
                continue
            name = f"{camera['role']}-{camera['calibration_sha256'][:12]}.json"
            existing = profile_root / name
            if not existing.exists():
                shutil.copyfile(folder / camera['calibration'], existing)
            camera['reusable_profile'] = name
        dump_json(report_path, result)
        notify(100, 'EXO 整理完成' if complete else 'EXO 整理完成，部分结果待核对')
        return result

def session_assets(folder):
    """Return declared session-relative EXO files for a safe station-to-station import."""
    pointer = folder / 'derived' / 'exo-session.json'
    if not pointer.exists():
        return []
    manifest = json.loads(pointer.read_text(encoding='utf-8-sig'))
    if manifest.get('schema') != SESSION or manifest.get('session_id') != folder.name:
        raise ValueError('EXO 处理记录属于另一条录制')
    ego_frames = manifest.get('ego', {}).get('frames')
    assets = {ego_frames} if ego_frames else set()
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
