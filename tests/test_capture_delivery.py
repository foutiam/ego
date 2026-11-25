"""Exercise ChArUco, QR and portable session metadata with generated video."""
import json
from pathlib import Path
import shutil
import sys
from fractions import Fraction
from types import SimpleNamespace

import av
import cv2
import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / name) for name in ('code', 'scripts')]
from ego_capture import exo_calibration as exo, qr_sync


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


def qr_image(seconds):
    image = np.full((480, 640), 255, np.uint8)
    data = f'EGO1:ab0123ef:{round((100+seconds)*1000)}:4'
    code = cv2.QRCodeEncoder_create().encode(data)
    code = cv2.copyMakeBorder(code, 4, 4, 4, 4, cv2.BORDER_CONSTANT, value=255)
    image[110:370, 190:450] = cv2.resize(code, (260, 260), interpolation=cv2.INTER_NEAREST)
    return image


def clock_image(seconds):
    image = np.full((480, 640), 255, np.uint8)
    if seconds < 3.5 or seconds >= 15.:
        return qr_image(seconds)
    else:
        # A schematic object distinguishes the rehearsal's action interval from QR bursts.
        cv2.line(image,(100,365),(540,365),210,2)
        x = 160 + int(np.clip((seconds-5.)/8.,0.,1.)*250)
        height = int(66*np.sin(np.pi*np.clip((seconds-5.)/8.,0.,1.)))
        y = 280-height
        cup = np.int32([[x-34,y],[x+34,y],[x+23,y+79],[x-23,y+79]])
        cv2.fillConvexPoly(image,cup,165)
        cv2.polylines(image,[cup],True,60,2)
        cv2.ellipse(image,(x,y),(34,9),0.,0.,360.,60,2)
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


def make_automatic_fixture(folder):
    """Codes only in the middle of a long recording; EXO 2 has no usable media."""
    cv2.setNumThreads(1)
    duration = 70
    timestamps = np.arange(0, duration, .25)
    blank = np.full((480, 640), 255, np.uint8)
    clocks = [qr_image(t) if 26 <= t < 30 or 40 <= t < 44 else blank for t in timestamps]
    recording(folder, clocks)
    assets = folder / 'exo-live'
    assets.mkdir()
    video(assets / 'exo1-live.mp4', clocks)
    metadata = json.loads((folder/'metadata.json').read_text())
    metadata.update(auto_qr_sync=True, duration_s=duration, evidence='synthetic_software_fixture',
                    object_id='fixture_cup', scene_id='fixture_table', split='test', name='中段二维码演练',
                    capture_sources={'exo':{'exo1':{}, 'exo2':{}}},
                    exo_live={'exo1':{'video':'exo-live/exo1-live.mp4','status':'saved'},
                              'exo2':{'status':'incomplete'}})
    save(folder/'metadata.json', metadata)
    save(folder/'annotations.json', {'reviewed':False})
    derived = folder/'derived'
    save(derived/'quality.json', {'session_id':folder.name, 'stereo_pairs_paired':len(timestamps),
                                  'glove_valid_frames':len(timestamps)})
    signals = np.arange(0, duration, .02)
    np.savez(derived/'glove_signals.npz', time_s=signals, score=np.zeros_like(signals), raw_values=np.zeros((len(signals),1)))
    with (derived/'paired_frames.jsonl').open('w', encoding='utf-8') as output:
        for index, timestamp in enumerate(timestamps):
            output.write(json.dumps(dict(t_s=timestamp+.125, device_ns=70_000_000_000+index*250_000_000,
                image=f'cam0/{index}.jpg', image1=f'cam1/{index}.jpg', stereo_delta_ms=0.))+'\n')


def test_automatic_qr_on_recording_queue(tmp_path):
    import time
    from ego_capture.application import Application
    from workflow_capture_inputs import capture_inputs

    settings = Path(__file__).resolve().parents[1]/'configs'/'ego-capture.example.json'
    config = tmp_path/'capture.json'
    shutil.copyfile(settings, config)
    folder = tmp_path/'recordings'/'sessions'/'auto-qr-test'
    make_automatic_fixture(folder)
    app = Application(config, standalone=True, connect_devices=False)
    # The same queued path runs on stop and resumes already saved work at startup.
    app.dispatch_recording_qc()
    deadline = time.monotonic() + 90
    while app.job['status'] == 'running' and time.monotonic() < deadline:
        time.sleep(.05)
    assert app.job['status'] == 'complete', app.job
    report = app.automatic_alignment(folder)
    assert report['status'] == 'partial' and report['drift_aligned_cameras'] == 3, report
    assert [row['status'] for row in report['outcomes']] == ['offset_and_drift']*3+['host_receipt']
    assert all(25 < model['anchors'][0]['host_mono_s']-100 < 45 for model in report['cameras'])
    assert app.list_sessions()[0]['qr_alignment']['expected_cameras'] == 4
    segment = SimpleNamespace(session=folder, recording_id=folder.name,
                              row={'start_s':26.,'end_s':30.}, identity=lambda:{})
    result = capture_inputs(segment, project=tmp_path)
    assert result['alignment']['cameraAnchor'] == 'cam0_qr_clock'
    assert 'derived/qr-auto-sync.json' in result['files']
    first = result['frames'][0]
    assert abs(first['alignedSessionTimeS'] - (first['time_s'] - .125)) < .04, first
    assert first['tactile']
    # An offset-only report never overrides the original association clock.
    for model in report['cameras']:
        model['mode'] = 'offset_only'
    save(folder/'derived'/qr_sync.AUTO_REPORT, report)
    result = capture_inputs(segment, project=tmp_path)
    assert result['alignment']['cameraAnchor'] == 'cam0_host_receipt'
    app.devices.close()


def test_continuously_visible_clock_estimates_drift():
    rows = [(t, 'ab0123ef', 100. + t*1.001, 2) for t in np.arange(0, 18., .2)]
    model = qr_sync.fit_clock(rows, 'continuous')
    assert model['mode'] == 'offset_and_drift'
    assert abs(model['scale'] - 1.001) < 1e-6


if __name__ == '__main__':
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        test_fixed_exo_2_and_3(Path(folder))
    with tempfile.TemporaryDirectory() as folder:
        test_automatic_qr_on_recording_queue(Path(folder))
    test_continuously_visible_clock_estimates_drift()
    print('PASS: EXO calibration, automatic middle-of-take QR, partial fallback and workflow 01')
