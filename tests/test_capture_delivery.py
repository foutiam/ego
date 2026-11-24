"""Exercise ChArUco, QR and portable session metadata with generated video."""
import json
from pathlib import Path
import shutil
import sys
from fractions import Fraction

import av
import cv2
import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / name) for name in ('code', 'scripts')]
from ego_capture import exo_calibration as exo


def save(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def video(path, frames, rate=4):
    with av.open(str(path), 'w') as container:
        stream = container.add_stream('mpeg4', rate=rate)
        stream.width, stream.height = 640, 480
        stream.pix_fmt = 'yuv420p'
        for position, image in enumerate(frames):
            frame = av.VideoFrame.from_ndarray(cv2.cvtColor(image, cv2.COLOR_GRAY2BGR), format='bgr24')
            frame.pts, frame.time_base = position, Fraction(1, rate)
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


def board_view(board, K, rotation, position):
    inner = 8 * .025, 5 * .025
    world = np.array([[0, 0, 0], [inner[0], 0, 0], [inner[0], inner[1], 0], [0, inner[1], 0]])
    points = cv2.projectPoints(world, rotation, position, K, np.zeros(5))[0].reshape(4, 2)
    source = np.float32([[0, 0], [board.shape[1], 0], [board.shape[1], board.shape[0]], [0, board.shape[0]]])
    homography = cv2.getPerspectiveTransform(source, points.astype(np.float32))
    return cv2.warpPerspective(board, homography, (640, 480), borderValue=255)


def clock_image(seconds):
    image = np.full((480, 640), 255, np.uint8)
    if seconds < 3.5 or seconds >= 15.:
        data = f'EGO1:ab0123ef:{int((100+seconds)*1000)}:4'
        code = cv2.QRCodeEncoder_create().encode(data)
        code = cv2.copyMakeBorder(code, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
        tiles = cv2.resize(code, (260, 260), interpolation=cv2.INTER_NEAREST)
        image[110:370, 190:450] = tiles
    return image


def fixture(root):
    cv2.setNumThreads(1)
    videos, profiles = root / 'inbox', root / 'profiles'
    videos.mkdir(parents=True)
    profiles.mkdir()
    K = np.array([[660., 0, 320], [0, 655, 240], [0, 0, 1]])
    save(profiles / 'lens.json', dict(image_size=[640, 480], model='pinhole', K=K.tolist(), D=[0]*5))
    board = exo._board().generateImage((800, 500))
    pose_r = np.array([.12, -.09, .08])
    pose_t = np.array([-.1, -.05, .6])
    table = board_view(board, K, pose_r, pose_t)
    video(videos / 'table.mp4', [table]*24)
    varied = [board_view(board, K, np.array([.31*np.sin(k), .48*np.cos(.8*k), .12*np.sin(.4*k)]),
                         np.array([-.09+.10*np.sin(.7*k), -.055+.082*np.cos(1.1*k), .53+.08*np.cos(k)]))
              for k in range(32)]
    video(videos / 'varied.mp4', varied)
    clocks = [clock_image(k/4) for k in range(73)]
    video(videos / 'exo1.mp4', clocks)
    for i in (2, 3):
        shutil.copyfile(videos / 'exo1.mp4', videos / f'exo{i}.mp4')
    return videos, profiles, clocks, pose_t, pose_r


def recording(folder, clocks):
    derived = folder / 'derived'
    derived.mkdir(parents=True)
    save(folder / 'metadata.json', dict(session_id=folder.name, status='recorded',
                                       start_mono_ns=100_000_000_000, duration_s=18.))
    save(folder / 'calibration.json', {'reported': {}})
    np.savez(derived / 'glove_signals.npz', time_s=np.arange(0, 18, .125))
    with (derived / 'frames.jsonl').open('w', encoding='utf-8') as rows:
        for eye in (0, 1):
            (derived / f'cam{eye}').mkdir()
            for index, image in enumerate(clocks):
                relative = f'cam{eye}/{index}.jpg'
                cv2.imencode('.jpg', image)[1].tofile(derived / relative)
                stamp = 70_000_000_000 + index * 250_000_000 + eye * 100_000_000
                row = dict(cam=eye, t_s=index/4, device_ns=stamp, image=relative)
                rows.write(json.dumps(row) + '\n')


def test_fixed_exo_2_and_3(tmp_path):
    videos, profiles, clocks, expected, _ = fixture(tmp_path)
    automatic = exo.calibrate(videos/'table.mp4', intrinsic_video=videos/'varied.mp4')
    assert automatic['intrinsics']['source'] == 'video_fit'
    assert automatic['validation']['holdout_rms_px'] < 2
    for count in (2, 3):
        folder = tmp_path / f'session-{count}'
        recording(folder, clocks)
        requests = [dict(role=f'exo-{i}', video=f'exo{i}.mp4', table_video='table.mp4', profile='lens.json')
                    for i in range(1,count+1)]
        result = exo.process_exo(folder, requests, videos, profiles, 0, lambda _: None)
        assert len(result['cameras']) == count and len(result['sync']['cameras']) == count+2
        assert result['sync']['glove']['sensor_to_receipt_offset_ms'] is None
        assert result['ego']['cameras'][0]['table_pose_frames'] == 0
        assert (folder / result['ego']['frames']).is_file()
        for row in result['cameras']:
            profile = json.loads((folder / row['calibration']).read_text(encoding='utf-8'))
            assert np.linalg.norm(np.asarray(profile['extrinsics']['t_camera_from_table_m'])-expected) < .012
            assert (folder / row['video']).is_file()
            assert (profiles / row['reusable_profile']).is_file()
            indexed=[json.loads(line) for line in (folder/row['frames']).read_text().splitlines()]
            assert len(indexed) == len(clocks)
            assert abs(indexed[14]['session_s'] - 3.5) < .03


if __name__ == '__main__':
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        test_fixed_exo_2_and_3(Path(folder))
    print('PASS: imported and fitted intrinsics, 2/3 EXO QR clocks and relocatable session indexes')
